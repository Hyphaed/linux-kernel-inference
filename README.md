[![Kernel](https://img.shields.io/badge/kernel.org-7.1.x%20%7C%207.2.x-orange.svg)](https://kernel.org/)
[![Tested on](https://img.shields.io/badge/running-7.2.1--hyphaed-green.svg)](https://ubuntu.com/)
[![Python](https://img.shields.io/badge/python-3.10%2B-yellow.svg)](https://python.org/)
[![Tests](https://img.shields.io/badge/tests-475%20passing-brightgreen.svg)](tests/)
[![License: GPL v2](https://img.shields.io/badge/license-GPL--2.0-red.svg)](https://www.gnu.org/licenses/old-licenses/gpl-2.0.en.html)

# 🐧 Hyphaed Kernel

A Linux kernel built while doing local AI inference on a real workstation, with
patches written to close gaps GreenBoost's kernel module kept hitting and
config fragments tuned against what a model-serving box actually does under
load, not what a stock desktop kernel assumes. This is the kernel side of
[GreenBoost](https://gitlab.com/IsolatedOctopi/greenboost), which extends a
GPU's VRAM with system RAM and NVMe so a model bigger than the card still runs
with its compute on the GPU.

The build tool is called `hyphaed`. It's a phased pipeline — detect the
machine, patch a real kernel.org tree, configure it from hardware-selected
fragments, build, package, install, and check the result actually works —
not a fork of the kernel and not a config generator you paste values into by
hand.

---

## What this contributes

Five patches here are original work, closing gaps found by running GreenBoost
hard against real inference workloads. Two more are substantial forward-ports
of other people's research code and Ubuntu's own SAUCE, done because nobody
else had carried them to a current kernel.org tree yet. The rest — fifteen
patches on the 7.1 series, eighteen on 7.2 — are other people's tuning work,
curated and pinned by sha256, with attribution intact.

### Original and forward-ported patches

| # | Patch | What it does | Author | Status |
|---|---|---|---|---|
| `0015` | kbuild: UBSAN opt-in for external modules | `is-kernel-object` made every `obj-m` inherit the kernel's UBSAN flags; out-of-tree modules (`greenboost.ko`, VMware's `vmnet`/`vmmon`) got instrumentation they never asked for and misbehaved silently at runtime | Ferran Duarri | **sent upstream** (as `0017`, before a later renumber — see below) |
| `0019` | dma-buf: generic reclaim-priority hint | Advisory `0..255` priority field on `struct dma_buf`, two ioctls, reported via fdinfo. First generic way for any dma-buf exporter to rank its own pinned buffers instead of inventing a private ioctl | Ferran Duarri | **sent as RFC**, blocker stated up front: only consumer is out-of-tree |
| `0020` | dma-buf: compressed-content descriptor | Lets an exporter that compressed a buffer in place tell an importer how to inflate it, so "compressed" becomes an alternative to "evicted" | Ferran Duarri | **finished, deliberately held** — no in-kernel producer or reader exists yet |
| `0021` | Documentation/ABI: four undocumented PCI link-speed attributes | `max_link_speed`/`max_link_width`/`current_link_speed`/`current_link_width` exported since 2018, documented nowhere. Also documents that a link retrains constantly under idle power management, so one sample can read 5.0 GT/s idle and 16.0 GT/s under load on the same boot | Ferran Duarri | **sent, v2 after a reviewer correction** (see below) |
| `0023` | mm/bpf: `cache_ext`, pluggable page-cache eviction via eBPF | Forward-port of Zussman et al.'s SOSP '25 research code (v6.6.8 → current kernel.org, real API drift across `bpf_struct_ops`, kfunc registration, tracepoint ordering) | Tal Zussman et al., forward-port by Ferran Duarri | local only — compiles, applies, **not yet measured against a real workload** |
| `0024` | AppArmor: restrict unprivileged user namespace creation | Forward-port of Ubuntu's own SAUCE, which never went upstream. Vanilla kernel.org has the full mediation mechanism; only the switch was missing, so unprivileged userns creation was unrestricted on a kernel-org build while restricted on Ubuntu's `-generic` beside it in GRUB | John Johansen (Canonical), forward-port by Ferran Duarri | local only |
| `0028` | AppArmor: advertise userns restriction via securityfs | `0024` wired real enforcement but never added the securityfs boolean `apparmor.service` checks before trusting the sysctl — so Ubuntu's own boot script silently reset the restriction to off on every boot. One-line advertisement fix, no enforcement change | Ferran Duarri | local only |

**Numbering note.** `0021`'s `From:` and `Signed-off-by:` lines are frozen at
whatever they were the moment it was mailed — that's the historical record
`git send-email` produced, and it can't be renamed after the fact. The local
series has been renumbered since some of these were first sent (a patch got
removed, later ones shifted down), so a Message-ID you find in
`upstream-candidates/SUBMISSION.md` may cite a number one or two off from
what's in `patches/kernel-org-7.2/series` today. The filename slug
(`kbuild-ubsan-extmod-opt-in`, `pci-sysfs-document-link-speed-width-attrs`)
is the stable identity; the number is series position, not upstream identity.

**`0019` and `0020` explained further, because the "why" matters for local
inference:** memory pinned through `pin_user_pages()`/`FOLL_LONGTREM` sits
outside normal reclaim by design, so when RAM gets tight nothing could
distinguish a KV cache re-read on every decode step from a cold expert's
weights that may not be touched again this generation — they aren't equally
valuable, and only the exporter knows it. `0019` gives GreenBoost's own T2
eviction sweep a standard way to read that signal instead of a private ioctl
nothing else can see. `0020` composes with it: `0019` says which buffers to
keep, `0020` would let an exporter that kept one under compression describe
how — but nothing on this stack currently compresses a dma-buf in place, so
it stays a specification rather than something with a measured effect.

### An honest submission outcome, not a hidden one

`0016` (THP defrag default → `defer+madvise`) was sent 2026-08-20 and
**withdrawn the next day.** A reviewer's question sent the commit message's
author back to read `vma_thp_gfp_mask()` properly, and the claimed mechanism
— that non-madvised faults could stall in direct compaction — was backwards;
they can't, they get `GFP_TRANSHUGE_LIGHT` with no reclaim flag. The patch
did close to the opposite of what its own changelog claimed. Full trail in
`upstream-candidates/replies/0016-withdrawal.txt` and
`upstream-candidates/SUBMISSION.md`. It's recorded here because a wrong
submission is real project history, not something to leave out of the table.

### Curated from upstream: 16–18 patches, other people's work

| Source | Patches | What they cover |
|---|---|---|
| CachyOS | BORE scheduler (`0001`, native 7.2 port; hand-forward-ported for 7.1) | Burst-Oriented Response Enhancer scheduling |
| XanMod (Alexandre Frade et al.) | `vmscan`/vfs-cache tuning, `max_map_count`, block-layer wbt latency and rq-affinity, mq-deadline defaults, ACS override, kbuild extmod-sign, `-dbg` package opt-in, working-set protection | Sustained-memory-pressure and I/O-latency behavior a stock desktop config doesn't tune for |
| Liquorix (Steven Barrett) | dm-crypt workqueue | Encryption throughput under load |
| TKG / Clear Linux (Arjan van de Ven) | PCIe PME timeout | Wake-latency tuning |
| Mark Weiman | PCI ACS override (hand-forward-ported for 7.1; XanMod carries a native 7.2 port of the same patch) | VFIO/GPU-passthrough support, dormant by default |
| Jason Gunthorpe | udmabuf malformed-scatterlist fix (7.1 only) | Correctness fix, stable-backport candidate |

Every source is pinned by sha256 in `patches/VENDOR-kernel-org-7.1.lock` and
`patches/VENDOR-kernel-org-7.2.lock` — `patches/fetch.py` reads from a local
clone, nothing is fetched from the network at build time.

### The kernel build itself

Beyond the patches, the curated series and config fragments cover what a
workstation holding a large model resident and reading it hard actually
needs, where a stock kernel assumes a desktop: BORE scheduling, vmscan/VFS-
cache behavior under sustained memory pressure, `max_map_count`, timer
frequency, block-layer latency, THP defrag defaults, and the AppArmor userns
mediation above. `configs/fragments/` is selected against what the machine
reports (`hyphaed detect`), not a fixed profile — an AMD laptop and an Intel
desktop pull different fragment sets from the same preset.

---

## Layout

```
hyphaed/                  the build tool: a phased pipeline
  phases/                 detect, source, patch, configure, build,
                           package, install, postinstall, security
  greenboost.py            reads GreenBoost's hardware profile and
                           cross-checks it against ours
patches/
  kernel-org-7.1/series    active series for kernel.org 7.1.x
  kernel-org-7.2/series    active series for kernel.org 7.2.x (current default)
  custom/                  original work and forward-ports, including the six above
  VENDOR-kernel-org-*.lock every source pinned by sha256
  fetch.py                 populate from the pinned local sources
configs/
  fragments/               Kconfig fragments, selected by detected hardware
  presets/                 ai-only, gaming-ai-vm, gaming-ai-laptop-amd, safe
upstream-candidates/       submission-ready trees + SUBMISSION.md (what was
                           sent, what came back, what's still held and why)
diagnostics/               dry-run-by-default scripts for real hardware issues
                           found on this box (NVIDIA driver track switching,
                           GDS symvers mismatches, boot repairs, DGX-OS parity)
docs/                      research notes, patch planning, boot audits
tests/                     475 tests; tests/kernel_runtime/ reads the
                           actually-running kernel and skips itself on anything
                           that isn't a -hyphaed build
```

## Building

```bash
python -m hyphaed detect        # what this machine actually is
python -m hyphaed configure     # fragments -> .config
python -m hyphaed build
python -m hyphaed install       # dpkg -i + GRUB drop-in + DKMS + smoke checks
```

`install_wizard.sh` walks the whole thing interactively if you'd rather be
asked than remember flags. `--target` defaults to `7.2.1` — the version this
box actually boots (`uname -r` reads `7.2.1-hyphaed`); bump it in
`hyphaed/cli.py` once a newer kernel.org stable release has been validated
the same way, or run `hyphaed list-versions` to see the last five with dates.

The upstream trees aren't vendored here. `linux/`, the CachyOS patch
collection and the other reference clones are fetched, not committed, which
is why a checkout is a few megabytes rather than a couple hundred gigabytes.
`$KI_ROOT` in the lock files expands to your checkout, so the same lock
resolves on any machine.

## Testing

```bash
python -m pytest tests/ -q
```

475 tests, well under a second — mostly `hyphaed`'s own tooling invariants
(fragment wiring, patch-series consistency, cmdline composition, floor
validation), not kernel behavior. `tests/kernel_runtime/` is different: it
reads the currently *running* kernel back and checks whether each patch in
the series actually reaches the machine, not just whether it applied. It
skips itself entirely unless booted into a `-hyphaed` kernel, so
`python -m pytest tests/` stays green on any other box. On this one it found,
for example, that `0004`'s raised `max_map_count` default is masked by
systemd's own sysctl.d override running last — a patch that applies cleanly
and never takes effect, which is a different thing from a patch that's
broken.

## 🖥️ Scope, for context — currently used hardware

Built, tested, and used primarily on two machines, with the desktop used by
far the most:

**desktop** — RTX 5070 12GB VRAM, PCIe 4.0 x16, 64GB DDR4, i9-14900KF

**laptop** — RTX 5070 mobile 8GB VRAM, PCIe 5.0 x16, 32GB DDR5, Ryzen AI 9 365

Plus whatever hardware contributors or issue-reporters run, sometimes with
logs attached.

## License

The kernel patches are GPL-2.0, as the kernel is. Third-party patches keep
their original authorship and are recorded in the `VENDOR-*.lock` files; the
originals under `patches/custom/` are Ferran Duarri's. `hyphaed` itself is
GPL-2.0.
