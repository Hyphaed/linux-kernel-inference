#!/usr/bin/env bash
# Runtime PCIe Relaxed Ordering toggle for the data NVMe, ported from DGX
# OS's nvidia-relaxed-ordering-nvme.sh (nvme-cli set-feature, Samsung-only —
# DGX's own script gates on vendor 0x144d, and this box's data drive is a
# Samsung 990 EVO Plus, so the gate is real here, not decorative).
#
#   sudo bash diagnostics/dgx-pci-relaxed-ordering-toggle.sh --status
#   sudo bash diagnostics/dgx-pci-relaxed-ordering-toggle.sh --enable  --go
#   sudo bash diagnostics/dgx-pci-relaxed-ordering-toggle.sh --disable --go
#
# All three need root: /dev/nvme* is root-only, so even --status must open
# the device to read the feature.
#
# WHY THIS EXISTS
#
# This exact question is open and unresolved in this repo's CLAUDE.md:
# "NVIDIA driver >=525 itself disables relaxed-ordering for GPU-initiated P2P
# transactions on some host bridges (a correctness erratum) -- forcing RO on
# could be actively counterproductive." That's a stated guess, not a
# measurement. DGX ships a real, vendor-blessed toggle for exactly this knob;
# porting it lets the question finally get answered with a real before/after
# GDS throughput number instead of staying an open question forever.
#
# NOT applied by dgx-parity-check.sh, and this script does not decide the
# question for you -- it only lets you test both states. Per this repo's own
# rule (real data over synthetic tests): run diagnostics/vram-residency-
# baseline.py or gdscheck -p before and after, on the real served model, not
# a synthetic read/write loop.
set -uo pipefail

MODE=""; GO=0
while [ $# -gt 0 ]; do
  case "$1" in
    --status)  MODE="status"; shift ;;
    --enable)  MODE="enable"; shift ;;
    --disable) MODE="disable"; shift ;;
    --go)      GO=1; shift ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -z "$MODE" ] && MODE="status"

hdr()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; }

if ! command -v nvme >/dev/null 2>&1; then
  bad "nvme-cli not installed — sudo apt install nvme-cli"
  exit 1
fi

# /dev/nvme* is root-only (crw------- root root), so --status needs root too,
# not just --enable/--disable. Found by testing this script for real: without
# this check, `nvme get-feature` fails with "Permission denied" and dumps its
# own --help text, which reads exactly like a broken feature-id probe rather
# than the permission issue it actually is.
if [ "$EUID" -ne 0 ]; then
  bad "this needs root to open /dev/nvme* — re-run with sudo (even for --status)"
  exit 1
fi

VID_SAMSUNG="0x144d"
FEATURE_ID=198   # vendor-specific feature id DGX uses for RO on Samsung NVMe

FOUND=0
for NVME in /sys/class/nvme/nvme*; do
  [ -d "$NVME" ] || continue
  NAME=$(basename "$NVME")
  VID=$(cat "$NVME/device/vendor" 2>/dev/null)
  MODEL=$(cat "$NVME/model" 2>/dev/null | xargs)

  if [ "$VID" != "$VID_SAMSUNG" ]; then
    warn "$NAME ($MODEL, vendor $VID) — this toggle is Samsung-specific (DGX's own script"
    warn "  only supports vendor 0x144d); skipping, not a supported combination"
    continue
  fi
  FOUND=1

  case "$MODE" in
    status)
      CUR=$(nvme get-feature "/dev/${NAME}" -f "$FEATURE_ID" -H 2>/dev/null | tail -1)
      echo "  $NAME ($MODEL): ${CUR:-could not read feature $FEATURE_ID}"
      ;;
    enable|disable)
      VAL=1; [ "$MODE" = "disable" ] && VAL=0
      if (( GO )); then
        logger "dgx-pci-relaxed-ordering-toggle: setting RO=$VAL on $NAME"
        nvme set-feature "/dev/${NAME}" -f "$FEATURE_ID" -v "$VAL" -s \
          && ok "$NAME: RO $MODE'd" \
          || bad "$NAME: nvme set-feature failed"
      else
        echo "  would set-feature $FEATURE_ID=$VAL on /dev/${NAME} ($MODE)"
      fi
      ;;
  esac
done

if [ "$FOUND" -eq 0 ]; then
  bad "no Samsung NVMe found — this toggle has nothing to act on here"
  exit 2
fi
if [ "$MODE" != "status" ] && [ "$GO" -eq 0 ]; then
  warn "dry run — nothing changed. Re-run with --go to apply."
fi
