#!/usr/bin/env bash
# Run AFTER rebooting into the new kernel. Read-only apart from the doc skeleton it writes.
. "$(dirname "$0")/common.sh"
[[ "$(uname -r)" == "$VER"* ]] || { echo "running $(uname -r), not $VER: reboot into it first"; exit 1; }
D="docs/boot-audit-$VER-$(date +%F).md"
m() { printf '%-34s %s\n' "$1" "$2"; }
{
echo "# Boot audit $VER ($(date +%F))"; echo; echo '```'
m "uname -r"                        "$(uname -r)"
m "taint (7.2.8 baseline 12288)"    "$(cat /proc/sys/kernel/tainted)"
m "failed units"                    "$(systemctl --failed --no-legend | wc -l)"
m "0030: 'retraining failed' lines" "$(journalctl -b -k --no-pager | grep -c 'retraining failed' || true)"
m "0030: 00:1c.0 first/last dmesg"  "$(dmesg | grep -E 'pci 0000:00:1c\.0' | sed -n '1p;$p' | sed 's/\].*//' | tr '\n' ' ')"
m "0029: nvidia-fs numa warnings"   "$(journalctl -b -k --no-pager | grep -c 'error retrieving numa node' || true)"
m "0029: [Firmware Bug] NUMA lines" "$(journalctl -b -k --no-pager | grep -c 'Overriding NUMA node' || true)"
m "nvidia-smi driver"               "$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>&1)"
m "nvidia-fs.ko bytes"              "$(stat -c %s "$(modinfo -n nvidia_fs 2>/dev/null)" 2>/dev/null || echo missing)"
echo; dkms status; echo '```'; echo
echo '## hyphaed verify'; echo '```'; $PY -m hyphaed verify 2>&1 | sed 's/\x1b\[[0-9;]*m//g'; echo '```'
} | tee "$D"
echo "wrote $D"
