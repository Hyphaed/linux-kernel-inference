#!/usr/bin/env bash
# Fix the two things that are wrong with the 7.1.10-hyphaed boot that can be
# fixed without rebuilding the kernel.
#
# What is wrong:
#
#   1. /lib/modules/7.1.10-hyphaed/updates/dkms/nvidia-fs.ko is 0 bytes. The
#      DKMS build was killed part-way on 2026-08-25 at 22:35, the same minute
#      the previous 7.1.10 boot ended. DKMS still reports "installed".
#
#      modprobe nvidia_fs then fails with "Invalid argument",
#      systemd-modules-load.service fails with it, and that one failure is the
#      whole reason `systemctl is-system-running` says degraded.
#
#      Downstream: /dev/nvidia-fs* does not exist, so cuFile runs in POSIX
#      compat mode and every NVMe->GPU byte crosses PCIe twice instead of once.
#
# What this does NOT fix: the cache_ext kfunc registration failure
# (patch 0023). That is compiled into vmlinux and needs a kernel rebuild.
#
#   sudo bash diagnostics/fix-boot-7.1.10.sh          # show what it would do
#   sudo bash diagnostics/fix-boot-7.1.10.sh --go     # do it
set -uo pipefail

GO=0
[[ "${1:-}" == "--go" ]] && GO=1

KVER="$(uname -r)"
NVFS_VER="$(dkms status nvidia-fs 2>/dev/null | head -1 | sed -E 's|^nvidia-fs/([^,]+).*|\1|')"
KO="/lib/modules/$KVER/updates/dkms/nvidia-fs.ko"

hdr() { printf '\n\033[1m%s\033[0m\n' "$*"; }
run() {
  if (( GO )); then
    printf '  + %s\n' "$*"
    "$@"
  else
    printf '  would run: %s\n' "$*"
  fi
}

[ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }

hdr "Where we are"
echo "  running kernel : $KVER"
echo "  nvidia-fs      : ${NVFS_VER:-NOT REGISTERED WITH DKMS}"
if [ -e "$KO" ]; then
  echo "  built module   : $(stat -c '%s bytes, %y' "$KO")"
else
  echo "  built module   : missing"
fi
echo "  system state   : $(systemctl is-system-running 2>&1)"

if [ -z "$NVFS_VER" ]; then
  echo
  echo "nvidia-fs is not registered with DKMS at all. Nothing here can fix" >&2
  echo "that -- reinstall the nvidia-gds / nvidia-fs-dkms package first." >&2
  exit 1
fi

# ---------------------------------------------------------------- 1. Makefile
# Upstream nvidia-fs bug: Makefile line 111 runs ./create_nv.symvers.sh with no
# argument, and that script does KVER=${1:-$(uname -r)}. Build for a kernel you
# are not booted on and the module carries the wrong nvidia_p2p_* CRCs. Every
# other line in that Makefile passes $(KVER) properly.
hdr "1. nvidia-fs Makefile: pass the target kernel to create_nv.symvers.sh"
SRC="/usr/src/nvidia-fs-$NVFS_VER"
if [ ! -d "$SRC" ]; then
  echo "  $SRC missing -- skipping, cannot patch what is not there"
elif grep -qE '^[[:space:]]*@ \./create_nv\.symvers\.sh[[:space:]]*$' "$SRC/Makefile"; then
  echo "  line 111 still drops \$(KVER) -- patching (backup: Makefile.orig)"
  run cp -n "$SRC/Makefile" "$SRC/Makefile.orig"
  if (( GO )); then
    sed -i -E 's|^([[:space:]]*@ \./create_nv\.symvers\.sh)[[:space:]]*$|\1 $(KVER)|' "$SRC/Makefile"
  else
    echo "  would run: sed -i ... '\$(KVER)' $SRC/Makefile"
  fi
else
  echo "  already patched:$(grep -n 'create_nv.symvers.sh' "$SRC/Makefile" | tail -1 | sed 's/^/ /')"
fi

# ------------------------------------------------------------ 2. rebuild + load
hdr "2. Rebuild nvidia-fs/$NVFS_VER for $KVER and load it"
if lsmod | grep -q '^nvidia_fs'; then
  echo "  nvidia_fs is already loaded -- nothing to rebuild."
else
  run dkms remove  "nvidia-fs/$NVFS_VER" -k "$KVER"
  run dkms install "nvidia-fs/$NVFS_VER" -k "$KVER"
  run modprobe nvidia-fs
fi

# ------------------------------------------------------------------ 3. verify
hdr "3. Did it work"
if (( ! GO )); then
  echo "  dry run -- nothing changed. Re-run with --go."
  exit 0
fi

fail=0

if [ -s "$KO" ]; then
  echo "  module file    : $(stat -c '%s bytes' "$KO")"
else
  echo "  module file    : STILL EMPTY OR MISSING"; fail=1
fi

LOG="/var/lib/dkms/nvidia-fs/$NVFS_VER/$KVER/x86_64/log/make.log"
if [ -s "$LOG" ]; then
  echo "  symvers used   : $(grep -i 'Module.symvers' "$LOG" | head -1 | sed 's/^ *//')"
else
  echo "  build log      : empty -- the build produced no output, which is how"
  echo "                   the 0-byte module happened in the first place"; fail=1
fi

if lsmod | grep -q '^nvidia_fs'; then
  echo "  loaded         : yes"
else
  echo "  loaded         : NO"; fail=1
fi

if compgen -G "/dev/nvidia-fs*" >/dev/null; then
  echo "  device nodes   : $(ls -d /dev/nvidia-fs* | tr '\n' ' ')"
  echo "  GDS            : real peer-to-peer DMA, out of POSIX compat mode"
else
  echo "  device nodes   : NONE -- GDS is still in POSIX compat mode"; fail=1
fi

systemctl restart systemd-modules-load.service 2>/dev/null || true
echo "  modules-load   : $(systemctl is-active systemd-modules-load.service)"
echo "  system state   : $(systemctl is-system-running 2>&1)"

echo
if (( fail )); then
  echo "Not everything came back. Next thing to look at:"
  echo "  tail -40 $LOG"
  echo "  dmesg | tail -20"
  exit 1
fi
echo "Done. nvidia-fs is loaded and GDS is doing real peer-to-peer DMA again."
echo
echo "Still outstanding, and it needs a kernel rebuild: patch 0023's cache_ext"
echo "kfuncs do not register. See docs/patch-verification-7.1.10.md."
