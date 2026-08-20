# Ferran custom patches — index

This is the complete catalog of every kernel/QEMU patch touched in this
workspace, split by who actually wrote it. It exists because `patches/` mixes
20 files that look uniform (same numbering, same directory) but are not: 15
are other people's out-of-tree work carried for local convenience, and 5 are
originals. Only the originals are ours to send anywhere.

See `UPSTREAMING.md` for the send procedure (LKML submission, originals
only) and `PUBLISHING.md` for the full publishing guide, which also covers
the separate public-download track for the complete set. `proposals.md` has
an honest per-patch readiness grade. `originals/` has one page per original
patch.

## Kernel patches — `~/Dev/kernel_inference/patches/*.patch`

Base tree: `~/Dev/kernel_inference/linux`, currently `7.1.2-xanmod1` plus two
local commits (`b2c8fd0de75d` = `0017`, `b6097ab1cfe7` = `0019`). Note this
directory (`~/Dev/kernel_inference`) is not itself a git repo; `linux/` inside
it is.

### Imported third-party (15) — never send these, see UPSTREAMING.md

| # | Subject | From (real header) | Origin project |
|---|---|---|---|
| 0001 | bore | Piotr Gorski `<lucjan.lucjanov@gmail.com>` | CachyOS (BORE scheduler) |
| 0002 | tcp_bbr: v3: update TCP 'bbr' congestion control module | Oleksandr Natalenko `<oleksandr@natalenko.name>` | XanMod |
| 0003 | XANMOD: mm/vmscan: Reduce amount of swapping | Alexandre Frade `<kernel@xanmod.org>` | XanMod |
| 0004 | XANMOD: vfs: Decrease rate at which vfs caches are reclaimed | Alexandre Frade `<kernel@xanmod.org>` | XanMod |
| 0005 | XANMOD: mm: Raise max_map_count default value | Alexandre Frade `<kernel@xanmod.org>` | XanMod |
| 0006 | XANMOD: kconfig: add 500Hz timer interrupt kernel config | Alexandre Frade `<kernel@xanmod.org>` | XanMod |
| 0007 | XANMOD: fair: Set scheduler tunable latencies to unscaled | Alexandre Frade `<kernel@xanmod.org>` | XanMod |
| 0008 | ZEN: dm-crypt: Disable workqueues for crypto ops | Steven Barrett `<steven@liquorix.net>` | Liquorix / ZEN |
| 0009 | ZEN: input/evdev: Use call_rcu when detaching client | Kenny Levinsen `<kl@kl.wtf>` | Liquorix / ZEN |
| 0010 | XANMOD: blk-wbt: Set wbt_default_latency_nsec() to 2msec | Alexandre Frade `<kernel@xanmod.org>` | XanMod |
| 0011 | XANMOD: block: Set rq_affinity to force complete I/O requests | Alexandre Frade `<kernel@xanmod.org>` | XanMod |
| 0012 | XANMOD: block/mq-deadline: Disable front_merges by default | Alexandre Frade `<kernel@xanmod.org>` | XanMod |
| 0013 | XANMOD: block/mq-deadline: Increase write priority to improve | Alexandre Frade `<kernel@xanmod.org>` | XanMod |
| 0014 | pci: Set PME_TIMEOUT to 4000ms to reduce spurious wakeups | Arjan van de Ven `<arjan@linux.intel.com>` | TKG |
| 0018 | pci: Enable overrides for missing ACS capabilities | Mark Weiman `<mark.weiman@markzz.com>` | TKG |

These are carried locally because they're useful, several deliberately live
out-of-tree by their own authors' choice (ACS override and PME timeout tweaks
are the classic examples — real upstream pushback exists on both). Nothing
here is a bug we found; it's software we didn't write.

### Original (5) — ours, upstream candidates

| # | Subject | Authored as | Status |
|---|---|---|---|
| 0015 | nvme: lower default APST max latency for desktop/workstation use | hyphaed workstation tuning `<noreply@local>` | local default change, see `proposals.md` |
| 0016 | mm: default THP defrag to defer+madvise instead of madvise | hyphaed workstation tuning `<noreply@local>` | local default change, see `proposals.md` |
| 0017 | kbuild: ubsan: skip UBSAN for external modules by default | Ferran `<ferran.duarri@me.com>` | plausible candidate, see `proposals.md` |
| 0019 | [RFC PATCH] dma-buf: add a generic reclaim-priority hint | Ferran `<ferran.duarri@me.com>` | ready to send, see `proposals.md` and `upstream-candidates/dma-buf-priority-hint/` |
| 0020 | [RFC PATCH] dma-buf: add a generic compressed-content descriptor | Ferran `<ferran.duarri@me.com>` | plausible candidate (land the GreenBoost consumer first), see `proposals.md` and `upstream-candidates/dma-buf-compressed-descriptor/` |

## Existing RFC / investigation write-ups — `~/Dev/kernel_inference/upstream-candidates/`

| Directory | What it is | Status |
|---|---|---|
| `dma-buf-priority-hint/` | Full RFC package for patch `0019`: `README.md` (plain-English explanation + who else benefits beyond GreenBoost + what/why/how-to-send), `EXPLAINER.md` (line-by-line technical walkthrough), the patch itself | Drafted, compile-tested, signed off, **not sent** |
| `dma-buf-compressed-descriptor/` | Full RFC package for patch `0020`, sibling of `0019` (same 3 files, designed to stack on top of it): `README.md`, `EXPLAINER.md`, the patch itself | Drafted, compile-tested against the real stacked `0001`-`0020` series, checkpatch clean, signed off, **not sent** |
| `pcie-bwctrl-stale-target-speed/` | Investigation into a suspected stale-cache bug in `drivers/pci/pcie/bwctrl.c` / `drivers/thermal/pcie_cooling.c` | **Correctly abandoned** — the diagnostic script disproved the "stale cache" theory; kept as a worked example of not writing a patch you can't prove helps, see `proposals.md` |

## QEMU patches — `~/Dev/greenboost_all/greenboost_vgpu/qemu/*.patch`

Different project, different upstream, different mailing list (`qemu-devel`,
not LKML). Listed here only so the catalog is complete; see `proposals.md` for
why neither is submission-ready.

| File | Subject | From |
|---|---|---|
| `0001-hw-display-add-gb-vgpu.patch` | hw/display: add GB-VGPU (GreenBoost Virtual GPU) device | Ferran Duarri |
| `0002-hw-display-add-gb-pvg.patch` | hw/display: add GB-PVG (original Apple PVG protocol device) | Ferran Duarri |

## Why this catalog exists now (2026-08-06)

Written after a session that asked whether a custom kernel patch could enable
NVIDIA vGPU on an RTX 5070 for `greenboost_vgpu`'s macOS paravirtualization
work. Short answer: no — every blocker (closed driver, no SR-IOV VF support in
`nvidia.ko`, GeForce excluded from the vGPU support matrix) sits outside the
kernel, so no kernel patch was written for that. The full reasoning lives in
the plan file this session produced; this catalog is the durable, in-repo
record of the patch inventory and the publishing process, independent of that
one investigation.
