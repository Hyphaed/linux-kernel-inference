#!/usr/bin/env bash
# PCIe path check for the platform-kernel-levers sweep (Phase 2/3). Wraps
# the two existing read-only probes (gpu-link-speed-under-load.sh,
# pcie-mps-mrrs-check.sh) and adds the two checks neither of them covers:
# IOMMU passthrough mode and ASPM policy. All read-only; nothing here writes
# a setting.
#
#   sudo bash diagnostics/bench-pcie.sh
#   sudo bash diagnostics/bench-pcie.sh --skip-load    # skip the CUDA matmul link-speed probe (fast)
set -uo pipefail

SKIP_LOAD=0
[[ "${1:-}" == "--skip-load" ]] && SKIP_LOAD=1

hdr()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
  echo "Needs root: extended PCI config space, lspci -vv. Re-run: sudo $0" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="out/bench"; mkdir -p "$OUT_DIR"
TS="$(date +%Y%m%d-%H%M%S)"
LOG="$OUT_DIR/pcie-${TS}.log"

{
hdr "1/4 — link speed / width, MPS / MRRS, ExtTag (pcie-mps-mrrs-check.sh)"
bash "$SCRIPT_DIR/pcie-mps-mrrs-check.sh" 01:00.0

hdr "2/4 — IOMMU passthrough"
if grep -q 'iommu=pt' /proc/cmdline; then
  ok "iommu=pt on cmdline"
else
  warn "iommu=pt NOT on cmdline — GPU DMA maps go through the full IOMMU translation path instead of 1:1 passthrough"
fi
IOMMU_GROUPS="$(find /sys/kernel/iommu_groups -maxdepth 2 -name '01:00.0' 2>/dev/null)"
if [[ -n "$IOMMU_GROUPS" ]]; then
  ok "GPU is in an IOMMU group: $IOMMU_GROUPS"
else
  warn "GPU not found in any IOMMU group — IOMMU may not be active at all"
fi
dmesg 2>/dev/null | grep -iE "iommu.*(enabled|passthrough|dmar)" | tail -10 | sed 's/^/  /'

hdr "3/4 — ASPM policy"
echo "  policy   : $(cat /sys/module/pcie_aspm/parameters/policy 2>/dev/null)"
grep -o 'pcie_aspm=[^ ]*' /proc/cmdline || echo "  cmdline  : (no pcie_aspm= override)"
for bdf in 00:01.0 01:00.0; do
  L1="$(lspci -vv -s "$bdf" 2>&1 | grep -A3 "LnkCtl:" | grep -oE 'ASPM (L0s\+? ?L1\+?|Disabled)' | head -1)"
  echo "  $bdf ASPM : ${L1:-unknown}"
done

hdr "4/4 — link speed under real load"
if (( SKIP_LOAD )); then
  echo "  skipped (--skip-load)"
else
  bash "$SCRIPT_DIR/gpu-link-speed-under-load.sh" 01:00.0 15
fi

hdr "GPU / topology summary"
nvidia-smi --query-gpu=name,pcie.link.gen.current,pcie.link.gen.max,pcie.link.gen.gpumax,pcie.link.gen.hostmax,pcie.link.width.current,memory.used,memory.total --format=csv 2>/dev/null || warn "nvidia-smi not available"

} | tee "$LOG"

hdr "Done"
echo "  full log: $LOG"
