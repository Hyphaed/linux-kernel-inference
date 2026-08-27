# hyphaed — Architecture Reference

## Overview

hyphaed is a Python wizard that builds and installs a custom Linux kernel
as a coexisting Ubuntu flavour (`linux-image-X.Y.Z-N-hyphaed`). It is
structured as a linear pipeline of **phases**, each of which reads and
writes a shared `Ctx` state object. The pipeline can be entered at any
phase from a previous run's saved state.

## Phase pipeline

```
detect → source → patch → configure → build → package → install → postinstall
  [1]      [2]     [3]       [4]        [5]      [6]       [7]         [8]
```

Each phase module in `hyphaed/phases/` exports `NAME: str` and
`run_phase(ctx) -> <phase-specific output>`. Phases are ordered and
registered in `phases/__init__.py::ORDER` and `MODULES`.

### Phase responsibilities

| Phase | Key inputs | Outputs / mutations |
|---|---|---|
| `detect` | `uname -r`, `/proc/cpuinfo`, `/sys`, nvidia-smi | `ctx.profile` (HardwareProfile) |
| `source` | `ctx.profile.running_kernel`, apt | `ctx.source_dir` (Path to extracted linux-N.M/) |
| `patch` | `ctx.source_dir`, `patches/series`, `VENDOR.lock` | git repo with patches applied; `state/applied-series.json` |
| `configure` | `ctx.source_dir`, `configs/base/`, `configs/fragments/`, preset | `ctx.source_dir/.config` (final); floor-validated |
| `build` | `ctx.source_dir`, `ctx.kernel_pkgver` | `.deb` files in `build/` |
| `package` | `.deb` files | `out/debs/*.deb` (packages only), `out/manifest.json`, `out/System.map-<pkgver>` |
| `install` | `out/debs/`, GRUB state | installed kernel, `/etc/default/grub.d/90-hyphaed.cfg` |
| `postinstall` | running kernel, DKMS | DKMS modules rebuilt; smoke-check report |

### Ctx state object

`hyphaed/state.py` defines `Ctx`, a `dataclass` that carries all
inter-phase state. After each phase, CLI snapshots `ctx` to
`state/machines/<hostname>/ctx.json` (keyed by `platform.node()` via
`cli.py::_machine_state_dir()`, so desktop and laptop state stay isolated
when the repo is shared across boxes) so a subsequent `--phase X` or
`--from-phase X` can restore where the last run left off. The repo-root
`state/ctx.json`/`applied-series.json`/`profile-*.json` files predate this
per-machine migration and are permanently inert — nothing writes to them
anymore.

Fields of note:
- `ctx.profile` — `HardwareProfile`: P/E core masks, GPU arch, Secure Boot flag, NVIDIA driver version
- `ctx.source_dir` — `Path` to `build/linux-N.M.P/` extracted source tree
- `ctx.kernel_pkgver` — the `X.Y.Z-N` version string used for `LOCALVERSION` and deb naming
- `ctx.build_dir` — `Path` to `build/` (parent of source_dir)
- `ctx.dry_run` — bool; propagated from `--dry-run` CLI flag
- `ctx.args` — the raw argparse namespace, for per-phase flags like `--target`

## Key modules

### `hyphaed/topology.py` — HardwareProfile

Detects and encodes everything hardware-specific. Called once in the
`detect` phase and persisted to `state/profile.json`.

Output includes:
- `cpu_model`, `p_core_mask`, `e_core_mask` — CPU topology for `nohz_full` / `rcu_nocbs` cmdline composition
- `gpu_arch` — `"blackwell"`, `"ada"`, `"ampere"` etc. — used to select NVIDIA config fragments
- `secure_boot` — bool; triggers MOK check in install phase
- `nvidia_driver_version` — floor check in detect; postinstall smoke-check baseline
- `running_kernel` — `"7.0.0-15-generic"` — used by source phase to select the right `apt source` target

### `hyphaed/grub.py` — cmdline composer + drop-in writer

The composer builds a complete kernel cmdline from three layers:
1. **Base**: what's already in `/etc/default/grub GRUB_CMDLINE_LINUX`
2. **Profile**: topology-derived parameters (P/E core isolation, IOMMU, nvidia.modeset, etc.)
3. **Preset**: any extra flags from the active YAML preset

Skip-keys logic ensures parameters already set in `/etc/default/grub` are
not duplicated in the drop-in. The `mitigations=off` guard is in the
composer and cannot be overridden.

Output: `/etc/default/grub.d/90-hyphaed.cfg` — a single-stanza file that
only sets `GRUB_CMDLINE_LINUX_DEFAULT` and `GRUB_DEFAULT`.

`grub.py::pin_default_kernel(kver, dry_run=...)` (added 2026-07-30) persists
the newly-installed kernel as GRUB's default via `grub-set-default` (unlike
the one-shot `grub-reboot`), parsing the installed kernel's menu-entry id out
of `/boot/grub/grub.cfg`. Called from `install.py::run_phase` after the
cmdline drop-in + `update_grub()`. `cli.py`'s `status`/`doctor` commands
cross-check `grubenv`'s `saved_entry` still resolves to a currently-installed
`linux-image-*-hyphaed` package, catching the class of stale pointer this
was added to prevent.

### `hyphaed/kconfig.py` — .config parser + floor validator

After `make olddefconfig`, the configure phase reads the final `.config`
and checks that GreenBoost-required options (`CONFIG_DMA_BUF`,
`CONFIG_HUGETLB_PAGE`, `CONFIG_IOMMU_SUPPORT`, `CONFIG_EVENTFD`,
`CONFIG_MODULES`, `CONFIG_CPU_FREQ`) are not `=n`. Any demotion aborts
the build with a clear error listing the culprit fragment.

### `hyphaed/gitops.py` — patch application

The patch phase runs `git init` in the source tree, commits the virgin
tree as a base commit, then applies patches one-by-one via
`git am --3way`. This gives clean conflict resolution (fallback to 3-way
merge) and a legible `git log` of what was applied.

`gitops.py` also enforces the **BORE vs prjc mutex**: `CONFIG_SCHED_BORE`
and `CONFIG_SCHED_CLASS_EXT` can coexist (BORE patches the default
scheduler; sched_ext is a parallel extension hook), but `prjc` (Project C
PDS scheduler) conflicts with BORE and must not be included simultaneously.

### `hyphaed/phases/build.py` — make progress UI

The build phase runs `make -j$(nproc) bindeb-pkg` and renders a Rich
progress bar. Two tasks share the same `Progress` instance:

- **compile** task: ticked on every `_STEP_RE`-matched kbuild line
  (`CC`, `LD`, `RUSTC`, etc.). Initial total `_STEP_ESTIMATE = 22_000`;
  `_grow_total()` extends it dynamically so the bar never hits 100% early.
- **packaging** task: activated only when `_PKG_RE` fires on real
  packaging-stage lines (`dh_*`, `dpkg-deb`, `dpkg-buildpackage`,
  `dpkg-genchanges`). Notably, `dpkg-source` lines are excluded from
  `_PKG_RE` to prevent false early-activation during the source-unpack
  prelude of `bindeb-pkg`.

### `hyphaed/phases/source.py` — apt source + dpkg-source progress

`_apt_source` wraps `apt source <pkgname>` with a Rich `Progress` bar
that streams `dpkg-source`'s output via `util/run.run(..., on_line=...)`.

Progress strategy:
- Task starts in **indeterminate spinner** mode on the first
  `dpkg-source: info: extracting` line.
- When the first `applying` line arrives, the series file at
  `build/linux-*/debian/patches/series` is read once to set the total
  (typically 400–600 entries for Ubuntu kernels). The bar becomes determinate.
- Each subsequent `applying <patch-name>` ticks the bar and updates the
  description with the truncated patch name (48-char cap for stable width).
- If the series file is absent (tarball fallback path), the spinner runs
  with live patch-name labels but no ETA.

### `hyphaed/util/run.py` — subprocess wrapper

All subprocess calls go through `run()` (or `run_sudo()`). Key design
decisions:
- **`force=True`**: bypasses the module-level `_DRY_RUN` flag. Used for
  read-only probes (`uname`, `lspci`, `nvidia-smi`) that must run even in
  dry-run mode so detection still works.
- **`on_line` callback**: optional `Callable[[str], None]` invoked for each
  complete output line (both stdout and stderr). Used by the source phase
  for dpkg-source progress. Per-stream byte buffers ensure lines split
  across chunks are reassembled correctly.
- **`tee_log`**: when set, all output is simultaneously written to the
  provided log file. Used by the build phase to persist the full build log
  to `out/logs/build.log` even while the progress bar consumes stdout.

## Patch system

### patches/series

Ordered list of patch names, one per line. Applied left-to-right by the
patch phase. Removing a line skips that patch — the build still works
because all patches are optional performance overlays, not requirements.

### patches/VENDOR.lock

Maps each patch name to a source URL and sha256. Two kinds:

| Kind | Format | Fetched by |
|---|---|---|
| `local-file` | `file:///abs/path/to/file.patch` | `open(path).read()` |
| `git-commit` | `git+file:///abs/repo@<sha>` | `git format-patch -1 --no-signature <sha>` |

`patches/fetch.py` is run once per kernel release. It resolves each entry,
verifies the sha256, and writes the `.patch` file to `patches/`. The wizard
at runtime reads these pre-fetched files and never hits the network.

### patches/custom/

Hand-maintained single-hunk patches (e.g. extracted from a larger upstream
patch that had conflicts). Referenced as `local-file file:///abs/path`.
`python patches/fetch.py --discover` pins the sha256 for anything in
`custom/` that isn't yet in `VENDOR.lock`.

### Patch compatibility headers

Each patch should carry two optional headers (in the git commit message or
patch header) that help forward-porting to new kernel versions:

```
Forward-Port-Notes: touches kernel/sched/bore.c, kernel/sched/fair.c
Conflict-Marker: sched_entity.vlag field (added by XanMod; absent in stock)
```

These are documentation-only — not parsed by the wizard, but used by the
developer when a future `git am --3way` produces conflicts.

## Config system

### configs/base/

Snapshots of `/boot/config-<running-kernel>` captured during the detect
phase. The configure phase picks the newest file in this directory as the
starting `.config`, ensuring the custom kernel inherits all Ubuntu security
and hardware hardening decisions.

### configs/fragments/

`CONFIG_*=y/n/m` delta files. Named with a numeric prefix (10–80) that
controls merge order. `merge_config.sh` applies them left-to-right over
the base config. Fragment 70 (`70-greenboost-required.config`) must be
last because it pins options that other fragments might otherwise demote.

Fragment naming convention:
```
10-base.config          # shared baseline for all presets
20-ai-inference.config  # CUDA, huge pages, NUMA, io_uring
30-gaming.config        # BORE, HZ=1000, PREEMPT, low-latency audio
40-vm.config            # VFIO, KVM, IOMMU, nested virtualization
50-network.config       # BBR, fq_codel, WireGuard
60-security.config      # kernel-hardening-checker recommendations
70-greenboost-required.config  # GreenBoost floor — always last
```

### configs/presets/

YAML files that list which fragments to activate:
```yaml
# gaming-ai-vm.yaml
name: Gaming + AI + VM
fragments:
  - 10-base
  - 20-ai-inference
  - 30-gaming
  - 40-vm
  - 50-network
  - 60-security
  - 70-greenboost-required
```

## GreenBoost integration

`~/Dev/greenboost_all/greenboost` is a first-class constraint, not an
afterthought. Three integration points:

1. **Configure phase**: `kconfig.py` validates the final `.config` and
   aborts if any GreenBoost-required option is demoted (see hard constraints).
2. **Fragment ordering**: `70-greenboost-required.config` is always the
   last fragment, so it overrides any conflicting earlier fragment.
3. **GRUB drop-in**: the cmdline composer includes `iommu=pt intel_iommu=on,igfx_off`
   which GreenBoost requires for DMA-BUF sharing. Coordinates with GreenBoost's
   own `/etc/default/grub` edits via the skip-keys mechanism.
4. **Post-install smoke check**: `postinstall.py` checks
   `/sys/class/greenboost/greenboost/status` to confirm GreenBoost came up
   cleanly under the new kernel.

## Security model

All CPU mitigations stay ON at both Kconfig and cmdline. This is enforced
at two layers:
- `hyphaed/grub.py`: `mitigations=off` is stripped and warned about.
- `configs/60-security.config`: aligns with kernel-hardening-checker
  recommendations; `CONFIG_CRYPTO_USER_API_AEAD=n` disables the AF_ALG
  AEAD interface (defense-in-depth for CVE-2026-31431).

See the project's security notes for CVE tracking.

## Testing

Tests live in `tests/` and use pytest. They cover all pure-logic modules
without running a kernel build:

| Test file | What it covers |
|---|---|
| `test_topology.py` | cpu_list parsing, P/E mask round-trip |
| `test_version.py` | uname-r parsing, pkgver formatting |
| `test_grub.py` | cmdline composition, dedupe, skip-keys, mitigation guard |
| `test_kconfig.py` | .config parsing, floor validation |
| `test_install_order.py` | phase ORDER invariant |
| `test_gitops.py` | BORE vs prjc mutex detection |
| `test_state.py`, `test_state_resolve.py` | Ctx persistence round-trip |
| `test_fetch.py` | local-file + git-commit fetch kinds |
| `test_source_pkg.py` | apt source package name detection |
| `test_build_progress.py` | _STEP_RE / _PKG_RE regex correctness (dpkg-source exclusion) |
| `test_verify.py` | post-install verification helpers |

Run: `python -m pytest tests/ -q` (90 tests, <200ms).
