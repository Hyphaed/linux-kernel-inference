# Changelog

## 1.0.1 (2026-11-04)

### 7.2.15: the default series, and an experimental set that is not measured

The default `kernel-org-7.2` series applies unchanged on v7.2.15 (sequential
`git am --3way`, 32 touched objects compiled). This box now boots
`7.2.15-hyphaed` built from it plus `series.experimental`, and `hyphaed verify`
is green. That proves the kernel boots and every patch reaches the machine. It
doesn't prove any of the experimental patches makes anything faster: there is no
A/B yet, so none of them is in the default series.

`series.experimental` now carries two groups (ADR-009):

- `0031`-`0035`, Intel's hybrid-core load-balancing fixes from 7.3, re-cut for
  7.2. Two of the six upstream patches are already in 7.2.15. They are inert on
  the 14900KF (SMT keeps ITMT), so expect no change here; they exist for the P/E
  topology and for the laptop's successors.
- 28 commits from 7.3 under `patches/custom/perf73/`, picked from 474 that claim a
  performance effect (302 apply cleanly, 171 conflict). They're small, reviewed
  and self-contained; 5 more were dropped because they need other 7.3 code the
  compiler found. 11 more validate but stay commented out.

One thing worth knowing before reading the list: `scx_bpfland` runs here with
`switch_all=1`, so every normal task is scheduled by sched_ext, not the fair
class. Fair-class backports change nothing for day-to-day load while that service
runs. Reasoning and the full table: `docs/research/7.2.15-perf-backports-2026-11-04.md`
and `docs/research/7.2.15-patch-sweep-2026-11-04.md`.

### A scheduler swap that can't be expressed as "append to the series"

`patches/kernel-org-7.2/series.sched73` puts the 7.3 scheduler stack on 7.2.15
(cgroup task-migrate notifier `0036`, sched_ext `0037`, the single-runqueue EEVDF
fair class `0038`, then CachyOS's 7.3 BORE, which is written against exactly that
`fair.c`). `HYPHAED_SERIES_VARIANT=sched73` selects it and the patch phase fails
loudly if the named file doesn't exist. It validates and the full `vmlinux`
links. It has not been built into a package or booted, and the fair-class part
buys little while `scx_bpfland` owns normal tasks. Don't also enable
`0031`-`0035`: the transplant already contains them.

### The 7.3 series was a copy of 7.2.12 that had never run

Run on v7.3.1 it failed at the first patch. Three of the 26 patches needed work,
the other 23 applied unchanged: BORE is replaced by CachyOS's own 7.3 file (with
one hunk re-cut because vanilla lacks `CONFIG_CACHY`), and `0023` cache_ext and
`0024` AppArmor userns were re-anchored (`custom/*-7.3.patch`). It applies and
compiles (31 objects); it has not been booted, and whether the cache_ext hooks
fire is still unproven. Notes: `docs/research/7.3.1-patch-sweep-2026-11-04.md`.

### Bump pipeline: five new stages, and builds stop eating the machine

- `22-perf-candidates.py` lists donor-release commits missing from the target
  stable tag (`git cherry`, so a commit stable already backported is dropped),
  filtered to subsystems this box runs and ranked by how plainly they claim a
  speedup. `22b-perf-apply-test.py` then cherry-picks each onto the target plus
  the default series. Neither decides anything; a human writes the verdicts.
- `35-nvidia-compile-check.sh` compiles the pinned NVIDIA open driver against the
  new kernel's headers, so a driver that breaks on a release candidate shows up
  before the build instead of in the `nvidia` phase after it. It can't prove
  link-time symbols; only the real phase does.
- `36-qemu-smoke-test.sh` boots a kernel under QEMU/KVM with a busybox initramfs,
  loads the host's real `scx_bpfland`, then runs futex/epoll/mmap/signal/fork/
  memcg stress. 7.2.15 passed three times. It's a VM: no NVIDIA, NVMe or DKMS,
  and its throughput numbers aren't performance evidence (`mmap` was about 7%
  lower than 7.2.14 with barely overlapping ranges, n=3; worth watching, not a
  finding).
- `61-first-boot-health.sh` prints PASS/WARN/FAIL right after booting a new
  kernel and a rollback hint. `scripts/prune-kernels.sh` keeps one hyphaed build
  and the newest stock `-generic` and removes the rest (dry run unless
  `--apply`).
- Builds were using all 32 threads while the workstation was in use. The kernel
  build, the package step and the NVIDIA module build now take 75% of the CPU
  (`HYPHAED_JOBS=N` overrides) and the kernel `make` runs at `nice -n 10`.
- `run-all.sh` keeps the validation tree for stage 35, then deletes it. The
  `--target` default in `hyphaed/cli.py` moves from 7.2.10 to 7.2.15.

### `nvidia_fs` never got built for the new kernel

On 7.2.15-hyphaed `nvidia-fs.ko` didn't exist, so `nvidia-fs-load.service`
failed at boot and GPUDirect Storage ran in POSIX compat mode (every NVMe to GPU
byte crossing PCIe twice). `hyphaed verify` showed two red rows for one cause.

nvidia-fs's `dkms.conf` has `BUILD_DEPENDS=nvidia`, and DKMS only counts its own
nvidia build. Ours is built outside DKMS since 2026-10-31, so `dkms autoinstall`
quietly skipped nvidia-fs, and the nvidia phase ran it with `check=False`. The
`nvidia` phase now follows the autoinstall loop with
`dkms install nvidia-fs/<ver> -k <kver> --force` when no `nvidia-fs.ko` exists for
the target kernel, and warns with the `make.log` path if that build fails.
`hyphaed verify` names the cause instead of a bare "NOT loaded".

Checked on the box: the forced build signed and installed the module, it loaded
against our `nvidia`, and `hyphaed verify` went to 24/24 green. Not covered: the
phase hook itself hasn't run end to end on a real bump yet, only in unit tests.
Manual fix if it ever happens again:
`sudo dkms install nvidia-fs/2.30.2 -k <kver> --force`.

### Smaller nvidia phase fixes

- Installing a second kernel's NVIDIA modules overwrote `depmod.d`, deleting the
  override lines for the first one, which rollback needs. They're merged per
  kernel now.
- Booting a kernel with no NVIDIA module made `nvidia-smi`'s driver error read as
  a running GPU job, blocking the phase. No driver means nothing can be on the GPU.
- The module build used every CPU thread; it follows the 75% build cap like the
  kernel build.

## 1.0.0 (2026-11-02)

First tagged release. Before this the version string said 0.1.0 and nothing was
tagged, so everything below is "since the repo started", with the newest work
first.

### The wizard stops at `install`; three phases now run after it

`hyphaed` used to end at `postinstall`. It now has `nvidia`, `desktop` and
`proton`, in that order, and `make bump` runs the whole chain.

- `nvidia` builds the open kernel modules from a pinned checkout with the
  kernel's own compiler and flags, and installs the userspace from the official
  `.run` with `--no-kernel-modules` so both halves are the same version. Rollback
  to the apt packages: `diagnostics/nvidia-restore-apt.sh`.
- `desktop` rebuilds mutter and gnome-shell from the Ubuntu source, per host
  (`-march=native` isn't portable), and keeps the result only if
  `scripts/measure_desktop.sh` shows it isn't worse.
- `proton` reports the Valve Protons Steam has installed and re-installs the
  GreenBoost wrapper. The from-source `proton-gb-nr` rebuild is opt-in
  (`proton.rebuild_gb_nr: true`). It costs about 25 GB and an hour, and no
  benchmark shows it beating Valve's builds yet.

None of these three has a before/after A/B in the repo for a game or the
desktop. They're installed and unit-tested, and the Proton build hasn't been
run in a game yet.

### A config fragment can be bound to one motherboard

`85-ncore-board-trim.config` removes drivers and guest-OS code for hardware the
ASRock B760M-ITX/D4 doesn't have. On any other machine that would delete a NIC
or storage driver the box needs, and the CPU codename can't tell two Raptor Lake
boards apart. A fragment now declares `# requires-board: <DMI vendor> <name>` in
its leading comment, `HardwareProfile.board` is read from
`/sys/class/dmi/id/`, and `_select_fragments()` skips the file on any other
board, or when the board is unknown. This changes build time, module count and
attack surface. It doesn't change a runtime default, so it has no tok/s number.

`44-block-iosched.config` pins kyber and bfq as modules and mq-deadline built
in. Behaviour is unchanged (NVMe still defaults to `none`); it only stops a
base-config change from silently dropping the schedulers.

### Patches without evidence can't reach the default series by accident

`patches/kernel-org-<MM>/series.experimental` holds patches with no A/B result
(ADR-009). It's applied only with `--experimental` or `HYPHAED_EXPERIMENTAL=1`,
and the patch phase warns when it is on. `patches/kernel-org-7.3/` is staged:
it applies and compiles against the tag, it has not been booted.

### `-y` opened menuconfig on its own

`make bump` runs unattended with `-y`, and the "open menuconfig to review?"
prompt answered yes, launching a full-screen editor in a non-interactive run.
`-y` no longer auto-answers that prompt.

### Bump pipeline

- `15-research.py` queries arXiv, Semantic Scholar, OpenAlex, lore.kernel.org
  and the NVIDIA open-module releases for items newer than the last bump, tags
  ones already in `docs/research/` as `[seen]`, and never fails the run. It
  prints `TIER=full` when major.minor changed.
- `25-ubuntu-security-audit.py` lists Ubuntu security/SAUCE commits missing from
  the kernel.org tag. Read-only. Ubuntu ships no 7.2 kernel, so there's no
  branch to rebase on.
- Fixes in `30-validate-series.sh`, `40-build.sh` and `run-all.sh`; new
  `clean-space.sh`, `dedupe-clones.py`.

### Tuning tools that refuse to guess

`diagnostics/perf_tune.py ab <knob>` benches, flips the knob, benches, restores,
benches a second baseline, and only calls a delta real when it clears run-to-run
noise. `diagnostics/gpu_baseline.py` records RTX 5070 tensor rates with clocks,
power and PCIe link logged alongside, so a number can be told apart from
throttling. It's the "before" number: there is no tensor-core unlock for GB205
(`docs/research/rtx5070-tensor-unlock-2026-10-28.md`).

```bash
python3 diagnostics/perf_tune.py audit      # no root, no changes
python3 diagnostics/gpu_baseline.py --label before
```

### Kernel 7.2.x

The box boots `7.2.14-hyphaed`, and `configs/base/` has the 7.2.14 generic
snapshot. `--target` still defaults to
`7.2.10` in `hyphaed/cli.py`, so pass `--target 7.2.14` until that default is
bumped.

### Earlier work (2026-08 to 2026-10)

- Workstation hardening and a kernel hardening scan, wired into the wizard.
- `pcie_aspm=performance` had been silently dropped for six builds.
- Fragments that set a symbol but never got the capability behind it.
- The `make install` target was in `.PHONY` with no rule, so it did nothing.
- A fake five-hour build ETA derived from the Rust prologue.
- `fetch.py` and `fetch-patches` resolving the wrong `VENDOR` lock for 7.2.
- Patches 0015, 0019 and 0021 were sent upstream, with `Assisted-by:`
  disclosure enforced by the test suite. 0016 (THP defrag default) was sent and
  withdrawn the next day: its commit message had `vma_thp_gfp_mask()`
  backwards. Trail in `upstream-candidates/SUBMISSION.md`.
- Boot audits for 7.1.10, 7.2.0, 7.2.2, 7.2.6 and 7.2.11 under `docs/`.

### Verification

`python -m pytest tests/ -q` → 719 passed, 8 skipped (751 passed as of 1.0.1).
