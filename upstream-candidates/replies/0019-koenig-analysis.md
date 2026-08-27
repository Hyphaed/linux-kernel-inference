# What Christian König's reply actually says, and what can be said back

Thread: `[RFC PATCH] dma-buf: add a generic reclaim-priority hint`
His reply: 14:24, 25-Aug-2026, `christian.koenig@amd.com`, In-Reply-To the
**original** 20:08 posting — not either of the two follow-ups sent on the 20th
at 21:53 and 22:04. He may not have read them.

## The word is "forbidden", not "forgiven"

"Well to start exporting pages pinned with FOLL_LONGTERM as DMA-buf is
absolutely \*STRICTLY\* forbidden."

And his closing line is garbled but unambiguous in intent: "as long as you can
demonstrate any of this with an in tree driver the whole approach is not
something we would discuss in the first place" means *unless* you can
demonstrate this with an in-tree driver, we won't discuss it.

## Reason 1: why FOLL_LONGTERM export is forbidden

Checked against the 7.1.10 tree, not from memory.

- `include/linux/mm_types.h:1883` — `FOLL_LONGTERM` means the page is held
  "for an indefinite time period". An indefinitely pinned page cannot be
  migrated.
- `mm_types.h:1829` — "long term pins in a CMA region would unnecessarily
  fragment that region. And so, CMA attempts to migrate the page before
  pinning."
- `mm/gup.c:2386` — `check_and_migrate_movable_pages_or_folios()` exists to
  move pages *out* of movable zones before a longterm pin is granted. Losing
  migratability breaks CMA, memory hot-remove and compaction.
- `mm_types.h:1820` — for file-backed pages the pin also fights the
  filesystem's own lifetime rules.

dma-buf's contract runs the other way. `dma_buf_ops.pin` is documented as
telling the exporter "the DMA-buf can't be moved any more", and `dma_buf_pin()`
says plainly: *"It is not permitted to allow userspace to pin arbitrary amounts
of buffers through this interface."*

**Verdict: not rebuttable.** He is right, the tree backs him, and arguing would
be arguing with `mm/gup.c`.

**And it lands on us.** `greenboost.c:1165` does exactly this:

```c
ret = gb_pin_user_pages(vaddr, np, FOLL_WRITE | FOLL_LONGTERM, buf->pages);
```

`gb_pin_user_buf()` is reached from `GB_IOCTL_PIN_USER_PTR`, and the result is
handed to `dma_buf_export()`. That is a userspace-triggerable, unaccounted,
un-migratable pin exported as a dma-buf — the thing he called strictly
forbidden. It is a real design problem in our own module, independent of
whether 0019 ever goes anywhere.

The one honest distinction worth keeping: `GB_IOCTL_ALLOC` allocates
kernel-side pages and is a different path, and **0019 itself never required
FOLL_LONGTERM at all**. The patch adds an `atomic_t` and two ioctls; it is
indifferent to how the exporter backs its memory. The FOLL_LONGTERM sentence
was our framing in the commit message, and it was a bad one — it invited
exactly this objection while not being load-bearing for the patch.

## Reason 2: why "no in-tree user" ends the discussion

UAPI is permanent. Ship `DMA_BUF_IOCTL_SET_PRIORITY` and ioctl numbers 4/5,
the `pad` semantics and the 0..255 range are frozen for good, with the only
definition of what any of it means living in an out-of-tree module no
maintainer can review, fix or delete.

**Verdict: not rebuttable, and already conceded.** The 22:04 follow-up said:
"If the answer is 'come back with an in-tree user', that is a useful answer and
I will take it." He gave that answer. Re-arguing it now would retract our own
sentence.

## The benchmark numbers do not exist

Asked for: "give benchmark numbers of using it vs not using it."

There are none, and the counter that would produce them reads zero.

GreenBoost exposes the hint's effect directly —
`/sys/class/greenboost/greenboost/status` prints
`Evict spared by dma-buf priority`, fed by `atomic64_inc(&gb_dev.prio_spared)`
at `greenboost.c:3058`, the one site that consumes `dma_buf_get_priority()`.
Measured on 7.1.10, 2026-08-25:

```
T2 allocated                     : 0 MB  (0%)
Active DMA-BUF objects           : 0
Evict spared by dma-buf priority : 0
```

Nothing is tiered, so nothing has ever been spared. The hint has changed the
outcome of zero eviction decisions on this machine.

Worse for the argument: the governed layer says decode here is capped by
something the hint cannot touch. `weights_dont_fit_vram` — 15.85 GB of weights
against 12227 MiB of VRAM, so the overflow crosses PCIe every forward pass
"regardless of tuning". Measured this session: **1.1–1.3 tok/s over 3 samples**,
against a 7.1.8 baseline of median 1.5 / min 1.2 / max 5.7 over 34 samples.
Re-ordering *which* already-evictable buffer goes first does not move that
number, and no honest experiment is going to show it does.

*(Source: semantic layer via `dataflux_critic` · Owner: gb_semantics ·
Governed: true)*

So a mail claiming a measured benefit would be inventing one, to the
maintainer who has just declined the patch, on a thread where a previous
patch of ours was already called out for asserting things it could not
support. That is the 0016 mistake with the volume turned up.

## Other patches worth citing — and their limit

Already cited in the 22:04 follow-up, and they remain the strongest material:

- **TTM** keeps a per-BO priority (`TTM_MAX_BO_PRIORITY`, four levels) with one
  LRU per level and ascends them during eviction. The kernel has already
  decided per-buffer eviction ordering is worth having.
- **`DRM_IOCTL_PANFROST_MADVISE`**, **`DRM_IOCTL_MSM_GEM_MADVISE`**,
  **`DRM_IOCTL_VC4_GEM_MADVISE`** — the same question answered three separate
  times at UAPI level, once per driver.

The limit, which the follow-up stated itself: none of those act on an
*exported dma-buf*. They are all driver-internal. They show the need recurs;
they are not an in-tree consumer of a dma-buf-level hint. They do not close
the gap he named.

## What is actually left to say

Two things, and only two:

1. Concede the FOLL_LONGTERM framing. It was our error, it is genuinely
   instructive, and saying so costs nothing.
2. Ask the one question he opened himself. He wrote that an exporter migrating
   a buffer between local memory, system memory and swap "can be done", and
   that it "could be interesting to implement something like that for backing
   some Vulkan extension". Asking whether *that* is the shape he'd expect, if
   an in-tree consumer ever appeared, is a legitimate question rather than
   re-litigation — and the answer is worth having before anyone spends time
   on a v2.

Draft: `0019-koenig-reply.txt`. Not sent.

## If the goal is to actually revive this

The blocker is evidence, not argument, and the order is fixed:

1. Get GreenBoost tiering something. `T2 allocated: 0 MB` means the shim
   isn't spilling; `prio_spared` cannot move until it is.
2. A/B with the hint honoured versus ignored, enough repetitions that the
   delta clears the 1.2–5.7 tok/s spread the baseline already shows.
3. If the delta is real, it is evidence. If it is not — which the
   `weights_dont_fit_vram` arithmetic predicts — then the patch has no
   demonstrated benefit and this repo's own MUST-RULE #3 says it should not
   be sent, by us or anyone.

None of that changes the in-tree-user blocker. It only decides whether there
is anything worth building an in-tree user *for*.
