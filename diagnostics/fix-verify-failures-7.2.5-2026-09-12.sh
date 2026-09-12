#!/usr/bin/env bash
# Fixes the three `hyphaed verify` failures found on 7.2.5-hyphaed on
# 2026-09-12:
#
#   DKMS modules all installed  ✗  hid-xpadneo/v0.11-pre-63-g3acca9f-dirty: added
#   nvidia_fs loaded             ✗  NOT loaded — GDS is in POSIX compat mode
#   no failed systemd units      ✗  nvidia-fs-load.service
#
# The last two are ONE fault, confirmed from
# /var/lib/dkms/nvidia-fs/2.30.1/7.2.5-hyphaed/x86_64/log/make.log:
#
#   -W- Module .../nvidia.ko contains relative CRCs, cannot get symbols from it!
#   Using nvidia DKMS Module.symvers: .../nvidia/615.71.09/7.2.3-hyphaed/x86_64/module/Module.symvers
#
# nvidia-fs was built FOR 7.2.5-hyphaed but AGAINST 7.2.3-hyphaed's nvidia
# symbol versions (the box was still booted on 7.2.3 during the 08:43 DKMS
# autoinstall). Three of the eight nvidia_p2p_* CRCs disagree with the
# nvidia.ko that 7.2.5 actually loads, so modprobe fails with EINVAL and
# nvidia-fs-load.service goes down with it. This is exactly the bug
# diagnostics/fix-nvidia-fs-symvers.sh already documents and fixes; it just
# needs re-running because /usr/src/nvidia-fs-2.30.1/Makefile.orig doesn't
# exist — nvidia-fs went 2.29.4 → 2.30.1 since that script last ran, and the
# one-word Makefile patch doesn't survive a package upgrade.
#
# The DKMS row is unrelated: hid-xpadneo/v0.11-pre-63-g3acca9f-dirty is a
# stale DKMS *source* registration ("added", no build for any kernel) left
# behind after the module moved on to v0.11-pre-64-g5de4fde-dirty.
#
#   sudo bash diagnostics/fix-verify-failures-7.2.5-2026-09-12.sh                        # show what it would do
#   sudo bash diagnostics/fix-verify-failures-7.2.5-2026-09-12.sh --go                    # do it
#   sudo bash diagnostics/fix-verify-failures-7.2.5-2026-09-12.sh --go --purge-xpadneo-src # also delete the stale source tree
#
# Safe to re-run. Every step checks whether it has already been applied.
set -uo pipefail

GO=0
PURGE_XPADNEO_SRC=0
for arg in "$@"; do
  case "$arg" in
    --go) GO=1 ;;
    --purge-xpadneo-src) PURGE_XPADNEO_SRC=1 ;;
  esac
done

hdr()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
skip() { printf '  \033[2m· %s\033[0m\n' "$*"; }
did()  { printf '  \033[32m✓\033[0m %s\n' "$*"; }
run() {
  if (( GO )); then printf '  + %s\n' "$*"; "$@"
  else printf '  would run: %s\n' "$*"; fi
}

[ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }
(( GO )) || printf '\033[33mDRY RUN — nothing will change. Re-run with --go.\033[0m\n'

KVER="$(uname -r)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ══════════════════════════════════ 1. nvidia-fs built against the wrong kernel
hdr "1. Fix nvidia-fs.ko's nvidia_p2p_* symbol versions (built for a different kernel)"
if lsmod | grep -q '^nvidia_fs'; then
  skip "nvidia_fs already loaded — nothing to fix"
else
  # Confirm the diagnosis before acting, rather than assuming this run hit
  # the same cause as the 2026-09-12 audit.
  LOG=$(ls -t /var/lib/dkms/nvidia-fs/*/"$KVER"/x86_64/log/make.log 2>/dev/null | head -1)
  if [ -n "$LOG" ]; then
    WRONG_LINE=$(grep "Using nvidia DKMS Module.symvers:" "$LOG" 2>/dev/null | grep -v "/$KVER/" || true)
    if [ -n "$WRONG_LINE" ]; then
      echo "  confirmed in $LOG:"
      echo "    $WRONG_LINE"
    else
      echo "  $LOG doesn't show a cross-kernel symvers pick — proceeding anyway,"
      echo "  since nvidia_fs isn't loaded either way."
    fi
  else
    echo "  no make.log found for nvidia-fs under kernel $KVER yet — proceeding."
  fi

  FIXER="$SCRIPT_DIR/fix-nvidia-fs-symvers.sh"
  if [ ! -f "$FIXER" ]; then
    echo "  ERROR: $FIXER not found — can't delegate the symvers fix" >&2
  else
    run bash "$FIXER"
    if (( GO )); then
      NEWLOG=$(ls -t /var/lib/dkms/nvidia-fs/*/"$KVER"/x86_64/log/make.log 2>/dev/null | head -1)
      if [ -n "$NEWLOG" ]; then
        echo "  symvers used on the rebuild:"
        grep -i "Module.symvers" "$NEWLOG" | sed 's/^/    /'
      fi
    fi
  fi
fi

# ══════════════════════════════ 2. nvidia-fs-load.service failed as a result
hdr "2. Clear nvidia-fs-load.service's failed state"
if ! systemctl is-failed --quiet nvidia-fs-load.service 2>/dev/null; then
  skip "nvidia-fs-load.service is not in a failed state"
elif ! lsmod | grep -q '^nvidia_fs'; then
  echo "  nvidia_fs still isn't loaded — leaving the unit failed rather than"
  echo "  masking a real problem with a green status."
else
  run systemctl reset-failed nvidia-fs-load.service
  run systemctl start nvidia-fs-load.service
  if (( GO )); then
    if systemctl is-active --quiet nvidia-fs-load.service; then
      did "nvidia-fs-load.service is active"
    else
      echo "  WARNING: still not active — check: systemctl status nvidia-fs-load.service" >&2
    fi
  fi
fi

# ══════════════════════════════════ 3. stale hid-xpadneo DKMS source entry
hdr "3. Remove the stale hid-xpadneo DKMS source registration"
STALE_VER="v0.11-pre-63-g3acca9f-dirty"
CURRENT_VER="v0.11-pre-64-g5de4fde-dirty"
if ! dkms status "hid-xpadneo/$STALE_VER" 2>/dev/null | grep -q .; then
  skip "hid-xpadneo/$STALE_VER is not registered — nothing to remove"
elif ! dkms status "hid-xpadneo/$CURRENT_VER" -k "$KVER" 2>/dev/null | grep -q installed; then
  echo "  hid-xpadneo/$CURRENT_VER isn't installed for $KVER — leaving the"
  echo "  stale entry alone rather than remove the only registration that works."
else
  echo "  found: $(dkms status "hid-xpadneo/$STALE_VER" 2>/dev/null)"
  run dkms remove "hid-xpadneo/$STALE_VER" --all
  if (( PURGE_XPADNEO_SRC )); then
    SRC="/usr/src/hid-xpadneo-$STALE_VER"
    if [ -d "$SRC" ]; then
      run rm -rf "$SRC"
    else
      skip "$SRC already gone"
    fi
  fi
fi

# ══════════════════════════════════════════════════════════════ 4. close out
hdr "Where that leaves things"
if (( ! GO )); then
  echo "  dry run — nothing changed."
  exit 0
fi
echo "  nvidia_fs loaded    : $(lsmod | grep -q '^nvidia_fs' && echo yes || echo no)"
echo "  nvidia-fs-load.service: $(systemctl is-active nvidia-fs-load.service 2>&1)"
echo "  dkms xpadneo entries: $(dkms status 2>/dev/null | grep -c hid-xpadneo)"
if command -v gdscheck >/dev/null 2>&1; then
  echo
  echo "  gdscheck -p (the real evidence GDS left POSIX compat, not just that"
  echo "  a module inserted):"
  gdscheck -p 2>&1 | sed 's/^/    /'
fi
echo
echo "Re-run: python -m hyphaed verify"
