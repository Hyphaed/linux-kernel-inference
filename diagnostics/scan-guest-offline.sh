#!/bin/bash
# Scan the Windows 11 guest's disk from the host with ClamAV, while the VM is OFF.
#     sudo bash diagnostics/scan-guest-offline.sh            # scan
#     sudo bash diagnostics/scan-guest-offline.sh --keep     # leave it mounted afterwards (read-only)
#
# Why it needs root: attaching a disk image as a block device (qemu-nbd) and mounting it.
# Everything else runs as your user: clamd and the scan itself drop to $SUDO_USER, so ClamAV
# never runs as root against an infected filesystem.
#
# Safety:
#   - refuses to run while the VM is running or its disk lock exists (a live disk + a second
#     reader can corrupt the guest)
#   - the disk is attached READ-ONLY (qemu-nbd -r) and mounted read-only; nothing here writes to it
#   - a trap detaches everything on exit or Ctrl-C
#   - ClamAV is a second opinion. Its coverage of obfuscated PowerShell loaders is weak; a clean
#     result does not prove the guest is clean (Defender already named this loader, ClamAV may not).
# Before shutting the guest down use a FULL shutdown so NTFS is clean (Windows fast-startup leaves
# it half-hibernated):   shutdown /s /full /t 0
set -u
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
U=${SUDO_USER:-ferran}; UID_=$(id -u "$U"); GID_=$(id -g "$U")
VMX="/home/$U/vmware/Windows 11 x64/Windows 11 x64.vmx"
VMDK="${VMX%.vmx}.vmdk"
OUT="/home/$U/Dev/kernel_inference/out/incident-2026-10-08"
MNT=/mnt/w11-offline
NBD=/dev/nbd0
WORK=$(mktemp -d /tmp/clam-offline.XXXXXX); chmod 755 "$WORK"
KEEP=0; [ "${1:-}" = "--keep" ] && KEEP=1
mkdir -p "$OUT"; chown "$U": "$OUT"

echo "== preflight"
command -v clamd >/dev/null && command -v clamdscan >/dev/null || { echo "clamav missing: sudo apt install clamav clamav-daemon"; exit 1; }
command -v qemu-nbd >/dev/null || { echo "qemu-nbd missing"; exit 1; }
[ -f "$VMDK" ] || { echo "disk not found: $VMDK"; exit 1; }
if su - "$U" -c 'vmrun list' 2>/dev/null | grep -q 'Windows 11 x64.vmx'; then echo "the VM is still running, shut it down first (shutdown /s /full /t 0)"; exit 1; fi
if ls -d "${VMDK}.lck" "${VMX}.lck" >/dev/null 2>&1; then echo "lock files present, VM not fully closed: $(ls -d "${VMDK}.lck" "${VMX}.lck" 2>/dev/null | tr '\n' ' ')"; exit 1; fi
if [ ! -e /var/lib/clamav/main.cvd ] && [ ! -e /var/lib/clamav/main.cld ]; then echo "no ClamAV signatures yet: sudo freshclam"; exit 1; fi
echo "  ok: VM off, disk $(du -h --apparent-size "$VMDK" | cut -f1), signatures present"

cleanup() {
  echo "== cleanup"
  [ -n "${CLAMD_PID:-}" ] && kill "$CLAMD_PID" 2>/dev/null
  if [ $KEEP = 0 ]; then
    mountpoint -q "$MNT" && umount "$MNT"
    qemu-nbd -d "$NBD" >/dev/null 2>&1
    rmdir "$MNT" 2>/dev/null
    echo "  detached (disk untouched)"
  else
    echo "  left mounted read-only at $MNT ; detach later with: sudo umount $MNT && sudo qemu-nbd -d $NBD"
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT INT TERM

echo "== attach disk read-only"
modprobe nbd max_part=16 || { echo "cannot load nbd module"; exit 1; }
qemu-nbd -d "$NBD" >/dev/null 2>&1
qemu-nbd -r -c "$NBD" "$VMDK" || { echo "qemu-nbd failed"; exit 1; }
for i in $(seq 1 20); do [ -b "${NBD}p1" ] && break; sleep 0.5; done
lsblk -rno NAME,SIZE,FSTYPE "$NBD" | sed 's/^/  /'

# The Windows system volume is the largest NTFS partition. lsblk's FSTYPE column is empty for nbd
# devices (no udev probe), so ask blkid to read each partition's superblock directly.
PART=""; BIG=0
for p in "${NBD}"p*; do
  [ -b "$p" ] || continue
  T=$(blkid -p -o value -s TYPE "$p" 2>/dev/null); S=$(blockdev --getsize64 "$p")
  echo "  $p  $((S/1024/1024/1024)) GB  fs=${T:-unknown}"
  case "$T" in
    BitLocker) echo "  $p is BitLocker-encrypted, ClamAV cannot read it from the host"; ;;
    ntfs) [ "$S" -gt "$BIG" ] && { BIG=$S; PART=$p; } ;;
  esac
done
[ -n "$PART" ] || { echo "no NTFS partition found"; exit 1; }
echo "  scanning partition: $PART"
mkdir -p "$MNT"
mount -t ntfs3 -o ro,uid="$UID_",gid="$GID_",umask=0222 "$PART" "$MNT" 2>/dev/null \
  || mount -t ntfs-3g -o ro,norecover,uid="$UID_",gid="$GID_",umask=0222 "$PART" "$MNT" \
  || { echo "mount failed. If it says the volume is hibernated, boot the guest and run: shutdown /s /full /t 0"; exit 1; }
ls "$MNT" | head -12 | sed 's/^/  /'
[ -d "$MNT/Windows" ] || { echo "no Windows folder on $PART, wrong partition?"; exit 1; }

echo "== start clamd as $U (all cores, own socket)"
cat > "$WORK/clamd.conf" <<EOF
DatabaseDirectory /var/lib/clamav
LocalSocket $WORK/clamd.sock
LocalSocketMode 666
Foreground yes
MaxThreads $(nproc)
MaxScanSize 2000M
MaxFileSize 1000M
MaxRecursion 16
LogFile $WORK/clamd.log
LogTime yes
ExcludePath ^$MNT/pagefile.sys
ExcludePath ^$MNT/hiberfil.sys
ExcludePath ^$MNT/swapfile.sys
ExcludePath ^$MNT/System Volume Information
EOF
chown "$U": "$WORK/clamd.conf" "$WORK"
sudo -u "$U" clamd -c "$WORK/clamd.conf" >"$WORK/clamd.out" 2>&1 &
CLAMD_PID=$!
for i in $(seq 1 120); do [ -S "$WORK/clamd.sock" ] && break; kill -0 "$CLAMD_PID" 2>/dev/null || { echo "clamd died:"; tail -5 "$WORK/clamd.out"; exit 1; }; sleep 2; done
[ -S "$WORK/clamd.sock" ] || { echo "clamd did not come up"; exit 1; }
echo "  clamd ready"

LOG="$OUT/clamav-offline-$(date +%Y%m%d-%H%M).log"
echo "== scanning (log: $LOG)  started $(date +%T)"
sudo -u "$U" clamdscan -c "$WORK/clamd.conf" --multiscan --fdpass --infected "$MNT" 2>&1 | tee "$LOG" | grep -E 'FOUND|ERROR|SCAN SUMMARY|Infected files|Scanned files|Data scanned|Time:'
RC=${PIPESTATUS[0]}
chown "$U": "$LOG"
echo "== finished $(date +%T)  clamdscan exit=$RC  (0 clean, 1 found something, 2 errors)"
echo "Every FOUND line above names a file and a signature; look each one up before deleting anything."
