"""The engine each lane runs, and its version, as the cockpit names them everywhere (decision 1178 of Hasan,
2026-11-07: « voir quel moteur est utilisé pour chaque lane, chaque modèle dans le cockpit »).

One table, by unit: the engine's name, its version as its makers number it, and what the lane's installer pins
for it (an image by its digest, or a wheel by its version). tests/test_engines.py reads each installer and fails
when a pin moves without this table: a version said here is the one the box installs.

Each version was read in the pinned image itself on the reference box on 2026-11-08 (importlib.metadata in the
image's Python, or the image's /workspace/build-metadata.yaml), never from a tag name alone.

reports: the version string the engine itself says when it serves the pinned image (SGLang's /server_info, vLLM's
/version: its package's own version, read in each image the same day, in the image the lane runs). The page says the
serving lane's engine by what it reports: a version other than this one is an image of the operator's. None for the
diffusion lanes, whose version the cockpit does not read."""

ENGINES = {
    # install.sh IMAGE ; the image's sglang distribution says 0.5.22
    "qwen38-sglang.service": {
        "engine": "SGLang", "version": "0.5.22", "reports": "0.5.22",
        "pin": "lmsysorg/sglang@sha256:e3a4f06001a29542fc4a8638536d9ffc5eb9c9942b08603c09b19a56eae4a1ae",
        "tag": "lmsysorg/sglang:v0.5.22"},
    # install.sh FLASH_IMAGE, the nightly-cu134-20261011-53295c6 tag ; its sglang says 0.0.0.dev1+g53295c697
    "qwen38-flash.service": {
        "engine": "SGLang", "version": "nightly 53295c6", "reports": "0.0.0.dev1+g53295c697",
        "pin": "lmsysorg/sglang@sha256:3e79541d1e2a6818979c4aa9ca1fa63d6961d7bc50120867953ea6baaf1d8f62",
        "tag": "lmsysorg/sglang:nightly-cu134-20261011-53295c6"},
    # install-flash-local.sh FLASH_LOCAL_IMAGE ; build-metadata.yaml: vLLM of local-inference-lab, branch
    # dev/karmic-kraken, commit 8244d1c, built 2026-10-15, with b12x
    "qwen38-flash-local.service": {
        "engine": "vLLM", "version": "b12x build 8244d1c", "reports": "0.1.dev21830+g8244d1c44.d20261015",
        "pin": "eugr/spark-vllm-b12x@sha256:03a956544e9f9b4b7ba91690469fbe7848bdbef29a7d2e0f5788368467bb3595"},
    # install-ornith-122b.sh takes the Flash FP4-Local lane's image
    "qwen38-ornith-122b.service": {
        "engine": "vLLM", "version": "b12x build 8244d1c", "reports": "0.1.dev21830+g8244d1c44.d20261015",
        "pin": "eugr/spark-vllm-b12x@sha256:03a956544e9f9b4b7ba91690469fbe7848bdbef29a7d2e0f5788368467bb3595"},
    # install-kolibri.sh KOLIBRI_BASE_IMAGE (vLLM 0.29.0) and KOLIBRI_PLUGIN_VERSION, Aleph Alpha's plugin
    "qwen38-kolibri.service": {
        "engine": "vLLM", "version": "0.29.0 + Aleph Alpha plugin 1.0.0", "reports": "0.29.0",
        "pin": "vllm/vllm-openai@sha256:18372a7224938643461b846fb64c5c9d3d6e9727e82caf2dc3043e620c9d4d7a",
        # the image install-kolibri.sh builds on the pin with the plugin, the tag its unit starts (LOCAL_IMAGE)
        "unit_image": "qwen38-kolibri:vllm0.29.0-aai1.0.0-18372a72249386434"},
    # install-nemotron.sh NEMOTRON_VLLM_LABEL and NEMOTRON_B12X_VERSION, built on NEMOTRON_BASE_IMAGE
    "qwen38-nemotron.service": {
        "engine": "vLLM", "version": "nightly 77154ca6 + b12x 1.5.1", "reports": "0.32.0rc1.dev100+g77154ca61",
        "pin": "vllm/vllm-openai@sha256:1ee85ca80e0fc110017d79ea2081a0cbef9a208a05d7f22724801bcf6a956620",
        # the image install-nemotron.sh builds on the pin with b12x, the tag its unit starts (LOCAL_IMAGE)
        "unit_image": "qwen38-nemotron:vllm77154ca6-b12x1.5.1-1ee85ca80e0fc"},
    "qwen38-nemotron-super.service": {
        "engine": "vLLM", "version": "nightly 77154ca6 + b12x 1.5.1", "reports": "0.32.0rc1.dev100+g77154ca61",
        "pin": "vllm/vllm-openai@sha256:1ee85ca80e0fc110017d79ea2081a0cbef9a208a05d7f22724801bcf6a956620",
        "unit_image": "qwen38-nemotron:vllm77154ca6-b12x1.5.1-1ee85ca80e0fc"},
    # install-image.sh and install-video.sh WHEEL: SGLang's diffusion runtime in the lane's own venv
    "qwen38-image.service": {"engine": "SGLang Diffusion", "version": "0.5.21", "reports": None,
                             "pin": "sglang[diffusion]==0.5.21"},
    "qwen38-video.service": {"engine": "SGLang Diffusion", "version": "0.5.21", "reports": None,
                             "pin": "sglang[diffusion]==0.5.21"},
}


def label(unit: str) -> str:
    """« SGLang 0.5.22 » : the engine and its version, the one way the cockpit says them; '' for a unit it has none."""
    e = ENGINES.get(unit)
    return f"{e['engine']} {e['version']}" if e else ""


def page_table() -> dict:
    """What the page reads (its config): by unit, the engine, its version, the label, the pin, which the Library
    compares with a recipe's image (a recipe of the operator's may name another image), and what the engine reports
    when it serves the pin."""
    return {u: {"engine": e["engine"], "version": e["version"], "label": label(u), "pin": e["pin"],
                "reports": e["reports"]} for u, e in ENGINES.items()}


def runs_pin(unit: str, images) -> "bool | None":
    """Whether a unit that starts these images runs its pinned engine: the pin by its digest, or, for a lane that
    builds its image on the pin, the exact tag its installer gives that build (unit_image: its plugin, its b12x and
    its base in the name), or the moving tag of the pin (tag). None when that cannot be told: no image read (no unit file), or a pin that is a wheel (the
    diffusion lanes). A build tag guessed by its digest's first hex digits held 13 of them for Nemotron, one more
    character in its pins away from failing, and said nothing of its b12x (code review of v1.45.0)."""
    e = ENGINES[unit]
    if "@sha256:" not in e["pin"] or not images:
        return None
    digest = e["pin"].split("@sha256:", 1)[1]
    # or its moving tag, which install.sh itself offers when the digest cannot be pulled (IMAGE=lmsysorg/sglang:v0.5.22)
    return any(digest in image or image in (e.get("unit_image"), e.get("tag")) for image in images)
