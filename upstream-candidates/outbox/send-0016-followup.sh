#!/usr/bin/env bash
# Short follow-up on 0016, answering only the disclosure point.
#
# Uses git send-email, which is the tool the list expects: it sets In-Reply-To
# and References so the reply lands inside the thread, and it sends flat
# plain text with no format=flowed rewrapping. Thunderbird's -compose command
# line cannot set either header, which is why this exists , see
# open-0016-in-thunderbird.sh for the GUI route and its caveat.
#
#   ./send-0016-reply.sh              # dry run, prints exactly what would go
#   ./send-0016-reply.sh --send       # actually send
#
# SMTP: iCloud needs an APP-SPECIFIC password, not the account password.
# Generate one at appleid.apple.com > Sign-In and Security > App-Specific
# Passwords. Either export it as GB_SMTP_PASS or let git prompt for it.

set -euo pipefail
cd "$(dirname "$0")"

BODY="0016-followup-body.txt"
OUT="0016-followup.eml"
FROM="Ferran Duarri <ferran.duarri@me.com>"

# Threading. Reply to Lorenzo's 18:53 mail, the one this answers. His mail
# and the withdrawal crossed (18:53 vs 19:10), which is the first thing the
# body says, so it has to hang off his message and not off the withdrawal.
IN_REPLY_TO="<aoh_40ku6lQMOUQb@gremlin>"
REFERENCES="<20260820190825.221308-1-ferran.duarri@me.com>"

# Everyone on the thread, taken from the real headers rather than guessed.
TO=(
  "Lorenzo Stoakes <ljs@kernel.org>"
  "David Hildenbrand <david@kernel.org>"
  "Zi Yan <ziy@nvidia.com>"
  "Andrew Morton <akpm@linux-foundation.org>"
)
CC=(
  "Johannes Weiner <hannes@cmpxchg.org>"
  "Baolin Wang <baolin.wang@linux.alibaba.com>"
  "Ryan Roberts <ryan.roberts@arm.com>"
  "Barry Song <baohua@kernel.org>"
  "linux-mm@kvack.org"
  "linux-kernel@vger.kernel.org"
)

[[ -f "$BODY" ]] || { echo "missing $BODY" >&2; exit 1; }

{
  echo "From: $FROM"
  echo "Subject: Re: [PATCH] mm: thp: default defrag mode to defer+madvise"
  echo "In-Reply-To: $IN_REPLY_TO"
  echo "References: $REFERENCES $IN_REPLY_TO"
  echo "Content-Type: text/plain; charset=UTF-8"
  echo "Content-Transfer-Encoding: 8bit"
  echo
  cat "$BODY"
} > "$OUT"

args=(--from="$FROM" --no-thread --no-chain-reply-to --suppress-cc=all
      --8bit-encoding=UTF-8 --no-format-patch --quiet)
for a in "${TO[@]}"; do args+=(--to="$a"); done
for a in "${CC[@]}"; do args+=(--cc="$a"); done

# iCloud SMTP. Submission port with STARTTLS.
args+=(--smtp-server=smtp.mail.me.com
       --smtp-server-port=587
       --smtp-encryption=tls
       --smtp-user=ferran.duarri@me.com)
[[ -n "${GB_SMTP_PASS:-}" ]] && args+=(--smtp-pass="$GB_SMTP_PASS")

if [[ "${1:-}" == "--send" ]]; then
    echo "sending $OUT ..."
    git send-email "${args[@]}" --confirm=always "$OUT"
else
    echo "DRY RUN , nothing sent. Pass --send to send it."
    echo
    git send-email "${args[@]}" --dry-run "$OUT"
    echo
    echo "message that would be sent:"
    echo "---------------------------------------------------------------"
    cat "$OUT"
fi
