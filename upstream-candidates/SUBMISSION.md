# Submission status

## Sent to LKML

| Patch | Sent | Lists | Message-ID | Thread |
|---|---|---|---|---|
| `0021` PCI/sysfs docs (v1) | 2026-08-20 20:42 CEST | linux-pci, linux-api, linux-kernel | `20260820184228.166566-1-ferran.duarri@me.com` | <https://lore.kernel.org/linux-pci/20260820184228.166566-1-ferran.duarri@me.com/> |
| `0017` kbuild UBSAN extmod | 2026-08-20 21:01 CEST | linux-kbuild, linux-kernel | `20260820190200.203185-1-ferran.duarri@me.com` | <https://lore.kernel.org/linux-kbuild/20260820190200.203185-1-ferran.duarri@me.com/> |
| `0016` THP defrag default | 2026-08-20 21:08 CEST | linux-mm, linux-kernel | `20260820190825.221308-1-ferran.duarri@me.com` | <https://lore.kernel.org/linux-mm/20260820190825.221308-1-ferran.duarri@me.com/> |
| `0019` dma-buf priority hint (RFC) | 2026-08-20 21:08 CEST | dri-devel, linux-media, linaro-mm-sig, linux-kernel | `20260820190838.221435-1-ferran.duarri@me.com` | <https://lore.kernel.org/dri-devel/20260820190838.221435-1-ferran.duarri@me.com/> |
| `0015` nvme APST default | **2026-08-20 21:44 CEST — sent 4x, see correction below** | linux-nvme, linux-kernel | `20260820194417.269110-1-ferran.duarri@me.com` (+3 dupes/resends) | <https://lore.kernel.org/linux-nvme/20260820194417.269110-1-ferran.duarri@me.com/> |
| `0021` v3 PCI/sysfs docs | 2026-08-21 10:33 CEST | linux-pci, linux-api, linux-kernel | `20260821083353.444300-1-ferran.duarri@me.com` | <https://lore.kernel.org/linux-pci/20260821083353.444300-1-ferran.duarri@me.com/> |

All sent with `git send-email` via `smtp.mail.me.com`, SMTP result 250, To: the
subsystem maintainers with the lists in Cc:. `0020` is deliberately held, and
this file explains why further down.

**Correction, 2026-08-31: this table was wrong on two rows for 10 days.** It
said "0015 is the last of ours still unsent" and never listed v3 of 0021. Both
were sent — the mailbox is the ground truth, this file was not kept in sync
with it, the exact failure this file's own v2 postmortem already named once
("a send is not done until its message-id is in this table" — that rule was
stated after the first occurrence and not enforced after the second). Real
timeline, read from the mailbox directly:

* `0021` v1 sent 20:42, v2 sent **twice** (21:53 and 22:03 — Greg KH's "You
  sent 2 v2 patches 🙁" was correct), v3 sent 2026-08-21 10:33 to Bjorn Helgaas
  and Ilpo Järvinen, threaded correctly off v1. **No reply to v3 yet** as of
  2026-08-31 (10 days).
* `0015` was sent **four times**, unthreaded, all as fresh `[PATCH]` (never
  versioned `v2`/`v3`) to the same four maintainers + linux-nvme: 21:44, 21:54,
  22:04 on 2026-08-20, and again 2026-08-28 11:48 — eight days after the first
  three, still unthreaded. **Alexey Bogoslavsky (SanDisk, wrote the current
  APST algorithm in 2021) replied 2026-08-27** to the 21:54 copy with a
  substantive technical objection — see "0015 — the SanDisk reply" below. This
  reply predates the fourth send, so the fourth send went out to someone who
  had already told us the direction was wrong. That should not have happened.
* `0019`'s RFC got a real, unaddressed reply from Christian König (dma-buf
  co-maintainer) on 2026-08-25, 14:24 CEST — 6 days unanswered as of
  2026-08-31. A reply is drafted (`replies/0019-koenig-reply.txt`,
  `replies/0019-koenig-analysis.md`) but was never sent. See below.

`0019` went out as `RFC PATCH` rather than `PATCH`, deliberately: it adds UAPI
whose only consumer is out-of-tree, and the reply to write is the one that says
so first. `linaro-mm-sig` is moderated, so a post from a non-subscriber waits
in a queue rather than bouncing , silence on that list is not a delivery
failure.

**Archival is recorded here but not independently confirmed.**
`lore.kernel.org` sits behind an Anubis proof-of-work wall that refuses both
`curl` and an automated browser, so the thread URLs above are constructed from
the message-ids rather than fetched. The message-ids themselves come from
`git send-email`'s own output, and every send Cc'd `ferran.duarri@me.com`, so
the copy in that mailbox is the delivery evidence. Open the URLs in a normal
browser to confirm the lists accepted them.

**Four threads have review on them now, not two.** `0016` and `0019` were
already recorded here. `0015` and `0017` were not — both found real problems,
added 2026-08-31 after actually reading the mailbox instead of assuming
silence meant nothing had arrived.

## `0017` , DECLINED 2026-08-21. Maintainer said no, seconded, never answered.

Nathan Chancellor (kbuild maintainer): *"I am open to other opinions but I am
not inclined to apply this change. If an external module has issues with
these checks, it should either be fixed or `UBSAN_SANITIZE := n` can be added
to the module's Makefile, rather than making the default worse for everyone
else, especially given the importance of UBSAN_BOUNDS."* Nicolas Schier
seconded: *"Yes, I second that."*

Both maintainers are right on the tradeoff as stated: the patch traded a
security-hardening default away from *every* external module to fix one
(VMware's vmnet/vmmon). The narrower fix Nathan named —
`UBSAN_SANITIZE := n` in VMware's own Makefile — is not ours to write, and
isn't the point; the point is the kernel's default should not have moved for
it. No reply has been sent. The honest close here is a one-line concession,
not a v2 — there is no version of this patch that answers "why should this
regress UBSAN_BOUNDS for everyone else" differently than Nathan already did.

## `0015` , the SanDisk reply, 2026-08-27 , not yet answered

Alexey Bogoslavsky, SanDisk, who states he wrote NVMe's current APST algorithm
in 2021, replied with a real technical objection, not a process one:

* Client-device EXLAT/ENLAT figures follow Microsoft's own power-management
  guidance, and vendors sometimes advertise ps4's latency **higher than its
  real value on purpose**, specifically so Windows keeps selecting ps3 instead.
  If that is true of this drive, the patch's own before/after numbers measured
  a real effect against a knowingly-inflated spec figure — the measurement
  methodology is not in question, what the spec number *means* is.
* Transitional energy is not fully captured by the advertised power-state
  numbers either.
* His recommendation: Linux should keep tracking the Windows-aligned default,
  because that is what client hardware is tuned against; a user who genuinely
  needs the latency bound should disable power management explicitly (the
  Windows "performance mode" equivalent), not get it as the kernel default.
  He states some OEMs found a similar change unacceptable before he introduced
  the current APST algorithm.

This is a domain-authority objection to the patch's actual premise, arrived
after the patch had already been sent a fourth time. It has had no reply for
four days as of 2026-08-31. Whether to concede, ask for the OEM precedent he
references, or defend on the grounds that the 990 EVO Plus measurement stands
regardless of *why* ps4 is labelled 43ms, is a real technical call this file
does not make unilaterally.

**Reply sent 2026-08-31 13:17 UTC**, message-id
`20260831131701.200793-1-ferran.duarri@me.com`, confirmed live on
`lore.kernel.org/linux-nvme/`. Concedes the EXLAT-inflation point, notes the
measurement itself still holds regardless of *why* the spec number is what it
is, asks Alexey for the OEM precedent, and apologizes for the fourth
duplicate send. No reply yet as of this check.

## `0016` , WITHDRAWN 2026-08-21. The commit message had the mechanism backwards.

Andrew pointed an AI review at it (sashiko.dev). Its High-severity finding was
correct, and verified here against `mm/huge_memory.c` rather than taken on
trust.

The patch claimed that under `transparent_hugepage=always` a faulting thread
"can stall in compaction". It cannot. `vma_thp_gfp_mask()` under the current
`madvise` defrag default returns `GFP_TRANSHUGE_LIGHT` with **no reclaim flag
at all** for a non-madvised VMA , it fails fast. The only faults that enter
direct reclaim are `MADV_HUGEPAGE` regions, and `defer+madvise` keeps
`__GFP_DIRECT_RECLAIM` for exactly those. **The patch removes no stall.**

What it does change is the other branch: non-madvised faults *gain*
`__GFP_KSWAPD_RECLAIM`, which they did not have , strictly more background
work, waking kswapd/kcompactd on failed THP allocations across every anonymous
fault under THP=always. On a fragmented machine that is a plausible regression.

`Documentation/admin-guide/mm/transhuge.rst:189` says it plainly: madvise
"will enter direct reclaim like ``always`` but only for regions that are have
used madvise(MADV_HUGEPAGE)". Reading that first would have prevented the
patch.

It also had no measurement , `thp_fault_fallback` was 0 across 60682
huge-page faults, so the condition was never reached. It rested entirely on a
correctness argument, and the correctness argument was wrong.

**Lesson, worth more than the patch:** the commit message asserted a code path
without reading the function that implements it. The measurement gate this repo
already had ("only patch what you can prove helps") did not catch it, because
the claim was about mechanism, not effect. `outbox/0016-*.patch` is not coming
back without fault-latency percentiles at `madvise` vs `defer+madvise` on a
machine that actually reaches fallback.

Withdrawal drafted at `replies/0016-withdrawal.txt`.

## `0021` , the full version history, written down because it got confusing

**v1** , sent 2026-08-20 20:42 CEST, message-id
`20260820184228.166566-1-ferran.duarri@me.com`.

**Why there is a v2 at all.** v1 described `max_link_speed` **backwards**. It
called it "the ceiling the link may negotiate, which is the lower of what the
two ends of the link support", then contradicted itself one sentence later.
`max_link_speed_show()` calls `pcie_get_speed_cap()`, which returns the
capability of **the device being read** and never consults the other end. A
documentation patch is a claim about behaviour, and that claim was false , in
an ABI file, about an attribute exported since 2018. That is the whole reason
v2 exists. Four more corrections rode along:

1. v1 said the value comes from Max Link Speed in Link Capabilities.
   `pcie_get_supported_speeds()` derives it from the Supported Link Speeds
   Vector in Link Capabilities **2**, masks it against Max Link Speed, and
   falls back to Max Link Speed alone only pre-PCIe-r3.0.
2. v1 never said the value is cached at enumeration in
   `pci_dev->supported_speeds`. Since v1's `current_link_speed` entry says
   nothing is cached there, a reader would infer the same for
   `max_link_speed`. It does not hold.
3. v1 told callers wanting the ceiling to read `max_link_speed`. That
   overestimates whenever the upstream port is the slower end.
4. v1 shipped a private `Forward-Port-Notes:` trailer inside the commit
   message , the same defect commit `9d2a71a` had already cleaned out of
   `0015` and `0016`. Nobody re-checked `0021`.

**v2** , sent 2026-08-20 ~22:03 CEST, about eighty minutes after v1. The
review reply came back with "You sent 2 v2 patches 🙁".

**Why the same-evening churn.** These patches were written over several weeks
and sat in this outbox undecided. On 2026-08-20 the backlog was released in one
sitting, and `0021` was reviewed properly only AFTER v1 had gone out , which is
the whole reason v1 and v2 landed the same evening. The sequencing was the
error: the review that produced v2 belonged before the send, not after it. A
patch that has sat for weeks can wait one more day for its own author to read
it.

Whether two copies of v2 *itself* also arrived cannot be settled from here ,
lore is behind bot protection and v2's message-id was never recorded. If it
happened, both copies were byte-identical; there was never more than one v2.

Two process failures either way, both now closed:

* Its message-id was never recorded here. Every other send in the table above
  has one; this one was written up as "Pending: send v2" in `outbox/SEND.md`
  and then sent from a different session, so the file said unsent while lore
  said otherwise. **A send is not done until its message-id is in this table.**
* Nothing checked the outbox for duplicate Subjects before a send.
  `tools/patch-audit` now refuses on exactly that.

**v3** , not yet sent. Content identical to v2. It exists to carry the
`Assisted-by:` tag (see below) and to be the single, unambiguous replacement
for the two v2s. Threaded to v1's message-id, which keeps the whole series in
one thread , v2's own message-id is not recoverable from anything we kept.

### The `Assisted-by:` tag , what went wrong

Bjorn also asked: *"Did you forget the Assisted-by: tag?"* Yes.

Every patch in this tree was written with an AI coding assistant.
`Documentation/process/coding-assistants.rst` , which is in the tree we build
against , requires that to be declared as
`Assisted-by: AGENT_NAME:MODEL_VERSION [TOOL1] [TOOL2]`, and `checkpatch.pl`
validates the format. `0016`, `0017`, `0019` and `0021` v1/v2 all went out
without it.

Nothing on our side checked. `tests/test_patch_authorship.py` enforced `From:`
and `Signed-off-by:` and placement, and simply had no rule for this. It does
now, scoped to the outbox , patches under `sent/` are the historical record of
what actually reached a list, and rewriting them to match a rule they were sent
without would make our archive disagree with lore's. They get the tag in their
next revision instead, which is what v3 is.

The tag we use:

    Assisted-by: Claude:claude-opus-5 checkpatch patch-audit

`Signed-off-by:` stays human and unchanged , the DCO can only be certified by
a person, and `coding-assistants.rst` says so explicitly.

`0021` was told its documentation describes `max_link_speed` backwards, which
checking `pcie_get_speed_cap()` confirmed , a v2 is prepared in
`outbox/0021-v2-*.patch` and `outbox/SEND.md` lists all five corrections.
`0019` was told `DMA_BUF_IOCTL_GET_PRIORITY` is `_IOR` and never validates the
`pad` field it documents as reserved, which is also correct; the reply in
`replies/0019-followup.txt` accepts it and defers the v2 until the shape
question is answered, since changing the direction bits changes the ioctl
number.

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
| **0016** | THP defrag default | **ours** | **sent 2026-08-20** |
| **0017** | kbuild UBSAN extmod | **ours** | **sent 2026-08-20** |
| **0019** | dma-buf priority hint | **ours** | **sent 2026-08-20**, RFC |
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
