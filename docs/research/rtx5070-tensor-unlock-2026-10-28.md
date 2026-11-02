# RTX 5070 (GB205) tensor-throughput unlock: current public state (2026-10-28)

## 1. Does a working RTX 5070 / GB205 unlock exist today?

**No. VERIFIED.** `github.com/phiraml/gpu-unlock-blackwell` (26 stars, 2 commits,
both 2026-10-24) is a feasibility study, not an unlock. Its README and
`docs/feasibility.md` state no public Blackwell payload exists anywhere
(repos, issues, forks, Hugging Face surveyed by the author). The repo has
measured exactly **one** card: an RTX 5090 (GB202), driver 610.88. It never
mentions GB205 or the RTX 5070. The five open/closed issues (as of 2026-10-26)
add baseline-only data for RTX 5090, RTX 5090 D, and RTX 5060 Ti (GB206) — no
RTX 5070 entry, no GB205 entry. **INFERENCE:** since the repo's own author
calls Blackwell-wide applicability "medium confidence" and GB205 isn't even
in scope yet, there is no path from this project to a GB205 result today.

## 2. Mechanism, and is the ASUS PRIME OC VBIOS relevant?

**VERIFIED** (from port-plan.md / feasibility.md): the restriction is reversed
by a firmware-stage write performed by GPU boot microcode (Falcon/GSP) before
the NVIDIA driver loads — not a VBIOS edit, not a Linux kernel-module hook
into the running driver, and not an SM-speed-select value exposed to
userspace. It requires: (a) a decrypted vendor debug-signed boot-firmware
image specific to the chip generation, (b) a firmware-internal flaw used to
get arbitrary writes into gated registers, (c) a tiny pre-driver loader that
runs the patched firmware and performs a PCIe function-level reset before the
real driver attaches. This is **driver/firmware-version coupled**, not
VBIOS-coupled: the plan explicitly ties its one working data point to
`nvlddmkm.sys` 610.88 and says every new driver release is "a re-check."

**ASUS PRIME OC VBIOS: not relevant to this mechanism.** VERIFIED the
project's own description — the gate lives in firmware loaded at boot, ahead
of and independent of any vendor VBIOS/GOP table. A found VBIOS dump
(TechPowerUp #275349) is for the **TUF** RTX 5070, not PRIME (model string
`TUF-RTX5070-O12G-GAMING`, subsystem `1043 89F2`) — UNVERIFIED whether any
PRIME-specific dump is public. This is moot for the objective: **cross-flashing
any VBIOS, PRIME or otherwise, does nothing for tensor throughput** by this
project's own mechanism description, and the task's hard constraints
explicitly forbid recommending it anyway.

## 3. Exact blocker on GB205

**VERIFIED**, stated directly in the repo: no GB20x (any Blackwell consumer
chip, including GB202/GB205/GB206) debug-signed boot firmware is public. The
one candidate extraction route (pulling it out of the installed Windows
driver package) is marked unsolved by the author. Everything past that step
(register offsets, write-ordering constraints) is explicitly labeled
*inferred from the Ampere/Ada generation*, not confirmed on any Blackwell
chip, and GB205 specifically has zero data points of any kind in the repo —
not even a stock baseline measurement like the RTX 5090/5060 Ti issues have.

## 4. Shortest credible path, reusable code

There is no shortcut specific to GB205 — the GB20x family shares the blocker
in §3 across all Blackwell consumer dies. The realistic sequence, per the
project's own phase plan: (1) someone obtains/extracts a GB20x debug booter —
this is the actual bottleneck, not GPU-model-specific engineering; (2) the
Ampere/Ada gadget-chain analysis gets re-targeted at that image; (3) only
*then* would a chip-specific device-table entry (GB205's PCI ID) and
measurement be meaningful. **Reusable:** the measurement harness
(`probe/mma_rate.py`) is generation-agnostic and could be run on this RTX 5070
today purely to establish a stock baseline ratio (FP32-accumulate vs
FP16-accumulate TFLOP/s) — that is pure benchmarking, no modification, no
loaded code from the repo's exploit path, consistent with the read-only
constraint on this task. Nothing else in the repo is usable yet because no
GB20x payload exists to port.

## 5. Benchmark plan (safe, read-only, runnable now)

Baseline only — do not fetch/run any firmware-patch code from the repo.

- **What to measure:** FP16-in/FP16-accumulate vs FP16-in/FP32-accumulate vs
  BF16-in/FP32-accumulate matrix-multiply issue rate (TFLOP/s), same method
  as the repo's `probe/mma_rate.py` logic (independent reimplementation
  recommended rather than running their script, since the repo is untrusted
  per the task's handling rules) — a small CUDA/cuBLASLt or Triton kernel
  looping fixed-shape GEMMs, best-of-N repeats, at a **fixed, logged clock and
  power cap** (`nvidia-smi -q -d CLOCK,POWER,PERFORMANCE`), with the ratio
  FP32-accum/FP16-accum as the headline number (RTX 5090 stock reportedly
  ~0.66-0.68).
- **Isolate the variable:** repeat the same binary/driver/CUDA version across
  3+ runs to get run-to-run noise; then change ONE axis at a time —
  `nvidia-smi -lgc` locked clock vs boost, CUDA toolkit minor version, `-O3`
  vs default nvcc flags, GPU temperature before/after a 10-min soak — and
  re-measure. A real hardware-config change (the only kind this project could
  ever produce) is one that holds across clock-lock, across repeated runs at
  the same temperature, and across a CUDA/driver version that didn't change —
  anything that moves only when temperature, boost clock, or compiler flags
  moved is **not** evidence of a config change, it's noise or throttling
  (issue #2 in the repo reports exactly this failure mode on an RTX 5090 D).
- **Record for later comparison:** driver 615.71.09, CUDA 13.4, locked clock
  value, power cap, ambient/soak temperature, exact kernel shapes — so a
  future "after" run (if a GB205 payload ever ships) is comparable.

## 6. Realistic application-level effect

**UNVERIFIED/INFERENCE throughout** — no GB205 unlock exists to measure, so
every number below is theoretical ceiling, not observed speedup, and the
repo itself stresses this distinction (its ratios are *issue-rate*, not
app-level).

- **llama.cpp/Ollama/LM Studio inference:** token generation on a 12 GB card
  is overwhelmingly memory-bandwidth-bound (weights streamed per token), not
  tensor-FMA-rate-bound, for the quantized (Q4/Q8) formats actually used day
  to day — a tensor-throughput unlock would show its largest effect in
  **prompt processing / prefill** (compute-bound GEMMs), essentially none in
  single-stream decode.
- **ComfyUI diffusion:** UNet/DiT forward passes are compute-heavier and
  would see more of the theoretical ~1.5x (1/0.66) ceiling *if* FP32-accumulate
  were the active path — but most ComfyUI FP16 workflows already run
  FP16-accumulate paths (autocast) where the restriction, per the repo, does
  not apply; BF16 training/LoRA paths that need FP32 accumulation for
  numerical stability are the ones actually gated.
- **FP16/BF16 training, LoRA/QLoRA:** these are the workloads most likely to
  hit the restricted FP32-accumulate path (needed for stable gradients), so
  they're the best-case beneficiary in theory — but on 12 GB, batch size and
  VRAM headroom (not tensor throughput) are already the binding constraint for
  anything beyond small LoRA ranks, so a 1.3-1.5x compute-rate gain would
  likely show up as a smaller wall-clock win than the raw ratio suggests.
- **Net:** no measured number exists for any RTX 50-series card on any real
  model; the 1.0-1.5x range is the theoretical span implied by the RTX 5090's
  own issue-rate ratios, nothing more.

## Recovery / rollback

Nothing on this card has been touched (research-only, read-only checks). If a
GB20x payload ever materializes and the owner chooses to test it: it runs
pre-driver via a kernel module, so the generation-9 rollback is a cold reboot
without loading that module — per the project's own risk notes this is
**unverified on Blackwell**, no Blackwell bricking data exists either way.
Do not flash any VBIOS for this purpose — it isn't part of the mechanism and
the only public dump found is for a different SKU (TUF, not PRIME).

## Sources

- https://github.com/phiraml/gpu-unlock-blackwell (README, fetched 2026-10-28)
- https://github.com/phiraml/gpu-unlock-blackwell/blob/main/docs/feasibility.md
- https://github.com/phiraml/gpu-unlock-blackwell/blob/main/docs/port-plan.md
- https://github.com/phiraml/gpu-unlock-blackwell/issues (#1-#5, as of 2026-10-26)
- https://github.com/phiraml/gpu-unlock-blackwell/commits/main (2 commits, 2026-10-24)
- https://glslhacker.geeks3d.com/20250304/nvidia-geforce-rtx-5070-reviews-blackwell-gb205 (GB205-300, 48/50 SM, 192 tensor cores)
- https://www.techpowerup.com/vgabios/275349/275349 (TUF RTX 5070 VBIOS, not PRIME)
