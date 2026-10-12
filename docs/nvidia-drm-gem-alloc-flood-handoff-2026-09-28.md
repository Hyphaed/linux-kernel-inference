# NVIDIA DRM GEM allocation flood — hand-off note, not actioned

Found in the 2026-09-28 log audit, boot `-2` (2026-09-25 15:09, 7.2.7-hyphaed).
Not a kernel or hyphaed defect — recorded here because it's real, it's not
small, and neither the driver nor GreenBoost side is this repo's to fix
directly.

## What happened

```
[drm:nv_drm_gem_alloc_nvkms_memory_ioctl [nvidia_drm]] *ERROR* [nvidia-drm]
[GPU ID 0x00000100] Failed to allocate NVKMS memory for GEM object
```

**296,378 occurrences** in one boot, peaking at **99,119 lines in a single
minute** (15:09). A second boot (`-3`, 2026-09-23) shows the same failure
mode from a different angle:

```
NVRM: GPU0 nvCheckOkFailedNoLog: Check failed: Out of memory [NV_ERR_NO_MEMORY]
NVRM: nvAssertOkFailedNoLog: Assertion failed: Out of memory [NV_ERR_NO_MEMORY]
NVRM: GPU0 _kgmmuClientShadowFaultBufferPagesAllocate: Allocation failed with
      big page size, retrying with default page size
```

## Why, as far as this audit can tell

The RTX 5070 has 12 GB VRAM. GreenBoost's own boot log the same session
shows `T1 reserve` numbers consistent with the model-tiering design
(`docs/research/*` and `CLAUDE.md`'s "Local-inference prior art" section):
GreenBoost is deliberately holding a large fraction of VRAM for model
weights plus a KV-cache reserve. `vram-residency-baseline.py`
(referenced in `CLAUDE.md`'s DGX-Spark-parity section) measured 10.4/12.2
GB in use with only 1.8 GB headroom at baseline, before any desktop
compositor/game allocation. The NVKMS allocation failures are the display
stack (Wayland/GNOME compositor GEM buffer allocations, window
thumbnails, etc.) losing the race for the remaining ~1.8 GB against
whatever else is running.

**Not verified**: this audit didn't correlate the flood's timing against a
specific GreenBoost inference run or a specific desktop action (opening an
app, a game launching) — only that it happened, twice, on two different
boots, and that the arithmetic (12 GB total, ~10.4 GB pre-reserved) leaves
very little room for anything else. Confirming the trigger needs a live
session with `nvidia-smi --query-gpu=memory.used --format=csv -l 1` running
alongside GreenBoost's own VRAM accounting at the moment a flood starts.

## Two things that would help, neither of which is this repo's to do

1. **Rate-limit the kernel-side log line.** `nv_drm_gem_alloc_nvkms_memory_ioctl`
   isn't ratelimited in the installed 615.71.09 driver — a single sustained
   allocation-pressure episode produces on the order of 10^5 log lines,
   which itself has a cost (journald disk I/O, `journalctl` becoming
   slow to query). This is NVIDIA's closed/open-kernel-module code, not
   something this repo can patch. Worth a GitHub issue against
   `NVIDIA/open-gpu-kernel-modules` if this recurs — draft text below,
   **not filed**, per the plan ("nothing gets posted without approval").
2. **GreenBoost-side VRAM headroom.** If GreenBoost's T1 pool sizing (or
   the KV-cache reserve) can be told to leave more than ~1.8 GB for the
   desktop compositor, or to detect NVKMS allocation pressure and back off,
   the flood's root cause goes away rather than just its logging volume.
   This is a GreenBoost tiering-policy question
   (`~/Dev/greenboost_all/greenboost`), out of this repo's scope per
   `CLAUDE.md`'s existing convention for GreenBoost-owned findings.

## Draft GitHub issue text (not filed)

> **Title**: `nv_drm_gem_alloc_nvkms_memory_ioctl` error log has no rate
> limit — 296k lines/boot under sustained VRAM pressure
>
> On a 12 GB RTX 5070 running near-full VRAM residency (large local-
> inference model resident in VRAM, ~1.8 GB headroom), sustained NVKMS
> allocation failures from the desktop compositor produce hundreds of
> thousands of identical `*ERROR* [nvidia-drm] [GPU ID 0x...] Failed to
> allocate NVKMS memory for GEM object` lines in a single boot (296,378
> observed, peak 99,119/minute). Driver 615.71.09, kernel 7.2.7, Ubuntu
> 26.04. Could this call site use `dev_err_ratelimited()` /
> `pr_err_ratelimited()` or equivalent instead of an unconditional log on
> every failed allocation attempt? Happy to provide a full dmesg capture
> if useful.

Not filed. If this recurs on 7.2.8, worth filing for real with a fresh
capture rather than this session's after-the-fact boot-log read.
