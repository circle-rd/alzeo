"""What the tests of the pair share (keepalive-proxy role 10): two real proxies, each in front of a fake engine
of its own, their identities and certificates, the fake peers and cockpits, and PairFixture, which starts the two
boxes as processes or with the entry in the test's own process.

It was the head of tests/test_proxy_pair.py until 2026-11-10, when that file was cut in four so that CI runs
them side by side: tests/test_proxy_pair.py, tests/test_proxy_pair_inprocess.py,
tests/test_proxy_pair_one_model.py and tests/test_proxy_pair_one_model_inprocess.py. It holds no test.
"""
import atexit
import hashlib
import http.client
import http.server
import importlib.util
import json
import os
import pathlib
import re
import shutil
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import localports  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[1]
KEY_1, KEY_2 = "k-entry-111", "k-peer-222"
TEST_REV = "1a2b3c4d5e" * 4            # the checkpoint every test box says it serves (PAIR_CHECKPOINT, decision 1018)
# What a box says before it cuts a request the other box sent on, which it ends by order (decision 1031)
HOP_ENDED_OPENAI = (b'data: {"id":"keepalive","object":"chat.completion.chunk","created":0,"model":"keepalive",'
                    b'"choices":[],"x_alzeo_ended":"order"}\n\n')
HOP_ENDED_NDJSON = b'{"x_alzeo_ended": "order"}\n'      # on the Ollama routes' NDJSON
PAIR_GRACE_READ_S = 6.0           # the 5 s an entry reads on after its client left (PAIR_ORDER_GRACE_S), and 1


def _until(pred, secs):
    """pred() once it holds, waited for up to secs seconds."""
    deadline = time.monotonic() + secs
    while not pred() and time.monotonic() < deadline:
        time.sleep(0.02)
    return pred()
# What vllm 0.29.0 (the pinned image) answers when its EngineCore died: rendered from its own code on the reference box
# (create_error_response of an EngineDeadError, by starlette's JSONResponse; a stream's event by json.dumps)
VLLM_DEAD_500 = (b'{"error":{"message":"EngineCore encountered an issue. See stack trace (above) for the root cause.",'
                 b'"type":"InternalServerError","param":null,"code":500}}')
VLLM_DEAD_EVENT = json.dumps({"error": {"message": "EngineCore encountered an issue. See stack trace (above) for the "
                                                   "root cause.", "type": "InternalServerError", "param": None,
                                        "code": 500}}).encode()
PAIR_TTL = 2                        # PAIR_TTL_S of the entry: its lists of the peer are read again after it
OPENSSL_CERT = ["-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-days", "36500",
                "-addext", "keyUsage=critical,digitalSignature,keyCertSign"]
_IDS: dict = {}
_IDS_DIR: list = []


def _ids_dir():
    if not _IDS_DIR:
        d = pathlib.Path(tempfile.mkdtemp(prefix="kp-pair-ids-"))
        atexit.register(shutil.rmtree, d, True)
        _IDS_DIR.append(d)
    return _IDS_DIR[0]


def ident(who):
    """(certificate PEM, its sha256, its directory) of a box, made once per run as pair.py makes it."""
    if who not in _IDS:
        d = _ids_dir() / who
        d.mkdir()
        subprocess.run(["openssl", "req", *OPENSSL_CERT, "-subj", f"/CN=alzeo-pair-{who}",
                        "-keyout", str(d / "key.pem"), "-out", str(d / "cert.pem")],
                       check=True, capture_output=True, timeout=120)
        pem = (d / "cert.pem").read_text()
        _IDS[who] = (pem, hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest(), d)
    return _IDS[who]


def child_of(parent):
    """A certificate of a key of its own, signed by `parent`'s key: its chain ends at `parent`'s certificate,
    but it is not that certificate."""
    who = f"child-of-{parent}"
    if who not in _IDS:
        d = _ids_dir() / who
        d.mkdir()
        _, _, pd = ident(parent)
        subprocess.run(["openssl", "req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                        "-subj", f"/CN={who}", "-keyout", str(d / "key.pem"), "-out", str(d / "req.pem")],
                       check=True, capture_output=True, timeout=120)
        subprocess.run(["openssl", "x509", "-req", "-in", str(d / "req.pem"), "-CA", str(pd / "cert.pem"),
                        "-CAkey", str(pd / "key.pem"), "-set_serial", "7", "-days", "30", "-out", str(d / "cert.pem")],
                       check=True, capture_output=True, timeout=120)
        pem = (d / "cert.pem").read_text()
        _IDS[who] = (pem, hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest(), d)
    return who


def future_ident():
    """A box's certificate valid from 2099 only, self-signed and CA:TRUE as pair.py's are: one whose dates this
    machine's clock does not hold (openssl ca, the tool of OpenSSL 3.0 that sets a start date)."""
    who = "future"
    if who not in _IDS:
        d = _ids_dir() / who
        d.mkdir()
        (d / "index.txt").write_text("")
        (d / "serial").write_text("01\n")
        (d / "ca.cnf").write_text(
            "[ ca ]\ndefault_ca = here\n[ here ]\n"
            f"database = {d}/index.txt\nnew_certs_dir = {d}\nserial = {d}/serial\n"
            "default_md = sha256\npolicy = any\nunique_subject = no\nx509_extensions = ext\n"
            "[ any ]\ncommonName = supplied\n"
            "[ ext ]\nbasicConstraints = critical,CA:TRUE\nkeyUsage = critical,digitalSignature,keyCertSign\n")
        subprocess.run(["openssl", "req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                        "-subj", "/CN=alzeo-pair-future", "-keyout", str(d / "key.pem"), "-out", str(d / "req.pem")],
                       check=True, capture_output=True, timeout=120)
        subprocess.run(["openssl", "ca", "-batch", "-selfsign", "-notext", "-config", str(d / "ca.cnf"),
                        "-keyfile", str(d / "key.pem"), "-startdate", "20990101000000Z", "-enddate", "21000101000000Z",
                        "-in", str(d / "req.pem"), "-out", str(d / "cert.pem")],
                       check=True, capture_output=True, timeout=120)
        pem = re.search(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----\n?", (d / "cert.pem").read_text(), re.S).group(0)
        (d / "cert.pem").write_text(pem)
        _IDS[who] = (pem, hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest(), d)
    return who


def install_identity(cfg, who):
    """`who`'s certificate and key as the box's own (cfg/pair/identity), as pair.py leaves them."""
    _, _, d = ident(who)
    (cfg / "pair/identity").mkdir(parents=True, exist_ok=True)
    for f in ("cert.pem", "key.pem"):
        shutil.copy(d / f, cfg / "pair/identity" / f)


def peer_entry(cfg, name, port, who=None, host="127.0.0.1"):
    """A pair.json entry for the peer `name` at https://host:port, the certificate of `who` (by default its
    own) written beside it, as pair.py writes it: a certificate, the pair's only credential (decision 978)."""
    pem, sha, _ = ident(who or name)
    (cfg / "pair").mkdir(exist_ok=True)
    (cfg / f"pair/{name}@t.crt").write_text(pem)
    return {"name": name, "proxy": f"https://{host}:{port}", "cert_file": f"pair/{name}@t.crt", "sha256": sha}


def server_ctx(me, trusts):
    """A pair port's TLS as a fake peer speaks it: `me`'s certificate, one of `trusts`' demanded."""
    c = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    _, _, d = ident(me)
    c.load_cert_chain(d / "cert.pem", d / "key.pem")
    c.verify_mode = ssl.CERT_REQUIRED
    c.load_verify_locations(cadata="".join(ident(t)[0] for t in trusts))
    return c


def client_ctx(me, server):
    """TLS toward a pair port as the box `me` (None: no certificate), `server`'s certificate trusted alone."""
    c = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    c.check_hostname = False
    c.load_verify_locations(cadata=ident(server)[0])
    if me:
        _, _, d = ident(me)
        c.load_cert_chain(d / "cert.pem", d / "key.pem")
    return c


def fake_engine(name, model):
    """A fake SGLang engine that serves `model`, writes down every POST it gets (path, model, the
    key it was given) and answers a generation whole or as an SSE stream."""
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        served = [model]
        seen: list = []
        rids: list = []             # (the body's rid, the x-override-rid header) of every POST
        users: list = []            # the "user" of every POST: which conversation, which turn
        bodies: list = []           # every POST's body, parsed
        gets: list = []             # the path of every GET
        delay = 0.0
        # set by a test's cleanup: every generation its x_delay (or delay) still holds answers now. A request
        # held for the whole test (busy) ends with it, not seconds later (the CI's time, decision 1137)
        release = threading.Event()
        count_delay = 0.0           # seconds the size guard's count waits before its answer
        ordered_now = False         # "vllm_dead_late_order(_cut)": its lane's stop job, seen only once it is set
        mode = "ok"                 # "down": /v1/models answers 503 (a boot) ; "cut": a stream dies mid-way ;
                                    # "busy": every POST answers 503 ; "die": every POST is read, then the
                                    # connection closed without an answer (an engine that died on it) ;
                                    # "vllm_dead_more": a stream whose EngineCore died, more events after it ;
                                    # "vllm_dead_late_order": its death, then ordered_now set, then a clean end ;
                                    # "vllm_dead_late_order_cut": the same, then a cut, no end ;
                                    # "vllm_dead_ordered_then_cut": its death while its stop job runs, the job over
                                    # (ordered_now False) a second later, then a cut ;
                                    # "ndjson_cut": an Ollama stream (/api/chat) cut after its first line ;
                                    # "vllm_dead_after_1s": an event a second after its first, then its death ;
                                    # "slow_10s": five more events two seconds apart, then its end ;
                                    # "ndjson_unended_end": an Ollama stream whose last line, a second after its
                                    # first, has no line end, then its clean end

        def log_message(self, *a):
            pass

        def reply(self, obj, code=200):
            out = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def do_GET(self):
            Handler.gets.append(self.path)
            if self.path.startswith("/v1/models") and Handler.mode == "down":
                return self.reply({"error": "loading"}, 503)
            if self.path.startswith("/v1/models"):
                return self.reply({"object": "list", "data": [{"id": m, "object": "model", "owned_by": "sglang",
                                                               "max_model_len": 262144} for m in Handler.served]})
            if self.path.startswith(("/server_info", "/get_server_info")) and Handler.mode == "nopool":
                return self.reply({"error": "starting"}, 503)      # an engine that just started: no pool to read
            if self.path.startswith(("/server_info", "/get_server_info")):
                return self.reply({"max_total_num_tokens": 900000})
            return self.reply({})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            Handler.seen.append((self.path, body.get("model"), self.headers.get("Authorization"),
                                 self.headers.get("X-Alzeo-Pair-Hop")))
            Handler.rids.append((body.get("rid"), self.headers.get("x-override-rid")))
            if self.path == "/tokenize":            # the size guard's count: two characters a token
                time.sleep(Handler.count_delay)
                if Handler.mode == "count_fails":       # an engine stopping: its count says 503
                    return self.reply({"error": {"message": "stopping", "type": "server_error"}}, 503)
                if Handler.mode == "count_dies":        # an engine that dies on the count: closed, no answer
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                return self.reply({"tokens": [], "count": len(json.dumps(body)) // 2, "max_model_len": 9990})
            Handler.users.append(body.get("user"))
            Handler.bodies.append(body)
            if Handler.mode == "busy":
                return self.reply({"error": {"message": "busy", "type": "server_error"}}, 503)
            if Handler.mode == "die":
                time.sleep(body.get("x_die_after", 0))  # an engine that dies on it, at once or after a while
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            if Handler.mode == "vllm_dead":             # vLLM 0.29.0's EngineCore died: its 500, byte for byte
                time.sleep(body.get("x_dead_after", 0))  # at once, or once a keeper opened the stream
                out = VLLM_DEAD_500
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return
            # float(): a null delay fails at once, as time.sleep did, where wait(None) would wait for good (review)
            Handler.release.wait(float(body.get("x_delay", Handler.delay)))
            if self.path.startswith("/v1/messages"):
                return self.reply({"id": "m", "type": "message", "role": "assistant", "model": Handler.served[0],
                                   "content": [{"type": "text", "text": f"from {name}"}]})
            if Handler.mode == "ndjson_unended_end" and self.path == "/api/chat" and body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                ln = json.dumps({"model": Handler.served[0], "message": {"role": "assistant",
                                 "content": f"{name}-0 "}, "done": False}).encode() + b"\n"
                self.wfile.write(b"%x\r\n%s\r\n" % (len(ln), ln)); self.wfile.flush()
                time.sleep(1.0)
                last = json.dumps({"model": Handler.served[0], "message": {"role": "assistant", "content": ""},
                                   "done": True, "done_reason": "stop"}).encode()      # no line end
                self.wfile.write(b"%x\r\n%s\r\n0\r\n\r\n" % (len(last), last)); self.wfile.flush()
                return
            if Handler.mode == "ndjson_cut" and self.path == "/api/chat" and body.get("stream"):
                self.send_response(200)                     # an Ollama stream whose engine dies in it: cut, no end
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                ln = json.dumps({"model": Handler.served[0], "message": {"role": "assistant",
                                 "content": f"{name}-0 "}, "done": False}).encode() + b"\n"
                self.wfile.write(b"%x\r\n%s\r\n" % (len(ln), ln)); self.wfile.flush()
                time.sleep(0.05)
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            if body.get("stream") and Handler.mode == "tool":   # a tool call naming a watched host (role 8)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                call = {"index": 0, "id": "t1", "type": "function", "function": {
                    "name": "bash", "arguments": json.dumps({"command": "curl https://b.oss-cn.aliyuncs.com/x"})}}
                for ev in ({"choices": [{"index": 0, "delta": {"tool_calls": [call]}}]}, "[DONE]"):
                    part = f"data: {ev if isinstance(ev, str) else json.dumps(ev)}\n\n".encode()
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part)); self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n"); self.wfile.flush()
                return
            if Handler.mode == "slow_body" and not body.get("stream"):   # its answer in two halves, 1.5 s apart
                out = json.dumps({"id": "c", "object": "chat.completion", "model": Handler.served[0], "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": f"from {name}"},
                     "finish_reason": "stop"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out[:len(out) // 2]); self.wfile.flush()
                time.sleep(1.5)
                self.wfile.write(out[len(out) // 2:]); self.wfile.flush()
                return
            if not body.get("stream"):
                return self.reply({"id": "c", "object": "chat.completion", "model": Handler.served[0],
                                   "choices": [{"index": 0, "message": {"role": "assistant", "content": f"from {name}"},
                                                "finish_reason": "stop"}]})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for i in range(3):
                ev = json.dumps({"id": "c", "object": "chat.completion.chunk", "model": Handler.served[0],
                                 "choices": [{"index": 0, "delta": {"content": f"{name}-{i} "}}]})
                part = f"data: {ev}\n\n".encode()
                self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part)); self.wfile.flush()
                time.sleep(0.05)
                if Handler.mode == "slow_10s":          # five events two seconds apart, no death (1112)
                    for _ in range(5):
                        time.sleep(2.0)
                        more = b'data: {"choices":[{"index":0,"delta":{"content":"x"}}]}\n\n'
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(more), more)); self.wfile.flush()
                    self.wfile.write(b"%x\r\n%s\r\n0\r\n\r\n" % (14, b"data: [DONE]\n\n")); self.wfile.flush()
                    return
                if Handler.mode == "vllm_dead_after_1s":    # an event a second on, then its death (1112)
                    time.sleep(1.0)
                    more = b'data: {"choices":[{"index":0,"delta":{"content":"x"}}]}\n\n'
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(more), more)); self.wfile.flush()
                    time.sleep(0.3)
                    part = b"data: " + VLLM_DEAD_EVENT + b"\n\n"
                    self.wfile.write(b"%x\r\n%s\r\n0\r\n\r\n" % (len(part), part)); self.wfile.flush()
                    return
                if Handler.mode == "vllm_dead_stream":  # its EngineCore died during the stream: its error event
                    part = b"data: " + VLLM_DEAD_EVENT + b"\n\n"
                    self.wfile.write(b"%x\r\n%s\r\n0\r\n\r\n" % (len(part), part)); self.wfile.flush()
                    return
                if Handler.mode == "vllm_dead_ordered_then_cut":
                    Handler.ordered_now = True                  # its stop job runs at its death
                    part = b"data: " + VLLM_DEAD_EVENT + b"\n\n"
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part)); self.wfile.flush()
                    time.sleep(1.0)
                    Handler.ordered_now = False                 # the job over, the unit inactive
                    time.sleep(0.3)
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                if Handler.mode in ("vllm_dead_late_order", "vllm_dead_late_order_cut"):
                    part = b"data: " + VLLM_DEAD_EVENT + b"\n\n"   # its death; its stop job seen after it
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part)); self.wfile.flush()
                    time.sleep(0.4)
                    Handler.ordered_now = True
                    time.sleep(0.3)
                    if Handler.mode == "vllm_dead_late_order_cut":  # its server killed: no clean end
                        self.close_connection = True
                        self.connection.shutdown(socket.SHUT_RDWR)
                        return
                    part = b"data: [DONE]\n\n"
                    self.wfile.write(b"%x\r\n%s\r\n0\r\n\r\n" % (len(part), part)); self.wfile.flush()
                    return
                if Handler.mode == "vllm_dead_more":    # the same event, and more after it before the end
                    part = b"data: " + VLLM_DEAD_EVENT + b"\n\n"
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part)); self.wfile.flush()
                    more = b'data: {"choices":[{"index":0,"delta":{"content":"x"}}]}\n\n'
                    for _ in range(40):
                        time.sleep(0.05)
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(more), more)); self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n"); self.wfile.flush()
                    return
                if Handler.mode == "cut" and i == 1:
                    self.connection.shutdown(socket.SHUT_RDWR)      # the engine dies mid-stream
                    self.close_connection = True
                    return
            part = b"data: [DONE]\n\n"
            self.wfile.write(b"%x\r\n%s\r\n0\r\n\r\n" % (len(part), part)); self.wfile.flush()

    return Handler


class Engine(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


GIB = 1 << 30
COCKPIT_STATE = {"machine": {"data": {"mem": {"MemTotal": 121 * GIB, "MemAvailable": 100 * GIB}, "cpu_pct": {"cpu": 12.5},
                                      "disks": {"home": {"total": 4 << 40, "free": 2 << 40}}}},
                 "gpu": {"data": {"temp_c": 51.0, "power_w": 40.5, "procs": []}},
                 "units": {"data": {"units": {"qwen38-flash-local.service": {"active": "active"},
                                              "qwen38-sglang.service": {"active": "inactive"}}}},
                 "job": {"data": {"current": {"action": "switch", "status": "running", "elapsed": 12.0,
                                              "argv": ["sudo", "-n", "/usr/bin/systemctl"], "lines": ["secret log"]}}},
                 "config": {"data": {"version": "1.41.0", "repo_dir": "/home/someone/alzeo"}},
                 "feed": {"data": {"rows": [{"peer": "10.0.0.9:5555", "path": "/v1/messages"}]}}}


class FakeCockpit(http.server.BaseHTTPRequestHandler):
    """A cockpit as the proxy meets it: /api/login with a box's key sets a session cookie, /api/state needs it."""
    protocol_version = "HTTP/1.1"
    keys = {KEY_1, KEY_2}
    logins: list = []
    sessions: set = set()
    states: list = []                       # one per /api/state read

    def log_message(self, *a):
        pass

    def reply(self, code, obj, cookie=None):
        out = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path == "/api/login" and body.get("key") in FakeCockpit.keys:
            tok = f"tok{len(FakeCockpit.logins)}"
            FakeCockpit.logins.append(body["key"])
            FakeCockpit.sessions.add(tok)
            return self.reply(200, {"ok": True}, cookie=f"cockpit={tok}; HttpOnly; SameSite=Strict; Path=/")
        self.reply(403, {"error": "bad key"})

    def do_GET(self):
        if (self.headers.get("Cookie") or "").removeprefix("cockpit=") not in FakeCockpit.sessions:
            return self.reply(401, {"error": "auth"})
        if self.path == "/api/state":
            FakeCockpit.states.append(1)
            return self.reply(200, COCKPIT_STATE)
        self.reply(404, {"error": "not found"})


class LateBox(http.server.BaseHTTPRequestHandler):
    """A box that lists the 27B and reads a request, then: "close", closes the connection 1.5 s later with no answer (its
    power, a restart of its proxy); "cut", writes the head of its answer and cuts it 1.5 s later; "silent" and
    "cut_silent", the same but 8 s later, past the seconds an entry keeps a connection whose client left (1112);
    "away", answers 503 1.5 s later (its engine away); "away_soon", the same once the test sets answer_now, right
    after its client left, well before an entry's watch first looks at its client; "close_soon", "moved_soon",
    "error_soon", "answer_soon" and "cut_soon" then: the connection closed with no answer, a 409 model_not_served, a 500
    that is no engine's death, a whole answer, the head of an answer cut 0.2 s later."""
    protocol_version = "HTTP/1.1"
    how = "close"
    got = []                            # the users of the requests it read
    answer_now = None                   # "away_soon": set by the test when it may answer

    def log_message(self, *a):
        pass

    def do_GET(self):
        out = json.dumps({"object": "list", "data": [{"id": "qwen3.8-27b"}], "x_alzeo_running": 0,
                          "x_alzeo_running_yours": 0, "x_alzeo_revision": TEST_REV}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        self.got.append(body.get("user"))
        if self.how in ("close_soon", "moved_soon", "error_soon", "answer_soon", "cut_soon"):
            self.answer_now.wait(5)
            if self.how == "cut_soon":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "1000")
                self.end_headers()
                self.wfile.write(b'{"id": "c", ')
                self.wfile.flush()
                time.sleep(0.2)
            if self.how in ("close_soon", "cut_soon"):
                try: self.connection.shutdown(socket.SHUT_RDWR)
                except OSError: pass
                self.close_connection = True
                return
            if self.how == "moved_soon":
                code, out = 409, {"error": {"type": "model_not_served", "message": "another model"}}
            elif self.how == "error_soon":
                code, out = 500, {"error": {"type": "server_error", "message": "something else broke"}}
            else:
                code, out = 200, {"id": "c", "object": "chat.completion", "model": "qwen3.8-27b", "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "late"}, "finish_reason": "stop"}]}
            raw = json.dumps(out).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if self.how in ("away", "away_soon"):
            if self.how == "away":
                time.sleep(1.5)
            else:
                self.answer_now.wait(5)
            out = json.dumps({"error": {"type": "engine_unavailable", "message": "away"}}).encode()
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        if self.how.startswith("cut"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "1000")
            self.end_headers()
            self.wfile.write(b'{"id": "c", ')
            self.wfile.flush()
        time.sleep(8.0 if self.how in ("silent", "cut_silent") else 1.5)
        try: self.connection.shutdown(socket.SHUT_RDWR)
        except OSError: pass
        self.close_connection = True


def silent_port(test):
    """A held port with a socket listening on it that takes the connection and never answers its TLS: a box gone without
    a reset. Its number from localports.held, never read out of a socket (test_localports, NoTestFile)."""
    port = localports.held(test)
    hole = socket.socket()
    hole.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)     # beside the socket that holds it, as serve() is
    hole.bind((localports.HOST, port))
    hole.listen(16)
    test.addCleanup(hole.close)
    return port


def tls_peer(test, handler, me="spark2", trusts=("spark1",)):
    """A fake peer's pair port, on a held port: `handler` behind TLS both ways (the handshake in its accept,
    a test's server), `me`'s certificate shown and one of `trusts`' demanded."""
    port = localports.held(test)
    srv = localports.serve(port, Engine, handler)
    srv.socket = server_ctx(me, trusts).wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    test.addCleanup(srv.server_close)
    test.addCleanup(srv.shutdown)
    return port


def sleeper(test, *args):
    """A process whose command line ends with args, as an engine's main process, waited for until /proc shows that
    command: Popen returns before the new image's argv is set, and 158 reads in 300 right after it were empty (the
    delta's review, 2026-11-04), which made the tests that read it fail half the time."""
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", *args])
    test.addCleanup(p.wait)
    test.addCleanup(p.kill)
    want = (args[-1] if args else "time.sleep").encode()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with open(f"/proc/{p.pid}/cmdline", "rb") as f:
                if want in f.read():
                    return p
        except OSError:
            pass
        time.sleep(0.01)
    test.fail("the sleeper's command line never showed")


def fake_peer(listed, post, running=0, slots=None, revision=TEST_REV):
    """The pair port of another box, for a spare entry: it lists `listed` (ids, or None: its list fails with a 503)
    and answers every POST with `post` ((status, body)); all three may change during a test (state). `seen`: its POSTs.
    running: the count its list says (x_alzeo_running, none of them the entry's), None for none, as a box of v1.42.0.
    slots: what it says its lane runs at once (x_alzeo_slots), None for nothing said. revision: the checkpoint it
    says it serves (x_alzeo_revision), None for nothing said."""
    class Peer(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        state = {"listed": listed, "post": post, "running": running}
        seen: list = []
        heard: list = []                    # the checkpoint each POST carried (X-Alzeo-Pair-Checkpoint), or None

        def log_message(self, *a):
            pass

        def reply(self, code, obj):
            out = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def do_GET(self):
            ids = Peer.state["listed"]
            if ids is None:
                return self.reply(503, {"error": {"type": "engine_unavailable", "message": "its engine is away"}})
            listing = {"object": "list", "data": [{"id": i, "object": "model"} for i in ids]}
            if Peer.state["running"] is not None:
                listing["x_alzeo_running"], listing["x_alzeo_running_yours"] = Peer.state["running"], 0
            if slots is not None:
                listing["x_alzeo_slots"] = slots
            if revision is not None:
                listing["x_alzeo_revision"] = revision
            self.reply(200, listing)

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            Peer.seen.append(self.path)
            Peer.heard.append(self.headers.get("X-Alzeo-Pair-Checkpoint"))
            code, obj = Peer.state["post"]
            time.sleep(Peer.state.get("delay", 0))
            if Peer.state.get("cut"):           # its answer cut on the way: fewer bytes than it said
                out = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out) + 100))
                self.end_headers()
                self.wfile.write(out[:10])
                self.wfile.flush()
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            if code == 409:                 # a box that switched lists its new lane from then on
                Peer.state["listed"] = ["ornith-1.5-122b"] + [i for i in (Peer.state["listed"] or []) if i.startswith("marker")]
            self.reply(code, obj)
    return Peer


PEER_ANSWER = (200, {"id": "c", "object": "chat.completion", "model": "qwen3.8-27b", "choices": [
    {"index": 0, "message": {"role": "assistant", "content": "from the peer"}, "finish_reason": "stop"}]})


class PairFixture:
    """Two proxies paired over TLS, each with a fake engine (MODELS: what each serves), and their helpers. The entry
    runs as a process of its own, and in this test's process in each class's twin (INPROC), where the coverage gate
    of CI sees its lines (as test_proxy_hold.py does for role 6)."""
    INPROC = False
    MODELS = (("qwen3.8-27b",), ("qwen3.8-flash-next-fp4",))
    ENTRY_ENV: dict = {}            # more of the entry's environment, for one class

    @classmethod
    def setUpClass(cls):
        cls.E1, cls.E2 = fake_engine("spark1", cls.MODELS[0][0]), fake_engine("spark2", cls.MODELS[1][0])
        cls.E1.served[:], cls.E2.served[:] = list(cls.MODELS[0]), list(cls.MODELS[1])
        cls.engines, cls.procs, cls.inproc = [], [], None
        for h in (cls.E1, cls.E2):
            e = Engine(("127.0.0.1", 0), h)
            threading.Thread(target=e.serve_forever, daemon=True).start()
            cls.engines.append(e)
        cls.tmp = pathlib.Path(tempfile.mkdtemp(prefix="kp-pair-"))
        cockpit = Engine(("127.0.0.1", 0), FakeCockpit)
        threading.Thread(target=cockpit.serve_forever, daemon=True).start()
        cls.engines.append(cockpit)
        cls.cockpit_url = f"http://127.0.0.1:{cockpit.server_address[1]}"
        try:
            # each alone at its start (one port each), then paired as pair.py pairs them
            cls.home2 = cls.home(cls.tmp / "spark2", KEY_2, None, who="spark2")
            cls.port2 = cls.start_proxy(cls.home2, cls.engines[1], "spark2")
            cls.home1 = cls.home(cls.tmp / "spark1", KEY_1, None, who="spark1")
            if cls.INPROC:
                cls.port1 = cls.start_inproc(cls.home1, cls.engines[0])
            else:
                cls.port1 = cls.start_proxy(cls.home1, cls.engines[0], "spark1", PAIR_TTL_S=str(PAIR_TTL),
                                            **cls.ENTRY_ENV)
            cls.pport1, cls.pport2 = cls.pair_up(cls.home1, "spark1", cls.home2, "spark2")
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def start_inproc(cls, home, engine):
        cls.saved_env = dict(os.environ)
        os.environ.update({"PAIR_CHECKPOINT": TEST_REV, **cls.ENTRY_ENV, "HOME": str(home), "PROXY_BIND": "127.0.0.1",
                           "PAIR_TTL_S": str(PAIR_TTL),
                           "PAIR_PORT": "0",
                           "QWEN38_COCKPIT_URL": cls.cockpit_url,
                           "UPSTREAM": f"http://127.0.0.1:{engine.server_address[1]}",
                           "PROMPT_CEILING_TOKENS": "0", "FLASH_PROMPT_CEILING_TOKENS": "0"})
        for k in ("QWEN38_CLIENT_KEYS_FILE", "QWEN38_UPSTREAM_API_KEY", "PROXY_HOLD_UNITS", "QWEN38_PAIR_FILE",
                  "http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
            os.environ.pop(k, None)
        spec = importlib.util.spec_from_file_location(f"kp_pair_{time.time_ns()}", REPO / "keepalive-proxy.py")
        cls.mod = importlib.util.module_from_spec(spec)
        saved_argv, sys.argv = sys.argv, ["keepalive-proxy.py"]
        try:
            spec.loader.exec_module(cls.mod)
        finally:
            sys.argv = saved_argv
        cls.lines = []
        cls.mod.log = lambda msg: cls.lines.append(f"[proxy] {msg}")
        cls.mod.pair_conf()
        cls.inproc = cls.mod.Server(("127.0.0.1", 0), cls.mod.H)
        threading.Thread(target=cls.inproc.serve_forever, daemon=True).start()
        threading.Thread(target=cls.mod.PAIR_PORT_KEEPER.run, daemon=True).start()
        return cls.inproc.server_address[1]

    @staticmethod
    def home(path, key, pair, who=None):
        cfg = path / ".config" / "qwen38"
        cfg.mkdir(parents=True)
        (cfg / "api-key").write_text(key + "\n")
        if who:
            install_identity(cfg, who)
        if pair is not None:
            (cfg / "pair.json").write_text(json.dumps(pair))
        return path

    @classmethod
    def start_proxy(cls, home, engine, label, **extra):
        env = {"PATH": os.environ["PATH"], "HOME": str(home), "PROXY_BIND": "127.0.0.1", "PAIR_PORT": "0",
               "QWEN38_COCKPIT_URL": cls.cockpit_url, "UPSTREAM": f"http://127.0.0.1:{engine.server_address[1]}",
               "PROMPT_CEILING_TOKENS": "0", "FLASH_PROMPT_CEILING_TOKENS": "0", "PAIR_CHECKPOINT": TEST_REV, **extra}
        with open(cls.tmp / f"{label}.journal", "ab") as journal:     # the child keeps its own copy
            proc, port = localports.spawn(lambda port: subprocess.Popen(
                [sys.executable, str(REPO / "keepalive-proxy.py"), str(port)], env=env,
                stdout=subprocess.DEVNULL, stderr=journal))
        cls.procs.append(proc)
        return port

    @classmethod
    def journal_of(cls, label):
        if label == "spark1" and cls.INPROC:
            return "\n".join(cls.lines)
        try:
            return (cls.tmp / f"{label}.journal").read_text(errors="replace")
        except FileNotFoundError:
            return ""

    @classmethod
    def pair_port(cls, label, within=8.0, after=0):
        """The port the proxy `label` opened its pair port on, its PAIR_PORT being 0 (the kernel's choice): its
        journal says it, or the keeper of the proxy in this process. `after`: open lines to skip."""
        deadline = time.monotonic() + within
        while time.monotonic() < deadline:
            if label == "spark1" and cls.INPROC:
                srv = cls.mod.PAIR_PORT_KEEPER.srv
                if srv is not None:
                    return srv.server_address[1]
            else:
                found = re.findall(r"pair: port 127\.0\.0\.1:(\d+) open over TLS", cls.journal_of(label))
                if len(found) > after:
                    return int(found[-1])
            time.sleep(0.1)
        raise RuntimeError(f"{label} opened no pair port within {within:g} s: {cls.journal_of(label)[-800:]}")

    @classmethod
    def reads(cls, label):
        """How many times the proxy `label` read its pair.json: each read leaves a line."""
        return len(re.findall(r"\[proxy\] pair: \S+ (?:with |is not used: )", cls.journal_of(label)))

    @classmethod
    def write_pair(cls, cfg, label, conf):
        """pair.json written whole, one rename as pair.py writes it (a file half written would be read as no
        pair), then the proxy `label` seen reading it: up to about 4 s after the write (review, 2026-11-03)."""
        before = cls.reads(label)
        tmp = cfg / ".pair.json.tmp"
        tmp.write_text(json.dumps(conf))
        os.replace(tmp, cfg / "pair.json")
        deadline = time.monotonic() + 10
        while cls.reads(label) <= before:
            if time.monotonic() > deadline:
                raise AssertionError(f"{label} did not read its pair.json within 10 s: {cls.journal_of(label)[-600:]}")
            time.sleep(0.05)

    @classmethod
    def pair_up(cls, home_a, a, home_b, b):
        """a and b paired as pair.py pairs them, both ways: b first, to a stand-in address, so that its pair port
        opens and a can name it, then a, then b again with a's pair port. (a's pair port, b's)."""
        cfg_a, cfg_b = home_a / ".config/qwen38", home_b / ".config/qwen38"
        cls.write_pair(cfg_b, b, {"name": b, "peers": [peer_entry(cfg_b, a, 9)]})
        pb = cls.pair_port(b)
        cls.write_pair(cfg_a, a, {"name": a, "peers": [peer_entry(cfg_a, b, pb)]})
        pa = cls.pair_port(a)
        cls.write_pair(cfg_b, b, {"name": b, "peers": [peer_entry(cfg_b, a, pa)]})
        return pa, pb

    @classmethod
    def tearDownClass(cls):
        if cls.inproc is not None:
            cls.mod.PAIR_PORT_KEEPER.stop()
            cls.inproc.shutdown(); cls.inproc.server_close()
            os.environ.clear(); os.environ.update(cls.saved_env)
        for p in cls.procs:
            if p.poll() is None:
                p.terminate()
                p.wait(10)
        for e in cls.engines:
            e.shutdown(); e.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.E1.seen.clear(); self.E2.seen.clear(); self.E1.rids.clear(); self.E2.rids.clear()
        self.E1.users.clear(); self.E2.users.clear(); self.E1.bodies.clear(); self.E2.bodies.clear()
        self.E1.served[:], self.E2.served[:] = list(self.MODELS[0]), list(self.MODELS[1])
        self.E1.delay = self.E2.delay = 0.0
        self.E1.count_delay = self.E2.count_delay = 0.0
        self.E1.mode = self.E2.mode = "ok"
        self.E1.ordered_now = self.E2.ordered_now = False
        if getattr(self, "mod", None) is not None:
            getattr(self.mod, "forget_unit_reads", lambda: None)()          # lane_away keeps systemd's answer a second
            # no binding of an earlier test: two left theirs (a request bound at a vLLM death, a hop's engine death in an
            # open stream), and a test that counts every binding failed after either, in an order other than the
            # alphabet's (the 11th review of v1.43.0, 2026-11-05)
            self.mod._KILLERS.clear()
        # where each journal stood: a line asserted is one this test wrote, never one of an earlier test (review,
        # 2026-11-03)
        self.journal_at = {label: len(self.journal_of(label)) for label in ("spark1", "spark2")}

    def call(self, path, body=None, port=None, key=KEY_1, headers=None, method="POST"):
        c = http.client.HTTPConnection("127.0.0.1", port or self.port1, timeout=60)
        try:
            h = {"Content-Type": "application/json", "Authorization": f"Bearer {key}", **(headers or {})}
            c.request(method, path, json.dumps(body).encode() if body is not None else None, h)
            r = c.getresponse()
            return r.status, r.read(), dict(r.getheaders())
        finally:
            c.close()

    def tls_call(self, port, path, body=None, me="spark2", server="spark1", headers=None, method="POST"):
        """A request to a box's pair port as the box `me`: its certificate, no key (decision 978)."""
        c = http.client.HTTPSConnection("127.0.0.1", port, timeout=60, context=client_ctx(me, server))
        try:
            h = {"Content-Type": "application/json", **(headers or {})}
            c.request(method, path, body if isinstance(body, bytes) else json.dumps(body).encode()
                      if body is not None else None, h)
            r = c.getresponse()
            return r.status, r.read(), dict(r.getheaders())
        finally:
            c.close()

    def chat(self, model, **kw):
        return {"model": model, "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}], **kw}

    def journal(self, label):
        if label == "spark1" and self.INPROC:
            time.sleep(0.2)                 # the outcome line is written after the answer's last byte
        return self.journal_of(label)

    def until_in_journal(self, label, text, within=8.0):
        start = getattr(self, "journal_at", {}).get(label, 0)
        deadline = time.monotonic() + within
        while text not in self.journal_of(label)[start:] and time.monotonic() < deadline:
            time.sleep(0.1)
        return text in self.journal_of(label)[start:]

    def assert_said(self, label, text):
        """An outcome line of the journal, waited for: it is written after the answer's last byte, and read at
        once it was not there yet now and then (a run of 2026-11-03)."""
        if not self.until_in_journal(label, text):
            self.fail(f"{text!r} not in the journal of {label}: ...{self.journal_of(label)[-1200:]}")

    def spare_proxy(self, label, key, peers=(), name="spark1", pair=None, who="spark1", upstream=None, extra=None,
                    keyfile=True):
        """A proxy of its own for one test: its pair.json names `peers` ((name, port, key), each with its own
        certificate), or is `pair` as given. Its pair port is a held one it cannot bind: it sends on, nobody
        sends to it, and it listens on one port (localports.spawn)."""
        home = self.home(self.tmp / label, key, None, who=who)
        cfg = home / ".config/qwen38"
        if not keyfile:
            (cfg / "api-key").unlink()
        if peers:
            pair = {"name": name, "peers": [peer_entry(cfg, n, p) for n, p in peers]}
        if callable(pair):
            pair = pair(cfg)
        if pair is not None:
            (cfg / "pair.json").write_text(json.dumps(pair))
        env = {"PATH": os.environ["PATH"], "HOME": str(home), "PROXY_BIND": "127.0.0.1",
               "PAIR_PORT": str(localports.held(self)),
               "UPSTREAM": upstream or f"http://127.0.0.1:{self.engines[0].server_address[1]}",
               "PROMPT_CEILING_TOKENS": "0", "FLASH_PROMPT_CEILING_TOKENS": "0", "PAIR_CHECKPOINT": TEST_REV,
               **(extra or {})}
        with open(self.tmp / f"{label}.journal", "ab") as journal:
            proc, port = localports.spawn(lambda port: subprocess.Popen(
                [sys.executable, str(REPO / "keepalive-proxy.py"), str(port)], env=env,
                stdout=subprocess.DEVNULL, stderr=journal))
        self.procs.append(proc)
        return port

    def repoint(self, port):
        """spark1's pair.json with spark2 at https://127.0.0.1:port, seen read; put back at cleanup."""
        cfg = self.home1 / ".config/qwen38"
        pair = json.loads((cfg / "pair.json").read_text())
        pair["peers"][0]["proxy"] = f"https://127.0.0.1:{port}"
        self.write_pair(cfg, "spark1", pair)

        def back():
            pair["peers"][0]["proxy"] = f"https://127.0.0.1:{self.pport2}"
            self.write_pair(cfg, "spark1", pair)
        self.addCleanup(back)
