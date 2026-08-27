#!/usr/bin/env bash
# Runtime PCIe ACS (Access Control Services) toggle, ported from DGX OS's
# nvidia-acs-disable behavior (setpci-based, no reboot needed) -- adapted to
# this repo's dry-run-by-default convention rather than a systemd unit that
# runs on every boot unconditionally.
#
#   bash diagnostics/dgx-acs-toggle.sh --status              # report only, no root
#   sudo bash diagnostics/dgx-acs-toggle.sh --disable --go   # actually disable ACS
#   sudo bash diagnostics/dgx-acs-toggle.sh --enable  --go   # restore ACS (isolation back on)
#
# WHAT THIS IS FOR
#
# ACS forces PCIe transactions between devices behind the same root port to
# be redirected up to the root complex for IOMMU-mediated isolation checks,
# instead of going device-to-device directly. That isolation is exactly what
# VFIO/GPU-passthrough and NVMe<->GPU peer-to-peer DMA (GDS) both want out of
# the way. Disabling it is the same tradeoff the dormant
# patches/custom/0018-tkg-pci-acs-override.patch boot-param already documents
# in this tree; this script is the no-reboot runtime equivalent DGX itself
# ships, useful for measuring the effect before committing to a boot param.
#
# THIS REDUCES DEVICE ISOLATION. It is not applied by dgx-parity-check.sh and
# must not be enabled by default. Per this repo's own rule (real data over
# synthetic tests): measure real GDS/P2P throughput before and after with
# diagnostics/vram-residency-baseline.py or gdscheck, don't toggle this on a
# hunch and call it a win.
set -uo pipefail

MODE=""; GO=0
while [ $# -gt 0 ]; do
  case "$1" in
    --status)  MODE="status"; shift ;;
    --disable) MODE="disable"; shift ;;
    --enable)  MODE="enable"; shift ;;
    --go)      GO=1; shift ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -z "$MODE" ] && MODE="status"

hdr()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }

if [ "$MODE" != "status" ] && [ "$EUID" -ne 0 ]; then
  echo "ERROR: --disable/--enable need root (setpci writes extended config space)" >&2
  exit 1
fi

hdr "PCIe ACS state, by device"
# IMPORTANT: probe WITHOUT -v and gate on exit code, not on parsing stdout.
# `setpci -v` prints the BDF on stdout even when the capability is absent
# ("Instance #0 of Extended capability 000d not found" goes to stderr, but
# -v's own header line still lands on stdout) -- an earlier version of this
# script parsed that header as if it were the register value and reported
# every device as "ACS enabled", which was wrong. Found and fixed 2026-08-27
# by testing against real hardware, not assuming the DGX source translated
# cleanly.
EXITCODE=0
CAP_COUNT=0
for BDF in $(lspci -d "*:*:*" 2>/dev/null | awk '{print $1}'); do
  CUR=$(setpci -s "$BDF" ECAP_ACS+0x6.w 2>/dev/null)
  [ $? -ne 0 ] && continue   # device has no ACS capability, skip
  CAP_COUNT=$((CAP_COUNT+1))

  NAME=$(lspci -s "$BDF" | cut -d' ' -f2-)
  case "$MODE" in
    status)
      printf '  %-12s %s  (%s)\n' "$BDF" "$CUR" "$NAME"
      ;;
    disable)
      if [ "$CUR" = "0000" ]; then
        printf '  %-12s already disabled  (%s)\n' "$BDF" "$NAME"
        continue
      fi
      if (( GO )); then
        logger "dgx-acs-toggle: disabling ACS on $BDF ($NAME)"
        setpci -s "$BDF" ECAP_ACS+0x6.w=0000 >/dev/null 2>&1 || { warn "$BDF: setpci write failed"; EXITCODE=2; continue; }
        NEW=$(setpci -s "$BDF" ECAP_ACS+0x6.w 2>/dev/null)
        [ "$NEW" = "0000" ] && ok "$BDF disabled  ($NAME)" || { warn "$BDF: wrote but still reads $NEW"; EXITCODE=3; }
      else
        printf '  would disable ACS on %-12s (%s), currently %s\n' "$BDF" "$NAME" "$CUR"
      fi
      ;;
    enable)
      if (( GO )); then
        logger "dgx-acs-toggle: restoring ACS default on $BDF ($NAME)"
        setpci -s "$BDF" ECAP_ACS+0x6.w=0007 >/dev/null 2>&1
        ok "$BDF restored to default ACS bits  ($NAME)"
      else
        printf '  would restore ACS defaults on %-12s (%s)\n' "$BDF" "$NAME"
      fi
      ;;
  esac
done

if [ "$CAP_COUNT" -eq 0 ]; then
  ok "no ACS-capable device found on this bus — this toggle has nothing to act on here"
  echo "  (real result on this box's motherboard, verified 2026-08-27, not a script bug:"
  echo "   plain, non-verbose setpci probes with no PCIe switch/enterprise NIC present"
  echo "   commonly find zero devices exposing the ACS extended capability at all)"
fi
if [ "$MODE" != "status" ] && [ "$GO" -eq 0 ] && [ "$CAP_COUNT" -gt 0 ]; then
  warn "dry run — nothing changed. Re-run with --go to apply."
fi
if [ "$MODE" = "disable" ] && (( GO )); then
  warn "not persistent — this reverts on reboot. If you keep it, a boot-param patch"
  warn "(0018-tkg-pci-acs-override.patch, dormant in this tree) is the durable form."
fi
exit "$EXITCODE"
