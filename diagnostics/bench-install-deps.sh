#!/usr/bin/env bash
# Installs the two packages the platform-kernel-levers benchmark harness needs
# that this box does not already have: fio (NVMe A/B) and stress-ng (CPU/mem
# background load for the PCIe-under-contention runs). Nothing else in
# diagnostics/bench-*.sh depends on anything not already installed — perf,
# bpftrace, numactl, hdparm and nvme-cli are all present (checked 2026-08-31).
#
#   bash diagnostics/bench-install-deps.sh          # report only
#   sudo bash diagnostics/bench-install-deps.sh --go
set -uo pipefail

GO=0
[[ "${1:-}" == "--go" ]] && GO=1

hdr()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }

hdr "Current state"
for pkg in fio stress-ng; do
  if command -v "$pkg" >/dev/null 2>&1; then
    ok "$pkg already installed: $(command -v "$pkg")"
  else
    warn "$pkg missing"
  fi
done

MISSING=()
command -v fio >/dev/null 2>&1 || MISSING+=(fio)
command -v stress-ng >/dev/null 2>&1 || MISSING+=(stress-ng)

if [[ ${#MISSING[@]} -eq 0 ]]; then
  ok "nothing to install"
  exit 0
fi

hdr "Would install"
echo "  ${MISSING[*]}"

if (( ! GO )); then
  echo
  echo "  Re-run with --go (as root) to actually install."
  exit 0
fi

if [[ $EUID -ne 0 ]]; then
  echo "  --go needs root" >&2
  exit 1
fi

hdr "Installing"
apt-get update -qq
apt-get install -y "${MISSING[@]}"

hdr "Verify"
fail=0
for pkg in "${MISSING[@]}"; do
  if command -v "$pkg" >/dev/null 2>&1; then
    ok "$pkg: $("$pkg" --version 2>&1 | head -1)"
  else
    echo "  ✗ $pkg still missing after install" >&2
    fail=1
  fi
done
exit $fail
