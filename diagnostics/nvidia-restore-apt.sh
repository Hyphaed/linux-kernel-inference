#!/usr/bin/env bash
# Undo the "nvidia" phase: remove the from-source modules and reinstall the apt
# packages recorded in state/machines/<host>/nvidia-rollback.txt.
#   sudo bash diagnostics/nvidia-restore-apt.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LIST="$ROOT/state/machines/$(hostname)/nvidia-rollback.txt"
[ -s "$LIST" ] || { echo "no rollback list at $LIST; nothing to restore" >&2; exit 1; }
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
[ -x /usr/bin/nvidia-uninstall ] && nvidia-uninstall --silent || true
rm -f /etc/depmod.d/hyphaed-nvidia.conf
rm -rf /lib/modules/*/extra/nvidia-hyphaed
apt-get install -y --allow-downgrades $(cat "$LIST")
apt-mark hold $(grep -o '^nvidia-driver-pinning[^=]*' "$LIST") 2>/dev/null || true
depmod -a
echo "restored apt NVIDIA packages from $LIST. Reboot to load them."
