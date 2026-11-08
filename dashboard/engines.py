"""The engine each lane runs, and its version, as the cockpit names them everywhere (decision 1178 of Hasan,
2026-11-07: « voir quel moteur est utilisé pour chaque lane, chaque modèle dans le cockpit »).

One table, by unit: the engine's name, its version as its makers number it, and what the lane's installer pins
for it (an image by its digest, or a wheel by its version). tests/test_engines.py reads each installer and fails
when a pin moves without this table: a version said here is the one the box installs.

Each version was read in the pinned image itself on the reference box on 2026-11-08 (importlib.metadata in the
image's Python, or the image's /workspace/build-metadata.yaml), never from a tag name alone."""

ENGINES = {
    # install.sh IMAGE ; the image's sglang distribution says 0.5.22
    "qwen38-sglang.service": {"engine": "SGLang", "version": "0.5.22",
                              "pin": "lmsysorg/sglang@sha256:e3a4f06001a29542fc4a8638536d9ffc5eb9c9942b08603c09b19a56eae4a1ae"},
    # install.sh FLASH_IMAGE, the nightly-cu134-20261011-53295c6 tag ; its sglang says 0.0.0.dev1+g53295c697
    "qwen38-flash.service": {"engine": "SGLang", "version": "nightly 53295c6",
                             "pin": "lmsysorg/sglang@sha256:3e79541d1e2a6818979c4aa9ca1fa63d6961d7bc50120867953ea6baaf1d8f62"},
    # install-flash-local.sh FLASH_LOCAL_IMAGE ; build-metadata.yaml: vLLM of local-inference-lab, branch
    # dev/karmic-kraken, commit 8244d1c, built 2026-10-15, with b12x
    "qwen38-flash-local.service": {"engine": "vLLM", "version": "b12x build 8244d1c",
                                   "pin": "eugr/spark-vllm-b12x@sha256:03a956544e9f9b4b7ba91690469fbe7848bdbef29a7d2e0f5788368467bb3595"},
    # install-ornith-122b.sh takes the Flash FP4-Local lane's image
    "qwen38-ornith-122b.service": {"engine": "vLLM", "version": "b12x build 8244d1c",
                                   "pin": "eugr/spark-vllm-b12x@sha256:03a956544e9f9b4b7ba91690469fbe7848bdbef29a7d2e0f5788368467bb3595"},
    # install-kolibri.sh KOLIBRI_BASE_IMAGE (vLLM 0.29.0) and KOLIBRI_PLUGIN_VERSION, Aleph Alpha's plugin
    "qwen38-kolibri.service": {"engine": "vLLM", "version": "0.29.0 + Aleph Alpha plugin 1.0.0",
                               "pin": "vllm/vllm-openai@sha256:18372a7224938643461b846fb64c5c9d3d6e9727e82caf2dc3043e620c9d4d7a"},
    # install-nemotron.sh NEMOTRON_VLLM_LABEL and NEMOTRON_B12X_VERSION, built on NEMOTRON_BASE_IMAGE
    "qwen38-nemotron.service": {"engine": "vLLM", "version": "nightly 77154ca6 + b12x 1.5.1",
                                "pin": "vllm/vllm-openai@sha256:1ee85ca80e0fc110017d79ea2081a0cbef9a208a05d7f22724801bcf6a956620"},
    "qwen38-nemotron-super.service": {"engine": "vLLM", "version": "nightly 77154ca6 + b12x 1.5.1",
                                      "pin": "vllm/vllm-openai@sha256:1ee85ca80e0fc110017d79ea2081a0cbef9a208a05d7f22724801bcf6a956620"},
    # install-image.sh and install-video.sh WHEEL: SGLang's diffusion runtime in the lane's own venv
    "qwen38-image.service": {"engine": "SGLang Diffusion", "version": "0.5.21", "pin": "sglang[diffusion]==0.5.21"},
    "qwen38-video.service": {"engine": "SGLang Diffusion", "version": "0.5.21", "pin": "sglang[diffusion]==0.5.21"},
}


def label(unit: str) -> str:
    """« SGLang 0.5.22 » : the engine and its version, the one way the cockpit says them; '' for a unit it has none."""
    e = ENGINES.get(unit)
    return f"{e['engine']} {e['version']}" if e else ""


def page_table() -> dict:
    """What the page reads (its config): by unit, the engine, its version, the label, and the pin, which the
    Library compares with a recipe's image (a recipe of the operator's may name another image)."""
    return {u: {"engine": e["engine"], "version": e["version"], "label": label(u), "pin": e["pin"]}
            for u, e in ENGINES.items()}
