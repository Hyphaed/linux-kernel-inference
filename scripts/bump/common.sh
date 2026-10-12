# shellcheck shell=bash
# Sourced by every scripts/bump/NN-*.sh. Needs VER (e.g. VER=7.2.9).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
: "${VER:?set VER, e.g. VER=7.2.9}"
SERIES_MM="${VER%.*}"                         # 7.2
SERIES_DIR="patches/kernel-org-$SERIES_MM"
LOCK="patches/VENDOR-kernel-org-$SERIES_MM.lock"
KO="$ROOT/github/kernelorg/linux"            # mainline + `stable` remote
OUT="$ROOT/out/sweep/$VER"
PY="${PY:-python3}"
# newest build/linux-X.Y.Z strictly below VER, unless PREV is given
PREV="${PREV:-$(ls build 2>/dev/null | sed -n 's/^linux-\(7\.[0-9.]*\)$/\1/p' \
    | { cat; echo "$VER"; } | sort -uV | sed -n "/^$VER\$/q;p" | tail -1)}"
step() { printf '\n==> %s\n' "$*"; }
