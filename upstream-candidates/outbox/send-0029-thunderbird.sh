#!/usr/bin/env bash
# 0029 via Thunderbird (iCloud SMTP auth failed for git send-email).
# Builds a plain-text .eml with X-Unsent so Thunderbird opens it as an
# editable compose window. It does NOT send; you review and press Send.
#
# Needs user.js prefs (plain text, wraplength 0, no format=flowed) and a
# Thunderbird restart after they were added. New thread, so no threading
# headers are needed.
set -euo pipefail
cd "$(dirname "$0")"

SRC="0029-x86-pci-single-node-root-bus-node.patch"
OUT="0029-rfc.eml"

python3 - "$SRC" "$OUT" <<'PY'
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
# drop the mbox "From <sha>" line and the From:/Date: headers, keep the
# subject as a header and everything after it as the body
lines = s.split("\n")
subj = next(i for i, l in enumerate(lines) if l.startswith("Subject:"))
subject = lines[subj].replace("[PATCH]", "[RFC PATCH]", 1)
j = subj + 1
while lines[j].startswith(" "):      # folded subject
    subject += lines[j]; j += 1
body = "\n".join(lines[j:]).lstrip("\n")
hdr = "\n".join([
    "X-Unsent: 1",
    "From: Ferran Duarri <ferran.duarri@me.com>",
    "To: Bjorn Helgaas <bhelgaas@google.com>, Thomas Gleixner <tglx@kernel.org>,"
    " x86@kernel.org, linux-pci@vger.kernel.org",
    "Cc: Ingo Molnar <mingo@redhat.com>, Borislav Petkov <bp@alien8.de>,"
    " Dave Hansen <dave.hansen@linux.intel.com>, \"H. Peter Anvin\" <hpa@zytor.com>,"
    " \"Rafael J. Wysocki\" <rafael@kernel.org>, Len Brown <lenb@kernel.org>,"
    " linux-acpi@vger.kernel.org, linux-kernel@vger.kernel.org",
    subject,
    "MIME-Version: 1.0",
    "Content-Type: text/plain; charset=us-ascii",
    "Content-Transfer-Encoding: 7bit",
])
open(dst, "w", newline="\n").write(hdr + "\n\n" + body)
PY
grep -qP '[^\x00-\x7F]' "$OUT" && { echo "non-ASCII in $OUT" >&2; exit 1; }
echo "wrote $PWD/$OUT ($(wc -l < "$OUT") lines)"

flatpak run --branch=stable --arch=x86_64 --command=thunderbird \
  --file-forwarding org.mozilla.thunderbird_esr @@ "$PWD/$OUT" @@
