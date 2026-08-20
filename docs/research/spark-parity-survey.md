# Reaching DGX-Spark-class local inference on ncore — research survey

Written 2026-08-10. Companion to the kernel-7.1.8 / PCIe-substrate work
tracked in `patches/`, `configs/fragments/33-pcie-dma-substrate.config`, and
`patches/custom/0020-dma-buf-compressed-descriptor.patch`. This document is
the "why" behind those changes: the arithmetic that sets the target, and the
top 10 arXiv papers + top 10 GitHub projects evaluated against it.

## The arithmetic that sets the target

DGX Spark (GB10) vs. this box (RTX 5070 + DDR4 + NVMe over PCIe Gen4):

| | DGX Spark (GB10) | ncore |
|---|---|---|
| Capacity | 128 GB unified | 12 GB VRAM + ~66 GB DDR4 + NVMe |
| Fast-tier bandwidth | 273 GB/s LPDDR5X (shared CPU+GPU) | **672 GB/s** VRAM (192-bit GDDR7 @ 14001 MHz) |
| Host→accelerator path | NVLink-C2C, no PCIe hop | PCIe Gen4 x16, ≈25 GB/s real |
| Llama 3.1 70B FP8 decode | 2.7 tok/s (LMSYS measured) | ~0.4–0.6 tok/s estimated — **loses** |
| GPT-OSS 20B MXFP4 decode | 49.7 tok/s measured | fits VRAM entirely — should beat comfortably |

**Spark is not fast, it is big.** Its 273 GB/s is 2.5× *slower* than this
box's VRAM. The gap to close is capacity, not raw bandwidth.

**The actual target model** is this workstation's default,
`DavidAU/Qwen3.6-27B-Fable-Fusion-711-…-MTP-GGUF`. It is architecturally
important to this survey because it rules out entire categories of technique:

- **Dense**, not MoE — every parameter activates every token, so
  expert-residency tiering does not apply to it (it does apply to the
  secondary target, `Qwen3.6-35B-A3B`, also cached locally).
- **Native MTP heads** — multi-token prediction ships built in, published
  1.4–2.2× decode speedup.
- **Hybrid attention**, 3:1 Gated DeltaNet (linear) : Gated Attention, and
  only 4 KV heads on the quadratic layers — so its **KV cache is unusually
  small**. Unlike most parity-with-Spark discussions, KV compression is not
  where the win is for this model; weights are the whole problem.

At Q4_K_M (~16 GB) on a 12 GB card, **the entire problem is a 4 GB gap.**
Closing it moves decode from an estimated ~5.6 tok/s (spilling to DDR over
PCIe) to ~35–40 tok/s (fully resident, VRAM-bandwidth-bound) — a step
change, landing roughly 2× ahead of Spark's own ~17 tok/s estimate at the
same quantization on the same model.

**Honest limit:** dense 70B-class models lose to Spark and will keep losing.
58 GB/token over a 25 GB/s link is ≈2.3 s/token even before compression; a
realistic 1.3–1.4× lossless ratio cannot close a ~11× bandwidth deficit. This
is reported, not hidden, in the verification plan.

---

## Top 10 arXiv papers

Ranked by contribution to the parity target, each with an integrate/cite
verdict.

### 1. [2605.30728](https://arxiv.org/abs/2605.30728) — Reducing the GPU Memory Bottleneck with Lossless Compression for ML (Invariant Bit Packing)
Kamath, Krishnamurthy, Canini, Peter (29 May 2026). The only paper in this
list that targets **PCIe transfer time** directly rather than storage size:
identifies and eliminates invariant bits across tensor groups, decompresses
with warp-parallel GPU kernels overlapped with async PCIe transfers. 74%
faster GNN training, 180% faster DLRM embedding lookup, 24% faster LLM
inference. **Verdict: primary blueprint** for the T2/T3→VRAM codec.

### 2. [2606.15789](https://arxiv.org/abs/2606.15789) — Approaching Shannon Bound with Lossless LLM Weight Compression
Tan, Chen, Alonso, Wong, He (ISCA 2026). Tile-level Asymmetric Numeral
Systems decoding aligned to GEMM tiling; establishes that **LLM weight
effective entropy is 2–10× lower than stored bitwidth**. Qwen-14B batch
capacity 47→75 (1.2× throughput); Mixtral-176B 20→95 (4.8× capacity, 1.6×
throughput) in SGLang. **Verdict: integrate as the compression ceiling
reference**; ANS as a stretch codec beyond the initial DFloat11-style pass.

### 3. [2603.17435](https://arxiv.org/abs/2603.17435) — ZipServ: Fast and Memory-Efficient LLM Inference with Hardware-Aware Lossless Compression
ASPLOS 2026. Tensor-Core-Aware Triple Bitmap Encoding + a fused
decompression-GEMM kernel (`ZipGEMM`) that decompresses weights directly
into tensor-core registers — weights stay **compressed in VRAM** the entire
time, decompressed only per-tile at multiply time. Up to 30% size reduction,
2.21× kernel-level speedup over cuBLAS, 1.22× end-to-end over vLLM.
**Verdict: integrate the compressed-resident-VRAM idea**; the custom fused
GEMM kernel itself is cited, not reproduced, as out of scope for this pass.

### 4. [2504.11651](https://arxiv.org/abs/2504.11651) — DFloat11: Lossless LLM Compression for Efficient GPU Inference via Dynamic-Length Float
NeurIPS 2025. Huffman coding of BF16 exponent bits; weights decompressed
just before matmul, discarded immediately after. ~30% size reduction,
bit-for-bit identical outputs, working repo with a CPU-offload mode.
**Verdict: first codec to implement** — lowest engineering risk, proven,
bit-exact by construction.

### 5. [2605.01708](https://arxiv.org/abs/2605.01708) — SplitZip: Ultra Fast Lossless KV Compression for Disaggregated LLM Serving
613.3 GB/s compression / 2181.8 GB/s decompression on real BF16 activation
tensors — outperforms prior lossless compressors on the latency-critical
codec path. **Verdict: deprioritized for the primary target** — Qwen3.6-27B's
hybrid attention design (3:1 linear:quadratic, only 4 KV heads) makes its KV
cache unusually small already; this matters more for the MoE secondary
target or a future long-context dense model.

### 6. [2604.12300](https://arxiv.org/abs/2604.12300) — TierBPF: Page Migration Admission Control for Tiered Memory via eBPF
Moves the tier-migration *admission* decision into eBPF, reacting at
page-fault timescale instead of a userspace poll loop. **Verdict:
integrate** — directly maps onto GreenBoost's T1/T2/T3 admission logic,
grounded in this box's already-compiled `BPF_JIT=y`/`BPF_LSM=y`.

### 7. [2404.13886](https://arxiv.org/abs/2404.13886) — Taming Server Memory TCO with Multiple Software-Defined Compressed Tiers
Upstream zswap supports only one active compressed tier; this proposes
multiple simultaneously-active compressed tiers with different
speed/ratio tradeoffs. **Verdict: integrate** — motivates
`CONFIG_ZRAM_MULTI_COMP` (newly enabled in
`configs/fragments/33-pcie-dma-substrate.config`) and a lz4-primary /
zstd-secondary scheme.

### 8. [2604.17172](https://arxiv.org/abs/2604.17172) — UCCL-Zip: Lossless Compression Supercharged GPU Communication
Lossless compression purpose-built for GPU-to-GPU/network communication
paths. **Verdict: integrate on the feeder fabric** —
`greenboost_netd.c` already negotiates plain zstd (`GB_NET_FEAT_ZSTD`); this
is a strictly better codec for that same negotiated-feature slot.

### 9. [2604.27844](https://arxiv.org/abs/2604.27844) — ZipCCL: Efficient Lossless Data Compression of Communication Collectives for Accelerating LLM Training
Same lane as UCCL-Zip, focused on collective operations rather than
point-to-point. **Verdict: cite** — relevant if GreenBoost's cluster fabric
grows collective-style operations; not applicable to the current
point-to-point feeder protocol.

### 10. [2603.26131](https://arxiv.org/abs/2603.26131) — IBEX: Internal Bandwidth-Efficient Compression Architecture for Scalable CXL Memory Expansion
Compression for CXL-attached memory expansion. **Verdict: cite only — no
CXL on consumer Raptor Lake.** This box's `numa_nodes: 1`, single-socket,
no CXL root ports; the technique has no applicable hardware here.

**Also reviewed, not ranked:** [2606.12556](https://arxiv.org/abs/2606.12556)
(ITME — CXL-hybrid inference memory expansion, same CXL gate as #10);
[2304.07342](https://arxiv.org/abs/2304.07342) (GPULZ — LZSS on GPU, superseded
by nvCOMP's GDeflate for this use case); [2508.19263](https://arxiv.org/abs/2508.19263)
(lossless compression of weights/checkpoints/low-precision KV, a broader
survey covering similar ground to #2 and #4); Cloudflare's *Unweight* (2026,
lossless MLP weight compression) — same family as DFloat11/ZipNN, evaluated
as a #4 alternative if DFloat11's licensing or repo maintenance becomes a
blocker.

---

## Top 10 GitHub projects

### 1. [NVIDIA/nvcomp](https://github.com/NVIDIA/nvcomp)
GPU-accelerated LZ4/Snappy/Deflate/GDeflate/ANS. **Not installed on this
box** — confirmed via `find / -name "libnvcomp*"` returning nothing during
this session's audit. **Verdict: integrate as the primary codec runtime**;
installing it is the prerequisite for the empirical codec gate (survey
section above, item 2.1 of the implementation plan).

### 2. [LeanModels/DFloat11](https://github.com/LeanModels/DFloat11)
Reference implementation of arXiv #4 above. Bit-exact, working repo, CPU
offload mode already merged. **Verdict: integrate** — codec of choice and
correctness oracle (byte-diff target) for any other codec evaluated.

### 3. [zipnn/zipnn](https://github.com/zipnn/zipnn)
C core + Python bindings, safetensors/HuggingFace hooks, ~80 GB/s
decompression measured with 16 workers across NUMA nodes (not applicable
here — single NUMA node — but single-threaded/single-node numbers are still
relevant). **Verdict: integrate** for the host-side (pre-VRAM) compression
pass, lowest integration friction given existing HF ecosystem hooks.

### 4. [NVIDIA/gds-nvidia-fs](https://github.com/NVIDIA/gds-nvidia-fs)
The `nvidia-fs.ko` kernel module. **Confirmed missing on this box** —
`gds-tools-13-3` and `libcufile.so.1.18.1` are installed via apt, but
`lsmod` shows no `nvidia_fs` and `/dev/nvidia-fs*` does not exist, meaning
GPUDirect Storage silently runs in POSIX compat mode (every NVMe→GPU byte
bounces through host RAM, crossing PCIe twice). **Verdict: integrate** —
build against 7.1.8 headers via DKMS; highest-confidence, lowest-risk win
in this entire survey since it's fixing something already broken rather
than adding something new.

### 5. [rapidsai/kvikio](https://github.com/rapidsai/kvikio)
Python bindings to cuFile/GDS. **Verdict: integrate** as the measurement
harness for GDS mode verification (`gdscheck -p` DMA vs. compat mode) and
for scripting the PCIe-under-load benchmarks.

### 6. [sched-ext/scx](https://github.com/sched-ext/scx)
`scx_lavd`, `scx_bpfland`, `scx_rusty` userspace-pluggable schedulers.
`CONFIG_SCHED_CLASS_EXT=y` is already compiled into this box's running
kernel but **nothing is loaded** — confirmed via config inspection this
session. **Verdict: integrate** — evaluate `scx_lavd`/`scx_bpfland`, then
tune a policy pinning the CUDA-submit thread to P-cores and
codec/decompress workers to E-cores. Matters more if the empirical codec
gate shows SM-based decompression costing real cycles (see the Blackwell
DE caveat below).

### 7. [damonitor/damo](https://github.com/damonitor/damo)
Userspace CLI for DAMON. This box already compiles in `CONFIG_DAMON=y`,
`CONFIG_DAMON_SYSFS=y`, `CONFIG_DAMON_STAT=y` but has **zero runtime
policy armed** — a gap already flagged in this repo's own project notes prior
to this session. **Verdict: integrate** — arm a `paddr` kdamond with a
quota-gated `pageout`/`migrate_cold` scheme targeting cold weight pages,
feeding directly into the "compress cold pages in place instead of
evicting" strategy this session's kernel patch (`0020`) exists to support.

### 8. [axboe/liburing](https://github.com/axboe/liburing)
io_uring userspace library, including NVMe `uring_cmd` passthrough.
`CONFIG_IO_URING=y` and `CONFIG_IO_URING_ZCRX=y` already ship on this box.
**Verdict: integrate** — model-shard reads via `/dev/ngXnY` + registered
buffers, bypassing the block layer; complements GDS (item 4) which handles
the GPU-destined read path while this handles host-destined reads.

### 9. [lz4/lz4](https://github.com/lz4/lz4)
Reference LZ4. **Verdict: integrate** as the low-latency alternative codec
— relevant regardless of the Blackwell Decompression Engine question below,
since LZ4 is cheap on SMs too if the DE turns out unavailable.

### 10. [facebook/zstd](https://github.com/facebook/zstd)
Already in-kernel (`CONFIG_CRYPTO_ZSTD=y`) and already used in
`greenboost_netd.c`'s `GB_NET_FEAT_ZSTD` negotiation. **Verdict: baseline to
beat**, not a new integration — every new codec evaluated in this survey is
scored against zstd's existing measured ratio/speed on this exact fabric.

---

## Hardware caveat that must not be assumed away

The Blackwell **Decompression Engine** — a fixed-function hardware block
decompressing LZ4/Snappy/Deflate at up to 600 GB/s with zero SM
occupancy cost — exists on **datacenter Blackwell**. The consumer RTX 50
architecture whitepaper does not document an equivalent block, and
independent DirectStorage benchmarking attributes the RTX 50 series' flat
decompression scaling (5090 down to 5060 showing no relative degradation)
to the improved AMP scheduler rather than a dedicated decompression engine.

**This survey does not assume either way.** The implementation plan's first
step (before building anything on top of a codec) is an empirical
benchmark: run nvCOMP's batched LZ4/GDeflate decompression on this exact
GB205 part and record both achieved GB/s and SM occupancy during the run.
If decompression measurably steals SM cycles from the GEMMs, that directly
motivates the scheduler work (GitHub #6, `sched-ext/scx`) to keep
decompression workers off the cores contending with CUDA dispatch. If it
turns out free (unlikely but not ruled out without measuring), the
scheduler work still helps the wider pipeline but loses its most urgent
justification.

## Where compression does *not* pay, stated plainly

Lossless compression and quantization compete for the same underlying
redundancy in the data. DFloat11-class codecs achieve ~30% on BF16/FP16
weights by exploiting the low entropy of the exponent field — but that
redundancy is largely already squeezed out once a model is quantized to
Q4_K_M or MXFP4. On this workstation's actual target
(Qwen3.6-27B-MTP at Q4_K_M), realistic compression contributes roughly
0.8 GB of the 4 GB gap, not the majority of it — the residency and
VRAM-overhead-reclaim levers matter more here, and MTP's per-token traffic
division matters most of all. This survey exists to set expectations
honestly rather than lead with the most exciting-sounding number.
