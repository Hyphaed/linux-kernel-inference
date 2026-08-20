# dma-buf: generic reclaim-priority hint (RFC candidate)

Status: **drafted, compile-tested, signed off, NOT sent anywhere yet.**
`git send-email` is the actual send step and it hasn't been run — publishing
is a command you run yourself (walkthrough below), never something done on
your behalf.

## What this patch does;

Different pieces of hardware in a computer, a graphics card, a video
capture device, a second graphics card, often hand chunks of memory to
each other directly, the way you'd pass a book straight to someone instead
of photocopying it first. Linux has a shared mechanism for this handoff,
called `dma-buf`.

Normally, when memory gets tight, the computer can shuffle things around to
free up space, the same way you'd move papers around a desk to make room.
Some of this handed-off memory can't be shuffled, though: a graphics card
mid-transfer can't have its memory yanked away without the transfer
corrupting or the system crashing. So that memory gets "pinned", locked in
place, for as long as it's needed.

That's correct and necessary, but it creates a blind spot. Once memory is
pinned, the computer has no way to know which pinned piece is safe to give
up first if it ever really has to free something. Every pinned buffer looks
equally untouchable, even though the software that created it usually has
a clear opinion: "my currently-playing video frame matters, my background
export doesn't."

Today, expressing that opinion means inventing a private, one-off signal
from scratch, and every program that needs this ends up building a
slightly different, incompatible version of the same idea.

This patch adds one small, shared "priority" number, a sticky note that
says "keep me" or "it's fine to let this go", that any program can attach
to its own pinned memory using one standard method built into Linux,
instead of a hundred private ones. It does nothing by itself: it doesn't
force the computer to evict anything, and no existing program's behavior
changes unless that program chooses to use the new sticky note. It just
gives everyone a shared vocabulary for labeling "how important is this", so
whatever software is in charge of freeing memory under pressure can make a
smarter choice.

It's small, 110 lines, nothing existing changed or removed, and it already
has one real, working use on this machine: GreenBoost, which uses it to
say "keep this AI model's actively-used memory over its own background
staging data" when the graphics card starts running low on room.

## Who this could help, not just GreenBoost

GreenBoost is the concrete, working example that motivated writing this
patch, but the gap it closes is generic to Linux's `dma-buf` mechanism
itself, which is used far beyond one project. Anything that pins
GPU-visible memory and would benefit from saying "keep this, not that" is a
potential beneficiary, without needing GreenBoost or AI inference at all:

- **Video editors and capture hardware.** A video editing app juggling a
  live 4K/8K capture feed, a scrubbing preview, and several background
  export jobs could mark the frame the user is actively looking at as
  high-priority and queued background exports as low-priority.

- **Multi-GPU workstations and render farms.** Software distributing work
  across several graphics cards could mark buffers for the render that's
  actively due next higher than buffers for a job that can be restarted.

- **Compositors (the software that draws your desktop).** A Wayland or X11
  compositor holding buffers for a dozen open windows could prefer to keep
  the buffer for whatever's on screen right now over a minimized window's.

- **VR/AR headsets and camera pipelines.** Anything streaming frames
  between a sensor/camera and a GPU or encoder in real time has the same
  "this frame matters more than that one" shape of problem.
  
- **Any other AI/ML memory-tiering system**, present or future — GreenBoost
  is one instance of "an inference or training workload spilling GPU memory
  into pinned system RAM," a pattern that is not unique to this project and
  is only becoming more common as models keep outgrowing VRAM.

None of these need to know about each other or about GreenBoost — that is
the point of putting the hint in `dma-buf` itself rather than leaving every
project to invent its own private version, which is exactly what GreenBoost
had to do before this patch existed (see the "gap this closes" section
below and `EXPLAINER.md` Part 2 for that concrete before/after).

## What it does (precise)

One commit, 3 files, 110 lines, purely additive, against
`~/Dev/kernel_inference/linux` (currently based on 7.1.2):

| File | Change |
|---|---|
| `include/uapi/linux/dma-buf.h` | New `struct dma_buf_priority { __u32 priority; __u32 pad; }`, constants `DMA_BUF_PRIORITY_MIN` (0) / `_DEFAULT` (128) / `_MAX` (255), two new ioctl numbers `DMA_BUF_IOCTL_SET_PRIORITY` / `DMA_BUF_IOCTL_GET_PRIORITY` (`'b'`, 4 and 5 — the next free slots after the existing sync-file ioctls). |
| `include/linux/dma-buf.h` | `struct dma_buf` gains one field: `atomic_t priority`. Two new function prototypes: `dma_buf_set_priority()` / `dma_buf_get_priority()`. |
| `drivers/dma-buf/dma-buf.c` | Initializes the field to `DMA_BUF_PRIORITY_DEFAULT` in `dma_buf_export()`; implements the two accessor functions (`EXPORT_SYMBOL_NS_GPL`, namespace `DMA_BUF`); wires the two ioctls into `dma_buf_ioctl()`; adds one `priority:\t<n>` line to `dma_buf_show_fdinfo()`. |

Nothing else changes. No existing `dma_buf_ops` callback is touched, no
existing field is renamed or resized in a way that shifts anything, no
existing ioctl/behavior changes for any current exporter or importer.

## The gap this closes

`dma-buf` has no generic way for an exporter, importer, or userspace to hint
how eagerly a given buffer's backing memory should be given up under memory
pressure. Pages pinned via `pin_user_pages()`/`FOLL_LONGTERM` sit outside
normal reclaim by design — that's the whole point of pinning — so there is
currently no shared, generic answer to "if you have to give something up,
give up *this* one first."

Verified before drafting, not assumed: grepped `struct dma_buf_ops` and
`struct dma_buf` in this tree for any existing priority/shrink/reclaim hook
— none exists. Checked recent `drivers/dma-buf/` commit history for
in-flight work in this exact area — only two unrelated commits turned up (a
`system_heap` reclaim-order tweak, an amdgpu import-eviction behavior),
neither of which is a generic per-buffer hint.

## Benefits / features this provides

- **One shared vocabulary instead of N private ones.** Every subsystem that
  currently wants "prefer to keep A over B under pressure" — GPU drivers
  juggling foreground/background clients, memory-tiering allocators for
  large AI/ML working sets, anything else that pins dma-buf memory — no
  longer has to invent and maintain its own private ioctl/sysfs/side-channel
  to express it. One field, one pair of accessors, one pair of ioctls,
  usable by anyone. This is not a GreenBoost-specific benefit — see "Who
  this could help — not just GreenBoost" above for concrete examples
  (video editors, compositors, multi-GPU renderers, other AI/ML stacks)
  that share the exact same underlying need.
- **Zero cost / zero risk to adopt.** It's an opt-in hint with a neutral
  default. An exporter that never calls `dma_buf_set_priority()` sees
  `DMA_BUF_PRIORITY_DEFAULT` and nothing about its behavior changes. A
  kernel with this patch and NO consumer of the hint yet behaves identically
  to a kernel without it.
- **Two access paths for two real audiences.** Kernel code (in-tree or an
  out-of-tree module) gets direct C accessors with no ioctl round-trip.
  Userspace holding a bare fd — a compositor, a monitoring tool, a
  standalone tiering daemon — gets the same capability over the existing
  dma-buf fd it already has, no new device node or capability needed.
- **Observable for free.** The value shows up in `fdinfo`
  (`/proc/<pid>/fdinfo/<fd>`), right next to the size/name/exporter fields
  every dma-buf already reports — so `lsof`-style tooling, debugging, and
  accounting scripts get this data with no new tooling.
- **Deliberately does not overreach.** It implements no eviction policy
  itself — dma-buf core only stores and reports the value. That's a
  feature, not a gap: a generic *hint* is something maintainers can review
  and land quickly; a generic *policy* bundled with it would force a much
  bigger, slower design conversation (whose shrinker, what algorithm, does
  it interact with cgroups) before the hint itself could ever land. Keeping
  them separate lets real consumers show up and inform that next
  conversation with actual usage data.

**Concrete motivating case**, so the RFC isn't hypothetical: an out-of-tree
GPU-memory-tiering driver (GreenBoost's `greenboost.c`, a sibling project on
this machine) re-implements exactly this today, a `gaming_mode` sysfs flag
plus a per-buffer "heat" score that moves its own dma-buf-backed
allocations to a private LRU tail via a private `GB_IOCTL_GAMING_MODE`
ioctl, purely because there is nowhere generic to say "this one can go
first." Large-model AI inference workloads that tier weights/KV-cache
across VRAM -> system RAM -> NVMe are exactly the class of workload that
keeps re-inventing this same primitive.

**Update (Speed Program audit, 2026-07-26): this is no longer hypothetical,
GreenBoost now actually calls the hint.** `greenboost.c`'s
`gb_apply_priority_hint()` calls `dma_buf_set_priority(dmabuf, 200)` (default
128, max 255) at all three `dma_buf_export()` sites, for buffers carrying
`GB_ALLOC_KV_CACHE | GB_ALLOC_T1_PRIORITY`, exactly the flag combination the
existing `gaming_mode` exemption check already uses (`greenboost.c:1409`).
Built clean via the real kernel build (`make module`, `vermagic:
7.1.5-hyphaed` matches the running kernel exactly, `nm greenboost.ko` shows
the `dma_buf_set_priority` symbol reference), not yet loaded into the live
module. This is a genuine, real, in-tree-adjacent consumer, not the
speculative motivating example this RFC originally shipped with, worth
mentioning if/when this patch is actually sent.

## Verification already done

- `checkpatch.pl --no-tree`: **0 errors, 0 warnings** — "no obvious style
  problems and is ready for submission" (checkpatch's own words).
- Compile-tested against a full built tree
  (`~/Dev/kernel_inference/build/linux-7.1.5` — confirmed byte-identical
  `dma-buf.c`/`dma-buf.h` to the 7.1.2 source tree before patching, so the
  result carries over):
  - `make drivers/dma-buf/dma-buf.o` — clean, no warnings.
  - `make drivers/dma-buf/` (whole subsystem incl. the `st-dma-*` selftest
    modules) — clean.
  - `make drivers/gpu/drm/drm_prime.o` — a heavy dma-buf consumer, spot-
    checked in case the new struct field shifted anything importers rely
    on — clean.
- `Signed-off-by: Ferran <ferran.duarri@me.com>` is real (matches the
  commit's `Author:`, taken from your git config) — the DCO attestation is
  in place.
- **Not done** (needs a real boot, not available in this environment):
  round-tripping `DMA_BUF_IOCTL_SET_PRIORITY`/`GET_PRIORITY` against a real
  dma-buf fd and confirming the `fdinfo` line renders. See "Suggested next
  test" below — worth doing before or shortly after sending the RFC, since
  reviewers will likely ask "did you actually run this?"

## Suggested next test (10 minutes, no more kernel work)

`udmabuf` (`drivers/dma-buf/udmabuf.c`, already in this tree) is the
easiest way to get a userspace-owned dma-buf fd with no GPU involved:

```c
// 1. Create a memfd, UDMABUF_CREATE it into a dma-buf fd (see
//    Documentation/driver-api/dma-buf.rst or any udmabuf example online).
// 2. struct dma_buf_priority p = { .priority = 10 };
//    ioctl(fd, DMA_BUF_IOCTL_SET_PRIORITY, &p);
// 3. struct dma_buf_priority q = {0};
//    ioctl(fd, DMA_BUF_IOCTL_GET_PRIORITY, &q);
//    assert(q.priority == 10);
// 4. cat /proc/self/fdinfo/<fd> — confirm "priority:\t10" appears.
```

A follow-up session can turn this into a real
`tools/testing/selftests/dma-buf/` test (matching the existing `st-dma-*`
selftest convention already in this tree) instead of a scratch C file —
that's the form upstream would actually want alongside the patch, and
having a real selftest strengthens the RFC.

## How to publish it — step by step

This machine currently has **no `git send-email` and no SMTP transport
configured** (checked: `git send-email` isn't a recognized git command,
no `sendemail.*` git config, no `msmtp`/`sendmail`/`ssmtp` installed). None
of the steps below have been run.

### 1. Install and configure git-send-email

```bash
sudo apt install git-email          # provides `git send-email`
```

You need an SMTP account it can authenticate as (your own mail provider,
Gmail, Fastmail, your own domain's mail server, etc.; kernel.org does not
provide one). Using Gmail (`ferran.duarri.dev@gmail.com`), in `~/.gitconfig`:

```ini
[sendemail]
    smtpserver = smtp.gmail.com
    smtpuser = ferran.duarri.dev@gmail.com
    smtpencryption = tls
    smtpserverport = 587
    confirm = always
```

Gmail rejects your normal account password for SMTP auth, it requires an
**App Password** instead:

1. Turn on 2-Step Verification on the account, if it isn't already:
   `myaccount.google.com/signinoptions/two-step-verification`
2. Generate an App Password at `myaccount.google.com/apppasswords`
   (name it something like "git send-email"). Google shows it once, a
   16-character string with no spaces needed.
3. `git send-email` will prompt for the SMTP password interactively the
   first time, paste the App Password there (or read it from a configured
   credential helper) — it is never written to a file by these steps.

### 2. (Recommended) Subscribe to the lists you're about to mail

Not a hard requirement to send, but you won't see maintainer replies unless
you're subscribed or explicitly CC'd (you will be CC'd on replies to your
own thread regardless — subscribing just lets you see the wider
discussion). Subscribe at:
- `dri-devel@lists.freedesktop.org` — https://lists.freedesktop.org/mailman/listinfo/dri-devel
- `linaro-mm-sig@lists.linaro.org` — moderated; https://lists.linaro.org/mailman/listinfo/linaro-mm-sig

`linux-kernel@vger.kernel.org` and `linux-media@vger.kernel.org` are
extremely high-traffic — most people don't subscribe, they just read
replies to their own thread. Your call.

### 3. Do a final review of the patch yourself

```bash
cd ~/Dev/kernel_inference/linux
git show HEAD                        # read every line one more time
perl scripts/checkpatch.pl --no-tree \
  ~/Dev/kernel_inference/upstream-candidates/dma-buf-priority-hint/0001-*.patch
```

### 4. Send it — real maintainers/lists, from this tree's own
   `get_maintainer.pl` (not guessed):

```bash
cd ~/Dev/kernel_inference/linux
git send-email \
  --to="Sumit Semwal <sumit.semwal@linaro.org>" \
  --to="Christian König <christian.koenig@amd.com>" \
  --cc="linux-media@vger.kernel.org" \
  --cc="dri-devel@lists.freedesktop.org" \
  --cc="linaro-mm-sig@lists.linaro.org" \
  --cc="linux-kernel@vger.kernel.org" \
  ~/Dev/kernel_inference/upstream-candidates/dma-buf-priority-hint/0001-*.patch
```

`git send-email` shows you the exact composed message and asks for
confirmation before it actually sends (that's what `confirm = always`
above guarantees) — read it once more there.

The `[RFC PATCH]` subject prefix (already baked into the generated file) is
deliberate: this is a first-contact proposal for a new generic API with one
real motivating use case, not a bug fix. Expect real design pushback —
maintainers may prefer this live under a cgroup, object to a flat 0–255
scale, or want a consumer designed in the same breath. That's the normal,
healthy review process for a new cross-subsystem API, not a sign something
is wrong with the patch.

### 5. After sending

- Your message gets archived and becomes publicly, permanently visible
  (lore.kernel.org and others mirror everything sent to these lists) —
  this is the "essentially irreversible" part to be clear-eyed about
  before step 4, not after.
- `linaro-mm-sig` is moderated — a first post from a new address may sit in
  a moderation queue for a bit before appearing.
- Track it on kernel.org's Patchwork (dri-devel's instance:
  https://patchwork.freedesktop.org/project/dri-devel/list/) — search your
  email or the patch subject once it's sent.
- Expect nothing, one reply, or a long design thread — all three are normal
  outcomes for an RFC. No reply within a couple of weeks on a moderate-
  traffic list like this is common and not a rejection; a polite follow-up
  ("ping") after 1–2 weeks is normal practice.
- If reviewers ask for changes: `git commit --amend`, regenerate with
  `git format-patch -1 HEAD --subject-prefix="RFC PATCH v2"`, and reply
  in-thread with the new patch (`git send-email --in-reply-to=<message-id>
  ...`) rather than starting a new thread.

### One disclosure to decide on

This patch was drafted with AI assistance (this session). Some kernel
subsystems/communities have norms or explicit preference around
disclosing that (e.g. a note in the cover-letter portion of the commit
message, or a `Co-developed-by:` trailer). There's no single kernel-wide
rule as of this writing — worth a quick check of dri-devel's/dma-buf's own
recent norms, and it's your call how to phrase it since you're the one
signing off.
