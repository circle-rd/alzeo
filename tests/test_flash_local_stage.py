#!/usr/bin/env python3
"""install-flash-local.sh --stage: the unit a switch to the Qwen3.8 Flash-Next FP4-Local lane puts in place.

switch-model.sh flash-local fetches the checkpoint, then has the lane's installer write its unit
to a fixed staging path with no sudo (the one renderer, so a switch puts in the unit an install
would), after its chat template and the copies of its n-gram table. These run the installer as
written, in a throwaway home, on a fake cache whose table sits in two of its three files."""
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests"))
from test_lean_level import STOCK  # noqa: E402  (the template patch-template.py knows how to patch)

PINS = {k: v.strip('"') for k, v in (line.split("=", 1) for line in (REPO / "install-flash-local.sh").read_text().splitlines()
                                       if line.startswith("FLASH_LOCAL_") and "=" in line)}
HUB = "models--" + PINS["FLASH_LOCAL_REPO"].replace("/", "--")


class Box:
    def __init__(self):
        self.t = pathlib.Path(tempfile.mkdtemp(prefix="flash-local-stage-"))
        self.home = self.t / "home"
        (self.home / ".config" / "qwen38").mkdir(parents=True)
        (self.home / ".config" / "qwen38" / "api-key").write_text("test-key\n")
        self.units = self.t / "units"
        self.units.mkdir()
        self.hf = self.t / "hf"
        blobs = self.hf / "hub" / HUB / "blobs"
        snap = self.hf / "hub" / HUB / "snapshots" / PINS["FLASH_LOCAL_REV"]
        (snap / "tensors").mkdir(parents=True)
        blobs.mkdir(parents=True)
        (snap / "chat_template.jinja").write_text(STOCK)
        weight_map, self.table = {}, []
        for i, (tensors, body) in enumerate([
                (["model.layers.0.mlp.experts.0.weight"], b"experts" * 64),
                (["model.language_model.layers.0.ple.ple_embedding.ngram_embedding.shard_0.weight"], b"table-0" * 64),
                (["model.language_model.layers.0.ple.ple_embedding.ngram_embedding.shard_1.weight"], b"table-1" * 64)]):
            name = f"tensors/model-0000{i + 1}-of-00003.safetensors"
            sha = hashlib.sha256(body).hexdigest()
            (blobs / sha).write_bytes(body)
            os.symlink(os.path.relpath(blobs / sha, (snap / name).parent), snap / name)
            weight_map.update({t: name for t in tensors})
            if i:
                self.table.append(sha)
        (snap / "model.safetensors.index.json").write_text(json.dumps({"weight_map": weight_map}))
        self.out = self.t / "stage.service"

    def stage(self, cwd=None, **env):
        e = {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "HF_CACHE": str(self.hf),
             "LANES_UNIT_DIR": str(self.units), **env}
        r = subprocess.run(["bash", str(REPO / "install-flash-local.sh"), "--stage", str(self.out)],
                           capture_output=True, text=True, env=e, timeout=120, cwd=cwd)
        return r.returncode, r.stdout + r.stderr

    def mounts(self):
        return re.findall(r"-v (\S+):/root/\.cache/huggingface/hub/\S+:ro", self.out.read_text())


def exec_command(unit: str) -> str:
    """The ExecStart command as bash runs it: systemd joins the lines a backslash ends, and hands
    bash a $ for each $$."""
    line = unit[unit.index("ExecStart=/bin/bash -c '") + len("ExecStart=/bin/bash -c '"):]
    line = line[:line.index("'\nExecStop=")]
    return line.replace("\\\n", " ").replace("$$", "$")


class TheStagedUnit(unittest.TestCase):
    def setUp(self):
        self.box = Box()

    def tearDown(self):
        shutil.rmtree(self.box.t, ignore_errors=True)

    def test_read_in_place_it_names_no_copy_and_is_whole(self):
        rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR="none")
        self.assertEqual(rc, 0, out)
        unit = self.box.out.read_text()
        self.assertNotRegex(unit, r"__[A-Z][A-Z0-9_]*__", "a placeholder left")
        self.assertNotIn("/blobs/", unit)
        self.assertIn(f"vllm serve {PINS['FLASH_LOCAL_REPO']} --revision {PINS['FLASH_LOCAL_REV']}", unit)
        self.assertIn(f"  {PINS['FLASH_LOCAL_IMAGE']} \\\n", unit)
        self.assertIn(f"--security-opt seccomp={self.box.home}/.config/qwen38/seccomp-io-uring.json", unit)
        self.assertNotIn("unconfined", unit)
        self.assertIn(f"-v {self.box.hf}:/root/.cache/huggingface", unit)
        self.assertIn("--served-model-name qwen3.8-flash-next-fp4", unit)
        self.assertIn("--host 127.0.0.1 --port 30000", unit)
        r = subprocess.run(["bash", "-n", "-c", exec_command(unit)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        # what the unit reads at its start is there: the filter, the template with lean as its default (v1.46.4)
        cfg = self.box.home / ".config" / "qwen38"
        self.assertEqual((cfg / "seccomp-io-uring.json").read_bytes(), (REPO / "seccomp-io-uring.json").read_bytes())
        self.assertIn("reasoning_effort|default('lean')", (cfg / "chat-template-flash-local.jinja").read_text())

    def test_its_boot_leaves_the_box_its_memory(self):
        # vLLM loads the weights with PyTorch's allocator splitting no block above 20 MiB: the blocks b12x
        # freed while it laid out the experts' NVFP4 scales went unused, and the allocator's cache grew
        # until the GPU's driver refused it memory, which on this box is the machine's (the driver refused
        # 44 to 149 allocations at each of twelve boots, MemAvailable down to 1.05 GiB, 2026-10-20).
        # PyTorch collects its cached blocks only under a ceiling below the whole GPU, past the share of
        # it the threshold says: the ceiling above the share vLLM is given, the collection under it and
        # above what the engine holds in service (0.74 of the GPU at the card's share of 0.8)
        rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR="none")
        self.assertEqual(rc, 0, out)
        cmd = exec_command(self.box.out.read_text())
        m = re.search(r"-e PYTORCH_CUDA_ALLOC_CONF=(\S+)", cmd)
        self.assertIsNotNone(m, "the engine's allocator is left as PyTorch sets it")
        conf = dict(kv.split(":", 1) for kv in m.group(1).split(","))
        ceiling, threshold = float(conf["per_process_memory_fraction"]), float(conf["garbage_collection_threshold"])
        share = float(re.search(r"--gpu-memory-utilization (\S+)", cmd).group(1))
        self.assertTrue(share < ceiling < 1.0, (share, ceiling))
        self.assertTrue(share - 0.05 <= ceiling * threshold < share, (ceiling, threshold, share))

    def guard(self, unit: str, tries: int = 0) -> list:
        """The check at the head of the unit's ExecStart, as bash runs it, with its tries of 3 s set."""
        cmd = exec_command(unit)
        self.assertIn("exec /usr/bin/docker run", cmd)
        head = cmd[:cmd.index("exec /usr/bin/docker run")].strip()
        script = f"{self.box.home}/.config/qwen38/flash-local-files.sh"
        # the check named once, there before it runs (else 78 and its line), then run
        self.assertTrue(head.startswith(f"check={script}; ") and 'bash "$check" ' in head, head)
        self.assertTrue(head.endswith("|| exit $?;"), head)
        self.assertTrue(os.access(script, os.X_OK), "the stage put no check where the unit runs it")
        return ["env", f"FLASH_LOCAL_FILES_TRIES={tries}", "bash", "-c", head + " exit 0"]

    def test_a_start_without_its_checkpoint_or_a_copy_stays_failed(self):
        # A start missing what the unit mounts made runc create it, owned by root: the checkpoint's
        # folders inside the Hugging Face cache, which install.sh then read as a lane begun, and a folder
        # in place of a missing copy of the table (found in review, proved on the reference box,
        # 2026-10-20). The unit's main process checks them before the engine runs and exits 78, which
        # RestartPreventExitStatus holds for the main process alone: from an ExecStartPre the same 78
        # was started again every 15 s (found in review, 2026-10-20, systemd.service(5) of systemd 255).
        folder = self.box.t / "copies"
        rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR=str(folder))
        self.assertEqual(rc, 0, out)
        unit = self.box.out.read_text()
        lines = unit.splitlines()
        self.assertEqual([l for l in lines if l.startswith("ExecStartPre=") and "exit 78" in l], [],
                         "the check is a start step, whose 78 systemd starts again")
        self.assertIn("RestartPreventExitStatus=78", lines)
        guard = self.guard(unit)

        def start():
            return subprocess.run(guard, capture_output=True, text=True, timeout=30)
        self.assertEqual(start().returncode, 0)
        copy = next(p for p in sorted(folder.iterdir()) if not p.name.startswith("."))
        copy.rename(copy.with_name("gone"))
        r = start()
        self.assertEqual(r.returncode, 78)
        self.assertIn(str(copy), r.stderr)
        self.assertIn("./switch-model.sh flash-local", r.stderr, "the remedy fetches nothing")
        self.assertIn("or the disk that holds the Hugging Face cache is not mounted", r.stderr)
        self.assertNotIn("prepare-lane.sh", r.stderr, "prepare-lane.sh pulls the image and nothing else")
        # a folder left in the copy's place is no copy: [ -s ] alone took it for one
        copy.mkdir()
        self.assertEqual(start().returncode, 78)
        copy.rmdir()
        copy.with_name("gone").rename(copy)
        index = self.box.hf / "hub" / HUB / "snapshots" / PINS["FLASH_LOCAL_REV"] / "model.safetensors.index.json"
        index.rename(index.with_name("gone.json"))
        self.assertEqual(start().returncode, 78)
        index.with_name("gone.json").rename(index)
        self.assertEqual(start().returncode, 0)

    def test_a_checkpoint_cut_short_or_a_copy_cut_short_stays_failed(self):
        # a fetch cut short leaves the small index before the large files, and a copy cut short
        # (a power cut, a full disk) has the copy's name: the check took both for whole, and the
        # engine then died at every start, started again every 15 s (found in review, 2026-10-20)
        folder = self.box.t / "copies"
        rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR=str(folder))
        self.assertEqual(rc, 0, out)
        guard = self.guard(self.box.out.read_text())

        def start():
            return subprocess.run(guard, capture_output=True, text=True, timeout=30)
        self.assertEqual(start().returncode, 0)
        snap = self.box.hf / "hub" / HUB / "snapshots" / PINS["FLASH_LOCAL_REV"]
        shard = snap / "tensors" / "model-00001-of-00003.safetensors"
        target = shard.resolve()
        target.rename(target.with_name("gone"))
        r = start()
        self.assertEqual(r.returncode, 78, r.stderr)
        self.assertIn(f"{shard} is missing or not whole", r.stderr)
        target.with_name("gone").rename(target)
        copy = next(p for p in sorted(folder.iterdir()) if not p.name.startswith("."))
        whole = copy.read_bytes()
        copy.write_bytes(whole[:-1])
        r = start()
        self.assertEqual(r.returncode, 78, r.stderr)
        self.assertIn(f"{copy} is missing or not whole", r.stderr)
        copy.write_bytes(whole)
        self.assertEqual(start().returncode, 0)

    def test_the_check_waits_for_a_disk_mounted_late(self):
        # a cache on another disk may be mounted after the unit starts at boot (nofail,
        # docs/external-disk.md): a 78 is final now, so the check gives the files time to come
        folder = self.box.t / "copies"
        rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR=str(folder))
        self.assertEqual(rc, 0, out)
        index = self.box.hf / "hub" / HUB / "snapshots" / PINS["FLASH_LOCAL_REV"] / "model.safetensors.index.json"
        index.rename(index.with_name("gone.json"))
        p = subprocess.Popen(self.guard(self.box.out.read_text(), tries=7),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        import time
        time.sleep(4)
        self.assertIsNone(p.poll(), "the check gave up before its time")
        index.with_name("gone.json").rename(index)
        self.assertEqual(p.wait(timeout=30), 0)

    def test_the_cockpit_reads_why_and_says_it(self):
        # its line is the cockpit's to read (lifecycle.parse_preflight): a line of another form
        # left the page at "keeps crashing", and "cannot start" would blame the GPU's ATS mode
        sys.path.insert(0, str(REPO / "dashboard"))
        import lifecycle
        folder = self.box.t / "copies"
        rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR=str(folder))
        self.assertEqual(rc, 0, out)
        copy = next(p for p in sorted(folder.iterdir()) if not p.name.startswith("."))
        copy.unlink()
        r = subprocess.run(self.guard(self.box.out.read_text()), capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 78)
        said = lifecycle.parse_preflight(r.stderr.splitlines())
        self.assertEqual((said or {}).get("verdict"), "lacks its files", r.stderr)
        flags = lifecycle.preflight_flags({"state": "failed"}, unit_sub="failed", preflight=said)
        self.assertIn(str(copy), flags.get("lacks", ""))
        self.assertNotIn("cannot", flags)

    def test_its_check_gone_the_start_stops_with_its_line(self):
        # a configuration restored from before the check: bash exited 127, which systemd started
        # again every 15 s, on a line the cockpit does not read (found in review, 2026-10-20)
        sys.path.insert(0, str(REPO / "dashboard"))
        import lifecycle
        rc, out = self.box.stage()
        self.assertEqual(rc, 0, out)
        guard = self.guard(self.box.out.read_text())
        script = pathlib.Path(f"{self.box.home}/.config/qwen38/flash-local-files.sh")
        script.unlink()
        r = subprocess.run(guard, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 78, r.stderr)
        said = lifecycle.parse_preflight(r.stderr.splitlines())
        self.assertEqual((said or {}).get("verdict"), "lacks its files", r.stderr)
        self.assertIn(f"{script} is missing (the check of its files)", said["reason"])
        # and the stage puts it back, as the lane's Load runs it for a lane that cannot start
        rc, out = self.box.stage()
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.access(script, os.X_OK))

    def test_read_in_place_the_start_checks_the_checkpoint_alone(self):
        rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR="none")
        self.assertEqual(rc, 0, out)
        guard = " ".join(self.guard(self.box.out.read_text()))
        self.assertNotIn("/blobs/", guard)
        self.assertNotIn("__", guard)

    def test_copies_of_the_table_are_mounted_over_the_caches_files(self):
        folder = self.box.t / "copies"
        rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR=str(folder))
        self.assertEqual(rc, 0, out)
        self.assertEqual(sorted(p.name for p in folder.iterdir() if not p.name.startswith(".")), sorted(self.box.table))
        unit = self.box.out.read_text()
        mounts = re.findall(r"-v (\S+):(/root/\.cache/huggingface/hub/\S+):ro", unit)
        self.assertEqual(mounts, [(str(folder / s), f"/root/.cache/huggingface/hub/{HUB}/blobs/{s}") for s in self.box.table])
        r = subprocess.run(["bash", "-n", "-c", exec_command(unit)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_the_folder_chosen_is_kept_for_a_load_run_with_none_of_the_users_environment(self):
        # a Load from the cockpit stages the unit with none of the user's environment: read without
        # the choice made at the install, it named no copy and the table was read where the cache is,
        # or copied it again to ~/flash-local-ple (found in review, 2026-10-20)
        folder = self.box.t / "copies"
        rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR=str(folder))
        self.assertEqual(rc, 0, out)
        rc, out = self.box.stage()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.box.mounts(), [str(folder / s) for s in self.box.table])
        self.assertFalse((self.box.home / "flash-local-ple").exists())
        # given empty, the default comes back: the cache here is on the home directory's disk
        rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR="")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.box.mounts(), [])
        rc, out = self.box.stage()
        self.assertEqual((rc, self.box.mounts()), (0, []), out)

    def test_it_writes_its_own_files_and_no_other_lanes(self):
        # the start scripts every lane runs are ./install.sh's: a Load put them in from the checkout,
        # moved past its install or not (found in review, 2026-10-20). The filter is the lane's own,
        # and an older one stays until ./install.sh as the scripts do.
        cfg = self.box.home / ".config" / "qwen38"
        for name in ("engine-preflight.sh", "engine-secrets.sh", "vllm-auth/vllm_engine_auth.py", "seccomp-io-uring.json"):
            (cfg / name).parent.mkdir(parents=True, exist_ok=True)
            (cfg / name).write_text("installed earlier\n")
        rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR="none")
        self.assertEqual(rc, 0, out)
        for name in ("engine-preflight.sh", "engine-secrets.sh", "vllm-auth/vllm_engine_auth.py", "seccomp-io-uring.json"):
            self.assertEqual((cfg / name).read_text(), "installed earlier\n", name)
        for cache in ("vllm", "flashinfer", "b12x", "triton"):
            self.assertTrue((cfg / "flash-local-cache" / cache).is_dir(), cache)

    def test_its_room_is_checked_before_the_switch_and_the_install_fetch(self):
        r = subprocess.run(["bash", str(REPO / "install-flash-local.sh"), "--room"], capture_output=True, text=True,
                           timeout=120, env={"PATH": "/usr/bin:/bin", "HOME": str(self.box.home), "HF_CACHE": str(self.box.hf),
                                             "LANES_UNIT_DIR": str(self.box.units), "FLASH_LOCAL_PLE_DIR": str(self.box.t / "c")})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("room for the table's copies under", r.stdout)
        installer = (REPO / "install-flash-local.sh").read_text()
        self.assertLess(installer.rindex("\nlane_table_room\n"), installer.index('vlane_fetch "$MODEL" "$MODEL_REV"'))
        switch = (REPO / "switch-model.sh").read_text()
        block = switch[switch.index('if [ "$CHOICE" = "flash-local" ]; then'):]
        self.assertLess(block.index("install-flash-local.sh\" --room"), block.index("/opt/qwen38/ckpt-fetch.py"))

    def test_a_folder_refused_is_not_kept(self):
        # kept before it was checked, a folder ple-table.py refuses stayed the choice of every Load
        # after, each failing the same way (found in review, 2026-10-20)
        for bad in (str(self.box.hf / "ple"), str(self.box.t / "a:b")):
            with self.subTest(folder=bad):
                rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR=bad)
                self.assertNotEqual(rc, 0, out)
                self.assertFalse((self.box.home / ".config" / "qwen38" / "flash-local-ple-dir").exists())

    def test_a_relative_folder_is_mounted_by_its_whole_path(self):
        # docker run -v takes a host path only whole: "copies/<sha>" is read as a volume's name,
        # refused, and the unit restarted in a loop
        rc, out = self.box.stage(cwd=self.box.t, FLASH_LOCAL_PLE_DIR="copies")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.box.mounts(), [str(self.box.t / "copies" / s) for s in self.box.table])

    def test_a_cache_on_the_home_directorys_disk_is_read_in_place(self):
        # a new box: the cache under the home directory, on its disk
        rc, out = self.box.stage()
        self.assertEqual(rc, 0, out)
        self.assertNotIn("/blobs/", self.box.out.read_text())
        self.assertFalse((self.box.home / "flash-local-ple").exists())

    def test_without_the_checkpoint_it_refuses(self):
        shutil.rmtree(self.box.hf / "hub")
        rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR="none")
        self.assertNotEqual(rc, 0, out)
        self.assertFalse(self.box.out.exists() and self.box.out.read_text().strip(), "a unit staged with no checkpoint")



class TheMillionTokenWindow(unittest.TestCase):
    """Discussion #63 (2026-11-09): FLASH_LOCAL_CONTEXT=1m serves 1,048,576 tokens by YaRN, as measured on the reference
    box (8 passphrases of 8 retrieved up to 1,036,576 tokens). The overrides come from the pinned checkpoint's own RoPE,
    and the choice is kept for the Loads after, which stage the unit with none of the user's environment."""

    CONFIG = {"text_config": {"max_position_embeddings": 262144, "rope_parameters": {
        "mrope_interleaved": True, "mrope_section": [11, 11, 10], "partial_rotary_factor": 0.25,
        "rope_theta": 10000000, "rope_type": "default"}}}

    def setUp(self):
        self.box = Box()
        self.snap = self.box.hf / "hub" / HUB / "snapshots" / PINS["FLASH_LOCAL_REV"]
        (self.snap / "config.json").write_text(json.dumps(self.CONFIG))
        self.cfg = self.box.home / ".config" / "qwen38"

    def tearDown(self):
        shutil.rmtree(self.box.t, ignore_errors=True)

    def window(self):
        return re.search(r"--max-model-len (\d+) ", self.box.out.read_text()).group(1)

    def test_native_by_default_1m_when_asked_and_kept_for_the_loads_after(self):
        rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR="none")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.window(), "262144")
        self.assertNotIn("--hf-overrides", self.box.out.read_text())
        rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR="none", FLASH_LOCAL_CONTEXT="1m")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.window(), "1048576")
        cmd = exec_command(self.box.out.read_text())
        self.assertIn(f'--hf-overrides "$(cat {self.cfg}/flash-local-yarn.json)"', cmd)
        r = subprocess.run(["bash", "-n", "-c", cmd], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        y = json.loads((self.cfg / "flash-local-yarn.json").read_text())["text_config"]
        self.assertEqual(y["max_position_embeddings"], 1048576)
        self.assertEqual(y["rope_parameters"], {"mrope_interleaved": True, "mrope_section": [11, 11, 10],
                                                "partial_rotary_factor": 0.25, "rope_theta": 10000000,
                                                "rope_type": "yarn", "factor": 4.0,
                                                "original_max_position_embeddings": 262144})
        rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR="none")          # a Load: none of the user's environment
        self.assertEqual((rc, self.window()), (0, "1048576"), out)
        rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR="none", FLASH_LOCAL_CONTEXT="native")
        self.assertEqual((rc, self.window()), (0, "262144"), out)
        self.assertFalse((self.cfg / "flash-local-context").exists())

    def test_a_checkpoint_whose_rope_moved_or_an_unknown_value_is_refused(self):
        moved = json.loads(json.dumps(self.CONFIG))
        moved["text_config"]["rope_parameters"]["mrope_section"] = [16, 24, 24]
        (self.snap / "config.json").write_text(json.dumps(moved))
        rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR="none", FLASH_LOCAL_CONTEXT="1m")
        self.assertNotEqual(rc, 0, out)
        self.assertIn("RoPE is not the one the 1M window was measured on", out)
        rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR="none", FLASH_LOCAL_CONTEXT="2m")
        self.assertNotEqual(rc, 0, out)
        self.assertIn("native (262,144 tokens, the default) or 1m", out)

    def test_a_level_given_with_a_window_refused_is_kept_with_its_template(self):
        # the template is written before the window is checked: refused there, the run left a template at xhigh with no
        # choice kept, and the Load after wrote it back at lean (found in review of v1.46.4)
        moved = json.loads(json.dumps(self.CONFIG))
        moved["text_config"]["rope_parameters"]["mrope_section"] = [16, 24, 24]
        (self.snap / "config.json").write_text(json.dumps(moved))
        cfg = self.box.home / ".config" / "qwen38"
        rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR="none", FLASH_LOCAL_CONTEXT="1m", FLASH_LOCAL_EFFORT="xhigh")
        self.assertNotEqual(rc, 0, out)
        self.assertIn("reasoning_effort|default('xhigh')", (cfg / "chat-template-flash-local.jinja").read_text())
        self.assertEqual((cfg / "flash-local-effort").read_text(), "xhigh\n")
        rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR="none")          # the Load after: the level kept, not the window
        self.assertEqual(rc, 0, out)
        self.assertIn("reasoning_effort|default('xhigh')", (cfg / "chat-template-flash-local.jinja").read_text())

    def test_a_refused_choice_is_not_kept_and_the_refusal_says_how_to_go_back(self):
        # the choice was kept before its overrides were checked: one refused try left every Load after it refused,
        # and nothing said how to go back (code review of v1.45.2)
        moved = json.loads(json.dumps(self.CONFIG))
        moved["text_config"]["rope_parameters"]["mrope_section"] = [16, 24, 24]
        (self.snap / "config.json").write_text(json.dumps(moved))
        rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR="none", FLASH_LOCAL_CONTEXT="1m")
        self.assertNotEqual(rc, 0, out)
        self.assertIn("FLASH_LOCAL_CONTEXT=native ./install-flash-local.sh --units-only", out)
        self.assertFalse((self.cfg / "flash-local-context").exists())
        rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR="none")          # the Load after it
        self.assertEqual((rc, self.window()), (0, "262144"), out)

    def test_a_unit_put_in_before_its_checkpoint_keeps_the_native_window_until_its_load(self):
        # --units-only (an update, a lane installed on demand) renders the unit before the checkpoint is in the cache:
        # the unit took the 1M window and named overrides no run had written, and its start failed (code review of
        # v1.45.2). The installer's own lines from the choice to the render run here, as --units-only runs them.
        (self.snap / "config.json").unlink()
        text = (REPO / "install-flash-local.sh").read_text()
        part = text[text.index('CONTEXT_CHOICE="$CONFIG_DIR/flash-local-context"'):
                    text.index("# Step 4, the one part that needs root")]
        unit = self.box.t / "units-only.service"
        script = "\n".join(["set -euo pipefail", 'die(){ echo "die: $*"; exit 1; }', f'HERE="{REPO}"',
                            "UNIT=qwen38-flash-local.service", f'CONFIG_DIR="{self.cfg}"', f'HF_CACHE="{self.box.hf}"',
                            f'MODEL="{PINS["FLASH_LOCAL_REPO"]}"', f'MODEL_REV="{PINS["FLASH_LOCAL_REV"]}"', "MODE=units",
                            "LOCAL_IMAGE=img GMU=0.8 SEQS=8 ENGINE_BIND=127.0.0.1 PORT=30000",
                            part, f'render_unit "{unit}"'])
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                           env={"PATH": "/usr/bin:/bin", "HOME": str(self.box.home), "FLASH_LOCAL_CONTEXT": "1m"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("--max-model-len 262144 ", unit.read_text())
        self.assertNotIn("--hf-overrides", unit.read_text())
        self.assertIn("the checkpoint is not in the cache yet", r.stdout + r.stderr)
        self.assertEqual((self.cfg / "flash-local-context").read_text(), "1m\n")
        # its Load, the checkpoint fetched, sets the 1M window
        (self.snap / "config.json").write_text(json.dumps(self.CONFIG))
        rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR="none")
        self.assertEqual((rc, self.window()), (0, "1048576"), out)

    def test_the_overrides_keep_their_date_when_they_do_not_change(self):
        # a lane that serves is told it runs older files when one is newer than its start (vlane_put_unit): written
        # again at every run, the overrides would have said so after every update
        rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR="none", FLASH_LOCAL_CONTEXT="1m")
        self.assertEqual(rc, 0, out)
        y = self.cfg / "flash-local-yarn.json"
        os.utime(y, (1_000_000_000, 1_000_000_000))
        rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR="none")
        self.assertEqual(rc, 0, out)
        self.assertEqual(y.stat().st_mtime, 1_000_000_000)
        y.write_text("{}")                                            # and changed, they are written again
        os.utime(y, (1_000_000_000, 1_000_000_000))
        rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR="none")
        self.assertEqual(rc, 0, out)
        self.assertNotEqual(y.stat().st_mtime, 1_000_000_000)
        self.assertIn('"factor":4.0', y.read_text())

    def test_a_lane_that_serves_is_told_when_its_overrides_changed(self):
        # vlane_put_unit is handed what the engine reads at its start: the overrides too, when its unit reads them
        text = (REPO / "install-flash-local.sh").read_text()
        body = text[text.index("install_service(){"):]
        body = body[:body.index("\n}\n")]
        self.assertIn("! grep -q -- '--hf-overrides' \"$RENDER\" || starts+=(\"$YARN_JSON\")", body)
        self.assertIn('vlane_put_unit "$RENDER" "${starts[@]}"', body)

    def test_a_choice_kept_from_before_that_the_checkpoint_refuses_serves_native_and_the_load_works(self):
        # kept from before, the choice met a checkpoint whose RoPE is not the one measured (a pin moved): every Load died
        # until the user found the command to go back (second review of v1.45.2). The Load works, at the native window,
        # and says why; the choice stays the user's.
        rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR="none", FLASH_LOCAL_CONTEXT="1m")
        self.assertEqual((rc, self.window()), (0, "1048576"), out)
        moved = json.loads(json.dumps(self.CONFIG))
        moved["text_config"]["rope_parameters"]["rope_theta"] = 5000000
        (self.snap / "config.json").write_text(json.dumps(moved))
        rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR="none")          # a Load: none of the user's environment
        self.assertEqual((rc, self.window()), (0, "262144"), out)
        self.assertNotIn("--hf-overrides", self.box.out.read_text())
        self.assertIn("is off for this checkpoint", out)
        self.assertEqual((self.cfg / "flash-local-context").read_text(), "1m\n")

    def run_lines(self, mode, tail, **env):
        """The installer's own lines from the choice to the render, then `tail`, as `mode` runs them."""
        text = (REPO / "install-flash-local.sh").read_text()
        part = text[text.index('CONTEXT_CHOICE="$CONFIG_DIR/flash-local-context"'):
                    text.index("# Step 4, the one part that needs root")]
        script = "\n".join(["set -euo pipefail", 'die(){ echo "die: $*"; exit 1; }', "step(){ :; }", f'HERE="{REPO}"',
                            "UNIT=qwen38-flash-local.service", f'CONFIG_DIR="{self.cfg}"', f'HF_CACHE="{self.box.hf}"',
                            f'MODEL="{PINS["FLASH_LOCAL_REPO"]}"', f'MODEL_REV="{PINS["FLASH_LOCAL_REV"]}"', f"MODE={mode}",
                            "LOCAL_IMAGE=img GMU=0.8 SEQS=8 ENGINE_BIND=127.0.0.1 PORT=30000", part, tail])
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                           env={"PATH": "/usr/bin:/bin", "HOME": str(self.box.home), **env})
        return r.returncode, r.stdout + r.stderr

    def test_prepare_keeps_the_choice_its_load_renders_once_checked(self):
        # --prepare renders nothing: the choice given with it was dropped, and its Load served the native window with no
        # word of it (second review of v1.45.2); then it kept one a checkpoint already in the cache refuses (third)
        text = (REPO / "install-flash-local.sh").read_text()
        block = text[text.index('if [ "$MODE" = prepare ]; then'):]
        block = block[:block.index("\nfi\n") + 4].replace("exit 0", ":")
        moved = json.loads(json.dumps(self.CONFIG))
        moved["text_config"]["rope_parameters"]["mrope_section"] = [16, 24, 24]
        (self.snap / "config.json").write_text(json.dumps(moved))
        rc, out = self.run_lines("prepare", block, FLASH_LOCAL_CONTEXT="1m")
        self.assertNotEqual(rc, 0, out)
        self.assertIn("RoPE is not the one the 1M window was measured on", out)
        self.assertFalse((self.cfg / "flash-local-context").exists())
        (self.snap / "config.json").write_text(json.dumps(self.CONFIG))
        rc, out = self.run_lines("prepare", block, FLASH_LOCAL_CONTEXT="1m")
        self.assertEqual(rc, 0, out)
        self.assertEqual((self.cfg / "flash-local-context").read_text(), "1m\n")
        (self.snap / "config.json").unlink()                         # not fetched yet: kept, its Load checks it
        (self.cfg / "flash-local-context").unlink()
        rc, out = self.run_lines("prepare", block, FLASH_LOCAL_CONTEXT="1m")
        self.assertEqual((rc, (self.cfg / "flash-local-context").read_text()), (0, "1m\n"), out)

    def test_a_fault_is_not_taken_for_the_checkpoints_refusal(self):
        # a kept choice whose overrides cannot be written (a config that does not parse, a disk full) served the native
        # window and said the checkpoint had refused it (third review of v1.45.2): the Load stops and says the fault
        rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR="none", FLASH_LOCAL_CONTEXT="1m")
        self.assertEqual((rc, self.window()), (0, "1048576"), out)
        (self.snap / "config.json").write_text("{ not json")
        rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR="none")          # a Load
        self.assertNotEqual(rc, 0, out)
        self.assertIn("could not be written", out)
        self.assertNotIn("is off for this checkpoint", out)

    def test_the_pinned_checkpoint_is_the_one_measured(self):
        # the real pinned config, where the box has it: the RoPE the 1M window was measured on (2026-11-09)
        real = pathlib.Path.home() / ".cache/huggingface/hub" / HUB / "snapshots" / PINS["FLASH_LOCAL_REV"] / "config.json"
        if not real.exists():
            # a reason the CI lists (ci.yml): the runner never has the checkpoint, the reference box does
            self.skipTest("needs the pinned bytes cached (the FP4-Local checkpoint's config.json at its pinned revision)")
        t = json.loads(real.read_text())["text_config"]
        self.assertEqual(t["max_position_embeddings"], 262144)
        self.assertEqual({k: t["rope_parameters"][k] for k in self.CONFIG["text_config"]["rope_parameters"]},
                         self.CONFIG["text_config"]["rope_parameters"])


class TheEffortARequestThatNamesNoneGets(unittest.TestCase):
    """v1.46.4: the lane answers at lean when a request names no effort, as the other Qwen lanes do, and
    FLASH_LOCAL_EFFORT=xhigh gives it its model card's xhigh, its default from v1.37.0 to v1.46.3: at xhigh it answered
    nothing 24 times of 32 to a request for a text of about 800 characters, every token spent reasoning (the reference
    box, 2026-11-11). The choice is kept for the Loads after, which stage the unit with none of the user's environment."""

    def setUp(self):
        self.box = Box()
        self.cfg = self.box.home / ".config" / "qwen38"
        self.template = self.cfg / "chat-template-flash-local.jinja"
        self.choice = self.cfg / "flash-local-effort"

    def tearDown(self):
        shutil.rmtree(self.box.t, ignore_errors=True)

    def default(self):
        """The effort the written template gives a request that names none."""
        m = re.search(r"reasoning_effort\|default\('([a-z]+)'\)", self.template.read_text())
        return m.group(1) if m else None

    def test_lean_by_default_xhigh_when_asked_and_kept_for_the_loads_after(self):
        rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR="none")
        self.assertEqual((rc, self.default()), (0, "lean"), out)
        self.assertIn("Supported types are lean (default), xhigh, medium, and low.", self.template.read_text())
        self.assertFalse(self.choice.exists())
        # the 27B and flash lanes' LEAN_DEFAULT, left in a shell, does not reach this lane: its own setting does
        rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR="none", LEAN_DEFAULT="0")
        self.assertEqual((rc, self.default()), (0, "lean"), out)
        rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR="none", FLASH_LOCAL_EFFORT="xhigh")
        self.assertEqual((rc, self.default()), (0, "xhigh"), out)
        self.assertIn("Supported types are lean, xhigh (default), medium, and low.", self.template.read_text())
        self.assertEqual(self.choice.read_text(), "xhigh\n")
        rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR="none")          # a Load: none of the user's environment
        self.assertEqual((rc, self.default()), (0, "xhigh"), out)
        for back in ("lean", ""):                                    # lean, or empty, brings the default back
            with self.subTest(given=back):
                rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR="none", FLASH_LOCAL_EFFORT="xhigh")
                self.assertEqual((rc, self.default()), (0, "xhigh"), out)
                rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR="none", FLASH_LOCAL_EFFORT=back)
                self.assertEqual((rc, self.default()), (0, "lean"), out)
                self.assertFalse(self.choice.exists())
                rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR="none")
                self.assertEqual((rc, self.default()), (0, "lean"), out)

    def test_an_unknown_value_is_refused_and_changes_nothing(self):
        rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR="none", FLASH_LOCAL_EFFORT="xhigh")
        self.assertEqual(rc, 0, out)
        written = self.template.read_text()
        for bad in ("max", "high", "medium", "XHIGH", "lean "):
            with self.subTest(value=bad):
                rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR="none", FLASH_LOCAL_EFFORT=bad)
                self.assertNotEqual(rc, 0, out)
                self.assertIn(f"FLASH_LOCAL_EFFORT={bad}: lean (the default, as on the other Qwen lanes) or xhigh", out)
                self.assertEqual(self.template.read_text(), written)
                self.assertEqual(self.choice.read_text(), "xhigh\n")

    def test_a_box_served_xhigh_by_v1_46_3_answers_at_lean_once_updated(self):
        # v1.46.3 wrote the template with LEAN_DEFAULT=0. Left unset, the patcher keeps the default of the template
        # installed, so the installer says LEAN_DEFAULT=1, or every such box would have stayed at xhigh
        def patch(**env):
            r = subprocess.run([sys.executable, str(REPO / "patch-template.py"), str(self.box.hf), str(self.template),
                                PINS["FLASH_LOCAL_REV"], PINS["FLASH_LOCAL_REPO"]], capture_output=True, text=True,
                               timeout=60, env={"PATH": "/usr/bin:/bin", **env})
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        patch(LEAN_DEFAULT="0")                                       # as install-flash-local.sh of v1.46.3 did
        self.assertEqual(self.default(), "xhigh")
        patch()
        self.assertEqual(self.default(), "xhigh", "the patcher kept the installed default: the installer must say lean")
        rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR="none")          # the update's run, or the lane's next Load
        self.assertEqual((rc, self.default()), (0, "lean"), out)
        self.assertFalse(self.choice.exists())
        # said once, with the command that keeps xhigh: the run after finds a template at lean
        self.assertIn("answers at the lean effort by default from now on (xhigh until v1.46.3): FLASH_LOCAL_EFFORT=xhigh "
                      "./install-flash-local.sh --units-only", out)
        rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR="none")
        self.assertEqual((rc, self.default()), (0, "lean"), out)
        self.assertNotIn("from now on", out)

    def run_lines(self, mode, tail, **env):
        """The installer's own lines from the end of its --uninstall to the render (the template, the choices kept, the
        unit), with vllm-lane.sh's, then `tail`, as `mode` runs them: no sudo, no docker."""
        text = (REPO / "install-flash-local.sh").read_text()
        uninstall = text.index('if [ "$ACTION" = uninstall ]; then')
        part = text[text.index("\n  exit 0\nfi\n", uninstall) + len("\n  exit 0\nfi\n"):
                    text.index("# Step 4, the one part that needs root")]
        script = "\n".join(["set -euo pipefail", 'die(){ echo "die: $*"; exit 1; }', "step(){ :; }", f'HERE="{REPO}"',
                            f'. "{REPO}/vllm-lane.sh"', "UNIT=qwen38-flash-local.service", f'CONFIG_DIR="{self.cfg}"',
                            f'HF_CACHE="{self.box.hf}"', f'MODEL="{PINS["FLASH_LOCAL_REPO"]}"',
                            f'MODEL_REV="{PINS["FLASH_LOCAL_REV"]}"', f"MODE={mode}",
                            "LOCAL_IMAGE=img GMU=0.8 SEQS=8 ENGINE_BIND=127.0.0.1 PORT=30000", part, tail])
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                           env={"PATH": "/usr/bin:/bin", "HOME": str(self.box.home), **env})
        return r.returncode, r.stdout + r.stderr

    def units_lines(self):
        """--units-only's own lines for the template, as the installer has them, then the render of its unit."""
        text = (REPO / "install-flash-local.sh").read_text()
        start = text.index("  lane_table mounts\n", text.index("the image and the checkpoint come with the lane's first Load"))
        lines = text[start + len("  lane_table mounts\n"):text.index("  install_service\n", start)]
        self.assertIn("lane_template_if_cached", lines)
        return lines + f'render_unit "{self.box.t}/units-only.service"'

    def test_units_only_keeps_a_level_given_with_a_window_that_is_refused(self):
        # the command the note and the lane's page give: its template was written apart from lane_template, and its
        # choice kept by the render after it only, which a window refused never reached (second review of v1.46.4)
        moved = json.loads(json.dumps(TheMillionTokenWindow.CONFIG))
        moved["text_config"]["rope_parameters"]["mrope_section"] = [16, 24, 24]
        (self.box.hf / "hub" / HUB / "snapshots" / PINS["FLASH_LOCAL_REV"] / "config.json").write_text(json.dumps(moved))
        rc, out = self.run_lines("units", self.units_lines(), FLASH_LOCAL_EFFORT="xhigh", FLASH_LOCAL_CONTEXT="1m")
        self.assertNotEqual(rc, 0, out)
        self.assertEqual((self.default(), self.choice.read_text()), ("xhigh", "xhigh\n"), out)
        rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR="none")          # the Load after: the level kept
        self.assertEqual((rc, self.default()), (0, "xhigh"), out)

    def test_a_level_asked_for_by_name_is_not_told_the_lane_went_to_lean(self):
        # the note is for a box that asked nothing: FLASH_LOCAL_EFFORT=lean on a lane kept at xhigh said "(xhigh until
        # v1.46.3) ... keeps xhigh" (second review of v1.46.4)
        rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR="none", FLASH_LOCAL_EFFORT="xhigh")
        self.assertEqual((rc, self.default()), (0, "xhigh"), out)
        rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR="none", FLASH_LOCAL_EFFORT="lean")
        self.assertEqual((rc, self.default()), (0, "lean"), out)
        self.assertNotIn("from now on", out)

    def test_units_only_and_prepare_keep_the_choice_for_the_load(self):
        # FLASH_LOCAL_EFFORT=xhigh ./install-flash-local.sh --units-only: its lines (the template when the checkpoint is
        # in the cache, then the unit), before the checkpoint is there and once it is, then --prepare's
        units = self.units_lines()
        source = self.box.hf / "hub" / HUB / "snapshots" / PINS["FLASH_LOCAL_REV"] / "chat_template.jinja"
        source.rename(source.with_name("away.jinja"))
        rc, out = self.run_lines("units", units, FLASH_LOCAL_EFFORT="xhigh")
        self.assertEqual(rc, 0, out)
        self.assertFalse(self.template.exists(), "a template written with no checkpoint")
        self.assertEqual(self.choice.read_text(), "xhigh\n")
        source.with_name("away.jinja").rename(source)
        rc, out = self.box.stage(FLASH_LOCAL_PLE_DIR="none")          # its Load, the checkpoint fetched
        self.assertEqual((rc, self.default()), (0, "xhigh"), out)
        rc, out = self.run_lines("units", units, FLASH_LOCAL_EFFORT="lean")
        self.assertEqual((rc, self.default(), self.choice.exists()), (0, "lean", False), out)
        rc, out = self.run_lines("units", units, FLASH_LOCAL_EFFORT="xhigh")
        self.assertEqual((rc, self.default(), self.choice.read_text()), (0, "xhigh", "xhigh\n"), out)
        text = (REPO / "install-flash-local.sh").read_text()
        block = text[text.index('if [ "$MODE" = prepare ]; then'):]
        block = block[:block.index("\nfi\n") + 4].replace("exit 0", ":")
        rc, out = self.run_lines("prepare", block, FLASH_LOCAL_EFFORT="")
        self.assertEqual((rc, self.choice.exists()), (0, False), out)
        rc, out = self.run_lines("prepare", block, FLASH_LOCAL_EFFORT="xhigh")
        self.assertEqual((rc, self.choice.read_text()), (0, "xhigh\n"), out)


class TheLoadSetsTheClientsForTheWindow(unittest.TestCase):
    """A Load of the FP4-Local lane gives opencode (and through its config pi and claude-local) the pair of the window its
    unit runs (FLASH_LOCAL_CONTEXT=1m, v1.45.2). It added the provider when it was missing and never touched one already
    there: a Load to 1M kept 205,000 under a compaction block sized for 840,000, and a Load back kept 840,000 against a
    proxy that refuses past 250,000 (second review of v1.45.2). switch-model.sh's own block runs here, on copies of the
    two configs, with a systemctl that answers nothing and a sudo that is never reached."""

    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp(prefix="fl-load-oc-"))
        self.addCleanup(shutil.rmtree, self.d, True)
        self.cfg = self.d / "cfg"
        self.home = self.d / "home"
        (self.home / ".config" / "opencode").mkdir(parents=True)
        self.cfg.mkdir()
        (self.cfg / "api-key").write_text("k\n")
        self.bin = self.d / "bin"
        self.bin.mkdir()
        for tool in ("systemctl", "sudo"):
            (self.bin / tool).write_text("#!/bin/sh\nexit 1\n")
            (self.bin / tool).chmod(0o755)
        gen = subprocess.run([sys.executable, str(REPO / "oc-flash-local.py"), "http://127.0.0.1:30001/v1",
                              str(self.cfg / "api-key"), "205000", "32000",
                              "Qwen3.8-Flash-Next FP4-Local NVIDIA + MTP (local, 262K)"],
                             capture_output=True, text=True, check=True)
        base = {"model": "qwen38/qwen3.8-27b", "compaction": {"preserve_recent_tokens": 46000, "prune": False},
                "provider": {"qwen38": {"models": {"qwen3.8-27b": {
                    "name": "x", "limit": {"context": 547000, "input": 547000, "output": 182000}}}},
                             **json.loads(gen.stdout)["provider"]}}
        self.configs = (self.cfg / "opencode.json", self.home / ".config" / "opencode" / "opencode.json")
        for c in self.configs:
            c.write_text(json.dumps(base, indent=2) + "\n")
        text = (REPO / "switch-model.sh").read_text()
        start = text.rindex('  if [ -f "$CONFIG_DIR/opencode.off" ]; then', 0, text.index("FLL_OC_SUM="))
        end = text.index('  echo "Clients reach it through the proxy on :30001 with the box\'s key, as model qwen3.8-flash-next-fp4."')
        self.block = text[start:end].replace("/etc/systemd/system/qwen38-keepalive.service", str(self.d / "keepalive.service"))

    def load(self, window):
        unit = self.d / "qwen38-flash-local.service"
        unit.write_text(f"ExecStart=/bin/bash -c 'exec docker run x vllm serve m \\\n    --max-model-len {window} --max-num-seqs 8'\n")
        script = "\n".join(["set -uo pipefail", f'REPO_DIR="{REPO}"', f'CONFIG_DIR="{self.cfg}"',
                            f'FLASH_LOCAL_UNIT="{unit}"', self.block])
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                           env={"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.home)})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("NOTE", r.stdout + r.stderr)
        return [json.loads(c.read_text()) for c in self.configs]

    def test_a_load_to_1m_and_back_moves_the_limits_of_the_entry_already_there(self):
        for window, ctx, label in ((1048576, 840000, "(local, 1M)"), (262144, 205000, "(local, 262K)")):
            keep = int(subprocess.run(["bash", str(REPO / "oc-limits.sh"), "--preserve", str(ctx)],
                                      capture_output=True, text=True, check=True).stdout)
            for c in self.load(window):
                with self.subTest(window=window):
                    m = c["provider"]["flash-local"]["models"]["qwen3.8-flash-next-fp4"]
                    self.assertEqual(m["limit"], {"context": ctx, "input": ctx, "output": 32000})
                    self.assertTrue(m["name"].endswith(label), m["name"])
                    self.assertEqual(c["model"], "flash-local/qwen3.8-flash-next-fp4")
                    self.assertEqual(c["compaction"]["preserve_recent_tokens"], keep)
                    self.assertLess(keep, ctx - 20000, "a compaction keeps less than its threshold")

    def test_the_provider_a_load_adds_keeps_the_order_of_its_levels(self):
        # xhigh first, as since v1.37.0, whatever the lane's default (lean since v1.46.4, or the xhigh its installer
        # keeps): opencode sends no level when none is picked, so the order decides no request, and another one would
        # have rewritten the generated config and restarted the Agent view's server on every box at its update
        choice = self.cfg / "flash-local-effort"
        for kept, first in ((None, "xhigh"), ("xhigh", "xhigh")):
            with self.subTest(kept=kept):
                for c in self.configs:
                    d = json.loads(c.read_text())
                    del d["provider"]["flash-local"]
                    c.write_text(json.dumps(d, indent=2) + "\n")
                if kept:
                    choice.write_text(kept + "\n")
                for c in self.load(262144):
                    variants = c["provider"]["flash-local"]["models"]["qwen3.8-flash-next-fp4"]["variants"]
                    self.assertEqual(list(variants)[0], first)
                    self.assertEqual(sorted(variants), ["lean", "low", "medium", "xhigh"])


class TheInstallSetsTheClientsForTheWindow(unittest.TestCase):
    """install.sh's own blocks: the user's opencode config gets the FP4-Local pair of the unit's window, as the compaction
    block it merges is sized from that pair when the lane boots, and FLASH_LOCAL_CONTEXT is not read there, which would
    have reached the unit and not the clients sized before it (second review of v1.45.2)."""
    TEXT = (REPO / "install.sh").read_text()

    def test_the_users_config_takes_the_fp4_local_pair(self):
        start = self.TEXT.index('if [ -f "$OC_USER_CFG" ]; then\n  if [ "${LANE:-27b}" = "flash" ]; then')
        block = self.TEXT[start:self.TEXT.index("# The default model and the served entry's picker name follow the install", start)]
        d = pathlib.Path(tempfile.mkdtemp(prefix="fl-install-oc-"))
        self.addCleanup(shutil.rmtree, d, True)
        cfg = d / "opencode.json"
        cfg.write_text(json.dumps({"provider": {
            "qwen38": {"models": {"qwen3.8-27b": {"limit": {"context": 547000, "input": 547000, "output": 182000}}}},
            "flash-local": {"models": {"qwen3.8-flash-next-fp4": {
                "limit": {"context": 205000, "input": 205000, "output": 32000}}}}}}, indent=2) + "\n")
        script = "\n".join(["set -uo pipefail", f'REPO_DIR="{REPO}"', f'OC_USER_CFG="{cfg}"', "LANE=27b",
                            "OC_CTX=547000 OC_OUT=182000 OC_KEEP=205000 CONTEXT_MODE=1m _ENV_CONTEXT_MODE= MEM_CLASS=128",
                            f'SGL_UNIT_PATH="{d}/none.service"', "OC_FLASH_LOCAL=1 OC_FLASH_LOCAL_CTX=840000 OC_FLASH_LOCAL_OUT=32000",
                            block])
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                           env={"PATH": "/usr/bin:/bin", "HOME": str(d)})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        c = json.loads(cfg.read_text())
        self.assertEqual(c["provider"]["flash-local"]["models"]["qwen3.8-flash-next-fp4"]["limit"],
                         {"context": 840000, "input": 840000, "output": 32000})
        self.assertEqual(c["compaction"]["preserve_recent_tokens"], 205000)

    def test_flash_local_context_is_not_read_by_the_install(self):
        start = self.TEXT.index('if [ -n "${FLASH_LOCAL_CONTEXT+set}" ]; then')
        block = self.TEXT[start:self.TEXT.index("\nfi\n", start) + 4]
        self.assertLess(start, self.TEXT.index("install-flash-local.sh\" \"$(side_mode"))
        r = subprocess.run(["bash", "-c", block + 'echo "after=${FLASH_LOCAL_CONTEXT-unset}"'], capture_output=True,
                           text=True, timeout=30, env={"PATH": "/usr/bin:/bin", "FLASH_LOCAL_CONTEXT": "1m"})
        self.assertIn("FLASH_LOCAL_CONTEXT=1m ./install-flash-local.sh --units-only", r.stdout)
        self.assertIn("after=unset", r.stdout)

    def test_flash_local_effort_is_not_read_by_the_install(self):
        # the same for the lane's default effort (v1.46.4): the opencode config is written before the lane's installer
        # runs, so a level given to install.sh would have reached the template a run before the clients' list
        start = self.TEXT.index('if [ -n "${FLASH_LOCAL_EFFORT+set}" ]; then')
        block = self.TEXT[start:self.TEXT.index("\nfi\n", start) + 4]
        self.assertLess(start, self.TEXT.index("install-flash-local.sh\" \"$(side_mode"))
        r = subprocess.run(["bash", "-c", block + 'echo "after=${FLASH_LOCAL_EFFORT-unset}"'], capture_output=True,
                           text=True, timeout=30, env={"PATH": "/usr/bin:/bin", "FLASH_LOCAL_EFFORT": "xhigh"})
        self.assertIn("FLASH_LOCAL_EFFORT=xhigh ./install-flash-local.sh --units-only", r.stdout)
        self.assertIn("after=unset", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
