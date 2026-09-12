#!/usr/bin/env bash
# Bench-only GRUB drop-in for the two reboot-gated levers found during the
# platform-kernel-levers sweep (L2 nvme.poll_queues, L3 nvme.max_host_mem_size_mb
# — both read-only at runtime, sysfs confirms: `max_host_mem_size_mb` is
# 0444, `poll_queues` module param only takes effect at nvme driver load,
# i.e. boot).
#
# Follows the repo's hard constraint: drop-in only, NEVER writes
# /etc/default/grub directly (hyphaed/grub.py owns that file's own drop-in at
# /etc/default/grub.d/90-hyphaed.cfg; this one is numbered 91 so it applies
# AFTER hyphaed's and wins on any token collision — last-wins on the kernel
# cmdline, same rule grub.py documents for its own drop-in).
#
# Refuses to compose mitigations=off, matching hyphaed/grub.py's own guard.
# This script does not touch mitigations at all, but the guard stays as a
# backstop against a mistyped --extra.
#
#   bash diagnostics/set-boot-levers.sh                              # report current state, no root needed
#   sudo bash diagnostics/set-boot-levers.sh --poll-queues 4 --go
#   sudo bash diagnostics/set-boot-levers.sh --hmb-mb 512 --go        # derive the real max from the drive first (see below)
#   sudo bash diagnostics/set-boot-levers.sh --revert --go            # remove the drop-in, back to hyphaed's own cmdline
set -uo pipefail

DROPIN=/etc/default/grub.d/91-hyphaed-bench.cfg
POLL_QUEUES=""; HMB_MB=""; EXTRA=""; GO=0; REVERT=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --poll-queues) POLL_QUEUES="${2:-}"; shift 2 ;;
    --hmb-mb)      HMB_MB="${2:-}"; shift 2 ;;
    --extra)       EXTRA="${2:-}"; shift 2 ;;
    --revert)      REVERT=1; shift ;;
    --go)          GO=1; shift ;;
    -h|--help)     sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

hdr()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; }

hdr "Current live (runtime) state"
echo "  nvme.poll_queues          : $(cat /sys/module/nvme/parameters/poll_queues 2>/dev/null)  (reboot-gated)"
echo "  nvme.max_host_mem_size_mb : $(cat /sys/module/nvme/parameters/max_host_mem_size_mb 2>/dev/null)  (reboot-gated, sysfs is read-only)"
[[ -f "$DROPIN" ]] && { echo "  existing bench drop-in:"; sed 's/^/    /' "$DROPIN"; } || echo "  no bench drop-in present"

if [[ $EUID -eq 0 ]]; then
  hdr "What the drive actually asks for (nvme id-ctrl HMPRE/HMMIN, 4KiB units)"
  HM_LINE="$(nvme id-ctrl /dev/nvme0 2>/dev/null | grep -iE '^hmpre|^hmmin')"
  if [[ -n "$HM_LINE" ]]; then
    echo "$HM_LINE" | sed 's/^/  /'
    HMPRE="$(echo "$HM_LINE" | awk '/hmpre/{print $NF}')"
    HMMIN="$(echo "$HM_LINE" | awk '/hmmin/{print $NF}')"
    [[ -n "$HMPRE" ]] && echo "  preferred HMB size : $(( HMPRE * 4 / 1024 )) MiB (HMPRE * 4KiB)"
    [[ -n "$HMMIN" ]] && echo "  minimum HMB size   : $(( HMMIN * 4 / 1024 )) MiB (HMMIN * 4KiB)"
    echo "  Do not exceed the preferred size without a reason — HMB is a real"
    echo "  claim on host RAM the controller can hold for as long as it's attached."
  else
    warn "could not read HMPRE/HMMIN — nvme id-ctrl needs the controller node (/dev/nvme0), not the block device"
  fi
fi

if (( REVERT )); then
  hdr "Revert"
  if [[ ! -f "$DROPIN" ]]; then
    ok "nothing to revert — $DROPIN does not exist"
    exit 0
  fi
  if (( ! GO )); then
    echo "  would remove: $DROPIN"
    echo "  would run: update-grub"
    exit 0
  fi
  [[ $EUID -eq 0 ]] || { bad "--go needs root"; exit 1; }
  rm -f "$DROPIN"
  ok "removed $DROPIN"
  update-grub
  echo "  Reboot to pick up the reverted cmdline."
  exit 0
fi

if [[ -z "$POLL_QUEUES" && -z "$HMB_MB" && -z "$EXTRA" ]]; then
  hdr "Nothing to set"
  echo "  Pass --poll-queues N and/or --hmb-mb N (and/or --extra 'token=value')."
  exit 0
fi

TOKENS=()
[[ -n "$POLL_QUEUES" ]] && TOKENS+=("nvme.poll_queues=$POLL_QUEUES")
[[ -n "$HMB_MB" ]] && TOKENS+=("nvme.max_host_mem_size_mb=$HMB_MB")
[[ -n "$EXTRA" ]] && TOKENS+=("$EXTRA")

for t in "${TOKENS[@]}"; do
  if [[ "$t" == *"mitigations=off"* ]]; then
    bad "refusing to compose mitigations=off (hard constraint, see CLAUDE.md)"
    exit 1
  fi
done

hdr "Would write"
echo "  $DROPIN :"
printf '  GRUB_CMDLINE_LINUX_DEFAULT="${GRUB_CMDLINE_LINUX_DEFAULT} %s"\n' "${TOKENS[*]}" | sed 's/^/    /'

if (( ! GO )); then
  echo
  echo "  Re-run with --go (as root) to write the drop-in and run update-grub."
  exit 0
fi
[[ $EUID -eq 0 ]] || { bad "--go needs root"; exit 1; }

hdr "Writing $DROPIN"
{
  echo "# Bench-only lever set, written by diagnostics/set-boot-levers.sh."
  echo "# Applies AFTER /etc/default/grub.d/90-hyphaed.cfg (lexical order, 91 > 90),"
  echo "# so any token here overrides hyphaed's own composed cmdline on collision."
  echo "# Revert: sudo bash diagnostics/set-boot-levers.sh --revert --go"
  printf 'GRUB_CMDLINE_LINUX_DEFAULT="${GRUB_CMDLINE_LINUX_DEFAULT} %s"\n' "${TOKENS[*]}"
} > "$DROPIN"
ok "wrote $DROPIN"

hdr "update-grub"
update-grub

hdr "Verify before rebooting"
grep -h "" /boot/grub/grub.cfg 2>/dev/null | grep -F "${TOKENS[0]}" | head -2 | sed 's/^/  /' \
  || warn "token not found in grub.cfg — check manually before rebooting"

echo
echo "  Reboot to apply. After boot, confirm with:"
echo "    cat /proc/cmdline"
echo "    cat /sys/module/nvme/parameters/poll_queues"
echo "    cat /sys/module/nvme/parameters/max_host_mem_size_mb"
echo "  If anything looks wrong, pick -generic from GRUB and:"
echo "    sudo bash diagnostics/set-boot-levers.sh --revert --go"
