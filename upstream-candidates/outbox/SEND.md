# Outbox , ready to send, not sent

Three patches. `0021` (linux-pci) and `0017` (linux-kbuild) were both sent on
2026-08-20 and now live in `../sent/`; their message-ids are in
`../SUBMISSION.md`. `0020` is deliberately not here at all; same file explains
why.

**Only six of the twenty-one patches in this series are ours.** The other
fifteen are CachyOS, XanMod, Liquorix and TKG work carried with their original
authorship, and they are not ours to submit , see `../SUBMISSION.md`.

Each was verified against the kernel's own gates on the 7.1 tree in `linux/`.

Everything below is authored `Ferran Duarri <ferran.duarri@me.com>` with a
matching `Signed-off-by:`, enforced by `tests/test_patch_authorship.py`.

## Status of each

| Patch | checkpatch | What it is | Send as |
|---|---|---|---|
| `0021-pci-sysfs-document-link-speed-width-attrs.patch` | 0 errors, 1 warning (see below) | Documentation only, no behaviour change | `PATCH` , **sent** |
| `0017-kbuild-ubsan-extmod-opt-in.patch` | clean, "ready for submission" | Bug fix | `PATCH` , **sent** |
| `0015-nvme-lower-default-apst-latency.patch` | clean | Default change, no measurement | `PATCH` , weak |
| `0016-mm-thp-defrag-defer-madvise-default.patch` | clean | Default change, no measurement | `PATCH` , weak |
| `0019-dma-buf-priority-hint.patch` | clean, "ready for submission" | New UAPI, contested | `RFC PATCH` |

Send in that order. It is ascending order of how much argument each one will
attract, which is also the order that builds the most credibility per reply.

The one remaining `0021` warning is on its commit-reference line:

    commit 56c1af4606f0 ("PCI: Add sysfs max_link_speed/width, ...")

That is the accepted exception. A commit reference must not be wrapped, so the
75-column preference does not apply to it, and wrapping it to satisfy
checkpatch produces a hard ERROR instead (verified, both ways).

## Prerequisite: the one thing that is not done

`git send-email` is already configured in this repo, and it is the only send
path used here. Thunderbird was considered and dropped: the kernel's own
`Documentation/process/email-clients.rst` opens by calling it "an Outlook clone
that likes to mangle text", and it needs two extensions plus an external editor
before it can send a patch safely.

Current config, verified:

    sendemail.smtpserver      smtp.mail.me.com
    sendemail.smtpuser        ferran.duarri@me.com
    sendemail.smtpserverport  587
    sendemail.smtpencryption  tls
    sendemail.from            Ferran Duarri <ferran.duarri@me.com>
    sendemail.confirm         always
    sendemail.annotate        yes

**What is missing is the password, and only you can supply it.** iCloud rejects
your Apple ID password over SMTP; it requires an app-specific password, created
at <https://appleid.apple.com> under Sign-In and Security. `git send-email`
prompts for it at send time. Do not put it in git config , this file is
committed and pushed.

**Send yourself a test first.** A mangled patch or a header-rewriting server is
invisible until it hits a public list, and a list post cannot be unsent:

    cd ~/Dev/kernel_inference/upstream-candidates/outbox
    git send-email --to=ferran.duarri@me.com --dry-run 0021-*.patch   # headers only
    git send-email --to=ferran.duarri@me.com 0021-*.patch             # real, to yourself

Open what arrives, save it, and confirm `git am` applies it cleanly before
going public.

### Already verified here

Applied on a clean 7.1 base (no series commits present), all three in order:

    git am 0021-*.patch 0017-*.patch 0019-*.patch
    -> 3 commits, each Ferran Duarri <ferran.duarri@me.com>

So a maintainer's first action succeeds. What is untested is the mail path
itself, which is exactly what the self-test above covers.

Recipients re-checked against the 7.1 `MAINTAINERS` with `get_maintainer.pl`:
the lists below for `0017` and `0019` match it exactly. `0021` under-resolves to
the open list only, so its recipients come from the `PCI SUBSYSTEM` entry
directly , confirmed as Bjorn Helgaas and `linux-pci@vger.kernel.org`.

`linaro-mm-sig@lists.linaro.org` (on `0019`) is a **moderated** list. A post
from a non-subscriber sits in a moderation queue rather than bouncing, so
silence there is not a delivery failure. The other lists are open.

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

## 3. `0015` , NVMe APST default, and `0016` , THP defrag default

Both were regenerated on 2026-08-20 and were **not submittable before that**.
They had been hand-written rather than produced by `git format-patch`: fake
blob hashes (`index 1111111..2222222`), a placeholder
`Date: Mon, 01 Jan 2024 00:00:00`, no `From <sha>` header, and a private
`Forward-Port-Notes:` trailer sitting above the `---` where `git am` would have
carried it into the commit message. They now come from real commits on a clean
7.1 base and are checkpatch-clean.

    git send-email \
      --to="Keith Busch <kbusch@kernel.org>" \
      --to="Jens Axboe <axboe@kernel.dk>" \
      --to="Christoph Hellwig <hch@lst.de>" \
      --to="Sagi Grimberg <sagi@grimberg.me>" \
      --cc=linux-nvme@lists.infradead.org \
      --cc=linux-kernel@vger.kernel.org \
      0015-nvme-lower-default-apst-latency.patch

    git send-email \
      --to="Andrew Morton <akpm@linux-foundation.org>" \
      --to="David Hildenbrand <david@kernel.org>" \
      --to="Lorenzo Stoakes <ljs@kernel.org>" \
      --cc="Zi Yan <ziy@nvidia.com>" \
      --cc="Baolin Wang <baolin.wang@linux.alibaba.com>" \
      --cc="Ryan Roberts <ryan.roberts@arm.com>" \
      --cc="Barry Song <baohua@kernel.org>" \
      --cc=linux-mm@kvack.org \
      --cc=linux-kernel@vger.kernel.org \
      0016-mm-thp-defrag-defer-madvise-default.patch

**Expect these two to be the hardest sell in the series, and know why before
you send them.** Neither fixes a bug. Each changes a compiled-in default that
is already runtime-tunable by the very mechanism its own commit message points
at , a module parameter for `0015`, a sysfs knob for `0016`. The standing
answer to that shape of patch is "your machine can already set this, so set
it", and it is a fair answer.

### Measured on this box, 2026-08-20

Read before you claim a benefit, because one of these does not say what we
expected.

`0016`, THP. The premise checks out and the benefit does not, yet:

    enabled: [always] madvise never          <- the config the argument needs
    defrag:  always defer [defer+madvise]    <- patch active
    compact_stall        0
    thp_fault_alloc      60682
    thp_fault_fallback   0

Zero stalls in 4h21m looks like a win and is not one. Direct compaction is
only entered when a THP allocation FAILS, and `thp_fault_fallback` is 0 , not
one of 60682 huge-page faults had to fall back. With 46 GB of 64 GB available
and swap untouched, unpatched `madvise` mode would have recorded 0 stalls too.
**This box has never reached the condition the patch protects against**, so it
cannot testify either way.

So do not send `0016` as a performance fix. Send it as the correctness argument
it actually is, which needs no benchmark: `CONFIG_TRANSPARENT_HUGEPAGE_ALWAYS`
and `_MADVISE` choose *whether* THP applies, the defrag flag chooses *how hard*
the allocator works, and the initializer pins the second as though the first
had decided it. That is reviewable on the code alone. State plainly that no
stall measurement is offered and why , an untriggered path on the author's
hardware is a better answer than a number that measured nothing.

`0015`, NVMe. Device is a Samsung SSD 990 EVO Plus 4TB, fw 2B2QKXG7, and the
running kernel already carries the patch (`default_ps_max_latency_us` reads
25000). The decisive figure is the deepest non-operational state's exit
latency, which needs root:

    sudo nvme id-ctrl /dev/nvme0 -H | grep -A2 -iE '^ps  '

If that state exits in well under 25ms the patch changes nothing on this
device and the honest thing is to say so. If it approaches 100ms, that number
IS the argument and belongs in the commit message.

What would additionally change the outcome, for both:

- `0015` needs a cold-read latency distribution on a device whose deepest
  non-operational state actually approaches the 100ms bound, at both settings.
  Without it the 25ms figure is a preference, not a finding. Worth knowing that
  the number is also device-dependent , on an SSD whose deepest state exits in
  5ms the patch changes nothing at all, which is an argument the list will
  make.
- `0016` needs fault-latency percentiles under memory pressure with
  `transparent_hugepage=always`, at `madvise` versus `defer+madvise`. The
  argument in the commit message (that the Kconfig choice is about *whether*,
  not *how hard*, and the two got conflated) is the genuinely interesting part
  and stands on its own reasoning , but a stall measurement is what makes it
  land.

Send them anyway if you want the reviewer's read on the reasoning; just lead
with the missing measurement rather than waiting to be asked, exactly as
`0019` leads with its missing in-tree user. An author who marks their own
evidence gap gets engagement; one who is caught in it gets ignored.

## 4. `0019` , dma-buf priority hint (RFC)

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
