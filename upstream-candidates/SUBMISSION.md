# Submission status

## Sent to LKML

| Patch | Sent | Lists | Message-ID | Thread |
|---|---|---|---|---|
| `0021` PCI/sysfs docs | 2026-08-20 20:42 CEST | linux-pci, linux-api, linux-kernel | `20260820184228.166566-1-ferran.duarri@me.com` | <https://lore.kernel.org/linux-pci/20260820184228.166566-1-ferran.duarri@me.com/> |
| `0017` kbuild UBSAN extmod | 2026-08-20 21:01 CEST | linux-kbuild, linux-kernel | `20260820190200.203185-1-ferran.duarri@me.com` | <https://lore.kernel.org/linux-kbuild/20260820190200.203185-1-ferran.duarri@me.com/> |

Both sent with `git send-email` via `smtp.mail.me.com`, SMTP result 250, To:
the subsystem maintainers with the lists in Cc:. Each moved from `outbox/` to
`sent/` on send. `0015`, `0016` and `0019` remain in `outbox/`; see
`outbox/SEND.md` for the order, why the two default-change patches are the
weakest of the six, and why `0019` goes last.

`0017` is the one most likely to get a substantive reply, and the reply will
ask for the reproducer. The answer is VMware's `vmnet`/`vmmon`: with
CONFIG_UBSAN=y the external module inherits UBSAN flags from KBUILD_CFLAGS
because `is-kernel-object` is 'y' for any `obj-m` object, builds clean, loads
clean, and then fails packet forwarding at runtime. Nothing in the build output
indicates the module was instrumented, which is what makes it cost hours to
find rather than minutes.

All submissions by this author, across every list lore archives:
<https://lore.kernel.org/all/?q=f:ferran.duarri@me.com>

Review queue for the PCI subsystem:
<https://patchwork.kernel.org/project/linux-pci/list/>

## What of the series can be submitted at all

The 7.1.9 series is 21 patches. **Six are ours. Fifteen are not, and cannot be
sent by us.**

| # | Patch | Author | Upstream path |
|---|---|---|---|
| 0001 | cachyos bore scheduler | Piotr Gorski | CachyOS |
| 0002 | xanmod bbr3 | Oleksandr Natalenko | XanMod |
| 0003-0007, 0010-0013 | xanmod zen tunings | Alexandre Frade | XanMod |
| 0008 | zen dm-crypt wq | Steven Barrett | Liquorix |
| 0009 | zen evdev rcu | Kenny Levinsen | XanMod |
| 0014 | tkg pci pme timeout | Arjan van de Ven | TKG / Clear Linux |
| 0018 | tkg pci acs override | Mark Weiman | TKG |
| **0015** | nvme APST default | **ours** | outbox |
| **0016** | THP defrag default | **ours** | outbox |
| **0017** | kbuild UBSAN extmod | **ours** | outbox |
| **0019** | dma-buf priority hint | **ours** | outbox, RFC |
| **0020** | dma-buf compressed descriptor | **ours** | held, see below |
| **0021** | PCI/sysfs docs | **ours** | **sent 2026-08-20** |

Submitting the fifteen under our name would be misattribution, which is the
thing `tests/test_patch_authorship.py` exists to prevent. They are also not
ours to relicense, re-date, or speak for in review , if a maintainer asks why
BORE behaves a certain way, the honest answer is that we did not write it.

Several of them are additionally not *wanted* upstream by their own authors:
the XanMod and CachyOS tunings are deliberate downstream divergence, carried
precisely because mainline chose different defaults. TKG's ACS override has
been proposed to LKML repeatedly over more than a decade and declined each
time, for reasons that have not changed.

**They are still published**, and that is the part that satisfies "publish
everything we use". The public collection carries all 21 with attribution
intact and no claim that any was accepted upstream. That is a different and
weaker claim than LKML submission, and it is the correct one for work that is
not ours.

## Readiness for 0015 and 0016 (2026-08-20)

Both were regenerated from real commits on this date. Before that they were
hand-written files carrying fabricated blob hashes and a placeholder date,
which `git apply` tolerates and a maintainer would not.

They are the weakest two of our six: neither fixes a bug, and each changes a
compiled-in default that is already reachable at runtime through the exact
mechanism its own commit message cites. Sending them is defensible, but only
with the missing measurement stated up front , `outbox/SEND.md` records what
each one would need to become persuasive rather than merely reasoned.

## Readiness for 0019 and 0020 (2026-08-18)

`docs/ferran_custom_patches/PUBLISHING.md` covers the mechanics: LKML via `b4`
for originals, the public downloadable collection for all 20 with attribution
intact. This file covers the part mechanics cannot answer, which is whether
each patch is ready to be sent at all.

## Short answer

| Patch | In-kernel reader | Producer of the described state | Recommendation |
|---|---|---|---|
| 0019 dma-buf priority hint | yes, as of today (greenboost.ko T2 eviction) | n/a, the hint is the state | **Send as RFC** |
| 0020 dma-buf compressed descriptor | no | **no** | **Hold. Do not send.** |

## 0019 — send, and lead with the weakness

What changed today: greenboost.ko now *reads* `dma_buf_get_priority()` in its
T2 eviction sweep, so an above-default priority re-orders which already
eligible buffers get reclaimed first. Before today the driver only ever wrote
the value and the patch's own consumer admitted, in a source comment, that the
call changed no behaviour.

That is a genuine improvement to the submission and it does not remove the
blocker.

**The blocker, stated the way the list will state it.** The only user is an
out-of-tree module. Adding UAPI (two ioctls, an fdinfo field, a
`DMA_BUF_PRIORITY_*` range) with no in-tree user is normally declined, and
correctly so: UAPI is forever, and nothing in-tree constrains the semantics or
proves the design survives a second consumer.

Do not attempt to disguise this. A cover letter that leads with "GreenBoost, an
out-of-tree memory-tiering module, uses this and here is exactly how" is
treated far better than one that buries it under motivation and gets found in
review. The realistic outcomes are: the API is redesigned by someone with an
in-tree use case, or it is declined with a "come back with an in-tree user".
Both are useful information.

**Cover letter skeleton.**

1. The problem in one paragraph: an exporter that knows some of its buffers are
   hot has no standard way to say so. Every out-of-tree tiering driver invents
   a private ioctl for it (GreenBoost's `GB_IOCTL_SET_HEAT` is exactly that),
   so nothing generic can act on the signal.
2. What the patch adds: a hint, not a policy. dma-buf core stores an
   advisory 0..255 value, reports it via fdinfo and `GET_PRIORITY`, and
   implements no reclaim of its own. Exporters that *do* have a reclaim path
   may consult it. This is deliberately the smallest possible surface.
3. Who uses it, named plainly as out-of-tree, with the reader described
   concretely: skip-on-threshold-sweep only, never overriding the hard KV-cache
   and frozen-buffer exemptions, so a foreign process cannot use the hint to
   talk the driver into evicting something critical.
4. What is *not* claimed: no throughput number. On the author's hardware the
   eviction path this feeds has not yet fired in production (T2 has never hit
   the critical watermark; lifetime T3 allocations are zero), so there is no
   before/after measurement and it would be dishonest to imply one.
5. The open question for the list: does anyone in-tree want this, and is
   0..255 with a 128 default the right shape, or should it be an enum?

Point 4 costs nothing and buys credibility. A reviewer who finds an
unsupported performance claim stops reading; one who finds an author who
already marked their own evidence gap tends to engage with the design.

## 0020 — hold it

`0020` adds a `{codec, block_size, uncompressed_size}` descriptor so an
exporter that compressed a buffer in place can tell an importer how to inflate
it. It is checkpatch-clean, compile-tested, and stacked correctly on 0019.

**Nothing anywhere produces a compressed dma-buf, and on this stack nothing
could consume one.** Two independent reasons, both verified today:

1. **No consumer is possible without GPU-side decompression.** GreenBoost's T2
   buffers are read directly by the GPU's SMs over the zero-copy
   `cuMemHostRegister` path. Compressed contents would be read as garbage
   unless CUDA-side inflate code exists. The embedded-PTX absmax KV codec that
   would have done this was removed as dead code in the 2026-07-26 audit.
   `gb_moe.py`'s newer lossless compression operates on torch tensors in
   userspace and never touches a `dma_buf`.
2. **The adjacent variant that *would* be useful targets a dead path.** The
   obvious "make it real" move is to compress the T3 (NVMe) eviction payload
   in-kernel, zram-style. That does not need 0020 at all, since the pages are
   freed on evict and inflated on promote before any importer sees them, and
   more decisively: `tier_t3_local_lifetime_mb=0`,
   `tier_t3_local_alloc_count=0`. T3 has never been used on this machine, not
   once, so compressing its payload would optimise a path that has never run.

Submitting UAPI with zero users, zero producers and no measurement is worse
than not submitting. The repo rule already covers this: *only patch what you
can prove helps.* Keep 0020 in the local series where it costs nothing, ship it
in the public collection as a documented proposal, and revisit if GPU-side
decompression ever lands.

## Both: keep them in the public collection either way

The downloadable collection is a different contract from LKML. It carries all
20 patches with attribution intact and no implied claim that anything was
accepted upstream. 0020 belongs there today; it does not belong on the list
today.
