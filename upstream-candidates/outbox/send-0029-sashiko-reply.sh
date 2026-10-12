#!/usr/bin/env bash
# Threaded reply to the sashiko-bot review of the 0029 RFC.
#
#   ./send-0029-sashiko-reply.sh          # dry run, prints exactly what would go
#   ./send-0029-sashiko-reply.sh --send   # actually send
#
# iCloud needs an APP-SPECIFIC password. Export GB_SMTP_PASS or let git prompt.
# Headers read off the real mailbox 2026-09-30, not reconstructed.

set -euo pipefail
cd "$(dirname "$0")"

BODY="0029-sashiko-reply-body.txt"
OUT="0029-sashiko-reply.eml"
FROM="Ferran Duarri <ferran.duarri@me.com>"

ORIG="<33c0b32e-11eb-4e3e-bb59-e349289c498b@me.com>"
IN_REPLY_TO="<20260930072358.C5FD81F000FF@smtp.kernel.org>"

TO=(
  "Bjorn Helgaas <bhelgaas@google.com>"
  "Thomas Gleixner <tglx@kernel.org>"
  "x86@kernel.org"
  "linux-pci@vger.kernel.org"
)
CC=(
  "Ingo Molnar <mingo@redhat.com>"
  "Borislav Petkov <bp@alien8.de>"
  "Dave Hansen <dave.hansen@linux.intel.com>"
  "H. Peter Anvin <hpa@zytor.com>"
  "Rafael J. Wysocki <rafael@kernel.org>"
  "Len Brown <lenb@kernel.org>"
  "linux-acpi@vger.kernel.org"
  "linux-kernel@vger.kernel.org"
)

[[ -f "$BODY" ]] || { echo "missing $BODY" >&2; exit 1; }
if LC_ALL=C grep -q -P '[^\x00-\x7F]' "$BODY"; then
    echo "non-ASCII in $BODY" >&2; exit 1
fi

{
  echo "From: $FROM"
  echo "Subject: Re: [RFC PATCH] x86/PCI: Use the only online node for root buses with no _PXM"
  echo "In-Reply-To: $IN_REPLY_TO"
  echo "References: $ORIG $IN_REPLY_TO"
  echo "Content-Type: text/plain; charset=us-ascii"
  echo "Content-Transfer-Encoding: 7bit"
  echo
  cat "$BODY"
} > "$OUT"

args=(--from="$FROM" --no-thread --no-chain-reply-to --suppress-cc=all
      --no-format-patch --quiet)
for a in "${TO[@]}"; do args+=(--to="$a"); done
for a in "${CC[@]}"; do args+=(--cc="$a"); done

args+=(--smtp-server=smtp.mail.me.com
       --smtp-server-port=587
       --smtp-encryption=tls
       --smtp-user=ferran.duarri@me.com)
[[ -n "${GB_SMTP_PASS:-}" ]] && args+=(--smtp-pass="$GB_SMTP_PASS")

if [[ "${1:-}" == "--send" ]]; then
    echo "sending $OUT ..."
    git send-email "${args[@]}" --confirm=always "$OUT"
else
    echo "DRY RUN, nothing sent. Pass --send to send it."
    echo
    git send-email "${args[@]}" --dry-run "$OUT"
    echo
    cat "$OUT"
fi
