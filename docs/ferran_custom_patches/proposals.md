# Proposals — honest per-patch upstream readiness

Grades for everything in the catalog that is, or was considered as, ours to
send. See `UPSTREAMING.md` for the send procedure and `originals/*.md` for
per-patch detail.

## Ready to send

### `0019` — dma-buf: add a generic reclaim-priority hint

**What it is, in one sentence:** a small, opt-in "priority" label (a number
0–255, default 128) that any program can attach to memory it's sharing with
a GPU or other hardware, so that if the system ever needs to free something
up under memory pressure, it has a way to know which memory matters more —
see `upstream-candidates/dma-buf-priority-hint/README.md`'s "In plain
English" section for a no-jargon walkthrough of why this gap exists and what
the patch does about it.

**Not GreenBoost-specific.** `dma-buf` is the generic Linux mechanism used
whenever hardware (GPUs, video capture cards, camera pipelines, VR/AR
headsets) hands memory to each other directly — GreenBoost is this patch's
concrete, working motivating case, not its only intended beneficiary. Video
editors juggling live capture + scrubbing + background exports, desktop
compositors deciding which window's buffer to keep, multi-GPU render
pipelines, and any other AI/ML memory-tiering system all have the identical
"prefer to keep A over B under pressure" need this closes generically instead
of everyone reinventing their own private ioctl. See the README's "Who this
could help — not just GreenBoost" section for the full list with reasoning.

**Send it.** checkpatch clean (0 errors, 0 warnings), compile-tested against
a full built tree including a heavy real consumer
(`drivers/gpu/drm/drm_prime.o`), and it now has a genuine, working consumer:
GreenBoost's `gb_apply_priority_hint()` calls `dma_buf_set_priority(dmabuf,
200)` at all three `dma_buf_export()` sites for KV-cache/T1-priority buffers.
The one remaining gap before sending is the runtime `udmabuf` round-trip test
already sketched in `upstream-candidates/dma-buf-priority-hint/README.md`'s
"Suggested next test" section — worth doing first, since reviewers will
likely ask "did you actually run this?" Full detail: `originals/0019.md`.

## Plausible, needs one more check

### `0020` — dma-buf: add a generic compressed-content descriptor

**What it is, in one sentence:** the sibling of `0019` — a small, opt-in
`{codec, block_size, uncompressed_size}` descriptor an exporter can attach to
a dma-buf it has compressed in place, so an importer knows how to inflate it
instead of assuming a fresh fetch is needed — see
`upstream-candidates/dma-buf-compressed-descriptor/README.md`'s "In plain
English" section.

**Not GreenBoost-specific**, same reach as `0019`: V4L2 compressed capture,
DRM/KMS framebuffer compression, and any other memory-tiering system share
this identical gap. GreenBoost is this patch's motivating case, not its only
intended beneficiary — see the README's "Who this could help" section.

**One notch below `0019`'s grade, and here's the honest reason why**: this
patch is checkpatch-clean and compile-tested against the real stacked
`0001`-`0020` series (not just in isolation — an earlier draft authored
against the clean base looked non-conflicting by inspection but produced 3
real conflicts once actually stacked with `0019` via `git am --3way`; it was
re-authored on top of a tree with `0019` already applied and re-verified end
to end, see `originals/0020.md`'s Verification section for the full story).
What it doesn't have yet, unlike `0019`, is a landed real consumer:
`gb_moe.py`'s cold-expert demotion currently uses a lossy `int4`
quantization, and swapping that for a lossless codec that publishes through
this descriptor is designed but not yet wired into
`~/Dev/greenboost_all/greenboost` as of this writing. Land that first — the
same bar `0019` cleared before being graded "ready to send." Full detail:
`originals/0020.md`.

## Plausible, needs one more check

### `0017` — kbuild: ubsan: skip UBSAN for external modules by default

Small, self-contained, fixes a real observed problem (VMware `vmnet`/`vmmon`
misbehaving under UBSAN they were never tested against), gated cleanly on
`KBUILD_EXTMOD` so in-tree builds are untouched. Before sending: search
`kbuild@vger.kernel.org` list archives for whether this exact gap has already
been raised or has a known reason for the current behavior — a two-line
`Makefile.lib` change to a widely-used sanitizer gate is exactly the kind of
thing that's either an easy accept or has a subtlety nobody local to this box
would know about. Full detail: `originals/0017.md`.

## Keep local, do not send as-is

### `0015` — nvme: lower default APST max latency for desktop/workstation use
### `0016` — mm: default THP defrag to defer+madvise instead of madvise

Both change a compiled-in *default* that would ship to every kernel using it,
justified by one workstation's specific workload (AI weight staging /
GreenBoost T2 tiering). Upstream reliably wants broad cross-workload evidence
before moving a shared default — a single machine's anecdote isn't that, and
neither patch's own commit message claims otherwise (both are honest about
being local tuning, not a fix). If either is worth pursuing further: reshape
as a Kconfig option or a documented workstation/AI-staging preset users can
opt into, rather than a default flip everyone inherits silently. Both values
are already runtime-overridable via sysfs with zero kernel patch needed — see
each `originals/` page for the exact command — which may be the better answer
regardless of upstream interest. Full detail: `originals/0015.md`,
`originals/0016.md`.

## Correctly abandoned — kept as the worked example

### `pcie-bwctrl-stale-target-speed` (investigation, no patch)

Started from a real, measured discrepancy: `current_link_speed` (live
`LnkSta` read) showed 16.0 GT/s while `cooling_device0`'s cached
`cur_bus_speed` field showed the link at 5.0 GT/s, drifting to 2.5 GT/s over
a few minutes — looked like a classic stale-cache bug in
`drivers/pci/pcie/bwctrl.c`. Running `diagnose_and_fix.sh` (the actual
diagnostic, not a guess) disproved it: a direct 10-sample poll showed the
link genuinely cycling through 2.5/5.0/16.0 GT/s within a 10-second idle
window, correlated with the GPU's own P-state cycling — normal NVIDIA idle
power management, not a kernel bug. The investigation's own conclusion: "do
not write the sketched kernel patch."

This is kept in the catalog deliberately, not as a failure but as the model
to follow: **only patch what you can prove helps**, and a plausible-looking
theory that doesn't survive its own diagnostic script gets written up and
shelved, not forced into a patch anyway. The one real, actionable finding
that survived — GreenBoost can hold `cooling_device0/cur_state=0` for the
duration of a `gb-synapse serve()` session to avoid retrain latency at burst
start — is a GreenBoost-side lever, not a kernel fix, and is tracked there
instead.

### This session (2026-08-06) — no new kernel patch

Investigated whether a kernel patch could enable NVIDIA vGPU (mdev/VFIO/
SR-IOV) on this RTX 5070 for `greenboost_vgpu`'s macOS paravirtualization
work. Verdict: no, and recorded explicitly here so the question isn't
re-opened without new evidence. Every blocker sits outside the kernel:
`nvidia.ko` (the installed open driver) imports zero sriov/mdev/vfio kernel
symbols and has no `sriov_configure`; the `open-gpu-kernel-modules` source
tree has no `nvidia-vgpu-vfio` component at all; the card's SR-IOV capability
is vestigial (`sriov_totalvfs=1`, VF device ID identical to the PF's own —
not what a working implementation looks like); GeForce is excluded from
every NVIDIA vGPU support matrix (Blackwell entries are RTX PRO Server
Edition, different silicon from this GB205 part); and the community
`vgpu_unlock` project stalled at "Ampere support is a work in progress" once
VF creation moved into signed GSP firmware. A kernel patch cannot make a
closed driver register an mdev parent, and cannot make silicon expose PCIe
VFs it doesn't implement. Separately, even a working vGPU would be moot for
the stated goal: NVIDIA's macOS driver support ended at Pascal/High Sierra,
so a macOS guest cannot drive this hardware by any route regardless.

## Different project, different process

### QEMU patches — `~/Dev/greenboost_all/greenboost_vgpu/qemu/*.patch`

`qemu-devel@nongnu.org`, not LKML — a different mailing list, review culture,
and submission process entirely (`git-publish` or plain `git send-email` to
that list, subject-prefixed `[PATCH]`, CC'd to `hw/display` maintainers per
QEMU's own `MAINTAINERS` file). Not evaluated against that process here; two
concrete obstacles worth resolving before anyone considers it:

- `0001-hw-display-add-gb-vgpu.patch` registers PCI vendor:device `1b36:0001`
  — QEMU's own vendor ID range, but the device ID is documented as a
  **placeholder** in `protocol/gb_vgpu_proto.md:115`, not a real allocation.
- `0002-hw-display-add-gb-pvg.patch` deliberately claims Apple's own
  vendor:device ID (`106B:EEEE`) so macOS's in-box kext binds to it — correct
  for this device's actual purpose, but means it cannot coexist as a generic
  upstream QEMU device the way `0001` might; it also overlaps conceptually
  with QEMU's own existing `apple-gfx` device (referenced in
  `gb_pvg_proto.md:1009`), which would need reconciling before submission
  makes sense.
