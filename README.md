# 🐧Hyphaed Kernel

A Linux kernel built while working on local AI inference, with patches created to extend GreenBoost capabilities and/or fix issues discovered during day-to-day work with AI pipelines.
Includes patches (sent to kernel lists), yet not included elsewhere as of Aug 21, 00:46.

This is the kernel side of [GreenBoost](https://gitlab.com/IsolatedOctopi/greenboost).
GreenBoost extends a GPU's VRAM with system RAM and NVMe so a model bigger than
your card still runs with its compute on the GPU. It does that from userspace
and one out-of-tree kernel module. Pushing that module hard, on real workloads,
kept running into places where the mainline kernel had no way to express what
the workload needed. Those places are what this repository collects: some as
config choices, some as patches carried locally, and four as original work
aimed at upstream.

The build tool is called `hyphaed`.

## What this contributes, and what it buys local inference

Most of what is here is other people's work, curated and pinned: sixteen of the
twenty patches in the default series come from CachyOS, XanMod and TKG, with
attribution intact and every source recorded by sha256 in
`patches/VENDOR.lock`.

Four are ours, and each one closes a gap that costs a local inference box
something measurable.

### A generic reclaim-priority hint for dma-buf (`0019`)

Adds an advisory `priority` field to `struct dma_buf`: `0..255`, default `128`,
lower reclaimed earlier. Two exported helpers, two ioctls so userspace holding
an fd can set it, and the current value reported through fdinfo. dma-buf core
implements no reclaim of its own; it stores the number for exporters that
already have a reclaim path.

This is the first generic way for any dma-buf exporter to rank its own pinned
buffers.

**What it buys local inference.** Memory pinned through
`pin_user_pages()`/`FOLL_LONGTERM` sits outside normal reclaim by design, so
when RAM gets tight nothing could distinguish a KV cache that is re-read on
every single decode step from a cold expert's weights that may not be touched
again this generation. They are not equally valuable and only the exporter
knows it. With the hint, the buffer whose loss costs you tokens per second is
the one that survives pressure.

The alternative is what everyone does today: invent a private ioctl. GreenBoost
carries a `gaming_mode` module parameter, a per-buffer heat score pushed
continuously from its CUDA shim through a private `GB_IOCTL_SET_HEAT`, and its
own LRU it reorders under pressure. All of that expresses one missing hint, in
a form nothing else can read. `greenboost.ko` consumes the generic version
today in its T2 eviction sweep.

**Status: ready to send as an RFC, and the cover letter leads with its own
blocker.** The only consumer is out-of-tree. Adding UAPI with no in-tree user
is normally declined, and that is the correct call, since UAPI is permanent and
nothing in-tree would constrain the semantics or prove the design survives a
second consumer. The realistic outcomes are that someone with an in-tree use
case redesigns it, or it is declined pending one. Both are useful.

### A compressed-content descriptor for dma-buf (`0020`)

Lets an exporter that has compressed a buffer's backing memory in place publish
how an importer should inflate it, so "compressed" becomes an alternative to
"evicted" rather than a private arrangement.

**What it buys local inference.** A cold mixture-of-experts expert can stay
resident and compressed instead of being dropped and re-fetched across the
host-to-device link the next time routing selects it. On the reference box that
link measures ~11-12 GB/s, so not paying the re-fetch is worth more than the
RAM the compression saves. It composes with `0019` without depending on it:
that patch says which buffers to keep, this one lets an exporter that kept one
say how it kept it.

**Status: finished, compile-tested, applied locally, and deliberately not
submitted.** There is no in-kernel reader and no in-kernel producer of the
state it describes. Sending it now would spend reviewer attention on an API
nothing can exercise.

### UBSAN made opt-in for external modules (`0017`)

Any `obj-m` object inherited the kernel's UBSAN `KBUILD_CFLAGS`, because
`is-kernel-object` evaluates to `y` for external modules too. This makes UBSAN
opt-in for external builds; in-tree builds are untouched, and an external
module that wants sanitizers sets `UBSAN_SANITIZE := y` itself.

**What it buys local inference.** Out-of-tree GPU and memory-tiering modules
stop inheriting instrumentation they never asked for. The failure this produced
is the hardest kind to attribute: the module builds, loads cleanly, reports no
error, and then misbehaves at runtime. Observed against VMware's
`vmnet`/`vmmon`, with packet forwarding failing and VMs destabilising;
`greenboost.ko` is an external module in the same blast radius.

**Status: a straightforward bug fix, not a hint or a new interface.**

### Documentation/ABI for four PCI link attributes (`0021`)

`max_link_speed`, `max_link_width`, `current_link_speed` and
`current_link_width` have been exported under `/sys/bus/pci/devices/.../` since
2018 and appear nowhere in `Documentation/ABI`. This documents them, and
records in particular that `current_link_speed_show()` performs a fresh
`PCI_EXP_LNKSTA` read on every open.

**What it buys local inference.** It kills a whole class of false diagnosis.
Modern GPUs retrain their link constantly as part of idle power management, so
a single read can legitimately return any speed the link supports. Comparing
`current_link_speed` against `max_link_speed` therefore reads exactly like a
test for a degraded link and is not one. Measured on an RTX 5070 in a PCIe 4.0
x16 slot, same boot, no configuration change between reads: **5.0 GT/s idle,
16.0 GT/s under load.**

GreenBoost built that false alarm and shipped it: its `pcie_degraded` check
fired thirteen times claiming a gen2 link on a connection that measures Gen4
x16 whenever it is actually being used, because the guard sampled at idle.
Anyone tuning a PCIe-bound inference box can now tell a real problem from power
management doing its job.

**Status: ready to send.** No behaviour changes.

### The kernel build itself

Beyond the originals, the curated series covers what a box holding a large
model resident and reading it hard actually needs, where a stock kernel assumes
a desktop: BORE scheduling, BBR3, vmscan and VFS-cache behaviour under
sustained memory pressure, `max_map_count`, timer frequency, block-layer
latency and mq-deadline tuning, THP defrag defaults. Config fragments in
`configs/fragments/` are selected against what the machine reports rather than
a fixed profile.

### Upstream status, in one place

None of these are merged. `0021` and `0017` are ready to send, `0019` is ready
as an RFC with its blocker stated up front, and `0020` is deliberately held.
`upstream-candidates/SUBMISSION.md` records that judgement per patch so it does
not have to be re-argued from memory.

## Authorship

Every patch we wrote is authored `Ferran Duarri <ferran.duarri@pm.me>` and
carries a matching `Signed-off-by:`, whether or not it is ever sent upstream.
The sign-off is not style: it is the attestation required by the kernel's
Developer's Certificate of Origin, and a patch without one cannot be applied.

Third-party patches keep their original authorship. Rewriting an upstream
author's name would be misattribution, which is the opposite of what the rule
is for.

`tests/test_patch_authorship.py` enforces both halves, including that the
sign-off sits above the `---` separator, since `git am` silently drops trailers
below it.

## The investigation that found nothing

`upstream-candidates/pcie-bwctrl-stale-target-speed/` is not a patch. It is a
theory that did not survive its own diagnostic script, kept in full.

The theory was a stale cached target speed that one sysfs write would correct.
Running the script: before the "fix", the live read was 5.0 GT/s. After writing
`cur_state=0` to request maximum speed, it read **2.5 GT/s**, worse. Seconds
later, with nothing further done, it was back to 16.0 GT/s on its own. A
ten-sample poll with no writes at all showed the link cycling through
2.5 / 5.0 / 16.0 GT/s inside a ten-second idle window, while `nvidia-smi`
showed the GPU's own power state cycling P3/P5/P8 alongside it.

The link was doing exactly what it should. There is no bug, so there is no
patch. The governing rule in the plan this belongs to is "only patch what you
can prove helps", and this never met it.

## Layout

```
hyphaed/            the build tool: a phased pipeline
  phases/           detect, source, patch, configure, build,
                    package, install, postinstall, security
  greenboost.py     reads GreenBoost's hardware profile and
                    cross-checks it against ours
patches/            the ordered series, `series` is the manifest
  custom/           original work, including the four above
  VENDOR*.lock      every source pinned by sha256
  fetch.py          populate from the pinned sources
configs/
  fragments/        Kconfig fragments, selected by detected hardware
  presets/          named combinations
upstream-candidates/ submission-ready trees, with EXPLAINER.md each
  SUBMISSION.md     per-patch: ready to send, or why not
docs/               research notes and patch planning
tests/              228 tests
```

## Building

```bash
python -m hyphaed detect        # what this machine actually is
python -m hyphaed configure     # fragments -> .config
python -m hyphaed build
python -m hyphaed install
```

`install_wizard.sh` walks the whole thing interactively if you would rather be
asked than remember flags.

The upstream trees are not vendored here. `linux/`, the CachyOS patch
collection and the other reference clones are fetched, not committed, which is
why a checkout is a few megabytes rather than a couple of hundred gigabytes.
`patches/VENDOR.lock` pins every source by sha256, and `$KI_ROOT` in those
lock files expands to your checkout, so the same lock resolves on any machine.

## 🖥️ Scope, for context ; currently used hardware

This is being built, tested, and used primarily on two machines,
with the desktop being used by far the most:

desktop; RTX 5070 12Gb VRAM, PCIe 4.0 x16, 64GB DDR4, i9 14900KF

laptop; RTX mobile 5070 8Gb VRAM, PCIe 5.0 x16, 32GB DDR5, Ryzen AI9 365

** apart from the hardware of contributors and/or users that open issues (sometimes sharing logs)


## License

The kernel patches are GPL-2.0, as the kernel is. Third-party patches keep
their original authorship and are recorded in `patches/VENDOR.lock`; the
originals under `patches/custom/` are mine. `hyphaed` itself is GPL-2.0.
