#!/usr/bin/env bash
# Six userspace defects found in the 2026-08-27 boot audit of 7.1.10-hyphaed.
# None of them is a kernel problem, and none of them stops the machine
# working today. Each one prints an error on every boot, and one of them is a
# real trap waiting for the next time you change networks.
#
#   sudo bash diagnostics/fix-boot-userspace-7.1.10.sh        # show what it would do
#   sudo bash diagnostics/fix-boot-userspace-7.1.10.sh --go   # do it
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

# ─────────────────────────────────────────────────────── 1. resolved.conf
# Line 47 is the literal string "nameserver 127.0.2.1" — resolv.conf syntax
# pasted into a systemd unit-config file. systemd-resolved reports
# "/etc/systemd/resolved.conf:47: Missing '=', ignoring line" every boot and
# carries on, so nothing is broken; the line just never did anything.
hdr "1. Remove the resolv.conf line stranded in resolved.conf"
RC=/etc/systemd/resolved.conf
if grep -qE '^[[:space:]]*nameserver[[:space:]]' "$RC" 2>/dev/null; then
  echo "  found: $(grep -nE '^[[:space:]]*nameserver[[:space:]]' "$RC")"
  run cp --no-clobber "$RC" "$RC.bak-$(date +%Y%m%d)"
  if (( GO )); then
    sed -i -E '/^[[:space:]]*nameserver[[:space:]]/d' "$RC" && did "line removed"
  else
    echo "  would run: sed -i '/^nameserver /d' $RC"
  fi
else
  skip "already clean"
fi

# ──────────────────────────────────────────────────────────────── 2. DNS
# The real one. NetworkManager has no `dns=` key, so it falls back to calling
# `resolvconf`. On this box /usr/sbin/resolvconf is a symlink to resolvectl
# (shipped by systemd-resolved, not by the resolvconf package, which is not
# installed). NM calls it with the pseudo-interface name "NetworkManager",
# resolvectl rejects that because it is not a real device, and NM logs:
#
#   resolvconf: Failed to resolve interface "NetworkManager": No such device
#   dns-mgr: could not commit DNS changes: resolvconf failed with status 256
#
# So NetworkManager cannot apply DNS at all. It looks fine right now only
# because /etc/resolv.conf is a hand-made *copy* of systemd-resolved's stub
# file that happens to still be correct. Change networks or join a VPN and
# you will be resolving against stale servers, which presents as "the
# internet is broken on this network", not as a DNS error.
hdr "2. Point NetworkManager at systemd-resolved instead of the resolvconf shim"
NMCONF=/etc/NetworkManager/conf.d/90-dns-systemd-resolved.conf
if [ -f "$NMCONF" ]; then
  skip "already present: $NMCONF"
elif ! systemctl is-active --quiet systemd-resolved; then
  echo "  systemd-resolved is NOT active — not changing DNS backend." >&2
  echo "  Fix that first, or this makes resolution worse, not better." >&2
else
  if (( GO )); then
    printf '[main]\ndns=systemd-resolved\n' > "$NMCONF"
    did "wrote $NMCONF"
  else
    echo "  would write $NMCONF containing: [main] / dns=systemd-resolved"
  fi
fi

# /etc/resolv.conf should be the symlink, not a copy. resolvectl itself is
# the authority on which target to use.
hdr "2b. Restore /etc/resolv.conf as a symlink"
STUB=/run/systemd/resolve/stub-resolv.conf
if [ -L /etc/resolv.conf ]; then
  skip "already a symlink -> $(readlink /etc/resolv.conf)"
elif [ ! -e "$STUB" ]; then
  echo "  $STUB missing — systemd-resolved has not written it. Skipping." >&2
else
  echo "  currently a regular file: $(stat -c '%s bytes, %y' /etc/resolv.conf)"
  run cp --no-clobber /etc/resolv.conf "/etc/resolv.conf.bak-$(date +%Y%m%d)"
  run ln -sf "$STUB" /etc/resolv.conf
fi

if (( GO )); then
  run systemctl reload-or-restart NetworkManager
fi

# ──────────────────────────────────────────────────── 3. cpufrequtils
# Both SysV scripts are enabled. /etc/init.d/cpufrequtils sets
# GOVERNOR="ondemand", and ondemand does not exist under intel_pstate in
# active mode — the only governors are performance and powersave. The script
# fails its own availability check, changes nothing, and costs three
# systemd-sysv-generator deprecation warnings per boot.
hdr "3. Disable cpufrequtils and loadcpufreq (they cannot do anything here)"
echo "  available governors: $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_available_governors 2>/dev/null)"
echo "  script asks for    : $(grep -E '^GOVERNOR=' /etc/init.d/cpufrequtils 2>/dev/null | tail -1)"
for svc in cpufrequtils loadcpufreq; do
  if systemctl is-enabled "$svc" >/dev/null 2>&1; then
    run systemctl disable "$svc"
  else
    skip "$svc already disabled"
  fi
done

# ──────────────────────────────────────────────────────────────── 4. sssd
# Six socket units fail their dependency every boot (sssd-nss, -autofs, -pac,
# -pam, -ssh, -sudo). Nothing on this workstation authenticates against a
# directory service.
hdr "4. Disable sssd (enabled, unconfigured, fails six sockets per boot)"
if systemctl is-enabled sssd >/dev/null 2>&1; then
  run systemctl disable --now sssd
else
  skip "sssd already disabled"
fi

# ─────────────────────────────────────────────────────────── 5. bluetooth
# bluetooth.service declares ConfigurationDirectoryMode=555; the directory is
# 755. systemd warns and carries on.
hdr "5. Match /etc/bluetooth mode to what bluetooth.service declares"
MODE=$(stat -c '%a' /etc/bluetooth 2>/dev/null || echo "")
if [ "$MODE" = "555" ]; then
  skip "already 555"
elif [ -z "$MODE" ]; then
  skip "/etc/bluetooth does not exist"
else
  echo "  currently $MODE, unit wants 555"
  run chmod 555 /etc/bluetooth
fi

# ────────────────────────────────────────────────────────────── 6. nvidia-fs
# The Makefile fix (line 111 passing $(KVER) to create_nv.symvers.sh) is in
# place, but its backup is a 0-byte file written the same minute the DKMS
# build was killed on 2026-08-25. It restores nothing. Worse, the fix itself
# will be silently reverted by the next nvidia-fs package upgrade — this only
# removes the misleading backup and tells you what to re-run.
hdr "6. Remove the useless 0-byte nvidia-fs Makefile backup"
for f in /usr/src/nvidia-fs-*/Makefile.orig; do
  [ -e "$f" ] || continue
  if [ -s "$f" ]; then
    skip "$f has real content — keeping it"
  else
    echo "  $f is 0 bytes ($(stat -c '%y' "$f"))"
    run rm -f "$f"
  fi
done
echo "  reminder: after any nvidia-fs package upgrade, re-run"
echo "            sudo bash diagnostics/fix-nvidia-fs-symvers.sh --go"

# ───────────────────────────────────────────────────────────────── verify
hdr "Where that leaves things"
if (( ! GO )); then
  echo "  dry run — nothing changed."
  exit 0
fi
echo "  system state    : $(systemctl is-system-running 2>&1)"
echo "  failed units    : $(systemctl --failed --no-legend | wc -l)"
echo "  resolv.conf     : $(if [ -L /etc/resolv.conf ]; then echo "symlink -> $(readlink /etc/resolv.conf)"; else echo "regular file"; fi)"
echo "  DNS resolves    : $(getent hosts kernel.org >/dev/null 2>&1 && echo yes || echo NO)"
echo
echo "The six above only stop printing on the NEXT boot — this run fixed the"
echo "state, not the log you already have. To confirm afterwards:"
echo
echo "  journalctl -b -p 4 | grep -E \"Missing '='|resolvconf failed|sssd|SysV service\""
