#!/usr/bin/env bash
# Install optional tools that improve hardware topology detection quality.
# Run as root or via sudo: sudo bash install-topology-deps.sh
set -euo pipefail

echo "==> Updating package lists..."
apt-get update -q

echo "==> Installing hardware topology inspection tools..."
apt-get install -y \
    dmidecode \
    hwloc \
    lshw \
    pciutils \
    usbutils \
    inxi \
    cpuid \
    cpufrequtils \
    linux-tools-common \
    lm-sensors \
    nvme-cli \
    mokutil

echo ""
echo "==> All tools installed."
echo ""
echo "Useful commands after install:"
echo "  sudo dmidecode -t chassis   -- chassis type (laptop=9/10, desktop=3)"
echo "  lstopo --no-io              -- CPU core/NUMA topology (requires hwloc)"
echo "  lshw -short                 -- full hardware inventory"
echo "  sudo sensors-detect         -- detect thermal sensors (one-time setup)"
echo "  sensors                     -- thermal/voltage readout"
echo "  sudo turbostat --interval 1 -- per-core frequency and C-state residency"
echo "  cpupower frequency-info     -- current scaling driver and governor"
echo "  nvme id-ctrl /dev/nvme0     -- NVMe SSD host memory buffer capabilities"
