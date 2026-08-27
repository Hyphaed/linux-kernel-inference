# Did the patches work? — 7.1.10-hyphaed, 2026-08-25

*Revised 2026-08-26 after a boot audit. The 0023 verdict below was wrong and
is corrected in place; everything else stands. What changed and why is in
the 0023 section.*

The series has been carried across four kernel bumps on the strength of
`git am` returning 0 and the tree compiling. That is not evidence a patch
does anything, and this repo's own MUST-RULE #3 refuses to accept it from
anyone else. This is the first time the twenty patches in
`patches/kernel-org-7.1/series` were checked against a booted kernel.

Booted 21:52. Everything below was measured on the running machine.

## The short version

Fifteen of twenty do what they say. Three are carried but cannot reach the
running system, and one of those is masked by systemd in a way nobody had
noticed. Two are inert because of a configuration choice, not a defect. One
cannot be proven on this hardware at all.

**One is broken.** 0023's kfunc sets fail to register on every boot with a
`WARNING` at `kernel/bpf/btf.c:9004`, so no eBPF eviction policy that calls
a cache_ext kfunc can load. The struct_ops half registered, which is what
this page originally checked and why it originally passed. Fixed in the
patch; needs a rebuild to land.

Two of the twenty had never once been executed before today, and both work.

## Per patch

| # | What it does | Verdict | The observation |
|---|---|---|---|
| 0001 | CachyOS BORE scheduler | **live** | `kernel.sched_bore = 1`, `CONFIG_SCHED_BORE=y` |
| 0002 | swappiness 60 → 10 | **can't testify** | live value is 10, but `99-zzz-greenboost.conf` also sets 10 — identical with or without the patch |
| 0003 | vfs_cache_pressure 100 → 50 | **live** | 50, and nothing in `/etc/sysctl.d` or `/usr/lib/sysctl.d` sets it. The one uncontested sysctl in the series |
| 0004 | max_map_count → 2147483642 | **masked** | live 1048576, set by `/usr/lib/sysctl.d/50-default.conf` and `55-map-count.conf`. Userspace runs last |
| 0005 | adds an HZ_500 choice | **inert by design** | `HZ_500` is in `kernel/Kconfig.hz`; the build selected `CONFIG_HZ=1000` |
| 0006 | dm-crypt workqueue bypass | *root pending* | needs `dmsetup table` |
| 0007 | evdev teardown under RCU | **live (source)** | `evdev_reclaim_client` + `call_rcu` in the tree; no runtime knob exists |
| 0008 | blk-wbt 2 ms for rotational too | **unprovable here** | all queues read 2000, but stock gives non-rotational 2000 anyway and this box has no rotational disk |
| 0009 | `QUEUE_FLAG_SAME_FORCE` in mq default | **live** | `rq_affinity=2` on every nvme queue. Stock is 1 — unambiguous |
| 0010 | mq-deadline front_merges → 0 | **inert** | scheduler is `none`; *root pending* to prove under mq-deadline |
| 0011 | mq-deadline write_expire 5s → 1s | **inert** | same |
| 0012 | PCI PME poll 1000 → 4000 ms | **live (source)** | `#define PME_TIMEOUT 4000` |
| 0013 | nvme APST latency | **live** | `default_ps_max_latency_us = 25000` |
| 0014 | THP defrag → defer+madvise | **live** | `[defer+madvise]` selected. No Kconfig or boot param can set this, so the bracketed value *is* the compiled-in default |
| 0015 | UBSAN opt-in for out-of-tree modules | **live** | the `$(if $(KBUILD_EXTMOD),,…)` guard is in the *shipped* `linux-headers` package, which is what DKMS builds against |
| 0018 | `pcie_acs_override=` boot option | **inert by design** | in `drivers/pci/quirks.c`, deliberately absent from the cmdline |
| 0019 | dma-buf priority hint | **live** | see below |
| 0020 | dma-buf compression descriptor | **live** | see below |
| 0022 | udmabuf scatterlist fix | **works, purpose unproven** | see below |
| 0023 | cache_ext BPF page-cache eviction | **half of it worked** | struct_ops registered, kfuncs did not — see below |

## 0019 and 0020 — first execution, and they pass

Both ship UAPI that reached `/usr/include/linux/dma-buf.h`, and neither had
ever had an ioctl issued against it. `tests/kernel_runtime/dmabuf_ioctl_probe.c`
exports a real 2 MiB dma-buf through udmabuf and runs 25 checks. All 25 pass:

- priority round-trips at 0, 1, 64, 128, 200 and 255
- a fresh buffer reads back `DMA_BUF_PRIORITY_DEFAULT` (128)
- `priority:` appears in `/proc/self/fdinfo/<fd>` and tracks every SET
- 256 is rejected `EINVAL`; a non-zero `pad` on SET is rejected `EINVAL`
- the compression descriptor round-trips `codec`, `block_size` and
  `uncompressed_size` intact, and resets to `DMA_BUF_CODEC_NONE`
- 6/7 don't collide with 4/5, `DMA_BUF_SET_NAME` still works, and an
  unclaimed number still returns `ENOTTY`

**The known review finding is now a tripwire.** `GET_PRIORITY` is `_IOR`, so
the kernel never copies the struct in and cannot see a non-zero `pad` — the
documented "must be zero" promise is enforced on SET only. The test asserts
today's behaviour deliberately, so the planned v2 (`_IOWR` + reject) makes it
fail loudly instead of slipping past.

## 0022 — the rewrite is sound, the point of it isn't shown

Jason Gunthorpe's upstream fix, rebased. `sg_alloc_table_from_pages` is in
`drivers/dma-buf/udmabuf.c` and the per-page `sg_set_folio` loop is gone.
udmabuf still exports correctly — the probe above depends on it and never
got past creation otherwise.

What is *not* shown is the coalescing the patch exists for. Scatterlist
nents aren't visible from userspace, and `/sys/kernel/debug/dma_buf/bufinfo`
needs root. Recorded as **unproven** rather than passed.

## 0023 — the struct_ops registered, the kfuncs did not

**Corrected 2026-08-26.** The row above used to say "registered", and the
section below used to end at *root pending*. Both were written from the
21:52 boot and both were too generous. Every 7.1.10 boot, including that
one, throws this during initcalls:

```
------------[ cut here ]------------
WARNING: kernel/bpf/btf.c:9004 at register_btf_kfunc_id_set+0x4b/0x60
 ? register_cache_ext_kfuncs+0x1d/0xa0
   do_one_initcall+0x86/0x370
---[ end trace 0000000000000000 ]---
cache_ext: failed to register kfunc sets (-22)
```

One more piece of v6.6.8 → v7.1.10 drift the forward-port missed.
`include/linux/btf_ids.h:202` defines `BTF_SET8_START(name)` as
`__BTF_SET8_START(name, local, 0)` — flags zero — while the kfunc form at
line 212, `BTF_KFUNCS_START`, passes `BTF_SET8_KFUNCS`.
`register_btf_kfunc_id_set()` rejects a set without that flag:

```c
if (!(kset->set->flags & BTF_SET8_KFUNCS)) {
        WARN_ON(!kset->owner);
        return -EINVAL;
}
```

Before 6.8, `BTF_SET8_START` *was* the kfunc macro. `mm/page_cache_ext_ds.c`
was the only file in the tree still using it; all ~40 in-tree kfunc sites
(`kernel/sched/ext.c`, `net/core/filter.c`, `kernel/bpf/helpers.c`, …) use
`BTF_KFUNCS_START`. `kset->owner` is `THIS_MODULE`, NULL for built-in code,
which is what turns the `-EINVAL` into a splat as well.

**What actually worked.** The registration risk the series comment flagged —
the hand-added `register_bpf_struct_ops()` replacing the gone 6.6 X-macro —
did not bite. BTF carries `bpf_struct_ops_page_cache_ext_ops` and all five
ops, and `/proc/page_cache_ext_enabled_cgroup` exists. A policy can attach.

**What did not.** All eight cache_ext kfuncs were absent from the verifier's
kfunc table, so any policy that called one would be rejected at load. That
is every real eviction policy — the kfuncs are how a policy ranks and moves
folios. 0023 was carried, compiled, and unusable.

**Why this page said otherwise.** The previous version cited "eight
`bpf_cache_ext_*` kfuncs" in BTF as evidence. BTF carries their *function
types* because they are `__used __retain noinline`; that says nothing about
whether the kfunc *set* registered, which is runtime state built at
initcall. And `cache_ext_probe.bpf.c` called no kfunc at all, so the probe
could not have caught it either. Checking BTF and calling nothing tests the
half that worked.

**Fixed** in `patches/custom/0023-…patch`: both sets switched to
`BTF_KFUNCS_START`/`BTF_KFUNCS_END`. Guarded three ways — a static check in
`tests/test_patch_series.py` that rejects `BTF_SET8_START` anywhere under
`patches/custom/`; a never-executed `bpf_cache_ext_list_del()` call in
`cache_ext_probe.bpf.c` so the load has to resolve a kfunc; and
`test_0023_kfunc_set_registered` asserting the load succeeded.

**Still pending, and unchanged by any of this:** whether 0023's call sites in
`mm/filemap.c` and `mm/vmscan.c` survived the rebase. A registered
struct_ops whose hooks never fire looks identical from userspace. The
counting probe answers that after 512 MiB of page-cache churn, and needs a
rebuilt kernel and root.

## Throughput: no change, and none was available

Three prompts through `gb`, same model and recipe as the baseline:
**1.1–1.3 tok/s**. The 7.1.8 baseline was 34 samples, median 1.5, min 1.2,
max 5.7.

That looks like a regression and isn't one. The governed layer names the
cause and it is capacity, not the kernel: `weights_dont_fit_vram` — 15.85 GB
of weights against 12227 MiB of VRAM, so the overflow crosses PCIe on every
forward pass "regardless of tuning". `vram_headroom_exhausted` puts fill at
97.3%, past the Rule #1 reserve. Neither is something a scheduler or block
patch can touch, and `docs/research/10x-tok-s-kernel-path-2026-08-17.md`
already put the kernel's whole share of decode at ~5.7%.

Three samples against thirty-four, across two kernels, inside a spread of
1.2–5.7 is not a measurement of anything. The honest statement is that the
series has no *detectable* effect on decode throughput, which is expected,
because not one of the twenty patches is on the decode path.

*(Source: semantic layer via `dataflux_critic` · Owner: gb_semantics ·
Governed: true)*

## Bugs found and fixed

**`hyphaed verify` reported BORE as failed on a machine where BORE was on.**
`check_bore_active()` read `/sys/kernel/debug/sched/features` and nothing
else. That needs root, so an unprivileged run printed *"BORE scheduler
active: FAIL — can't read … (run as root)"* next to twenty genuine results,
which reads as "the scheduler patch didn't apply". A check that fails
because it could not look is a false negative. It now reads
`kernel.sched_bore`, which is world-readable and only exists in a BORE
kernel, and falls back to debugfs.

**nvidia-fs was rejected at boot, not merely unloaded.**
`systemd-modules-load.service` is in a failed state:
`Failed to insert module 'nvidia_fs': Invalid argument`. The module is built
and present for 7.1.10 and `/etc/modules-load.d/nvidia-fs.conf` asks for it,
so something about the insert itself fails. Until it loads, GDS runs in POSIX
compat mode and every NVMe↔GPU byte crosses PCIe twice. `hyphaed verify` now
reports it; the real diagnosis is in `root-steps.sh`.

Worth naming why the existing guard missed it: `postinstall._check_nvidia_fs`
has caught exactly this since 7.1.8, but `phases_completed` stops at
`package`, so the install and postinstall phases never ran for 7.1.10.

**Two bugs in this session's own test code**, both caught before they could
lie:
- `dmabuf_ioctl_probe.c` passed `ioctl(...)` and `errno` as sibling arguments
  to the same call. Evaluation order is unspecified, and `errno` was read
  before the ioctl ran, so three checks printed `errno=0 (Success)` while
  asserting a rejection. The verdicts were right and the printed evidence was
  stale — which is worse than a plain failure, because it looks fine.
- `cache_ext_probe.bpf.c` named a parameter `ctx`, which `BPF_PROG()` uses
  internally. An earlier "compile succeeded" reading was wrong: it came from
  `$?` after a pipe into `head`, which reports head's status, not clang's.
  The Makefile caught it.

## Still open

- `gb synapse status` on the CLI hung past 60 s while the MCP tool answered
  instantly. Not chased.
- 0004 is decoration. Left in place — 1048576 is ample and removing it would
  change nothing — but it is now documented in the series so it isn't
  mistaken for a live tuning again.

## Running it

```bash
python -m pytest tests/kernel_runtime -q      # 28 pass, 5 need root
sudo bash tests/kernel_runtime/root-steps.sh  # unlocks the other 5
python -m hyphaed verify                      # now covers the series
```

The suite skips itself on any kernel without `-hyphaed` in its release
string, so the laptop and CI stay green.
