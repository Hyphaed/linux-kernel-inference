#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# Read-only diagnostic for Track 1.3 of docs/research/spark-parity-survey.md
# (TLP header-overhead reduction via MPS/MRRS). Reads the GPU's and its
# upstream root port's current MaxPayload/MaxReadReq — the fields that
# decide what fraction of every PCIe transaction is header vs. payload.
#
# Requires root: MaxPayload/MaxReadReq live in extended PCI config space,
# unreadable via unprivileged `lspci`. This is very likely why the
# 2026-07-30 audit recorded in the project notes dismissed MPS as "BIOS/setpci
# runtime state" without checking the actual negotiated value — this
# script is that missing check, non-mutating, read-only.
#
# Usage: sudo ./diagnostics/pcie-mps-mrrs-check.sh [gpu-bdf]
#   gpu-bdf defaults to 01:00.0 (this box's RTX 5070).

set -uo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Needs root (MaxPayload/MaxReadReq are in extended config space," >&2
    echo "not readable unprivileged). Re-run as: sudo $0" >&2
    exit 1
fi

GPU_BDF="${1:-01:00.0}"

sep() { printf '\n========== %s ==========\n' "$1"; }

sep "current kernel cmdline pci= options"
grep -o 'pci=[^ ]*' /proc/cmdline || echo "(none — MPS sits at whatever firmware negotiated; Linux does not touch it by default)"

sep "GPU ($GPU_BDF) DevCtl — MaxPayload/MaxReadReq as currently negotiated"
lspci -vv -s "$GPU_BDF" 2>&1 | grep -A2 "DevCtl:" || echo "(lspci found no DevCtl block for $GPU_BDF)"

sep "GPU DevCap — MaxPayload the device itself supports"
lspci -vv -s "$GPU_BDF" 2>&1 | grep "DevCap:" || echo "(lspci found no DevCap block)"

sep "upstream root port topology (each hop's MPS must be >= the GPU's for the GPU's setting to actually take effect end to end)"
UPSTREAM="$(lspci -t 2>&1 | grep -B2 "${GPU_BDF%%.*}" | head -5)"
echo "$UPSTREAM"
for bdf in $(lspci -D 2>&1 | grep -iE "PCI bridge" | awk '{print $1}'); do
    mps_line="$(lspci -vv -s "$bdf" 2>&1 | grep -A2 "DevCtl:" | grep -i "MaxPayload")"
    [[ -n "$mps_line" ]] && echo "  $bdf: $mps_line"
done

sep "extended tag support (outstanding-request depth — Track 1.4)"
lspci -vv -s "$GPU_BDF" 2>&1 | grep -iE "ExtTag"

sep "interpretation"
echo "Every PCIe TLP carries a 12-16 byte header regardless of payload size."
echo "  MPS=128B  -> ~11-14% of link time is header overhead"
echo "  MPS=256B  -> ~6%"
echo "  MPS=512B  -> ~3%"
echo "If MaxPayload above is well below what DevCap advertises as supported,"
echo "and every hop in the topology agrees on a higher common value, raising"
echo "it (e.g. via the 'pci=pcie_bus_perf' cmdline option — see"
echo "hyphaed/grub.py and docs/research/spark-parity-survey.md Track 1.3)"
echo "is free bandwidth. Verify EVERY bridge in the path agrees first --"
echo "pcie_bus_perf can otherwise regress a device stuck behind a"
echo "smaller-MPS bridge elsewhere in the tree."
