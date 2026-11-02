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
import unittest
import urllib.error

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import localports  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[1]
KEY_1, KEY_2 = "k-entry-111", "k-peer-222"
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


def install_identity(cfg, who):
    """`who`'s certificate and key as the box's own (cfg/pair/identity), as pair.py leaves them."""
    _, _, d = ident(who)
    (cfg / "pair/identity").mkdir(parents=True, exist_ok=True)
    for f in ("cert.pem", "key.pem"):
        shutil.copy(d / f, cfg / "pair/identity" / f)


def peer_entry(cfg, name, port, key, who=None, host="127.0.0.1"):
    """A pair.json entry for the peer `name` at https://host:port, its key and the certificate of `who` (by
    default its own) written beside it, as pair.py writes them."""
    pem, sha, _ = ident(who or name)
    (cfg / "pair").mkdir(exist_ok=True)
    (cfg / f"pair/{name}@t.key").write_text(key + "\n")
    (cfg / f"pair/{name}@t.crt").write_text(pem)
    return {"name": name, "proxy": f"https://{host}:{port}", "key_file": f"pair/{name}@t.key",
            "cert_file": f"pair/{name}@t.crt", "sha256": sha}


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
        delay = 0.0
        mode = "ok"                 # "down": /v1/models answers 503 (a boot) ; "cut": a stream dies mid-way

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
            if self.path.startswith("/v1/models") and Handler.mode == "down":
                return self.reply({"error": "loading"}, 503)
            if self.path.startswith("/v1/models"):
                return self.reply({"object": "list", "data": [{"id": m, "object": "model", "owned_by": "sglang",
                                                               "max_model_len": 262144} for m in Handler.served]})
            if self.path.startswith(("/server_info", "/get_server_info")):
                return self.reply({"max_total_num_tokens": 900000})
            return self.reply({})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            Handler.seen.append((self.path, body.get("model"), self.headers.get("Authorization"),
                                 self.headers.get("X-Alzeo-Pair-Hop")))
            time.sleep(Handler.delay)
            if self.path.startswith("/v1/messages"):
                return self.reply({"id": "m", "type": "message", "role": "assistant", "model": Handler.served[0],
                                   "content": [{"type": "text", "text": f"from {name}"}]})
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


class TwoBoxesBehindOneEndpoint(unittest.TestCase):
    # The entry runs as a process of its own here, and in this test's process in the subclass at the end,
    # where the coverage gate of CI sees its lines (as test_proxy_hold.py does for role 6)
    INPROC = False

    @classmethod
    def setUpClass(cls):
        cls.E1, cls.E2 = fake_engine("spark1", "qwen3.8-27b"), fake_engine("spark2", "qwen3.8-flash-next-fp4")
        cls.engines, cls.procs, cls.inproc = [], [], None
        for h in (cls.E1, cls.E2):
            e = Engine(("127.0.0.1", 0), h)
            threading.Thread(target=e.serve_forever, daemon=True).start()
            cls.engines.append(e)
        cls.tmp = pathlib.Path(tempfile.mkdtemp(prefix="kp-pair-"))
        try:
            # each alone at its start (one port each), then paired as pair.py pairs them
            cls.home2 = cls.home(cls.tmp / "spark2", KEY_2, None, who="spark2")
            cls.port2 = cls.start_proxy(cls.home2, cls.engines[1], "spark2")
            cls.home1 = cls.home(cls.tmp / "spark1", KEY_1, None, who="spark1")
            if cls.INPROC:
                cls.port1 = cls.start_inproc(cls.home1, cls.engines[0])
            else:
                cls.port1 = cls.start_proxy(cls.home1, cls.engines[0], "spark1", PAIR_TTL_S=str(PAIR_TTL))
            cls.pport1, cls.pport2 = cls.pair_up(cls.home1, "spark1", KEY_1, cls.home2, "spark2", KEY_2)
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def start_inproc(cls, home, engine):
        cls.saved_env = dict(os.environ)
        os.environ.update({"HOME": str(home), "PROXY_BIND": "127.0.0.1", "PAIR_TTL_S": str(PAIR_TTL), "PAIR_PORT": "0",
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
               "UPSTREAM": f"http://127.0.0.1:{engine.server_address[1]}",
               "PROMPT_CEILING_TOKENS": "0", "FLASH_PROMPT_CEILING_TOKENS": "0", **extra}
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
    def pair_up(cls, home_a, a, key_a, home_b, b, key_b):
        """a and b paired as pair.py pairs them, both ways: b first, to a stand-in address, so that its pair port
        opens and a can name it, then a, then b again with a's pair port. (a's pair port, b's)."""
        cfg_a, cfg_b = home_a / ".config/qwen38", home_b / ".config/qwen38"
        (cfg_b / "pair.json").write_text(json.dumps({"name": b, "peers": [peer_entry(cfg_b, a, 9, key_a)]}))
        pb = cls.pair_port(b)
        (cfg_a / "pair.json").write_text(json.dumps({"name": a, "peers": [peer_entry(cfg_a, b, pb, key_b)]}))
        pa = cls.pair_port(a)
        (cfg_b / "pair.json").write_text(json.dumps({"name": b, "peers": [peer_entry(cfg_b, a, pa, key_a)]}))
        time.sleep(2.5)                     # b reads its pair.json again
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
        self.E1.seen.clear(); self.E2.seen.clear()
        self.E1.served[:] = ["qwen3.8-27b"]; self.E2.served[:] = ["qwen3.8-flash-next-fp4"]
        self.E1.delay = self.E2.delay = 0.0
        self.E1.mode = self.E2.mode = "ok"

    def call(self, path, body=None, port=None, key=KEY_1, headers=None, method="POST"):
        c = http.client.HTTPConnection("127.0.0.1", port or self.port1, timeout=60)
        try:
            h = {"Content-Type": "application/json", "Authorization": f"Bearer {key}", **(headers or {})}
            c.request(method, path, json.dumps(body).encode() if body is not None else None, h)
            r = c.getresponse()
            return r.status, r.read(), dict(r.getheaders())
        finally:
            c.close()

    def tls_call(self, port, path, body=None, me="spark2", server="spark1", key=KEY_1, headers=None, method="POST"):
        """A request to a box's pair port as the box `me` (its certificate), with `key`."""
        c = http.client.HTTPSConnection("127.0.0.1", port, timeout=60, context=client_ctx(me, server))
        try:
            h = {"Content-Type": "application/json", "Authorization": f"Bearer {key}", **(headers or {})}
            c.request(method, path, json.dumps(body).encode() if body is not None else None, h)
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
        deadline = time.monotonic() + within
        while text not in self.journal_of(label) and time.monotonic() < deadline:
            time.sleep(0.1)
        return text in self.journal_of(label)

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
            pair = {"name": name, "peers": [peer_entry(cfg, n, p, k) for n, p, k in peers]}
        if callable(pair):
            pair = pair(cfg)
        if pair is not None:
            (cfg / "pair.json").write_text(json.dumps(pair))
        env = {"PATH": os.environ["PATH"], "HOME": str(home), "PROXY_BIND": "127.0.0.1",
               "PAIR_PORT": str(localports.held(self)),
               "UPSTREAM": upstream or f"http://127.0.0.1:{self.engines[0].server_address[1]}",
               "PROMPT_CEILING_TOKENS": "0", "FLASH_PROMPT_CEILING_TOKENS": "0", **(extra or {})}
        with open(self.tmp / f"{label}.journal", "ab") as journal:
            proc, port = localports.spawn(lambda port: subprocess.Popen(
                [sys.executable, str(REPO / "keepalive-proxy.py"), str(port)], env=env,
                stdout=subprocess.DEVNULL, stderr=journal))
        self.procs.append(proc)
        return port

    def repoint(self, port):
        """spark1's pair.json with spark2 at https://127.0.0.1:port, taken within 2 s; put back at cleanup."""
        f = self.home1 / ".config/qwen38/pair.json"
        pair = json.loads(f.read_text())
        pair["peers"][0]["proxy"] = f"https://127.0.0.1:{port}"
        f.write_text(json.dumps(pair))
        time.sleep(2.5)

        def back():
            pair["peers"][0]["proxy"] = f"https://127.0.0.1:{self.pport2}"
            f.write_text(json.dumps(pair))
            time.sleep(2.5)
        self.addCleanup(back)

    # ---- routing --------------------------------------------------------------------------------
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

    # ---- refusals -------------------------------------------------------------------------------
    def test_a_lane_no_box_serves_is_refused_with_what_each_box_serves(self):
        st, out, _ = self.call("/v1/chat/completions", self.chat("kolibri-1"))
        self.assertEqual(st, 404, out[:300])
        err = json.loads(out)["error"]
        self.assertEqual(err["type"], "model_not_found")
        self.assertEqual(err["served"], {"spark1": ["qwen3.8-27b"], "spark2": ["qwen3.8-flash-next-fp4"]})
        self.assertEqual(self.E1.seen + self.E2.seen, [], "no engine is asked for a model it does not serve")

    def test_the_anthropic_refusal_is_in_its_own_shape(self):
        st, out, _ = self.call("/v1/messages", self.chat("ornith-1.5-122b"))
        self.assertEqual(st, 404, out[:300])
        j = json.loads(out)
        self.assertEqual((j["type"], j["error"]["type"]), ("error", "model_not_found"))

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
        (cfg2b / "pair.json").write_text(json.dumps({"name": "spark2", "peers": [
            peer_entry(cfg2b, "spark1", self.pport1, KEY_1)]}))
        self.repoint(self.pair_port("spark2b"))
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"))
        self.assertEqual(st, 404, out[:300])
        self.assertEqual(self.E2.seen, [], "the peer's engine is never handed the old model's request")
        st, out, _ = self.call("/v1/chat/completions", self.chat("ornith-1.5-122b"))
        self.assertEqual(st, 200, out[:300])

    def test_a_request_a_peer_sent_on_is_served_or_refused_never_sent_on(self):
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"),
                               headers={"X-Alzeo-Pair-Hop": "spark9"})
        self.assertEqual(st, 409, out[:300])
        self.assertEqual(json.loads(out)["error"]["type"], "model_not_served")
        self.assertEqual(self.E1.seen + self.E2.seen, [])

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
        entry = self.spare_proxy("frozen-list", KEY_1, peers=[("spark2", tls_peer(self, Frozen), KEY_2)])
        for _ in range(3):
            t0 = time.monotonic()
            st, out, _ = self.call("/v1/models", port=entry, method="GET")
            self.assertLess(time.monotonic() - t0, 2, "needle.sh and bench-matrix.sh give it 5 s")
            self.assertEqual(st, 200)

    def test_the_peers_key_reaches_no_journal(self):
        self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"))
        self.call("/v1/chat/completions", self.chat("qwen3.8-27b"), port=self.port2, key=KEY_2)
        for label in ("spark1", "spark2"):
            text = self.journal(label)
            for secret in (KEY_1, KEY_2, "PRIVATE KEY", (ident("spark1")[2] / "key.pem").read_text().splitlines()[1],
                           (ident("spark2")[2] / "key.pem").read_text().splitlines()[1]):
                self.assertNotIn(secret, text, label)
        self.assertIn("-> pair spark2 for qwen3.8-flash-next-fp4", self.journal("spark1"))
        self.assertIn("-> pair spark1 for qwen3.8-27b", self.journal("spark2"))

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
        self.assertIn("sent on by spark2", self.journal("spark1"))

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
        entry = self.spare_proxy("rogue-peer", KEY_1, peers=[("spark2", tls_peer(self, Rogue, me="rogue"), KEY_2)])
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
        kept = (cfg1 / "pair.json").read_text()
        # spark2 out, another box in its place: spark1 stays paired, its pair port open
        (cfg1 / "pair.json").write_text(json.dumps({"name": "spark1", "peers": [
            peer_entry(cfg1, "spark3", 9, "k-3", who="rogue")]}))

        def back():
            (cfg1 / "pair.json").write_text(kept)
            time.sleep(2.5)
        self.addCleanup(back)
        time.sleep(2.5)
        with self.assertRaises((ssl.SSLError, ConnectionResetError, BrokenPipeError, http.client.RemoteDisconnected)):
            self.tls_call(self.pport1, "/v1/chat/completions", self.chat("qwen3.8-27b"))
        self.assertEqual(len(self.E1.seen), 1, "the second request never reached the engine")

    def test_the_pair_port_opens_with_a_pair_and_closes_without(self):
        home = self.home(self.tmp / "opens", KEY_1, None, who="spark1")
        self.start_proxy(home, self.engines[0], "opens")            # alone: no pair port
        self.assertNotIn("pair: port", self.journal("opens"))
        cfg = home / ".config/qwen38"
        (cfg / "pair.json").write_text(json.dumps({"name": "spark1", "peers": [
            peer_entry(cfg, "spark2", self.pport2, KEY_2)]}))
        port = self.pair_port("opens", within=5)                     # within 2 s of pair.json
        st, out, _ = self.tls_call(port, "/v1/models", method="GET")  # spark2's certificate gets past
        self.assertEqual(st, 200, out[:300])
        (cfg / "pair.json").unlink()
        self.assertTrue(self.until_in_journal("opens", f"pair: port {port} closed: this box has no pair now", 5))
        with self.assertRaises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", port), timeout=5).close()

    def test_a_certificate_that_is_not_the_one_pinned_leaves_the_box_alone(self):
        for label, said in (("pin-off", "is not the certificate pair.json pins"),
                            ("no-identity", "has no certificate of its own")):
            with self.subTest(label=label):
                home = self.home(self.tmp / label, KEY_1, None, who="spark1")
                cfg = home / ".config/qwen38"
                e = peer_entry(cfg, "spark2", self.pport2, KEY_2)
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
        # engine generated to the end for nobody)
        self.E2.delay = 8.0
        c = http.client.HTTPConnection("127.0.0.1", self.port1, timeout=60)
        t0 = time.monotonic()
        c.request("POST", "/v1/chat/completions", json.dumps(self.chat("qwen3.8-flash-next-fp4")).encode(),
                  {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
        time.sleep(1.5)
        c.close()                                   # the client leaves before the answer
        self.assertTrue(self.until_in_journal("spark2", "CLIENT GONE", 5.5), self.journal("spark2")[-600:])
        self.assertLess(time.monotonic() - t0, 7.5, "seen before its engine answered")

    # ---- what the review of 2026-11-02 found ----------------------------------------------------
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
        self.assertIn("UPSTREAM CUT pair", self.journal("spark1"))

    def test_a_peer_without_pair_json_refuses_a_request_sent_on_for_another_model(self):
        bare = self.home(self.tmp / "bare-peer", KEY_2, None)            # a box sent a request with the hop header
        port = self.start_proxy(bare, self.engines[0], "bare-peer")     # its engine serves the 27B
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=port, key=KEY_2,
                               headers={"X-Alzeo-Pair-Hop": "spark1"})
        self.assertEqual(st, 409, out[:300])
        self.assertEqual(json.loads(out)["error"]["type"], "model_not_served")
        self.assertEqual(self.E1.seen, [], "the 27B is never asked a request for Flash")
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=port, key=KEY_2)
        self.assertEqual(st, 200, "a client of its own, with no hop, is served as on a box alone")

    def test_a_request_sent_on_to_a_box_whose_engine_restarts_is_told_to_try_again_not_held(self):
        # a peer whose engine does not answer while its unit starts again (a switch): its own clients are
        # held for whatever lane comes back (role 6), but a request an entry sent on for one model is told
        # at once to try again, since the lane that comes back may serve another
        t = self.tmp / "restarting"
        (t / "bin").mkdir(parents=True)
        (t / "bin/systemctl").write_text(
            "#!/bin/sh\n"
            'case "$*" in *InvocationID*) echo inv-1; echo; exit 0;; esac\n'
            'if [ "$1" = show ]; then echo activating; exit 0; fi\n'
            'if [ "$1" = list-jobs ]; then exit 0; fi\n'
            "exit 3\n")
        (t / "bin/systemctl").chmod(0o755)
        home = self.home(t / "home", KEY_2, None)
        gone = localports.held(self)                      # its engine: nothing listens there
        env = {"PATH": f"{t / 'bin'}:{os.environ['PATH']}", "HOME": str(home), "PROXY_BIND": "127.0.0.1",
               "UPSTREAM": f"http://127.0.0.1:{gone}", "PROMPT_CEILING_TOKENS": "0", "FLASH_PROMPT_CEILING_TOKENS": "0",
               "PROXY_HOLD_UNITS": "qwen38-x.service", "PROXY_HOLD_MAX_S": "20", "PROXY_HOLD_POLL_S": "0.2"}
        with open(self.tmp / "restarting.journal", "ab") as journal:
            proc, port = localports.spawn(lambda port: subprocess.Popen(
                [sys.executable, str(REPO / "keepalive-proxy.py"), str(port)], env=env,
                stdout=subprocess.DEVNULL, stderr=journal))
        self.procs.append(proc)
        t0 = time.monotonic()
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"), port=port, key=KEY_2,
                               headers={"X-Alzeo-Pair-Hop": "spark1"})
        self.assertLess(time.monotonic() - t0, 5, "not held up to PROXY_HOLD_MAX_S")
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
        pa = self.start_proxy(a, self.engines[0], "sym-a")
        self.start_proxy(b, self.engines[1], "sym-b")
        self.pair_up(a, "sym-a", KEY_1, b, "sym-b", KEY_2)
        st, rows = self.models_once_known(port=pa)
        self.assertEqual(st, 200)
        self.assertEqual(rows, {"qwen3.8-27b": "sym-a", "qwen3.8-flash-next-fp4": "sym-b"})
        count = lambda label: self.journal(label).count("GET /v1/models ok get")
        before = (count("sym-a"), count("sym-b"))
        time.sleep(2)                       # a loop would go on asking with nobody calling
        self.assertEqual((count("sym-a"), count("sym-b")), before, "no GET goes on once the client stopped")
        self.assertLessEqual(before[1], before[0], "each GET of the client reads the peer's own list once at most")
        st, out, _ = self.call("/v1/chat/completions", self.chat("kolibri-1"), port=pa)
        self.assertEqual(st, 404, out[:300])
        self.assertEqual(json.loads(out)["error"]["served"],
                         {"sym-a": ["qwen3.8-27b"], "sym-b": ["qwen3.8-flash-next-fp4"]},
                         "each box says what it serves itself, not the union it shows its clients")

    # ---- what the code review of 2026-11-02 found -------------------------------------------------
    def test_a_box_that_cannot_read_its_own_key_sends_nothing_on(self):
        port = self.spare_proxy("no-key", KEY_1, peers=[("spark2", self.pport2, KEY_2)], keyfile=False)
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=port, key="anything")
        self.assertEqual(st, 503, out[:300])
        self.assertEqual(self.E2.seen, [], "the peer is never sent a request this box could not check")

    def test_a_pair_json_with_a_bad_port_or_name_leaves_the_box_alone(self):
        for label, pair in (("bad-port", lambda cfg: {"name": "spark1", "peers": [
                                dict(peer_entry(cfg, "spark2", 1, KEY_2), proxy="https://127.0.0.1:3000l")]}),
                            ("http-peer", lambda cfg: {"name": "spark1", "peers": [
                                dict(peer_entry(cfg, "spark2", 1, KEY_2), proxy=f"http://127.0.0.1:{self.port2}")]}),
                            ("bad-name", lambda cfg: {"name": "Spark \u2460 bureau", "peers": []})):
            with self.subTest(label=label):
                port = self.spare_proxy(label, KEY_1, pair=pair)
                st, out, _ = self.call("/v1/models", port=port, method="GET")
                self.assertEqual(st, 200, out[:300])
                self.assertEqual([r.get("box") for r in json.loads(out)["data"]], [None], "a box alone")
                self.assertIn("pair.json is not used", self.journal(label))
        self.assertIn("proxy is https://host:port", self.journal("http-peer"), "never in clear between two boxes")

    def test_an_entry_whose_engine_does_not_answer_says_so_on_v1_models(self):
        port = self.spare_proxy("entry-down", KEY_1, peers=[("spark2", self.pport2, KEY_2)],
                                upstream=f"http://127.0.0.1:{localports.held(self)}")
        st, out, _ = self.call("/v1/models", port=port, method="GET")
        self.assertEqual(st, 503, "as a box alone: no peer's model stands in for this box's engine")

    def test_no_proxy_of_the_environment_gets_the_peers_key(self):
        dead = f"http://127.0.0.1:{localports.held(self)}"
        port = self.spare_proxy("with-proxy-env", KEY_1, peers=[("spark2", self.pport2, KEY_2)],
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
        port = self.spare_proxy("switch-409", KEY_1, peers=[("spark2", tls_peer(self, Switched), KEY_2)],
                                extra={"PAIR_TTL_S": "300"})
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=port)
        self.assertEqual(st, 200, out[:300])          # its list is read and kept 300 s
        Switched.models = ["ornith-1.5-122b"]         # the peer switches; the entry's list is the old one
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=port)
        self.assertEqual(st, 404, out[:300])          # its 409 had every list read again: served nowhere now
        self.assertEqual(json.loads(out)["error"]["served"]["spark2"], ["ornith-1.5-122b"])
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
        port = self.spare_proxy("silent", KEY_1, peers=[("spark2", tls_peer(self, Silent), KEY_2)],
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
        port = self.spare_proxy("held-box", KEY_2, peers=[("spark1", localports.held(self), KEY_1)], name="spark2",
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

    def test_a_lane_this_box_serves_stays_here(self):
        super().test_a_lane_this_box_serves_stays_here()

    def test_a_lane_the_peer_serves_goes_there_with_the_peers_key(self):
        super().test_a_lane_the_peer_serves_goes_there_with_the_peers_key()

    def test_a_stream_from_the_peer_arrives_whole_and_in_order(self):
        super().test_a_stream_from_the_peer_arrives_whole_and_in_order()

    def test_the_anthropic_dialect_goes_to_the_peer_too(self):
        super().test_the_anthropic_dialect_goes_to_the_peer_too()

    def test_any_other_name_stays_on_this_box_as_on_a_box_alone(self):
        super().test_any_other_name_stays_on_this_box_as_on_a_box_alone()

    def test_either_box_is_an_entry(self):
        super().test_either_box_is_an_entry()

    def test_a_lane_no_box_serves_is_refused_with_what_each_box_serves(self):
        super().test_a_lane_no_box_serves_is_refused_with_what_each_box_serves()

    def test_the_anthropic_refusal_is_in_its_own_shape(self):
        super().test_the_anthropic_refusal_is_in_its_own_shape()

    def test_a_peer_that_switched_since_its_list_was_read_never_serves_the_wrong_model(self):
        super().test_a_peer_that_switched_since_its_list_was_read_never_serves_the_wrong_model()

    def test_a_request_a_peer_sent_on_is_served_or_refused_never_sent_on(self):
        super().test_a_request_a_peer_sent_on_is_served_or_refused_never_sent_on()

    def test_a_peer_that_does_not_answer_is_said_at_once(self):
        super().test_a_peer_that_does_not_answer_is_said_at_once()

    def test_the_models_list_names_both_boxes(self):
        super().test_the_models_list_names_both_boxes()

    def test_the_models_list_never_waits_for_a_peer(self):
        super().test_the_models_list_never_waits_for_a_peer()

    def test_the_peers_key_reaches_no_journal(self):
        super().test_the_peers_key_reaches_no_journal()

    def test_a_box_without_pair_json_is_what_it_was(self):
        super().test_a_box_without_pair_json_is_what_it_was()

    def test_a_pair_json_without_a_peer_is_a_box_alone(self):
        super().test_a_pair_json_without_a_peer_is_a_box_alone()

    def test_a_pair_json_that_does_not_parse_leaves_the_box_alone(self):
        super().test_a_pair_json_that_does_not_parse_leaves_the_box_alone()

    def test_a_request_on_the_pair_port_is_a_hop_from_the_box_of_its_certificate(self):
        super().test_a_request_on_the_pair_port_is_a_hop_from_the_box_of_its_certificate()

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

    def test_a_peer_whose_engine_restarts_gets_a_503_to_try_again_not_a_404(self):
        super().test_a_peer_whose_engine_restarts_gets_a_503_to_try_again_not_a_404()

    def test_a_stream_the_peer_cuts_reaches_the_client_cut_never_ended(self):
        super().test_a_stream_the_peer_cuts_reaches_the_client_cut_never_ended()

    def test_a_peer_without_pair_json_refuses_a_request_sent_on_for_another_model(self):
        super().test_a_peer_without_pair_json_refuses_a_request_sent_on_for_another_model()

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
        return {"name": name, "proxy": proxy, "key_file": f"pair/{name}.key", "cert_file": f"pair/{name}.crt",
                "sha256": ident(who)[1]}

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
        self.assertTrue(ok["peers"][0]["key_file"].endswith("/.config/qwen38/pair/spark2.key"))
        self.assertTrue(ok["peers"][0]["cert_file"].endswith("/.config/qwen38/pair/spark2.crt"))
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

    def test_the_tls_of_a_pair_is_checked_whole(self):
        good = peer_entry(self.cfg, "spark2", localports.held(self), KEY_2)
        conf = self.conf_now({"name": "spark1", "peers": [good]})
        self.assertEqual(conf["tls"]["pins"], {ident("spark2")[1]: "spark2"})
        self.assertEqual(conf["tls"]["mine"], ident("spark1")[1])
        self.assertTrue(conf["peers"][0]["opener"] and conf["peers"][0]["list_opener"])
        self.assertIn("over TLS, this box's certificate sha256 " + ident("spark1")[1][:16], self.lines[-1])
        (self.cfg / "pair/two.crt").write_text(ident("spark2")[0] + ident("rogue")[0])
        (self.cfg / "pair/none.crt").write_text("not a certificate\n")
        for why, e in (("is not the certificate pair.json pins", dict(good, sha256=ident("rogue")[1])),
                       ("cannot be read", dict(good, cert_file="pair/missing.crt")),
                       ("its certificate is this box's own", peer_entry(self.cfg, "me", localports.held(self), KEY_2, who="spark1")),
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

    def test_the_hop_header_and_the_model(self):
        hop = self.mod.pair_hop
        self.assertIsNone(hop({}))
        self.assertIsNone(hop({"X-Alzeo-Pair-Hop": "  "}))
        self.assertEqual(hop({"X-Alzeo-Pair-Hop": "spark1"}), "spark1")
        self.assertEqual(hop({"X-Alzeo-Pair-Hop": "spark1\nGET /fake"}), "a peer")
        bm = self.mod.body_model
        self.assertEqual(bm(b'{"model": "kolibri-1"}'), "kolibri-1")
        for body in (b"not json", b"[1]", b'{"model": 3}', b"{}"):
            self.assertIsNone(bm(body))

    def test_a_peers_list_is_read_kept_and_read_again(self):
        class Lists(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            answer, gets = (200, {"data": [{"id": "kolibri-1"}, {"id": 3}, "x"]}), []

            def log_message(self, *a):
                pass

            def do_GET(self):
                Lists.gets.append(self.headers.get("X-Alzeo-Pair-Hop"))
                code, obj = Lists.answer
                out = json.dumps(obj).encode()
                self.send_response(code); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
        port = tls_peer(self, Lists)
        conf = self.mod._pair_parse(json.dumps({"name": "spark1", "peers": [
            peer_entry(self.cfg, "lists", port, "k-peer", who="spark2"),
            peer_entry(self.cfg, "gone", localports.held(self), "k-peer", who="rogue"),
            dict(peer_entry(self.cfg, "nokey", port, "k-peer", who="spark3"), key_file="pair/missing.key")]}))
        self.mod._pair_tls(conf)
        peer, gone, nokey = conf["peers"]
        pm = self.mod.peer_models
        self.assertEqual(pm(peer), ("kolibri-1",))
        self.assertEqual(pm(peer), ("kolibri-1",), "kept")
        self.assertEqual(len(Lists.gets), 1)
        self.assertTrue(Lists.gets[0], "a peer's list is asked as a hop: its own, never its union")
        t = time.monotonic()
        self.assertEqual(pm(peer, fresh=True, since=t - 60), ("kolibri-1",), "read after `since`: kept")
        self.assertEqual(len(Lists.gets), 1)
        Lists.answer = (200, [1, 2])
        self.assertEqual(pm(peer, fresh=True), ())
        self.assertIn("not a list", self.mod._PAIR_MODELS["lists"]["error"])
        Lists.answer = (503, {"error": "loading"})
        self.assertEqual(pm(peer, fresh=True), ())
        self.assertEqual(self.mod._PAIR_MODELS["lists"]["error"], "HTTP 503 on /v1/models")
        self.assertEqual(pm(gone), ())
        self.assertIn("nothing listens on its pair port", self.mod._PAIR_MODELS["gone"]["error"])
        self.assertEqual(pm(nokey), ())
        self.assertTrue(self.mod._PAIR_MODELS["nokey"]["error"])

    def test_the_union_and_the_served_map(self):
        conf = self.conf_now({"name": "spark1", "peers": [peer_entry(self.cfg, "spark2", localports.held(self), KEY_2)]})
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
        self.conf_now({"name": "spark1", "peers": [peer_entry(self.cfg, "spark2", localports.held(self), KEY_2)]})
        keeper.check()
        self.assertIsNotNone(keeper.srv)
        port = keeper.srv.server_address[1]
        self.assertIn(f"pair: port 127.0.0.1:{port} open over TLS, to the certificates of spark2 only", self.lines)
        keeper.check()
        self.assertEqual(keeper.srv.server_address[1], port, "opened once")
        self.pair_file.unlink()
        self.mod._PAIR["seen"] = 0.0
        keeper.check()
        self.assertIsNone(keeper.srv)
        self.assertIn(f"pair: port {port} closed: this box has no pair now", self.lines)
        with self.assertRaises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", port), timeout=5).close()

    def test_a_pair_port_that_cannot_be_opened_is_said_once(self):
        keeper = self.mod.PairPort()
        self.addCleanup(keeper.stop)
        self.conf_now({"name": "spark1", "peers": [peer_entry(self.cfg, "spark2", localports.held(self), KEY_2)]})
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

    def test_a_connection_refused_in_setup_reads_nothing_and_a_box_taken_out_is_refused(self):
        class Fake:
            pass
        refused = Fake()
        refused._refused, refused.close_connection = True, False
        self.mod.H.handle_one_request(refused)            # it has no rfile: a read would raise here
        self.assertTrue(refused.close_connection)
        self.conf_now({"name": "spark1", "peers": [peer_entry(self.cfg, "spark3", localports.held(self), "k-3", who="rogue")]})
        out = Fake()
        out._refused, out.close_connection = False, False
        out._pair_box, out._pair_pin, out.client_address = "spark2", ident("spark2")[1], ("127.0.0.1", 5)
        self.mod.H.handle_one_request(out)
        self.assertTrue(out.close_connection)
        self.assertIn("pair port: spark2 (127.0.0.1) REFUSED: no peer of this box's pair now", self.lines)
        self.assertEqual(self.mod._pair_box_of(ident("rogue")[1]), "spark3")
        self.assertIsNone(self.mod._pair_box_of(ident("spark2")[1]))
        self.assertIsNone(self.mod._pair_box_of(None))


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
