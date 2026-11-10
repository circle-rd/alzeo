#!/usr/bin/env python3
"""The lanes of this box, one entry each, with what every script, unit and page writes about them by hand.

Adding a lane touched some 76 files (Ornith 1.5 122B, in the six commits of its own, counted on 2026-11-10),
and the lists written by hand drifted apart: the offline suite's guard never learnt the video and speech lanes'
ports, and install.sh named three lanes by short names beside the official ones its lane_title gives.
This is the one place that says what each lane is. Nothing reads it at run time yet (step 1 of the lane
factory, 2026-11-10): tests/test_lane_registry.py holds the lists and tables written by hand to it, so a
lane added, renamed or moved in one of them and not in the others fails there, with the file and the line.

Each value is read where the repo declares it, named beside it. A field is here because that test reads
it. Standard library only, and nothing happens at import.
"""

# The port every text lane's engine listens on, one at a time, behind the proxy: install.sh's PORT and
# vllm-lane.sh's vlane_engine_env, both 30000 by default; each text lane's unit says --port __PORT__.
TEXT_PORT = 30000

# By the names install.sh gives the lanes (its --with-<lane> flags, lane_title, boxmem.LANES), in
# boxmem.LANES' order.
#   unit       the systemd unit its installer writes (UNIT= in install-<lane>.sh; install.sh's
#              UNIT_NAME for the two lanes it installs itself)
#   installer  the script that installs it
#   flags      whether install.sh installs it, --with-<lane> and --no-<lane> (the speech lane is
#              installed by its own script only: "./install.sh does not run this", install-speech.sh)
#   kind       what it serves: text, image, video or speech
#   engine     sglang (lmsysorg/sglang images), vllm (the installers that source vllm-lane.sh),
#              diffusion (SGLang Diffusion's wheel in the lane's own venv), own (this repo's
#              speech-server.py, in front of llama.cpp's llama-server and Kokoro)
#   b12x       whether its vLLM engine runs NVIDIA's b12x kernels, as its installer says
#   targets    the switch targets of the lane (switch-model.sh, boxmem.TARGETS), in boxmem.TARGETS' order
#   heavy_64   those of its targets a 64 GB DGX Spark does not take although the lane fits it
#   served     its engine's --served-model-name (a text lane's), what clients ask for
#   provider   its opencode provider (a text lane's)
#   ports      where its engine listens: TEXT_PORT for a text lane, else its own ports
#   fits_64    whether a 64 GB DGX Spark holds it (boxmem.FITS_64)
#   title      its official name (decision 1180 of Hasan, 2026-11-07), as base.js's LANE_NAME gives it
LANES = {
    # install.sh: IMAGE lmsysorg/sglang, UNIT_NAME qwen38-sglang.service, MODEL_CHOICE's 27B targets
    "27b": {"unit": "qwen38-sglang.service", "installer": "install.sh", "flags": True, "kind": "text",
            "engine": "sglang", "b12x": False,
            "targets": ("stock", "uncensored", "fp8", "uncensored-fp8"), "heavy_64": ("fp8", "uncensored-fp8"),
            "served": "qwen3.8-27b", "provider": "qwen38", "ports": (TEXT_PORT,), "fits_64": True,
            "title": "Qwen3.8 27B"},
    # install.sh: FLASH_IMAGE lmsysorg/sglang, the launcher qwen38-flash-launch.sh.template serves it
    "flash": {"unit": "qwen38-flash.service", "installer": "install.sh", "flags": True, "kind": "text",
              "engine": "sglang", "b12x": False,
              "targets": ("flash", "flash-nvda", "flash-uncensored"), "heavy_64": (),
              "served": "qwen3.8-flash-next", "provider": "flashnext", "ports": (TEXT_PORT,), "fits_64": False,
              "title": "Qwen3.8 Flash-Next"},
    # install-image.sh: WHEEL sglang[diffusion], PORT 30020 (IMAGE_PORT)
    "image": {"unit": "qwen38-image.service", "installer": "install-image.sh", "flags": True, "kind": "image",
              "engine": "diffusion", "b12x": False,
              "targets": ("image", "image-turbo"), "heavy_64": (),
              "served": None, "provider": None, "ports": (30020,), "fits_64": False,
              "title": "Qwen-Image 2.1"},
    # install-video.sh: WHEEL sglang[diffusion], PORT 30022 (VIDEO_PORT)
    "video": {"unit": "qwen38-video.service", "installer": "install-video.sh", "flags": True, "kind": "video",
              "engine": "diffusion", "b12x": False,
              "targets": ("video",), "heavy_64": (),
              "served": None, "provider": None, "ports": (30022,), "fits_64": False,
              "title": "MiniMax-H3"},
    # install-kolibri.sh: vLLM 0.29 with Aleph Alpha's plugin, no b12x; VLANE_SERVED kolibri-1
    "kolibri": {"unit": "qwen38-kolibri.service", "installer": "install-kolibri.sh", "flags": True, "kind": "text",
                "engine": "vllm", "b12x": False,
                "targets": ("kolibri", "kolibri-nvfp4"), "heavy_64": (),
                "served": "kolibri-1", "provider": "kolibri", "ports": (TEXT_PORT,), "fits_64": False,
                "title": "Kolibri-1"},
    # install-nemotron.sh: vLLM nightly with b12x; VLANE_SERVED nemotron-3.5-bolt; no 64 GB guard, Bolt fits
    "nemotron": {"unit": "qwen38-nemotron.service", "installer": "install-nemotron.sh", "flags": True,
                 "kind": "text", "engine": "vllm", "b12x": True,
                 "targets": ("nemotron",), "heavy_64": (),
                 "served": "nemotron-3.5-bolt", "provider": "nemotron", "ports": (TEXT_PORT,), "fits_64": True,
                 "title": "Nemotron 3.5 Bolt"},
    # install-nemotron-super.sh: the Bolt lane's image; VLANE_SERVED nemotron-3.5-super-vl
    "nemotron-super": {"unit": "qwen38-nemotron-super.service", "installer": "install-nemotron-super.sh",
                       "flags": True, "kind": "text", "engine": "vllm", "b12x": True,
                       "targets": ("nemotron-super",), "heavy_64": (),
                       "served": "nemotron-3.5-super-vl", "provider": "nemotron-super", "ports": (TEXT_PORT,),
                       "fits_64": False, "title": "Nemotron 3.5 Super VL"},
    # install-flash-local.sh: the community b12x vLLM image; VLANE_SERVED qwen3.8-flash-next-fp4
    "flash-local": {"unit": "qwen38-flash-local.service", "installer": "install-flash-local.sh", "flags": True,
                    "kind": "text", "engine": "vllm", "b12x": True,
                    "targets": ("flash-local",), "heavy_64": (),
                    "served": "qwen3.8-flash-next-fp4", "provider": "flash-local", "ports": (TEXT_PORT,),
                    "fits_64": False, "title": "Qwen3.8 Flash-Next FP4-Local"},
    # install-ornith-122b.sh: the Flash FP4-Local lane's image; VLANE_SERVED ornith-1.5-122b
    "ornith-122b": {"unit": "qwen38-ornith-122b.service", "installer": "install-ornith-122b.sh", "flags": True,
                    "kind": "text", "engine": "vllm", "b12x": True,
                    "targets": ("ornith-122b",), "heavy_64": (),
                    "served": "ornith-1.5-122b", "provider": "ornith-122b", "ports": (TEXT_PORT,),
                    "fits_64": False, "title": "Ornith 1.5 122B"},
    # install-speech.sh: on the CPU beside any lane, PORT 30024 (SPEECH_PORT) and its recognizer on the
    # next one (ASR_PORT); no switch target, no official name of its own (it is "the speech lane")
    "speech": {"unit": "qwen38-speech.service", "installer": "install-speech.sh", "flags": False, "kind": "speech",
               "engine": "own", "b12x": False,
               "targets": (), "heavy_64": (),
               "served": None, "provider": None, "ports": (30024, 30025), "fits_64": True,
               "title": None},
}


def lanes(**fields):
    """The lanes whose fields hold the values given, in LANES' order: lanes(kind="text"), lanes(engine="vllm")."""
    return tuple(lane for lane, f in LANES.items() if all(f[k] == v for k, v in fields.items()))


def targets():
    """{target: lane} of every switch target, in LANES' order."""
    return {t: lane for lane, f in LANES.items() for t in f["targets"]}
