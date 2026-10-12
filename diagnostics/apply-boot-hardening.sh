#!/bin/bash
# Apply the five boot-time hardening parameters (hyphaed/grub.py CMDLINE_HARDENING)
# to the GRUB drop-in without rebuilding the kernel. Run as YOURSELF, not root:
#     bash diagnostics/apply-boot-hardening.sh
# hyphaed calls sudo itself. Because the parameters live in compose_cmdline(),
# every future `make install` / `update-cmdline` writes them again.
set -u
cd "$(dirname "$0")/.."
PY=$(command -v python || command -v python3)
[ "$(id -u)" = 0 ] && { echo "run as your user, not root (sudo breaks the python path)"; exit 1; }

echo "== dry run: what would change"
$PY -m hyphaed --dry-run update-cmdline || exit 1
read -r -p "Apply to /etc/default/grub.d/90-hyphaed.cfg and run update-grub? [y/N] " a
[ "$a" = y ] || { echo "aborted, nothing changed"; exit 0; }

$PY -m hyphaed update-cmdline || exit 1

echo "== verify drop-in"
miss=0
for p in page_alloc.shuffle=1 kfence.sample_interval=100 efi=disable_early_pci_dma \
         proc_mem.force_override=never; do
  if grep -q -- "$p" /etc/default/grub.d/90-hyphaed.cfg; then echo "  ok   $p"; else echo "  MISSING $p"; miss=1; fi
done
[ $miss = 0 ] || { echo "drop-in is missing parameters, do not reboot yet"; exit 1; }

cat <<'MSG'

Reboot to take effect. After reboot check:
  cat /proc/cmdline | tr ' ' '\n' | grep -E 'shuffle|kfence|early_pci_dma|force_override'
  sudo dmesg | grep -i -E 'kfence|shuffl'
If something misbehaves, the likeliest causes are:
  proc_mem.force_override=never  (debuggers, Wine/Proton anti-cheat)
To drop one: add the same key with another value to a preset's cmdline_extra
(it overrides CMDLINE_HARDENING), run update-cmdline again.
Rollback without a rebuild: pick the stock -generic entry in GRUB.
MSG
