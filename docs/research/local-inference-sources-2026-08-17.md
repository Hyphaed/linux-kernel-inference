# Local AI inference — source survey (2026-08-17)

**Scope.** Prior art that could plausibly benefit local LLM/diffusion inference on
a single NVIDIA GPU with the VRAM-extension approach GreenBoost takes
(`~/Dev/greenboost_all/greenboost`: kernel module + CUDA `LD_PRELOAD` shim,
T1 VRAM / T2 DDR / T3 NVMe / cluster tiers) and the gaming suite
(`~/Dev/greenboost_all/greenboost_gaming`).

**Method.** arXiv API phrase queries (`all:"X" AND all:"Y"`) across six themes,
plus direct GitHub/GitLab repo lookups on a curated candidate list. Star-ranked
GitHub *search* was tried first and discarded — it surfaced long-tail forks and
missed vLLM, llama.cpp, SGLang and LMCache entirely, so the repo table below
comes from direct `/repos/{owner}/{name}` lookups instead.

> **Honesty boundary — read this before acting on any row.**
> These entries were selected from **titles and arXiv metadata only**. No paper
> below has been fetched and read in full. The "Verdict" column is a triage
> signal for what to read next, *not* a finding. Anything marked **ADOPT** still
> needs the paper read and its claims checked against our own measurements
> before a line of code changes. Treating this table as evidence would repeat
> the exact mistake logged in the project notes under "Config fragment pitfalls":
> believing a thing because it looks right rather than verifying it landed.

---

## arXiv — 15 papers

Ordered by expected relevance to GreenBoost's tiering problem.

| # | arXiv ID | Yr | Title (abbrev.) | Why it matters here | Verdict |
|---|---|---|---|---|---|
| 1 | `2604.26968v2` | 2026 | Predictive Multi-Tier Memory Management for KV Cache in Large-Scale GPU Inference | This *is* GreenBoost's problem statement: predicting which KV blocks belong in which tier. Closest match in the whole survey. | **ADOPT — read first** |
| 2 | `2604.26074v1` | 2026 | DAK: Direct-Access-Enabled GPU Memory Offloading with Optimal Efficiency | Direct-access offloading, i.e. the same territory as the shim's fake-pointer/`cuMemHostRegister` path. Potential direct comparison against our design. | **ADOPT** |
| 3 | `2601.19910v1` | 2025 | Understanding Bottlenecks for Efficiently Serving LLM Inference With KV Offloading | Bottleneck characterisation — useful to check our T2/T3 assumptions against someone else's measurements. | **ADOPT** |
| 4 | `2412.19442v3` | 2024 | A Survey on LLM Acceleration based on KV Cache Management | Survey; cheapest way to map the whole design space and citation-chain outward. Start here if reading only one. | **ADOPT — cheapest entry** |
| 5 | `2411.01142v1` | 2024 | NEO: Saving GPU Memory Crisis with CPU Offloading for Online LLM Inference | Direct T2 (DDR) tier prior art. | **ADOPT** |
| 6 | `2409.04992v1` | 2024 | InstInfer: In-Storage Attention Offloading for Long-Context LLM Inference | Pushes attention *into* storage — the aggressive end of our T3 NVMe tier. | Evaluate |
| 7 | `2204.02974v1` | 2022 | An Intelligent Framework for Oversubscription Management in CPU-GPU Unified Memory | GreenBoost uses managed UVM (`BVMM_TYPE_MANAGED`); this is UVM oversubscription policy directly. | **ADOPT** |
| 8 | `1910.09598v1` | 2019 | Performance Evaluation of Advanced Features in CUDA Unified Memory | Older, but the UVM prefetch/advise semantics it measures are what the shim manipulates. | Evaluate |
| 9 | `2410.17954v2` | 2024 | ExpertFlow: MoE Inference via Predictive Expert Caching | `gb_moe.py` already has an off-by-default MTP predictive-prefetch oracle. This is the published version of that idea. | **ADOPT — maps to existing code** |
| 10 | `2511.14102v1` | 2025 | MoE-SpeQ: Speculative Quantized Decoding with Proactive Expert Prefetching | Combines our two MoE levers (quantisation + prefetch) in one design. | **ADOPT** |
| 11 | `2508.06978v1` | 2025 | SSD Offloading for LLM MoE Weights Considered *Harmful* in Energy Efficiency | **Counter-evidence, deliberately included.** Argues against exactly what T3 does for MoE. Read it *because* it disagrees. | **ADOPT — adversarial** |
| 12 | `2511.05814v1` | 2025 | In-depth Analysis on Caching and Pre-fetching in MoE Offloading | Empirical prefetch analysis to calibrate the oracle against. | Evaluate |
| 13 | `2312.17238v1` | 2023 | Fast Inference of Mixture-of-Experts Language Models with Offloading | The original Mixtral-offloading work; the baseline everything above cites. | Evaluate |
| 14 | `2506.06472v1` | 2025 | Cost-Efficient LLM Training with Lifetime-Aware Tensor Offloading via GPUDirect Storage | GDS lifetime-aware placement — relevant now that `nvidia-fs` is confirmed loaded (see the project notes, 2026-08-10). | Evaluate |
| 15 | `1903.04611v1` | 2019 | Evaluating Modern GPU Interconnect: PCIe, NVLink, NV-SLI, NVSwitch, GPUDirect | Methodology reference for the PCIe substrate work (`33-pcie-dma-substrate.config`, patches 0019/0020). | Evaluate |

**Deliberately excluded** despite matching queries: speculative-decoding papers
(`2405.20314`, `2506.01986`, `2602.16052`, …) — real work, but they optimise
token throughput, not memory placement, so they do not touch the tiering
substrate that is GreenBoost's actual contribution. CXL papers (`2403.18702`
NeoMem, `2502.19233`) excluded for the same reason plus no CXL hardware on
either target machine.

---

## GitHub — 15 projects

All star counts verified live via the GitHub API on 2026-08-17.

| # | Repo | Stars | Relevance |
|---|---|---|---|
| 1 | `LMCache/LMCache` | 11.2k | **Closest competitor/ally.** A KV cache layer that offloads to CPU + disk — the exact tier GreenBoost implements below the CUDA API instead of above it. Read their eviction policy. |
| 2 | `kvcache-ai/ktransformers` | 19.2k | Heterogeneous CPU/GPU inference; the reference implementation for running big MoE models on one consumer GPU. Direct overlap with `gb_moe.py`. |
| 3 | `vllm-project/vllm` | 89.3k | PagedAttention is the canonical KV memory manager. Its block allocator is the design to beat or integrate with. |
| 4 | `ggml-org/llama.cpp` | 124.4k | GGUF is what `gb_gguf_plan.py` already plans against; its mmap + partial-offload path is a direct alternative to our shim. |
| 5 | `sgl-project/sglang` | 32.0k | RadixAttention — prefix-sharing KV reuse, an orthogonal saving to tiering. |
| 6 | `deepspeedai/DeepSpeed` | 43.0k | ZeRO-Inference is the mature CPU/NVMe offload baseline any GreenBoost benchmark should cite. |
| 7 | `FMInference/FlexLLMGen` | 9.4k | FlexGen — the paper/repo that established single-GPU throughput offloading; still the standard baseline. |
| 8 | `NVIDIA/TensorRT-LLM` | 14.4k | NVIDIA's own answer; its KV cache manager sets the vendor-supported bar. |
| 9 | `NVIDIA/open-gpu-kernel-modules` | 17.3k | Already used here (595.x / the 610 experiment). The only legitimate window into driver-side memory behaviour. |
| 10 | `Dao-AILab/flash-attention` | 24.7k | Attention memory traffic is what `gb_attn.py` competes with; changes here move the goalposts. |
| 11 | `sched-ext/scx` | 2.1k | **Not yet vendored.** BPF schedulers — the supported way to express P/E placement policy without patching the kernel. Directly relevant to both greenboost and the gaming suite. |
| 12 | `InternLM/lmdeploy` | 8.0k | Alternative KV quantisation + serving design; useful cross-check on `gb_quant.py`. |
| 13 | `huggingface/text-generation-inference` | 10.9k | Production serving reference; good source of realistic workload traces. |
| 14 | `NVIDIA/nccl` | 5.0k | Relevant only to the cluster/feeder fabric (`greenboost_netd.c` / `netc.c`), not single-box. |
| 15 | `NVIDIA/MagnumIO` | 122 | Small, but the official GPUDirect Storage material behind `nvidia-fs`. |

## GitLab — 3, and that is genuinely all

GitLab hosts very little in this domain; padding this to 15 would be
fabrication. The real ones:

| Repo | Stars | Note |
|---|---|---|
| `gitlab.freedesktop.org/mesa/mesa` | 641 | Where DRM/Mesa development actually happens; relevant to the gaming suite's Vulkan layer. |
| `gitlab.freedesktop.org/nouveau/mesa` | 14 | Nouveau — currently disabled by fragment 62, but the open-driver reference. |
| `gitlab.com/xanmod/linux` | 58 | Already vendored as a patch source. |

Searching `gitlab.com` broadly for "linux kernel" returns `ryzen_smu` (70★) and
then noise — no AI-inference kernel work of substance.

---

## The five things actually worth doing

Ranked by expected value to GreenBoost, not by novelty:

1. **Read `2412.19442` (survey) and `2604.26968` (predictive multi-tier).** Two
   papers that between them map the design space and address our exact problem.
   Everything else is cheaper to judge afterwards.
2. **Read `2508.06978` — the paper arguing NVMe offload for MoE is harmful.**
   If it is right, part of T3's rationale needs revisiting. Finding that out
   from a paper is far cheaper than finding it out from our own benchmarks.
3. **Study `LMCache` and `ktransformers` eviction/placement policy.** Both solve
   our problem above the CUDA API. If their policies beat ours, the policy can
   be lifted without giving up the below-the-API position that makes GreenBoost
   distinctive.
4. **Vendor `sched-ext/scx`.** The only item here that touches
   `kernel_inference` directly: `CONFIG_SCHED_CLASS_EXT=y` is already set, so
   BPF schedulers can be loaded today with no kernel rebuild. Cheapest path to
   real P/E placement control for both inference and gaming.
5. **Refresh the four stale vendored patch sources** (CachyOS `kernel-patches`,
   `linux-tkg`, `linux-cachyos` all at 2026-05-13). Three months of upstream
   work is almost certainly worth more than any single paper above.

## What this survey does *not* establish

- No claim that any technique above outperforms the current GreenBoost design.
  Nothing was benchmarked.
- No paper was read past its title/metadata.
- No breakthrough is identified. A survey cannot produce one; it can only say
  where to look, and the honest reading is that items 4 and 5 (both local,
  both boring) have a higher expected payoff this month than any paper here.
