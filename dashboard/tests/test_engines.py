"""dashboard/engines.py names each lane's engine and its version everywhere in the cockpit (decision 1178): a
version said there must be the one the box installs. Each entry's pin is read back in the lane's installer; a pin
that moves without the table fails here."""
import importlib.util
import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
DASH = REPO / "dashboard"


def load(name):
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", DASH / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


EN = load("engines")
LC = load("lifecycle")


def assigned(script, var):
    """The value a script gives var (VAR="..." or VAR="${VAR:-...}"), or None."""
    m = re.search(rf'(?m)^{var}="(?:\$\{{[A-Z0-9_]+:-)?([^"}}]+)', (REPO / script).read_text())
    return m.group(1) if m else None


class EachLaneHasItsEngine(unittest.TestCase):
    def test_every_engine_unit_and_none_other(self):
        self.assertEqual(set(EN.ENGINES), set(LC.ENGINE_UNITS))

    def test_a_label_is_the_engine_and_its_version(self):
        self.assertEqual(EN.label("qwen38-sglang.service"), "SGLang 0.5.22")
        self.assertEqual(EN.label("qwen38-keepalive.service"), "")
        table = EN.page_table()
        self.assertEqual(table["qwen38-kolibri.service"]["label"], "vLLM 0.29.0 + Aleph Alpha plugin 1.0.0")
        self.assertEqual(set(table), set(EN.ENGINES))


class WhatEachEngineReports(unittest.TestCase):
    def test_it_says_the_pinned_version(self):
        # the page tells the pin's version from an image of the operator's by this string (decision 1178): it says
        # the same version, or the serving lane would read as another image
        for unit, e in EN.ENGINES.items():
            with self.subTest(unit=unit):
                if e["reports"] is None:
                    self.assertEqual(e["engine"], "SGLang Diffusion")       # the cockpit reads no version there
                    continue
                said = e["version"].split(" + ")[0].split()[-1]             # 0.5.22, 53295c6, 8244d1c, 0.29.0, 77154ca6
                self.assertIn(said, e["reports"], (e["version"], e["reports"]))
        self.assertEqual(EN.page_table()["qwen38-flash.service"]["reports"], "0.0.0.dev1+g53295c697")


class WhetherAUnitRunsThePin(unittest.TestCase):
    def test_the_pin_by_its_digest_or_the_exact_tag_its_installer_builds_on_it(self):
        # decision 1178: a lane is given its pin's version only when its unit starts that image (code review of v1.45.0)
        pin = EN.ENGINES["qwen38-sglang.service"]["pin"]
        self.assertTrue(EN.runs_pin("qwen38-sglang.service", [pin]))
        self.assertFalse(EN.runs_pin("qwen38-sglang.service", ["lmsysorg/sglang@sha256:" + "a" * 64]))
        self.assertFalse(EN.runs_pin("qwen38-sglang.service", ["lmsysorg/sglang:v0.5.23"]))
        # the moving tag of the pin, which install.sh offers when the digest cannot be pulled (third review of v1.45.0)
        self.assertTrue(EN.runs_pin("qwen38-sglang.service", ["lmsysorg/sglang:v0.5.22"]))
        self.assertTrue(EN.runs_pin("qwen38-flash.service", ["lmsysorg/sglang:nightly-cu134-20261011-53295c6"]))
        self.assertTrue(EN.runs_pin("qwen38-kolibri.service", ["qwen38-kolibri:vllm0.29.0-aai1.0.0-18372a72249386434"]))
        self.assertTrue(EN.runs_pin("qwen38-nemotron.service", ["qwen38-nemotron:vllm77154ca6-b12x1.5.1-1ee85ca80e0fc"]))
        # an older build on the same base is not the pinned engine: its b12x is in its name (second review of v1.45.0)
        self.assertFalse(EN.runs_pin("qwen38-nemotron.service", ["qwen38-nemotron:vllm77154ca6-b12x1.5.0-1ee85ca80e0fc"]))
        self.assertFalse(EN.runs_pin("qwen38-kolibri.service", ["qwen38-kolibri:vllm0.29.0-aai0.9.0-18372a72249386434"]))
        # what it cannot tell: no unit file read, a wheel
        self.assertIsNone(EN.runs_pin("qwen38-sglang.service", []))
        self.assertIsNone(EN.runs_pin("qwen38-image.service", ["anything"]))

    def test_the_tag_of_a_build_is_the_one_its_installer_gives_it(self):
        # the installers name the image they build on the pin (LOCAL_IMAGE, cut at 52 characters): the table's tag is
        # that name, read back from their variables, or every box would read "another image" after a pin moved
        for script, unit, line in (
                ("install-kolibri.sh", "qwen38-kolibri.service",
                 'LOCAL_IMAGE="qwen38-kolibri:vllm0.29.0-aai${KOLIBRI_PLUGIN_VERSION}-${KOLIBRI_BASE_IMAGE##*:}"'),
                ("install-nemotron.sh", "qwen38-nemotron.service",
                 'LOCAL_IMAGE="qwen38-nemotron:vllm${NEMOTRON_VLLM_LABEL#nightly-}-b12x${NEMOTRON_B12X_VERSION}-'
                 '${NEMOTRON_BASE_IMAGE##*:}"'),
                ("install-nemotron-super.sh", "qwen38-nemotron-super.service",
                 'LOCAL_IMAGE="qwen38-nemotron:vllm${NEMOTRON_VLLM_LABEL#nightly-}-b12x${NEMOTRON_B12X_VERSION}-'
                 '${NEMOTRON_BASE_IMAGE##*:}"')):
            with self.subTest(script=script):
                text = (REPO / script).read_text()
                self.assertIn(line, text)
                self.assertIn('LOCAL_IMAGE="${LOCAL_IMAGE:0:52}"', text)
        kol = ("qwen38-kolibri:vllm0.29.0-aai" + assigned("install-kolibri.sh", "KOLIBRI_PLUGIN_VERSION") + "-"
               + assigned("install-kolibri.sh", "KOLIBRI_BASE_IMAGE").split(":")[-1])[:52]
        self.assertEqual(EN.ENGINES["qwen38-kolibri.service"]["unit_image"], kol)
        label = assigned("install-nemotron.sh", "NEMOTRON_VLLM_LABEL")
        nem = ("qwen38-nemotron:vllm" + label[len("nightly-"):] if label.startswith("nightly-") else
               "qwen38-nemotron:vllm" + label)
        nem = (nem + "-b12x" + assigned("install-nemotron.sh", "NEMOTRON_B12X_VERSION") + "-"
               + assigned("install-nemotron.sh", "NEMOTRON_BASE_IMAGE").split(":")[-1])[:52]
        for unit in ("qwen38-nemotron.service", "qwen38-nemotron-super.service"):
            self.assertEqual(EN.ENGINES[unit]["unit_image"], nem)


class EachVersionIsTheOneInstalled(unittest.TestCase):
    def pin(self, unit):
        return EN.ENGINES[unit]["pin"]

    def test_the_sglang_text_lanes(self):
        self.assertEqual(self.pin("qwen38-sglang.service"), assigned("install.sh", "IMAGE"))
        self.assertEqual(self.pin("qwen38-flash.service"), assigned("install.sh", "FLASH_IMAGE"))
        text = (REPO / "install.sh").read_text()
        # the image's own tag, written beside its digest
        self.assertIn("lmsysorg/sglang:v" + EN.ENGINES["qwen38-sglang.service"]["version"], text)
        self.assertIn("nightly-cu134-20261011-" + EN.ENGINES["qwen38-flash.service"]["version"].split()[-1], text)
        # the moving tag a unit may name instead: the one install.sh writes beside each digest
        for unit in ("qwen38-sglang.service", "qwen38-flash.service"):
            self.assertIn("= " + EN.ENGINES[unit]["tag"], text, unit)

    def test_the_vllm_lanes(self):
        local = assigned("install-flash-local.sh", "FLASH_LOCAL_IMAGE")
        self.assertEqual(self.pin("qwen38-flash-local.service"), local)
        self.assertIn('LOCAL_IMAGE="$FLASH_LOCAL_IMAGE"', (REPO / "install-ornith-122b.sh").read_text())
        self.assertEqual(self.pin("qwen38-ornith-122b.service"), local)
        self.assertEqual(self.pin("qwen38-kolibri.service"), assigned("install-kolibri.sh", "KOLIBRI_BASE_IMAGE"))
        self.assertIn("Aleph Alpha plugin " + assigned("install-kolibri.sh", "KOLIBRI_PLUGIN_VERSION"),
                      EN.ENGINES["qwen38-kolibri.service"]["version"])
        for unit in ("qwen38-nemotron.service", "qwen38-nemotron-super.service"):
            with self.subTest(unit=unit):
                self.assertEqual(self.pin(unit), assigned("install-nemotron.sh", "NEMOTRON_BASE_IMAGE"))
                v = EN.ENGINES[unit]["version"]
                self.assertIn(assigned("install-nemotron.sh", "NEMOTRON_VLLM_LABEL").replace("-", " "), v)
                self.assertIn("b12x " + assigned("install-nemotron.sh", "NEMOTRON_B12X_VERSION"), v)

    def test_the_diffusion_lanes(self):
        for unit, script in (("qwen38-image.service", "install-image.sh"), ("qwen38-video.service", "install-video.sh")):
            with self.subTest(unit=unit):
                wheel = assigned(script, "WHEEL")
                self.assertEqual(EN.ENGINES[unit]["version"], wheel)
                self.assertEqual(self.pin(unit), f"sglang[diffusion]=={wheel}")


if __name__ == "__main__":
    unittest.main()
