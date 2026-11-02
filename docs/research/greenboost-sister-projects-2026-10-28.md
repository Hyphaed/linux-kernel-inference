# What GreenBoost can take from greenboost_vgpu and RTX-Kit

Date: 2026-10-28. Read-only study; nothing was built, run or modified. Target: GreenBoost
(`~/Dev/greenboost_all/greenboost`) on one RTX 5070 12 GB, PCIe Gen4 x16 (~11-12 GB/s
measured per GreenBoost's own CLAUDE.md), 64 GB DDR4.

## Short answer

Neither project has much to offer for the actual bottleneck, which is moving bytes over PCIe
fast enough. greenboost_vgpu is a QEMU paravirtual GPU: it never imports host pages into the
GPU (no `VK_EXT_external_memory_host` anywhere in `qemu/hw_display_gb-pvg-vk.inc.c`), it
`memcpy`s into HOST_VISIBLE staging maps. What it does well is avoiding copies it doesn't
need, and one of those ideas transfers. RTX-Kit is graphics middleware; one piece (RTXTS's
standby queue) has a transferable residency idea, one piece (RTXMU's suballocator) is a
plain allocator, and the rest doesn't apply.

What GreenBoost already has (so it isn't proposed again): DMA-BUF pinned T2 and
`cuMemHostRegister` paths (CLAUDE.md:121-124), multi-stream scheduler
(`gb_stream_sched.py`), deterministic layer prefetch overlapped with compute
(`gb_prefetch.py`), MoE expert prefetch (`gb_moe.py`), LRU+priority eviction ported from
TensorRT-LLM (`gb_evict_policy.py:1-30`), PCIe tuning (`gb_pcie_tune.py`).

## Project 1: greenboost_vgpu (licence: GPL-2.0-or-later, same as GreenBoost's GPL-2.0-only module: code can't be pasted into the kernel module without checking the "or later" direction; ideas are free)

### 1.1 Skip transfers for data that didn't change (dirty tracking)

- Source: `protocol/gb_pvg_dirty.h:1-90` (userfaultfd write-protect in async mode,
  `UFFD_FEATURE_WP_ASYNC`, then `PAGEMAP_SCAN` with `PAGE_IS_WRITTEN | PM_SCAN_WP_MATCHING`
  returns only pages written since the last scan and re-arms them; unprivileged, kernel
  >= 6.7). The consumer is `xvk_buf_sync_dirty()` at `qemu/hw_display_gb-pvg-vk.inc.c:1368-1514`:
  it re-derives the host page runs, arms pages once, scans, records a per-page generation in
  a shared hash (`dirty_gen`, line 1446), and copies only pages whose generation is newer
  than the buffer's own `trk_gen` (lines 1457-1476). Buffers below `dirty_min` skip tracking
  because a scan costs more than re-reading them.
- Measured by the project: 23,451 skipped copies, 0 mismatches in a verification boot
  (README.md:36). That is a count of skipped copies, not a bandwidth or latency number.
- Mapping to GreenBoost: the useful form isn't the uffd machinery, it's the principle. A
  tensor that was uploaded T2 -> T1 and not modified since has a still-valid copy in T2, so
  evicting it from T1 is a free drop with no D2H copy. For weights this needs no tracking at
  all (they're immutable: a flag is enough). For CPU-written T2 data (anything the host
  rewrites between uses) uffd-wp would say which pages need re-upload.
- Caveat that limits it: uffd-wp sees CPU stores through page tables. It does not see
  GPU DMA writes into pinned host pages. So it can't track GPU-written KV cache that was
  spilled to T2; that needs a block-level version counter on the GPU side. I did not verify
  how uffd-wp behaves on `cuMemHostRegister`-pinned or DMA-BUF-backed pages, or whether
  write-protecting splits transparent huge pages (the probe `tools/uffd_async_wp_probe.c`
  ran on THP memory and reported correct counts, but never on pinned memory).
- Did GreenBoost already do clean-shadow eviction? I grepped `gb_model_tier.py`,
  `gb_tiering.py`, `gb_tier_kv.py`, `gb_moe.py` for write-back/shadow/clean-copy wording and
  found nothing. That is absence of a keyword, not proof; someone who knows the T1 -> T2
  demotion path should check whether weights are copied back down or just dropped.
- Benefit and measurement: for a model whose weights cycle T2 -> T1 -> T2 each forward pass,
  every avoided D2H is up to the evicted tensor's size on the PCIe link's other direction
  (it doesn't compete with H2D bandwidth on a full-duplex link, so the win is smaller than
  it sounds unless D2H is on the critical path). Measure with `nvidia-smi dmon -s t` (PCIe
  rx/tx) and the tier-move counters in dataflux (`dataflux_tier_moves`) before/after on one
  fixed prompt, repeated enough to beat noise.
- Risk: correctness. A missed write means stale data silently used.
- Verdict: **MAYBE**. Clean-shadow eviction for immutable weights is cheap and worth a
  look if it isn't already there. The uffd-wp tracker itself is **MAYBE-low**: only for
  CPU-written data, and unverified on pinned memory.

### 1.2 A verify mode for every skip-copy optimisation

- Source: `dirty_verify`, vk.inc.c:1477-1500. After a "skipped" sync it re-reads the real
  bytes, compares, counts mismatches, and uses the real bytes on mismatch. Env switch
  `GBPVG_DIRTY=verify`. Also a one-time selftest (line 1375).
- Mapping: any GreenBoost path that avoids a copy (1.1, prefix/KV reuse, snapshot restore)
  gets a debug mode that does the copy anyway and diffs. The 0-mismatch result is what turns
  "should be safe" into evidence.
- Verdict: **ADOPT-IDEA** (methodology, no code to port).

### 1.3 Lesson from a mistake: don't trust version records you don't control

- Source: vk.inc.c:1520-1555 and the `GBPVG_BUF_TRUST` comment (~line 1538): the guest's
  validity records do not report CPU stores into shared buffers (test 47), so the default is
  to re-read every buffer every command buffer, and the trust shortcut is opt-in.
- Mapping: any GreenBoost "this block is unchanged" shortcut must be driven by something
  the writer cannot bypass (page protection, an ioctl on the write path), not by a
  convention the writer is supposed to follow.
- Verdict: **ADOPT-IDEA** (design rule).

### 1.4 SPSC ring: acquire/release, zero-copy peek/commit, in-place handback

- Source: `backend/ring.c:31-46` (acquire/release atomics on head/tail), `backend/ring.h:50-70`
  (`pop_peek` returns pointers into the ring, `pop_commit` frees the slot; backend writes a
  new resource id into the producer's own slot instead of a reply message), header layout in
  `protocol/gb_vgpu_proto.h:102-109`, fence page on its own 4 KiB page.
- Mapping: a command channel between GreenBoost's Python scheduler and a native copy worker
  or the kernel module. Today scheduling is Python-level (`gb_prefetch.py` hooks), where
  per-command cost is microseconds-to-milliseconds, so a lock-free ring saves nothing
  measurable unless a native prefetch thread is built.
- Verdict: **MAYBE-low** (only if a native prefetch worker is written; then copy the design).

### 1.5 Generation-tagged handles

- Source: `backend/vk_backend.c:92-129`: handle = `(generation << 32) | (slot+1)`, O(1)
  lookup, stale handle fails the generation check, generation skips 0 on wrap,
  `tests/test_res_handles.c` asserts handles are never reused.
- Mapping: IDs for T2 allocations passed between the shim, daemon and kernel module can be
  stale-safe without an ever-growing counter table. Whether GreenBoost's ioctl ids already
  do this I didn't check.
- Verdict: **MAYBE-low**.

### 1.6 Things that look relevant and aren't

- Evict-LRU-and-retry on allocation failure (vk.inc.c:765-852): GreenBoost has
  `gb_evict_policy.py` and `gb_reactive.py`; I didn't diff them. **NOT RELEVANT** unless the
  reactive path lacks retry.
- Single staging buffer with `xvk_flush` after every upload (vk.inc.c:1578-1688): this
  serialises uploads. It's what not to copy. **NOT RELEVANT**.
- Pipeline and shader caches (`qemu/hw_display_gb-pvg-shader.inc.c:11`, vk.inc.c:2661-2678):
  keyed by hash of AIR bytes, disk-persisted. Triton already caches compiled kernels.
  **NOT RELEVANT**.
- dma-buf export/import (`tools/dmabuf_probe.c`): Vulkan image -> dma-buf -> EGL, for display
  scanout. GreenBoost already uses DMA-BUF for the opposite direction (host memory into
  CUDA). **NOT RELEVANT**.
- Command batching: the executor groups commands into render-pass events
  (`protocol/gb_pvg_pass.h`); that's graphics command structure, no analogue. **NOT RELEVANT**.

## Project 2: RTX-Kit (2026.3 release)

Licences, one line each (checked by reading the first line of each file):

- rtxmu: MIT-style permissive licence (`rtxmu/LICENSE.txt`). Only component that could be
  adapted with attribution.
- rtxts, rtxntc, rtxns, dlss, nrd, omm, rtxdi, rtxmg, rtxpt, rtxtf: "NVIDIA RTX SDKs
  LICENSE" (SDK licence, not open source; check terms before adapting any code).
- streamline: own `license.txt`, opens with a code comment block; not read further.
- rtxcr, rtxgi, stbn: no licence file at the top of the directory when I listed them
  (rtxgi/rtxcr/stbn not individually checked).

### 2.1 RTXMU: suballocator

- Source: `rtxmu/include/rtxmu/Suballocator.h:76-200` (blocks of a fixed size, 8 MB in the
  README example; first-fit search for a freed sub-block, else bump `currentOffset`, else new
  block; oversize requests get a dedicated block), `findFreeSubBlock` at line 330, optional
  free-block merging at line 407 behind `MERGE_FREE_BLOCKS`. Per-block alignment accounting
  (`alignmentSavings`). Vulkan/D3D12 allocators are thin wrappers.
- Mapping: GreenBoost's pinned path has per-allocation overhead; CLAUDE.md:123 says small
  (< 4 MB) allocations use the pinned path because DMA-BUF import overhead isn't worth it
  there. Carving small allocations out of a few large pinned blocks would cut the number of
  pin/import calls. I did not check whether `gb_mem_pool.py` (184 lines, Python-side tensor
  pool) or the kernel module already sub-allocates.
- Benefit and measurement: count of `GB_IOCTL_PIN_USER_PTR` / import calls and time spent
  in them during model load, before/after. Pinned memory is also scarce and can't be handed
  back partially, so block size matters.
- Risk: fragmentation in blocks you can't shrink; a first-fit allocator with no
  defragmentation is the weak spot (RTXMU doesn't defragment either).
- Verdict: **MAYBE**. Small idea; write our own, don't import C++.
- The rest of RTXMU (BLAS compaction via post-build info, `AccelStructManager.h`) is
  ray-tracing acceleration-structure specific. **NOT RELEVANT**.

### 2.2 RTXTS: tiled texture manager, standby queue

- Source: `rtxts/libraries/rtxts-ttm/include/rtxts-ttm/TiledTextureManager.h:57` (64 KB
  tiles, 256 per heap), `:63` (`numExtraStandbyTiles = 1000`), `src/TiledTextureManager.cpp:242-250`
  (`TrimStandbyTiles`: evict only the oldest standby tiles beyond the target), `:555-576`
  (a tile that was no longer requested goes to standby, stays resident, and is moved back to
  mapped for free if requested again), `:330` (`DefragmentTiles`: move tiles to heaps with
  free space on the left, so empty heaps can be released). Residency is driven by sampler
  feedback (what the shader actually sampled), not prediction.
- Mapping: a "standby" state in T1. When a layer/expert isn't needed it isn't evicted, it
  becomes reclaimable; eviction happens only to satisfy a real allocation, oldest standby
  first; a re-request revives it with zero PCIe traffic. The knob is a VRAM headroom target.
  GreenBoost's LRU+priority policy is close to this (an LRU is effectively a standby queue),
  so the genuine new part is only: explicit headroom target for standby, and the defrag step
  that lets whole heaps be freed.
- Benefit and measurement: only matters when the working set slightly exceeds VRAM and
  access has temporal locality (MoE experts do; dense layers cycled in fixed order don't, an
  LRU on a strict cycle evicts exactly the thing needed next). Measure T1 hit rate and
  bytes moved per token from dataflux on a MoE model.
- Risk: low, but it's mostly a re-description of what `gb_evict_policy.py` does.
- Hard limit: the SDK is D3D12 only, needs sampler feedback, and says Vulkan isn't
  supported (`rtxts/README.md` requirements section). It can't be reused; only the policy
  idea can.
- Verdict: **MAYBE-low**. The dense-layer case is a known anti-pattern for LRU; a
  cyclic-aware policy (don't evict what's next in the fixed order) is already implicit in
  `gb_prefetch.py`.

### 2.3 RTXNTC: neural texture compression

- Source: `rtxntc/README.md:7-42`. A small per-material MLP decoder plus a tensor of latents
  replaces a bundle of PBR textures; table at lines 35-42 shows PCIe traffic falling from
  12 MB (BCn) to 2.5 MB for a 2k bundle, with decode on load or on sample through
  `VK_NV_cooperative_vector` (README:62-72).
- Why it doesn't map: it's trained per texture bundle and relies on correlated image
  channels; it isn't a general tensor codec. LLM and diffusion weights already go through
  GreenBoost's quantisers (`gb_quant*.py`), which cut PCIe bytes the same way for tensors.
  The general lesson ("move compressed bytes, decode in VRAM, count PCIe traffic as a
  column") is already how GreenBoost works.
- Verdict: **NOT RELEVANT**.

### 2.4 RTXNS: neural shaders

- Source: `rtxns/README.md:3-9`, samples under `rtxns/samples/` (simple inference,
  training, Slang/SlangPy). Cooperative-vector MLP inference inside graphics shaders. No
  memory management, streaming or transfer scheduling. A reference for how
  `VK_NV_cooperative_vector` is used, nothing for tiering.
- Verdict: **NOT RELEVANT**.

### 2.5 Everything else

dlss, nrd (denoiser), omm (opacity micromaps), rtxcr (character rendering), rtxdi
(direct illumination), rtxgi (global illumination), rtxmg (mega geometry), rtxpt (path
tracer), rtxtf (texture filtering), stbn (blue-noise textures), streamline (plugin
manager that loads DLSS/Reflex-style features into game engines). Graphics-only. I judged
these from directory names and top-level READMEs and did not read their sources.
**NOT RELEVANT** for an LLM/diffusion offload stack.

## What I could not determine

- Whether GreenBoost already skips D2H on eviction of unmodified weights (keyword grep
  found nothing; I did not trace the demotion code).
- How uffd-wp behaves on pinned or DMA-BUF-backed pages, and whether it splits THP. No
  test was run (read-only study).
- Whether the kernel module or `gb_mem_pool.py` already sub-allocates small pinned
  allocations.
- Whether GreenBoost's ioctl allocation ids are generation-tagged.
- greenboost_vgpu's real cost of a dirty scan (no timing in the repo, only skip counts);
  `workflow/state/PROGRESS.md` is 3 lines and the protocol notes are 3000+ lines, of which I
  read only grep hits.
- Licence text of rtxcr, rtxgi, stbn, and the body of streamline's licence.
