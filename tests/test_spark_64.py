#!/usr/bin/env python3
"""A 64 GB DGX Spark gets the lanes that fit it, and every way in refuses the others before a byte.

The 64 GB DGX Spark ships from 2026-10-23 (boxmem.py says how it is told apart). install.sh sends
it the 27B in a unit sized for it and Bolt; the lanes' installers, the switch and the 27B's heavier
targets refuse the rest there, each before it fetches, pulls or writes anything. These run each
script's own lines with the class forced (QWEN38_MEMORY_CLASS), so no installer runs here."""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import installer_wall as wall  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[1]
SIDE = {"install-image.sh": "image", "install-video.sh": "video", "install-kolibri.sh": "kolibri",
        "install-nemotron-super.sh": "nemotron-super", "install-flash-local.sh": "flash-local",
        "install-ornith-122b.sh": "ornith-122b"}
SOURCE = '. "$HERE/lanes.sh"\n'


def bash(script, cls, **env):
    """script after lanes.sh and install.sh's die(), the class forced, in a throwaway HOME."""
    die = next(ln for ln in (REPO / "install.sh").read_text().splitlines() if ln.startswith("die()"))
    clean = {k: v for k, v in os.environ.items() if not k.startswith("QWEN38_")}
    with tempfile.TemporaryDirectory(prefix="spark64-home-") as home:
        r = subprocess.run(["bash", "-c", f'set -euo pipefail\n. "{REPO}/lanes.sh"\n{die}\n{script}'],
                           capture_output=True, text=True, timeout=30,
                           env={**clean, "QWEN38_MEMORY_CLASS": cls, "HOME": home, **env})
    return r.returncode, r.stdout, r.stderr


class TheSideLanes(unittest.TestCase):
    def guard(self, script):
        text = (REPO / script).read_text()
        self.assertEqual(text.count(SOURCE), 1, script)
        after = text.split(SOURCE, 1)[1].splitlines()
        # the comment, then the refusal: the first thing the script does once it has the library
        self.assertTrue(after[0].startswith("# A 64 GB DGX Spark cannot hold this lane"), after[0])
        self.assertTrue(after[2].startswith('[ "$ACTION" = uninstall ] || [ "$(box_mem_class)" != 64 ] || lane_fits '), after[2])
        return after[2]

    def test_each_refuses_a_64_gb_box_and_lets_a_128_gb_one_through(self):
        for script, lane in SIDE.items():
            with self.subTest(script):
                line = self.guard(script)
                self.assertIn(f"lane_fits {lane} ||", line)
                rc, out, err = bash('ACTION=install\n' + line + '\necho "went on"', "64")
                self.assertEqual(rc, 1, err)
                self.assertNotIn("went on", out)
                self.assertIn("needs a 128 GB DGX Spark, and this box is a 64 GB one.", err)
                self.assertEqual(bash('ACTION=install\n' + line + '\necho "went on"', "128")[:2], (0, "went on\n"))
                # its --uninstall takes away what an older release put there, on a 64 GB box too
                self.assertEqual(bash('ACTION=uninstall\n' + line + '\necho "went on"', "64")[:2], (0, "went on\n"))

    def test_nothing_runs_before_the_refusal_but_the_script_finding_itself(self):
        # what comes before the library: comments, set, die(), the arguments and HERE, no command
        # that fetches, pulls, writes a unit or asks for root
        for script in SIDE:
            with self.subTest(script):
                head = (REPO / script).read_text().split(SOURCE, 1)[0]
                for ln in head.splitlines():
                    self.assertNotRegex(ln.strip(), r"^(docker|sudo|curl|wget|pip3?|systemctl|hf|huggingface-cli) ",
                                        f"{script} runs this before the refusal")


class Bolt(unittest.TestCase):
    def test_bolt_takes_the_share_of_its_class(self):
        text = (REPO / "install-nemotron.sh").read_text()
        pins = "\n".join(ln for ln in text.splitlines() if re.match(r"NEMOTRON_BOLT_(GMU|GMU_64|GB)=", ln))
        pick = next(ln for ln in text.splitlines() if ln.startswith('GMU="$NEMOTRON_BOLT_GMU"'))
        pick64 = next(ln for ln in text.splitlines() if "GMU=\"$NEMOTRON_BOLT_GMU_64\"" in ln)
        for cls, want in (("128", "0.70"), ("64", "0.60")):
            with self.subTest(cls):
                rc, out, err = bash(f'{pins}\n{pick}\n{pick64}\necho "$GMU"', cls)
                self.assertEqual((rc, out), (0, want + "\n"), err)

    def test_a_switch_on_a_64_gb_box_writes_its_share(self):
        text = (REPO / "switch-model.sh").read_text()
        self.assertIn('NEMOTRON_BOLT_GMU_64', (REPO / "install-nemotron.sh").read_text())
        self.assertIn("|GMU|GMU_64|GB)=", text, "the switch reads the 64 GB share with the others")
        line = next(ln for ln in text.splitlines() if 'eval "NEM_GMU=' in ln and "_GMU_64" in ln)
        rc, out, err = bash(f'NEM_PIN=NEMOTRON_BOLT; NEMOTRON_BOLT_GMU_64=0.60; NEM_GMU=0.70\n{line}\necho "$NEM_GMU"', "64")
        self.assertEqual((rc, out), (0, "0.60\n"), err)
        rc, out, err = bash(f'NEM_PIN=NEMOTRON_BOLT; NEMOTRON_BOLT_GMU_64=0.60; NEM_GMU=0.70\n{line}\necho "$NEM_GMU"', "128")
        self.assertEqual((rc, out), (0, "0.70\n"), err)


class TheSwitch(unittest.TestCase):
    def test_a_target_a_64_gb_box_cannot_hold_is_refused_before_anything(self):
        text = (REPO / "switch-model.sh").read_text()
        src = '. "$REPO_DIR/lanes.sh"\n'
        after = text.split(src, 1)[1].splitlines()
        self.assertTrue(after[1].startswith('[ "$(box_mem_class)" != 64 ] || target_fits "$CHOICE"'), after[1])
        for choice, goes in (("stock", True), ("uncensored", True), ("nemotron", True), ("fp8", False),
                             ("uncensored-fp8", False), ("flash", False), ("image-turbo", False),
                             ("video", False), ("kolibri-nvfp4", False), ("nemotron-super", False),
                             ("flash-local", False), ("ornith-122b", False)):
            with self.subTest(choice):
                rc, out, err = bash(f'CHOICE={choice}\n{after[1]}\necho "went on"', "64")
                self.assertEqual(rc == 0 and out == "went on\n", goes, err)
                if not goes:
                    self.assertIn(f"The {choice} target needs a 128 GB DGX Spark", err)
                self.assertEqual(bash(f'CHOICE={choice}\n{after[1]}\necho "went on"', "128")[:2], (0, "went on\n"))


class The27BUnit(unittest.TestCase):
    def test_a_64_gb_box_gets_the_27b_unit_sized_for_it(self):
        text = (REPO / "install.sh").read_text()
        i = text.index('  UNIT_TPL="$REPO_DIR/qwen38-sglang.service.template"\n')
        block = text[i:text.index("\n", text.index("qwen38-sglang-64.service.template", i)) + 1]
        for cls, ctx, want in (("64", "native", "qwen38-sglang-64.service.template"),
                               ("128", "native", "qwen38-sglang.service.template"),
                               ("128", "1m", "qwen38-sglang-1m.service.template")):
            with self.subTest((cls, ctx)):
                rc, out, err = bash(f'REPO_DIR=R; MEM_CLASS={cls}; CONTEXT_MODE={ctx}\n{block}echo "$UNIT_TPL"', cls)
                self.assertEqual((rc, out), (0, f"R/{want}\n"), err)

    def test_the_conformity_list_recommends_the_window_a_box_holds(self):
        for cls, want in (("64", "native"), ("128", "1m")):
            with self.subTest(cls):
                r = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
                                    "import conformity; print(conformity.recommended()['CONTEXT_MODE'])", str(REPO)],
                                   capture_output=True, text=True, env={**os.environ, "QWEN38_MEMORY_CLASS": cls})
                self.assertEqual(r.stdout.strip(), want, r.stderr)


INSTALL = (REPO / "install.sh").read_text()


def between(text, start, end):
    i = text.index(start)
    return text[i:text.index(end, i) + len(end)]


# the 27B's first pull, once its target is known
PREPARE_27B_PULL = '  local img_need=48\n'
# where it hands a side lane to that lane's installer
PREPARE_SIDE = '    exec bash "$HERE/install-$1.sh" --prepare ;;\n'


class ANew64GBBoxServesBolt(unittest.TestCase):
    """A 64 GB DGX Spark serves Nemotron 3.5 Bolt (Hasan, 2026-10-24): the 27B gets 2 requests and a
    small pool on that budget. A new box ends its install on Bolt, loaded as the cockpit's Load
    does it: prepare-lane.sh, switch-model.sh (which only points the boot and opencode at it), the
    27B stopped, Bolt started and answering; anything that fails puts the box back on the 27B.
    install.sh's own lines run here, with prepare-lane.sh, switch-model.sh, sudo, systemctl, curl
    and oc-fit-limits.py replaced by stubs that write down their calls (found in review, 2026-10-24:
    the first epilogue stopped at the switch and said Bolt served while the 27B did). The stand-in
    for oc-fit-limits.py answers as the tool does to a vLLM engine, which has neither /server_info
    nor /get_server_info (keepalive-proxy.py, v6.37): one line and a return code of 1."""

    def epilogue(self, cls="64", side_boot="0", secondary="", env_choice="", want="1", recommended="0",
                 ran_before=None, fail=(), health="ok"):
        d = pathlib.Path(tempfile.mkdtemp(prefix="bolt-first-"))
        self.addCleanup(shutil.rmtree, d, True)
        calls = d / "calls"
        def stub(name, body):
            (d / name).write_text("#!/bin/bash\n" + body)
            (d / name).chmod(0o755)
        for name in ("prepare-lane.sh", "switch-model.sh"):
            rc = 1 if any(f == name or f == f"{name} nemotron" for f in fail) else 0
            (d / name).write_text(f'echo "{name} $*" >> "{calls}"\n'
                                  f'[ "$*" = nemotron ] && exit {rc}\nexit 0\n')
        (d / "oc-fit-limits.py").write_text(
            f'open("{calls}", "a").write("oc-fit-limits.py\\n")\n'
            'print("no engine to read at http://127.0.0.1:30000 (HTTPError): start one first, "\n'
            '      "the pool is only known once it has booted")\nraise SystemExit(1)\n')
        start_rc = 1 if "start" in fail else 0
        stub("sudo", f'echo "sudo $*" >> "{calls}"\n[ "$2" = start ] && [ "$3" = qwen38-nemotron.service ] && exit {start_rc}\nexit 0\n')
        stub("systemctl", 'echo "active"\n' if health != "failed" else 'case "$*" in *nemotron*) echo failed ;; *) echo active ;; esac\n')
        stub("curl", f'echo "curl $*" >> "{calls}"\nexit {0 if health == "ok" else 7}\n')
        stub("sleep", "exit 0\n")
        cfg = d / "cfg"
        cfg.mkdir()
        if ran_before:
            (cfg / "installed.json").write_text(json.dumps({"path": ran_before}))
        helpers = between(INSTALL, "ran_whole_before() {", "record_install() {")
        helpers = helpers[:helpers.rindex("record_install() {")]
        block = between(INSTALL, '    [ "$LANE" = "27b" ] && echo "  Benchmark  : ./bench.sh"\n', "    record_install full\n")
        script = ("set -euo pipefail\nstep(){ echo \"== $*\"; }\nrecord_install(){ :; }\n"
                  f'PATH="{d}:$PATH"; REPO_DIR="{d}"; CONFIG_DIR="{cfg}"; PORT=30000; UNIT_NAME=qwen38-sglang.service\n'
                  f'MODEL_CHOICE=stock; LANE=27b; MEM_CLASS={cls}; SIDE_BOOT={side_boot}; SECONDARY="{secondary}"\n'
                  f'_ENV_MODEL_CHOICE="{env_choice}"; WANT_NEMOTRON={want}; RECOMMENDED={recommended}\n'
                  + helpers + block)
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout, calls.read_text().splitlines() if calls.exists() else []

    LOAD = ["prepare-lane.sh nemotron", "switch-model.sh nemotron", "sudo systemctl stop qwen38-sglang.service",
            "sudo systemctl start qwen38-nemotron.service"]

    def test_a_new_64_gb_box_ends_its_install_on_bolt_serving(self):
        out, calls = self.epilogue()
        self.assertEqual(calls[:4], self.LOAD)
        self.assertTrue(calls[4].startswith("curl ") and "/health" in calls[4], calls)
        self.assertIn("Nemotron 3.5 Bolt serves now, and at boot", out)

    def test_a_bolt_that_serves_is_not_asked_for_a_pool_it_does_not_have(self):
        """Bolt's opencode limits are its own pair, written before its engine starts: nothing is
        fitted after its first load. The fit that stood there reads SGLang's /server_info, which vLLM
        does not have, so on a box with opencode on the ending of every such install said "no engine
        to read ... start one first" under a Bolt that served, then gave a NOTE naming a cockpit
        button which refuses on this lane (read in the code, 2026-11-10)."""
        out, calls = self.epilogue()
        self.assertEqual(calls[:4], self.LOAD)
        self.assertEqual(calls[5:], [], "nothing runs after the probe that found Bolt answering")
        self.assertNotIn("no engine to read", out)
        self.assertNotIn("NOTE", out)
        self.assertNotIn("not fitted", out)
        self.assertTrue(out.rstrip().endswith("the 27B is a Load away in the cockpit"), out)

    def test_a_bolt_that_does_not_answer_puts_the_box_back_on_the_27b(self):
        for fail, health in (((), "failed"), (("start",), "ok")):
            with self.subTest(fail=fail, health=health):
                out, calls = self.epilogue(fail=fail, health=health)
                self.assertEqual(calls[:4], self.LOAD)
                tail = [c for c in calls[4:] if not c.startswith("curl ")]
                self.assertEqual(tail, ["sudo systemctl stop qwen38-nemotron.service", "switch-model.sh stock",
                                        "sudo systemctl start qwen38-sglang.service"])
                self.assertIn("so the 27B serves this box", out)
                self.assertNotIn("Bolt serves now", out)

    def test_a_prepare_or_a_switch_that_fails_stops_nothing(self):
        out, calls = self.epilogue(fail=("prepare-lane.sh",))
        self.assertEqual(calls, ["prepare-lane.sh nemotron"])
        out, calls = self.epilogue(fail=("switch-model.sh nemotron",))
        self.assertEqual(calls, ["prepare-lane.sh nemotron", "switch-model.sh nemotron", "switch-model.sh stock"])
        self.assertNotIn("Bolt serves now", out)

    def test_a_first_run_that_did_not_end_whole_is_new_and_one_that_did_is_not(self):
        for ran, moved in ((None, True), ("no-start", True), ("full", False), ("side-boot", False)):
            with self.subTest(ran=ran):
                out, calls = self.epilogue(ran_before=ran)
                self.assertEqual(calls[:1] == ["prepare-lane.sh nemotron"], moved, calls)
        # --recommended is what a new box gets
        out, calls = self.epilogue(ran_before="full", recommended="1")
        self.assertEqual(calls[:4], self.LOAD)

    def test_nothing_else_is_moved(self):
        for kw in ({"cls": "128"}, {"side_boot": "1"}, {"secondary": "27b"}, {"env_choice": "uncensored"},
                   {"want": "0"}):
            with self.subTest(**kw):
                out, calls = self.epilogue(**kw)
                self.assertEqual(calls, [])
                self.assertNotIn("Bolt", out)

    def test_recommended_keeps_bolt_on_a_64_gb_box(self):
        # --recommended is what a new box gets, and a new 64 GB box serves Bolt
        d = pathlib.Path(tempfile.mkdtemp(prefix="bolt-rec-"))
        self.addCleanup(shutil.rmtree, d, True)
        (d / "systemctl").write_text('#!/bin/sh\ncase "$*" in *qwen38-nemotron.service*) exit 0 ;; esac\nexit 1\n')
        (d / "systemctl").chmod(0o755)
        block = between(INSTALL, "# The Nemotron lane is one too", "# And the Nemotron Super lane")
        for cls, boot, said in (("64", "1", "The Nemotron 3.5 Bolt lane is this box's boot lane"),
                                ("128", "0", "the 27B lane becomes the boot lane again")):
            with self.subTest(cls=cls):
                script = (f'set -euo pipefail\nPATH="{d}:$PATH"\nSECONDARY=""; SGL_ENABLED=0; FLASH_ENABLED=0\n'
                          f'_ENV_MODEL_CHOICE=""; RECOMMENDED=1; MEM_CLASS={cls}\n' + block + '\necho "BOOT=$NEMOTRON_BOOT"\n')
                r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn(f"BOOT={boot}", r.stdout)
                self.assertIn(said, r.stdout)


class TheRunSaysIt(unittest.TestCase):
    """What install.sh says of a 64 GB box, run from its own lines (found in review, 2026-10-23)."""
    def too_big_fn(self):
        return between(INSTALL, "too_big(){", "\n")

    def test_a_lane_the_box_cannot_hold_needs_128_gb_and_was_left_out_by_no_flag(self):
        lane_line = between(INSTALL, "    lane_line(){", "\n    }\n")
        script = (f"TOO_BIG=(video flash)\nCOCKPIT=1\n{self.too_big_fn()}{lane_line}"
                  'lane_line "Videos     " off video video "174 GB" /nonexistent\n'
                  'lane_line "Images     " off image image "46 GB" /nonexistent\n')
        rc, out, err = bash(script, "64")
        self.assertEqual(rc, 0, err)
        self.assertIn("Videos     : needs a 128 GB DGX Spark, and this box is a 64 GB one", out)
        self.assertNotIn("--no-video", out)
        self.assertIn("Images     : left out by --no-image on this run", out)

    def test_the_other_text_lane_too(self):
        block = between(INSTALL, '    case "$SECOND_STATE" in\n      installed) echo "  the ${SECOND_LANE/27b/27B} lane is up to date',
                        "\n    esac\n")
        rc, out, err = bash(f"TOO_BIG=(flash)\nSECOND_LANE=flash\nSECOND_STATE=off\nSECOND_CANNOT=\n{self.too_big_fn()}{block}", "64")
        self.assertEqual((rc, out.strip()), (0, "the flash lane needs a 128 GB DGX Spark, and this box is a 64 GB one"), err)

    def test_a_27b_unit_serving_a_model_of_its_own_still_updates(self):
        block = between(INSTALL, 'if [ "$MEM_CLASS" = 64 ]; then\n  # run.sh serves', "\nfi\n")
        for choice, goes in (("custom", True), ("stock", True), ("fp8", False)):
            with self.subTest(choice):
                rc, out, err = bash(f"MEM_CLASS=64\nNO_SERVICE=0\nLANE=27b\nCONTEXT_MODE=\nMODEL_CHOICE={choice}\n{block}"
                                    'echo "went on"', "64")
                self.assertEqual(rc == 0 and out == "went on\n", goes, err)
                self.assertNotIn("invalid choice", err)

    def test_no_service_is_refused_as_run_sh_serves_a_128_gb_box(self):
        block = between(INSTALL, 'if [ "$MEM_CLASS" = 64 ]; then\n  # run.sh serves', "\nfi\n")
        rc, out, err = bash(f"MEM_CLASS=64\nNO_SERVICE=1\nLANE=27b\nCONTEXT_MODE=\nMODEL_CHOICE=stock\n{block}"
                            'echo "went on"', "64")
        self.assertEqual(rc, 1, err)
        self.assertIn("--no-service prepares ./run.sh, which runs the 27B with the memory settings of a 128 GB DGX Spark", err)

    def test_a_forced_class_that_is_no_class_is_refused_by_its_name(self):
        for cls, ok in (("64", True), ("128", True), ("", True), ("64GB", False), ("96", False)):
            with self.subTest(cls):
                env = {k: v for k, v in os.environ.items() if not k.startswith("QWEN38_")}
                r = subprocess.run(["bash", "-c", f'. "{REPO}/lanes.sh"\necho sourced'], capture_output=True,
                                   text=True, timeout=30, env={**env, "QWEN38_MEMORY_CLASS": cls})
                self.assertEqual((r.returncode, r.stdout), (0, "sourced\n") if ok else (1, ""), r.stderr)
                if not ok:
                    self.assertIn(f"QWEN38_MEMORY_CLASS={cls} is not a memory class: 64 or 128", r.stderr)

    def test_prepare_lane_refuses_what_a_64_gb_box_cannot_hold_before_a_pull(self):
        # the cockpit's Load runs it before its switch, which refused these only after the image
        # and the draft were pulled (found in review, 2026-10-23). A walled copy, stopped before the
        # 27B's first pull, a 27B unit of its own in place and docker fenced: a refusal that slipped
        # meets the wall, never this box's units, images or checkpoints (found in review, 2026-10-23).
        # The 27B's target is checked once it is known: by its name, by its repo, or the unit's own;
        # a typo is a typo, and stock by its repo goes on (found in review, 2026-10-23)
        repos = dict(re.findall(r'^(STOCK|UNC|FP8|UNCFP8)_REPO="([^"]+)"', (REPO / "install.sh").read_text(), re.M))
        def unit(model):
            return ("[Service]\nExecStart=/bin/bash -c 'exec /usr/bin/docker run --rm --name qwen38-sglang \\\n"
                    "  -v /nowhere/hf:/root/.cache/huggingface \\\n  lmsysorg/sglang@sha256:0 \\\n"
                    f"  python3 -m sglang.launch_server --model-path {model} --port 30000'\n")
        too_big = "The fp8 target needs a 128 GB DGX Spark, and this box is a 64 GB one."
        for args, served, rc, said in ((("27b", "fp8"), repos["STOCK"], 1, too_big),
                                       (("27b", repos["FP8"]), repos["STOCK"], 1, too_big),
                                       (("27b",), repos["FP8"], 1, too_big),
                                       (("27b", "fp9"), repos["STOCK"], 1, "fp9 is not a 27B lane target"),
                                       (("27b", repos["STOCK"]), repos["STOCK"], 97, wall.WALL),
                                       (("27b", "uncensored"), repos["STOCK"], 97, wall.WALL),
                                       (("flash",), repos["STOCK"], 1, "The flash lane needs a 128 GB DGX Spark")):
            with self.subTest(args=args, served=served):
                units = wall.units_dir({"qwen38-sglang.service": unit(served)})
                script = wall.walled("prepare-lane.sh", at=PREPARE_27B_PULL, units=units)
                shutil.copy(REPO / "install.sh", pathlib.Path(script).parent)     # the pins it reads
                env, record = wall.fenced_env(QWEN38_MEMORY_CLASS="64")
                r = subprocess.run(["bash", script, *args], capture_output=True, text=True, timeout=60, env=env)
                self.assertEqual(r.returncode, rc, (r.stdout + r.stderr)[-400:])
                self.assertIn(said, r.stdout + r.stderr)
                if said != too_big and "flash lane" not in said:
                    self.assertNotIn("needs a 128 GB DGX Spark", r.stderr)
                self.assertEqual(wall.reached(record), [])

    def test_run_sh_refuses_a_64_gb_box(self):
        # it runs the 27B with a 128 GB box's memory settings and took MODEL_CHOICE=fp8: refused there,
        # as --no-service, which prepares it, is (found in review, 2026-10-23). Its throwaway HOME holds
        # no key, so a run that went on stops at that, and docker is fenced
        said = "./run.sh runs the 27B with the memory settings of a 128 GB DGX Spark, and this box is a 64 GB one."
        for cls, choice in (("64", "stock"), ("64", "fp8"), ("128", "stock")):
            with self.subTest(cls=cls, choice=choice):
                env, record = wall.fenced_env(QWEN38_MEMORY_CLASS=cls, MODEL_CHOICE=choice)
                r = subprocess.run(["bash", str(REPO / "run.sh")], capture_output=True, text=True, timeout=60, env=env)
                self.assertEqual(r.returncode, 1, r.stderr[-400:])
                (self.assertIn if cls == "64" else self.assertNotIn)(said, r.stderr)
                self.assertEqual(wall.reached(record), [])

    def test_the_help_and_the_readme_say_it_too(self):
        # the refusal was said in the README's 64 GB section only: the help, the table of choices
        # and the line that gives the path offered --no-service with no word of it (found in review,
        # 2026-10-23)
        help_text = re.sub(r"\s+", " ", between(INSTALL, "  --no-service ", "  --no-opencode"))
        self.assertIn("a 64 GB one refuses it", help_text)
        readme = (REPO / "README.md").read_text()
        row = [ln for ln in readme.splitlines() if ln.startswith("| systemd service |")]
        self.assertEqual(len(row), 1)
        self.assertIn("refused on a 64 GB DGX Spark", row[0])
        path = [ln for ln in readme.splitlines() if ln.startswith("- **Don't want a systemd service?**")]
        self.assertEqual(len(path), 1)
        self.assertIn("a 64 GB one refuses both", path[0])

    def test_prepare_lane_says_a_side_lane_needs_128_gb_not_an_install(self):
        # its unit is never there on a 64 GB box, and ./install.sh leaves the lane out: the old
        # answer sent the owner round in a circle (found in review, 2026-10-23). Walled before the
        # lane's own installer; a lane that fits, with its unit, goes on to it
        units = wall.units_dir({"qwen38-nemotron.service": "[Service]\n"})
        script = wall.walled("prepare-lane.sh", at=PREPARE_SIDE, units=units)
        for lane in ("image", "video", "kolibri", "nemotron-super", "flash-local", "ornith-122b"):
            with self.subTest(lane):
                env, record = wall.fenced_env(QWEN38_MEMORY_CLASS="64")
                r = subprocess.run(["bash", script, lane], capture_output=True, text=True, timeout=60, env=env)
                self.assertEqual(r.returncode, 1, (r.stdout + r.stderr)[-400:])
                self.assertIn(f"The {lane} lane needs a 128 GB DGX Spark, and this box is a 64 GB one.", r.stderr)
                self.assertNotIn("./install.sh installs its units", r.stderr)
                self.assertEqual(wall.reached(record), [])
        env, record = wall.fenced_env(QWEN38_MEMORY_CLASS="64")
        r = subprocess.run(["bash", script, "nemotron"], capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual((r.returncode, wall.WALL in r.stdout), (97, True), (r.stdout + r.stderr)[-400:])

    def test_opencode_s_limits_are_fitted_to_the_pool_on_a_64_gb_box(self):
        # the native pair was sized on a 128 GB box's pool: a 64 GB box gets the fit 1m gets
        # (found in review, 2026-10-23)
        cond = '{ [ "$CONTEXT_MODE" = "1m" ] || [ "$MEM_CLASS" = 64 ]; } && [ "$OPENCODE" -eq 1 ]'
        self.assertEqual(INSTALL.count(f"if {cond}; then"), 2)
        self.assertNotIn('if [ "$CONTEXT_MODE" = "1m" ] && [ "$OPENCODE" -eq 1 ]; then', INSTALL)
        block = between(INSTALL, f"    if {cond}; then\n      echo", "\n    fi\n")
        for mode, cls, opencode, fits in (("native", "64", 1, True), ("1m", "128", 1, True),
                                          ("native", "128", 1, False), ("native", "64", 0, False)):
            with self.subTest(mode=mode, cls=cls, opencode=opencode):
                rc, out, err = bash(f"CONTEXT_MODE={mode}\nMEM_CLASS={cls}\nOPENCODE={opencode}\nREPO_DIR=R\nPORT=30000\n"
                                    f"python3(){{ echo FIT; }}\n{block}", cls)
                self.assertEqual(rc, 0, err)
                self.assertEqual("FIT" in out, fits, out)

    def test_the_lanes_that_fit_come_from_boxmem(self):
        r = subprocess.run([sys.executable, "-I", str(REPO / "boxmem.py"), "fitting", "--class", "64"],
                           capture_output=True, text=True)
        self.assertEqual(r.stdout.split(), ["27b", "nemotron", "speech"])
        self.assertNotIn("the 27B and Nemotron 3.5 Bolt here", INSTALL, "a second list of them in install.sh")

class NothingUnmeasured(unittest.TestCase):
    def test_no_64_gb_value_ships_before_it_is_measured(self):
        # the values the 64 GB lanes started from carry this mark until a run on a 64 GB budget
        # replaced them: none may reach a release
        marked = subprocess.run(["git", "-C", str(REPO), "grep", "-l", "PROVISIONAL 64 GB VALUE"],
                                capture_output=True, text=True).stdout.split()
        self.assertEqual([m for m in marked if m != "tests/test_spark_64.py"], [])


if __name__ == "__main__":
    unittest.main()
