# Kernel ↔ NVIDIA GPU workflow, RTX 5070 12GB — 15 papers, 2026-09-10

Companion to `docs/research/topology-levers-2026-09-10.md` (Set A). Same
dedup/fetch/docling process — see that file's header for the tooling note
(`hyperresearch fetch` fails on raw PDF URLs; worked around with `curl` +
docling). All 15 IDs here are new to the vault.

---

## Triage table

| arXiv | Title | Verdict | Reason |
|---|---|---|---|
| 2609.06172 | AutoUVM: framework-aware UVM prefetching | die (kernel), userspace-real, wrong stack | Genuinely close to our failure mode (model bigger than VRAM under UVM), and it's five days old. But the mechanism is entirely userspace: a PyTorch-level profiling pass that recovers tensor identity, then calls the *existing* `cudaMemPrefetchAsync`/`cudaMemAdvise` driver APIs more intelligently. Nothing kernel-reachable, and it's built for PyTorch's caching allocator — GreenBoost/llama.cpp uses GGML's own allocator, not PyTorch, so even the userspace idea doesn't port directly. |
| 2604.26889 | Revealing NVIDIA closed-source driver command streams | observability technique, not a patch | Genuinely clever methodology — recovers the driver's real DMA submission pattern by instrumenting the open kernel driver's mmap path and watchpointing the GPU doorbell register. Useful reading for understanding what `nvidia-open` actually does on the Copy-Engine path, but it's a measurement technique for *researchers studying the driver*, not something that changes what our driver does. No action. |
| 2512.24637 | MSched: proactive GPU memory scheduling via context-switch prediction | die | Requires deep hooks into GPU context-switch handling (predicting working sets from kernel-launch arguments, co-designed with a task scheduler) that live inside the GPU's own firmware/driver scheduling path. NVIDIA's real GPU scheduling is owned by closed-source GSP firmware; nothing in this paper's mechanism is reachable from the host Linux kernel on a consumer NVIDIA card. Also targets *concurrent* multi-tenant GPU workloads — this box mostly runs one saturating GPU workload at a time. |
| 2605.22416 | AVMP: asymmetric paging for hybrid Mamba-Transformer KV/state caches | thematically closest match, not actionable | The served model on this box genuinely is hybrid (Gated-DeltaNet + full-attention layers, `full_attention_interval=4`) — this is the single paper in either set whose target architecture matches our deployment. But the prototype is a standalone pure-Python allocator tested against synthetic cells and ShareGPT replay, built for vLLM/SGLang's paged-KV model. GGML (what llama.cpp/GreenBoost actually uses) doesn't allocate KV the way vLLM does, so the allocator doesn't port. No kernel surface either way — it's a serving-engine allocator. Worth a note to GreenBoost as a shape to watch for, not something to adopt today. |
| 2411.05309 | GPUVM: GPU-driven UVM via RDMA, bypassing host CPU/OS | die | Needs an RDMA-capable NIC and GPU-resident page-fault handling that NVIDIA does not expose on consumer hardware or open-source drivers — evaluated on Cloudlab, not a workstation. Nothing here is reachable on this box. |
| 2405.04437 | vAttention: KV memory management via CUDA VMM instead of PagedAttention | die for our stack | Fixes a real vLLM-specific problem (PagedAttention's non-contiguous KV layout costs up to 2.8x kernel slowdown) using existing CUDA virtual-memory-management driver APIs (`cuMemAddressReserve`/`cuMemCreate`/`cuMemMap`) — all userspace, already available. GGML's KV allocation is structured differently and doesn't have vLLM's fragmentation problem to begin with, so the fix doesn't apply to our stack. No kernel involvement in the mechanism regardless. |
| 2310.12554 | GMEM: generalized unified device memory management (Rice Univ.) | die | The most kernel-subsystem-shaped paper in this batch — a real FreeBSD IOMMU driver rewrite, -700 LOC, +54% network throughput. But it's a 2023 research OS-interface proposal with no evidence of landing in Linux mainline (mainline still uses HMM + IOMMU/SVA, not a generalized KPI like this). Even hypothetically adopting it wouldn't touch NVIDIA's actual UVM path: `nvidia-uvm` is NVIDIA's own from-scratch closed-source subsystem, not built on the generic `mm/hmm.c` code GMEM targets. Out of reach twice over. |
| 2310.09443 | G10: unified GPU memory+flash via compiler-scheduled tensor migration | die | Evaluated on an **open-source GPU simulator**, not real silicon — fails the real-hardware evidence bar on its own terms, separate from topology mismatch. Also needs compiler cooperation (tensor-behavior scheduling at compile time) that GGML/llama.cpp's interpreter-style execution doesn't provide. |
| 2605.11678 | OOM-Free Alpamayo: CPU-GPU layer swapping, evaluated on RTX 5070 Ti | userspace lever, real hardware match | Evaluated on an **RTX 5070 Ti (16GB)** — same Blackwell generation as this box's RTX 5070 (12GB). The mechanism (demand-layer loading + pipelined double-buffer prefetch + a GPU-resident-layer policy) is structurally the same problem GreenBoost's own T1/T2 tiering and llama.cpp's `--n-gpu-layers` already solve. The transferable idea is narrower and real: a closed-form residency-benefit model that picks the optimal resident-layer count from **one profiling run** at <1.3% prediction error, instead of static heuristics. Worth relaying to GreenBoost as a tiering-policy improvement — entirely userspace, no kernel surface. |
| 2504.14893 | H2M2: hardware-accelerator co-design for capacity+bandwidth memory | die | Cycle-accurate-simulator hardware architecture (custom compute units on both memory pools, Grace-Hopper-adjacent) — no purchasable part implements this. |
| 2604.02473 | Reverse Address Translation in NVLink/UALink scale-up pods | die | Multi-GPU, multi-node NVLink-network/UALink fabric — this box has one GPU and no scale-up interconnect. Also simulated (ASTRA-sim + Omnet++), not measured on real hardware. |
| 2508.12743 | Dissecting CPU-GPU Unified Physical Memory on AMD MI300A (El Capitan) | die, kept as calibration | Deliberately counter-evidence per the sweep design. AMD hardware, not purchasable as a consumer part. The real number worth keeping: true unified *physical* memory (not just unified virtual memory) matches explicit-copy performance while cutting memory cost 44% — that's the ceiling a PCIe-attached discrete GPU structurally cannot reach. Calibrates expectations for any future GDS/P2PDMA work on this box: we are permanently below what an APU gets for free. |
| 2405.06811 | Shared Virtual Memory design/performance on AMD ROCm | die, corroborating | AMD SVM (`hipMallocManaged`), not CUDA. The finding — aggressive prefetch plus an eviction policy causes thrashing specifically under oversubscription — is architecturally the same failure class NVIDIA's own UVM exhibits (and the reason GreenBoost uses explicit `--n-gpu-layers` partial offload instead of UVM oversubscription today). Corroborates the existing design choice; no new action. |
| 2603.16428 | SlideFormer: single-GPU LLM fine-tuning memory co-design | die | Training/fine-tuning workload. This box's served workload is inference-only (GreenBoost/gb-synapse); fine-tuning isn't in scope per this repo's own conventions. |
| 2608.30877 | "DeepSeek 175B" on an RTX 4060 laptop, 200k-scale virtual screening | die, flagged unreliable | No DeepSeek model named "175B" exists in any public release (DeepSeek's public weights are V2-236B, V3-671B, R1-671B) — the paper's own citation for it, "DeepSeek AI (2024), arXiv:2401.06066," is a real arXiv ID but belongs to a different paper (DeepSeekMoE, a 16.4B model). The headline claim (100x the throughput of an 8-card A100 cluster, on 8GB VRAM) is physically implausible for any real inference stack. Kept in this table only to record why it was excluded — not used as evidence for anything, per the untrusted-source handling this repo already applies to fetched content. |

---

## What survived the kernel-lever gate

**Nothing, again.** Combined with Set A, this is a 30-paper sweep that
produced zero kernel patch candidates for this machine. The real mechanisms
in this batch split the same way as Set A did:

- **Existing driver APIs used more cleverly** (AutoUVM's `cudaMemPrefetchAsync`,
  vAttention's CUDA VMM calls) — userspace, already available, and in both
  cases built for a different framework (PyTorch, vLLM) than what GreenBoost
  actually runs (GGML/llama.cpp).
- **Hardware we don't have** (AMD MI300A/ROCm, RDMA NICs, multi-GPU NVLink/UALink
  scale-up fabrics, unbuilt accelerator silicon).
- **Simulator-only evidence** (G10, the ASTRA-sim reverse-translation study) —
  fails the real-hardware bar this repo enforces regardless of topology fit.
- **Wrong workload** (SlideFormer's fine-tuning focus).
- **Genuinely kernel-subsystem-shaped, but not upstream and wouldn't reach
  NVIDIA's closed UVM path anyway** (GMEM).
- **One paper that doesn't survive scrutiny at all** (the "DeepSeek 175B"
  screening paper) — recorded, not cited as evidence.

The one item worth carrying forward is **not a kernel patch**: OOM-Free
Alpamayo's closed-form resident-layer-count predictor (2605.11678),
measured on the same Blackwell generation this box runs, is a real,
transferable idea for GreenBoost's own T1/T2 tiering policy.

## Phase 2 disposition (per the approved plan)

The plan's Phase 2 called for drafting the top 2–3 ranked patch candidates
into `patches/custom/`. **There is nothing to draft.** Across both sets (30
papers), no candidate produced a kernel-reachable mechanism that (a) exists
on this hardware, (b) isn't already covered by something already shipped
(GDS, P2PDMA, MGLRU, DAMON, the existing `mbind()`/`move_pages` syscalls),
and (c) has a realistic path to a falsifiable before/after measurement on
this box. Manufacturing a patch from userspace-only or wrong-hardware
material would fail the evidence gate in `CLAUDE.md` before it was ever
built — the same standing this repo already took with 0020's PCIe
compressed-descriptor patch (`docs/research/pcie-compression-verdict-2026-09-03.md`).

This is a result, not a gap, in the same sense the 7.2.4 vendor-patch sweep
recorded "nothing to add" as a finding rather than an omission. The
higher-value output of this sweep is the four non-kernel notes above,
directed at GreenBoost/gb-synapse rather than the kernel tree.
