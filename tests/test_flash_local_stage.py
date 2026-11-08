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
        # what the unit reads at its start is there: the filter, the template with xhigh as its default
        cfg = self.box.home / ".config" / "qwen38"
        self.assertEqual((cfg / "seccomp-io-uring.json").read_bytes(), (REPO / "seccomp-io-uring.json").read_bytes())
        self.assertIn("reasoning_effort|default('xhigh')", (cfg / "chat-template-flash-local.jinja").read_text())

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
