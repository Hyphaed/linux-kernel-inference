# How to publish a patch upstream — step by step

Generalized from the procedure already written and verified for `0019` in
`~/Dev/kernel_inference/upstream-candidates/dma-buf-priority-hint/README.md`.
That document is patch-specific and still the canonical reference for `0019`
itself; this page is the same procedure made patch-agnostic, so it applies to
`0017` or any future original patch too.

**Applies only to the 4 original patches** (`0015`, `0016`, `0017`, `0019`).
See the warning below on why the 15 imported patches must never go through
this procedure.

## Before you start: read this warning

**Never submit the 15 imported third-party patches** (`0001`-`0014`, `0018`
minus `0017`/`0019` — see `README.md`'s split). `Signed-off-by:` is a legal
attestation under the kernel's Developer's Certificate of Origin (DCO) that
you have the right to submit the patch under its license — it is not a
formality. Several of these (the ACS-override and PME-timeout patches in
particular) are deliberately kept out of tree by their own authors, for
reasons worth understanding before assuming they'd be welcome upstream even
if you were the author. If in doubt whether a patch in `patches/` is yours to
send, check its `From:` header first — `git grep -m1 '^From:' patches/*.patch`
across the whole directory shows all 19 at once.

**Sending is irreversible and public.** `lore.kernel.org` and other archives
mirror everything sent to kernel mailing lists permanently. This is a step
you run yourself, deliberately, when you're ready — it is never something
run on your behalf.

## Environment state on this box (verified this session)

- `git send-email`: **not installed** (`git send-email --help` fails —
  "not a recognized git command").
- No `sendemail.*` git config present.
- No `msmtp`, `sendmail`, or `ssmtp` installed.
- `git config user.name` = `Ferran`, `user.email` = `ferran.duarri@me.com` —
  already set, will be used for `Signed-off-by` and the `From:` header.

Re-check this section's commands yourself before relying on it; it reflects
one point in time, not a guarantee of the current state.

## 1. Install and configure git-send-email

```bash
sudo apt install git-email          # provides `git send-email`
```

You need an SMTP account it can authenticate as — your own mail provider
(kernel.org does not provide one). Example, in `~/.gitconfig`:

```ini
[sendemail]
    smtpserver = smtp.yourprovider.com
    smtpuser = ferran.duarri@me.com
    smtpencryption = tls
    smtpserverport = 587
    confirm = always
```

`confirm = always` is the safety net: `git send-email` will show you the
exact composed message and ask for confirmation before it actually sends,
every time. It prompts for the SMTP password interactively (or reads it from
a configured credential helper) — it is never written to a file by this
config.

## 2. (Recommended) Subscribe to the target list(s)

Not required to send, but you won't see maintainer replies unless subscribed
or explicitly CC'd (you're always CC'd on replies to your own thread
regardless). Subscribe at the relevant list's `listinfo` page — for example,
for dma-buf work: `dri-devel@lists.freedesktop.org`
(https://lists.freedesktop.org/mailman/listinfo/dri-devel) and
`linaro-mm-sig@lists.linaro.org` (moderated;
https://lists.linaro.org/mailman/listinfo/linaro-mm-sig). `linux-kernel@` and
subsystem-specific `linux-*@vger.kernel.org` lists are high-traffic; most
people don't subscribe, they just read replies to their own thread.

## 3. Final review

```bash
cd ~/Dev/kernel_inference/linux
git show <commit>                    # read every line one more time
perl scripts/checkpatch.pl --no-tree <path-to-patch-file>
```

Do not skip this even for a patch already marked "checkpatch clean" earlier —
re-run it fresh, against the exact file you're about to send.

## 4. Derive the recipient list — never guess it

```bash
cd ~/Dev/kernel_inference/linux
perl scripts/get_maintainer.pl <path-to-patch-file>
```

This tells you exactly who and which lists own the touched files, from this
tree's own `MAINTAINERS` file. `0019`'s recipient list
(`Sumit Semwal`, `Christian König`, `linux-media@`, `dri-devel@`,
`linaro-mm-sig@`, `linux-kernel@`) was derived this way, not guessed — see
`upstream-candidates/dma-buf-priority-hint/README.md` for the exact command
that produced it.

## 5. Format and send

```bash
git format-patch -1 <commit> --subject-prefix="RFC PATCH"   # or "PATCH" for a non-RFC send
git send-email \
  --to="<maintainer1>" \
  --to="<maintainer2>" \
  --cc="<list1>" --cc="<list2>" \
  <the-generated-file>
```

Use `[RFC PATCH]` for a first-contact proposal of new API surface with one
motivating use case (as `0019` already does) — it signals "expect design
discussion, not just a merge/reject." Use plain `[PATCH]` for something more
mechanical like a build-system fix (`0017`'s likely category).

`git send-email` shows the exact composed message and asks for confirmation
(`confirm = always` from step 1) — read it once more there before confirming.

## 6. After sending

- The message is now archived permanently and publicly (lore.kernel.org and
  mirrors). This was the irreversible step, and it already happened.
- A moderated list (e.g. `linaro-mm-sig`) may hold a first post from a new
  address in a moderation queue briefly before it appears.
- Track the patch on the relevant Patchwork instance (e.g.
  `https://patchwork.freedesktop.org/project/dri-devel/list/` for dri-devel)
  — search by your email or the patch subject once sent.
- Expect nothing, one reply, or a long design thread — all three are normal
  for a first submission. No reply within a couple of weeks on a
  moderate-traffic list is common and not a rejection; a polite "ping" after
  1-2 weeks is normal practice.
- If reviewers ask for changes: `git commit --amend`, regenerate with
  `git format-patch -1 HEAD --subject-prefix="RFC PATCH v2"`, and reply
  in-thread with `git send-email --in-reply-to=<message-id> ...` rather than
  starting a new thread.

## One disclosure to decide on

These patches were drafted with AI assistance. Some kernel
subsystems/communities have norms or explicit preferences around disclosing
that (a note in the cover-letter, or a `Co-developed-by:` trailer); there is
no single kernel-wide rule as of this writing. Worth a quick check of the
target subsystem's recent list traffic for its current norm — it's your call
how to phrase it, since you're the one signing off with your real name and
email.

## DCO / Signed-off-by, briefly

`Signed-off-by: <Your Name> <your@email>` is your attestation, under the
kernel's Developer's Certificate of Origin, that you wrote the patch or
otherwise have the right to submit it under the kernel's license (GPLv2).
This is the concrete reason the 15 imported patches at the top of this
document must never be sent under your name — you did not write them, and
signing off on someone else's out-of-tree work as if it were yours to submit
would be a false attestation, not a paperwork shortcut.
