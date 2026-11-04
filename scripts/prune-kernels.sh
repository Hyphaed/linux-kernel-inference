#!/usr/bin/env bash
# Keep exactly two kernels: the hyphaed build in use and the newest stock -generic.
# Everything else (packages, rc leftovers, /boot files, /lib/modules, /usr/src
# headers, DKMS builds for dead kernels) goes. Dry run by default.
#
#   scripts/prune-kernels.sh            # print what would be removed (no root needed)
#   sudo scripts/prune-kernels.sh --apply
#
# Override what is kept:  KEEP_HYPHAED=7.2.14-hyphaed KEEP_GENERIC=7.0.0-42-generic
set -euo pipefail

KEEP_HYPHAED="${KEEP_HYPHAED:-7.2.14-hyphaed}"
KEEP_GENERIC="${KEEP_GENERIC:-7.0.0-42-generic}"
KEEP_GENERIC_BASE="${KEEP_GENERIC%-generic}"        # 7.0.0-42 (the arch-all headers/tools pkgs)
APPLY=0; [ "${1:-}" = "--apply" ] && APPLY=1
RUNNING="$(uname -r)"
DKMS_STATUS="$(dkms status 2>/dev/null || true)"     # captured once: grep -q on a pipe + pipefail lies

say()  { printf '%s\n' "$*"; }
run()  { if [ "$APPLY" = 1 ]; then say "+ $*"; "$@"; else say "would: $*"; fi; }

# ---- guards -----------------------------------------------------------
[ "$RUNNING" = "$KEEP_HYPHAED" ] || [ "$RUNNING" = "$KEEP_GENERIC" ] || {
  say "refusing: running kernel $RUNNING is not one of the two to keep"; exit 1; }
for v in "$KEEP_HYPHAED" "$KEEP_GENERIC"; do
  [ -f "/boot/vmlinuz-$v" ] && [ -d "/lib/modules/$v" ] || { say "refusing: $v is not fully installed"; exit 1; }
done
if [ "$APPLY" = 1 ] && [ "$(id -u)" -ne 0 ]; then say "run with sudo for --apply"; exit 1; fi
if [ "$APPLY" = 1 ] && pgrep -x 'dpkg|apt|apt-get|dkms' >/dev/null 2>&1; then
  say "refusing: a package/DKMS job is running"; exit 1
fi
if [ "$APPLY" = 1 ] && pgrep -f 'bump/40-build|hyphaed --phase' >/dev/null 2>&1; then
  say "refusing: a kernel build is running"; exit 1
fi

is_kept() {  # $1 = package or version string
  case "$1" in
    *"$KEEP_HYPHAED"*|*"$KEEP_GENERIC"*) return 0 ;;
    "$KEEP_GENERIC_BASE"|"$KEEP_GENERIC_BASE"-*|*"-$KEEP_GENERIC_BASE"|*"-$KEEP_GENERIC_BASE-"*) return 0 ;;
  esac; return 1
}

# ---- 1. packages (installed and rc) -----------------------------------
mapfile -t PKGS < <(dpkg-query -W -f='${Package}\n' \
  'linux-image-*' 'linux-image-unsigned-*' 'linux-headers-*' 'linux-modules-*' 'linux-modules-extra-*' \
  'linux-tools-*' 'linux-cloud-tools-*' 'linux-buildinfo-*' 'linux-objects-nvidia-*' \
  'linux-signatures-nvidia-*' 'linux-hwe-*-headers-*' 2>/dev/null | sort -u)
PURGE=()
for p in "${PKGS[@]}"; do
  # versioned (7.x.y-N...) or an -aws metapackage; unversioned generic metapackages stay
  if [[ "$p" =~ [0-9]+\.[0-9]+\.[0-9]+-[A-Za-z0-9]+ || "$p" == *-aws ]]; then
    is_kept "$p" || PURGE+=("$p")
  fi
done
say "== packages to purge (${#PURGE[@]}):"; printf '   %s\n' "${PURGE[@]:-}"

# nvidia-fs-dkms needs "nvidia-dkms-kernel | nvidia-prebuilt-kernel | ..." and on this box the only thing
# satisfying it is the AWS prebuilt-nvidia metapackage. The real driver (615.x open) is built by the wizard
# outside apt. A tiny local package that Provides it lets the AWS kernel go without removing nvidia-fs.
if ! dpkg -s hyphaed-nvidia-provides >/dev/null 2>&1 && printf '%s\n' "${PURGE[@]:-}" | grep -q 'nvidia-595-aws'; then
  say "== local provider for nvidia-dkms-kernel (so nvidia-fs-dkms survives the AWS purge)"
  if [ "$APPLY" = 1 ]; then
    T="$(mktemp -d)"; mkdir -p "$T/pkg/DEBIAN"
    cat >"$T/pkg/DEBIAN/control" <<'EOF'
Package: hyphaed-nvidia-provides
Version: 1.0
Architecture: all
Maintainer: local <root@localhost>
Provides: nvidia-dkms-kernel
Description: satisfies nvidia-dkms-kernel; the NVIDIA driver is built outside apt by hyphaed
EOF
    dpkg-deb --build "$T/pkg" "$T/hyphaed-nvidia-provides.deb" >/dev/null
    dpkg -i "$T/hyphaed-nvidia-provides.deb"; rm -rf "$T"
  else
    say "would: build and dpkg -i hyphaed-nvidia-provides (Provides: nvidia-dkms-kernel)"
  fi
fi
[ ${#PURGE[@]} -gt 0 ] && run dpkg --purge "${PURGE[@]}"

# ---- 2. DKMS builds for kernels that are going away -------------------
say "== DKMS entries for dead kernels:"
while IFS= read -r line; do
  # "mod/ver, kernel, arch: state"
  mv="${line%%,*}"; rest="${line#*, }"; kv="${rest%%,*}"
  [ -n "$kv" ] || continue
  is_kept "$kv" && continue
  [ "$kv" = "$RUNNING" ] && continue
  say "   $mv  $kv"
  run dkms remove "$mv" -k "$kv" 2>/dev/null || true
done <<<"$DKMS_STATUS"

# ---- 3. /boot residues --------------------------------------------------
say "== /boot files for dead kernels:"
for f in /boot/vmlinuz-* /boot/initrd.img-* /boot/System.map-* /boot/config-* /boot/vmlinuz-*.dpkg-* ; do
  [ -e "$f" ] || continue
  v="${f#/boot/*-}"; v="${v%%.dpkg-*}"
  is_kept "$v" && continue
  say "   $f"; run rm -f -- "$f"
done
for l in /boot/vmlinuz /boot/vmlinuz.old /boot/initrd.img /boot/initrd.img.old; do   # dangling symlinks
  [ -L "$l" ] && [ ! -e "$l" ] && { say "   dangling $l"; run rm -f -- "$l"; }
done

# ---- 4. /lib/modules and /usr/src leftovers ---------------------------
say "== /lib/modules and /usr/src leftovers:"
for d in /lib/modules/*; do
  v="${d##*/}"; is_kept "$v" && continue; [ "$v" = "$RUNNING" ] && continue
  say "   $d"; run rm -rf -- "$d"
done
for d in /usr/src/linux-headers-*; do
  [ -e "$d" ] || continue
  v="${d##*/linux-headers-}"; is_kept "$v" && continue
  say "   $d"; run rm -rf -- "$d"
done
# DKMS source trees nothing registers and no package owns (older nvidia-fs / xpadneo revisions)
for d in /usr/src/nvidia-fs-* /usr/src/hid-xpadneo-*; do
  [ -d "$d" ] || continue
  n="${d##*/}"; ver="${n#nvidia-fs-}"; [ "$n" = "$ver" ] && ver="${n#hid-xpadneo-}"
  grep -qF "/$ver," <<<"$DKMS_STATUS" && continue
  dpkg -S "$d" >/dev/null 2>&1 && continue
  say "   $d (not registered, not packaged)"; run rm -rf -- "$d"
done
say "== other kernels' DKMS state dirs:"
for k in /var/lib/dkms/*/kernel-*; do
  [ -e "$k" ] || continue
  v="$(basename "$k")"; v="${v#kernel-}"; v="${v%-x86_64}"
  is_kept "$v" && continue
  say "   $k"; run rm -rf -- "$k"
done

# ---- 5. bootloader + dependency cleanup ------------------------------
run update-grub
[ "$APPLY" = 1 ] && { say "== apt would now autoremove (review, not run):"; apt-get -s autoremove --purge | grep '^Remv' || true; }
say; say "kept: $KEEP_HYPHAED and $KEEP_GENERIC (running: $RUNNING)"
[ "$APPLY" = 1 ] || say "dry run only. Apply with: sudo $0 --apply"
