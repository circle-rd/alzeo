#!/usr/bin/env python3
"""pair.py, the tool that names the other box of a pair (proxy role 10, #45).

The key is read on standard input and written to a file of mode 600, never on a command line, in
pair.json or on the screen; a peer that does not take the key, or does not answer, gets nothing
written (unless --no-check); a name or an address the proxy would refuse is refused with the proxy's
own reason; removing the last peer leaves the box alone, as it was; and what it writes is what the
proxy reads. A fake peer proxy stands for the other box."""
import http.server
import importlib.util
import json
import os
import pathlib
import shutil
import socketserver
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import localports  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[1]
PEER_KEY = "k-peer-7c1"


class Peer(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    hops: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        Peer.hops.append(self.headers.get("X-Alzeo-Pair-Hop"))
        if self.headers.get("Authorization") != f"Bearer {PEER_KEY}":
            code, obj = 401, {"error": "bad key"}
        else:
            code, obj = 200, {"object": "list", "data": [{"id": "qwen3.8-flash-next-fp4"}]}
        out = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class PairTool(unittest.TestCase):
    def setUp(self):
        self.home = pathlib.Path(tempfile.mkdtemp(prefix="pair-tool-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        (self.home / ".config/qwen38").mkdir(parents=True)
        self.cfg = self.home / ".config/qwen38"
        port = localports.held(self)
        srv = localports.serve(port, Server, Peer)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.peer = f"http://127.0.0.1:{port}"
        Peer.hops.clear()

    def run_tool(self, *args, stdin=""):
        env = {"PATH": os.environ["PATH"], "HOME": str(self.home)}
        r = subprocess.run([sys.executable, str(REPO / "pair.py"), *args], input=stdin, env=env,
                           capture_output=True, text=True, timeout=60)
        return r.returncode, r.stdout + r.stderr

    def test_add_reads_the_key_on_stdin_and_keeps_it_in_a_file_of_its_own(self):
        rc, out = self.run_tool("name", "spark1")
        self.assertEqual(rc, 0, out)
        rc, out = self.run_tool("add", "spark2", self.peer, stdin=PEER_KEY + "\n")
        self.assertEqual(rc, 0, out)
        self.assertIn("spark2 answers and serves: qwen3.8-flash-next-fp4", out)
        self.assertNotIn(PEER_KEY, out)
        pair = json.loads((self.cfg / "pair.json").read_text())
        self.assertEqual(pair, {"name": "spark1", "peers": [
            {"name": "spark2", "proxy": self.peer, "key_file": "pair/spark2.key"}]})
        self.assertNotIn(PEER_KEY, (self.cfg / "pair.json").read_text())
        key = self.cfg / "pair/spark2.key"
        self.assertEqual(key.read_text(), PEER_KEY + "\n")
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.cfg / "pair").stat().st_mode), 0o700)
        self.assertEqual(Peer.hops, ["spark1"], "its list is read as the proxy reads it: a hop, its own")

    def test_what_it_writes_is_what_the_proxy_reads(self):
        self.run_tool("add", "spark2", self.peer, stdin=PEER_KEY)
        saved = dict(os.environ)
        os.environ.update({"HOME": str(self.home)})
        os.environ.pop("QWEN38_PAIR_FILE", None)
        try:
            spec = importlib.util.spec_from_file_location(f"kp_pair_tool_{time.time_ns()}", REPO / "keepalive-proxy.py")
            mod = importlib.util.module_from_spec(spec)
            argv, sys.argv = sys.argv, ["keepalive-proxy.py"]
            try:
                spec.loader.exec_module(mod)
            finally:
                sys.argv = argv
        finally:
            os.environ.clear(); os.environ.update(saved)
        mod.log = lambda msg: None
        conf = mod.pair_conf()
        self.assertEqual([(p["name"], p["proxy"]) for p in conf["peers"]], [("spark2", self.peer)])
        self.assertEqual(mod.peer_models(conf["peers"][0]), ("qwen3.8-flash-next-fp4",))

    def test_a_key_the_peer_refuses_writes_nothing(self):
        rc, out = self.run_tool("add", "spark2", self.peer, stdin="not-its-key\n")
        self.assertNotEqual(rc, 0)
        self.assertIn("HTTP 401", out)
        self.assertNotIn("not-its-key", out)
        self.assertFalse((self.cfg / "pair.json").exists())
        self.assertFalse((self.cfg / "pair/spark2.key").exists())

    def test_a_peer_that_does_not_answer_writes_nothing_but_with_no_check(self):
        gone = f"http://127.0.0.1:{localports.held(self)}"
        rc, out = self.run_tool("add", "spark2", gone, stdin=PEER_KEY)
        self.assertNotEqual(rc, 0)
        self.assertIn("does not answer", out)
        self.assertFalse((self.cfg / "pair.json").exists())
        rc, out = self.run_tool("add", "spark2", gone, "--no-check", stdin=PEER_KEY)
        self.assertEqual(rc, 0, out)
        self.assertTrue((self.cfg / "pair.json").exists())

    def test_a_name_or_an_address_the_proxy_would_refuse_is_refused(self):
        for args in (("two words", self.peer), ("spark2", "https://127.0.0.1:30001"), ("spark2", "http://127.0.0.1:3000l"),
                     ("spark2", "http://127.0.0.1:30001/v1")):
            with self.subTest(args=args):
                rc, out = self.run_tool("add", *args, stdin=PEER_KEY)
                self.assertNotEqual(rc, 0)
                self.assertIn("pair.py:", out)
                self.assertFalse((self.cfg / "pair.json").exists())
        rc, out = self.run_tool("name", "no spaces please")
        self.assertNotEqual(rc, 0)

    def test_no_key_on_stdin_is_refused(self):
        for stdin in ("", "\n", "two words\n"):
            rc, out = self.run_tool("add", "spark2", self.peer, stdin=stdin)
            self.assertNotEqual(rc, 0)
            self.assertIn("no key read", out)

    def test_removing_the_last_peer_leaves_the_box_alone(self):
        self.run_tool("add", "spark2", self.peer, stdin=PEER_KEY)
        self.run_tool("add", "spark3", self.peer, stdin=PEER_KEY)
        rc, out = self.run_tool("remove", "spark3")
        self.assertEqual(rc, 0, out)
        self.assertEqual([p["name"] for p in json.loads((self.cfg / "pair.json").read_text())["peers"]], ["spark2"])
        self.assertFalse((self.cfg / "pair/spark3.key").exists())
        rc, out = self.run_tool("remove", "spark2")
        self.assertEqual(rc, 0, out)
        self.assertFalse((self.cfg / "pair.json").exists())
        self.assertFalse((self.cfg / "pair/spark2.key").exists())
        rc, out = self.run_tool("remove", "spark2")
        self.assertNotEqual(rc, 0)

    def test_status_says_what_each_peer_serves_and_never_the_key(self):
        rc, out = self.run_tool("status")
        self.assertIn("alone", out)
        self.run_tool("name", "spark1")
        self.run_tool("add", "spark2", self.peer, stdin=PEER_KEY)
        rc, out = self.run_tool("status")
        self.assertEqual(rc, 0, out)
        self.assertIn("this box: spark1", out)
        self.assertIn(f"spark2 ({self.peer}): serves qwen3.8-flash-next-fp4", out)
        self.assertNotIn(PEER_KEY, out)
        (self.cfg / "pair/spark2.key").write_text("stale\n")
        rc, out = self.run_tool("status")
        self.assertIn("answers HTTP 401", out)


if __name__ == "__main__":
    unittest.main()
