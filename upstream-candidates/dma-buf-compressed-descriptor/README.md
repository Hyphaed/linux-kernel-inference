# dma-buf: generic compressed-content descriptor (RFC candidate)

Status: **drafted, compile-tested, checkpatch-clean, signed off, NOT sent
anywhere yet.** `git send-email` is the actual send step and it hasn't been
run — publishing is a command you run yourself (walkthrough below), never
something done on your behalf.

Sibling of `../dma-buf-priority-hint/` — read that one first if you haven't;
this patch assumes the same dma-buf/pinning background it explains and
doesn't repeat it here.

## What this patch does, in plain English

The sibling patch (`0019`) added a "keep me" / "let me go" sticky note that
any program can attach to a piece of memory it's sharing with a GPU or other
hardware. This patch adds a second, related sticky note: "this is
compressed, here's how to read it back."

Here's the gap it closes. Say a program is juggling more data than fits in
fast memory, and has decided, using `0019`'s sticky note or its own logic,
that a particular chunk is worth keeping around rather than throwing away.
"Keeping it" doesn't have to mean keeping it at full size — the program
could shrink it by compressing it in place, the way you'd zip a folder
instead of deleting it. That's often a better trade than throwing it away
and fetching a fresh copy later: fetching a fresh copy means sending the
whole thing again over a comparatively slow connection between two pieces of
hardware, while unzipping something already sitting in fast memory is
comparatively instant.

The catch: once a chunk of memory is compressed, whoever eventually wants to
use it again needs to know it's compressed and how to un-compress it. Right
now there's no shared, standard way to say that — so, exactly like the gap
`0019` closed, every program that wants this has to invent its own private
signal.

This patch adds one small, shared descriptor: a compressed chunk of memory
can carry a label saying "I'm compressed, using method X, here's how big I
am once un-compressed," using one standard method built into Linux instead
of a private one. It does nothing by itself — it doesn't compress or
decompress anything, and no existing program's behavior changes unless that
program chooses to use the new label. It's the "how do I read this back"
half of the "should I keep this" question `0019` already answers.

## Who this could help — not just GreenBoost

Same generic mechanism, same reach as `0019`:

- **Any GPU memory-tiering system** (this patch's own motivating case) —
  compressing cold data in place instead of evicting it, so a later
  "promote this back" is a local decompress instead of a fresh, slower
  transfer over the host↔device link.
- **Video/camera pipelines** that already compress frames (many capture
  formats are compressed on the wire) and want a standard way to tell a GPU
  or encoder downstream how to read them, instead of a codec-specific
  side-channel.
- **Compositors** holding buffers for minimized or off-screen windows —
  compress those in place rather than paying full VRAM for content nobody's
  looking at.
- **Any future dma-buf exporter** that wants "compressed, in place" as an
  option without inventing its own descriptor from scratch.

None of these need to know about each other, or about `0019`'s hint, or
about GreenBoost — that's the point of putting this in `dma-buf` itself.

## Why this is a genuine improvement, not scope creep

It stays disciplined the same way `0019` did: it's a **descriptor**, not a
**codec** and not a **policy**. The kernel doesn't gain a compression
algorithm, doesn't gain an opinion about when to compress, and doesn't gain
a new eviction policy — it gains one more standard place to write down a
fact an exporter already knows, so importers don't have to guess or invent
their own way to ask. `@codec` is deliberately just a number the kernel
never looks inside — exporter and importer agree on what it means between
themselves, the same way a video file's compression format is a number in
its header that the file format doesn't interpret either.

## Real, working motivation on this machine

GreenBoost (the same sibling project that motivated `0019`) manages AI model
memory that doesn't fit in GPU VRAM. Part of that system currently demotes
memory it isn't actively using by shrinking it with a lossy technique (some
precision is thrown away and never comes back exactly the same). Swapping
that for an in-place compression that loses nothing, and using this new
descriptor to record what happened, means: memory that gets less use stays
compressed and takes up less room, and if it's needed again, getting it back
is quick and exact, a local decompress, not a fresh fetch across the slower
connection to the GPU, and not degraded either.

That specific piece of GreenBoost isn't wired up to use this descriptor yet
as of this writing (see `EXPLAINER.md`'s "Current status" section) — this
patch is the generic groundwork, tracked separately from that consumer work
so each can be reviewed on its own terms, the same separation `0019`
maintained between "the hint" and "GreenBoost's specific use of it."

## Suggested next test (same shape as `0019`'s open item)

A live round-trip: create a `udmabuf`-backed dma-buf, call
`DMA_BUF_IOCTL_SET_COMPRESSION` from userspace with a made-up codec/size,
read it back with `DMA_BUF_IOCTL_GET_COMPRESSION`, confirm it matches, check
`/proc/<pid>/fdinfo/<fd>` shows the `compression:` line. Needs a real boot;
worth doing before sending, same reasoning `0019`'s README gives.

## How to send it (once you're ready)

Same environment, same tooling as `0019` — see `../UPSTREAMING.md` for the
generalized procedure and `../dma-buf-priority-hint/README.md`'s "1. Install
and configure git-send-email" section for the concrete Gmail App Password
walkthrough already worked out on this machine. Summarized here for this
specific patch:

### 1. Final review

```bash
perl scripts/checkpatch.pl --no-tree \
  ~/Dev/kernel_inference/upstream-candidates/dma-buf-compressed-descriptor/0001-*.patch
```

Already clean (0 errors, 0 warnings) as of this drafting — re-run fresh
before sending regardless, per `../UPSTREAMING.md`'s standing instruction.

### 2. Recipients — from this tree's own `get_maintainer.pl`, not guessed

Identical list to `0019` (same subsystem, same files):

```bash
git send-email \
  --to="Sumit Semwal <sumit.semwal@linaro.org>" \
  --to="Christian König <christian.koenig@amd.com>" \
  --cc="linux-media@vger.kernel.org" \
  --cc="dri-devel@lists.freedesktop.org" \
  --cc="linaro-mm-sig@lists.linaro.org" \
  --cc="linux-kernel@vger.kernel.org" \
  ~/Dev/kernel_inference/upstream-candidates/dma-buf-compressed-descriptor/0001-*.patch
```

### 3. Consider sending as a 2-patch series with `0019`

Both patches touch the same 3 files, the same subsystem, and share a
motivation (an exporter's opinion about a buffer it's decided to keep). A
cover letter explaining that shared motivation, with `0019` as patch 1/2 and
this as patch 2/2, is more useful to reviewers than two independent threads
— `git format-patch -2 --cover-letter` or `b4 prep` (see `../UPSTREAMING.md`)
handle a series naturally. Not required — either patch stands alone — but
worth doing since they were, in fact, designed together.

### 4. After sending

Same expectations as `0019`'s README: permanent public archive on
lore.kernel.org, `linaro-mm-sig` moderation delay possible for a first post,
track on Patchwork, no reply within a couple of weeks is normal, reply
in-thread with `--in-reply-to` for a v2 rather than starting fresh.

### One disclosure to decide on

Same note as `0019`: this patch was drafted with AI assistance. No
single kernel-wide rule on disclosing that as of this writing — your call
on phrasing, since you're the one signing off with your real name and
email.
