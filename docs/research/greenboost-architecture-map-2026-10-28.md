# GreenBoost architecture map and PCIe / RAM-as-VRAM review (2026-10-28)

Read-only study of `/home/ferran/Dev/greenboost_all/greenboost` (called "GB" below). Nothing in that repo was built, run or modified. The GPU was busy with the owner's job, so no GPU measurement was taken. A few live, read-only facts were read from this machine (sysfs, `/run/greenboost/shim_stats`, `~/.local/share/greenboost/dataflux.jsonl`).

All paths below are relative to the GB repo unless they start with `/`. `INSTR.md` is the alias used here for the repo-root agent-instructions markdown file (1,170 lines, listed first in the root directory with AGENTS.md). `workflow/` is gitignored in GB but exists on this box, so those references are real but local-only.

## 0. The short version

On this machine (RTX 5070, CC 12.x, Blackwell) the kernel module is not on the weights path at all. The shim sends every overflow allocation to plain anonymous host memory, registers it with `cuMemHostRegister(DEVICEMAP|PORTABLE)`, and hands llama.cpp a device pointer that SM kernels read directly over PCIe. There are no tier moves, no copy-engine traffic and no prefetch for that path. Placement is decided once, at `cudaMalloc` time. Everything the project describes as promotion, eviction, heat, LFRU, double buffering, KV prefetch and GDS is either not on this path, measure-only, or a stub.

Live proof (read from `/run/greenboost/shim_stats` while the owner's job runs): `active_path=blackwell_zerocopy`, `path_a_count=0`, `path_b_count=0`, `t2_overflow_total_mb=4454`, `kv_prefetch_mode=0`. `/sys/class/greenboost/greenboost/status` shows `T2 allocated: 0 MB` and `Active DMA-BUF objects: 0` with `greenboost.ko` loaded.

## 1. Architecture map

### 1.1 Components and where they run

| Component | Where | What it does | Evidence |
|---|---|---|---|
| `greenboost.ko` | kernel | DMA-BUF exporter for pinned DDR (2 MB compound pages), LRU of buffers, evictor workqueue, T3 backing file, watchdog, sysfs, ioctls 1-26 | `greenboost.c:1493-1556` (alloc), `greenboost_ioctl.h:191-326` (ioctls), `AGENTS.md:19` |
| `libgreenboost_cuda.so` shim | userspace, `LD_PRELOAD` into the CUDA app | Intercepts `cudaMalloc`, `cuMemAlloc`, `cuMemCreate`, `cuMemGetInfo`, launches, memcpy; decides tier per allocation; phase detector (MODEL_LOAD, INFERENCE, STEADY, IDLE); cluster fabric client | `greenboost_cuda_shim.c` (11,581 lines), header `:1-126` |
| `libgreenboost_vmm_override.so` | userspace, preloaded ahead of the shim | Makes ggml's dlsym-based loader see the shim's Blackwell overrides | `INSTR.md:244-249` |
| `greenboost_netd.c` / `greenboost_netc.c` | userspace daemon / host client | Cluster fabric (remote feeder GPUs as tier T4). Not used on a single-GPU box beyond a connect probe | `AGENTS.md:20` |
| gb-synapse | userspace Python | Pulls GGUFs, computes placement, launches `llama-server` with the shim, translation proxy (Ollama and OpenAI-style APIs) that also records tok/s | `gb_synapse.py` (6,484 lines), `gb_synapse_backends.py:990-1194` (llama.cpp command line), `gb_synapse_api.py` |
| Orchestrator, supervisor, tuner, control, actuation | userspace Python | Feedback loops over telemetry; gated levers exposed over MCP/A2A | `gb_orchestrator.py`, `gb_supervisor.py`, `gb_tuner.py`, `gb_actuation.py` |
| Dataflux | userspace Python | JSONL event log (`~/.local/share/greenboost/dataflux.jsonl`, 51 MB) and MCP drill-down | `gb_dataflux.py`, `gb_dataflux_kinds.py` |
| Python tiering layer | userspace, PyTorch processes only | `ModelTierManager` (module-level T1/T2/T3 moves), `LayerPrefetcher`, `gb_moe` expert manager, `gb_evict_policy`, stream scheduler | `gb_model_tier.py`, `gb_prefetch.py`, `gb_moe.py`, `gb_stream_sched.py` |
| Quant layer | userspace Python + Triton | `gb_quant` (HQQ plus GemLite Triton GEMM), `gb_quant_tq` (TurboQuant LUT-GEMM), `gb_attn` (TurboQuant KV in PyTorch SDPA) | `gb_quant.py`, `gb_quant_tq.py`, `gb_attn.py` |
| `gb_expert_tier.h` | header-only C99 | LFRU hot-chunk scoring for front-load split | see 3.5: has no caller outside tests |
| eBPF tracer | kernel/userspace | `ebpf/gb_trace.bpf.c` event tracing | `ebpf/` |

### 1.2 How they talk

- App to shim: `LD_PRELOAD`; the shim resolves the real CUDA entry points by `dlsym` (`greenboost_cuda_shim.c:5423-5462`). `gb_cluster.shim_env(workload="llm")` builds the environment (`gb_cluster.py:105-126`, includes `GB_VRAM_FRONTLOAD=1`).
- Shim to kernel module: ioctls on `/dev/greenboost` (alloc, pin user ptr, madvise, evict, heat). On Blackwell the alloc/pin ioctls are not reached for weights (see 2).
- Shim to Python: files and sysfs. `/run/greenboost/shim_stats` (key=value), dataflux events (`shim_decision`, `shim_transition`), `GREENBOOST_*` env vars.
- Python to llama-server: process launch and an HTTP proxy. The proxy is where `tok_s_measured` events are produced (`source: "proxy"`).
- Agents to everything: five MCP servers and an A2A gateway over one verb table (`AGENTS.md:42-62`).

### 1.3 Kernel vs userspace on the Blackwell inference path

Kernel: nothing GB-specific is in the hot path. Pinning is done by the NVIDIA driver inside `cuMemHostRegister`; kernel THP supplies the pages. Userspace: shim allocator, bookkeeping tables, safety floor checks. The kernel module only matters for non-Blackwell (CC < 12) and for the T3 file store.

### 1.4 Triton kernels and where they launch

GB ships no custom Triton kernel in the llama.cpp serving path. llama.cpp uses its own CUDA kernels. Triton is used only on the PyTorch side:

| Kernel | Defined | Launched from | Purpose |
|---|---|---|---|
| `_tq_gemm_kernel` (autotuned, 2/3-bit LUT GEMM) | `gb_quant_tq.py:137-` | `gb_quant_tq.py:346` (`_tq_gemm_kernel[grid]`), wrapped by `TQLinear.forward` `:426` | TurboQuant weight GEMM: codebook gather done as an ALU select tree, scales per 128-K group |
| GemLite kernels (`gemm_kernels.py`, `gemm_splitK*_kernels.py`, `gemv_*` under `third_party/gemlite/triton_kernels/`) | vendored third party | via `gb_quant.quantize_module` (`gb_quant.py:895`, backend selection `gb_kernel_backends.py`) | int4/int8/fp8/nvfp4 weight-only GEMM/GEMV |
| `synapse_engine/gllm/layers/**` (about 40 `@triton.jit`: fused MoE `fused_moe_triton/fused_moe.py:189,209`, FLA/GDN, mamba SSD, fp8, rotary, KV cache kernels) | vendored torch engine "gLLM" | selected only when `import gllm, sgl_kernel` works in the synapse torch venv (`gb_synapse_backends.py:169-200`) | alternate non-llama.cpp engine |
| `gb_attn.py` TurboQuant KV | pure PyTorch ops monkey-patched over `F.scaled_dot_product_attention` (`gb_attn.py:567-1104`) | any PyTorch app | KV compression; no Triton launch found in this file |

None of these touches the tier or PCIe logic. They reduce bytes or FLOPs, not transfers.

## 2. How host RAM is exposed to the GPU today

### 2.1 Mechanism on this machine (CC 12, Blackwell)

`gb_overflow_alloc_ex` has a dedicated branch for `gb_cc_major >= 12` (`greenboost_cuda_shim.c:6687`). It never reaches Path A or Path B code:

1. T2 byte reservation (`gb_t2_try_reserve`, `:6720`, defined `:2671`).
2. Host RAM safety floor from `/proc/meminfo` MemAvailable minus pending managed bytes; default 10% of RAM, minimum 1536 MB, env `GREENBOOST_HOST_RAM_SAFETY_MB` (`:6734-6790`).
3. `gb_vmm_t2_alloc_blackwell_zerocopy` (called `:6803`, defined `:1602-1668`):
   - `mmap(NULL, size, PROT_READ|PROT_WRITE, MAP_PRIVATE|MAP_ANONYMOUS)` at `:1608`. No `MAP_HUGETLB`, no `MADV_HUGEPAGE`, no `MAP_POPULATE`, no `mbind`.
   - `cuMemHostRegister(DEVICEMAP|PORTABLE)` (`:1614`), then `cuMemHostGetDevicePointer` (`:1625`). The returned pointer is a "device VA" that SMs dereference over PCIe.
   - `cuMemAdvise(SET_READ_MOSTLY / SET_ACCESSED_BY)` called on non-managed memory, expected to fail with INVALID_VALUE, kept for an IOMMU side effect, then `cudaDeviceSynchronize` and drain `cudaGetLastError` (`:1648-1672`). This is a synchronous device sync on every T2 allocation and rests on an undocumented side effect (see weak spots).
4. Tracking: `bvmm_ht` open-addressing table, 512 slots, keyed by device VA, type `BVMM_TYPE_ZEROCOPY` (`:1429-1448`, insert `:1635`). Free path does `cuMemHostUnregister` + `munmap`.
5. Fallbacks in order: managed UVM (`gb_vmm_t2_alloc_blackwell_managed`, `:1834`, called `:6812`), then OOM. INSTR.md says the managed path was removed (`INSTR.md:127-128`), but it is still compiled and still a fallback (doc drift, harmless).

Other mechanisms, and whether they are used here:

| Mechanism | Used on this box? | Where |
|---|---|---|
| `cudaHostAlloc` / `cudaMallocHost` | no (pathbench only) | `tests/bench/gb_pathbench.cu:152,202` |
| `cudaMallocManaged` / UVM | fallback only; INSTR.md says it was SM-inaccessible on CUDA 13 Blackwell | `shim:6795-6815` comments |
| `cuMemCreate` VMM HOST_NUMA_CURRENT | only inside the front-load split (`gb_frontload_split_alloc`, `:7548-7730`) and when ggml uses VMM; the shim reports VMM unsupported (attribute 102) so ggml takes the legacy `cudaMalloc` pool (`:9372-9391`) | see open question in 5.9 |
| dma-buf import (`cudaImportExternalMemory`) | no: skipped on CC >= 12 (`:3338`, `:5481`) | |
| Pre-registered slab pool | no: skipped on CC >= 12 (`:1910-1918`) | |
| kmod pinned DDR (`GB_IOCTL_ALLOC`, 2 MB compound pages) | no: `path_a_count=0` | `greenboost.c:1493-1521` |
| Mapped zero-copy reads by kernels | yes, this is the T2 path | above |
| BAR1 CPU writes into VRAM | no: the words BAR1, ReBAR and resizable BAR do not appear in any GB source | grep over `*.c *.py *.md` |

### 2.2 Page sizes

- Kernel module path (not used here): `alloc_pages(GFP_KERNEL|__GFP_ZERO|__GFP_COMP, order 9)` per 2 MB page, 4K fallback (`greenboost.c:1493-1556`). T3-spillable buffers force 4K pages (`:1490`).
- Userspace zero-copy path (used here): whatever THP gives. This box has `transparent_hugepage=always` on the command line but `defrag = defer+madvise`, so an unmadvised fault only gets a 2 MB page if one is free right now; otherwise it falls back to 4K without compaction. `MemAvailable` was 17 GB while the owner's job ran, so fragmentation is plausible. Live `AnonHugePages` is 559 MB system-wide. The shim never verifies what it got.
- 1 GiB: `hugepages-1048576kB/nr_hugepages` is 0 and `HugePages_Total` is 0 (2 MB pool). Never used by the shim in the zero-copy path. A 1 GiB row exists in `gb_pathbench.cu` section 3b (`:225-262`) but no result for it is recorded anywhere (see 4).
- NUMA: one node, so placement policy is moot. No `mbind`/`set_mempolicy` in the shim.
- CUDA side: the page granularity the GPU uses to map a `cuMemHostRegister` range is not observable from GB code; that unknown is exactly what pathbench 3b was written to test.

### 2.3 Allocation tracking

- Userspace: `bvmm_ht` (zero-copy/VMM/managed), `gb_fl_ht` (front-load split VAs, 256 slots, `:7487-7540`), per-pointer flags `ht_set_flags` for KV/T1-priority, atomics for T2 bytes (`gb_t2_overflow_bytes`) and pending managed bytes. Shared state exported to `/run/greenboost/shim_stats`.
- Kernel: IDR of `struct gb_buf`, global LRU, per-buffer `frozen`, `t1_priority`, `session_priority` (`greenboost.c:1565-1590`). Unused for weights on Blackwell.
- Accounting gap: pinned zero-copy bytes are invisible to the kernel module (`T2 allocated: 0 MB`), so the module's watchdog, pressure levels and OOM guard do not see the real T2 usage on this machine. The shim's own host-RAM floor is the only guard.

## 3. The PCIe data path

### 3.1 Per-tier movement on the Blackwell path

| Move | Mechanism | Evidence |
|---|---|---|
| T2 to GPU compute | SM-issued loads from mapped host memory. No copy, no staging, no stream, no overlap. Each token re-reads the T2-resident bytes the kernels touch | `shim:1602-1668`, `tests/bench/gb_pathbench.cu:195-262` |
| T1 to T2 / T2 to T1 | none at runtime. Placement is fixed at `cudaMalloc` | no `cuMemcpy*Async` call exists in the local path; only `gb_batch_flush` (`:6504-6545`, DeviceToHost batch, sorted large first, one stream) and feeder staging (`:10986-11024`) |
| T3 NVMe | kernel module only: per-page 4 KiB `kernel_write`/`kernel_read` loop through the page cache (`greenboost.c:507`, `:636`), single ordered workqueue (`:3679`). Not reachable from the Blackwell path | |
| GDS / cuFile | STUB: `gb_gds_read/write` have zero callers and the handle is never assigned (`shim:96-100`, helpers `:6593-6620`). `nvidia_fs` is loaded on this box, root is on LVM so true GDS is unlikely (`workflow/architecture.md:51`) | |
| io_uring | not used anywhere found | |
| Cluster | TCP fabric to feeders (`greenboost_netc.c`), not relevant to one GPU | |

Streams: none created by the shim (`cuStreamCreate` absent from the shim). Python side creates four torch streams (gemm, transfer, quant, copy) in `gb_stream_sched.py:75`.

### 3.2 Chunk sizes

- Zero-copy: one `mmap` per CUDA allocation, so chunk = whatever llama.cpp asks for. llama.cpp allocates one big weights buffer per device, which is why the shim added the front-load split.
- Front-load split: physical handles in 1 GiB units (`GB_FL_CHUNK_UNIT`, `shim:7487`), device portion filled up to `GB_VRAM_FRONTLOAD_PCT` (default 90) of total VRAM (`:7440`, `:7589`), remainder host-backed via `cuMemCreate HOST_NUMA_CURRENT`, one VA, one `cuMemSetAccess`. Default OFF in the shim (`:7428`) but `gb_cluster.shim_env("llm")` turns it on (`gb_cluster.py:116,126`).
- PyTorch tier moves: whole `nn.Module` per move (`gb_model_tier.py:373-436`); `LayerPrefetcher` is per decoder layer (`gb_prefetch.py:44-62`).

### 3.3 Prefetch and overlap

- `LayerPrefetcher`: lookahead N layers on the "transfer" stream with event hand-off. GB's own sweep found it never beat the no-prefetch baseline: best 9.91 tok/s at keep_resident=32/36 against 15.45 tok/s baseline, lookahead had no effect (`gb_prefetch.py:50-62`, raw rows in `tests/bench/results.jsonl`, dated 2026-06-22, Qwen3-8B, 14 rows). Docstring says not to stack it on the gb_quant-placed path.
- Shim `prefetch_worker`: calls `madvise(MADV_WILLNEED)` on mapped DMA-BUF ranges and optionally the next tile when `GREENBOOST_DOUBLE_BUFFER=1` (`:583-680`, `:537`, `:662`). `MADV_WILLNEED` on pinned, already-resident pages does nothing useful; it only matters for T3 file-backed pages in the kernel path. Default off.
- KV prefetch: `gb_kv_prefetch_tick` measures opportunity only; live promotion "intentionally deferred" (`:2531-2565`). `kv_prefetch_mode=0` live.
- GPU never prefetches zero-copy data into VRAM; there is no overlap to speak of because there is no copy. The only overlap is whatever the GPU's own latency hiding gives SM loads.

### 3.4 Eviction and placement policy: who decides what lives where

1. At alloc time, in order: KV and recurrent state go to T1 first (`kv_reserve`, `GB_ALLOC_KV_CACHE|T1_PRIORITY`, `shim:2300-2340`, `INSTR.md:153-172`). Everything else fills VRAM to about 90% if front-load is on (`INSTR.md:174-196`), the rest goes to T2 (`gb_needs_overflow`, `:6193`).
2. Which bytes land in the T2 tail is decided by allocation order, not heat. For llama.cpp's single big weights buffer, the tail of the buffer (the last tensors in the GGUF) is what ends up host-backed.
3. Kernel LRU/evict (`greenboost.c:3021-3198`) and `GB_IOCTL_SET_HEAT` pushes (`shim:996-1039`) only apply to kmod-registered buffers, which do not exist on Blackwell.
4. Python `ModelTierManager` uses a prioritised LRU (`gb_evict_policy.py`, port of a TensorRT-LLM controller) at module granularity, for PyTorch workloads only. `gb_thrash.py` is observation only.
5. MoE: placement by `--n-cpu-moe` (measured CPU placement, permitted by the 2026-07-26 amendment, `INSTR.md:139-151`) or all-GPU with T2 spill. `_fit_cpu_moe_layers` chooses the count and has an open bug (`workflow/known-issues.md`, item 5: 24 gives 14.1 tok/s, 28 gives 49.7).

### 3.5 Dead or inert mechanisms (OBSERVED)

- `gb_tier_pick_lfru` in `gb_expert_tier.h` has no caller except `tests/c/test_gb_expert_tier.c` (grep over the repo, third_party excluded). Its own header says the shim "sees which byte ranges are touched by cuLaunchKernel argument pointers", but `cuLaunchKernel` (`shim:10083-10105`) only dispatches to feeders and forwards. There is no live chunk heat and no promote/demote swap.
- KV prefetch promotion: measure-only (above).
- GDS: stub (above).
- `GREENBOOST_KV_COMPRESS`: documented no-op (`shim:89-94`).

### 3.6 Compression over the link

Measured and closed:
- `docs/research/pcie-compression-verdict-2026-09-03.md` (this repo): IQ4_XS sample compresses 0.01% with lz4 and 0.18% with zstd-3, so a perfect free decompressor is worth about 0.17% tok/s. Q8_0 mmproj compresses 13% with zstd-3, but nothing served uses it.
- `workflow/pcie-bandwidth-findings.md` (GB, 2026-08-20): IQ4_XS entropy 7.891 bits/byte, 1.002x best case; fp16 KV 1.07x on a synthetic AR(1) fixture only.
- `gb_bench_codec.py` states the break-even (codec must beat the link whatever the ratio).
- Not measured: compression of the MoE model actually served now (APEX-I-MiniPlus, a different quant mix from the IQ4_XS samples), real KV tensors under real prompts, and activation traffic. KV is on-GPU for this serving setup, so there is no KV over the link to compress.

## 4. Measured numbers the project holds

| Number | Value | Source | Age on 2026-10-28 | Caveat |
|---|---|---|---|---|
| Pinned H2D `cudaMemcpyAsync`, Gen4 x16 | 24.4 GB/s | `gb_pathbench.cu` header; `workflow/pcie-bandwidth-findings.md` | 2026-08 | ceiling is about 26 GB/s usable |
| CPU memory bandwidth | 24.2 GB/s | `gb_bench_codec.py` header | 2026-08 | single-stream test |
| SM zero-copy read of host memory | about 24 GB/s, tracks bulk DMA within noise, sequential and 8-of-256 MoE gather | `workflow/architecture.md:50` | 2026-07-26 | measured with `gb_pathbench` |
| SM zero-copy read of host memory | about 11 to 12 GB/s | `workflow/aug19_1720_tasks.md` (GB-K4 "Current"), `gb_pcie_tune.py` docstring | 2026-08-05 | derived from tok/s with a speculative factor later called an artifact |
| Same, re-derived | about 22 GB/s, at least 18.6 | `workflow/known-issues.md` item 2 | 2026-10-01 | "unresolved, not fixed" |
| IQ4_XS-shaped kernel reading host memory | about 19 GB/s | same item | 2026-10-01 | ran while a server held the GPU |
| Dense 27B per-pass bus time | 4.49 GiB per pass, 390 ms on bus vs 17 ms compute | `gb_pcie_tune.py` docstring | 2026-08-18 | contradicts the 24 GB/s figure (4.49 GiB / 0.39 s = 11.5 GB/s) |
| Dense 27B Cold-Fusion IQ4_XS decode | 3.80 tok/s depth 0, 5.91 to 6.03 at MTP depth 4 | `workflow/pcie-bandwidth-findings.md` | 2026-08-20 | spill 4.6 GB |
| IQ3_M instead (spill 2.27 GB) | 3.75 / 3.21 tok/s | same | 2026-08-20 | halving spill gave no gain, mechanism not isolated |
| MoE 35B-A3B decode, `--n-cpu-moe` sweep | 28 gives 49.7, 24 gives 14.1 tok/s | `workflow/known-issues.md` item 5 | 2026-10-02 | mechanism unknown |
| Live proxy tok/s, qwen38-35b-a3b-apex-miniplus | daily median 51.1 (10-04), 48.2, 39.7, 37.9, 55.7 (10-10), 42.5 (10-19), 30.3 at ctx 32768 and 35.3 at ctx 16384 (10-27) | `dataflux.jsonl`, `tok_s_measured` events | up to 1 day | medians of per-request values; prompt length, MTP acceptance (0.22 to 0.71) and shim state differ day to day. The 10-27 values are the lowest recorded. Cause not investigated |
| Live T2 spill for that model | 4454 MB, VRAM physical used 93.2 to 93.8% | `shim_stats`, `shim_decision` event | now | weights 13.73 GB, `-ngl 40` all-GPU override, `mtp` and `vision` on |
| Tier move latency | none recorded | dataflux has zero `tier_move` events (`kinds` list above 54,806 snapshots, 15,375 kernel_backend, 1,163 shim_transition, 529 shim_decision) | | no tier moves happen on this path |
| Lease overhead | about 60 ms per job | `docs/greenboost-audit/measurements.md` | 2026-10-10 | trading workloads, no overflow |
| Shim start-up | +3.1 to +6.2 s per process with cluster probing; 37 ms with `GREENBOOST_CLUSTER=0` | same | 2026-10-10 | peer 192.168.50.246 unreachable |
| PCIe link now | root port `max_link_speed 16.0 GT/s`, GPU supports 32 GT/s, `nvidia-smi` hostmax 4, current 4 under load, sysfs read 5.0 GT/s when idle | `/sys/devices/pci0000:00/0000:00:01.0`, `nvidia-smi` | now | Gen4 is a host-side cap |

Never measured (as far as the repo and dataflux show):
- Zero-copy read rate by backing page size (4 KiB / 2 MiB / 1 GiB). The tool landed 2026-08-19 (`gb_pathbench.cu:225-262`), no result row (`zerocopy_sm_read_*`) exists anywhere in the repo.
- Actual page size of the live T2 buffers (`AnonHugePages` for the registered ranges).
- Zero-copy rate on the real weights buffer of the real model while it decodes, with no other GPU job. Every number above was taken either on a different model, a synthetic buffer, or while something else held the GPU.
- PCIe counters during decode (bytes/s from `nvidia-smi dmon -s t` or `ncu`/nsys PCIe metrics) against tok/s.
- `MaxReadReq` / `MaxPayload` on this link (needs root; `gb_pcie_tune.py report`).
- Effect of the shim's per-allocation `cudaDeviceSynchronize` on load time (it is incurred once per T2 allocation).
- Why the daily median fell from about 50 to about 30 tok/s on the same model between 2026-10-04 and 2026-10-27.
- GDS, T3 and kmod DMA-BUF throughput on this Blackwell card (not reachable).

## 5. Weak spots

1. **T2 is read by SMs on every token with no residency control.** Anything in the T2 tail is re-read over PCIe each token. Placement is by allocation order, not heat (3.4). The project's own LFRU code is dead (3.5).
2. **THP not requested.** `mmap` without `MADV_HUGEPAGE` or pre-fault on a system with `defrag=defer+madvise`. Page size of the registered range is unknown and unrecorded (2.2).
3. **Contradictory bandwidth beliefs.** 24 GB/s (2026-07-26) vs 11 to 12 GB/s (2026-08) vs 18.6 to 22 GB/s (2026-10-01); two repo docs disagree and the resolving experiment is written down but not run (`workflow/known-issues.md` item 2).
4. **Synchronous device sync per T2 allocation.** `cudaDeviceSynchronize` plus an error drain loop after the `cuMemAdvise` calls (`shim:1648-1672`). The comment says the advise calls fail by design and are kept for an IOMMU side effect. That claim has no test in the repo that I found; it may be historical. It serialises model load and is a candidate to measure and possibly remove (risk: the comment says SM access fails without it).
5. **Kernel module blind to Blackwell T2.** `T2 allocated: 0 MB` while 4.4 GB is pinned, so kmod watchdog and pressure logic don't see it. Only the shim's host-RAM floor protects the machine. On a 64 GB box with the owner's other jobs, that matters.
6. **Pageable copies labelled non-blocking (PyTorch tier moves).** `gb_model_tier.py:400,426` call `module.to("cuda"/"cpu", non_blocking=True)`; there is no `pin_memory` or `cudaHostRegister` anywhere in that file. Non-blocking only overlaps when the host side is pinned; pageable H2D goes through the driver's staging buffer at lower rate. INFERRED from standard CUDA behaviour; not measured here.
7. **Per-page 4 KiB T3 I/O in the kernel.** `kernel_write/kernel_read` per `PAGE_SIZE` in a loop (`greenboost.c:507`, `:636`), one ordered workqueue. Explains the 1.8 GB/s figure; a 990 EVO Plus can do several GB/s. Not reachable from the Blackwell path, so it is only a problem for non-Blackwell or if T3 is ever wired to this path.
8. **GIL/Python in the loop.** The proxy (`gb_synapse_api.py`) sits in the request path and counts tokens. `gb_moe` and `gb_prefetch` use per-module forward pre-hooks in Python (`gb_prefetch.py:265-268`), so per-layer decisions run on the interpreter inside the decode loop. Not on the llama-server decode path itself.
9. **Open question on the front-load split.** `GB_VRAM_FRONTLOAD=1` makes the shim back the host remainder with `cuMemCreate(HOST_NUMA_CURRENT)` plus `cuMemSetAccess` (`shim:7548-7730`), while a comment in the main allocator says that exact combination gives DMA-only, SM-inaccessible pointers on Blackwell PCIe. The live state reports `active_path=blackwell_zerocopy` and `n_items: 6` for the T2 decision, so the zero-copy path is what ran at the last observation, and whether the split ever ran on this machine can't be determined from the data I read. If the split silently declines, the "fill VRAM to 90%" rule is satisfied only by allocation order.
10. **Doc drift.** `INSTR.md:127` says Path C (UVM) was removed; the shim header still lists it as the Blackwell primary (`shim:22-27`) and the code keeps it as fallback. `AGENTS.md` and `workflow/architecture.md` describe DMA-BUF zero-copy as the T2 mechanism; on this card it isn't.
11. **GB-K4 benchmark never published.** The single experiment that decides whether page granularity matters has a tool and no result.
12. **MoE placement bug.** `_fit_cpu_moe_layers` picks 24, which decodes 3.5x slower than 28; unexplained, candidates include a compute-buffer allocation falling into T2 (`workflow/known-issues.md` item 5).
13. **Placement tension.** The owner rule forbids CPU offload, but the best measured MoE number (49.7 tok/s) is `--n-cpu-moe 28`, while the current live configuration is all-GPU with a 4.4 GB T2 spill at 30 to 40 tok/s. The two measurements are on different days and contexts, so this is a hypothesis, not a finding.

Sanity check that ties several numbers together (INFERRED): a 3B-active MoE at Q4-class reads on the order of 1.7 GB per token. If the T2 tail holds about a third of the layers (4.4 of 13.7 GB), roughly 0.5 GB per token crosses PCIe. At 24 GB/s that is about 23 ms, a ceiling near 43 tok/s, which is close to the live 30 to 50 tok/s band. If right, tok/s on this model is PCIe-bound and bytes per token is the number to attack.

## 6. Ranked hypotheses (at most 10)

Tags: OBSERVED means the code gap or fact was seen directly; INFERRED means derived from observed facts plus CUDA/kernel behaviour; UNVERIFIED means plausible but nothing here supports it yet.

Before-and-after protocol for all of them (needs the GPU idle, i.e. not while the owner's job runs): serve the same model, same GGUF, same ctx, same MTP settings, 5 runs of the same fixed 512-token generation, record tok/s from the llama-server timings, plus PCIe RX bytes with `nvidia-smi dmon -s t -c 60 -d 1` during decode and `grep -E 'AnonHugePages|HugePages' /proc/meminfo`. Report median and spread, same boot, alternating A/B.

**H1. Publish the page-size and path microbenchmark first (OBSERVED gap, measurement).**
- Touches: nothing in GB. Build `tests/bench/gb_pathbench.cu` out of tree (it needs `/usr/local/cuda/bin/nvcc` 13.x, `-arch=sm_120a`) and run `python3 tests/bench/gb_pathbench.py` on an idle GPU.
- Mechanism: resolves the 11 to 12 vs 24 GB/s contradiction and gives zero-copy vs staged-copy vs VRAM numbers on the same boot, plus the `4k/2m/1g` rows with `AnonHugePages` verification. Every later decision depends on it.
- Measure: the tool's own JSON lines (`zerocopy_read`, `staged_read`, `bulk_h2d`, `zerocopy_sm_read_<backing>_thp<N>MB`).
- Risk: none to the system; about a minute of idle GPU. Cheap enough that H2 to H4 shouldn't be coded before it.

**H2. Ask for 2 MB pages explicitly and verify them (OBSERVED gap, INFERRED effect).**
- Touches: `greenboost_cuda_shim.c:1608` (and the Path B `mmap` at `:7089-7100`): align the mapping to 2 MiB, `madvise(MADV_HUGEPAGE)`, pre-touch (or `MAP_POPULATE`) before `cuMemHostRegister`, then read `AnonHugePages` from `/proc/self/smaps` for the range and emit it in the `shim_decision` event.
- Mechanism: with `defrag=defer+madvise` the kernel only compacts for madvised ranges; unmadvised ranges fall back to 4K when fragmented. Larger pages mean fewer GPU-side translations per byte read, if pathbench 3b shows the GPU walks per-4K entries.
- Measure: H1's 4k vs 2m rows first. If they differ by more than noise, A/B the shim with and without the madvise on the real model and compare tok/s and the verified `AnonHugePages` fraction.
- Risk: low. More compaction work at load (a few seconds), possible RAM fragmentation pressure; falls back to 4K if no huge page is available.

**H3. Fix the placement of what lands in T2: keep the every-token tensors in VRAM, push the sparse ones out (INFERRED).**
- Touches: `gb_synapse_backends.py:1085-1121` (llama.cpp tensor placement flags) and/or `shim:7548-7730` (front-load split ordering). Options: per-tensor `-ot` override so attention, SSM state, shared experts and norms stay on device and only routed expert FFN tensors go to host; or reorder the logical buffer in the shim so the T2 tail is sparse data.
- Mechanism: bytes crossing PCIe per token = bytes of T2-resident tensors that are actually read. Routed experts are read at about 8/256, attention and shared tensors at 100%. Putting the sparse ones in T2 minimises per-token bytes for the same spill size. Whether llama.cpp's scheduler will run ops on weights in a host buffer on the GPU rather than CPU is not established, so this needs a probe first.
- Measure: log which tensors fall in the T2 range (GGUF tensor offsets vs the T2 boundary; `gb_gguf_tensor_map.py` exists), then PCIe RX bytes per token (`dmon -s t`) and tok/s, before and after.
- Risk: medium. llama.cpp may route those ops to the CPU backend, which breaks the "GPU compute always" rule and could be slower. Probe with a short run and check for CPU-assigned ops in the log before trusting any tok/s.

**H4. Check whether the board can train at Gen5 (UNVERIFIED, potentially 2x on the link).**
- Touches: BIOS only (PCIe slot speed setting, Gen5 / Auto vs Gen4), not code.
- Mechanism: `/sys/devices/pci0000:00/0000:00:01.0/max_link_speed` reads `16.0 GT/s` and `nvidia-smi` reports host max gen 4, while the GPU reports gpumax 5. i9-14900KF root ports are Gen5 capable by CPU spec, but whether this ITX board routes the slot at Gen5 can't be determined from software.
- Measure: after any BIOS change, `nvidia-smi --query-gpu=pcie.link.gen.current,pcie.link.gen.hostmax --format=csv` under load, then H1 bulk H2D.
- Risk: board and riser signal integrity at Gen5 can cause link retrains or instability. GB's `gb_pcie_tune.py` docstring says the cap is the board's root port and "not fixable in software"; that is evidence for the cap, not for the BIOS having no option.

**H5. Pin the CPU side of PyTorch tier moves (OBSERVED code, INFERRED effect).**
- Touches: `gb_model_tier.py:373-436` (promote/demote), `gb_moe.py` demote path: keep demoted weights in pinned buffers (`pin_memory()` or `cudaHostRegister` once), and copy parameter by parameter into preallocated pinned arenas.
- Mechanism: `.to("cuda", non_blocking=True)` from pageable memory is a staged, effectively synchronous copy at lower rate; pinned copies run on the copy engine and can truly overlap with compute.
- Measure: a 1 GiB `torch.empty(..., pin_memory=False/True).to("cuda")` micro-benchmark (expect a clear gap on Gen4), then a diffusion pipeline with `ModelTierManager` before/after.
- Risk: low to medium. Pinned memory is non-swappable and counts against the host RAM floor. Only helps PyTorch workloads (FLUX/diffusion), not llama-server.

**H6. Tune speculative-decoding depth against PCIe bytes, not just acceptance (OBSERVED variable, INFERRED benefit).**
- Touches: `gb_synapse_backends.py:1182-1191` (`--spec-draft-n-max`, `--spec-draft-p-min`).
- Mechanism: when weights stream over PCIe every pass, each accepted draft token amortises the whole pass. Dataflux shows daily acceptance of 0.22 to 0.71 for the same model, and the 2026-08 dense study showed depth 4 doubled tok/s only while acceptance stayed above about 0.5.
- Measure: sweep `draft_n_max` in {0,2,3,4,6} and p-min in {0.3,0.5} with the fixed prompt; record tok/s, `draft_n_accepted/draft_n`, PCIe RX bytes per generated token.
- Risk: low. Pure serving parameters; quality-neutral by construction of speculative decoding.

**H7. Check and raise MaxReadReq, and confirm the link doesn't sit in a low-power state when decode starts (UNVERIFIED, single-digit to about 20% by GB's own literature note).**
- Touches: `gb_pcie_tune.py report` (read), `--apply` (root only, not persisted). Link state: sysfs read `5.0 GT/s` when idle, so the first decode tokens may start below Gen4.
- Mechanism: larger read requests mean fewer completion round trips on a long sequential stream; `pcie_aspm.policy=performance` is already in the cmdline.
- Measure: `sudo python3 gb_pcie_tune.py report` for the current MRRS, H1 bulk and zero-copy rates before and after `--apply`, and `nvidia-smi --query-gpu=pcie.link.gen.current -l 1` through the first seconds of a decode.
- Risk: low. Needs root, which I did not use; setpci write is reversible by reboot.

**H8. Wire real heat to a promote path for MoE chunks (OBSERVED dead code, UNVERIFIED benefit).**
- Touches: `gb_expert_tier.h` (already written and unit-tested), `greenboost_cuda_shim.c:10083` (`cuLaunchKernel` hook sees kernel argument pointers), `gb_frontload_split_alloc`; add an async `cuMemcpyHtoDAsync` on a private stream into VRAM chunks and a VA remap.
- Mechanism: hot experts migrate into VRAM while cold ones stay in T2; the 1 GiB chunk unit (`:7487`) is far larger than an expert tensor, so it must shrink to tens of MB or the heat signal is blurred.
- Measure: tok/s and PCIe RX bytes per token on a long multi-topic conversation, with swap count and ping-pong rate from `gb_thrash`.
- Risk: high. Hot path in the interposer, VA remapping under live kernels, `cuMemSetAccess` cost. The same project's `LayerPrefetcher` sweep lost to a static placement, which is a warning about churn. Do only if H1 and H3 show the T2-resident bytes are a large share of per-token bytes.

**H9. Staged copy-engine prefetch into a VRAM ring for dense spill (INFERRED, capped by GB's own arithmetic).**
- Touches: shim launch hook plus a ring of VRAM buffers; or llama.cpp's own layer split. `pathbench` already reports a `staged_read` figure for exactly this.
- Mechanism: copy engines run at bulk-DMA rate independently of SMs. If H1 shows zero-copy SM reads are much slower than bulk (the 11 to 12 GB/s reading), staging could recover up to about 2x of PCIe efficiency on dense models. If they are equal (the 24 GB/s reading), the ceiling is the 9% overlap figure from `workflow/pcie-bandwidth-findings.md` section 2.
- Measure: H1's `zerocopy_read` vs `staged_read`; only build if the ratio exceeds about 1.3.
- Risk: high and gated entirely by H1.

**H10. Remove or measure the per-allocation device sync and early-pin overhead at load (OBSERVED, INFERRED effect on load time only).**
- Touches: `shim:1648-1672`.
- Mechanism: `cudaDeviceSynchronize` plus an error drain after every T2 allocation serialises load. It does not affect steady decode throughput.
- Measure: time from launch to first token with `GREENBOOST_DEBUG=1`, plus NVTX range `ALLOC_T2_ZC`; compare with the sync gated to the last allocation of a burst.
- Risk: medium. The code comment says SM access fails without the advise side effect, so any change needs a decode-correctness check (run a kernel over the T2 pointer, compare checksums) before it counts. Low value for tok/s, listed because the owner asked about serial work in hot-ish paths.

## 7. What couldn't be determined

- Whether the front-load split runs on this machine at all, and which T2 allocation path served the model in the last observed run (shim_stats shows only the last path).
- Actual page size of the registered T2 ranges (needs `smaps` of the live llama-server; none was running).
- Current `MaxReadReq`, `MaxPayload` and ASPM link state under load (needs root for `lspci -vv`).
- Whether llama.cpp will execute ops on weights held in a host buffer on the GPU (H3 depends on it).
- Whether the board offers a Gen5 slot setting (H4).
- Why tok/s on the same model fell from a median of about 50 (2026-10-04) to about 30 (2026-10-27).
- Real PCIe RX bytes per token on the current model. Every conclusion in section 5's sanity check is derived arithmetic, not a counter reading.
