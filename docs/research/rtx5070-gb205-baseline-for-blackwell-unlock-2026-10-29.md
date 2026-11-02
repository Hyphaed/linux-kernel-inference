# RTX 5070 (GB205) tensor baseline, and a software route to the same goal

A note for whoever is working on the Blackwell consumer tensor-throughput
unlock (the `gpu-unlock-blackwell` feasibility study). It has two parts: a
GB205 baseline you don't have yet, measured independently, and a description of
GreenBoost, which gets more real-world throughput out of a stock Blackwell
card today while the firmware route stays blocked.

Nothing from the unlock repo was fetched, built or run to produce this. These
are stock-card measurements from an independent harness, so they're a clean
reference point, not a result of any modification.

## Why you might want this

Your repo's own data stops at GB202: one RTX 5090 on driver 610.88, plus
baseline-only issues for the 5090 D and a 5060 Ti (GB206). There's no GB205
entry anywhere in it, and your own notes rate Blackwell-wide applicability as
"medium confidence" precisely because the family hasn't been measured across
dies. This is that missing die, measured the same way your `mma_rate` probe
measures: the FP32-accumulate vs FP16-accumulate issue-rate ratio, at logged
clocks and power, repeated so the number isn't noise.

## Hardware and method

- **Card:** NVIDIA GeForce RTX 5070, GB205 (48 of 50 SMs, 192 tensor cores),
  12 GB.
- **Stack:** driver 615.71.09, CUDA 13.4, built for `sm_120`.
- **What ran:** a cuBLASLt FP16/BF16 MMA microbenchmark (fixed-shape GEMMs,
  best-of-N) for the tensor issue rates, plus `torch` matmul at FP16/BF16/
  TF32/FP32/FP8, a device-bandwidth and pinned-copy test, and a 120 s soak.
  Clocks, power, temperature and PCIe link were logged for the whole run so a
  real number can be told apart from throttling.
- **Not changed:** no clock lock, no power-limit edit, no firmware, no VBIOS.
  This is the stock "before" number.

## Results

| Measurement | Value |
|---|---|
| FP16-in / **FP16-accumulate** MMA | **128.2 TFLOP/s** |
| FP16-in / **FP32-accumulate** MMA | **66.5 TFLOP/s** |
| **FP32-accum / FP16-accum ratio** | **0.52** |
| BF16-in / FP32-accumulate MMA | 66.6 TFLOP/s |
| torch matmul FP16 / BF16 | 63.9 / 64.3 TFLOP/s |
| torch matmul TF32 / FP32 | 32.3 / 23.3 TFLOP/s |
| torch matmul **FP8 (e4m3)** | **238.2 TFLOP/s** |
| Device memory bandwidth | 544 GB/s (81% of the 672 GB/s spec) |
| PCIe pinned copy H2D / D2H | 24.1 / 28.3 GB/s, Gen4 x16 |
| Under load | SM 2737 MHz median, 295 W max, 78 C max, **no throttle** |
| 120 s soak | SM 2752 to 2730 MHz, 62 to 68 C |

The headline for your work is the **0.52** ratio. On GB205 the FP32-accumulate
path runs at roughly half the FP16-accumulate rate. That's a tighter gate than
the ~0.66 to 0.68 you reported on the 5090 (GB202). If the restriction is the
same mechanism family-wide, GB205 is throttled harder than GB202, not less, so
a working unlock would have more to give here in principle. Read it as one more
data point for the "is this Blackwell-wide" question, measured rather than
inferred.

The clocks held flat across the soak and the card never flagged a throttle bit,
so the ratio is a real architectural number, not a thermal or power artifact.
If a locked-clock run, a different GEMM shape, or the same sweep on a second
driver build would help you compare against the 5090, we can run those and send
them over. Reproduce ours with a fixed-shape cuBLASLt GEMM loop at a logged,
locked clock.

## Where this leaves the unlock, from our read of your repo

Your own `feasibility.md` and `port-plan.md` are clear that the blocker isn't
GPU-model-specific engineering. It's that no decrypted, debug-signed GB20x boot
firmware is public, the one candidate extraction route (pulling it from the
Windows driver package) is marked unsolved, and everything past that step is
inferred from Ampere/Ada rather than confirmed on any Blackwell die. A GB205
measurement doesn't move that blocker. It just means that if the booter ever
surfaces, there's a stock GB205 ratio here to compare an "after" run against.

So this baseline is offered as data, not as a claim that the unlock is close.

## A different route to "more out of a stock Blackwell card": GreenBoost

The unlock targets one number: FP32-accumulate tensor throughput. That number
mostly matters for FP16/BF16 **training** with FP32 accumulation, where the
gated path is the one you actually hit. For **inference**, which is what most
people run on a 12 GB consumer card, the restriction is largely moot: inference
uses FP16-accumulate or FP8, and FP8 on this card already sits at 238 TFLOP/s,
nearly four times the gated FP32-accumulate rate. The card isn't short on
tensor throughput for inference. It's short on VRAM and bandwidth.

GreenBoost attacks those instead, and it ships today. It's a CUDA memory and
compute orchestrator for NVIDIA GPUs, GPL-2.0, a Linux kernel module plus an
`LD_PRELOAD` CUDA shim plus a Python stack. What it offers right now (v3.5):

- **Tiering.** T1 VRAM, T2 system DDR and T3 NVMe are presented as one virtual
  memory pool through the shim and kernel module, so a model larger than VRAM
  runs by spilling across PCIe instead of failing to load. On this 12 GB 5070
  that's about 12 GB VRAM plus a 43 GB DDR T2 pool plus 74 GB of NVMe T3,
  roughly 126 GB combined. Spill goes through the shim, never as CPU-resident
  layers.
- **Quantization.** `gb_quant` does weight quantization with FP8 as the quality
  floor (also NVFP4/INT8/INT4), KV-cache compression, and MoE expert tier
  placement. The FP8 path is `torch._scaled_mm` (cuBLASLt) on `sm_120`, which
  measured about 9% faster and higher-fidelity than NVFP4 on this card, so FP8
  is the default floor rather than FP4.
- **Serving (gb-synapse).** An OpenAI/Ollama-compatible server built on a
  patched llama.cpp: tensor-split across a LAN cluster over RPC, multi-token
  prediction (speculative decode), MoE expert CPU placement, and first-class
  prefill batch/ubatch sizing. Measured on Qwen3.8-35B-A3B (a 256-expert MoE)
  on this one 12 GB card: **49.7 tok/s decode**, and raising the prefill
  micro-batch to 2048 took cold prefill from 1363 to **2470 tok/s, 1.81x**,
  with decode unchanged. The memory shim is on by default so a 13.7 GB model
  fits the 12 GB card by spilling the MoE experts to DDR.
- **Cluster.** Idle LAN machines (feeders) lend their GPU and RAM into one
  unified virtual GPU, so a model too big for one card runs split across the
  house.
- **Telemetry and governance.** A flight-recorder (`dataflux`) logs every
  subsystem's events, and a governed metrics layer answers questions like "is
  my boot tuning going to survive a reboot" or "can my pinned recipes actually
  serve" without guessing.
- **Card sharing.** A GPU-phase lease lets a vision model, an image generator,
  the browser and a video encoder take turns on one card, pausing the resident
  model rather than killing it.

The point for your audience is that these two efforts aren't competing. An
unlock, if the firmware ever ships, would help FP32-accumulate training.
GreenBoost helps inference on the stock card now, by fixing the VRAM and
bandwidth limits that actually bind a 12 GB Blackwell card, and it's measured,
legal and open rather than blocked on a missing signed image.

## Honest limitations

- One box, one driver/CUDA pair. The 0.52 ratio is solid for this GB205 sample
  at this clock, but it's a single card.
- FP8's 238 TFLOP/s is weight-only quantization through the torch/cuBLASLt
  path. It is not the FP32-accumulate restriction you're targeting, and it
  isn't reachable from a GGUF/llama.cpp model (that path dequantizes to FP16).
  So FP8 is a reason inference doesn't need the unlock, not a workaround for it.
- Single-stream decode is bandwidth-bound by the 544 GB/s ceiling. No tensor
  trick, unlocked or not, moves single-stream decode. Only prefill, which is
  compute-bound, benefits from more tensor throughput.
- The theoretical ceiling of a successful unlock is roughly the 1/0.52 span on
  the gated path, and only on workloads that actually use FP32 accumulation.
  Real wall-clock gains would be smaller, and on a 12 GB card VRAM is usually
  the binding constraint anyway.

## Links

- GreenBoost: <https://github.com/Hyphaed/greenboost> and
  <https://gitlab.com/IsolatedOctopi/greenboost> (GPL-2.0, v3.5).
- Happy to run more GB205 baselines (locked clock, other shapes, other driver
  builds) if a specific comparison against your 5090 data would help.
