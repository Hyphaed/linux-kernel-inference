#!/usr/bin/env bash
# Does the pinned NVIDIA open driver COMPILE against v$VER's headers? (ADR-002: the driver gates every kernel.)
# Needs the throwaway tree from stage 30 (KEEP=1 30-validate-series.sh) and the extracted .run source in
# build/nvidia/src-<running kver>/ (copy the WHOLE tree: kernel-open/nvidia/nv-kernel.o_binary is a symlink into ../../src).
# Link-time (modpost) symbols can only be proven by the real nvidia phase after the kernel build; here we prove the
# C objects and conftest API probes against the new headers. Written 2026-11-04 after 615.71.09 broke on 7.3-rc.
. "$(dirname "$0")/common.sh"
V="$ROOT/build/validate-$VER"; [ -f "$V/include/generated/autoconf.h" ] || { echo "NOT RUN: run KEEP=1 30-validate-series.sh first"; exit 2; }
SRC=$(ls -d "$ROOT"/build/nvidia/src-* 2>/dev/null | tail -1); [ -n "$SRC" ] || { echo "NOT RUN: no build/nvidia/src-*"; exit 2; }
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
cp -r "$SRC" "$W/src"
find "$W/src/kernel-open" \( -name '*.ko' -o -name '*.o' -o -name '.*.cmd' \) -not -name '*binary*' -delete
rm -rf "$W/src/kernel-open/conftest"
# conftest reads Module.symvers for "is symbol exported"; a tree that was never fully built has none. The running
# kernel's list is a proxy: it makes the symbol probes meaningful, the API/type probes still use the NEW headers.
cp "/usr/src/linux-headers-$(uname -r)/Module.symvers" "$V/Module.symvers"
step "compile NVIDIA $(basename "$SRC") open modules against v$VER headers"
set +e; make -C "$W/src/kernel-open" -j"$JOBS" SYSSRC="$V" SYSOUT="$V" modules >"$W/log" 2>&1; set -e
rm -f "$V/Module.symvers"
errs=$(grep -c 'error:' "$W/log" || true)
if [ "$errs" -gt 0 ]; then grep 'error:' "$W/log" | sed -E 's/^[^ ]+ ?//' | sort | uniq -c | sort -rn | head -15; echo "FAIL: $errs compile errors"; exit 1; fi
# modpost 'undefined' against the proxy symvers is expected (7.3 exports symbols 7.2 lacks); anything else is real
# (|| true: grep exits 1 when there are none, and with pipefail that killed this script on SUCCESS, found 2026-11-04)
{ grep -E "modpost: .*undefined" "$W/log" || true; } | sed 's/.*symbol/  link-time (proxy symvers):/' | sort -u | head
echo "NVIDIA C objects compile on v$VER (link proven only by the nvidia phase)"
