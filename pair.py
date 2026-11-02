#!/usr/bin/env python3
"""Two boxes behind one endpoint: say which box this one sends requests to (#45, proxy role 10).

On the box clients talk to (the entry), name the other box and give its key on standard input, never
on the command line, where ps shows it to every user:

    ssh spark2 cat .config/qwen38/api-key | python3 ~/dgx-spark-qwen38/pair.py add spark2 http://spark2:30001
    python3 ~/dgx-spark-qwen38/pair.py status        # this box's pair and what each peer serves now
    python3 ~/dgx-spark-qwen38/pair.py remove spark2 # this box alone again, as it was
    python3 ~/dgx-spark-qwen38/pair.py name spark1   # the name this box gives itself in the pair

The proxy (keepalive-proxy.py) reads ~/.config/qwen38/pair.json again within 2 s: nothing restarts.
From then on, a request that names a model of one of this repo's lanes goes to the box that serves it,
and any other name stays on this box. The other box needs nothing: a request sent on to it is served
there, or refused when it serves another model. The key is written to ~/.config/qwen38/pair/<name>.key
(mode 600) and goes only to that box's proxy. The address is the other box's proxy, http://host:30001,
over a link of your own (the ConnectX-7 cable, a tailnet): the traffic between the boxes is plain HTTP.
"""
import argparse
import getpass
import importlib.util
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent
PAIR_FILE = Path(os.path.expanduser(os.environ.get("QWEN38_PAIR_FILE") or "~/.config/qwen38/pair.json"))


def proxy_module():
    """keepalive-proxy.py itself, for its own reading of pair.json: what this writes, the proxy takes."""
    spec = importlib.util.spec_from_file_location("keepalive_proxy_for_pair", REPO / "keepalive-proxy.py")
    mod = importlib.util.module_from_spec(spec)
    argv, sys.argv = sys.argv, ["keepalive-proxy.py"]
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.argv = argv
    return mod


def read_pair():
    try:
        return json.loads(PAIR_FILE.read_text())
    except FileNotFoundError:
        return {"peers": []}


def write_atomic(path, text, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def peer_list(proxy, key, me):
    """The peer's own models, read as the proxy reads them (a hop: its own list, never its union)."""
    req = urllib.request.Request(proxy + "/v1/models", headers={"Authorization": f"Bearer {key}",
                                                                "X-Alzeo-Pair-Hop": me})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=10) as r:
        data = json.loads(r.read(1 << 20).decode())
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise ValueError("its /v1/models is not a list of models")
    return [m["id"] for m in data["data"] if isinstance(m, dict) and isinstance(m.get("id"), str)]


def checked(mod, conf):
    """conf as the proxy would take it, or SystemExit with the proxy's own reason."""
    try:
        return mod._pair_parse(json.dumps(conf))
    except ValueError as e:
        raise SystemExit(f"pair.py: {e}")


def cmd_add(args, mod):
    conf = read_pair()
    peers = [p for p in conf.get("peers", []) if p.get("name") != args.name]
    key_file = f"pair/{args.name}.key"
    new = dict(conf, peers=peers + [{"name": args.name, "proxy": args.proxy, "key_file": key_file}])
    parsed = checked(mod, new)
    if sys.stdin.isatty():
        key = getpass.getpass(f"the API key of {args.name} (not shown): ")
    else:
        key = sys.stdin.readline()
    key = key.strip()
    if not key or any(c.isspace() for c in key):
        raise SystemExit("pair.py: no key read on standard input (one line, the other box's ~/.config/qwen38/api-key)")
    proxy = next(p["proxy"] for p in parsed["peers"] if p["name"] == args.name)
    if not args.no_check:
        try:
            models = peer_list(proxy, key, parsed["name"])
        except urllib.error.HTTPError as e:
            raise SystemExit(f"pair.py: {proxy} answered HTTP {e.code} with this key: nothing was written "
                             f"(a 401 is a key it does not take; --no-check writes it anyway)")
        except Exception as e:
            raise SystemExit(f"pair.py: {proxy} does not answer ({str(getattr(e, 'reason', None) or e)[:120]}): "
                             f"nothing was written (--no-check writes it anyway)")
        print(f"{args.name} answers and serves: {', '.join(models) or 'nothing now'}")
    write_atomic(PAIR_FILE.parent / key_file, key + "\n", 0o600)
    os.chmod(PAIR_FILE.parent / "pair", 0o700)
    write_atomic(PAIR_FILE, json.dumps(new, indent=2) + "\n", 0o600)
    print(f"{PAIR_FILE}: {parsed['name']} sends each model {args.name} serves to {proxy} (the proxy reads it within 2 s)")


def cmd_remove(args, mod):
    conf = read_pair()
    peers = conf.get("peers", [])
    gone = [p for p in peers if p.get("name") == args.name]
    if not gone:
        raise SystemExit(f"pair.py: no peer named {args.name} in {PAIR_FILE}")
    rest = [p for p in peers if p.get("name") != args.name]
    for p in gone:
        kf = Path(os.path.join(PAIR_FILE.parent, os.path.expanduser(p.get("key_file", ""))))
        if kf.is_file() and kf.resolve().parent == (PAIR_FILE.parent / "pair").resolve():
            kf.unlink()
    if rest:
        new = dict(conf, peers=rest)
        checked(mod, new)
        write_atomic(PAIR_FILE, json.dumps(new, indent=2) + "\n", 0o600)
        print(f"{args.name} removed; the pair keeps {', '.join(p['name'] for p in rest)}")
    else:
        PAIR_FILE.unlink(missing_ok=True)
        print(f"{args.name} removed: this box is alone again ({PAIR_FILE} removed)")


def cmd_name(args, mod):
    conf = read_pair()
    new = dict(conf, name=args.name)
    checked(mod, new)
    write_atomic(PAIR_FILE, json.dumps(new, indent=2) + "\n", 0o600)
    print(f"this box is {args.name} in its pair")


def cmd_status(args, mod):
    if not PAIR_FILE.exists():
        print("this box is alone (no pair.json): every request goes to its own engine")
        return
    try:
        conf = mod._pair_parse(PAIR_FILE.read_text())
    except ValueError as e:
        raise SystemExit(f"pair.py: {PAIR_FILE} is not used by the proxy: {e}")
    print(f"this box: {conf['name']}")
    for p in conf["peers"]:
        try:
            key = Path(p["key_file"]).read_text().split("\n")[0].strip()
        except OSError:
            print(f"  {p['name']} ({p['proxy']}): its key file cannot be read ({p['key_file']})")
            continue
        try:
            models = peer_list(p["proxy"], key, conf["name"])
            print(f"  {p['name']} ({p['proxy']}): serves {', '.join(models) or 'nothing now'}")
        except urllib.error.HTTPError as e:
            print(f"  {p['name']} ({p['proxy']}): answers HTTP {e.code}")
        except Exception as e:
            print(f"  {p['name']} ({p['proxy']}): does not answer ({str(getattr(e, 'reason', None) or e)[:120]})")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pair.py", description="Two boxes behind one endpoint (proxy role 10).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="name the other box; its API key on standard input")
    a.add_argument("name")
    a.add_argument("proxy", help="the other box's proxy, http://host:30001")
    a.add_argument("--no-check", action="store_true", help="write it even if the other box does not answer now")
    r = sub.add_parser("remove", help="forget a peer; the last one leaves this box alone")
    r.add_argument("name")
    n = sub.add_parser("name", help="the name this box gives itself in the pair")
    n.add_argument("name")
    sub.add_parser("status", help="this box's pair, and what each peer serves now")
    args = ap.parse_args(argv)
    mod = proxy_module()
    {"add": cmd_add, "remove": cmd_remove, "name": cmd_name, "status": cmd_status}[args.cmd](args, mod)


if __name__ == "__main__":
    main()
