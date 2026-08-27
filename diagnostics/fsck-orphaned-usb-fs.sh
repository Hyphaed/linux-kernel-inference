#!/usr/bin/env bash
# One ext4 filesystem on an external enclosure was left with an aborted
# journal on 2026-08-26, and it has not been checked since.
#
# What happened, from the boot -1 log:
#
#   [12720.382] usb 2-1: SuperSpeed Plus Gen 2x1 — ASUS ROG STRIX Arion (0b05:1932)
#   [12726.004] sda: 1953525168 blocks (1.00 TB) — Attached SCSI disk
#   [12734.340] EXT4-fs (sda1): mounted filesystem 8563871e-… r/w
#   [12767.742] usb 2-1: USB disconnect, device number 2
#   [12767.744] device offline error … Buffer I/O error … Aborting journal
#   [12767.745] EXT4-fs (sda1): Remounting filesystem read-only
#   [12775.578] sda: 3907029168 blocks (2.00 TB) — Attached SCSI disk
#
# The enclosure was unplugged 33 seconds after being mounted read-write, with
# dirty pages still outstanding, and a different, larger drive was attached
# 2.3 seconds later. That is a live drive swap. It is not a hardware fault and
# it is not a kernel bug — the kernel did exactly the right thing by refusing
# further writes.
#
# What it costs: writes from those last 33 seconds are gone, and the journal
# was aborted rather than closed. ext4 will normally replay it on the next
# mount, but a filesystem that went read-only mid-write deserves a real check
# before you trust it with anything.
#
# Nothing here runs until you plug that drive back in.
#
#   bash diagnostics/fsck-orphaned-usb-fs.sh          # report only, no root needed
#   sudo bash diagnostics/fsck-orphaned-usb-fs.sh --go
set -uo pipefail

UUID="${FSCK_UUID:-8563871e-aed4-460a-a1bb-7f3881af2d75}"
GO=0
[[ "${1:-}" == "--go" ]] && GO=1

hdr() { printf '\n\033[1m%s\033[0m\n' "$*"; }

hdr "Looking for $UUID"
DEV="$(blkid -U "$UUID" 2>/dev/null || true)"

if [ -z "$DEV" ]; then
  echo "  not attached."
  echo
  echo "  Nothing to do. Plug the 1 TB drive into the ROG Arion enclosure and"
  echo "  re-run this. Attached block devices right now:"
  lsblk -o NAME,SIZE,FSTYPE,UUID,MOUNTPOINT 2>/dev/null | grep -vE '^loop|squashfs' | sed 's/^/    /'
  exit 0
fi

echo "  found at: $DEV"
echo "  $(lsblk -no NAME,SIZE,FSTYPE,MODEL,TRAN "$DEV" 2>/dev/null | sed 's/^/  /')"

MNT="$(findmnt -no TARGET --source "$DEV" 2>/dev/null || true)"
if [ -n "$MNT" ]; then
  echo
  echo "  It is MOUNTED at $MNT. fsck on a mounted filesystem can destroy it." >&2
  echo "  Unmount it first:  sudo umount $MNT" >&2
  exit 1
fi

hdr "What the superblock says"
if command -v dumpe2fs >/dev/null 2>&1; then
  dumpe2fs -h "$DEV" 2>/dev/null | grep -iE \
    "Filesystem state|Errors behavior|Last checked|Mount count|Last error|Filesystem features" \
    | sed 's/^/  /'
else
  echo "  dumpe2fs not available (e2fsprogs); skipping the read-only summary"
fi

hdr "Check"
if (( ! GO )); then
  echo "  Report only. To actually check and repair:"
  echo
  echo "      sudo bash $0 --go"
  echo
  echo "  That runs:  fsck.ext4 -f -y $DEV"
  echo "  -y answers yes to every repair prompt. If you would rather review"
  echo "  each one, run  sudo fsck.ext4 -f $DEV  by hand instead."
  exit 0
fi

[ "$(id -u)" -eq 0 ] || { echo "  --go needs root" >&2; exit 1; }

echo "  running: fsck.ext4 -f -y $DEV"
fsck.ext4 -f -y "$DEV"
rc=$?

hdr "Result"
# fsck exit codes are a bitmask, not a status: 0 clean, 1 errors corrected,
# 2 corrected + reboot advised, 4 errors left UNcorrected, 8 operational error.
case $rc in
  0) echo "  clean, nothing to correct." ;;
  1) echo "  errors found and corrected. The filesystem is usable again." ;;
  2) echo "  corrected, and fsck advises a reboot." ;;
  *) if (( rc & 4 )); then
       echo "  errors were left UNCORRECTED (exit $rc). Do not trust this"
       echo "  filesystem with new data until that is resolved. Next step:"
       echo "      sudo fsck.ext4 -f $DEV        # answer the prompts by hand"
     else
       echo "  fsck exited $rc — an operational error, not a filesystem verdict."
     fi ;;
esac
echo
echo "  Worth knowing for next time: this enclosure runs in slow BOT mode, not"
echo "  UAS — the kernel's own in-tree quirk for 0b05:1932 does that on"
echo "  purpose (US_FL_IGNORE_UAS, drivers/usb/storage/unusual_uas.h) because"
echo "  of a firmware bug. Unmount before unplugging and none of this recurs."
exit $rc
