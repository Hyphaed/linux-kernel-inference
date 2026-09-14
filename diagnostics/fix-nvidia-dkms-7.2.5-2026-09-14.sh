#!/usr/bin/env bash
# Fixes the DKMS failure that left linux-image-7.2.5-hyphaed half-configured
# after `sudo dpkg -i *.deb`, found 2026-09-14:
#
#   nvidia/os-interface.c:764:5: error: implicit declaration of function
#   'strncpy' [-Wimplicit-function-declaration]
#   Error! Bad return status for module build on kernel: 7.2.5-hyphaed (x86_64)
#   dpkg: error processing package linux-image-7.2.5-hyphaed (--install):
#    old linux-image-7.2.5-hyphaed package postinst maintainer script
#    subprocess failed with exit status 1
#
# Kernel 7.2 removed strncpy() from include/linux/string.h outright -- 7.1.5
# declares it at include/linux/string.h:71, 7.2.5 names it only in comments as
# the thing to replace (this repo already knows: tests/test_kernel_org_72.py's
# REMOVED_IN_7_2, and patch 0023's forward-port notes). NVIDIA 595.84 still
# calls strncpy() in five places (nvidia/os-interface.c, linux_nvswitch.c x2,
# nvidia-modeset-linux.c, uvm_pmm_gpu.c), and the kernel builds external
# modules with -Werror=implicit-function-declaration, so the first call site
# to compile kills the whole build. nvidia-fs then fails too, since its
# dkms.conf has BUILD_DEPENDS[0]="nvidia".
#
# (The "ERROR (dkms apport): kernel package ... is not supported" line dpkg
# printed is unrelated noise -- Ubuntu's apport hook declining to file a bug
# about a non-Ubuntu kernel. The real error is the implicit-declaration one,
# in /var/lib/dkms/nvidia/595.84/build/make.log.)
#
# FIX CHOSEN: upgrade to NVIDIA 615.71.09 from the CUDA repo already
# configured on this box, rather than patch 595.84's source (which would be
# silently reverted by the next `nvidia-dkms-595-open` package upgrade).
# Verified against the local source clone at
# ~/Dev/greenboost_all/greenboost_sources/open-gpu-kernel-modules (tag
# 615.71.09): the kernel modules contain no strncpy() call at all -- the one
# repo-wide hit (liblogdecode.c) is in the userspace-only #else branch, not
# the kernel (NVRM) build path. Confirmed available: nvidia-open /
# nvidia-dkms-open 615.71.09-2ubuntu1 and nvidia-driver-pinning-615.71.09,
# both from developer.download.nvidia.com/compute/cuda/repos/ubuntu2604/x86_64.
#
# THE TRAP THIS WORKS AROUND: on this box, `apt-get install --dry-run`
# appends `Conf linux-image-7.2.5-hyphaed` to EVERY transaction while it's
# stuck half-configured, and that reconfigure re-runs the same failing DKMS
# hook -- so apt reports failure on things as small as installing a pinning
# package. diagnostics/nvidia-driver-switch.sh treats a non-zero
# `apt-get install nvidia-open` as "nothing was switched" and aborts, which
# would be a FALSE failure here (nvidia-open installs fine; the trailing dpkg
# reconfigure is what fails). So step 1 below defuses the stuck dpkg
# transaction FIRST, by telling dkms not to autobuild nvidia/nvidia-fs during
# `dpkg --configure -a` (AUTOINSTALL="no" in their dkms.conf, restored after).
#
#   bash diagnostics/fix-nvidia-dkms-7.2.5-2026-09-14.sh                # dry run, no root
#   sudo bash diagnostics/fix-nvidia-dkms-7.2.5-2026-09-14.sh --go
#
# Safe to re-run -- every stage checks whether it already applied.
set -uo pipefail

KVER="7.2.5-hyphaed"
FALLBACK_KVER="7.1.5-hyphaed"
NV_VERSION="615.71.09"
GO=0
while [ $# -gt 0 ]; do
  case "$1" in
    --go) GO=1; shift ;;
    --kernel) KVER="${2:?}"; shift 2 ;;
    --fallback-kernel) FALLBACK_KVER="${2:?}"; shift 2 ;;
    --version) NV_VERSION="${2:?}"; shift 2 ;;
    -h|--help) sed -n '2,45p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

hdr()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; }
skip() { printf '  \033[2m· %s\033[0m\n' "$*"; }
run()  { if (( GO )); then printf '  + %s\n' "$*"; "$@"; else printf '  would run: %s\n' "$*"; fi; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

(( GO )) || printf '\033[33mDRY RUN — nothing will change. Re-run with --go.\033[0m\n'

# ══════════════════════════════════════════════════════════ 0. diagnose
hdr "0. Confirm the diagnosis (does not require root)"
FAIL=0

HDRS="/usr/src/linux-headers-$KVER/include/linux/string.h"
if [ -f "$HDRS" ] && ! grep -qE '^\s*extern\s+char\s*\*\s*strncpy' "$HDRS"; then
  ok "strncpy() is not declared in $KVER's headers (as expected for 7.2)"
elif [ -f "$HDRS" ]; then
  warn "strncpy() IS declared in $KVER's headers — this may not be the same bug. Check make.log by hand."
else
  bad "$HDRS not found — is linux-headers-$KVER installed?"
  FAIL=1
fi

NV_LOG=$(ls -t /var/lib/dkms/nvidia/*/build/make.log 2>/dev/null | head -1)
if [ -n "$NV_LOG" ] && grep -q "implicit declaration of function .strncpy." "$NV_LOG"; then
  ok "confirmed in $NV_LOG"
else
  warn "no implicit-strncpy error found in the newest nvidia make.log — proceeding anyway"
fi

DPKG_STATE=$(dpkg-query -W -f='${Status}' "linux-image-$KVER" 2>/dev/null || true)
case "$DPKG_STATE" in
  *"install ok installed"*) skip "linux-image-$KVER is already fully configured — nothing to defuse in step 1" ;;
  *) if [ -n "$DPKG_STATE" ]; then
       warn "linux-image-$KVER dpkg state: $DPKG_STATE (half-configured)"
     else
       bad "linux-image-$KVER is not installed at all"; FAIL=1
     fi ;;
esac

if apt-cache show "nvidia-driver-pinning-$NV_VERSION" >/dev/null 2>&1; then
  ok "nvidia-driver-pinning-$NV_VERSION is available"
else
  bad "nvidia-driver-pinning-$NV_VERSION not found in any configured repo"
  apt-cache search --names-only '^nvidia-driver-pinning' 2>/dev/null | sed 's/^/      /'
  FAIL=1
fi

SWITCH_SCRIPT="$SCRIPT_DIR/nvidia-driver-switch.sh"
[ -f "$SWITCH_SCRIPT" ] || { bad "$SWITCH_SCRIPT not found"; FAIL=1; }

if (( FAIL )); then
  bad "diagnosis did not fully confirm — refusing to continue"
  exit 1
fi

if (( ! GO )); then
  hdr "Plan"
  echo "  1. Temporarily set AUTOINSTALL=no in nvidia/nvidia-fs dkms.conf, then"
  echo "     dpkg --configure -a  (unblocks apt without needing a working build)"
  echo "  2. diagnostics/nvidia-driver-switch.sh --version $NV_VERSION --kernel $KVER --go"
  echo "  3. Restore nvidia-fs's dkms.conf; report any surviving 595 packages"
  echo "  4. dkms install nvidia/$NV_VERSION for $FALLBACK_KVER (warn-only) and $KVER (hard fail)"
  echo "     then nvidia-fs for both"
  echo "  5. Check nvidia-fs wasn't built against the wrong kernel's symvers"
  echo "  6. Report final state"
  echo
  echo "  Nothing has changed. Re-run with --go to proceed."
  exit 0
fi

[ "$(id -u)" -eq 0 ] || { bad "--go needs root"; exit 1; }

# ══════════════════════════════════════════════ 1. defuse the stuck dpkg
hdr "1. Defuse the stuck dpkg transaction"
NV_CONF="/usr/src/nvidia-595.84/dkms.conf"
NVFS_CONF="/usr/src/nvidia-fs-2.30.1/dkms.conf"

DPKG_STATE=$(dpkg-query -W -f='${Status}' "linux-image-$KVER" 2>/dev/null || true)
if [[ "$DPKG_STATE" == *"install ok installed"* ]]; then
  skip "linux-image-$KVER already configured — skipping the AUTOINSTALL dance"
else
  for conf in "$NV_CONF" "$NVFS_CONF"; do
    [ -f "$conf" ] || { warn "$conf not found — skipping"; continue; }
    if grep -q '^AUTOINSTALL="no"' "$conf"; then
      skip "$conf already has AUTOINSTALL=no"
    else
      run cp -n "$conf" "$conf.orig-fix-nvidia-dkms"
      run sed -i 's/^AUTOINSTALL="yes"/AUTOINSTALL="no"/' "$conf"
    fi
  done

  run dpkg --configure -a

  DPKG_STATE=$(dpkg-query -W -f='${Status}' "linux-image-$KVER" 2>/dev/null || true)
  if [[ "$DPKG_STATE" == *"install ok installed"* ]]; then
    ok "linux-image-$KVER is now fully configured"
  else
    bad "linux-image-$KVER is still not configured (state: $DPKG_STATE) — stopping"
    exit 1
  fi
fi

# ══════════════════════════════════════════════════ 2. switch the driver
hdr "2. Switch NVIDIA driver to $NV_VERSION"
INSTALLED_NV_OPEN_VER=$(dpkg-query -W -f='${Version}' nvidia-open 2>/dev/null || true)
if [[ "$INSTALLED_NV_OPEN_VER" == "$NV_VERSION-"* ]]; then
  # Idempotence, and not just style: on a re-run BEFORE rebooting into the
  # new module, the kernel module still loaded is the old one while userspace
  # (libcuda etc.) is already the new version. nvidia-driver-switch.sh's own
  # very first check treats that as "already mismatched, fix that before
  # switching" and hard-exits -- correctly, for a fresh mismatch, but wrongly
  # here where WE caused it by already having switched. So once nvidia-open
  # is at the target version, skip calling it again.
  skip "nvidia-open already at $INSTALLED_NV_OPEN_VER — driver switch already done"
else
  bash "$SWITCH_SCRIPT" --version "$NV_VERSION" --kernel "$KVER" --go
  SWITCH_RC=$?

  # A non-zero rc here does NOT necessarily mean "nothing was switched": that
  # script's own final section (5. Verify) checks nvidia-fs for the target
  # kernel and exits non-zero if nvidia-fs isn't built there yet -- which is
  # expected and EXACTLY what stages 3-5 below fix, on a kernel where
  # nvidia-fs was never registered before (so the automatic post-driver-
  # switch DKMS rebuild trigger, which only rebuilds modules already
  # installed for a given kernel, never touched it). Confirmed 2026-09-14:
  # sections 1-4 of that script (pin, driver install, packaging-changed
  # check) all have their own hard exits on real failure, so reaching its
  # "5. Verify" section at all means the actual switch already succeeded.
  # The real signal is whether nvidia-open is installed, not the raw rc.
  if dpkg-query -W -f='${Status}' nvidia-open 2>/dev/null | grep -q "install ok installed"; then
    if (( SWITCH_RC != 0 )); then
      warn "nvidia-driver-switch.sh exited $SWITCH_RC, but nvidia-open IS installed"
      echo "      — the switch itself succeeded; its own nvidia-fs check for $KVER is what"
      echo "      failed, and that's what stages 4-5 below fix. Continuing."
    else
      ok "driver switch script completed"
    fi
  else
    bad "nvidia-open is NOT installed — the switch genuinely did not happen (rc=$SWITCH_RC)"
    echo "      Re-running this script is safe once you've investigated the output above."
    exit 1
  fi
fi

# ══════════════════════════════════════════ 3. restore configs, check leftovers
hdr "3. Restore nvidia-fs dkms.conf; check for 595 leftovers"
if [ -f "$NVFS_CONF.orig-fix-nvidia-dkms" ]; then
  run cp -f "$NVFS_CONF.orig-fix-nvidia-dkms" "$NVFS_CONF"
  ok "restored $NVFS_CONF (AUTOINSTALL=yes)"
else
  skip "no backup found for $NVFS_CONF — nothing to restore"
fi
# nvidia's own dkms.conf leaves with the nvidia-dkms-595-open package when
# apt removes it as part of the switch; only warn if it's still there.
if [ -f "$NV_CONF" ]; then
  warn "$NV_CONF still present — nvidia-dkms-595-open may not have been removed"
fi

LEFTOVERS=$(dpkg -l 'nvidia-dkms-595*' 'nvidia-kernel-source-595*' 'nvidia-utils-595' \
                    'nvidia-kernel-common-595' 'nvidia-firmware-595*' 'libnvidia-compute-595*' \
                    2>/dev/null | awk '/^ii/{print $2}')
if [ -n "$LEFTOVERS" ]; then
  warn "595 packages still installed (not purged automatically):"
  echo "$LEFTOVERS" | sed 's/^/      /'
else
  ok "no surviving 595 NVIDIA packages"
fi

# ══════════════════════════════════ 4. build 615 for the kernels that matter
hdr "4. Build nvidia/$NV_VERSION for $FALLBACK_KVER (fallback) and $KVER (target)"
if dkms status "nvidia/$NV_VERSION" -k "$FALLBACK_KVER" 2>/dev/null | grep -q installed; then
  skip "nvidia/$NV_VERSION already installed for $FALLBACK_KVER"
else
  run dkms install "nvidia/$NV_VERSION" -k "$FALLBACK_KVER"
  if dkms status "nvidia/$NV_VERSION" -k "$FALLBACK_KVER" 2>/dev/null | grep -q installed; then
    ok "nvidia/$NV_VERSION built for fallback kernel $FALLBACK_KVER"
  else
    warn "nvidia/$NV_VERSION did NOT build for fallback kernel $FALLBACK_KVER — that kernel would boot without a GPU driver too. Investigate before relying on it as a rollback."
  fi
fi

if dkms status "nvidia/$NV_VERSION" -k "$KVER" 2>/dev/null | grep -q installed; then
  skip "nvidia/$NV_VERSION already installed for $KVER"
else
  run dkms install "nvidia/$NV_VERSION" -k "$KVER"
  if dkms status "nvidia/$NV_VERSION" -k "$KVER" 2>/dev/null | grep -q installed; then
    ok "nvidia/$NV_VERSION built for target kernel $KVER"
  else
    bad "nvidia/$NV_VERSION did NOT build for $KVER — this is the kernel you're trying to fix"
    echo "      check: /var/lib/dkms/nvidia/$NV_VERSION/$KVER/x86_64/log/make.log"
    exit 1
  fi
fi

NVFS_VER=$(dkms status 2>/dev/null | sed -n 's#^nvidia-fs/\([^,]*\),.*#\1#p' | head -1)
if [ -n "$NVFS_VER" ]; then
  for k in "$FALLBACK_KVER" "$KVER"; do
    if dkms status "nvidia-fs/$NVFS_VER" -k "$k" 2>/dev/null | grep -q installed; then
      skip "nvidia-fs/$NVFS_VER already installed for $k"
    else
      run dkms install "nvidia-fs/$NVFS_VER" -k "$k"
    fi
  done
else
  warn "nvidia-fs not registered with dkms — skipping"
fi

# ══════════════════════════════════════════ 5. the nvidia-fs symvers trap
hdr "5. Check nvidia-fs wasn't built against the wrong kernel's symvers"
NVFS_CHECK=$(PYTHONPATH="$REPO_ROOT" python3 -c "
import sys
sys.path.insert(0, '$REPO_ROOT')
from hyphaed.nvidia_fs import nvidia_fs_module_is_truncated, nvidia_fs_built_against_wrong_kernel
truncated = nvidia_fs_module_is_truncated('$KVER')
wrong = nvidia_fs_built_against_wrong_kernel('$KVER')
if truncated:
    print(f'TRUNCATED:{truncated}')
elif wrong:
    print(f'WRONG_KERNEL:{wrong}')
else:
    print('OK')
" 2>/dev/null || echo "CHECK_FAILED")

case "$NVFS_CHECK" in
  OK) ok "nvidia-fs.ko for $KVER looks consistent" ;;
  TRUNCATED:*)
    bad "nvidia-fs.ko for $KVER is 0 bytes (${NVFS_CHECK#TRUNCATED:})"
    run bash "$SCRIPT_DIR/fix-nvidia-fs-symvers.sh"
    ;;
  WRONG_KERNEL:*)
    bad "nvidia-fs was built for $KVER against ${NVFS_CHECK#WRONG_KERNEL:}'s nvidia symbols"
    run bash "$SCRIPT_DIR/fix-nvidia-fs-symvers.sh"
    ;;
  *) warn "could not run the nvidia_fs.py checks (rc/output: $NVFS_CHECK) — check by hand" ;;
esac

# ══════════════════════════════════════════════════════════════ 6. report
hdr "6. Final state"
echo "  linux-image-$KVER : $(dpkg-query -W -f='${Status}\n' "linux-image-$KVER" 2>/dev/null)"
echo "  dkms nvidia       :"
dkms status nvidia 2>/dev/null | sed 's/^/    /'
echo "  dkms nvidia-fs    :"
dkms status nvidia-fs 2>/dev/null | sed 's/^/    /'
echo
RUNNING_MOD="$(sed -n 's/^NVRM version: .*Module for x86_64 *\([0-9.]*\).*/\1/p' /proc/driver/nvidia/version 2>/dev/null)"
USERSPACE="$(basename "$(readlink -f /usr/lib/x86_64-linux-gnu/libcuda.so.1 2>/dev/null)" 2>/dev/null | sed 's/^libcuda\.so\.//')"
if [ -n "$RUNNING_MOD" ] && [ -n "$USERSPACE" ] && [ "$RUNNING_MOD" != "$USERSPACE" ]; then
  warn "loaded kernel module ($RUNNING_MOD) != installed userspace ($USERSPACE) — expected until you reboot"
fi
echo
echo "  Next: cd $REPO_ROOT && make install    # runs the install+postinstall phases you skipped"
echo "  Then: reboot into $KVER, and run: make verify && make doctor"
