# Kolibri-1 on a DGX Spark (German and English)

Aleph Alpha's Kolibri-1, the official FP8 release (128x128 block scales), served through Aleph
Alpha's own vLLM plugin with a native context of 262,144 tokens. The model built for German, and
good at English too.

## One command

```bash
./switch-model.sh kolibri        # or: kolibri-nvfp4
```

The one-line install sets up its units and its first Load builds it (102 GB: 79 of checkpoint, 23
of image); `./install.sh --with-kolibri` builds it at install and proves it, in about 10 min.

## Measured on our box (ASUS Ascent GX10, 128 GB)

- **48.0 / 121.9 / 165.8 / 202.0 tok/s** decoding at 1 / 4 / 8 / 16 streams.
- One stream through the repo's proxy decodes at **47.9 to 48.2 tok/s**, the same as outside it.
- The plugin's pin was A/B-ed against plain vLLM: token log-probabilities identical to the bit at
  153,450 positions and the same speed, so the lane keeps the plugin and nothing else changed.
- `kolibri-nvfp4` (`Quantinine/Kolibri-1-NVFP4A16-FP8`, its routed experts in NVFP4) decodes at
  **52.9 / 157.7 / 248.2 / 377.2 tok/s** with 42.8 GiB of weights against 73.6, at the level of the
  official FP8 in English and German, about 4% of perplexity weaker in French.

## What it is good at

German-first teams on a Spark: writing, summarising and agents in German at full speed, and quick
English too. Its 262K window fits long documents on one box.

## In the repo

- [docs/kolibri-lane.md](../kolibri-lane.md), the lane page with both checkpoints, the plugin pin and
  the A/B
- [BENCHMARKS.md](../../BENCHMARKS.md), every number with its method
