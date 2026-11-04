"""extras/cve-2026-98432 (decision 1040, 2026-11-04): the table drops IPv4 TCP with IP options and nothing else, the unit
loads the file the script installs, before the network, the sysctls are the two of the mitigation, and the script
refuses what it does not know and dry-runs without changing anything (sudo, sysctl, systemctl and nft stubbed: the test
never touches the machine)."""
import os
import pathlib
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
D = REPO / "extras/cve-2026-98432"


class TheCveExtra(unittest.TestCase):
    def test_the_table_drops_ipv4_tcp_with_ip_options_and_nothing_else(self):
        t = (D / "cve-2026-98432.nft").read_text()
        self.assertIn("table ip cve_2026_98432 {", t)
        self.assertIn("type filter hook prerouting priority -300; policy accept;", t)
        rules = [ln.strip() for ln in t.splitlines() if ln.strip().endswith("drop")]
        self.assertEqual(rules, ["ip protocol tcp ip hdrlength != 5 counter drop"])
        self.assertNotIn("flush", t, "a flush would take Docker's rules with it")

    def test_the_unit_loads_the_installed_table_before_the_network(self):
        u = (D / "cve-2026-98432-nft.service").read_text()
        self.assertIn("ExecStart=/usr/sbin/nft -f /etc/nft-cve-2026-98432.nft", u)
        self.assertIn("ExecStop=/usr/sbin/nft delete table ip cve_2026_98432", u)
        self.assertIn("Before=network-pre.target", u)
        self.assertIn("WantedBy=multi-user.target", u)

    def test_the_sysctls_are_the_mitigations_two(self):
        keys = {ln.split("=")[0].strip(): ln.split("=")[1].strip()
                for ln in (D / "99-cve-2026-98432.conf").read_text().splitlines() if "=" in ln and not ln.startswith("#")}
        self.assertEqual(keys, {"net.mptcp.enabled": "0", "net.ipv4.tcp_migrate_req": "0"})

    def test_the_script_installs_each_file_where_it_is_read(self):
        s = (D / "setup.sh").read_text()
        for src, dest in (("cve-2026-98432.nft", "/etc/nft-cve-2026-98432.nft"),
                          ("cve-2026-98432-nft.service", "/etc/systemd/system/cve-2026-98432-nft.service"),
                          ("99-cve-2026-98432.conf", "/etc/sysctl.d/99-cve-2026-98432.conf")):
            self.assertIn(src, s)
            self.assertIn(dest, s)
        self.assertNotIn("nftables.service restart", s)
        self.assertNotIn("systemctl enable nftables", s)

    def _run(self, *args):
        """The script with sudo, sysctl, systemctl and nft stubbed first on PATH: nothing reaches the machine."""
        with tempfile.TemporaryDirectory() as tmp:
            bin_ = pathlib.Path(tmp)
            for name, body in (("sudo", "exit 1"), ("nft", "exit 1"), ("systemctl", "exit 1"),
                               ("sysctl", 'echo 1')):
                f = bin_ / name
                f.write_text(f"#!/bin/sh\n{body}\n")
                f.chmod(0o755)
            env = dict(os.environ, PATH=f"{bin_}:/usr/bin:/bin")
            return subprocess.run(["bash", str(D / "setup.sh"), *args], env=env, capture_output=True, text=True,
                                  timeout=60)

    def test_an_unknown_flag_is_refused_before_anything(self):
        p = self._run("--bogus")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("unknown flag", p.stderr)

    def test_a_dry_run_shows_the_three_files_and_changes_nothing(self):
        p = self._run("--dry-run")
        self.assertEqual(p.returncode, 0, p.stderr)
        for dest in ("/etc/nft-cve-2026-98432.nft", "/etc/systemd/system/cve-2026-98432-nft.service",
                     "/etc/sysctl.d/99-cve-2026-98432.conf"):
            self.assertIn(dest, p.stdout)
        self.assertIn("ip hdrlength != 5", p.stdout)

    def test_the_check_fails_where_the_mitigation_is_not_in_place(self):
        p = self._run("--check")                           # sysctl says 1, no unit: not in place
        self.assertNotEqual(p.returncode, 0, p.stdout)


if __name__ == "__main__":
    unittest.main()
