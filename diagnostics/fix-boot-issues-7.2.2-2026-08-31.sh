#!/usr/bin/env bash
# Everything found in the 2026-08-31 boot audit of 7.2.2-hyphaed that needs
# root to fix. See docs/boot-audit-7.2.2-2026-08-31.md for the full writeup.
#
#   sudo bash diagnostics/fix-boot-issues-7.2.2-2026-08-31.sh        # show what it would do
#   sudo bash diagnostics/fix-boot-issues-7.2.2-2026-08-31.sh --go   # do it
#
# Safe to re-run. Every step checks whether it has already been applied.
set -uo pipefail

GO=0
[[ "${1:-}" == "--go" ]] && GO=1

hdr()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
skip() { printf '  \033[2m· %s\033[0m\n' "$*"; }
did()  { printf '  \033[32m✓\033[0m %s\n' "$*"; }
run() {
  if (( GO )); then printf '  + %s\n' "$*"; "$@"
  else printf '  would run: %s\n' "$*"; fi
}

[ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }
(( GO )) || printf '\033[33mDRY RUN — nothing will change. Re-run with --go.\033[0m\n'

# ═══════════════════════════════════════ 1. nvidia_peermem fails every boot
# nvidia-peermem.c's nv_mem_client_init() only does real work #if defined
# (NV_MLNX_IB_PEER_MEM_SYMBOLS_PRESENT) -- this box has no Mellanox OFED /
# ib_core stack, so that branch is a bare `return -EINVAL`. The module
# cannot succeed here regardless of how it was built; it only matters for
# GPUDirect RDMA, which needs InfiniBand hardware this box doesn't have.
# hyphaed/phases/postinstall.py's _check_dgx_parity_basics() wrote a
# boot-time loader config for it anyway (fixed 2026-08-31, see the repo) --
# this removes what it already wrote on this box before that fix existed.
hdr "1. Stop loading nvidia_peermem at boot (it structurally cannot succeed here)"
if lsmod | grep -q '^nvidia_peermem'; then
  skip "already loaded — IB peer-memory support must be present now, leaving it alone"
elif ! modinfo nvidia-peermem >/dev/null 2>&1; then
  skip "nvidia-peermem isn't built for the running kernel — nothing to remove"
elif modprobe nvidia-peermem 2>/tmp/peermem-probe.$$; then
  echo "  modprobe nvidia-peermem SUCCEEDED just now — that contradicts the" >&2
  echo "  diagnosed always-fails mode. Not removing the boot config." >&2
  rm -f /tmp/peermem-probe.$$
else
  echo "  reproduced: $(cat /tmp/peermem-probe.$$)"
  rm -f /tmp/peermem-probe.$$
  did "confirmed — no IB peer-memory stack on this box, as diagnosed"
  CONF=/etc/modules-load.d/nvidia-peermem.conf
  if [ ! -e "$CONF" ]; then
    skip "$CONF already absent"
  else
    echo "  found: $CONF"
    run rm -f "$CONF"
  fi
fi

# ═══════════════════════════ 2. vmware.service fails every boot as a result
# vmware.service Requires=systemd-modules-load.service, so #1 above took it
# down every boot too. /etc/systemd/system/vmware.service.d/override.conf
# (dated 2026-08-26) already tries to fix exactly this by setting an empty
# `Requires=` to clear the dependency and downgrading to `Wants=` -- verified
# 2026-08-31 that this does NOT work on this systemd version (259): an empty
# Requires= in a *.service.d/ drop-in does not clear a Requires= set in the
# unit's own [Unit] section. Reproduced with a synthetic throwaway unit pair
# to rule out anything vmware/modules-load-specific -- confirmed the same way
# every time. The only mechanism that actually removes the dependency is
# editing the Requires= line out of vmware.service directly (confirmed with
# `SYSTEMD_LOG_LEVEL=debug systemd-analyze verify` before touching the real
# file). vmware.service is not owned by any dpkg package (VMware Workstation
# writes it directly, not via apt), so a direct edit isn't at risk of being
# silently reverted by a package upgrade the way a vendor-shipped unit would
# be -- only by re-running VMware's own installer/vmware-modconfig.
hdr "2. Stop vmware.service's start job from aborting when modules-load fails"
VMSVC=/etc/systemd/system/vmware.service
VMOVERRIDE=/etc/systemd/system/vmware.service.d/override.conf
if [ ! -f "$VMSVC" ]; then
  skip "$VMSVC not present — VMware Workstation isn't installed this way here"
elif ! grep -qE '^Requires=systemd-modules-load\.service\s*$' "$VMSVC"; then
  skip "$VMSVC no longer Requires= modules-load — already fixed"
else
  echo "  found: $(grep -n '^Requires=' "$VMSVC")"
  run cp --no-clobber "$VMSVC" "$VMSVC.bak-$(date +%Y%m%d)"
  if (( GO )); then
    sed -i -e '/^Requires=systemd-modules-load\.service\s*$/d' \
           -e '/^After=network\.target systemd-modules-load\.service\s*$/a Wants=systemd-modules-load.service' \
           "$VMSVC"
    did "Requires= removed, Wants= added — ordering kept, failure no longer propagates"
  else
    echo "  would remove the Requires= line and add Wants=systemd-modules-load.service"
  fi
  if [ -f "$VMOVERRIDE" ]; then
    echo "  $VMOVERRIDE is now redundant (its Requires=/Wants= trick never worked" \
         "and the real fix is now in the main unit file above)"
    run cp --no-clobber "$VMOVERRIDE" "$VMOVERRIDE.bak-$(date +%Y%m%d)"
    run rm -f "$VMOVERRIDE"
  fi
fi

# ═══════════════════════════════════════════════════════ 3. apply immediately
hdr "3. Reload systemd so neither failure repeats on the next boot"
run systemctl daemon-reload
if (( GO )); then
  run systemctl reset-failed systemd-modules-load.service vmware.service 2>/dev/null || true
fi

# ═════════════════════════════ 4. stale initramfs still logs the peermem fail
# Step 1 removes /etc/modules-load.d/nvidia-peermem.conf from the root
# filesystem, but every /boot/initrd.img-* was built BEFORE that removal and
# still carries the old copy -- so the *initramfs* copy of
# systemd-modules-load logs "Failed to find module 'nvidia_peermem'" at
# monotonic ~1.6s on every boot regardless (ENOENT, tolerated, boot stays
# clean -- but it's a red line in `journalctl -p err -b` that shouldn't be
# there). update-initramfs -u rebuilds every installed kernel's initrd from
# the current /etc state in one pass.
hdr "4. Rebuild initramfs so it stops carrying the removed nvidia_peermem.conf"
if ! command -v update-initramfs >/dev/null 2>&1; then
  skip "update-initramfs not found -- not a Debian/Ubuntu initramfs-tools system"
elif ! command -v lsinitramfs >/dev/null 2>&1; then
  skip "lsinitramfs not found -- can't check before acting, skipping"
else
  STALE=0
  for img in /boot/initrd.img-*; do
    [ -f "$img" ] || continue
    if lsinitramfs "$img" 2>/dev/null | grep -qi 'nvidia-peermem\.conf'; then
      echo "  stale reference in: $img"
      STALE=1
    fi
  done
  if (( ! STALE )); then
    skip "no initramfs carries the stale nvidia-peermem.conf reference"
  else
    run update-initramfs -u -k all
    if (( GO )); then
      REMAIN=0
      for img in /boot/initrd.img-*; do
        [ -f "$img" ] || continue
        lsinitramfs "$img" 2>/dev/null | grep -qi 'nvidia-peermem\.conf' && REMAIN=1
      done
      if (( REMAIN )); then
        echo "  WARNING: a stale reference survived the rebuild -- investigate" >&2
      else
        did "rebuilt -- no initramfs carries the stale reference any more"
      fi
    fi
  fi
fi

# ═══════════════════════════ 5. 41 rc (removed, config-files-left) packages
# `dpkg -l` still lists an `rc` package by name -- only the leading status
# letter says it's gone -- and that's exactly what caused the NVIDIA driver
# version in CLAUDE.md to drift to a stale reading three times (see the
# 2026-08-31 correction there). Some of these leave real files behind:
# nvidia-kernel-common-580 and nvidia-kernel-common-595-server still have
# conffiles under /etc/modprobe.d and /lib/modprobe.d, which is exactly
# where module parameters are read from at boot. Purging just removes the
# leftover conffiles and the dpkg-database entry; nothing currently running
# depends on any of them (verified: none show up as reverse-deps of an
# installed package).
hdr "5. Purge removed-but-not-purged (rc) packages"
mapfile -t RC_PKGS < <(dpkg -l 2>/dev/null | awk '$1=="rc"{print $2}')
if [ "${#RC_PKGS[@]}" -eq 0 ]; then
  skip "no rc packages"
else
  echo "  ${#RC_PKGS[@]} rc packages: ${RC_PKGS[*]}"
  run apt-get purge -y "${RC_PKGS[@]}"
fi

# ═══════════════════════ 6. linux-image-7.1.10-hyphaed-dbg (7.61 GB, largest
# package on the system). Debug symbols only for the GRUB fallback kernel --
# does not affect booting 7.1.10, only crash-symbol resolution for it.
# 7.2.2-hyphaed has no -dbg counterpart because patch 0027 made it opt-in
# behind DEB_DEBUG_PKG=1; this is a leftover from before that change.
hdr "6. Remove the 7.61 GB linux-image-7.1.10-hyphaed-dbg package"
if ! dpkg -l linux-image-7.1.10-hyphaed-dbg 2>/dev/null | grep -q '^ii'; then
  skip "not installed"
else
  run apt-get purge -y linux-image-7.1.10-hyphaed-dbg
fi

# ═══════════════ 7. GreenBoost's own sysctl drop-in is stale on this box
# /etc/sysctl.d/99-zzz-greenboost.conf (172 bytes, Aug 27 15:47) predates
# greenboost_setup.sh's current tune_sysctl() output by ~28 minutes and is
# missing vm.overcommit_memory, vm.dirty_writeback_centisecs,
# vm.dirty_expire_centisecs and -- the one with a real behavioural effect --
# vm.max_map_count. Live vm.max_map_count reads 1048576 (the distro default)
# instead of GreenBoost's intended 2147483642; live vm.overcommit_memory
# reads 0 instead of 1. Both matter for mmap-heavy LLM loaders splitting
# large weight files across many VMA segments. This is GreenBoost's own
# script, not hyphaed's -- re-running its tune-sysctl subcommand is the
# correct fix, not hand-editing the conf file here.
hdr "7. Regenerate GreenBoost's stale sysctl drop-in (vm.max_map_count etc.)"
GB_SETUP=/home/ferran/Dev/greenboost_all/greenboost/greenboost_setup.sh
GB_CONF=/etc/sysctl.d/99-zzz-greenboost.conf
if [ ! -x "$GB_SETUP" ]; then
  skip "$GB_SETUP not found or not executable"
elif [ -f "$GB_CONF" ] && grep -q 'vm.max_map_count' "$GB_CONF" 2>/dev/null; then
  skip "$GB_CONF already sets vm.max_map_count -- not stale"
else
  echo "  $GB_CONF is missing vm.max_map_count (and vm.overcommit_memory)"
  run "$GB_SETUP" tune-sysctl
  if (( GO )); then
    live=$(sysctl -n vm.max_map_count 2>/dev/null || echo '?')
    if [ "$live" = "2147483642" ]; then
      did "vm.max_map_count=$live"
    else
      echo "  WARNING: vm.max_map_count=$live after tune-sysctl, expected 2147483642" >&2
    fi
  fi
fi

# ═══════════════════════════════════════════════════════════════════ verify
hdr "Where that leaves things"
if (( ! GO )); then
  echo "  dry run — nothing changed."
  exit 0
fi
echo "  failed units       : $(systemctl --failed --no-legend | wc -l)"
echo "  system state       : $(systemctl is-system-running 2>&1)"
echo "  vmware.service     : $(systemctl is-active vmware.service 2>&1)"
echo "  root free space    : $(df -h / | awk 'NR==2{print $4" free of "$2}')"
echo "  rc packages left   : $(dpkg -l 2>/dev/null | awk '$1=="rc"' | wc -l)"
echo "  vm.max_map_count   : $(sysctl -n vm.max_map_count 2>/dev/null)"
echo
echo "modules-load/initramfs fixes only take full effect on the NEXT boot --"
echo "reboot to fully confirm, or check now with:"
echo
echo "  systemctl --failed"
echo "  systemctl status systemd-modules-load.service vmware.service"
echo "  journalctl -p err -b | grep -i peermem     # should be empty after reboot"
