"""extras/cve-2026-98432 (decision 1040, 2026-11-04): the table drops IPv4 TCP with IP options and nothing else, the unit
loads the file the script installs, before the network, the sysctls are the two of the mitigation, and the script
refuses what it does not know and dry-runs without changing anything (sudo, sysctl, systemctl and nft stubbed: the test
never touches the machine)."""
import os
import pathlib
import shutil
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
        self.assertIn("ExecStop=-/usr/sbin/nft delete table ip cve_2026_98432", u)
        self.assertIn("Before=network-pre.target", u)
        self.assertIn("WantedBy=multi-user.target", u)

    def test_the_unit_follows_nftables_service_and_a_table_already_gone(self):
        # nftables.service's stock config flushes the whole ruleset, this table included: at a boot that ran it after the
        # unit, or at its reload, the table went and the unit still said active; and a stop with the table gone failed
        # (the 4th review of v1.43.0, 2026-11-04)
        u = (D / "cve-2026-98432-nft.service").read_text().splitlines()
        for line in ("After=nftables.service", "PartOf=nftables.service", "ReloadPropagatedFrom=nftables.service",
                     "ExecReload=/usr/sbin/nft -f /etc/nft-cve-2026-98432.nft",
                     "ExecStop=-/usr/sbin/nft delete table ip cve_2026_98432"):
            self.assertIn(line, u)

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

    # ---- the script run whole in a root of its own: sudo runs what it is given, nft, systemctl and sysctl say what the
    # test sets, and every call is written down
    def _box(self, nft_parses=True, mptcp="1", sudo=True):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="cve-extra-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        root, bin_, log = tmp / "root", tmp / "bin", tmp / "calls.log"
        for d in ("etc/systemd/system", "etc/sysctl.d"):
            (root / d).mkdir(parents=True)
        bin_.mkdir()
        stubs = {
            "sudo": 'echo "sudo $*" >> "$LOG"\n[ "$1" = -n ] && exit $SUDO_N\nexec "$@"',
            "nft": ('echo "nft $*" >> "$LOG"\n[ "$1" = -c ] && exit $NFT_C\n'
                    'case "$*" in "list table ip cve_2026_98432") echo "ip hdrlength != 5 counter packets 3 bytes 9 drop";; '
                    'esac\nexit 0'),
            "systemctl": ('echo "systemctl $*" >> "$LOG"\ncase "$1" in is-enabled) echo enabled;; is-active) echo active;; '
                          'esac\nexit 0'),
            "sysctl": ('echo "sysctl $*" >> "$LOG"\ncase "$*" in "-n net.mptcp.enabled") echo "$MPTCP";; '
                       '"-n net.ipv4.tcp_migrate_req") echo 0;; esac\nexit 0'),
        }
        for name, body in stubs.items():
            f = bin_ / name
            f.write_text(f"#!/bin/sh\n{body}\n")
            f.chmod(0o755)
        env = dict(os.environ, PATH=f"{bin_}:/usr/bin:/bin", CVE_EXTRA_ROOT=str(root), LOG=str(log),
                   NFT_C="0" if nft_parses else "1", MPTCP=mptcp, SUDO_N="0" if sudo else "1")

        def run(*args):
            p = subprocess.run(["bash", str(D / "setup.sh"), *args], env=env, capture_output=True, text=True,
                               timeout=60)
            calls = log.read_text() if log.exists() else ""
            log.unlink(missing_ok=True)
            return p, calls
        return root, env, run

    def test_a_table_that_does_not_parse_leaves_the_installed_one_alone(self):
        # it was installed over /etc's before nft -c read it: "nothing loaded", and the next boot loaded nothing (the 4th
        # review of v1.43.0, 2026-11-04)
        root, env, run = self._box(nft_parses=False)
        good = root / "etc/nft-cve-2026-98432.nft"
        good.write_text("the table an earlier run installed\n")
        p, calls = run()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("does not parse", p.stderr)
        self.assertEqual(good.read_text(), "the table an earlier run installed\n")
        self.assertNotIn("sudo install", calls)

    def test_a_run_again_loads_the_table_it_installs(self):
        # enable --now leaves a oneshot that already ran as it was: the table changed, the old one stayed until a reboot
        # (the 4th review of v1.43.0, 2026-11-04)
        root, env, run = self._box(mptcp="0")
        p, _ = run()
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        p, calls = run()                                                        # the unit active since the first
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertIn("systemctl reload-or-restart cve-2026-98432-nft.service", calls)
        self.assertEqual((root / "etc/nft-cve-2026-98432.nft").read_text(), (D / "cve-2026-98432.nft").read_text())

    def test_the_check_without_sudo_says_the_table_was_not_read(self):
        # without sudo at hand it read the sysctls and the unit, not the table, and said 0: in place (the 4th review of
        # v1.43.0, 2026-11-04)
        root, env, run = self._box(mptcp="0", sudo=False)
        p, _ = run("--check")
        self.assertEqual(p.returncode, 2, p.stdout)
        self.assertIn("nft table: not read", p.stdout)

    def test_the_uninstall_puts_mptcp_back_as_it_was(self):
        # it forced MPTCP on, whatever it was before the install (the 4th review of v1.43.0, 2026-11-04)
        for before in ("0", "1"):
            with self.subTest(before=before):
                root, env, run = self._box(mptcp=before)
                p, _ = run()
                self.assertEqual((root / "etc/nft-cve-2026-98432.before").read_text(), f"net.mptcp.enabled={before}\n")
                env["MPTCP"] = "0"                                            # the mitigation's own value now
                p, _ = run()                                                    # a run again keeps the first
                self.assertEqual((root / "etc/nft-cve-2026-98432.before").read_text(), f"net.mptcp.enabled={before}\n")
                p, calls = run("--uninstall")
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertIn(f"sysctl -w net.mptcp.enabled={before}", calls)
                self.assertFalse((root / "etc/nft-cve-2026-98432.before").exists())


if __name__ == "__main__":
    unittest.main()
