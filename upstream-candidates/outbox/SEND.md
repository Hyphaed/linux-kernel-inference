# Outbox , ready to send, not sent

Three patches, each verified against the kernel's own gates on the 7.1 tree in
`linux/`. `0020` is deliberately **not** here; see `../SUBMISSION.md`.

Everything below is authored `Ferran Duarri <ferran.duarri@pm.me>` with a
matching `Signed-off-by:`, enforced by `tests/test_patch_authorship.py`.

## Status of each

| Patch | checkpatch | What it is | Send as |
|---|---|---|---|
| `0021-pci-sysfs-document-link-speed-width-attrs.patch` | 0 errors, 1 warning (see below) | Documentation only, no behaviour change | `PATCH` |
| `0017-kbuild-ubsan-extmod-opt-in.patch` | clean, "ready for submission" | Bug fix | `PATCH` |
| `0019-dma-buf-priority-hint.patch` | clean, "ready for submission" | New UAPI, contested | `RFC PATCH` |

The one remaining `0021` warning is on its commit-reference line:

    commit 56c1af4606f0 ("PCI: Add sysfs max_link_speed/width, ...")

That is the accepted exception. A commit reference must not be wrapped, so the
75-column preference does not apply to it, and wrapping it to satisfy
checkpatch produces a hard ERROR instead (verified, both ways).

## Prerequisite: your mail account

`git send-email` needs SMTP credentials that only you should hold. Configure
once, in your own environment:

    git config --global sendemail.smtpServer        smtp.your-provider
    git config --global sendemail.smtpUser          ferran.duarri@pm.me
    git config --global sendemail.smtpEncryption    tls
    git config --global sendemail.smtpServerPort    587

Do NOT put the password in git config. `git send-email` will prompt, or read
it from your keyring.

**Send yourself a test first.** A malformed From:, a mangled patch, or an
SMTP server that rewrites headers is invisible until it hits a public list,
and a list post cannot be unsent:

    git send-email --to=ferran.duarri@pm.me --dry-run 0021-*.patch   # inspect headers
    git send-email --to=ferran.duarri@pm.me 0021-*.patch             # real, to yourself

Open what arrives and confirm `git am` applies it cleanly before going public.

## 1. `0021` , PCI/sysfs documentation

Lowest risk, send this one first. It documents four attributes exported since
2018, changes no behaviour, and is the natural way to find out whether your
mail path is clean.

`get_maintainer.pl` under-resolves this one, returning only the open list,
because the patch touches an ABI file the script does not attribute to a
subsystem. The correct recipients come from `MAINTAINERS`' PCI SUBSYSTEM entry:

    git send-email \
      --to="Bjorn Helgaas <bhelgaas@google.com>" \
      --cc=linux-pci@vger.kernel.org \
      --cc=linux-api@vger.kernel.org \
      --cc=linux-kernel@vger.kernel.org \
      0021-pci-sysfs-document-link-speed-width-attrs.patch

## 2. `0017` , kbuild UBSAN

    git send-email \
      --to="Nathan Chancellor <nathan@kernel.org>" \
      --to="Nicolas Schier <nsc@kernel.org>" \
      --cc=linux-kbuild@vger.kernel.org \
      --cc=linux-kernel@vger.kernel.org \
      0017-kbuild-ubsan-extmod-opt-in.patch

Expect to be asked for the reproducer. The answer is VMware `vmnet`/`vmmon`:
builds clean, loads clean, then fails packet forwarding at runtime because it
inherited UBSAN flags it never asked for.

## 3. `0019` , dma-buf priority hint (RFC)

Send **last**, and only once the two above have landed or at least been
received without mail problems. This is new UAPI and it will be argued about.

    git send-email \
      --subject-prefix="RFC PATCH" \
      --to="Sumit Semwal <sumit.semwal@linaro.org>" \
      --to="Christian König <christian.koenig@amd.com>" \
      --cc=linux-media@vger.kernel.org \
      --cc=dri-devel@lists.freedesktop.org \
      --cc=linaro-mm-sig@lists.linaro.org \
      --cc=linux-kernel@vger.kernel.org \
      0019-dma-buf-priority-hint.patch

**Lead with the blocker in your reply, do not wait to be caught.** The only
consumer is out-of-tree. Adding UAPI (two ioctls, an fdinfo field, a
`DMA_BUF_PRIORITY_*` range) with no in-tree user is normally declined, and
correctly so: UAPI is permanent and nothing in-tree constrains the semantics.

`../SUBMISSION.md` has the full cover-letter skeleton. The short version, in
the order the list will want it:

1. The problem: an exporter that knows some of its pinned buffers are hot has
   no standard way to say so, so every out-of-tree tiering driver invents a
   private ioctl. GreenBoost's `GB_IOCTL_SET_HEAT` is exactly that.
2. What this adds: a hint, not a policy. An advisory 0-255 value, reported via
   fdinfo and `GET_PRIORITY`, with no reclaim implemented in dma-buf core.
   Deliberately the smallest possible surface.
3. Who uses it, named plainly as out-of-tree, with the reader described
   concretely: `greenboost.ko` consults it in its T2 eviction sweep, as a
   skip-on-threshold check that never overrides the hard KV-cache invariant.
4. What you are asking for: whether the shape is right, not whether it can be
   merged today.

Realistic outcomes are that someone with an in-tree use case redesigns it, or
it is declined pending one. Both are worth knowing.

## After sending

Record the lore.kernel.org message-id for each in `../SUBMISSION.md` so the
thread can be found later, and move the patch out of this outbox.
