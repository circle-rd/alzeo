#!/usr/bin/env python3
"""Load a lane from a terminal, through the cockpit's one server-owned mechanism.

The page and this script both ask the cockpit for the same plan and start the same
``lane_load`` action (dashboard/cockpit.py). The server runs the whole chain, so a
sign-out, a closed tab or a dropped session cannot leave the box between two steps.
Nothing here stops, starts or switches a lane itself.

    ./lane-load.py stock
    ./lane-load.py --plan image-h3
    ./lane-load.py --url http://127.0.0.1:30090 flash-uncensored

The cockpit must be running. The API key is read from ~/.config/qwen38/api-key, or
COCKPIT_API_KEY. The login is the page's: the key, a session cookie, then a CSRF
token for the action (dashboard/cockpit.py).
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_URL = os.environ.get("COCKPIT_URL", "http://127.0.0.1:30090")
KEY_FILE = Path(os.environ.get("COCKPIT_KEY_FILE", Path.home() / ".config/qwen38/api-key"))


class Client:
    def __init__(self, base: str, key: str):
        self.base = base.rstrip("/")
        self.key = key
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def request(self, method: str, path: str, body: dict | None = None, timeout: float = 30.0):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with self.opener.open(req, timeout=timeout) as r:
                raw = r.read()
                return r.status, json.loads(raw or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return e.code, {"error": raw.decode("utf-8", "replace")[:200]}

    def login(self):
        code, out = self.request("POST", "/api/login", {"key": self.key})
        if code != 200:
            raise SystemExit(f"login refused ({code}): {out.get('error', out)}")
        code, out = self.request("POST", "/api/csrf", {})
        if code != 200 or not out.get("token"):
            raise SystemExit(f"could not read the CSRF token ({code}): {out.get('error', out)}")
        return out["token"]

    def plan(self, target: str):
        code, out = self.request("GET", "/api/lane-plan?" + urllib.parse.urlencode({"target": target}))
        if code != 200:
            raise SystemExit(f"the cockpit did not give the plan ({code}): {out.get('error', out)}")
        return out.get("steps") or []

    def start(self, target: str, csrf: str):
        code, out = self.request("POST", "/api/action",
                                 {"name": "lane_load", "params": {"target": target}, "csrf": csrf})
        if code != 202:
            raise SystemExit(f"the load did not start ({code}): {out.get('message') or out.get('error') or out}")
        return out["job"]

    def job(self, job_id: str):
        code, out = self.request("GET", "/api/state")
        if code != 200:
            return None, []
        j = out.get("job", {}).get("data") or {}
        cur = j.get("current")
        if cur and cur.get("id") == job_id:
            return "running", cur.get("lines") or []
        for row in j.get("recent") or []:
            if row.get("id") == job_id:
                return row.get("status"), row.get("lines") or []
        return None, []


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Load a lane through the cockpit's server-owned lane load.")
    ap.add_argument("target", help="a switch target (stock, uncensored, image, video, flash-uncensored, ...)")
    ap.add_argument("--plan", action="store_true", help="print the steps without starting anything")
    ap.add_argument("--url", default=DEFAULT_URL, help=f"the cockpit's base URL (default {DEFAULT_URL})")
    ap.add_argument("--key-file", default=str(KEY_FILE), help=f"the API key file (default {KEY_FILE})")
    args = ap.parse_args(argv)

    key = os.environ.get("COCKPIT_API_KEY", "")
    if not key:
        try:
            key = Path(args.key_file).read_text().strip()
        except OSError as e:
            raise SystemExit(f"no API key: {e} (or set COCKPIT_API_KEY)")
    client = Client(args.url, key)
    csrf = client.login()
    steps = client.plan(args.target)
    if not steps:
        print(f"{args.target}: already serving.")
        return 0
    print(f"{args.target}: {len(steps)} step(s)")
    for i, step in enumerate(steps, 1):
        print(f"  {i}. {step.get('title')}")
    if args.plan:
        return 0

    job_id = client.start(args.target, csrf)
    print(f"job {job_id} started; the cockpit owns it now (Ctrl-C leaves it running)")
    seen = 0
    while True:
        status, lines = client.job(job_id)
        for line in lines[seen:]:
            print(line)
        seen = max(seen, len(lines))
        if status in ("done", "failed"):
            if status == "failed" and not lines:
                print("the load failed; the cockpit's dock and the lane's journal say where.", file=sys.stderr)
            return 0 if status == "done" else 1
        if status is None:
            print("the cockpit no longer knows this job (restarted?): its outcome is unknown", file=sys.stderr)
            return 2
        try:
            time.sleep(1.0)
        except KeyboardInterrupt:
            print("\nthe job keeps running in the cockpit; this script only stops watching.", file=sys.stderr)
            return 130


if __name__ == "__main__":
    raise SystemExit(main())