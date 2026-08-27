#!/usr/bin/env bash
# Fix GDS silently breaking whenever nvidia-fs is built for a kernel that is
# not the one currently running.
#
# What happened: nvidia_fs refuses to load with
#
#   nvidia_fs: disagrees about version of symbol nvidia_p2p_dma_map_pages
#   nvidia_fs: Unknown symbol nvidia_p2p_dma_map_pages (err -22)
#
# Why: /usr/src/nvidia-fs-<ver>/Makefile line 111 runs
#
#   @ ./create_nv.symvers.sh
#
# with no argument, and create_nv.symvers.sh line 31 does
# `KVER=${1:-$(uname -r)}`. So it resolves nvidia's Module.symvers for the
# RUNNING kernel instead of the target one. Every other line in that Makefile
# passes $(KVER) properly -- line 106 does `./configure $(KVER)`, line 120
# does `$(DEPMOD) -a $(KVER)`. Line 111 is the one that forgot.
#
# Build nvidia-fs for 7.1.10 while booted on 7.1.9 and you get a module
# carrying 7.1.9's nvidia_p2p_* CRCs. It installs, depmod is happy, dkms says
# "installed", and it cannot load. GDS then falls back to POSIX compat mode,
# where every NVMe<->GPU byte crosses PCIe twice instead of once.
#
# It hides because rebuilding while booted on the target kernel works fine,
# which is exactly what anyone does when they investigate.
#
# This is an upstream NVIDIA bug (seen in nvidia-fs 2.29.4). The fix is one
# word, and it survives until the next nvidia-fs package upgrade.
#
#   sudo bash diagnostics/fix-nvidia-fs-symvers.sh
set -euo pipefail

KVER="$(uname -r)"
SRC=$(ls -d /usr/src/nvidia-fs-* 2>/dev/null | head -1)

[ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }
[ -n "$SRC" ]        || { echo "no /usr/src/nvidia-fs-* found" >&2; exit 1; }

VER="${SRC##*nvidia-fs-}"
echo "nvidia-fs $VER, target kernel $KVER"

if lsmod | grep -q '^nvidia_fs'; then
    echo "nvidia_fs is already loaded - nothing to do."
    exit 0
fi

# 1. Patch the Makefile to pass the target kernel through.
if grep -qE '^[[:space:]]*@ \./create_nv\.symvers\.sh[[:space:]]*$' "$SRC/Makefile"; then
    cp -n "$SRC/Makefile" "$SRC/Makefile.orig" || true
    sed -i -E 's|^([[:space:]]*@ \./create_nv\.symvers\.sh)[[:space:]]*$|\1 $(KVER)|' "$SRC/Makefile"
    echo "patched $SRC/Makefile (backup at Makefile.orig)"
else
    echo "Makefile already passes \$(KVER), or the line moved - check by hand:"
    grep -n "create_nv.symvers.sh" "$SRC/Makefile" || true
fi

# 2. Rebuild for the running kernel so the CRCs come from ITS nvidia.
echo "rebuilding nvidia-fs/$VER for $KVER ..."
dkms remove  "nvidia-fs/$VER" -k "$KVER" >/dev/null 2>&1 || true
dkms install "nvidia-fs/$VER" -k "$KVER"

# 3. Confirm which symvers it actually used this time.
LOG="/var/lib/dkms/nvidia-fs/$VER/$KVER/x86_64/log/make.log"
if [ -f "$LOG" ]; then
    echo "--- symvers chosen this build ---"
    grep -i "Module.symvers" "$LOG" | head -3
fi

# 4. Load it and say plainly whether GDS is out of compat mode.
modprobe nvidia-fs
if lsmod | grep -q '^nvidia_fs'; then
    echo
    echo "nvidia_fs loaded. GDS is doing real peer-to-peer DMA again."
    ls -la /dev/nvidia-fs* 2>/dev/null | head -3 || true
else
    echo "still not loading - check: dmesg | tail -20" >&2
    exit 1
fi
