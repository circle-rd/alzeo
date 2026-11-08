#!/usr/bin/env python3
"""seccomp-io-uring.json is Docker's own default seccomp profile, unchanged, with one rule more.

The Qwen3.8 Flash-Next FP4-Local lane's engine reads its n-gram table over io_uring, whose three system calls
Docker's default profile refuses, and its model card turns seccomp off for it. The lane's unit passes
this file instead (qwen38-flash-local.service.template says why): Docker 29.6.2's default profile,
moby's profiles/seccomp/default.json at moby 3d80467, a git blob of 13,470 bytes, with a rule that
allows io_uring_setup, io_uring_enter and io_uring_register, and nothing else. Taking that one rule
out must give Docker's file back, byte for byte."""
import hashlib
import json
import pathlib
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
FILTER = REPO / "seccomp-io-uring.json"
# git hash-object of moby's profiles/seccomp/default.json at 3d80467, the profile of Docker 29.6.2
DOCKER_BLOB = "ea5a494afb8d64898fa0f4f47ae0c4f5ba9cbbc9"
RULE = ('\t\t},\n\t\t{\n\t\t\t"names": [\n\t\t\t\t"io_uring_enter",\n\t\t\t\t"io_uring_register",\n'
        '\t\t\t\t"io_uring_setup"\n\t\t\t],\n\t\t\t"action": "SCMP_ACT_ALLOW"\n')
IO_URING = ["io_uring_enter", "io_uring_register", "io_uring_setup"]


class TheFilter(unittest.TestCase):
    def test_without_its_one_rule_it_is_dockers_profile_byte_for_byte(self):
        ours = FILTER.read_bytes()
        self.assertEqual(ours.count(RULE.encode()), 1, "the io_uring rule is not there as written")
        theirs = ours.replace(RULE.encode(), b"", 1)
        # Docker's file ends without a newline, this one with one
        self.assertTrue(theirs.endswith(b"}\n"))
        theirs = theirs[:-1]
        self.assertEqual(hashlib.sha1(b"blob %d\0" % len(theirs) + theirs).hexdigest(), DOCKER_BLOB)

    def test_its_rule_allows_the_three_io_uring_calls_and_nothing_more(self):
        profile = json.loads(FILTER.read_text())
        self.assertEqual(profile["defaultAction"], "SCMP_ACT_ERRNO")
        rules = [r for r in profile["syscalls"] if set(r["names"]) & set(IO_URING)]
        self.assertEqual(rules, [{"names": IO_URING, "action": "SCMP_ACT_ALLOW"}])
        # and the calls a namespace escape starts with stay under Docker's own conditions
        for name in ("unshare", "setns", "mount", "clone3"):
            plain = [r for r in profile["syscalls"] if name in r["names"] and r["action"] == "SCMP_ACT_ALLOW"
                     and not r.get("includes") and not r.get("args")]
            self.assertEqual(plain, [], f"{name} allowed with no condition")


if __name__ == "__main__":
    unittest.main(verbosity=2)
