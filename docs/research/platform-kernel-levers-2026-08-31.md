# Platform-wide kernel lever sweep — 25 papers, 2026-08-31

Companion to the four unmeasured levers found by probing the running
7.2.2-hyphaed kernel (mTHP, `nvme.poll_queues`, NVMe HMB, `cache_ext`
unmeasured) — see `docs/plan_patches.md`'s successor and the bench harness in
`diagnostics/bench-*.sh`. This document is the arXiv side: 25 papers selected
across NVMe/block, page cache/mm, PCIe/GPU interconnect, GPU/eBPF OS policy,
and memory tiering, triaged against four criteria (public code? mainline
kernel surface? applies to THIS topology — one NUMA node,
`numa_balancing=disable`, no CXL/Optane, PCIe Gen4 ceiling, RTX 5070 12GB,
i9-14900KF hybrid P/E, DRAM-less Samsung 990 EVO Plus? measured on real
hardware or only projected/simulated?).

**Tool note:** `hyperresearch fetch` fails on every direct PDF URL
(`JUNK_CONTENT: Binary PDF garbage in content`) — only the `/abs/` page works
cleanly. Abstracts were fetched through it; PDFs for the four full-read
survivors were pulled with `curl` directly and handed to docling (which does
its own PDF parsing, so this still satisfies the docling-before-reading rule
— pymupdf was never in the loop for these four). Worth filing as a real infra
regression before the next session relies on the CLI for a PDF.

**GPU note:** docling ran with `--device cuda` throughout (not the default
`auto`) — 4 papers converted with full picture-description enrichment in
under a minute total (9-15s each) on the RTX 5070, versus a multi-minute
CPU estimate.

---

## 1. Triage table — 19 that die, and why

| arXiv | Title | Verdict | Reason |
|---|---|---|---|
| 1903.04611 | Evaluating modern GPU interconnect (PCIe/NVLink/NVSwitch/GPUDirect) | die | Multi-GPU NVLink/NVSwitch topology — this box has one GPU, no NVLink |
| 2512.16056 | MultiPath Memory Access: host-GPU bandwidth | die | Multi-GPU/multi-path hardware we don't have |
| 2607.28633 | Topology-aware data movement for disaggregated GPU inference | die | Multi-node disaggregated serving, not a single-box topology |
| 2508.19670 | IOMMU interference in mixed-criticality systems | die | ARM SMMUv2 target, not x86/VT-d |
| 2504.18714 | Memory tiering through parameter tuning | die | CXL-tiered-memory hardware we don't have |
| 2510.22869 | Jenga: tiered memory without thrashing | die | Same — CXL tiering |
| 1911.09829 | Leap: prefetching remote memory | die | RDMA-backed remote memory, not local NVMe/DDR |
| 2603.05162 | RESYSTANCE: LSM compaction via eBPF | die | Database compaction workload, not this box's I/O pattern |
| 2607.02969 | Cross-IP request coalescing (virtualized I/O) | die | VM fan-out problem, not this box's direct-attach path |
| 2608.08751 | InSituANN: PCIe-efficient vector search | die | Vector-DB workload, wrong domain |
| 2306.05701 | CAWL: cache-aware write performance model | die | Write-heavy database model; this box's hot path is read-dominated model load |
| 2207.13685 | THP with FLASH (astrophysical sim) | die | HPC Fortran workload, marginal gains, wrong architecture generation |
| 2606.15137 | uringscope: io_uring observability | keep as tool, not patch | Real, public, useful for instrumenting the L2 poll_queues bench — not a kernel change itself |
| 2505.23072 | fastsafetensors: faster model loading | die | We serve GGUF via llama.cpp, not safetensors — wrong loader |
| 2109.05366 | Readahead prefetcher for GPU filesystem layer | die | Dead GPUfs research-OS lineage, no mainline surface |
| 2605.03375 | Tutti: SSD-backed KV cache for LLM serving | die | KV-cache-bound design; our dense model (4 KV heads) is weight-bound, not KV-bound — same reason `2604.26968` died in the earlier survey |
| 2506.06472 | Lifetime-aware tensor offloading via GPUDirect Storage | die | Training workload, multi-GPU/multi-SSD, not single-box inference |
| 2409.10946 | Skip TLB flushes for reused pages in mmap | die | No public code, invasive mm/TLB correctness surface — high risk, unverifiable locally |
| 2102.12922 | XRP: BPF for storage (OSDI '21) | die | 2019-era design, superseded by later in-kernel-function work; never landed upstream in this form |
| 2607.13604 | MARS: staged buffered reads | parked, unchanged | Still T17's verdict — authors haven't released code as of this sweep |
| 2006.12144 | Scalable range locks for address spaces | die, confirmed | `CONFIG_PER_VMA_LOCK=y` already in `build/linux-7.2.2/.config` — mainline per-VMA locking (landed ~6.4-6.6) already solves the `mmap_lock` contention problem this 2020 paper targets |

19 die (18 above + 2006.12144), 1 stays parked (MARS, no change), 1 becomes a
tooling note (uringscope) rather than a patch candidate. **4 survive for a
full docling read.**

---

## 2. The four full reads

### 2512.12615 — gpu_ext: Extensible OS Policies for GPUs via eBPF

Real hardware (RTX 5090, P40), real workloads (PyTorch, vLLM 0.11.0,
llama.cpp, Faiss), measured not simulated. Extends **NVIDIA's open GPU
kernel modules** — the same driver family this box already runs
(`nvidia-driver-610-open`) — with `struct_ops`-style host-side hooks
(`gpu_mem_ops`: activate/access/evict_prepare/prefetch;
`gpu_sched_ops`: task_init/task_destroy) plus a device-side eBPF runtime
that JIT-compiles a restricted eBPF subset into PTX and runs it inside GPU
kernels via binary trampolines.

**Directly relevant, measured numbers on our exact workload class**
(llama.cpp MoE expert offload under VRAM oversubscription, vLLM KV-cache +
weight offload under oversubscription — this is GreenBoost's own problem):

- GPT-OSS-120B MoE (59 GiB) on RTX 5090 (32GB), 1.84x oversubscription:
  **4.8x decode speedup** over framework-managed CPU offload (`ncmoe`),
  because gpu_ext operates at page granularity with stride-prefetch + LFU
  eviction instead of migrating whole experts as atomic units.
- Qwen-30B FP8 MoE + KV-cache growth to 36-40GB on 32GB card: **1.7-2x
  better TTFT, 1.3x better decode throughput** than vLLM's CPU-offload,
  via adaptive prefetch that treats KV-cache (temporal locality) and expert
  weights (stride locality) differently instead of one LRU for both.
- Host-runtime overhead with hooks attached but no policy loaded: **<0.2%**.

**Not a mainline Linux patch candidate** — it targets NVIDIA's out-of-tree
open kernel modules, not upstream `mm/` or `drivers/`, so it's outside this
project's LKML submission track entirely. **But it's the single most
directly-applicable finding in this whole sweep to GreenBoost's own design
problem** (VRAM-oversubscription tiering for local LLM inference on exactly
this hardware class). Code availability for `gpu_ext` itself is not
confirmed in the paper text (no explicit repo link found); the JIT
substrate it builds on, `bpftime`, is public
(`github.com/eunomia-bpf/bpftime`).

Also matches T14/P3's earlier conclusion about the right hook shape for any
future GreenBoost eviction/prefetch policy: safe fallback under memory
pressure (kernel retains eviction authority even with a bad policy loaded),
zero cost until a program is attached, no new UAPI needed.

### 2605.26168 — LearnedCache: eBPF perceptron eviction on cache_ext

Single-author (Zejia Qi), built directly on cache_ext (already forward-ported
here as patch 0023). Two policies: `ml_protect` (cheap — FIFO with a
model-based reprieve check at the tail) and `ml_rank` (expensive — oversample
N pages, rank by model score, evict the worst).

**Real numbers, but with real caveats stated honestly by the paper itself:**

- `ml_rank` at 30x oversampling beats MGLRU by **up to 40%** on YCSB E
  (LevelDB/RocksDB key-value workload, Zipfian access), and by ~13% on the
  two write-heavy workloads (A, F) where access frequency alone isn't
  predictive.
- `ml_protect` **underperforms** the kernel's own heuristics on most YCSB
  workloads — it can only improve FIFO's decisions, and FIFO is a bad fit
  for YCSB to begin with.
- Under real production cache pressure (Twitter traces), `ml_rank` is
  **"wholly infeasible"** — the paper's own words — due to oversampling
  overhead; only the cheap `ml_protect` was tested there, and it just ties
  with or narrowly beats cache_ext's own heuristics (LHD/S3-FIFO), it
  doesn't dominate.
- No code-release statement found for LearnedCache's own eBPF programs
  (unlike cache_ext, which has an explicit repo link) — treat as
  **unconfirmed public**.

**Workload mismatch for this project**: LearnedCache is tuned and evaluated
against LevelDB/YCSB (random KV-store access) and Twitter CDN traces, not
against sequential model-weight loading. The *technique* — collect
per-page/per-inode access-delta features via eBPF ring buffer, train a tiny
quantized-integer perceptron offline, load weights into eBPF array maps,
score folios with unrolled `PROCESS_FEATURE` macros at eviction time — is
real, working, and directly portable. The specific trained models are not
useful to us; the pipeline is.

### 2502.02750 — "Cache is King" / cachebpf → this is cache_ext's own precursor

**Corrected from the fork's flag.** The fork read this as a *different*
project from patch 0023's cache_ext (different framework name "cachebpf" vs
"cache_ext") and was right to flag it for verification. Reading the full
paper resolves it: **identical author list** — Tal Zussman, Ioannis
Zarkadas, Jeremy Carin, Andrew Cheng, Hubertus Franke, Jonas Pfefferle, Asaf
Cidon, same as patch 0023's own `Original design and implementation:`
citation — and a `struct_ops` hook set (`policy_init`/`evict_folios`/
`folio_added`/`folio_accessed`/`folio_removed`) that matches patch 0023's
own hooks (`init`/`evict_folios`/`folio_added`/`folio_accessed`/
`folio_evicted`) one-for-one except a naming polish on the last hook. This
is the arXiv preprint (Feb 2025, Columbia/IBM) of the same system that was
later published as the SOSP '25 paper cited in patch 0023
(`doi.org/10.1145/3731569.3764820`, which resolves straight to ACM DL — no
arXiv version of the final camera-ready exists to fetch instead).

**So this genuinely is usable as "the cache_ext paper"** for understanding
what patch 0023 implements and why — with the caveat that exact numbers may
have shifted slightly between this Feb-2025 preprint and the Oct-2025 SOSP
camera-ready.

Real measured numbers, CloudLab hardware (16-core AMD Rome, 128GB RAM,
480GB SSD), Linux v6.6.8:

- MRU policy on a ripgrep file-search workload (scan-heavy, the case LRU is
  known to handle badly): **~2x faster** than both default LRU and MGLRU.
- LFU on YCSB/LevelDB: **up to 37% better throughput**, up to 55% better P99
  latency than default; beats MGLRU by an even larger margin.
- Application-informed GET-SCAN policy (two eviction lists, one per request
  type): **1.70x throughput, 57% lower P99** for the prioritized GET
  traffic, at an honestly-stated **18% throughput cost** to the
  deprioritized SCAN traffic — a real tradeoff, not a free lunch.
- Twitter production traces: **no single policy dominates** — confirms the
  same "no one-size-fits-all" finding LearnedCache also reports.
- Overhead: memory 0.4-1.2% of cgroup size (valid-folios registry), CPU at
  most 1.7% (fio randread microbenchmark, no-op policy attached).

### 1912.06998 — Faster than Flash (KAIST, ULL SSD polling study)

2019-2020 hardware (800GB Z-NAND prototype, Intel 750 NVMe reference), not
current-generation, but directly answers the L2 (`nvme.poll_queues`)
question with a mechanism that doesn't depend on the specific device:

- Polled-mode completion cuts I/O latency **up to 33%** vs interrupt-driven,
  averaged across block sizes.
- **The cost**: `blk_mq_poll()` + `nvme_poll()` alone consume **84% of
  total kernel-side CPU cycles** under pure polling — effectively pinning
  one full CPU thread to busy-spin on completion queues. Hybrid polling
  (sleep-then-poll) only trims this to ~56-58% utilization, still **2.2x**
  the CPU cost of interrupts, and is frequently *worse* on latency (5%)
  because its sleep-duration estimate is inaccurate under real access-time
  variance (GC, ECC, DRAM-cache misses).
- The paper's own conclusion is a caution, not an endorsement: polling pays
  off only when device-level latency is comparable to interrupt/
  context-switch overhead (their Z-NAND: ~3µs read). **This box's real NVMe
  latency, measured today with `bench-nvme.sh`, is 189µs at QD1** — two
  orders of magnitude higher than the device class this paper studied, and
  much closer to typical interrupt-service overhead already.

**Implication for L2**: this is a real, measured reason to expect
`nvme.poll_queues>0` to be a **net loss** on this box specifically, not just
an untested lever. It's a shared workstation running inference, gaming, and
VMs concurrently — dedicating a full thread to busy-polling competes
directly with all three, for a device whose latency profile doesn't match
the regime where polling wins. Recommend **testing it anyway** (the bench
harness makes this cheap) but going in with the expectation the result is
negative, and reporting a negative result if it is one — that's still the
honest deliverable per this repo's evidence rule.

---

## 3. Phase 4 patch-candidate ranking — revised after actually reading the papers

Original ranking (from the plan, written before any paper was read):
1. 0015 NVMe APST (already written)
2. cache_ext-based LearnedCache-style policy
3. gpu_ext-shaped hook
4. mTHP/NVMe default changes (treat with suspicion)

**Revised:**

1. **0015 NVMe APST** — unchanged in rank, but its status changed underneath
   it: already sent (4 times, a bookkeeping failure, see `SUBMISSION.md`),
   already drew a real technical objection from the person who wrote the
   algorithm it changes. This is now a reply-and-possibly-revise task, not a
   new-patch task.
2. **Blocked, measured 2026-08-31 — not just unmeasured, structurally dead
   on this kernel's default config.** Live-tested patch 0023 with the real
   upstream FIFO policy (`diagnostics/measure-cache-ext.sh`): `folio_added`
   fires correctly (17,559 confirmed calls on real I/O), but `evict_folios`
   never fires — MGLRU's `shrink_lruvec()` returns before reaching
   cache_ext's hook (`mm/vmscan.c:6157-6161`, confirmed against the real
   source), and this box runs MGLRU by default. Forcing real reclaim
   without that hook produced a contained cgroup-OOM kill instead of clean
   eviction. Full writeup: `docs/tasks_patches.md` T18. This candidate is
   parked until/unless MGLRU-disabled operation is separately evaluated —
   see T18 for why that's not a decision to make in passing.
3. **gpu_ext's hook shape, not gpu_ext itself, as a reference design for
   any future GreenBoost eviction/prefetch eBPF hook** — not an LKML
   candidate (targets NVIDIA's OOT driver), but the strongest evidence yet
   that the `struct_ops` + safe-fallback + zero-cost-until-attached pattern
   T14/P3 already concluded was right actually delivers (4.8x, real
   hardware, our exact workload shape) when built well. Worth reading in
   full if GreenBoost's own tiering hooks are ever redesigned.
4. **L2 `nvme.poll_queues`** — downgraded from "measure it, might be
   positive" to "measure it, expect negative." `1912.06998`'s CPU-cost
   finding is a real, mechanism-level reason, not a guess.
5. **mTHP / NVMe default changes** — unchanged: treat with suspicion,
   these ship as fragments in `configs/fragments/`, not LKML patches, per
   the original plan's reasoning (0016's lesson about changing a compiled
   default from one machine's data).

---

## 5. Round 2 — 25 more papers (per owner instruction: "if you find no patches, fetch 25 more")

Round 1 found a real *direction* (a cache_ext policy trained on real access data)
but zero ready-to-write patches, so round 2 ran. Different angles this time:
io_uring zero-copy, DAMON/access-pattern reclaim, zswap/zram, sched_ext
scheduling, KVM/paravirt, sequential readahead, MoE expert-cache tiering,
eBPF memory-management techniques.

### 5a. Triage — 21 of 25 die or stay reference-only

| arXiv | Title | Verdict |
|---|---|---|
| 2512.04859 | High-Performance DBMSs with io_uring: a practical guide | reference guide, not a research finding or patch |
| 2307.16693 | Pome: parallelizing LSM-tree I/O | wrong domain (database) |
| 2411.16254 | Asynchronous I/O — With Great Power... | perspective essay, no new mechanism |
| 2601.16448 | Ringmaster: TrustZone TEE syscall juggling | wrong platform (ARM TrustZone) |
| 2511.08297 | Function-as-Subtask DAG scheduling | wrong domain (serverless FaaS) |
| 2605.02377 | Unfair by design: eBPF DB scheduling | **keep as reference** — real sched_ext-based isolation of latency-critical vs background tasks; relevant to this box's inference+gaming+VM concurrency, not read in full |
| 2602.09345 | AgentCgroup: OS resources of AI agents | out of scope for this sweep (interesting, not GPU/PCIe/NVMe) |
| 2511.11628 | Mixture-of-Schedulers (ML routing) | not kernel-level |
| 2304.01721 | Virtio-FPGA | no FPGA on this box |
| 2410.00395 | IaaS cloud virtualization performance | thin/generic |
| 2607.17518 | Page-cache side-channel leakage across containers/VMs | wrong threat model — single-user desktop, not shared cloud tenancy |
| 1905.08493 | SvTPM | not relevant |
| 2501.00068 | RL-based storage optimization | thin/generic |
| 2111.11554 | KML: ML for storage systems | thin/generic |
| 2407.07850 | Grace Hopper CPU-GPU unified memory | wrong hardware (ARM+H100, not Raptor Lake+Blackwell) — but confirms gpu_ext's Rule 2/3 coherent-link reasoning independently |
| 2303.05919 | eBPF-based working-set-size estimation | **keep as technique reference** — cheap WSS estimation via eBPF vs. self-ballooning; could inform GreenBoost's own heat detection, not a kernel patch |
| 1903.07038 | Compiler-assisted big.LITTLE scheduling (2019) | superseded by what's already active here (BORE + SCHED_MC_PRIO/ITMT) |
| 2503.11665 | NVMe Flexible Data Placement flash caches | hardware doesn't support it — FDP needs cooperating enterprise SSD firmware; the DRAM-less consumer Samsung 990 EVO Plus almost certainly doesn't implement the FDP command set |
| 2511.00321 | CXL-enabled KV-cache beyond GPU limits | no CXL hardware; also KV-cache-bound (our model is weight-bound) |
| 2602.08585 | Task-agnostic KV cache eviction | application-level, KV-cache-bound — wrong bottleneck for our dense 4-KV-head model |
| 2512.14946 | EVICPRESS: KV-cache compression+eviction | same — KV-cache-bound, wrong bottleneck |
| 2605.18825 | Semantic-aware eviction for LLM prefix caches | same — KV-cache-bound, wrong bottleneck |

`2503.18292` (Jenga: heterogeneous LLM memory management, vLLM/Berkeley team)
is genuinely relevant to this box's real model architecture (3 Gated-DeltaNet
layers per 1 full-attention layer — real embedding/attention heterogeneity),
but it's a vLLM serving-framework paper, not a kernel patch. Worth reading for
`gb-synapse` serving-config decisions, out of scope for this document.

### 5b. Two full reads

#### 2608.12103 — Who Should Own the Expert Cache? Kernel-Managed Tiering for Trillion-Parameter MoE Inference

**The single most consequential finding of both rounds, and it points at
GreenBoost, not the kernel.** Rigorous empirical paper (Waterloo +
independent researchers, arXiv-only as of this sweep — not yet venue-reviewed,
flagged as such), real GH200 hardware, three router traces (128-896 experts/
layer, including a production 1.45 TB trillion-parameter model), three
independent capacity-enforcement mechanisms, two rounds of adversarial
self-audit that each caught and corrected the paper's own earlier errors (a
Python-harness artifact, a silently-dead balloon) — the kind of
methodological honesty this repo's own MUST-RULE #3 asks for.

**The finding:** letting the kernel page cache own eviction for an
oversized weight pool — `mmap` it, strip `F_NOCACHE`/uncached-read flags,
use `fadvise(WILLNEED)` for router-predicted prefetch hints, let LRU/MGLRU
handle reclaim — matches or beats a hand-tuned userspace pinning cache
(the "incumbent design" every MoE-offload engine uses: MoE-Infinity,
PowerInfer, LLM-in-a-Flash, DeepSpeed-Inference). Measured:

- Admission to the page cache is **free on the miss path** — a page is
  already paid for by the read; refusing to cache it (the `F_NOCACHE`
  pattern) forfeits every future hit for zero benefit. Enabling admission on
  a repeat-read microbenchmark: **5.3x** (13.1 → 68.9 GB/s).
- Equal-memory, apples-to-apples comparison (enforced via cgroup wall, not
  inferred from a balloon — the paper's own earlier draft got this wrong by
  trusting a balloon that had silently died): the oracle-pinned userspace
  arena beats the page cache by only **1.09x**, and that gap is pure kernel
  lookup/reclaim overhead, not fewer misses — device bytes read are nearly
  identical (7.4 vs 7.2 GB/iteration).
- Kernel recency (plain LRU/MGLRU) **matches** a same-domain frequency
  oracle at host-tier budgets (75.3% vs 74.6% hit rate) and **holds
  70-71% off-domain** while the frequency oracle collapses to 21-34% —
  "a deployed pin set is always somebody's yesterday."
- End-to-end, in a real production engine on GH200: admitting the cache is
  worth **1.09-1.10x** in steady decode time, token-identical output
  verified by SHA-256 across balanced A/B/BA pairs.
- A genuine methodological finding for anyone benchmarking cache capacity
  on this project's own box: **ballooning + MGLRU together can overstate
  device traffic at the pressured end by ~2x** versus a cgroup wall or a
  real `mem=` boot at the same nominal capacity — the two mechanisms
  disagree specifically when memory is mostly one giant `mlock`ed
  anonymous region, which is exactly GreenBoost's own T1 pinned-arena shape.
  Worth remembering before trusting any future capacity sweep that uses
  memory ballooning to emulate a smaller machine.

**Not a kernel patch — the paper's own Algorithm 2 explicitly requires zero
kernel modification.** It is a direct, measured argument about how
`gb-synapse` should read model weights.

**Resolved, 2026-08-31, by reading GreenBoost's own code rather than
assuming.** `gb_synapse_backends.py:997` adds `--no-mmap` unconditionally at
first — comment: *"DMA-BUF pinning (Path A) is incompatible with
mmap-backed pages"* — but `:1133` removes it again whenever the CUDA shim
isn't loaded (`if "LD_PRELOAD" not in env: cmd.remove("--no-mmap")`), with
its own comment stating the exact tradeoff this paper measures: *"mmap lets
the page cache serve a 20 GB GGUF instead of re-reading it from disk on
every restart."* So `--no-mmap` is not GreenBoost blindly repeating the
"incumbent design" this paper criticizes (F_NOCACHE applied by theory, not
by measurement) — it is a real, narrower constraint: DMA-BUF pinning needs
kernel-unmovable pages, and mmap-backed pages are movable. The two are
mechanically incompatible, not a policy choice GreenBoost got wrong.

The paper's own §8 Rule 3 predicts and explains this split without anyone
having to measure it fresh: *"On PCIe machines with no coherent read path
the adopt row disappears and the calculus collapses to own-or-restream — the
regime where user-space HBM caches genuinely earn their complexity."* The
paper's own regime is a GH200 with a coherent NVLink-C2C host-GPU link,
where the kernel page cache and a framework-owned allocation read at
identical bandwidth (its companion submission's "three-condition execution
contract"). This box's RTX 5070 is PCIe-attached with no such coherent
path, so GreenBoost's Path A pinning is exactly the "own" branch Rule 3
says a PCIe machine should take. **No GreenBoost bug, no patch — the
existing conditional is already the theoretically correct answer for this
hardware class**, confirmed by reading the code rather than assuming the
paper's numbers port. Not measured end-to-end on this box, since the
mechanism itself is verified correct at the source-code level.

#### 2409.11220 — eBPF-mm: Userspace-guided memory management with eBPF

Directly on-target for the L1 mTHP problem — a **new eBPF hook point in
Linux's page-fault handling path** that lets userspace supply per-region
"expected benefit of promotion to size X" profiles (64KB/2MB/32MB), so huge
page decisions can be made per memory-region instead of via one global
system default. Preliminary SPEC CPU2006 `astar` results show competitive
performance to full THP while backing only a fraction of the address space
with 2MiB pages. Zero overhead on unhinted faults (same "changes nothing
until attached" shape as cache_ext/0019/0020).

**Parked, not pursued — same reason MARS was parked in T17.** This is a
2-page NTUA workshop poster (Mores, Psomadakis, Goumas), preliminary results
on one benchmark, and **no code-release statement or repo link found**.
Reimplementing a new kernel hook point from a 2-page abstract with nothing
to check correctness against is the same risk profile T17 already declined
for MARS, just at a smaller scale. Re-open if the authors publish code or a
full paper.

---

## 7. Mailing-list reconnaissance, 2026-08-31 — before writing anything else

Per owner instruction: read the actual target lists before any future patch,
not just the papers. **Correction: `lore.kernel.org` is not reliably blocked.**
`SUBMISSION.md` said it sits behind an Anubis wall that refuses `curl` and
automated browsers. Live-tested today: a bare `curl` gets a 200 on the first
request, then 403s on the next few from the same source (rate-limiting, not a
hard wall) — `hyperresearch fetch`'s browser-backed provider gets through
cleanly every time. Use that, not raw `curl`, for any future list check.

**0015's thread (linux-nvme) has moved since the last check.** The drafted
reply (`replies/0015-alexey-reply.txt`) is **already sent and live** —
2026-08-31 13:17 UTC, message-id `20260831131701.200793-1-ferran.duarri@me.com`,
recorded in `SUBMISSION.md`. No reply to it yet as of this check.

**0019's thread (dri-devel) has not moved.** Still ends at Christian König's
2026-08-25 message; the drafted reply (`replies/0019-koenig-reply.txt`) is
still unsent. Nothing new to report there beyond what was already known
(the sashiko-bot review comment about `_IOR`/`pad` validation is the same one
`SUBMISSION.md` already cites).

**The load-bearing finding: `cache_ext` has never been submitted to LKML.**
Searched `lore.kernel.org/all/?q=cache_ext` — every result is a low-relevance
(0-4%) tangential match, none of them cache_ext's own patch series. Its
authors published SOSP '25 and a GitHub fork
(`github.com/cache-ext/linux-cache-ext`, "their v6.6.8 kernel fork" per
patch 0023's own commit message) — not an LKML submission. **This changes
what "a cache_ext policy" as a Phase-4 patch candidate actually means**: it
is not "propose an eBPF policy to run on an already-upstream mechanism." The
mechanism itself is out-of-tree, forward-ported by us, and would need its
own upstream case made before anything built on top of it could go to LKML
as anything other than an RFC explicitly flagged as depending on
not-yet-upstream infrastructure — the same "no in-tree user" shape that
already killed 0019.

**What linux-mm is actually doing about this problem right now**: an active
RFC series, `[PATCH RFC] mm/mglru: frequency guided promotion (MGLRU-FG) and
flag cleanup` (Kairui Song, with Joanne Koong reviewing, threads from
2026-07-22 through 2026-08-03), extends **MGLRU natively** with
frequency-guided promotion and refault-distance support — a mainline C
answer to the same "one-size-fits-all eviction is wrong for some workloads"
problem cache_ext/cachebpf/LearnedCache all target via eBPF. This is the
real current state of play on linux-mm@: the community's preferred direction
right now is extending MGLRU in C, not adding an eBPF hook point. Any future
proposal in this space should be framed relative to MGLRU-FG — either as
complementary (an escape hatch for policies MGLRU-FG structurally can't
express) or should explain why eBPF is the better mechanism, not ignore that
this thread exists.

---

## 9. Round 4 — strategy redirect, then ~100 more papers scanned

Per owner instruction after round 3's negative result: redirected from "search
arXiv for frameworks" to "hunt for small narrow bugs specific to this exact
hardware" — full local boot-audit re-review (nothing kernel-patchable left,
everything real was already fixed today in wizard/systemd code, not kernel
code) plus 8 targeted `lore.kernel.org` searches across the exact silicon on
this board (Samsung 990 EVO Plus, GB205/`10de:2f04`, i9-14900KF, ASRock
B760M-ITX/D4, `iwlwifi` `8086:7a70`, Raptor Lake HDA `8086:7a50`). Nothing
usable — the one real lead (nova-drm's GB205 chipid detection thread) isn't
safely testable without risking the production GPU driver stack.

Redirected back to literature per owner instruction ("fetch 75 more"), this
time also using OpenAlex and arXiv's date-sort (not just relevance-sort,
which was hiding recent submissions) — roughly 100 more titles scanned across
25+ search categories (dm-crypt, ext4, cgroup v2, IOMMU, PREEMPT_RT, Wayland,
thermal, consumer-GPU UVM, RCU, kernel fuzzing, memory encryption, Spectre
mitigation overhead, NVMe HMB, DRAM-less SSD FTL, io_uring correctness,
Nouveau/Nova, virtio-balloon, BPF LSM, live patching, GPU driver comparison,
persistent-memory filesystems, recent cs.OS submissions). Full triage below.

### 9a. Dies quickly — wrong hardware class or superseded

SplitFS, NVCache (persistent memory / NVDIMM — no such hardware on this box),
software-only device passthrough (2015, superseded by the IOMMU/VT-d we
already use), Next4 ext4 snapshots (solved differently already — this root
is on LVM, which already does snapshots at a different layer), MetaCache
(2014, too old/thin), vTensor (real 1.86-3.92x speedups but vLLM/CUDA-API
library code, not a kernel patch, and GreenBoost serves via llama.cpp not
vLLM), ProbeLogits (targets "Anima OS," a from-scratch Rust kernel — not
Linux at all), Concordia (GPU-resident checkpointing for datacenter-scale
fault-tolerant serving — wrong scale, this box isn't a long-running
distributed service), TME-Box (Intel TME-MK cloud multi-tenant sandboxing —
wrong threat model for a single-user desktop), RCU formal-verification
methodology (2016, produces no patch), the kernel-fuzzing/crash-triage
cluster (KernelGPT, kAgent, LockDoc, etc. — testing infrastructure, not a
patch), LMS-AR and Xkernel (both real, both require adopting substantial new
out-of-tree infrastructure — same "needs adoption first" pattern that killed
cache_ext/gpu_ext).

### 9b. Real, evidenced, but wrong scale for this box

**"Mitigating context switching in densely packed Linux clusters with
Latency-Aware Group Scheduling"** (Cambridge, real ~300-line patch to
`kernel/sched/fair.c` against v5.18, public code, real evaluation on Intel
Xeon + AMD EPYC hardware). CFS-LAGS trades strict per-cgroup fairness for
faster run-queue drainage, cutting scheduling overhead from up to 28% to
single digits under **100+ colocated Kubernetes function cgroups per node**.
Genuinely good work — but the overhead it fixes only appears beyond ~8x
hardware-thread colocation density (96+ cgroups on a 12-thread machine).
This box runs 3 concurrent workloads (gaming, inference, VM), not 100+
serverless functions. Wrong scale, not wrong quality — the problem this
patch solves does not exist here.

### 9c. Real, GreenBoost-relevant, not kernel patches

Three papers landed on GreenBoost's own architecture, not the kernel:

**"The Ingestion Tax: Adopting File-Backed Weights in Tensor Frameworks"**
(same Waterloo team as the expert-cache paper) — **the single most decisive
finding of this whole sweep, and it closes an open question from §2's read
of the expert-cache paper.** §7.3 tests file-backed mmap/DLPack weight
adoption on a **NVIDIA RTX 5070 Ti** (same GB205-family chip as this box's
RTX 5070) and finds it **39x slower** (146.9 → 3.79 tok/s) than a real copy
into VRAM, because every mapped read crosses PCIe at 48 GB/s against an
815 GB/s resident kernel. The paper's own words: *"the copy buys a real
placement change and should be retained... topology changes the verdict."*
This is real, measured, on our own GPU model — and it **confirms GreenBoost's
existing `--no-mmap` + DMA-BUF pinning is architecturally correct**, not a
missed optimization. The earlier speculation in §2 ("flagging for a real
measurement on this box, not assuming the numbers port") is now answered:
don't pursue page-cache-as-accelerator-storage on this hardware class, ever
— the same paper that made kernel-owned caching look good on a GH200
explicitly rejects it on PCIe-discrete GPUs, using our exact chip.

**Hummingbird** (SLO-oriented GPU preemption, closed-source-GPU compatible)
and **Nixie** (Duke, RTX 5090, llama.cpp-integrated temporal multiplexing for
consumer GPUs shared across concurrent apps) — both real, both public,
both directly relevant to this box's gaming+inference+VM concurrency, both
implemented entirely as **userspace CUDA-Driver-API interception**
(`cuLaunchKernel`/`cuMemAlloc` hooking), architecturally identical in kind to
GreenBoost's own CUDA shim. Not Linux kernel patches — no driver or kernel
code involved in either. Worth reading in full if GreenBoost's own
multi-workload GPU-sharing logic is ever revisited, out of scope here.

### 9d. Round 4 verdict

No new Linux kernel patch emerged from either the hardware-specific bug hunt
or ~100 more papers across 25+ categories. Total across all four rounds:
~175 papers/threads scanned, 76+ fetched as notes, 12 read in full via
docling. Three GreenBoost-architecture findings of real value came out of
it (the Ingestion Tax confirmation being the strongest), but the specific
ask — a new kernel patch — was not found. That is the honest, complete
result of genuinely exhaustive effort across two fundamentally different
search strategies, run twice each.

---

## 10. What would be dishonest to claim from this sweep

None of the 25 papers, including the 4 read in full, produce a mainline
Linux kernel patch this project can send to LKML. `gpu_ext` and
`cache_ext`/`cachebpf` are the two genuinely strong findings, and both are
either out-of-tree-driver-scoped (gpu_ext) or already in the tree
(cache_ext, as patch 0023, unmeasured). The actionable output of this sweep
is: measure 0023 for real, build a narrow eBPF policy on top of it trained
on this box's real access pattern, and treat `nvme.poll_queues` as a likely
loss rather than an open question. That is a smaller claim than "found
patches for 10x," and it is the accurate one — same shape as every prior
finding in this repo's research history.

---

## 11. Round 5 — 65 more papers, direct arXiv API sweep, targeted at this exact topology (2026-09-03)

Owner instruction: fetch a further batch (verbally "50") aimed specifically at
this box's real topology — i9-14900KF (8P+16E hybrid, single NUMA node),
RTX 5070 (GB205 Blackwell, 12 GB VRAM, PCIe Gen4 x16), 64 GB DDR4, Samsung 990
EVO Plus NVMe, three concurrent workloads (local dense-model inference,
gaming, VMware) — with the explicit goal of five patches. Twelve targeted
arXiv API queries (`export.arxiv.org/api/query`, sorted by submission date,
not relevance, to surface anything from the last few weeks) across hybrid
P/E scheduling, consumer-GPU offload, PCIe P2P DMA, io_uring, zram/zswap,
sched_ext/eBPF scheduling, CPU thermal/power management, speculative
decoding, KV-cache offload, memory tiering, nested KVM, and VRAM-capacity
work. 65 unique papers after dedup, none previously seen in rounds 1-4.

### Triage verdict: same structural result as rounds 1-4, for the same reason

The literature at this scale is dominated by three problem shapes this box
does not have: **multi-GPU / cluster inference** (Harvest's P2P GPU cache,
Petals' collaborative inference — this box has one GPU), **MoE-specific
memory pressure** (RotaryQuant, WiSP, HERALD, AcceptMoE, DraftExpert,
MoBiLE, HeadInfer, CATS — the served model is dense, `general.architecture
qwen35`, no `expert_count`, confirmed in `CLAUDE.md`'s "Local-inference prior
art" correction; none of this offloading-skew machinery applies), and
**CXL-based memory tiering** (TPP, SupMario, the FPGA CXL emulator, the CXL
telemetry limits study — this board has no CXL hardware, PCIe only). A fourth
cluster — cloud/container multi-tenant scheduling (SchedBlame, XLB, Crab,
the agentic-workflow papers) — targets 84-container/96-core hosts, wrong
scale by roughly two orders of magnitude for a 3-workload desktop.

**The `pe_core` query was contaminated by a term collision.** "P-core"/"E-core"
matched physics papers on electronic p-orbitals and group-theory p-cores
(NiX₂ DFT+DMFT, Stingray Patterns of Dominant Weights, X-ray photoelectron
entanglement) — arXiv full-text search has no notion of "the Intel hybrid
core kind of P-core." Zero of that query's 8 results were CPU-architecture
papers. Recorded so the query isn't re-run the same way next time; a query
scoped to `cs.OS`/`cs.AR` categories would avoid this.

### The two candidates worth a full read, and why both still fail

**`2608.13689` — "A Bounded Reclaim Actuator for PSI-Guided Compressed
Memory: A Controlled Ablation."** Closest thing to a kernel-memory-tuning
result in the batch: compares zram-from-boot vs. PSI-triggered zram vs.
zram-from-boot plus a one-time bounded cgroup reclaim request. Read in full
(abstract is the whole finding — this is a short empirical paper). Result:
**6% p99 latency improvement on a compute workload, statistically
indistinguishable on SQLite**, run on nine 1-vCPU cloud VMs. Three problems
for this box: (1) the improvement is not general, the paper's own conclusion
is "depends on the foreground workload and its memory-access path" — exactly
the kind of unproven-on-our-hardware claim this repo's MUST-RULE exists to
stop; (2) 1-vCPU cloud VMs share nothing with a 24-core/32-thread hybrid
desktop's memory pressure profile; (3) the mechanism itself is a **userspace
actuator** watching PSI and issuing cgroup reclaim requests — there's no
kernel code change here to forward-port, only a policy GreenBoost's own
tuner could in principle try, and even that would need this box's own A/B,
not a borrowed 6%.

**`2609.02052` — "SchedBlame: Culprit-Attributed CPU Contention for
Containers on Stock Kernels."** Read in full. Explicitly designed to run on
**unmodified 4.18 and 5.10 kernels** — avoiding a kernel patch is the paper's
stated design goal, not an oversight to fix. It's a diagnostic eBPF tracer
for "which co-tenant stole my CPU," evaluated at 84 containers on a 96-core
host. This box runs three workloads, not eighty-four, and none of them are
adversarial co-tenants competing for attribution — the concurrency question
here (gaming + inference + VM sharing 24 threads) is already handled by
BORE/EEVDF scheduling classes and cgroup weights, not something needing
after-the-fact blame attribution. Reference-worthy if GreenBoost ever needs
to diagnose contention between its own inference workers, but it isn't a
kernel patch, and it isn't sized for this workload either.

### Round 5 verdict

Zero new kernel-patch candidates from 65 more papers, for a fourth consecutive
independent reason category (multi-GPU, MoE-skew, CXL, cloud-container-scale)
beyond the four already documented across rounds 1-4. Combined across all five
rounds: ~240 papers/threads scanned, ~14 read in full. The honest total
remains what round 4 already concluded: no arXiv paper in this project's
literature sweep produces a Linux kernel patch for this specific hardware.
The five-patch goal, where it can be met at all, is met by direct
measurement on this box (Candidate A's x86/PCI NUMA-node fix, plus whatever
survives the `bench-mm.sh`/`bench-nvme.sh` A/B harness), not by literature —
see `docs/tasks_patches.md` and this repo's `CLAUDE.md` plan for the current
tally.
