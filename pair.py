#!/usr/bin/env python3
"""Two boxes behind one endpoint: pair this box with another (discussion #45, proxy role 10).

    python3 ~/dgx-spark-qwen38/pair.py add 192.168.100.2   # pair this box with the box at that address
    python3 ~/dgx-spark-qwen38/pair.py status              # this box's pair, and what each box serves now
    python3 ~/dgx-spark-qwen38/pair.py remove spark2       # unpair: both boxes alone again
    python3 ~/dgx-spark-qwen38/pair.py name spark1         # the name this box takes in a pair, before pairing
    python3 ~/dgx-spark-qwen38/pair.py identity            # this box's certificate and its fingerprint

`add` works over SSH without a password, as step 4 of NVIDIA's connect-two-sparks playbook sets it up
between two Sparks, and one command pairs both boxes (decisions 973-974): each box makes a certificate of
its own once, the two boxes swap their certificates over SSH, each pins the other's in its pair.json, and
from then on the two proxies talk over TLS both ways on port 30002. The certificate is the pair's only
credential (decision 978): no box ever holds the other's API key, which is also its cockpit's login, and
the pair port takes the generation routes, the model list and the summary of the tile only. Either box is then an entry: a request
that names a model of one of this repo's lanes goes to the box that serves it. Both proxies read pair.json
again within seconds: nothing restarts. `add` then checks both ways, and keeps nothing on either box when
one way fails (--no-check keeps the pair anyway). Nothing secret is ever on a command line, where ps shows
it to every user, nor on the screen.

This file runs on the other box too: `add` and `remove` send it there over SSH on standard input and run
its remote() there, which leaves no file behind. Each box checks what it writes with its own installed
proxy (~/.config/qwen38/keepalive-proxy.py), the code that reads pair.json.
"""
import argparse
import base64
import contextlib
import errno
import fcntl
import hashlib
import importlib.util
import ipaddress
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PAIR_FILE = Path(os.path.expanduser(os.environ.get("QWEN38_PAIR_FILE") or "~/.config/qwen38/pair.json"))
CONFIG = PAIR_FILE.parent
PAIR_DIR = CONFIG / "pair"
IDENTITY = PAIR_DIR / "identity"
PLAYBOOK = "https://github.com/NVIDIA/dgx-spark-playbooks/tree/main/nvidia/connect-two-sparks"
# what the other box runs, its argument the same every time and no secret: this file and the request come
# on standard input, so neither the keys nor the code are ever on a command line there
# what the other box says reaches this terminal: never a control character, an escape included
SAFE_TEXT = re.compile(r"[\x00-\x1f\x7f]")
REMOTE_BOOT = ("import sys,base64,json;g={'__name__':'pair_remote'};"
               "exec(compile(base64.b64decode(sys.stdin.readline()),'pair.py','exec'),g);"
               "g['remote'](json.loads(sys.stdin.readline()))")


def proxy_module():
    """This box's installed proxy, whose reading of pair.json is the one that counts: what this file
    writes, that code takes. An older proxy, without pairs over TLS, is said as such."""
    path = CONFIG / "keepalive-proxy.py"
    if not path.is_file():
        raise SystemExit(f"pair.py: {path} is missing: install Alzeo on this box first (curl -fsSL https://get.alzeo.ai | bash)")
    spec = importlib.util.spec_from_file_location("keepalive_proxy_for_pair", path)
    mod = importlib.util.module_from_spec(spec)
    argv, sys.argv = sys.argv, ["keepalive-proxy.py"]
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.argv = argv
    if not hasattr(mod, "_pair_tls"):
        raise SystemExit(f"pair.py: the proxy of this box ({path}) is older than pairs over TLS: update Alzeo "
                         f"on this box (curl -fsSL https://get.alzeo.ai | bash)")
    mod.log = lambda msg: None          # its journal lines are the service's, not this tool's
    return mod


@contextlib.contextmanager
def pair_lock(within=30.0):
    """One pair.py at a time changes this box's pair, here or run from the other box."""
    PAIR_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(PAIR_DIR, 0o700)
    fd = os.open(PAIR_DIR / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + within
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EACCES) or time.monotonic() > deadline:
                    raise SystemExit("pair.py: another pair.py is changing this box's pair: try again in a moment")
                time.sleep(0.2)
        yield
    finally:
        os.close(fd)


def read_conf():
    try:
        conf = json.loads(PAIR_FILE.read_text())
    except FileNotFoundError:
        return {"peers": []}
    except ValueError as e:
        raise SystemExit(f"pair.py: {PAIR_FILE} is not JSON ({e}): fix it or remove it")
    if not isinstance(conf, dict) or not isinstance(conf.get("peers", []), list):
        raise SystemExit(f"pair.py: {PAIR_FILE} is not a pair.json (an object whose \"peers\" is a list)")
    conf.setdefault("peers", [])
    return conf


def my_name(mod, conf):
    try:
        return mod._pair_parse(json.dumps(dict(conf, peers=[])))["name"]
    except ValueError as e:
        raise SystemExit(f"pair.py: {PAIR_FILE}: {e}")


def write_atomic(path, text, mode):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_pair(mod, conf):
    """pair.json written whole (one rename), once this box's proxy takes it, certificates included: the
    single moment a pair changes. Without a peer the box is alone again: pair.json is removed, or keeps
    only the name it was given (pair.py name), which the proxy reads as a box alone."""
    if not conf["peers"]:
        if conf.get("name") is None:
            PAIR_FILE.unlink(missing_ok=True)
        else:
            write_atomic(PAIR_FILE, json.dumps({"name": conf["name"], "peers": []}, indent=2) + "\n", 0o600)
        return
    text = json.dumps(conf, indent=2) + "\n"
    try:
        parsed = mod._pair_parse(text)
        mod._pair_tls(parsed)
    except (ValueError, OSError) as e:
        raise SystemExit(f"pair.py: this box's proxy would not use that pair.json: {e}")
    write_atomic(PAIR_FILE, text, 0o600)


def fingerprint(mod, pem, where):
    return hashlib.sha256(mod._pem_der(pem, where)).hexdigest()


def colons(hexdigest):
    return ":".join(hexdigest[i:i + 2] for i in range(0, len(hexdigest), 2)).upper()


def ensure_identity(name):
    """This box's certificate (PEM), made once: EC P-256, 100 years, its key readable by this user alone.
    Made in a directory of its own and renamed into place whole, so a certificate never meets another
    key. Called under pair_lock()."""
    cert, key = IDENTITY / "cert.pem", IDENTITY / "key.pem"
    if cert.is_file() and key.is_file():
        return cert.read_text()
    if IDENTITY.exists():
        raise SystemExit(f"pair.py: {IDENTITY} holds no certificate and key: remove that directory and run again")
    tmp = Path(tempfile.mkdtemp(prefix=".identity.", dir=PAIR_DIR))
    try:
        old = os.umask(0o077)
        try:
            r = subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
                                "-nodes", "-days", "36500", "-subj", f"/CN=alzeo-pair-{name}",
                                "-addext", "keyUsage=critical,digitalSignature,keyCertSign",
                                "-keyout", str(tmp / "key.pem"), "-out", str(tmp / "cert.pem")],
                               capture_output=True, text=True, timeout=120)
        except FileNotFoundError:
            raise SystemExit("pair.py: openssl is not installed on this box: sudo apt install openssl")
        finally:
            os.umask(old)
        if r.returncode != 0 or not (tmp / "cert.pem").is_file() or not (tmp / "key.pem").is_file():
            raise SystemExit(f"pair.py: openssl could not make this box's certificate: {r.stderr.strip()[-300:]}")
        os.chmod(tmp / "key.pem", 0o600)
        os.chmod(tmp / "cert.pem", 0o600)
        os.rename(tmp, IDENTITY)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return cert.read_text()


def write_peer_cert(name, pem):
    """The other box's certificate, in a file of a new name: a file the pair uses now is never written
    over (the proxy reads a peer's certificate when it reads pair.json, never again)."""
    crt_rel = f"pair/{name}@{secrets.token_hex(4)}.crt"
    write_atomic(CONFIG / crt_rel, pem, 0o600)
    return crt_rel


def drop_files(entries, keep=()):
    """The certificate files of these pair.json entries, those under pair/ and in no entry kept (an entry
    edited by hand into something else is passed over)."""
    def cert(q):
        rel = q.get("cert_file") if isinstance(q, dict) else None
        return (CONFIG / os.path.expanduser(rel)).resolve() if isinstance(rel, str) and rel else None
    kept = {cert(q) for q in keep} - {None}
    for f in map(cert, entries):
        if f is not None and f.parent == PAIR_DIR.resolve() and f not in kept:
            f.unlink(missing_ok=True)


def url_host(addr):
    """An address as the host of a URL: an IPv6 one in brackets."""
    return f"[{addr}]" if ":" in addr and not addr.startswith("[") else addr


def with_peer(mod, conf, name, proxy, pem, ssh=None):
    """conf with `name` as a peer (in place of any entry of that name or of that certificate), its
    certificate written, the entries it replaces returned for drop_files() once the new pair.json is in
    place."""
    if not isinstance(name, str) or not mod.PAIR_NAME.fullmatch(name):
        raise SystemExit(f"pair.py: a box's name is 1 to 40 letters, digits, dots, dashes or underscores, "
                         f"not {SAFE_TEXT.sub('?', repr(name))[:60]}")
    fp = fingerprint(mod, pem if isinstance(pem, str) else "", f"the certificate of {name}")
    gone = [q for q in conf["peers"] if not isinstance(q, dict) or q.get("name") == name
            or str(q.get("sha256", "")).replace(":", "").lower() == fp]
    crt_rel = write_peer_cert(name, pem)
    entry = {"name": name, "proxy": proxy, "cert_file": crt_rel, "sha256": colons(fp)}
    if ssh:
        entry["ssh"] = ssh
    new = dict(conf, peers=[q for q in conf["peers"] if q not in gone] + [entry])
    return new, gone, entry


def without_peer(conf, name, sha=None):
    """conf without the peer of that name, or of that certificate: a box renamed since the pair was made is
    known on the other box by its certificate alone (found in review, 2026-11-03)."""
    def norm(v):
        return str(v or "").replace(":", "").lower()
    gone = [q for q in conf["peers"] if isinstance(q, dict) and (q.get("name") == name
                                                                 or (sha and norm(q.get("sha256")) == sha))]
    return dict(conf, peers=[q for q in conf["peers"] if q not in gone]), gone


def my_sha(mod):
    """This box's certificate's fingerprint, or None before it has one."""
    try:
        return fingerprint(mod, (IDENTITY / "cert.pem").read_text(), "this certificate")
    except (OSError, SystemExit):
        return None


def reach(mod, name, within):
    """What the peer `name` of this box's pair serves, read as the proxy reads it (its pair port, over
    TLS both ways, with its key), tried for `within` seconds: its proxy opens that port within 2 s of
    its pair.json. (models, None), or (None, why)."""
    try:
        conf = mod._pair_parse(PAIR_FILE.read_text())
        mod._pair_tls(conf)
    except (OSError, ValueError) as e:
        return None, f"this box's pair.json is not used now: {e}"
    peer = next((p for p in conf["peers"] if p["name"] == name), None)
    if peer is None:
        return None, f"{name} is not in this box's pair now"
    deadline = time.monotonic() + within
    while True:
        models = mod._peer_models_read(peer)
        row = mod._PAIR_MODELS.get(name) or {}
        if not row.get("error"):
            return list(models), None
        if row.get("answered"):
            # its proxy answered over TLS both ways, its engine did not (a lane loading, stopped, switching): the
            # pair works, and that box serves nothing now (found in review, 2026-11-03: add undid a working pair)
            return [], None
        if time.monotonic() >= deadline:
            return None, row.get("error")
        time.sleep(1)


# ---- the other box's half, run there by remote() ---------------------------------------------------
def ssh_client_address():
    """The address this box's SSH server sees the other box come from: on a cable or a LAN, the one it
    reaches that box at."""
    addr = (os.environ.get("SSH_CLIENT") or "").split(" ")[0]
    if not addr:
        raise SystemExit("cannot tell the address this box reaches the other one at (no SSH_CLIENT): "
                         "pass --back with it")
    if "%" in addr:
        raise SystemExit(f"the other box comes from a link-local address ({addr}): pass --back with one it "
                         f"can be reached at")
    return addr


def do_accept(req):
    """The other box names itself and asks to pair: its peer entry written here, and this box's name,
    certificate and pair port in return."""
    mod = proxy_module()
    with pair_lock():
        conf = read_conf()
        me = my_name(mod, conf)
        if req["name"] == me:
            raise SystemExit(f"both boxes are named {me}: name one of them otherwise first "
                             f"(python3 ~/dgx-spark-qwen38/pair.py name <another name>)")
        back = address(req["back"], "--back") if req.get("back") else ssh_client_address()
        pem = ensure_identity(me)
        new, gone, _ = with_peer(mod, conf, req["name"], f"https://{url_host(back)}:{int(req['port'])}",
                                 req["cert"], ssh=back)
        try:
            write_pair(mod, new)
        except BaseException:
            drop_files(new["peers"][-1:], keep=conf["peers"])
            raise
        drop_files(gone, keep=new["peers"])
    return {"name": me, "cert": pem, "port": mod.PAIR_PORT, "back": back,
            "replaced": [q.get("name") for q in gone if isinstance(q, dict)]}


def do_check(req):
    mod = proxy_module()
    models, why = reach(mod, req["name"], min(float(req.get("within", 10)), 60.0))
    return {"models": models} if why is None else {"error": why}


def do_forget(req):
    mod = proxy_module()
    sha = str(req.get("sha256") or "").replace(":", "").lower() or None
    gone = unpair_here(mod, req["name"], sha)
    return {"forgot": bool(gone), "alone": not read_conf()["peers"]}


def remote(req):
    """The other box's half (run there over SSH by ssh_call): one JSON line on standard output."""
    try:
        out = {"accept": do_accept, "check": do_check, "forget": do_forget}[req["do"]](req)
    except SystemExit as e:
        out = {"error": str(e.code if e.code is not None else "").removeprefix("pair.py: ")[:800]}
    except Exception as e:
        out = {"error": f"{type(e).__name__}: {e}"[:800]}
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()


# ---- this box's half --------------------------------------------------------------------------------
def ssh_call(dest, do, req, timeout=120):
    """remote() run on `dest` over SSH: this file and the request on standard input, the answer one JSON
    line on standard output. A box that SSH does not reach without a password is said with what to do."""
    stdin = base64.b64encode(Path(__file__).read_bytes()).decode() + "\n" + json.dumps(dict(req, do=do)) + "\n"
    # host keys checked whatever ~/.ssh/config says (accept-new would send this box's certificate to whoever
    # answers at that address the first time), and no terminal asked for (found in review, 2026-11-03)
    cmd = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10",
           "-o", "ServerAliveInterval=15", "--", dest, "python3 -I -c " + shlex.quote(REMOTE_BOOT)]
    try:
        r = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise SystemExit("pair.py: ssh is not installed on this box: sudo apt install openssh-client")
    except subprocess.TimeoutExpired:
        raise SystemExit(f"pair.py: {dest} did not answer over SSH within {timeout} s")
    reply = None
    for line in reversed(r.stdout.splitlines()):    # a login script may print before it
        try:
            reply = json.loads(line)
        except ValueError:
            continue
        if isinstance(reply, dict):
            break
        reply = None
    err = SAFE_TEXT.sub("?", (r.stderr.strip().splitlines() or [""])[-1])[:300]
    if reply is None and r.returncode == 255:
        if "Host key verification failed" in r.stderr:
            raise SystemExit(f"pair.py: this box does not know the SSH host key of {dest} yet: run ssh {dest} once "
                             f"and answer yes once you have checked it, then pair.py add again")
        raise SystemExit(f"pair.py: this box cannot run a command on {dest} over SSH without a password ({err}). "
                         f"Set SSH up between the two boxes as step 4 of NVIDIA's playbook does ({PLAYBOOK}): "
                         f"ssh {dest} true must then work from this box without a question. When ssh reaches "
                         f"that box by another name, give it: --ssh user@host")
    if reply is None:
        if r.returncode == 127:
            raise SystemExit(f"pair.py: python3 is missing on {dest}")
        raise SystemExit(f"pair.py: {dest} gave no answer over SSH (exit {r.returncode}): {err}")
    if "error" in reply:
        raise SystemExit(f"pair.py: on {dest}: {SAFE_TEXT.sub('?', str(reply['error']))[:800]}")
    return reply


def forget_there(dest, me, sha=None, answer=None):
    """The other box forgets this one (by its name or its certificate), or the reason it could not be told;
    never raises. Its answer goes into `answer` when one is given."""
    try:
        r = ssh_call(dest, "forget", {"name": me, "sha256": sha}, timeout=60)
        if answer is not None:
            answer.append(r)
        return None
    except SystemExit as e:
        return str(e.code if e.code is not None else "")


def address(addr, what):
    """An address alone: a name, an IPv4 or an IPv6 one; no scheme, port, path or user."""
    addr = (addr or "").strip().strip("[]")
    if not addr or addr.startswith("-") or any(c in addr for c in "/@ \t") or "%" in addr:
        raise SystemExit(f"pair.py: {what} is an address alone (192.168.100.2, spark2, spark2.tailnet.ts.net), "
                         f"not {addr!r}: a user for SSH goes with --ssh user@host")
    if ":" in addr:
        try:
            ipaddress.IPv6Address(addr)
        except ValueError:
            raise SystemExit(f"pair.py: {what} is an address alone, without a port: not {addr!r}")
    return addr


def unpair_here(mod, name, sha=None):
    with pair_lock():
        conf = read_conf()
        new, gone = without_peer(conf, name, sha)
        if gone:
            write_pair(mod, new)
            drop_files(gone, keep=new["peers"])
    return gone


def cmd_add(args, mod):
    host = address(args.host, "the other box's address")
    back = address(args.back, "--back") if args.back is not None else None
    dest = (args.ssh or host).strip()
    if not dest or dest.startswith("-") or any(c.isspace() for c in dest):
        raise SystemExit("pair.py: --ssh is how ssh reaches the other box: host, user@host or a name of ~/.ssh/config")
    with pair_lock():
        conf = read_conf()
        me = my_name(mod, conf)
        pem = ensure_identity(me)
    mine = fingerprint(mod, pem, "this certificate")
    print(f"pairing {me} (this box) with the box at {host}, over SSH ({dest})...")
    # 1. the other box writes its side and says who it is: its name, certificate and pair port. A failure
    # once it may have written (an error after its write, SSH cut, a timeout, Ctrl-C) is undone there too:
    # forget is nothing on a box that wrote nothing (found in review, 2026-11-03)
    try:
        r = ssh_call(dest, "accept", {"name": me, "port": mod.PAIR_PORT, "back": back, "cert": pem})
    except BaseException:
        forget_there(dest, me, mine)
        raise
    name, their_pem = r.get("name"), r.get("cert")
    try:
        if not isinstance(name, str) or not mod.PAIR_NAME.fullmatch(name) or name == me:
            raise ValueError(f"its name {SAFE_TEXT.sub('?', repr(name))[:60]}")
        port = int(r.get("port"))
        if not 0 < port < 65536:
            raise ValueError(f"its port {port}")
        fingerprint(mod, their_pem if isinstance(their_pem, str) else "", "its certificate")
        their_back = address(r.get("back") or "", "the address it reaches this box at")
        replaced = [n for n in (r.get("replaced") or []) if isinstance(n, str) and mod.PAIR_NAME.fullmatch(n)]
    except (ValueError, TypeError, SystemExit) as e:
        why = forget_there(dest, me, mine)
        said = SAFE_TEXT.sub("?", str(getattr(e, "code", None) or e))[:200]
        raise SystemExit(f"pair.py: {dest} answered with something this box does not take ({said}): nothing was kept"
                         + (f" here, and it could not be told ({why}): run pair.py remove {me} there" if why else ""))
    # 2. this box writes its side
    try:
        with pair_lock():
            conf = read_conf()
            new, gone, entry = with_peer(mod, conf, name, f"https://{url_host(host)}:{port}", their_pem, ssh=dest)
            try:
                write_pair(mod, new)
            except BaseException:
                drop_files([entry], keep=conf["peers"])
                raise
            drop_files(gone, keep=new["peers"])
    except BaseException:
        forget_there(dest, me, mine)
        raise
    # 3. both ways, as the proxies will: this box to its pair port, and that box to this one's
    if not args.no_check:
        there, why_there = reach(mod, name, 10)
        back_said = f"https://{url_host(their_back)}:{mod.PAIR_PORT}"
        here, why_here = None, None
        if why_there is None:
            try:
                got = ssh_call(dest, "check", {"name": me, "within": 10}).get("models") or []
                here = [m for m in got if isinstance(m, str) and mod._PAIR_SAFE_ID.fullmatch(m)]
            except SystemExit as e:
                why_here = str(e.code if e.code is not None else "").removeprefix(f"pair.py: on {dest}: ")
        if why_there is not None or why_here is not None:
            # undone on both boxes whatever fails on the way, and never in place of why the pair failed
            # (found in review, 2026-11-03)
            try:
                unpair_here(mod, name)
                undone = None
            except BaseException as e:
                undone = SAFE_TEXT.sub("?", str(getattr(e, "code", None) or e))[:200]
            why = forget_there(dest, me, mine)
            if why is None and undone is None:
                kept = "Nothing was kept on either box"
            else:
                kept = ("Nothing was kept here" if undone is None else
                        f"This box keeps it ({undone}): run pair.py remove {name} here")
                if why is not None:
                    kept += f", and {name} could not be told ({why}): run pair.py remove {me} there"
            if why_there is not None:
                raise SystemExit(f"pair.py: this box cannot reach the pair port of {name} at https://{url_host(host)}:{port}: "
                                 f"{why_there}. {kept}. Check that both boxes run this version of Alzeo, that no "
                                 f"firewall stops {port}/tcp between them, and that {host} is the address of {name} on "
                                 f"a link both boxes share (--no-check keeps a pair anyway).")
            raise SystemExit(f"pair.py: {name} cannot reach the pair port of this box at {back_said}: {why_here}. "
                             f"{kept}. Check that no firewall stops {mod.PAIR_PORT}/tcp toward this box, and give "
                             f"this box's address as {name} reaches it with --back (--no-check keeps a pair anyway).")
    print(f"{me} and {name} are a pair: each box now sends a request for a model the other serves to that box.")
    if not args.no_check:
        print(f"  {me} -> {name} (https://{url_host(host)}:{port}): answers over TLS, serves {', '.join(there) or 'nothing now'}")
        print(f"  {name} -> {me} ({back_said}): answers over TLS, serves {', '.join(here or []) or 'nothing now'}")
    print(f"  certificates: {me} {colons(fingerprint(mod, pem, 'this certificate'))[:23]}..., "
          f"{name} {colons(fingerprint(mod, their_pem, 'its certificate'))[:23]}... (pair.py identity on each box shows its own)")
    print("  neither box holds the other's API key: the certificates are the pair's only credential")
    if replaced:
        print(f"  {name} had a pair with {', '.join(replaced)} under that name or certificate: replaced")


def cmd_remove(args, mod):
    conf = read_conf()
    me = my_name(mod, conf)
    gone = unpair_here(mod, args.name)
    if not gone:
        raise SystemExit(f"pair.py: no peer named {args.name} in {PAIR_FILE}")
    print(f"{args.name} removed from this box's pair" + ("" if read_conf()["peers"] else ": this box is alone again"))
    dest = gone[0].get("ssh")
    if not dest:
        print(f"  on {args.name}, run: python3 ~/dgx-spark-qwen38/pair.py remove {me}")
        return
    said = []
    why = forget_there(dest, me, my_sha(mod), said)
    if why is None and said and said[0].get("forgot"):
        print(f"  {args.name} forgot this box too")
    elif why is None:
        # its answer was read: a remove there said nothing to forget when it knew this box under another name
        # (found in review, 2026-11-03)
        print(f"  {args.name} had no pair with this box: nothing to forget there")
    else:
        print(f"  {args.name} could not be told ({why}); this box refuses its requests now. On {args.name}, run: "
              f"python3 ~/dgx-spark-qwen38/pair.py remove {me}")


def cmd_name(args, mod):
    with pair_lock():
        conf = read_conf()
        if conf["peers"]:
            raise SystemExit(f"pair.py: this box is paired ({', '.join(str(p.get('name')) for p in conf['peers'])}), "
                             f"and the other box knows it as {my_name(mod, conf)}: remove the pair, name this box, "
                             f"then pair again")
        new = dict(conf, name=args.name)
        try:
            mod._pair_parse(json.dumps(new))
        except ValueError as e:
            raise SystemExit(f"pair.py: {e}")
        write_atomic(PAIR_FILE, json.dumps(new, indent=2) + "\n", 0o600)
    print(f"this box is {args.name} in a pair")


def cmd_identity(args, mod):
    with pair_lock():
        me = my_name(mod, read_conf())
        pem = ensure_identity(me)
    print(f"this box: {me}, certificate {IDENTITY / 'cert.pem'}")
    print(f"  sha256 {colons(fingerprint(mod, pem, 'this certificate'))}")


def cmd_status(args, mod):
    conf = read_conf()
    if not conf["peers"]:
        print("this box is alone (no pair): every request goes to its own engine")
        return
    try:
        parsed = mod._pair_parse(json.dumps(conf))
        mod._pair_tls(parsed)
    except (ValueError, OSError) as e:
        raise SystemExit(f"pair.py: this box's proxy does not use {PAIR_FILE}: {e}")
    print(f"this box: {parsed['name']}, certificate sha256 {colons(parsed['tls']['mine'])[:23]}..., "
          f"pair port {mod.PAIR_PORT}")
    for p in parsed["peers"]:
        models, why = reach(mod, p["name"], 0)
        said = f"answers over TLS, serves {', '.join(models) or 'nothing now'}" if why is None else f"does not answer: {why}"
        print(f"  {p['name']} ({p['proxy']}, certificate {colons(p['sha256'])[:23]}...): {said}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pair.py", description="Two boxes behind one endpoint (proxy role 10).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="pair this box with the box at HOST, both boxes in one command (SSH)")
    a.add_argument("host", help="the other box's address as this box reaches it: 192.168.100.2, spark2")
    a.add_argument("--ssh", help="how ssh reaches it when not as HOST: user@host, or a name of ~/.ssh/config")
    a.add_argument("--back", help="this box's address as the other box reaches it (by default, the one its "
                                  "SSH server sees this box come from)")
    a.add_argument("--no-check", action="store_true", help="keep the pair even if the boxes cannot reach each other now")
    r = sub.add_parser("remove", help="unpair: both boxes alone again")
    r.add_argument("name")
    n = sub.add_parser("name", help="the name this box takes in a pair (before pairing)")
    n.add_argument("name")
    sub.add_parser("status", help="this box's pair, and what each box serves now")
    sub.add_parser("identity", help="this box's certificate and its fingerprint")
    args = ap.parse_args(argv)
    mod = proxy_module()
    {"add": cmd_add, "remove": cmd_remove, "name": cmd_name, "status": cmd_status,
     "identity": cmd_identity}[args.cmd](args, mod)


if __name__ == "__main__":
    main()
