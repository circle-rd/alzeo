# An external disk for the models

A DGX Spark has one internal NVMe drive, and the lanes of this repository want a lot of it: about
930 GB for every lane at once (the installer's `--all-weights`). Many boxes keep their models on an
external SSD instead. This page says what can live there, what must not, how to set it up, and what
the installer does about it. Every number on it was measured on the reference box, an ASUS Ascent
GX10 whose Hugging Face cache sits on an Intenso Portable SSD of 2 TB on USB 3.2 Gen 2x2 (about
1.8 GB/s sequential), against its internal NVMe (5.4 to 6.0 GB/s), in October 2026.

## What a disk is used for: once at boot, or at every token

Most lanes read their weights once, when they start, into the GPU's memory, and after that the disk
is not used. Those weights can live on an external disk: only the start is slower.

| Lane | What it reads, and when | From the external SSD |
|---|---|---|
| 27B, flash, Kolibri, both Nemotron lanes, Ornith | the weights, once, at the start | the start is slower (the flash lane's, read in many small pieces, about 80 s more), the serving is the same |
| Images (Qwen-Image 2.1) | the weights, once, at the start | 75 s to start instead of about 70, the same image bit for bit, in the same time |
| Videos (MiniMax-H3) | its weights **at every step of every generation** (SGLang offloads its layers by itself) | **1.78 times slower** to generate: 29.4 s a step against 14.55 |
| Qwen3.8 Flash-Next, its PLE table | a 47.7 GiB file **at every token**, written beside the home directory at every boot (`PLE_DIR`, `~/flashnext-ple`) | not on the external disk by default: `PLE_DIR` is under the home directory |
| Qwen3.8 Flash-Next FP4-Local, its n-gram table | seven of its checkpoint's files **at every token**, rows at random | **five times slower** with eight requests (15.6 tok/s in all, against 77.7 from the NVMe), its first token twice as late: the lane copies them for you (below) |

So: what is read at every token or every step belongs on the internal disk, and what is read once
at the start can go anywhere.

## How to put the cache on an external disk

1. Format the disk as **ext4** (exFAT and NTFS keep neither the permissions nor the symbolic links
   the Hugging Face cache is made of), and mount it with `nofail`, so that a box that boots without
   it still boots:

   ```
   UUID=<the disk's UUID>  /mnt/ssd  ext4  defaults,noatime,nofail,x-systemd.device-timeout=30s  0 2
   ```

2. Either point the whole cache there with a link, which every tool follows (the lanes, the
   cockpit, `huggingface-cli`):

   ```
   rsync -aH ~/.cache/huggingface/ /mnt/ssd/huggingface/   # then check, then
   mv ~/.cache/huggingface ~/.cache/huggingface.old && ln -s /mnt/ssd/huggingface ~/.cache/huggingface
   ```

   or give the installer another folder, `HF_CACHE=/mnt/ssd/huggingface ./install.sh`. The installer
   keeps that choice at every update, but tools outside this repository still write to
   `~/.cache/huggingface`.

3. Keep on the internal disk what is read while it serves. The two PLE tables already are, by
   default. For the video lane, keep its checkpoint's folder on the internal disk and link it from the
   external cache (`/mnt/ssd/huggingface/hub/models--MiniMaxAI--MiniMax-H3` → a folder on the NVMe):
   the video lane runs in a virtual environment of its own and follows the link. The lanes that run in
   Docker do not follow a link that leaves the folder they mount: keep their checkpoints inside it.

4. Optionally, make every lane wait for the disk at boot, with a drop-in of your own per unit (the
   installer never touches these folders, and `./uninstall.sh` removes only the 27B unit's: remove
   the others yourself when the disk goes):

   ```
   # /etc/systemd/system/qwen38-sglang.service.d/ssd.conf, and the same for the other lanes' units
   [Unit]
   RequiresMountsFor=/mnt/ssd
   ```

A box that boots without the disk finds a link to nothing: its lanes fail at once, with the reason in
their journal (they serve offline, `HF_HUB_OFFLINE=1`), and nothing is downloaded again onto the
internal disk.

## What the installer does about it

- It measures the free space on the disk each part really lands on, following the link: the
  checkpoints on the cache's disk, the images on Docker's, the PLE tables on the home directory's.
  When two of them share a disk, it asks for their sum.
- The Qwen3.8 Flash-Next FP4-Local lane reads its table where its cache is when the cache is on the disk of the
  home directory, which is a new box's default. When the cache is on another disk, its first Load
  (or `./switch-model.sh flash-local`, or its installer) copies the seven files that hold the table,
  32 GB, into `~/flash-local-ple`, checks each copy against its sha256, and mounts them over the
  cache's own. It checks that they fit on that folder's disk before it downloads anything, and
  refuses with the reason when they do not. `FLASH_LOCAL_PLE_DIR=<folder>` puts the copies elsewhere (a folder outside the
  Hugging Face cache, its path of letters, digits and . _ / + @ - only, as for `HF_CACHE`), `FLASH_LOCAL_PLE_DIR=none` reads the
  table where the cache is: given to `./install.sh`, `./install-flash-local.sh` or
  `./switch-model.sh flash-local`, the choice holds for every run after, a Load from the cockpit
  included, and `FLASH_LOCAL_PLE_DIR=` brings the default back. The copies go when the lane is
  removed (`./install-flash-local.sh --uninstall`), and only they: the lane lists them in the folder
  (`.qwen38-ple-copies`), so a folder of your own keeps your files.

## A USB SSD's own traps

- **A disk stuck slow.** After hours of heavy reads (lane starts one after another, the disk at 74 °C
  at its hottest), the reference box's SSD stayed at about 450 MB/s instead of 1.8 GB/s, every file
  alike, with nothing in the kernel's log. A flash start then took 24.5 minutes. Measure it with
  `sudo dd if=<a large file on it> of=/dev/null bs=16M iflag=direct status=progress`. If it reads
  near 450 MB/s, stop the lane that mounts the cache, `sudo umount /mnt/ssd`, unplug the disk for
  30 seconds and plug it back: it read 1.8 GB/s again.
- **Its SMART data** go through the USB bridge: `sudo smartctl -a -d sat /dev/sdX`.
- **A kernel line** such as `xhci-hcd ...: Transfer event 26 for unknown stream ring slot 1 ep 4`
  at plug time and during reads was harmless on the reference box: no reset, no read error.
