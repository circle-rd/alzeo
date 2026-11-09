#!/usr/bin/env python3
"""A text lane left out on a box booting a side lane (issue #57, 2026-10-21).

A box booting Flash FP4-Local whose record named the flash lane, the text lane it served before,
took that lane for its run's text lane whatever --no-flash said: the run pulled its image and
fetched its checkpoint again (35 and 136 GB) under "Leaving the flash lane out". A first fix, never
released, took the other text lane in its place by its unit file alone: a 27B left out made the
flash lane, there with its units only, the run's own and installed it whole (223 GB), and a box
with no 27B unit lost its port, its cache and its proxy bind (review of 2026-10-21).

The lane left out stays the run's text lane, read as the box has it, and gets its units only, at this
release's pins: none of its downloads, nothing started, its port, cache and binds kept. The other text
lane follows its own rule and the rest of the box is updated. These run install.sh as written, past
its first step, up to a wall after that rule, and then to the end of the run, on a box whose commands
answer as a GB10 with 128 GB, whose docker pulls and runs nothing, and whose units are the test's own.
A second fix, reviewed on 2026-10-21, said the way back was ./install.sh --with-<lane>, a run that
fetches the lane whole at once, and was tested only up to that rule: a step 2 that pulled again
passed every test. The boot a side lane's uninstall gives back goes only to a text lane that can
start (test_text_lane.py).
"""
import os
import pathlib
import re
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import installer_wall as wall  # noqa: E402
from test_fresh_machine import MEMINFO, box_for_step_1  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[1]
# the wall goes right after the rule (install.sh: LEFT_OUT), before the disk checks
AFTER_RULE = "# Measured where the weights actually land: HF_CACHE may live on another disk,\n"
PROBE = ('echo "RESOLVED LANE=$LANE UNITS_ONLY=$UNITS_ONLY LEFT_OUT=${LEFT_OUT:-} INSTALLED=$INSTALLED_CHOICE '
         'PORT=$PORT HF=$HF_CACHE PBIND=$PROXY_BIND"\n')

LAUNCHER = ("docker run --rm --network host -v {hf}:/root/.cache/huggingface img "
            "python3 -m sglang.launch_server --model-path RadixArk/Qwen3.8-Flash-Next-NVFP4 "
            "--revision 7b719225242aacd3dbd3f9407468c2ee9a9d2594 --host 127.0.0.1 --port {port} "
            "--max-running-requests 8 --context-length 262144\n")
FLASH_UNIT = "[Service]\nExecStart=/bin/bash /x/launch-flash.sh\n"
SGL_UNIT = ("[Service]\nExecStart=/bin/bash -c 'exec /usr/bin/docker run --rm --name qwen38-sglang "
            "-v {hf}:/root/.cache/huggingface lmsysorg/sglang@sha256:" + "a" * 64 +
            " python3 -m sglang.launch_server --model-path RadixArk/Qwen3.8-27B-NVFP4 "
            "--revision 52d1adc5f38aa5ebf099c29ed7025ba34cfbb854 --context-length {ctx} --host 127.0.0.1 --port {port}'\n")
KEEPALIVE = "[Service]\nEnvironment=PROXY_BIND={pbind}\nEnvironment=UPSTREAM=http://127.0.0.1:{port}\n"
FP4 = "qwen38-flash-local.service"


def box_run(record, units=("flash", "27b"), off=(), args=(), boot=FP4, port=31000, hf="/mnt/x/hf", pbind="127.0.0.1",
            env_extra=None, ctx=1010000, prepared=False):
    """(rc, out): install.sh up to the wall, on a box booting `boot` whose text-lane record says `record`,
    with the text units of `units`, the markers of `off` (an older release's), the flash launcher and the
    keepalive unit carrying the box's own port, cache and proxy bind. No PORT, HF_CACHE or bind in the
    environment: what the run keeps, it reads. prepared: both text lanes' checkpoints are in a cache of
    the test's own (their images are, as docker answers here), which makes each lane prepared."""
    box = box_for_step_1(docker_err="", docker_rc=0, member=True, in_session=True)
    if prepared:
        hf = str(wall._tmp("left-out-hf-"))
        for repo, rev in (("RadixArk--Qwen3.8-Flash-Next-NVFP4", "7b719225242aacd3dbd3f9407468c2ee9a9d2594"),
                          ("RadixArk--Qwen3.8-27B-NVFP4", "52d1adc5f38aa5ebf099c29ed7025ba34cfbb854")):
            snap = pathlib.Path(hf) / "hub" / f"models--{repo}" / "snapshots" / rev
            snap.mkdir(parents=True)
            (snap / "model.safetensors").write_text("")
    unit_dir = wall._tmp("left-out-units-")
    (unit_dir / FP4).write_text("[Service]\n")
    (unit_dir / "qwen38-keepalive.service").write_text(KEEPALIVE.format(pbind=pbind, port=port))
    if "flash" in units:
        (unit_dir / "qwen38-flash.service").write_text(FLASH_UNIT)
    if "27b" in units:
        (unit_dir / "qwen38-sglang.service").write_text(SGL_UNIT.format(hf=hf, port=port, ctx=ctx))
    copy = pathlib.Path(wall.walled(at=AFTER_RULE, units=str(unit_dir)))
    # its memory as install.sh reads it, by QWEN38_MEMINFO: a /proc/meminfo rewritten in the copy is
    # only its fallback, which the wall's own file passes over (found in review, 2026-10-23)
    mem = copy.parent / "meminfo"
    mem.write_text(MEMINFO)
    text = copy.read_text()
    copy.write_text(text.replace(f'echo "{wall.WALL}"', PROBE + f'echo "{wall.WALL}"', 1))
    home = wall._tmp("left-out-home-")
    cfg = home / ".config" / "qwen38"
    cfg.mkdir(parents=True)
    (cfg / "text-lane").write_text(record + "\n")
    (cfg / "launch-flash.sh").write_text(LAUNCHER.format(hf=hf, port=port))
    for lane in off:
        (cfg / f"{lane}.off").write_text(f"left out with ./install.sh --no-{lane} on 2026-10-20T08:00Z\n")
    env, record_file = wall.fenced_env(home=home, enabled=(boot,), first=str(box.bin), QWEN38_MEMINFO=str(mem),
                                       **(env_extra or {}))
    r = subprocess.run(["bash", str(copy), *args], capture_output=True, text=True, env=env, cwd=wall.cwd(),
                       timeout=120, stdin=subprocess.DEVNULL, start_new_session=True)
    return r.returncode, r.stdout + r.stderr


def resolved(case, out):
    m = re.search(r"^RESOLVED (.*)$", out, re.M)
    case.assertIsNotNone(m, out[-1500:])
    return dict(kv.split("=", 1) for kv in m.group(1).split())


class ALaneLeftOutGetsItsUnitsOnly(unittest.TestCase):

    def test_the_issue_an_old_marker_no_longer_leaves_the_flash_out_and_still_fetches_nothing(self):
        # a --no-flash counts for its run only (Hasan, 2026-10-22): the marker an older release wrote is
        # said and dropped, and the lane, not prepared on this box, still gets none of its downloads
        rc, out = box_run("qwen38-flash.service", off=("flash",))
        self.assertEqual(rc, 97, out[-1500:])
        r = resolved(self, out)
        self.assertEqual((r["LANE"], r["UNITS_ONLY"], r["LEFT_OUT"]), ("flash", "1", "1"))
        self.assertIn("Your earlier --no-flash is no longer remembered (left out with ./install.sh --no-flash on "
                      "2026-10-20T08:00Z): a --no-flash counts for the run that passes it only now, so the Qwen3.8 Flash-Next lane is "
                      "back in this run. Pass --no-flash on each run to keep it out.", out)
        self.assertIn("NOTE: the flash lane is not prepared on this box: its units only, none of its downloads, and this "
                      "run starts nothing.", out)
        self.assertNotIn("Leaving the flash lane out", out)

    def test_and_by_its_flag(self):
        rc, out = box_run("qwen38-flash.service", args=("--no-flash",))
        self.assertEqual(rc, 97, out[-1500:])
        r = resolved(self, out)
        self.assertEqual((r["LANE"], r["UNITS_ONLY"]), ("flash", "1"))
        self.assertIn("NOTE: the flash lane is left out (--no-flash): its units only, none of its downloads", out)

    def test_the_box_keeps_its_port_cache_and_proxy_bind(self):
        """No 27B unit, as on the first fix's second regression: the run reads them from the lane it keeps."""
        rc, out = box_run("qwen38-flash.service", units=("flash",), off=("flash",))
        self.assertEqual(rc, 97, out[-1500:])
        r = resolved(self, out)
        self.assertEqual((r["LANE"], r["UNITS_ONLY"], r["INSTALLED"]), ("flash", "1", "flash"))
        self.assertEqual((r["PORT"], r["HF"], r["PBIND"]), ("31000", "/mnt/x/hf", "127.0.0.1"))

    def test_a_27b_left_out_stays_the_runs_and_never_makes_the_flash_its_own(self):
        """The first fix's first regression: the flash lane there with its units only was installed whole."""
        rc, out = box_run("qwen38-sglang.service", args=("--no-27b",))
        self.assertEqual(rc, 97, out[-1500:])
        r = resolved(self, out)
        self.assertEqual((r["LANE"], r["UNITS_ONLY"], r["INSTALLED"]), ("27b", "1", "27b"))
        self.assertIn("NOTE: the 27B lane is left out (--no-27b): its units only", out)
        self.assertEqual((r["PORT"], r["HF"], r["PBIND"]), ("31000", "/mnt/x/hf", "127.0.0.1"))

    def test_with_both_left_out_the_run_goes_on(self):
        for args, off in (((), ("flash", "27b")), (("--no-flash", "--no-27b"), ())):
            with self.subTest(args=args, off=off):
                rc, out = box_run("qwen38-flash.service", args=args, off=off)
                self.assertEqual(rc, 97, out[-1500:])
                r = resolved(self, out)
                self.assertEqual((r["LANE"], r["UNITS_ONLY"]), ("flash", "1"))
                self.assertNotIn("ERROR", out)

    def test_a_lane_prepared_is_brought_up_to_date_whole_as_before(self):
        for record, lane in (("qwen38-flash.service", "flash"), ("qwen38-sglang.service", "27b")):
            with self.subTest(record):
                rc, out = box_run(record, prepared=True)
                self.assertEqual(rc, 97, out[-1500:])
                r = resolved(self, out)
                self.assertEqual((r["LANE"], r["UNITS_ONLY"]), (lane, "0"))
                self.assertIn(r["LEFT_OUT"], ("0", ""))
                self.assertNotIn("its units only", out)

    def test_a_lane_not_prepared_gets_its_units_only_with_no_flag_at_all(self):
        # with --no-<lane> no longer remembered, what keeps issue #57's downloads off a box that left a text
        # lane out once is that the lane is not prepared: it was updated whole, 35 and 136 GB for the flash
        for record, lane, shown in (("qwen38-flash.service", "flash", "flash"), ("qwen38-sglang.service", "27b", "27B")):
            with self.subTest(record):
                rc, out = box_run(record)
                self.assertEqual(rc, 97, out[-1500:])
                r = resolved(self, out)
                self.assertEqual((r["LANE"], r["UNITS_ONLY"], r["LEFT_OUT"]), (lane, "1", "1"))
                self.assertIn(f"NOTE: the {shown} lane is not prepared on this box: its units only, none of its downloads", out)

    def test_named_or_with_all_weights_a_lane_not_prepared_is_updated_whole(self):
        for args in (("--with-flash",), ("--all-weights",)):
            with self.subTest(args):
                rc, out = box_run("qwen38-flash.service", args=args)
                self.assertEqual(rc, 97, out[-1500:])
                r = resolved(self, out)
                self.assertEqual((r["LANE"], r["UNITS_ONLY"]), ("flash", "0"))
                self.assertIn(r["LEFT_OUT"], ("0", ""))

    def test_contradicting_flags_are_refused_first_and_alone(self):
        rc, out = box_run("qwen38-flash.service", args=("--no-flash", "--with-flash"))
        self.assertEqual(rc, 1, out[-1500:])
        self.assertIn("--no-flash and --with-flash contradict each other", out)
        self.assertNotIn("is left out", out)
        self.assertNotIn("RESOLVED", out)

    def test_the_lane_that_serves_is_brought_up_to_date_whatever_it_was_asked(self):
        for record, lane, shown, unit in (("qwen38-flash.service", "flash", "flash", "qwen38-flash.service"),
                                           ("qwen38-sglang.service", "27b", "27B", "qwen38-sglang.service")):
            for args, off in (((), (lane,)), ((f"--no-{lane}",), ())):
                with self.subTest(lane=lane, args=args, off=off):
                    rc, out = box_run(record, args=args, off=off, boot=unit)
                    self.assertEqual(rc, 97, out[-1500:])
                    r = resolved(self, out)
                    self.assertEqual((r["LANE"], r["UNITS_ONLY"]), (lane, "0"))
                    self.assertNotIn(f"Leaving the {shown} lane out", out)
                    # what it says holds on a GPU outside ATS too, whose flash lane gets its units only (#54)
                    if off:
                        self.assertIn(f"Your earlier --no-{lane} is no longer remembered", out)
                    else:
                        self.assertIn(f"The {shown} lane is the one this run serves: --no-{lane} leaves nothing out of it.", out)
                    self.assertNotIn("brought up to date", out)

    def test_a_27b_left_out_is_named_so(self):
        for args in ((), ("--recommended",)):
            with self.subTest(args):
                rc, out = box_run("qwen38-sglang.service", off=("27b",), args=args)
                self.assertIn("Your earlier --no-27b is no longer remembered (left out with ./install.sh --no-27b on "
                              "2026-10-20T08:00Z): a --no-27b counts for the run that passes it only now, so the Qwen3.8 27B lane "
                              "is back in this run. Pass --no-27b on each run to keep it out.", out)
                self.assertNotIn("the 27b lane", out)

    def test_every_side_lane_at_boot_not_only_flash_local(self):
        """The rule is the side lanes' (SIDE_BOOT): run with FP4-Local only, a rule read from it alone passed."""
        for boot in ("qwen38-image.service", "qwen38-kolibri.service"):
            with self.subTest(boot=boot):
                rc, out = box_run("qwen38-flash.service", off=("flash",), boot=boot)
                self.assertEqual(rc, 97, out[-1500:])
                r = resolved(self, out)
                self.assertEqual((r["LANE"], r["UNITS_ONLY"], r["LEFT_OUT"]), ("flash", "1", "1"))

    def test_the_run_for_the_other_text_lane_is_never_a_lane_left_out(self):
        """The run install.sh starts for the other text lane (QWEN38_SECONDARY) sees no side lane at boot, so
        the rule never takes it: its own words are those of a lane installed on demand."""
        # as the parent starts it on a box that has the lane (env -u MODEL_CHOICE): a MODEL_CHOICE given would make
        # the run take its text lane back from FP4-Local, and the test would pass for that reason
        rc, out = box_run("qwen38-flash.service", off=("27b",), env_extra={"QWEN38_SECONDARY": "27b", "QWEN38_UNITS_ONLY": "1"})
        self.assertEqual(rc, 97, out[-1500:])
        r = resolved(self, out)
        self.assertEqual((r["LANE"], r["UNITS_ONLY"], r["LEFT_OUT"]), ("27b", "1", "0"))
        self.assertNotIn("is left out", out)

    def test_no_service_with_the_lane_left_out_is_refused(self):
        """--no-service prepares the lane for ./run.sh: with its downloads left out the run said Prepared, and
        run.sh then stopped on a checkpoint not found (review of 2026-10-21)."""
        # the 27B at its native window: the flash lane is refused --no-service already, and so is the 27B's 1m
        rc, out = box_run("qwen38-sglang.service", args=("--no-27b", "--no-service"), ctx=262144)
        self.assertEqual(rc, 1, out[-1500:])
        self.assertIn("--no-service prepares the 27B lane for ./run.sh, and that lane is left out (--no-27b): nothing "
                      "would run. Drop --no-27b to prepare it whole, or run without --no-service", out)
        self.assertNotIn("RESOLVED", out)
        # an older release's marker no longer refuses it, and the lane, not prepared, is prepared whole for run.sh
        rc, out = box_run("qwen38-sglang.service", off=("27b",), args=("--no-service",), ctx=262144)
        self.assertEqual(rc, 97, out[-1500:])
        r = resolved(self, out)
        self.assertEqual((r["LANE"], r["UNITS_ONLY"]), ("27b", "0"))

    def test_plan_lists_and_stops_with_both_left_out(self):
        rc, out = box_run("qwen38-flash.service", args=("--plan",), off=("flash", "27b"))
        self.assertEqual(rc, 0, out[-1500:])
        self.assertIn("--plan: nothing was changed.", out)
        self.assertNotIn("ERROR", out)


class ItsEndSaysSo(unittest.TestCase):
    """The end of a run on a box booting a side lane, the branch of a text lane left out, extracted, for
    each state of the other text lane and with or without the cockpit (its wiring is the full run's
    below). It said "the text lane is up to date" over a lane that got its units only, then sent the
    way back through ./install.sh --with-<lane>, a run that fetches it whole at once, and said nothing
    of the other text lane (review of 2026-10-21)."""

    def ending(self, lane, second="units", cockpit=1, choice=None, why=None):
        text = (REPO / "install.sh").read_text()
        start = text.index('  elif [ "$LEFT_OUT" -eq 1 ]; then\n')
        end = text.index('  else\n    step "Done: the text lane is up to date', start)
        other = "27b" if lane == "flash" else "flash"
        why = why or f"left out (--no-{lane})"
        script = ('step(){ echo "STEP $*"; }\n'
                  f'LANE={lane}; SERVING_LANE="Qwen3.8 Flash-Next FP4-Local"; LEFT_OUT=1; LEFT_WHY="{why}"; COCKPIT={cockpit}; '
                  f'MODEL_CHOICE={choice or ("stock" if lane == "27b" else "flash")}\n'
                  f'SECOND_LANE={other}; SECOND_STATE={second}; SECOND_CANNOT="the GPU\'s addressing mode is None"\n'
                  'if false; then :\n' + text[start:end] + "fi\n")
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_it_names_the_lane_left_out_and_the_way_back(self):
        out = self.ending("flash")
        self.assertIn("STEP Done: the flash lane is left out (--no-flash), so it got its units only, none of its downloads", out)
        self.assertNotIn("this box is up to date", out)   # the side lanes' own NOTEs can say otherwise
        self.assertIn("the Qwen3.8 Flash-Next FP4-Local lane stays this box's serving lane", out)
        self.assertIn("back to it: Load it from the cockpit's Lanes view, which fetches what it lacks", out)
        self.assertIn("./install.sh --with-flash prepares it whole instead: that run fetches it all at once", out)
        self.assertNotIn("every update", out)     # a --no-<lane> no longer outlives its run
        self.assertNotIn("first Load from the cockpit then fetches", out)
        out = self.ending("27b", cockpit=0)
        self.assertIn("STEP Done: the 27B lane is left out (--no-27b)", out)
        self.assertIn("back to it: ./prepare-lane.sh 27b stock fetches what it lacks, then ./switch-model.sh stock switches to it", out)
        self.assertIn("./install.sh --with-27b prepares it whole instead", out)
        self.assertNotIn("text lane is up to date", out)

    def test_a_lane_not_prepared_is_said_so(self):
        out = self.ending("flash", why="not prepared on this box")
        self.assertIn("STEP Done: the flash lane is not prepared on this box, so it got its units only, none of its downloads", out)

    def test_a_custom_model_kept_goes_back_through_the_installer(self):
        """A custom model has no switch target and no Load prepares it: ./prepare-lane.sh 27b custom is refused."""
        for cockpit in (1, 0):
            with self.subTest(cockpit=cockpit):
                out = self.ending("27b", cockpit=cockpit, choice="custom")
                self.assertIn("back to it: ./install.sh --with-27b, which installs it whole with the custom model it keeps", out)
                for nothing in ("prepare-lane.sh", "Load it from the cockpit", "custom switches"):
                    self.assertNotIn(nothing, out)

    def test_it_says_where_the_other_text_lane_stands(self):
        for second, said in (("installed", "the 27B lane is up to date, ready to Load"),
                             ("units", "the 27B lane got its units: its first Load fetches its image and weights"),
                             ("off", "the 27B lane is left out too, by --no-27b on this run"),
                             ("failed", "the 27B lane: ./install.sh --with-27b installs it whole")):
            with self.subTest(second):
                out = self.ending("flash", second)
                self.assertIn(said, out)
                if second == "failed":
                    self.assertIn("STEP Done, but the 27B lane did not install (see its NOTE above); the flash lane is left "
                                  "out (--no-flash)", out)
                    self.assertNotIn("this box is up to date", out)
        out = self.ending("27b", "cannot")
        self.assertIn("the flash lane got its units only: this GPU cannot start it (the GPU's addressing mode is None)", out)


AFTER_SIDE_BOOT = "  record_install side-boot\n"
FLASH_PIN = re.search(r'^FLASH_IMAGE="\$\{FLASH_IMAGE:-([^}]+)\}"', (REPO / "install.sh").read_text(), re.M).group(1)
SGL_PIN = re.search(r'^IMAGE="\$\{IMAGE:-([^}]+)\}"', (REPO / "install.sh").read_text(), re.M).group(1)
# sudo runs what the run asks of it on the test's own files (and the run's mktemp files), and nothing else
SAFE_SUDO = """while [ -n "${1:-}" ] && [ "${1#-}" != "$1" ]; do shift; done
[ "$#" -eq 0 ] && exit 0
case "$1" in
  true) exit 0 ;;
  install|cp|chown|rm|rmdir|touch|cat|cmp|systemctl|mkdir|chmod|stat) ;;
  *) echo "sudo stub refuses: $1" >&2; exit 1 ;;
esac
for a in "$@"; do case "$a" in /*) case "$a" in "$SAFE_ROOT"/*|/tmp/tmp.*) ;; *) echo "sudo stub refuses path $a" >&2; exit 1 ;; esac ;; esac; done
exec "$@"
"""


def full_run(record, off=(), args=()):
    """install.sh as written, to the end of the path a box booting a side lane takes (a wall after its
    record), on the box of the issue: Flash FP4-Local at boot, both text lanes installed, whose images
    are not on the box (the release moves both pins), the flash launcher and the keepalive unit on the
    box's own port, cache and proxy bind. docker pulls, runs and builds nothing (it refuses, and says so
    in the calls); the side lanes' installers are there to read, not to run; the cockpit's and the
    agent's installers, prepare-lane.sh and switch-model.sh only write down their call.
    (rc, out, calls, units dir, config dir, HF cache)."""
    units = pathlib.Path(wall.units_dir())
    script = pathlib.Path(wall.walled(at=AFTER_SIDE_BOOT, after=True, units=str(units)))
    mem = script.parent / "meminfo"       # read by QWEN38_MEMINFO, as above
    mem.write_text(MEMINFO)
    for f in REPO.iterdir():      # what install.sh reads beside itself (its templates, its helpers)
        if f.is_file() and not (script.parent / f.name).exists():
            shutil.copy2(f, script.parent / f.name)
    shutil.copytree(REPO / "dashboard", script.parent / "dashboard", dirs_exist_ok=True)
    stubs = wall._tmp("left-out-stubs-")
    calls = stubs / "calls"
    calls.touch()
    for side in ("install-image.sh", "install-video.sh", "install-kolibri.sh", "install-nemotron.sh",
                 "install-nemotron-super.sh", "install-flash-local.sh", "install-ornith-122b.sh"):
        (script.parent / side).write_text((REPO / side).read_text())
        (script.parent / side).chmod(0o644)
    for runs in ("dashboard/install-dashboard.sh", "dashboard/install-agent.sh", "prepare-lane.sh", "switch-model.sh"):
        (script.parent / runs).write_text(f'#!/bin/bash\necho "{runs} $*" >> "{calls}"\n')
        (script.parent / runs).chmod(0o755)
    home = wall._tmp("left-out-home-")
    cfg = home / ".config" / "qwen38"
    cfg.mkdir(parents=True)
    (cfg / "api-key").write_text("k\n")
    hf = wall._tmp("left-out-hf-")
    (units / FP4).write_text("[Service]\nExecStart=/bin/true\n")
    (units / "qwen38-keepalive.service").write_text(KEEPALIVE.format(pbind="127.0.0.1", port=31000))
    (units / "qwen38-sglang.service").write_text(SGL_UNIT.format(hf=hf, port=31000, ctx=1010000))
    (units / "qwen38-flash.service").write_text(FLASH_UNIT)
    (cfg / "launch-flash.sh").write_text(LAUNCHER.format(hf=hf, port=31000))
    (cfg / "text-lane").write_text(record + "\n")
    for lane in off:
        (cfg / f"{lane}.off").write_text(f"left out with ./install.sh --no-{lane} on 2026-10-20T08:00Z\n")
    docker_root = wall._tmp("left-out-docker-")
    for name, body in (
            ("nvidia-smi", 'case "$*" in *driver_version*) echo 580.173.02 ;; *memory.total*) echo "[N/A]" ;; '
                           '*addressing_mode*) echo ATS ;; *) echo "NVIDIA GB10" ;; esac\n'),
            ("uname", 'case "$1" in -m) echo aarch64 ;; *) exec /usr/bin/uname "$@" ;; esac\n'),
            ("docker", f'case "$1 $2" in "info "*) echo "{docker_root}"; exit 0 ;; "image inspect") exit 1 ;; '
                       '"inspect "*) exit 1 ;; esac\n'
                       'case "$1" in pull|run|build|create|start|exec|tag|rmi|login) echo "docker refuses $1" >&2; exit 1 ;; esac\n'
                       'exit 0\n'),
            ("sudo", SAFE_SUDO),
            ("systemctl", f'case "$1" in is-enabled) for a in "$@"; do [ "$a" = {FP4} ] && exit 0; done; exit 1 ;; '
                          'is-active|list-unit-files) exit 1 ;; esac\nexit 0\n'),
            ("ss", 'exit 0\n'),
            ("apt-get", 'exit 1\n')):
        (stubs / name).write_text(f'#!/bin/bash\necho "{name} $*" >> "{calls}"\n' + body)
        (stubs / name).chmod(0o755)
    env, _ = wall.fenced_env(home=home, first=str(stubs), QWEN38_HOST_PACKAGES_TRIED="1", QWEN38_MEMINFO=str(mem),
                             SAFE_ROOT=str(pathlib.Path(wall._tmp("x")).parent))
    r = subprocess.run(["bash", str(script), *args], capture_output=True, text=True, env=env, cwd=wall.cwd(),
                       timeout=600, stdin=subprocess.DEVNULL, start_new_session=True)
    return r.returncode, r.stdout + r.stderr, calls.read_text().splitlines(), units, cfg, hf


class TheRunOfALaneLeftOutToItsEnd(unittest.TestCase):
    """The run on the issue's box, to its last line: none of the left-out lane's downloads at any step,
    its units at this release's pins on the box's own port, cache and bind, and an end that says what
    happened and the way back. The tests above stop after the rule: with step 2 pulling again, or the
    end's branch never taken, all of them passed (mutants of the review of 2026-10-21)."""

    def check(self, out, rc, calls, hf):
        self.assertTrue(wall.WALL in out, out[-2500:])
        self.assertEqual(rc, 97, out[-2500:])
        self.assertEqual([c for c in calls if c.split()[:2] in (["docker", v] for v in
                          ("pull", "run", "build", "create", "start", "exec", "tag", "rmi", "login"))], [], calls)
        self.assertNotIn("docker refuses", out)
        self.assertEqual(list(hf.rglob("*.safetensors")) + list(hf.rglob("*.incomplete")), [], "a checkpoint fetched")
        self.assertNotIn("ERROR", out)

    def test_the_flash_left_out_by_its_flag_or_not_prepared_after_an_old_marker(self):
        for args, off, why in ((("--no-flash",), (), "left out (--no-flash)"), ((), ("flash",), "not prepared on this box")):
            with self.subTest(args=args, off=off):
                rc, out, calls, units, cfg, hf = full_run("qwen38-flash.service", off=off, args=args)
                self.check(out, rc, calls, hf)
                for said in (f"NOTE: the flash lane is {why}: its units only, none of its downloads, and this run starts nothing.",
                             f"2/10 The Flash-Next image: not pulled, as the flash lane is {why} (its next Load pulls what it lacks)",
                             f"3/10 The GPU from a container: not asked, as the flash lane is {why}",
                             f"4/10 The Flash-Next checkpoint: not fetched, as the flash lane is {why} (its next Load fetches what it lacks)",
                             f"Done: the flash lane is {why}, so it got its units only, none of its downloads",
                             "the Qwen3.8 Flash-Next FP4-Local lane stays this box's serving lane",
                             "back to it: Load it from the cockpit's Lanes view, which fetches what it lacks",
                             "./install.sh --with-flash prepares it whole instead: that run fetches it all at once",
                             "the 27B lane got its units: its first Load fetches its image and weights"):
                    self.assertTrue(said in out, said)
                # the run's own steps (the 27B's run beside it, on demand, says its own)
                self.assertNotIn("── 2/10 The Flash-Next image: pulled at the lane's first Load", out)
                self.assertNotIn("── 3/10 The GPU from a container: seen by the run that serves", out)
                launcher = (cfg / "launch-flash.sh").read_text()
                self.assertIn(FLASH_PIN, launcher, "its units at this release's pins")
                for kept in ("--port 31000", f"-v {hf}:/root/.cache/huggingface", "--host 127.0.0.1"):
                    self.assertIn(kept, launcher)
                # no marker written by the flag, and an older release's removed once said
                self.assertFalse((cfg / "flash.off").exists(), "a --no-flash outlives its run")

    def test_the_27b_left_out(self):
        rc, out, calls, units, cfg, hf = full_run("qwen38-sglang.service", args=("--no-27b",))
        self.check(out, rc, calls, hf)
        for said in ("NOTE: the 27B lane is left out (--no-27b): its units only, none of its downloads, and this run starts nothing.",
                     "2/10 The SGLang image: not pulled, as the 27B lane is left out (--no-27b) (its next Load pulls what it lacks)",
                     "3/10 The GPU from a container: not asked, as the 27B lane is left out (--no-27b)",
                     "4/10 The checkpoints (the target and its DFlash2 draft): not fetched, as the 27B lane is left out (--no-27b)",
                     "Done: the 27B lane is left out (--no-27b), so it got its units only, none of its downloads",
                     "./install.sh --with-27b prepares it whole instead: that run fetches it all at once",
                     "the flash lane got its units: its first Load fetches its image and weights"):
            self.assertTrue(said in out, said)
        unit = (units / "qwen38-sglang.service").read_text()
        self.assertIn(SGL_PIN, unit, "its units at this release's pins")
        for kept in ("--port 31000", f"-v {hf}:/root/.cache/huggingface", "--host 127.0.0.1"):
            self.assertIn(kept, unit)

    def test_both_left_out(self):
        rc, out, calls, units, cfg, hf = full_run("qwen38-flash.service", args=("--no-flash", "--no-27b"))
        self.check(out, rc, calls, hf)
        self.assertIn("Done: the flash lane is left out (--no-flash)", out)
        self.assertIn("the 27B lane is left out too, by --no-27b on this run", out)
        # both markers of an older release: both lanes back, neither prepared, none of their downloads
        rc, out, calls, units, cfg, hf = full_run("qwen38-flash.service", off=("flash", "27b"))
        self.check(out, rc, calls, hf)
        self.assertIn("Done: the flash lane is not prepared on this box", out)
        self.assertIn("the 27B lane got its units: its first Load fetches its image and weights", out)
        self.assertEqual([m.name for m in cfg.glob("*.off")], [])


class TheDocsSayIt(unittest.TestCase):
    """No page promises a --no-<lane> that later runs remember: README.md, five lane pages and a
    comment of install.sh still did after the rule changed (found in review, 2026-10-23)."""
    STALE = re.compile(r"leaves (?:it|the lane) out for good|a marker file remembers it|and a run remembers it"
                       r"|later runs remember|brings? it back after a `--no-|and bring it back \|")

    def test_no_page_says_a_lane_left_out_stays_out(self):
        pages = [REPO / "README.md", *sorted((REPO / "docs").glob("*.md"))]
        # the whole page, one space for every run of whitespace: a phrase cut over two lines counts
        found = [f"{p.name}: {m.group(0)}" for p in pages
                 for m in self.STALE.finditer(re.sub(r"\s+", " ", p.read_text()))]
        self.assertEqual(found, [])

    def test_no_installer_s_help_says_it_either(self):
        # -h and --help print each installer's header, and the seven lane installers' still promised
        # the marker; install.sh's own comments too (found in review, 2026-10-23)
        found = []
        for f in sorted(REPO.glob("install*.sh")):
            head = []
            for line in f.read_text().splitlines()[1:]:
                if not line.startswith("#"):
                    break
                head.append(line.lstrip("#"))
            found += [f"{f.name}: {m.group(0)}" for m in self.STALE.finditer(re.sub(r"\s+", " ", " ".join(head)))]
        comments = " ".join(line.strip().lstrip("#") for line in (REPO / "install.sh").read_text().splitlines()
                            if line.strip().startswith("#"))
        found += [f"install.sh: {m.group(0)}" for m in self.STALE.finditer(re.sub(r"\s+", " ", comments))]
        self.assertEqual(found, [])

if __name__ == "__main__":
    unittest.main(verbosity=2)
