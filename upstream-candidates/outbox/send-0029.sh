#!/usr/bin/env bash
# Send 0029 (x86/pci single-node root bus node) as an RFC PATCH.
#
#   ./send-0029.sh              # dry run, prints exactly what would go
#   ./send-0029.sh --send       # actually send (asks to confirm first)
#
# iCloud needs an APP-SPECIFIC password. Export GB_SMTP_PASS or let git prompt.
# The RFC prefix and the reviewer note below the '---' are added to a temp
# copy only; the committed patch and its changelog are untouched.

set -euo pipefail
cd "$(dirname "$0")"

SRC="0029-x86-pci-single-node-root-bus-node.patch"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
PATCH="$TMP/$SRC"

python3 - "$SRC" "$PATCH" <<'PY'
import sys
src, dst = sys.argv[1:]
s = open(src).read()
note = """---
Only user I've got is the out-of-tree nvidia-fs, hence the RFC. The
single-node condition is the same reasoning as Bjorn's 2019 "PCI: Warn
about host bridge device when its numa node is NO_NODE" change. Fine to
drop this if callers are expected to cope with NUMA_NO_NODE.

"""
assert s.count("\n---\n") == 1
s = s.replace("\n---\n", "\n" + note, 1)
assert "Subject: [PATCH]" in s
s = s.replace("Subject: [PATCH]", "Subject: [RFC PATCH]", 1)
open(dst, "w").write(s)
PY

FROM="Ferran Duarri <ferran.duarri@me.com>"
args=(--from="$FROM"
      --to="Bjorn Helgaas <bhelgaas@google.com>"
      --to="Thomas Gleixner <tglx@kernel.org>"
      --to="x86@kernel.org"
      --to="linux-pci@vger.kernel.org"
      --cc="Ingo Molnar <mingo@redhat.com>"
      --cc="Borislav Petkov <bp@alien8.de>"
      --cc="Dave Hansen <dave.hansen@linux.intel.com>"
      --cc="H. Peter Anvin <hpa@zytor.com>"
      --cc="Rafael J. Wysocki <rafael@kernel.org>"
      --cc="Len Brown <lenb@kernel.org>"
      --cc="linux-acpi@vger.kernel.org"
      --cc="linux-kernel@vger.kernel.org"
      --suppress-cc=all
      --no-signed-off-by-cc)

if [[ "${1:-}" == "--send" ]]; then
  args+=(--smtp-server=smtp.mail.me.com --smtp-server-port=587
         --smtp-encryption=tls --smtp-user=ferran.duarri@me.com
         --confirm=always)
  [[ -n "${GB_SMTP_PASS:-}" ]] && args+=(--smtp-pass="$GB_SMTP_PASS")
else
  args+=(--dry-run --confirm=never)
fi

git send-email "${args[@]}" "$PATCH"
