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
import ssl
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
        mode = "ok"                 # "down": /v1/models answers 503 (a boot) ; "cut": a stream dies mid-way ;
                                    # "busy": every POST answers 503 ; "die": every POST is read, then the
                                    # connection closed without an answer (an engine that died on it)

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
                if Handler.mode == "count_fails":       # an engine stopping: its count says 503
                    return self.reply({"error": {"message": "stopping", "type": "server_error"}}, 503)
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
            time.sleep(body.get("x_delay", Handler.delay))
            if self.path.startswith("/v1/messages"):
                return self.reply({"id": "m", "type": "message", "role": "assistant", "model": Handler.served[0],
                                   "content": [{"type": "text", "text": f"from {name}"}]})
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
        self.E1.mode = self.E2.mode = "ok"
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
                self.E2.delay = 8.0
                gone = self.journal("spark2").count("CLIENT GONE")
                c = http.client.HTTPConnection("127.0.0.1", self.port1, timeout=60)
                t0 = time.monotonic()
                c.request("POST", "/v1/chat/completions",
                          json.dumps(self.chat("qwen3.8-flash-next-fp4", stream=stream)).encode(),
                          {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
                time.sleep(1.5)
                c.close()                           # the client leaves before the answer
                deadline = time.monotonic() + 5.5
                while self.journal("spark2").count("CLIENT GONE") <= gone and time.monotonic() < deadline:
                    time.sleep(0.1)
                self.assertGreater(self.journal("spark2").count("CLIENT GONE"), gone, self.journal("spark2")[-600:])
                self.assertLess(time.monotonic() - t0, 7.5, "seen before its engine answered")

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
        self.addCleanup(lambda: [t.join(30) for t in threads])
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
        hole = socket.socket()
        hole.bind(("127.0.0.1", 0))
        hole.listen(16)                     # takes the connection, never answers its TLS
        self.addCleanup(hole.close)
        self.repoint(hole.getsockname()[1])
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
        hole = socket.socket()
        hole.bind(("127.0.0.1", 0))
        hole.listen(16)                     # takes the connection, never answers its TLS
        self.addCleanup(hole.close)
        self.repoint(hole.getsockname()[1])
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
        eng = Engine(("127.0.0.1", 0), h)
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
                                 upstream=f"http://127.0.0.1:{eng.server_address[1]}")
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
        self.assertIn(b"serves another model now", out)
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
        m._LAST_REV["qwen3.8-27b"] = "ab" * 20                                # this box served another checkpoint
        self.addCleanup(m._LAST_REV.pop, "qwen3.8-27b", None)
        real = m.served_models

        def away(*a, **k):
            raise m.EngineUnreachable("connection refused (the test's engine away)")
        m.served_models = away
        self.addCleanup(setattr, m, "served_models", real)
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"))
        self.assertEqual(st, 503, out[:300])
        self.assertIn(b"another checkpoint", out)
        self.assertEqual(self.E2.users, [], "a client of this box got the other checkpoint")
        m._LAST_REV["qwen3.8-27b"] = TEST_REV                                 # the same checkpoint there: it serves
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"))
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark2", out)

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
        script.write_text("#!/bin/bash\n# --max-num-seqs 99, a comment, counts not\nexec vllm serve m --max-num-seqs 16\n")
        shown = ("ActiveState=inactive\nExecStart={ path=/usr/bin/docker ; argv[]=docker run x --max-running-requests 8 "
                 f"; }}\n\nActiveState=active\nExecStart={{ path={script} ; argv[]={script} ; }}\n")
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
            h._pair_alt = ([{"name": peer}], "qwen3.8-27b", {"name": "me", "peers": []}, None, [])
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
                                 ("PAIR_SPREAD_COOL_S", "-5", 30.0), ("PAIR_SPREAD_COOL_S", "0", 0.0)):
            saved = dict(os.environ)
            os.environ.update({"HOME": str(self.tmp), "QWEN38_PAIR_FILE": str(self.pair_file), "PAIR_PORT": "0",
                               "PROXY_BIND": "127.0.0.1", name: said})
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

    def test_this_box_reads_its_checkpoint_from_its_unit_and_forgets_it_with_its_engine(self):
        m = self.mod
        rev = "ef" * 20
        shown = f"ActiveState=active\nExecStart={{ path=/usr/bin/docker ; argv[]=docker run x --revision {rev} ; }}\n"
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
        shown = shown.replace(rev, "01" * 20)
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
            h._pair_alt = ([{"name": "p"}], "qwen3.8-27b", {"name": "me", "peers": []}, None, [])
            self.assertFalse(h._pair_take_over(b"{}"))
        self.assertEqual(tried, [], "a request whose answer began was sent to the other box")

    def test_a_request_no_box_took_over_stays_this_boxs(self):
        # the other box in its pause after a refusal: the request stays here, reached by a stop of this box and in its
        # count, where the take-over had dropped it from both for good (review, 2026-11-03)
        m = self.mod
        h = object.__new__(m.H)
        h._peer, h._hold_until, h._held_sse = "test", None, False
        alt = ([{"name": "p"}], "qwen3.8-27b", {"name": "me", "peers": []}, None, [])
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
