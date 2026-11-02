#!/usr/bin/env python3
"""The Ornith 1.5 122B lane (install-ornith-122b.sh, v1.38): the unit its --stage writes for a switch,
what its --uninstall removes and keeps, and the two things it shares with the Flash FP4-Local lane,
the image (each lane its own local tag on it) and the seccomp profile b12x's loader needs.

--stage runs the installer as written, in a throwaway home, with no sudo and no docker. The uninstall
blocks of both lanes run whole against a fake unit folder and a fake config directory, sudo,
systemctl and docker stubs writing down every call, as in test_flash_local_uninstall.py."""
import importlib.util
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
TEXT = (REPO / "install-ornith-122b.sh").read_text()
PINS = {k: v.strip('"') for k, v in (ln.split("=", 1) for ln in TEXT.splitlines()
                                       if ln.startswith("ORNITH_122B_") and "=" in ln)}
FL_TEXT = (REPO / "install-flash-local.sh").read_text()
FL_IMAGE = re.search(r'^FLASH_LOCAL_IMAGE="([^"]+)"', FL_TEXT, re.M).group(1)
_spec = importlib.util.spec_from_file_location("patch_template", REPO / "patch-template.py")
PT = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(PT)
# The checkpoint's template as the patcher finds it: its system-message check, as upstream wrote it.
TEMPLATE_AT_PIN = "HEAD\n" + PT.SYSTEM_ANCHOR + "\nTAIL\n"


def exec_command(unit: str) -> str:
    """The ExecStart command as bash runs it: systemd joins the lines a backslash ends, and hands
    bash a $ for each $$."""
    line = unit[unit.index("ExecStart=/bin/bash -c '") + len("ExecStart=/bin/bash -c '"):]
    line = line[:line.index("'\nExecStop=")]
    return line.replace("\\\n", " ").replace("$$", "$")


class TheStagedUnit(unittest.TestCase):
    def setUp(self):
        self.t = pathlib.Path(tempfile.mkdtemp(prefix="ornith-stage-"))
        self.home = self.t / "home"
        self.cfg = self.home / ".config" / "qwen38"
        self.cfg.mkdir(parents=True)
        (self.cfg / "api-key").write_text("test-key\n")
        self.units = self.t / "units"
        self.units.mkdir()
        self.hf = self.t / "hf"
        self.out = self.t / "stage.service"
        self.snap = (self.hf / "hub" / f"models--{PINS['ORNITH_122B_REPO'].replace('/', '--')}" / "snapshots"
                     / PINS["ORNITH_122B_REV"])
        self.snap.mkdir(parents=True)
        (self.snap / "chat_template.jinja").write_text(TEMPLATE_AT_PIN)

    def tearDown(self):
        shutil.rmtree(self.t, ignore_errors=True)

    def stage(self, **env):
        e = {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "HF_CACHE": str(self.hf),
             "LANES_UNIT_DIR": str(self.units), **env}
        r = subprocess.run(["bash", str(REPO / "install-ornith-122b.sh"), "--stage", str(self.out)],
                           capture_output=True, text=True, env=e, timeout=120)
        return r.returncode, r.stdout + r.stderr

    def test_the_unit_is_whole_and_serves_the_pinned_checkpoint_on_the_shared_image(self):
        rc, out = self.stage()
        self.assertEqual(rc, 0, out)
        unit = self.out.read_text()
        self.assertNotRegex(unit, r"__[A-Z][A-Z0-9_]*__", "a placeholder left")
        self.assertIn(f"vllm serve {PINS['ORNITH_122B_REPO']} --revision {PINS['ORNITH_122B_REV']}", unit)
        self.assertIn(f"  {FL_IMAGE} \\\n", unit, "the image is the Flash FP4-Local lane's pin")
        self.assertIn(f"--security-opt seccomp={self.home}/.config/qwen38/seccomp-io-uring.json", unit)
        self.assertNotIn("unconfined", unit)
        self.assertIn(f"-v {self.hf}:/root/.cache/huggingface", unit)
        self.assertIn("--served-model-name ornith-1.5-122b", unit)
        self.assertIn("--host 127.0.0.1 --port 30000", unit)
        self.assertIn(f"--gpu-memory-utilization {PINS['ORNITH_122B_GMU']} ", unit)
        self.assertIn(f"--max-num-seqs {PINS['ORNITH_122B_SEQS']} ", unit)
        self.assertIn(f"--speculative-config.num_speculative_tokens {PINS['ORNITH_122B_SPEC']} ", unit)
        r = subprocess.run(["bash", "-n", "-c", exec_command(unit)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        # what the unit reads at its start is there: the filter, as the repository ships it, and its caches
        self.assertEqual((self.cfg / "seccomp-io-uring.json").read_bytes(), (REPO / "seccomp-io-uring.json").read_bytes())
        for d in ("vllm", "flashinfer", "b12x", "triton"):
            self.assertTrue((self.cfg / "ornith-122b-cache" / d).is_dir(), d)
            self.assertIn(f"-v {self.cfg}/ornith-122b-cache/{d}:", unit)

    def test_the_stage_writes_the_chat_template_with_its_one_fix(self):
        """Ornith's template refused a system message past the first one, which opencode 1.19.4
        sends: every opencode request on this lane got a 400 (2026-10-24). The stage writes the
        checkpoint's template with that one fix, as the unit's engine reads it at its start."""
        rc, out = self.stage()
        self.assertEqual(rc, 0, out)
        tpl = self.cfg / "chat-template-ornith-122b.jinja"
        self.assertEqual(tpl.read_text(), TEMPLATE_AT_PIN.replace(PT.SYSTEM_ANCHOR, PT.SYSTEM_PATCHED))
        unit = self.out.read_text()
        self.assertIn(f"--mount type=bind,source={self.cfg}/chat-template-ornith-122b.jinja,"
                      "target=/out/chat-template-ornith-122b.jinja,readonly", unit)
        self.assertIn("--chat-template /out/chat-template-ornith-122b.jinja", unit)

    def test_a_stage_with_no_template_in_the_cache_writes_no_unit(self):
        (self.snap / "chat_template.jinja").unlink()
        rc, out = self.stage()
        self.assertNotEqual(rc, 0, out)
        self.assertIn("the chat template could not be patched", out)
        self.assertFalse(self.out.exists(), "a unit naming a template that is not there")

    def test_the_install_writes_it_after_the_checkpoint_and_checks_its_patcher(self):
        step3 = TEXT[TEXT.index('step "3/5 Checkpoint'):TEXT.index("install_service\n\nif [ \"$SMOKE\"")]
        self.assertLess(step3.index('vlane_fetch "$MODEL" "$MODEL_REV"'), step3.index("lane_template"))
        pre = TEXT[TEXT.index("vlane_preflight "):TEXT.index("vlane_room ")]
        self.assertIn("patch-template.py", pre)
        self.assertIn("TEMPLATE_FIXES=(--patches=system-reminder,tool-reference)", TEXT)

    def test_the_window_is_a_million_with_yarn_written_where_the_layers_read_it(self):
        # The model card's form, --hf-overrides '{"rope_scaling": ...}', sets a field at the top of the
        # config, which this vLLM's layers do not read: it changed no probability on the reference box
        # (2026-10-21). The dotted form writes text_config.rope_parameters whole: the checkpoint's own
        # values, YaRN added, the dict vLLM builds from it being what the campaign served.
        rc, out = self.stage()
        self.assertEqual(rc, 0, out)
        argv = shlex.split(exec_command(self.out.read_text()))
        args = argv[argv.index("serve") + 1:]
        overrides = {}
        for i, a in enumerate(args):
            if a.startswith("--hf-overrides"):
                self.assertTrue(a.startswith("--hf-overrides.text_config.rope_parameters."), a)
                try:
                    value = json.loads(args[i + 1])
                except ValueError:
                    value = args[i + 1]
                overrides[a.rsplit(".", 1)[1]] = value
        self.assertEqual(overrides, {"mrope_interleaved": True, "mrope_section": [11, 11, 10],
                                     "partial_rotary_factor": 0.25, "rope_theta": 10000000, "rope_type": "yarn",
                                     "factor": 4.0, "original_max_position_embeddings": 262144})
        self.assertNotIn("rope_scaling", " ".join(argv))
        window = int(args[args.index("--max-model-len") + 1])
        self.assertEqual(window, overrides["factor"] * overrides["original_max_position_embeddings"])
        # vLLM derives 262,144 from max_position_embeddings and refuses a longer window without this
        self.assertIn("VLLM_ALLOW_LONG_MAX_MODEL_LEN=1", argv)
        # the pool is fixed, not a share of the memory, whose pool fell under the window at some boots
        self.assertEqual(args[args.index("--kv-cache-memory-bytes") + 1], PINS["ORNITH_122B_KV_BYTES"])

    def test_an_agents_next_request_starts_from_the_cache_up_to_where_it_differs(self):
        # Without it vLLM matched whole blocks of this hybrid model (4,224 tokens) less the one MTP
        # drops: opencode and Claude Code recomputed 10,000 to 17,000 tokens a turn on the second GX10
        # (2026-10-22), where with it 99 to 100 % of each prompt came from the cache.
        rc, out = self.stage()
        self.assertEqual(rc, 0, out)
        args = shlex.split(exec_command(self.out.read_text()))
        args = args[args.index("serve") + 1:]
        self.assertIn("--prefix-match-unit", args)
        unit = int(args[args.index("--prefix-match-unit") + 1])
        # vLLM refuses to start on a unit that does not divide the block it picks for this model
        self.assertEqual((unit, 4224 % unit), (16, 0))
        # and it acts only on a prefix cache aligned to the Mamba blocks; MTP drafting stays on
        self.assertIn("--enable-prefix-caching", args)
        self.assertEqual(args[args.index("--mamba-cache-mode") + 1], "align")
        self.assertEqual(args[args.index("--speculative-config.method") + 1], "mtp")

    def test_a_long_prompt_leaves_the_engine_to_the_other_requests(self):
        # Issue #61 (2026-10-31): while a cold prompt of 362,694 tokens was computed, 20 KB requests sent by
        # 10 clients took 265 s at the median for 64 tokens (484 s at worst) with vLLM's defaults, and 34 s
        # (62 s) with these four settings, a prompt alone taking the time it took. A step the prompt has alone
        # keeps its 4,096 tokens: steps of 8,192 kept 8 requests of 80 waiting 668 to 777 s for their first token.
        rc, out = self.stage()
        self.assertEqual(rc, 0, out)
        args = shlex.split(exec_command(self.out.read_text()))
        args = args[args.index("serve") + 1:]
        got = {k: args[args.index(k) + 1] for k in ("--max-parallel-prefills", "--prefill-policy",
                                                     "--prefill-compute-share", "--max-num-prefill-tokens-per-step")}
        self.assertEqual(got, {"--max-parallel-prefills": "auto", "--prefill-policy": "decode-aware",
                               "--prefill-compute-share": "0.25", "--max-num-prefill-tokens-per-step": "1024"})
        self.assertEqual(args[args.index("--max-num-batched-tokens") + 1], "4096")
        for flag in got:
            self.assertEqual(args.count(flag), 1, flag)

    def test_the_engine_sees_its_key_and_its_template_alone_and_runs_no_code_of_the_checkpoint(self):
        # the image is a community build: of the config directory, the key and the chat template,
        # each read-only, and nothing else; no remote code. The template is the checkpoint's with
        # one fix (Hasan, 2026-10-24, where 2026-10-21 had chosen it as published: opencode 1.19.4
        # sends a second system message, which the published one refuses)
        rc, out = self.stage()
        self.assertEqual(rc, 0, out)
        cmd = exec_command(self.out.read_text())
        self.assertIn(f"--mount type=bind,source={self.cfg}/api-key,target=/out/api-key,readonly", cmd)
        self.assertIn(f"--mount type=bind,source={self.cfg}/chat-template-ornith-122b.jinja,"
                      "target=/out/chat-template-ornith-122b.jinja,readonly", cmd)
        self.assertNotRegex(cmd, rf"-v {re.escape(str(self.cfg))}:")
        self.assertEqual(re.findall(r"--chat-template (\S+)", cmd), ["/out/chat-template-ornith-122b.jinja"])
        self.assertNotIn("--trust-remote-code", cmd)
        self.assertIn("--middleware vllm_engine_auth.RequireKey", cmd)
        self.assertIn("--allowed-media-domains media.invalid", cmd)
        self.assertIn("-e HF_HUB_OFFLINE=1", cmd)
        self.assertIn("-e VLLM_NO_USAGE_STATS=1", cmd)

    def test_its_boot_leaves_the_box_its_memory(self):
        # the Flash FP4-Local lane's allocator settings, for the same b12x loader: the ceiling above the
        # share vLLM is given, the collection under it (test_flash_local_stage.py says why)
        rc, out = self.stage()
        self.assertEqual(rc, 0, out)
        cmd = exec_command(self.out.read_text())
        conf = dict(kv.split(":", 1) for kv in re.search(r"-e PYTORCH_CUDA_ALLOC_CONF=(\S+)", cmd).group(1).split(","))
        ceiling, threshold = float(conf["per_process_memory_fraction"]), float(conf["garbage_collection_threshold"])
        share = float(re.search(r"--gpu-memory-utilization (\S+)", cmd).group(1))
        self.assertTrue(share < ceiling < 1.0, (share, ceiling))
        self.assertTrue(share - 0.05 <= ceiling * threshold < share, (ceiling, threshold, share))

    def test_a_filter_already_there_is_left_as_it_is(self):
        (self.cfg / "seccomp-io-uring.json").write_text('{"kept": true}\n')
        rc, out = self.stage()
        self.assertEqual(rc, 0, out)
        self.assertEqual((self.cfg / "seccomp-io-uring.json").read_text(), '{"kept": true}\n')

    def test_with_no_file_to_write_to_it_refuses(self):
        r = subprocess.run(["bash", str(REPO / "install-ornith-122b.sh"), "--stage"], capture_output=True, text=True,
                           env={"PATH": "/usr/bin:/bin", "HOME": str(self.home), "LANES_UNIT_DIR": str(self.units)},
                           timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--stage takes the file to write the unit to", r.stdout + r.stderr)


def uninstall_block(text: str) -> str:
    start = text.index('if [ "$ACTION" = uninstall ]; then')
    return text[start:text.index("\n  exit 0\nfi\n", start) + len("\n  exit 0\nfi\n")]


class TheUninstalls(unittest.TestCase):
    """Each lane's --uninstall keeps the seccomp profile while the other lane's unit reads it."""

    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp(prefix="ornith-uninstall-"))
        self.home = self.d / "home"
        self.cfg = self.home / ".config" / "qwen38"
        self.cfg.mkdir(parents=True)
        (self.cfg / "vllm-auth").mkdir()
        (self.cfg / "seccomp-io-uring.json").write_text("{}\n")
        self.sd = self.d / "systemd"
        self.sd.mkdir()
        self.bin = self.d / "bin"
        self.bin.mkdir()
        self.log = self.d / "calls.log"
        for tool in ("sudo", "docker", "systemctl"):
            (self.bin / tool).write_text(f'#!/bin/bash\necho "{tool} $*" >> "{self.log}"\n'
                                         + ('[ "$1" = is-enabled ] && exit 1\n' if tool == "systemctl" else "")
                                         + "exit 0\n")
            (self.bin / tool).chmod(0o755)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def run_block(self, lane, others=()):
        text = TEXT if lane == "ornith-122b" else FL_TEXT
        pins = [ln for ln in text.splitlines()
                if ln.startswith(("ORNITH_122B_REPO=", "FLASH_LOCAL_REPO=", "FLASH_LOCAL_IMAGE=", "VLANE="))]
        if lane == "ornith-122b":
            pins.append(f'FLASH_LOCAL_IMAGE="{FL_IMAGE}"')
        unit = self.sd / f"qwen38-{lane}.service"
        unit.write_text("ExecStart=/bin/bash -c 'exec /usr/bin/docker run x'\n")
        for o in others:
            (self.sd / f"qwen38-{o}.service").write_text("ExecStart=/bin/true\n")
        script = "\n".join(["set -euo pipefail", "step(){ :; }", f'LANES_UNIT_DIR="{self.sd}"',
                            f'. "{REPO}/lanes.sh"', f'. "{REPO}/vllm-lane.sh"', *pins, f'HERE="{REPO}"',
                            "ACTION=uninstall", f"UNIT=qwen38-{lane}.service", f'INSTALLED="{unit}"',
                            f"CONTAINER=qwen38-{lane}", f'CONFIG_DIR="{self.cfg}"', f'HF_CACHE="{self.d}/hf"',
                            uninstall_block(text)])
        env = dict(os.environ, PATH=f"{self.bin}:/usr/bin:/bin", HOME=str(self.home))
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout, self.log.read_text() if self.log.exists() else ""

    def test_ornith_alone_takes_the_profile_with_it(self):
        out, calls = self.run_block("ornith-122b")
        self.assertFalse((self.cfg / "seccomp-io-uring.json").exists())
        self.assertIn("docker rm -f qwen38-ornith-122b", calls)
        self.assertIn("rm -f", calls)

    def test_its_template_goes_with_it(self):
        (self.cfg / "chat-template-ornith-122b.jinja").write_text("x\n")
        self.run_block("ornith-122b")
        self.assertFalse((self.cfg / "chat-template-ornith-122b.jinja").exists())

    def test_beside_flash_local_ornith_leaves_the_profile(self):
        out, _ = self.run_block("ornith-122b", others=["flash-local"])
        self.assertTrue((self.cfg / "seccomp-io-uring.json").exists())
        self.assertIn("seccomp-io-uring.json kept: the Flash FP4-Local lane reads it too", out)

    def test_beside_ornith_flash_local_leaves_the_profile(self):
        out, _ = self.run_block("flash-local", others=["ornith-122b"])
        self.assertTrue((self.cfg / "seccomp-io-uring.json").exists())
        self.assertIn("seccomp-io-uring.json kept: the Ornith 1.5 122B lane reads it too", out)

    def test_the_vllm_auth_middleware_stays_while_another_vllm_lane_is_installed(self):
        out, _ = self.run_block("ornith-122b", others=["kolibri"])
        self.assertTrue((self.cfg / "vllm-auth").is_dir())
        self.assertIn("vllm-auth kept: another vLLM lane uses it too", out)


class TheSharedImagesTags(unittest.TestCase):
    """pin_untag (lanes.sh): a lane's tag goes with its uninstall, and the line says whether the image
    can go too. The Flash FP4-Local and Ornith lanes pin one image, each with a tag of its own: with
    the other lane's tag still on it, "a docker image prune can reclaim it" was not true."""

    def untag(self, tags, units=None):
        d = pathlib.Path(tempfile.mkdtemp(prefix="ornith-tags-"))
        try:
            (d / "bin").mkdir()
            (d / "units").mkdir()   # the box's own units are not this test's
            for name, text in (units or {}).items():
                (d / "units" / name).write_text(text)
            state = d / "tags"
            state.write_text("".join(t + "\n" for t in tags))
            # a docker that knows one image, its tags in a file: images lists them, rmi drops one,
            # image inspect prints those left
            (d / "bin" / "docker").write_text(f"""#!/bin/bash
S="{state}"
case "$1 $2" in
  "images --no-trunc") while read -r t; do echo "$t sha256:abc"; done < "$S" ;;
  "rmi "*) grep -vx "$2" "$S" > "$S.n"; mv "$S.n" "$S" ;;
  "image inspect") while read -r t; do echo "$t"; done < "$S" ;;
  "tag "*) echo "$3" >> "$S" ;;
esac
exit 0
""")
            (d / "bin" / "docker").chmod(0o755)
            r = subprocess.run(["bash", "-c", f'. "{REPO}/lanes.sh"; pin_untag ornith-122b'], capture_output=True,
                               text=True, timeout=30, env={"PATH": f"{d / 'bin'}:/usr/bin:/bin", "HOME": str(d),
                                    "LANES_UNIT_DIR": str(d / "units")})
            return r.returncode, r.stdout, state.read_text()
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_with_the_other_lanes_tag_on_it_the_image_stays_and_the_line_says_so(self):
        rc, out, left = self.untag(["qwen38-pinned:ornith-122b-03a956544e9f", "qwen38-pinned:flash-local-03a956544e9f"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(left, "qwen38-pinned:flash-local-03a956544e9f\n")
        self.assertIn("untagged qwen38-pinned:ornith-122b-03a956544e9f: the image stays, "
                      "qwen38-pinned:flash-local-03a956544e9f still names it", out)
        self.assertNotIn("prune can reclaim it", out)

    def test_the_other_lanes_unit_that_runs_it_gives_it_its_tag(self):
        # Flash FP4-Local pulled the image, Ornith was loaded from the cockpit after it (no tag of its
        # own), then Flash FP4-Local went: the Ornith unit runs the image by its digest
        image = "eugr/spark-vllm-b12x@sha256:03a956544e9f" + "9b4b7ba91690469fbe7848bdbef29a7d2e0f5788368467bb3595"
        rc, out, left = self.untag(["qwen38-pinned:ornith-122b-03a956544e9f"],
                                   units={"qwen38-flash-local.service": f"ExecStart=... {image} vllm serve ...\n",
                                          "qwen38-kolibri.service": "ExecStart=... qwen38-kolibri:local ...\n"})
        self.assertEqual(rc, 0, out)
        self.assertEqual(left, "qwen38-pinned:flash-local-03a956544e9f\n")
        self.assertIn("untagged qwen38-pinned:ornith-122b-03a956544e9f: the image stays, tagged "
                      "qwen38-pinned:flash-local-03a956544e9f, as the flash-local lane's unit runs it", out)
        self.assertNotIn("prune can reclaim it", out)

    def test_a_unit_of_another_image_gives_it_nothing(self):
        other = "eugr/spark-vllm-b12x@sha256:" + "0" * 64
        rc, out, left = self.untag(["qwen38-pinned:ornith-122b-03a956544e9f"],
                                   units={"qwen38-flash-local.service": f"ExecStart=... {other} ...\n"})
        self.assertEqual(left, "")
        self.assertIn("a docker image prune can reclaim it", out)

    def test_alone_on_it_the_image_is_left_to_a_prune(self):
        rc, out, left = self.untag(["qwen38-pinned:ornith-122b-03a956544e9f"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(left, "")
        self.assertIn("untagged qwen38-pinned:ornith-122b-03a956544e9f: a docker image prune can reclaim it", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
