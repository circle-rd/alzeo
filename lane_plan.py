#!/usr/bin/env python3
"""One plan for loading a lane, wherever the load is started from.

The page used to build this plan in the browser and send each step to the cockpit,
one job after the other. A sign-out, a closed tab or a lost session then stopped the
chain between two steps, and the box could stay without a serving engine. The plan
now lives here, the cockpit runs it as one job, and the page and the CLI both ask the
cockpit for it: one mechanism, owned by the server.

Nothing here starts a process or reads the box. ``plan`` takes a snapshot of the
state the cockpit already collects, and returns the steps in the order they run.
Each step names an action of the cockpit's closed registry (dashboard/cockpit.py,
ACTIONS) and its validated parameters, so the executor and the CLI never build a
command from free text.
"""

from __future__ import annotations

import lane_registry

# The units of the lanes that serve alone, by the constants the page uses (base.js).
U27 = "qwen38-sglang.service"
UFLASH = "qwen38-flash.service"
IMAGE_UNIT = "qwen38-image.service"
IMAGE_H3_UNIT = "qwen38-image-h3.service"
VIDEO_UNIT = "qwen38-video.service"
DIFFUSION_UNITS = (IMAGE_UNIT, IMAGE_H3_UNIT, VIDEO_UNIT)
KOLIBRI_UNIT = "qwen38-kolibri.service"
NEMOTRON_UNIT = "qwen38-nemotron.service"
NEMOTRON_SUPER_UNIT = "qwen38-nemotron-super.service"
FLASH_LOCAL_UNIT = "qwen38-flash-local.service"
ORNITH_UNIT = "qwen38-ornith-122b.service"
FLASH_UNSLOTH_UNIT = "qwen38-flash-unsloth.service"

LANE_UNITS = (U27, UFLASH, FLASH_LOCAL_UNIT, FLASH_UNSLOTH_UNIT, ORNITH_UNIT,
              KOLIBRI_UNIT, NEMOTRON_UNIT, NEMOTRON_SUPER_UNIT, IMAGE_UNIT,
              IMAGE_H3_UNIT, VIDEO_UNIT)

# The lane whose runtime a first Load builds, by the unit (base.js, SIDE_LANE).
SIDE_LANE = {IMAGE_UNIT: "image", VIDEO_UNIT: "video", KOLIBRI_UNIT: "kolibri",
             NEMOTRON_UNIT: "nemotron", NEMOTRON_SUPER_UNIT: "nemotron-super",
             FLASH_LOCAL_UNIT: "flash-local", ORNITH_UNIT: "ornith-122b",
             FLASH_UNSLOTH_UNIT: "flash-unsloth", IMAGE_H3_UNIT: "image-h3",
             UFLASH: "flash", U27: "27b"}

# Each lane's versions, in the page's order (base.js, LANE_TARGETS): the first one is what
# a lane whose unit names none loads without a switch.
LANE_TARGETS = {
    U27: ("stock", "uncensored", "fp8", "uncensored-fp8", "arete"),
    UFLASH: ("flash", "flash-uncensored", "flash-nvda"),
    FLASH_LOCAL_UNIT: ("flash-local",),
    KOLIBRI_UNIT: ("kolibri", "kolibri-nvfp4"),
    NEMOTRON_UNIT: ("nemotron",),
    NEMOTRON_SUPER_UNIT: ("nemotron-super",),
    ORNITH_UNIT: ("ornith-122b",),
    FLASH_UNSLOTH_UNIT: ("flash-unsloth-iq3", "flash-unsloth"),
    IMAGE_UNIT: ("image", "image-turbo"),
    IMAGE_H3_UNIT: ("image-h3",),
    VIDEO_UNIT: ("video",),
}

# The lanes whose clients follow the window their unit starts with. Their Load sets
# the clients with switch-model.sh --clients before a restart, and their switch does
# it when the boot is pointed at them.
WINDOW_CLIENTS = frozenset((FLASH_LOCAL_UNIT, FLASH_UNSLOTH_UNIT))

# A switch target's lane, as switch-model.sh and the cockpit name it.
TARGET_UNIT = {
    "stock": U27, "uncensored": U27, "fp8": U27, "uncensored-fp8": U27, "arete": U27,
    "flash": UFLASH, "flash-uncensored": UFLASH, "flash-nvda": UFLASH,
    "image": IMAGE_UNIT, "image-turbo": IMAGE_UNIT, "image-h3": IMAGE_H3_UNIT,
    "video": VIDEO_UNIT,
    "kolibri": KOLIBRI_UNIT, "kolibri-nvfp4": KOLIBRI_UNIT,
    "nemotron": NEMOTRON_UNIT, "nemotron-super": NEMOTRON_SUPER_UNIT,
    "flash-local": FLASH_LOCAL_UNIT, "ornith-122b": ORNITH_UNIT,
    "flash-unsloth": FLASH_UNSLOTH_UNIT, "flash-unsloth-iq3": FLASH_UNSLOTH_UNIT,
}

# The official names of the versions, as base.js's TARGET_NAME gives them: the plan's
# steps are read by people, and the page and the CLI show the same words.
TARGET_NAME = {
    "stock": "Qwen3.8 27B NVFP4 · RadixArk",
    "uncensored": "Qwen3.8 27B abliterated NVFP4 · edp1096",
    "fp8": "Qwen3.8 27B FP8 · Qwen",
    "uncensored-fp8": "Qwen3.8 27B abliterated FP8 · edp1096",
    "arete": "Arete 27B NVFP4 · atakans",
    "flash": "Qwen3.8 Flash-Next NVFP4 · RadixArk",
    "flash-uncensored": "Qwen3.8 Flash-Next abliterated NVFP4 · dealignai",
    "flash-nvda": "Qwen3.8 Flash-Next NVFP4 · NVIDIA",
    "flash-local": "Qwen3.8 Flash-Next FP4-Local · NVIDIA",
    "image": "Qwen-Image 2.1 · Qwen",
    "image-turbo": "Qwen-Image 2.1 Turbo · Qwen",
    "video": "MiniMax-H3 · MiniMax",
    "kolibri": "Kolibri-1 FP8 · Aleph Alpha",
    "kolibri-nvfp4": "Kolibri-1 NVFP4 · Quantinine",
    "nemotron": "Nemotron 3.5 Bolt 24GB · NVIDIA",
    "nemotron-super": "Nemotron 3.5 Super VL NVFP4 · NVIDIA",
    "ornith-122b": "Ornith 1.5 122B NVFP4 · Ornith AI",
    "flash-unsloth": "Qwen3.8 Flash-Next UD-Q4_K_XL GGUF · Unsloth",
    "flash-unsloth-iq3": "Qwen3.8 Flash-Next UD-IQ3_XXS GGUF · Unsloth",
    "image-h3": "MiniMax-H3-Image · MiniMax",
}

# The lane key lane_registry.py gives each unit, for its official name.
LANE_KEY = {meta["unit"]: lane for lane, meta in lane_registry.LANES.items()}


def lane_title(unit: str) -> str:
    """A lane's official name, or its unit when the registry does not name it."""
    key = LANE_KEY.get(unit)
    return (lane_registry.LANES[key]["title"] if key else None) or unit.replace(".service", "")


def target_title(target: str) -> str:
    """A switch target's official name, or the target's own id."""
    return TARGET_NAME.get(target, target)


def lane_target(unit: str, state: dict) -> str | None:
    """The target the lane is set to or serves: what it serves now when that is known,
    else the target its unit names for the next start (base.js, laneTarget)."""
    engine = (state.get("engines") or {}).get(unit) or {}
    return engine.get("served_target") or engine.get("target")


def _engine(state: dict, unit: str) -> dict:
    return (state.get("engines") or {}).get(unit) or {}


def _step(name: str, params: dict, title: str, desc: str) -> dict:
    return {"name": name, "params": params, "title": title, "desc": desc}


def _clients_step(unit: str, target: str) -> dict:
    return _step("clients", {"target": target}, f"Set the clients for {lane_title(unit)}'s window",
                 "Sets the limits of opencode, pi and claude-local for the window its unit starts with; "
                 "the boot and opencode's default model stay where they are, and the lane does not restart.")


def _restart_step(unit: str, title: str) -> dict:
    return _step("unit", {"verb": "restart", "unit": unit}, title,
                 "It answers again in its usual time. "
                 + ("A generation in flight is lost." if unit in (IMAGE_UNIT, IMAGE_H3_UNIT, VIDEO_UNIT) else ""))


def plan(target: str, state: dict) -> list[dict]:
    """The steps that load ``target`` now, in order.

    ``state`` is the snapshot the cockpit already has:
      engines        {unit: {state, target, served_target}}  (collect_lifecycle)
      units          {unit: {enabled, ...}}                  (collect_units)
      serving        the unit that is not down, or None
      lanes_ready    {lane: bool|None}                       (collect_lanes_ready)
      lanes_startable {lane: bool|None}                      (collect_lanes_startable)
      update_owed    {unit: reason}                          (stale_engines)
    """
    unit = TARGET_UNIT.get(target)
    if unit is None:
        raise ValueError(f"unknown target: {target}")
    steps: list[dict] = []
    lane = SIDE_LANE.get(unit)

    # a lane installed on demand builds its runtime before its first switch fetches
    # the checkpoint (prepare-lane.sh)
    if lane and (state.get("lanes_ready") or {}).get(lane) is False:
        steps.append(_step("prepare", {"target": target},
                           f"Build the runtime of {lane_title(unit)}",
                           "Its first Load builds the lane's runtime, with no sudo. "
                           "The next step fetches its checkpoint."))

    serving = state.get("serving")
    served_now = _engine(state, unit).get("served_target")

    # The Load of the lane that serves this very target, with an update owed to it,
    # is the restart that applies the update and nothing else: the steps below would
    # point the boot at it or switch it back, which no update asked for.
    if serving == unit and lane_target(unit, state) == target and (state.get("update_owed") or {}).get(unit):
        if unit in WINDOW_CLIENTS:
            steps.append(_clients_step(unit, target))
        steps.append(_restart_step(unit, f"Restart {lane_title(unit)} on what the update installed"))
        return steps

    enabled = state.get("boot")
    current = _engine(state, unit).get("target")
    whole = (state.get("lanes_startable") or {}).get(lane) is not False
    targets = LANE_TARGETS.get(unit, ())
    switch_at = len(steps)
    if (enabled != unit or (current and current != target)
            or (not current and len(targets) > 1 and target != targets[0]) or not whole):
        if whole or enabled != unit:
            title = f"Point the boot at {target_title(target)}"
            desc = "Verifies the checkpoint and makes this lane the one enabled at boot. Nothing restarts."
        else:
            title = f"Bring back what {lane_title(unit)} lacks"
            desc = "Fetches what its checkpoint lacks and makes this lane the one enabled at boot. Nothing restarts."
        steps.append(_step("switch", {"target": target}, title, desc))
    pointed = len(steps) > switch_at

    if serving and serving != unit:
        steps.append(_step("unit", {"verb": "stop", "unit": serving}, f"Stop {lane_title(serving)}",
                           "A generation in flight is lost." if serving in (IMAGE_UNIT, IMAGE_H3_UNIT, VIDEO_UNIT)
                           else "Clients on :30001 see the engine unavailable until the next one answers."))

    # The engine serves what it loaded when it started; a switch since then changed
    # only what the next start loads, and the restart is still owed.
    owed = bool(served_now and served_now != target)
    is_image = unit in (IMAGE_UNIT, IMAGE_H3_UNIT)
    switched = owed if (is_image and served_now) else bool(steps) or owed
    updated = bool((state.get("update_owed") or {}).get(unit))
    if serving == unit and (switched or updated):
        steps.append(_restart_step(unit, f"Restart {lane_title(unit)} on the new checkpoint"
                                  if switched else f"Restart {lane_title(unit)} on what the update installed"))
    elif not serving or serving != unit:
        steps.append(_step("unit", {"verb": "start", "unit": unit}, f"Start {lane_title(unit)}",
                           "It loads and answers in its usual time."))

    # a boot lane whose clients follow its window, started or restarted: they are set
    # first, where its switch would be
    if unit in WINDOW_CLIENTS and not pointed and steps and steps[-1].get("params", {}).get("unit") == unit:
        steps.insert(switch_at, _clients_step(unit, target))
    return steps