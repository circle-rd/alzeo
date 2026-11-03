#!/usr/bin/env python3
"""pair.py, the tool that pairs two boxes (proxy role 10, discussion #45, decisions 971-978).

Two boxes on this machine, each a home of its own (its API key, its installed proxy), a fake engine and its
proxy run in this process, its client port and its pair port each on a held port. A fake ssh stands for SSH
between them: it runs the command it is given as the other box, with that box's home. `pair.py add` pairs
both boxes in one command over that SSH, each with a certificate of its own, the pair's only credential:
no box ever holds the other's API key, and nothing secret is on a command line or on the screen; what each
box writes is what its proxy takes, and a request then goes from one box to the other over TLS; a check that
fails, or an SSH cut once the other box wrote, keeps nothing on either box; `remove` unpairs both; an SSH
that does not reach the other box, two boxes of one name and an older proxy are said with what to do."""
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
request = sys.stdin.read()
r = subprocess.run(["sh", "-c", command], env=env, input=request, capture_output=True, text=True)
if box.get("cut") and box["cut"] in request:
    sys.stderr.write("Connection to the box closed by remote host.\r\n")
    sys.exit(255)                       # its half ran, wrote, and its answer never came back
sys.stdout.write(r.stdout)
sys.stderr.write(r.stderr)
sys.exit(r.returncode)
'''


def fake_engine(name, model):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        seen: list = []
        loading = False                     # SGLang while it loads: a 503 to everything

        def log_message(self, *a):
            pass

        def loading_503(self):
            if not Handler.loading:
                return False
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return True

        def reply(self, obj):
            out = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def do_GET(self):
            if self.loading_503():
                return
            if self.path.startswith("/v1/models"):
                return self.reply({"object": "list", "data": [{"id": model, "object": "model"}]})
            if self.path.startswith(("/server_info", "/get_server_info")):
                return self.reply({"max_total_num_tokens": 900000})
            return self.reply({})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if self.loading_503():
                return
            Handler.seen.append(body.get("model"))
            self.reply({"id": "c", "object": "chat.completion", "model": model,
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": f"from {name}"},
                                     "finish_reason": "stop"}]})
    return Handler


class Engine(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class Box:
    """A box on this machine: its home, its fake engine, its proxy in this process on held ports."""

    def __init__(self, test, root, name, key, model, env=None):
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
        os.environ.update(env or {})
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

    def holds(self, secret):
        """Whether any file this box keeps for its pair holds `secret`."""
        return any(secret in f.read_text(errors="replace") for f in (self.cfg / "pair").rglob("*") if f.is_file()) \
            or secret in ((self.cfg / "pair.json").read_text() if (self.cfg / "pair.json").exists() else "")


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

    def call_until_200(self, box, model, within=15.0):
        """A call made again until it answers 200: a proxy reads pair.json again within 2 s of a change."""
        deadline = time.monotonic() + within
        while True:
            st, body = self.call(box, model)
            if st == 200 or time.monotonic() > deadline:
                return st, body
            time.sleep(0.5)

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
        self.assertIn("neither box holds the other's API key", out)
        p1, p2 = self.b1.pair(), self.b2.pair()
        self.assertEqual([(q["name"], q["proxy"], q["ssh"]) for q in p1["peers"]],
                         [("box2", f"https://127.0.0.1:{self.b2.pair_port}", "box2")])
        self.assertEqual([(q["name"], q["proxy"]) for q in p2["peers"]], [("box1", f"https://127.0.0.1:{self.b1.pair_port}")])
        # each pins the other's certificate, made on its own box; neither holds the other's API key (decision 978)
        for here, there, p in ((self.b1, self.b2, p1), (self.b2, self.b1, p2)):
            entry = p["peers"][0]
            self.assertEqual(entry["sha256"].replace(":", "").lower(), self.sha(there.cfg / "pair/identity/cert.pem"))
            self.assertNotIn("key_file", entry)
            self.assertFalse(here.holds(there.key), f"{here.name} holds the API key of {there.name}")
            for f in (here.cfg / entry["cert_file"], here.cfg / "pair.json", here.cfg / "pair/identity/key.pem"):
                self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600, f)
            self.assertEqual(stat.S_IMODE((here.cfg / "pair").stat().st_mode), 0o700)
        # what each wrote, its proxy takes: a request goes from one box to the other over TLS, both ways, and the
        # engine of the box that serves it is admitted with that box's own key
        st, body = self.call(self.b1, "qwen3.8-flash-next-fp4")
        self.assertEqual((st, b"from box2" in body), (200, True), body[:300])
        st, body = self.call(self.b2, "qwen3.8-27b")
        self.assertEqual((st, b"from box1" in body), (200, True), body[:300])

    def test_nothing_secret_is_on_a_command_line_or_on_the_screen(self):
        rc, out = self.add()
        self.assertEqual(rc, 0, out)
        calls = [json.loads(line) for line in self.ssh_log.read_text().splitlines()]
        self.assertGreaterEqual(len(calls), 2, "accept, then check")
        for argv in calls:
            joined = " ".join(argv)
            for secret in (KEY_1, KEY_2, "PRIVATE KEY", "BEGIN CERTIFICATE"):
                self.assertNotIn(secret, joined)
            for option in ("BatchMode=yes", "StrictHostKeyChecking=yes"):
                self.assertIn(option, joined, "never a password asked, never a host key taken unseen")
            self.assertIn("-T", argv, "no terminal asked for")
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

    def test_an_ssh_cut_once_the_other_box_wrote_keeps_nothing_on_either_box(self):
        # the other box's half ran and wrote its side, and its answer never came back (SSH cut, a timeout,
        # Ctrl-C): it was left paired with a box that knew nothing of it (found in review, 2026-11-03)
        self.ssh_boxes({"box2": {"home": str(self.b2.home), "pair_port": self.b2.pair_port, "from": "127.0.0.1",
                                 "cut": '"do": "accept"'}})
        rc, out = self.add()
        self.assertNotEqual(rc, 0)
        self.assertIn("cannot run a command on box2", out)
        for b in (self.b1, self.b2):
            self.assertEqual(b.pair(), {"name": b.name, "peers": []}, f"{b.name} keeps no pair")
            self.assertEqual(b.peer_files(), [])

    def test_a_check_that_fails_keeps_nothing_on_either_box(self):
        # box2 says a pair port nothing listens on: box1 cannot reach it
        self.ssh_boxes({"box2": {"home": str(self.b2.home), "pair_port": localports.held(self), "from": "127.0.0.1"}})
        rc, out = self.add()
        self.assertNotEqual(rc, 0)
        self.assertIn("this box cannot reach the pair port of box2", out)
        self.assertIn("nothing listens on its pair port", out)
        self.assertIn("Both boxes are as they were before this run", out)
        for b in (self.b1, self.b2):
            self.assertEqual(b.pair(), {"name": b.name, "peers": []}, "its name kept, its pair gone")
            self.assertEqual(b.peer_files(), [], "no key or certificate of the other box left")

    def test_a_failed_add_on_a_working_pair_leaves_it_working_both_ways(self):
        # decision 988: add run again on a pair that worked, and its answer cut once the other box wrote, left the
        # pair one way (this box's entry forgotten there, the other kept here; review, 2026-11-03)
        rc, out = self.add()
        self.assertEqual(rc, 0, out)
        self.assertEqual(list((self.b1.cfg / "pair").glob("undo-*")), [], "no record left here once it worked")
        self.ssh_boxes({"box2": {"home": str(self.b2.home), "pair_port": self.b2.pair_port, "from": "127.0.0.1",
                                 "cut": '"do": "accept"'}})
        rc, out = self.add()
        self.assertNotEqual(rc, 0)
        self.assertEqual(([p["name"] for p in self.b1.pair()["peers"]], [p["name"] for p in self.b2.pair()["peers"]]),
                         (["box2"], ["box1"]), "the pair as it was, on both boxes")
        self.assertEqual((len(self.b1.peer_files()), len(self.b2.peer_files())), (1, 1), "each certificate back")
        for here, model in ((self.b1, "qwen3.8-flash-next-fp4"), (self.b2, "qwen3.8-27b")):
            st, body = self.call_until_200(here, model)
            self.assertEqual(st, 200, body[:300])

    def test_a_check_that_fails_on_a_working_pair_puts_both_boxes_back(self):
        rc, out = self.add()
        self.assertEqual(rc, 0, out)
        # run again with box2 now reaching box1 at an address where box1 does not listen: the check fails
        self.ssh_boxes({"box2": {"home": str(self.b2.home), "pair_port": self.b2.pair_port, "from": "127.0.0.2"}})
        rc, out = self.add()
        self.assertNotEqual(rc, 0)
        self.assertIn("Both boxes are as they were before this run", out)
        self.ssh_boxes({})
        for here, model in ((self.b1, "qwen3.8-flash-next-fp4"), (self.b2, "qwen3.8-27b")):
            st, body = self.call_until_200(here, model)
            self.assertEqual(st, 200, body[:300])

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

    def test_a_box_renamed_since_it_was_paired_is_forgotten_by_its_certificate(self):
        # remove asked the other box to forget a name it did not know, and said it had (review, 2026-11-03)
        rc, out = self.add()
        self.assertEqual(rc, 0, out)
        conf = self.b1.pair()
        (self.b1.cfg / "pair.json").write_text(json.dumps(dict(conf, name="box1-renamed")))
        rc, out = self.run_tool(self.b1, "remove", "box2")
        self.assertEqual(rc, 0, out)
        self.assertIn("box2 forgot this box too", out)
        self.assertEqual(self.b2.pair()["peers"], [], "forgotten by its certificate")

    def test_remove_says_when_the_other_box_had_nothing_to_forget(self):
        rc, out = self.add()
        self.assertEqual(rc, 0, out)
        (self.b2.cfg / "pair.json").write_text(json.dumps({"name": "box2", "peers": []}))
        rc, out = self.run_tool(self.b1, "remove", "box2")
        self.assertEqual(rc, 0, out)
        self.assertIn("box2 had no pair with this box: nothing to forget there", out)
        self.assertNotIn("forgot this box too", out)

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
        self.assertNotEqual(again[0]["cert_file"], first["cert_file"])
        self.assertNotIn("key_file", again[0], "a pair holds no key of the other box")
        self.assertEqual(self.b1.peer_files(), [pathlib.Path(again[0]["cert_file"]).name], "the earlier one dropped")
        self.assertEqual(len(self.b2.peer_files()), 1, "box2 too: one certificate, no key")
        st, body = self.call(self.b1, "qwen3.8-flash-next-fp4")
        self.assertEqual(st, 200, body[:300])

    def test_a_box_whose_engine_does_not_answer_is_paired_all_the_same(self):
        # a lane loading, stopped or switching on either box: its proxy answers 503 on its pair port, which proves
        # the pair both ways; add undid the pair on both boxes, a working one too (found in review, 2026-11-03)
        self.b2.engine_handler.loading = True
        rc, out = self.add()
        self.assertEqual(rc, 0, out)
        self.assertIn("serves nothing now", out)
        rc, out = self.add()
        self.assertEqual(rc, 0, out)
        self.assertEqual(([p["name"] for p in self.b1.pair()["peers"]], [p["name"] for p in self.b2.pair()["peers"]]),
                         (["box2"], ["box1"]), "kept on both boxes")
        self.b2.engine_handler.loading = False
        deadline, st, body = time.monotonic() + 15, None, b""
        while time.monotonic() < deadline:
            st, body = self.call(self.b1, "qwen3.8-flash-next-fp4")
            if st == 200:
                break
            time.sleep(0.5)
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
        # box2 forgets box1 behind its back: box1's status says why box2 does not answer
        rc, out = self.run_tool(self.b2, "remove", "box1")
        self.assertEqual(rc, 0, out)
        deadline = time.monotonic() + 8                  # box2's proxy reads its pair.json within seconds
        while True:
            rc, out = self.run_tool(self.b1, "status")
            if "does not answer" in out or time.monotonic() > deadline:
                break
            time.sleep(0.5)
        self.assertIn("box2 (https://127.0.0.1:", out)
        self.assertIn("does not answer", out)

    def test_a_box_with_an_identity_wall_is_paired_all_the_same(self):
        # a box that names its clients (QWEN38_CLIENT_KEYS_FILE) took the other box's request for an unknown
        # client before (found in review, 2026-11-03): its certificate admits it, the wall stays for clients
        keys = self.root / "keys.json"
        keys.write_text(json.dumps({"k-client-of-box3": "laptop"}))
        b3 = Box(self, self.root, "box3", "k-box3-0a1", "qwen3.8-flash-next-fp4", env={"QWEN38_CLIENT_KEYS_FILE": str(keys)})
        rc, out = self.run_tool(b3, "name", "box3")
        self.assertEqual(rc, 0, out)
        self.ssh_boxes({"box3": {"home": str(b3.home), "pair_port": b3.pair_port, "from": "127.0.0.1"}})
        rc, out = self.run_tool(self.b1, "add", "127.0.0.1", "--ssh", "box3")
        self.assertEqual(rc, 0, out)
        st, body = self.call(self.b1, "qwen3.8-flash-next-fp4")
        self.assertEqual((st, b"from box3" in body), (200, True), body[:300])

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


class TheUninstallNamesThePair(unittest.TestCase):
    """uninstall.sh --list names pair/ when there is one: the other box keeps this one until it is told. The
    block runs here as uninstall.sh has it."""

    def inventory(self, state):
        text = (REPO / "uninstall.sh").read_text()
        start = text.index('if [ -d "$CONFIG_DIR" ]; then\n  echo "  config    $CONFIG_DIR')
        block = text[start:text.index("\nfi\n", start) + 4]
        cfg = pathlib.Path(tempfile.mkdtemp(prefix="uninst-pair-"))
        self.addCleanup(shutil.rmtree, cfg, True)
        (cfg / "api-key").write_text("k\n")
        if state in ("paired", "certificate", "named"):
            (cfg / "pair/identity").mkdir(parents=True)
        if state == "paired":
            (cfg / "pair.json").write_text(json.dumps({"name": "a", "peers": [{"name": "b", "proxy": "https://b:30002",
                                                                              "cert_file": "pair/b@1.crt", "sha256": "0" * 64}]}, indent=2))
        if state == "named":
            (cfg / "pair.json").write_text(json.dumps({"name": "a", "peers": []}, indent=2))
        script = 'set -euo pipefail\ndir_size(){ echo 4K; }\n' + f'CONFIG_DIR="{cfg}"\n' + block + "echo INVENTORY-END\n"
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        return cfg, r.stdout + r.stderr

    def test_a_paired_box_is_told_to_unpair_first(self):
        cfg, out = self.inventory("paired")
        self.assertIn(f"  pair      {cfg}/pair.json (this box is paired: run pair.py remove first, so that the other "
                      "box forgets this one)", out)
        self.assertIn("INVENTORY-END", out)

    def test_a_box_that_kept_its_certificate_is_told_what_it_is(self):
        # pair.py name, identity or remove leave pair/ without a pair: no word of unpairing (review, 2026-11-03)
        for state in ("certificate", "named"):
            with self.subTest(state=state):
                cfg, out = self.inventory(state)
                self.assertIn(f"  pair      {cfg}/pair (this box's certificate for a pair, made by pair.py)", out)
                self.assertNotIn("run pair.py remove", out)

    def test_a_box_never_paired_gets_no_such_line(self):
        _, out = self.inventory("never")
        self.assertNotIn("  pair  ", out)
        self.assertIn("INVENTORY-END", out)


if __name__ == "__main__":
    unittest.main()
