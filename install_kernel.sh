#!/usr/bin/env bash
# Full hyphaed kernel build + install + test driver.
# Wraps: install-deps → fetch-patches → tests → print-config → build/install → verify.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

PRESET="gaming-ai-vm"
DRY_RUN=0
SKIP_TESTS=0
YES=0

usage() {
  cat <<EOF
Usage: $(basename "$0") [OPTIONS]

One-shot driver for the hyphaed kernel wizard. Runs the full pipeline:
  install-deps → fetch-patches → pytest → print-config → build + install → verify

Options:
  --preset NAME     Config preset to use (default: gaming-ai-vm)
  --dry-run         No mutations — passes --dry-run through to the wizard
  --skip-tests      Skip the pytest gate before the build
  -y, --yes         Skip the top-level confirmation prompt
  -h, --help        Show this help and exit
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --preset) PRESET="${2:?'--preset requires a NAME argument'}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --skip-tests) SKIP_TESTS=1; shift ;;
    -y|--yes) YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
done

if [[ ${EUID} -eq 0 ]]; then
  echo "ERROR: Do not run as root — the wizard manages sudo internally." >&2
  exit 1
fi

if [[ ! -f "configs/presets/${PRESET}.yaml" ]]; then
  AVAILABLE="$(ls configs/presets/*.yaml 2>/dev/null | xargs -n1 basename | sed 's/\.yaml//' | tr '\n' ' ')"
  echo "ERROR: Unknown preset '${PRESET}'. Available: ${AVAILABLE}" >&2
  exit 1
fi

mkdir -p out/logs
LOG_FILE="out/logs/install_kernel-$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "${LOG_FILE}") 2>&1

BOLD=$'\e[1m'; RESET=$'\e[0m'
HR="========================================================"

echo "${HR}"
echo "${BOLD}  hyphaed kernel wizard — install_kernel.sh${RESET}"
echo "  preset    : ${PRESET}"
echo "  dry-run   : $([[ ${DRY_RUN} -eq 1 ]] && echo 'yes (nothing will be written)' || echo no)"
echo "  skip-tests: $([[ ${SKIP_TESTS} -eq 1 ]] && echo yes || echo no)"
echo "  log       : ${LOG_FILE}"
echo "${HR}"

if [[ ${YES} -eq 0 ]]; then
  read -r -p "Proceed? [y/N] " REPLY
  echo
  if [[ ! ${REPLY} =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 0
  fi
fi

WIZARD_FLAGS=(--non-interactive -y --preset "${PRESET}")
[[ ${DRY_RUN} -eq 1 ]] && WIZARD_FLAGS+=(--dry-run)

step() { echo; echo "${BOLD}>>> STEP ${*}${RESET}"; }

trap 'rc=$?; echo; echo "ERROR: step failed (exit ${rc}) — full log at ${LOG_FILE}" >&2' ERR

if [[ ${DRY_RUN} -eq 0 ]]; then
  step "1/6  install-deps"
  make install-deps
else
  step "1/6  install-deps (skipped — dry-run)"
fi

step "2/6  fetch-patches"
make fetch-patches

if [[ ${SKIP_TESTS} -eq 0 ]]; then
  step "3/6  pytest invariants"
  make test
else
  step "3/6  pytest invariants (skipped via --skip-tests)"
fi

step "4/6  print-config preview"
python3 -m hyphaed "${WIZARD_FLAGS[@]}" print-config

step "5/6  build + install"
python3 -m hyphaed "${WIZARD_FLAGS[@]}"

if [[ ${DRY_RUN} -eq 0 ]]; then
  step "6/6  verify"
  make verify
else
  step "6/6  verify (skipped — dry-run)"
fi

echo
echo "${HR}"
echo "${BOLD}  Done!${RESET}  Log: ${LOG_FILE}"
if [[ ${DRY_RUN} -eq 0 ]]; then
  echo
  echo "  Reboot and pick 'hyphaed' from the GRUB menu, then run:"
  echo "    make status && make verify && make doctor"
fi
echo "${HR}"
