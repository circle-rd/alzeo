#!/usr/bin/env python3
"""Two boxes behind one endpoint (keepalive-proxy role 10, discussion #45, decisions 961-976).

Two real proxies, each in front of a fake engine of its own: spark1 serves the 27B, spark2 serves Flash
FP4-Local, and each names the other in its pair.json with the other's key and certificate (a pair is mutual,
decision 973), the two talking over TLS both ways on their pair ports (decision 971). A generation that
names one of this repo's lanes' models goes to the box that serves it; a lane name no box serves is refused
with what each box serves; any other name stays on the box it was sent to, as on a box alone; a peer that
switched since its list was read answers 409 and is never made to serve the wrong model; a request a peer
sends on is never sent on again; GET /v1/models lists both boxes; the peer's key never reaches a journal.
The pair port admits the paired box alone: no certificate, a certificate not pinned, a box taken out of the
pair are refused, and a box never sends to an address that does not present the pinned certificate. A box
without pair.json behaves exactly as before.
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
import select
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error

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

    # ---- routing --------------------------------------------------------------------------------
class TwoBoxesBehindOneEndpoint(PairFixture, unittest.TestCase):
    """Two boxes that serve two models: each request goes to the box that serves its model."""

    def test_a_box_serving_its_own_model_writes_no_line_on_the_other(self):
        # each request of a model this box serves reads the other box's list again every PAIR_TTL_S (v6.51): those
        # reads must not push the other box's real requests out of the lines its cockpit reads (review of v1.43.0)
        listed = lambda: self.journal_of("spark2").count("GET /v1/models ok get")
        before = listed()
        for _ in range(4):
            st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"))
            self.assertEqual(st, 200, out[:300])
            time.sleep(1.5)                 # past PAIR_TTL (2 s) twice: the list is read again
        self.assertEqual(listed(), before, "the other box's list reads wrote lines in its journal")

    def test_a_lane_this_box_serves_stays_here(self):
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"))
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out)
        self.assertEqual([s[1] for s in self.E1.seen], ["qwen3.8-27b"])
        self.assertEqual(self.E2.seen, [])

    def test_a_lane_the_peer_serves_goes_there_with_the_peers_key(self):
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"))
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark2", out)
        self.assertEqual(self.E1.seen, [])
        self.assertEqual(len(self.E2.seen), 1)
        path, model, auth, hop = self.E2.seen[0]
        self.assertEqual((path, model, auth), ("/v1/chat/completions", "qwen3.8-flash-next-fp4", f"Bearer {KEY_2}"))
        self.assertIsNone(hop, "the hop header stops at the peer's proxy, never reaches its engine")

    def test_the_ollama_and_sagemaker_routes_go_to_the_box_that_serves_too(self):
        # they stayed on this box, whose model answered under the other box's name (review, 2026-11-03)
        for route in ("/api/chat", "/api/generate", "/invocations"):
            with self.subTest(route=route):
                st, out, _ = self.call(route, self.chat("qwen3.8-flash-next-fp4"))
                self.assertEqual(st, 200, out[:300])
                self.assertIn(b"from spark2", out)
                self.assertEqual(self.E2.seen[-1][:2], (route, "qwen3.8-flash-next-fp4"))
        self.assertEqual(self.E1.seen, [])

    def test_a_stream_from_the_peer_arrives_whole_and_in_order(self):
        st, out, hdrs = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4", stream=True))
        self.assertEqual(st, 200, out[:300])
        self.assertIn("text/event-stream", hdrs.get("Content-Type", ""))
        text = out.decode()
        self.assertLess(text.index("spark2-0"), text.index("spark2-1"))
        self.assertLess(text.index("spark2-1"), text.index("spark2-2"))
        self.assertTrue(text.rstrip().endswith("data: [DONE]"), text[-200:])

    def test_the_anthropic_dialect_goes_to_the_peer_too(self):
        st, out, _ = self.call("/v1/messages", self.chat("qwen3.8-flash-next-fp4"))
        self.assertEqual(st, 200, out[:300])
        self.assertEqual(json.loads(out)["content"][0]["text"], "from spark2")

    def test_any_other_name_stays_on_this_box_as_on_a_box_alone(self):
        for name in ("claude-haiku-4-5", "gpt-4o", ""):
            st, out, _ = self.call("/v1/chat/completions", self.chat(name))
            self.assertEqual(st, 200, out[:300])
        self.assertEqual(len(self.E1.seen), 3)
        self.assertEqual(self.E2.seen, [])

    def test_either_box_is_an_entry(self):
        # decision 973: a pair is mutual, and a client of spark2 that asks for the 27B gets it from spark1
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"), port=self.port2, key=KEY_2)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out)
        self.assertEqual([s[1:3] for s in self.E1.seen], [("qwen3.8-27b", f"Bearer {KEY_1}")])
        self.assertEqual(self.E2.seen, [])
        st, rows = self.models_once_known(port=self.port2, key=KEY_2)
        self.assertEqual(rows, {"qwen3.8-flash-next-fp4": "spark2", "qwen3.8-27b": "spark1"})

    # ---- a lane no box serves ---------------------------------------------------------------------
    def test_a_lane_no_box_serves_is_answered_here_as_on_a_box_alone(self):
        # decision 986 (2026-11-03): Claude Code, pi and Hermes as installed name lanes that are often not loaded,
        # and a box alone answers any name with its lane; the 404 of 961 made them fail once the boxes were paired
        st, out, _ = self.call("/v1/chat/completions", self.chat("kolibri-1"))
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out)
        self.assertEqual([m for _, m, _, _ in self.E1.seen], ["kolibri-1"])
        self.assertEqual(self.E2.seen, [], "never sent to the other box")
        self.assertTrue(self.until_in_journal("spark1", "no box of the pair serves kolibri-1 now"))

    def test_the_anthropic_dialect_of_a_lane_no_box_serves_is_answered_here_too(self):
        st, out, _ = self.call("/v1/messages", self.chat("ornith-1.5-122b"))
        self.assertEqual(st, 200, out[:300])
        self.assertEqual(json.loads(out)["type"], "message")
        self.assertIn(b"from spark1", out)

    def test_a_peer_that_switched_since_its_list_was_read_never_serves_the_wrong_model(self):
        st, _, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"))
        self.assertEqual(st, 200)
        self.E2.served[:] = ["ornith-1.5-122b"]      # the peer switched; the entry's list is up to 5 s old
        self.E2.seen.clear()
        # the peer's own proxy keeps its engine's names 600 s: a real switch restarts it, here its cache is the
        # stale side, so the peer is given a fresh proxy that reads the new name, with spark2's certificate
        home2b = self.home(self.tmp / "spark2b", KEY_2, None, who="spark2")
        self.start_proxy(home2b, self.engines[1], "spark2b")
        cfg2b = home2b / ".config/qwen38"
        self.write_pair(cfg2b, "spark2b", {"name": "spark2", "peers": [peer_entry(cfg2b, "spark1", self.pport1)]})
        self.repoint(self.pair_port("spark2b"))
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"))
        self.assertEqual(st, 200, out[:300])           # served by no box now: this box's lane answers it (986)
        self.assertIn(b"from spark1", out)
        self.assertEqual(self.E2.seen, [], "the peer's engine is never handed the old model's request")
        st, out, _ = self.call("/v1/chat/completions", self.chat("ornith-1.5-122b"))
        self.assertEqual(st, 200, out[:300])

    def test_a_hop_header_on_the_client_port_is_no_hop(self):
        # a hop is a request that came in on the pair port, from the box of its certificate: the header that
        # said it before let any key holder have a request refused and the journal name a box that sent nothing
        # (found in review, 2026-11-03)
        said = self.journal("spark1").count("sent on by spark9")
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"),
                               headers={"X-Alzeo-Pair-Hop": "spark9"})
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark2", out, "sent on to the box that serves it, as any request")
        self.assertEqual(self.journal("spark1").count("sent on by spark9"), said)

    def test_a_peer_that_does_not_answer_is_said_at_once(self):
        self.repoint(localports.held(self))        # bound, never listening: refused, as a box that is gone
        t0 = time.monotonic()
        st, out, _hdrs = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"))
        self.assertLess(time.monotonic() - t0, 15)
        self.assertEqual(st, 503, out[:300])        # the box that may serve it does not answer: try again
        self.assertEqual(_hdrs.get("Retry-After"), "30", "every 503 of this proxy says when to try again")
        err = json.loads(out)["error"]
        self.assertEqual(err["type"], "engine_unavailable")
        self.assertIn("does not answer", err["served"]["spark2"])
        self.assertIn("nothing listens on its pair port", err["served"]["spark2"])

    # ---- the list, the key, a box alone ---------------------------------------------------------
    def models_once_known(self, port=None, key=KEY_1):
        """GET /v1/models until the peer's list, read in the background, is in it (3 s at most)."""
        deadline = time.monotonic() + 3
        while True:
            st, out, _ = self.call("/v1/models", port=port, key=key, method="GET")
            rows = {r["id"]: r.get("box") for r in json.loads(out)["data"]} if st == 200 else {}
            if len(rows) > 1 or time.monotonic() > deadline:
                return st, rows
            time.sleep(0.2)

    def test_the_models_list_names_both_boxes(self):
        st, rows = self.models_once_known()
        self.assertEqual(st, 200)
        self.assertEqual(rows, {"qwen3.8-27b": "spark1", "qwen3.8-flash-next-fp4": "spark2"})

    def test_the_models_list_never_waits_for_a_peer(self):
        class Frozen(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                time.sleep(20)
        entry = self.spare_proxy("frozen-list", KEY_1, peers=[("spark2", tls_peer(self, Frozen))])
        for _ in range(3):
            t0 = time.monotonic()
            st, out, _ = self.call("/v1/models", port=entry, method="GET")
            self.assertLess(time.monotonic() - t0, 2, "needle.sh and bench-matrix.sh give it 5 s")
            self.assertEqual(st, 200)

    def test_no_secret_reaches_a_journal(self):
        self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"))
        self.call("/v1/chat/completions", self.chat("qwen3.8-27b"), port=self.port2, key=KEY_2)
        for label in ("spark1", "spark2"):
            text = self.journal(label)
            for secret in (KEY_1, KEY_2, "PRIVATE KEY", (ident("spark1")[2] / "key.pem").read_text().splitlines()[1],
                           (ident("spark2")[2] / "key.pem").read_text().splitlines()[1]):
                self.assertNotIn(secret, text, label)
        self.assertIn("-> pair spark2 for qwen3.8-flash-next-fp4", self.journal("spark1"))
        self.assertIn("-> pair spark1 for qwen3.8-27b", self.journal("spark2"))

    def test_no_key_or_cookie_crosses_the_pair(self):
        # the certificate is the pair's only credential (decision 978): the client's key never goes to the other
        # box, nor a cookie either way (cookies know no port: this box's cockpit cookie would have gone)
        class Recording(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            seen: list = []

            def log_message(self, *a):
                pass

            def reply(self, obj):
                out = json.dumps(obj).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie", "session=from-the-other-box")
                self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

            def do_GET(self):
                Recording.seen.append(("GET", {k.lower(): v for k, v in self.headers.items()}))
                self.reply({"object": "list", "data": [{"id": "qwen3.8-flash-next-fp4"}]})

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                Recording.seen.append(("POST", {k.lower(): v for k, v in self.headers.items()}))
                self.reply({"id": "c", "choices": [{"message": {"content": "recorded"}}]})
        entry = self.spare_proxy("recorded", KEY_1, peers=[("spark2", tls_peer(self, Recording))])
        st, out, hdrs = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=entry,
                                  headers={"Cookie": "qwen38_session=this-box-cockpit", "X-Api-Key": KEY_1,
                                           "Proxy-Authorization": "Basic eA=="})
        self.assertEqual(st, 200, out[:300])
        self.assertNotIn("set-cookie", {k.lower() for k in hdrs}, "no cookie of the other box set here")
        self.assertEqual([m for m, _ in Recording.seen].count("POST"), 1)
        for _, h in Recording.seen:
            for name in ("authorization", "x-api-key", "cookie", "proxy-authorization", "x-alzeo-pair-hop"):
                self.assertNotIn(name, h)
            self.assertNotIn(KEY_1, json.dumps(h))

    def test_the_pair_port_takes_the_pairs_routes_only(self):
        for method, path in (("GET", "/server_info"), ("GET", "/get_server_info"), ("POST", "/v1/systemone"),
                             ("POST", "/generate"), ("PUT", "/v1/chat/completions"), ("GET", "/health"),
                             ("POST", "/proxy/stop"), ("GET", "/metrics")):
            with self.subTest(route=f"{method} {path}"):
                st, out, _ = self.tls_call(self.pport1, path, {} if method != "GET" else None, method=method)
                self.assertEqual(st, 403, out[:300])
                self.assertIn("takes the generation routes and GET /v1/models only", json.loads(out)["error"]["message"])
        self.assertEqual(self.E1.seen, [], "none reached the engine")
        st, out, _ = self.tls_call(self.pport1, "/v1/messages/count_tokens", self.chat("qwen3.8-27b"))
        self.assertEqual(st, 200, out[:300])

    def test_a_box_without_pair_json_is_what_it_was(self):
        alone = self.home(self.tmp / "alone", KEY_1, None)
        port = self.start_proxy(alone, self.engines[0], "alone")
        st, out, _ = self.call("/v1/chat/completions", self.chat("kolibri-1"), port=port)
        self.assertEqual(st, 200, out[:300])           # any name reaches the lane that serves, as before
        st, out, _ = self.call("/v1/models", port=port, method="GET")
        self.assertEqual([r.get("box") for r in json.loads(out)["data"]], [None])
        self.assertNotIn("pair: port", self.journal("alone"), "no pair port on a box alone")

    def test_a_pair_json_without_a_peer_is_a_box_alone(self):
        # `pair.py name` alone writes one: a lane name this box does not serve was a 404 (found in review, 2026-11-02)
        named = self.home(self.tmp / "named-only", KEY_1, {"name": "spark1", "peers": []})
        port = self.start_proxy(named, self.engines[0], "named-only")
        st, out, _ = self.call("/v1/chat/completions", self.chat("kolibri-1"), port=port)
        self.assertEqual(st, 200, out[:300])
        self.assertNotIn("pair: port", self.journal("named-only"))

    def test_a_pair_json_that_does_not_parse_leaves_the_box_alone(self):
        broken = self.home(self.tmp / "broken", KEY_1, None)
        (broken / ".config/qwen38/pair.json").write_text('{"peers": [{"name": "x"}]}')
        port = self.start_proxy(broken, self.engines[0], "broken")
        st, out, _ = self.call("/v1/chat/completions", self.chat("kolibri-1"), port=port)
        self.assertEqual(st, 200, out[:300])
        self.assertRegex(self.journal("broken"), r"pair: .*pair\.json is not used: peer 0 needs")

    # ---- the pair port: TLS both ways (decision 971) --------------------------------------------
    def test_a_request_on_the_pair_port_is_a_hop_from_the_box_of_its_certificate(self):
        # no X-Alzeo-Pair-Hop: the certificate says spark2 sent it, so it is served here or refused, never sent on
        st, out, _ = self.tls_call(self.pport1, "/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"))
        self.assertEqual(st, 409, out[:300])
        self.assertEqual(json.loads(out)["error"]["type"], "model_not_served")
        self.assertEqual(self.E1.seen + self.E2.seen, [], "never sent back to spark2, never served by the 27B")
        st, out, _ = self.tls_call(self.pport1, "/v1/chat/completions", self.chat("qwen3.8-27b"))
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out)
        st, out, _ = self.tls_call(self.pport1, "/v1/models", method="GET")
        self.assertEqual([r["id"] for r in json.loads(out)["data"]], ["qwen3.8-27b"], "its own list, not the union")
        self.assert_said("spark1", "sent on by spark2")

    def test_the_other_boxs_rows_and_summary_are_rebuilt_from_their_fields(self):
        # nested 9,990 deep, a row parsed and then made every GET /v1/models here drop when written back, and a
        # summary froze every view of the cockpit (review, 2026-11-03)
        deep = "[" * 9990 + "]" * 9990

        class Deep(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path.startswith("/pair/cockpit/state"):
                    out = ('{"name": "spark2", "models": ["qwen3.8-flash-next-fp4"], "job": {"action": ' + deep
                           + '}, "extra": 1}').encode()
                else:
                    out = ('{"object": "list", "data": [{"id": "qwen3.8-flash-next-fp4", "owned_by": "sglang", '
                           '"nested": ' + deep + '}]}').encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
        port = self.spare_proxy("deep", KEY_1, peers=[("spark2", tls_peer(self, Deep))])
        st, rows = self.models_once_known(port=port)
        self.assertEqual((st, rows.get("qwen3.8-flash-next-fp4")), (200, "spark2"))
        st, out, _ = self.call("/v1/models", port=port, method="GET")
        row = [r for r in json.loads(out)["data"] if r["id"] == "qwen3.8-flash-next-fp4"][0]
        self.assertEqual(set(row), {"id", "owned_by", "box"}, "its scalar fields alone")
        st, out, _ = self.call("/proxy/pair/spark2/state", port=port, method="GET")
        self.assertEqual(st, 200, out[:200])
        s = json.loads(out)
        self.assertEqual((s["models"], s["job"]["action"], "extra" in s), (["qwen3.8-flash-next-fp4"], None, False))

    def test_an_ndjson_stream_from_the_other_box_arrives_as_it_comes(self):
        # the Ollama routes stream NDJSON, and the entry read every answer but SSE whole (review, 2026-11-03)
        class Ollama(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                out = json.dumps({"object": "list", "data": [{"id": "qwen3.8-flash-next-fp4"}]}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(200); self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Transfer-Encoding", "chunked"); self.end_headers()
                for line, pause in ((b'{"message": {"content": "one"}, "done": false}\n', 2.0),
                                    (b'{"message": {"content": "two"}, "done": true}\n', 0.0)):
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(line), line)); self.wfile.flush()
                    time.sleep(pause)
                self.wfile.write(b"0\r\n\r\n"); self.wfile.flush()
        port = self.spare_proxy("ndjson", KEY_1, peers=[("spark2", tls_peer(self, Ollama))])
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        self.addCleanup(c.close)
        t0 = time.monotonic()
        c.request("POST", "/api/chat", json.dumps(self.chat("qwen3.8-flash-next-fp4", stream=True)).encode(),
                  {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
        r = c.getresponse()
        first = r.readline()
        self.assertIn(b'"one"', first)
        self.assertLess(time.monotonic() - t0, 1.5, "its first line before its end")
        self.assertIn(b'"two"', r.read())

    def test_an_ndjson_stream_from_the_other_box_waits_out_a_long_prefill(self):
        # NDJSON carries no keepalive: the entry bounded its silence as an SSE stream's, and the long prefill of an Ollama
        # stream on the other box was cut and bound 10 minutes (the 5th review of v1.43.0, 2026-11-04)
        class SlowOllama(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                out = json.dumps({"object": "list", "data": [{"id": "qwen3.8-flash-next-fp4"}]}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(200); self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Transfer-Encoding", "chunked"); self.end_headers()
                time.sleep(3.0)                                                 # its prefill, past the 2 s set below
                for line in (b'{"message": {"content": "one"}, "done": false}\n',
                             b'{"message": {"content": "two"}, "done": true}\n'):
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(line), line)); self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n"); self.wfile.flush()
        port = self.spare_proxy("ndjson-prefill", KEY_1, peers=[("spark2", tls_peer(self, SlowOllama))],
                                extra={"PAIR_STREAM_SILENCE_S": "2"})
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        self.addCleanup(c.close)
        c.request("POST", "/api/chat", json.dumps(self.chat("qwen3.8-flash-next-fp4", stream=True)).encode(),
                  {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
        r = c.getresponse()
        try:
            body = r.read()
        except http.client.IncompleteRead as e:
            body = e.partial
        self.assertIn(b'"two"', body, "cut during its prefill")

    def test_the_other_boxs_end_by_order_line_never_reaches_an_ollama_client(self):
        # decision 1031's line on the Ollama routes is the entry's to read: relayed, ollama-python's ChatResponse(**part)
        # failed on a line with no message (the 4th review of v1.43.0, 2026-11-04)
        class EndsByOrder(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                out = json.dumps({"object": "list", "data": [{"id": "qwen3.8-flash-next-fp4"}]}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(200); self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Transfer-Encoding", "chunked"); self.end_headers()
                for line in (b'{"message": {"content": "one"}, "done": false}\n', HOP_ENDED_NDJSON):
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(line), line)); self.wfile.flush()
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)                      # cut, not ended
        port = self.spare_proxy("ndjson-ended", KEY_1, peers=[("spark2", tls_peer(self, EndsByOrder))])
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        self.addCleanup(c.close)
        c.request("POST", "/api/chat", json.dumps(self.chat("qwen3.8-flash-next-fp4", stream=True)).encode(),
                  {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
        r = c.getresponse()
        got = b""
        try:
            while True:
                b = r.read1(65536)
                if not b:
                    break
                got += b
        except (http.client.HTTPException, OSError):
            pass                                                                # cut, not ended
        self.assertIn(b'"one"', got)
        self.assertNotIn(b"x_alzeo_ended", got, "the other box's line reached the Ollama client")
        self.assert_said("ndjson-ended", "ended it by order, nothing is bound")

    def test_the_other_box_never_names_the_engines_requests_here(self):
        # SGLang aborts every request whose rid starts with one it aborts: the other box naming rids "0" to "f",
        # in a body or in the header, and leaving, ended every generation here (review, 2026-11-03)
        for path in ("/v1/completions", "/v1/chat/completions", "/invocations"):
            with self.subTest(path=path):
                st, out, _ = self.tls_call(self.pport1, path, dict(self.chat("qwen3.8-27b"), rid="0"),
                                           headers={"X-Override-Rid": "0"})
                self.assertEqual(st, 200, out[:300])
                rid, header = self.E1.rids[-1]
                self.assertIsNone(rid, "no rid of the other box's in the body")
                self.assertNotEqual(header, "0", "nor in the header")
                if path == "/v1/chat/completions":
                    self.assertRegex(header or "", r"^[0-9a-f]{32}$", "this box names it")
        # a body in UTF-16, whose bytes show none of its keys, is refused unread (security review, 2026-11-03)
        st, out, _ = self.tls_call(self.pport1, "/v1/completions",
                                   json.dumps(dict(self.chat("qwen3.8-27b"), rid="0")).encode("utf-16"))
        self.assertEqual(st, 415, out[:300])
        # a key written with escapes, which the bytes do not show, and /v1/responses' request_id (security review,
        # 2026-11-03)
        for path, raw, key in (("/v1/completions", b'{"model": "qwen3.8-27b", "prompt": "hi", "r\\u0069d": "0"}', "rid"),
                               ("/v1/responses", b'{"model": "qwen3.8-27b", "input": "hi", "request_id": "0"}',
                                "request_id")):
            with self.subTest(path=path, body=raw):
                st, out, _ = self.tls_call(self.pport1, path, raw)
                self.assertEqual(st, 200, out[:300])
                self.assertNotIn(key, self.E1.bodies[-1], f"the other box's id reached the engine: {self.E1.bodies[-1]}")

    def test_the_cockpits_stop_is_never_taken_from_the_pair_port(self):
        # the other box's connection may come from loopback (a tunnel's end, these tests): POST /proxy/stop is
        # this machine's operator's, never a pair's
        st, out, _ = self.tls_call(self.pport1, "/proxy/stop", {"why": "a pair's stop", "kind": "operator"})
        self.assertEqual(st, 403, out[:300])
        self.assert_said("spark1", "403 pair route refused")

    def test_the_pair_port_refuses_a_box_without_a_pinned_certificate(self):
        for me in (None, "rogue"):
            with self.subTest(me=me), self.assertRaises((ssl.SSLError, ConnectionResetError, BrokenPipeError,
                                                         http.client.RemoteDisconnected)):
                self.tls_call(self.pport1, "/v1/chat/completions", self.chat("qwen3.8-27b"), me=me)
        self.assertEqual(self.E1.seen, [], "not a byte of a request read")
        self.assertRegex(self.journal("spark1"), r"pair port: TLS handshake from 127\.0\.0\.1 failed \(")

    def test_a_certificate_that_chains_to_a_pinned_one_is_not_it(self):
        # the chain ends at spark2's certificate, but the certificate is another: the pin itself is checked
        with self.assertRaises((ssl.SSLError, ConnectionResetError, BrokenPipeError, http.client.RemoteDisconnected)):
            self.tls_call(self.pport1, "/v1/chat/completions", self.chat("qwen3.8-27b"), me=child_of("spark2"))
        self.assertEqual(self.E1.seen, [])
        self.assertIn("REFUSED: its certificate is no peer's of this box's pair", self.journal("spark1"))

    def test_an_entry_never_sends_to_a_box_without_the_pinned_certificate(self):
        class Rogue(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            seen: list = []

            def log_message(self, *a):
                pass

            def do_GET(self):
                Rogue.seen.append((self.path, self.headers.get("Authorization")))
                self.send_response(200); self.send_header("Content-Length", "0"); self.end_headers()

            do_POST = do_GET
        entry = self.spare_proxy("rogue-peer", KEY_1, peers=[("spark2", tls_peer(self, Rogue, me="rogue"))])
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=entry)
        self.assertEqual(st, 503, out[:300])
        self.assertIn("another certificate than the one pinned", json.loads(out)["error"]["served"]["spark2"])
        self.assertEqual(Rogue.seen, [], "neither a request nor the key reaches it")

    def test_a_box_taken_out_of_the_pair_is_refused_at_its_next_connection(self):
        # every answer of this proxy ends its connection (Connection: close), so a box taken out of the pair meets
        # the handshake again at its next request; the check of a connection still open is in ThePairsPieces
        st, _, _ = self.tls_call(self.pport1, "/v1/chat/completions", self.chat("qwen3.8-27b"))
        self.assertEqual(st, 200)
        cfg1 = self.home1 / ".config/qwen38"
        kept = json.loads((cfg1 / "pair.json").read_text())
        # spark2 out, another box in its place: spark1 stays paired, its pair port open
        self.write_pair(cfg1, "spark1", {"name": "spark1", "peers": [peer_entry(cfg1, "spark3", 9, who="rogue")]})
        self.addCleanup(self.write_pair, cfg1, "spark1", kept)
        with self.assertRaises((ssl.SSLError, ConnectionResetError, BrokenPipeError, http.client.RemoteDisconnected)):
            self.tls_call(self.pport1, "/v1/chat/completions", self.chat("qwen3.8-27b"))
        self.assertEqual(len(self.E1.seen), 1, "the second request never reached the engine")

    def test_the_pair_port_opens_with_a_pair_and_closes_without(self):
        home = self.home(self.tmp / "opens", KEY_1, None, who="spark1")
        self.start_proxy(home, self.engines[0], "opens")            # alone: no pair port
        proc = self.procs[-1]
        self.assertNotIn("pair: port", self.journal("opens"))
        cfg = home / ".config/qwen38"
        self.write_pair(cfg, "opens", {"name": "spark1", "peers": [peer_entry(cfg, "spark2", self.pport2)]})
        port = self.pair_port("opens", within=5)
        self.assertIn(port, localports._ports_of(proc.pid))
        st, out, _ = self.tls_call(port, "/v1/models", method="GET")  # spark2's certificate gets past
        self.assertEqual(st, 200, out[:300])
        (cfg / "pair.json").unlink()
        self.assertTrue(self.until_in_journal("opens", f"pair: port {port} closed: this box has no pair now", 8))
        # what it listens on, read from /proc: no connection to a port number another program may hold now
        self.assertNotIn(port, localports._ports_of(proc.pid))

    def test_a_certificate_that_is_not_the_one_pinned_leaves_the_box_alone(self):
        for label, said in (("pin-off", "is not the certificate pair.json pins"),
                            ("no-identity", "has no certificate of its own")):
            with self.subTest(label=label):
                home = self.home(self.tmp / label, KEY_1, None, who="spark1")
                cfg = home / ".config/qwen38"
                e = peer_entry(cfg, "spark2", self.pport2)
                if label == "pin-off":
                    e["sha256"] = ident("rogue")[1]
                else:
                    shutil.rmtree(cfg / "pair/identity")
                (cfg / "pair.json").write_text(json.dumps({"name": "spark1", "peers": [e]}))
                port = self.start_proxy(home, self.engines[0], label)  # a pair it does not take: one port
                st, out, _ = self.call("/v1/chat/completions", self.chat("kolibri-1"), port=port)
                self.assertEqual(st, 200, out[:300])                # a box alone: any name reaches its lane
                self.assertIn("pair.json is not used: ", self.journal(label))
                self.assertIn(said, self.journal(label))
                self.assertEqual(self.E2.seen, [])

    def test_an_entrys_client_that_leaves_ends_the_request_on_the_other_box(self):
        # through TLS the other box sees the entry's connection end (no peek was made through TLS before: its
        # engine generated to the end for nobody), the answer not streamed or streamed
        for stream in (False, True):
            with self.subTest(stream=stream):
                self.E2.delay = 8.0 + PAIR_GRACE_READ_S
                gone = self.journal("spark2").count("CLIENT GONE")
                c = http.client.HTTPConnection("127.0.0.1", self.port1, timeout=60)
                t0 = time.monotonic()
                c.request("POST", "/v1/chat/completions",
                          json.dumps(self.chat("qwen3.8-flash-next-fp4", stream=stream)).encode(),
                          {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
                time.sleep(1.5)
                c.close()                           # the client leaves before the answer
                # once the seconds the entry reads on for a death are over (decision 1112)
                deadline = time.monotonic() + 5.5 + PAIR_GRACE_READ_S
                while self.journal("spark2").count("CLIENT GONE") <= gone and time.monotonic() < deadline:
                    time.sleep(0.1)
                self.assertGreater(self.journal("spark2").count("CLIENT GONE"), gone, self.journal("spark2")[-600:])
                self.assertLess(time.monotonic() - t0, 7.5 + PAIR_GRACE_READ_S, "seen before its engine answered")

    def test_an_answer_past_the_cap_is_refused(self):
        # an answer not streamed was read whole: a box gone wrong could fill this one's memory (review, 2026-11-03)
        class Huge(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def reply(self, out):
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

            def do_GET(self):
                self.reply(json.dumps({"object": "list", "data": [{"id": "qwen3.8-flash-next-fp4"}]}).encode())

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.reply(b'{"pad": "' + b"x" * 20000 + b'"}')
        port = self.spare_proxy("huge", KEY_1, peers=[("spark2", tls_peer(self, Huge))], extra={"MAX_BODY_BYTES": "8192"})
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=port)
        self.assertEqual(st, 502, out[:300])
        self.assertEqual(json.loads(out)["error"]["type"], "upstream_too_large")
        self.assert_said("huge", "502 pair answer too large")

    def test_an_error_answer_is_relayed_whole_up_to_the_cap_and_refused_past_it(self):
        # an error answer over 1 MiB went out cut to 1 MiB as if whole: a validation error that echoes a long
        # prompt does that (review, 2026-11-03)
        class BigError(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def reply(self, code, out):
                self.send_response(code); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

            def do_GET(self):
                self.reply(200, json.dumps({"object": "list", "data": [{"id": "qwen3.8-flash-next-fp4"}]}).encode())

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.reply(400, json.dumps({"error": {"message": "x" * 1_300_000}}).encode())
        port = self.spare_proxy("big-error", KEY_1, peers=[("spark2", tls_peer(self, BigError))])
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=port)
        self.assertEqual(st, 400, out[:300])
        self.assertEqual(len(json.loads(out)["error"]["message"]), 1_300_000, "the whole answer")
        port = self.spare_proxy("big-error-capped", KEY_1, peers=[("spark2", tls_peer(self, BigError))],
                                extra={"MAX_BODY_BYTES": "8192"})
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=port)
        self.assertEqual(st, 502, out[:300])
        self.assertEqual(json.loads(out)["error"]["type"], "upstream_too_large")

    def test_an_error_answer_the_other_box_cuts_is_said_cut(self):
        # its error answer cut on the way escaped the relay before (review, 2026-11-03)
        class CutError(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                out = json.dumps({"object": "list", "data": [{"id": "qwen3.8-flash-next-fp4"}]}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(500); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "400"); self.end_headers()
                self.wfile.write(b'{"error": "half'); self.wfile.flush()
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
        port = self.spare_proxy("cut-error", KEY_1, peers=[("spark2", tls_peer(self, CutError))])
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=port)
        self.assertEqual(st, 502, out[:300])
        self.assertEqual(json.loads(out)["error"]["type"], "upstream_cut")
        self.assert_said("cut-error", "UPSTREAM CUT pair")

    def test_an_answer_the_other_box_cuts_is_said_cut(self):
        # a 200 whose body ended before its Content-Length went to the client whole, half a document as a
        # complete one: http.client's read(n) says nothing of a body cut short (found by the tests, 2026-11-03)
        class CutAnswer(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                out = json.dumps({"object": "list", "data": [{"id": "qwen3.8-flash-next-fp4"}]}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "400"); self.end_headers()
                self.wfile.write(b'{"choices": [{"message": {"content": "half'); self.wfile.flush()
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
        port = self.spare_proxy("cut-answer", KEY_1, peers=[("spark2", tls_peer(self, CutAnswer))])
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=port)
        self.assertEqual(st, 502, out[:300])
        self.assertEqual(json.loads(out)["error"]["type"], "upstream_cut")
        self.assert_said("cut-answer", "UPSTREAM CUT pair")

    def test_a_certificate_whose_dates_this_clock_does_not_hold_still_pairs(self):
        # a new certificate is valid from the second it is made: two boxes whose clocks disagree could not pair
        # (review, 2026-11-03); the pin is the certificate itself, its dates add nothing
        class Lists(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                out = json.dumps({"object": "list", "data": [{"id": "qwen3.8-flash-next-fp4"}]}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

            do_POST = do_GET
        later = future_ident()
        port = self.spare_proxy("clock", KEY_1, pair=lambda cfg: {"name": "spark1", "peers": [
            peer_entry(cfg, "spark2", tls_peer(self, Lists, me=later), who=later)]})
        st, out, _ = self.call("/v1/models", port=port, method="GET")
        deadline = time.monotonic() + 3
        while b"qwen3.8-flash-next-fp4" not in out and time.monotonic() < deadline:
            time.sleep(0.2)
            st, out, _ = self.call("/v1/models", port=port, method="GET")
        self.assertIn(b"qwen3.8-flash-next-fp4", out, "the other box's list read: its certificate taken")

    def test_the_tile_reads_the_other_box_through_the_pair(self):
        # decision 975: this box's cockpit asks its own proxy; the other box's proxy sums its own cockpit up, logged
        # in with its own key, never relayed whole
        FakeCockpit.logins.clear()
        time.sleep(2.2)                     # past the summary another test may have had kept (PAIR_SUMMARY_KEEP_S)
        FakeCockpit.states.clear()
        lines = (self.journal("spark1").count("pair/spark2/state"), self.journal("spark2").count("/pair/cockpit/state"))
        for _ in range(2):
            st, out, _ = self.call("/proxy/pair/spark2/state", method="GET")
            self.assertEqual(st, 200, out[:300])
        # a read comes every 5 s on both boxes: its lines pushed the real requests out of the 800 the cockpit reads
        # in 34 minutes (review, 2026-11-03)
        time.sleep(0.3)
        self.assertEqual((self.journal("spark1").count("pair/spark2/state"),
                          self.journal("spark2").count("/pair/cockpit/state")), lines, "a read of the tile writes no line")
        s = json.loads(out)
        self.assertEqual(s["name"], "spark2")
        self.assertEqual(s["models"], ["qwen3.8-flash-next-fp4"])
        self.assertEqual((s["mem_total"], s["mem_available"], s["cpu_pct"]), (121 * GIB, 100 * GIB, 12.5))
        self.assertEqual((s["gpu_temp_c"], s["gpu_power_w"], s["version"]), (51.0, 40.5, "1.41.0"))
        self.assertEqual(s["job"], {"action": "switch", "status": "running", "elapsed": 12.0})
        for kept_home in ("feed", "repo_dir", "argv", "secret log", "10.0.0.9", "disks"):
            self.assertNotIn(kept_home, out.decode(), "its feed, paths and logs stay on the other box")
        self.assertEqual(FakeCockpit.logins, [KEY_2], "logged in once, with that box's own key, its session kept")
        # the other box could ask for it as fast as it liked, each time a read of this cockpit (review, 2026-11-03)
        self.assertEqual(len(FakeCockpit.states), 1, "two reads within PAIR_SUMMARY_KEEP_S: one read of the cockpit")

    def test_the_tiles_route_is_this_machines_alone(self):
        st, out, _ = self.tls_call(self.pport1, "/proxy/pair/spark2/state", method="GET")
        self.assertEqual(st, 403, "never from the other box: " + out[:200].decode(errors="replace"))
        st, out, _ = self.call("/proxy/pair/spark2/state", method="GET", key="not-this-box")
        self.assertEqual(st, 401, out[:300])
        st, out, _ = self.call("/proxy/pair/spark9/state", method="GET")
        self.assertEqual(st, 404, out[:300])
        self.assertIn("names no box of this box's pair", json.loads(out)["error"]["message"])
        st, out, _ = self.tls_call(self.pport1, "/pair/cockpit/state", method="GET")
        self.assertEqual(st, 200, "the summary itself is the other box's to read")

    def test_a_peer_whose_engine_restarts_gets_a_503_to_try_again_not_a_404(self):
        self.E2.mode = "down"
        try:
            time.sleep(PAIR_TTL + 0.5)                   # the entry's list of the peer is read again
            st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"))
            self.assertEqual(st, 503, out[:300])
            self.assertEqual(json.loads(out)["error"]["type"], "engine_unavailable")
            self.assertEqual(self.E2.seen, [])
        finally:
            self.E2.mode = "ok"
            time.sleep(PAIR_TTL + 0.5)

    def test_a_box_never_read_holds_no_lane_past_its_first_read(self):
        # the entry restarted while the other box stays off (a power cut): every lane it does not load got a 503
        # for as long as that box was off (review, 2026-11-03); past its first read, it holds nothing up (986)
        port = self.spare_proxy("first-read", KEY_1, peers=[("spark2", localports.held(self))],
                                extra={"PAIR_FIRST_READ_S": "1"})
        time.sleep(1.5)
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=port)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out)

    def test_a_box_that_went_off_holds_its_lanes_for_a_while_then_none(self):
        class Going(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            up = True

            def log_message(self, *a):
                pass

            def reply(self, code, obj):
                out = json.dumps(obj).encode()
                self.send_response(code); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

            def do_GET(self):
                if not Going.up:
                    return self.reply(503, {"error": "its lane is loading"})
                self.reply(200, {"object": "list", "data": [{"id": "qwen3.8-flash-next-fp4"}]})

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.reply(200, {"id": "c", "choices": [{"message": {"content": "from going"}}]})
        port = self.spare_proxy("known-wait", KEY_1, peers=[("spark2", tls_peer(self, Going))],
                                extra={"PAIR_KNOWN_WAIT_S": "3", "PAIR_TTL_S": "0.5"})
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=port)
        self.assertIn(b"from going", out)
        Going.up = False
        time.sleep(1)
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=port)
        self.assertEqual(st, 503, out[:300])           # it served it at its last good read: try again
        time.sleep(3.5)
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=port)
        self.assertEqual(st, 200, out[:300])           # off for longer than a switch: this box's lane answers it
        self.assertIn(b"from spark1", out)

    def test_a_box_that_lists_a_model_and_refuses_it_twice_gets_a_503_never_this_boxs_answer(self):
        # docs/pair.md promises that a box never answers the other box's model with its own: a 409 twice from a
        # box whose list says it serves it fell through to this box's lane (review, 2026-11-03)
        class Lagging(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            posts: list = []

            def log_message(self, *a):
                pass

            def reply(self, code, obj):
                out = json.dumps(obj).encode()
                self.send_response(code); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

            def do_GET(self):
                self.reply(200, {"object": "list", "data": [{"id": "qwen3.8-flash-next-fp4"}]})

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                Lagging.posts.append(1)
                self.reply(409, {"error": {"type": "model_not_served", "message": "another model here now"}})
        port = self.spare_proxy("twice-409", KEY_1, peers=[("spark2", tls_peer(self, Lagging))])
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=port)
        self.assertEqual(st, 503, out[:300])
        self.assertIn("refused it twice", json.loads(out)["error"]["message"])
        self.assertNotIn(b"from spark1", out)
        self.assertEqual(len(Lagging.posts), 2)

    def test_a_lane_the_box_that_does_not_answer_never_served_is_answered_here(self):
        # decision 986 holds while the other box is off: only a model it served at its last good read waits
        # for it (503 above); a client set up for a lane loaded nowhere is answered here, as on a box alone
        self.E2.mode = "down"
        try:
            time.sleep(PAIR_TTL + 0.5)
            st, out, _ = self.call("/v1/chat/completions", self.chat("kolibri-1"))
            self.assertEqual(st, 200, out[:300])
            self.assertIn(b"from spark1", out)
            self.assertEqual(self.E2.seen, [])
        finally:
            self.E2.mode = "ok"
            time.sleep(PAIR_TTL + 0.5)

    def test_a_stream_the_peer_cuts_reaches_the_client_cut_never_ended(self):
        self.E2.mode = "cut"
        c = http.client.HTTPConnection("127.0.0.1", self.port1, timeout=60)
        try:
            c.request("POST", "/v1/chat/completions", json.dumps(self.chat("qwen3.8-flash-next-fp4", stream=True)).encode(),
                      {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
            r = c.getresponse()
            self.assertEqual(r.status, 200)
            with self.assertRaises(http.client.IncompleteRead):   # no closing chunk: the client sees a cut
                r.read()
        finally:
            c.close()
        self.assert_said("spark1", "UPSTREAM CUT pair")

    def test_a_request_sent_on_to_a_box_whose_engine_restarts_is_told_to_try_again_not_held(self):
        # a box whose engine does not answer while its unit starts again (a switch): its own clients are held for
        # whatever lane comes back (role 6), but a request the other box sent on for one model is told at once to
        # try again, since the lane that comes back may serve another
        t = self.tmp / "restarting"
        (t / "bin").mkdir(parents=True)
        (t / "bin/systemctl").write_text(
            "#!/bin/sh\n"
            'case "$*" in *InvocationID*) echo inv-1; echo; exit 0;; esac\n'
            'if [ "$1" = show ]; then echo activating; exit 0; fi\n'
            'if [ "$1" = list-jobs ]; then exit 0; fi\n'
            "exit 3\n")
        (t / "bin/systemctl").chmod(0o755)
        home = self.home(t / "home", KEY_2, None, who="spark2")
        gone = localports.held(self)                      # its engine: nothing listens there
        env = {"PATH": f"{t / 'bin'}:{os.environ['PATH']}", "HOME": str(home), "PROXY_BIND": "127.0.0.1",
               "PAIR_PORT": "0", "UPSTREAM": f"http://127.0.0.1:{gone}", "PROMPT_CEILING_TOKENS": "0",
               "FLASH_PROMPT_CEILING_TOKENS": "0", "PROXY_HOLD_UNITS": "qwen38-x.service", "PROXY_HOLD_MAX_S": "20",
               "PROXY_HOLD_POLL_S": "0.2"}
        with open(self.tmp / "restarting.journal", "ab") as journal:
            proc, _port = localports.spawn(lambda port: subprocess.Popen(
                [sys.executable, str(REPO / "keepalive-proxy.py"), str(port)], env=env,
                stdout=subprocess.DEVNULL, stderr=journal))
        self.procs.append(proc)
        cfg = home / ".config/qwen38"
        self.write_pair(cfg, "restarting", {"name": "spark2", "peers": [peer_entry(cfg, "spark1", 9)]})
        t0 = time.monotonic()
        st, out, _ = self.tls_call(self.pair_port("restarting"), "/v1/chat/completions", self.chat("qwen3.8-27b"),
                                   me="spark1", server="spark2")
        self.assertLess(time.monotonic() - t0, 12, "not held up to PROXY_HOLD_MAX_S")
        self.assertEqual(st, 503, out[:300])
        self.assertIn("try again", json.loads(out)["error"]["message"])

    def test_a_peer_that_takes_the_connection_and_never_answers_holds_nobody(self):
        class Frozen(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                time.sleep(40)                      # a proxy that hangs: the connection is taken, no answer

            do_POST = do_GET
        self.repoint(tls_peer(self, Frozen))
        t0 = time.monotonic()
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"))
        self.assertLess(time.monotonic() - t0, 20, "two reads of its list, 5 s each, then the answer")
        self.assertEqual(st, 503, out[:300])
        t0 = time.monotonic()
        st, out, _ = self.call("/v1/models", method="GET")
        self.assertLess(time.monotonic() - t0, 12)
        self.assertEqual([r["id"] for r in json.loads(out)["data"]], ["qwen3.8-27b"])

    def test_two_boxes_that_name_each_other_ask_each_other_once(self):
        a = self.home(self.tmp / "sym-a", KEY_1, None, who="sym-a")
        b = self.home(self.tmp / "sym-b", KEY_2, None, who="sym-b")
        # an engine of its own each, whose list reads say what each box asked: the other box's reads write no line
        # since v1.43.0, so the journals counted nothing for sym-b, and a loop would have gone unseen (review,
        # 2026-11-03)
        ha, hb = fake_engine("sym-a", "qwen3.8-27b"), fake_engine("sym-b", "qwen3.8-flash-next-fp4")
        engines = [Engine(("127.0.0.1", 0), h) for h in (ha, hb)]
        for e in engines:
            threading.Thread(target=e.serve_forever, daemon=True).start()
            self.addCleanup(e.server_close)
            self.addCleanup(e.shutdown)
        pa = self.start_proxy(a, engines[0], "sym-a")
        self.start_proxy(b, engines[1], "sym-b")
        self.pair_up(a, "sym-a", b, "sym-b")
        st, rows = self.models_once_known(port=pa)
        self.assertEqual(st, 200)
        self.assertEqual(rows, {"qwen3.8-27b": "sym-a", "qwen3.8-flash-next-fp4": "sym-b"})
        reads = {"sym-a": ha, "sym-b": hb}
        count = lambda label: sum(1 for p in list(reads[label].gets) if p.startswith("/v1/models"))
        # each box reads the other's list once when the pair loads, and under load that read can land after the
        # client's last GET (the single-process CI step, 2026-11-03): the counts settle first; a loop never would
        before, still_since, deadline = None, None, time.monotonic() + 15
        while time.monotonic() < deadline:
            now = (count("sym-a"), count("sym-b"))
            if now != before:
                before, still_since = now, time.monotonic()
            elif time.monotonic() - still_since >= 1.5:
                break
            time.sleep(0.25)
        time.sleep(2)                       # a loop would go on asking with nobody calling
        self.assertEqual((count("sym-a"), count("sym-b")), before, "no GET goes on once the client stopped")
        self.assertLessEqual(before[1], before[0], "each GET of the client reads the peer's own list once at most")
        st, out, _ = self.call("/v1/chat/completions", self.chat("kolibri-1"), port=pa)
        self.assertEqual(st, 200, out[:300])           # served by no box: sym-a's own lane answers it (986)
        st, out, _ = self.tls_call(self.pair_port("sym-b"), "/v1/models", me="sym-a", server="sym-b", method="GET")
        self.assertEqual([r["id"] for r in json.loads(out)["data"]], ["qwen3.8-flash-next-fp4"],
                         "a box asked over the pair says what it serves itself, not the union it shows its clients")

    # ---- what the code review of 2026-11-02 found -------------------------------------------------
    def test_a_box_that_cannot_read_its_own_key_sends_nothing_on(self):
        port = self.spare_proxy("no-key", KEY_1, peers=[("spark2", self.pport2)], keyfile=False)
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=port, key="anything")
        self.assertEqual(st, 503, out[:300])
        self.assertEqual(self.E2.seen, [], "the peer is never sent a request this box could not check")

    def test_a_pair_json_with_a_bad_port_or_name_leaves_the_box_alone(self):
        for label, pair in (("bad-port", lambda cfg: {"name": "spark1", "peers": [
                                dict(peer_entry(cfg, "spark2", 1), proxy="https://127.0.0.1:3000l")]}),
                            ("http-peer", lambda cfg: {"name": "spark1", "peers": [
                                dict(peer_entry(cfg, "spark2", 1), proxy=f"http://127.0.0.1:{self.port2}")]}),
                            ("bad-name", lambda cfg: {"name": "Spark \u2460 bureau", "peers": []})):
            with self.subTest(label=label):
                port = self.spare_proxy(label, KEY_1, pair=pair)
                st, out, _ = self.call("/v1/models", port=port, method="GET")
                self.assertEqual(st, 200, out[:300])
                self.assertEqual([r.get("box") for r in json.loads(out)["data"]], [None], "a box alone")
                self.assertIn("pair.json is not used", self.journal(label))
        self.assertIn("proxy is https://host:port", self.journal("http-peer"), "never in clear between two boxes")

    def test_an_entry_whose_engine_does_not_answer_says_so_on_v1_models(self):
        port = self.spare_proxy("entry-down", KEY_1, peers=[("spark2", self.pport2)],
                                upstream=f"http://127.0.0.1:{localports.held(self)}")
        st, out, _ = self.call("/v1/models", port=port, method="GET")
        self.assertEqual(st, 503, "as a box alone: no peer's model stands in for this box's engine")

    def test_no_proxy_of_the_environment_gets_the_peers_key(self):
        dead = f"http://127.0.0.1:{localports.held(self)}"
        port = self.spare_proxy("with-proxy-env", KEY_1, peers=[("spark2", self.pport2)],
                                extra={"http_proxy": dead, "HTTP_PROXY": dead, "https_proxy": dead, "HTTPS_PROXY": dead})
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=port)
        self.assertEqual(st, 200, out[:300])           # through the dead proxy it would have failed
        self.assertIn(b"from spark2", out)

    def test_a_peer_that_answers_409_has_every_list_read_again(self):
        class Switched(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            models = ["qwen3.8-flash-next-fp4"]
            posts: list = []

            def log_message(self, *a):
                pass

            def reply(self, obj, code=200):
                out = json.dumps(obj).encode()
                self.send_response(code); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

            def do_GET(self):
                self.reply({"object": "list", "data": [{"id": m, "object": "model"} for m in Switched.models]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                Switched.posts.append(body.get("model"))
                if body.get("model") not in Switched.models:
                    return self.reply({"error": {"type": "model_not_served", "message": "another model here"}}, 409)
                self.reply({"id": "c", "choices": [{"message": {"content": "from the switched peer"}}]})
        port = self.spare_proxy("switch-409", KEY_1, peers=[("spark2", tls_peer(self, Switched))],
                                extra={"PAIR_TTL_S": "300"})
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=port)
        self.assertEqual(st, 200, out[:300])          # its list is read and kept 300 s
        Switched.models = ["ornith-1.5-122b"]         # the peer switches; the entry's list is the old one
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=port)
        self.assertEqual(st, 200, out[:300])          # its 409 had every list read again: served by no box now,
        self.assertIn(b"from spark1", out)            # so this box's lane answers it, as on a box alone (986)
        self.assertEqual(Switched.posts, ["qwen3.8-flash-next-fp4", "qwen3.8-flash-next-fp4"])
        self.assertIn("serves another model now; the lists are read again", self.journal("switch-409"))
        st, out, _ = self.call("/v1/chat/completions", self.chat("ornith-1.5-122b"), port=port)
        self.assertEqual(st, 200, out[:300])

    def test_a_silent_peer_stream_reaches_the_client_cut(self):
        class Silent(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                out = json.dumps({"object": "list", "data": [{"id": "qwen3.8-flash-next-fp4"}]}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(200); self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked"); self.end_headers()
                part = b'data: {"choices": [{"delta": {"content": "x"}}]}\n\n'
                self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part)); self.wfile.flush()
                time.sleep(30)                      # the box went: no byte, no FIN
        port = self.spare_proxy("silent", KEY_1, peers=[("spark2", tls_peer(self, Silent))],
                                extra={"PAIR_STREAM_SILENCE_S": "2"})
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        try:
            t0 = time.monotonic()
            c.request("POST", "/v1/chat/completions", json.dumps(self.chat("qwen3.8-flash-next-fp4", stream=True)).encode(),
                      {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
            r = c.getresponse()
            with self.assertRaises(http.client.IncompleteRead):
                r.read()
            self.assertLess(time.monotonic() - t0, 10)
        finally:
            c.close()

    def test_a_request_held_through_a_restart_is_never_answered_by_another_lane(self):
        t = self.tmp / "held"
        (t / "bin").mkdir(parents=True)
        state = t / "state"
        state.write_text("activating\n")
        (t / "bin/systemctl").write_text(
            "#!/bin/sh\n"
            'case "$*" in *InvocationID*) echo inv-1; echo; exit 0;; esac\n'
            f'if [ "$1" = show ]; then cat "{state}"; exit 0; fi\n'
            'if [ "$1" = list-jobs ]; then exit 0; fi\n'
            "exit 3\n")
        (t / "bin/systemctl").chmod(0o755)
        engine_port = localports.held(self)
        # a box of a pair (its peer gone): a pair.json without a peer is a box alone now
        port = self.spare_proxy("held-box", KEY_2, peers=[("spark1", localports.held(self))], name="spark2",
                                who="spark2", upstream=f"http://127.0.0.1:{engine_port}",
                                extra={"PATH": f"{t / 'bin'}:{os.environ['PATH']}", "PROXY_HOLD_UNITS": "qwen38-x.service",
                                       "PROXY_HOLD_MAX_S": "30", "PROXY_HOLD_POLL_S": "0.2"})
        Ornith = fake_engine("ornith-box", "ornith-1.5-122b")
        def come_back():
            time.sleep(1.5)
            e = localports.serve(engine_port, Engine, Ornith)    # the lane that comes back serves Ornith
            self.addCleanup(e.server_close); self.addCleanup(e.shutdown)
            e.serve_forever()
        threading.Thread(target=come_back, daemon=True).start()
        st, out, hdrs = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=port, key=KEY_2)
        self.assertEqual(st, 503, out[:300])             # to try again: sent again, it goes where Flash is served
        self.assertEqual(hdrs.get("Retry-After"), "30")
        self.assertIn("came back serving ornith-1.5-122b", json.loads(out)["error"]["message"])
        self.assertEqual(Ornith.seen, [], "Ornith is never asked a request for Flash")


# The same cases with the entry in this process: what CI's coverage floor measures. Each is declared again
# rather than only inherited, as in test_proxy_hold.py: CI counts the tests a file declares against the tests
# that ran, and an inherited case runs without being declared.
class TwoBoxesBehindOneEndpointInProcess(TwoBoxesBehindOneEndpoint):
    INPROC = True

    def test_a_box_serving_its_own_model_writes_no_line_on_the_other(self):
        super().test_a_box_serving_its_own_model_writes_no_line_on_the_other()

    def test_a_lane_this_box_serves_stays_here(self):
        super().test_a_lane_this_box_serves_stays_here()

    def test_a_lane_the_peer_serves_goes_there_with_the_peers_key(self):
        super().test_a_lane_the_peer_serves_goes_there_with_the_peers_key()

    def test_the_ollama_and_sagemaker_routes_go_to_the_box_that_serves_too(self):
        super().test_the_ollama_and_sagemaker_routes_go_to_the_box_that_serves_too()

    def test_a_stream_from_the_peer_arrives_whole_and_in_order(self):
        super().test_a_stream_from_the_peer_arrives_whole_and_in_order()

    def test_the_anthropic_dialect_goes_to_the_peer_too(self):
        super().test_the_anthropic_dialect_goes_to_the_peer_too()

    def test_any_other_name_stays_on_this_box_as_on_a_box_alone(self):
        super().test_any_other_name_stays_on_this_box_as_on_a_box_alone()

    def test_either_box_is_an_entry(self):
        super().test_either_box_is_an_entry()

    def test_a_lane_no_box_serves_is_answered_here_as_on_a_box_alone(self):
        super().test_a_lane_no_box_serves_is_answered_here_as_on_a_box_alone()

    def test_the_anthropic_dialect_of_a_lane_no_box_serves_is_answered_here_too(self):
        super().test_the_anthropic_dialect_of_a_lane_no_box_serves_is_answered_here_too()

    def test_a_peer_that_switched_since_its_list_was_read_never_serves_the_wrong_model(self):
        super().test_a_peer_that_switched_since_its_list_was_read_never_serves_the_wrong_model()

    def test_a_hop_header_on_the_client_port_is_no_hop(self):
        super().test_a_hop_header_on_the_client_port_is_no_hop()

    def test_a_peer_that_does_not_answer_is_said_at_once(self):
        super().test_a_peer_that_does_not_answer_is_said_at_once()

    def test_the_models_list_names_both_boxes(self):
        super().test_the_models_list_names_both_boxes()

    def test_the_models_list_never_waits_for_a_peer(self):
        super().test_the_models_list_never_waits_for_a_peer()

    def test_no_secret_reaches_a_journal(self):
        super().test_no_secret_reaches_a_journal()

    def test_no_key_or_cookie_crosses_the_pair(self):
        super().test_no_key_or_cookie_crosses_the_pair()

    def test_the_pair_port_takes_the_pairs_routes_only(self):
        super().test_the_pair_port_takes_the_pairs_routes_only()

    def test_a_box_without_pair_json_is_what_it_was(self):
        super().test_a_box_without_pair_json_is_what_it_was()

    def test_a_pair_json_without_a_peer_is_a_box_alone(self):
        super().test_a_pair_json_without_a_peer_is_a_box_alone()

    def test_a_pair_json_that_does_not_parse_leaves_the_box_alone(self):
        super().test_a_pair_json_that_does_not_parse_leaves_the_box_alone()

    def test_a_request_on_the_pair_port_is_a_hop_from_the_box_of_its_certificate(self):
        super().test_a_request_on_the_pair_port_is_a_hop_from_the_box_of_its_certificate()

    def test_the_other_boxs_rows_and_summary_are_rebuilt_from_their_fields(self):
        super().test_the_other_boxs_rows_and_summary_are_rebuilt_from_their_fields()

    def test_an_ndjson_stream_from_the_other_box_arrives_as_it_comes(self):
        super().test_an_ndjson_stream_from_the_other_box_arrives_as_it_comes()

    def test_the_other_boxs_end_by_order_line_never_reaches_an_ollama_client(self):
        super().test_the_other_boxs_end_by_order_line_never_reaches_an_ollama_client()

    def test_an_ndjson_stream_from_the_other_box_waits_out_a_long_prefill(self):
        super().test_an_ndjson_stream_from_the_other_box_waits_out_a_long_prefill()

    def test_the_other_box_never_names_the_engines_requests_here(self):
        super().test_the_other_box_never_names_the_engines_requests_here()

    def test_the_cockpits_stop_is_never_taken_from_the_pair_port(self):
        super().test_the_cockpits_stop_is_never_taken_from_the_pair_port()

    def test_the_pair_port_refuses_a_box_without_a_pinned_certificate(self):
        super().test_the_pair_port_refuses_a_box_without_a_pinned_certificate()

    def test_a_certificate_that_chains_to_a_pinned_one_is_not_it(self):
        super().test_a_certificate_that_chains_to_a_pinned_one_is_not_it()

    def test_an_entry_never_sends_to_a_box_without_the_pinned_certificate(self):
        super().test_an_entry_never_sends_to_a_box_without_the_pinned_certificate()

    def test_a_box_taken_out_of_the_pair_is_refused_at_its_next_connection(self):
        super().test_a_box_taken_out_of_the_pair_is_refused_at_its_next_connection()

    def test_the_pair_port_opens_with_a_pair_and_closes_without(self):
        super().test_the_pair_port_opens_with_a_pair_and_closes_without()

    def test_a_certificate_that_is_not_the_one_pinned_leaves_the_box_alone(self):
        super().test_a_certificate_that_is_not_the_one_pinned_leaves_the_box_alone()

    def test_an_entrys_client_that_leaves_ends_the_request_on_the_other_box(self):
        super().test_an_entrys_client_that_leaves_ends_the_request_on_the_other_box()

    def test_an_answer_past_the_cap_is_refused(self):
        super().test_an_answer_past_the_cap_is_refused()

    def test_an_error_answer_is_relayed_whole_up_to_the_cap_and_refused_past_it(self):
        super().test_an_error_answer_is_relayed_whole_up_to_the_cap_and_refused_past_it()

    def test_an_error_answer_the_other_box_cuts_is_said_cut(self):
        super().test_an_error_answer_the_other_box_cuts_is_said_cut()

    def test_an_answer_the_other_box_cuts_is_said_cut(self):
        super().test_an_answer_the_other_box_cuts_is_said_cut()

    def test_a_certificate_whose_dates_this_clock_does_not_hold_still_pairs(self):
        super().test_a_certificate_whose_dates_this_clock_does_not_hold_still_pairs()

    def test_the_tile_reads_the_other_box_through_the_pair(self):
        super().test_the_tile_reads_the_other_box_through_the_pair()

    def test_the_tiles_route_is_this_machines_alone(self):
        super().test_the_tiles_route_is_this_machines_alone()

    def test_a_peer_whose_engine_restarts_gets_a_503_to_try_again_not_a_404(self):
        super().test_a_peer_whose_engine_restarts_gets_a_503_to_try_again_not_a_404()

    def test_a_box_never_read_holds_no_lane_past_its_first_read(self):
        super().test_a_box_never_read_holds_no_lane_past_its_first_read()

    def test_a_box_that_went_off_holds_its_lanes_for_a_while_then_none(self):
        super().test_a_box_that_went_off_holds_its_lanes_for_a_while_then_none()

    def test_a_box_that_lists_a_model_and_refuses_it_twice_gets_a_503_never_this_boxs_answer(self):
        super().test_a_box_that_lists_a_model_and_refuses_it_twice_gets_a_503_never_this_boxs_answer()

    def test_a_lane_the_box_that_does_not_answer_never_served_is_answered_here(self):
        super().test_a_lane_the_box_that_does_not_answer_never_served_is_answered_here()

    def test_a_stream_the_peer_cuts_reaches_the_client_cut_never_ended(self):
        super().test_a_stream_the_peer_cuts_reaches_the_client_cut_never_ended()

    def test_a_request_sent_on_to_a_box_whose_engine_restarts_is_told_to_try_again_not_held(self):
        super().test_a_request_sent_on_to_a_box_whose_engine_restarts_is_told_to_try_again_not_held()

    def test_a_peer_that_takes_the_connection_and_never_answers_holds_nobody(self):
        super().test_a_peer_that_takes_the_connection_and_never_answers_holds_nobody()

    def test_two_boxes_that_name_each_other_ask_each_other_once(self):
        super().test_two_boxes_that_name_each_other_ask_each_other_once()

    def test_a_box_that_cannot_read_its_own_key_sends_nothing_on(self):
        super().test_a_box_that_cannot_read_its_own_key_sends_nothing_on()

    def test_a_pair_json_with_a_bad_port_or_name_leaves_the_box_alone(self):
        super().test_a_pair_json_with_a_bad_port_or_name_leaves_the_box_alone()

    def test_an_entry_whose_engine_does_not_answer_says_so_on_v1_models(self):
        super().test_an_entry_whose_engine_does_not_answer_says_so_on_v1_models()

    def test_no_proxy_of_the_environment_gets_the_peers_key(self):
        super().test_no_proxy_of_the_environment_gets_the_peers_key()

    def test_a_peer_that_answers_409_has_every_list_read_again(self):
        super().test_a_peer_that_answers_409_has_every_list_read_again()

    def test_a_silent_peer_stream_reaches_the_client_cut(self):
        super().test_a_silent_peer_stream_reaches_the_client_cut()

    def test_a_request_held_through_a_restart_is_never_answered_by_another_lane(self):
        super().test_a_request_held_through_a_restart_is_never_answered_by_another_lane()


class TwoBoxesServeOneModel(PairFixture, unittest.TestCase):
    """Role 10, a model both boxes serve (decisions 996 to 998 of 2026-11-03): a conversation stays on the box that
    holds its prefix, a new one goes to the box with the fewest requests in flight, boxes far apart share even a
    conversation, and a box that refuses hands its request back to the entry before any byte of an answer. Both
    serve the 27B from their start, as two boxes that loaded it do (a proxy learns what its engine serves at a
    lane's start); spark2 lists a marker beside it, which shows in the entry's list once it read spark2's."""
    MODELS = (("qwen3.8-27b",), ("qwen3.8-27b", "marker-27b"))
    ENTRY_ENV = {"PAIR_SPREAD_COOL_S": "2"}     # a refusal's pause, short: the tests after it share again

    def setUp(self):
        super().setUp()
        if self.INPROC:
            # no pause an earlier test left: once busy() ended a test's generations with it, a pause that test caused
            # still ran as the next one began (1.07 s of it after
            # test_a_box_gone_without_a_reset_holds_a_shared_request_seconds_not_ten, review of v1.44.3)
            self.mod._PAIR_COOL.clear()
        self.until_listed("marker-27b")


    def until_listed(self, model, within=15.0, port=None, gone=False):
        """Until the entry's own list shows model (or no longer does): the union of its list and what it kept of the
        other box's."""
        deadline = time.monotonic() + within
        while time.monotonic() < deadline:
            st, out, _ = self.call("/v1/models", method="GET", port=port)
            if st == 200 and (model in [m.get("id") for m in json.loads(out).get("data", [])]) != gone:
                return
            time.sleep(0.2)
        self.fail(f"the entry's list never {'lost' if gone else 'showed'} {model}")

    def turns(self, tag, n, size=9000, turn=1500):
        """n turns of one conversation, as an agent sends them: each body is the one before plus a turn."""
        msgs = [{"role": "system", "content": (tag + " ") * (size // (len(tag) + 1))}]
        out = []
        for i in range(n):
            msgs = msgs + [{"role": "user", "content": f"turn {i} " + "x" * turn}]
            out.append({"model": "qwen3.8-27b", "max_tokens": 8, "messages": list(msgs), "user": f"{tag}-{i}"})
            msgs = msgs + [{"role": "assistant", "content": "ok " * 100}]
        return out

    def sent(self, port=None):
        """What the entry's tile says it sent spark2, and why."""
        st, out, _ = self.call("/proxy/pair/spark2/state", method="GET", port=port)
        self.assertIn(st, (200, 503), out[:300])
        return json.loads(out).get("sent")

    @staticmethod
    def moved(before, after):
        """The counts that changed, by how much."""
        return {k: after[k] - before.get(k, 0) for k in after if after[k] != before.get(k, 0)}

    def busy(self, engine, port, key, model, n, secs):
        """n requests of secs each, sent to the box behind port by its own client: its generations in flight, until
        its engine has them all."""
        threads = [threading.Thread(target=self.call, args=("/v1/chat/completions", {
            "model": model, "max_tokens": 8, "messages": [{"role": "user", "content": "busy"}], "x_delay": secs,
            "user": f"busy-{i}"}), kwargs={"port": port, "key": key}, daemon=True) for i in range(n)]
        for t in threads:
            t.start()

        def ended():
            # the test is over: its generations answer now, and the next test starts once they are in, as
            # it did when they ran their whole secs (no assertion reads them past the test)
            engine.release.set()
            try:
                for t in threads:
                    t.join(30)
            finally:
                engine.release.clear()
        self.addCleanup(ended)
        deadline = time.monotonic() + 10
        while sum(1 for u in engine.users if u and u.startswith("busy-")) < n:
            if time.monotonic() > deadline:
                self.fail(f"the engine has {engine.users} of {n} busy requests")
            time.sleep(0.05)
        return threads

    def test_a_conversation_stays_on_the_box_that_served_it(self):
        a, b = self.turns("conv-a", 2), self.turns("conv-b", 1)
        # spark1 runs 2, spark2 none: a new conversation goes to spark2
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 2, 3.0)
        st, out, _ = self.call("/v1/chat/completions", a[0])
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark2", out)
        self.assert_said("spark1", "-> pair spark2 for qwen3.8-27b")
        # spark2 runs 2 and says so, spark1 none: the conversation's next turn still goes to spark2, whose cache
        # holds all of it but the new turn, and a new conversation goes to spark1
        for t in held:
            t.join(30)
        self.busy(self.E2, self.port2, KEY_2, "marker-27b", 2, 6.0)
        self.E2.served[:] = ["qwen3.8-27b", "marker-busy"]
        self.until_listed("marker-busy")        # read after its 2 went in: the entry knows it runs 2
        st, out, _ = self.call("/v1/chat/completions", a[1])
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark2", out, "the turn left the box that holds its conversation")
        st, out, _ = self.call("/v1/chat/completions", b[0])
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out, "a new conversation went to the busier box")

    def test_a_long_prompt_a_warming_engine_cannot_take_goes_to_the_other_box(self):
        # this box's engine just started, its pool not measured yet: a long prompt waited for it (503), where the other
        # box's is measured (review, 2026-11-03)
        if not self.INPROC:
            self.skipTest("this box's pool is dropped in this test's process")
        self.E1.mode = "nopool"
        self.mod.invalidate_pool()
        body = dict(self.chat("qwen3.8-27b"), messages=[{"role": "user", "content": "w" * 300000}])
        st, out, _ = self.call("/v1/chat/completions", body)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark2", out, "a long prompt waited for this box's warming engine")
        self.assert_said("spark1", "this box's engine is warming up for qwen3.8-27b: trying spark2")

    def test_a_prompt_too_long_for_this_boxs_pool_goes_to_the_other_box(self):
        # a 64 GB box paired with a 128 GB one: what the small pool refuses, the big one serves (review, 2026-11-03)
        if not self.INPROC:
            self.skipTest("this box's pool is set in this test's process")
        self.mod._POOL.update(tokens=10000, window=9990, ts=time.time())
        self.addCleanup(self.mod._POOL.update, tokens=None, window=None, ts=0.0)
        body = dict(self.chat("qwen3.8-27b"), messages=[{"role": "user", "content": "w " * 30000}])
        st, out, _ = self.call("/v1/chat/completions", body)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark2", out, "this box refused as too long what the other box's pool holds")
        self.assert_said("spark1", "the prompt is too long for this box's pool for qwen3.8-27b: trying spark2")

    def test_a_box_gone_without_a_reset_holds_a_shared_request_seconds_not_ten(self):
        # its power or its cable gone: its port answers no TLS. A request this box could serve itself waited the whole
        # connection timeout (10 s) for it (review, 2026-11-03)
        if not self.INPROC:
            self.skipTest("the kept list is set in this test's process")
        self.repoint(silent_port(self))
        self.mod._PAIR_MODELS["spark2"] = {"ts": time.monotonic(), "models": ("qwen3.8-27b",), "rows": [],
                                           "error": None, "answered": True, "known": ("qwen3.8-27b",),
                                           "known_ts": time.monotonic(), "read_s": 0.01, "running": 0,
                                           "revision": TEST_REV}         # the same checkpoint: it is shared (1018)
        self.busy(self.E1, self.port1, KEY_1, "local-only", 1, 9.0)         # one here, none there: it goes there
        t0 = time.monotonic()
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"))
        took = time.monotonic() - t0
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out)
        self.assertLess(took, 6.0, f"a shared request waited {took:.1f} s for a box gone without a reset")

    def test_a_client_that_leaves_while_the_other_box_does_not_answer_still_pauses_it(self):
        # its client gave up during the connection wait to a box gone without a reset: that box was neither paused nor
        # forgotten, and each retry waited the 3 s again (review, 2026-11-03)
        if not self.INPROC:
            self.skipTest("the kept list is set in this test's process")
        self.repoint(silent_port(self))
        self.mod._PAIR_MODELS["spark2"] = {"ts": time.monotonic(), "models": ("qwen3.8-27b",), "rows": [],
                                           "error": None, "answered": True, "known": ("qwen3.8-27b",),
                                           "known_ts": time.monotonic(), "read_s": 0.01, "running": 0,
                                           "revision": TEST_REV}
        self.addCleanup(self.mod._PAIR_COOL.pop, "spark2", None)
        self.busy(self.E1, self.port1, KEY_1, "local-only", 1, 9.0)         # one here, none there: it goes there
        c = http.client.HTTPConnection("127.0.0.1", self.port1, timeout=1.0)    # a client that gives up after 1 s
        try:
            c.request("POST", "/v1/chat/completions", json.dumps(self.chat("qwen3.8-27b")).encode(),
                      {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
            with self.assertRaises(OSError):
                c.getresponse()
        finally:
            c.close()
        self.assertTrue(self.until_in_journal("spark1", "its client left, and spark2 gets nothing of a model both "
                                                        "serve", within=6.0), "the box that did not answer was not paused")
        t0 = time.monotonic()
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"))
        took = time.monotonic() - t0
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out)
        self.assertLess(took, 2.0, f"the next request waited {took:.1f} s again for the box that does not answer")

    def test_a_refusal_served_here_is_counted_once_this_box_served_it(self):
        # the tile counted "served here" before this box tried: a request this box then refused too was said served
        # here (review, 2026-11-03)
        Peer = fake_peer(["qwen3.8-27b", "marker-count-here"], (503, {"error": {"type": "engine_unavailable",
                                                                                 "message": "away"}}))
        entry = self.spare_proxy("spark1-count", KEY_1, peers=(("spark2", tls_peer(self, Peer)),),
                                 extra={"PAIR_SPREAD_COOL_S": "0"})
        self.until_listed("marker-count-here", port=entry)
        held = self.busy(self.E1, entry, KEY_1, "local-only", 1, 4.0)       # one here, none there: it goes there
        before = self.sent(entry)
        self.E1.mode = "busy"                                               # and this box's engine says 503 too
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"), port=entry)
        self.assertNotEqual(st, 200, out[:300])
        self.assertEqual(self.moved(before, self.sent(entry)), {}, "a request no box served was counted served here")
        self.E1.mode = "ok"
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"), port=entry)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out)
        self.assertEqual(self.moved(before, self.sent(entry)), {"failover": 1})
        for th in held:
            th.join(30)

    def test_a_box_held_by_its_memory_floor_sends_on_what_the_other_box_serves(self):
        # the cockpit's floor stopped a generation here: new ones wait here (v6.47), but the other box serves the model:
        # it answers, where this box said 503 or 529 for the whole hold (review, 2026-11-03)
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 1, 6.0)        # a generation the stop cuts
        st, out, _ = self.call("/proxy/stop", {"why": "test floor", "hold_s": 3})
        self.assertEqual(st, 200, out[:300])
        self.assertEqual(json.loads(out).get("stopped"), 1, out[:300])
        before = self.sent()
        for path in ("/v1/chat/completions", "/v1/messages"):
            with self.subTest(path=path):
                st, out, _ = self.call(path, self.chat("qwen3.8-27b"))
                self.assertEqual(st, 200, out[:300])
                self.assertIn(b"from spark2", out, "this box refused for memory what the other box serves")
        # sent there at the decision, this box held by its floor being no box to keep them: the take-over passed the
        # test alone when the decision kept them here (review, 2026-11-03). Counted as what this box could not serve,
        # never "to even the load"
        self.assertEqual(self.moved(before, self.sent()), {"rescued": 2})
        self.assertFalse(self.until_in_journal("spark1", "this box is short of memory for qwen3.8-27b: trying spark2",
                                               within=0.5), "kept here, then taken over")
        for th in held:
            th.join(30)
        time.sleep(3)                                                       # the hold is over

    def test_a_claude_code_conversation_stays_on_its_box(self):
        # Claude Code writes its system prompt and its tools after the conversation: the turns share their messages
        # alone, 32 KiB here, under 30 % of a body that carries 80 KB of system and tools (review, 2026-11-03)
        system, tools = "s" * 40000, [{"name": f"t{i}", "description": "d" * 4000} for i in range(10)]
        first = [{"role": "user", "content": "claude-conv " + "c" * 36000}]
        turns = []
        for msgs in (first, first + [{"role": "assistant", "content": "ok"}, {"role": "user", "content": "more " * 1000}]):
            turns.append({"model": "qwen3.8-27b", "max_tokens": 8, "messages": msgs, "system": system, "tools": tools})
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 1, 3.0)      # one here, none there: it goes there
        st, out, _ = self.call("/v1/messages", turns[0])
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark2", out)
        for th in held:
            th.join(30)
        self.E2.users.clear()
        self.busy(self.E2, self.port2, KEY_2, "marker-27b", 2, 6.0)         # spark2 runs 2 of its own, spark1 none
        self.E2.served[:] = ["qwen3.8-27b", "marker-cc"]
        self.until_listed("marker-cc")
        st, out, _ = self.call("/v1/messages", turns[1])
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark2", out, "the turn left the box that holds its conversation")

    def test_boxes_far_apart_share_even_a_conversation(self):
        a = self.turns("conv-c", 3)
        st, out, _ = self.call("/v1/chat/completions", a[0])
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out, "two idle boxes: the entry serves it, no hop")
        # the edge, pinned (review, 2026-11-03): spark1 runs 4, spark2 none: 4 apart is not more than 4, the turn stays
        # on the box that holds its prefix
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 4, 4.0)
        st, out, _ = self.call("/v1/chat/completions", a[1])
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out, "4 apart moved a conversation off the box that holds its prefix")
        for th in held:
            th.join(30)
        self.E1.users.clear()
        # spark1 runs 5, spark2 none: more than 4 apart and more than 1.5 times as many, the turn goes to spark2
        self.busy(self.E1, self.port1, KEY_1, "local-only", 5, 3.0)
        st, out, _ = self.call("/v1/chat/completions", a[2])
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark2", out, "5 apart kept a conversation on the busier box")

    def test_a_lane_of_16_keeps_a_conversation_5_ahead(self):
        # decision 1014: a box may run half its lane's slots ahead, 8 of 16. 5 ahead moved a conversation off a box
        # with 11 places free, its prefix computed again on the other one
        if not self.INPROC:
            self.skipTest("this box's slots are set in this test's process")
        saved = self.mod.unit_slots
        self.mod.unit_slots = lambda: 16
        self.addCleanup(setattr, self.mod, "unit_slots", saved)
        a = self.turns("conv-s16", 2)
        st, out, _ = self.call("/v1/chat/completions", a[0])
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out, "two idle boxes: the entry serves it, no hop")
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 5, 4.0)
        st, out, _ = self.call("/v1/chat/completions", a[1])
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out, "5 ahead on a lane of 16 moved a conversation off a box with room to spare")
        for th in held:
            th.join(30)
        self.E1.users.clear()

    def test_the_gap_is_half_the_slots_the_other_box_says(self):
        # decision 1014, the other box's side: a conversation it holds stays there 5 ahead when its lane runs 16 at
        # once, and comes here when it says nothing of its slots (the 27B's 8, a gap of 4)
        for slots in (16, None):
            with self.subTest(slots=slots):
                Peer = fake_peer(["qwen3.8-27b", f"marker-s{slots}-a"], PEER_ANSWER, slots=slots)
                entry = self.spare_proxy(f"spark1-s{slots}", KEY_1, peers=(("spark2", tls_peer(self, Peer)),))
                self.until_listed(f"marker-s{slots}-a", port=entry)
                a = self.turns(f"conv-gap-{slots}", 2)
                held = self.busy(self.E1, entry, KEY_1, "local-only", 1, 3.0)     # one here, none there
                st, out, _ = self.call("/v1/chat/completions", a[0], port=entry)
                self.assertIn(b"from the peer", out, out[:300])
                for th in held:
                    th.join(30)
                self.E1.users.clear()
                # now it runs 5 of its own clients' generations, and this box none
                Peer.state.update(running=5, listed=["qwen3.8-27b", f"marker-s{slots}-b"])
                self.until_listed(f"marker-s{slots}-b", port=entry)
                st, out, _ = self.call("/v1/chat/completions", a[1], port=entry)
                self.assertEqual(st, 200, out[:300])
                if slots == 16:
                    self.assertIn(b"from the peer", out, "5 ahead on a lane of 16 moved the conversation off its box")
                    self.assertEqual(len(Peer.seen), 2)
                else:
                    self.assertIn(b"from spark1", out, "5 ahead on a box of unknown slots kept the conversation there")
                    self.assertEqual(len(Peer.seen), 1)

    def test_a_lane_whose_port_refuses_hands_its_request_to_the_other_box(self):
        # decision 998's first case: this box's engine refuses the connection (its lane stops or starts), and the
        # other box that serves the model answers at once. Every test passed with a refusal never handed over (review,
        # 2026-11-03)
        h = fake_engine("spark1-refuses", "qwen3.8-27b")
        port = localports.held(self)        # kept to the end: once the engine stops, no other test's server takes it
        eng = localports.serve(port, Engine, h)
        threading.Thread(target=eng.serve_forever, daemon=True).start()
        stopped = []

        def stop():
            if not stopped:
                stopped.append(1)
                eng.shutdown()
                eng.server_close()
        self.addCleanup(stop)
        Peer = fake_peer(["qwen3.8-27b", "marker-refuses"], PEER_ANSWER)
        entry = self.spare_proxy("spark1-refuses", KEY_1, peers=(("spark2", tls_peer(self, Peer)),),
                                 upstream=f"http://127.0.0.1:{port}")
        self.until_listed("marker-refuses", port=entry)
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"), port=entry)
        self.assertEqual(st, 200, out[:300])
        self.assertEqual(Peer.seen, [], "two idle boxes: the entry serves it")
        stop()                                              # its lane stops: the port refuses connections
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"), port=entry)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from the peer", out, "this box's engine refused the connection, the other box was not tried")
        self.assert_said("spark1-refuses", "this box's engine does not answer for qwen3.8-27b: trying spark2")

    def test_a_request_kept_here_goes_over_when_the_floor_holds_this_box(self):
        # the floor may start after the sharing kept a request here: it goes to the other box that serves the model,
        # where it waited the whole hold here (v6.52). The floor test passed without this hand-over, its requests sent
        # there at the decision (review, 2026-11-03)
        if not self.INPROC:
            self.skipTest("this box's floor is set in this test's process")
        m = self.mod
        decided = []
        real_floor, real_spread = m.floor_refusal, m.H._pair_spread

        def spread(h, *a, **k):
            kept = real_spread(h, *a, **k)
            decided.append(1)                               # the floor starts once the sharing decided
            return kept
        m.floor_refusal = lambda *a, **k: ("the test's floor, after the decision", 3) if decided else None
        m.H._pair_spread = spread
        self.addCleanup(setattr, m, "floor_refusal", real_floor)
        self.addCleanup(setattr, m.H, "_pair_spread", real_spread)
        before = self.sent()
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"))
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark2", out, "a request kept here was held by the floor while the other box serves it")
        self.assertEqual(self.moved(before, self.sent()), {"rescued": 1})

    def test_a_count_this_box_failed_is_no_engine_that_closed_on_a_request(self):
        # the size guard's own count failed (a 5xx of a stopping engine), the generation never went: the other box gets
        # a 503 it serves the request on, not the 502 of an engine that closed the connection while it had it, and
        # this box's own client is served by the other box (review, 2026-11-03)
        if not self.INPROC:
            self.skipTest("this box's pool is set in this test's process")
        self.mod._POOL.update(tokens=10000, window=9990, ts=time.time())
        self.addCleanup(self.mod._POOL.update, tokens=None, window=None, ts=0.0)
        self.E1.mode = "count_fails"
        body = dict(self.chat("qwen3.8-27b"), messages=[{"role": "user", "content": "count " * 12000}])
        st, out = self.tls_call(self.pport1, "/v1/chat/completions", body)[:2]
        self.assertEqual(st, 503, out[:300])
        self.assertNotIn(b"upstream_closed", out, "a failed count was said to be an engine that died on the request")
        self.mod._POOL.update(tokens=10000, window=9990, ts=time.time())    # the failed count dropped the pool read
        st, out, _ = self.call("/v1/chat/completions", body)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark2", out, "a request whose count failed here was not handed to the other box")

    def test_a_hop_whose_engine_dies_in_an_open_stream_gets_an_error_event(self):
        # the other box's hold opened the stream (200 and keepalives after KEEPALIVE_S), then its engine died on the
        # request: its 502 was a status line written inside the stream, which cut this box's client with no error
        # event (review, 2026-11-03)
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 1, 16.0)       # one here, none there: it goes there
        self.E2.mode = "die"
        body = dict(self.turns("conv-held", 1)[0], stream=True, x_die_after=12.0)
        try:
            st, out, _ = self.call("/v1/chat/completions", body)
        except http.client.HTTPException as e:
            self.fail(f"the stream was cut with no error event: {e!r}")
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"closed the connection while it had the request", out, out[-400:])
        self.assertNotIn(b"HTTP/1.1 502", out)
        self.assertFalse(self.until_in_journal("spark1", "UPSTREAM CUT pair", within=1.0),
                         "the relay of the other box's stream broke")
        self.assert_said("spark1", "502 pair upstream")                       # not "ok pair" (review, 2026-11-04)
        # sent again: its engine closed on it, so that box or nowhere (decision 1019), its engine away now (503)
        self.E2.mode = "busy"
        st, out, _ = self.call("/v1/chat/completions", body)       # the same body: its engine says 503 first
        self.assertNotIn("conv-held-0", self.E1.users, "sent again, the request reached this box's engine")
        for th in held:
            th.join(30)

    def test_a_request_asked_of_this_box_goes_nowhere_else_while_its_engine_is_away(self):
        # the cockpit's smoke test asks whether THIS box's engine answers: with its engine away (a crash, a restart: the
        # names it serves unknown), the request went to the other box, whose answer said the engine was fine (review,
        # 2026-11-03)
        if not self.INPROC:
            self.skipTest("this box's engine is said away in this test's process")
        m = self.mod
        real = m.served_models

        def away(*a, **k):
            raise m.EngineUnreachable("connection refused (the test's engine away)")
        m.served_models = away
        self.addCleanup(setattr, m, "served_models", real)
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"), headers={"X-Alzeo-Here": "1"})
        self.assertNotIn(b"from spark2", out, "the smoke test of this box got the other box's answer")
        self.assertEqual(self.E2.users, [])

    def test_a_request_this_boxs_engine_died_on_stays_here_when_sent_again(self):
        # decision 1019: a client sends a 5xx's request again within seconds. This box's engine died on it: sent again,
        # it may take that engine down again, never the other box's (review, 2026-11-03)
        r = self.turns("conv-killer-here", 1)[0]
        self.E1.mode = "die"
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 503, out[:300])
        self.E1.mode = "ok"
        # 5 here, none there: far apart, its next turn would go there whatever its prefix (decision 1014: 4 of 8)
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 5, 4.0)
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out, "sent again, a request this box's engine died on went to the other box")
        self.assertNotIn("conv-killer-here-0", self.E2.users)
        for th in held:
            th.join(30)

    def test_a_request_the_other_boxs_engine_died_on_goes_there_or_nowhere_when_sent_again(self):
        # decision 1019: the other box's engine died on it (its 502); sent again while that engine is away (503), the
        # entry served it on its own engine, which it could take down too (review, 2026-11-03)
        r = self.turns("conv-killer-there", 1)[0]
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 1, 6.0)     # one here, none there: it goes there
        self.E2.mode = "die"
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 502, out[:300])
        self.E2.mode = "busy"                                               # its engine away: it says 503
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 503, out[:300])
        self.assertNotIn("conv-killer-there-0", self.E1.users, "sent again, it went to this box's engine")
        for th in held:
            th.join(30)

    def test_a_box_serving_another_checkpoint_gets_nothing_shared(self):
        # decision 1018: switch-model.sh serves the 27B's stock, uncensored and fp8 checkpoints under one name. Shared
        # as one model, a client of the stock box got the uncensored one (review, 2026-11-03)
        for said in ("9f8e7d6c5b" * 4, None):
            with self.subTest(revision=said):
                Peer = fake_peer(["qwen3.8-27b", f"marker-rev-{said is None}"], PEER_ANSWER, revision=said)
                entry = self.spare_proxy(f"spark1-rev-{said is None}", KEY_1, peers=(("spark2", tls_peer(self, Peer)),))
                self.until_listed(f"marker-rev-{said is None}", port=entry)
                held = self.busy(self.E1, entry, KEY_1, "local-only", 1, 3.0)     # one here, none there
                st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"), port=entry)
                self.assertEqual(st, 200, out[:300])
                self.assertIn(b"from spark1", out, "a box serving another checkpoint, or saying none, was sent it")
                self.assertEqual(Peer.seen, [])
                for th in held:
                    th.join(30)
                self.E1.users.clear()

    def test_a_stream_the_other_box_ends_by_order_binds_nothing(self):
        # decision 1031: the other box's Load, Abort all or floor cut a stream it served: the entry bound the request to
        # it for 10 minutes, and its retries got 503s while that box was away (review, 2026-11-04)
        Base = fake_peer(["qwen3.8-27b", "marker-ended"], PEER_ANSWER)

        class EndsByOrder(Base):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                Base.seen.append(self.path)
                if Base.state.get("away"):
                    return self.reply(503, {"error": {"type": "engine_unavailable", "message": "its engine is away"}})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                first = (b'data: {"id":"c","object":"chat.completion.chunk","model":"qwen3.8-27b","choices":[{"index":0,'
                         b'"delta":{"content":"from the peer "}}]}\n\n')
                for part in (first, HOP_ENDED_OPENAI):
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
                    self.wfile.flush()
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)                      # cut, not ended
        entry = self.spare_proxy("spark1-ended", KEY_1, peers=(("spark2", tls_peer(self, EndsByOrder)),))
        self.until_listed("marker-ended", port=entry)
        held = self.busy(self.E1, entry, KEY_1, "local-only", 1, 4.0)          # one here, none there: it goes there
        r = dict(self.turns("conv-ended", 1)[0], stream=True)
        try:
            self.call("/v1/chat/completions", r, port=entry)
        except http.client.HTTPException:
            pass                                                                # a cut stream, as the client reads it
        self.assertEqual(len(Base.seen), 1)
        Base.state["away"] = True                                               # that box's engine away now
        st, out, _ = self.call("/v1/chat/completions", dict(r, stream=False), port=entry)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out, "a request the other box ended by order was bound to it")
        self.assert_said("spark1-ended", "ended it by order, nothing is bound")
        for th in held:
            th.join(30)

    def test_a_503_the_other_box_says_is_an_order_is_not_run_here_nor_bound(self):
        # decision 1031 with 1017: the other box's Abort all ended it before any answer; not run here too, nor bound
        Base = fake_peer(["qwen3.8-27b", "marker-ended-503"], PEER_ANSWER)

        class Ends503(Base):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                Base.seen.append(self.path)
                out = json.dumps({"error": {"type": "engine_unavailable", "message": "keepalive-proxy: this box ended "
                                            "the request by order"}}).encode()
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                if not Base.state.get("away"):
                    self.send_header("X-Alzeo-Pair-Ended", "order")
                self.end_headers()
                self.wfile.write(out)
        entry = self.spare_proxy("spark1-ended-503", KEY_1, peers=(("spark2", tls_peer(self, Ends503)),))
        self.until_listed("marker-ended-503", port=entry)
        held = self.busy(self.E1, entry, KEY_1, "local-only", 1, 4.0)          # one here, none there: it goes there
        r = self.turns("conv-ended-503", 1)[0]
        st, out, headers = self.call("/v1/chat/completions", r, port=entry)
        self.assertEqual(st, 503, out[:300])
        self.assertNotIn("conv-ended-503-0", self.E1.users, "an Abort all of the other box was undone here")
        self.assertNotIn("X-Alzeo-Pair-Ended", headers)
        self.assertEqual(headers.get("Retry-After"), "30")                     # as every 503 of the pair
        self.assertIn(b"box spark2 ended this request by order", out, "the other box's own words, 'this box', relayed")
        Base.state["away"] = True                                               # a plain 503 now: its engine away
        st, out, _ = self.call("/v1/chat/completions", r, port=entry)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out, "a request the other box ended by order was bound to it")
        for th in held:
            th.join(30)

    def test_an_ordered_503_whose_body_was_cut_is_still_an_order(self):
        # the body-dependent fallback came first: an ordered 503 whose body was cut ran here, undoing an Abort all there
        # (the delta's 3rd review, 2026-11-04)
        Base = fake_peer(["qwen3.8-27b", "marker-ended-cut"], PEER_ANSWER)

        class EndsCut(Base):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                Base.seen.append(self.path)
                out = json.dumps({"error": {"type": "engine_unavailable", "message": "ended by order"}}).encode()
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out) + 50))         # its body cut on the way
                self.send_header("X-Alzeo-Pair-Ended", "order")
                self.end_headers()
                self.wfile.write(out[:10])
                self.wfile.flush()
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
        entry = self.spare_proxy("spark1-ended-cut", KEY_1, peers=(("spark2", tls_peer(self, EndsCut)),))
        self.until_listed("marker-ended-cut", port=entry)
        held = self.busy(self.E1, entry, KEY_1, "local-only", 1, 4.0)          # one here, none there: it goes there
        r = self.turns("conv-ended-cut", 1)[0]
        st, out, _ = self.call("/v1/chat/completions", r, port=entry)
        self.assertEqual(st, 503, out[:300])
        self.assertIn(b"ended this request by order", out)
        self.assertNotIn("conv-ended-cut-0", self.E1.users, "an Abort all of the other box was undone here")
        for th in held:
            th.join(30)

    def test_a_death_the_other_box_said_was_ordered_after_it_binds_nothing(self):
        # vLLM's death relayed first, the end by order said after: the death bound it at once (the delta's 3rd review)
        Base = fake_peer(["qwen3.8-27b", "marker-dead-then-order"], PEER_ANSWER)

        class DeadThenOrder(Base):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                Base.seen.append(self.path)
                if Base.state.get("away"):
                    return self.reply(503, {"error": {"type": "engine_unavailable", "message": "its engine is away"}})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                first = (b'data: {"id":"c","object":"chat.completion.chunk","model":"qwen3.8-27b","choices":[{"index":0,'
                         b'"delta":{"content":"from the peer "}}]}\n\n')
                for part in (first, b"data: " + VLLM_DEAD_EVENT + b"\n\n", HOP_ENDED_OPENAI):
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        entry = self.spare_proxy("spark1-dead-order", KEY_1, peers=(("spark2", tls_peer(self, DeadThenOrder)),))
        self.until_listed("marker-dead-then-order", port=entry)
        held = self.busy(self.E1, entry, KEY_1, "local-only", 1, 4.0)          # one here, none there: it goes there
        r = dict(self.turns("conv-dead-order", 1)[0], stream=True)
        st, out, _ = self.call("/v1/chat/completions", r, port=entry)
        self.assertEqual(st, 200, out[:300])
        Base.state["away"] = True
        st, out, _ = self.call("/v1/chat/completions", dict(r, stream=False), port=entry)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out, "a death under an order was bound to the other box")
        for th in held:
            th.join(30)

    def test_a_box_that_refuses_this_boxs_certificate_never_had_the_request(self):
        # decision 1017: under TLS 1.3 the other box refuses this box's certificate (re-paired, its entry of this box
        # replaced) at the first read, the request sent: it was said lost, bound there and that box paused, where its
        # proxy never read it (review, 2026-11-04). Its list was read before, so the entry still lists the model there
        Peer = fake_peer(["qwen3.8-27b", "marker-tls"], PEER_ANSWER)
        state = {"ctx": server_ctx("spark2", ("spark1",))}

        class Switching(Engine):
            def get_request(self):
                sock, addr = self.socket.accept()
                return state["ctx"].wrap_socket(sock, server_side=True), addr
        port = localports.held(self)
        srv = localports.serve(port, Switching, Peer)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        entry = self.spare_proxy("spark1-tls", KEY_1, peers=(("spark2", port),))
        self.until_listed("marker-tls", port=entry)
        state["ctx"] = server_ctx("spark2", ("spark9",))                     # this box's certificate refused from now
        held = self.busy(self.E1, entry, KEY_1, "local-only", 1, 3.0)          # one here, none there: it goes there
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"), port=entry)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out, "a request the other box's TLS refused was said lost")
        self.assertEqual(Peer.seen, [])
        self.assert_said("spark1-tls", "refused this box's certificate")
        for th in held:
            th.join(30)

    def test_a_client_that_leaves_while_a_box_that_took_its_request_reads_it_pauses_nothing(self):
        # the client left while its body was on its way to a box that took the connection: this request's own watch
        # cut it, and that healthy box was paused 30 s as one that does not answer (review, 2026-11-04)
        Base = fake_peer(["qwen3.8-27b", "marker-slow-read"], PEER_ANSWER)

        class Slow(Base):
            def do_POST(self):
                if Base.state.get("fast"):
                    return Base.do_POST(self)
                # it reads nothing of the body yet, past the seconds this box's watch keeps the connection once its
                # client left (decision 1112): ended sooner, its own close cut the upload, a box that does not answer
                time.sleep(14)
        entry = self.spare_proxy("spark1-slow-read", KEY_1, peers=(("spark2", tls_peer(self, Slow)),))
        self.until_listed("marker-slow-read", port=entry)
        held = self.busy(self.E1, entry, KEY_1, "local-only", 1, 6.0)          # one here, none there: it goes there
        body = json.dumps(dict(self.chat("qwen3.8-27b"), messages=[{"role": "user", "content": "x" * (64 << 20)}]))
        s = socket.create_connection(("127.0.0.1", entry), timeout=60)
        try:
            s.sendall(f"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                      f"Authorization: Bearer {KEY_1}\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body.encode())
            time.sleep(2.5)                                                     # its body on its way, unread there
        finally:
            s.close()                                                           # the client leaves
        self.assert_said("spark1-slow-read", "CLIENT GONE during pair")
        self.assertFalse(self.until_in_journal("spark1-slow-read", "its client left", within=2.0),
                         "a box that took the connection was paused as one that does not answer")
        for th in held:
            th.join(30)
        # that box never read the conversation: it holds none of it, and the next turn is not pulled there by a prefix it
        # does not have (the conversation was moved there, the delta's review, 2026-11-04)
        Base.state["fast"] = True
        again = dict(self.chat("qwen3.8-27b"), messages=[{"role": "user", "content": "x" * (200 << 10)}])
        st, out, _ = self.call("/v1/chat/completions", again, port=entry)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out, "the conversation was kept on the box that never read it")

    def test_a_shared_request_carries_this_boxs_checkpoint_and_never_the_clients(self):
        # decision 1018: the checkpoint the other box must serve a shared request from comes with it, set by the entry
        # alone (review, 2026-11-04)
        Peer = fake_peer(["qwen3.8-27b", "marker-carries"], PEER_ANSWER)
        entry = self.spare_proxy("spark1-carries", KEY_1, peers=(("spark2", tls_peer(self, Peer)),))
        self.until_listed("marker-carries", port=entry)
        held = self.busy(self.E1, entry, KEY_1, "local-only", 1, 3.0)          # one here, none there: it goes there
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"), port=entry,
                               headers={"X-Alzeo-Pair-Checkpoint": "ab" * 20})
        self.assertEqual(st, 200, out[:300])
        self.assertEqual(len(Peer.seen), 1)
        self.assertEqual(Peer.heard, [TEST_REV], "the entry's checkpoint, never the one its client wrote")
        for th in held:
            th.join(30)

    def test_a_request_for_a_model_only_the_other_box_serves_carries_no_checkpoint(self):
        Peer = fake_peer(["qwen3.8-flash-next-fp4", "marker-alone"], PEER_ANSWER)
        entry = self.spare_proxy("spark1-alone", KEY_1, peers=(("spark2", tls_peer(self, Peer)),))
        self.until_listed("marker-alone", port=entry)
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=entry,
                               headers={"X-Alzeo-Pair-Checkpoint": "ab" * 20})
        self.assertEqual(st, 200, out[:300])
        self.assertEqual(Peer.heard, [None], "a model this box does not serve: no checkpoint to keep")

    def test_a_tool_call_the_other_box_served_is_watched_here_too(self):
        # role 8: a call naming a watched host, in an answer this box relayed from the other one, was written in the
        # other box's journal alone, and this box's cockpit showed no banner for its own client (review, 2026-11-03)
        tool = (200, {"id": "c", "object": "chat.completion", "model": "qwen3.8-27b", "choices": [{
            "index": 0, "finish_reason": "tool_calls", "message": {"role": "assistant", "content": None, "tool_calls": [{
                "id": "t1", "type": "function", "function": {
                    "name": "bash", "arguments": json.dumps({"command": "curl https://b.oss-cn.aliyuncs.com/x"})}}]}}]})
        Peer = fake_peer(["qwen3.8-27b", "marker-tool"], tool)
        entry = self.spare_proxy("spark1-tool", KEY_1, peers=(("spark2", tls_peer(self, Peer)),))
        self.until_listed("marker-tool", port=entry)
        held = self.busy(self.E1, entry, KEY_1, "local-only", 1, 3.0)          # one here, none there: it goes there
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"), port=entry)
        self.assertEqual(st, 200, out[:300])
        self.assertEqual(len(Peer.seen), 1)
        self.assert_said("spark1-tool", "TOOL TARGET aliyuncs.com in a bash call on /v1/chat/completions")
        for th in held:
            th.join(30)
        self.E1.users.clear()                               # the next busy() waits for its own request
        # and in a stream relayed as it came
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 1, 3.0)     # one here, none there: it goes there
        self.E2.mode = "tool"
        st, out, _ = self.call("/v1/chat/completions", dict(self.turns("conv-tool", 1)[0], stream=True))
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"aliyuncs", out)
        self.assert_said("spark1", "TOOL TARGET aliyuncs.com in a bash call on /v1/chat/completions")
        for th in held:
            th.join(30)

    def test_the_other_boxs_list_reads_write_a_line_a_minute_while_this_engine_is_away(self):
        # each read of the other box (every PAIR_TTL_S while it has traffic) wrote "engine unreachable" and a 503 line:
        # some 24 a minute, the 800 lines this box's cockpit reads filled in about 33 minutes (review, 2026-11-03)
        self.E1.mode = "down"                               # this box's engine: its list answers 503 (a boot)
        for _ in range(6):
            st, out = self.tls_call(self.pport1, "/v1/models", method="GET")[:2]
            self.assertEqual(st, 503, out[:300])
        time.sleep(0.5)
        lines = self.journal_of("spark1")[self.journal_at.get("spark1", 0):]
        self.assertLessEqual(lines.count("engine unreachable"), 1, lines[-1500:])

    def test_a_request_bound_to_a_box_now_serving_another_checkpoint_goes_nowhere(self):
        # decisions 1018 and 1019 together: bound to the other box by its engine dying on it, a request went there
        # even once that box served another checkpoint of the model, the uncensored 27B for a client of the stock one
        if not self.INPROC:
            self.skipTest("this box's checkpoint is set in this test's process")
        m = self.mod
        r = self.turns("conv-bound-rev", 1)[0]
        m.killer_note(m.body_fingerprint(json.dumps(r).encode(), "/v1/chat/completions"), "spark2")
        self.addCleanup(m._KILLERS.clear)
        saved = m.unit_revision
        m.unit_revision = lambda: "ab" * 20             # this box serves another checkpoint than spark2 now
        self.addCleanup(setattr, m, "unit_revision", saved)
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 503, out[:300])
        self.assertIn(b"from another checkpoint", out)
        self.assertNotIn("conv-bound-rev-0", self.E2.users, "it went to the box serving another checkpoint")
        self.assertNotIn("conv-bound-rev-0", self.E1.users, "it was served here, where an engine never closed on it")

    def test_a_request_bound_to_a_box_that_serves_another_model_now_gets_an_answer(self):
        # decision 1019: bound to the other box, which said 409 (it serves another model now): the client got no answer
        # at all, the 409 written nowhere (review, 2026-11-04)
        class DropsThen409(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            state = {"drop": True}

            def log_message(self, *a):
                pass

            def do_GET(self):
                out = json.dumps({"object": "list", "data": [{"id": "qwen3.8-27b"}, {"id": "marker-409"}],
                                  "x_alzeo_running": 0, "x_alzeo_running_yours": 0,
                                  "x_alzeo_revision": TEST_REV}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if DropsThen409.state["drop"]:                  # it had it, and went: the request is bound to it
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.close_connection = True
                    return
                out = json.dumps({"error": {"type": "model_not_served", "message": "another model here"}}).encode()
                self.send_response(409)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        entry = self.spare_proxy("spark1-409", KEY_1, peers=(("spark2", tls_peer(self, DropsThen409)),))
        self.until_listed("marker-409", port=entry)
        r = self.turns("conv-bound-409", 1)[0]
        held = self.busy(self.E1, entry, KEY_1, "local-only", 1, 4.0)          # one here, none there: it goes there
        st, out, _ = self.call("/v1/chat/completions", r, port=entry)
        self.assertEqual(st, 503, out[:300])                                    # lost (decision 1017), and bound
        DropsThen409.state["drop"] = False                                      # now it serves another model
        c = http.client.HTTPConnection("127.0.0.1", entry, timeout=10)
        try:
            c.request("POST", "/v1/chat/completions", json.dumps(r).encode(),
                      {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
            resp = c.getresponse()
            st, out = resp.status, resp.read()
        except OSError as e:
            self.fail(f"the client got no answer: {e!r}")
        finally:
            c.close()
        self.assertEqual(st, 503, out[:300])
        self.assertIn(b"serves another model or checkpoint now", out)
        self.assertNotIn("conv-bound-409-0", self.E1.users, "served here, where no engine closed on it")
        for th in held:
            th.join(30)

    def test_a_request_the_proxy_rewrites_is_bound_as_its_client_sends_it(self):
        # decision 1019: the binding was noted on the body this proxy rewrote (a reasoning effort it maps, a tool schema
        # it fixes), the retry looked up with the body as sent: they never matched (review, 2026-11-04)
        r = dict(self.turns("conv-killer-lean", 1)[0], reasoning_effort="lean")
        self.E1.mode = "die"
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 503, out[:300])
        self.E1.mode = "ok"
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 5, 4.0)     # far apart: it would go there
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out, "a rewritten request this box's engine died on went to the other box")
        for th in held:
            th.join(30)

    def test_an_engine_that_dies_during_its_answer_binds_the_request(self):
        # decision 1019: this box's engine died in the middle of the stream; sent again, the request went to the other
        # engine (review, 2026-11-04)
        r = dict(self.turns("conv-killer-cut", 1)[0], stream=True)
        self.E1.mode = "cut"                                                    # its stream dies after two chunks
        try:
            self.call("/v1/chat/completions", r)
        except http.client.HTTPException:
            pass                                                                # a cut stream, as the client reads it
        self.E1.mode = "ok"
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 5, 4.0)     # far apart: it would go there
        try:
            st, out, _ = self.call("/v1/chat/completions", r)
        except http.client.HTTPException as e:
            self.fail(f"the retry broke: {e!r}")
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"spark1-", out, "a request this box's engine died on, mid-answer, went to the other box")
        self.assertNotIn("conv-killer-cut-0", self.E2.users)
        for th in held:
            th.join(30)

    def test_this_engine_away_a_model_its_lane_served_goes_to_no_other_checkpoint(self):
        # decision 1018, this box's engine away (a crash, a restart): the model its lane served went to the other box
        # whatever its checkpoint, the uncensored 27B for a client of the stock one (review, 2026-11-04)
        if not self.INPROC:
            self.skipTest("this box's engine is said away in this test's process")
        m = self.mod
        saved_lane = m._LAST_LANE["lane"]
        m._LAST_LANE["lane"] = (frozenset({"qwen3.8-27b"}), "ab" * 20)       # this box served another checkpoint
        self.addCleanup(m._LAST_LANE.__setitem__, "lane", saved_lane)
        real = m.served_models

        def away(*a, **k):
            raise m.EngineUnreachable("connection refused (the test's engine away)")
        m.served_models = away
        self.addCleanup(setattr, m, "served_models", real)
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"))
        # this box's own path, which holds it while its engine comes back (role 6): a 503 at once cut a restart's
        # clients off (the delta's review, 2026-11-04). The engine of these tests answers, so it serves it
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out)
        self.assertEqual(self.E2.users, [], "a client of this box got the other checkpoint")
        m._LAST_LANE["lane"] = (frozenset({"qwen3.8-27b"}), TEST_REV)        # the same checkpoint there: it serves
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"))
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark2", out)

    def test_a_request_a_dead_vllm_engine_answered_stays_here_when_sent_again(self):
        # decision 1019: vLLM answers the requests in flight when its EngineCore died with a 500, relayed and bound to
        # nothing: the SDKs' retry went to the other box, which the same request could take down (review, 2026-11-04)
        r = self.turns("conv-vllm-dead", 1)[0]
        self.E1.mode = "vllm_dead"
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 500, out[:300])
        self.E1.mode = "ok"
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 5, 4.0)     # far apart: it would go there
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out, "sent again, a request vLLM's engine died on here went to the other box")
        self.assertNotIn("conv-vllm-dead-0", self.E2.users)
        for th in held:
            th.join(30)

    def test_a_request_a_dead_vllm_engine_ended_in_its_stream_stays_here_when_sent_again(self):
        r = dict(self.turns("conv-vllm-dead-stream", 1)[0], stream=True)
        self.E1.mode = "vllm_dead_stream"
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"EngineCore encountered an issue", out)
        self.E1.mode = "ok"
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 5, 4.0)     # far apart: it would go there
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"spark1-", out, "sent again, a request vLLM's engine died on during its stream went elsewhere")
        self.assert_said("spark1", "502 upstream")                            # not "ok" (the delta's review, 2026-11-04)
        self.assertNotIn("conv-vllm-dead-stream-0", self.E2.users)
        for th in held:
            th.join(30)

    def test_a_request_the_other_boxs_dead_vllm_engine_answered_goes_there_or_nowhere(self):
        r = self.turns("conv-vllm-dead-there", 1)[0]
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 1, 6.0)     # one here, none there: it goes there
        self.E2.mode = "vllm_dead"
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 500, out[:300])
        self.E2.mode = "busy"                                               # its engine away now: it says 503
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 503, out[:300])
        self.assertNotIn("conv-vllm-dead-there-0", self.E1.users, "sent again, it went to this box's engine")
        for th in held:
            th.join(30)

    def test_an_engine_that_closes_the_connection_on_the_count_closed_it_on_the_request(self):
        # decision 1019: the size guard's count is this request's prompt; an engine that closed the connection on it
        # was said a failed count, which a hop answers 503 and the other box serves (review, 2026-11-04)
        if not self.INPROC:
            self.skipTest("this box's pool is set in this test's process")
        self.mod._POOL.update(tokens=10000, window=9990, ts=time.time())
        self.addCleanup(self.mod._POOL.update, tokens=None, window=None, ts=0.0)
        self.E1.mode = "count_dies"
        body = dict(self.chat("qwen3.8-27b"), messages=[{"role": "user", "content": "count " * 12000}],
                    user="hop-count-dies")
        st, out = self.tls_call(self.pport1, "/v1/chat/completions", body)[:2]
        self.assertEqual(st, 502, out[:300])
        self.assertIn(b"upstream_closed", out)

    def test_a_request_bound_while_this_box_was_alone_is_this_boxs(self):
        # a box alone notes "" for itself: paired within the 10 minutes, it took that for a box that left the pair and
        # served the request nowhere (found re-reading the rule, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the binding is set in this test's process")
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        r = self.turns("conv-bound-alone", 1)[0]
        m.killer_note(m.body_fingerprint(json.dumps(r).encode(), "/v1/chat/completions"), "")
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 5, 4.0)     # far apart: it would go there
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out, "a request bound while this box was alone was not served here")
        self.assertNotIn("conv-bound-alone-0", self.E2.users)
        for th in held:
            th.join(30)

    def test_a_request_bound_here_for_a_name_no_box_serves_is_this_boxs_lanes(self):
        # decision 986: a lane name no box serves is answered by this box's lane; bound here, it was refused "this box
        # serves another model now" for 10 minutes though nothing switched (the delta's review, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the binding is set in this test's process")
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        r = dict(self.turns("conv-bound-986", 1)[0], model="qwen3.8-flash-next-fp4")
        m.killer_note(m.body_fingerprint(json.dumps(r).encode(), "/v1/chat/completions"), "")       # this box
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out)

    def test_a_request_bound_to_the_other_box_on_a_box_that_reads_no_key_runs_nowhere(self):
        # decision 1019 and the key rule: bound to the other box, a box that reads no key ran it on its own engine, the
        # fallback 1019 rules out (the delta's review, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the key is read in this test's process")
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        saved = m._door_key
        m._door_key = lambda: None
        self.addCleanup(setattr, m, "_door_key", saved)
        r = self.turns("conv-bound-nokey", 1)[0]
        m.killer_note(m.body_fingerprint(json.dumps(r).encode(), "/v1/chat/completions"), "spark2")
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 503, out[:300])
        self.assertIn(b"cannot read its own key", out)
        self.assertNotIn("conv-bound-nokey-0", self.E1.users + self.E2.users)

    def test_a_request_bound_to_a_box_of_a_pair_dissolved_since_goes_nowhere(self):
        # decision 1019: the pair dissolved, a request bound to the box that left ran on this box's engine (the delta's
        # review, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the pair is read in this test's process")
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        # the pair port's keeper waits while pair.json reads as dissolved here: seeing it (it looks every 2 s), it closed
        # this box's pair port and opened another (PAIR_PORT=0), and the next tests' connections to the port they knew
        # were refused (the whole file run alone, 2026-11-05)
        m.PAIR_PORT_KEEPER._lock.acquire()
        self.addCleanup(m.PAIR_PORT_KEEPER._lock.release)
        saved = m.pair_conf
        m.pair_conf = lambda: {"name": "spark1", "peers": []}
        self.addCleanup(setattr, m, "pair_conf", saved)
        port = m.PAIR_PORT_KEEPER.srv.server_address[1]
        r = self.turns("conv-bound-dissolved", 1)[0]
        m.killer_note(m.body_fingerprint(json.dumps(r).encode(), "/v1/chat/completions"), "spark2")
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 503, out[:300])
        self.assertIn(b"no longer in this pair", out)
        self.assertNotIn("conv-bound-dissolved-0", self.E1.users)
        st, out, _ = self.call("/v1/chat/completions", self.turns("conv-not-bound", 1)[0])
        self.assertEqual(st, 200, out[:300])                                    # an unbound one is served as on a box alone
        time.sleep(2.5)                                                         # past a look of the keeper
        self.assertEqual(m.PAIR_PORT_KEEPER.srv.server_address[1], port, "the pair port moved under the next tests")

    def test_a_request_bound_to_a_box_that_left_the_pair_goes_nowhere(self):
        # decision 1019: bound to a box no longer in pair.json, it was served here, the fallback 1019 rules out
        # (review, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the binding is set in this test's process")
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        r = self.turns("conv-bound-left", 1)[0]
        m.killer_note(m.body_fingerprint(json.dumps(r).encode(), "/v1/chat/completions"), "spark7")
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 503, out[:300])
        self.assertIn(b"no longer in this pair", out)
        self.assertNotIn("conv-bound-left-0", self.E1.users + self.E2.users)

    def test_a_hop_this_box_stops_by_order_before_any_answer_gets_a_503_that_says_so(self):
        # decision 1031: this box's Abort all cut a request the other box sent on, with no answer: the other box took
        # it for a request it lost (1017) and bound it to this box for 10 minutes (review, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the Abort all runs in this test's process")
        got = {}
        hop = dict(self.chat("qwen3.8-27b"), user="hop-abort", x_delay=6.0)

        def send():
            c = http.client.HTTPSConnection("127.0.0.1", self.pport1, timeout=60, context=client_ctx("spark2", "spark1"))
            try:
                c.request("POST", "/v1/chat/completions", json.dumps(hop).encode(), {"Content-Type": "application/json"})
                r = c.getresponse()
                got.update(st=r.status, headers=dict(r.getheaders()), body=r.read())
            except Exception as e:
                got["err"] = repr(e)
            finally:
                c.close()
        th = threading.Thread(target=send, daemon=True)
        th.start()
        deadline = time.monotonic() + 10
        while "hop-abort" not in self.E1.users and time.monotonic() < deadline:
            time.sleep(0.05)
        self.mod.stop_generations("test: Abort all", floor=False)
        th.join(30)
        self.assertNotIn("err", got, "cut with no answer")
        self.assertEqual(got["st"], 503, got.get("body", b"")[:300])
        self.assertEqual(got["headers"].get("X-Alzeo-Pair-Ended"), "order")

    def test_a_hop_this_box_stops_by_order_in_its_open_stream_says_so_before_the_cut(self):
        # decision 1031, in a stream the hold opened: the other box read a cut and bound the request (review, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the Abort all runs in this test's process")
        hop = dict(self.chat("qwen3.8-27b"), user="hop-abort-stream", stream=True, x_delay=16.0)
        c = http.client.HTTPSConnection("127.0.0.1", self.pport1, timeout=60, context=client_ctx("spark2", "spark1"))
        self.addCleanup(c.close)
        c.request("POST", "/v1/chat/completions", json.dumps(hop).encode(), {"Content-Type": "application/json"})
        r = c.getresponse()                                                     # the hold opens it after KEEPALIVE_S
        self.assertEqual(r.status, 200)
        got = r.read1(65536)                                                    # its first keepalive
        self.mod.stop_generations("test: Abort all", floor=False)
        try:
            while True:
                b = r.read1(65536)
                if not b:
                    break
                got += b
        except (http.client.HTTPException, OSError):
            pass                                                                # cut, not ended
        self.assertIn(b'"x_alzeo_ended":"order"', got, got[-300:])

    def test_a_hop_this_box_stops_by_order_answers_in_its_clients_dialect(self):
        # the 503 of an end by order was written by hand: no Retry-After, the OpenAI shape on /v1/messages (the delta's 3rd
        # review, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the Abort all runs in this test's process")
        got = {}
        hop = {"model": "qwen3.8-27b", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}],
               "metadata": {"user_id": "hop-abort-anthropic"}, "x_delay": 6.0}

        def send():
            c = http.client.HTTPSConnection("127.0.0.1", self.pport1, timeout=60, context=client_ctx("spark2", "spark1"))
            try:
                c.request("POST", "/v1/messages", json.dumps(hop).encode(), {"Content-Type": "application/json"})
                r = c.getresponse()
                got.update(st=r.status, headers=dict(r.getheaders()), body=r.read())
            except Exception as e:
                got["err"] = repr(e)
            finally:
                c.close()
        before = len(self.E1.seen)
        th = threading.Thread(target=send, daemon=True)
        th.start()
        deadline = time.monotonic() + 10
        while len(self.E1.seen) == before and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.3)
        self.mod.stop_generations("test: Abort all", floor=False)
        th.join(30)
        self.assertNotIn("err", got, "cut with no answer")
        self.assertEqual(got["st"], 503, got.get("body", b"")[:300])
        self.assertEqual(got["headers"].get("X-Alzeo-Pair-Ended"), "order")
        self.assertEqual(got["headers"].get("Retry-After"), "30")
        self.assertEqual(json.loads(got["body"]).get("type"), "error", "not the Anthropic dialect")

    def test_a_dead_vllm_engine_under_an_ordered_stop_is_said_to_be_one(self):
        # decision 1031: vLLM answers the requests in flight with its EngineDeadError while its lane stops by order, and
        # the other box bound them (the delta's 3rd review, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the unit's job is said in this test's process")
        m = self.mod
        saved = m.unit_stopping_by_order
        m.unit_stopping_by_order = lambda: True
        self.addCleanup(setattr, m, "unit_stopping_by_order", saved)
        self.E1.mode = "vllm_dead"
        st, out, headers = self.tls_call(self.pport1, "/v1/chat/completions",
                                         dict(self.chat("qwen3.8-27b"), user="hop-dead-ordered"))
        self.assertEqual(st, 500, out[:300])
        self.assertEqual(headers.get("X-Alzeo-Pair-Ended"), "order")

    def test_a_request_bound_here_is_refused_when_the_others_list_failed_but_named_it(self):
        # bound to this box, which serves another model now: refused only when the other box's current list named it; a
        # read that failed (its models empty, its last good list kept in "known") let this box answer with its other
        # model (the delta's 3rd review, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the binding and the lists are set in this test's process")
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        r = self.turns("conv-bound-failed-read", 1)[0]
        m.killer_note(m.body_fingerprint(json.dumps(r).encode(), "/v1/chat/completions"), "")
        real = m.served_models
        m.served_models = lambda *a, **k: ["qwen3.8-flash-next-fp4"]           # this box serves another model now
        self.addCleanup(setattr, m, "served_models", real)
        with m._PAIR_LOCK:
            row = dict(m._PAIR_MODELS["spark2"])
            m._PAIR_MODELS["spark2"] = dict(row, models=(), known=("qwen3.8-27b",), error="unreachable",
                                            ts=time.monotonic())
        self.addCleanup(lambda: m._PAIR_MODELS.__setitem__("spark2", row))
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 503, out[:300])
        self.assertIn(b"serves another model now", out)
        self.assertNotIn("conv-bound-failed-read-0", self.E1.users)

    def test_a_hop_whose_engine_closes_as_its_lane_stops_by_order_gets_a_503_that_says_so(self):
        # decision 1031: a hop queued or in prefill here when this box's cockpit loads another lane, its engine closing
        # before any byte: a plain 503, which the other box took for an engine away, served the request itself, paused
        # this box 30 s and counted a failover (the 4th review of v1.43.0, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the unit's job is said in this test's process")
        m = self.mod
        saved = m.unit_stopping_by_order
        m.unit_stopping_by_order = lambda: True
        self.addCleanup(setattr, m, "unit_stopping_by_order", saved)
        self.addCleanup(m._KILLERS.clear)
        self.E1.mode = "die"
        st, out, headers = self.tls_call(self.pport1, "/v1/chat/completions",
                                         dict(self.chat("qwen3.8-27b"), user="hop-closed-ordered"))
        self.assertEqual(st, 503, out[:300])
        self.assertEqual(headers.get("X-Alzeo-Pair-Ended"), "order", out[:300])
        self.assertEqual(len(m._KILLERS), 0, "an ordered stop bound the request")

    def test_an_ordered_death_in_a_stream_the_keeper_opened_is_said_in_it(self):
        # decision 1031: vLLM's 500 under an ordered stop, once the keeper had opened the stream: no header goes then,
        # and the other box bound the request for 10 minutes (the 4th review of v1.43.0, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the unit's job is said in this test's process")
        m = self.mod
        saved = m.unit_stopping_by_order
        m.unit_stopping_by_order = lambda: True
        self.addCleanup(setattr, m, "unit_stopping_by_order", saved)
        self.E1.mode = "vllm_dead"
        hop = dict(self.chat("qwen3.8-27b"), user="hop-dead-kept", stream=True, x_dead_after=m.KEEPALIVE_S + 1.5)
        st, out, _ = self.tls_call(self.pport1, "/v1/chat/completions", hop)
        self.assertEqual(st, 200, out[:300])                                    # the keeper's stream
        self.assertIn(b"EngineCore", out)
        self.assertIn(b'"x_alzeo_ended":"order"', out, out[-400:])

    def test_a_death_the_other_box_relayed_before_its_client_left_is_bound(self):
        # decision 1019: a death the other box relayed in its stream was bound at the stream's end, once an order said
        # after it would have come; a client that left first (the SDKs raise on that event) left it unbound, and its
        # resend could take this box's engine down too (the 4th review of v1.43.0, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 2, 6.0)    # this box busy: a new one goes there
        self.E2.mode = "vllm_dead_more"
        raw = json.dumps(dict(self.turns("conv-dead-left", 1)[0], stream=True)).encode()
        c = http.client.HTTPConnection("127.0.0.1", self.port1, timeout=30)
        c.request("POST", "/v1/chat/completions", raw,
                  {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
        resp = c.getresponse()
        got = b""
        while b"EngineCore" not in got:
            b = resp.read1(65536)
            if not b:
                break
            got += b
        self.assertIn(b"EngineCore", got, got[-300:])
        self.assertIn(b"spark2", got, "served here: the test needs the other box's stream")
        resp.close()                                                            # it raises on that event, and leaves:
        c.close()                                                               # its answer held the socket
        fp = m.body_fingerprint(raw, "/v1/chat/completions")
        deadline = time.monotonic() + 6
        while m.killer_box(fp) is None and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(m.killer_box(fp), "spark2", "a death relayed before its client left was bound to nothing")
        for th in held:
            th.join(30)

    def test_a_request_bound_here_is_answered_here_once_the_other_box_said_nothing_for_long(self):
        # bound to this box, which serves another model now: the other box's last good list named the model long ago,
        # every read failing since (it is off): refused for the binding's 10 minutes, where the same request unbound got
        # this box's lane's answer (decision 986; the 4th review of v1.43.0, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the binding and the lists are set in this test's process")
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        r = self.turns("conv-bound-off", 1)[0]
        m.killer_note(m.body_fingerprint(json.dumps(r).encode(), "/v1/chat/completions"), "")
        real = m.served_models
        m.served_models = lambda *a, **k: ["qwen3.8-flash-next-fp4"]           # this box serves another model now
        self.addCleanup(setattr, m, "served_models", real)
        with m._PAIR_LOCK:
            row = dict(m._PAIR_MODELS["spark2"])
            m._PAIR_MODELS["spark2"] = dict(row, models=(), known=("qwen3.8-27b",), error="unreachable",
                                            known_ts=time.monotonic() - m.PAIR_KNOWN_WAIT_S - 60, ts=time.monotonic())
        self.addCleanup(lambda: m._PAIR_MODELS.__setitem__("spark2", row))
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out)

    def test_a_request_bound_here_is_answered_here_when_the_other_box_moved_to_another_model(self):
        # a 409 "moved" leaves the other box's row with no models, its last list and no error: refused 10 minutes as if
        # its read had failed, where the same request unbound got this box's lane's answer (the 5th review of v1.43.0)
        if not self.INPROC:
            self.skipTest("the binding and the lists are set in this test's process")
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        r = self.turns("conv-bound-moved", 1)[0]
        m.killer_note(m.body_fingerprint(json.dumps(r).encode(), "/v1/chat/completions"), "")
        real = m.served_models
        m.served_models = lambda *a, **k: ["qwen3.8-flash-next-fp4"]           # this box serves another model now
        self.addCleanup(setattr, m, "served_models", real)
        with m._PAIR_LOCK:
            row = dict(m._PAIR_MODELS["spark2"])
            m._PAIR_MODELS["spark2"] = dict(row, models=(), known=("qwen3.8-27b",), error=None,
                                            known_ts=time.monotonic(), ts=time.monotonic())
        self.addCleanup(lambda: m._PAIR_MODELS.__setitem__("spark2", row))
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out)

    def test_a_death_relayed_then_an_order_said_after_it_binds_nothing(self):
        # decision 1054: the other box relays its engine's death, its client leaves at once (the SDKs raise on that
        # event), and the other box says only then that it ended the request by order (its read of systemd was late):
        # bound 10 minutes at the client's leaving, where the order said otherwise (the 5th review of v1.43.0)
        self._death_then_order(between=10, silence=1.0)

    def test_a_death_relayed_then_an_order_said_after_a_silence_binds_nothing(self):
        # the same with nothing between the death and the order: no write failed, the watch saw the client gone first,
        # shut the connection within a second, and the request was bound 10 minutes in 5 runs of 5 (the 6th review of
        # v1.43.0, 2026-11-04; the test before replaced the watch, which hid it)
        self._death_then_order(between=0, silence=1.5)

    def _death_then_order(self, between, silence):
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        Base = fake_peer(["qwen3.8-27b", "marker-1054"], PEER_ANSWER)
        event = (b'data: {"id":"c","object":"chat.completion.chunk","model":"qwen3.8-27b","choices":[{"index":0,'
                 b'"delta":{"content":"from the peer "}}]}\n\n')

        class DeadThenOrder(Base):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                Base.seen.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()

                def chunk(part):
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
                    self.wfile.flush()
                try:
                    chunk(event)
                    chunk(b"data: " + VLLM_DEAD_EVENT + b"\n\n")
                    for _ in range(between):                                    # the entry writes them to no client
                        time.sleep(0.05)
                        chunk(event)
                    time.sleep(silence)
                    chunk(HOP_ENDED_OPENAI)                                     # the order, said after the death
                except OSError:
                    return
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)                      # cut, not ended
        self.repoint(tls_peer(self, DeadThenOrder))
        self.until_listed("marker-1054")
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 2, 6.0)    # this box busy: a new one goes there
        raw = json.dumps(dict(self.turns("conv-dead-order", 1)[0], stream=True)).encode()
        c = http.client.HTTPConnection("127.0.0.1", self.port1, timeout=30)
        c.request("POST", "/v1/chat/completions", raw,
                  {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
        resp = c.getresponse()
        got = b""
        while b"EngineCore" not in got:
            b = resp.read1(65536)
            if not b:
                break
            got += b
        self.assertIn(b"from the peer", got, "served here: the test needs the other box's stream")
        self.assertIn(b"EngineCore", got)
        resp.close()                                                            # it raises on that event, and leaves
        c.close()
        self.assert_said("spark1", "said it ended by order: its binding is undone (decision 1054)")
        self.assertIsNone(m.killer_box(m.body_fingerprint(raw, "/v1/chat/completions")),
                          "an order said after the death was bound 10 minutes")
        for th in held:
            th.join(30)

    def test_a_death_relayed_is_bound_at_once_when_its_client_leaves_and_that_box_goes_quiet(self):
        # decision 1061: the wait for an order after the death had no bound once the death was relayed: the other box
        # quiet (no FIN), the request was bound 60 s after its client left, its retry running unbound on this box's engine
        # meanwhile (the 7th review of v1.43.0, 2026-11-04). Bound at once now; an order said in 5 s would undo it
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        Base = fake_peer(["qwen3.8-27b", "marker-quiet"], PEER_ANSWER)
        event = (b'data: {"id":"c","object":"chat.completion.chunk","model":"qwen3.8-27b","choices":[{"index":0,'
                 b'"delta":{"content":"from the peer "}}]}\n\n')

        class DeadThenQuiet(Base):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                Base.seen.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                try:
                    for part in (event, b"data: " + VLLM_DEAD_EVENT + b"\n\n"):
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
                        self.wfile.flush()
                    time.sleep(8.0)                                             # quiet, its connection open
                except OSError:
                    return
        self.repoint(tls_peer(self, DeadThenQuiet))
        self.until_listed("marker-quiet")
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 2, 6.0)    # this box busy: a new one goes there
        raw = json.dumps(dict(self.turns("conv-dead-quiet", 1)[0], stream=True)).encode()
        c = http.client.HTTPConnection("127.0.0.1", self.port1, timeout=30)
        c.request("POST", "/v1/chat/completions", raw,
                  {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
        resp = c.getresponse()
        got = b""
        while b"EngineCore" not in got:
            b = resp.read1(65536)
            if not b:
                break
            got += b
        self.assertIn(b"from the peer", got, "served here: the test needs the other box's stream")
        resp.close()                                                            # it raises on that event, and leaves
        c.close()
        fp = m.body_fingerprint(raw, "/v1/chat/completions")
        deadline = time.monotonic() + 3
        while m.killer_box(fp) is None and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(m.killer_box(fp), "spark2", "its retry ran unbound while that box was quiet")
        for th in held:
            th.join(30)

    def test_a_death_here_whose_order_shows_at_the_streams_end_is_unbound(self):
        # a request of this box's own client, bound at the death by a read of systemd that saw no stop job yet, stayed
        # bound 10 minutes once the order showed at the stream's end, its retries refused (the 7th review of v1.43.0)
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        saved = m.unit_stopping_by_order
        m.unit_stopping_by_order = lambda: self.E1.ordered_now                  # the stop job, seen after the death
        self.addCleanup(setattr, m, "unit_stopping_by_order", saved)
        self.E1.mode = "vllm_dead_late_order"
        r = dict(self.turns("conv-local-late-order", 1)[0], stream=True)
        raw = json.dumps(r).encode()
        fp = m.body_fingerprint(raw, "/v1/chat/completions")
        c = http.client.HTTPConnection("127.0.0.1", self.port1, timeout=30)
        self.addCleanup(c.close)
        c.request("POST", "/v1/chat/completions", raw,
                  {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
        resp = c.getresponse()
        self.assertEqual(resp.status, 200)
        got = b""
        while b"EngineCore" not in got:
            b = resp.read1(65536)
            if not b:
                break
            got += b
        self.assertIn(b"EngineCore", got)
        # the premise: bound at the death, its order not seen yet; unread, the test passed whenever the death came after
        # the order (the 9th review of v1.43.0, 2026-11-05)
        self.assertEqual(m.killer_box(fp), "", "the test needs the request bound at the death")
        got += resp.read()                                                      # its clean end
        self.assertIn("conv-local-late-order-0", self.E1.users, "the test needs this box's engine to have it")
        self.assertIsNone(m.killer_box(fp), "an order shown at the stream's end left the request bound")

    def test_a_death_here_whose_order_shows_before_a_cut_is_unbound(self):
        # the same death, its stop job seen after it, then the server killed: the binding was undone at a clean end only,
        # and the client's retries stayed bound 10 minutes (the 8th review of v1.43.0, 2026-11-05)
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        saved = m.unit_stopping_by_order
        m.unit_stopping_by_order = lambda: self.E1.ordered_now                  # the stop job, seen after the death
        self.addCleanup(setattr, m, "unit_stopping_by_order", saved)
        self.E1.mode = "vllm_dead_late_order_cut"
        r = dict(self.turns("conv-local-late-order-cut", 1)[0], stream=True)
        raw = json.dumps(r).encode()
        c = http.client.HTTPConnection("127.0.0.1", self.port1, timeout=30)
        c.request("POST", "/v1/chat/completions", raw,
                  {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
        resp = c.getresponse()
        fp = m.body_fingerprint(raw, "/v1/chat/completions")
        got, premise = b"", None
        try:
            while True:
                b = resp.read1(65536)
                if not b:
                    break
                got += b
                if premise is None and b"EngineCore" in got:
                    premise = m.killer_box(fp)  # bound at the death, its order not seen yet (the 9th review of v1.43.0)
        except (OSError, http.client.HTTPException):
            pass                                                                # cut, as its engine's end was
        c.close()
        self.assertIn(b"EngineCore", got)
        self.assertEqual(premise, "", "the test needs the request bound at the death")
        self.assertIn("conv-local-late-order-cut-0", self.E1.users, "the test needs this box's engine to have it")
        deadline = time.monotonic() + 3
        while m.killer_box(fp) is not None and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertIsNone(m.killer_box(fp), "an order shown before the stream's cut left the request bound")

    def test_a_death_here_whose_client_leaves_before_its_order_shows_is_unbound(self):
        # its client gone at the death (the SDKs raise on that event), before the stream's end: that end, where a stop job
        # seen late undoes the binding, never came, and the request stayed bound 10 minutes after an order (the 9th review
        # of v1.43.0, 2026-11-05)
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        saved = m.unit_stopping_by_order
        m.unit_stopping_by_order = lambda: self.E1.ordered_now                  # the stop job, seen after the death
        self.addCleanup(setattr, m, "unit_stopping_by_order", saved)
        self.E1.mode = "vllm_dead_late_order"
        r = dict(self.turns("conv-local-leaves-first", 1)[0], stream=True)
        raw = json.dumps(r).encode()
        fp = m.body_fingerprint(raw, "/v1/chat/completions")
        c = http.client.HTTPConnection("127.0.0.1", self.port1, timeout=30)
        c.connect()
        c.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))   # it leaves with a reset
        c.request("POST", "/v1/chat/completions", raw,
                  {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
        resp = c.getresponse()
        got = b""
        while b"EngineCore" not in got:
            b = resp.read1(65536)
            if not b:
                break
            got += b
        self.assertIn(b"EngineCore", got)
        self.assertEqual(m.killer_box(fp), "", "the test needs the request bound at the death")
        resp.close()
        c.close()                                                               # before its order shows (0.4 s on)
        time.sleep(3.5)
        self.assertIn("conv-local-leaves-first-0", self.E1.users, "the test needs this box's engine to have it")
        self.assertIsNone(m.killer_box(fp), "an order shown after its client left left the request bound")

    def test_a_death_whose_own_read_saw_the_order_stays_unbound_at_the_cut(self):
        # its death came while its stop job ran (nothing bound), the cut was read once the job was over: the cut bound
        # it 10 minutes, as if an engine had died on it (the 9th review of v1.43.0, 2026-11-05)
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        saved = m.unit_stopping_by_order
        m.unit_stopping_by_order = lambda: self.E1.ordered_now
        self.addCleanup(setattr, m, "unit_stopping_by_order", saved)
        self.E1.mode = "vllm_dead_ordered_then_cut"
        r = dict(self.turns("conv-ordered-then-cut", 1)[0], stream=True)
        raw = json.dumps(r).encode()
        c = http.client.HTTPConnection("127.0.0.1", self.port1, timeout=30)
        self.addCleanup(c.close)
        c.request("POST", "/v1/chat/completions", raw,
                  {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
        resp = c.getresponse()
        fp = m.body_fingerprint(raw, "/v1/chat/completions")
        got, premise = b"", "unread"
        try:
            while True:
                b = resp.read1(65536)
                if not b:
                    break
                got += b
                if premise == "unread" and b"EngineCore" in got:
                    premise = m.killer_box(fp)  # its death read its stop job: nothing bound (the 10th review)
        except (OSError, http.client.HTTPException):
            pass                                                                # cut
        self.assertIn(b"EngineCore", got)
        self.assertIsNone(premise, "the test needs the death's own read to see the order")
        self.assertIn("conv-ordered-then-cut-0", self.E1.users, "the test needs this box's engine to have it")
        time.sleep(0.5)
        self.assertIsNone(m.killer_box(m.body_fingerprint(raw, "/v1/chat/completions")),
                          "a death its own read saw ordered was bound at the cut")

    def test_a_cut_is_bound_before_its_client_sees_it(self):
        # an engine that dies with no death event (a crash): its connection was shut, then systemd read again and the
        # request bound, a quick retry arriving unbound meanwhile, free to reach the other box (decision 1019; the 9th
        # review of v1.43.0, 2026-11-05). systemd is slow here, as under the memory pressure that ends engines
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        saved = m.unit_stopping_by_order
        reads = []

        def slow():
            reads.append(1)
            time.sleep(1.0)
            return False
        m.unit_stopping_by_order = slow
        self.addCleanup(setattr, m, "unit_stopping_by_order", saved)
        self.E1.mode = "cut"
        r = dict(self.turns("conv-cut-bound", 1)[0], stream=True)
        raw = json.dumps(r).encode()
        fp = m.body_fingerprint(raw, "/v1/chat/completions")
        c = http.client.HTTPConnection("127.0.0.1", self.port1, timeout=30)
        self.addCleanup(c.close)
        c.request("POST", "/v1/chat/completions", raw,
                  {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
        resp = c.getresponse()
        try:
            while resp.read1(65536):
                pass
        except (OSError, http.client.HTTPException):
            pass                                                                # cut: its client retries now
        self.assertIn("conv-cut-bound-0", self.E1.users, "the test needs this box's engine to have it")
        self.assertEqual(m.killer_box(fp), "", "the cut reached its client before the request was bound")
        self.assertEqual(len(reads), 1, "the cut read systemd more than once")

    def _sent(self, raw):
        """raw sent to this box as its own client's request: what it got, cut or ended."""
        c = http.client.HTTPConnection("127.0.0.1", self.port1, timeout=30)
        try:
            c.request("POST", "/v1/chat/completions", raw,
                      {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
            try:
                return c.getresponse().read()
            except http.client.IncompleteRead as e:
                return e.partial
        finally:
            c.close()

    def test_a_retry_that_bound_nothing_keeps_the_binding_a_death_before_it_made(self):
        # a retry whose death read saw its lane's stop bound nothing, and at its ordered end undid the binding an earlier
        # real death of the same body had made, the next retry free to reach the other box (decision 1019; the 12th
        # review of v1.43.0, 2026-11-05)
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        m = self.mod
        ordered = {"now": False}
        saved = m.unit_stopping_by_order
        m.unit_stopping_by_order = lambda: ordered["now"]
        self.addCleanup(setattr, m, "unit_stopping_by_order", saved)
        self.E1.mode = "vllm_dead_stream"
        raw = json.dumps(dict(self.turns("conv-unbound-retry", 1)[0], stream=True)).encode()
        fp = m.body_fingerprint(raw, "/v1/chat/completions")
        self.assertIn(b"EngineCore", self._sent(raw))
        self.assertEqual(m.killer_box(fp), "", "the test needs the first death bound here")
        ordered["now"] = True                   # its lane's stop job: the retry's death is the order's, seen at once
        self.assertIn(b"EngineCore", self._sent(raw))
        self.assertEqual(self.E1.users.count("conv-unbound-retry-0"), 2, "the test needs the retry served here")
        self.assertEqual(m.killer_box(fp), "", "a retry that bound nothing undid the binding of a death before it")

    def test_a_try_ended_by_order_keeps_the_binding_its_first_try_made(self):
        # its first try closed by its engine (a real death: bound), the request held and sent again: the second try's
        # ordered end undid the first try's binding, the binding state kept for the whole request (decision 1019; the
        # 12th review of v1.43.0, 2026-11-05)
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        m = self.mod
        ordered = {"now": False}
        saved = m.unit_stopping_by_order
        m.unit_stopping_by_order = lambda: ordered["now"]
        self.addCleanup(setattr, m, "unit_stopping_by_order", saved)
        held = m.H._hold

        def back_at_once(h, why, streamed):
            ordered["now"] = True               # the try sent again runs as its lane is stopped by order
            self.E1.mode = "vllm_dead_stream"
            return True
        m.H._hold = back_at_once
        self.addCleanup(setattr, m.H, "_hold", held)
        self.E1.mode = "die"
        raw = json.dumps(dict(self.turns("conv-two-tries", 1)[0], stream=True)).encode()
        fp = m.body_fingerprint(raw, "/v1/chat/completions")
        out = self._sent(raw)
        self.assertEqual(self.E1.users.count("conv-two-tries-0"), 2, "the test needs both tries on this box's engine")
        self.assertIn(b"EngineCore", out)
        self.assertEqual(m.killer_box(fp), "", "a try ended by order undid the binding its first try made")

    def test_a_stop_during_a_dead_streams_reads_counts_nothing_and_holds_nothing_back(self):
        # dead, a vLLM stream stayed a stop target while systemd was read at its death and at its clean end: a floor stop
        # that landed then counted it, started the floor's episode, and new generations were refused for nothing (the
        # 12th review of v1.43.0, 2026-11-05)
        if not self.INPROC:
            self.skipTest("the stop is made in this test's process")
        m = self.mod
        saved = m.unit_stopping_by_order
        self.addCleanup(setattr, m, "unit_stopping_by_order", saved)
        floor = dict(m._FLOOR)                                                  # the floor's state, given back
        self.addCleanup(lambda: (m._FLOOR.clear(), m._FLOOR.update(floor)))
        self.E1.mode = "vllm_dead_more"         # its death said, its stream's end 2 s later
        for which in (1, 2):                    # the read at its death, the read at its clean end
            with self.subTest(read=which):
                m._FLOOR.clear(); m._FLOOR.update(floor)                        # no episode of the read before
                reads, stops = [], []

                def a_stop_meanwhile():
                    reads.append(1)
                    if len(reads) == which:
                        stops.append(m.stop_generations("this test's memory floor", floor=True))
                    return False
                m.unit_stopping_by_order = a_stop_meanwhile
                out = self._sent(json.dumps(dict(self.turns(f"conv-dead-read-{which}", 1)[0], stream=True)).encode())
                self.assertIn(b"EngineCore", out)
                self.assertEqual(len(stops), 1, "the test needs its stop")
                self.assertEqual(stops[0][0], 0, "a dead stream was counted as stopped")
                self.assertIsNone(m.floor_refusal(), "a floor that stopped nothing held new generations back")

    def test_a_hops_death_relayed_is_never_said_an_order_by_a_stop_after_it(self):
        # this box's engine died on a request the other box sent, the death relayed to it, and a floor stop here before
        # the stream's end counted the dead stream and cut it as stopped, telling that box the request ended by order,
        # which undid the binding its real death had made there (decision 1019; the 12th review of v1.43.0, 2026-11-05)
        if not self.INPROC:
            self.skipTest("the stop is made in this test's process")
        m = self.mod
        floor = dict(m._FLOOR)
        self.addCleanup(lambda: (m._FLOOR.clear(), m._FLOOR.update(floor)))
        self.E1.mode = "vllm_dead_more"         # its death said, then more events for 2 s before its end
        got = {}

        def hop():
            try:
                got["out"] = self.tls_call(self.pport1, "/v1/chat/completions",
                                           dict(self.chat("qwen3.8-27b"), user="hop-dead-then-stop", stream=True))[1]
            except http.client.IncompleteRead as e:
                got["out"] = e.partial
        th = threading.Thread(target=hop, daemon=True)
        th.start()
        deadline = time.monotonic() + 10
        while "hop-dead-then-stop" not in self.E1.users and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIn("hop-dead-then-stop", self.E1.users, "the test needs this box's engine to have it")
        time.sleep(0.6)                         # its death relayed (0.15 s in), its stream not over (2 s)
        stopped = m.stop_generations("this test's memory floor", floor=True)
        th.join(30)
        self.assertEqual(stopped[0], 0, "a dead stream was counted as stopped")
        self.assertIn(b"EngineCore", got.get("out", b""))
        self.assertNotIn(b"x_alzeo_ended", got["out"], "a real death relayed was then said an order")

    def _left_after_its_first_event(self, tag):
        """A stream this box sends to the other box (one generation here, none there), its client gone after the first
        event it got: the request's fingerprint, and the generation that kept this box busy."""
        m = self.mod
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 1, 6.0)     # one here, none there: it goes there
        raw = json.dumps(dict(self.turns(tag, 1)[0], stream=True)).encode()
        c = http.client.HTTPConnection("127.0.0.1", self.port1, timeout=30)
        c.connect()
        c.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))   # it leaves with a reset
        c.request("POST", "/v1/chat/completions", raw,
                  {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
        resp = c.getresponse()
        resp.read1(65536)                       # its first event
        resp.close()
        c.close()                               # gone: the next event's write fails, before the death comes
        return m.body_fingerprint(raw, "/v1/chat/completions"), held

    def _left_at_once(self, tag, stream=False, after=0.3, reset=True, **extra):
        """A request this box sends to the other box (one generation here, none there), its client gone `after` seconds
        after sending it, with a reset (or a plain close, reset=False): the request's fingerprint, and the generation
        that kept this box busy."""
        m = self.mod
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 1, 6.0)     # one here, none there: it goes there
        raw = json.dumps(dict(self.turns(tag, 1)[0], stream=stream, **extra)).encode()
        c = http.client.HTTPConnection("127.0.0.1", self.port1, timeout=30)
        c.connect()
        if reset:
            c.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))   # it leaves with a reset
        c.request("POST", "/v1/chat/completions", raw,
                  {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
        time.sleep(after)
        c.close()
        return m.body_fingerprint(raw, "/v1/chat/completions"), held

    def _bound_within(self, fp, secs):
        m = self.mod
        deadline = time.monotonic() + secs
        while m.killer_box(fp) is None and time.monotonic() < deadline:
            time.sleep(0.05)
        return m.killer_box(fp)

    def _the_other_box_is(self, how):
        """spark2 a LateBox that does `how`, listed as serving the 27B: its class, whose `got` says what it read."""
        late = type("Late", (LateBox,), {"how": how, "got": []})
        self.repoint(tls_peer(self, late))
        self.mod._PAIR_MODELS["spark2"] = {"ts": time.monotonic(), "models": ("qwen3.8-27b",), "rows": [],
                                           "error": None, "answered": True, "known": ("qwen3.8-27b",),
                                           "known_ts": time.monotonic(), "read_s": 0.01, "running": 0,
                                           "revision": TEST_REV}
        self.addCleanup(self.mod._PAIR_COOL.pop, "spark2", None)
        return late

    def _stopped_in_its_count(self, tag, floor, hop=False, fails=False):
        """A long prompt this box keeps (the other box busier), a stop during its count (floor or not), the count
        answering too long (or failing): (status, body) its client got, None when the proxy cut it. hop: sent by the
        other box to this box's pair port."""
        m = self.mod
        m._POOL.update(tokens=10000, window=9990, ts=time.time())
        self.addCleanup(m._POOL.update, tokens=None, window=None, ts=0.0)
        key = m.UPSTREAM_API_KEY
        self.addCleanup(setattr, m, "UPSTREAM_API_KEY", key)
        m.UPSTREAM_API_KEY = KEY_1
        windows, floor_state = dict(m._STOP_WINDOWS), dict(m._FLOOR)
        self.addCleanup(lambda: (m._STOP_WINDOWS.clear(), m._STOP_WINDOWS.update(windows)))
        self.addCleanup(lambda: (m._FLOOR.clear(), m._FLOOR.update(floor_state)))
        if not hop:                             # a request the other box sent on is no box's to share
            self.busy(self.E2, self.port2, KEY_2, "marker-27b", 2, 10.0)
            self.E2.served[:] = ["qwen3.8-27b", f"marker-busy-{tag}"]
            self.until_listed(f"marker-busy-{tag}")
        self.E1.count_delay = 1.5
        if fails:
            self.E1.mode = "count_fails"
        body = dict(self.chat("qwen3.8-27b"), user=tag, messages=[{"role": "user", "content": f"{tag} " + "count " * 12000}])
        got = {}

        def send():
            try:
                got["r"] = (self.tls_call(self.pport1, "/v1/chat/completions", body)[:2] if hop
                            else self.call("/v1/chat/completions", body)[:2])
            except (OSError, http.client.HTTPException):
                got["r"] = None
        th = threading.Thread(target=send, daemon=True)
        th.start()
        self.assertTrue(_until(lambda: any(p == "/tokenize" for p, *_ in self.E1.seen), 5), "no count was made")
        self.assertEqual(m.stop_generations(f"this test's {'memory floor' if floor else 'Abort all'}", floor=floor)[0], 1,
                         "the stop did not reach it")
        th.join(30)
        return got.get("r")

    def test_a_hop_a_stop_marks_during_its_count_still_gets_its_400(self):
        # a request the other box sent on, its count too long: the size guard's 400, which the entry serves itself on,
        # became a 503 ended by order once a stop reached it during its count (the 4th review of v1.44.0)
        if not self.INPROC:
            self.skipTest("this box's pool and stop are in this test's process")
        r = self._stopped_in_its_count("conv-hop-too-long", floor=False, hop=True)
        self.assertIsNotNone(r, "the hop's request was cut")
        st, out = r
        self.assertEqual(st, 400, out[:300])
        self.assertIn(b"context_length_exceeded", out)

    def test_a_request_the_floor_stops_during_its_count_is_refused_by_it_never_handed_on(self):
        # its count too long for this box's pool, which hands it to the other box's: the floor's stop during the count is
        # answered by the floor, with when to come back, the other box never getting it (the 4th review of v1.44.0)
        if not self.INPROC:
            self.skipTest("this box's pool and stop are in this test's process")
        r = self._stopped_in_its_count("conv-floor-too-long", floor=True)
        self.assertIsNotNone(r, "a request the floor stopped was cut bare")
        st, out = r
        self.assertIn(st, (503, 529), out[:300])
        self.assertIn(b"memory floor", out)
        self.assertNotIn("conv-floor-too-long", self.E2.users, "a request the floor stopped went to the other box")

    def _left_then(self, how, tag, reset=True):
        """A shared request to a LateBox that does `how` once its client left (answer_now set right after): the box."""
        late = self._the_other_box_is(how)
        late.answer_now = threading.Event()
        fp, held = self._left_at_once(tag, after=0.1, reset=reset)
        late.answer_now.set()
        self.addCleanup(lambda: [th.join(30) for th in held])
        self.assertTrue(_until(lambda: f"{tag}-0" in late.got, 5), "the test needs the other box to have it")
        return late, fp

    def test_a_box_that_closes_on_a_request_as_its_client_leaves_is_read_with_its_client_gone(self):
        # its close came within the second its client left, before the watch's first look: read as one with its client
        # there (the 6th review of v1.44.0: no test held the immediate look of that branch)
        if not self.INPROC:
            self.skipTest("the journal is read in this test's process")
        self._left_then("close_soon", "conv-entry-close-soon")
        self.assertTrue(self.until_in_journal("spark1", "its client gone, spark2 had the request and closed the "
                                                        "connection with no answer", within=3.0),
                        "a close that came as its client left was read with its client there")

    def test_a_refusal_as_its_client_leaves_moves_no_conversation_there(self):
        # a 409 that came once its client had left read as served: the conversation's next turn went to the box that
        # refused it (the 6th review of v1.44.0)
        if not self.INPROC:
            self.skipTest("the affinities are read in this test's process")
        m = self.mod
        before = len(m._AFFINITY.get("spark2", ()))
        self._left_then("moved_soon", "conv-entry-moved-soon")
        self.assertTrue(self.until_in_journal("spark1", "CLIENT GONE during pair", within=3.0))
        time.sleep(0.3)
        self.assertEqual(len(m._AFFINITY.get("spark2", ())), before, "a refusal after its client left moved its turns there")
        # forgotten there, never paused: that box answered (the 7th review of v1.44.0)
        self.assertLessEqual(m._PAIR_COOL.get("spark2", 0), time.monotonic(), "a box that answered was paused")

    def test_an_error_as_its_client_leaves_keeps_the_conversation_there(self):
        # an error that is no refusal (a 500 that is no engine's death) once its client had left: the conversation's
        # prefix forgotten on the box that holds it in its cache, its retry placed by balance alone and prefilled again
        # elsewhere (the 7th review of v1.44.0)
        if not self.INPROC:
            self.skipTest("the affinities are read in this test's process")
        m = self.mod
        before = len(m._AFFINITY.get("spark2", ()))
        self._left_then("error_soon", "conv-entry-error-soon")
        self.assertTrue(self.until_in_journal("spark1", "CLIENT GONE during pair", within=3.0))
        time.sleep(0.3)
        self.assertGreater(len(m._AFFINITY.get("spark2", ())), before, "an error after its client left moved its turns away")

    def test_an_answer_cut_as_its_client_leaves_is_read_with_its_client_gone(self):
        # the head of an answer not streamed, then its cut, within the second its client left: read with its client
        # there, a 502 written to nobody and an upstream cut said (the 7th review of v1.44.0)
        if not self.INPROC:
            self.skipTest("the journal is read in this test's process")
        cuts = self.journal("spark1").count("UPSTREAM CUT pair")
        self._left_then("cut_soon", "conv-entry-cut-soon")
        self.assertTrue(self.until_in_journal("spark1", "CLIENT GONE during pair", within=3.0),
                        "an answer cut as its client left was read with its client there")
        self.assertEqual(self.journal("spark1").count("UPSTREAM CUT pair"), cuts)

    def test_a_whole_answer_as_its_client_leaves_goes_to_nobody(self):
        # a whole answer that came within the second its client left, before the watch's first look: written to a
        # client gone, said served or with no outcome (the 6th review of v1.44.0)
        if not self.INPROC:
            self.skipTest("the journal is read in this test's process")
        gone = self.journal("spark1").count("CLIENT GONE during pair")
        oks = self.journal("spark1").count("ok pair")
        self._left_then("answer_soon", "conv-entry-answer-soon", reset=False)
        self.assertTrue(_until(lambda: self.journal("spark1").count("CLIENT GONE during pair") > gone, 5),
                        "a whole answer that came as its client left was not said gone")
        self.assertEqual(self.journal("spark1").count("ok pair"), oks, "an answer written to nobody was said served")

    def test_a_box_that_refuses_a_shared_request_as_its_client_leaves_serves_it_nowhere(self):
        # its 503 came within the second its client left, before the watch's tick: read as a refusal with its client
        # there, the request was served here for nobody (the 5th review of v1.44.0)
        if not self.INPROC:
            self.skipTest("the pause is read in this test's process")
        m = self.mod
        self._left_then("away_soon", "conv-entry-left-away-soon")   # its refusal once its client left
        self.assertTrue(_until(lambda: m._PAIR_COOL.get("spark2", 0) > time.monotonic(), 6),
                        "a box that refused a shared request as its client left was not paused")
        # its client's leaving read with the refusal: the request served nowhere, where the path of a client still there
        # had this box serve it, cut a moment later as its client was found gone
        self.assertTrue(self.until_in_journal("spark1", "its client left, and spark2 gets nothing", within=3.0),
                        "the refusal was read as one with its client there")
        self.assertFalse(self.until_in_journal("spark1", "this box serves it", within=0.5),
                         "a request whose client left was served here")

    def test_a_box_away_that_refuses_a_shared_request_after_its_client_left_is_paused(self):
        # its 503 came once its client had left: said served, the box was neither paused nor its conversation
        # forgotten there, its retry refused again (the 3rd review of v1.44.0)
        if not self.INPROC:
            self.skipTest("the pause is read in this test's process")
        m = self.mod
        late = self._the_other_box_is("away")
        fp, held = self._left_at_once("conv-entry-left-away")
        self.assertTrue(_until(lambda: "conv-entry-left-away-0" in late.got, 5), "the test needs the other box to have it")
        self.assertTrue(_until(lambda: m._PAIR_COOL.get("spark2", 0) > time.monotonic(), 6),
                        "a box that refused a shared request after its client left was not paused")
        self.assertIsNone(m.killer_box(fp), "a refusal was bound")
        # its client gone first, its path pinned: with the watch late, the refusal took the path of a client still
        # there, which pauses too and serves it here for nobody (the 4th review of v1.44.0)
        self.assertTrue(self.until_in_journal("spark1", "its client left, and spark2 gets nothing", within=3.0))
        self.assertNotIn("conv-entry-left-away-0", self.E1.users, "a request whose client left was served here")
        for th in held:
            th.join(30)

    def test_a_request_a_stop_marks_during_its_count_is_never_handed_on_as_too_long(self):
        # its count said too long for this box's pool, which hands it to the other box's (v6.52): a request a stop
        # marked during its count went there and was served (the 3rd review of v1.44.0)
        if not self.INPROC:
            self.skipTest("this box's pool and stop are in this test's process")
        m = self.mod
        m._POOL.update(tokens=10000, window=9990, ts=time.time())
        self.addCleanup(m._POOL.update, tokens=None, window=None, ts=0.0)
        key = m.UPSTREAM_API_KEY
        self.addCleanup(setattr, m, "UPSTREAM_API_KEY", key)
        m.UPSTREAM_API_KEY = KEY_1
        windows = dict(m._STOP_WINDOWS)
        self.addCleanup(lambda: (m._STOP_WINDOWS.clear(), m._STOP_WINDOWS.update(windows)))
        held = self.busy(self.E2, self.port2, KEY_2, "marker-27b", 2, 10.0)
        self.E2.served[:] = ["qwen3.8-27b", "marker-busy-long"]
        self.until_listed("marker-busy-long")
        self.E1.count_delay = 1.5               # its count answers, too long, 1.5 s on
        body = dict(self.chat("qwen3.8-27b"), user="conv-stopped-too-long",
                    messages=[{"role": "user", "content": "stopped too long " + "count " * 12000}])
        t = threading.Thread(target=lambda: self._quiet_call(body), daemon=True)
        t.start()
        self.assertTrue(_until(lambda: any(p == "/tokenize" for p, *_ in self.E1.seen), 5), "no count was made")
        self.assertEqual(m.stop_generations("this test's Abort all", floor=False)[0], 1, "the stop did not reach it")
        t.join(30)
        self.assertNotIn("conv-stopped-too-long", self.E2.users, "a request a stop marked was handed to the other box")
        self.assertTrue(self.until_in_journal("spark1", "stopped by the cockpit", within=3.0),
                        "a request a stop marked during its count was not cut as stopped")
        for th in held:
            th.join(30)

    def test_a_request_the_floor_stops_during_its_count_is_refused_by_the_floor(self):
        # the floor's stop reached it during its count, which then failed: refused by the floor, with when to come
        # back, never a bare cut (no test drove the floor's branch, the 3rd review of v1.44.0)
        if not self.INPROC:
            self.skipTest("this box's pool and stop are in this test's process")
        r = self._stopped_in_its_count("conv-floor-in-count", floor=True, fails=True)
        self.assertIsNotNone(r, "a request the floor stopped was cut bare")
        st, out = r
        self.assertIn(st, (503, 529), out[:300])
        self.assertIn(b"memory floor", out)
        self.assertNotIn("conv-floor-in-count", self.E2.users, "a request the floor stopped reached the other box")

    def _quiet_call(self, body):
        """(status, body) of a call, or (None, b"") when the proxy cut it."""
        try:
            st, out, _ = self.call("/v1/chat/completions", body)
            return st, out
        except (OSError, http.client.HTTPException):
            return None, b""

    def test_a_box_that_closes_on_a_shared_request_after_its_client_left_is_paused(self):
        # bound as its client left, but said "served": the box that closed on it with no answer was neither paused nor
        # its conversation forgotten there, where the same close with its client present pauses it (the 2nd review of
        # v1.44.0)
        if not self.INPROC:
            self.skipTest("the pause is read in this test's process")
        m = self.mod
        late = self._the_other_box_is("close")
        fp, held = self._left_at_once("conv-entry-left-pause")
        self.assertEqual(self._bound_within(fp, 10), "spark2", "the test needs the close bound")
        # its client gone first, its path pinned: with the watch late, the same close took the path of a client still
        # there, which binds and pauses too (the 3rd review of v1.44.0)
        self.assertTrue(self.until_in_journal("spark1", "its client gone, spark2 had the request and closed the "
                                                        "connection with no answer", within=3.0))
        self.assertTrue(_until(lambda: m._PAIR_COOL.get("spark2", 0) > time.monotonic(), 3),
                        "a box that closed on a shared request after its client left was not paused")
        self.assertIn("conv-entry-left-pause-0", late.got)
        for th in held:
            th.join(30)

    def test_an_ollama_stream_that_ends_whole_as_its_client_leaves_binds_nothing(self):
        # its last line had no line end, written once the stream had ended whole; its write failed, its client gone,
        # and the read after it, on the connection urllib had closed, read as a cut: bound 10 minutes (the 2nd review
        # of v1.44.0)
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        m = self.mod
        self.E2.mode = "ndjson_unended_end"
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 1, 6.0)     # one here, none there: it goes there
        raw = json.dumps({"model": "qwen3.8-27b", "stream": True, "user": "conv-ollama-end-0",
                          "messages": self.turns("conv-ollama-end", 1)[0]["messages"]}).encode()
        c = http.client.HTTPConnection("127.0.0.1", self.port1, timeout=30)
        c.connect()
        c.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        c.request("POST", "/api/chat", raw, {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
        resp = c.getresponse()
        resp.read1(65536)                       # its first line
        resp.close()
        c.close()                               # gone with a reset, before its last line
        time.sleep(PAIR_GRACE_READ_S)
        self.assertIn("conv-ollama-end-0", self.E2.users, "the test needs the other box's engine to have it")
        self.assertIsNone(m.killer_box(m.body_fingerprint(raw, "/api/chat")),
                          "a stream that ended whole was bound when its last line could not be written")
        for th in held:
            th.join(30)

    def test_an_answer_that_comes_after_its_client_left_goes_to_nobody(self):
        # an answer not streamed whose body came while its client left, within the seconds the watch keeps the
        # connection: written to a client gone, it was said served, "ok pair" (the 2nd review of v1.44.0)
        if not self.INPROC:
            self.skipTest("the journal is read in this test's process")
        gone = self.journal("spark1").count("CLIENT GONE during pair")
        oks = self.journal("spark1").count("ok pair")
        self.E2.mode = "slow_body"              # its head at once, its body's second half 1.5 s on
        # a plain close: the first write after it goes through, and "ok pair" was said (a reset made it fail, the 3rd
        # review of v1.44.0)
        fp, held = self._left_at_once("conv-entry-left-late-200", reset=False)
        self.assertTrue(_until(lambda: "conv-entry-left-late-200-0" in self.E2.users, 5))
        self.assertTrue(_until(lambda: self.journal("spark1").count("CLIENT GONE during pair") > gone, 8),
                        "an answer that came after its client left was not said gone")
        self.assertEqual(self.journal("spark1").count("ok pair"), oks, "an answer written to nobody was said served")
        for th in held:
            th.join(30)

    def test_a_request_a_stop_marks_during_its_count_is_cut_never_held_or_handed_on(self):
        # the size guard's count under way when the cockpit's stop came, the count failed (its engine stopping): the
        # stop's mark lasted through the hold that followed, or the other box took the request over, a stop's request
        # served all the same (the 2nd review of v1.44.0)
        if not self.INPROC:
            self.skipTest("this box's pool and stop are in this test's process")
        self._stopped_in_its_count("conv-stopped-in-count", floor=False, fails=True)
        self.assertNotIn("conv-stopped-in-count", self.E2.users, "a request a stop marked reached the other box")
        self.assertTrue(self.until_in_journal("spark1", "stopped by the cockpit", within=3.0),
                        "a request a stop marked during its count was not cut as stopped")

    def test_a_death_the_other_box_answers_after_this_boxs_client_left_is_bound(self):
        # an answer not streamed, its client gone: the other box's engine died on it a second later, which that box
        # answered 502 within the seconds the watch keeps the connection; read after its client left, it bound nothing,
        # and its client's retry ran on this box's engine too (decision 1112)
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        self.E2.mode = "die"
        fp, held = self._left_at_once("conv-entry-left-502", x_die_after=1.5)
        bound = self._bound_within(fp, 10)
        self.assertIn("conv-entry-left-502-0", self.E2.users, "the test needs the other box's engine to have it")
        self.assertEqual(bound, "spark2", "a death the other box answered after this box's client left was left unbound")
        for th in held:
            th.join(30)

    def test_a_stream_the_other_box_begins_after_this_boxs_client_left_is_read_for_a_death(self):
        # its client gone before the other box's stream began: the entry left at its head, unread, the death that
        # stream carried a second later (decision 1112)
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        self.E2.mode = "vllm_dead_after_1s"
        fp, held = self._left_at_once("conv-entry-left-late", stream=True, x_delay=1.6)
        bound = self._bound_within(fp, 10)
        self.assertIn("conv-entry-left-late-0", self.E2.users, "the test needs the other box's engine to have it")
        self.assertEqual(bound, "spark2", "a death in a stream begun after this box's client left was left unbound")
        for th in held:
            th.join(30)

    def test_a_stream_whose_head_this_box_could_not_write_is_read_for_a_death(self):
        # its client reset before the watch saw it go: the stream's head failed to reach it, and the entry left there,
        # unread, the death the stream carried a second later (decision 1112)
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        self.E2.mode = "vllm_dead_after_1s"
        fp, held = self._left_at_once("conv-entry-left-head", stream=True, after=0.05, x_delay=0.5)
        bound = self._bound_within(fp, 10)
        self.assertIn("conv-entry-left-head-0", self.E2.users, "the test needs the other box's engine to have it")
        self.assertEqual(bound, "spark2", "a death in a stream whose head failed to reach its client was left unbound")
        for th in held:
            th.join(30)

    def test_a_box_that_closes_on_a_request_after_its_client_left_is_bound(self):
        # its client gone, the other box closed the connection on the request with no answer a second later (its power,
        # a restart of its proxy): bound as when its client stays (decision 1017), its retry otherwise free to run on
        # this box's engine (decision 1112)
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        late = self._the_other_box_is("close")
        fp, held = self._left_at_once("conv-entry-left-drop")
        _until(lambda: "conv-entry-left-drop-0" in late.got, 5)
        self.assertIn("conv-entry-left-drop-0", late.got, "the test needs the other box to have it")
        self.assertEqual(self._bound_within(fp, 10), "spark2",
                         "a box that closed on the request after its client left was left unbound")
        for th in held:
            th.join(30)

    def test_an_answer_cut_after_its_client_left_is_bound(self):
        # an answer not streamed whose head came, its client gone, then cut by the other box a second later: a death, as
        # when its client stays (decision 1112)
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        late = self._the_other_box_is("cut")
        fp, held = self._left_at_once("conv-entry-left-cut")
        _until(lambda: "conv-entry-left-cut-0" in late.got, 5)
        self.assertIn("conv-entry-left-cut-0", late.got, "the test needs the other box to have it")
        self.assertEqual(self._bound_within(fp, 10), "spark2", "an answer cut after its client left was left unbound")
        for th in held:
            th.join(30)

    def _the_watch_ends_it(self, how):
        # the other box silent past the seconds the watch keeps the connection, then ended by the watch: that end is this
        # box's, no death (decision 1112)
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        late = self._the_other_box_is(how)
        fp, held = self._left_at_once(f"conv-entry-left-{how}")
        time.sleep(PAIR_GRACE_READ_S + 2)
        self.assertIn(f"conv-entry-left-{how}-0", late.got, "the test needs the other box to have it")
        self.assertIsNone(self.mod.killer_box(fp), "this box ending the connection was read as a death")
        for th in held:
            th.join(30)

    def test_the_watchs_own_end_of_a_silent_box_binds_nothing(self):
        self._the_watch_ends_it("silent")

    def test_the_watchs_own_end_of_an_answer_gone_silent_binds_nothing(self):
        self._the_watch_ends_it("cut_silent")

    def test_a_death_the_other_box_sends_after_this_boxs_client_left_is_bound(self):
        # the other box's engine died on the request just after this box's client left, the death unrelayed: the watch
        # shut the connection at once, the death went unread, and the request unbound, its retry free to run on this
        # box's engine too (decision 1019; the 14th review of v1.43.0, decision 1112)
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        m = self.mod
        self.E2.mode = "vllm_dead_after_1s"
        fp, held = self._left_after_its_first_event("conv-entry-left-dead")
        deadline = time.monotonic() + 10
        while m.killer_box(fp) is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIn("conv-entry-left-dead-0", self.E2.users, "the test needs the other box's engine to have it")
        self.assertEqual(m.killer_box(fp), "spark2", "a death sent after this box's client left was left unbound")
        for th in held:
            th.join(30)

    def test_a_stream_that_ends_after_this_boxs_client_left_binds_nothing(self):
        # read on after its client left (decision 1112), a stream that ends as streams do binds nothing
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        m = self.mod
        self.E2.mode = "ok"
        fp, held = self._left_after_its_first_event("conv-entry-left-ok")
        time.sleep(PAIR_GRACE_READ_S)
        self.assertIn("conv-entry-left-ok-0", self.E2.users, "the test needs the other box's engine to have it")
        self.assertIsNone(m.killer_box(fp), "a stream that ended as streams do was bound")
        for th in held:
            th.join(30)

    def test_this_boxs_own_end_of_the_connection_is_no_death_of_the_other_boxs_engine(self):
        # its client gone, seen first by the watch, which ends the connection once its grace is over: the read under way
        # then fails, which read as the other box's engine cutting its stream, and bound the request (decision 1112)
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        m = self.mod
        self.E2.mode = "slow_10s"
        fp, held = self._left_after_its_first_event("conv-entry-left-slow")
        time.sleep(PAIR_GRACE_READ_S + 3)
        self.assertIn("conv-entry-left-slow-0", self.E2.users, "the test needs the other box's engine to have it")
        self.assertIsNone(m.killer_box(fp), "this box ending the connection was read as the other box's engine's death")
        for th in held:
            th.join(30)

    def test_a_cut_during_a_floor_stop_is_bound_and_no_stop_counts_it(self):
        # the cockpit's floor stop landed during the systemd read of an engine's cut: it counted the stream, cut by its
        # engine, as stopped, started the floor's episode for nothing, and the stream was left unbound, its retry free to
        # reach the other box's engine (decision 1019; the 11th review of v1.43.0, 2026-11-05)
        if not self.INPROC:
            self.skipTest("the stop is made in this test's process")
        m = self.mod
        saved = m.unit_stopping_by_order
        stops = []

        def a_stop_meanwhile():
            if not stops:
                stops.append(m.stop_generations("this test's memory floor", floor=True))
            return False
        m.unit_stopping_by_order = a_stop_meanwhile
        self.addCleanup(setattr, m, "unit_stopping_by_order", saved)
        floor = dict(m._FLOOR)                                                  # the floor's state, given back
        self.addCleanup(lambda: (m._FLOOR.clear(), m._FLOOR.update(floor)))
        self.E1.mode = "ndjson_cut"
        body = {"model": "qwen3.8-27b", "messages": [{"role": "user", "content": "an ollama stream"}], "stream": True,
                "user": "conv-cut-floor-0"}
        raw = json.dumps(body).encode()
        c = http.client.HTTPConnection("127.0.0.1", self.port1, timeout=30)
        self.addCleanup(c.close)
        c.request("POST", "/api/chat", raw, {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
        resp = c.getresponse()
        try:
            while resp.read1(65536):
                pass
        except (OSError, http.client.HTTPException):
            pass                                                                # cut
        self.assertIn("conv-cut-floor-0", self.E1.users, "the test needs this box's engine to have it")
        self.assertEqual(len(stops), 1, "the test needs its stop")
        self.assertEqual(stops[0][0], 0, "a stream its engine had cut was counted as stopped")
        self.assertEqual(m.killer_box(m.body_fingerprint(raw, "/api/chat")), "", "a cut during a stop was left unbound")

    def test_a_cut_under_an_order_binds_nothing_and_tells_the_hops_box(self):
        # the two ordered outcomes of an engine's cut with no death before it had no test (the 11th review of v1.43.0,
        # 2026-11-05): this box's own client's request stays unbound (decisions 1030 and 1041), and a request the other
        # box sent is said ended by order in its stream (decision 1031)
        if not self.INPROC:
            self.skipTest("the unit's job is said in this test's process")
        m = self.mod
        saved = m.unit_stopping_by_order
        m.unit_stopping_by_order = lambda: True
        self.addCleanup(setattr, m, "unit_stopping_by_order", saved)
        self.E1.mode = "cut"
        r = dict(self.turns("conv-cut-ordered", 1)[0], stream=True)
        try:
            self.call("/v1/chat/completions", r)
        except http.client.IncompleteRead:
            pass                                                                # cut, as its engine's end was
        self.assertIn("conv-cut-ordered-0", self.E1.users, "the test needs this box's engine to have it")
        self.assertIsNone(m.killer_box(m.body_fingerprint(json.dumps(r).encode(), "/v1/chat/completions")),
                          "a cut under an order bound this box's own request")
        try:
            st, out, _ = self.tls_call(self.pport1, "/v1/chat/completions",
                                       dict(self.chat("qwen3.8-27b"), user="hop-cut-ordered", stream=True))
        except http.client.IncompleteRead as e:                                 # cut, its order said before
            out = e.partial
        self.assertIn(b'"x_alzeo_ended":"order"', out, out[-400:])

    def test_an_ndjson_cut_is_bound_before_its_client_sees_it(self):
        # the 9th review's fix reached SSE streams alone: an Ollama stream, or a non-streamed answer, was cut to its client
        # before its request was bound, and its retry went to the other box (decision 1019; the 10th review of v1.43.0,
        # 2026-11-05)
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        saved = m.unit_stopping_by_order
        reads = []

        def slow():
            reads.append(1)
            time.sleep(1.0)
            return False
        m.unit_stopping_by_order = slow
        self.addCleanup(setattr, m, "unit_stopping_by_order", saved)
        self.E1.mode = "ndjson_cut"
        body = {"model": "qwen3.8-27b", "messages": [{"role": "user", "content": "an ollama stream"}], "stream": True,
                "user": "conv-nd-cut-0"}
        raw = json.dumps(body).encode()
        fp = m.body_fingerprint(raw, "/api/chat")
        c = http.client.HTTPConnection("127.0.0.1", self.port1, timeout=30)
        self.addCleanup(c.close)
        c.request("POST", "/api/chat", raw, {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
        resp = c.getresponse()
        try:
            while resp.read1(65536):
                pass
        except (OSError, http.client.HTTPException):
            pass                                                                # cut: its client retries now
        self.assertIn("conv-nd-cut-0", self.E1.users, "the test needs this box's engine to have it")
        self.assertEqual(m.killer_box(fp), "", "an Ollama stream's cut reached its client before the binding")
        self.assertEqual(len(reads), 1, "the cut read systemd more than once")

    def _ndjson_peer(self, marker, script):
        """The other box serving the 27B, whose /api/chat streams NDJSON as script(chunk, handler) writes it."""
        Base = fake_peer(["qwen3.8-27b", marker], PEER_ANSWER)

        class Peer(Base):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                Base.seen.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()

                def chunk(part):
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
                    self.wfile.flush()
                try:
                    script(chunk)
                except OSError:
                    return
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)                      # cut, not ended
        self.repoint(tls_peer(self, Peer))
        self.until_listed(marker)

    NDJSON_LINE = (b'{"model": "qwen3.8-27b", "message": {"role": "assistant", "content": "from the peer "}, '
                   b'"done": false}\n')
    NDJSON_DEATH = (b'{"error": {"message": "EngineCore encountered an issue. See stack trace (above) for the root '
                    b'cause."}}\n')

    def test_a_death_relayed_is_bound_as_it_is_relayed(self):
        # bound when the watch saw its client gone, a retry within that watch's second went unbound, free to take this
        # box's engine down too (the 8th review of v1.43.0, 2026-11-05): bound as the death is relayed, its client still
        # there
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        Base = fake_peer(["qwen3.8-27b", "marker-bound-at-death"], PEER_ANSWER)
        event = (b'data: {"id":"c","object":"chat.completion.chunk","model":"qwen3.8-27b","choices":[{"index":0,'
                 b'"delta":{"content":"from the peer "}}]}\n\n')

        class DeadThenQuiet(Base):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                Base.seen.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                try:
                    for part in (event, b"data: " + VLLM_DEAD_EVENT + b"\n\n"):
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
                        self.wfile.flush()
                    time.sleep(4.0)                                             # quiet, its connection open
                except OSError:
                    return
        self.repoint(tls_peer(self, DeadThenQuiet))
        self.until_listed("marker-bound-at-death")
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 2, 6.0)    # this box busy: a new one goes there
        raw = json.dumps(dict(self.turns("conv-bound-at-death", 1)[0], stream=True)).encode()
        c = http.client.HTTPConnection("127.0.0.1", self.port1, timeout=30)
        self.addCleanup(c.close)
        c.request("POST", "/v1/chat/completions", raw,
                  {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
        resp = c.getresponse()
        got = b""
        while b"EngineCore" not in got:
            b = resp.read1(65536)
            if not b:
                break
            got += b
        self.assertIn(b"from the peer", got, "served here: the test needs the other box's stream")
        fp = m.body_fingerprint(raw, "/v1/chat/completions")
        self.assertEqual(m.killer_box(fp), "spark2", "a death relayed was not bound while its client stayed")
        resp.close()
        for th in held:
            th.join(30)

    def test_an_order_line_split_where_its_client_leaves_is_read_whole(self):
        # the other box's NDJSON: a line and the first half of its order line in one read, the write of that line
        # failing (its client gone at the death): the half held back was not read with the rest, and the request stayed
        # bound 10 minutes (the 8th review of v1.43.0, 2026-11-05)
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        order = HOP_ENDED_NDJSON

        def script(chunk):
            chunk(self.NDJSON_LINE)
            chunk(self.NDJSON_DEATH)
            time.sleep(1.0)                                                     # its client reads the death and leaves
            chunk(self.NDJSON_LINE + order[:10])
            time.sleep(0.3)
            chunk(order[10:])
            time.sleep(1.0)
        self._ndjson_peer("marker-nd-split", script)
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 2, 6.0)    # this box busy: a new one goes there
        raw = json.dumps(dict(self.turns("conv-nd-split", 1)[0], stream=True)).encode()
        c = http.client.HTTPConnection("127.0.0.1", self.port1, timeout=30)
        c.connect()
        c.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))   # it leaves with a reset
        c.request("POST", "/api/chat", raw, {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
        resp = c.getresponse()
        got = b""
        while b"EngineCore" not in got:
            b = resp.read1(65536)
            if not b:
                break
            got += b
        self.assertIn(b"from the peer", got, "served here: the test needs the other box's stream")
        resp.close()
        c.close()
        fp = m.body_fingerprint(raw, "/api/chat")
        # past the order's second half (1.3 s after the death) and its read: read before, a binding not made yet passed
        # for one undone (the 9th review of v1.43.0, 2026-11-05)
        time.sleep(2.5)
        self.assertIsNone(m.killer_box(fp), "an order line split where its client left was not read whole")
        for th in held:
            th.join(30)

    def test_the_line_end_before_an_order_line_stays_at_the_entry(self):
        # after a line over 64 KiB relayed unended, the other box puts a line end before its order line: relayed to the
        # client, it ended half a line as if whole (the 8th review of v1.43.0, 2026-11-05)
        if not self.INPROC:
            self.skipTest("the entry's relay is read in this test's process")
        long_line = b'{"model": "qwen3.8-27b", "message": {"role": "assistant", "content": "' + b"x" * 70000

        def script(chunk):
            chunk(long_line)                                                    # over 64 KiB, unended
            time.sleep(0.3)
            chunk(b"\n" + HOP_ENDED_NDJSON)
            time.sleep(0.5)
        self._ndjson_peer("marker-nd-sep", script)
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 2, 6.0)    # this box busy: a new one goes there
        raw = json.dumps(dict(self.turns("conv-nd-sep", 1)[0], stream=True)).encode()
        c = http.client.HTTPConnection("127.0.0.1", self.port1, timeout=30)
        self.addCleanup(c.close)
        c.request("POST", "/api/chat", raw, {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
        resp = c.getresponse()
        got = b""
        try:
            while True:
                b = resp.read1(65536)
                if not b:
                    break
                got += b
        except (OSError, http.client.HTTPException):
            pass                                                                # cut, as the other box's was
        self.assertTrue(got.startswith(long_line), got[:120])
        self.assertNotIn(b"x_alzeo_ended", got)
        self.assertFalse(got.endswith(b"\n"), "the line end before the order line reached the client")
        for th in held:
            th.join(30)

    def test_a_stop_job_seen_after_a_vllm_death_is_said_before_the_streams_end(self):
        # vLLM ends a dead stream cleanly ([DONE]); its lane's stop job, not seen yet at the death, went unsaid, and the
        # other box bound the request 10 minutes (the 6th review of v1.43.0, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the unit's job is said in this test's process")
        m = self.mod
        saved = m.unit_stopping_by_order
        m.unit_stopping_by_order = lambda: self.E1.ordered_now                  # the stop job, seen after the death
        self.addCleanup(setattr, m, "unit_stopping_by_order", saved)
        self.E1.mode = "vllm_dead_late_order"
        st, out, _ = self.tls_call(self.pport1, "/v1/chat/completions",
                                   dict(self.chat("qwen3.8-27b"), user="hop-late-order", stream=True))
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"EngineCore", out)
        self.assertIn(b'"x_alzeo_ended":"order"', out, out[-400:])
        self.assertLess(out.index(b"EngineCore"), out.index(b'"x_alzeo_ended":"order"'))

    def test_a_box_that_reads_no_key_holds_its_own_lane_from_the_same_checkpoint_too(self):
        # its engine away, the other box on the same checkpoint: refused at once ("cannot read its own key"), where the
        # other checkpoint went on to this box's own path (the 4th review of v1.43.0, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the key is read in this test's process")
        m = self.mod
        saved = (m._door_key, m.served_models, m.unit_texts, m.unit_jobs, m._LAST_LANE["lane"])

        def restore():
            m._door_key, m.served_models, m.unit_texts, m.unit_jobs = saved[:4]
            m._LAST_LANE["lane"] = saved[4]
        self.addCleanup(restore)

        def away(*a, **k):
            raise m.EngineUnreachable("connection refused (the test's engine away)")
        m._door_key, m.served_models, m.unit_texts, m.unit_jobs = (lambda: None), away, (lambda: None), (lambda: [])
        m._LAST_LANE["lane"] = (frozenset({"qwen3.8-27b"}), TEST_REV)         # its own lane, the other box's checkpoint
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"))
        self.assertNotIn(b"cannot read its own key", out, "a request of its own lane was turned away")
        self.assertEqual(st, 200, out[:300])                                    # its own path; this test's engine answers
        self.assertIn(b"from spark1", out)

    def test_a_recovery_restart_to_a_staged_checkpoint_binds_what_its_engine_closed_on(self):
        # decision 1049: an update staged another checkpoint, and the cockpit restarts a wedged engine; 1041 took that
        # restart for the Load of the staged checkpoint, an order, and the request that wedged the engine went unbound
        # (the 4th review of v1.43.0, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the unit's job is said in this test's process")
        m = self.mod
        saved = (m.unit_jobs, m.unit_start_texts, m.recovery_runs)

        def restore():
            m.unit_jobs, m.unit_start_texts, m.recovery_runs = saved
        self.addCleanup(restore)
        self.addCleanup(m._KILLERS.clear)
        m.unit_jobs = lambda: [("qwen38-sglang.service", "restart")]
        m.unit_start_texts = lambda unit: [f"docker run x --served-model-name qwen3.8-27b --revision {'ab' * 20}"]
        m.recovery_runs = lambda: {"qwen38-sglang.service": True}             # the cockpit's mark (decision 1049)
        r = self.turns("conv-recovery", 1)[0]
        self.E1.mode = "die"
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertNotEqual(st, 200, out[:300])
        self.assertIn("conv-recovery-0", self.E1.users, "the test needs this box's engine to have it")
        self.assertEqual(m.killer_box(m.body_fingerprint(json.dumps(r).encode(), "/v1/chat/completions")), "",
                         "the cockpit's recovery was taken for an order, and bound nothing")

    def test_a_box_that_reads_no_key_holds_what_its_own_engine_will_serve(self):
        # its engine away, the other box on another checkpoint: the request goes on to this box's own path; the key's
        # refusal came first and turned it away (the delta's 3rd review, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the key is read in this test's process")
        m = self.mod
        saved = (m._door_key, m.served_models, m.unit_texts, m.unit_jobs, m._LAST_LANE["lane"])

        def restore():
            m._door_key, m.served_models, m.unit_texts, m.unit_jobs = saved[:4]
            m._LAST_LANE["lane"] = saved[4]
        self.addCleanup(restore)

        def away(*a, **k):
            raise m.EngineUnreachable("connection refused (the test's engine away)")
        m._door_key, m.served_models, m.unit_texts, m.unit_jobs = (lambda: None), away, (lambda: None), (lambda: [])
        m._LAST_LANE["lane"] = (frozenset({"qwen3.8-27b"}), "ab" * 20)       # its own lane, another checkpoint
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"))
        self.assertNotIn(b"cannot read its own key", out, "a request of its own lane was turned away")
        self.assertEqual(st, 200, out[:300])                                    # its own path; this test's engine answers
        self.assertIn(b"from spark1", out)

    def test_a_restart_binds_what_its_engine_closed_on(self):
        # decision 1030: only a stop is an order. The cockpit restarts an engine a request may have wedged (autoheal,
        # zombie): counted as an order, that request was bound to nothing and its retry went to the other box, where it
        # could wedge the other engine too (review, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the unit's job is said in this test's process")
        m = self.mod
        saved = (m.unit_jobs, m.unit_start_texts)
        m.unit_jobs = lambda: [("qwen38-sglang.service", "restart")]          # the cockpit's restart of a wedged engine
        # to the checkpoint it serves: this proxy names no unit, and the comparison of decision 1041 was never reached
        # (the 4th review of v1.43.0, 2026-11-04)
        m.unit_start_texts = lambda unit: [f"docker run x --served-model-name qwen3.8-27b --revision {TEST_REV}"]
        self.addCleanup(lambda: (setattr(m, "unit_jobs", saved[0]), setattr(m, "unit_start_texts", saved[1])))
        self.addCleanup(m._KILLERS.clear)
        r = self.turns("conv-restart", 1)[0]
        self.E1.mode = "die"
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertNotEqual(st, 200, out[:300])
        hop = dict(self.chat("qwen3.8-27b"), user="hop-restart")
        st, out = self.tls_call(self.pport1, "/v1/chat/completions", hop)[:2]
        self.assertEqual(st, 502, out[:300])                                    # the 502 of an engine that closed on it
        self.E1.mode = "ok"
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 5, 4.0)     # far apart: it would go there, unbound
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out, "a request its engine closed on under a restart went to the other box")
        for th in held:
            th.join(30)

    def test_this_engine_starting_its_lane_goes_to_no_other_checkpoint(self):
        # decision 1018: an update restarts this proxy and the engine together, and the 27B loads for minutes. This
        # proxy, new, had never seen its engine answer, and sent the 27B's clients to the other box whatever its
        # checkpoint (review, 2026-11-04): the command of the engine starting names its model and checkpoint
        if not self.INPROC:
            self.skipTest("this box's engine is said away in this test's process")
        m = self.mod
        saved = (m.served_models, m.unit_texts, m._LAST_LANE["lane"])

        def restore():
            m.served_models, m.unit_texts = saved[:2]
            m._LAST_LANE["lane"] = saved[2]
        self.addCleanup(restore)

        def away(*a, **k):
            raise m.EngineUnreachable("connection refused (the test's engine starting)")
        m.served_models = away
        m._LAST_LANE["lane"] = (frozenset(), None)                             # a proxy that never saw it answer
        starting = ("/usr/bin/docker run --rm --name qwen38-sglang img python3 -m sglang.launch_server --model-path "
                    "/m --served-model-name qwen3.8-27b --revision {} --max-running-requests 8")
        m.unit_texts = lambda: [starting.format("ab" * 20)]                   # its engine loads another checkpoint
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"))
        self.assertEqual(st, 200, out[:300])                                    # this box's own path (role 6's hold)
        self.assertIn(b"from spark1", out)
        self.assertEqual(self.E2.users, [], "a client of this box got the other checkpoint")
        m.unit_texts = lambda: [starting.format(TEST_REV)]                    # the same checkpoint there: it serves
        getattr(m, "forget_unit_reads", lambda: None)()                         # lane_away keeps its read a second
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"))
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark2", out)
        m.unit_texts = lambda: [starting.replace("qwen3.8-27b", "qwen3.8-flash-next-fp4").format("ab" * 20)]
        getattr(m, "forget_unit_reads", lambda: None)()
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"))
        self.assertEqual(st, 200, out[:300])                                    # its lane serves another model: there
        self.assertIn(b"from spark2", out)

    def test_another_lane_saying_no_checkpoint_is_noted_with_none(self):
        # a lane whose command names no revision (a custom model) was never noted: while its engine was away its model's
        # requests went to the other box whatever its checkpoint (the delta's 3rd review, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the lane seen last is read in this test's process")
        m = self.mod
        saved = (m.unit_revision, m._LAST_LANE["lane"])

        def restore():
            m.unit_revision = saved[0]
            m._LAST_LANE["lane"] = saved[1]
        self.addCleanup(restore)
        here = frozenset(m.served_models())
        m._LAST_LANE["lane"] = (frozenset({"qwen3.8-flash-next-fp4"}), "ab" * 20)    # another lane before
        m.unit_revision = lambda: None
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"))
        self.assertEqual(st, 200, out[:300])
        self.assertEqual(m._LAST_LANE["lane"], (here, None))

    def test_the_lane_seen_last_keeps_its_checkpoint_while_the_unit_says_none(self):
        # its unit with no process to read for a moment, the lane seen last was written with no checkpoint, and once its
        # engine was away every request of its model got "another checkpoint" (the delta's review, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the lane seen last is read in this test's process")
        m = self.mod
        saved = (m.unit_revision, m._LAST_LANE["lane"])

        def restore():
            m.unit_revision = saved[0]
            m._LAST_LANE["lane"] = saved[1]
        self.addCleanup(restore)
        here = frozenset(m.served_models())                                     # the lane this box serves
        m._LAST_LANE["lane"] = (here, TEST_REV)
        m.unit_revision = lambda: None
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"))
        self.assertEqual(st, 200, out[:300])
        self.assertEqual(m._LAST_LANE["lane"], (here, TEST_REV))

    def test_a_box_that_serves_no_text_lane_now_sends_the_others_model_there(self):
        # decision 1018 holds for a model this box's lane serves: a box switched to images (or stopped) kept the 27B it
        # served last as its own, and refused its clients the other box's 27B of another checkpoint (found re-reading
        # the rule, 2026-11-04). A crash on its way back keeps it
        if not self.INPROC:
            self.skipTest("systemd's answer is set in this test's process")
        m = self.mod
        saved = (m.served_models, m.unit_texts, m.unit_jobs, m.HOLD_UNITS, m._LAST_LANE["lane"],
                 m._UNIT_FACTS.get("states"))

        def restore():
            m.served_models, m.unit_texts, m.unit_jobs, m.HOLD_UNITS = saved[:4]
            m._LAST_LANE["lane"] = saved[4]
            m._UNIT_FACTS["states"] = saved[5]
        self.addCleanup(restore)
        m.unit_jobs = lambda: []

        def away(*a, **k):
            raise m.EngineUnreachable("connection refused (the test's text lane stopped)")
        m.served_models = away
        m.HOLD_UNITS = ("qwen38-sglang.service",)
        m.unit_texts = lambda: None                                             # no text unit active
        m._LAST_LANE["lane"] = (frozenset({"qwen3.8-27b"}), "ab" * 20)       # the 27B it served before, another one
        m._UNIT_FACTS["states"] = ("inactive",)                                 # images now, or no lane
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"))
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark2", out, "a box that serves no text lane kept the other box's model from its client")
        m._UNIT_FACTS["states"] = ("deactivating",)                             # a crash, its restart not due yet
        getattr(m, "forget_unit_reads", lambda: None)()                         # lane_away keeps its read a second
        before = len(self.E2.users)
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"))
        self.assertEqual(st, 200, out[:300])                                    # this box's own path (role 6's hold)
        self.assertIn(b"from spark1", out)
        self.assertEqual(len(self.E2.users), before, "a client of this box got the other checkpoint")

    def test_a_request_sent_on_for_another_checkpoint_than_this_boxs_is_refused(self):
        # decision 1018, the receiving side: the other box's row of this one, read minutes ago, said the checkpoint this
        # box served before a switch under the same name, and the hop said nothing this box could check (review,
        # 2026-11-04). The entry's checkpoint comes with the request now, and another one gets a 409
        hop = dict(self.chat("qwen3.8-27b"), user="hop-checkpoint")
        st, out = self.tls_call(self.pport1, "/v1/chat/completions", hop,
                                headers={"X-Alzeo-Pair-Checkpoint": "ab" * 20})[:2]
        self.assertEqual(st, 409, out[:300])
        self.assertIn(b"model_not_served", out)
        self.assertIn(b"another checkpoint", out)
        self.assertNotIn("hop-checkpoint", self.E1.users, "this box served another checkpoint's request")
        st, out = self.tls_call(self.pport1, "/v1/chat/completions", hop, headers={"X-Alzeo-Pair-Checkpoint": TEST_REV})[:2]
        self.assertEqual(st, 200, out[:300])
        st, out = self.tls_call(self.pport1, "/v1/chat/completions", dict(hop, user="hop-no-checkpoint"))[:2]
        self.assertEqual(st, 200, out[:300])                                    # a hop that carries none: no check

    def test_an_ordered_stop_binds_no_request(self):
        # decision 1019 binds a request that may have killed an engine: a stop ordered to the lane's unit (a Load, an
        # update) closed every request in flight and bound each one for 10 minutes, and its hop got the 502 no box takes
        # back (review, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the unit's job is said in this test's process")
        m = self.mod
        saved = m.unit_jobs
        m.unit_jobs = lambda: [("qwen38-sglang.service", "stop")]             # a Load of another lane stops this one
        self.addCleanup(setattr, m, "unit_jobs", saved)
        self.addCleanup(m._KILLERS.clear)
        r = self.turns("conv-ordered", 1)[0]
        self.E1.mode = "die"
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertNotEqual(st, 200, out[:300])
        hop = dict(self.chat("qwen3.8-27b"), user="hop-ordered")
        st, out = self.tls_call(self.pport1, "/v1/chat/completions", hop)[:2]
        self.assertEqual(st, 503, out[:300])                                    # not the 502 of an engine that died
        self.E1.mode = "ok"
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 5, 4.0)     # far apart: it goes there, unbound
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark2", out, "a request an ordered stop closed was bound to this box")
        for th in held:
            th.join(30)

    def test_a_request_bound_to_a_box_whose_list_is_unknown_is_tried_there(self):
        # its list read failed (it restarted): the bound branch said "another checkpoint" to each retry for 10 minutes
        # (review, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the kept list is set in this test's process")
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        r = self.turns("conv-bound-unknown", 1)[0]
        m.killer_note(m.body_fingerprint(json.dumps(r).encode(), "/v1/chat/completions"), "spark2")
        with m._PAIR_LOCK:
            m._PAIR_MODELS["spark2"]["revision"] = None                        # read while it restarted
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark2", out, "a box whose list is unknown was said to serve another checkpoint")

    def test_a_request_bound_here_goes_nowhere_once_this_box_serves_another_model(self):
        # bound to this box, which then switched lanes: its new lane answered a request for the other model, which
        # docs/pair.md says never happens (review, 2026-11-04)
        if not self.INPROC:
            self.skipTest("this box's model is said in this test's process")
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        r = self.turns("conv-bound-here-other", 1)[0]
        m.killer_note(m.body_fingerprint(json.dumps(r).encode(), "/v1/chat/completions"), "")       # this box
        real = m.served_models
        m.served_models = lambda *a, **k: ["qwen3.8-flash-next-fp4"]           # this box serves another model now
        self.addCleanup(setattr, m, "served_models", real)
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 503, out[:300])
        self.assertIn(b"serves another model now", out)
        self.assertNotIn("conv-bound-here-other-0", self.E1.users + self.E2.users)

    def test_a_burst_of_new_conversations_is_shared(self):
        self.E1.delay = self.E2.delay = 1.0
        out = []
        threads = [threading.Thread(target=lambda i=i: out.append(self.call(
            "/v1/chat/completions", self.turns(f"burst-{i}", 1)[0])[1])) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        here, there = sum(b"from spark1" in o for o in out), sum(b"from spark2" in o for o in out)
        self.assertEqual(here + there, 8, out)
        self.assertGreaterEqual(min(here, there), 3, f"{here} here, {there} on spark2: the burst went to one box")

    def test_a_box_that_refuses_hands_its_share_back(self):
        self.busy(self.E1, self.port1, KEY_1, "local-only", 2, 3.0)
        self.E2.mode = "busy"                   # spark2's engine answers 503: spark2 relays it
        self.addCleanup(time.sleep, 2.5)        # its pause (ENTRY_ENV) over before the next test
        before = self.sent()
        st, out, _ = self.call("/v1/chat/completions", self.turns("conv-d", 1)[0])
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out, "the entry answered the peer's 503 instead of serving it")
        self.assert_said("spark1", "spark2 does not answer for qwen3.8-27b: this box serves it")
        # served here, never "sent there" too (the tile counted both, review of 2026-11-03)
        self.assertEqual(self.moved(before, self.sent()), {"failover": 1})

    def test_a_request_the_other_box_drops_runs_nowhere_else(self):
        # decision 1017: the other box had it whole and closed the connection with no answer (an Abort all there, its
        # floor, a restart, its power). It was run here again: an abort there undone, and a request that took that box
        # down sent to this one (review, 2026-11-03)
        class Drops(http.server.BaseHTTPRequestHandler):
            """A box that lists the 27B and goes as a request arrives (its power, its cable)."""
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                out = json.dumps({"object": "list", "data": [{"id": "qwen3.8-27b"}, {"id": "marker-drop"}],
                                  "x_alzeo_running": 0, "x_alzeo_running_yours": 0,
                                  "x_alzeo_revision": TEST_REV}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def do_POST(self):
                self.connection.shutdown(socket.SHUT_RDWR)
                self.close_connection = True

        entry = self.spare_proxy("spark1-drop", KEY_1, peers=(("spark2", tls_peer(self, Drops)),))
        self.until_listed("marker-drop", port=entry)
        self.busy(self.E1, entry, KEY_1, "local-only", 1, 3.0)     # one here, none there: the turn goes there
        st, out, _ = self.call("/v1/chat/completions", self.turns("conv-e", 1)[0], port=entry)
        self.assertEqual(st, 503, out[:300])
        self.assertIn(b"had this request and closed the connection", out)
        self.assertNotIn("conv-e-0", self.E1.users, "a request the other box had was run on this box's engine too")
        self.assert_said("spark1-drop", "spark2 closed the connection on a request of qwen3.8-27b: its client is told "
                                        "503, and spark2 gets nothing of a model both serve")
        # sent again, as a client does after a 5xx: that box or nowhere, though it is paused (decision 1019)
        st, out, _ = self.call("/v1/chat/completions", self.turns("conv-e", 1)[0], port=entry)
        self.assertEqual(st, 503, out[:300])
        self.assertNotIn("conv-e-0", self.E1.users, "sent again, the request reached this box's engine")

    def test_this_box_hands_a_request_its_engine_refuses_to_the_other(self):
        self.E1.mode = "busy"                   # spark1's engine answers 503 to generations: a lane stopping or starting
        before = self.sent()
        for stream in (False, True):
            with self.subTest(stream=stream):
                body = dict(self.turns(f"conv-g-{stream}", 1)[0], stream=stream)
                st, out, _ = self.call("/v1/chat/completions", body)
                self.assertEqual(st, 200, out[:300])
                self.assertIn(b"spark2", out, "the entry held or refused a request the other box serves too")
                if stream:
                    self.assertIn(b"data: [DONE]", out)
        self.assert_said("spark1", "this box's engine does not answer for qwen3.8-27b: trying spark2")
        # counted in delta: an earlier test's rescues never stand in for these two (review, 2026-11-03)
        self.assertEqual(self.moved(before, self.sent()), {"rescued": 2})

    def test_a_request_this_boxs_engine_died_on_goes_to_no_other_engine(self):
        # the request may be what took the engine down: sent on, it could take the other box's down too (security
        # review of v1.43.0, 2026-11-03). Decision 998 hands over a refused connection or a 502, 503 or 504 only
        self.E1.mode = "die"
        st, out, _ = self.call("/v1/chat/completions", self.turns("conv-m", 1)[0])
        self.assertEqual(st, 503, out[:300])
        self.assertEqual(self.E2.users, [], "the request that the engine died on went to the other box's engine")

    def test_the_other_box_whose_engine_died_on_a_request_says_502_and_it_goes_no_further(self):
        self.busy(self.E1, self.port1, KEY_1, "local-only", 1, 3.0)         # one here, none there: it goes there
        self.E2.mode = "die"
        st, out, _ = self.call("/v1/chat/completions", self.turns("conv-n", 1)[0])
        self.assertEqual(st, 502, out[:300])
        self.assertEqual([u for u in self.E1.users if u and not u.startswith("busy-")], [],
                         "the request that the other box's engine died on was served here after it")
        self.assert_said("spark2", "502 engine closed a hop")

    def test_a_request_asked_of_this_box_is_never_shared_nor_handed_over(self):
        # the cockpit's smoke test asks whether THIS box's engine answers (X-Alzeo-Here: 1)
        here = {"X-Alzeo-Here": "1"}
        self.busy(self.E1, self.port1, KEY_1, "local-only", 1, 3.0)         # one here, none there
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"), headers=here)
        self.assertIn(b"from spark1", out, "a request asked of this box was shared")
        self.E1.mode = "busy"                                               # this box's engine says 503
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"), headers=here)
        self.assertEqual(st, 503, "a request asked of this box was handed to the other one")
        self.assertEqual(self.E2.users, [], self.E2.users)

    def test_a_box_that_does_not_say_what_it_runs_gets_nothing_shared(self):
        # a box of v1.42.0 lists no x_alzeo_running: its load is unknown, and its own clients may fill it
        Peer = fake_peer(["qwen3.8-27b", "marker-old"], PEER_ANSWER, running=None)
        entry = self.spare_proxy("spark1-old", KEY_1, peers=(("spark2", tls_peer(self, Peer)),))
        self.until_listed("marker-old", port=entry)
        self.busy(self.E1, entry, KEY_1, "local-only", 1, 4.0)
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"), port=entry)
        self.assertIn(b"from spark1", out, "a box that says nothing of its load was sent a request")
        self.assertEqual(Peer.seen, [])

    def test_the_tile_says_what_this_box_sent_the_other(self):
        self.busy(self.E1, self.port1, KEY_1, "local-only", 1, 3.0)     # one here, none there
        before = self.sent()
        st, out, _ = self.call("/v1/chat/completions", self.turns("conv-f", 1)[0])
        self.assertIn(b"from spark2", out)
        sent = self.sent()
        self.assertEqual(set(sent), {"affinity", "balance", "failover", "too_long", "rescued"}, sent)
        self.assertEqual(self.moved(before, sent), {"balance": 1})

    def test_the_tile_still_says_what_was_shared_when_the_other_box_does_not_answer(self):
        # what this box served while the other box did not answer mattered most then, and the 503 said none of it
        # (review, 2026-11-03)
        self.busy(self.E1, self.port1, KEY_1, "local-only", 1, 3.0)         # one here, none there: it goes there
        before = self.sent()
        st, out, _ = self.call("/v1/chat/completions", self.turns("conv-q", 1)[0])
        self.assertIn(b"from spark2", out)
        self.repoint(localports.held(self))                                 # spark2 gone: nothing listens there
        st, out, _ = self.call("/proxy/pair/spark2/state", method="GET")
        self.assertEqual(st, 503, out[:300])
        self.assertEqual(self.moved(before, json.loads(out).get("sent") or {}), {"balance": 1}, out[:300])

    def test_a_box_that_said_529_gets_nothing_shared_for_a_while(self):
        # its cockpit hit the memory floor: 529 on /v1/messages, the floor's word in the Anthropic dialect
        Peer = fake_peer(["qwen3.8-27b", "marker-529"], (529, {"type": "error", "error": {
            "type": "overloaded_error", "message": "the memory floor"}}))
        entry = self.spare_proxy("spark1-529", KEY_1, peers=(("spark2", tls_peer(self, Peer)),))
        self.until_listed("marker-529", port=entry)
        self.busy(self.E1, entry, KEY_1, "local-only", 1, 6.0)          # one here, none there
        msg = {"model": "qwen3.8-27b", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]}
        for _ in range(2):
            st, out, _ = self.call("/v1/messages", msg, port=entry)
            self.assertEqual(st, 200, out[:300])
            self.assertIn(b"from spark1", out, "the other box's 529 went to the client while this box idled")
        self.assertEqual(len(Peer.seen), 1, "the box that said 529 got the next request too")
        self.assert_said("spark1-529", "spark2 does not answer for qwen3.8-27b: this box serves it, and spark2 gets nothing")

    def test_a_prompt_too_long_for_the_other_box_is_served_here_and_the_next_ones_still_go_there(self):
        Peer = fake_peer(["qwen3.8-27b", "marker-long"], (400, {"error": {
            "type": "context_too_long", "code": "context_length_exceeded", "message": "too long for this lane"}}))
        entry = self.spare_proxy("spark1-long", KEY_1, peers=(("spark2", tls_peer(self, Peer)),))
        self.until_listed("marker-long", port=entry)
        self.busy(self.E1, entry, KEY_1, "local-only", 1, 6.0)
        before = self.sent(entry)
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"), port=entry)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out, "the other box's pool refused it and the client got that refusal")
        self.assert_said("spark1-long", "the prompt is too long for spark2's pool: this box serves it")
        # counted apart: a small pool says nothing of the other box's health (review, 2026-11-03)
        self.assertEqual(self.moved(before, self.sent(entry)), {"too_long": 1})
        Peer.state["post"] = PEER_ANSWER
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"), port=entry)
        self.assertIn(b"from the peer", out, "one prompt too long for it kept the other box out")
        self.assertEqual(self.moved(before, self.sent(entry)), {"too_long": 1, "balance": 1})

    def test_a_refusal_cut_on_the_way_is_still_served_here(self):
        # its status said it all: a 503 whose body was cut is a refusal, served here, not a 502 (review, 2026-11-03)
        Peer = fake_peer(["qwen3.8-27b", "marker-cut"], (503, {"error": {"type": "engine_unavailable", "message": "x" * 200}}))
        Peer.state["cut"] = True
        entry = self.spare_proxy("spark1-cut", KEY_1, peers=(("spark2", tls_peer(self, Peer)),))
        self.until_listed("marker-cut", port=entry)
        self.busy(self.E1, entry, KEY_1, "local-only", 1, 4.0)
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"), port=entry)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out, "a cut refusal reached the client as a 502")

    def test_a_box_that_switched_is_not_sent_the_next_request(self):
        Peer = fake_peer(["qwen3.8-27b", "marker-moved"], (409, {"error": {
            "type": "model_not_served", "message": "this box (spark2) serves ornith-1.5-122b, not qwen3.8-27b"}}))
        entry = self.spare_proxy("spark1-moved", KEY_1, peers=(("spark2", tls_peer(self, Peer)),))
        self.until_listed("marker-moved", port=entry)
        self.busy(self.E1, entry, KEY_1, "local-only", 1, 6.0)
        for _ in range(3):
            st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"), port=entry)
            self.assertEqual(st, 200, out[:300])
            self.assertIn(b"from spark1", out)
        self.assertEqual(len(Peer.seen), 1, "each request went to the box that switched, for another 409")

    def test_a_list_read_long_ago_still_shares(self):
        Peer = fake_peer(["qwen3.8-27b", "marker-pause"], PEER_ANSWER)
        entry = self.spare_proxy("spark1-pause", KEY_1, peers=(("spark2", tls_peer(self, Peer)),),
                                 extra={"PAIR_TTL_S": "1"})
        self.until_listed("marker-pause", port=entry)
        time.sleep(4)                       # after a pause: the list was read more than 3 reads' time ago
        self.busy(self.E1, entry, KEY_1, "local-only", 1, 6.0)
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"), port=entry)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from the peer", out, "a burst after a pause stayed here, before the list's read")

    def test_a_conversation_served_here_while_the_other_box_was_away_stays_here(self):
        Peer = fake_peer(["qwen3.8-27b", "marker-away"], PEER_ANSWER)
        entry = self.spare_proxy("spark1-away", KEY_1, peers=(("spark2", tls_peer(self, Peer)),),
                                 extra={"PAIR_TTL_S": "1"})
        self.until_listed("marker-away", port=entry)
        # turns of 3,000 bytes after 12,000: the first is 15 KB, the second 18 KB, its 16 KiB block the first's not
        a = self.turns("conv-h", 3, size=12000, turn=3000)
        self.busy(self.E1, entry, KEY_1, "local-only", 1, 25.0)         # one here the whole test, none there
        st, out, _ = self.call("/v1/chat/completions", a[0], port=entry)
        self.assertIn(b"from the peer", out, "a new conversation, to the box with none in flight")
        Peer.state["listed"] = None                                     # the other box goes (its list fails)
        self.until_listed("marker-away", port=entry, gone=True)
        st, out, _ = self.call("/v1/chat/completions", a[1], port=entry)
        self.assertIn(b"from spark1", out, "served here while the other box was away")
        Peer.state["listed"] = ["qwen3.8-27b", "marker-back"]           # back, its prefix cache empty
        self.until_listed("marker-back", port=entry)
        st, out, _ = self.call("/v1/chat/completions", a[2], port=entry)
        self.assertIn(b"from spark1", out, "the conversation went back to the box that lost its cache")

    def test_a_token_count_stays_here_and_moves_no_conversation(self):
        # Claude Code counts a conversation's tokens with its whole body: the count generates nothing and fills no
        # prefix cache, so it is counted here (the same model, the same tokenizer), however far apart the boxes are.
        # Its last check, a turn served here after the count, could not fail: the fingerprints start with the route
        # (review, 2026-11-03). Its engine here answering the count is what says it stayed
        a = self.turns("conv-k", 2)
        st, out, _ = self.call("/v1/chat/completions", a[0])
        self.assertIn(b"from spark1", out, "two idle boxes: the entry serves the first turn")
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 6, 3.0)     # 6 here, none there: far apart
        count = {"model": "qwen3.8-27b", "messages": a[1]["messages"]}
        st, out, _ = self.call("/v1/messages/count_tokens", count)
        self.assertEqual(st, 200, out[:300])
        self.assertNotIn("/v1/messages/count_tokens", [x[0] for x in self.E2.seen], "the count went to the other box")
        self.assertIn("/v1/messages/count_tokens", [x[0] for x in self.E1.seen], "this box's engine did not count it")
        for t in held:
            t.join(30)

    def test_a_box_tells_the_other_how_many_it_runs_and_how_many_are_its(self):
        said = lambda: {k: v for k, v in json.loads(self.tls_call(self.pport1, "/v1/models", method="GET")[1]).items()
                        if k.startswith("x_alzeo_running")}
        listed = json.loads(self.tls_call(self.pport1, "/v1/models", method="GET")[1])
        self.assertEqual(listed.get("x_alzeo_revision"), TEST_REV, "the checkpoint this box serves")
        self.assertEqual(said(), {"x_alzeo_running": 0, "x_alzeo_running_yours": 0})
        self.busy(self.E1, self.port1, KEY_1, "local-only", 2, 3.0)        # two of its own clients' generations
        # and one the other box sent (on the pair port, its certificate)
        hop = threading.Thread(target=self.tls_call, args=(self.pport1, "/v1/chat/completions",
                                                            dict(self.chat("qwen3.8-27b"), x_delay=3.0, user="hop-1")),
                               daemon=True)
        hop.start()
        self.addCleanup(hop.join, 30)
        deadline = time.monotonic() + 10
        while "hop-1" not in self.E1.users and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(said(), {"x_alzeo_running": 3, "x_alzeo_running_yours": 1})
        st, out, _ = self.call("/v1/models", method="GET")
        self.assertEqual(st, 200, out[:300])
        self.assertNotIn(b"x_alzeo_running", out, "a client of the box sees what the other box reads")


class TwoBoxesServeOneModelInProcess(TwoBoxesServeOneModel):
    INPROC = True

    def test_a_conversation_stays_on_the_box_that_served_it(self):
        super().test_a_conversation_stays_on_the_box_that_served_it()

    def test_a_box_held_by_its_memory_floor_sends_on_what_the_other_box_serves(self):
        super().test_a_box_held_by_its_memory_floor_sends_on_what_the_other_box_serves()

    def test_a_box_gone_without_a_reset_holds_a_shared_request_seconds_not_ten(self):
        super().test_a_box_gone_without_a_reset_holds_a_shared_request_seconds_not_ten()

    def test_a_prompt_too_long_for_this_boxs_pool_goes_to_the_other_box(self):
        super().test_a_prompt_too_long_for_this_boxs_pool_goes_to_the_other_box()

    def test_a_long_prompt_a_warming_engine_cannot_take_goes_to_the_other_box(self):
        super().test_a_long_prompt_a_warming_engine_cannot_take_goes_to_the_other_box()

    def test_a_claude_code_conversation_stays_on_its_box(self):
        super().test_a_claude_code_conversation_stays_on_its_box()

    def test_boxes_far_apart_share_even_a_conversation(self):
        super().test_boxes_far_apart_share_even_a_conversation()

    def test_a_burst_of_new_conversations_is_shared(self):
        super().test_a_burst_of_new_conversations_is_shared()

    def test_a_box_that_refuses_hands_its_share_back(self):
        super().test_a_box_that_refuses_hands_its_share_back()

    def test_a_request_the_other_box_drops_runs_nowhere_else(self):
        super().test_a_request_the_other_box_drops_runs_nowhere_else()

    def test_the_tile_says_what_this_box_sent_the_other(self):
        super().test_the_tile_says_what_this_box_sent_the_other()

    def test_this_box_hands_a_request_its_engine_refuses_to_the_other(self):
        super().test_this_box_hands_a_request_its_engine_refuses_to_the_other()

    def test_a_box_that_said_529_gets_nothing_shared_for_a_while(self):
        super().test_a_box_that_said_529_gets_nothing_shared_for_a_while()

    def test_a_prompt_too_long_for_the_other_box_is_served_here_and_the_next_ones_still_go_there(self):
        super().test_a_prompt_too_long_for_the_other_box_is_served_here_and_the_next_ones_still_go_there()

    def test_a_box_that_switched_is_not_sent_the_next_request(self):
        super().test_a_box_that_switched_is_not_sent_the_next_request()

    def test_a_refusal_cut_on_the_way_is_still_served_here(self):
        super().test_a_refusal_cut_on_the_way_is_still_served_here()

    def test_a_list_read_long_ago_still_shares(self):
        super().test_a_list_read_long_ago_still_shares()

    def test_a_conversation_served_here_while_the_other_box_was_away_stays_here(self):
        super().test_a_conversation_served_here_while_the_other_box_was_away_stays_here()

    def test_a_token_count_stays_here_and_moves_no_conversation(self):
        super().test_a_token_count_stays_here_and_moves_no_conversation()

    def test_a_request_this_boxs_engine_died_on_goes_to_no_other_engine(self):
        super().test_a_request_this_boxs_engine_died_on_goes_to_no_other_engine()

    def test_the_other_box_whose_engine_died_on_a_request_says_502_and_it_goes_no_further(self):
        super().test_the_other_box_whose_engine_died_on_a_request_says_502_and_it_goes_no_further()

    def test_a_request_asked_of_this_box_is_never_shared_nor_handed_over(self):
        super().test_a_request_asked_of_this_box_is_never_shared_nor_handed_over()

    def test_a_box_that_does_not_say_what_it_runs_gets_nothing_shared(self):
        super().test_a_box_that_does_not_say_what_it_runs_gets_nothing_shared()

    def test_a_box_tells_the_other_how_many_it_runs_and_how_many_are_its(self):
        super().test_a_box_tells_the_other_how_many_it_runs_and_how_many_are_its()

    def test_the_tile_still_says_what_was_shared_when_the_other_box_does_not_answer(self):
        super().test_the_tile_still_says_what_was_shared_when_the_other_box_does_not_answer()

    def test_a_lane_of_16_keeps_a_conversation_5_ahead(self):
        super().test_a_lane_of_16_keeps_a_conversation_5_ahead()

    def test_the_gap_is_half_the_slots_the_other_box_says(self):
        super().test_the_gap_is_half_the_slots_the_other_box_says()

    def test_a_lane_whose_port_refuses_hands_its_request_to_the_other_box(self):
        super().test_a_lane_whose_port_refuses_hands_its_request_to_the_other_box()

    def test_a_request_kept_here_goes_over_when_the_floor_holds_this_box(self):
        super().test_a_request_kept_here_goes_over_when_the_floor_holds_this_box()

    def test_a_count_this_box_failed_is_no_engine_that_closed_on_a_request(self):
        super().test_a_count_this_box_failed_is_no_engine_that_closed_on_a_request()

    def test_a_hop_whose_engine_dies_in_an_open_stream_gets_an_error_event(self):
        super().test_a_hop_whose_engine_dies_in_an_open_stream_gets_an_error_event()

    def test_a_request_asked_of_this_box_goes_nowhere_else_while_its_engine_is_away(self):
        super().test_a_request_asked_of_this_box_goes_nowhere_else_while_its_engine_is_away()

    def test_a_request_this_boxs_engine_died_on_stays_here_when_sent_again(self):
        super().test_a_request_this_boxs_engine_died_on_stays_here_when_sent_again()

    def test_a_request_the_other_boxs_engine_died_on_goes_there_or_nowhere_when_sent_again(self):
        super().test_a_request_the_other_boxs_engine_died_on_goes_there_or_nowhere_when_sent_again()

    def test_a_box_serving_another_checkpoint_gets_nothing_shared(self):
        super().test_a_box_serving_another_checkpoint_gets_nothing_shared()

    def test_a_tool_call_the_other_box_served_is_watched_here_too(self):
        super().test_a_tool_call_the_other_box_served_is_watched_here_too()

    def test_the_other_boxs_list_reads_write_a_line_a_minute_while_this_engine_is_away(self):
        super().test_the_other_boxs_list_reads_write_a_line_a_minute_while_this_engine_is_away()

    def test_a_client_that_leaves_while_the_other_box_does_not_answer_still_pauses_it(self):
        super().test_a_client_that_leaves_while_the_other_box_does_not_answer_still_pauses_it()

    def test_a_refusal_served_here_is_counted_once_this_box_served_it(self):
        super().test_a_refusal_served_here_is_counted_once_this_box_served_it()

    def test_a_request_bound_to_a_box_now_serving_another_checkpoint_goes_nowhere(self):
        super().test_a_request_bound_to_a_box_now_serving_another_checkpoint_goes_nowhere()

    def test_a_request_bound_to_a_box_that_serves_another_model_now_gets_an_answer(self):
        super().test_a_request_bound_to_a_box_that_serves_another_model_now_gets_an_answer()

    def test_a_request_the_proxy_rewrites_is_bound_as_its_client_sends_it(self):
        super().test_a_request_the_proxy_rewrites_is_bound_as_its_client_sends_it()

    def test_an_engine_that_dies_during_its_answer_binds_the_request(self):
        super().test_an_engine_that_dies_during_its_answer_binds_the_request()

    def test_this_engine_away_a_model_its_lane_served_goes_to_no_other_checkpoint(self):
        super().test_this_engine_away_a_model_its_lane_served_goes_to_no_other_checkpoint()

    def test_an_ordered_stop_binds_no_request(self):
        super().test_an_ordered_stop_binds_no_request()

    def test_a_restart_binds_what_its_engine_closed_on(self):
        super().test_a_restart_binds_what_its_engine_closed_on()

    def test_a_hop_this_box_stops_by_order_answers_in_its_clients_dialect(self):
        super().test_a_hop_this_box_stops_by_order_answers_in_its_clients_dialect()

    def test_a_dead_vllm_engine_under_an_ordered_stop_is_said_to_be_one(self):
        super().test_a_dead_vllm_engine_under_an_ordered_stop_is_said_to_be_one()

    def test_a_request_bound_here_is_refused_when_the_others_list_failed_but_named_it(self):
        super().test_a_request_bound_here_is_refused_when_the_others_list_failed_but_named_it()

    def test_a_box_that_reads_no_key_holds_what_its_own_engine_will_serve(self):
        super().test_a_box_that_reads_no_key_holds_what_its_own_engine_will_serve()

    def test_a_hop_whose_engine_closes_as_its_lane_stops_by_order_gets_a_503_that_says_so(self):
        super().test_a_hop_whose_engine_closes_as_its_lane_stops_by_order_gets_a_503_that_says_so()

    def test_an_ordered_death_in_a_stream_the_keeper_opened_is_said_in_it(self):
        super().test_an_ordered_death_in_a_stream_the_keeper_opened_is_said_in_it()

    def test_a_death_the_other_box_relayed_before_its_client_left_is_bound(self):
        super().test_a_death_the_other_box_relayed_before_its_client_left_is_bound()

    def test_a_request_bound_here_is_answered_here_once_the_other_box_said_nothing_for_long(self):
        super().test_a_request_bound_here_is_answered_here_once_the_other_box_said_nothing_for_long()

    def test_a_box_that_reads_no_key_holds_its_own_lane_from_the_same_checkpoint_too(self):
        super().test_a_box_that_reads_no_key_holds_its_own_lane_from_the_same_checkpoint_too()

    def test_a_request_bound_here_is_answered_here_when_the_other_box_moved_to_another_model(self):
        super().test_a_request_bound_here_is_answered_here_when_the_other_box_moved_to_another_model()

    def test_a_death_relayed_then_an_order_said_after_it_binds_nothing(self):
        super().test_a_death_relayed_then_an_order_said_after_it_binds_nothing()

    def test_a_death_relayed_then_an_order_said_after_a_silence_binds_nothing(self):
        super().test_a_death_relayed_then_an_order_said_after_a_silence_binds_nothing()

    def test_a_stop_job_seen_after_a_vllm_death_is_said_before_the_streams_end(self):
        super().test_a_stop_job_seen_after_a_vllm_death_is_said_before_the_streams_end()

    def test_a_death_relayed_is_bound_at_once_when_its_client_leaves_and_that_box_goes_quiet(self):
        super().test_a_death_relayed_is_bound_at_once_when_its_client_leaves_and_that_box_goes_quiet()

    def test_a_death_here_whose_order_shows_at_the_streams_end_is_unbound(self):
        super().test_a_death_here_whose_order_shows_at_the_streams_end_is_unbound()

    def test_a_death_relayed_is_bound_as_it_is_relayed(self):
        super().test_a_death_relayed_is_bound_as_it_is_relayed()

    def test_an_order_line_split_where_its_client_leaves_is_read_whole(self):
        super().test_an_order_line_split_where_its_client_leaves_is_read_whole()

    def test_the_line_end_before_an_order_line_stays_at_the_entry(self):
        super().test_the_line_end_before_an_order_line_stays_at_the_entry()

    def test_an_ndjson_cut_is_bound_before_its_client_sees_it(self):
        super().test_an_ndjson_cut_is_bound_before_its_client_sees_it()

    def test_a_cut_during_a_floor_stop_is_bound_and_no_stop_counts_it(self):
        super().test_a_cut_during_a_floor_stop_is_bound_and_no_stop_counts_it()

    def test_a_death_the_other_box_sends_after_this_boxs_client_left_is_bound(self):
        super().test_a_death_the_other_box_sends_after_this_boxs_client_left_is_bound()

    def test_a_death_the_other_box_answers_after_this_boxs_client_left_is_bound(self):
        super().test_a_death_the_other_box_answers_after_this_boxs_client_left_is_bound()

    def test_a_box_that_closes_on_a_shared_request_after_its_client_left_is_paused(self):
        super().test_a_box_that_closes_on_a_shared_request_after_its_client_left_is_paused()

    def test_a_box_away_that_refuses_a_shared_request_after_its_client_left_is_paused(self):
        super().test_a_box_away_that_refuses_a_shared_request_after_its_client_left_is_paused()

    def test_a_box_that_refuses_a_shared_request_as_its_client_leaves_serves_it_nowhere(self):
        super().test_a_box_that_refuses_a_shared_request_as_its_client_leaves_serves_it_nowhere()

    def test_a_box_that_closes_on_a_request_as_its_client_leaves_is_read_with_its_client_gone(self):
        super().test_a_box_that_closes_on_a_request_as_its_client_leaves_is_read_with_its_client_gone()

    def test_a_refusal_as_its_client_leaves_moves_no_conversation_there(self):
        super().test_a_refusal_as_its_client_leaves_moves_no_conversation_there()

    def test_an_error_as_its_client_leaves_keeps_the_conversation_there(self):
        super().test_an_error_as_its_client_leaves_keeps_the_conversation_there()

    def test_an_answer_cut_as_its_client_leaves_is_read_with_its_client_gone(self):
        super().test_an_answer_cut_as_its_client_leaves_is_read_with_its_client_gone()

    def test_a_whole_answer_as_its_client_leaves_goes_to_nobody(self):
        super().test_a_whole_answer_as_its_client_leaves_goes_to_nobody()

    def test_a_hop_a_stop_marks_during_its_count_still_gets_its_400(self):
        super().test_a_hop_a_stop_marks_during_its_count_still_gets_its_400()

    def test_a_request_the_floor_stops_during_its_count_is_refused_by_it_never_handed_on(self):
        super().test_a_request_the_floor_stops_during_its_count_is_refused_by_it_never_handed_on()

    def test_a_request_a_stop_marks_during_its_count_is_never_handed_on_as_too_long(self):
        super().test_a_request_a_stop_marks_during_its_count_is_never_handed_on_as_too_long()

    def test_a_request_the_floor_stops_during_its_count_is_refused_by_the_floor(self):
        super().test_a_request_the_floor_stops_during_its_count_is_refused_by_the_floor()

    def test_an_ollama_stream_that_ends_whole_as_its_client_leaves_binds_nothing(self):
        super().test_an_ollama_stream_that_ends_whole_as_its_client_leaves_binds_nothing()

    def test_an_answer_that_comes_after_its_client_left_goes_to_nobody(self):
        super().test_an_answer_that_comes_after_its_client_left_goes_to_nobody()

    def test_a_request_a_stop_marks_during_its_count_is_cut_never_held_or_handed_on(self):
        super().test_a_request_a_stop_marks_during_its_count_is_cut_never_held_or_handed_on()

    def test_a_stream_the_other_box_begins_after_this_boxs_client_left_is_read_for_a_death(self):
        super().test_a_stream_the_other_box_begins_after_this_boxs_client_left_is_read_for_a_death()

    def test_a_stream_whose_head_this_box_could_not_write_is_read_for_a_death(self):
        super().test_a_stream_whose_head_this_box_could_not_write_is_read_for_a_death()

    def test_a_box_that_closes_on_a_request_after_its_client_left_is_bound(self):
        super().test_a_box_that_closes_on_a_request_after_its_client_left_is_bound()

    def test_an_answer_cut_after_its_client_left_is_bound(self):
        super().test_an_answer_cut_after_its_client_left_is_bound()

    def test_the_watchs_own_end_of_a_silent_box_binds_nothing(self):
        super().test_the_watchs_own_end_of_a_silent_box_binds_nothing()

    def test_the_watchs_own_end_of_an_answer_gone_silent_binds_nothing(self):
        super().test_the_watchs_own_end_of_an_answer_gone_silent_binds_nothing()

    def test_a_stream_that_ends_after_this_boxs_client_left_binds_nothing(self):
        super().test_a_stream_that_ends_after_this_boxs_client_left_binds_nothing()

    def test_this_boxs_own_end_of_the_connection_is_no_death_of_the_other_boxs_engine(self):
        super().test_this_boxs_own_end_of_the_connection_is_no_death_of_the_other_boxs_engine()

    def test_a_retry_that_bound_nothing_keeps_the_binding_a_death_before_it_made(self):
        super().test_a_retry_that_bound_nothing_keeps_the_binding_a_death_before_it_made()

    def test_a_try_ended_by_order_keeps_the_binding_its_first_try_made(self):
        super().test_a_try_ended_by_order_keeps_the_binding_its_first_try_made()

    def test_a_stop_during_a_dead_streams_reads_counts_nothing_and_holds_nothing_back(self):
        super().test_a_stop_during_a_dead_streams_reads_counts_nothing_and_holds_nothing_back()

    def test_a_hops_death_relayed_is_never_said_an_order_by_a_stop_after_it(self):
        super().test_a_hops_death_relayed_is_never_said_an_order_by_a_stop_after_it()

    def test_a_cut_under_an_order_binds_nothing_and_tells_the_hops_box(self):
        super().test_a_cut_under_an_order_binds_nothing_and_tells_the_hops_box()

    def test_a_death_whose_own_read_saw_the_order_stays_unbound_at_the_cut(self):
        super().test_a_death_whose_own_read_saw_the_order_stays_unbound_at_the_cut()

    def test_a_death_here_whose_client_leaves_before_its_order_shows_is_unbound(self):
        super().test_a_death_here_whose_client_leaves_before_its_order_shows_is_unbound()

    def test_a_cut_is_bound_before_its_client_sees_it(self):
        super().test_a_cut_is_bound_before_its_client_sees_it()

    def test_a_death_here_whose_order_shows_before_a_cut_is_unbound(self):
        super().test_a_death_here_whose_order_shows_before_a_cut_is_unbound()

    def test_a_recovery_restart_to_a_staged_checkpoint_binds_what_its_engine_closed_on(self):
        super().test_a_recovery_restart_to_a_staged_checkpoint_binds_what_its_engine_closed_on()

    def test_an_ordered_503_whose_body_was_cut_is_still_an_order(self):
        super().test_an_ordered_503_whose_body_was_cut_is_still_an_order()

    def test_a_death_the_other_box_said_was_ordered_after_it_binds_nothing(self):
        super().test_a_death_the_other_box_said_was_ordered_after_it_binds_nothing()

    def test_a_hop_this_box_stops_by_order_before_any_answer_gets_a_503_that_says_so(self):
        super().test_a_hop_this_box_stops_by_order_before_any_answer_gets_a_503_that_says_so()

    def test_a_hop_this_box_stops_by_order_in_its_open_stream_says_so_before_the_cut(self):
        super().test_a_hop_this_box_stops_by_order_in_its_open_stream_says_so_before_the_cut()

    def test_a_stream_the_other_box_ends_by_order_binds_nothing(self):
        super().test_a_stream_the_other_box_ends_by_order_binds_nothing()

    def test_a_503_the_other_box_says_is_an_order_is_not_run_here_nor_bound(self):
        super().test_a_503_the_other_box_says_is_an_order_is_not_run_here_nor_bound()

    def test_this_engine_starting_its_lane_goes_to_no_other_checkpoint(self):
        super().test_this_engine_starting_its_lane_goes_to_no_other_checkpoint()

    def test_a_request_a_dead_vllm_engine_answered_stays_here_when_sent_again(self):
        super().test_a_request_a_dead_vllm_engine_answered_stays_here_when_sent_again()

    def test_a_request_a_dead_vllm_engine_ended_in_its_stream_stays_here_when_sent_again(self):
        super().test_a_request_a_dead_vllm_engine_ended_in_its_stream_stays_here_when_sent_again()

    def test_a_request_the_other_boxs_dead_vllm_engine_answered_goes_there_or_nowhere(self):
        super().test_a_request_the_other_boxs_dead_vllm_engine_answered_goes_there_or_nowhere()

    def test_an_engine_that_closes_the_connection_on_the_count_closed_it_on_the_request(self):
        super().test_an_engine_that_closes_the_connection_on_the_count_closed_it_on_the_request()

    def test_a_request_bound_to_a_box_that_left_the_pair_goes_nowhere(self):
        super().test_a_request_bound_to_a_box_that_left_the_pair_goes_nowhere()

    def test_a_request_bound_here_for_a_name_no_box_serves_is_this_boxs_lanes(self):
        super().test_a_request_bound_here_for_a_name_no_box_serves_is_this_boxs_lanes()

    def test_a_request_bound_to_the_other_box_on_a_box_that_reads_no_key_runs_nowhere(self):
        super().test_a_request_bound_to_the_other_box_on_a_box_that_reads_no_key_runs_nowhere()

    def test_a_request_bound_to_a_box_of_a_pair_dissolved_since_goes_nowhere(self):
        super().test_a_request_bound_to_a_box_of_a_pair_dissolved_since_goes_nowhere()

    def test_a_request_bound_while_this_box_was_alone_is_this_boxs(self):
        super().test_a_request_bound_while_this_box_was_alone_is_this_boxs()

    def test_a_box_that_refuses_this_boxs_certificate_never_had_the_request(self):
        super().test_a_box_that_refuses_this_boxs_certificate_never_had_the_request()

    def test_a_client_that_leaves_while_a_box_that_took_its_request_reads_it_pauses_nothing(self):
        super().test_a_client_that_leaves_while_a_box_that_took_its_request_reads_it_pauses_nothing()

    def test_a_shared_request_carries_this_boxs_checkpoint_and_never_the_clients(self):
        super().test_a_shared_request_carries_this_boxs_checkpoint_and_never_the_clients()

    def test_a_request_for_a_model_only_the_other_box_serves_carries_no_checkpoint(self):
        super().test_a_request_for_a_model_only_the_other_box_serves_carries_no_checkpoint()

    def test_a_request_sent_on_for_another_checkpoint_than_this_boxs_is_refused(self):
        super().test_a_request_sent_on_for_another_checkpoint_than_this_boxs_is_refused()

    def test_a_box_that_serves_no_text_lane_now_sends_the_others_model_there(self):
        super().test_a_box_that_serves_no_text_lane_now_sends_the_others_model_there()

    def test_the_lane_seen_last_keeps_its_checkpoint_while_the_unit_says_none(self):
        super().test_the_lane_seen_last_keeps_its_checkpoint_while_the_unit_says_none()

    def test_another_lane_saying_no_checkpoint_is_noted_with_none(self):
        super().test_another_lane_saying_no_checkpoint_is_noted_with_none()

    def test_a_request_bound_to_a_box_whose_list_is_unknown_is_tried_there(self):
        super().test_a_request_bound_to_a_box_whose_list_is_unknown_is_tried_there()

    def test_a_request_bound_here_goes_nowhere_once_this_box_serves_another_model(self):
        super().test_a_request_bound_here_goes_nowhere_once_this_box_serves_another_model()


class EveryCaseHasItsInProcessTwin(unittest.TestCase):
    def test_each_twin_declares_every_case_of_its_class(self):
        # as tests/test_proxy_hold.py checks its own: four tests of the sharing were not declared in its twin (found on
        # 2026-11-04), which ran them by inheritance, so a twin that stops inheriting would drop them unseen
        for twin in (TwoBoxesBehindOneEndpointInProcess, TwoBoxesServeOneModelInProcess):
            base = twin.__bases__[0]
            cases = {n for n in vars(base) if n.startswith("test_")}
            self.assertTrue(cases, base.__name__)
            self.assertEqual({n for n in vars(twin) if n.startswith("test_")}, cases, twin.__name__)


class ThePairsPieces(unittest.TestCase):
    """The pair's functions one by one, in this process: what pair.json may say, the TLS made of it, how a
    peer's list is read and kept, the hop header, the union of /v1/models, the pair port and its keeper."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = pathlib.Path(tempfile.mkdtemp(prefix="kp-pair-pieces-"))
        cls.cfg = cls.tmp / ".config/qwen38"
        cls.cfg.mkdir(parents=True)
        install_identity(cls.cfg, "spark1")
        cls.pair_file = cls.cfg / "pair.json"
        saved = dict(os.environ)
        os.environ.update({"HOME": str(cls.tmp), "QWEN38_PAIR_FILE": str(cls.pair_file), "PAIR_TTL_S": "30",
                           "PAIR_PORT": "0", "PROXY_BIND": "127.0.0.1"})
        try:
            spec = importlib.util.spec_from_file_location(f"kp_pair_pieces_{time.time_ns()}", REPO / "keepalive-proxy.py")
            cls.mod = importlib.util.module_from_spec(spec)
            saved_argv, sys.argv = sys.argv, ["keepalive-proxy.py"]
            try:
                spec.loader.exec_module(cls.mod)
            finally:
                sys.argv = saved_argv
        finally:
            os.environ.clear(); os.environ.update(saved)
        cls.lines = []
        cls.mod.log = lambda msg: cls.lines.append(msg)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.pair_file.unlink(missing_ok=True)
        self.mod._PAIR.update(seen=0.0, stamp=None, conf=None)

    def parse(self, conf):
        return self.mod._pair_parse(json.dumps(conf))

    def peer(self, name="spark2", proxy="https://127.0.0.1:30002", who="spark2"):
        return {"name": name, "proxy": proxy, "cert_file": f"pair/{name}.crt", "sha256": ident(who)[1]}

    def conf_now(self, conf):
        """conf written as pair.json and read by the proxy at once."""
        self.pair_file.write_text(json.dumps(conf))
        self.mod._PAIR["seen"] = 0.0
        return self.mod.pair_conf()

    def _relayed_behind_a_frozen_client(self, n, fp=None, meanwhile=None, cut=True, tail=(), freeze=0.0,
                                        path="/v1/chat/completions", hop=None, frozen_at=50, until=None,
                                        need_freeze=True, eof_delay=0.0, settle=None, abort=None, real_conn=False,
                                        pace=0.0,
                                        event=b'data: {"choices":[{"index":0,"delta":{"content":"x"}}]}\n\n'):
        """A stream of n events relayed by a bare handler, then the event of tail if any, then its engine's cut (or its
        end, cut=False). Its client is frozen once, at its 51st write, until the pump has read all of it and the proxy
        took the stream out of every stop's reach, or 2 s on, and freeze seconds more: the pump never waits for the
        client since v1.44.0, so the whole stream is read during the freeze, however long. meanwhile(h) runs during it,
        and a BrokenPipeError it raises is the client leaving. frozen_at and until: another write to freeze at, and
        what the freeze waits for instead (until(h)). eof_delay: seconds between the last event and the stream's end,
        so that only what the pump reads in the event itself takes the stream out of reach; settle: the freeze waits
        for the last event read and that many seconds more, not for the stream out of reach, which an end delayed less
        than its 10 s would bring anyway. A binding's deferred reads of systemd are counted in
        self.later, not run: run, they outlived the test and took the next one's systemd answers (the 15th review of
        v1.43.0). What the relay ended with; what the client got is in self.relayed. The relay gives up past 20 s of
        silence: a test whose end never comes fails, never hangs for the hour of MAX_SILENCE_S. tail: records read one
        by one after the n events. abort: the upstream's abort (True: told); self.order says when each abort returned
        and each close of the upstream came. real_conn: the client's connection a real socket, whose writes fail once
        its TCP side is shut; self.finish_at: how many writes had gone when its stream was ended."""
        m = self.mod
        got, writes, read_all, frozen, errors = [], [], threading.Event(), [], []
        self.relayed, self.later, self.order, self.finish_at = writes, [], [], []
        if real_conn:
            mine, peer = socket.socketpair()
            self.addCleanup(mine.close)
            self.addCleanup(peer.close)
            peer.setblocking(False)

        def shut():
            # its TCP side shut (shutdown from any thread): its peer reads its end
            try:
                return real_conn and peer.recv(1) == b""
            except BlockingIOError:
                return False
        silent = m.MAX_SILENCE_S
        m.MAX_SILENCE_S = 20
        self.addCleanup(setattr, m, "MAX_SILENCE_S", silent)

        class Resp:
            status = 200
            headers = {"Content-Type": "text/event-stream"}

            def __init__(self):
                self.n = 0

            def read1(self, size):
                self.n += 1
                if self.n <= n:
                    if pace:
                        time.sleep(pace)            # an engine that sends at its pace (pace: seconds an event)
                    return event
                if self.n <= n + len(tail):
                    if self.n == n + len(tail):
                        read_all.set()
                    return tail[self.n - n - 1]
                read_all.set()
                time.sleep(eof_delay)
                if not cut:
                    return b""                                          # the engine's stream ends
                raise http.client.IncompleteRead(b"", 10)              # the engine dies: its stream cut

            def close(self):
                order.append("close")

        order = self.order

        class Conn:
            def shutdown(self, how):
                got.append("shutdown")

        def chunk(b):
            if len(writes) == frozen_at and not frozen:
                # the effect waited for, not a time: the pump may parse what it read later than read1 returned it
                ready = (lambda: until(h)) if until else (lambda: read_all.is_set() and h not in m._GENERATIONS)
                if settle is not None:
                    read_all.wait(10)
                    time.sleep(settle)
                    ready = read_all.is_set
                deadline = time.monotonic() + 10
                while not ready() and time.monotonic() < deadline:
                    time.sleep(0.01)
                frozen.append(bool(ready()))       # what the test waited for came while its client read nothing
                if meanwhile:
                    try:
                        meanwhile(h)
                    except BrokenPipeError:
                        raise                       # the client leaves
                    except Exception as e:          # noqa: BLE001 - said after the relay, not taken for a client gone
                        errors.append(e)
                time.sleep(freeze)
            if shut():
                raise BrokenPipeError("its connection was shut")
            writes.append(b)
        h = object.__new__(m.H)
        self.h = h
        h.path, h._t0, h._first, h._last, h._bytes = path, time.time(), None, None, 0
        h._stop_why, h._pair_box, h._killer_fp, h._killer_bound, h._killer_before = None, hop, fp, False, None
        h._peer, h._held_sse, h.connection = "test", True, (mine if real_conn else Conn())  # the hold opened it

        def told(why):
            order.append("abort")
            return True
        h._abort_upstream = abort or told
        h._note_rid = lambda v: None
        h._scan_corruption = lambda v: False
        h._watch_tools = lambda v, a: None
        h._unbind_if_ordered_later = lambda: self.later.append(1)
        h._chunk = chunk
        h._finish = lambda: (got.append("finish"), self.finish_at.append(len(writes)))
        h._done = lambda outcome, quiet=False: (got.append(outcome), m._gen_discard(h))
        with m._GEN_LOCK:
            m._GENERATIONS.add(h)               # as _forward adds it
        self.addCleanup(m._gen_discard, h)
        h._relay_inner(Resp())
        if errors:
            raise errors[0]
        if need_freeze:
            self.assertEqual(frozen, [True], "the test needs its client frozen once, the whole stream read meanwhile")
        return got

    def _a_stop(self, reached):
        """What stop_generations marks, its engine's abort aside: the handler if it is a stop target. reached gets how
        many it marked."""
        m = self.mod

        def stop(h):
            with m._GEN_LOCK:
                targets = [x for x in m._GENERATIONS if x is h and not getattr(x, "_relaying_whole", False)]
                for x in targets:
                    x._mark_stopped("this test's memory floor", True)
            reached.append(len(targets))
        return stop

    def _bindings_read_here(self, ordered=False):
        """No binding of another test, and systemd's answer: a stop of the lane when ordered["now"]."""
        m = self.mod
        m._KILLERS.clear()
        self.addCleanup(m._KILLERS.clear)
        saved = m.unit_stopping_by_order
        said = {"now": ordered, "reads": 0}

        def read():
            said["reads"] += 1
            return said["now"]
        m.unit_stopping_by_order = read
        self.addCleanup(setattr, m, "unit_stopping_by_order", saved)
        return said

    def test_an_engines_cut_behind_a_client_that_reads_nothing_is_never_a_whole_answer(self):
        # the pump put the cut once, with 5 s to wait: the queue full that long behind a client that read nothing, it was
        # dropped, the end that followed went in, and the client's stream was closed as whole on a cut answer (the 12th
        # review of v1.43.0, 2026-11-05)
        got = self._relayed_behind_a_frozen_client(300, freeze=0.5)
        self.assertNotIn("finish", got, "a cut answer was ended as whole")
        self.assertEqual(got[-1], "UPSTREAM CUT", got)

    def test_an_engines_cut_waiting_behind_a_slow_client_is_no_stop_target(self):
        # cut by its engine while its relay wrote the events before the cut to a slow client: a stop then counted it and
        # cut it as stopped, and the cut was left unbound, its retry free to reach the other box (decision 1019; the 12th
        # review of v1.43.0, 2026-11-05)
        m = self.mod
        self._bindings_read_here()
        reached = []
        got = self._relayed_behind_a_frozen_client(300, fp="c" * 32, meanwhile=self._a_stop(reached))
        self.assertEqual(reached, [0], "an engine's cut waiting behind a slow client was a stop target")
        self.assertEqual(got[-1], "UPSTREAM CUT", got)
        self.assertEqual(m.killer_box("c" * 32), "", "an engine's cut was left unbound")
        self.assertEqual(self.later, [1], "a stop job that shows late would not undo the binding")

    def test_a_stream_ended_while_its_relay_writes_to_a_slow_client_is_no_stop_target(self):
        # its engine's stream over, its last events still on their way to a slow client: a stop counted it, started the
        # floor's episode for nothing, and cut the client of an answer whole at its engine (the 12th review of v1.43.0,
        # 2026-11-05)
        reached = []
        got = self._relayed_behind_a_frozen_client(300, meanwhile=self._a_stop(reached), cut=False)
        self.assertEqual(reached, [0], "a stream its engine had ended was a stop target")
        self.assertEqual(got[-2:], ["finish", "ok"], got)

    def test_a_death_read_behind_a_client_that_reads_nothing_is_no_stop_target_and_is_bound(self):
        # vLLM's death read by the pump behind a client that read nothing: out of a stop's reach only once the relay took
        # it, the stop counted the dead stream, cut its client as stopped and left the death unbound (decision 1019; the
        # 13th review of v1.43.0, 2026-11-05)
        m = self.mod
        self._bindings_read_here()
        reached = []
        got = self._relayed_behind_a_frozen_client(300, fp="d" * 32, meanwhile=self._a_stop(reached), cut=False,
                                                   tail=(b"data: " + VLLM_DEAD_EVENT + b"\n\n",))
        self.assertEqual(reached, [0], "a death the pump had read was a stop target")
        self.assertEqual(got[-1], "502 upstream", got)
        self.assertEqual(m.killer_box("d" * 32), "", "the death was left unbound")

    def test_a_death_read_far_behind_a_client_that_reads_nothing_is_read_at_once(self):
        # the same death 5,000 events into the stream: the pump waited to put the 1,076th behind a full queue of 1,024
        # and read nothing more, the death included, until the client took some; a stop then counted the dead stream and
        # cut it, the death never bound (the 14th review of v1.43.0, 2026-11-05)
        m = self.mod
        self._bindings_read_here()
        reached = []
        got = self._relayed_behind_a_frozen_client(5000, fp="g" * 32, meanwhile=self._a_stop(reached), cut=False,
                                                   tail=(b"data: " + VLLM_DEAD_EVENT + b"\n\n",))
        self.assertEqual(reached, [0], "a death 5,000 events in was a stop target")
        self.assertEqual(got[-1], "502 upstream", got)
        self.assertEqual(m.killer_box("g" * 32), "", "a death 5,000 events in was left unbound")

    def test_a_death_whose_client_leaves_before_its_relay_gets_there_is_bound(self):
        # vLLM's death read, its client gone before the relay reached it: bound only by the relay, it was left unbound,
        # the client's retry free to take the other box's engine down too (decision 1019; the 13th review of v1.43.0)
        m = self.mod
        self._bindings_read_here()

        def gone(h):
            raise BrokenPipeError("the client left")
        got = self._relayed_behind_a_frozen_client(300, fp="h" * 32, meanwhile=gone, cut=False,
                                                   tail=(b"data: " + VLLM_DEAD_EVENT + b"\n\n",))
        self.assertEqual(got[-1], "CLIENT GONE on write", got)
        self.assertEqual(m.killer_box("h" * 32), "", "a death its client left before was left unbound")

    def test_an_order_read_at_the_death_binds_nothing_however_late_its_relay_gets_there(self):
        # the lane's stop job ran as vLLM's engine died, and was over when the relay reached the death behind a slow
        # client: systemd read then said no order, and an ordered end was bound 10 minutes (decisions 1030 and 1041; the
        # 14th review of v1.43.0, 2026-11-05)
        m = self.mod
        ordered = self._bindings_read_here(ordered=True)

        def the_job_over(h):
            ordered["now"] = False
        # frozen once systemd was read at the death: frozen once the handler left the stop's reach, a step before that
        # read, the job could be over before it (the review of v1.44.0)
        got = self._relayed_behind_a_frozen_client(300, fp="i" * 32, meanwhile=the_job_over, cut=False, eof_delay=1.0,
                                                   until=lambda h: ordered["reads"] >= 1,
                                                   tail=(b"data: " + VLLM_DEAD_EVENT + b"\n\n",))
        self.assertEqual(got[-1], "502 upstream", got)
        self.assertIsNone(m.killer_box("i" * 32), "a death by order was bound once its job was over")

    def test_a_real_death_stays_bound_when_a_stop_job_starts_after_its_stream(self):
        # vLLM's engine died with no stop job, one started once its stream was over, before the relay reached the death
        # behind a slow client: systemd read then took the real death for the order's (the 14th review of v1.43.0,
        # 2026-11-05). Read as the death comes and at the stream's end now; a stop job within 3 s of the binding still
        # undoes it, by the deferred reads it starts (decision 1061)
        m = self.mod
        ordered = self._bindings_read_here(ordered=False)

        def a_job_starts_once_the_stream_is_over(h):
            # the pump read the stream's end (systemd read at the death and at the end): waited for, not 0.3 s (the
            # review of v1.44.0)
            deadline = time.monotonic() + 10
            while ordered["reads"] < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            ordered["now"] = True
        got = self._relayed_behind_a_frozen_client(300, fp="j" * 32, meanwhile=a_job_starts_once_the_stream_is_over,
                                                   cut=False, tail=(b"data: " + VLLM_DEAD_EVENT + b"\n\n",))
        self.assertEqual(got[-1], "502 upstream", got)
        self.assertEqual(m.killer_box("j" * 32), "", "a real death was unbound by a stop job that started after it")
        self.assertEqual(self.later, [1], "the deferred reads of systemd were not started at the binding")

    def test_a_client_too_slow_is_cut_and_its_generation_stopped(self):
        # a client that reads nothing: the queue held 1,024 events and the pump waited, its engine generating on; now
        # the spool holds up to SPOOL_MAX_BYTES, past which the generation stops and the client is cut (decision 1108)
        m = self.mod
        cap = m.SPOOL_MAX_BYTES
        m.SPOOL_MAX_BYTES = 4096
        self.addCleanup(setattr, m, "SPOOL_MAX_BYTES", cap)
        got = self._relayed_behind_a_frozen_client(300, cut=False, frozen_at=0, until=lambda h: h._relay_q.over,
                                                   need_freeze=False)       # cut before its first write, or after it
        self.assertEqual(got[-1], "CLIENT TOO SLOW", got)
        self.assertNotIn("finish", got, "a client too slow got its stream ended as whole")
        # its generation stopped, once: a too_slow that stopped nothing passed (the review of v1.44.0)
        deadline = time.monotonic() + 5
        while "abort" not in self.order and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.order.count("abort"), 1, self.order)

    def test_an_end_read_behind_a_client_that_reads_nothing_is_no_stop_target(self):
        # the pump read [DONE] and waited to put it in a queue full behind a client that read nothing: a stop counted an
        # answer whole at its engine, and cut its client, who generated all of it again (the 13th review of v1.43.0,
        # 2026-11-05)
        reached = []
        got = self._relayed_behind_a_frozen_client(300, meanwhile=self._a_stop(reached), cut=False, eof_delay=3.0, settle=0.5,
                                                   tail=(b"data: [DONE]\n\n",))
        self.assertEqual(reached, [0], "an answer whose end the pump had read was a stop target")
        self.assertEqual(got[-2:], ["finish", "ok"], got)
        self.assertEqual(self.relayed[-1], b"data: [DONE]\n\n", "the end did not reach the client")

    def test_an_anthropic_end_read_behind_a_client_that_reads_nothing_is_no_stop_target(self):
        # message_stop, the end of the Anthropic dialect Claude Code reads, read by the pump behind a client that read
        # nothing: no test reached its branch (the 14th review of v1.43.0, 2026-11-05)
        reached = []
        got = self._relayed_behind_a_frozen_client(
            300, meanwhile=self._a_stop(reached), cut=False, eof_delay=3.0, settle=0.5, path="/v1/messages",
            event=b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
                  b'"delta":{"type":"text_delta","text":"x"}}\n\n',
            tail=(b'event: message_stop\ndata: {"type":"message_stop"}\n\n',))
        self.assertEqual(reached, [0], "an Anthropic answer whose end the pump had read was a stop target")
        self.assertEqual(got[-2:], ["finish", "ok"], got)
        self.assertEqual(self.relayed[-1], b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
                         "the end did not reach the client")

    def test_a_responses_end_read_behind_a_client_that_reads_nothing_is_no_stop_target(self):
        # /v1/responses ends with response.completed and no [DONE] (SGLang 0.5.22, vLLM 0.29): its end was never read
        # as one, and a stop cut an answer whole at its engine behind a slow client (the 14th review of v1.43.0)
        reached = []
        got = self._relayed_behind_a_frozen_client(
            300, meanwhile=self._a_stop(reached), cut=False, eof_delay=3.0, settle=0.5, path="/v1/responses",
            event=b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"x"}\n\n',
            tail=(b'event: response.completed\ndata: {"type":"response.completed"}\n\n',))
        self.assertEqual(reached, [0], "a Responses answer whose end the pump had read was a stop target")
        self.assertEqual(got[-2:], ["finish", "ok"], got)

    def test_vllms_death_on_its_anthropic_route_is_recognised_when_its_engine_is_gone(self):
        # vLLM 0.29's Anthropic route says its engine's death as the chat event it could not read, an error event
        # without "EngineCore": never recognised, never bound (the 14th and 15th reviews of v1.43.0, 2026-11-05)
        m = self.mod
        self._bindings_read_here()
        death = (b'event: error\ndata: {"type":"error","error":{"type":"internal_error","message":"1 validation error '
                 b'for ChatCompletionStreamResponse\\nid\\n  Field required [type=missing, input_value={\'error\': '
                 b'{\'message\': \'En...ode\': 500}}, input_type=dict]"}}\n\n')
        anthropic = b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,' \
                    b'"delta":{"type":"text_delta","text":"x"}}\n\n'
        answers = m.engine_answers
        self.addCleanup(setattr, m, "engine_answers", answers)
        asked = []
        for gone in (True, False):
            with self.subTest(engine_gone=gone):
                m._KILLERS.clear()
                m.engine_answers = lambda path=None: (asked.append(path), not gone)[1]
                fp = ("k" if gone else "l") * 32
                got = self._relayed_behind_a_frozen_client(300, fp=fp, cut=False, path="/v1/messages", event=anthropic,
                                                           tail=(death,))
                self.assertEqual(got[-1], "502 upstream" if gone else "ok", got)
                self.assertEqual(m.killer_box(fp), "" if gone else None)
        # vLLM's /health: on the info route a pool read had not set yet, its 404 said the engine answered (the review)
        self.assertEqual(asked, ["/health", "/health"])

    def test_the_stop_says_a_stream_whose_end_was_read_is_finishing(self):
        # an answer whole at its engine, still going out to a slow client: out of the stop's reach, it was neither
        # stopped nor finishing, and the cockpit said no generation ran (the 13th to 15th reviews of v1.43.0)
        m = self.mod
        seen = []
        self._relayed_behind_a_frozen_client(300, meanwhile=lambda h: seen.append(h in m._FINISHING), cut=False,
                                             tail=(b"data: [DONE]\n\n",))
        self.assertEqual(seen, [True], "a stream whose end was read was not said finishing")
        self.assertEqual(len(m._FINISHING), 0, "a stream done was still said finishing")

    def test_a_floor_that_lands_during_an_abort_all_holds_new_generations_back(self):
        # an Abort all, then the floor while its generation still runs: the floor reaches it again and holds new
        # generations back, SGLang answering abort_all before its scheduler frees anything. As v1.43.0 has it: the 15th
        # review of v1.43.0 called it a double count, v1.44.0's three reviews disagreed, and the box's behaviour is
        # kept until it is decided
        m = self.mod
        upstream = m.UPSTREAM
        m.UPSTREAM = "http://127.0.0.1:9"       # no engine: the stop's abort_all is refused at once, nothing aborted
        self.addCleanup(setattr, m, "UPSTREAM", upstream)
        self._a_stops_state_given_back()
        h = object.__new__(m.H)
        h._stop_why, h._relay_q, h._peer = None, None, "test"
        h._upstream_sock = lambda: None
        h._stop_waiting = lambda: None
        with m._GEN_LOCK:
            m._GENERATIONS.add(h)
        self.addCleanup(m._gen_discard, h)
        self.assertEqual(m.stop_generations("this test's Abort all", floor=False)[0], 1)
        self.assertIsNone(m.floor_refusal(), "an operator's stop held new generations back")
        self.assertEqual(m.stop_generations("this test's memory floor", floor=True)[0], 1,
                         "the floor did not reach a generation still running")
        self.assertIsNotNone(m.floor_refusal(), "a floor that landed during an Abort all held nothing back")

    def test_a_stop_ends_a_running_vllm_relays_upstream_at_once(self):
        # on vLLM a stop ended a running relay's upstream only at its next event, after a write to a slow client
        # returned, up to CLIENT_IO_S, the generation going on meanwhile (the 15th review of v1.43.0, 2026-11-05)
        m = self.mod
        upstream = m.UPSTREAM
        m.UPSTREAM = "http://127.0.0.1:9"
        self.addCleanup(setattr, m, "UPSTREAM", upstream)
        self._a_stops_state_given_back()
        shut = []
        h = object.__new__(m.H)
        h._stop_why, h._peer, h._upstream_engine = None, "test", "vllm"
        h._relay_q = m._Spool(0)
        h._upstream_sock = lambda: object()
        h._stop_waiting = lambda: None
        h._shut_upstream = lambda: shut.append(1)
        with m._GEN_LOCK:
            m._GENERATIONS.add(h)
        self.addCleanup(m._gen_discard, h)
        m.stop_generations("this test's Abort all", floor=False)
        self.assertEqual(shut, [1], "a running vLLM relay's upstream was not ended by the stop")

    def test_an_ollama_streams_last_line_read_is_no_stop_target(self):
        # its last line ("done": true) on its way to a slow client: a stop counted it, and cut a whole answer once its
        # write returned (the 14th and 15th reviews of v1.43.0, 2026-11-05)
        m = self.mod
        reached, got, writes = [], [], []
        stop = self._a_stop(reached)
        last = b'{"model":"m","message":{"role":"assistant","content":""},"done":true,"done_reason":"stop"}\n'

        class Resp:
            status = 200
            headers = {"Content-Type": "application/x-ndjson"}

            def __init__(self):
                self.lines = [b'{"model":"m","message":{"role":"assistant","content":"x"},"done":false}\n'] * 3 + [last]

            def read1(self, size):
                return self.lines.pop(0) if self.lines else b""

            def close(self):
                pass

        def chunk(b):
            if b == last:
                stop(h)                         # the stop lands while the last line is written
            writes.append(b)
        h = object.__new__(m.H)
        h.path, h._t0, h._first, h._last, h._bytes = "/api/chat", time.time(), None, None, 0
        h.command = "POST"
        h._stop_why, h._pair_box, h._peer, h._held_sse = None, None, "test", False
        h._out_ndjson, h._answer_body = True, bytearray()
        h._begin = lambda status, headers: None
        h._chunk = chunk
        h._finish = lambda: got.append("finish")
        h._done = lambda outcome, quiet=False: (got.append(outcome), m._gen_discard(h))
        h.connection = types.SimpleNamespace(shutdown=lambda how: got.append("shutdown"))
        with m._GEN_LOCK:
            m._GENERATIONS.add(h)
        self.addCleanup(m._gen_discard, h)
        h._relay_inner(Resp())
        self.assertEqual(reached, [0], "an Ollama stream whose last line was read was a stop target")
        self.assertEqual(got[-2:], ["finish", "ok non-sse"], got)

    def _a_stops_state_given_back(self):
        """What a stop changes (the floor, the stops' windows) given back at cleanup, and the key its abort_all
        carries this test's: ThePairsPieces gives HOME back after its import, and this machine's real key file was read
        and sent to the test's own server (the review of v1.44.0)."""
        m = self.mod
        floor, key = dict(m._FLOOR), m.UPSTREAM_API_KEY
        self.addCleanup(lambda: (m._FLOOR.clear(), m._FLOOR.update(floor)))
        for name in ("_STOP_WINDOWS", "_STOP_WINDOW"):      # read with getattr: a proxy before v1.44.0's review has one
            if hasattr(m, name):
                kept = dict(getattr(m, name))
                self.addCleanup(lambda name=name, kept=kept: (getattr(m, name).clear(), getattr(m, name).update(kept)))
        self.addCleanup(setattr, m, "UPSTREAM_API_KEY", key)
        m.UPSTREAM_API_KEY = "k-test-stop"      # never this machine's real key file

    def _abort_all_server(self, answer):
        """An engine whose abort_all answers what answer(n) says for its nth: (status, seconds it takes first)."""
        m = self.mod
        seen = []

        class Aborts(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                seen.append(self.headers.get("Authorization"))
                status, wait = answer(len(seen))
                time.sleep(wait)
                self.send_response(status)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Aborts)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        m.UPSTREAM = f"http://127.0.0.1:{srv.server_address[1]}"
        return seen

    def _a_stop_made(self, how):
        """stop_generations as the cockpit's Abort all makes it, against an engine whose abort_all answers 200
        ("goes"), 503 ("fails"), or none listening ("refused", at once): the state it changes given back."""
        m = self.mod
        upstream = m.UPSTREAM
        self.addCleanup(setattr, m, "UPSTREAM", upstream)
        self._a_stops_state_given_back()
        self.aborts_seen = []
        if how == "refused":
            m.UPSTREAM = "http://127.0.0.1:9"   # no engine: abort_all refused at once, nothing aborted
        else:
            self.aborts_seen = self._abort_all_server(lambda n: (200 if how == "goes" else 503, 0.0))
        return m.stop_generations("this test's Abort all", floor=False)

    def _a_generation_now(self):
        """A generation registered, then going out, as _forward makes them."""
        m = self.mod
        h = object.__new__(m.H)
        h._stop_why, h._relay_q, h._peer = None, None, "test"
        h._now_a_generation()
        getattr(h, "_marked_if_a_stop_is_under_way", lambda: None)()   # before v1.44.0's review, marked as it registered
        self.addCleanup(m._gen_discard, h)
        return h

    def test_a_stops_abort_all_carries_the_tests_key_never_this_machines(self):
        # ThePairsPieces gives HOME back after its import: this machine's real key file was read and sent (the review)
        self._a_stop_made("goes")
        self.assertEqual(self.aborts_seen, ["Bearer k-test-stop"])

    def test_a_hop_too_slow_is_told_the_order_however_long_its_box_reads_nothing(self):
        # the other box read nothing a while (its own client paused): cut at SPOOL_CUT_WAIT_S, the order this box decided
        # could not be written, and that box read a bare cut, bound 10 minutes (decisions 1019 and 1031, the 2nd review
        # of v1.44.0)
        m = self.mod
        cap, wait = m.SPOOL_MAX_BYTES, m.SPOOL_CUT_WAIT_S
        m.SPOOL_MAX_BYTES, m.SPOOL_CUT_WAIT_S = 4096, 0.3
        self.addCleanup(setattr, m, "SPOOL_MAX_BYTES", cap)
        self.addCleanup(setattr, m, "SPOOL_CUT_WAIT_S", wait)
        got = self._relayed_behind_a_frozen_client(300, hop="spark1", cut=False, frozen_at=0, freeze=1.0,
                                                   until=lambda h: h._relay_q.over, need_freeze=False, real_conn=True)
        self.assertEqual(got[-1], "CLIENT TOO SLOW", got)
        self.assertIn(b"x_alzeo_ended", b"".join(self.relayed), "the other box was not told the order")

    def test_a_hop_too_slow_after_a_death_by_order_says_the_order(self):
        # the pump read its engine's death while its lane's stop ran: the order put before the death, still in the
        # spool when it went over, was never said, and the other box bound an ordered end (the 2nd review of v1.44.0)
        m = self.mod
        self._bindings_read_here(ordered=True)
        cap = m.SPOOL_MAX_BYTES
        m.SPOOL_MAX_BYTES = 4096
        self.addCleanup(setattr, m, "SPOOL_MAX_BYTES", cap)
        more = b'data: {"choices":[{"index":0,"delta":{"content":"' + b"y" * 5000 + b'"}}]}\n\n'
        got = self._relayed_behind_a_frozen_client(1, hop="spark1", cut=False, frozen_at=0,
                                                   until=lambda h: h._relay_q.over, need_freeze=False,
                                                   tail=(b"data: " + VLLM_DEAD_EVENT + b"\n\n", more))
        self.assertEqual(got[-1], "CLIENT TOO SLOW", got)
        self.assertIn(b"x_alzeo_ended", b"".join(self.relayed), "an order put before a death by order was lost")

    def test_a_stop_during_the_death_probe_never_takes_the_death(self):
        # vLLM's Anthropic death is probed on /health, up to 10 s: a stop during the probe marked the dying stream, and
        # its death reached its client as a stop, unbound; out of every stop's reach before the probe, nothing in the
        # pump proved it (the 2nd review of v1.44.0)
        m = self.mod
        self._bindings_read_here()
        answers = m.engine_answers
        self.addCleanup(setattr, m, "engine_answers", answers)
        reached = []

        def a_stop_during_the_probe(path=None):
            with m._GEN_LOCK:
                targets = [x for x in m._GENERATIONS if not getattr(x, "_stop_why", None)]
                for x in targets:
                    x._mark_stopped("this test's memory floor", True)
            reached.append(len(targets))
            return False                        # its engine gone
        m.engine_answers = a_stop_during_the_probe
        death = (b'event: error\ndata: {"type":"error","error":{"type":"internal_error","message":"1 validation error '
                 b'for ChatCompletionStreamResponse"}}\n\n')
        got = self._relayed_behind_a_frozen_client(
            300, fp="q" * 32, cut=False, path="/v1/messages", tail=(death,),
            event=b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
                  b'"delta":{"type":"text_delta","text":"x"}}\n\n')
        self.assertEqual(reached, [0], "a stop during the probe reached the dying stream")
        self.assertEqual(got[-1], "502 upstream", got)
        self.assertEqual(m.killer_box("q" * 32), "", "a death probed during a stop was not bound")

    def test_a_failed_responses_stream_is_never_said_finishing(self):
        # response.failed, then its engine's end: read at its end as no whole answer, then said finishing at its EOF
        # (the 2nd review of v1.44.0)
        m = self.mod
        seen = []
        self._relayed_behind_a_frozen_client(
            300, meanwhile=lambda h: seen.append(h in m._FINISHING), cut=False, settle=0.5, path="/v1/responses",
            event=b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"x"}\n\n',
            tail=(b'event: response.failed\ndata: {"type":"response.failed"}\n\n',))
        self.assertEqual(seen, [False], "a failed stream was said finishing at its end")

    def test_a_hops_stream_its_engine_aborted_says_the_order(self):
        # an abort this proxy did not make (an abort_all the cockpit sent straight, SGLang's own): cut bare, the other
        # box read its engine's death and bound it 10 minutes (the 2nd review of v1.44.0)
        self._bindings_read_here()
        aborted = b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"abort"}]}\n\n'
        got = self._relayed_behind_a_frozen_client(10, hop="spark1", cut=False, frozen_at=-1, need_freeze=False,
                                                   tail=(aborted,))
        self.assertEqual(got[-1], "UPSTREAM ABORTED", got)
        self.assertIn(b"x_alzeo_ended", b"".join(self.relayed), "the other box was not told the abort binds nothing")

    def test_a_stopped_stream_whose_engine_knows_no_rid_is_drained(self):
        # a stream a stop marked before its relay began, its abort refused for want of a rid: closed, its engine
        # generated on for nobody (sglang#35255); drained, as drop_upstream does (the 2nd review of v1.44.0)
        m = self.mod
        done, drained, order = [], [], []

        class Resp:
            status = 200
            headers = {"Content-Type": "text/event-stream"}

            def close(self):
                order.append("close")
        h = object.__new__(m.H)
        h.path, h._peer, h._stop_why = "/v1/chat/completions", "test", "this test's Abort all"
        h._abort_upstream = lambda why: False
        h._drain_detached = lambda resp: drained.append(1)
        h._cut_stopped = lambda: done.append("cut")
        h._relay_inner(Resp())
        self.assertEqual(done, ["cut"])
        self.assertEqual((drained, order), ([1], []), "a stopped stream whose engine knows no rid was closed, not drained")

    def test_a_client_too_slow_in_a_keepalive_write_is_cut_too_slow(self):
        # its relay in a keepalive's write when the spool went over: said gone, not too slow, and drop_upstream ran a
        # second time, its drain begun again (the 2nd review of v1.44.0)
        m = self.mod
        cap, wait, ka = m.SPOOL_MAX_BYTES, m.SPOOL_CUT_WAIT_S, m.KEEPALIVE_S
        m.SPOOL_MAX_BYTES, m.SPOOL_CUT_WAIT_S, m.KEEPALIVE_S = 4096, 0.3, 0.2
        self.addCleanup(setattr, m, "SPOOL_MAX_BYTES", cap)
        self.addCleanup(setattr, m, "SPOOL_CUT_WAIT_S", wait)
        self.addCleanup(setattr, m, "KEEPALIVE_S", ka)
        got, in_ka = [], threading.Event()
        mine, peer = socket.socketpair()
        self.addCleanup(mine.close)
        self.addCleanup(peer.close)

        class Resp:
            status = 200
            headers = {"Content-Type": "text/event-stream"}

            def read1(self, size):
                in_ka.wait(10)                  # quiet until its relay is in a keepalive's write
                return b'data: {"choices":[{"index":0,"delta":{"content":"' + b"z" * 200 + b'"}}]}\n\n'

            def close(self):
                pass

        def chunk(b):
            if b == m.KA_OPENAI:
                in_ka.set()
                if not select.select([peer], [], [], 8)[0] or peer.recv(1) != b"":
                    raise AssertionError("never unblocked")
                raise BrokenPipeError("shut")
        h = object.__new__(m.H)
        h.path, h._t0, h._first, h._last, h._bytes = "/v1/chat/completions", time.time(), None, None, 0
        h._stop_why, h._pair_box, h._killer_fp, h._killer_bound, h._killer_before = None, None, None, False, None
        h._peer, h._held_sse, h.connection = "test", True, mine
        h._abort_upstream = lambda why: True
        h._note_rid = lambda v: None
        h._scan_corruption = lambda v: False
        h._watch_tools = lambda v, a: None
        h._chunk = chunk
        h._finish = lambda: got.append("finish")
        h._done = lambda outcome, quiet=False: (got.append(outcome), m._gen_discard(h))
        with m._GEN_LOCK:
            m._GENERATIONS.add(h)
        self.addCleanup(m._gen_discard, h)
        h._relay_inner(Resp())
        self.assertEqual(got[-1], "CLIENT TOO SLOW", got)

    def test_a_stops_window_stays_open_while_its_abort_all_answers(self):
        # its window was ABORT_TIMEOUT_S + 2 s from its start, the abort_all's timeout bounding the connection and each
        # read apart: a slow answer outlived it, and a generation that went out then was ended by it unmarked (the 2nd
        # review of v1.44.0)
        m = self.mod
        upstream, timeout = m.UPSTREAM, m.ABORT_TIMEOUT_S
        self.addCleanup(setattr, m, "UPSTREAM", upstream)
        self.addCleanup(setattr, m, "ABORT_TIMEOUT_S", timeout)
        self._a_stops_state_given_back()
        m.ABORT_TIMEOUT_S = 1.0                 # a window of 3 s before the 2nd review, each read 1 s at most

        class Drip(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                out = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nX-Pad: " + b"x" * 20 + b"\r\n\r\n{}"
                for i in range(len(out)):       # a byte every 0.1 s: each read well within its 1 s, about 7 s in all
                    self.wfile.write(out[i:i + 1])
                    self.wfile.flush()
                    time.sleep(0.1)
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Drip)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        m.UPSTREAM = f"http://127.0.0.1:{srv.server_address[1]}"
        t = threading.Thread(target=m.stop_generations, args=("this test's Abort all",), kwargs={"floor": False})
        t.start()
        self.addCleanup(t.join, 15)             # ended before the windows are given back
        time.sleep(4.0)
        self.assertTrue(t.is_alive(), "the test needs the abort_all still answering")
        self.assertEqual(self._a_generation_now()._stop_why, "this test's Abort all",
                         "a generation that went out while abort_all still answered was not the stop's")
        t.join(10)

    def test_a_stop_that_raises_still_closes_its_window(self):
        # open until its aborts are answered (the 2nd review of v1.44.0): a stop that raised before saying its end left
        # it open for good, every new generation cut until a restart
        m = self.mod
        self._a_stops_state_given_back()
        engines = m._stop_engines
        self.addCleanup(setattr, m, "_stop_engines", engines)

        def fails(targets):
            raise RuntimeError("can't start new thread")
        m._stop_engines = fails
        with self.assertRaises(RuntimeError):
            m.stop_generations("this test's Abort all", floor=False)
        with m._GEN_LOCK:
            w = m._stop_window_now()
        self.assertIsNotNone(w, "the window closed at once, the cockpit's own abort_all still to come")
        self.assertLessEqual(w["until"], time.monotonic() + m.COCKPIT_ABORT_GRACE_S, "a stop that raised left its window open")

    def test_a_drained_answers_spool_is_freed(self):
        # its client gone, its engine knowing no rid of it: the answer drained to its end, every event queued before kept
        # alive the drain long, up to DRAIN_MAX_S (the 4th review of v1.44.0)
        hs = []

        def leaves(h):
            hs.append(h)
            raise BrokenPipeError("the client left")
        got = self._relayed_behind_a_frozen_client(300, cut=False, meanwhile=leaves, abort=lambda why: False)
        self.assertEqual(got[-1], "CLIENT GONE on write", got)
        self.assertEqual(hs[0]._relay_q.bytes, 0, "what waited for a client gone stayed through the drain")

    def test_a_stop_wakes_an_idle_relay_with_no_keepalive(self):
        # its relay idle, waiting for the engine's next event, when a stop lands: woken with nothing to read, it took
        # the wake for a keepalive's silence and wrote one to the stopped client before cutting it (the 6th review);
        # the wake marker that keeps it from that was held by no test (the 7th review of v1.44.0)
        m = self.mod
        reached = []

        def a_stop_once_idle():
            _until(lambda: getattr(self, "h", None) is not None and len(getattr(self, "relayed", ())) >= 20, 10)
            time.sleep(0.3)                     # the relay waits for the engine's next event
            self._a_stop(reached)(self.h)
        self.h = None
        threading.Thread(target=a_stop_once_idle, daemon=True).start()
        self._relayed_behind_a_frozen_client(20, cut=False, eof_delay=3.0, frozen_at=-1, need_freeze=False)
        self.assertEqual(reached, [1], "the test needs the stop to reach its idle relay")
        self.assertNotIn(m.KA_OPENAI, self.relayed, "a stopped client got a keepalive before its cut")

    def test_a_stop_frees_its_frozen_relays_spool(self):
        # a stopped relay frozen in a write kept what waited in its spool, never written once stopped, up to
        # SPOOL_MAX_BYTES and CLIENT_IO_S long, on the box whose memory the floor's stop defends; cleared alone, it
        # filled again while the engine still sent (the 5th and 6th reviews of v1.44.0)
        reached, held = [], []

        def a_stop_while_frozen(h):
            held.append(h._relay_q.bytes)
            self._a_stop(reached)(h)
            held.append(h._relay_q.bytes)
            time.sleep(0.5)                     # the engine still sending
            held.append(h._relay_q.bytes)
        self._relayed_behind_a_frozen_client(3000, cut=False, pace=0.002, until=lambda h: h._relay_q.bytes > 10000,
                                             meanwhile=a_stop_while_frozen)
        self.assertEqual(reached, [1], "the test needs the stop to reach the running generation")
        self.assertGreater(held[0], 10000, "the test needs its spool full when the stop came")
        self.assertEqual(held[1:], [0, 0], "a stop left what waited, or let what came after fill it again")

    def test_a_client_too_slows_spool_is_freed_before_its_abort(self):
        # freed before the abort's round trip, up to ABORT_TIMEOUT_S on an engine the floor finds slow (the 4th and 5th
        # reviews of v1.44.0)
        m = self.mod
        cap = m.SPOOL_MAX_BYTES
        m.SPOOL_MAX_BYTES = 4096
        self.addCleanup(setattr, m, "SPOOL_MAX_BYTES", cap)
        at_abort = []

        def abort(why):
            at_abort.append(self.h._relay_q.bytes)
            self.order.append("abort")
            return True
        got = self._relayed_behind_a_frozen_client(300, cut=False, frozen_at=0, need_freeze=False, abort=abort,
                                                   until=lambda h: h._relay_q.over)
        self.assertEqual(got[-1], "CLIENT TOO SLOW", got)
        self.assertEqual(at_abort, [0], "the spool was freed only after the abort's round trip")

    def test_a_take_over_of_a_request_a_stop_marked_answers_it_here(self):
        # the stop's mark read under the lock the stop marks under, where the hand-off is decided: read before it, a
        # stop in between had the other box serve its request (the 4th and 5th reviews of v1.44.0)
        m = self.mod
        calls, cut = [], []
        h = object.__new__(m.H)
        h._peer, h._stop_why, h._hold_until, h._held_sse = "test", "this test's Abort all", None, False
        h._pair_alt = ([{"name": "spark2"}], "qwen3.8-27b", {"name": "spark1"}, (), [], None)
        h._forward_peer = lambda *a, **k: calls.append(1)
        h._floor_refused = lambda: False
        h._cut_stopped = lambda: cut.append(1)
        self.assertTrue(h._pair_take_over(b"{}"), "a stopped request's take-over said nothing answered it")
        self.assertEqual((calls, cut), ([], [1]), "a request a stop marked was handed to the other box")

    def test_a_client_too_slows_spool_is_freed_at_once(self):
        # what waited in the spool of a client too slow is never written: it stayed SPOOL_MAX_BYTES and more behind a
        # hop that reads again only within CLIENT_IO_S, or a drain of up to DRAIN_MAX_S (the 3rd review of v1.44.0)
        m = self.mod
        cap = m.SPOOL_MAX_BYTES
        m.SPOOL_MAX_BYTES = 4096
        self.addCleanup(setattr, m, "SPOOL_MAX_BYTES", cap)
        held = []
        got = self._relayed_behind_a_frozen_client(300, hop="spark1", cut=False, frozen_at=0, need_freeze=False,
                                                   until=lambda h: h._relay_q.over and h._relay_q.bytes == 0,
                                                   meanwhile=lambda h: held.append(h._relay_q.bytes), real_conn=True)
        self.assertEqual(got[-1], "CLIENT TOO SLOW", got)
        self.assertEqual(held, [0], "the spool of a client too slow was kept")

    def test_a_stop_whose_engine_refuses_its_abort_all_keeps_no_window(self):
        # no engine listening: the cockpit's own abort_all reaches none either, and the window open 8 s cut the requests
        # that came then, where they waited for the engine and were served (the review of v1.44.0)
        _, sent = self._a_stop_made("refused")
        self.assertFalse(sent, "the test needs the proxy's abort_all refused")
        self.assertIsNone(self._a_generation_now()._stop_why, "a generation after a stop no engine heard was stopped")

    def test_two_stops_at_once_keep_each_its_window(self):
        # the floor's stop and an operator's Abort all at once: the first to end closed the window of the other, still
        # open for the cockpit's own abort_all (the review of v1.44.0)
        m = self.mod
        upstream = m.UPSTREAM
        self.addCleanup(setattr, m, "UPSTREAM", upstream)
        self._a_stops_state_given_back()
        first_in, second_in = threading.Event(), threading.Event()

        def answer(n):
            # the first abort_all answers once the second came and was refused: its stop ends last, its window closed
            if n == 1:
                first_in.set()
                second_in.wait(5)
                return 200, 0.5
            second_in.set()
            return 503, 0.0
        self._abort_all_server(answer)
        first = threading.Thread(target=m.stop_generations, args=("this test's Abort all",), kwargs={"floor": False})
        first.start()
        self.assertTrue(first_in.wait(5), "the first stop's abort_all never came")
        m.stop_generations("this test's memory floor", floor=True)     # refused 503: its window open 8 s more
        first.join(10)
        self.assertFalse(first.is_alive(), "the first stop never ended")
        self.assertEqual(self._a_generation_now()._stop_why, "this test's memory floor",
                         "a stop that ended closed the window of one still open")

    def test_a_generation_that_registers_in_a_stops_window_is_marked_as_it_goes_out(self):
        # marked as it registered, before the size guard: one the guard then held (its engine warming up, its count
        # unanswered) kept the mark through its hold, up to 20 minutes, and was cut after it (the review of v1.44.0)
        m = self.mod
        self._a_stop_made("fails")
        h = object.__new__(m.H)
        h._stop_why, h._relay_q, h._peer = None, None, "test"
        h._now_a_generation()
        self.addCleanup(m._gen_discard, h)
        self.assertIsNone(h._stop_why, "a generation was marked as it registered, before the size guard")
        h._marked_if_a_stop_is_under_way()
        self.assertEqual(h._stop_why, "this test's Abort all", "a generation going out in the stop's window was not marked")

    def test_a_vllm_client_that_leaves_is_no_death_of_its_engine(self):
        # vLLM: its client gone, the proxy shuts the upstream (closing is vLLM's abort), and the pump read the read that
        # fails then as its engine cutting the stream, before stop was set: bound 10 minutes (decision 1019; the review
        # of v1.44.0, 18 of 60 on loopback)
        m = self.mod
        self._bindings_read_here()
        fp, shut, got, writes = "v" * 32, threading.Event(), [], []

        class Resp:
            status = 200
            headers = {"Content-Type": "text/event-stream"}

            def __init__(self):
                self.n = 0

            def read1(self, size):
                self.n += 1
                if self.n <= 3:
                    return b'data: {"choices":[{"index":0,"delta":{"content":"x"}}]}\n\n'
                shut.wait(10)
                raise http.client.IncompleteRead(b"", 10)      # the socket this proxy shut

            def close(self):
                pass

        class Conn:
            def shutdown(self, how):
                pass

        def chunk(b):
            if writes:
                raise BrokenPipeError("the client left")
            writes.append(b)

        def shut_upstream():
            shut.set()
            time.sleep(0.3)                 # the pump reads the shut before its caller sets stop, as a slow switch has it
        h = object.__new__(m.H)
        h.path, h._t0, h._first, h._last, h._bytes = "/v1/chat/completions", time.time(), None, None, 0
        h._stop_why, h._pair_box, h._killer_fp, h._killer_bound, h._killer_before = None, None, fp, False, None
        h._peer, h._held_sse, h.connection, h._upstream_engine = "test", True, Conn(), "vllm"
        h._shut_upstream = shut_upstream
        h._note_rid = lambda v: None
        h._scan_corruption = lambda v: False
        h._watch_tools = lambda v, a: None
        h._unbind_if_ordered_later = lambda: None
        h._chunk = chunk
        h._finish = lambda: got.append("finish")
        h._done = lambda outcome, quiet=False: (got.append(outcome), m._gen_discard(h))
        with m._GEN_LOCK:
            m._GENERATIONS.add(h)
        self.addCleanup(m._gen_discard, h)
        h._relay_inner(Resp())
        time.sleep(0.5)                     # the pump's end
        self.assertEqual(got[-1], "CLIENT GONE on write", got)
        self.assertIsNone(m.killer_box(fp), "its client gone, the proxy's own close bound the request")

    def test_a_hops_vllm_anthropic_death_is_said_so_the_entry_reads_it(self):
        # vLLM's death on /v1/messages, said as a validation error with no EngineCore: the box that sent the request on
        # reads a death in EngineCore or in this proxy's own words, and neither box bound it (the review of v1.44.0)
        m = self.mod
        self._bindings_read_here()
        answers = m.engine_answers
        self.addCleanup(setattr, m, "engine_answers", answers)
        m.engine_answers = lambda path=None: False
        death = (b'event: error\ndata: {"type":"error","error":{"type":"internal_error","message":"1 validation error '
                 b'for ChatCompletionStreamResponse\\nid\\n  Field required [type=missing]"}}\n\n')
        got = self._relayed_behind_a_frozen_client(
            300, hop="spark1", cut=False, path="/v1/messages",
            event=b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
                  b'"delta":{"type":"text_delta","text":"x"}}\n\n', tail=(death,))
        self.assertEqual(got[-1], "502 upstream", got)
        said = b"".join(self.relayed)
        self.assertIn(m.HOP_CLOSED_B, said, "the other box was not told its engine's death in words it reads")
        self.assertNotIn(b"ChatCompletionStreamResponse", said)

    def test_a_client_too_slow_has_its_generation_aborted_before_the_upstream_closes(self):
        # the relay closed the upstream as soon as the spool was over, while the pump's abort was on its way: the
        # close first, an engine without sglang#35255 dropped the state the abort needed (the review of v1.44.0)
        m = self.mod
        cap = m.SPOOL_MAX_BYTES
        m.SPOOL_MAX_BYTES = 4096
        self.addCleanup(setattr, m, "SPOOL_MAX_BYTES", cap)

        def slow_abort(why):
            time.sleep(0.5)                 # an abort by rid, on its way
            self.order.append("abort")
            return True
        got = self._relayed_behind_a_frozen_client(300, cut=False, frozen_at=0, until=lambda h: h._relay_q.over,
                                                   need_freeze=False, abort=slow_abort)
        self.assertEqual(got[-1], "CLIENT TOO SLOW", got)
        deadline = time.monotonic() + 5
        while "abort" not in self.order and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.order[:1], ["abort"], f"the upstream closed before its abort: {self.order}")

    def test_a_client_too_slow_whose_engine_knows_no_rid_has_its_answer_drained(self):
        # no rid the engine knows: the upstream was shut with no abort, the engine generating on for nobody where it
        # does not see a close (sglang#35255); drained instead, as drop_upstream does (the review of v1.44.0)
        m = self.mod
        cap = m.SPOOL_MAX_BYTES
        m.SPOOL_MAX_BYTES = 4096
        self.addCleanup(setattr, m, "SPOOL_MAX_BYTES", cap)
        shut = []
        saved = m.H._shut_upstream
        self.addCleanup(setattr, m.H, "_shut_upstream", saved)
        m.H._shut_upstream = lambda h: shut.append(1)
        n0 = len(self.lines)
        got = self._relayed_behind_a_frozen_client(300, cut=False, frozen_at=0, until=lambda h: h._relay_q.over,
                                                   need_freeze=False, abort=lambda why: False)
        self.assertEqual(got[-1], "CLIENT TOO SLOW", got)
        deadline = time.monotonic() + 5
        while "close" not in self.order and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(shut, [], "the upstream was shut with no abort")
        self.assertEqual(self.order, ["close"], f"the upstream closed before its answer was drained: {self.order}")
        self.assertTrue(any("drained an abandoned" in ln for ln in self.lines[n0:]), "the answer was not drained")

    def test_a_hop_too_slow_after_its_engines_death_says_no_order(self):
        # the pump read the death, then the spool went over: the box that sent the request on was told an order, and
        # undid the binding of a real death (the review of v1.44.0)
        m = self.mod
        self._bindings_read_here()
        cap = m.SPOOL_MAX_BYTES
        m.SPOOL_MAX_BYTES = 4096
        self.addCleanup(setattr, m, "SPOOL_MAX_BYTES", cap)
        more = b'data: {"choices":[{"index":0,"delta":{"content":"' + b"y" * 5000 + b'"}}]}\n\n'
        got = self._relayed_behind_a_frozen_client(1, hop="spark1", cut=False, frozen_at=0,
                                                   until=lambda h: h._relay_q.over, need_freeze=False,
                                                   tail=(b"data: " + VLLM_DEAD_EVENT + b"\n\n", more))
        self.assertEqual(got[-1], "CLIENT TOO SLOW", got)
        self.assertNotIn(b"x_alzeo_ended", b"".join(self.relayed), "an order was said after its engine's death")

    def test_an_end_over_the_spools_bound_stops_nothing(self):
        # its end the event that went over the bound: a generation whole at its engine was aborted and its client cut,
        # who generated all of it again (the review of v1.44.0)
        m = self.mod
        cap = m.SPOOL_MAX_BYTES
        m.SPOOL_MAX_BYTES = 4096
        self.addCleanup(setattr, m, "SPOOL_MAX_BYTES", cap)
        end = b'event: message_stop\ndata: {"type":"message_stop","pad":"' + b"p" * 1200 + b'"}\n\n'
        event = (b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
                 b'"delta":{"type":"text_delta","text":"x"}}\n\n')
        n = 3000 // len(event)
        got = self._relayed_behind_a_frozen_client(n, cut=False, path="/v1/messages", event=event, tail=(end,),
                                                   frozen_at=0, settle=0.3, need_freeze=False)
        self.assertEqual(got[-2:], ["finish", "ok"], got)
        self.assertNotIn("abort", self.order, "a generation whole at its engine was aborted")

    def test_a_dead_stream_is_never_said_finishing(self):
        # vLLM's death, then its [DONE]: the stop said a finished answer was still going out whole (the review of
        # v1.44.0)
        m = self.mod
        self._bindings_read_here()
        seen = []
        self._relayed_behind_a_frozen_client(300, meanwhile=lambda h: seen.append(h in m._FINISHING), cut=False,
                                             tail=(b"data: " + VLLM_DEAD_EVENT + b"\n\n", b"data: [DONE]\n\n"))
        self.assertEqual(seen, [False], "a dead stream was said finishing")

    def test_a_decision_that_fails_in_the_pump_still_ends_the_stream(self):
        # _engine_end raising in the pump: its 'e' never went, and the relay waited MAX_SILENCE_S, then ended the
        # stream as whole (the review of v1.44.0)
        m = self.mod
        h_engine_end = m.H._engine_end
        self.addCleanup(setattr, m.H, "_engine_end", h_engine_end)

        def fails(self, put, died=False):
            raise RuntimeError("can't start new thread")
        m.H._engine_end = fails
        t0 = time.monotonic()
        got = self._relayed_behind_a_frozen_client(60, fp="w" * 32, need_freeze=False, frozen_at=-1)
        self.assertEqual(got[-1], "UPSTREAM CUT", got)
        self.assertLess(time.monotonic() - t0, 10, "the relay waited for an end the pump never put")

    def test_a_death_whose_probe_is_slow_is_out_of_every_stops_reach_first(self):
        # the probe of vLLM's Anthropic death takes up to 10 s: a stop during it marked the stream, and the death
        # reached its client as a stop, unbound (the review of v1.44.0)
        m = self.mod
        calls = []
        answers = m.engine_answers
        self.addCleanup(setattr, m, "engine_answers", answers)
        m.engine_answers = lambda path=None: calls.append("probe") or False
        death = (b'event: error\ndata: {"type":"error","error":{"type":"internal_error","message":"1 validation error '
                 b'for ChatCompletionStreamResponse"}}\n\n')
        self.assertTrue(m.engine_died_in(death, lambda: calls.append("out of reach")))
        self.assertEqual(calls, ["out of reach", "probe"])

    def test_a_frozen_client_is_cut_too_slow_within_seconds(self):
        # a client that reads nothing at all: its relay stayed in its write CLIENT_IO_S (900 s), the spool with it,
        # and was said gone, never too slow (the review of v1.44.0)
        m = self.mod
        cap, wait = m.SPOOL_MAX_BYTES, m.SPOOL_CUT_WAIT_S
        m.SPOOL_MAX_BYTES, m.SPOOL_CUT_WAIT_S = 4096, 0.5
        self.addCleanup(setattr, m, "SPOOL_MAX_BYTES", cap)
        self.addCleanup(setattr, m, "SPOOL_CUT_WAIT_S", wait)
        got, in_write = [], threading.Event()
        mine, peer = socket.socketpair()
        self.addCleanup(mine.close)
        self.addCleanup(peer.close)

        class Resp:
            status = 200
            headers = {"Content-Type": "text/event-stream"}

            def __init__(self):
                self.n = 0

            def read1(self, size):
                self.n += 1
                if self.n > 1:
                    in_write.wait(10)       # its relay in its first write, the spool going over behind it
                return b'data: {"choices":[{"index":0,"delta":{"content":"' + b"z" * 200 + b'"}}]}\n\n'

            def close(self):
                pass

        def chunk(b):
            in_write.set()
            # a client that reads nothing: its write returns once its socket's TCP side is shut, its peer reading its end
            if not select.select([peer], [], [], 8)[0] or peer.recv(1) != b"":
                raise AssertionError("never unblocked")
            raise BrokenPipeError("shut")
        h = object.__new__(m.H)
        h.path, h._t0, h._first, h._last, h._bytes = "/v1/chat/completions", time.time(), None, None, 0
        h._stop_why, h._pair_box, h._killer_fp, h._killer_bound, h._killer_before = None, None, None, False, None
        h._peer, h._held_sse, h.connection = "test", True, mine
        h._abort_upstream = lambda why: True
        h._note_rid = lambda v: None
        h._scan_corruption = lambda v: False
        h._watch_tools = lambda v, a: None
        h._chunk = chunk
        h._finish = lambda: got.append("finish")
        h._done = lambda outcome, quiet=False: (got.append(outcome), m._gen_discard(h))
        with m._GEN_LOCK:
            m._GENERATIONS.add(h)
        self.addCleanup(m._gen_discard, h)
        t0 = time.monotonic()
        h._relay_inner(Resp())
        self.assertEqual(got[-1], "CLIENT TOO SLOW", got)
        self.assertLess(time.monotonic() - t0, 5, "a frozen client kept its relay")

    def test_a_hops_order_read_at_its_death_goes_before_the_death_behind_a_slow_client(self):
        # a hop: its lane's stop read at its engine's death, the order put before the death, written so behind a
        # client that read nothing meanwhile (decisions 1031 and 1061); no test drove a hop behind a slow client (the
        # review of v1.44.0)
        self._bindings_read_here(ordered=True)
        got = self._relayed_behind_a_frozen_client(300, hop="spark1", cut=False,
                                                   tail=(b"data: " + VLLM_DEAD_EVENT + b"\n\n",))
        self.assertEqual(got[-1], "502 upstream", got)
        said = b"".join(self.relayed)
        self.assertIn(b"x_alzeo_ended", said, "the other box was not told the order")
        self.assertLess(said.index(b"x_alzeo_ended"), said.index(b"EngineCore"), "the order came after the death")

    def test_a_hops_order_seen_at_its_streams_end_is_said_before_its_end(self):
        # a hop: no stop job at its engine's death, one at its stream's end: said before the end, behind a client that
        # read nothing meanwhile (decision 1061, _engine_end_late)
        ordered = self._bindings_read_here(ordered=False)
        m = self.mod
        saved = m.unit_stopping_by_order

        def read():
            ordered["reads"] += 1
            return ordered["reads"] >= 2        # the read at the death says none, the one at the end says one
        m.unit_stopping_by_order = read
        self.addCleanup(setattr, m, "unit_stopping_by_order", saved)
        got = self._relayed_behind_a_frozen_client(300, hop="spark1", cut=False, eof_delay=0.5,
                                                   tail=(b"data: " + VLLM_DEAD_EVENT + b"\n\n",))
        self.assertEqual(got[-1], "502 upstream", got)
        orders = [i for i, w in enumerate(self.relayed) if b"x_alzeo_ended" in w]
        self.assertTrue(orders, "the order seen at the stream's end was not said")
        self.assertLess(orders[0], self.finish_at[0], "the order was said after the stream's end")

    def test_a_generation_that_starts_while_a_stop_is_under_way_is_that_stops(self):
        # abort_all ends every generation SGLang runs, one that started after the stop's list was made included, and the
        # cockpit sends its own, straight to the engine, when this proxy's did not go: unmarked, SGLang ended an
        # Anthropic stream as end_turn, whole to its client (the 15th review of v1.43.0, 2026-11-05)
        _, sent = self._a_stop_made("fails")
        self.assertFalse(sent, "the test needs the proxy's abort_all not to go")
        self.assertEqual(self._a_generation_now()._stop_why, "this test's Abort all",
                         "a generation that started while the cockpit's own abort_all could still come was not marked")

    def test_a_generation_that_starts_after_a_stop_whose_abort_all_went_is_not_its(self):
        _, sent = self._a_stop_made("goes")
        self.assertTrue(sent, "the test needs the proxy's abort_all to go")
        self.assertIsNone(self._a_generation_now()._stop_why, "a generation after a stop that was over was stopped")

    def test_what_pair_json_may_say(self):
        colons = ":".join(ident("spark3")[1][i:i + 2] for i in range(0, 64, 2)).upper()
        ok = self.parse({"name": "Spark-1", "peers": [
            self.peer(), self.peer("b", "https://[fe80::1]:30002", who="rogue"),
            dict(self.peer("c", "HTTPS://spark2.local:30002/"), sha256=colons)]})
        self.assertEqual([p["proxy"] for p in ok["peers"]],
                         ["https://127.0.0.1:30002", "https://[fe80::1]:30002", "https://spark2.local:30002"])
        self.assertEqual(ok["peers"][2]["sha256"], ident("spark3")[1], "openssl's form, with colons, is taken")
        self.assertTrue(ok["peers"][0]["cert_file"].endswith("/.config/qwen38/pair/spark2.crt"))
        self.assertNotIn("key_file", ok["peers"][0], "the certificate is the pair's only credential")
        self.assertTrue(self.mod.PAIR_NAME.fullmatch(self.parse({"peers": []})["name"]), "the host's name, made safe")

    def test_what_pair_json_may_not_say(self):
        p = self.peer
        for conf in ([], {"peers": {}}, {"peers": [{"name": "x"}]}, {"peers": [p("two words")]},
                     {"peers": [p(proxy="http://127.0.0.1:30002")]}, {"peers": [p(proxy="https://127.0.0.1:3000l")]},
                     {"peers": [p(proxy="https://127.0.0.1")]}, {"peers": [p(proxy="https://127.0.0.1:0")]},
                     {"peers": [p(proxy="https://127.0.0.1:30002/v1")]}, {"peers": [p(proxy="https://u:p@127.0.0.1:30002")]},
                     {"peers": [p(proxy="https://127.0.0.1:30002?a")]},
                     {"peers": [dict(p(), cert_file="")]}, {"peers": [{k: v for k, v in p().items() if k != "sha256"}]},
                     {"peers": [dict(p(), sha256="xyz")]}, {"peers": [dict(p(), sha256=ident("spark2")[1][:63])]},
                     {"name": "", "peers": []}, {"name": "Spark \u2460", "peers": []}, {"name": "x" * 41, "peers": []},
                     {"peers": [p("a"), p("a", who="rogue")]}, {"name": "a", "peers": [p("a")]},
                     {"peers": [p("a"), p("b")]}):
            with self.subTest(conf=conf), self.assertRaises(ValueError):
                self.parse(conf)

    def test_pair_json_is_read_again_when_it_changes_and_a_bad_one_leaves_the_box_alone(self):
        self.pair_file.write_text(json.dumps({"name": "one", "peers": []}))
        self.assertEqual(self.mod.pair_conf()["name"], "one")
        self.assertEqual(self.mod.pair_conf()["name"], "one", "kept between two looks")
        self.pair_file.write_text("{not json")
        self.mod._PAIR["seen"] = 0.0
        self.assertIsNone(self.mod.pair_conf())
        self.assertTrue(any("is not used" in ln for ln in self.lines))
        self.pair_file.unlink()
        self.mod._PAIR["seen"] = 0.0
        self.assertIsNone(self.mod.pair_conf())

    def test_a_box_paired_again_under_a_name_inherits_nothing_of_the_sharing(self):
        # the conversations sent to a box, its pause, its tile's counts belong to that certificate: another box paired
        # under the same name got them for 10 min, each conversation computed again there (review, 2026-11-03)
        m = self.mod
        port = localports.held(self)
        def facts(name):
            return (name in m._AFFINITY, name in m._PAIR_COOL, name in m._PAIR_SPREAD)
        def note(name):
            m.affinity_note(name, [(4096, "fp-" + name)], time.monotonic())
            m._PAIR_COOL[name] = time.monotonic() + 30
            m.pair_spread_count(name, "balance")
        self.addCleanup(lambda: [d.clear() for d in (m._AFFINITY, m._PAIR_COOL, m._PAIR_SPREAD)])
        self.conf_now({"name": "spark1", "peers": [peer_entry(self.cfg, "spark2", port),
                                                   peer_entry(self.cfg, "spark3", port)]})
        note("spark2"); note("spark3")
        # spark2 paired again, another box under its name; spark3 the same box, read again
        self.conf_now({"name": "spark1", "peers": [peer_entry(self.cfg, "spark2", port, who="rogue"),
                                                   peer_entry(self.cfg, "spark3", port)]})
        self.assertEqual(facts("spark2"), (False, False, False), "another box got the old one's facts")
        self.assertEqual(facts("spark3"), (True, True, True), "the same box lost its facts")
        self.conf_now({"name": "spark1", "peers": []})          # and a box that left the pair
        self.assertEqual(facts("spark3"), (False, False, False))

    def test_the_tls_of_a_pair_is_checked_whole(self):
        good = peer_entry(self.cfg, "spark2", localports.held(self))
        conf = self.conf_now({"name": "spark1", "peers": [good]})
        self.assertEqual(conf["tls"]["pins"], {ident("spark2")[1]: "spark2"})
        self.assertEqual(conf["tls"]["mine"], ident("spark1")[1])
        self.assertTrue(conf["tls"]["server"].verify_flags & 0x200000, "dates not checked: the pin is the certificate")
        self.assertTrue(conf["peers"][0]["opener"] and conf["peers"][0]["list_opener"])
        self.assertIn("over TLS, this box's certificate sha256 " + ident("spark1")[1][:16], self.lines[-1])
        (self.cfg / "pair/two.crt").write_text(ident("spark2")[0] + ident("rogue")[0])
        (self.cfg / "pair/none.crt").write_text("not a certificate\n")
        for why, e in (("is not the certificate pair.json pins", dict(good, sha256=ident("rogue")[1])),
                       ("cannot be read", dict(good, cert_file="pair/missing.crt")),
                       ("its certificate is this box's own", peer_entry(self.cfg, "me", localports.held(self), who="spark1")),
                       ("holds 2 certificates, not one", dict(good, cert_file="pair/two.crt")),
                       ("holds 0 certificates, not one", dict(good, cert_file="pair/none.crt"))):
            with self.subTest(why=why):
                self.assertIsNone(self.conf_now({"name": "spark1", "peers": [e]}), "a pair it does not take: a box alone")
                self.assertIn(why, self.lines[-1])
        aside = self.tmp / "identity-aside"
        shutil.move(self.cfg / "pair/identity", aside)
        try:
            self.assertIsNone(self.conf_now({"name": "spark1", "peers": [good]}))
            self.assertIn("this box has no certificate of its own", self.lines[-1])
        finally:
            shutil.move(aside, self.cfg / "pair/identity")

    def test_why_a_peer_does_not_answer(self):
        why = self.mod._pair_why
        self.assertIn("another certificate than the one pinned", why(urllib.error.URLError(ssl.SSLCertVerificationError("x"))))
        self.assertIn("refuses this box's certificate",
                      why(urllib.error.URLError(ssl.SSLError(1, "[SSL: TLSV1_ALERT_UNKNOWN_CA] tlsv1 alert unknown ca"))))
        self.assertIn("nothing listens on its pair port", why(urllib.error.URLError(ConnectionRefusedError(111, "refused"))))
        self.assertEqual(why(socket.timeout("timed out")), "no answer in time")
        self.assertEqual(why(ValueError("its /v1/models is not a list of models")), "its /v1/models is not a list of models")
        self.assertEqual(why(ValueError()), "ValueError")
        self.assertEqual(why(ValueError("HTTP/1.1 200\r\n\x1b[2Jforged")), "HTTP/1.1 200???[2Jforged",
                         "what the other box wrote never holds a control character")

    def test_the_model_a_body_names(self):
        bm = self.mod.body_model
        self.assertEqual(bm(b'{"model": "kolibri-1"}'), "kolibri-1")
        for body in (b"not json", b"[1]", b'{"model": 3}', b"{}"):
            self.assertIsNone(bm(body))

    def test_each_peers_list_is_read_as_soon_as_the_pair_is(self):
        # the first GET /v1/models after a start or a pairing listed this box's models alone (a pair simulated on
        # the reference box, 2026-11-03)
        class One(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                out = json.dumps({"data": [{"id": "kolibri-1"}]}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
        self.mod._PAIR_MODELS.clear()
        self.conf_now({"name": "spark1", "peers": [peer_entry(self.cfg, "early", tls_peer(self, One), who="spark2")]})
        deadline = time.monotonic() + 8
        while (self.mod._PAIR_MODELS.get("early") or {}).get("models") != ("kolibri-1",) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual((self.mod._PAIR_MODELS.get("early") or {}).get("models"), ("kolibri-1",), "read with no request")

    def test_a_box_that_did_not_answer_is_read_again_in_the_background_once(self):
        # a box off without a reset took each read's whole timeout, and every request for a lane loaded nowhere
        # waited 5 s for it (review, 2026-11-03); the first fix of it handed the read back to the background from
        # the background, a thread after a thread: one read at a time, never waited for
        class Frozen(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                time.sleep(30)
        conf = self.mod._pair_parse(json.dumps({"name": "spark1", "peers": [
            peer_entry(self.cfg, "frozen", tls_peer(self, Frozen), who="spark2")]}))
        self.mod._pair_tls(conf)
        peer = conf["peers"][0]
        self.mod._PAIR_MODELS.pop("frozen", None)
        self.assertEqual(self.mod.peer_models(peer), ())          # its first read: its whole timeout, once
        self.assertTrue(self.mod._PAIR_MODELS["frozen"]["error"])
        self.mod._PAIR_MODELS["frozen"]["ts"] -= 60                # stale: read again, in the background
        t0 = time.monotonic()
        for _ in range(20):
            self.assertEqual(self.mod.peer_models(peer, fresh=True, since=time.monotonic()), ())
        self.assertLess(time.monotonic() - t0, 1.0, "no request waits for a box that did not answer")
        time.sleep(0.3)
        self.assertLessEqual(sum(t.name == "pair-list-frozen" for t in threading.enumerate()), 1,
                             "one read of it in the background at a time")

    def test_a_peers_list_is_read_kept_and_read_again(self):
        class Lists(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            answer, auths = (200, {"data": [{"id": "kolibri-1"}, {"id": 3}, "x", {"id": "forged\n[proxy] line"},
                                            {"id": "\x1b[31mred"}]}), []

            def log_message(self, *a):
                pass

            def do_GET(self):
                Lists.auths.append(self.headers.get("Authorization"))
                code, obj = Lists.answer
                out = json.dumps(obj).encode()
                self.send_response(code); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
        port = tls_peer(self, Lists)
        conf = self.mod._pair_parse(json.dumps({"name": "spark1", "peers": [
            peer_entry(self.cfg, "lists", port, who="spark2"),
            peer_entry(self.cfg, "gone", localports.held(self), who="rogue"),
            peer_entry(self.cfg, "othercert", port, who="spark3")]}))
        self.mod._pair_tls(conf)
        peer, gone, other = conf["peers"]
        pm = self.mod.peer_models
        self.assertEqual(pm(peer), ("kolibri-1",), "an id that is no printable word never kept: a journal line")
        self.assertEqual(pm(peer), ("kolibri-1",), "kept")
        self.assertEqual(Lists.auths, [None], "read once, with no key: the certificate is the pair's")
        t = time.monotonic()
        self.assertEqual(pm(peer, fresh=True, since=t - 60), ("kolibri-1",), "read after `since`: kept")
        self.assertEqual(len(Lists.auths), 1)
        Lists.answer = (200, [1, 2])
        self.assertEqual(pm(peer, fresh=True), ())
        self.assertIn("not a list", self.mod._PAIR_MODELS["lists"]["error"])
        Lists.answer = (503, {"error": "loading"})
        self.assertEqual(pm(peer, fresh=True), ())
        self.assertEqual(self.mod._PAIR_MODELS["lists"]["error"], "HTTP 503 on /v1/models")
        self.assertEqual(pm(gone), ())
        self.assertIn("nothing listens on its pair port", self.mod._PAIR_MODELS["gone"]["error"])
        self.assertEqual(pm(other), (), "the box at that address shows another certificate than the pinned one")
        self.assertIn("another certificate than the one pinned", self.mod._PAIR_MODELS["othercert"]["error"])

    def test_the_union_and_the_served_map(self):
        conf = self.conf_now({"name": "spark1", "peers": [peer_entry(self.cfg, "spark2", localports.held(self))]})
        self.mod._PAIR_MODELS["spark2"] = {"ts": time.monotonic(), "models": ("kolibri-1",), "error": None,
                                           "rows": [{"id": "kolibri-1", "object": "model"}, {"id": "qwen3.8-27b"}]}
        out = json.loads(self.mod.pair_models_body(b'{"object": "list", "data": [{"id": "qwen3.8-27b"}]}'))
        self.assertEqual([(r["id"], r["box"]) for r in out["data"]], [("qwen3.8-27b", "spark1"), ("kolibri-1", "spark2")])
        self.assertIsNone(self.mod.pair_models_body(b"not json"))
        self.assertIsNone(self.mod.pair_models_body(b'{"data": "x"}'))
        self.assertEqual(self.mod.pair_served_map(conf, None), {"spark1": "does not answer", "spark2": ["kolibri-1"]})
        self.mod._PAIR_MODELS["spark2"].update(models=(), error="timed out")
        self.assertEqual(self.mod.pair_served_map(conf, ("qwen3.8-27b",))["spark2"], "does not answer (timed out)")
        self.pair_file.unlink()
        self.mod._PAIR["seen"] = 0.0
        self.assertIsNone(self.mod.pair_models_body(b'{"data": []}'), "a box alone: its list as it came")

    def test_the_pair_port_opens_with_a_pair_and_closes_without_one(self):
        keeper = self.mod.PairPort()
        self.addCleanup(keeper.stop)
        keeper.check()
        self.assertIsNone(keeper.srv, "a box alone opens no port")
        self.conf_now({"name": "spark1", "peers": [peer_entry(self.cfg, "spark2", localports.held(self))]})
        keeper.check()
        self.assertIsNotNone(keeper.srv)
        opened = keeper.srv
        port = keeper.srv.server_address[1]
        self.assertIn(f"pair: port 127.0.0.1:{port} open over TLS, to the certificates of spark2 only", self.lines)
        keeper.check()
        self.assertEqual(keeper.srv.server_address[1], port, "opened once")
        self.pair_file.unlink()
        self.mod._PAIR["seen"] = 0.0
        keeper.check()
        self.assertIsNone(keeper.srv)
        self.assertIn(f"pair: port {port} closed: this box has no pair now", self.lines)
        self.assertEqual(opened.socket.fileno(), -1, "its socket closed (never a connection to a number now free)")

    def test_a_pair_port_that_cannot_be_opened_is_said_once(self):
        keeper = self.mod.PairPort()
        self.addCleanup(keeper.stop)
        self.conf_now({"name": "spark1", "peers": [peer_entry(self.cfg, "spark2", localports.held(self))]})
        saved, self.mod.PAIR_PORT = self.mod.PAIR_PORT, localports.held(self)     # bound by another: EADDRINUSE
        try:
            keeper.check()
            keeper.check()
        finally:
            self.mod.PAIR_PORT = saved
        self.assertIsNone(keeper.srv)
        self.assertEqual(sum("cannot be opened" in ln for ln in self.lines), 1, "said once, tried again every 2 s")
        keeper.check()
        self.assertIsNotNone(keeper.srv, "opened once it can be")

    def test_the_pair_port_admits_nobody_without_a_pair(self):
        srv = self.mod.PairServer(("127.0.0.1", 0), self.mod.H)
        self.addCleanup(srv.server_close)
        cli = socket.create_connection(srv.server_address, timeout=5)
        self.addCleanup(cli.close)
        with self.assertRaisesRegex(OSError, "admits nobody"):
            srv.get_request()
        self.assertEqual(cli.recv(1), b"", "closed at once, before any handshake")

    def test_a_pair_connection_is_the_networks_even_from_loopback(self):
        # the header clock of the network (CLIENT_HEADER_S), not loopback's CLIENT_IO_S: a slow box on a
        # tunnel's end holds a thread seconds, not 15 minutes
        self.conf_now({"name": "spark1", "peers": [peer_entry(self.cfg, "spark2", localports.held(self))]})
        srv = self.mod.PairServer(("127.0.0.1", 0), self.mod.H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        saved = self.mod.CLIENT_HEADER_S
        self.mod.CLIENT_HEADER_S = 1.0
        self.addCleanup(setattr, self.mod, "CLIENT_HEADER_S", saved)
        tls = client_ctx("spark2", "spark1").wrap_socket(socket.create_connection(srv.server_address, timeout=10))
        self.addCleanup(tls.close)
        tls.sendall(b"GET /v1/models HTTP/1.1\r\n")             # and nothing more
        t0 = time.monotonic()
        try:
            got = tls.recv(1)
        except (ConnectionResetError, ssl.SSLError):
            got = b""
        except socket.timeout:
            self.fail("the pair port waited past the header clock: it took a loopback address for this machine's")
        self.assertEqual(got, b"")
        self.assertLess(time.monotonic() - t0, 6)
        self.assertTrue(self.mod.PairServer.from_network and not self.mod.Server.from_network)

    def test_a_request_of_the_pair_is_admitted_by_this_boxs_own_key(self):
        # the other box holds no key of this one (decision 978): its certificate admits it on the pair port, and
        # this box's own key admits it to the engine, whatever key it sent (none, or another)
        class Fake:
            pass
        f = Fake()
        f._pair_box, f.headers, f.command, f.path = "spark2", {"Authorization": "Bearer not-this-box"}, "POST", "/v1/messages"
        saved = self.mod.UPSTREAM_API_KEY
        self.mod.UPSTREAM_API_KEY = "k-own-key"          # never this machine's real key file
        self.addCleanup(setattr, self.mod, "UPSTREAM_API_KEY", saved)
        self.assertEqual(self.mod._upstream_auth(f), "Bearer k-own-key")
        self.assertFalse(self.mod.H._door_shut(f, "/v1/messages"), "no key asked of the pair: its certificate")
        f._pair_box = None
        self.assertEqual(self.mod._upstream_auth(f), "Bearer not-this-box", "a client of the client port: its own")

    def test_a_summary_without_its_cockpit_still_says_what_this_box_serves(self):
        # the other box's cockpit stopped or never installed (--no-cockpit): the whole tile read "HTTP 503"
        # (review, 2026-11-03)
        saved = os.environ.get("QWEN38_COCKPIT_URL")
        os.environ["QWEN38_COCKPIT_URL"] = f"http://127.0.0.1:{localports.held(self)}"
        self.addCleanup(lambda: os.environ.pop("QWEN38_COCKPIT_URL") if saved is None
                        else os.environ.__setitem__("QWEN38_COCKPIT_URL", saved))
        served = self.mod.served_models
        self.mod.served_models = lambda: ("qwen3.8-27b",)
        self.addCleanup(setattr, self.mod, "served_models", served)
        s = self.mod.cockpit_summary()
        self.assertEqual(s["models"], ["qwen3.8-27b"])
        self.assertTrue(isinstance(s["cockpit"], str) and s["cockpit"], s)
        self.assertIsNone(s["job"])

    def test_what_a_browser_cannot_parse_is_not_taken_from_the_other_box(self):
        # NaN, Infinity and 1e400 parse in Python and not in a browser: written back into GET /v1/models or the tile,
        # they broke the answer for its client (review, 2026-11-03)
        for bad in ('{"data": [{"id": "x", "created": NaN}]}', '{"a": Infinity}', '{"a": -1e400}'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.mod._json_finite(bad)
        self.assertEqual(self.mod._json_finite('{"a": 1.5, "b": [1, 2e3]}'), {"a": 1.5, "b": [1, 2000.0]})

    def test_a_request_the_other_box_sent_on_is_never_held(self):
        # held through a whole boot, it got a 409 inside the held stream, relayed by its entry as an answer
        # (review, 2026-11-03): answered at once, the entry reads the lists again or says 503
        saved = (self.mod.engine_coming, self.mod.engine_answers)
        self.mod.engine_coming, self.mod.engine_answers = (lambda: True), (lambda: False)   # a lane on its way back
        self.addCleanup(lambda: (setattr(self.mod, "engine_coming", saved[0]), setattr(self.mod, "engine_answers", saved[1])))
        hop = types.SimpleNamespace(_pair_hop=lambda: "spark2", _hold_until=None)
        self.assertFalse(self.mod.H._hold(hop, "the engine does not answer", True))
        self.assertIsNone(hop._hold_until, "no hold begun")

    def test_a_connection_between_the_boxes_probes_a_box_that_went(self):
        # a box that loses power sends no FIN: a request in flight to it waited an hour (review, 2026-11-03)
        lsn = socket.socket()
        lsn.bind(("127.0.0.1", 0)); lsn.listen(1)
        self.addCleanup(lsn.close)
        c = socket.create_connection(lsn.getsockname())
        self.addCleanup(c.close)
        s, _ = lsn.accept()
        self.addCleanup(s.close)
        self.mod._pair_keepalive(c)
        got = (c.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE), c.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE),
               c.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL), c.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT),
               c.getsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT))
        # no TCP_USER_TIMEOUT: it also cut a live stream whose client stopped reading for 25 s (review, 2026-11-03)
        self.assertEqual(got, (1, 10, 5, 3, 0))
        # both ends of a pair: the connection a box opens to its peer, and the one its pair port accepts
        src = (REPO / "keepalive-proxy.py").read_text()
        self.assertIn("        super().connect()\n        _pair_keepalive(self.sock)\n", src)
        self.assertIn("        _pair_keepalive(conn)", src)

    def test_a_line_said_once_a_minute_counts_the_others(self):
        lines = []
        saved = self.mod.log
        self.mod.log = lines.append
        self.addCleanup(setattr, self.mod, "log", saved)
        say = self.mod._say_once_a_minute
        say("test-kind", "first")
        say("test-kind", "second")
        say("test-kind", "third")
        self.assertEqual(lines, ["first"])
        t, quiet = self.mod._SAID["test-kind"]
        self.mod._SAID["test-kind"] = (t - 61, quiet)
        say("test-kind", "fourth")
        self.assertEqual(lines, ["first", "fourth (2 more such since the last line)"])

    def test_the_summary_of_this_box_logs_in_once_and_again_after_a_restart(self):
        srv = Engine(("127.0.0.1", 0), FakeCockpit)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        saved = (self.mod.UPSTREAM_API_KEY, self.mod.served_models, self.mod.HOLD_UNITS)
        url = os.environ.get("QWEN38_COCKPIT_URL")
        def back():
            self.mod.UPSTREAM_API_KEY, self.mod.served_models, self.mod.HOLD_UNITS = saved
            os.environ.pop("QWEN38_COCKPIT_URL", None)
            if url is not None:
                os.environ["QWEN38_COCKPIT_URL"] = url
            self.mod._COCKPIT["cookie"] = None
        self.addCleanup(back)
        self.mod.UPSTREAM_API_KEY = KEY_1                # never this machine's real key file
        self.mod.served_models = lambda: ("qwen3.8-27b",)  # never this machine's engine
        self.mod.HOLD_UNITS = ("qwen38-flash-local.service", "qwen38-sglang.service")
        self.mod._COCKPIT["cookie"] = None
        os.environ["QWEN38_COCKPIT_URL"] = f"http://127.0.0.1:{srv.server_address[1]}"
        FakeCockpit.logins.clear()
        first = self.mod.cockpit_summary()
        self.assertEqual((first["models"], first["lanes"]), (["qwen3.8-27b"], {"qwen38-flash-local.service": "active"}))
        self.mod.cockpit_summary()
        self.assertEqual(FakeCockpit.logins, [KEY_1], "one login, its session kept")
        FakeCockpit.sessions.clear()                     # the cockpit restarted: its sessions went with it
        self.mod.cockpit_summary()
        self.assertEqual(FakeCockpit.logins, [KEY_1, KEY_1], "logged in again, once")
        os.environ["QWEN38_COCKPIT_URL"] = f"http://127.0.0.1:{localports.held(self)}"
        self.mod._COCKPIT["cookie"] = None
        gone = self.mod.cockpit_summary()                # its cockpit gone: what this box serves, and why no more
        self.assertEqual(gone["models"], ["qwen3.8-27b"])
        self.assertTrue(gone["cockpit"], gone)
        self.assertEqual(len(FakeCockpit.logins), 2)

    def test_the_cockpit_is_found_where_its_unit_binds_it(self):
        unit = self.tmp / "dashboard.service"
        saved, url = self.mod.COCKPIT_UNIT_FILE, os.environ.pop("QWEN38_COCKPIT_URL", None)
        def back():
            self.mod.COCKPIT_UNIT_FILE = saved
            if url is not None:
                os.environ["QWEN38_COCKPIT_URL"] = url
        self.addCleanup(back)
        self.mod.COCKPIT_UNIT_FILE = str(unit)
        for text, base in (("", "http://127.0.0.1:30090"),
                           ("Environment=COCKPIT_BIND=0.0.0.0\nEnvironment=COCKPIT_PORT=30091\n", "http://127.0.0.1:30091"),
                           ("Environment=COCKPIT_BIND=192.0.2.10\n", "http://192.0.2.10:30090"),   # RFC 5737
                           ('Environment="COCKPIT_BIND=fd7a::1"\n', "http://[fd7a::1]:30090")):
            with self.subTest(unit=text):
                unit.write_text(text)
                self.assertEqual(self.mod._cockpit_base(), base)

    def test_a_connection_refused_in_setup_reads_nothing_and_a_box_taken_out_is_refused(self):
        class Fake:
            pass
        refused = Fake()
        refused._refused, refused.close_connection = True, False
        self.mod.H.handle_one_request(refused)            # it has no rfile: a read would raise here
        self.assertTrue(refused.close_connection)
        self.conf_now({"name": "spark1", "peers": [peer_entry(self.cfg, "spark3", localports.held(self), who="rogue")]})
        out = Fake()
        out._refused, out.close_connection = False, False
        out._pair_box, out._pair_pin, out.client_address = "spark2", ident("spark2")[1], ("127.0.0.1", 5)
        self.mod.H.handle_one_request(out)
        self.assertTrue(out.close_connection)
        self.assertIn("pair port: spark2 (127.0.0.1) REFUSED: no peer of this box's pair now", self.lines)
        self.assertEqual(self.mod._pair_box_of(ident("rogue")[1]), "spark3")
        self.assertIsNone(self.mod._pair_box_of(ident("spark2")[1]))
        self.assertIsNone(self.mod._pair_box_of(None))


    # Role 10, a model both boxes serve (decisions 996 to 998): the pieces of the decision

    def conversation(self, turns, max_tokens=8, stream=False):
        msgs = [{"role": "system", "content": "a system prompt " * 600}]
        for i in range(turns):
            msgs += [{"role": "user", "content": f"turn {i} " + "y" * 3000}, {"role": "assistant", "content": "ok"}]
        return json.dumps({"model": "qwen3.8-27b", "max_tokens": max_tokens, "stream": stream,
                           "messages": msgs[:-1]}).encode()

    def test_the_blocks_of_a_turn_start_the_blocks_of_the_next(self):
        m, route = self.mod, "/v1/chat/completions"
        one, size1 = m.prefix_blocks(self.conversation(1), route)
        two, size2 = m.prefix_blocks(self.conversation(3), route)
        self.assertEqual([n for n, _ in one], [4096, 8192])
        self.assertEqual([n for n, _ in two], [4096, 8192, 16384])
        self.assertLess(size1, 16384)
        self.assertEqual(two[:2], one, "the next turn does not start with the blocks of this one")
        # what a client changes between two turns, as role 9 reads it, changes no block
        self.assertEqual(m.prefix_blocks(self.conversation(3, max_tokens=32000, stream=True), route)[0], two)
        # each block is the fingerprint of the body's first bytes so read, the route first: one pass, no copy
        body = self.conversation(3)
        canon = m.RESEND_SKIP_RE.sub(lambda x: x.group(1), body)
        self.assertEqual(size2, len(canon))
        for n, fp in two:
            self.assertEqual(fp, hashlib.sha256(route.encode() + canon[:n]).hexdigest()[:16])
        self.assertEqual(m.prefix_blocks(b'{"model": "qwen3.8-27b"}', route), ([], 0), "a short body has no block")
        self.assertNotEqual(m.prefix_blocks(body, "/v1/completions")[0], two, "another route, other blocks")

    def test_a_box_says_what_it_runs_as_a_count_or_not_at_all(self):
        run = self.mod.pair_running
        said = lambda total, yours: {"x_alzeo_running": total, "x_alzeo_running_yours": yours}
        self.assertEqual(run(said(3, 0)), 3)
        self.assertEqual(run(said(0, 0)), 0)
        self.assertEqual(run(said(3, 1)), 2, "its own clients' generations, this box's apart")
        self.assertIsNone(run(said(3, 4)), "more of this box's than it runs")
        self.assertIsNone(run({"x_alzeo_running": 3}), "a list that says not how many are this box's")
        for bad in (True, -1, 3.0, "3", None, [], {"n": 1}, 10 ** 9):
            self.assertIsNone(run(said(bad, 0)), bad)
            self.assertIsNone(run(said(3, bad)), bad)
        self.assertIsNone(run({}))
        self.assertIsNone(run(["x_alzeo_running"]))

    def test_a_box_says_its_slots_as_a_count_or_not_at_all(self):
        slots = self.mod.pair_slots
        self.assertEqual(slots({"x_alzeo_slots": 16}), 16)
        self.assertEqual(slots({"x_alzeo_slots": 1}), 1)
        for bad in (0, -1, True, False, 8.0, "8", None, [], {"n": 8}, self.mod.PAIR_SLOTS_MAX + 1):
            self.assertIsNone(slots({"x_alzeo_slots": bad}), bad)
        self.assertIsNone(slots({}))
        self.assertIsNone(slots(["x_alzeo_slots"]))

    def test_the_gap_a_box_may_run_ahead_is_half_its_lanes_slots(self):
        # decision 1014: the 64 of SGLang's router brought to each lane
        gap = self.mod.pair_spread_gap
        for slots, want in ((8, 4), (16, 8), (24, 12), (4, 2), (5, 2), (3, 1), (2, 1), (1, 1)):
            self.assertEqual(gap(slots), want, slots)
        for unknown in (None, 0, -8, True, 8.0, "16", self.mod.PAIR_SLOTS_MAX + 1):
            self.assertEqual(gap(unknown), 4, f"{unknown!r}: the 27B's 8, as before v1.43.0")

    def test_the_slots_of_a_lane_are_read_from_its_engines_command(self):
        slots_in = self.mod.slots_in
        self.assertEqual(slots_in(["argv[]=docker run x --max-running-requests 8 --mem-fraction-static 0.76 ;"]), 8)
        self.assertEqual(slots_in(["vllm serve m --max-num-seqs=16 --port 30000"]), 16)
        self.assertEqual(slots_in(["/home/u/launch.sh", "exec vllm serve m \\\n  --max-num-seqs 24 \\\n"]), 24)
        self.assertEqual(slots_in(["--max-num-seqs 8", "x --max-num-seqs=8"]), 8, "said twice alike")
        self.assertIsNone(slots_in(["--max-num-seqs 8", "--max-num-seqs 16"]), "two values: not known")
        self.assertIsNone(slots_in(["--max-running-requests ${N}", "--max-num-seqs-per-step 9", "--max-num-seqs 8.5"]))
        self.assertIsNone(slots_in(["xx--max-num-seqs 8"]), "the end of another flag")
        self.assertIsNone(slots_in([""]))
        self.assertIsNone(slots_in([]))

    def test_this_box_reads_its_slots_from_its_active_unit_and_the_script_it_runs(self):
        m = self.mod
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        script = tmp / "launch-lane.sh"
        script.write_text("#!/bin/bash\n# --max-num-seqs 99, a comment, counts not\n"
                          "exec vllm serve m --served-model-name m-test --max-num-seqs 16\n")
        # the active unit's main process, as it was started: a script not yet at its exec
        engine = sleeper(self, str(script))
        shown = ("ActiveState=inactive\nMainPID=0\n\n"
                 f"MainPID={engine.pid}\nActiveState=active\n")
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, shown, "")
        saved = (m.HOLD_UNITS, m.subprocess)
        m.HOLD_UNITS = ("qwen38-a.service", "qwen38-b.service")
        m.subprocess = types.SimpleNamespace(run=run)
        m._UNIT_FACTS.update(ts=-1e9, texts=None)

        def restore():
            m.HOLD_UNITS, m.subprocess = saved
            m._UNIT_FACTS.update(ts=-1e9, texts=None)
        self.addCleanup(restore)
        self.assertEqual(m.unit_slots(), 16, "the active unit's script, its comment left out")
        self.assertEqual(m.unit_slots(), 16)
        self.assertEqual(len(calls), 1, "read again 10 s on at most")
        self.assertEqual(calls[0][:2], ["systemctl", "show"])
        m.HOLD_UNITS = ()
        m._UNIT_FACTS.update(ts=-1e9, texts=None)
        self.assertIsNone(m.unit_slots(), "no unit named: a proxy run by hand or by a test")

    def test_this_box_says_the_checkpoint_its_engine_runs_not_the_one_its_unit_names_next(self):
        # switch-model.sh rewrites the unit and reloads systemd without a restart: the box said the new checkpoint while
        # its engine served the old one until its next start (review, 2026-11-04)
        m = self.mod
        engine = sleeper(self, "--served-model-name", "qwen3.8-27b", "--revision", "ab" * 20,
                         "--max-running-requests", "8")
        shown = (f"ActiveState=active\nMainPID={engine.pid}\nExecStart={{ path=/usr/bin/docker ; argv[]=docker run x "
                 f"--served-model-name qwen3.8-27b --revision {'cd' * 20} --max-running-requests 8 ; }}\n")

        def run(argv, **kw):
            return subprocess.CompletedProcess(argv, 0, shown, "")
        saved = (m.HOLD_UNITS, m.subprocess, m.PAIR_CHECKPOINT)
        m.HOLD_UNITS = ("qwen38-sglang.service",)
        m.subprocess = types.SimpleNamespace(run=run)
        m.PAIR_CHECKPOINT = None                                                # a box whose units say it
        m._UNIT_FACTS.update(ts=-1e9, texts=None)

        def restore():
            m.HOLD_UNITS, m.subprocess, m.PAIR_CHECKPOINT = saved
            m._UNIT_FACTS.update(ts=-1e9, texts=None)
        self.addCleanup(restore)
        self.assertEqual(m.unit_revision(), "ab" * 20, "the unit's next start, not what its engine serves")
        self.assertEqual(m.lane_away(), (frozenset({"qwen3.8-27b"}), "ab" * 20))
        engine.kill()
        engine.wait()
        m._UNIT_FACTS.update(ts=-1e9, texts=None)
        # its process gone: unsure, read again a second on (never kept 10 s), and no checkpoint: what its unit starts is
        # its next start, which an update may have changed (the 4th review of v1.43.0, 2026-11-04)
        self.assertIsNone(m.unit_revision())
        self.assertLess(m._UNIT_FACTS["ts"], time.monotonic() - 8)

    def _systemd_says(self, shown):
        m = self.mod
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, shown["out"], "")
        saved = (m.HOLD_UNITS, m.subprocess, m.PAIR_CHECKPOINT, m._LAST_LANE["lane"])
        m.HOLD_UNITS, m.subprocess, m.PAIR_CHECKPOINT = ("qwen38-sglang.service",), types.SimpleNamespace(run=run), None
        self._forget()

        def restore():
            m.HOLD_UNITS, m.subprocess, m.PAIR_CHECKPOINT = saved[:3]
            m._LAST_LANE["lane"] = saved[3]
            m._UNIT_FACTS.update(states=())
            self._forget()
        self.addCleanup(restore)
        return calls

    def _forget(self):
        """What systemd said, forgotten: read again at the next ask (lane_away keeps it a second, the 4th review)."""
        m = self.mod
        getattr(m, "forget_unit_reads", lambda: None)()
        m._UNIT_FACTS.update(ts=-1e9, texts=None)
        m._HOLD_SEEN["ts"] = -1e9

    def test_an_engine_whose_command_reads_empty_is_read_again_not_kept(self):
        # its process between fork and exec, or a zombie: an empty command was kept 10 s, the box saying no checkpoint
        # and no slots for that long after a start (the delta's review, 2026-11-04)
        m = self.mod
        p = subprocess.Popen(["true"])                                          # a zombie: its command line is empty
        self.addCleanup(p.wait)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with open(f"/proc/{p.pid}/stat") as f:
                if f.read().split(")")[-1].split()[0] == "Z":
                    break
            time.sleep(0.01)
        calls = self._systemd_says({"out": f"ActiveState=active\nMainPID={p.pid}\n"})
        self.assertIsNone(m.unit_texts())
        time.sleep(1.1)                                                         # an unsure read is kept a second
        self.assertIsNone(m.unit_texts())
        self.assertEqual(len(calls), 2, "an empty command was kept 10 s")

    def _systemd_answers(self, jobs, shown, starts, run=None, changed=None, left=None):
        """systemctl stubbed: list-jobs says jobs, show -p ExecStart --value says starts, show -p InvocationID says run
        (the unit's run, decision 1049), changed (its last state change, unix time; now by default) and left (when it
        left active, unix time; not said by default), any other show says shown."""
        m = self.mod

        def answer(argv, **kw):
            if argv[1] == "list-jobs":
                out = jobs["out"]
            elif "--value" in argv and "ExecStart" in argv:
                out = starts["out"]
            elif "InvocationID" in argv:
                at = (changed or {}).get("at", time.time())
                out = f"InvocationID={(run or {'out': ''})['out'].strip()}\nStateChangeTimestamp=@{int(at)}\n"
                out += f"ActiveExitTimestamp=@{int(left['at'])}\n" if left else "ActiveExitTimestamp=\n"
            else:
                out = shown["out"]
            return subprocess.CompletedProcess(argv, 0, out, "")
        saved = (m.HOLD_UNITS, m.subprocess, m.PAIR_CHECKPOINT, m._LAST_LANE["lane"])
        m.HOLD_UNITS, m.subprocess = ("qwen38-sglang.service",), types.SimpleNamespace(run=answer)
        m.PAIR_CHECKPOINT = None
        self._forget()

        def restore():
            m.HOLD_UNITS, m.subprocess, m.PAIR_CHECKPOINT = saved[:3]
            m._LAST_LANE["lane"] = saved[3]
            m._UNIT_FACTS.update(states=())
            self._forget()
        self.addCleanup(restore)

    def test_a_lane_stopped_by_order_is_no_lane_here_and_a_restart_brings_back_what_its_unit_starts(self):
        # the delta's 3rd review: a unit stopping by order (a Load of another lane) kept the lane seen last, and its
        # model's requests were held through the next lane's whole boot, then refused
        m = self.mod
        jobs = {"out": "7 qwen38-sglang.service stop running\n"}
        shown = {"out": "ActiveState=deactivating\nMainPID=0\n"}
        starts = {"out": f"{{ path=/usr/bin/docker ; argv[]=docker run x --served-model-name qwen3.8-27b --revision "
                         f"{'ab' * 20} ; }}"}
        self._systemd_answers(jobs, shown, starts)
        m._LAST_LANE["lane"] = (frozenset({"qwen3.8-27b"}), "cd" * 20)
        self.assertEqual(m.lane_away(), (frozenset(), None), "a lane stopped by order kept its model as this box's")
        jobs["out"] = "8 qwen38-sglang.service restart running\n"            # a restart: what it brings back
        self._forget()                                                          # lane_away keeps its read a second
        self.assertEqual(m.lane_away(), (frozenset({"qwen3.8-27b"}), "ab" * 20))

    def test_a_restart_to_another_checkpoint_is_an_order_and_to_the_same_one_is_not(self):
        # decision 1041: the cockpit's Load of another checkpoint of the same lane is a restart; a recovery or an update
        # restarts on the same one (decision 1030)
        m = self.mod
        jobs = {"out": "8 qwen38-sglang.service restart running\n"}
        starts = {"out": f"{{ path=/usr/bin/docker ; argv[]=docker run x --served-model-name qwen3.8-27b --revision "
                         f"{'ab' * 20} ; }}"}
        self._systemd_answers(jobs, {"out": ""}, starts)
        m._LAST_LANE["lane"] = (frozenset({"qwen3.8-27b"}), "cd" * 20)       # it served another checkpoint
        self.assertTrue(m.unit_stopping_by_order(), "the Load of another checkpoint bound what it ended")
        m._LAST_LANE["lane"] = (frozenset({"qwen3.8-27b"}), "ab" * 20)       # the same one: a recovery
        self.assertFalse(m.unit_stopping_by_order())
        jobs["out"] = "9 qwen38-sglang.service stop running\n"
        self.assertTrue(m.unit_stopping_by_order())
        jobs["out"] = ""
        self.assertFalse(m.unit_stopping_by_order())

    def test_a_restart_the_cockpit_makes_to_recover_its_engine_is_no_order_whatever_it_loads(self):
        # decision 1049: the cockpit marks its restarts of a wedged or zombie engine with the run they restart. A restart
        # to another checkpoint than the one served is an order (1041) without that mark, and a mark of another run (a
        # Load since) is none (the 4th review of v1.43.0, 2026-11-04)
        m = self.mod
        jobs = {"out": "8 qwen38-sglang.service restart running\n"}
        starts = {"out": f"{{ path=/usr/bin/docker ; argv[]=docker run x --served-model-name qwen3.8-27b --revision "
                         f"{'ab' * 20} ; }}"}
        run = {"out": "run-1\n"}
        self._systemd_answers(jobs, {"out": ""}, starts, run)
        m._LAST_LANE["lane"] = (frozenset({"qwen3.8-27b"}), "cd" * 20)       # it served another checkpoint
        marks = pathlib.Path(tempfile.mkdtemp(prefix="kp-recovery-")) / "recovery-restarts.json"
        self.addCleanup(shutil.rmtree, marks.parent, True)
        saved = getattr(m, "RECOVERY_FILE", None)
        m.RECOVERY_FILE = str(marks)
        self.addCleanup(setattr, m, "RECOVERY_FILE", saved)
        self.assertTrue(m.unit_stopping_by_order(), "no mark: the Load of another checkpoint, an order (1041)")
        marks.write_text(json.dumps({"qwen38-sglang.service": {"invocation": "run-1", "at": time.time(),
                                                                "why": "autoheal"}}))
        self.assertFalse(m.unit_stopping_by_order(), "the cockpit's recovery was taken for an order")
        run["out"] = "run-2\n"                                                  # a later run: the mark is another's
        self.assertTrue(m.unit_stopping_by_order())
        marks.write_text("[1, 2]")                                              # a mark that cannot be read
        run["out"] = "run-1\n"
        self.assertTrue(m.unit_stopping_by_order())

    def _marked(self, at, run="run-1"):
        """RECOVERY_FILE a file of this test's, saying the cockpit recovers the 27B's run `run`, marked at `at`."""
        m = self.mod
        marks = pathlib.Path(tempfile.mkdtemp(prefix="kp-recovery-")) / "recovery-restarts.json"
        self.addCleanup(shutil.rmtree, marks.parent, True)
        saved = m.RECOVERY_FILE
        m.RECOVERY_FILE = str(marks)
        self.addCleanup(setattr, m, "RECOVERY_FILE", saved)
        marks.write_text(json.dumps({"qwen38-sglang.service": {"invocation": run, "at": at, "why": "autoheal"}}))
        return marks

    def test_a_recovery_mark_left_behind_is_no_recovery(self):
        # a mark the cockpit left on a run whose restart was refused made a later Load of that run a recovery, which
        # binds what it ends (the 5th review of v1.43.0, 2026-11-04): no mark older than RECOVERY_MARK_S counts
        m = self.mod
        jobs = {"out": "8 qwen38-sglang.service restart running\n"}
        starts = {"out": f"{{ path=/usr/bin/docker ; argv[]=docker run x --served-model-name qwen3.8-27b --revision "
                         f"{'ab' * 20} ; }}"}
        self._systemd_answers(jobs, {"out": ""}, starts, {"out": "run-1\n"})
        m._LAST_LANE["lane"] = (frozenset({"qwen3.8-27b"}), "cd" * 20)
        self._marked(time.time() - 700)
        self.assertTrue(m.unit_stopping_by_order(), "a stale mark made the Load of another checkpoint a recovery")

    def test_the_run_is_read_before_the_jobs(self):
        # read after the jobs, the run could be the restart's next one, its stop half over: the cockpit's recovery then
        # read as an order, and the request that wedged the engine went unbound (the 5th review of v1.43.0, 2026-11-04)
        m = self.mod
        asked = []

        def answer(argv, **kw):
            asked.append(argv[1] if argv[1] == "list-jobs" else "show " + argv[-2])
            if argv[1] == "list-jobs":
                out = "8 qwen38-sglang.service restart running\n"
            elif "ExecStart" in argv:
                out = f"{{ path=/usr/bin/docker ; argv[]=docker run x --served-model-name qwen3.8-27b --revision {'ab' * 20} ; }}"
            elif "InvocationID" in argv:                                        # its next run, once the jobs are read
                out = (f"InvocationID={'run-2' if 'list-jobs' in asked else 'run-1'}\n"
                       f"StateChangeTimestamp=@{int(time.time())}\n")
            else:
                out = ""
            return subprocess.CompletedProcess(argv, 0, out, "")
        self._systemd_answers({"out": ""}, {"out": ""}, {"out": ""})
        m.subprocess = types.SimpleNamespace(run=answer)
        m._LAST_LANE["lane"] = (frozenset({"qwen3.8-27b"}), "cd" * 20)
        self._marked(time.time())
        self.assertFalse(m.unit_stopping_by_order(), f"the cockpit's recovery read as an order ({asked})")

    def test_a_stop_is_an_order_whatever_the_marks(self):
        # decision 1059: the operator's stop was marked as a recovery (1055), and the Load journey's own stop of a stuck
        # engine with it, its requests bound 10 minutes and refused (the 6th review of v1.43.0, 2026-11-04): withdrawn
        m = self.mod
        jobs = {"out": "9 qwen38-sglang.service stop running\n"}
        self._systemd_answers(jobs, {"out": "ActiveState=deactivating\nMainPID=0\n"}, {"out": ""}, {"out": "run-1\n"})
        m._LAST_LANE["lane"] = (frozenset({"qwen3.8-27b"}), "cd" * 20)
        self._marked(time.time())
        self.assertTrue(m.unit_stopping_by_order(), "a marked stop read as a recovery")
        self.assertEqual(m.lane_away(), (frozenset(), None))

    def test_a_refusal_for_another_checkpoint_keeps_the_name(self):
        # the other box's 409 for another checkpoint of the model dropped the name from its row like a 409 for another
        # model, and peer_may_serve then said no box served it (the 6th review of v1.43.0, 2026-11-04)
        m = self.mod
        same = json.dumps({"error": {"type": "model_not_served", "message": "x",
                                     "served": {"spark2": ["qwen3.8-27b"]}}}).encode()
        other = json.dumps({"type": "error", "error": {"type": "model_not_served", "message": "x",
                                                       "served": {"spark2": ["qwen3.8-flash-next-fp4"]}}}).encode()
        self.assertTrue(m.refusal_serves(same, "qwen3.8-27b"))
        self.assertFalse(m.refusal_serves(other, "qwen3.8-27b"))
        self.assertFalse(m.refusal_serves(b"not json", "qwen3.8-27b"))
        h = object.__new__(m.H)
        h._peer = "test"
        with m._PAIR_LOCK:
            saved = dict(m._PAIR_MODELS)
        self.addCleanup(lambda: (m._PAIR_MODELS.clear(), m._PAIR_MODELS.update(saved)))
        # its revision dropped too, which the 409 proves stale: kept, the next requests went there for a 409 each (the
        # 7th review of v1.43.0, 2026-11-04)
        for res, models, revision in (("moved checkpoint", ("qwen3.8-27b", "marker"), None),
                                      ("moved", ("marker",), "ab" * 20)):
            with self.subTest(res=res):
                m._PAIR_MODELS["spark2"] = {"models": ("qwen3.8-27b", "marker"), "ts": 5.0, "revision": "ab" * 20}
                h._pair_refused("spark2", "qwen3.8-27b", res, "read again")
                row = m._PAIR_MODELS["spark2"]
                self.assertEqual((row["models"], row["revision"], row["ts"]), (models, revision, 0.0))

    def test_a_recovery_mark_counts_for_the_restart_it_was_written_for(self):
        # a mark the cockpit left behind (stopped between the mark and the restart) counted for the next restart of that
        # run within 10 minutes, the Load of another checkpoint then binding what it ended (the 7th review of v1.43.0,
        # 2026-11-04): the unit's state must change after the mark, within RECOVERY_START_S
        m = self.mod
        jobs = {"out": "8 qwen38-sglang.service restart running\n"}
        starts = {"out": f"{{ path=/usr/bin/docker ; argv[]=docker run x --served-model-name qwen3.8-27b --revision "
                         f"{'ab' * 20} ; }}"}
        changed = {"at": time.time()}
        self._systemd_answers(jobs, {"out": ""}, starts, {"out": "run-1\n"}, changed)
        m._LAST_LANE["lane"] = (frozenset({"qwen3.8-27b"}), "cd" * 20)
        self._marked(time.time() - 300)                                         # fresh, written for another restart
        self.assertTrue(m.unit_stopping_by_order(), "a mark left behind counted for a later restart")
        self._marked(time.time() - 1)                                           # written for this one
        self.assertFalse(m.unit_stopping_by_order())

    def test_a_recovery_mark_outlives_a_stop_slower_than_its_window(self):
        # systemd stamps every step of a stop (its unit_notify): read alone, the last state change moved past
        # RECOVERY_START_S while a slow stop went on, and the cockpit's recovery then read as an order. The moment the
        # unit left active is set once (the 8th review of v1.43.0, 2026-11-05)
        m = self.mod
        jobs = {"out": "8 qwen38-sglang.service restart running\n"}
        starts = {"out": f"{{ path=/usr/bin/docker ; argv[]=docker run x --served-model-name qwen3.8-27b --revision "
                         f"{'ab' * 20} ; }}"}
        now = time.time()
        self._systemd_answers(jobs, {"out": ""}, starts, {"out": "run-1\n"}, {"at": now}, {"at": now - 199})
        m._LAST_LANE["lane"] = (frozenset({"qwen3.8-27b"}), "cd" * 20)
        self._marked(now - 200)                                                 # its stop began a second after it
        self.assertFalse(m.unit_stopping_by_order(), "a slow stop outlived the recovery's mark")
        self.assertEqual(m.unix_stamp("@1730757600"), 1730757600.0)
        self.assertIsNone(m.unix_stamp(""))
        self.assertIsNone(m.unix_stamp("n/a"))

    def test_undoing_a_requests_binding_gives_back_the_one_before(self):
        # an order that ended a retry erased the binding an earlier real death of the same body had made, and the next
        # retry could take this box's engine down too (decision 1019; the 8th review of v1.43.0, 2026-11-05)
        m = self.mod
        self.addCleanup(m._KILLERS.clear)

        def request(fp):
            h = object.__new__(m.H)
            h._killer_fp, h._killer_bound, h._killer_before = fp, False, None
            return h
        m.killer_note("f" * 16, "spark2")                                       # an earlier death of that body there
        h = request("f" * 16)
        h._bind_killer("spark2")                                                # this request's own, at its death
        h._bind_killer("spark2")                                                # and again at its stream's end
        h._unbind_killer("spark2")                                              # the order said after it
        self.assertEqual(m.killer_box("f" * 16), "spark2", "an order erased the binding of an earlier death")
        h = request("e" * 16)
        h._bind_killer("")
        h._unbind_killer("")
        self.assertIsNone(m.killer_box("e" * 16), "nothing was bound before it: nothing is left")
        m.killer_note("d" * 16, "spark2")                                       # bound to the other box before
        h = request("d" * 16)
        h._bind_killer("")                                                      # this box's engine died on it since
        h._unbind_killer("")
        self.assertEqual(m.killer_box("d" * 16), "spark2")

    def test_an_answer_relayed_whole_leaves_the_stops_reach_at_once(self):
        # set at its relay, the keeper's join left a window where a stop cut a finished answer, and on vLLM shut its
        # upstream mid-body (the 7th review of v1.43.0, 2026-11-04): set as the engine gives it, under the stop's lock
        m = self.mod

        class Answer:
            def __init__(self, ctype):
                self.headers = {"Content-Type": ctype}

        def taken(ctype, stopped=None):
            h = object.__new__(m.H)
            h._stop_why = stopped
            h._take_whole_answer(Answer(ctype))
            return getattr(h, "_relaying_whole", False)
        self.assertTrue(taken("application/json"))
        self.assertFalse(taken("text/event-stream"))
        self.assertFalse(taken("application/x-ndjson"))
        self.assertFalse(taken("application/json", "a floor stop"), "a stopped answer was taken out of its reach")

    def test_a_death_in_ndjson_is_a_whole_lines_top_level_error(self):
        # matched anywhere in the bytes, an Ollama tool call's arguments, a raw object, could carry the words (the 7th
        # review of v1.43.0, 2026-11-04)
        m = self.mod
        dead = b'{"error": {"message": "EngineCore encountered an issue. See stack trace (above) for the root cause."}}\n'
        args = (b'{"message": {"tool_calls": [{"function": {"arguments": {"error": {"message": "EngineCore '
                b'encountered an issue."}}}}]}, "done": false}\n')
        self.assertTrue(m.ndjson_death(b'{"done": false}\n' + dead))
        self.assertFalse(m.ndjson_death(args))
        self.assertFalse(m.ndjson_death(b'"error" EngineCore encountered an issue.\n'))

    def test_an_order_split_across_two_reads_is_read_whole(self):
        # its first half in the last read, its second in the next: the wait for an order never matched it, and the request
        # stayed bound (the 7th review of v1.43.0, 2026-11-04)
        m = self.mod

        class Stream:
            def __init__(self, parts):
                self.parts = list(parts)

            def read1(self, n):
                return self.parts.pop(0) if self.parts else b""
        h = object.__new__(m.H)
        h._peer = "test"
        half = len(HOP_ENDED_OPENAI) // 2
        self.assertEqual(h._end_after(Stream([HOP_ENDED_OPENAI[half:]]), None, HOP_ENDED_OPENAI[:half]), (False, True))
        self.assertEqual(h._end_after(Stream([b'data: {"choices": []}\n\n']), None, b""), (False, False))

    def test_a_death_split_where_its_client_leaves_is_read_whole(self):
        # read after its client left (decision 1112), a death whose first half came in the last read relayed: its SSE
        # event, and an NDJSON line held back there
        m = self.mod

        class Stream:
            def __init__(self, parts):
                self.parts = list(parts)

            def read1(self, n):
                return self.parts.pop(0) if self.parts else b""
        h = object.__new__(m.H)
        h._peer = "test"
        event = b'data: {"error": {"message": "keepalive-proxy: ' + m.HOP_CLOSED.encode() + b'"}}\n\n'
        line = json.dumps({"error": {"message": "keepalive-proxy: " + m.HOP_CLOSED}}).encode() + b"\n"
        for sse, whole, before in ((True, event, b'data: {"choices": []}\n\n'), (False, line, b'{"done": false}\n')):
            with self.subTest(sse=sse):
                for cut in (1, len(whole) // 2, len(whole) - 2):
                    self.assertEqual(h._end_after(Stream([whole[cut:]]), None, before + whole[:cut], sse), (True, False),
                                     f"a death split at byte {cut} went unread")
                self.assertEqual(h._end_after(Stream([b'{"done": true}\n' if not sse else b"data: [DONE]\n\n"]), None,
                                              before, sse), (False, False))

    def test_the_ndjson_order_line_starts_a_line_of_its_own(self):
        # after a line over 64 KiB relayed unended, the order line glued to it went unread at the entry, which bound the
        # request and gave its client a broken line (the 7th review of v1.43.0, 2026-11-04)
        import io
        m = self.mod
        h = object.__new__(m.H)
        h._pair_box, h.path, h.wfile = "spark2", "/api/chat", io.BytesIO()
        h._held_sse, h._out_sse, h._out_ndjson, h._out_begun, h._nd_open = False, False, True, True, True
        h._say_ended_by_order()
        line = b"\n" + m.HOP_ENDED_NDJSON
        self.assertEqual(h.wfile.getvalue(), b"%x\r\n%s\r\n" % (len(line), line))

    def test_a_peer_never_read_may_serve_for_the_first_read_only_in_both_rules(self):
        # the never-read rule lived in peer_may_serve alone: _pair_served_nowhere's waited_for read a missing row as {}
        # (the 7th review of v1.43.0, 2026-11-04)
        m = self.mod
        saved = m._PAIR["loaded"]
        self.addCleanup(m._PAIR.__setitem__, "loaded", saved)
        m._PAIR["loaded"] = time.monotonic()
        self.assertTrue(m.peer_row_may_serve(None, "qwen3.8-27b"))
        m._PAIR["loaded"] = time.monotonic() - m.PAIR_FIRST_READ_S - 1
        self.assertFalse(m.peer_row_may_serve(None, "qwen3.8-27b"))

    def test_an_engine_away_reads_systemd_once_a_second_whatever_the_requests(self):
        # lane_away read systemd afresh for each request while this box's engine was away: two or three systemctl a
        # request, for hours on a box serving images (the 4th review of v1.43.0, 2026-11-04)
        m = self.mod
        calls = self._systemd_says({"out": "ActiveState=inactive\nMainPID=0\n"})
        for _ in range(20):
            self.assertEqual(m.lane_away(), (frozenset(), None))               # images now, or no lane
        self.assertLessEqual(len(calls), 3, [c[1] for c in calls])

    def test_a_read_of_systemd_that_failed_decides_nothing(self):
        # a systemctl that failed stored no state, which lane_away read as no text lane: the lane's model went to the
        # other box whatever its checkpoint (decision 1018), and the failure was kept 10 s (the 4th review of v1.43.0)
        m = self.mod
        self._systemd_says({"out": ""})

        def times_out(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 3)

        def refused(argv, **kw):
            return subprocess.CompletedProcess(argv, 1, "", "Failed to connect to bus")
        m._LAST_LANE["lane"] = (frozenset({"qwen3.8-27b"}), "cd" * 20)
        for fails in (times_out, refused):
            with self.subTest(fails=fails.__name__):
                m.subprocess = types.SimpleNamespace(run=fails)
                self._forget()
                self.assertEqual(m.lane_away(), (frozenset({"qwen3.8-27b"}), "cd" * 20))
                self.assertLess(m._UNIT_FACTS["ts"], time.monotonic() - 8, "a read that failed was kept 10 s")

    def test_a_main_process_gone_or_a_zombie_names_no_checkpoint(self):
        # its unit's ExecStart is its NEXT start: read for a main process gone or a zombie (a crash, before systemd reaps
        # it), this box said the checkpoint an update or switch-model.sh had staged for the one it served (the 4th review
        # of v1.43.0, 2026-11-04)
        m = self.mod
        p = subprocess.Popen(["true"])                                          # a zombie: not reaped yet
        self.addCleanup(p.wait)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with open(f"/proc/{p.pid}/stat") as f:
                if f.read().split(")")[-1].split()[0] == "Z":
                    break
            time.sleep(0.01)
        starts = (f"ExecStart={{ path=/usr/bin/docker ; argv[]=docker run x --served-model-name qwen3.8-27b --revision "
                  f"{'ab' * 20} ; }}\n")
        shown = {"out": f"ActiveState=active\nMainPID={p.pid}\n{starts}"}
        self._systemd_says(shown)
        self.assertIsNone(m.unit_texts(), "a zombie said what its unit starts next")
        shown["out"] = f"ActiveState=active\nMainPID=4194305\n{starts}"        # over any pid_max: gone
        self._forget()
        self.assertIsNone(m.unit_texts(), "a process gone said what its unit starts next")
        shown["out"] = f"ActiveState=activating\nMainPID=0\n{starts}"           # a start under way: what it starts
        self._forget()
        self.assertEqual(m.unit_revision(), "ab" * 20)

    def test_a_script_that_is_not_utf8_is_read_all_the_same(self):
        # a launcher edited by hand in Latin-1 raised UnicodeDecodeError out of unit_stopping_by_order and lane_away under
        # a restart job, and the request's handler died with no answer (the 4th review of v1.43.0, 2026-11-04)
        m = self.mod
        d = pathlib.Path(tempfile.mkdtemp(prefix="kp-latin1-"))
        self.addCleanup(shutil.rmtree, d, True)
        script = d / "launch.sh"
        script.write_bytes(b"#!/bin/bash\n# caf\xe9\nexec docker run x --served-model-name qwen3.8-27b\n")
        self.assertEqual(m.names_in(m.with_scripts(f"/bin/bash {script}")), frozenset({"qwen3.8-27b"}))

    def test_a_command_that_names_no_model_yet_reads_what_the_unit_starts(self):
        # systemd 255 runs systemd-executor as the main process before the exec: a command naming no model, kept 10 s,
        # said no checkpoint and no slots (the delta's 3rd review, 2026-11-04)
        m = self.mod
        executor = sleeper(self, "systemd-executor", "--deserialize", "3")
        shown = {"out": f"ActiveState=active\nMainPID={executor.pid}\nExecStart={{ path=/usr/bin/docker ; argv[]=docker "
                        f"run x --served-model-name qwen3.8-27b --revision {'ab' * 20} --max-running-requests 8 ; }}\n"}
        self._systemd_answers({"out": ""}, shown, {"out": ""})
        self.assertEqual(m.unit_revision(), "ab" * 20)
        self.assertEqual(m.unit_slots(), 8)
        self.assertLess(m._UNIT_FACTS["ts"], time.monotonic() - 8, "an unsure read was kept 10 s")

    def test_the_bindings_that_ended_leave_on_any_lookup(self):
        # after one binding, a box alone hashed every body for good: only a note purged the ones that ended
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        m._KILLERS["ended"] = ("b", time.monotonic() - 1)
        self.assertIsNone(m.killer_box("another"))
        self.assertEqual(len(m._KILLERS), 0, "a binding that ended stayed")

    def test_this_box_notes_itself_as_the_empty_name(self):
        # its pair name noted for itself read as a box that left the pair once pair.json was gone (the delta's 3rd review)
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        saved = (m.unit_stopping_by_order, m.pair_conf)
        m.unit_stopping_by_order, m.pair_conf = (lambda: False), (lambda: {"name": "spark1", "peers": []})
        self.addCleanup(lambda: (setattr(m, "unit_stopping_by_order", saved[0]), setattr(m, "pair_conf", saved[1])))
        h = object.__new__(m.H)
        h._killer_fp, h._stop_why = "fp-own", None
        h._killed_here()
        self.assertEqual(m._KILLERS["fp-own"][0], "")

    def test_a_unit_starting_says_what_it_starts_and_one_stopping_keeps_the_lane_seen_last(self):
        # the delta's review, 2026-11-04: an "activating" unit (ExecStartPre, the wait before a restart) was skipped, and
        # a proxy restarted with its engine sent its 27B to the other box whatever its checkpoint; a unit stopping after
        # a crash, its restart not due yet, read as no lane at all
        m = self.mod
        shown = {"out": "ActiveState=activating\nMainPID=0\nExecStart={ path=/usr/bin/docker ; argv[]=/usr/bin/docker run "
                        f"x --served-model-name qwen3.8-27b --revision {'ab' * 20} ; ignore_errors=no ; }}\n"}
        self._systemd_says(shown)
        self.assertEqual(m.lane_away(), (frozenset({"qwen3.8-27b"}), "ab" * 20))
        shown["out"] = "ActiveState=deactivating\nMainPID=0\n"                 # stopping, no job: a crash
        self._forget()
        m._LAST_LANE["lane"] = (frozenset({"qwen3.8-27b"}), "cd" * 20)
        self.assertEqual(m.lane_away(), (frozenset({"qwen3.8-27b"}), "cd" * 20))
        shown["out"] = "ActiveState=inactive\nMainPID=0\n"                     # no text lane: images, or none
        self._forget()
        self.assertEqual(m.lane_away(), (frozenset(), None))

    def test_a_dead_engines_answer_relayed_in_a_stream_the_keeper_opened_is_known(self):
        # _answer prefixes "keepalive-proxy: " to the message it relays in a stream it opened: the entry missed vLLM's
        # death there (the delta's review, 2026-11-04)
        m = self.mod
        said = "EngineCore encountered an issue. See stack trace (above) for the root cause."
        for event in (m.sse_error_openai(said, "InternalServerError"), m.sse_error(said)):
            self.assertTrue(m.ENGINE_DIED_RE.search(event), event[:120])

    def test_the_names_a_command_serves_are_read_from_it(self):
        names_in = self.mod.names_in
        self.assertEqual(names_in(["docker run x --served-model-name qwen3.8-27b --revision ab"]),
                         frozenset({"qwen3.8-27b"}))
        self.assertEqual(names_in(["exec vllm serve m --served-model-name=ornith-1.5-122b \\\n --port 1"]),
                         frozenset({"ornith-1.5-122b"}))
        self.assertEqual(names_in(["x --served-model-name-extra y", "xx--served-model-name z"]), frozenset())
        self.assertEqual(names_in([]), frozenset())

    def test_this_box_says_its_slots_in_the_list_it_gives_the_other(self):
        m = self.mod
        data = json.dumps({"object": "list", "data": [{"id": "qwen3.8-27b", "object": "model"}]}).encode()
        saved = m.unit_slots
        self.addCleanup(setattr, m, "unit_slots", saved)
        m.unit_slots = lambda: 16
        self.assertEqual(json.loads(m.running_said(data, "spark2"))["x_alzeo_slots"], 16)
        m.unit_slots = lambda: None
        self.assertNotIn("x_alzeo_slots", json.loads(m.running_said(data, "spark2")), "slots not known, none said")

    def test_the_gap_is_that_of_the_box_the_turn_would_go_to(self):
        m = self.mod
        conf = {"name": "me", "peers": []}
        saved = m.unit_slots
        self.addCleanup(setattr, m, "unit_slots", saved)
        m.unit_slots = lambda: 24
        self.assertEqual(m.pair_slots_of("me", conf), 24, "this box: its unit")
        with m._PAIR_LOCK:
            m._PAIR_MODELS["peer-s"] = {"slots": 16}
        self.addCleanup(m._PAIR_MODELS.pop, "peer-s", None)
        self.assertEqual(m.pair_slots_of("peer-s", conf), 16, "the other box: what its list said")
        self.assertIsNone(m.pair_slots_of("gone", conf))

    def test_the_take_over_learns_from_a_refusal_as_the_sharing_does(self):
        # a box that said 503 to a take-over gets no shared request for a while, and one that serves another model now
        # is read again (review, 2026-11-03): no test failed with the take-over learning nothing
        m = self.mod

        def cleanup():
            m._PAIR_COOL.pop("p-cool", None)
            with m._PAIR_LOCK:
                m._PAIR_MODELS.pop("p-moved", None)
        self.addCleanup(cleanup)
        for peer, res in (("p-cool", "fallback"), ("p-moved", "moved")):
            h = object.__new__(m.H)
            h._peer, h._hold_until, h._held_sse = "test", None, False
            h._forward_peer = lambda *a, res=res, **k: res
            h._pair_alt = ([{"name": peer}], "qwen3.8-27b", {"name": "me", "peers": []}, None, [], TEST_REV)
            with m._PAIR_LOCK:
                m._PAIR_MODELS[peer] = {"models": ("qwen3.8-27b",), "ts": time.monotonic()}
            self.assertFalse(h._pair_take_over(b"{}"), res)
        self.assertGreater(m._PAIR_COOL.get("p-cool", 0), time.monotonic(), "a box that said 503 got no pause")
        self.assertNotIn("qwen3.8-27b", m._PAIR_MODELS["p-moved"]["models"], "a box that serves another model kept it")

    def test_a_box_gone_from_the_pair_file_keeps_none_of_its_facts(self):
        # pair.py remove on a box without a name deletes pair.json: the box paired next under the same name got the
        # conversations, the pause and the tile's counts of the one before; a pair.json this box cannot use, the same
        # (review, 2026-11-03)
        m = self.mod
        port = localports.held(self)
        self.addCleanup(lambda: [d.clear() for d in (m._AFFINITY, m._PAIR_COOL, m._PAIR_SPREAD)])
        for gone in ("removed", "unreadable"):
            with self.subTest(gone=gone):
                self.conf_now({"name": "spark1", "peers": [peer_entry(self.cfg, "spark2", port)]})
                m.affinity_note("spark2", [(4096, "fp-spark2")], time.monotonic())
                m._PAIR_COOL["spark2"] = time.monotonic() + 30
                m.pair_spread_count("spark2", "balance")
                if gone == "removed":
                    self.pair_file.unlink()
                else:
                    self.pair_file.write_text("{ not json")
                m._PAIR["seen"] = 0.0
                self.assertIsNone(m.pair_conf())
                facts = ("spark2" in m._AFFINITY, "spark2" in m._PAIR_COOL, "spark2" in m._PAIR_SPREAD)
                self.assertEqual(facts, (False, False, False), f"pair.json {gone}: what was learned of spark2 stayed")

    def test_the_sharing_timings_keep_their_defaults_when_set_wrong(self):
        # PAIR_SHARE_CONNECT_S: 0 made the connect non-blocking (every shared send failed, then a pause), -1, nan or
        # inf raised an error nothing caught, as PAIR_CONNECT_S before its own guard. PAIR_SPREAD_COOL_S: nan paused a
        # box for ever (review, 2026-11-03)
        for name, said, want in (("PAIR_SHARE_CONNECT_S", "0", 3.0), ("PAIR_SHARE_CONNECT_S", "-1", 3.0),
                                 ("PAIR_SHARE_CONNECT_S", "nan", 3.0), ("PAIR_SHARE_CONNECT_S", "inf", 3.0),
                                 ("PAIR_SPREAD_COOL_S", "nan", 30.0), ("PAIR_SPREAD_COOL_S", "inf", 30.0),
                                 ("PAIR_SPREAD_COOL_S", "-5", 30.0), ("PAIR_SPREAD_COOL_S", "0", 0.0),
                                 ("PAIR_CONNECT_S", "inf", 10.0), ("PAIR_CONNECT_S", "nan", 10.0),
                                 # an empty value stopped the proxy at its start, -1 cut every stream (the review of
                                 # v1.44.0)
                                 ("SPOOL_MAX_BYTES", "", 32 << 20), ("SPOOL_MAX_BYTES", "nan", 32 << 20),
                                 ("SPOOL_MAX_BYTES", "inf", 32 << 20), ("SPOOL_MAX_BYTES", "-1", 32 << 20),
                                 ("SPOOL_MAX_BYTES", "x", 32 << 20), ("SPOOL_MAX_BYTES", "0", 0),
                                 ("SPOOL_MAX_BYTES", "0.5", 1 << 19)):
            saved = dict(os.environ)
            env = "PROXY_SPOOL_MAX_MIB" if name == "SPOOL_MAX_BYTES" else name
            os.environ.update({"HOME": str(self.tmp), "QWEN38_PAIR_FILE": str(self.pair_file), "PAIR_PORT": "0",
                               "PROXY_BIND": "127.0.0.1", env: said})
            try:
                spec = importlib.util.spec_from_file_location(f"kp_pair_share_{time.time_ns()}",
                                                              REPO / "keepalive-proxy.py")
                mod = importlib.util.module_from_spec(spec)
                saved_argv, sys.argv = sys.argv, ["keepalive-proxy.py"]
                try:
                    spec.loader.exec_module(mod)
                finally:
                    sys.argv = saved_argv
            finally:
                os.environ.clear()
                os.environ.update(saved)
            self.assertEqual(getattr(mod, name), want, f"{name}={said}")

    def test_the_end_by_order_is_said_in_events_every_client_takes(self):
        m = self.mod
        for event in (m.HOP_ENDED_ANTHROPIC, m.HOP_ENDED_OPENAI):
            self.assertTrue(m.ended_by_order_in(b"data: {}\n\n" + event))
            data = [ln for ln in event.split(b"\n") if ln.startswith(b"data: ")]
            self.assertEqual(len(data), 1)
            json.loads(data[0][6:])                                             # one JSON event, not a comment
            self.assertFalse(event.startswith(b":"), "a comment kills the Claude-family parsers (role 1)")
        self.assertEqual(m.HOP_ENDED_OPENAI, HOP_ENDED_OPENAI)
        quoted = ("data: " + json.dumps({"choices": [{"delta": {"content": '"x_alzeo_ended": "order"'}}]})).encode()
        self.assertFalse(m.ended_by_order_in(quoted), "model output quoting it")
        self.assertTrue(m.ended_by_order_in(b'{"model": "m", "done": false}\n' + m.HOP_ENDED_NDJSON))
        # an Ollama route's tool call carries its arguments as an object: one set a field matched anywhere (the delta's
        # 3rd review, 2026-11-04)
        call = json.dumps({"model": "m", "message": {"role": "assistant", "tool_calls": [{"function": {
            "name": "f", "arguments": {"x_alzeo_ended": "order"}}}]}, "done": False}).encode() + b"\n"
        self.assertFalse(m.ended_by_order_in(call), "a tool call's arguments said an end by order")
        self.assertFalse(m.ended_by_order_in(b'{"x_alzeo_ended": "order"} and more\n'), "not a line of its own")

    def test_an_ollama_stream_this_box_ends_by_order_says_so_on_a_line_of_its_own(self):
        # the Ollama routes' NDJSON streams said nothing before an ordered cut, and the other box bound them (the delta's
        # 3rd review, 2026-11-04)
        import io
        m = self.mod
        h = object.__new__(m.H)
        h._pair_box, h.path, h.wfile = "spark2", "/api/chat", io.BytesIO()
        h._held_sse, h._out_sse, h._out_ndjson, h._out_begun = False, False, True, True
        h._say_ended_by_order()
        h._say_ended_by_order()                                                 # said once
        self.assertEqual(h.wfile.getvalue(), b"%x\r\n%s\r\n" % (len(m.HOP_ENDED_NDJSON), m.HOP_ENDED_NDJSON))

    def test_only_a_stop_job_is_an_order_and_a_restart_is_on_its_way_back(self):
        m = self.mod
        listed = {"out": ""}

        def run(argv, **kw):
            return subprocess.CompletedProcess(argv, 0, listed["out"], "")
        saved = (m.HOLD_UNITS, m.subprocess)
        m.HOLD_UNITS, m.subprocess = ("qwen38-sglang.service", "qwen38-flash.service"), types.SimpleNamespace(run=run)
        self.addCleanup(lambda: (setattr(m, "HOLD_UNITS", saved[0]), setattr(m, "subprocess", saved[1])))
        for jobs, order in (("12 qwen38-sglang.service stop running\n", True),
                            ("13 qwen38-sglang.service restart running\n", False),
                            ("14 qwen38-flash.service start waiting\n", False),
                            ("15 qwen38-other.service stop running\n", False),      # not a lane's unit
                            ("", False)):
            listed["out"] = jobs
            self.assertEqual(m.unit_stopping_by_order(), order, jobs)
        listed["out"] = "13 qwen38-sglang.service restart running\n"
        self.assertEqual(m.unit_jobs(), [("qwen38-sglang.service", "restart")])

    def test_the_marker_of_an_engine_closing_on_a_hop_is_its_error_event_only(self):
        m = self.mod
        for event in (m.sse_error_openai(m.HOP_CLOSED, "upstream_closed"), m.sse_error(m.HOP_CLOSED)):
            self.assertIn(m.HOP_CLOSED_B, event)
        quoted = json.dumps({"choices": [{"delta": {"content": '"message": "keepalive-proxy: ' + m.HOP_CLOSED + '"'}}]})
        self.assertNotIn(m.HOP_CLOSED_B, ("data: " + quoted).encode(), "model output quoting it bound a request")
        self.assertNotIn(m.HOP_CLOSED_B, m.HOP_CLOSED.encode(), "the bare sentence, in any answer")

    def test_the_bindings_that_ended_leave_as_others_come(self):
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        m._KILLERS["old-ended"] = ("b", time.monotonic() - 1)
        m.killer_note("new-one", "b")
        self.assertNotIn("old-ended", m._KILLERS)
        self.assertIn("new-one", m._KILLERS)

    def test_the_checkpoint_of_a_lane_is_read_from_its_engines_command(self):
        m = self.mod
        rev, other = "ab" * 20, "cd" * 20
        self.assertEqual(m.revision_in([f"--model-path R/Qwen --revision {rev} --max-running-requests 8"]), rev)
        self.assertEqual(m.revision_in([f"vllm serve n/F --revision={rev}",
                                        f"--speculative-draft-model-revision {other} --tokenizer-revision {other}"]), rev,
                         "a draft model's or a tokenizer's revision is another flag")
        self.assertIsNone(m.revision_in([f"--revision {rev}", f"--revision {other}"]), "two checkpoints: not known")
        self.assertIsNone(m.revision_in(["--revision main", "--revision " + rev[:39], "--revision " + rev + "0"]))
        self.assertIsNone(m.revision_in([]))
        self.assertEqual(m.pair_revision({"x_alzeo_revision": rev}), rev)
        for bad in (rev.upper(), rev[:39], rev + "0", "main", 7, None, [rev]):
            self.assertIsNone(m.pair_revision({"x_alzeo_revision": bad}), bad)
        self.assertIsNone(m.pair_revision([rev]))

    def test_this_box_reads_its_checkpoint_from_its_engines_command_and_forgets_it_with_its_engine(self):
        m = self.mod
        rev = "ef" * 20
        engines = []
        for r in (rev, "01" * 20):                                              # this engine, then another one
            engines.append(sleeper(self, "docker", "run", "x", "--served-model-name", "qwen3.8-27b", "--revision", r))
        shown = f"ActiveState=active\nMainPID={engines[0].pid}\n"
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, shown, "")
        saved = (m.HOLD_UNITS, m.subprocess)
        m.HOLD_UNITS, m.subprocess = ("qwen38-a.service",), types.SimpleNamespace(run=run)
        m._UNIT_FACTS.update(ts=-1e9, texts=None)

        def restore():
            m.HOLD_UNITS, m.subprocess = saved
            m._UNIT_FACTS.update(ts=-1e9, texts=None)
        self.addCleanup(restore)
        self.assertEqual(m.unit_revision(), rev)
        self.assertEqual(m.unit_slots(), None, "no slots in that command")
        self.assertEqual(len(calls), 1, "one read for the revision and the slots")
        shown = f"ActiveState=active\nMainPID={engines[1].pid}\n"
        self.assertEqual(m.unit_revision(), rev, "kept 10 s")
        m.forget_engine_rules()                             # another engine serves: its unit is read again
        self.assertEqual(m.unit_revision(), "01" * 20)
        m.HOLD_UNITS = ()
        self.assertIsNone(m.unit_revision(), "no unit and no PAIR_CHECKPOINT: nothing said, nothing shared")

    def test_the_load_a_box_says_counts_system_one_and_what_it_holds(self):
        # a box busy with System One's branches, or with requests held while its engine comes back, said it ran none,
        # and the other box sent it more (review, 2026-11-03)
        m = self.mod
        conf = {"name": "me", "peers": []}
        data = json.dumps({"object": "list", "data": [{"id": "qwen3.8-27b"}]}).encode()
        seen = []
        saved = (m.systemone_call, m.systemone_read, m.SYSTEMONE_THINK_TOKENS)

        def call(*a, **k):
            seen.append((m.busy_here(), m.pair_load("me", conf), json.loads(m.running_said(data, "b"))["x_alzeo_running"]))
            return {}

        def restore():
            m.systemone_call, m.systemone_read, m.SYSTEMONE_THINK_TOKENS = saved
        self.addCleanup(restore)
        m.systemone_call, m.systemone_read, m.SYSTEMONE_THINK_TOKENS = call, lambda r, labels: ([1.0], 1.0, 1, 1, None), 0
        plan = {"branches": [(None, "q", ["a", "b"], 5, None)], "prefix": "p", "warm_first": False}
        m.systemone_run("Bearer x", "qwen3.8-27b", plan)
        self.assertEqual(seen, [(1, 1, 1)], "a branch at the engine is load, said and weighed")
        self.assertEqual(m.busy_here(), 0, "and no more once it ended")
        held = object()
        with m._GEN_LOCK:
            m._HELD.add(held)
        self.addCleanup(m._HELD.discard, held)
        self.assertEqual((m.busy_here(), m.pair_load("me", conf)), (1, 1), "a request held is load too")
        self.assertEqual(json.loads(m.running_said(data, "b"))["x_alzeo_running"], 1)

    def test_a_request_an_engine_closed_on_is_kept_with_its_box_for_a_while(self):
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        big = b'{"model": "qwen3.8-27b", "stream": true, "messages": [{"role": "user", "content": "' + b"k" * 300000 + b'"}]}'
        self.assertEqual(m.prompt_fingerprint(big, "/v1/chat/completions"), m.body_fingerprint(big, "/v1/chat/completions"),
                         "role 9's fingerprint changed")
        self.assertIsNone(m.prompt_fingerprint(b'{"model": "x"}', "/v1/chat/completions"), "role 9's size floor")
        r = b'{"model": "qwen3.8-27b", "stream": true, "max_tokens": 9, "messages": [{"role": "user", "content": "k"}]}'
        again = r.replace(b'"stream": true', b'"stream": false').replace(b'"max_tokens": 9', b'"max_tokens": 50')
        fp = lambda body, route="/v1/chat/completions": m.body_fingerprint(body, route)
        self.assertIsNone(m.killer_box(fp(r)))
        m.killer_note(fp(r), "spark2")
        self.assertEqual(m.killer_box(fp(again)), "spark2", "a retry changes stream and max_tokens")
        self.assertIsNone(m.killer_box(fp(r, "/v1/messages")), "another route")
        self.assertIsNone(m.killer_box(None), "a request routing never saw")
        m._KILLERS[fp(r)] = ("spark2", time.monotonic() - 1)
        self.assertIsNone(m.killer_box(fp(r)), "kept no longer")
        for i in range(m.PAIR_KILLER_MAX + 5):
            m.killer_note(fp(b'{"n": %d}' % i), "b")
        self.assertEqual(len(m._KILLERS), m.PAIR_KILLER_MAX, "bounded, the oldest dropped first")

    def test_the_blocks_kept_for_a_box_are_bounded_and_age_out(self):
        m = self.mod
        m._AFFINITY.clear()
        self.addCleanup(m._AFFINITY.clear)
        blocks = [(4096 << i, f"fp{i:02d}") for i in range(4)]
        m.affinity_note("b", blocks, 100.0)
        self.assertEqual(m.affinity_of("b", blocks, 100.0), 4096 << 3, "the longest block known wins")
        self.assertEqual(m.affinity_of("b", blocks[:2], 100.0), 8192)
        self.assertEqual(m.affinity_of("c", blocks, 100.0), 0, "another box knows none")
        self.assertEqual(m.affinity_of("b", blocks, 100.0 + m.PAIR_AFFINITY_KEEP_S), 0, "kept no longer")
        m.affinity_forget("b", blocks[2:])
        self.assertEqual(m.affinity_of("b", blocks, 100.0), 8192, "the blocks forgotten are gone")
        many = [(4096, f"x{i}") for i in range(m.PAIR_AFFINITY_MAX + 10)]
        m.affinity_note("b", many, 200.0)
        self.assertEqual(len(m._AFFINITY["b"]), m.PAIR_AFFINITY_MAX)
        self.assertNotIn("x0", m._AFFINITY["b"], "the oldest go first")
        self.assertIn(f"x{m.PAIR_AFFINITY_MAX + 9}", m._AFFINITY["b"])

    def test_a_box_counts_what_it_sent_the_other_even_past_a_read(self):
        m, conf = self.mod, {"name": "me", "peers": []}
        m._PAIR_OUT.pop("p", None)
        m._PAIR_MODELS["p"] = {"ts": time.monotonic(), "models": (), "running": 5}
        self.addCleanup(m._PAIR_MODELS.pop, "p", None)
        self.assertEqual(m.pair_load("p", conf), 5, "what it runs for its own clients")
        m.pair_sent("p", 1); m.pair_sent("p", 1)
        self.assertEqual(m.pair_load("p", conf), 7, "and this box's requests there")
        # a read whatever its moment: its own clients' count, ours apart, so ours in transit are not lost
        m._PAIR_MODELS["p"]["running"] = 3
        self.assertEqual(m.pair_load("p", conf), 5)
        m.pair_sent("p", -1); m.pair_sent("p", -1)
        self.assertEqual(m.pair_load("p", conf), 3, "the ones that ended, counted once")
        # this box: its generations and what the sharing kept here, each counted once
        a, b = object(), object()
        with m._GEN_LOCK:
            m._GENERATIONS.add(a); m._PAIR_HERE.update((a, b))
        self.addCleanup(lambda: (m._GENERATIONS.discard(a), m._PAIR_HERE.difference_update((a, b))))
        self.assertEqual(m.pair_load("me", conf), 2)

    def test_a_request_whose_answer_began_is_never_taken_over(self):
        # a hold that holds it, or opened its stream with keepalives: bytes may have gone out, and the other box would
        # start an answer over them (decision 998: never a request whose answer began; review, 2026-11-03)
        m = self.mod
        h = object.__new__(m.H)
        h._peer, tried = "test", []
        h._forward_peer = lambda *a, **k: tried.append(a)
        for hold_until, held_sse in ((time.monotonic() + 30, False), (None, True)):
            h._hold_until, h._held_sse = hold_until, held_sse
            h._pair_alt = ([{"name": "p"}], "qwen3.8-27b", {"name": "me", "peers": []}, None, [], TEST_REV)
            self.assertFalse(h._pair_take_over(b"{}"))
        self.assertEqual(tried, [], "a request whose answer began was sent to the other box")

    def test_a_request_no_box_took_over_stays_this_boxs(self):
        # the other box in its pause after a refusal: the request stays here, reached by a stop of this box and in its
        # count, where the take-over had dropped it from both for good (review, 2026-11-03)
        m = self.mod
        h = object.__new__(m.H)
        h._peer, h._hold_until, h._held_sse = "test", None, False
        alt = ([{"name": "p"}], "qwen3.8-27b", {"name": "me", "peers": []}, None, [], TEST_REV)
        m._PAIR_COOL["p"] = time.monotonic() + 60
        self.addCleanup(m._PAIR_COOL.pop, "p", None)
        with m._GEN_LOCK:
            m._GENERATIONS.add(h); m._PAIR_HERE.add(h)
        self.addCleanup(lambda: (m._GENERATIONS.discard(h), m._PAIR_HERE.discard(h)))
        h._pair_alt = alt
        self.assertFalse(h._pair_take_over(b"{}", "this box is short of memory"))
        self.assertIn(h, m._GENERATIONS, "a stop of this box no longer reached it")
        self.assertIn(h, m._PAIR_HERE, "this box's load no longer counted it")
        # held, it was out of the generations: it stays out
        with m._GEN_LOCK:
            m._GENERATIONS.discard(h)
        h._pair_alt = alt
        self.assertFalse(h._pair_take_over(b"{}"))
        self.assertNotIn(h, m._GENERATIONS, "a held request is in flight nowhere")


class TheLaneNamesAreThoseTheUnitsServe(unittest.TestCase):
    def test_lane_names_match_every_served_model_name(self):
        src = (REPO / "keepalive-proxy.py").read_text()
        block = re.search(r"LANE_NAMES = frozenset\(\{(.*?)\}\)", src, re.S).group(1)
        names = set(re.findall(r'"([^"]+)"', block))
        served = set()
        for t in REPO.glob("*.template"):
            served |= set(re.findall(r"--served-model-name[ =]([A-Za-z0-9._/-]+)", t.read_text()))
        self.assertEqual(names, served, "a lane's served name and the pair's list of lane names agree")


if __name__ == "__main__":
    unittest.main()
