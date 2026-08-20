# dma-buf reclaim-priority hint — a from-first-principles explainer

Audience: this doc assumes you can read C and know roughly what a kernel
module is, but assumes NOTHING about dma-buf, memory reclaim, or pinning.
By the end you should be able to review this patch (or one like it)
yourself.

---

## Part 1 — the concepts, before the code

### 1.1 What is `dma-buf`?

Modern computers move large chunks of memory between hardware components
that don't otherwise know about each other: a GPU, a video capture card, a
network card doing zero-copy receive, another GPU on a different driver
stack. Before `dma-buf` existed (it landed in Linux 3.3, 2012), every pair
of drivers that wanted to share a buffer had to invent its own private way
to do it.

`dma-buf` (`drivers/dma-buf/dma-buf.c`, `include/linux/dma-buf.h`) is the
generic answer: any driver that owns some memory can **export** it as a
`struct dma_buf`, which shows up to userspace as a plain file descriptor.
Any OTHER driver (or userspace, via `mmap()`/`ioctl()` on that fd) can then
**import** it, without either side needing to know how the other allocated
the memory. This is exactly the mechanism that lets, say, a webcam driver
hand a captured frame directly to a GPU driver for encoding, with zero
copies.

Two roles you'll see in the code:
- **Exporter**: the driver that owns the memory and calls
  `dma_buf_export()` to wrap it. Implements `struct dma_buf_ops` — the
  vtable of callbacks (`map_dma_buf`, `release`, `mmap`, ...) that make
  the generic framework able to drive ITS specific memory.
- **Importer**: anyone holding the resulting fd, via `dma_buf_get()`.

### 1.2 What is "pinning", and why can't the kernel just reclaim these pages?

Under normal memory pressure (you're running low on free RAM), the
kernel's reclaim machinery can evict page-cache pages (they're backed by a
file, so it can just re-read them later) or swap out anonymous pages. This
is the everyday "the kernel gets memory back when it needs to" behavior.

But some memory CANNOT be moved or evicted safely: if a GPU has an active
DMA transfer in flight to a physical address, and the kernel decides to
swap that page out mid-transfer, the GPU either corrupts memory or the
whole system crashes. So drivers that need a guarantee "this memory will
not move out from under me" call `pin_user_pages()` (or the older
`get_user_pages()`) with `FOLL_LONGTERM`, which marks the pages so the core
MM (memory management) subsystem's reclaim code skips them entirely.

This is exactly right for correctness — but it creates a new problem:
**once a page is pinned, the kernel has zero say in "which pinned buffer
should give way to which, if we're desperate?"** Every pinned dma-buf is
equally untouchable from the reclaim subsystem's point of view. Any policy
about "if you MUST give something up, prefer to keep A over B" has to live
somewhere else entirely, because it's not something normal reclaim can
express.

### 1.3 So what's actually missing?

Nothing forces two dma-buf-pinning drivers to cooperate on that "prefer A
over B" policy today. If Driver X wants that behavior, it has to:
1. Track its own list of "its" buffers (fine, it already does).
2. Invent its OWN way to rank them (fine, it's the only one who knows its
   own workload).
3. Invent its OWN private API (an ioctl, a sysfs file, a debugfs knob) so
   something ELSE — a userspace daemon, another kernel module, a shrinker —
   can read/set that ranking. **This is the part with no shared answer.**

That third step is what this patch adds a shared answer for: one small,
generic field on `struct dma_buf` itself, plus accessors, so step 3 never
needs reinventing again.

---

## Part 2 — the concrete, real-world motivating case

This patch was written after finding this exact problem, live, in an
out-of-tree kernel module called `greenboost.ko` (part of the sibling
`greenboost` project on this same machine — a GPU memory-tiering driver
for large local AI model inference).

**What GreenBoost does, briefly:** an AI model (say, a 30-billion-parameter
LLM) is often bigger than the GPU's VRAM. GreenBoost's kernel module
exports pinned system-RAM buffers as dma-bufs so the GPU can still DMA into
them directly when VRAM is full — a "T2" tier of slower-but-present memory,
sitting behind the GPU's own fast VRAM ("T1").

**The exact problem GreenBoost hit:** this same machine is ALSO used for
gaming (Steam/Proton). When a game launches, ITS textures also want to
live in that same system RAM as pinned, GPU-visible buffers. GreenBoost
wants a rule: *"while a game is running, evict the AI inference model's T2
buffers before the game's texture buffers."* Nothing in the kernel could
express that, so GreenBoost built its own: a `gaming_mode` sysfs flag, a
per-buffer "heat" score, a private LRU list, and a private
`GB_IOCTL_GAMING_MODE` ioctl that moves its OWN buffers to the tail of its
OWN list.

That works, but only for GreenBoost's own buffers, understood by
GreenBoost's own private ioctl. Any OTHER driver or userspace tool with the
exact same "prefer to keep A over B" need has to build the exact same
private machinery from scratch. That's the pattern this patch generalizes.

---

## Part 3 — walking through the actual diff, line by line

The patch touches exactly 3 files, 110 lines total, all additions (no
line is removed or changed). Here's every piece, in the order you'd read
them if reviewing.

### 3.1 `include/uapi/linux/dma-buf.h` — the userspace-visible contract

This header is what a `.c` file OUTSIDE the kernel (a userspace program)
`#include`s to talk to dma-buf via ioctls. "uapi" = "userspace API" — this
file's layout is a permanent contract; once released, it can never change
shape, only grow.

```c
struct dma_buf_priority {
	__u32 priority;
	__u32 pad;
};
```

**Why a struct instead of just passing an integer?** Every OTHER dma-buf
ioctl in this file (`dma_buf_sync`, `dma_buf_export_sync_file`, ...) passes
a struct, even a tiny one. This is a deliberate kernel convention: if you
ever need to add a field later (say, a flags word), you can grow the
struct's un-used tail without breaking the ioctl NUMBER — old programs that
only fill in `priority` keep compiling and working, and the kernel simply
reads however many bytes the caller says it's passing.

**Why `pad` and not just a 4-byte struct?** `__u32 priority` alone would
put the struct at 4 bytes. Kernel ioctl structs are conventionally padded
to a multiple of 8 bytes because that avoids alignment surprises when a
struct is embedded inside another, and it's free future-proofing — this
particular reservation costs nothing today and means a v2 field can be
added without changing the ioctl number.

```c
#define DMA_BUF_PRIORITY_MIN		0
#define DMA_BUF_PRIORITY_DEFAULT	128
#define DMA_BUF_PRIORITY_MAX		255
```

**Why 0–255 (a `u8`-range value, even though the field is a `u32`)?** Two
reasons: (a) it's a small, easy-to-reason-about range — nobody needs finer
granularity than 256 buckets for "how eagerly should this go", and (b) it
mirrors a convention the kernel ALREADY uses elsewhere for exactly this
kind of "coarse priority hint" (compare `ionice`'s priority levels, or
`skb->priority`). Keeping it a `u32` in the struct (rather than `u8`)
avoids any future padding/alignment headache if the valid range ever needs
to grow — the WIRE FORMAT is generous even though the initially-valid
VALUES are narrow.

**Why is `128` the default, not `0` or `255`?** Picture the range as a
line from "evict me first" (0) to "never evict me" (255). A brand-new
buffer that NO ONE has expressed an opinion about should start in the
MIDDLE of that line, not at either extreme — otherwise every buffer nobody
has annotated would either always lose (default 0) or always win
(default 255) against a buffer somebody DID annotate, which isn't a neutral
starting point.

```c
#define DMA_BUF_IOCTL_SET_PRIORITY	_IOW(DMA_BUF_BASE, 4, struct dma_buf_priority)
#define DMA_BUF_IOCTL_GET_PRIORITY	_IOR(DMA_BUF_BASE, 5, struct dma_buf_priority)
```

**What is `_IOW`/`_IOR`?** These are macros (from `<linux/ioctl.h>`) that
pack a "direction" (Write = userspace writes data TO the kernel; Read =
kernel writes data BACK to userspace), a "magic" character identifying
which subsystem this ioctl belongs to (`DMA_BUF_BASE` is `'b'`, already
used by every other dma-buf ioctl), a per-subsystem NUMBER (this patch
uses the next two free numbers, 4 and 5 — 0/1/2/3 were already taken by
existing dma-buf ioctls), and the struct type/size, into one opaque
integer the kernel's `ioctl()` syscall dispatch uses to route the call to
the right handler and safely copy the right number of bytes. Getting the
NUMBER wrong (reusing one already in use) would make two DIFFERENT ioctls
collide on the same integer — this is why the numbers are picked by
reading the existing file first, not chosen arbitrarily.

### 3.2 `include/linux/dma-buf.h` — the kernel-internal contract

This header is `#include`d by kernel code (drivers, this file's own `.c`),
never directly by userspace.

```c
	/**
	 * @priority:
	 * ...
	 */
	atomic_t priority;
```

Added as a new field inside `struct dma_buf` (the ONE struct that
represents "a dma-buf" everywhere in the kernel — every exporter's buffer
is one of these under the hood).

**Why `atomic_t` and not a plain `int` protected by the existing
`name_lock` spinlock (which is right next to it, protecting the similar
`name` field)?** Two different needs:
- `name` is a POINTER to a heap-allocated string. Reading it safely means
  "read the pointer AND the bytes it points to without another thread
  freeing them mid-read" — that genuinely needs a lock (or RCU).
- `priority` is a single machine-word-sized SCALAR. Reading or writing a
  correctly-aligned `atomic_t` is already guaranteed atomic (torn reads/
  writes are impossible) by the CPU + the `atomic_t` API, with NO lock
  needed. Since this value is explicitly documented as a coarse HINT
  (nothing downstream depends on reading the exact value some other
  thread is mid-way through writing — there's no "mid-way" possible with
  a single atomic word anyway), adding a whole new lock here would be
  pure overhead for zero correctness benefit. Using the SIMPLEST tool
  that's still fully correct is a real kernel-style value, not laziness.

```c
void dma_buf_set_priority(struct dma_buf *dmabuf, unsigned int priority);
unsigned int dma_buf_get_priority(struct dma_buf *dmabuf);
```

Two new function prototypes any kernel code (this file's own `.c`, or a
completely separate driver like `greenboost.ko`) can call directly, no
ioctl round-trip needed, as long as it already has a `struct dma_buf *`
(which any exporter always does — it's the thing IT created).

### 3.3 `drivers/dma-buf/dma-buf.c` — the actual implementation

**(a) Initialization**, inside `dma_buf_export()` (the function every
exporter calls once, when it FIRST wraps its memory as a dma-buf):

```c
	atomic_set(&dmabuf->priority, DMA_BUF_PRIORITY_DEFAULT);
```

One line, right next to the other one-line field initializations already
there (`spin_lock_init(&dmabuf->name_lock)`, etc.) — every new dma_buf
starts at the neutral default described above.

**(b) The two accessor functions**, right after the existing
`dma_buf_set_name()` (deliberately placed next to its closest sibling, so
a future reader scanning this file top-to-bottom sees related
"set/get a per-buffer attribute" functions grouped together):

```c
void dma_buf_set_priority(struct dma_buf *dmabuf, unsigned int priority)
{
	if (priority > DMA_BUF_PRIORITY_MAX)
		priority = DMA_BUF_PRIORITY_MAX;
	atomic_set(&dmabuf->priority, priority);
}
EXPORT_SYMBOL_NS_GPL(dma_buf_set_priority, "DMA_BUF");
```

**Why clamp instead of returning an error for an out-of-range value?**
This mirrors how the OTHER numeric hint macro in this exact style of API
usually behaves in the kernel (compare how `nice()` clamps rather than
rejecting out-of-range values) — a hint that's "too enthusiastic" just
gets capped to the most extreme meaningful value rather than failing the
whole call. It also means a caller can never accidentally break a whole
code path over a hint being slightly out of range.

**What's `EXPORT_SYMBOL_NS_GPL(fn, "DMA_BUF")`?** Two things bundled
together:
- `EXPORT_SYMBOL_...` makes this function callable from OTHER kernel
  modules (without it, a function is only visible inside the `.c` file
  that defines it, or at most the same built-in subsystem).
- `_GPL` means only GPL-licensed modules may call it (a licensing
  boundary the kernel enforces for symbols it doesn't want proprietary
  drivers using) — `dma_buf_export`, `dma_buf_fd`, and every other public
  dma-buf function already use this same suffix, so this matches
  existing convention exactly, it's not a new decision.
- `_NS_...` ("namespaced") means a calling module must explicitly declare
  `MODULE_IMPORT_NS(DMA_BUF)` to use it — a relatively recent kernel
  mechanism (symbol namespaces) that makes "which modules actually use
  dma-buf's public API" grep-able/auditable, rather than every exported
  symbol being globally visible to every module unconditionally.

```c
unsigned int dma_buf_get_priority(struct dma_buf *dmabuf)
{
	return atomic_read(&dmabuf->priority);
}
EXPORT_SYMBOL_NS_GPL(dma_buf_get_priority, "DMA_BUF");
```

The read side — nothing to validate, just hands back whatever was last
set (or the default, if never set).

**(c) Wiring the two new ioctls** into the existing `dma_buf_ioctl()`
dispatch function (the one function every dma-buf ioctl call goes
through first):

```c
	case DMA_BUF_IOCTL_SET_PRIORITY:
		if (copy_from_user(&prio, (void __user *)arg, sizeof(prio)))
			return -EFAULT;
		if (prio.pad || prio.priority > DMA_BUF_PRIORITY_MAX)
			return -EINVAL;
		dma_buf_set_priority(dmabuf, prio.priority);
		return 0;

	case DMA_BUF_IOCTL_GET_PRIORITY:
		memset(&prio, 0, sizeof(prio));
		prio.priority = dma_buf_get_priority(dmabuf);
		if (copy_to_user((void __user *)arg, &prio, sizeof(prio)))
			return -EFAULT;
		return 0;
```

**Why `copy_from_user`/`copy_to_user` instead of just dereferencing
`arg`?** `arg` is a pointer into USERSPACE's address space, handed to the
kernel by a syscall. The kernel must NEVER directly dereference a raw
userspace pointer (a malicious or buggy program could pass any address,
including kernel memory, or an address that's about to be unmapped from
another thread) — `copy_from_user`/`copy_to_user` are the mandatory,
safe-crossing functions that check the pointer's validity and copy byte-
for-byte, failing cleanly (`-EFAULT`) rather than crashing if the address
is bad. Every OTHER case in this same function (see `DMA_BUF_IOCTL_SYNC`
just above) does the exact same thing — this isn't new pattern, just
applying the existing one to new data.

**Why does `SET_PRIORITY` reject the request outright (`-EINVAL`)
instead of just clamping, unlike the internal `dma_buf_set_priority()`
function which clamps?** Different audiences, different courtesy rules.
`dma_buf_set_priority()` is a KERNEL-internal API — a driver author who
calls it wrote the calling code and can read the clamping behavior in the
function's own comment. The IOCTL is what an arbitrary, possibly-buggy
USERSPACE program calls — for a syscall-facing interface, silently
"fixing" bad input (clamping) hides bugs in the calling program;
rejecting bad input loudly (`-EINVAL`) is the standard, safer contract
for anything crossing the user/kernel boundary, and matches how
`DMA_BUF_IOCTL_SYNC` right above it already rejects an invalid `flags`
value rather than silently masking it.

**(d) Reporting via `fdinfo`**:

```c
	seq_printf(m, "priority:\t%u\n", dma_buf_get_priority(dmabuf));
```

`fdinfo` is what powers `cat /proc/<pid>/fdinfo/<fd>` — a plain-text,
already-standard way any tool (a shell one-liner, a monitoring script) can
introspect a file descriptor's kernel-side state, no new tool needed. This
one line means the new field is observable for free, the same way
`size`/`count`/`exp_name`/`name` already are on the lines just above it.

---

## Part 4 — why this is a genuine improvement, not just "more code"

1. **It replaces N private mechanisms with 1 shared one, at essentially
   zero cost.** Before this patch: every subsystem wanting "prefer A over
   B under pressure" for its pinned dma-bufs invents its own ioctl/sysfs/
   debugfs knob (GreenBoost's `GB_IOCTL_GAMING_MODE` is a real, working
   example of exactly this pattern, done in isolation). After this patch:
   that N-th reinvention is a single already-standard field read/write.
2. **It changes nothing for anyone who doesn't opt in.** This is the
   single most important property for something touching a framework this
   central (`dma-buf` is used by essentially every GPU driver, V4L2
   capture drivers, and more). A kernel with this patch, with zero
   consumers of the new field, behaves BYTE-FOR-BYTE identically to a
   kernel without it for every existing exporter/importer. Verified, not
   assumed: `drivers/dma-buf/` (including its own self-test module) and
   `drivers/gpu/drm/drm_prime.o` (a heavy dma-buf consumer) both compile
   clean against this change.
3. **It deliberately stops at "the hint", not "the policy".** A tempting,
   bigger version of this patch would ALSO teach some shrinker to actually
   act on the priority value. This patch does not do that, on purpose —
   bundling "add the hint" with "and here's my opinion on how it should be
   used" turns an easy, narrow, quickly-reviewable change into a much
   bigger, slower, more contentious design discussion (whose shrinker?
   what algorithm? does it interact with cgroup memory limits?) before the
   simple, useful part could ever land. Keeping them separate lets real
   consumers show up first and inform that harder conversation with actual
   usage data, instead of guessing up front.
4. **Concretely, for local AI inference specifically:** large-model
   inference on consumer hardware is exactly the workload class that keeps
   needing "which pinned GPU-adjacent buffer should give way to which" —
   model weights spilling from VRAM into pinned system RAM (GreenBoost's
   own T2 tier), KV-cache buffers, multiple models sharing one card in
   rotation, a game and an inference workload sharing the same box. Every
   one of those is a "rank my own pinned buffers against someone else's"
   problem this patch gives a real, generic, zero-cost-if-unused answer to
   — instead of each of those systems (present and future, in GreenBoost or
   anywhere else) reinventing the same private ioctl again.

---

## Part 5 — anticipated review questions (so you're not surprised by them)

- *"Why not use cgroups for this?"* — cgroup v2's memory controller
  governs page-cache/anonymous memory reclaim; it has no hook into pinned
  dma-buf memory at all today (that's the actual gap). A maintainer may
  suggest cgroup integration as a BETTER long-term home for policy — that
  would be a valid, healthy design discussion for a v2, not a reason this
  hint itself is wrong.
- *"Why a flat 0–255 int and not an enum (LOW/MEDIUM/HIGH)?"* — an enum is
  easier to read but harder to extend gracefully; a numeric range lets two
  buffers both be "kind of low priority" but still be ordered relative to
  each other, which an enum with only 3-4 buckets can't express. Both are
  legitimate choices; this patch picked the numeric range because it's
  strictly more expressive at the same implementation cost.
- *"What actually reads this value?"* — nothing, yet, on purpose (see Part
  4, point 3). Expect this question; the honest answer is the patch's own
  argument for landing the hint before the policy.
- *"Did you test this against real hardware?"* — compile-tested, yes (see
  the patch's own commit message and this project's own README.md for the
  exact `make` invocations and their clean results). A live ioctl round-
  trip test (via `udmabuf`, no GPU needed) has NOT been run yet — see
  `README.md`'s "Suggested next test" section; doing that before or
  shortly after sending is the honest, correct thing to do, and reviewers
  are right to ask for it.
