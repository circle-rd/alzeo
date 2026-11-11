#!/usr/bin/env python3
"""A lane installed on demand comes in two halves (decided 2026-10-14): --units-only puts in
what needs root (the unit, its start scripts) and nothing heavy, and --prepare, which the
cockpit's first Load of the lane runs as the user, builds its runtime with no sudo at all.
The weights come with that Load's switch, as for any Load.

The installers run whole here, on a copy whose unit path is the test's own, with fakes for
everything that acts on the machine: sudo, systemctl and docker record their calls (sudo
runs what follows it only when the test allows root), python3 and git stand in for the venv,
the wheel and the source, and uname says aarch64 on any machine the suite runs on.
"""
import os
import pathlib
import re
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]

# python3 and the venv's python: the checks the installers ask answer yes, a venv is a folder
# with this same script as its python, pip records what it would install, an import of sglang
# fails until the wheel is "installed", and the heredoc checks of the runtime pass.
FAKE_PY = r'''#!/bin/bash
printf 'python %s\n' "$*" >> "$CALLS"
case "$1" in
  -m)
    if [ "$2" = venv ]; then d="${@: -1}"; mkdir -p "$d/bin"; cp "$0" "$d/bin/python"; ln -sf python "$d/bin/pip"; exit 0; fi
    if [ "$2" = pip ]; then [ "$3" = install ] && case "$*" in *sglang*) touch "$FAKE_STATE/sglang" ;; esac; exit 0; fi ;;
  -c)
    case "$2" in
      *sysconfig*) echo "$FAKE_STATE/include"; exit 0 ;;
      *"import sglang, pathlib"*) exit 0 ;;
      *"import sglang"*) [ -e "$FAKE_STATE/sglang" ]; exit ;;
    esac
    exit 0 ;;
  -) cat > /dev/null; exit 0 ;;
esac
case "$(basename "$0")" in pip) case "$*" in *sglang*) touch "$FAKE_STATE/sglang" ;; esac ;; esac
exit 0
'''
FAKE_GIT = r'''#!/bin/bash
printf 'git %s\n' "$*" >> "$CALLS"
if [ "$1" = clone ]; then mkdir -p "${@: -1}/.git"; exit 0; fi
case "$*" in *rev-parse*) echo "$FAKE_PIN"; exit 0 ;; *"apply --reverse --check"*) exit 0 ;; esac
exit 0
'''
RECORD = '#!/bin/bash\nprintf "%s %s\\n" "$(basename "$0")" "$*" >> "$CALLS"\n'
FAKE_SUDO = RECORD + '[ -n "$ROOT_OK" ] && exec "$@"\nexit 1\n'


class Box:
    def __init__(self, installer, root_ok=False):
        self.t = pathlib.Path(tempfile.mkdtemp(prefix="on-demand-"))
        self.home = self.t / "home"
        (self.home / ".config/qwen38").mkdir(parents=True)
        (self.home / ".config/qwen38/api-key").write_text("k\n")
        self.state = self.t / "state"
        (self.state / "include").mkdir(parents=True)
        (self.state / "include" / "Python.h").write_text("")
        self.units = self.t / "units"
        self.units.mkdir()
        repo = self.t / "repo"
        repo.mkdir()
        text = (REPO / installer).read_text()
        old = '"/etc/systemd/system/$UNIT"'
        # the vLLM lanes' installers name their unit where lanes.sh reads every other one (LANES_UNIT_DIR,
        # which run() points at this box's units, for every installer: none reads the real ones)
        if '"${LANES_UNIT_DIR:-/etc/systemd/system}/$UNIT"' in text:
            (repo / installer).write_text(text)
        else:
            assert old in text, f"{installer} no longer names its unit that way"
            (repo / installer).write_text(text.replace(old, f'"{self.units}/$UNIT"'))
        for f in REPO.iterdir():
            if f.is_file() and f.name != installer and (f.suffix in (".sh", ".py", ".template") or f.name.endswith(".service.template")):
                (repo / f.name).write_text(f.read_text())
        for d in ("image-sglang", "video-sglang"):
            if (REPO / d).is_dir():
                (repo / d).mkdir()
                for f in (REPO / d).iterdir():
                    (repo / d / f.name).write_text(f.read_text())
        self.script = repo / installer
        self.bin = self.t / "bin"
        self.bin.mkdir()
        for name, body in (("python3", FAKE_PY), ("git", FAKE_GIT), ("sudo", FAKE_SUDO),
                           ("systemctl", RECORD + '[ "$1" = daemon-reload ] && exit 0\nexit 1\n'),
                           ("docker", RECORD + "exit 1\n"),
                           ("nvidia-smi", RECORD + "exit 0\n"), ("uname", "#!/bin/sh\necho aarch64\n"),
                           ("ffmpeg", RECORD + "exit 0\n"), ("ffprobe", RECORD + "exit 0\n")):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        self.calls = self.t / "calls"
        self.root_ok = root_ok
        pin = re.search(r'SGLANG_DIFFUSION_PIN:-([0-9a-f]{40})', text)
        self.pin = pin.group(1) if pin else ""

    def run(self, *args):
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.home), "CALLS": str(self.calls),
               "FAKE_STATE": str(self.state), "FAKE_PIN": self.pin, "HF_CACHE": str(self.t / "hf"),
               "LANES_UNIT_DIR": str(self.units)}
        if self.root_ok:
            env["ROOT_OK"] = "1"
        r = subprocess.run(["bash", str(self.script), *args], capture_output=True, text=True, env=env, timeout=120)
        calls = self.calls.read_text().splitlines() if self.calls.exists() else []
        return r.returncode, r.stdout + r.stderr, calls


# The image and video lanes' installers have the same two halves: one test each, with a subTest
# per lane (the CI counts every test once against what unittest runs, and a test class inheriting
# another's tests ran them twice under one name).
LANES_IN_TWO_HALVES = (("install-image.sh", ".local/share/qwen38-image", "qwen38-image.service"),
                       ("install-video.sh", ".local/share/qwen38-video", "qwen38-video.service"))


class TheImageAndVideoLanesInTwoHalves(unittest.TestCase):
    def test_units_only_puts_in_the_unit_and_nothing_heavy(self):
        for installer, lane, unit in LANES_IN_TWO_HALVES:
            with self.subTest(installer=installer):
                box = Box(installer, root_ok=True)
                rc, out, calls = box.run("--units-only")
                self.assertEqual(rc, 0, out)
                self.assertTrue((box.units / unit).exists(), out)
                self.assertFalse((box.home / lane / "venv").exists(), "a venv was made")
                self.assertFalse([c for c in calls if c.startswith(("git ", "python -m pip", "python -m venv"))], calls)
                self.assertFalse([c for c in calls if "ckpt-fetch.py" in c], calls)
                self.assertIn("first Load", out)

    def test_prepare_builds_the_runtime_with_no_sudo_and_no_unit(self):
        for installer, lane, unit in LANES_IN_TWO_HALVES:
            with self.subTest(installer=installer):
                box = Box(installer)
                rc, out, calls = box.run("--prepare")
                self.assertEqual(rc, 0, out)
                self.assertTrue((box.home / lane / "venv/bin/python").exists(), out)
                self.assertTrue((box.home / lane / "sglang-wheel").exists(), out)
                self.assertFalse([c for c in calls if c.startswith(("sudo ", "systemctl ", "docker "))], calls)
                self.assertFalse((box.units / unit).exists(), "--prepare wrote the unit")
                self.assertFalse([c for c in calls if "ckpt-fetch.py" in c], calls)
                # its checkpoint is not there, and not for it to fetch: it said "already cached"
                self.assertIn("checkpoint left to the Load's switch", out)
                self.assertNotIn("already cached", out)

    def test_a_preparation_stopped_on_the_way_leaves_no_note(self):
        # the note is what says prepared: a run that stops after the wheel (here its clone fails)
        # leaves none, and the next Load prepares again instead of serving the bare wheel, with
        # none of the patches (found in review, 2026-10-15: the note came right after the wheel)
        no_clone = FAKE_GIT.replace('if [ "$1" = clone ]; then mkdir -p "${@: -1}/.git"; exit 0; fi',
                                    'if [ "$1" = clone ]; then exit 128; fi')
        assert no_clone != FAKE_GIT
        for installer, lane, _unit in LANES_IN_TWO_HALVES:
            with self.subTest(installer=installer):
                box = Box(installer)
                (box.bin / "git").write_text(no_clone)
                rc, out, calls = box.run("--prepare")
                self.assertNotEqual(rc, 0, out)
                self.assertIn("could not clone SGLang", out)
                self.assertTrue([c for c in calls if "sglang[diffusion]" in c], calls)     # the wheel went in
                self.assertTrue((box.home / lane / "venv/bin/python").exists(), out)
                self.assertFalse((box.home / lane / "sglang-wheel").exists(), "a half-built runtime has its note")

    def test_a_run_that_changes_nothing_leaves_the_notes_date(self):
        # the note's date is one of the runtime's (the lane serves older files, the cockpit's
        # stale-runtime check that turns the video lane's Turbo off): written at every run, it said
        # so after every update (found in review, 2026-10-15)
        for installer, lane, _unit in LANES_IN_TWO_HALVES:
            with self.subTest(installer=installer):
                box = Box(installer)
                self.assertEqual(box.run("--prepare")[0], 0)
                note = box.home / lane / "sglang-wheel"
                first = note.stat().st_mtime_ns
                __import__("time").sleep(0.05)
                rc, out, _ = box.run("--prepare")
                self.assertEqual(rc, 0, out)
                self.assertEqual(note.stat().st_mtime_ns, first)

    def test_a_new_wheel_rewrites_the_note(self):
        # written only when it changes: a wheel of another release reinstalls the runtime and the
        # note names the new one, which the cockpit compares with what the lane serves
        for installer, lane, _unit in LANES_IN_TWO_HALVES:
            with self.subTest(installer=installer):
                box = Box(installer)
                self.assertEqual(box.run("--prepare")[0], 0)
                note = box.home / lane / "sglang-wheel"
                wheel = note.read_text()
                note.write_text("0.5.19\n")
                rc, out, calls = box.run("--prepare")
                self.assertEqual(rc, 0, out)
                self.assertEqual(note.read_text(), wheel)
                self.assertNotEqual(wheel, "0.5.19\n")

    def test_the_two_halves_are_asked_for_one_at_a_time(self):
        for installer, _lane, _unit in LANES_IN_TWO_HALVES:
            with self.subTest(installer=installer):
                rc, out, calls = Box(installer).run("--units-only", "--prepare")
                self.assertNotEqual(rc, 0)
                self.assertEqual(calls, [])


class TheVideoLaneTurboAdapter(unittest.TestCase):
    def test_prepare_fetches_the_turbo_adapter_and_not_the_checkpoint(self):
        # the switch fetches the checkpoint; the adapter is the installer's alone to fetch
        rc, out, calls = Box("install-video.sh").run("--prepare")
        self.assertEqual(rc, 0, out)
        self.assertIn("python - larryvrh/MiniMax-H3-Turbo-Lora", "\n".join(calls))


# The three vLLM lanes, Kolibri and the two Nemotron lanes: one test each, with a subTest per lane (as
# above). Super VL's preparation is the Nemotron lane's, through install-nemotron.sh --prepare.
VLLM_LANES = (("install-kolibri.sh", "qwen38-kolibri.service"), ("install-nemotron.sh", "qwen38-nemotron.service"),
              ("install-nemotron-super.sh", "qwen38-nemotron-super.service"))


class TheVllmLanesInTwoHalves(unittest.TestCase):
    """The vLLM lanes' runtime is an image built on the box: --prepare builds it as the
    user (docker, no sudo). Here the image is already built, as the fake docker says; the build
    itself is the step's own lines, which call no sudo."""

    def box(self, installer, unit, **kw):
        box = Box(installer, **kw)
        (box.bin / "docker").write_text(RECORD + 'case "$1" in info|images) exit 0 ;; image) exit 0 ;; esac\nexit 1\n')
        for f in ("engine-secrets.sh", "vllm_engine_auth.py", unit + ".template"):
            assert (box.script.parent / f).exists(), f
        return box

    def test_units_only_puts_in_the_unit_and_builds_nothing(self):
        for installer, unit in VLLM_LANES:
            with self.subTest(installer=installer):
                box = self.box(installer, unit, root_ok=True)
                rc, out, calls = box.run("--units-only")
                self.assertEqual(rc, 0, out)
                self.assertTrue((box.units / unit).exists(), out)
                self.assertFalse([c for c in calls if c.startswith(("docker build", "docker pull", "docker run"))], calls)
                self.assertIn("first Load", out)

    def test_prepare_readies_the_image_with_no_sudo_and_no_unit(self):
        for installer, unit in VLLM_LANES:
            with self.subTest(installer=installer):
                box = self.box(installer, unit)
                rc, out, calls = box.run("--prepare")
                self.assertEqual(rc, 0, out)
                self.assertFalse([c for c in calls if c.startswith(("sudo ", "systemctl ", "docker run"))], calls)
                self.assertFalse((box.units / unit).exists())
                self.assertIn("image is ready", out)
                # its checkpoints are not for it to fetch, nor in the cache: it said "already cached"
                self.assertRegex(out, r"checkpoints? left to the Load's switch")
                self.assertNotIn("already cached", out)

    def test_the_engine_port_comes_from_the_proxy_unit_of_this_box(self):
        # every unit is read where LANES_UNIT_DIR says, its own and the others alike: the box's
        # proxy says 31234 here, and the real one of the machine running this, 30000, is never read
        # (the vLLM installers read the real proxy unit and the real units for their image's prune
        # while their own unit was this test's: found in review, 2026-10-16)
        for installer, unit in VLLM_LANES:
            with self.subTest(installer=installer):
                box = self.box(installer, unit, root_ok=True)
                (box.units / "qwen38-keepalive.service").write_text(
                    "[Service]\nEnvironment=UPSTREAM=http://127.0.0.1:31234\n")
                rc, out, _ = box.run("--units-only")
                self.assertEqual(rc, 0, out)
                self.assertIn("--port 31234", (box.units / unit).read_text())

    def test_the_build_step_calls_no_sudo(self):
        for installer, _ in VLLM_LANES:
            with self.subTest(installer=installer):
                text = (REPO / installer).read_text()
                build = text[text.index('step "2/5 Image ($LOCAL_IMAGE'):text.index('step "3/5 Checkpoint')]
                self.assertNotRegex(build, r"(^|[;&|(]\s*)sudo\b")



class TheDispatcher(unittest.TestCase):
    """prepare-lane.sh, what the cockpit's prepare action runs: the lane is an argument of its
    own (the cockpit never builds one argument out of a parameter), mapped to its installer's
    --prepare, and nothing else is taken."""

    def run_it(self, *args, units=("image", "video", "kolibri", "nemotron", "nemotron-super")):
        d = pathlib.Path(tempfile.mkdtemp(prefix="prepare-lane-"))
        (d / "units").mkdir()
        for lane in units:
            (d / "units" / f"qwen38-{lane}.service").write_text("[Unit]\n")
        (d / "prepare-lane.sh").write_text((REPO / "prepare-lane.sh").read_text().replace("/etc/systemd/system/", f"{d}/units/"))
        # the library a side lane's branch sources since the 64 GB DGX Spark, its units where the
        # copy's are, and the memory reader it asks; a 128 GB box's memory, so the machine the
        # tests run on, or a class exported in their shell, decides nothing here
        (d / "lanes.sh").write_text((REPO / "lanes.sh").read_text().replace("/etc/systemd/system", f"{d}/units"))
        (d / "boxmem.py").write_text((REPO / "boxmem.py").read_text())
        (d / "meminfo").write_text("MemTotal:       127533104 kB\nMemAvailable:   121000000 kB\n")
        for lane in ("image", "video", "kolibri", "nemotron", "nemotron-super"):
            (d / f"install-{lane}.sh").write_text(f'echo "install-{lane}.sh $*"\n')
        env = {k: v for k, v in os.environ.items() if not k.startswith("QWEN38_")}
        r = subprocess.run(["bash", str(d / "prepare-lane.sh"), *args], capture_output=True, text=True, timeout=30,
                           env={**env, "QWEN38_MEMINFO": str(d / "meminfo")})
        return r.returncode, (r.stdout + r.stderr).strip()

    def test_each_lane_runs_its_installers_prepare(self):
        for lane in ("image", "video", "kolibri", "nemotron", "nemotron-super"):
            with self.subTest(lane=lane):
                self.assertEqual(self.run_it(lane), (0, f"install-{lane}.sh --prepare"))

    def test_a_lane_with_no_unit_is_not_prepared(self):
        # its units come with ./install.sh; a preparation alone would build what nothing runs
        for lane in ("image", "video", "kolibri", "nemotron", "nemotron-super"):
            with self.subTest(lane=lane):
                rc, out = self.run_it(lane, units=())
                self.assertEqual(rc, 1, out)
                self.assertIn("not installed on this box", out)
                self.assertNotIn("--prepare", out)

    def test_anything_else_is_refused(self):
        for args in ((), ("27B",), ("../install",), ("image", "--uninstall"), ("image", "image"), ("",),
                     ("flash", "flash-nvda", "x")):
            with self.subTest(args=args):
                rc, out = self.run_it(*args)
                self.assertEqual(rc, 2, out)
                self.assertIn("usage", out)



class TheSwitchSendsALaneNotBuiltToItsPreparation(unittest.TestCase):
    """A switch to a side lane whose runtime is not built sent its user to re-run its whole
    installer (sudo, the weights, a smoke that stops the lane that serves): it names the
    preparation (found in review, 2026-10-15)."""

    def test_each_side_lane_names_prepare_lane(self):
        text = (REPO / "switch-model.sh").read_text()
        for lane in ("image", "video", "kolibri"):
            with self.subTest(lane=lane):
                self.assertIn(f"./prepare-lane.sh {lane} builds it (no sudo; its first Load from the cockpit runs it), then switch again", text)
        # the two Nemotron lanes' one block, entered for their two targets, which are their lanes' names
        self.assertIn('./prepare-lane.sh $CHOICE builds it (no sudo; its first Load from the cockpit runs it), then switch again', text)
        self.assertIn('if [ "$CHOICE" = "nemotron" ] || [ "$CHOICE" = "nemotron-super" ]; then', text)
        self.assertNotIn("runtime is missing ($IMG_PY): re-run", text)
        self.assertNotIn("runtime is missing ($VID_PY): re-run", text)


class TextBox:
    """prepare-lane.sh <flash|27b>, run as written in a HOME of the test's own, against a docker
    that records its calls and knows which images are on the box (one is, once pulled), a df that
    gives the room the test says, and a switch-model.sh and a patch-yarn.py that record theirs.
    The launcher and the unit are written as their templates write them."""

    PINS = dict(re.findall(r'^((?:FLASH(?:_NVDA|_UNC)?|STOCK|UNC|FP8|UNCFP8)_(?:REPO|REV))="([^"]+)"',
                           (REPO / "install.sh").read_text(), re.M))

    def __init__(self, image_here=False, free_gb=1000, free_at=(), hf_on=None):
        """free_at: (folder, GB) pairs, the room df gives under each (the rest has free_gb);
        hf_on: a folder on another disk the HF cache links to, as ~/.cache/huggingface does to
        an SSD on the reference box."""
        self.t = t = pathlib.Path(tempfile.mkdtemp(prefix="prepare-text-"))
        self.d = d = t / "repo"
        d.mkdir()
        for f in ("lanes.sh", "install.sh", "flash-ats-check.sh"):
            (d / f).write_text((REPO / f).read_text())
        self.units = t / "units"
        self.units.mkdir()
        (d / "prepare-lane.sh").write_text((REPO / "prepare-lane.sh").read_text().replace("/etc/systemd/system/", f"{self.units}/"))
        (d / "switch-model.sh").write_text(f'echo "switch-model.sh $*" >> "{t}/calls"\n')
        (d / "patch-yarn.py").write_text(f'import sys\nopen("{t}/calls", "a").write("patch-yarn.py " + " ".join(sys.argv[1:]) + "\\n")\n')
        self.hf, self.ple = t / "hf", t / "ple"
        if hf_on is not None:
            (pathlib.Path(hf_on) / "hf").mkdir()
            self.hf.symlink_to(pathlib.Path(hf_on) / "hf")
        else:
            self.hf.mkdir()
        self.ple.mkdir()
        self.cfg = t / "home" / ".config" / "qwen38"
        self.cfg.mkdir(parents=True)
        (t / "docker-root").mkdir()
        (t / "bin").mkdir()
        self.stub("docker", f'echo "docker $*" >> "{t}/calls"\n'
                  'case "$1 $2" in\n'
                  f'  "info "*) echo "{t}/docker-root" ;;\n'
                  '  "image inspect") case "$3" in qwen38-pinned:*) exit 1 ;; esac\n'
                  f'    [ -e "{t}/pulled" ] || [ {int(image_here)} = 1 ] || exit 1; echo sha256:abc ;;\n'
                  f'  "pull "*) touch "{t}/pulled" ;;\n'
                  'esac\nexit 0\n')
        self.stub("df", f'printf "Avail\\n{free_gb}G\\n"')
        # the reference box's GPU: the flash lane's first Load asks its addressing mode
        # (flash-ats-check.sh), and every case here asked the host's own GPU (review, 2026-10-19)
        self.stub("nvidia-smi", 'echo ATS')
        if free_at:
            # df answers for the disk of the folder it is given, a link followed
            self.stub("df", 'p="$(readlink -f "${@: -1}")"\ncase "$p" in\n'
                      + "".join(f'  "{pathlib.Path(d).resolve()}"*) printf "Avail\\n{gb}G\\n" ;;\n' for d, gb in free_at)
                      + f'  *) printf "Avail\\n{free_gb}G\\n" ;;\nesac\n')

    def stub(self, name, body):
        (self.t / "bin" / name).write_text(("#!/bin/bash\n" if "${@" in body else "#!/bin/sh\n") + body)
        (self.t / "bin" / name).chmod(0o755)

    def flash(self, model, image, map_line=True, ple="here"):
        ple = self.ple if ple == "here" else ple       # None: a launcher with no PLE table
        (self.cfg / "launch-flash.sh").write_text(
            "exec /usr/bin/docker run --rm --name qwen38-flash --gpus all \\\n"
            f"  -v {self.hf}:/root/.cache/huggingface \\\n  -v {self.cfg}:/out \\\n"
            + (f"  -v {ple}:/ple \\\n" if ple else "")
            + f"  -e HF_HUB_OFFLINE=1 \\\n  {image} \\\n  python3 -m sglang.launch_server \\\n"
            f"    --model-path {model} --tp-size 1 \\\n"
            + (f'[ -s "{self.cfg}/token-map-65536.pt" ] && TIER+=(--speculative-token-map /out/token-map-65536.pt)\n' if map_line else ""))

    def unit27(self, model, image, window="1m", draft="org/Qwen3.8-27B-DFlash2", drev="d" * 40):
        (self.units / "qwen38-sglang.service").write_text(
            "[Service]\nExecStart=/usr/bin/docker run --rm --name qwen38-sglang --gpus all \\\n"
            f"  -v {self.cfg}/sglang-cache:/cache \\\n  -v {self.hf}:/root/.cache/huggingface \\\n  -v {self.cfg}:/out \\\n"
            f"  {image} \\\n  python3 -m sglang.launch_server \\\n"
            f"    --trust-remote-code --model-path {model} --tp-size 1 \\\n"
            + ("    --context-length 1010000 \\\n" if window == "1m" else "")
            + f"    --speculative-algorithm DFLASH --speculative-draft-model-path {draft} \\\n"
            f"    --speculative-draft-model-revision {drev} \\\n")

    def run(self, lane, *rest, **env):
        r = subprocess.run(["bash", str(self.d / "prepare-lane.sh"), lane, *rest], capture_output=True, text=True, timeout=60,
                           env={"PATH": f"{self.t}/bin:/usr/bin:/bin", "HOME": str(self.t / "home"), **env})
        calls = (self.t / "calls").read_text().splitlines() if (self.t / "calls").exists() else []
        return r.returncode, r.stdout + r.stderr, calls


def heads(calls):
    return [" ".join(c.split()[0:2]) for c in calls]


class TheFlashPreparation(unittest.TestCase):
    """prepare-lane.sh flash: the image the launcher runs pulled (its room checked first) and
    tagged, the checkpoint of the target it serves fetched by the switch's own --fetch, the room
    of the PLE table it writes at every boot checked after it, and the reduced draft vocabulary
    built from it when the launcher takes one."""

    IMG = "lmsysorg/sglang@sha256:" + "c" * 64

    def box(self, repo_var="FLASH", map_line=True, image="", **kw):
        b = TextBox(**kw)
        model = b.PINS[f"{repo_var}_REPO"]
        (b.hf / "hub" / ("models--" + model.replace("/", "--")) / "snapshots" / b.PINS[f"{repo_var}_REV"]).mkdir(parents=True)
        b.flash(model, image or self.IMG, map_line)
        return b

    def test_image_checkpoint_and_vocabulary_in_that_order(self):
        rc, out, calls = self.box().run("flash")
        self.assertEqual(rc, 0, out)
        self.assertEqual(heads(calls), ["docker image", "docker info",                  # the room of each part, summed first
                                        "docker image", "docker info", "docker pull", "docker image", "docker image",
                                        "docker tag", "switch-model.sh flash", "docker run"])
        self.assertIn(f"docker pull {self.IMG}", calls)
        # tagged as install.sh tags what it pulls, so a docker image prune leaves it alone
        self.assertIn(f"docker tag {self.IMG} qwen38-pinned:flash-cccccccccccc", calls)
        self.assertIn("switch-model.sh flash --fetch", calls)
        self.assertIn(f"/snapshots/{TextBox.PINS['FLASH_REV']} --out /out/token-map-65536.pt --size 65536", calls[-1])

    def test_the_target_follows_the_model_the_launcher_serves(self):
        rc, out, calls = self.box(repo_var="FLASH_NVDA", image_here=True).run("flash")
        self.assertEqual(rc, 0, out)
        self.assertIn("switch-model.sh flash-nvda --fetch", calls)
        self.assertNotIn("docker pull", heads(calls))

    def test_the_target_the_load_loads_is_the_one_fetched(self):
        # the Load names its target: that checkpoint is fetched and the vocabulary built from it,
        # not the launcher's (found in review, 2026-10-15: a first Load of flash-nvda fetched the
        # default, 135 GB, then its switch fetched the NVIDIA export)
        b = self.box()                                                  # the launcher serves the default
        nv, nv_rev = b.PINS["FLASH_NVDA_REPO"], b.PINS["FLASH_NVDA_REV"]
        (b.hf / "hub" / ("models--" + nv.replace("/", "--")) / "snapshots" / nv_rev).mkdir(parents=True)
        rc, out, calls = b.run("flash", "flash-nvda")
        self.assertEqual(rc, 0, out)
        self.assertIn("switch-model.sh flash-nvda --fetch", calls)
        self.assertNotIn("switch-model.sh flash --fetch", calls)
        self.assertIn(f"models--{nv.replace('/', '--')}/snapshots/{nv_rev} --out /out/token-map-65536.pt", calls[-1])

    def test_the_vocabulary_build_stops_with_its_job(self):
        # --init: a job the cockpit stops takes the build's container with it (found in review,
        # 2026-10-15: it ran on)
        rc, out, calls = self.box().run("flash")
        self.assertEqual(rc, 0, out)
        self.assertTrue(calls[-1].startswith("docker run --rm --init "), calls[-1])

    def test_a_gpu_outside_its_ats_mode_is_refused_before_anything(self):
        # issue #54: on a GB10 outside its ATS mode the engine refused the table at load, after the image
        # and 126 GiB of checkpoint had come
        b = self.box()
        b.stub("nvidia-smi", 'echo None')
        rc, out, calls = b.run("flash")
        self.assertEqual(rc, 1, out)
        self.assertIn("the flash lane cannot start on this box: the GPU's addressing mode is None", out)
        self.assertEqual(calls, [])
        b.stub("nvidia-smi", 'echo ATS')
        rc, out, calls = b.run("flash")
        self.assertEqual(rc, 0, out)
        self.assertIn("docker pull", heads(calls))

    def test_a_target_of_another_lane_is_refused_before_anything(self):
        for target in ("stock", "image", "flash-turbo"):
            with self.subTest(target=target):
                rc, out, calls = self.box().run("flash", target)
                self.assertEqual(rc, 1, out)
                self.assertIn("is not a flash lane target", out)
                self.assertEqual(calls, [])

    def test_an_image_an_operator_named_by_tag_is_read_too(self):
        tag = "lmsysorg/sglang:dev-qwen38-next-local"
        rc, out, calls = self.box(image=tag).run("flash")
        self.assertEqual(rc, 0, out)
        self.assertIn(f"docker pull {tag}", calls)
        self.assertNotIn("docker tag", heads(calls))          # a tag needs none

    def test_a_launcher_with_no_image_where_it_should_be_is_refused_before_a_pull(self):
        # edited by hand, another word on that line: its pull failed as if the internet were down
        rc, out, calls = self.box(image="-e").run("flash")
        self.assertEqual(rc, 1, out)
        self.assertIn("names no image, model or cache where this expects it", out)
        self.assertEqual(calls, [])

    def test_an_image_named_with_no_tag_is_refused_before_a_pull(self):
        # the switch the Load runs next reads name:tag or name@sha256:... and serves its pin for
        # any other word: an image named alone was pulled here, then the switch died on a pin the
        # box did not have (found in review, 2026-10-15)
        rc, out, calls = self.box(image="qwen38-custom", image_here=True).run("flash")
        self.assertEqual(rc, 1, out)
        self.assertIn("names its image as qwen38-custom, with no tag or digest", out)
        self.assertEqual(calls, [])
        self.assertEqual(self.box(image="qwen38-custom:latest", image_here=True).run("flash")[0], 0)

    def test_a_checkpoint_already_whole_asks_its_caches_only(self):
        # as install.sh: 10 for a target whole in the cache, which a Download from Library fetched
        # before the first Load; it was asked 180 less what its blobs hold (54 on the reference
        # box), summed with the image and the PLE table (found in review, 2026-10-15)
        b = self.box(free_gb=110)
        snap = b.hf / "hub" / ("models--" + b.PINS["FLASH_REPO"].replace("/", "--")) / "snapshots" / b.PINS["FLASH_REV"]
        (snap / "model.safetensors").write_text("w")
        rc, out, calls = b.run("flash")
        self.assertEqual(rc, 0, out)                                 # 49 + 10 + 50 on its one disk

    def test_the_room_is_the_one_of_the_target_it_loads(self):
        # its launcher on the default, the Load on flash-nvda: what the cache holds of flash-nvda
        # comes off, not what it holds of the default (90 GiB of it there, none of the default)
        b = self.box(image_here=True, free_gb=95)
        (b.ple / "ple_table_0.bin").write_text("t")
        blobs = b.hf / "hub" / ("models--" + b.PINS["FLASH_NVDA_REPO"].replace("/", "--")) / "blobs"
        blobs.mkdir(parents=True)
        with open(blobs / "part.incomplete", "wb") as f:
            f.truncate(90 * 1024 ** 3)                               # sparse: its apparent size counts
        rc, out, calls = b.run("flash", "flash-nvda")
        self.assertEqual(rc, 0, out)                                 # 180 - 90 = 90 of the 95 free
        self.assertIn("switch-model.sh flash-nvda --fetch", calls)

    def test_each_part_is_counted_on_the_disk_it_lands_on(self):
        # the cache linked to another disk (the reference box's SSD), Docker's images and the PLE
        # table on the home disk: 49 and 50 summed there, the checkpoint's 180 on the other one
        ssd = pathlib.Path(tempfile.mkdtemp(dir="/dev/shm", prefix="prepare-ssd-"))
        self.addCleanup(__import__("shutil").rmtree, ssd, True)
        for home_free, rc in ((99, 0), (98, 1)):
            with self.subTest(home_free=home_free):
                b = self.box(free_gb=home_free, free_at=((ssd, 1000),), hf_on=ssd)
                got, out, calls = b.run("flash")
                self.assertEqual(got, rc, out)
                if rc:
                    self.assertIn(f"need ~99 GiB free on the disk of {b.t}/docker-root and {b.ple} for this first Load "
                                  "(49 for its image; 50 for its PLE table, 47.7 GiB written at every boot); found 98 GiB", out)
                    self.assertEqual(heads(calls), ["docker image", "docker info"])
                (ssd / "hf").rename(ssd / f"hf-{home_free}")

    def test_a_ple_folder_not_made_yet_is_on_the_disk_of_its_parent(self):
        # its launcher makes it at its first start: the nearest folder of it that exists says the disk
        b = self.box(free_gb=1000)
        b.flash(b.PINS["FLASH_REPO"], self.IMG, ple=b.t / "ple-later")
        rc, out, calls = b.run("flash")
        self.assertEqual(rc, 0, out)

    def test_a_launcher_with_no_ple_table_asks_no_room_for_one(self):
        # 49 for the image and 180 for the checkpoint, on a disk with just that free
        b = self.box(free_gb=229)
        b.flash(b.PINS["FLASH_REPO"], self.IMG, ple=None)
        rc, out, calls = b.run("flash")
        self.assertEqual(rc, 0, out)

    def test_a_vocabulary_that_does_not_build_stops_the_load_and_says_so(self):
        # it said the lane serves without it, as the Load stopped (found in review, 2026-10-15)
        b = self.box(image_here=True)
        b.stub("docker", f'echo "docker $*" >> "{b.t}/calls"\n'
               'case "$1 $2" in\n'
               f'  "info "*) echo "{b.t}/docker-root" ;;\n'
               '  "image inspect") case "$3" in qwen38-pinned:*) exit 1 ;; esac; echo sha256:abc ;;\n'
               '  "run "*) exit 1 ;;\n'
               'esac\nexit 0\n')
        rc, out, calls = b.run("flash")
        self.assertEqual(rc, 1, out)
        self.assertIn("this Load stops here, and a Load again tries again", out)
        self.assertNotIn("serves without it", out)

    def test_no_vocabulary_asked_none_built(self):
        rc, out, calls = self.box(map_line=False).run("flash")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("docker run", heads(calls))

    def test_no_room_for_what_it_downloads_is_refused_before_the_first_byte(self):
        # the image, the checkpoint and its caches, the PLE table: summed on their one disk
        rc, out, calls = self.box(free_gb=20).run("flash")
        self.assertEqual(rc, 1, out)
        self.assertIn("need ~279 GiB free on the disk of ", out)
        self.assertIn("for this first Load (49 for its image; 180 for its checkpoint and its caches; "
                      "50 for its PLE table, 47.7 GiB written at every boot)", out)
        self.assertEqual(heads(calls), ["docker image", "docker info"])

    def test_room_for_the_checkpoint_and_not_its_ple_table_is_refused_before_either(self):
        # each part checked its own room as it came: 150 GB came, then the table had none (found
        # in review, 2026-10-15)
        rc, out, calls = self.box(image_here=True, free_gb=200).run("flash")
        self.assertEqual(rc, 1, out)
        self.assertIn("need ~230 GiB free on the disk of ", out)
        self.assertIn("50 for its PLE table", out)
        self.assertNotIn("for its image", out)                         # what is already here is not listed
        self.assertNotIn("switch-model.sh flash", heads(calls))      # nothing fetched
        b = self.box(image_here=True, free_gb=200)
        (b.ple / "ple_table_0.bin").write_text("t")                   # a table already there takes its own room
        self.assertEqual(b.run("flash")[0], 0)


class TheTwentySevenBPreparation(unittest.TestCase):
    """prepare-lane.sh 27b, the 27B beside a serving flash: the image its unit runs pulled and
    tagged, its DFlash2 draft fetched in that image, its config patched for the 1M window the unit
    serves (restored for the native one), and the checkpoint of the target it serves fetched by
    the switch's own --fetch, which patches the target's config at the Load."""

    IMG = "lmsysorg/sglang@sha256:" + "e" * 64
    DRAFT, DREV = "org/Qwen3.8-27B-DFlash2", "d" * 40

    def box(self, repo_var="UNC", window="1m", **kw):
        b = TextBox(**kw)
        b.unit27(b.PINS[f"{repo_var}_REPO"], self.IMG, window, self.DRAFT, self.DREV)
        return b

    def test_image_draft_its_config_and_checkpoint_in_that_order(self):
        b = self.box()
        rc, out, calls = b.run("27b")
        self.assertEqual(rc, 0, out)
        self.assertEqual(heads(calls), ["docker image", "docker info",                  # the room of each part, summed first
                                        "docker image", "docker info", "docker pull", "docker image", "docker image",
                                        "docker tag", "docker run", "patch-yarn.py " + str(b.hf), "switch-model.sh uncensored"])
        run = next(c for c in calls if c.startswith("docker run"))
        # the cache first (--offline-first): a draft already whole asks the Hub nothing
        self.assertIn(f"-v {b.hf}:/hf {self.IMG} /opt/qwen38/ckpt-fetch.py {self.DRAFT} {self.DREV} --offline-first --refs-main fill", run)
        self.assertIn(f"patch-yarn.py {b.hf} {self.DRAFT} {self.DREV}", calls)
        self.assertIn("switch-model.sh uncensored --fetch", calls)
        self.assertIn(f"docker tag {self.IMG} qwen38-pinned:27b-eeeeeeeeeeee", calls)

    def test_a_native_unit_puts_the_drafts_config_back(self):
        b = self.box(window="native", image_here=True)
        rc, out, calls = b.run("27b")
        self.assertEqual(rc, 0, out)
        self.assertIn(f"patch-yarn.py --restore {b.hf} {self.DRAFT} {self.DREV}", calls)

    def test_the_target_follows_the_model_the_unit_serves(self):
        for repo_var, target in (("STOCK", "stock"), ("FP8", "fp8"), ("UNCFP8", "uncensored-fp8")):
            with self.subTest(target=target):
                rc, out, calls = self.box(repo_var=repo_var, image_here=True).run("27b")
                self.assertEqual(rc, 0, out)
                self.assertEqual(calls[-1], f"switch-model.sh {target} --fetch")

    def test_the_target_the_load_loads_is_the_one_fetched(self):
        # an uncensored unit, a Load of fp8: fp8 is fetched, not the unit's (2026-10-15)
        rc, out, calls = self.box(repo_var="UNC", image_here=True).run("27b", "fp8")
        self.assertEqual(rc, 0, out)
        self.assertEqual(calls[-1], "switch-model.sh fp8 --fetch")
        rc, out, calls = self.box().run("27b", "flash")
        self.assertEqual(rc, 1, out)
        self.assertIn("is not a 27B lane target", out)
        self.assertEqual(calls, [])

    def test_the_drafts_fetch_stops_with_its_job(self):
        # --init: a job the cockpit stops takes its container with it (found in review,
        # 2026-10-15: it ran on)
        rc, out, calls = self.box(image_here=True).run("27b")
        self.assertEqual(rc, 0, out)
        run = next(c for c in calls if c.startswith("docker run"))
        self.assertTrue(run.startswith("docker run --rm --init "), run)

    def test_the_room_is_the_one_of_the_target_it_loads(self):
        # the unit on uncensored, the Load on fp8: what the cache holds of fp8 comes off (30 GiB)
        b = self.box(image_here=True, free_gb=20)
        blobs = b.hf / "hub" / ("models--" + b.PINS["FP8_REPO"].replace("/", "--")) / "blobs"
        blobs.mkdir(parents=True)
        with open(blobs / "part.incomplete", "wb") as f:
            f.truncate(30 * 1024 ** 3)
        rc, out, calls = b.run("27b", "fp8")
        self.assertEqual(rc, 0, out)                                 # 45 - 30 = 15 of the 20 free
        self.assertIn("switch-model.sh fp8 --fetch", calls)

    def test_a_target_whole_at_its_pin_asks_its_caches_only(self):
        # the revision the Load loads, as the switch fetches it: 10 for a target whole at it
        b = self.box(image_here=True, free_gb=10)
        snap = b.hf / "hub" / ("models--" + b.PINS["FP8_REPO"].replace("/", "--")) / "snapshots" / b.PINS["FP8_REV"]
        snap.mkdir(parents=True)
        (snap / "model.safetensors").write_text("w")
        rc, out, calls = b.run("27b", "fp8")
        self.assertEqual(rc, 0, out)

    def test_an_image_named_with_no_tag_is_refused_before_a_pull(self):
        b = TextBox(image_here=True)
        b.unit27(b.PINS["UNC_REPO"], "qwen38-custom", "1m", self.DRAFT, self.DREV)
        rc, out, calls = b.run("27b")
        self.assertEqual(rc, 1, out)
        self.assertIn("names its image as qwen38-custom, with no tag or digest", out)
        self.assertEqual(calls, [])

    def test_no_room_for_what_it_downloads_is_refused_before_the_first_byte(self):
        # its image, its checkpoint with its draft and their caches, summed on their one disk
        rc, out, calls = self.box(free_gb=60).run("27b")
        self.assertEqual(rc, 1, out)
        # the draft in the checkpoint's 45, as install.sh counts them: 5 more for it asked 98 where
        # install.sh asks 93 (found in review, 2026-10-15)
        self.assertIn("need ~93 GiB free on the disk of ", out)
        self.assertIn("for this first Load (48 for its image; 45 for its checkpoint, its DFlash2 draft and their caches)", out)
        self.assertEqual(heads(calls), ["docker image", "docker info"])

    def test_a_unit_with_no_image_where_it_should_be_is_refused_before_a_pull(self):
        # a flag where the image should be, as for the flash lane (its pull failed as if the
        # internet were down)
        b = TextBox()
        b.unit27(b.PINS["UNC_REPO"], "-e", "1m", self.DRAFT, self.DREV)
        rc, out, calls = b.run("27b")
        self.assertEqual(rc, 1, out)
        self.assertIn("names no image, model or cache where this expects it", out)
        self.assertEqual(calls, [])

    def test_a_token_goes_by_its_name_only(self):
        rc, out, calls = self.box(image_here=True).run("27b", HF_TOKEN="hf_value_never_shown")
        self.assertEqual(rc, 0, out)
        run = next(c for c in calls if c.startswith("docker run"))
        self.assertIn(" -e HF_TOKEN ", run)
        self.assertNotIn("hf_value_never_shown", "\n".join(calls) + out)

    def test_what_it_cannot_prepare_is_refused(self):
        b = TextBox()
        b.unit27("org/a-custom-27b", self.IMG)
        rc, out, _ = b.run("27b")
        self.assertEqual(rc, 1, out)
        self.assertIn("which no 27B target names", out)
        rc, out, _ = TextBox().run("27b")                                   # no unit at all
        self.assertEqual(rc, 1, out)
        self.assertIn("the 27B lane is not installed", out)


if __name__ == "__main__":
    unittest.main()
