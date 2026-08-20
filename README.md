# linux-kernel-inference

A Linux kernel built for running large models locally, and the patches that
came out of doing it.

This is the kernel side of [GreenBoost](https://gitlab.com/IsolatedOctopi/greenboost).
GreenBoost extends a GPU's VRAM with system RAM and NVMe so a model bigger than
your card still runs with its compute on the GPU. It does that from userspace
and one out-of-tree kernel module. Pushing that module hard, on real workloads,
kept running into places where the mainline kernel had no way to express what
the workload needed. Those places are what this repository collects: some as
config choices, some as patches carried locally, and four as original work
aimed at upstream.

The build tool is called `hyphaed`.

## The honest summary first

Most of what is here is other people's work, curated and pinned. Sixteen of the
twenty patches in the default series come from CachyOS, XanMod and TKG, with
attribution intact and every source recorded by sha256 in `patches/VENDOR.lock`.

Four patches are original. Of those:

- one is a straightforward bug fix,
- one is an RFC worth sending, with a blocker its own cover letter has to admit,
- one is finished, works, and **should not be sent yet**,
- one is documentation for sysfs attributes that have been undocumented since
  2018.

There is also a fifth investigation that concluded **there is no bug**, kept in
the repository because a disproved theory is worth writing down.

## Why a custom kernel at all

Not for a benchmark score. Three concrete reasons, all from running GreenBoost:

**External modules were being built with sanitizer flags they never asked for.**
Any `obj-m` object inherits the kernel's UBSAN `KBUILD_CFLAGS`, because
`is-kernel-object` is `y` for external modules too. Third-party modules whose
code trips a UBSAN check then misbehave at runtime while loading perfectly
cleanly, which is the worst combination for diagnosis. This bit VMware's
`vmnet`/`vmmon` visibly, with packet forwarding failing and VMs destabilising.
`0017-kbuild-ubsan-extmod-opt-in.patch` makes UBSAN opt-in for external
modules; in-tree builds are untouched, and a module that wants it can still set
`UBSAN_SANITIZE := y`.

**Scheduling and memory defaults are tuned for a desktop, and an inference box
is not one.** The curated series covers that: BORE scheduling, BBR3, vmscan and
VFS-cache behaviour under sustained pressure, `max_map_count`, timer frequency,
block-layer latency and mq-deadline tuning, THP defrag defaults. Config
fragments in `configs/fragments/` handle the rest, selected against what the
machine actually reports rather than a fixed profile.

**dma-buf cannot express memory tiering.** This one is the reason two of the
four originals exist, and it needs its own section.

## The dma-buf gap, and how GreenBoost hit it

GreenBoost pins system RAM as DMA-BUF hugepages and hands it to CUDA, so the
GPU reads weights straight out of DDR over PCIe. Pages pinned through
`pin_user_pages()`/`FOLL_LONGTERM` sit outside the normal reclaim path by
design. That is correct, and it leaves a hole: when memory gets tight, nothing
generic lets an exporter say *which* of its pinned buffers should be given up
first.

A KV cache re-read on every decode step and a cold expert's weights are not
equally valuable, and only the exporter knows that. dma-buf has nowhere to put
the distinction.

So GreenBoost invented its own. It carries a `gaming_mode` module parameter, a
per-buffer heat score pushed continuously from the CUDA shim via a private
`GB_IOCTL_SET_HEAT` ioctl, and its own LRU that the module reorders under
pressure. All of that is a workaround for one missing generic hint, and every
other out-of-tree tiering driver has to invent the same thing differently.

That is the argument for `0019-dma-buf-priority-hint.patch`: an advisory
`atomic_t` on `struct dma_buf`, `0..255`, default `128`, lower reclaimed
earlier. Two exported helpers, two ioctls so userspace holding an fd can do the
same, and the value reported via fdinfo. A hint, not a policy: dma-buf core
implements no reclaim of its own, it only stores the number for exporters that
already have a reclaim path.

**The blocker, stated plainly.** The only user is an out-of-tree module. Adding
UAPI with no in-tree user is normally declined, and that is the right call:
UAPI is forever, and nothing in-tree constrains the semantics or proves the
design survives a second consumer. The cover letter has to lead with this
rather than bury it. `greenboost.ko` does now genuinely *read*
`dma_buf_get_priority()` in its T2 eviction sweep instead of only writing it,
which makes the submission honest, and does not remove the blocker.

`0020-dma-buf-compressed-descriptor.patch` is the sibling. GreenBoost keeps
cold mixture-of-experts weights compressed in place rather than evicting them,
because re-fetching over PCIe costs more than inflating locally. An importer
has no standard way to be told "this buffer is compressed, here is how to read
it back". The patch adds that descriptor.

**It is finished and it should not be sent.** There is no in-kernel reader and
no in-kernel producer of the state it describes. Sending it now would spend
reviewer attention on an API nothing can exercise. It stays here, applied
locally, until that changes. `upstream-candidates/SUBMISSION.md` records that
assessment, patch by patch, so the decision does not have to be re-argued from
memory.

## The measurement that was lying

`0021-pci-sysfs-document-link-speed-width-attrs.patch` is documentation, and it
exists because of a misdiagnosis.

`max_link_speed`, `max_link_width`, `current_link_speed` and
`current_link_width` have been exported under `/sys/bus/pci/devices/.../` since
2018, and none of the four appear anywhere in `Documentation/ABI`.
`current_link_speed_show()` performs a fresh `PCI_EXP_LNKSTA` read on every
open, so it reports the link state at that instant. Modern GPUs retrain their
link constantly as part of idle power management.

Which means comparing `current_link_speed` against `max_link_speed` reads
exactly like a test for a degraded link, and is not one. Measured on an
RTX 5070 in a PCIe 4.0 x16 slot, same boot, no configuration change between
reads: **5.0 GT/s idle, 16.0 GT/s under load.**

GreenBoost got this wrong in production. Its `pcie_degraded` alarm fired
thirteen times claiming a gen2 link on a connection that measures Gen4 x16
whenever it is actually being used. The patch does not change any behaviour; it
writes down what the attribute means so the next person does not build the same
false alarm.

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

## Scope, honestly

This is built and run on one machine: an RTX 5070, 12 GB, PCIe 4.0 x16, 61 GB
DDR5. The config fragments are selected from detected topology rather than
hardcoded, and the patch series is generic, but "works here" is the only claim
that has been earned. If you run it somewhere else, the interesting part is
where it disagrees with your hardware.

## License

The kernel patches are GPL-2.0, as the kernel is. Third-party patches keep
their original authorship and are recorded in `patches/VENDOR.lock`; the
originals under `patches/custom/` are mine. `hyphaed` itself is GPL-2.0.
