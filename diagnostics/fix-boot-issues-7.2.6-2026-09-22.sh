#!/usr/bin/env bash
# Four defects found in the 2026-09-22 boot audit of 7.2.6-hyphaed, verified
# with real diagnostics (udevadm verify, dpkg -S, package status) rather
# than guessed from the warning text alone. None of them is a kernel
# problem. One is hyphaed's own bug; three are pre-existing local config.
#
#   sudo bash diagnostics/fix-boot-issues-7.2.7-2026-09-22.sh        # dry run
#   sudo bash diagnostics/fix-boot-issues-7.2.7-2026-09-22.sh --go   # apply
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

# ──────────────────────────────────────── 1. nvidia-pci-bridge-power.service
# hyphaed's own bug: the ExecStart= line embedded `awk "{print \$1}"` and a
# bare `$b` inside a systemd unit value. systemd's unit-file parser does its
# own backslash-escape and $VAR expansion on ExecStart= BEFORE the shell
# ever runs — `\$1` isn't a systemd escape sequence (hence "Ignoring
# unknown escape sequences" every boot), and the code fix
# (hyphaed/phases/postinstall.py, this same commit) moves the shell logic
# to a real script file so a future build's postinstall never reinstalls
# the broken version. This step re-installs the CURRENTLY LIVE unit with
# the corrected form, since the code fix alone only affects future builds.
hdr "1. Fix nvidia-pci-bridge-power.service's unparseable ExecStart="
UNIT=/etc/systemd/system/nvidia-pci-bridge-power.service
SCRIPT=/usr/local/sbin/hyphaed-pci-bridge-power-on.sh
if [ -f "$UNIT" ] && grep -q '\\\$1' "$UNIT" 2>/dev/null; then
  echo "  current ExecStart=: $(grep '^ExecStart=' "$UNIT")"
  if (( GO )); then
    cat > "$SCRIPT" <<'SCRIPT_EOF'
#!/bin/sh
# Written by hyphaed's postinstall phase. Forces PCIe bridge
# power/control to 'on' so bridges don't power-down (DGX-parity port).
for b in $(lspci -D -d ::0604 | awk '{print $1}'); do
    echo on > "/sys/bus/pci/devices/$b/power/control" 2>/dev/null || true
done
SCRIPT_EOF
    chmod 755 "$SCRIPT"
    sed -i "s|^ExecStart=.*|ExecStart=$SCRIPT|" "$UNIT"
    run systemctl daemon-reload
    did "rewrote $UNIT to call $SCRIPT (no more inline shell in the unit file)"
  else
    echo "  would write $SCRIPT and rewrite ExecStart= to call it"
  fi
else
  skip "$UNIT already fixed or not installed"
fi

# ──────────────────────────────────────── 2. esd-s1c-ssd.rules: DEVTYPE key
# `udevadm verify` (not just the boot-log warning text) confirms the real
# defect: this systemd (259) requires ENV{DEVTYPE}=="..." to match the
# per-device environment property — a bare DEVTYPE=="..." match key is
# rejected as unknown. Every shipped rule in /usr/lib/udev/rules.d/ that
# matches DEVTYPE uses the ENV{} form; none uses the bare form. This is
# NOT this box's file to silently rewrite without asking — it's the
# user's own personal rule (see the file's own "Installed 2026-09-21"
# header) — but the fix is a single token, verified correct against real
# udev rule syntax rather than guessed.
hdr "2. Fix 99-esd-s1c-ssd.rules: DEVTYPE match key needs the ENV{} form"
ESD=/etc/udev/rules.d/99-esd-s1c-ssd.rules
if [ -f "$ESD" ] && grep -q '^ACTION=="add", SUBSYSTEM=="usb", DEVTYPE==' "$ESD"; then
  echo "  before: $(grep -n 'DEVTYPE==' "$ESD")"
  run cp --no-clobber "$ESD" "$ESD.bak-$(date +%Y%m%d)"
  if (( GO )); then
    sed -i 's/DEVTYPE=="usb_device"/ENV{DEVTYPE}=="usb_device"/' "$ESD"
    if udevadm verify "$ESD" >/tmp/udev-verify-esd.$$ 2>&1; then
      did "fixed and verified clean: udevadm verify $ESD"
    else
      echo "  udevadm verify still unhappy — see /tmp/udev-verify-esd.$$:"
      cat /tmp/udev-verify-esd.$$
    fi
    run udevadm control --reload-rules
  else
    echo "  would change DEVTYPE==\"usb_device\" -> ENV{DEVTYPE}==\"usb_device\""
  fi
else
  skip "$ESD already fixed, or the line this script targets is gone — check manually"
fi

# ──────────────────────────────────────── 3. xpadneo GOTO/LABEL split across two files
# `udevadm verify` on BOTH files together (not just the one with the GOTO)
# still reports "no matching label" — GOTO/LABEL resolution is per-FILE in
# this systemd, not a single stream across /etc/udev/rules.d/, despite
# looking like one at a glance. The GOTO in 59-xpadneo-hidraw-preempt.rules
# can never reach the LABEL that lives in 70-xpadneo-disable-hidraw.rules.
# Fix: merge both files' content into the earlier-sorting filename (59-),
# so GOTO and its LABEL are co-located, then remove the now-empty 70- file.
# This changes real controller-input ACL behavior — TEST WITH THE ACTUAL
# CONTROLLER after applying, this script does not verify that part.
hdr "3. Merge xpadneo hidraw rules so GOTO/LABEL are in the same file"
X59=/etc/udev/rules.d/59-xpadneo-hidraw-preempt.rules
X70=/etc/udev/rules.d/70-xpadneo-disable-hidraw.rules
if [ -f "$X59" ] && [ -f "$X70" ] && grep -q 'GOTO="xpadneo_hidraw_lockdown"' "$X59" \
   && ! grep -q 'LABEL="xpadneo_hidraw_lockdown"' "$X59"; then
  echo "  before: udevadm verify reports \"has no matching label\" for $X59"
  run cp --no-clobber "$X59" "$X59.bak-$(date +%Y%m%d)"
  run cp --no-clobber "$X70" "$X70.bak-$(date +%Y%m%d)"
  if (( GO )); then
    { cat "$X59"; echo; cat "$X70"; } > "$X59.merged"
    mv "$X59.merged" "$X59"
    rm -f "$X70"
    if udevadm verify "$X59" >/tmp/udev-verify-xpadneo.$$ 2>&1; then
      did "merged into $X59 and verified clean; removed $X70"
    else
      echo "  udevadm verify still unhappy after merge — see /tmp/udev-verify-xpadneo.$$:"
      cat /tmp/udev-verify-xpadneo.$$
    fi
    run udevadm control --reload-rules
    echo "  NOW TEST: reconnect the xpadneo controller and confirm input still works"
  else
    echo "  would merge $X70 into $X59 (GOTO/LABEL need to share a file) and remove $X70"
  fi
else
  skip "already merged, or the rules this script targets have changed — check manually"
fi

# ──────────────────────────────────────── 4. cpufrequtils/loadcpufreq: purge, not just disable
# Both scripts are already `disabled` (confirmed: systemctl is-enabled).
# Disabling stops them from RUNNING but does not stop
# systemd-sysv-generator from SCANNING /etc/init.d/ every boot and
# printing "lacks a native systemd unit file" for each one it finds —
# that scan is unconditional. Neither script can do anything under
# intel_pstate=active anyway (their target governor, "ondemand", doesn't
# exist under intel_pstate — the 7.1.10 fix already established this).
# The only way to stop the generator noise is to remove the package.
hdr "4. Purge cpufrequtils package (scripts are dead weight, not just disabled)"
if dpkg -s cpufrequtils >/dev/null 2>&1; then
  echo "  owns: $(dpkg -S /etc/init.d/cpufrequtils /etc/init.d/loadcpufreq 2>/dev/null | cut -d: -f1 | sort -u | tr '\n' ' ')"
  run apt-get purge -y cpufrequtils
else
  skip "cpufrequtils package already removed"
fi

# ──────────────────────────────────────── summary
hdr "Where that leaves things"
if (( ! GO )); then
  echo "  dry run — nothing changed."
  exit 0
fi
echo "  nvidia-pci-bridge-power.service: $(systemctl is-active nvidia-pci-bridge-power.service 2>&1)"
echo "  esd udev rule verifies         : $(udevadm verify "$ESD" >/dev/null 2>&1 && echo clean || echo 'still failing')"
echo "  xpadneo udev rule verifies     : $(udevadm verify "$X59" >/dev/null 2>&1 && echo clean || echo 'still failing')"
echo "  cpufrequtils package           : $(dpkg -s cpufrequtils >/dev/null 2>&1 && echo 'still installed' || echo purged)"
echo
echo "These fixes take effect on udev/systemd reload for items 1/2/4; item 3"
echo "(xpadneo) needs the controller reconnected once to confirm input still"
echo "works. All four stop printing on the NEXT boot — this run fixed the"
echo "state, not the log you already have. To confirm afterwards:"
echo
echo "  journalctl -b -p 4 | grep -E \"Ignoring unknown escape|Invalid key|no matching label|lacks a native systemd unit file.*cpufreq\""
