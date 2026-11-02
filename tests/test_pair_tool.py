#!/usr/bin/env python3
"""pair.py, the tool that pairs two boxes (proxy role 10, discussion #45, decisions 971-976).

Two boxes on this machine, each a home of its own (its API key, its installed proxy), a fake engine and its
proxy run in this process, its client port and its pair port each on a held port. A fake ssh stands for SSH
between them: it runs the command it is given as the other box, with that box's home. `pair.py add` pairs
both boxes in one command over that SSH, each with a certificate of its own; no key is ever on a command
line or on the screen; what each box writes is what its proxy takes, and a request then goes from one box
to the other over TLS; a check that fails keeps nothing on either box; `remove` unpairs both; an SSH that
does not reach the other box, two boxes of one name and an older proxy are said with what to do."""
import hashlib
import http.client
import http.server
import importlib.util
import json
import os
import pathlib
import shutil
import socketserver
import ssl
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
KEY_1, KEY_2 = "k-box1-5d2", "k-box2-8e7"

FAKE_SSH = r'''#!/usr/bin/env python3
"""ssh for the tests: the command runs as the box named, with its home; each call's arguments are kept."""
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["FAKE_SSH_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")
i = 0
while i < len(args):
    if args[i] == "--":
        i += 1
        break
    if args[i] == "-o":
        i += 2
        continue
    if args[i].startswith("-"):
        i += 1
        continue
    break
dest, command = args[i], " ".join(args[i + 1:])
box = json.load(open(os.environ["FAKE_SSH_BOXES"])).get(dest.split("@")[-1])
if box is None:
    sys.stderr.write(f"ssh: Could not resolve hostname {dest}: Name or service not known\r\n")
    sys.exit(255)
if box.get("hostkey") == "unknown":
    sys.stderr.write("Host key verification failed.\r\n")
    sys.exit(255)
env = {"PATH": os.environ["PATH"], "HOME": box["home"], "SSH_CLIENT": f"{box['from']} 50022 22",
       "PAIR_PORT": str(box["pair_port"])}
sys.exit(subprocess.run(["sh", "-c", command], env=env).returncode)
'''


def fake_engine(name, model):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        seen: list = []

        def log_message(self, *a):
            pass

        def reply(self, obj):
            out = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def do_GET(self):
            if self.path.startswith("/v1/models"):
                return self.reply({"object": "list", "data": [{"id": model, "object": "model"}]})
            if self.path.startswith(("/server_info", "/get_server_info")):
                return self.reply({"max_total_num_tokens": 900000})
            return self.reply({})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            Handler.seen.append(body.get("model"))
            self.reply({"id": "c", "object": "chat.completion", "model": model,
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": f"from {name}"},
                                     "finish_reason": "stop"}]})
    return Handler


class Engine(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class Box:
    """A box on this machine: its home, its fake engine, its proxy in this process on held ports."""

    def __init__(self, test, root, name, key, model):
        self.name, self.key = name, key
        self.home = root / name
        self.cfg = self.home / ".config/qwen38"
        self.cfg.mkdir(parents=True)
        (self.cfg / "api-key").write_text(key + "\n")
        shutil.copy(REPO / "keepalive-proxy.py", self.cfg / "keepalive-proxy.py")    # its installed proxy
        self.engine_handler = fake_engine(name, model)
        engine = Engine(("127.0.0.1", 0), self.engine_handler)
        threading.Thread(target=engine.serve_forever, daemon=True).start()
        test.addCleanup(engine.server_close)
        test.addCleanup(engine.shutdown)
        self.port, self.pair_port = localports.held(test), localports.held(test)
        outer = dict(os.environ)
        os.environ.update({"HOME": str(self.home), "PROXY_BIND": "127.0.0.1", "PAIR_PORT": str(self.pair_port),
                           "QWEN38_UPSTREAM_API_KEY": key, "PAIR_TTL_S": "2",
                           "UPSTREAM": f"http://127.0.0.1:{engine.server_address[1]}",
                           "PROMPT_CEILING_TOKENS": "0", "FLASH_PROMPT_CEILING_TOKENS": "0"})
        for k in ("QWEN38_CLIENT_KEYS_FILE", "PROXY_HOLD_UNITS", "QWEN38_PAIR_FILE", "http_proxy", "HTTP_PROXY",
                  "https_proxy", "HTTPS_PROXY"):
            os.environ.pop(k, None)
        try:
            spec = importlib.util.spec_from_file_location(f"kp_tool_{name}_{time.time_ns()}", REPO / "keepalive-proxy.py")
            self.mod = importlib.util.module_from_spec(spec)
            argv, sys.argv = sys.argv, ["keepalive-proxy.py"]
            try:
                spec.loader.exec_module(self.mod)
            finally:
                sys.argv = argv
        finally:
            os.environ.clear()
            os.environ.update(outer)
        self.lines = []
        self.mod.log = self.lines.append
        for port, cls in ((self.port, self.mod.Server), (self.pair_port, self.mod.PairServer)):
            srv = localports.serve(port, cls, self.mod.H)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            test.addCleanup(srv.server_close)
            test.addCleanup(srv.shutdown)

    def pair(self):
        try:
            return json.loads((self.cfg / "pair.json").read_text())
        except FileNotFoundError:
            return None

    def peer_files(self):
        return sorted(p.name for p in (self.cfg / "pair").glob("*@*"))


class PairTool(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="pair-tool-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        # the test's own home while the proxies run here: a proxy reads ~/.config/qwen38/api-key as it goes,
        # and this machine's own key is never read
        mine = self.root / "test-home"
        (mine / ".config/qwen38").mkdir(parents=True)
        (mine / ".config/qwen38/api-key").write_text("k-test-home\n")
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        os.environ["HOME"] = str(mine)
        self.b1 = Box(self, self.root, "box1", KEY_1, "qwen3.8-27b")
        self.b2 = Box(self, self.root, "box2", KEY_2, "qwen3.8-flash-next-fp4")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "ssh").write_text(FAKE_SSH)
        (self.bin / "ssh").chmod(0o755)
        self.ssh_log = self.root / "ssh-argv.log"
        self.boxes_file = self.root / "ssh-boxes.json"
        self.ssh_boxes({})
        for b in (self.b1, self.b2):
            rc, out = self.run_tool(b, "name", b.name)
            self.assertEqual(rc, 0, out)

    def ssh_boxes(self, more):
        boxes = {b.name: {"home": str(b.home), "pair_port": b.pair_port, "from": "127.0.0.1"} for b in (self.b1, self.b2)}
        boxes.update(more)
        self.boxes_file.write_text(json.dumps(boxes))

    def run_tool(self, box, *args, stdin=""):
        env = {"PATH": f"{self.bin}:{os.environ['PATH']}", "HOME": str(box.home), "PAIR_PORT": str(box.pair_port),
               "FAKE_SSH_BOXES": str(self.boxes_file), "FAKE_SSH_LOG": str(self.ssh_log)}
        r = subprocess.run([sys.executable, str(REPO / "pair.py"), *args], input=stdin, env=env,
                           capture_output=True, text=True, timeout=180)
        return r.returncode, r.stdout + r.stderr

    def add(self, *extra):
        return self.run_tool(self.b1, "add", "127.0.0.1", "--ssh", "box2", *extra)

    def call(self, box, model):
        c = http.client.HTTPConnection("127.0.0.1", box.port, timeout=60)
        try:
            c.request("POST", "/v1/chat/completions",
                      json.dumps({"model": model, "messages": [{"role": "user", "content": "hi"}]}).encode(),
                      {"Content-Type": "application/json", "Authorization": f"Bearer {box.key}"})
            r = c.getresponse()
            return r.status, r.read()
        finally:
            c.close()

    @staticmethod
    def sha(path):
        return hashlib.sha256(ssl.PEM_cert_to_DER_cert(path.read_text())).hexdigest()

    def test_add_pairs_both_boxes_in_one_command(self):
        rc, out = self.add()
        self.assertEqual(rc, 0, out)
        self.assertIn("box1 and box2 are a pair", out)
        self.assertIn(f"box1 -> box2 (https://127.0.0.1:{self.b2.pair_port}): answers over TLS, serves "
                      f"qwen3.8-flash-next-fp4", out)
        self.assertIn(f"box2 -> box1 (https://127.0.0.1:{self.b1.pair_port}): answers over TLS, serves qwen3.8-27b", out)
        p1, p2 = self.b1.pair(), self.b2.pair()
        self.assertEqual([(q["name"], q["proxy"], q["ssh"]) for q in p1["peers"]],
                         [("box2", f"https://127.0.0.1:{self.b2.pair_port}", "box2")])
        self.assertEqual([(q["name"], q["proxy"]) for q in p2["peers"]], [("box1", f"https://127.0.0.1:{self.b1.pair_port}")])
        # each pins the other's certificate, made on its own box, and holds the other's key
        for here, there, p in ((self.b1, self.b2, p1), (self.b2, self.b1, p2)):
            entry = p["peers"][0]
            self.assertEqual(entry["sha256"].replace(":", "").lower(), self.sha(there.cfg / "pair/identity/cert.pem"))
            self.assertEqual((here.cfg / entry["key_file"]).read_text(), there.key + "\n")
            for f in (here.cfg / entry["key_file"], here.cfg / entry["cert_file"], here.cfg / "pair.json",
                      here.cfg / "pair/identity/key.pem"):
                self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600, f)
            self.assertEqual(stat.S_IMODE((here.cfg / "pair").stat().st_mode), 0o700)
        # what each wrote, its proxy takes: a request goes from one box to the other over TLS, both ways
        st, body = self.call(self.b1, "qwen3.8-flash-next-fp4")
        self.assertEqual((st, b"from box2" in body), (200, True), body[:300])
        st, body = self.call(self.b2, "qwen3.8-27b")
        self.assertEqual((st, b"from box1" in body), (200, True), body[:300])

    def test_no_key_is_on_a_command_line_or_on_the_screen(self):
        rc, out = self.add()
        self.assertEqual(rc, 0, out)
        calls = [json.loads(line) for line in self.ssh_log.read_text().splitlines()]
        self.assertGreaterEqual(len(calls), 2, "accept, then check")
        for argv in calls:
            joined = " ".join(argv)
            for secret in (KEY_1, KEY_2, "PRIVATE KEY", "BEGIN CERTIFICATE"):
                self.assertNotIn(secret, joined)
            self.assertIn("BatchMode=yes", joined, "never a password asked")
            self.assertEqual(argv[argv.index("--") + 1], "box2", "the destination after --: never taken for an option")
        for secret in (KEY_1, KEY_2, "PRIVATE KEY"):
            self.assertNotIn(secret, out)
        for b in (self.b1, self.b2):
            self.assertNotIn(KEY_1, "\n".join(b.lines))
            self.assertNotIn(KEY_2, "\n".join(b.lines))

    def test_a_box_ssh_does_not_reach_is_said_with_what_to_do(self):
        rc, out = self.run_tool(self.b1, "add", "127.0.0.1", "--ssh", "nowhere")
        self.assertNotEqual(rc, 0)
        self.assertIn("cannot run a command on nowhere over SSH without a password", out)
        self.assertIn("step 4 of NVIDIA's playbook", out)
        self.assertIn("connect-two-sparks", out)
        self.assertEqual(self.b1.pair()["peers"], [], "nothing kept")
        self.ssh_boxes({"box3": {"home": str(self.b2.home), "pair_port": self.b2.pair_port, "from": "127.0.0.1",
                                 "hostkey": "unknown"}})
        rc, out = self.run_tool(self.b1, "add", "127.0.0.1", "--ssh", "box3")
        self.assertNotEqual(rc, 0)
        self.assertIn("does not know the SSH host key of box3 yet: run ssh box3 once", out)

    def test_a_check_that_fails_keeps_nothing_on_either_box(self):
        # box2 says a pair port nothing listens on: box1 cannot reach it
        self.ssh_boxes({"box2": {"home": str(self.b2.home), "pair_port": localports.held(self), "from": "127.0.0.1"}})
        rc, out = self.add()
        self.assertNotEqual(rc, 0)
        self.assertIn("this box cannot reach the pair port of box2", out)
        self.assertIn("nothing listens on its pair port", out)
        self.assertIn("Nothing was kept on either box", out)
        for b in (self.b1, self.b2):
            self.assertEqual(b.pair(), {"name": b.name, "peers": []}, "its name kept, its pair gone")
            self.assertEqual(b.peer_files(), [], "no key or certificate of the other box left")

    def test_a_check_the_other_way_that_fails_keeps_nothing_either(self):
        # box2 reaches box1 at an address where box1 does not listen
        self.ssh_boxes({"box2": {"home": str(self.b2.home), "pair_port": self.b2.pair_port, "from": "127.0.0.2"}})
        rc, out = self.add()
        self.assertNotEqual(rc, 0)
        self.assertIn("box2 cannot reach the pair port of this box at https://127.0.0.2:", out)
        self.assertIn("--back", out)
        for b in (self.b1, self.b2):
            self.assertEqual(b.pair()["peers"], [])
        rc, out = self.add("--back", "127.0.0.1")
        self.assertEqual(rc, 0, out)

    def test_no_check_keeps_a_pair_the_boxes_cannot_use_yet(self):
        self.ssh_boxes({"box2": {"home": str(self.b2.home), "pair_port": localports.held(self), "from": "127.0.0.1"}})
        rc, out = self.add("--no-check")
        self.assertEqual(rc, 0, out)
        self.assertEqual([q["name"] for q in self.b1.pair()["peers"]], ["box2"])
        self.assertEqual([q["name"] for q in self.b2.pair()["peers"]], ["box1"])

    def test_remove_unpairs_both_boxes(self):
        rc, out = self.add()
        self.assertEqual(rc, 0, out)
        rc, out = self.run_tool(self.b1, "remove", "box2")
        self.assertEqual(rc, 0, out)
        self.assertIn("box2 removed from this box's pair: this box is alone again", out)
        self.assertIn("box2 forgot this box too", out)
        for b in (self.b1, self.b2):
            self.assertEqual(b.pair(), {"name": b.name, "peers": []})
            self.assertEqual(b.peer_files(), [])
            self.assertTrue((b.cfg / "pair/identity/cert.pem").is_file(), "its certificate stays, for the next pair")
        rc, out = self.run_tool(self.b1, "remove", "box2")
        self.assertNotEqual(rc, 0)
        self.assertIn("no peer named box2", out)

    def test_a_box_that_cannot_be_told_is_said_with_the_command_to_run_there(self):
        rc, out = self.add()
        self.assertEqual(rc, 0, out)
        self.ssh_boxes({"box2": {"home": str(self.b2.home), "pair_port": self.b2.pair_port, "from": "127.0.0.1",
                                 "hostkey": "unknown"}})
        rc, out = self.run_tool(self.b1, "remove", "box2")
        self.assertEqual(rc, 0, out)
        self.assertIn("box2 could not be told", out)
        self.assertIn("python3 ~/dgx-spark-qwen38/pair.py remove box1", out)
        self.assertEqual(self.b1.pair()["peers"], [])

    def test_two_boxes_of_one_name_are_told_to_name_one(self):
        rc, out = self.run_tool(self.b2, "name", "box1")
        self.assertEqual(rc, 0, out)
        rc, out = self.add()
        self.assertNotEqual(rc, 0)
        self.assertIn("both boxes are named box1", out)
        self.assertIn("pair.py name", out)
        self.assertEqual(self.b1.pair()["peers"], [])
        self.assertEqual(self.b2.pair()["peers"], [])

    def test_an_older_proxy_on_the_other_box_is_said(self):
        (self.b2.cfg / "keepalive-proxy.py").write_text("PAIR_PORT = 30002\n")     # a proxy without pairs over TLS
        rc, out = self.add()
        self.assertNotEqual(rc, 0)
        self.assertIn("on box2:", out)
        self.assertIn("older than pairs over TLS: update Alzeo", out)
        self.assertEqual(self.b1.pair()["peers"], [])

    def test_pairing_again_writes_new_files_and_never_over_one_in_use(self):
        rc, out = self.add()
        self.assertEqual(rc, 0, out)
        first = self.b1.pair()["peers"][0]
        rc, out = self.add()
        self.assertEqual(rc, 0, out)
        self.assertIn("box2 had a pair with box1 under that name or certificate: replaced", out)
        again = self.b1.pair()["peers"]
        self.assertEqual(len(again), 1)
        self.assertNotEqual((again[0]["key_file"], again[0]["cert_file"]), (first["key_file"], first["cert_file"]))
        self.assertEqual(self.b1.peer_files(), sorted(pathlib.Path(again[0][k]).name for k in ("cert_file", "key_file")))
        self.assertEqual(len(self.b2.peer_files()), 2, "box2 too: one key, one certificate")
        st, body = self.call(self.b1, "qwen3.8-flash-next-fp4")
        self.assertEqual(st, 200, body[:300])

    def test_status_says_what_each_box_serves_and_never_a_key(self):
        rc, out = self.run_tool(self.b1, "status")
        self.assertEqual(rc, 0, out)
        self.assertIn("this box is alone", out)
        self.assertEqual(self.add()[0], 0)
        rc, out = self.run_tool(self.b1, "status")
        self.assertEqual(rc, 0, out)
        self.assertIn("this box: box1, certificate sha256 ", out)
        self.assertIn(f"pair port {self.b1.pair_port}", out)
        self.assertIn(f"box2 (https://127.0.0.1:{self.b2.pair_port}, certificate ", out)
        self.assertIn("answers over TLS, serves qwen3.8-flash-next-fp4", out)
        self.assertNotIn(KEY_2, out)
        key_file = self.b1.cfg / self.b1.pair()["peers"][0]["key_file"]
        key_file.write_text("k-stale\n")
        rc, out = self.run_tool(self.b1, "status")
        self.assertIn("does not answer: HTTP 401 on /v1/models: it does not take the key this box holds for it", out)

    def test_a_name_is_given_before_pairing_only(self):
        self.assertEqual(self.add()[0], 0)
        rc, out = self.run_tool(self.b1, "name", "other")
        self.assertNotEqual(rc, 0)
        self.assertIn("remove the pair, name this box, then pair again", out)
        rc, out = self.run_tool(self.b1, "name", "two words")
        self.assertNotEqual(rc, 0)

    def test_the_identity_is_made_once_and_its_key_is_this_users_alone(self):
        rc, out = self.run_tool(self.b1, "identity")
        self.assertEqual(rc, 0, out)
        rc2, out2 = self.run_tool(self.b1, "identity")
        self.assertEqual(out, out2, "made once")
        ident = self.b1.cfg / "pair/identity"
        self.assertEqual(stat.S_IMODE(ident.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((ident / "key.pem").stat().st_mode), 0o600)
        fp = self.sha(ident / "cert.pem")
        self.assertIn(":".join(fp[i:i + 2] for i in range(0, 64, 2)).upper(), out)
        self.assertNotIn("PRIVATE KEY", out)

    def test_an_address_is_an_address_alone(self):
        for args in (("127.0.0.1:30002",), ("https://box2",), ("user@box2",), ("--", "-oProxyCommand=x"),
                     ("127.0.0.1", "--ssh=-oProxyCommand=x"), ("127.0.0.1", "--ssh", "box 2"),
                     ("127.0.0.1", "--back", "10.0.0.1:30002"), ("fe80::1%eth0",)):
            with self.subTest(args=args):
                rc, out = self.run_tool(self.b1, "add", *args)
                self.assertNotEqual(rc, 0)
                self.assertIn("pair.py:", out)
        self.assertFalse(self.ssh_log.exists() and self.ssh_log.read_text().strip(), "ssh never ran")
        self.assertEqual(self.b1.pair()["peers"], [])


if __name__ == "__main__":
    unittest.main()
