#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Ferran Duarri.
#
# Run with: sudo bash diagnose_and_fix.sh
#
# Two things, in order, matching README.md's "exact next command" section:
#   1. READ-ONLY diagnostic: lspci -vv LnkCtl2 on the GPU's root port
#      (0000:00:01.0), the one piece of state that disambiguates which of
#      the two failure modes this is (see EXPLAINER.md Part 3). No change
#      to the system from this step.
#   2. THE FIX: retrain the GPU's PCIe link to full speed via the standard
#      thermal-cooling-device interface (drivers/thermal/pcie_cooling.c,
#      already in the running kernel, no patch needed). Fully reversible,
#      standard kernel interface, not GreenBoost-specific.
#
# Safe to re-run. Does not touch any other device or setting.

set -uo pipefail

PORT="0000:00:01.0"
COOLDEV="/sys/class/thermal/cooling_device0"

if [[ $EUID -ne 0 ]]; then
    echo "This script needs root (reads PCI extended config space, writes a" >&2
    echo "thermal cooling-device sysfs file). Re-run as: sudo bash $0" >&2
    exit 1
fi

echo "=================================================================="
echo "STEP 1/3: BEFORE state (read-only)"
echo "=================================================================="
echo
echo "-- cooling_device0 (should be $PORT's PCIe link speed device) --"
if [[ -d "$COOLDEV" ]]; then
    echo "type:      $(cat "$COOLDEV/type" 2>/dev/null)"
    echo "cur_state: $(cat "$COOLDEV/cur_state" 2>/dev/null)"
    echo "max_state: $(cat "$COOLDEV/max_state" 2>/dev/null)"
else
    echo "NOT FOUND at $COOLDEV -- check 'ls /sys/class/thermal/' for the" >&2
    echo "correct cooling_deviceN for $PORT (type should be" >&2
    echo "PCIe_Port_Link_Speed_${PORT})" >&2
fi
echo
echo "-- live negotiated link speed (LnkSta, via sysfs) --"
echo "current_link_speed: $(cat /sys/bus/pci/devices/$PORT/current_link_speed 2>/dev/null)"
echo "max_link_speed:     $(cat /sys/bus/pci/devices/$PORT/max_link_speed 2>/dev/null)"
echo
echo "-- lspci -vv LnkCtl2 (the diagnostic README.md/EXPLAINER.md need) --"
echo "This is the field that tells us which of the two failure modes this"
echo "is: does the Target Link Speed already say 16GT/s (cheap read-side"
echo "cache-staleness fix) or something lower (a different, more"
echo "interesting bug about who re-requests full speed)?"
echo
lspci -vv -s "${PORT#0000:}" 2>&1 | grep -A3 "LnkCtl2" || \
    echo "(lspci found no LnkCtl2 block -- unexpected, save the full lspci -vv output instead)"
echo
lspci -vv -s "${PORT#0000:}" > /tmp/gb_lspci_before.txt 2>&1
echo "Full 'lspci -vv' output for this port saved to /tmp/gb_lspci_before.txt"
echo "(for pasting back if the LnkCtl2 grep above didn't show what's needed)"
echo

echo "=================================================================="
echo "STEP 2/3: APPLYING THE FIX"
echo "=================================================================="
echo
echo "Writing cur_state=0 to $COOLDEV/cur_state (requests the port's"
echo "maximum PCIe speed via the standard kernel thermal-cooling API,"
echo "pcie_cooling_set_cur_level() -> pcie_set_target_speed() ->"
echo "PCI_EXP_LNKCTL2_TLS write + retrain). Reversible: this is a normal,"
echo "documented cooling-device control, not a hack."
echo
if [[ ! -e "$COOLDEV/cur_state" ]]; then
    echo "$COOLDEV/cur_state not found, skipping the fix." >&2
elif echo 0 > "$COOLDEV/cur_state"; then
    echo "Write succeeded."
else
    echo "Write FAILED. See dmesg for details:" >&2
    dmesg | tail -20 >&2
fi
echo
echo "Waiting 2s for the link to retrain..."
sleep 2

echo
echo "=================================================================="
echo "STEP 3/3: AFTER state"
echo "=================================================================="
echo
echo "-- cooling_device0 --"
echo "cur_state: $(cat "$COOLDEV/cur_state" 2>/dev/null)  (expect 0 now)"
echo
echo "-- live negotiated link speed --"
echo "current_link_speed: $(cat /sys/bus/pci/devices/$PORT/current_link_speed 2>/dev/null)"
echo
echo "-- lspci -vv LnkCtl2 (after) --"
lspci -vv -s "${PORT#0000:}" 2>&1 | grep -A3 "LnkCtl2" || true
lspci -vv -s "${PORT#0000:}" > /tmp/gb_lspci_after.txt 2>&1
echo "Full 'lspci -vv' output saved to /tmp/gb_lspci_after.txt"
echo

echo "=================================================================="
echo "SUMMARY"
echo "=================================================================="
echo
echo "If cur_state above still doesn't read back 0, that CONFIRMS the"
echo "worse failure mode from README.md: pcie_set_target_speed()'s early"
echo "return (bus->cur_bus_speed == speed_req) means the cache never gets"
echo "corrected by a normal write, because it already (wrongly) claims to"
echo "be at the requested speed. That is the strongest possible grounds"
echo "for the upstream bwctrl fix described in EXPLAINER.md Part 3."
echo
echo "Please paste this script's full output back for the writeup and"
echo "kernel-patch decision (which of the two fix shapes to write, if any)."
