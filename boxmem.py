#!/usr/bin/env python3
"""What this box is: its memory class, the lanes that fit it, and its maker's name.

Since 2026-10-23 the DGX Spark comes in two sizes. NVIDIA's product page gives "64 GB LPDDR5X* or
128 GB LPDDR5x, coherent unified system memory" on one 256-bit bus at 273 GB/s, the 64 GB
"available exclusively through participating OEM partners", and its blog of 2026-10-21 names what
the 64 GB runs: "Nemotron 3.5 Bolt 74B 24GB, Qwen3.8 27B and Gemma 4 26B-A4B". Linux sees less
than the label says: 121.6 GiB of MemTotal on the reference box's 128 GB (130.6 GB). The class
comes from MemTotal, in GiB as the kernel counts it. The maker's name comes from DMI
(/sys/class/dmi/id), which ASUS gives both sizes alike ("GX10"), so it never decides the class.

install.sh and the lane installers read it through lanes.sh (box_mem_class, lane_fits), and the
cockpit loads it the way it loads conformity.py. Standard library only.
"""
import argparse
import os
import sys
from pathlib import Path

GIB = 1024 ** 3
# MemTotal from which a box is of the 128 GB class: what install.sh asked of every box until the
# 64 GB one (110 GiB, below the 112.3 GiB a 128 GB box keeps on the 7.0 kernel without kho=off,
# forum t/383222).
MIN_128_GIB = 110
# MemTotal from which a box is of the 64 GB class; below it nothing here fits with room to spare.
MIN_64_GIB = 48
# The lanes of the 64 GB class, by the names install.sh gives them: the 27B (23.5 GB of weights
# and drafter on disk), Nemotron 3.5 Bolt (24.1 GiB in memory) and the speech lane, on the CPU.
# The text lanes left out weigh 45.8 GB (Kolibri) to 135.3 GB (the flash); the video lane peaks
# at 75 to 80 GiB (docs/video-lane.md); the image lane waits for its own measurement on a 64 GB
# budget.
FITS_64 = ("27b", "nemotron", "speech")
LANES = ("27b", "flash", "image", "video", "kolibri", "nemotron", "nemotron-super", "flash-local",
         "ornith-122b", "speech")
# Each target switch-model.sh and MODEL_CHOICE name, and its lane.
TARGETS = {"stock": "27b", "uncensored": "27b", "fp8": "27b", "uncensored-fp8": "27b",
           "flash": "flash", "flash-nvda": "flash", "flash-uncensored": "flash",
           "image": "image", "image-turbo": "image", "video": "video",
           "kolibri": "kolibri", "kolibri-nvfp4": "kolibri", "nemotron": "nemotron",
           "nemotron-super": "nemotron-super", "flash-local": "flash-local", "ornith-122b": "ornith-122b"}
# Targets of a lane that fits, which a 64 GB box still does not take: Qwen's FP8 weights of the 27B
# (30.9 GB against 21.9 GB for NVFP4) leave its KV pool too little, and no 64 GB run measured them.
HEAVY_64 = ("fp8", "uncensored-fp8")
# (sys_vendor, product_name) as a box's DMI gives them, each read on a real box and never
# guessed, and the name its maker sells it under. Any other box is named by its DMI as it is.
KNOWN = {
    # both GX10 the project is measured on (gx10-eff9 and gx10-f56c), read 2026-10-22
    ("ASUSTeK COMPUTER INC.", "GX10"): "ASUS Ascent GX10",
}


def mem_total(meminfo=None):
    """MemTotal in bytes, or None when the file has no such line."""
    path = meminfo or os.environ.get("QWEN38_MEMINFO") or "/proc/meminfo"
    for line in Path(path).read_text().splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1]) * 1024
    return None


def mem_class(meminfo=None):
    """"128", "64", or None for a box too small for any lane. QWEN38_MEMORY_CLASS forces it: the
    tests, and the 64 GB proofs on a 128 GB box with memory held aside."""
    forced = os.environ.get("QWEN38_MEMORY_CLASS")
    if forced:
        if forced not in ("128", "64"):
            raise ValueError(f"QWEN38_MEMORY_CLASS={forced}: 128 or 64")
        return forced
    total = mem_total(meminfo)
    if total is None:
        return None
    if total >= MIN_128_GIB * GIB:
        return "128"
    if total >= MIN_64_GIB * GIB:
        return "64"
    return None


def fits(lane, cls):
    """Whether a lane fits a box of that class."""
    if lane not in LANES:
        raise ValueError(f"{lane}: not a lane ({', '.join(LANES)})")
    return cls == "128" or (cls == "64" and lane in FITS_64)


def target_fits(target, cls):
    """Whether a target (a MODEL_CHOICE, a switch's argument) fits a box of that class."""
    if target not in TARGETS:
        raise ValueError(f"{target}: not a target ({', '.join(TARGETS)})")
    return fits(TARGETS[target], cls) and not (cls == "64" and target in HEAVY_64)


def machine(dmi=None):
    """{vendor, product, family, name}: what the box's DMI says, and the name to show for it."""
    d = Path(dmi or os.environ.get("QWEN38_DMI_DIR") or "/sys/class/dmi/id")

    def read(key):
        try:
            return (d / key).read_text(errors="replace").strip()
        except OSError:
            return ""
    vendor, product, family = read("sys_vendor"), read("product_name"), read("product_family")
    name = KNOWN.get((vendor, product)) or " ".join(x for x in (vendor, product) if x) or None
    return {"vendor": vendor, "product": product, "family": family, "name": name}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="boxmem.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("class", help="print the box's memory class: 128, 64, or none")
    sub.add_parser("gib", help="print MemTotal in GiB, one decimal")
    f = sub.add_parser("fits", help="exit 0 when the lane fits this box, 1 when it does not")
    f.add_argument("lane", choices=LANES)
    f.add_argument("--class", dest="cls", choices=("128", "64", "none"),
                   help="the class already read (install.sh's MEM_CLASS) instead of reading it again")
    t = sub.add_parser("fits-target", help="exit 0 when the target fits this box, 1 when it does not")
    t.add_argument("target", choices=tuple(TARGETS))
    t.add_argument("--class", dest="cls", choices=("128", "64", "none"),
                   help="the class already read (install.sh's MEM_CLASS) instead of reading it again")
    sub.add_parser("machine", help="print the box's name, as its DMI gives it")
    g = sub.add_parser("fitting", help="print the lanes that fit this box, in LANES' order")
    g.add_argument("--class", dest="cls", choices=("128", "64", "none"),
                   help="the class already read (install.sh's MEM_CLASS) instead of reading it again")
    a = ap.parse_args(argv)
    if a.cmd == "class":
        print(mem_class() or "none")
    elif a.cmd == "gib":
        total = mem_total()
        print(f"{total / GIB:.1f}" if total is not None else "unknown")
    elif a.cmd == "fits":
        cls = a.cls if a.cls else mem_class()
        return 0 if fits(a.lane, None if cls == "none" else cls) else 1
    elif a.cmd == "fits-target":
        cls = a.cls if a.cls else mem_class()
        return 0 if target_fits(a.target, None if cls == "none" else cls) else 1
    elif a.cmd == "machine":
        print(machine()["name"] or "unknown")
    elif a.cmd == "fitting":
        cls = a.cls if a.cls else mem_class()
        print(" ".join(lane for lane in LANES if fits(lane, None if cls == "none" else cls)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
