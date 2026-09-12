# Hardware-topology levers for gaming + local inference — 15 papers, 2026-09-10

Companion sweep to `docs/research/platform-kernel-levers-2026-08-31.md` and
`docs/research/pcie-compression-verdict-2026-09-03.md`. All 15 papers below
are new to this vault (checked against 333 already-indexed arXiv IDs before
fetch). Same triage criteria as the prior sweep: does the mechanism exist on
THIS topology (i9-14900KF, 8P+16E, one NUMA node, RTX 5070 12GB, PCIe Gen4
x16, single-GPU, dense `qwen35`-hybrid model with 4 KV heads), and does the
gain clear the 5.7% kernel-share bound from
`docs/research/10x-tok-s-kernel-path-2026-08-17.md`.

**Fetch note:** `hyperresearch fetch` on a raw `arxiv.org/pdf/<id>` URL fails
with `JUNK_CONTENT: Binary PDF garbage in content` for every paper in this
sweep — same regression already flagged in the 2026-08-31 doc. Worked around
by fetching the `/abs/` page through hyperresearch (metadata + note) and
pulling the PDF directly with `curl`, then converting every PDF with docling
before reading (`--image-export-mode referenced --enrich-picture-description`).
One paper (2406.18980) additionally hit a login-wall redirect on the abs page
through hyperresearch's crawler; `curl` fetched it fine, so the wall reads as
crawler-fingerprint rate-limiting rather than a real login requirement.

---

## Triage table

| arXiv | Title | Verdict | Reason |
|---|---|---|---|
| 2609.01338 | mzCache: on-device LLM memory under mobile multitasking | die (kernel) / corroborates existing finding | Mechanism needs OpenCL SVM sharing *physical* DRAM between CPU and GPU (true UMA). Our RTX 5070 is a discrete card over PCIe Gen4 — no such buffer. The measured fact that zRAM's zstd/lz4 compress KV cache by only 0.2–9.3% is the mobile-side echo of `pcie-compression-verdict-2026-09-03.md`'s 0.18% weight-compressibility finding — same conclusion, independent hardware, no new lever. |
| 2607.10183 | ATSInfer: tensor-granularity hybrid CPU-GPU scheduling | userspace lever | Real mechanism for our exact shape (weights exceed VRAM, PCIe Gen4 x16, single-batch local inference) — tensor-level static placement + load-aware dynamic transfer, exploiting the NVIDIA Copy Engine vs. Zero-Copy SM path split. Entirely a llama.cpp-fork feature (15k LOC extension); nothing here is kernel-reachable — the two transfer paths it schedules between are both userspace CUDA API calls. Worth relaying to `gb-synapse`/GreenBoost as an inference-engine idea, out of this repo's scope. |
| 2604.18529 | HybridGen: CPU-GPU hybrid attention over CXL-tiered KV | die | Needs a CXL/remote-memory tier for the "semantic-aware K/V split" to matter — single NUMA node here, no CXL. Its host-side allocator hook is the existing `mbind()` syscall, already available; nothing to patch. |
| 2608.22643 | NeuroPrefetcher: delta prefetching for storage-backed sparse LLMs | die | Core mechanism (82–85% next-token MLP-neuron persistence) is a property of *sparsely-activated* edge models. Our served model (`qwen35`, dense, `full_attention_interval=4`) fires every parameter every token — no activation sparsity to predict from. Same "dense not MoE" mismatch already recorded for the served model elsewhere in this repo. |
| 2609.05635 | DejaVu: eliminating CPU/GPU copy pairs on UMA SoCs | die | Targets Jetson-class unified-memory boards where host and device buffers are the same physical DRAM. Discrete-GPU topology here has no such redundant-copy pattern to eliminate — the H2D/D2H copy is real, not an artifact of a legacy dGPU-style API used on UMA hardware. |
| 2603.03271 | vmcache_n: n-tier virtual-memory buffer pool, new `move_pages2` syscall | parked, no memory tier to target | Genuinely kernel-shaped (a real new syscall, `move_pages2`, with `migration_mode`/`nr_max_batched_migration` knobs for batched page migration across DRAM→RMem→NVMe tiers) — the most patch-adjacent idea in this set. But it exists to move pages between local DRAM and a *remote* memory tier (NUMA-remote socket, CXL, RDMA). This box has one socket, one NUMA node, no CXL — nothing for it to migrate between. Worth re-reading if this workstation ever grows a CXL tier; not actionable now. |
| 2609.04238 | Budgeting Bytes: windowed storage roofline, address-determinism taxonomy | die for this model, useful theory | Explicitly targets MoE `--n-cpu-moe`/`--override-tensor` placement in llama.cpp and cites an open llama.cpp feature request (#20757, "two-tier expert cache") as the gap it fills. Our model is dense — no experts to place. The theoretical framework (A0–A3 address-determinism classes, EDF-optimal prefetch hiding) is a clean way to reason about what DAMON/readahead *could* prefetch, but the paper's own validated regime is sub-100M-parameter research models, not our 15.85 GiB dense deployment. |
| 2608.25062 | FLINT: High Bandwidth Flash substrate for LLM inference | die | Requires HBF (3D-stacked NAND in an HBM form factor) silicon that isn't a purchasable product — this is a memory-vendor/accelerator co-design paper, not something a Samsung 990 EVO Plus over Gen4 NVMe can imitate. |
| 2605.07719 | Fluxion: hybrid sparse attention over CPU-resident KV | userspace lever, model-mismatched | Head-specific/output-aware KV budgeting is a real serving-stack idea, but it's built for long-context serving (LongBench/RULER-scale contexts) with multi-request batching. Local single-user inference at batch size 1 with a modest KV footprint (this repo's own numbers: KV cache is a minority of the 15.85 GiB footprint given only 4 KV heads and 17-of-65 layers carrying real attention) doesn't hit the CPU-side top-k bottleneck this paper targets. |
| 2605.25298 | eBPF thread-dynamics diagnostics (TSA + inter-thread dependency graph) | tooling, not a patch | 16 eBPF metrics across sched/VFS/net/futex/epoll/block-IO, open-sourced artifact. Directly usable as a diagnostic the next time GreenBoost and a Proton game are suspected of contending for the same resource class (lock, disk, CPU) — worth trying as a tool, not a kernel change. |
| 2406.18980 | E-Mapper: energy-aware resource allocation on heterogeneous CPUs | die (kernel), tooling-shaped | Measured on a real Raptor Lake **i9-13900K** (this box's direct predecessor) plus an Arm big.LITTLE board: 20% faster, 34% less energy via P/E-aware placement. Confirmed by reading its design section (`2406.18980.md:137`): E-Mapper is explicitly a **userspace daemon** layered on the existing scheduler/EAS, "similar to modern system management daemons such as systemd" — reads hardware/application description files and issues placement hints, no kernel patch. Interesting precedent for a possible future `power-profiles-daemon`-style tool that pins GreenBoost's CPU-side compute away from a game's P-core threads; not a kernel change. |
| 2608.23165 | Effects of hybrid CPU/cache on parallel HPC apps (Alder Lake, empirical) | tuning input, not a patch | Two measured findings on real 8P+8E Alder Lake hardware, structurally close to our 8P+16E Raptor Lake: (1) work-imbalanced parallel applications scale *better* across P/E cores with **thread affinity disabled**, letting the scheduler rebalance; (2) the E-core-cluster shared L2 has negligible impact on shared-data workloads except lock-heavy ones. Directly informs how GreenBoost's own CPU-side thread pinning (T2 spillover compute) should be configured relative to a co-running game — a recipe/config decision, not a kernel patch. |
| 2605.00519 | Silicon Showdown: consumer NVIDIA vs. Apple LLM inference | die (kernel), informs stack choice | Empirical finding worth keeping: the RTX 5090's own successor stack (TensorRT-LLM + NVFP4) shows a 2.2x TTFT regression versus the prior-gen RTX 4090 on the same tasks — "software maturity gap" on Blackwell, not a hardware regression. NVFP4 is a TensorRT-LLM-specific format; GreenBoost's GGUF/llama.cpp path doesn't touch it. No kernel or fragment action; confirms staying on the GGUF/llama.cpp path rather than chasing TensorRT-LLM on this RTX 5070 (same Blackwell generation) is the safer choice today. |
| 2606.00486 | DEPOT: dead-entry GPU L2 TLB protection | die | Up to 99% of L2 TLB misses on TLB-sensitive workloads are needless re-walks of just-evicted entries — a striking number, but the fix is a proposed **GPU-silicon** Bloom filter inside the SM86 (Ampere) TLB hierarchy studied in the paper. Nothing here is reachable from the host kernel: the GPU's own page-walk hardware and MMSU are opaque to us, and the RTX 5070 is Blackwell, not the Ampere part characterized. The validating detail (huge-page experiments distinguish the two miss classes) is about the GPU's own page tables, not host `CONFIG_TRANSPARENT_HUGEPAGE`. |
| 2605.09490 | Semantics-aware 4-tier KV hierarchy for reasoning LLMs | userspace/GreenBoost-policy lever | "Zero-approximation-error offloading" result — accuracy depends only on how much KV is *permanently discarded*, not on how much stays resident in HBM — is a genuinely useful policy input for GreenBoost's own T1/T2/T3 cold-demotion logic, which today is not attention-score-aware. Entirely userspace/inference-engine territory (tiering decision inside the serving stack), and the paper's own workload (long chain-of-thought reasoning, thousands of CoT tokens) is a heavier KV regime than this box's typical chat/code usage. Worth a note for GreenBoost, not a kernel patch. |

---

## What survived the kernel-lever gate

**Nothing.** Of 15 papers, zero produce a kernel patch candidate for this
machine right now. The one syscall-shaped idea (`move_pages2`, 2603.03271)
is gated on a memory tier this box doesn't have (CXL/remote NUMA); the rest
split cleanly into "wrong topology" (UMA-SoC or multi-socket mechanisms on a
single discrete-GPU desktop), "wrong model shape" (sparse/MoE mechanisms
against a dense-hybrid served model), or "real, but userspace" (inference
engine scheduling, GreenBoost tiering policy, a diagnostics tool, or a
config/recipe tuning input).

That is consistent with the standing finding in
`docs/research/10x-tok-s-kernel-path-2026-08-17.md`: the kernel's share of
decode time on this box is bounded at ~5.7%, and every mechanism in this
batch that clears the "real and applicable" bar lives in the other 94.3% —
the inference engine, the driver, or operator config.

## Non-kernel notes worth relaying elsewhere

- **GreenBoost / gb-synapse**: ATSInfer's tensor-granularity static+dynamic
  placement (2607.10183) and the KV eviction-score policy from 2605.09490 are
  both inference-engine ideas that could improve tok/s or VRAM headroom
  without touching the kernel.
- **Thread-pinning recipe**: 2608.23165's finding (disable affinity for
  work-imbalanced parallel loads on hybrid P/E) is a candidate change to how
  GreenBoost's CPU-side threads are pinned relative to a co-running Proton
  game — test before adopting, it's one paper on different-but-related
  silicon (Alder Lake 8P+8E vs. this box's Raptor Lake 8P+16E).
- **Diagnostics**: 2605.25298's public eBPF thread-dynamics tool is worth
  trying next time a "game feels stuttery while GreenBoost is serving"
  session needs root-causing between lock/disk/CPU contention.
- **Stack choice confirmed**: 2605.00519 confirms staying on GGUF/llama.cpp
  rather than chasing TensorRT-LLM/NVFP4 on this RTX 5070 — the newer stack
  has a documented regression on the same Blackwell generation.

No patches were drafted from this set — see
`docs/research/kernel-nvidia-path-2026-09-10.md` for the companion Set B
sweep, which is where the (also negative) patch-candidate search concluded.
