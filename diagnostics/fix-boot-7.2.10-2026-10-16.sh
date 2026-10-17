#!/usr/bin/env bash
# Boot-log fixes from the 7.2.10-hyphaed boot of 2026-10-16.
#
#   sudo bash diagnostics/fix-boot-7.2.10-2026-10-16.sh
#
# 1. nvidia_fs would not load ("disagrees about version of symbol
#    nvidia_p2p_*"). nvidia-fs's Makefile resolves nvidia's symbol CRCs for the
#    RUNNING kernel (7.2.9) instead of the target (7.2.10). The existing fixer
#    patches the Makefile once and rebuilds; the patch also covers the 7.2.11
#    DKMS autoinstall, so run this BEFORE `make install` of 7.2.11.
# 2. /etc/fstab lists /swap.img twice (plain, and with pri=10), so
#    systemd-fstab-generator exits 1 on every boot. Keep the pri=10 line.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== 1. nvidia-fs symvers"
bash "$HERE/fix-nvidia-fs-symvers.sh"

echo "== 2. duplicate /swap.img in fstab"
n=$(grep -cE '^[[:space:]]*/swap\.img[[:space:]]' /etc/fstab || true)
if [ "$n" -gt 1 ]; then
    cp -n /etc/fstab /etc/fstab.bak-2026-10-16
    # drop the entry that has no priority option, keep the pri=10 one
    sed -i -E '/^[[:space:]]*\/swap\.img[[:space:]]+none[[:space:]]+swap[[:space:]]+sw[[:space:]]+0[[:space:]]+0[[:space:]]*$/d' /etc/fstab
    systemctl daemon-reload
    echo "removed duplicate; backup at /etc/fstab.bak-2026-10-16"
else
    echo "only $n /swap.img entry, nothing to do"
fi
grep -n swap /etc/fstab
mkdir -p /tmp/fstab-gen-test
/usr/lib/systemd/system-generators/systemd-fstab-generator /tmp/fstab-gen-test /tmp/fstab-gen-test /tmp/fstab-gen-test \
    && echo "fstab generator: clean" || echo "fstab generator still failing" >&2
rm -rf /tmp/fstab-gen-test

echo "== done. Verify: systemctl --failed ; lsmod | grep nvidia_fs"
