#!/usr/bin/env bash
# Switch this machine from Ubuntu's NVIDIA packaging to NVIDIA's own repo, at
# a version you choose, without letting apt pick a different one.
#
#   bash diagnostics/nvidia-driver-switch.sh                      # report only, no root needed
#   sudo bash diagnostics/nvidia-driver-switch.sh --version 595.91.07 --go
#   sudo bash diagnostics/nvidia-driver-switch.sh --branch 595 --go
#
# WHAT THIS IS FOR, AND WHAT IT IS NOT
#
# You do NOT need this to stay safe. Verified 2026-08-27: `apt upgrade` and
# `apt full-upgrade` touch nothing NVIDIA on this box. Ubuntu ships
# nvidia-driver-595-open / libnvidia-gl-595; NVIDIA's repo ships nvidia-open /
# libnvidia-gl. Different package names, so the repo's Pin-Priority 600 never
# applies to what is installed. The 610 exposure begins only once you switch.
#
# THE TRAP THIS EXISTS TO AVOID
#
#   apt install nvidia-open=595.91.07-1ubuntu1
#
# does not install 595.91.07. That package declares
# `Depends: nvidia-driver-open (>= 595.91.07)` -- a >=, not an = -- so apt
# satisfies it with the newest thing in the repo, currently 610.57.04, and
# pulls nvidia-driver-pinning-610 along for company.
#
# NVIDIA's answer is nvidia-driver-pinning-<version>, which ships one file:
# /etc/apt/preferences.d/nvidia-driver-pin, Pin-Priority 1000 on
# `version <ver>-*`. It has to be INSTALLED BEFORE the driver is resolved.
# Both in one apt transaction still lands on 610, because apt computes the
# solution before the pin file exists. Hence two transactions, in order.
set -uo pipefail

VERSION=""; BRANCH=""; GO=0; TARGET_KERNEL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --version) VERSION="${2:-}"; shift 2 ;;
    --branch)  BRANCH="${2:-}";  shift 2 ;;
    --kernel)  TARGET_KERNEL="${2:-}"; shift 2 ;;
    --go)      GO=1; shift ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

hdr()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; }
run() { if (( GO )); then printf '  + %s\n' "$*"; "$@"; else printf '  would run: %s\n' "$*"; fi; }

# ─────────────────────────────────────────────────────────── where we are
hdr "Current state"
RUNNING_MOD="$(sed -n 's/^NVRM version: .*Module for x86_64 *\([0-9.]*\).*/\1/p' /proc/driver/nvidia/version 2>/dev/null)"
USERSPACE="$(basename "$(readlink -f /usr/lib/x86_64-linux-gnu/libcuda.so.1 2>/dev/null)" 2>/dev/null | sed 's/^libcuda\.so\.//')"
echo "  kernel module   : ${RUNNING_MOD:-not loaded}"
echo "  userspace libcuda: ${USERSPACE:-unknown}"
echo "  packaging       : $(dpkg -l 'nvidia-driver-*-open' 2>/dev/null | awk '/^ii/{print $2" "$3}' | head -1 || echo 'NVIDIA repo (nvidia-open)')"
echo "  running kernel  : $(uname -r)"

if [ -n "$RUNNING_MOD" ] && [ -n "$USERSPACE" ] && [ "$RUNNING_MOD" != "$USERSPACE" ]; then
  bad "kernel module $RUNNING_MOD != userspace $USERSPACE -- already mismatched, fix that before switching"
  exit 1
fi
[ -n "$RUNNING_MOD" ] && ok "kernel module and userspace agree at $RUNNING_MOD"

hdr "What is holding the GPU"
# Deliberately a warning, not a gate. Installing does not unload the running
# module -- DKMS builds the new one alongside and the old stays live until
# reboot. So a desktop session does not block the switch; it only means the
# new driver is not in use yet, which the reboot notice at the end covers.
# An earlier version of this script hard-failed here and was unrunnable on any
# booted desktop, because --query-compute-apps also lists browsers and file
# managers doing ordinary rendering.
PROCS="$(nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader 2>/dev/null | grep -v '^$' || true)"
NPROC=$(printf '%s' "$PROCS" | grep -c . || true)
if [ "${NPROC:-0}" -gt 0 ]; then
  echo "  $NPROC process(es) hold GPU memory:"
  nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader 2>/dev/null \
    | while IFS=, read -r pid mem; do
        pid="$(echo "$pid" | tr -d ' ')"
        name="$(ps -p "$pid" -o comm= 2>/dev/null || echo '?')"
        printf '      %-8s %-22s %s\n' "$pid" "$name" "$(echo "$mem" | tr -d ' ')"
      done
  echo "      These do not block the install. They do mean the currently loaded"
  echo "      module stays in use until you reboot."
else
  ok "nothing holding GPU memory"
fi
GFX="$(nvidia-smi --query-gpu=memory.used --format=csv,noheader 2>/dev/null)"
echo "  VRAM in use     : ${GFX:-unknown}"

# ───────────────────────────────────────────────────────── pick the target
hdr "Target"
if [ -z "$VERSION" ] && [ -z "$BRANCH" ]; then
  echo "  No --version or --branch given, so this is a report only."
  echo
  echo "  Available in NVIDIA's repo:"
  apt-cache madison nvidia-driver-open 2>/dev/null | awk '{print "    "$3}' | sort -Vr
  echo
  echo "  Pick one:"
  echo "    --version 595.91.07   exact release, never moves"
  echo "    --branch 595          newest 595.x, never crosses to 610"
  exit 0
fi
if [ -n "$VERSION" ] && [ -n "$BRANCH" ]; then
  echo "  give --version OR --branch, not both" >&2; exit 2
fi

if [ -n "$VERSION" ]; then
  PIN_PKG="nvidia-driver-pinning-$VERSION"; TARGET="$VERSION"
else
  PIN_PKG="nvidia-driver-pinning-$BRANCH";  TARGET="branch $BRANCH"
fi
echo "  target      : $TARGET"
echo "  pin package : $PIN_PKG"

if ! apt-cache show "$PIN_PKG" >/dev/null 2>&1; then
  bad "$PIN_PKG is not in any configured repo. Available pinning packages:"
  apt-cache search --names-only '^nvidia-driver-pinning' 2>/dev/null | awk '{print "      "$1}' | sort -V
  exit 1
fi
ok "$PIN_PKG exists"

hdr "Preflight"
[ "$(id -u)" -eq 0 ] || { bad "--go needs root"; (( GO )) && exit 1; }
AVAIL_ROOT=$(df --output=avail -BG / 2>/dev/null | tail -1 | tr -dc '0-9')
[ "${AVAIL_ROOT:-0}" -ge 5 ] && ok "/ has ${AVAIL_ROOT}G free" || warn "/ has only ${AVAIL_ROOT}G free"
if command -v mokutil >/dev/null 2>&1; then
  SB="$(mokutil --sb-state 2>/dev/null | head -1)"
  echo "  secure boot : ${SB:-unknown}"
  case "$SB" in *enabled*) warn "Secure Boot is on -- new DKMS modules must be MOK-signed or they will not load" ;; esac
fi

hdr "What this will cost"
warn "Switching replaces Ubuntu's packaging with NVIDIA's. The dry run below"
echo "      shows what goes. On this box that included nvidia-prime, the"
echo "      hybrid-graphics switcher -- irrelevant on a desktop with one GPU,"
echo "      but not on the Omen laptop."
warn "nvidia-fs (GDS) has BUILD_DEPENDS[0]=\"nvidia\" in its dkms.conf, so it"
echo "      MUST rebuild against the new driver. That rebuild is what produced"
echo "      a 0-byte nvidia-fs.ko on 2026-08-25 and silently dropped GDS into"
echo "      POSIX compat mode. This script re-checks it afterwards."
echo
echo "  Dry run of the switch:"
apt-get install --dry-run "$PIN_PKG" nvidia-open 2>&1 \
  | grep -E "^(Remv|Inst)" | sed 's/^/      /' | head -30
echo "      (note: with both in ONE transaction apt resolves to the NEWEST"
echo "       version -- that is the trap. The real run below uses two.)"

if (( ! GO )); then
  hdr "Dry run"
  echo "  Nothing changed. Re-run with --go to proceed."
  exit 0
fi

# ──────────────────────────────────────────────────────── do it, in order
hdr "1. Install the pin FIRST, on its own"
run apt-get install -y "$PIN_PKG"
if [ -f /etc/apt/preferences.d/nvidia-driver-pin ]; then
  ok "pin file present"
  grep -E "^(Package|Pin|Pin-Priority):" /etc/apt/preferences.d/nvidia-driver-pin | head -6 | sed 's/^/      /'
else
  bad "pin file did not appear -- stopping before touching the driver"
  exit 1
fi
run apt-get update -qq

hdr "2. Confirm the pin actually bites BEFORE installing"
RESOLVED="$(apt-cache policy nvidia-driver-open 2>/dev/null | awk '/Candidate:/{print $2}')"
echo "  nvidia-driver-open now resolves to: $RESOLVED"
case "$RESOLVED" in
  ${VERSION:-${BRANCH:-x}}*) ok "pin is working" ;;
  *) bad "pin did not take -- candidate is $RESOLVED, not $TARGET."
     echo "      Refusing to continue. Remove $PIN_PKG and investigate." ; exit 1 ;;
esac

hdr "3. Install the driver, second transaction"
# --allow-downgrades is required, not optional. Ubuntu's nvidia-settings can
# be NEWER than the pinned branch (610.57.04 vs 595.91.07 here), and without
# this apt aborts the ENTIRE transaction with
#   E: Packages were downgraded and -y was used without --allow-downgrades
# leaving the pin installed and the driver untouched -- which looks like a
# partial success and is not.
if (( GO )); then
  if ! apt-get install -y --allow-downgrades nvidia-open; then
    bad "driver install failed -- nothing was switched."
    echo "      The pin ($PIN_PKG) IS installed. Either re-run this script,"
    echo "      or back it out with:  sudo apt remove $PIN_PKG"
    exit 1
  fi
else
  echo "  would run: apt-get install -y --allow-downgrades nvidia-open"
fi

hdr "3b. Did the packaging actually change?"
if dpkg -l nvidia-open 2>/dev/null | grep -q '^ii'; then
  ok "nvidia-open installed: $(dpkg-query -W -f='${Version}' nvidia-open 2>/dev/null)"
else
  bad "nvidia-open is NOT installed -- the switch did not happen"
  exit 1
fi
if dpkg -l 'nvidia-driver-*-open' 2>/dev/null | grep -q '^ii'; then
  bad "Ubuntu's nvidia-driver-*-open is STILL installed alongside NVIDIA's."
  echo "      That is a half-switched state. Investigate before rebooting."
  exit 1
fi

hdr "4. Rebuild everything that depends on the driver"
run dkms autoinstall

hdr "5. Verify"
# Against the TARGET kernel, not `uname -r`. An earlier version of this script
# checked the running kernel and `dkms status nvidia | head -1`, which returned
# a 7.0.0-15-generic entry from months earlier -- so it printed a green tick
# over a switch that had entirely failed. Check the kernel you are about to
# boot, and check it by name.
if [ -z "$TARGET_KERNEL" ]; then
  # newest installed kernel that has a modules dir, preferring one that is not
  # the running one (that is the kernel you just built and mean to boot)
  TARGET_KERNEL="$(ls -1 /lib/modules 2>/dev/null | grep -v "^$(uname -r)$" | sort -V | tail -1)"
  [ -n "$TARGET_KERNEL" ] || TARGET_KERNEL="$(uname -r)"
  echo "  no --kernel given; checking newest non-running kernel: $TARGET_KERNEL"
fi
echo "  target kernel : $TARGET_KERNEL"
fail=0

NV_LINE="$(dkms status nvidia 2>/dev/null | grep -F "$TARGET_KERNEL" || true)"
if [ -n "$NV_LINE" ]; then
  ok "nvidia: $NV_LINE"
else
  bad "nvidia has NO module built for $TARGET_KERNEL"
  echo "      dkms status nvidia says:"
  dkms status nvidia 2>/dev/null | sed 's/^/        /'
  echo "      log: /var/lib/dkms/nvidia/*/build/make.log"
  fail=1
fi

NVFS_KO="/lib/modules/$TARGET_KERNEL/updates/dkms/nvidia-fs.ko"
if [ -e "$NVFS_KO" ]; then
  SZ=$(stat -c%s "$NVFS_KO")
  if [ "$SZ" -gt 0 ]; then ok "nvidia-fs.ko for $TARGET_KERNEL: $SZ bytes"
  else bad "nvidia-fs.ko for $TARGET_KERNEL is 0 bytes -- GDS would fall back to POSIX compat"
       echo "      fix: sudo bash diagnostics/fix-nvidia-fs-symvers.sh --go"; fail=1; fi
else
  bad "no nvidia-fs.ko for $TARGET_KERNEL (it depends on nvidia, which must build first)"
  fail=1
fi

if (( fail )); then
  echo
  bad "DO NOT REBOOT INTO $TARGET_KERNEL -- it has no working NVIDIA driver."
  echo "      The currently running kernel ($(uname -r)) is unaffected."
fi

echo
echo "  A REBOOT is required before the new module is actually in use."
echo "  After rebooting, confirm the two halves match -- this is the check"
echo "  that catches a half-applied switch:"
echo
echo "      cat /proc/driver/nvidia/version"
echo "      basename \$(readlink -f /usr/lib/x86_64-linux-gnu/libcuda.so.1)"
echo "      nvidia-smi"
echo "      cat /proc/driver/nvidia-fs/stats | head -3"
echo
echo "  If the GPU does not come back:"
echo "      sudo apt install --reinstall nvidia-driver-595-open"
echo "      sudo apt remove $PIN_PKG"
echo "  or pick -generic from GRUB and repair from there."

exit $fail
