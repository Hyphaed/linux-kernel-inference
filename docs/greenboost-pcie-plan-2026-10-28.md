# GreenBoost on the RTX 5070: what to measure, what to change, in what order

Written 2026-10-28 from four read-only studies (all in `docs/research/`):
`greenboost-architecture-map`, `pcie-hostmem-research`, `pcie-gen5-platform`,
`greenboost-sister-projects`. Nothing in GreenBoost has been changed. Every
change below waits for the measurement that justifies it, which is the
evidence rule this project already runs on.

## What the card is actually doing today

On this machine `greenboost.ko` isn't on the weights path. The CUDA shim sends
overflow allocations to plain anonymous host memory, registers them with
`cuMemHostRegister`, and hands llama.cpp a device pointer. The GPU's cores then
read those bytes over PCIe on every token. There are no tier moves, no copy
engine traffic, no prefetch. Live numbers: `T2 allocated: 0 MB` in the module,
4,454 MB spilled by the shim, `path_a_count=0`.

If the spilled third of the model holds the experts a token touches, roughly
0.5 GB crosses the link per token. At 24 GB/s that's a ceiling near 43 tok/s,
and the live median is 30 to 50. That arithmetic is inferred, not a counter
reading, but it says tok/s on this model is probably bound by bytes per token
over PCIe. So the levers are: fewer bytes per token, faster bytes, or not
sending the bytes at all.

## What's settled and what isn't

* **Gen5 is off the table.** The board's x16 slot is Gen4 by design (manual and
  the BIOS form data both agree: `PCIE1 Link Speed` offers Auto and Gen1 to
  Gen4, nothing higher). The link tops out near 25 GB/s usable. Even Gen5 would
  have bought at most 1.3x to 1.5x, because dual-channel DDR4 delivers about
  45 to 50 GB/s.
* **The project holds three contradictory bandwidth numbers** for the same
  path: 24 GB/s (July), 11 to 12 GB/s (August), 18.6 to 22 GB/s (October). The
  experiment that settles it was written in August and never run.
* **Tok/s on the same model fell from a median of about 50 (4 Oct) to about 30
  (27 Oct).** Nobody has investigated. Context length, MTP acceptance (0.22 to
  0.71) and shim state all moved, so it may not be a regression at all.
* **MoE decode with experts on the CPU isn't PCIe-bound.** Only hidden states
  cross the link; the limit is the CPU and DDR4. GreenBoost's current approach
  does the opposite and drags expert weights over PCIe. The project's own
  sweep saw `--n-cpu-moe 28` at 49.7 tok/s and 24 at 14.1, mechanism unknown.
  The global 5070 rule allows `--n-cpu-moe`, so this is a legitimate A/B, not a
  rule conflict.

## Measured since (read-only, 2026-10-28, root run at 11:23)

* **DIMMs.** Two 32 GB Crucial BL32G36C16U4B (DDR4-3600 CL16), one per channel,
  configured at 3600 MT/s, so XMP is on. Dual-channel peak is about 57.6 GB/s. The
  PCIe link (about 25 GB/s usable) is the narrower pipe, and the platform study's
  3200 assumption was wrong.
* **PCIe registers.** MaxPayload is 256 bytes at both ends, and the CPU port's own
  capability tops out at 256, so there's nothing left to raise. The GPU's
  MaxReadReq is 512 bytes; the port's 128 applies to requests the port itself
  issues, not to GPU reads. A larger GPU read size is the only register left to
  try, and the expected gain is small. H7 drops to "measure last".
* **Idle link state.** Both ends read Gen2 (5 GT/s) at idle and the GPU reports
  "downgraded". Under load it reaches Gen4. Still unmeasured: how fast it climbs
  when a decode starts.
* **Huge pages.** Since boot, `thp_fault_fallback` is 252,130 against
  `thp_fault_alloc` 233,126, about 52% fallbacks (49% an hour earlier). Still
  system-wide, so it supports H2 without proving the T2 ranges suffer.
* **C-states.** C6 and package states are never used (all 0.00 in turbostat).
  That's the earlier BIOS review's deliberate choice: package C-state support
  disabled for DMA and interrupt latency on the GPU path. Package power was 59.7
  to 60.9 W at about 14% busy while the art job ran, right under the 65 W
  long-term limit. Busy P-core threads ran at 4.2 to 4.65 GHz and E-cores at 3.7
  to 3.9 GHz, both below their maximum turbo. That fits a power ceiling but
  doesn't prove one: `perf_tune.py ab rapl_pl1` decides it.

None of this changes the order of the plan. It strengthens H2 and moves H7 down.

## Phase 0: measure (no code, GPU idle, about 20 minutes)

Run only after the art job has released the card. Each answers a question the
studies couldn't.

| Question | How | Why it decides something |
|---|---|---|
| What does a pinned copy really do on this link? | `python3 diagnostics/gpu_baseline.py --label idle` | The 8.4 GB/s seen earlier was a 64 MiB smoke test sharing the card. Real expectation is 24 to 26 |
| Which of the three bandwidth numbers is true, and does page size matter? | build and run GreenBoost's `tests/bench/gb_pathbench.cu` (4K / 2M / 1G rows) | H2 and H9 both hinge on it |
| What are MaxReadReq, MaxPayload and the idle link state? | `sudo lspci -vv -s 00:01.0` and `-s 01:00.0`, plus `gb_pcie_tune.py report` | needs root; read-only |
| What is the DIMM speed, and is XMP on? | `sudo dmidecode -t memory \| grep -E "Speed\|Locator"` | the real aggregate ceiling |
| What page size backs the live spilled weights? | `grep AnonHugePages /proc/<llama-server pid>/smaps_rollup` while it serves | the shim never checks |
| How many bytes cross PCIe per generated token? | `nvidia-smi dmon -s t -d 1` during a fixed 512-token decode | turns the 43 tok/s arithmetic into a number |
| Did tok/s really drop, and why? | compare dataflux days at equal context and MTP settings | may be a measurement artefact |

`sudo python3 diagnostics/optimize_loop.py` collects the link, memory and
baseline facts in one go (see its `pcie` step).

## Phase 1: configuration only, reversible

1. **`--n-cpu-moe` sweep against the current all-GPU-plus-spill setup** on the
   real model, five runs each, same prompt and context. If the CPU path wins or
   ties, bytes per token over PCIe stops being the problem.
2. **`--moe-cache-mib`**, the llama.cpp expert cache merged 2026-10-07. Check
   first that the GreenBoost llama.cpp build is new enough to have it. Measured
   gains were 1.57x on a 4090, 2.2x on a 5090, 1.29x on a 16 GB 5060 Ti, and it
   only helps batches of 32 tokens or fewer. A 12 GB card is more likely near
   the last figure, so expect modest and prove it.
3. **Speculative-decode depth** swept against PCIe bytes per token, not just
   acceptance rate (`--spec-draft-n-max` in 0, 2, 3, 4, 6).

## Phase 2: small code changes, each only if Phase 0 supports it

* **Ask for 2 MB pages explicitly in the shim** (`greenboost_cuda_shim.c:1608`):
  aligned `mmap`, `MADV_HUGEPAGE`, pre-touch, then read `smaps` and report the
  huge-page fraction in the `shim_decision` event. With `defrag=defer+madvise`
  an unmadvised mapping silently falls back to 4K when memory is fragmented.
  Go ahead only if the 4K and 2M rows differ beyond noise.
* **Pin the host side of PyTorch tier moves** (`gb_model_tier.py:400,426`): the
  copies are labelled non-blocking but come from pageable memory. Helps FLUX and
  diffusion only, not llama-server.
* **`gb_pcie_tune.py --apply`** to raise MaxReadReq, only if the report shows it
  low. Root, not persisted, undone by a reboot.
* **Verify mode for every skip-copy path.** From the vGPU project: any
  optimisation that skips a copy (for example not copying back an unmodified
  weight on eviction) gets a counter that re-reads and diffs a sample. Its
  README reports 23,451 skipped copies with 0 mismatches, a skip count rather
  than a bandwidth number, but the discipline is worth copying.

## Phase 3: only if Phases 0 and 1 show T2-resident bytes dominate

* **Placement by access pattern:** keep every-token tensors (attention, norms,
  shared experts) in VRAM and push only routed experts out. First probe whether
  llama.cpp runs ops on host-buffer weights on the GPU or silently on the CPU.
* **Heat-based promotion of expert chunks.** Prefer the upstream expert cache
  over reviving GreenBoost's dead LFRU code (`gb_expert_tier.h` has no caller
  outside its tests). High risk: it's a hot path in the CUDA interposer, and the
  project's own `LayerPrefetcher` sweep lost to a static placement.
* **Copy-engine staging ring for dense spill:** build only if `staged_read`
  beats `zerocopy_read` by more than 1.3x in Phase 0.

## Deliberately not doing

* Gen5, by BIOS or board patching. Not available; see above.
* Managed memory or HMM as a VRAM extension: faults and migrates on a PCIe card.
* Bulk CPU writes through the 16 GB BAR: write-combined, buys latency and not
  capacity.
* GPUDirect Storage (root is on LVM, and the module's version is a stub), nvCOMP
  for already-quantised weights, KTransformers (no AVX-512 or AMX on this CPU).
* RTX Kit. Almost all of it is graphics-only; the one idea worth a look is
  RTXMU-style suballocation of small pinned blocks, and we'd write our own.
* Removing the shim's per-allocation `cudaDeviceSynchronize`: it only costs load
  time, and its comment says SM access fails without it.

## Open questions nobody could answer read-only

Which allocation path the front-load split takes (if it runs at all), the live
page size of the spilled ranges, MaxReadReq and idle link state, whether
llama.cpp runs host-buffer ops on the GPU, why tok/s fell, and the real bytes
per token. Phase 0 answers all of them.
