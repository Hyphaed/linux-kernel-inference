#!/usr/bin/env bash
# Read-only diagnostic: BIOS-controlled settings only visible via dmidecode
# (needs root for raw SMBIOS table access). Confirms memory speed/XMP status,
# which BIOS version is actually flashed, and a few related fields.
# Nothing here writes or changes any setting.
#
# Usage: sudo ./diagnostics/bios-settings-check.sh > bios-settings-output.txt 2>&1

set -uo pipefail
exec < /dev/null

if [[ $EUID -ne 0 ]]; then
    echo "Re-run with sudo: sudo $0" >&2
    exit 1
fi

sep() { printf '\n========== %s ==========\n' "$1"; }

if ! command -v dmidecode >/dev/null 2>&1; then
    PKG_MGR="apt-get"
    command -v nala >/dev/null 2>&1 && PKG_MGR="nala"
    "$PKG_MGR" install -y dmidecode </dev/null >/dev/null 2>&1
fi

sep "BIOS version actually flashed (compare against B760M-ITXD4_BIOS/ ROM versions)"
dmidecode -t bios 2>&1

sep "Memory: configured speed vs rated speed (XMP check)"
dmidecode -t memory 2>&1 | grep -iE "speed|configured voltage|manufacturer|part number|locator|size" | grep -v "No Module Installed"

sep "Memory: full per-DIMM detail"
dmidecode -t memory 2>&1

sep "CPU info from SMBIOS (cross-check against expected i9-14900KF)"
dmidecode -t processor 2>&1 | grep -iE "version|max speed|current speed|voltage|status"

sep "System/baseboard info"
dmidecode -t baseboard 2>&1 | grep -iE "manufacturer|product|version"
dmidecode -t system 2>&1 | grep -iE "manufacturer|product|version"

sep "Current RAPL power limits (cross-reference against earlier 65W/135W finding)"
for f in /sys/class/powercap/intel-rapl:0/constraint_*_power_limit_uw /sys/class/powercap/intel-rapl:0/constraint_*_max_power_uw; do
    [[ -f "$f" ]] && echo "$f: $(cat "$f")"
done

sep "thermald status (still running / still clamping?)"
systemctl is-active thermald 2>&1
systemctl is-enabled thermald 2>&1

sep "Done"
echo "Paste the full output back for analysis."
