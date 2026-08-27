# DGX OS parity — what's real, what's ported, what's rejected (2026-08-27)

Investigated by pulling and inspecting NVIDIA's own DGX baseos apt repo
directly (`repo.download.nvidia.com/baseos/ubuntu/noble/x86_64/`, GPG-signed,
`dpkg-deb -c`'d for real) rather than reading the thin DGX User Guide pages,
which don't list package contents or repo URLs at all.

## The framing tension, stated up front

DGX OS's own "auto-update" model is **Ubuntu Pro ESM + a curated,
version-pinned NVIDIA repo** — `Pin-Priority: 600` on the whole DGX baseos
repo, explicit `nvidia-driver-pinning-<version>` metapackages. It is not
"always latest." That's the same pin-then-apt-install discipline this repo
already fought through in the 595→610 driver saga (see `CLAUDE.md`'s
"Installing a specific NVIDIA driver" section). Anyone expecting "wire in
DGX's repo and it just tracks newest" is expecting the opposite of what DGX
actually does.

**More concretely, it isn't installable here at all**: the DGX baseos repo
only publishes a `dgx` component for Ubuntu 24.04 (`noble`). Verified live,
2026-08-27:

```
$ curl -o /dev/null -w '%{http_code}\n' https://repo.download.nvidia.com/baseos/ubuntu/noble/x86_64/dgx-repo-files.tgz
200
$ curl -o /dev/null -w '%{http_code}\n' https://repo.download.nvidia.com/baseos/ubuntu/resolute/x86_64/dgx-repo-files.tgz
404
```

This box runs 26.04 (`resolute`). So `apt install nvidia-system-core` is not
an option here — what follows is hand-porting the applicable subset, the way
`0024-apparmor-restrict-unprivileged-userns.patch` and the NVIDIA
driver-pinning scripts already do in this repo, not wiring in a repo that
doesn't serve this release.

## What's actually in the DGX metapackages

Pulled real dependency lists from `dists/noble/common/binary-amd64/Packages`:

- **`nvidia-system-core`**: `nv-grubmenu`, `nv-grubserial`, `nv-cpu-governor`,
  `nv-iommu`, `nv-limits`, `nvidia-acs-disable`, `nvidia-disable-init-on-alloc`,
  `nvidia-disable-numa-balancing`, `nvidia-fs-loader`, `nvidia-kernel-defaults`,
  `nvidia-mig-manager`, `nvidia-pci-bridge-power`, `nvidia-pci-realloc`,
  `nvidia-raid-config`, `nvidia-relaxed-ordering-{gpu,nvme}`, plus `nvme-cli`,
  `ipmitool`, `tpm2-tools`.
- **`nvidia-system-utils`**: `nv-persistence-mode`, `nvidia-modprobe`,
  `nvidia-container-toolkit`, `nvidia-fs-loader`, `nvsm`, `nvidia-logrotate`,
  `nvidia-motd`, `nvidia-conf-cachefilesd`.
- **`nvidia-system-extra`**: generic admin/build tooling (`build-essential`,
  `docker-ce`, `smartmontools`, `sosreport`, ...) — no kernel-relevant tuning.
- **`nvidia-system-station`**: a full GNOME desktop + LibreOffice + printer
  drivers metapackage for DGX Station hardware — not relevant here.

## Decision table

| DGX item | Verdict | Why |
|---|---|---|
| `nv-mitigations-off` (grub `mitigations=off`) | **REJECT** | Directly violates this repo's hard constraint: mitigations stay ON at both Kconfig and cmdline (`hyphaed/grub.py` actively refuses to compose this). Never port. |
| `nv-hugepage` (`transparent_hugepage=madvise`) | **REJECT** | DGX tunes for many isolated container/job workloads; this box intentionally runs `=always` (cmdline, `ai-only`/`gaming-ai-vm` presets) for one large inference process. Opposite goals — porting this would regress the exact thing this repo tunes for. |
| `nvidia-conf-cachefilesd` (assumes `/raid`) | **REJECT** | RAID/NFS-fleet specific (`ExecStartPre=/bin/mountpoint /raid`). This box has one NVMe root, no `/raid`. |
| NVSM, `nvidia-system-station`, the DGX metapackages themselves | **REJECT — not installable** | Repo has no `resolute` (26.04) component; 404 confirmed live. Only `noble`/24.04 is published. |
| `nv-iommu` (`iommu=pt`), `nvidia-drm-options` (`nvidia-drm.modeset=1`), `nvidia-disable-numa-balancing` | **Already adopted** | This box's cmdline already carries `iommu=pt`, `nvidia-drm.modeset=1`, `numa_balancing=disable`. No action. |
| `nv-limits` (nofile 500000) | **Already exceeds** | `ulimit -Hn` = 524288 already, from Canonical's own defaults. No action. |
| `nvidia-container-toolkit` (from `nvidia-system-utils`) | **Already installed** | `1.20.0-1` present via the existing `nvidia-container-toolkit` apt repo. No action. |
| `nvidia-peermem-loader` | **ADOPT** | DKMS builds `nvidia-peermem.ko` for this kernel already, but nothing loads it. Trivial, safe: `hyphaed/phases/postinstall.py::_check_dgx_parity_basics()` now `modprobe`s it and drops a `modules-load.d` entry. Only matters if GreenBoost's cluster/feeder ever does GPUDirect RDMA — harmless if not. |
| `nvidia-pci-bridge-power` | **ADOPT** | Forces PCIe bridge `power/control=on`, same latency-over-power-saving philosophy this box already applies via `pcie_aspm.policy=performance` on the cmdline. Ported as a systemd oneshot in `_check_dgx_parity_basics()`, same shape DGX ships (find `0604`-class bridges via `lspci`, write `on` to each bridge's `power/control`). Live check: `diagnostics/dgx-parity-check.sh`. |
| **DCGM** (`datacenter-gpu-manager-4-cuda13`) | **ADOPT** | Real, generic GPU health/Xid monitoring — not DGX-hardware-locked. Confirmed available today from the *already-configured* CUDA repo: `apt-cache policy datacenter-gpu-manager-4-cuda13` shows candidate `1:4.6.1-1`. Complements, doesn't duplicate, GreenBoost's own dataflux telemetry — DCGM watches the GPU itself (Xid errors, ECC, thermal/power health), dataflux watches the inference pipeline's state. Installed + enabled by `_check_dgx_parity_basics()`. |
| `nvidia-relaxed-ordering-{gpu,nvme}` toggle scripts | **ADOPT — opt-in diagnostic only** | This exact question is already open and unresolved in `CLAUDE.md`: "NVIDIA driver ≥525 itself disables relaxed-ordering for GPU-initiated P2P transactions on some host bridges (a correctness erratum) — forcing RO on could be actively counterproductive." That's a stated guess, not a measurement. `diagnostics/dgx-pci-relaxed-ordering-toggle.sh` ports DGX's real, vendor-blessed toggle (`nvme set-feature -f 198`, Samsung-only — and this box's data drive genuinely is a Samsung 990 EVO Plus, so the gate is real here) so the question can finally get a real before/after GDS-throughput answer instead of staying a guess forever. **Not applied automatically.** |
| `nvidia-acs-disable` | **ADOPT — opt-in diagnostic only, off by default** | Same effect area as the already-dormant `0018-tkg-pci-acs-override.patch` boot-param, but a no-reboot runtime alternative (`setpci` on the live `ECAP_ACS` register). Reduces PCIe device isolation, so it must stay an explicit opt-in — matching the existing `enable_pcie_acs_override` opt-in kwarg already in `hyphaed/grub.py`. `diagnostics/dgx-acs-toggle.sh`. **Real finding while porting this**: this motherboard has **zero** PCIe devices exposing the ACS extended capability at all (verified with a corrected, exit-code-based `setpci` probe — an earlier draft of the port mis-detected all 19 devices as "ACS enabled" by parsing `setpci -v`'s stdout, which prints the bus address even on a "capability not found" error; fixed and re-verified against real hardware before landing). So this toggle currently has nothing to act on here — it exists for completeness and for the AMD laptop or any future PCIe-switch-bearing box, not because this desktop needs it. |
| `nv-cpu-governor`, `nv-limits`'s auto-detect, `nv-iommu`'s platform-detect logic | **Skip — already covered** | This box's `powerprofilesctl set performance` (from the 7.1.10 boot audit) plus the existing generated cmdline already achieve the same or better than DGX's `cpupower frequency-set -g performance` oneshot. Porting DGX's version would be redundant, and on `intel_pstate=active` (not the legacy cpufreq governor model DGX's script assumes) could be actively confusing to layer on top. |

## Deliverables landed

- `hyphaed/phases/postinstall.py::_check_dgx_parity_basics()` — applies the
  three `ADOPT` items (peermem, PCIe bridge power, DCGM), idempotently, as
  part of the normal postinstall phase.
- `diagnostics/dgx-parity-check.sh` — report-only, no root needed for the
  read side, prints every gap above without changing anything.
- `diagnostics/dgx-acs-toggle.sh` — opt-in, dry-run by default, `--go` to
  apply, `--enable` to restore. Not persistent across reboot by design; the
  durable form is the dormant `0018-tkg-pci-acs-override.patch` boot param.
- `diagnostics/dgx-pci-relaxed-ordering-toggle.sh` — opt-in, dry-run by
  default, Samsung-NVMe-gated (matching DGX's own script's vendor gate).
- `patches/custom/0028-apparmor-userns-sfs-advertise.patch` — see
  `docs/boot-audit-7.2.0-2026-08-27.md`; unrelated to DGX directly, but found
  during the same audit session and landed alongside this work.

## Non-goals

- **No apt/PPA repo is added for auto-tracking DGX packages.** The repo
  doesn't publish a component for this Ubuntu release, and even where it
  does, DGX's real update model is a pinned-ESM fleet pattern, not "always
  newest" — wiring it in on the assumption of continuous auto-update would
  fight this box's actual update posture (rolling-release Ubuntu, latest
  kernel.org source), not extend it.
- **No install of `nvidia-system-station`, NVSM, or the DGX release/OTA
  metapackages.** Not installable for this release, and largely irrelevant
  to a single desktop workstation even where they are (fleet/BMC/RAID
  tooling).
- **ACS and Relaxed-Ordering are not defaults.** Both trade something real
  (device isolation; an open, unmeasured driver-behavior question) for a
  potential gain that hasn't been measured on this hardware yet. Per this
  repo's "real data over synthetic tests" rule, they stay manual, opt-in
  tools until someone runs the real before/after numbers.

## Verification

```bash
# adopted items:
lsmod | grep nvidia_peermem
systemctl status nvidia-pci-bridge-power.service
dcgmi discovery -l

# opt-in diagnostics (report-only, safe to run anytime):
bash diagnostics/dgx-parity-check.sh

# opt-in toggles (only with a real before/after GDS measurement either side):
sudo bash diagnostics/dgx-pci-relaxed-ordering-toggle.sh --status
bash diagnostics/dgx-acs-toggle.sh --status
```
