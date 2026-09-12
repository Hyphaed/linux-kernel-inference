# Lossless PCIe compression verdict — measured, 2026-09-03

Owner asked for kernel-level lossless PCIe bus compression to speed up local AI
inference and raise game FPS. Measured against real files on this box before
writing any code, because the premise is cheap to test and expensive to build
around if wrong.

## What was measured

Not a synthetic buffer — real tensor bytes and real game data, sampled with
`dd` from the middle of each file (skipping headers/metadata) and piped
through `lz4` and `zstd` at several levels.

```bash
dd if=<file> bs=1M skip=<N> count=<M> of=sample.bin
lz4 -1 -c sample.bin | wc -c
zstd -3 -T8 -c sample.bin | wc -c
```

| Data | File | Sample | lz4 -1 | zstd -3 | zstd -12 |
|---|---|---|---|---|---|
| **IQ4_XS — the model GreenBoost actually serves** | `…Cold-Fusion-GAIN-V1.1-NM-DAU-NEO-MAX-NEO-MTP-IQ4_XS.gguf` (15.85 GiB) | 512 MiB @ offset 6000 MiB | **0.01%** | **0.18%** | 0.16% |
| Q8_0 mmproj (nothing on this box serves this) | `mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf` | 256 MiB @ offset 200 MiB | 0.00% | 13.05% | — |
| UE `.pak` (disk→RAM at load, not the per-frame PCIe path) | FF7 Rebirth `pakchunk0-WindowsNoEditor.pak` | 90 MiB @ offset 500 MiB | 13.53% | 59.37% | — |

Sanity check on the pak sample: Shannon entropy 7.703 bits/byte, 4.3% zero
bytes — consistent with mixed compressed/uncompressed UE5 asset chunks, not an
artifact of a bad sample.

## Why IQ4_XS is flat and Q8_0 isn't

IQ4_XS is a 4-bit importance-weighted quantization — every byte is already
near-maximum entropy by construction; there is no redundancy left for a
general-purpose lossless codec to find. Q8_0 is 8-bit with block scales, which
leaves enough structure for zstd to find real redundancy (13%). **The model
actually served on this box is the IQ4_XS one.** Confirmed the served path from
GreenBoost's own model directory (`/var/lib/greenboost/synapse/models/`), not
from `/opt/models/` — this repo has been burned twice before by reading the
metadata of a retired model instead of the one actually running (see
`CLAUDE.md`'s "Local-inference prior art" section, third correction).

## What this is worth in tok/s

`docs/research/10x-tok-s-kernel-path-2026-08-17.md` measured this box's real
decode budget: 263 ms/token, of which 248 ms is PCIe Gen4 transfer of the
6.2 GB non-resident weight set. A perfect, zero-CPU-cost in-kernel
decompressor delivering IQ4_XS's real 0.18% reduction saves:

```
248 ms * 0.0018 = 0.45 ms/token
263 / (263 - 0.45) = 1.0017x  →  ~0.17% faster
```

That is the *entire* upside, before subtracting the CPU cost of running the
decompressor at all, which on this workload would very likely make the change
net-negative rather than net-positive. There is no version of this that clears
this repo's MUST-RULE bar ("a patch does not leave this machine until there is
evidence, in hand, that it makes something measurably better").

## Games: same conclusion, different mechanism

The 59% number is real, but it answers the wrong question. A `.pak` is
compressed *on disk*; Unreal Engine already decompresses it once, at
load/streaming time, into GPU-resident texture and mesh data. The bytes that
actually cross the PCIe bus during gameplay are the decoded, GPU-native
formats (BC-compressed texture blocks, vertex/index buffers) — not the
on-disk archive. Kernel-level PCIe compression would either:

- compress data that's already at its target entropy (BC-block textures are
  themselves a lossy compressed format — recompressing them loses more
  quality or gains nothing lossless), or
- require re-plumbing the engine's asset-streaming path to hand the kernel
  raw archive bytes instead of decoded GPU resources, which is not a kernel
  change at all.

Neither delivers an FPS win. Load-time (level transitions, texture pop-in)
might improve marginally from a smarter streaming path, but that is a
different problem than "raise FPS" and belongs to the engine/compositor, not
the PCIe bus.

## Cross-reference: this closes two open threads

- **`docs/tasks_patches.md` T4** — "Give 0020 something to describe · will not
  do" — already killed the same idea for a different reason (no importer that
  can decompress a dma-buf on this stack; T3 remains never-fired
  `t1_priority`). This measurement is the second, independent line of
  evidence: even with an importer, there would be nothing worth compressing.
- **`upstream-candidates/SUBMISSION.md` / T5** — graded 0020 "plausible, land
  the GreenBoost consumer first." Downgraded below, in both files, to reflect
  that the consumer can never be worth building against this workload's real
  numbers.

## Decision

Dropped. The real lever for bytes-per-token on this hardware is speculative
decoding, which amortises one PCIe weight transfer across many accepted
tokens instead of trying to shrink the transfer itself — see
`docs/research/10x-found-speculative-offload-2026-08-17.md` (SubSpec 9.1x avg
up to 13.76x, lossless, no separate draft model; SpecExec 10.6x on weaker PCIe
Gen3). That is GreenBoost/llama.cpp userspace work, out of scope for this
repo's kernel patches.
