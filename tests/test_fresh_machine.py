#!/usr/bin/env python3
"""A box fresh from its first-boot setup installs in one run: the docker group and the packages.

NVIDIA's first-boot setup puts the account it creates in adm, sudo, audio, dip, plugdev, users and
lpadmin, never docker (its oobe-admin, DGX OS 7.6.0 on an ASUS GX10), and the socket is root:docker
0660. The one-liner stopped at its first check on every new box, its fix telling a desktop user to
log out; past it, the image and video lanes stopped on python3-dev and ffmpeg, which DGX OS 7.6.0
has neither of (found on an ASUS GX10, 2026-10-20). lanes.sh puts both right where a script needs
them: docker_grant adds the account (said before sudo runs) at install.sh's step 1, after every
refusal that comes before it; docker_session runs a script again with the group when its terminal
predates it; docker_relaunch does that with the groups a new login has, the environment the
process started with (its unchanged values in the environment, never in a command line) and the
signals it ignored; host_packages
installs what a lane lacks while a terminal can give sudo its password, once per run.

Each case runs the real functions under bash with stand-ins for docker, id, getent, stat, sudo, sg
and env (which record what they were asked, sg the environment it got), and install.sh, get.sh and
uninstall.sh run whole up to a wall, so a relaunch that does not happen shows as the wall.
"""
import os
import pathlib
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import installer_wall as wall  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[1]
LANES = REPO / "lanes.sh"
DENIED = ("permission denied while trying to connect to the docker API at "
          "unix:///var/run/docker.sock")                   # Docker 29.6.2, the GX10, 2026-10-20
DENIED_OLD = ("permission denied while trying to connect to the Docker daemon socket at "
              "unix:///var/run/docker.sock: Get \"http://%2Fvar%2Frun%2Fdocker.sock/v1.47/info\": "
              "dial unix /var/run/docker.sock: connect: permission denied")
DOWN = ("failed to connect to the docker API at unix:///var/run/docker.sock; check if the path is "
        "correct and if the daemon is running: dial unix /var/run/docker.sock: connect: no such "
        "file or directory")                               # Docker 29.6.2, measured 2026-10-20
DOWN_OLD = ("Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
            "Is the docker daemon running?")
MEMINFO = "MemTotal:       127541200 kB\nMemAvailable:   121000000 kB\n"
PROBE = "sg [docker] [-c] [true]"  # docker_relaunch_ok's try of sg, as the stand-in records it


class Box:
    """Stand-ins for what the functions ask the machine, in a directory first on PATH.

    The account is alice (her group alice, gid 1000; docker's gid is 988). `in_session` puts 988 in
    this process's groups, `member` puts docker in the account's groups as a new login reads them,
    and a granted usermod adds it there. `id -gn` alone answers docker, as in a shell after
    `newgrp docker`; asked about alice, it answers her own group. sudo and sg print a marker on
    stdout when they run, so that the order of what was said around them can be read. sg records its
    argv, the environment it got and whether a terminal could ask through it, then exits 0: a relaunch
    ends the run there. `sg_asks` makes it ask the group's password instead, and fail, as the real one
    does for an account added to /etc/group alone."""

    def __init__(self, docker_err=DENIED, docker_rc=1, member=False, in_session=False,
                 sock_group="docker", sudo_n=1, usermod_rc=0, usermod_adds=True,
                 apt_rcs=(0,), docker_group=True, with_sg=True, sudo_refuses_env=False,
                 listed=True, apt_err="", apt_err_fr="", group_nameless=False, sg_asks=False,
                 with_setsid=True):
        self.d = wall._tmp("fresh-")
        self.bin = self.d / "bin"
        self.bin.mkdir()
        # the real tools the functions and the stand-ins use, and nothing else: no sg, sudo,
        # docker or apt-get of the machine can be reached from a case
        self.tools = self.d / "tools"
        self.tools.mkdir()
        for t in ("cat", "head", "sed", "basename", "dirname", "tr", "cut", "awk", "printenv", "grep", "xargs",
                  "mktemp", "rm") + (("setsid",) if with_setsid else ()):
            (self.tools / t).symlink_to(shutil.which(t))
        self.calls = self.d / "calls"
        self.calls.touch()
        (self.d / "docker.err").write_text(docker_err)
        (self.d / "nG").write_text("alice sudo docker" if member else "alice sudo")
        (self.d / "apt.rcs").write_text("\n".join(str(r) for r in apt_rcs) + "\n")
        groups = "1000 27 988" if in_session else "1000 27"
        rec = f'echo "$(basename "$0") $*" >> "{self.calls}"\n'
        stubs = {
            "docker": rec + f'[ "$1" = info ] || exit 0\ncat "{self.d}/docker.err" >&2\nexit {docker_rc}\n',
            "id": ('case "$1 ${2-}" in\n  "-u ") echo 1000 ;;\n  "-un ") echo alice ;;\n'
                   '  "-gn ") echo docker ;;\n'
                   + ('  "-gn alice") echo 1000; exit 1 ;;\n' if group_nameless else '  "-gn alice") echo alice ;;\n')
                   + f'  "-G ") echo "{groups}" ;;\n  "-nG alice") cat "{self.d}/nG" ;;\n'
                   '  *) exec /usr/bin/id "$@" ;;\nesac\n'),
            # the group's own member list, which sg reads: alice there once she is a member,
            # unless a directory service puts her in the group without it (listed=False)
            "getent": (('[ "$*" = "group docker" ] || exec /usr/bin/getent "$@"\n'
                        + (f'grep -qw docker "{self.d}/nG" && [ {int(listed)} -eq 1 ] && '
                           'echo docker:x:988:alice && exit 0\necho docker:x:988:\nexit 0\n'
                           if docker_group else 'exit 2\n'))),
            "stat": ('case "$*" in\n'
                     f'  "-L -c %G /run/docker.sock") echo {sock_group} ;;\n'
                     f'  "-L -c %U:%G %a /run/docker.sock") echo "root:{sock_group} 660" ;;\n'
                     '  *) exec /usr/bin/stat "$@" ;;\nesac\n'),
            "sudo": (rec + 'case "$*" in\n'
                     f'  "-n true") exit {sudo_n} ;;\n'
                     '  "usermod -aG docker alice")\n    echo "[sudo usermod]"\n'
                     + (f'    [ {usermod_rc} -eq 0 ] && echo "alice sudo docker" > "{self.d}/nG"\n'
                        if usermod_adds else "")
                     + f'    exit {usermod_rc} ;;\n'
                     + ('  DEBIAN_FRONTEND=*)\n    if [ "${LC_ALL:-}" = C ]; then echo "sudo: sorry, you are not allowed '
                        'to set the following environment variables: DEBIAN_FRONTEND" >&2\n'
                        '    else echo "sudo: désolé, vous n\'êtes pas autorisé à définir les variables '
                        'd\'environnement suivantes : DEBIAN_FRONTEND" >&2; fi\n    exit 1 ;;\n' if sudo_refuses_env else "")
                     + '  *apt-get*)\n    echo "[sudo apt-get]"\n'
                     f'    r=$(head -1 "{self.d}/apt.rcs"); sed -i 1d "{self.d}/apt.rcs"\n'
                     + (f'    if [ "${{r:-0}}" -ne 0 ]; then if [ "${{LC_ALL:-}}" = C ] || [ -z "{apt_err_fr}" ]; '
                        f'then echo "{apt_err}" >&2; else echo "{apt_err_fr}" >&2; fi; fi\n' if apt_err else '')
                     + '    exit "${r:-0}" ;;\n'
                     'esac\nexit 0\n'),
            # what runs `env` asks it, recorded, then the real one
            "env": f'echo "env $*" >> "{self.calls}"\nexec {shutil.which("env")} "$@"\n',
        }
        if with_sg:
            stubs["sg"] = (f'{{ printf "sg"; printf " [%s]" "$@"; echo; }} >> "{self.calls}"\n'
                           # a terminal it could ask a password through, or none, and its input
                           'if { : > /dev/tty; } 2>/dev/null; then t=tty; else t=none; fi\n'
                           'if [ -t 0 ]; then i=tty; else i=none; fi\n'
                           f'echo "$*: $t, stdin $i" >> "{self.d}/sg.tty"\n'
                           + ('echo "Password: Invalid password." >&2\nexit 1\n' if sg_asks else
                              f'echo "[sg]"\n{shutil.which("env")} > "{self.d}/sg.env"\nexit 0\n'))
        for name, body in stubs.items():
            f = self.bin / name
            f.write_text("#!/bin/bash\n" + body)
            f.chmod(0o755)

    def env(self, **extra):
        e = {"PATH": f"{self.bin}:{self.tools}", "HOME": str(self.d), "FOO": "a b c",
             "SECRET_TOKEN": "s3cr3t-value"}
        e.update(extra)
        return e

    def bash(self, body, **extra):
        """body after lanes.sh, under the installers' own set -euo pipefail and die."""
        script = ("set -euo pipefail\n"
                  "die(){ printf 'ERROR: %s\\n' \"$*\" >&2; exit 1; }\n"
                  f". '{LANES}'\n" + body)
        # a session of its own: no terminal to give sudo a password, wherever the suite runs
        r = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, timeout=60,
                           env=self.env(**extra), stdin=subprocess.DEVNULL, cwd=str(self.d),
                           start_new_session=True)
        return r.returncode, r.stdout, r.stderr

    def bash_in_terminal(self, body, **extra):
        """body as bash() runs it, in a terminal of its own (script), as an install from one runs; its
        stdout and stderr are the terminal's one output."""
        f = self.d / "body.sh"
        f.write_text("set -euo pipefail\n"
                     "die(){ printf 'ERROR: %s\\n' \"$*\" >&2; exit 1; }\n"
                     f". '{LANES}'\n" + body)
        r = subprocess.run([shutil.which("script"), "-qec", f"/bin/bash {f}", "/dev/null"],
                           capture_output=True, text=True, timeout=60, env=self.env(**extra),
                           stdin=subprocess.DEVNULL, cwd=str(self.d), start_new_session=True)
        return r.returncode, r.stdout.replace("\r", "")

    def called(self):
        return self.calls.read_text().splitlines()

    def sg_calls(self):
        """The relaunches: every sg call but docker_relaunch_ok's try."""
        return [c for c in self.called() if c.startswith("sg ") and c != PROBE]

    def sg_probes(self):
        return [c for c in self.called() if c == PROBE]

    def sg_ttys(self):
        p = self.d / "sg.tty"
        return p.read_text().splitlines() if p.exists() else []

    def env_calls(self):
        return [c for c in self.called() if c.startswith("env ")]

    def sudo_calls(self):
        return [c for c in self.called() if c.startswith("sudo ")]

    def sg_env(self):
        p = self.d / "sg.env"
        return dict(ln.split("=", 1) for ln in p.read_text().splitlines() if "=" in ln) if p.exists() else {}


GRANT = ('export LEAK=late; FOO=changed\n'
         'docker_grant bash /opt/q/install.sh --no-video\necho CONTINUED\n')
SESSION = 'docker_session bash /opt/q/switch-model.sh flash\necho CONTINUED\n'


class TheDockerGroup(unittest.TestCase):
    def test_docker_that_answers_changes_nothing(self):
        box = Box(docker_err="", docker_rc=0)
        rc, out, err = box.bash(GRANT)
        self.assertEqual(rc, 0, err)
        self.assertIn("CONTINUED", out)
        self.assertEqual(box.called(), ["docker info"])

    def test_an_account_outside_the_group_is_added_then_the_command_runs_again(self):
        box = Box()
        rc, out, err = box.bash(GRANT, TMPDIR="/var/tmp/alice")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("CONTINUED", out, "the run went on without the group")
        self.assertIn("sudo usermod -aG docker alice", box.sudo_calls())
        self.assertEqual(box.sg_probes(), [PROBE], "sg is tried once, whoever asks again")
        # said, with why, before sudo runs: on the one stream, the lines come before its marker
        self.assertLess(out.index("is not in the docker group"), out.index("[sudo usermod]"))
        self.assertLess(out.index("which makes it root on this machine"), out.index("[sudo usermod]"))
        self.assertLess(out.index("[sudo usermod]"), out.index("is in the docker group now"))
        self.assertLess(out.index("is in the docker group now"), out.index("[sg]"))
        # sg to docker, then to the account's own group (alice, not the docker this shell has),
        # with the command, and TMPDIR handed back past the setuid sg (glibc drops it there)
        sg, = box.sg_calls()
        self.assertTrue(sg.startswith("sg [docker] [-c] [exec "), sg)
        self.assertRegex(sg, r"/sg alice -c 'exec \S+/env TMPDIR=/var/tmp/alice bash /opt/q/install\.sh --no-video'\]$")
        env = box.sg_env()
        self.assertEqual(env.get("FOO"), "a b c", "a value changed after the start was not set back")
        self.assertNotIn("LEAK", env, "a variable exported after the start reached the relaunch")
        self.assertEqual(env.get("SECRET_TOKEN"), "s3cr3t-value")
        self.assertEqual(env.get("QWEN38_DOCKER_RELAUNCHED"), "granted")

    def test_the_starts_values_pass_in_the_environment_not_in_a_command_line(self):
        # a value the run did not change is never an argument; one it changed is set back as one
        box = Box()
        rc, out, err = box.bash(GRANT)
        self.assertEqual(rc, 0, err)
        argv = " ".join(box.env_calls() + box.sg_calls())
        self.assertNotIn("s3cr3t-value", argv, "a value of the environment showed in a command line")
        self.assertIn("-u LEAK", argv)
        self.assertIn("FOO=a b c", argv, "the value the script changed is the one set back")

    def test_the_older_dockers_wording_is_read_the_same(self):
        box = Box(docker_err=DENIED_OLD)
        rc, out, err = box.bash(GRANT)
        self.assertEqual(rc, 0, err)
        self.assertIn("sudo usermod -aG docker alice", box.sudo_calls())
        self.assertEqual(len(box.sg_calls()), 1)

    def test_a_member_whose_terminal_predates_the_group_is_not_added_again(self):
        box = Box(member=True)
        rc, out, err = box.bash(GRANT)
        self.assertEqual(rc, 0, err)
        self.assertEqual(box.sudo_calls(), [])
        self.assertIn("this terminal was opened before it joined", out)
        self.assertEqual(len(box.sg_calls()), 1)
        self.assertEqual(box.sg_env().get("QWEN38_DOCKER_RELAUNCHED"), "session")

    def test_a_daemon_that_is_not_running_is_said_and_nothing_is_changed(self):
        for text in (DOWN, DOWN_OLD):
            with self.subTest(text[:40]):
                box = Box(docker_err=text)
                rc, out, err = box.bash(GRANT)
                self.assertEqual(rc, 1)
                self.assertIn("its service is not running", err)
                self.assertIn("sudo systemctl enable --now docker", err)
                self.assertEqual(box.sudo_calls(), [])
                self.assertFalse(box.sg_calls())

    def test_a_daemon_out_of_reach_elsewhere_is_not_called_the_systems(self):
        # DOCKER_HOST at a socket of the account's own: the system's service is not the way out
        box = Box(docker_err="failed to connect to the docker API at unix:///run/user/1000/docker.sock; "
                             "check if the path is correct and if the daemon is running: dial unix: no such file")
        rc, out, err = box.bash(GRANT)
        self.assertEqual(rc, 1)
        self.assertIn("docker does not answer", err)
        self.assertNotIn("systemctl", err)

    def test_a_refusal_elsewhere_than_the_systems_socket_touches_no_group(self):
        box = Box(docker_err="permission denied while trying to connect to the docker API at "
                             "unix:///run/user/1000/docker.sock")
        rc, out, err = box.bash(GRANT)
        self.assertEqual(rc, 1)
        self.assertIn("docker does not answer", err)
        self.assertEqual(box.sudo_calls(), [])

    def test_a_socket_that_is_not_the_docker_groups_touches_no_group(self):
        box = Box(sock_group="root")
        rc, out, err = box.bash(GRANT)
        self.assertEqual(rc, 1)
        self.assertIn("which is not the docker group's", err)
        self.assertEqual(box.sudo_calls(), [])

    def test_a_refusal_with_the_group_in_hand_is_said_as_it_is(self):
        box = Box(member=True, in_session=True)
        rc, out, err = box.bash(GRANT)
        self.assertEqual(rc, 1)
        self.assertIn("although this process has the docker group", err)
        self.assertFalse(box.sg_calls())

    def test_a_run_that_uses_no_sudo_says_how_instead(self):
        box = Box()
        rc, out, err = box.bash("DOCKER_GRANT=no\n" + GRANT)
        self.assertEqual(rc, 1)
        self.assertIn("this run uses no sudo", err)
        self.assertIn("Run sudo usermod -aG docker alice, log out and back in", err)
        self.assertEqual(box.sudo_calls(), [])

    def test_sudo_refused_says_the_way_out_and_never_newgrp(self):
        box = Box(usermod_rc=1)
        rc, out, err = box.bash(GRANT)
        self.assertEqual(rc, 1)
        self.assertIn("Run sudo usermod -aG docker alice, log out and back in", err)
        self.assertNotIn("newgrp", out + err, "newgrp makes docker the shell's own group, and the units' Group=")
        self.assertFalse(box.sg_calls())

    def test_an_account_usermod_cannot_change_is_said(self):
        # an account of a directory service: usermod succeeds on nothing local
        box = Box(usermod_adds=False)
        rc, out, err = box.bash(GRANT)
        self.assertEqual(rc, 1)
        self.assertIn("still not in the docker group", err)
        self.assertFalse(box.sg_calls())

    def test_no_second_relaunch(self):
        # started again once already and still refused: the way out, not a loop
        box = Box(member=True)
        rc, out, err = box.bash(GRANT, QWEN38_DOCKER_RELAUNCHED="granted")
        self.assertEqual(rc, 1)
        self.assertIn("Log out and back in", err)
        self.assertFalse(box.sg_calls())

    def test_a_word_sg_would_read_as_its_own_is_never_passed(self):
        box = Box()
        rc, out, err = box.bash("docker_grant bash /opt/q/install.sh 'a b;touch x'\n")
        self.assertEqual(rc, 1)
        self.assertIn("this run cannot start again with it here. Log out and back in", err)
        # the account is added (its next login needs it), and the run does not say it goes on
        self.assertIn("sudo usermod -aG docker alice", box.sudo_calls())
        self.assertNotIn("this run goes on with it", out)
        self.assertFalse(box.sg_calls())

    def test_added_and_asked_a_password_by_sg_is_told_the_way_out(self):
        box = Box(sg_asks=True)
        rc, out, err = box.bash(GRANT)
        self.assertEqual(rc, 1)
        self.assertIn("[sudo usermod]", out)
        self.assertIn("is in the docker group now, and this run cannot start again with it here", err)
        self.assertNotIn("this run goes on with it", out)
        self.assertEqual(box.sg_probes(), [PROBE])
        self.assertFalse(box.sg_calls())

    def test_an_account_whose_own_group_has_no_name_is_told_the_way_out(self):
        # id answers a number and fails: sg takes no number, and the run may not end without a word
        box = Box(group_nameless=True)
        rc, out, err = box.bash(GRANT)
        self.assertEqual(rc, 1)
        self.assertIn("this run cannot start again with it here. Log out and back in", err)
        self.assertNotIn("this run goes on with it", out)
        self.assertFalse(box.sg_calls())

    def test_an_account_only_a_directory_service_puts_in_the_group_is_not_started_again(self):
        # in the group by id, not in the member list sg reads: sg would ask a group password
        box = Box(member=True, listed=False)
        rc, out, err = box.bash(GRANT)
        self.assertEqual(rc, 1)
        self.assertIn("Log out and back in", err)
        self.assertNotIn("going on with the group", out)
        self.assertFalse(box.sg_calls())

    def test_a_value_handed_back_that_sg_would_read_stops_rather_than_drops(self):
        box = Box(member=True)
        rc, out, err = box.bash(GRANT, TMPDIR="/tmp/a b")
        self.assertEqual(rc, 1)
        self.assertIn("Log out and back in", err)
        self.assertFalse(box.sg_calls())

    def test_without_sg_the_way_out_is_said(self):
        box = Box(member=True, with_sg=False)
        rc, out, err = box.bash(GRANT)
        self.assertEqual(rc, 1)
        self.assertIn("Log out and back in", err)

    def test_no_docker_command_is_left_to_the_callers_own_check(self):
        box = Box()
        (box.bin / "docker").unlink()
        rc, out, err = box.bash(GRANT)
        self.assertEqual(rc, 0, err)
        self.assertIn("CONTINUED", out)


class TheSignalsARunIgnored(unittest.TestCase):
    def test_a_run_under_nohup_is_relaunched_with_its_sighup_ignored(self):
        box = Box()
        rc, out, err = box.bash("trap '' HUP\n" + GRANT)
        self.assertEqual(rc, 0, err)
        self.assertIn("HUP", box.sg_env().get("QWEN38_SIG_IGNORED", "").split())

    def test_the_relaunch_says_exactly_what_the_run_ignored(self):
        # whatever the suite's own process ignores (nohup, a background job): read from /proc
        # here, it is what the relaunch must say, no more and no less
        box = Box()
        probe = ("m=$(awk '/^SigIgn:/ {print $2}' /proc/$$/status); w=''\n"
                 "for s in 1:HUP 2:INT 3:QUIT 20:TSTP 21:TTIN 22:TTOU; do\n"
                 "  [ $(( 0x$m >> (${s%%:*} - 1) & 1 )) -eq 0 ] || w=\"$w ${s#*:}\"\ndone\n"
                 "echo \"IGNORED=[${w# }]\"\n")
        rc, out, err = box.bash(probe + GRANT)
        self.assertEqual(rc, 0, err)
        said = out.split("IGNORED=[", 1)[1].split("]", 1)[0]
        self.assertEqual(box.sg_env().get("QWEN38_SIG_IGNORED"), said)

    def test_lanes_sh_ignores_them_again_in_the_run_it_starts(self):
        box = Box()
        rc, out, err = box.bash("awk '/^SigIgn:/ {print $2}' /proc/$$/status\n", QWEN38_SIG_IGNORED="HUP QUIT")
        self.assertEqual(rc, 0, err)
        mask = int(out.split()[-1], 16)
        self.assertTrue(mask & 1 and mask & 4, f"SIGHUP and SIGQUIT not ignored: {out}")
        base = box.bash("awk '/^SigIgn:/ {print $2}' /proc/$$/status\n")[1].split()[-1]
        rc, out, err = box.bash("awk '/^SigIgn:/ {print $2}' /proc/$$/status\n", QWEN38_SIG_IGNORED="KILL TERM")
        self.assertEqual(out.split()[-1], base, "a name outside the six was acted on")


class ATerminalOlderThanTheGroup(unittest.TestCase):
    def test_in_session_nothing_happens(self):
        box = Box(member=True, in_session=True)
        rc, out, err = box.bash(SESSION)
        self.assertEqual(rc, 0, err)
        self.assertIn("CONTINUED", out)
        self.assertEqual(box.called(), [])

    def test_outside_the_group_nothing_happens_here(self):
        box = Box(member=False)
        rc, out, err = box.bash(SESSION)
        self.assertEqual(rc, 0, err)
        self.assertIn("CONTINUED", out)
        self.assertEqual(box.called(), [])

    def test_a_member_outside_its_session_goes_on_with_the_group(self):
        box = Box(member=True)
        rc, out, err = box.bash(SESSION)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("CONTINUED", out)
        self.assertEqual(box.sg_probes(), [PROBE], "sg is tried once, whoever asks again")
        sg, = box.sg_calls()
        self.assertRegex(sg, r"/sg alice -c 'exec \S+/env bash /opt/q/switch-model\.sh flash'\]$")
        self.assertFalse(box.sudo_calls() + [c for c in box.called() if c.startswith("docker")],
                         "the session's relaunch needs neither sudo nor docker")

    def test_a_member_sg_would_ask_a_password_of_is_told_the_way_out(self):
        # added to /etc/group alone, by hand: sg takes the members from /etc/gshadow and asks the
        # group's password, and the run ended on "Invalid password."; the try says it first
        box = Box(member=True, sg_asks=True)
        rc, out, err = box.bash(SESSION)
        self.assertEqual(rc, 1)
        self.assertIn("this run cannot start again with it here. Log out and back in", err)
        self.assertNotIn("going on with the group", out)
        self.assertEqual(box.sg_probes(), [PROBE])
        self.assertFalse(box.sg_calls())

    def test_the_try_has_no_terminal_to_ask_a_password_on(self):
        # from a terminal, as an install runs: the try away from it (setsid) and from its input
        # (an sg that read a password from stdin without a terminal would wait there, its prompt
        # thrown away), the relaunch in it, once
        box = Box(member=True)
        rc, out = box.bash_in_terminal(SESSION)
        self.assertEqual(rc, 0, out)
        try_, relaunch = box.sg_ttys()
        self.assertEqual(try_, "docker -c true: none, stdin none")
        self.assertRegex(relaunch, r"^docker -c exec \S+/sg alice -c .*: tty, stdin tty$")

    def test_no_setsid_on_the_box_is_told_the_way_out(self):
        box = Box(member=True, with_setsid=False)
        rc, out, err = box.bash(SESSION)
        self.assertEqual(rc, 1)
        self.assertIn("this run cannot start again with it here. Log out and back in", err)
        self.assertFalse(box.sg_probes() + box.sg_calls())

    def test_no_docker_group_on_the_box_is_nothing_to_do_here(self):
        # a box whose Docker came without its group: no account is in it, nothing to relaunch
        box = Box(docker_group=False, member=False)
        rc, out, err = box.bash(SESSION)
        self.assertEqual(rc, 0, err)
        self.assertIn("CONTINUED", out)


class HostPackages(unittest.TestCase):
    def test_without_a_terminal_or_a_ticket_it_says_the_command(self):
        box = Box(sudo_n=1)
        rc, out, err = box.bash('host_packages "the video lane" ffmpeg python3-dev || echo REFUSED\n')
        self.assertEqual(rc, 0, err)
        self.assertIn("REFUSED", out)
        self.assertIn("run sudo apt-get install -y ffmpeg python3-dev yourself", err)
        self.assertNotIn("Installing", out, "a run that cannot install said it was installing")
        self.assertFalse([c for c in box.called() if "apt-get" in c])

    def test_with_a_ticket_it_installs_them_said_first(self):
        box = Box(sudo_n=0, apt_rcs=(0,))
        rc, out, err = box.bash('host_packages "the video lane" ffmpeg python3-dev && echo DONE\n')
        self.assertEqual(rc, 0, err)
        self.assertIn("DONE", out)
        self.assertLess(out.index("Installing ffmpeg python3-dev for the video lane, missing on this box"),
                        out.index("[sudo apt-get]"))
        apt = [c for c in box.called() if "apt-get" in c]
        self.assertEqual(apt, ["sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l apt-get "
                               "-o DPkg::Lock::Timeout=300 -q -y install ffmpeg python3-dev"])

    def test_a_sudo_rule_for_apt_get_alone_gets_it_without_the_variables(self):
        box = Box(sudo_n=0, sudo_refuses_env=True, apt_rcs=(0,))
        rc, out, err = box.bash('host_packages "the image lane" python3-dev && echo DONE\n')
        self.assertEqual(rc, 0, err)
        self.assertIn("DONE", out)
        self.assertEqual([c for c in box.called() if "apt-get" in c][-1],
                         "sudo apt-get -o DPkg::Lock::Timeout=300 -q -y install python3-dev")

    def test_lists_too_old_are_refreshed_once(self):
        box = Box(sudo_n=0, apt_rcs=(100, 0, 0))
        rc, out, err = box.bash('host_packages "the image lane" python3-dev && echo DONE\n')
        self.assertEqual(rc, 0, err)
        self.assertIn("DONE", out)
        verbs = [c.split()[-2] if c.endswith("python3-dev") else c.split()[-1]
                 for c in box.called() if "apt-get" in c]
        self.assertEqual(verbs, ["install", "update", "install"])

    def test_apt_failing_twice_says_the_command(self):
        box = Box(sudo_n=0, apt_rcs=(100, 100, 100))
        rc, out, err = box.bash('host_packages "the image lane" python3-dev || echo REFUSED\n')
        self.assertEqual(rc, 0, err)
        self.assertIn("REFUSED", out)
        self.assertIn("apt-get could not install python3-dev", err)

    def test_a_lock_held_five_minutes_is_said_not_waited_for_again(self):
        box = Box(sudo_n=0, apt_rcs=(100, 0, 0),
                  apt_err="E: Could not get lock /var/lib/dpkg/lock-frontend. It is held by process 1234 (unattended-upgr)")
        rc, out, err = box.bash('host_packages "the image lane" python3-dev || echo REFUSED\n')
        self.assertEqual(rc, 0, err)
        self.assertIn("REFUSED", out)
        self.assertIn("another package manager held apt's lock", err)
        self.assertEqual(len([c for c in box.called() if "apt-get" in c]), 1, "the lock was waited for again")

    def test_a_sudo_that_refuses_the_password_is_not_asked_again(self):
        box = Box(sudo_n=0, apt_rcs=(1, 0, 0), apt_err="sudo: 3 incorrect password attempts")
        rc, out, err = box.bash('host_packages "the image lane" python3-dev || echo REFUSED\n')
        self.assertEqual(rc, 0, err)
        self.assertIn("REFUSED", out)
        self.assertIn("sudo refused", err)
        self.assertEqual(len([c for c in box.called() if "apt-get" in c]), 1, "sudo was asked again")

    def test_sudo_in_french_is_read_as_well(self):
        # sudo speaks the account's language (this box: fr_FR); host_packages reads it in English
        box = Box(sudo_n=0, apt_rcs=(1, 0, 0), apt_err="sudo: 3 incorrect password attempts",
                  apt_err_fr="sudo: 3 saisies de mots de passe incorrectes")
        rc, out, err = box.bash('host_packages "the image lane" python3-dev || echo REFUSED\n', LANG="fr_FR.UTF-8")
        self.assertEqual(rc, 0, err)
        self.assertIn("REFUSED", out)
        self.assertIn("sudo refused", err)
        self.assertEqual(len([c for c in box.called() if "apt-get" in c]), 1, "sudo was asked again")

    def test_a_french_refusal_of_the_variables_is_retried_without_them(self):
        box = Box(sudo_n=0, sudo_refuses_env=True, apt_rcs=(0,))
        rc, out, err = box.bash('host_packages "the image lane" python3-dev && echo DONE\n', LANG="fr_FR.UTF-8")
        self.assertEqual(rc, 0, err)
        self.assertIn("DONE", out)

    def test_an_account_sudo_may_not_run_apt_for_is_not_asked_again(self):
        box = Box(sudo_n=0, apt_rcs=(1, 0, 0),
                  apt_err="Sorry, user alice is not allowed to execute '/usr/bin/apt-get install' as root on gx10.")
        rc, out, err = box.bash('host_packages "the image lane" python3-dev || echo REFUSED\n')
        self.assertEqual(rc, 0, err)
        self.assertIn("REFUSED", out)
        self.assertEqual(len([c for c in box.called() if "apt-get" in c]), 1)

    def test_once_per_run(self):
        # install.sh tried: the lanes' installers it calls do not wait on apt again
        box = Box(sudo_n=0, apt_rcs=(100, 100, 100, 0))
        rc, out, err = box.bash('host_packages "the image and video lanes" python3-dev || true\n'
                                'host_packages "the image lane" python3-dev || echo SKIPPED\n')
        self.assertEqual(rc, 0, err)
        self.assertIn("SKIPPED", out)
        self.assertEqual(len([c for c in box.called() if "apt-get" in c]), 3)

    def test_nothing_missing_nothing_asked(self):
        box = Box(sudo_n=0)
        rc, out, err = box.bash('host_packages "the image lane" && echo DONE\n')
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.strip(), "DONE")
        self.assertEqual(box.called(), [])


def block(text, start, end_line):
    """The lines of text from the one starting with `start` to the one holding `end_line`."""
    a = text.index(start)
    b = text.index("\n", text.index(end_line, a)) + 1
    return text[a:b]


class InstallShPutsThemInBeforeTheCockpitNeedsThem(unittest.TestCase):
    """The packages step of install.sh, run as written: python3 says where its headers are (a
    folder without them here), and PATH has no ffmpeg."""
    TEXT = (REPO / "install.sh").read_text()

    def step(self):
        return block(self.TEXT, "HOST_PKGS=()\n# --no-start and --no-service",
                     '|| host_packages "the image and video lanes" "${HOST_PKGS[@]}" || true')

    def run_step(self, want_image, want_video, headers, no_start=0, no_service=0):
        box = Box(sudo_n=0)
        inc = box.d / "include"
        inc.mkdir()
        if headers:
            (inc / "Python.h").write_text("/* */\n")
        (box.bin / "python3").write_text(f'#!/bin/bash\ncase "$*" in *sysconfig*) echo "{inc}" ;; esac\nexit 0\n')
        (box.bin / "python3").chmod(0o755)
        rc, out, err = box.bash(f"WANT_IMAGE={want_image}; WANT_VIDEO={want_video}; "
                                f"NO_START={no_start}; NO_SERVICE={no_service}\n" + self.step())
        self.assertEqual(rc, 0, err)
        return [c for c in box.called() if "apt-get" in c]

    def test_a_new_box_gets_both(self):
        apt = self.run_step(1, 1, headers=False)
        self.assertEqual(len(apt), 1, apt)
        self.assertTrue(apt[0].endswith("install python3-dev ffmpeg"), apt)

    def test_the_image_lane_alone_needs_no_ffmpeg(self):
        apt = self.run_step(1, 0, headers=False)
        self.assertTrue(apt[0].endswith("install python3-dev"), apt)

    def test_a_box_that_has_them_is_asked_nothing(self):
        self.assertEqual(self.run_step(1, 0, headers=True), [])

    def test_no_image_or_video_lane_asks_nothing(self):
        self.assertEqual(self.run_step(0, 0, headers=False), [])

    def test_no_start_and_no_service_ask_nothing(self):
        # both return before those lanes; --no-service uses no sudo at all
        self.assertEqual(self.run_step(1, 1, headers=False, no_start=1), [])
        self.assertEqual(self.run_step(1, 1, headers=False, no_service=1), [])

    def test_it_comes_after_the_ticket_and_before_the_first_download(self):
        t = self.TEXT
        at = t.index('host_packages "the image and video lanes"')
        self.assertLess(t.index('sudo -v || die "sudo could not be used here'), at)
        self.assertLess(at, t.index('step "2/10'))


class TheLanesInstallersPutThemInFromATerminal(unittest.TestCase):
    def test_each_tries_before_its_own_check_which_stays(self):
        for f, pkgs, check in (
                ("install-image.sh", ("python3-venv", "python3-dev"), 'Python.h" ] || die "python3-dev is missing'),
                ("install-video.sh", ("ffmpeg", "python3-venv", "python3-dev"), 'command -v ffmpeg >/dev/null || die'),
                ("install-speech.sh", ("ffmpeg", "python3-venv", "build-essential"), 'command -v ffmpeg >/dev/null || die')):
            with self.subTest(f):
                text = (REPO / f).read_text()
                at = text.index("|| host_packages ")
                self.assertLess(at, text.index(check), "the attempt must come before the check")
                for p in pkgs:
                    self.assertIn(f"HOST_PKGS+=({p})" if p != "build-essential" else "c++:build-essential",
                                  text[:at])


class TheScriptsRunFromATerminalGoOnWithTheGroup(unittest.TestCase):
    def test_each_asks_before_its_first_docker_command(self):
        for f in ("switch-model.sh", "install-image.sh", "install-video.sh", "install-kolibri.sh",
                  "install-nemotron.sh", "install-nemotron-super.sh", "install-flash-local.sh",
                  "install-ornith-122b.sh", "uninstall.sh", "install.sh"):
            with self.subTest(f):
                text = (REPO / f).read_text()
                at = text.index('docker_session bash "$')
                self.assertIn('/${BASH_SOURCE[0]##*/}" "$@"', text[at:text.index("\n", at)])
                code = [ln for ln in text[:at].splitlines() if not ln.lstrip().startswith("#")]
                first = [ln for ln in code if ln.lstrip().startswith("docker ")]
                self.assertEqual(first, [], f"docker is run before the group is asked for: {first}")

    def test_each_has_the_die_the_library_calls(self):
        for f in ("switch-model.sh", "install-image.sh", "install-video.sh", "install-kolibri.sh",
                  "install-nemotron.sh", "install-nemotron-super.sh", "install-flash-local.sh",
                  "install-ornith-122b.sh", "uninstall.sh", "install.sh", "prepare-lane.sh", "install-speech.sh"):
            with self.subTest(f):
                text = (REPO / f).read_text()
                self.assertRegex(text[:text.index('lanes.sh"')], r"\ndie ?\(\) *\{")

    def test_prepare_lane_asks_where_it_loads_the_library(self):
        text = (REPO / "prepare-lane.sh").read_text()
        at = text.index('    . "$HERE/lanes.sh"\n')
        ask = text.index('    docker_session bash "$HERE/${BASH_SOURCE[0]##*/}" "$@"\n', at)
        # only a 64 GB box's refusal comes between the two, and it runs no docker
        code = [ln for ln in text[at:ask].splitlines() if not ln.lstrip().startswith("#")]
        self.assertEqual([ln for ln in code if "docker" in ln], [])
        self.assertIn("    # a terminal opened before the account joined the docker group", text[at:ask])



def walled_with_memory(at=None, after=False):
    """walled() install.sh, its /proc/meminfo a file of a 128 GB box (the CI's runner has 16)."""
    path = pathlib.Path(wall.walled(at=at, after=after))
    mem = path.parent / "meminfo"
    mem.write_text(MEMINFO)
    path.write_text(path.read_text().replace("/proc/meminfo", str(mem)))
    return str(path)


def box_for_step_1(**kw):
    box = Box(**kw)
    for name, body in (("uname", 'case "$1" in -m) echo aarch64 ;; *) exec /usr/bin/uname "$@" ;; esac\n'),
                       ("nvidia-smi", 'echo "NVIDIA GB10"\n')):
        (box.bin / name).write_text("#!/bin/bash\n" + body)
        (box.bin / name).chmod(0o755)
    return box


# the wall goes after the docker step, and after the line that gives the screen back to a run
# started again by it
GRANT_LINE = '[ -z "$DOCKER_SAID" ] || exec 1>&3 3>&-\n'


class TheWholeScripts(unittest.TestCase):
    """install.sh, get.sh and uninstall.sh run as written, up to a wall."""

    def run_install(self, box, copy, *args, **extra):
        env, record = wall.fenced_env(first=str(box.bin), **extra)
        r = subprocess.run(["bash", copy, *args], capture_output=True, text=True, env=env,
                           cwd=wall.cwd(), timeout=60, stdin=subprocess.DEVNULL, start_new_session=True)
        return r, r.stdout + r.stderr, record

    def test_install_sh_a_terminal_older_than_the_group_relaunches_before_printing(self):
        box = Box(member=True)
        r, out, record = self.run_install(box, wall.walled(), "--no-video")
        self.assertNotIn(wall.WALL, out)
        self.assertEqual(r.returncode, 0, out[-800:])
        self.assertTrue(out.lstrip().startswith("Docker: alice is in the docker group"), out[:300])
        sg, = box.sg_calls()
        self.assertRegex(sg, r"/install\.sh --no-video'\]$")
        self.assertEqual(wall.reached(record), [])

    def test_install_sh_step_1_adds_the_account_then_starts_again(self):
        box = box_for_step_1()
        r, out, record = self.run_install(box, walled_with_memory(at=GRANT_LINE, after=True))
        self.assertNotIn(wall.WALL, out, "the run went past the docker step without the group")
        self.assertEqual(r.returncode, 0, out[-800:])
        self.assertIn("sudo usermod -aG docker alice", box.sudo_calls())
        self.assertIn("is in the docker group now; this run goes on with it", out)
        self.assertEqual(len(box.sg_calls()), 1)

    def test_install_sh_with_no_service_says_how_and_uses_no_sudo(self):
        box = box_for_step_1()
        r, out, record = self.run_install(box, walled_with_memory(at=GRANT_LINE, after=True), "--no-service")
        self.assertEqual(r.returncode, 1, out[-800:])
        self.assertIn("this run uses no sudo", out)
        self.assertEqual(box.sudo_calls(), [])

    def test_install_sh_asks_nothing_before_its_refusals(self):
        # --help, --plan and a mistyped flag end before step 1; a box below the memory refusal
        # ends before the docker step: none of them may add the account to the group
        for args in (("--help",), ("--plan",), ("--bogus-flag",)):
            with self.subTest(args):
                box = box_for_step_1()
                r, out, record = self.run_install(box, walled_with_memory(at=GRANT_LINE, after=True), *args)
                self.assertEqual(box.sudo_calls(), [], out[-500:])
                self.assertFalse(box.sg_calls())
        box = box_for_step_1()
        copy = pathlib.Path(wall.walled(at=GRANT_LINE, after=True))
        small = copy.parent / "meminfo-32"
        small.write_text("MemTotal:       32505856 kB\n")      # 31.0 GiB, below the 64 GB class
        copy.write_text(copy.read_text().replace("/proc/meminfo", str(small)))
        r, out, record = self.run_install(box, str(copy), QWEN38_MEMINFO=str(small))
        self.assertIn("This box has 31.0 GiB of memory (MemTotal), and the lanes here need a 64 GB or a 128 GB DGX Spark.", out)
        self.assertEqual(box.sudo_calls(), [])

    def test_a_64_gb_box_goes_on_with_the_lanes_that_fit_it(self):
        # until the 64 GB DGX Spark (2026-10-23) step 1 refused it as "a ~121 GB unified-memory
        # machine" short; it goes on now, and says which lanes it leaves out
        box = box_for_step_1(docker_err="", docker_rc=0, member=True, in_session=True)
        copy = pathlib.Path(wall.walled(at=GRANT_LINE, after=True))
        mem = copy.parent / "meminfo-64"
        mem.write_text("MemTotal:       60397977 kB\n")      # 57.6 GiB: 64 GiB held aside on the reference box
        copy.write_text(copy.read_text().replace("/proc/meminfo", str(mem)))
        r, out, record = self.run_install(box, str(copy), QWEN38_MEMINFO=str(mem))
        self.assertIn(wall.WALL, out, out[-800:])
        self.assertIn("This box is a 64 GB DGX Spark: the lanes that fit it are the Qwen3.8 27B and Nemotron 3.5 Bolt here", out)
        self.assertIn("Left out, as they need a 128 GB one: image (Qwen-Image 2.1), video (MiniMax-H3), Qwen3.8 Flash-Next, Kolibri-1, "
                      "Nemotron 3.5 Super VL, Qwen3.8 Flash-Next FP4-Local, Ornith 1.5 122B.", out)
        self.assertIn("Context mode: native (262144 tokens), the one a 64 GB DGX Spark holds.", out)
        self.assertNotIn("needs a ~121 GB", out)

    def test_a_64_gb_box_refuses_what_it_cannot_hold_before_anything(self):
        for args, env, said in (
                ((), {"MODEL_CHOICE": "flash"}, "The flash lane needs a 128 GB DGX Spark (its checkpoint alone is 135.3 GB)"),
                ((), {"MODEL_CHOICE": "fp8"}, "MODEL_CHOICE=fp8 (Qwen's FP8 weights, 30.9 GB) leaves the 27B's KV pool too little"),
                ((), {"CONTEXT_MODE": "1m"}, "CONTEXT_MODE=1m needs a 128 GB DGX Spark"),
                (("--with-video",), {}, "The video (MiniMax-H3) lane needs a 128 GB DGX Spark, and this box is a 64 GB one. Drop --with-video."),
                (("--with-flash-local",), {}, "The Qwen3.8 Flash-Next FP4-Local lane needs a 128 GB DGX Spark")):
            with self.subTest(args or env):
                box = box_for_step_1(docker_err="", docker_rc=0, member=True, in_session=True)
                copy = pathlib.Path(wall.walled(at=GRANT_LINE, after=True))
                mem = copy.parent / "meminfo-64"
                mem.write_text("MemTotal:       60397977 kB\n")
                copy.write_text(copy.read_text().replace("/proc/meminfo", str(mem)))
                r, out, record = self.run_install(box, str(copy), *args, **{**env, "QWEN38_MEMINFO": str(mem)})
                self.assertEqual(r.returncode, 1, out[-800:])
                self.assertIn(said, out)
                self.assertNotIn(wall.WALL, out)
                self.assertEqual(box.sudo_calls(), [])

    def test_install_sh_started_again_does_not_say_its_first_lines_twice(self):
        box = box_for_step_1(docker_err="", docker_rc=0, member=True, in_session=True)
        r, out, record = self.run_install(box, walled_with_memory(at=GRANT_LINE, after=True),
                                          QWEN38_DOCKER_RELAUNCHED="granted")
        self.assertIn(wall.WALL, r.stdout, "the screen did not come back after the docker step")
        for said_before in ("Context mode", "1/10 Preflight checks", "GPU: NVIDIA GB10"):
            self.assertNotIn(said_before, r.stdout)

    def test_get_sh_leaves_the_docker_step_to_install_sh(self):
        box = Box()
        clone = box.d / "clone"
        clone.mkdir()
        (clone / ".git").mkdir()
        (box.bin / "git").write_text(
            "#!/bin/bash\n"
            'case "$*" in\n'
            '  *"remote get-url origin"*) echo https://github.com/hasso5703/dgx-spark-qwen38 ;;\n'
            '  *"symbolic-ref"*) echo main ;;\n'
            '  *"rev-list --count"*) echo 0 ;;\n'
            '  *"log -1"*) echo "abc1234 the release" ;;\n'
            'esac\nexit 0\n')
        (box.bin / "git").chmod(0o755)
        env, record = wall.fenced_env(first=str(box.bin), DIR=str(clone))
        r = subprocess.run(["bash", wall.walled("get.sh", at='  exec bash ./install.sh "$@"\n'), "--help"],
                           capture_output=True, text=True, env=env, cwd=wall.cwd(), timeout=60,
                           stdin=subprocess.DEVNULL, start_new_session=True)
        out = r.stdout + r.stderr
        self.assertIn(wall.WALL, out, "get.sh did not go on to install.sh")
        self.assertEqual(box.sudo_calls(), [], "get.sh asked sudo before install.sh read its flags")
        self.assertFalse([c for c in box.called() if c.startswith("docker")])

    def test_uninstall_sh_says_the_way_out_when_it_cannot_start_again(self):
        box = Box(member=True, with_sg=False)
        d = box.d / "repo"
        d.mkdir()
        for f in ("uninstall.sh", "lanes.sh", "oc-merge-limits.py"):
            (d / f).write_text((REPO / f).read_text())
        r = subprocess.run(["/bin/bash", str(d / "uninstall.sh"), "--list"], capture_output=True, text=True,
                           env=box.env(), cwd=str(d), timeout=60,
                           stdin=subprocess.DEVNULL, start_new_session=True)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("Log out and back in", r.stderr)
        self.assertNotIn("command not found", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
