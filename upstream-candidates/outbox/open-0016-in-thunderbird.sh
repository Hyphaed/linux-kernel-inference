#!/usr/bin/env bash
# Thunderbird route for the 0016 withdrawal.
#
# Read this before using it. Thunderbird's -compose command line cannot set
# In-Reply-To or References , it accepts to/cc/subject/body/format and nothing
# else. A mail sent that way arrives at linux-mm as a NEW thread rather than a
# reply, which on a mailing list is visible and annoying.
#
# So this script does NOT compose the mail. It puts the body on your clipboard
# and raises Thunderbird, and you hit Reply All on the original message, which
# is the one path where Thunderbird sets the threading headers correctly.
#
# Passing the body on the command line would fail anyway: -compose splits its
# argument on commas and quotes values with apostrophes, and this body
# contains both.
#
# If you want it fully scripted, use ./send-0016-reply.sh instead , git
# send-email sets the headers properly.
#
#   ./open-0016-in-thunderbird.sh

set -euo pipefail
cd "$(dirname "$0")"

BODY="0016-reply-body.txt"
TB=(/usr/bin/flatpak run --branch=stable --arch=x86_64 --command=thunderbird
    --file-forwarding org.mozilla.thunderbird_esr)

[[ -f "$BODY" ]] || { echo "missing $BODY" >&2; exit 1; }

if command -v wl-copy >/dev/null 2>&1; then
    wl-copy < "$BODY"
    echo "body copied to the clipboard (wl-copy, $(wc -l < "$BODY") lines)"
elif command -v xclip >/dev/null 2>&1; then
    # Wayland session, so this goes through XWayland. Thunderbird reads it
    # fine; a native-Wayland-only clipboard manager may not show it.
    xclip -selection clipboard < "$BODY"
    echo "body copied to the clipboard (xclip/XWayland, $(wc -l < "$BODY") lines)"
else
    echo "no clipboard tool found , open $PWD/$BODY and copy it by hand" >&2
fi

cat <<'INSTRUCTIONS'

In Thunderbird:

  1. Open the thread "[PATCH] mm: thp: default defrag mode to defer+madvise"
  2. Select David Hildenbrand's mail of Fri 21 Aug 16:18 (the one asking why
     this belongs in the kernel rather than in the distro)
  3. Reply All
  4. Paste

Check before sending:

  - Composition is PLAIN TEXT, not HTML. Options > Delivery Format >
    Plain Text Only, or hold Shift while clicking Reply All. vger silently
    drops HTML mail, so getting this wrong looks like the mail vanished.
  - Reply All kept the list on: linux-mm@kvack.org and
    linux-kernel@vger.kernel.org must both be there.
  - No signature got appended below "Ferran".
  - Line wrapping stays where it is. mailnews.wraplength should be 72-76;
    if Thunderbird reflows the quoted code block, the diff context becomes
    unreadable.

Full recipient list, from the real headers on the thread:

  To:  David Hildenbrand <david@kernel.org>
       Lorenzo Stoakes <ljs@kernel.org>
       Zi Yan <ziy@nvidia.com>
       Andrew Morton <akpm@linux-foundation.org>
  Cc:  Johannes Weiner <hannes@cmpxchg.org>
       Baolin Wang <baolin.wang@linux.alibaba.com>
       Ryan Roberts <ryan.roberts@arm.com>
       Barry Song <baohua@kernel.org>
       linux-mm@kvack.org
       linux-kernel@vger.kernel.org

INSTRUCTIONS

echo "raising Thunderbird ..."
"${TB[@]}" >/dev/null 2>&1 &
disown
