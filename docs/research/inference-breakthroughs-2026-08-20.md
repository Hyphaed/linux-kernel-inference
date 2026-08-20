# Breakthrough survey: what can still make local inference faster here (2026-08-20)

Written to answer one question the owner asked directly: **is there a
DLSS-shaped result out there — software extracting far more from the same
silicon — that we could turn into a kernel patch?**

The short answer is that the DLSS-shaped result exists, it is speculative
decoding under weight offloading, **and it is already running on this box at
its measured optimum.** The survey's real value turned out to be negative: it
killed three kernel-patch candidates on evidence, two of which this session had
proposed itself a few hours earlier.

## Method, and what "read" means in the tables below

13 papers fetched as PDF and converted with docling before reading, per the
standing rule now recorded in the project notes:

```bash
docling convert --to md --image-export-mode referenced \
  --enrich-picture-description <file.pdf>
```

Candidates came from 11 arXiv API sweeps (~120 titles) plus 7 GitHub repo
searches. Two honesty conventions, both inherited from
`local-inference-sources-2026-08-17.md`:

- **read-in-full** means the marked sections were read in the converted
  markdown. **triage** means abstract only. No row is left ambiguous.
- A verdict of "does not apply" is a real result and is stated as such rather
  than softened into "future work".

## The measured baseline every verdict is judged against

| Quantity | Value | Source |
|---|---|---|
| decode | **5.64 tok/s median, n=12** | `gb_bench_spec` this session, f16 KV, draft_n 4 |
| draft acceptance | 0.65 | engine timings |
| speculation on/off | 3.80-3.92 → 5.62-6.03 tok/s | dataflux `spec_decode`, 2026-08-20 |
| weights vs budget | 15.9 GB against 10.4 GB → ~6.5 GB spills | `gb-synapse` serve line |
| PCIe effective | 11-12 GB/s of a 32 GB/s Gen4 x16 link | `docs/tasks_patches.md` T11 |
| decode split | 248 ms transfer of 263 ms total | `10x-tok-s-kernel-path-2026-08-17.md` |

Model geometry, read from the **served blob's own GGUF** this session, not from
a document:

```
general.architecture           qwen35        (arch id — the "Qwen3.8" merge uses it)
qwen35.block_count             65
qwen35.attention.head_count    24
qwen35.attention.head_count_kv 4
qwen35.attention.key_length    256
qwen35.full_attention_interval 4     -> 17 KV layers, 48 Gated DeltaNet
qwen35.nextn_predict_layers    1     -> one MTP head
(no expert_count)                    -> DENSE
```

## Tier A — speculation under offloading

| ID | Read | Headline | Verdict here |
|---|---|---|---|
| `2605.11186` **CATS** | read-in-full (§1, §3, §5.2.2) | 5.08x, self-speculative, cascaded verification, no draft model, peak memory = target model alone | **Explains our ceiling.** Its Table 5 measures mean accepted length 2.25-3.33 in the memory-limited regime, *not* the 14-30 that SpecExec/SubSpec report. Our 0.65 acceptance at depth 4 gives ~1.5 extra tokens, and 5.9/3.9 = 1.51x measured. Internally consistent. Needs distilled adapters — training cost, not a config change. |
| `2608.08097` **OasisKV** | read-in-full (§3.1-3.2, §4.1) | 1.69-2.1x; uses speculative lookahead tokens to predict and prefetch the KV blocks the next step will need | **Killed our prefetch patch.** Its overlap budget is `C_token = B_link × T_decode`. Prefetch hides latency only if the link is idle during compute. Here the link is busy 248 ms of a 263 ms step — 94%. There is no window to prefetch into. |
| `2607.26475` **DualDecoder** | triage | 2.62x; critical KV entries predictable from the preceding speculated token, prefetch overlapped with decode | Same overlap assumption as OasisKV, same objection. Also targets high-concurrency long-context serving; we are batch-1. |
| `2505.10259` **SpecOffload** | triage | 2.54x throughput, 4.49x GPU core utilisation, by putting a draft model in *idle* GPU capacity | Nearest published match to our configuration. Its premise is idle GPU resources; we measure 100% GPU utilisation during decode. Worth a real read before any drafting rework — [code](https://github.com/MobiSense/SpecOffload-public) exists. |
| `2607.25852` **AngelSpec** | triage | 1.98-2.40x; MTP vs block-diffusion drafters, +30% accepted length; verification depth adapted to load | Our engine already exposes `draft-dflash` in `--spec-type`. Needs a trained DFlash drafter for this architecture, which does not exist for a custom merge. |
| `2607.03876` **AdaptiveSD** | triage | Runtime-adaptive draft depth for GGUF models, 68-82% speculative efficiency | Our fixed depth 4 sits inside that band already (0.65 acceptance). Framework is heavy — 11-rule policy hierarchy plus RL — and llama.cpp exposes no hook for per-step depth. |
| `2509.18344` SubSpec, `2406.02532` SpecExec | read 2026-08-17 | 9.1x / 10.6x | **Their acceptance lengths do not transfer.** Both use large *tree* speculation (budget 128-256) with a real draft model. llama.cpp's `draft-mtp` is a linear chain off one MTP head. CATS measures what the linear regime actually yields, and it is what we get. |

## Tier B — kernel-shaped

| ID | Read | Headline | Verdict here |
|---|---|---|---|
| `2604.12300` **TierBPF** | read-in-full (§1, §4, §5.3, §6) | +7.4-17.7% geomean; three eBPF hooks on the NUMA migration path, 540 lines of kernel change | **Mechanism does not apply. Pattern does.** Its hooks live in `mm/migrate.c` and `kernel/sched/fair.c`, driven by NUMA faults. This box has **one NUMA node** and boots `numa_balancing=disable`; that path never runs. Its slow tiers are CXL and Optane PMEM; we have neither. What *is* worth stealing is the hook shape: a `noinline` function with `ALLOW_ERROR_INJECTION` returning a safe default, overridable by an `fmod_ret` BPF program — zero cost when nothing is attached, and no new UAPI. That is the right form for any future GreenBoost eviction-policy hook. |
| `2603.10030` **dmaplane** | read-in-full (§1.1-1.2, §4.5, §6.3, §6.5) | A kernel module exposing buffer orchestration via `/dev/dmaplane`: dma-buf export, NUMA-aware allocation, RDMA registration, GPU BAR pinning | **Two results, both useful.** (1) It does *not* contest 0019/0020's UAPI ground — it is a private char device, not an extension of dma-buf core, so the RFC story is unchanged. (2) §4.5 states our bottleneck's mechanism outright: *"GPU to host reads remain limited by non-posted PCIe read semantics."* That is the first citable explanation for 11-12 GB/s on a 32 GB/s link. Caveats: single author, preprint, RDMA numbers are Soft-RoCE only. |
| `2604.26557` **DUAL-BLADE** | triage | NVMe-direct KV path bypassing the page cache via LBA mapping, because page-cache thrashing was the real cost | Aimed at T3. `tier_t3_local_lifetime_mb=0` on this box — T3 has never been allocated in this machine's lifetime. Optimising it would violate "only patch what you can prove helps". |
| `2605.03375` **Tutti** | triage | GPU-centric SSD KV store, GPU io_uring, −78.3% TTFT vs GDS-enabled LMCache | Same T3 objection. Notable that it finds GDS *itself* CPU-bound because the CPU still initiates each I/O — worth remembering before anyone proposes GDS as a fix here again. |

## Tier C — residency and quantisation

| ID | Read | Headline | Verdict here |
|---|---|---|---|
| `2606.20474` **UltraQuant** | triage | 4-bit KV with Walsh-Hadamard rotation; 3.47x P50 TTFT in cache-pressured rounds, 1.63x output throughput over FP8 KV | Implementation is AMD CDNA4 (`scaled MFMA`, UE8M0 scales) — not portable to Blackwell or GGUF. Its *premise* also does not hold here: KV quantisation frees 0 MB on this model. But it is the one Tier C paper pointing the right way by accident — it reports 4-bit beating 8-bit on throughput, which is exactly the non-monotonic ordering measured on this box (q4_0 7.18 > f16 4.74 > q8_0 2.69). |
| `2608.04074` | triage | KV vector quantisation with attention-preserving transforms | Same direction, no deployment path in llama.cpp, and moot here for the same reason as UltraQuant. |
| `2601.09527` **Blackwell consumer GPUs** | read-in-full (abstract, §1) | 79 configurations across RTX 5060 Ti / 5070 Ti / 5090; **NVFP4 gives 1.6x throughput over BF16 at 41% less energy and 2-4% quality loss** | Our arch. NVFP4 is a Blackwell tensor-core format and GGUF cannot emit it — this is a TensorRT-LLM / vLLM path. Note `synapse_status` reports `torch_engine_ready: true`, so the door is not closed. Largest single unexplored multiplier found in this survey. |

## GitHub

| Repo | Stars | Verdict |
|---|---|---|
| [`MobiSense/SpecOffload-public`](https://github.com/MobiSense/SpecOffload-public) | — | Reference implementation for `2505.10259`. The one repo here worth reading line by line if drafting is revisited. |
| [`sqliteai/warp`](https://github.com/sqliteai/warp) | 2.2k | Streams *activations* to exceed RAM. Closest analogue to what the shim does. |
| [`kvcache-ai/ktransformers`](https://github.com/kvcache-ai/ktransformers) | 19.3k | Heterogeneous CPU/GPU placement. Its expert-offload machinery is MoE-only and **does not apply** — our model is dense. |
| [`ikawrakow/ik_llama.cpp`](https://github.com/ikawrakow/ik_llama.cpp) | 3.1k | Same engine family, SOTA quants. Portable by construction. Highest-value repo for us after SpecOffload. |
| [`Tiiny-AI/PowerInfer`](https://github.com/Tiiny-AI/PowerInfer) | 9.7k | Hot/cold neuron split — the original locality argument. Relies on ReLU sparsity this model does not have. |
| `JustVugg/colibri`, `danveloper/flash-moe`, `MakazhanAlpamys/Soup` | 25.6k / 4.1k / 2.5k | **MoE expert streaming. Architectural reading only.** Our model is dense with no `expert_count`; `--n-cpu-moe` and `-ot` do not apply to it. |
| [`hemingkx/SpeculativeDecodingPapers`](https://github.com/hemingkx/SpeculativeDecodingPapers) | 1.3k | Index, used to check the sweeps missed nothing. |

## What this means for the kernel: nothing new, and here is the proof

Three candidates were proposed in this session's own plan. All three are dead,
each for a different and independently checkable reason.

**P1 — a userspace prefetch-ahead ioctl (`GB_IOCTL_PREFETCH`).** Killed by
OasisKV's own overlap budget. Prefetching moves a transfer into a window where
the link is otherwise idle. Our link is occupied 248 ms of every 263 ms decode
step. A transfer cannot be overlapped with itself. The gap in the ioctl
surface is real — `SET_HEAT` reports the past, `SESSION_ACTIVE` reorders an
LRU, and the only actual prefetch is shim-side on a 2 s cadence against a
~250 ms loop — but closing it would optimise a window that does not exist.

**P2 — PCIe TLP efficiency (`pci=pcie_bus_perf`, MPS/MRRS).** Killed by the
register dump. The GPU already runs `MaxPayload 256B` against a `DevCap` of
256B — its own ceiling — with `MaxReadReq 4096B`, the largest PCIe defines,
and `ExtTag+`. Its root port agrees at 256B. There is nothing to raise.
Worse, `00:1d.0` sits at 128B, so tree-wide "performance" mode carries
regression risk for zero upside. `grub.py`'s default (off) is correct and
should stay off.

**P3 — TierBPF-style hooks.** Killed by topology: one NUMA node,
`numa_balancing=disable`, no CXL, no PMEM. Its hooks would never fire.

Add to those the three layers below the runtime, all now confirmed at their
ceilings: PCIe Gen4 is the board's maximum (T11), DDR4-3600 dual channel runs
at 5x the rate the path consumes (`BIOS_TUNING_AI_INFERENCE.md` §1), and
Above-4G/ReBAR are on. **Every configurable layer beneath the inference
runtime is measured and maxed.** The 11-12 GB/s is the non-posted read pattern
itself, and that is a CUDA/shim property.

The 5.7% kernel bound from `10x-tok-s-kernel-path-2026-08-17.md` stands
unchanged, and this survey found no way to spend it better.

## Decision taken

`serving/recipes/qwen38-27b-cold-fusion-gain-v11.yaml` now pins
`kvCache: key/value: q4_0` (was `f16`), with the full measurement and its
caveats recorded in the recipe's own comment block. Verified live: the serve
line reports `kv=q4_0` when the recipe default is taken, draft depth still 4.

The caveat that belongs next to the number: the NIAH gate is **saturated** at
the largest haystack the fixture supports, so it establishes that q4_0 is not
*separable* from f16, not that it is lossless. `GB_SYNAPSE_KV=f16` pins the
old behaviour for one run; reverting the two recipe lines restores it
permanently.

## What it means for GreenBoost

1. **Speculation is already at its measured optimum** and should be left
   alone. Depth 4 was re-validated on this exact model today: 0 → 3.90,
   2 → 4.51, **4 → 6.03**, 6 → 4.61 tok/s. The apparent "it is turned down to
   4" reading of the default is wrong; 4 is where the acceptance curve knees.
2. **`GB_SYNAPSE_SPEC_EXTRA` — measured 2026-08-20, and it is large.**
   `gb_synapse.py:5502` set it empty with its own comment saying *"this is a
   measured lever, not an assumption"*, and nobody had measured it. The
   `ngram-*` modes need no draft model and no VRAM — they predict from text
   already in the context — and llama.cpp's `--spec-type` takes a comma list,
   so they stack with the model's own MTP head.

   Four legs, n=5, at a 14.5k-token prompt, baseline run first and last:

   | `--spec-type` | decode tok/s | range | accept |
   |---|---|---|---|
   | `draft-mtp` (baseline) | 7.88 | 7.63-7.91 | 0.85 |
   | `draft-mtp,ngram-cache` | 9.02 | 9.02-9.09 | 0.737 |
   | **`draft-mtp,ngram-cache,ngram-mod`** | **16.17** | **9.10-16.55** | 0.521 |
   | `draft-mtp` (control) | 7.58 | 7.33-7.60 | 0.85 |

   The control reproduced to within 4%, so the effect is real: **2.1x median**
   for the stacked ngram modes. Acceptance *falls* to 0.52 while throughput
   doubles, which is the correct trade — a rejected ngram draft costs almost
   nothing, because it was never a model forward pass.

   **The caveat is load-bearing and the number must not be quoted without it.**
   The probe prompt deliberately asked the model to *quote a sentence verbatim
   from its context*, which is precisely what ngram speculation wins on. That
   is why the range is 9.10-16.55 rather than tight: some turns hit the
   pattern and some do not. **2.1x is the favourable end, not a general
   figure.** A neutral, non-quoting prompt has not been measured yet, and that
   measurement is what should gate making this a default.

   It is still the most promising unexploited lever in the stack, because the
   real workload — an agentic turn re-emitting file contents, paths,
   identifiers and quoted tool output — is much closer to the favourable end
   than to a creative-writing turn.
3. **`kv_type` is a real throughput lever — and not the one anybody would
   have guessed.** Two facts, both measured here today, and they only make
   sense together.

   *It is not about memory.* The shim-measured footprint in
   `/var/lib/greenboost/synapse/kv_measurements.json` is **218 MB for f16,
   q8_0 and q4_0 alike** at ctx 24576 — real `cudaMalloc` interception, not a
   formula. 48 of 65 blocks are Gated DeltaNet, whose recurrent state does not
   depend on `--cache-type-k/v` at all. Quantising KV frees nothing.

   *It is about kernels.* At a fixed 14,549-token prompt, n=5 per leg:

   | kv_type | decode tok/s | range | prefill ms |
   |---|---|---|---|
   | f16 | 4.74 | 4.66-4.89 | 786 |
   | **q8_0** | **2.69** | 2.60-2.69 | 1095 |
   | **q4_0** | **7.18** | 7.17-7.18 | 511 |
   | f16 (control, run last) | 4.89 | 4.88-5.03 | 751 |

   **q4_0 is 1.48x faster than f16. q8_0 is a 1.8x regression** — slower than
   not quantising. The f16 control reproduced to within 3% while GreenBoost's
   VRAM budget drifted 1.5 → 0.9 GB between legs, so the ordering belongs to
   `kv_type`, not to run conditions. Within-leg spread is negligible (q4_0
   spans 0.01 tok/s over five turns).

   Working hypothesis: llama.cpp's flash-attention kernel coverage. This model
   has `head_dim 256`, an unusual size; quantised-KV FA kernels are selected
   per type and per head_dim, and q8_0 appears to miss a fast path that q4_0
   hits. Untested — the measurement does not depend on it.

   **The effect is invisible at short prompts.** At 71-73 tokens f16 spans
   4.0-7.4 tok/s and `kv_type` does nothing measurable. That is precisely the
   shape `gb_bench_spec` sends, which is why this sat undiscovered. This box's
   real requests carry ~13,000 tokens of system prompt and MCP schemas.

   *Consequence for Tier C:* UltraQuant and the KV-VQ literature aim at
   footprint, which is not the currency here. What matters on this model is
   which quantised-KV attention kernel the engine can reach.
4. **NVFP4 on the torch engine** is the largest unexplored multiplier
   (`2601.09527`: 1.6x, −41% energy, 2-4% quality). Out of scope for the GGUF
   path entirely.

## Corrections log

Three claims made earlier in this same session were wrong. All three came from
trusting a written artefact over the live one — the exact failure the project notes
already records twice.

1. **"Speculation is turned down to 4, that is the missing 10x."** False.
   Read from the default value; the sweep validating it was already in
   dataflux. Depth 4 is optimal for this model.
2. **"SpecExec/SubSpec predict τ ≈ 14-30 for us."** False. Those use tree
   speculation with a separate draft model. CATS measures the linear
   self-speculative regime we are actually in and reports 2.25-3.33.
3. **"q8_0 KV frees 3 GB."** Wrong twice, in two different ways.

   First wrong: the `kv_gb` values in the `kv_quality` events (6.0 / 3.0 /
   1.5) are not measurements. They come from `gb_bench_kv._kv_gb`'s fallback,
   which hardcoded `layers, heads_dim = 64, 128 * 8` as "this model's shape".
   Fixed this session to derive from the entry's real geometry via
   `gb_synapse.estimate_kv_gb`, with two tests: one pinning the derived value
   to the model's own geometry, one asserting a blank rather than an invented
   number when the geometry is unknown.

   Then wrong again: the corrected formula gives 1.59 / 0.80 / 0.40 GiB, so
   "q8_0 frees 0.8 GiB" — and that is *also* wrong, because the formula
   overstates this architecture. The shim-measured truth was on disk the whole
   time: **218 MB for all three types**. q8_0 frees nothing.

   Three sources, each more authoritative-looking than the last, and only the
   third was real. The lesson generalises past this number: **when a measured
   value exists on disk, no formula gets a vote.**

4. **"The 9.1 tok/s q8_0 reading is sampling noise."** Also wrong, and this
   one in the favourable direction. Filtering the raw `tok_s_measured` events
   by `prompt_tokens` showed those readings were a matched A/B at 19,571
   tokens — f16 5.7 measured twice, q8_0 9.1, q4_0 8.5 — while the 4.3 figure
   I compared against came from 73-token turns. Different workload, not
   different noise. Chasing it down produced the `q4_0` result above, which is
   the session's only positive finding.

   Note the direction reversed twice: the long-prompt A/B put q8_0 at 2.69,
   below f16, contradicting that 9.1 reading. Both were single-sample. n=5
   with a reproduced control is what settled it, and only q4_0 survived.

5. **A quality gate that could not pass.** `gb_aviary.smoke_gate()` failed
   `q4_0` with *"only 6 tokens of content"*. It was not a quantisation
   failure: the gate asked *"Say hello and name three colors."*, the model
   answered `Hello! Three colors: red, blue, green.` — correct, and six
   whitespace tokens against a flat `len(toks) < 8 -> FAIL` at
   `gb_aviary.py:257`. **f16 failed identically, byte for byte**, which is
   what proved it was grading its own prompt.

   It would have blocked the only positive finding of the session, and it
   would have done so while emitting `verdict: FAIL` into dataflux — a false
   result that reads as evidence. Fixed: the prompt now asks for a sentence
   per colour, an empty response still FAILs, and short-but-present returns
   `INCONCLUSIVE` with a reason saying it is not evidence of collapse. Four
   regression tests, including the exact live string. Both kv_types now PASS
   with identical greedy output at temperature 0.

   Worth stating in general terms: a gate whose own prompt invites an answer
   shorter than its scoring window can emit a verdict no correct model can
   avoid. That is worse than having no gate, because the FAIL gets believed.

The pattern in all three: a number that looked authoritative because it was
specific. The fix in all three was to read the artefact that is actually live.
