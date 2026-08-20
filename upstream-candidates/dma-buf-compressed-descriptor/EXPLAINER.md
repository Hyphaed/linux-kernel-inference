# dma-buf compressed-content descriptor — line-by-line explainer

Audience: same as `../dma-buf-priority-hint/EXPLAINER.md` — this doc assumes
you've read that one (dma-buf fundamentals, pinning, why reclaim can't just
happen automatically) and picks up from there rather than repeating it.

---

## Part 1 — what's actually missing

`0019` gave an exporter a way to say "keep this over that." This patch
answers the next question: once something is being kept, does it have to
stay at full size?

Not necessarily — an exporter can compress a buffer's contents in place
instead of freeing them, and un-compress on demand. For **immutable**
content (model weights that don't change mid-session are the motivating
case, but any read-only buffer qualifies), this is strictly better than
evicting and re-fetching: decompression from memory already local to the
device is fast; re-fetching means re-sending the same bytes across a slower
host↔device link, bytes that were already sent once before.

The gap: once compressed, an importer has no generic way to know. Grep
before drafting confirms it — no codec/compression descriptor anywhere on
`struct dma_buf` or `struct dma_buf_ops`.

## Part 2 — the concrete motivating case

GreenBoost (`~/Dev/greenboost_all/greenboost`, the same sibling project
`0019` cites) manages Mixture-of-Experts model memory in `gb_moe.py`: cold
experts (ones not recently used) get demoted out of VRAM to make room for
hot ones. As of this session, that demotion used
`gb_quant.quantize_module(module, "int4")` — a lossy technique. An expert
promoted back to VRAM later comes back int4-degraded, not bit-identical to
the original.

The fix this patch enables: demote by compressing losslessly instead, and
publish the result through this descriptor. Two wins stack: the expert
returns bit-exact when promoted (no quality loss), and promotion becomes a
local decompress instead of a fetch across the PCIe link — for an immutable
buffer, eliminating the transfer outright rather than merely shrinking it.

**Current status**: this patch is the generic kernel-side groundwork. The
GreenBoost consumer — replacing the lossy int4 path, calling
`dma_buf_set_compression()` at the same three `dma_buf_export()` sites
`0019`'s priority hint already touches — is tracked as separate work in that
repository, not bundled into this patch. Same separation of concerns `0019`
maintained between "the hint" and "GreenBoost's use of it."

## Part 3 — walking through the diff

### 3.1 `include/uapi/linux/dma-buf.h` — the userspace contract

```c
struct dma_buf_compression {
	__u32 codec;
	__u32 block_size;
	__u64 uncompressed_size;
};

#define DMA_BUF_CODEC_NONE		0
```

`codec` is opaque to the kernel — a plain number exporter and importer agree
on out of band, exactly like `DRM_FORMAT_MODIFIER` or a V4L2 pixel-format
fourcc. `DMA_BUF_CODEC_NONE` (0) is the only value this patch defines; it
means "not compressed," which is also the default for every buffer that
never calls the setter. `block_size` lets an exporter describe block-wise
compression (0 means "whole buffer, one block"). `uncompressed_size` is what
an importer allocates before decompressing.

```c
#define DMA_BUF_IOCTL_SET_COMPRESSION	_IOW(DMA_BUF_BASE, 6, struct dma_buf_compression)
#define DMA_BUF_IOCTL_GET_COMPRESSION	_IOR(DMA_BUF_BASE, 7, struct dma_buf_compression)
```

Numbered 6/7, not 4/5, because `0019` in this same series already claims 4/5
for `SET_PRIORITY`/`GET_PRIORITY`. This is the one place in the patch where
its existence as a *sibling* of `0019`, not a standalone patch, is
load-bearing — sending this without `0019` would need renumbering (or the
maintainers would simply assign the next free slot; either is fine, this is
a userspace-visible detail worth flagging in the cover letter, not a defect).

### 3.2 `include/linux/dma-buf.h` — the kernel-internal contract

```c
struct dma_buf_compression_hint {
	u32 codec;
	u32 block_size;
	u64 uncompressed_size;
} compression;

spinlock_t compression_lock;
```

Same three fields, kernel-internal type (`u32`/`u64` vs. the uapi struct's
`__u32`/`__u64` — standard kernel convention, the uapi header uses the
`__`-prefixed fixed-width types safe for a userspace-facing ABI, the
internal one doesn't need to).

**The one real design choice in this patch**: `0019`'s `priority` field is a
bare `atomic_t` with no lock, because it's a single value where a stale read
is harmless. This descriptor is three fields that form one logical unit — a
reader must never observe `codec` from one `dma_buf_set_compression()` call
paired with `block_size` from a different, earlier or later, call. That's
what `compression_lock` (a small dedicated spinlock, not a reuse of the
existing `name_lock`, which guards unrelated state) exists to prevent. It's
taken for the shortest possible critical section in both the setter and
getter — a plain field copy, no allocation, no sleeping.

### 3.3 `drivers/dma-buf/dma-buf.c` — the implementation

`dma_buf_set_compression()` / `dma_buf_get_compression()` — lock, copy the
three fields as a unit, unlock. `EXPORT_SYMBOL_NS_GPL(..., "DMA_BUF")`, same
namespace `0019`'s accessors use, so a module already importing that
namespace for the priority hint needs no additional `MODULE_IMPORT_NS()`.

The ioctl handler gains a `struct dma_buf_compression comp;` local and two
new `case` arms, placed after `0019`'s `SET_PRIORITY`/`GET_PRIORITY` cases
and before the `CONFIG_SYNC_FILE` block — copy-from-user, validate nothing
extra is needed (a codec value is just stored, not interpreted, so there's
no invalid range to reject the way `0019` rejects `priority >
DMA_BUF_PRIORITY_MAX`), call the setter/getter, copy-to-user.

`dma_buf_show_fdinfo()` gains a conditional `compression:` line, printed
only when `codec != DMA_BUF_CODEC_NONE` — an uncompressed buffer (the
overwhelming majority, since this is opt-in) produces zero extra fdinfo
output, unlike `0019`'s `priority:` line which always prints since every
buffer has *some* priority value.

`dma_buf_export()` gains one `spin_lock_init(&dmabuf->compression_lock);`
call. The `compression` struct itself needs no explicit reset — `dmabuf` is
allocated via the existing `kzalloc()`, so `codec` starts at `0`
(`DMA_BUF_CODEC_NONE`) for free, the same way `0019`'s `priority` field
needs an explicit `atomic_set(..., DMA_BUF_PRIORITY_DEFAULT)` precisely
*because* its default (128) isn't zero — this patch's default happens to be
zero, so it's implicit.

### 3.4 A note on how this patch was actually built, worth keeping visible

An early draft was authored against a clean (no-`0019`) tree. By inspection
its hunks looked non-overlapping with `0019`'s — different functions,
different struct regions. They were not, in the sense that matters: `0019`
*inserts* lines, which shifts every line number below the insertion point,
and a patch's context lines are matched by content, not intent. Stacked for
real via `git am --3way`, that draft produced three conflicts (`dma-buf.c`
and both headers) at exactly the spots where `0019`'s insertions landed
inside this patch's context radius.

The fix was mechanical once identified: re-author the same logical change
directly on top of a tree with `0019` already applied, so every hunk's
context reflects what's actually there when the two are stacked, and
re-verify with a real sequential `git am --3way` of the whole series rather
than `patches/eval.py`'s per-patch check (which validates each patch against
the same static base independently, and would not have caught this). Left
here, not scrubbed from the history, because it's a useful, concrete example
of the gap between "these two diffs don't textually overlap" and "these two
patches apply together" — the second is what actually matters, and the only
way to know it's true is to actually do it.

## Part 4 — anticipated review questions

- **"Why not fold this into the codec's own driver instead of dma-buf
  core?"** Because the codec producing the compressed data and the driver
  consuming it are frequently different modules (a memory-tiering allocator
  compressing, a GPU driver's importer decompressing) with no other shared
  channel — the same argument `0019` makes for priority.
- **"Why no codec ID registry in this patch?"** Deliberately deferred, the
  same way DRM format modifiers weren't all pre-allocated on day one.
  Defining a registry before there's more than one real out-of-tree user to
  agree on it risks guessing wrong; `DMA_BUF_CODEC_NONE` is the only value
  that has to be right on day one, and it is (0, "not compressed," matches
  every existing buffer's default state).
- **"Does this belong under a cgroup, like some reclaim-adjacent
  interfaces?"** No — this isn't a reclaim policy, it's a fact an exporter
  already knows about its own buffer. No cgroup involvement needed, same as
  `0019`.
- **"Why two ioctls instead of folding into the existing sync ioctl?"**
  Matches `0019`'s precedent and the existing `DMA_BUF_SET_NAME` pattern —
  one ioctl per orthogonal piece of metadata, not an ever-growing multiplexed
  one.
