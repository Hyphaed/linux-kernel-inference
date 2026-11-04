# Changelog

## 1.0.1 (2026-11-04)

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

`python -m pytest tests/ -q` → 719 passed, 8 skipped.
