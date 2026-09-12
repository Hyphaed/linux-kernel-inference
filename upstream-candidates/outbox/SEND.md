# Outbox

**Correction, 2026-08-31: this file was wrong.** It said "0015 has never been
sent" as its opening line. The mailbox shows 0015 was sent four times
(2026-08-20 three times, then again 2026-08-28), and 0021 v3 was sent
2026-08-21. Both patches are moved to `../sent/` now; both message-ids and the
reviewer replies they drew are recorded in `../SUBMISSION.md`. Nothing below
this point has been re-verified against the mailbox — read `../SUBMISSION.md`
first for current state before acting on anything below.

`0021`, `0017`, `0016`, `0019`, `0015` and `0021` v3 have all gone out; their
message-ids are in `../SUBMISSION.md`. `0020` is deliberately not here at all;
that file explains why.

**Only six of the twenty-one patches in this series are ours.** The other
fifteen are CachyOS, XanMod, Liquorix and TKG work carried with their original
authorship, and they are not ours to submit , see `../SUBMISSION.md`.

Everything here is authored `Ferran Duarri <ferran.duarri@me.com>` with a
matching `Signed-off-by:`, enforced by `tests/test_patch_authorship.py`.

## Status

| Patch | checkpatch | What it is | Send as |
|---|---|---|---|
| `0021-v2-pci-sysfs-document-link-speed-width-attrs.patch` | 0 errors, 1 warning (commit-reference line, accepted exception) | Documentation only, **corrects a wrong v1** | `PATCH v2` , ready |
| `0017-kbuild-ubsan-extmod-opt-in.patch` | clean | Bug fix | `PATCH` , **sent** |
| `0016-mm-thp-defrag-defer-madvise-default.patch` | 0 errors, 0 warnings, 2 strict-mode checks | Default change, no measurement offered | `PATCH` , **sent** |
| `0019-dma-buf-priority-hint.patch` | clean | New UAPI, contested | `RFC PATCH` , **sent** |
| `0015-nvme-lower-default-apst-latency.patch` | clean, "ready for submission" | Default change, **measured** | `PATCH` , ready |

`0016`'s two checks are `--strict`-only style preferences about `(1<<FLAG)|`
spacing, and the added line matches the formatting of the initializer block it
sits in. Matching surrounding style is the right call; they were not defects.

Run checkpatch **from inside `linux/`**. Invoked from the repo root it cannot
find the tree, silently reads a patch as though it were a source file, and
reports a normal diff context line as a trailing-whitespace ERROR. That false
positive cost time on 2026-08-20; the correct invocation is:

    cd linux && perl scripts/checkpatch.pl --strict ../upstream-candidates/outbox/00XX-*.patch

## `0015` , NVMe APST default, ready to send

Recipients confirmed against the 7.1 tree with `get_maintainer.pl`, which
resolves this one exactly (unlike `0021`):

    cd ~/Dev/kernel_inference/upstream-candidates/outbox
    git send-email \
      --to="Keith Busch <kbusch@kernel.org>" \
      --to="Jens Axboe <axboe@kernel.dk>" \
      --to="Christoph Hellwig <hch@lst.de>" \
      --to="Sagi Grimberg <sagi@grimberg.me>" \
      --cc=linux-nvme@lists.infradead.org \
      --cc=linux-kernel@vger.kernel.org \
      0015-nvme-lower-default-apst-latency.patch

### This one is no longer the weak sell it was

Earlier revisions of this file said `0015` and `0016` were the weakest of the
six because neither offered a measurement. That is still true of `0016`. It is
no longer true of `0015`.

The device (Samsung SSD 990 EVO Plus 4TB, fw 2B2QKXG7) advertises two
non-operational states, `ps 3` exiting in 4.6 ms and `ps 4` in 43 ms. The stock
100 ms bound admits `ps 4`; the patch's 25 ms bound stops at `ps 3`. Confirmed
directly from the controller's own APST table rather than inferred: at 25000 it
targets `ps [0, 3]`, at 100000 `ps [0, 3, 4]`.

Measured with `diagnostics/nvme-apst-cold-read.py`, 60 trials per setting,
the two settings interleaved trial by trial:

                       p50      p90      p99      max
    100000 (ps 4)     3.3ms   33.0ms   33.5ms   33.5ms
    25000  (ps 3)     3.2ms    3.6ms    3.7ms    4.2ms
    warm, no idle     0.2ms    0.3ms    1.7ms    3.6ms

Two things make this trustworthy, and one caveat is stated in the commit
message rather than hidden:

- **Both states landed at 78% of their advertised exit latency** (3.57 of
  4.6 ms; 33.46 of 43 ms). Two independent magnitudes with the same systematic
  relationship to the spec figure is the cross-check that the sampler measures
  wake latency rather than something else.
- **The 25000 arm never exceeded 4.228 ms across 60 trials**, and shows no warm
  mode at all. The patch's bound is observed, not merely advertised.
- **Caveat: roughly five trials in six at 100000 did not finish descending to
  `ps 4`** inside the 4.81 s idle window and were sampled at `ps 3` depth,
  which is why that row's p50 sits at the other row's value. The `ps 4` figure
  rests on the remaining sixth, where it was tightly reproducible
  (32.9-33.5 ms). This dilution can only understate the cost of the current
  default, never overstate it.

The idle window has since been widened (`derived * 2.0 + 2.0` rather than
`* 1.5 + 1.0`) because a disturbance mid-window costs the whole descent again,
not a fraction of it. A re-run at the wider setting would put most trials in
the `ps 4` mode and raise that figure's sample count; it is not needed for the
claim, which is already conservative.

`25 ms` is a bound on latency an interactive reader notices, not a value fitted
to this drive , any value between 4.6 and 43 ms behaves identically here. The
commit message says so explicitly, and says that on a device whose deepest
state exits under 25 ms the patch is a no-op, which is correct for a bound.
That answers the objection this file used to predict ("on an SSD whose deepest
state exits in 5 ms the patch changes nothing") before it is raised.

## `0021` v2 , the sent version was wrong

AI review on the v1 thread raised three objections to the documentation text.
All three were checked against `drivers/pci/` and all three hold, and a fourth
and fifth turned up while checking:

1. v1 called `max_link_speed` "the ceiling the link may negotiate, which is the
   lower of what the two ends of the link support", then contradicted itself one
   sentence later. `max_link_speed_show()` calls `pcie_get_speed_cap()`, which
   is now just `PCIE_LNKCAP2_SLS2SPEED(dev->supported_speeds)` , the capability
   of the device being read, never consulting the other end at all.
2. v1 attributed the value to the Max Link Speed field of Link Capabilities.
   `pcie_get_supported_speeds()` reads the Supported Link Speeds Vector in Link
   Capabilities **2**, masks it against Max Link Speed, and falls back to
   synthesizing from Max Link Speed only for devices predating PCIe r3.0.
3. v1 told callers wanting the ceiling to use `max_link_speed`. That
   overestimates whenever the upstream port is the slower end , exactly the
   error a user-space tool would then report as available bandwidth.
4. Found while checking, not raised in review: the value is read once at
   enumeration and cached in `pci_dev->supported_speeds` (`probe.c`). v1 never
   said so, while its `current_link_speed` entry advertises that nothing is
   cached , inviting the reader to assume the same of `max_link_speed`.
5. Also found here: v1 carried a private `Forward-Port-Notes:` trailer inside
   the commit message. That is the same defect commit `9d2a71a` cleaned out of
   `0015` and `0016`; nobody re-checked `0021`, and it shipped.

`max_link_width`'s register attribution was correct and is unchanged in
substance. Send v2 threaded to v1:

    cd ~/Dev/kernel_inference/upstream-candidates/outbox
    git send-email \
      --in-reply-to="<20260820184228.166566-1-ferran.duarri@me.com>" \
      --to="Bjorn Helgaas <bhelgaas@google.com>" \
      --cc=linux-pci@vger.kernel.org \
      --cc=linux-api@vger.kernel.org \
      --cc=linux-kernel@vger.kernel.org \
      0021-v2-pci-sysfs-document-link-speed-width-attrs.patch

The lesson worth keeping: a documentation patch is a claim about behaviour, and
it needs the same verification against the implementation that a code change
gets. v1's text was written from how the attributes *ought* to work.

## Pending: the `0019` follow-up reply

`../replies/0019-followup.txt`, ready to send, dry-run verified:

    cd ~/Dev/kernel_inference/upstream-candidates/replies
    git send-email \
      --in-reply-to="<20260820190838.221435-1-ferran.duarri@me.com>" \
      --to="Sumit Semwal <sumit.semwal@linaro.org>" \
      --to="Christian König <christian.koenig@amd.com>" \
      --cc=linux-media@vger.kernel.org \
      --cc=dri-devel@lists.freedesktop.org \
      --cc=linaro-mm-sig@lists.linaro.org \
      --cc=linux-kernel@vger.kernel.org \
      0019-followup.txt

`0019` went out as a bare single patch with no cover letter, so the four points
this file said to lead with never travelled with it. Its commit message does
disclose that the consumer is out-of-tree, so nothing was concealed, but it
does not name `greenboost.ko`, does not say it is the *only* consumer, does not
state the ask, and asserts that "GPU drivers juggling foreground and background
clients want it" with nothing cited. The reply fixes all four, including
withdrawing that claim , the same move that made `0016` honest.

## Queued 2026-08-21 , three sends, none made. Commands below are DRY-RUN VERIFIED.

Recipients are from `MAINTAINERS` / `get_maintainer.pl`, not memory:
Bjorn Helgaas <bhelgaas@google.com> and linux-pci for PCI;
Ilpo Järvinen <ilpo.jarvinen@linux.intel.com> is the reviewer who replied.
(`Documentation/ABI/testing/sysfs-bus-pci` is NOT listed under PCI SUBSYSTEM's
`F:` lines, so `get_maintainer.pl` returns only the open list for it , the
addresses above are the right ones anyway and the thread already reaches them.)

Run `./tools/patch-audit` first. Currently green.

Every command below has been run with `--dry-run` and reported `Dry-OK`.
To send for real, drop `--dry-run`.

**1. Reply to the `0021` review** , answers the Assisted-by question, the
same-evening v1/v2 churn, and Ilpo's two technical points:

    cd ~/Dev/kernel_inference/upstream-candidates/replies
    git send-email \
      --in-reply-to="<20260820184228.166566-1-ferran.duarri@me.com>" \
      --to="Ilpo Järvinen <ilpo.jarvinen@linux.intel.com>" \
      --to="Bjorn Helgaas <bhelgaas@google.com>" \
      --cc=linux-pci@vger.kernel.org \
      --cc=linux-api@vger.kernel.org \
      --cc=linux-kernel@vger.kernel.org \
      0021-review-reply.txt

**2. `0021` v3** , same thread, straight after the reply:

    cd ~/Dev/kernel_inference/upstream-candidates/outbox
    git send-email \
      --in-reply-to="<20260820184228.166566-1-ferran.duarri@me.com>" \
      --to="Bjorn Helgaas <bhelgaas@google.com>" \
      --to="Ilpo Järvinen <ilpo.jarvinen@linux.intel.com>" \
      --cc=linux-pci@vger.kernel.org \
      --cc=linux-api@vger.kernel.org \
      --cc=linux-kernel@vger.kernel.org \
      0021-v3-pci-sysfs-document-link-speed-width-attrs.patch

**3. Withdraw `0016`** , different subsystem, different thread. Same recipient
set the original went to, so everyone who saw the patch sees the withdrawal:

    cd ~/Dev/kernel_inference/upstream-candidates/replies
    git send-email \
      --in-reply-to="<20260820190825.221308-1-ferran.duarri@me.com>" \
      --to="Andrew Morton <akpm@linux-foundation.org>" \
      --to="David Hildenbrand <david@kernel.org>" \
      --to="Lorenzo Stoakes <ljs@kernel.org>" \
      --cc="Zi Yan <ziy@nvidia.com>" \
      --cc="Baolin Wang <baolin.wang@linux.alibaba.com>" \
      --cc="Ryan Roberts <ryan.roberts@arm.com>" \
      --cc="Barry Song <baohua@kernel.org>" \
      --cc=linux-mm@kvack.org \
      --cc=linux-kernel@vger.kernel.org \
      0016-withdrawal.txt

### What happens when you run one

`sendemail.confirm = always` and `sendemail.annotate = yes` are both set, so
each message stops twice: an editor opens on the message (save and quit to keep
it, or edit in place), then a `Send this email? ([y]es|[n]o|[q]uit|[a]ll)`
prompt. `q` at that prompt sends nothing.

The SMTP password is an **app-specific password** from
<https://appleid.apple.com> under Sign-In and Security. iCloud rejects the
Apple ID password itself. It is prompted for at send time and is not in git
config, which is deliberate , this file is committed.

### Immediately after each send , non-optional

`git send-email` prints `Message-ID: <...>` for every message. Put it in
`../SUBMISSION.md` before you run the next command. **A send is not done until
its message-id is recorded there.** That missing step is the entire reason the
`0021` history could not be reconstructed and why nobody noticed v2 had already
gone out.

## Pre-send gate , run 2026-08-21 02:28 CEST, all three items

Verified, nothing sent. Every send is outward-facing and irreversible and none
of the three went out; they wait on an explicit per-item go-ahead. (The mail
path could not have sent unattended in any case , `git send-email` prompts for
the app-specific password, and `sendemail.confirm always` stops on every send.)

| Item | checkpatch (from inside `linux/`) | authorship test | threading |
|---|---|---|---|
| `0015` nvme APST | **clean** , 0 errors, 0 warnings, 0 checks, 8 lines | 62 passed | new thread, correct , no parent |
| `0021` v2 PCI/sysfs | 0 errors, **1 warning** (see below) | 62 passed | `--in-reply-to` matches v1's recorded message-id exactly |
| `0019` follow-up | n/a , prose reply, not a patch | n/a | `--in-reply-to` matches `0019`'s recorded message-id exactly |

**Threading checked against `../SUBMISSION.md`, not assumed.** Both ids match
character for character:

    0021 v1 : 20260820184228.166566-1-ferran.duarri@me.com
    0019    : 20260820190838.221435-1-ferran.duarri@me.com

Note the `0019` reply carries no `In-Reply-To:` header inside
`0019-followup.txt` itself , the threading comes from the `--in-reply-to` flag
on the send command above. That is correct, but it means **the flag is the only
thing keeping the reply in the thread**: sending that file without it starts a
new thread, which is worse than not replying.

**The one `0021` v2 warning, and why it is being left alone:**

    WARNING: Prefer a maximum 75 chars per line (possible unwrapped commit description?)
    #10: commit 56c1af4606f0 ("PCI: Add sysfs max_link_speed/width, current_link_speed/width, etc"),

That is the standard `commit <sha12> ("subject")` citation form that
`Documentation/process/submitting-patches.rst` asks for. The subject being
cited is itself 62 characters, so no wrapping keeps the line under 75 without
mangling a quoted title. Kernel history is full of merged commits that trip
this exact warning on citation lines. Rewriting it would mean editing the
commit message of a v2 whose v1 is already public , more risk than the warning
is worth. Left as-is deliberately; if a maintainer objects, wrap it in v3.

`0015`'s measurement IS in its commit message (Samsung 990 EVO Plus, ps 3 vs
ps 4, 60 interleaved trials per setting, p50/p90/p99/max table), together with
the caveat that five trials in six never finished descending to ps 4 and the
73mW idle-power cost. That was the standing condition on sending it , "only if
the measurement is correct and not contaminated" , and it is met.

## What each sent patch will be asked

- **`0017`** is the likeliest to get a substantive reply, and the reply will ask
  for the reproducer. The answer is VMware `vmnet`/`vmmon`: builds clean, loads
  clean, then fails packet forwarding at runtime because it inherited UBSAN
  flags it never asked for. `../SUBMISSION.md` carries the full version.
- **`0016`** offers no stall measurement and says so in its own commit message.
  The box has never reached the condition the patch guards against:
  `thp_fault_fallback` was 0 across 60682 huge-page faults, so direct
  compaction was never entered and `compact_stall 0` testifies to nothing. It
  rests on the correctness argument instead ,
  `CONFIG_TRANSPARENT_HUGEPAGE_ALWAYS`/`_MADVISE` decide *whether* THP applies,
  the defrag flag decides *how hard* the allocator works, and the initializer
  pins the second as though the first had decided it. Judgeable on the code
  alone. What would change it: fault-latency percentiles under real memory
  pressure at `madvise` versus `defer+madvise`.
- **`0019`** will be told it has no in-tree user. That is correct and the
  follow-up above says it first.
- **`0021`** documents four attributes exported since 2018 and changes no
  behaviour. v1 described them incorrectly; see the v2 section above. Do not
  wait for Bjorn to find it.

## Mail path

`git send-email` via `smtp.mail.me.com`, already configured in this repo and
the only send path used here. `sendemail.confirm always` and
`sendemail.annotate yes` are both set, so every send stops for confirmation.

**The password is not in git config and must not be**, this file is committed.
iCloud rejects the Apple ID password over SMTP; it needs an app-specific
password from <https://appleid.apple.com> under Sign-In and Security, which
`git send-email` prompts for at send time.

`linaro-mm-sig@lists.linaro.org` (on `0019`) is **moderated**. A post from a
non-subscriber waits in a queue rather than bouncing, so silence there is not a
delivery failure. The other lists are open.

**Archival has not been independently confirmed.** `lore.kernel.org` sits
behind an Anubis proof-of-work wall that refuses `curl` and an automated
browser alike, so the thread URLs in `../SUBMISSION.md` are constructed from
the message-ids rather than fetched. Every send Cc'd `ferran.duarri@me.com`, so
the copies in that mailbox are the delivery evidence , check there for bounces,
and open the lore URLs in a normal browser to confirm the lists accepted them.

## After sending

Record the message-id in `../SUBMISSION.md`, mark the row in its series table,
and `git mv` the patch into `../sent/`.

## Queued 2026-08-21 (evening) , the `0016` withdrawal. DRY-RUN VERIFIED.

`../replies/0016-withdrawal.txt`. Withdraws the THP defrag default patch
because its commit message has the mechanism backwards, and separately answers
the reviewer's "why not set this in the distro or boot config" question.

Recipients from `get_maintainer.pl --nogit --nogit-fallback --norolestats` run
against the sent patch, not from memory. `David Hildenbrand <david@kernel.org>`
is in that list and is who replied , **confirm the address against the `From:`
header of the mail you actually received before sending**, since more than one
David reviews mm.

    cd ~/Dev/kernel_inference/upstream-candidates/replies
    git send-email \
      --in-reply-to="<20260820190825.221308-1-ferran.duarri@me.com>" \
      --to="David Hildenbrand <david@kernel.org>" \
      --cc="Andrew Morton <akpm@linux-foundation.org>" \
      --cc="Lorenzo Stoakes <ljs@kernel.org>" \
      --cc=linux-mm@kvack.org \
      --cc=linux-kernel@vger.kernel.org \
      0016-withdrawal.txt

Dry run confirmed the threading: `Subject: Re: [PATCH] mm: thp: default defrag
mode to defer+madvise`, `In-Reply-To:` and `References:` both set to `0016`'s
message-id, so it lands in the existing thread rather than starting a new one.

**Record the printed `Message-ID:` in `../SUBMISSION.md` immediately.** That
file's own rule: a send is not done until its message-id is in the table.

### Why the withdrawal and not a v2

`vma_thp_gfp_mask()` gives a non-madvised fault `GFP_TRANSHUGE_LIGHT` with no
reclaim flag at all, and `GFP_TRANSHUGE_LIGHT` masks out `__GFP_RECLAIM`
outright (`include/linux/gfp_types.h:387`). So the stall the commit message
described cannot occur, and `defer+madvise` keeps `__GFP_DIRECT_RECLAIM` for
madvised regions in both modes. The patch removes no stall; it adds
`__GFP_KSWAPD_RECLAIM` to the non-madvised branch, which is more background
work, not less.

What survives, and is in the reply as a question rather than a claim: there is
no way to express this policy at build or boot time. No Kconfig symbol exists
for defrag (`mm/Kconfig` covers only the *enabled* axis), and
`setup_transparent_hugepage()` (`mm/huge_memory.c:1035`) sets only
`TRANSPARENT_HUGEPAGE_FLAG` and `TRANSPARENT_HUGEPAGE_REQ_MADV_FLAG`.
Tree-wide, `defrag_store()` is the only writer of the DEFRAG bits.

## Not for LKML: the `udmabuf` backport

`patches/custom/0022-udmabuf-do-not-create-malformed-scatterlists.patch` is
Jason Gunthorpe's `5bf888673e0d`, rebased onto 7.1.9. It is **Track B**: carried
with his authorship intact, never signed off by us, never sent as ours.

`5bf888673e0d` carries a `Fixes:` tag, landed in `v7.2`, and is absent from
7.1.y. Verified by content comparison (`git show <tag>:drivers/dma-buf/udmabuf.c`)
rather than log traversal, because the local kernel.org mirror is shallow.
7.1.y did take the later `DMA_ATTR_SKIP_CPU_SYNC` change without this one, so
the tree has the cacheline fix and still builds a per-4K scatterlist.

A note to `stable@vger.kernel.org` pointing that out is a normal request and
needs no authorship claim. It is not written yet.
