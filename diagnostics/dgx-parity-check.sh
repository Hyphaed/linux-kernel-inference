#!/usr/bin/env bash
# Report gaps between this box and the applicable subset of NVIDIA DGX OS's
# tuning, per docs/dgx-os-parity-2026-08-27.md. Report-only, changes nothing.
#
#   bash diagnostics/dgx-parity-check.sh
#
# WHAT THIS CHECKS AND WHY
#
# DGX OS's own baseos apt repo (repo.download.nvidia.com/baseos/ubuntu/) only
# publishes for Ubuntu 24.04 (noble) -- confirmed 404 on this box's 26.04
# (resolute), 2026-08-27. So "apt install nvidia-system-core" is not
# available here; what follows is the hand-ported subset judged applicable
# after reading the real DGX packages (pulled and dpkg-deb -c'd from the
# noble repo this session), see the parity doc for the full decision table
# and the items deliberately REJECTED (mitigations=off, transparent_hugepage
# =madvise, NVSM, the DGX metapackages themselves).
set -uo pipefail

hdr()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; }

hdr "GPUDirect RDMA peer-memory module"
if lsmod | grep -q '^nvidia_peermem'; then
  ok "nvidia_peermem loaded"
elif [ -e "/lib/modules/$(uname -r)/updates/dkms/nvidia-peermem.ko" ] || \
     modinfo nvidia-peermem >/dev/null 2>&1; then
  warn "nvidia-peermem is built (DKMS) but not loaded"
  echo "      DGX loads this via /etc/modules-load.d/nvidia-peermem.conf."
  echo "      Fix: sudo modprobe nvidia-peermem"
  echo "      (only matters if GreenBoost's cluster/feeder ever does GPUDirect RDMA)"
else
  warn "nvidia-peermem module not found for the running kernel — DKMS may not have built it"
fi

hdr "PCIe bridge power control (DGX forces 'on' for latency, same goal as our ASPM=performance)"
NEED_FIX=0
for BRIDGE in $(lspci -D -d ::0604 2>/dev/null | awk '{print $1}'); do
  CTRL_FILE="/sys/bus/pci/devices/${BRIDGE}/power/control"
  if [ -r "$CTRL_FILE" ]; then
    VAL=$(cat "$CTRL_FILE" 2>/dev/null)
    if [ "$VAL" != "on" ]; then
      warn "$BRIDGE power/control=$VAL (DGX forces 'on' to prevent bridge power-down latency)"
      NEED_FIX=1
    fi
  fi
done
[ "$NEED_FIX" -eq 0 ] && ok "all PCIe bridges already power/control=on"

hdr "DCGM (Data Center GPU Manager) — generic GPU health/Xid monitoring, not DGX-hardware-locked"
if command -v dcgmi >/dev/null 2>&1; then
  ok "dcgmi present: $(dcgmi --version 2>/dev/null | head -1)"
  systemctl is-active --quiet nvidia-dcgm 2>/dev/null && ok "nvidia-dcgm service active" \
    || warn "nvidia-dcgm service not active — sudo systemctl enable --now nvidia-dcgm"
else
  CAND=$(apt-cache policy datacenter-gpu-manager-4-cuda13 2>/dev/null | awk '/Candidate:/{print $2}')
  if [ -n "$CAND" ] && [ "$CAND" != "(none)" ]; then
    warn "not installed. Available from the already-configured CUDA repo: $CAND"
    echo "      sudo apt install datacenter-gpu-manager-4-cuda13 && sudo systemctl enable --now nvidia-dcgm"
  else
    bad "not installed and no candidate found — check the CUDA repo is configured"
  fi
fi

hdr "PCIe ACS state (opt-in territory — see dgx-acs-toggle.sh, NOT applied here)"
# Probe WITHOUT -v, gate on exit code. `setpci -v` prints the BDF on stdout
# even when the extended capability is absent (the "not found" message goes
# to stderr), so parsing -v's stdout misreports every device as ACS-capable.
# Found by testing this script against real hardware, 2026-08-27.
ACS_CAP=0
ACS_ON=0
for BDF in $(lspci -d "*:*:*" 2>/dev/null | awk '{print $1}'); do
  RESULT=$(setpci -s "$BDF" ECAP_ACS+0x6.w 2>/dev/null)
  [ $? -ne 0 ] && continue
  ACS_CAP=$((ACS_CAP+1))
  [ "$RESULT" != "0000" ] && ACS_ON=$((ACS_ON+1))
done
if [ "$ACS_CAP" -eq 0 ]; then
  echo "  no ACS-capable devices on this bus — nothing for dgx-acs-toggle.sh to act on"
  echo "  (real result on this motherboard: no PCIe switch/enterprise NIC exposing"
  echo "   the ACS extended capability at all, verified live, not a rare edge case)"
else
  echo "  $ACS_ON of $ACS_CAP ACS-capable device(s) have ACS bits set (default, isolation-preserving)."
  echo "  0018-tkg-pci-acs-override.patch is dormant in the tree for a boot-param"
  echo "  version of the same tradeoff; dgx-acs-toggle.sh is the no-reboot runtime"
  echo "  equivalent. Neither is applied by this check."
fi

hdr "PCIe Relaxed Ordering on data NVMe (opt-in territory — see dgx-pci-relaxed-ordering-toggle.sh)"
for NVME in /sys/class/nvme/nvme*; do
  [ -d "$NVME" ] || continue
  NAME=$(basename "$NVME")
  MODEL=$(cat "$NVME/model" 2>/dev/null | xargs)
  VID=$(cat "$NVME/device/vendor" 2>/dev/null)
  if [ "$VID" = "0x144d" ]; then
    echo "  $NAME ($MODEL, Samsung) — RO is a real toggle here (nvme set-feature -f 198)."
    echo "    This exact question is open in CLAUDE.md: does forcing RO on help or hurt"
    echo "    GPU-initiated P2P GDS on this driver? Use dgx-pci-relaxed-ordering-toggle.sh"
    echo "    with a real before/after GDS throughput measurement, not a guess."
  else
    echo "  $NAME ($MODEL) — vendor $VID, RO toggle via nvme set-feature is Samsung-specific"
    echo "    on DGX's own tooling; not offered for this drive."
  fi
done

hdr "Already adopted / already exceeds DGX defaults (no action)"
echo "  iommu=pt, nvidia-drm.modeset=1, numa_balancing=disable — already on this cmdline"
echo "  nofile limit $(ulimit -Hn) >= DGX's 500000 default"
dpkg -l nvidia-container-toolkit >/dev/null 2>&1 && ok "nvidia-container-toolkit already installed"

hdr "Deliberately NOT ported (see docs/dgx-os-parity-2026-08-27.md for why)"
echo "  - mitigations=off               : violates this repo's hard constraint, never port"
echo "  - transparent_hugepage=madvise  : opposite of this box's single-large-process tuning"
echo "  - NVSM / DGX metapackages       : DGX's repo has no component for this Ubuntu release"
