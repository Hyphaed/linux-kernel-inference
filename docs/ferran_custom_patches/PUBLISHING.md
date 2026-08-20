# Publishing guide — two tracks, kept strictly separate

This page exists because "publish these patches so people can download
them" has two legitimate but legally different meanings, and conflating
them is the mistake to avoid:

- **Track A — send to LKML.** Only the 5 patches that are actually ours to
  sign off on (`0015`, `0016`, `0017`, `0019`, `0020`). Sending requires a
  `Signed-off-by:` under the kernel's Developer's Certificate of Origin —
  a legal attestation that you wrote the patch or otherwise have the right
  to submit it. `UPSTREAMING.md` already covers this procedure in detail
  for a single patch (originally written for `0019`); this page **modernizes
  and generalizes it** rather than duplicating it — read `UPSTREAMING.md`
  first, this page layers `b4` and multi-patch-series handling on top.

- **Track B — a public downloadable patch collection.** All 20 files,
  including the 15 carried from CachyOS/XanMod/Liquorix/TKG. This is
  legitimate for the *whole* set: redistributing someone else's GPL'd
  patch with their original authorship intact is redistribution, not
  authorship fraud. What Track A forbids — signing off on someone else's
  work as if you wrote it — has no equivalent restriction on simply
  hosting their patch file with their name still on it, exactly the way
  CachyOS's own `kernel-patches` repo or XanMod's tree do for patches they
  didn't originate either.

Never confuse the two. A patch appearing in the Track B collection does
**not** make it eligible for Track A, and vice versa.

## Attribution matrix

The authoritative split, derived from `README.md`'s existing
imported-vs-original catalog so this table can't drift from it silently.

| # | Subject | Real author | Track A (LKML) | Track B (public collection) | Why |
|---|---|---|:-:|:-:|---|
| 0001 | bore scheduler | Piotr Gorski (CachyOS) | ✗ | ✓ | Not ours to sign off on |
| 0002 | tcp_bbr v3 | Oleksandr Natalenko (XanMod) | ✗ | ✓ | Not ours to sign off on |
| 0003 | vmscan swap reduction | Alexandre Frade (XanMod) | ✗ | ✓ | Not ours to sign off on |
| 0004 | vfs cache reclaim rate | Alexandre Frade (XanMod) | ✗ | ✓ | Not ours to sign off on |
| 0005 | max_map_count default | Alexandre Frade (XanMod) | ✗ | ✓ | Not ours to sign off on |
| 0006 | 500Hz timer option | Alexandre Frade (XanMod) | ✗ | ✓ | Not ours to sign off on |
| 0007 | sched tunable latencies | Alexandre Frade (XanMod) | ✗ | ✓ | Not ours to sign off on |
| 0008 | dm-crypt workqueue disable | Steven Barrett (Liquorix) | ✗ | ✓ | Not ours to sign off on |
| 0009 | evdev call_rcu | Kenny Levinsen (Liquorix) | ✗ | ✓ | Not ours to sign off on |
| 0010 | blk-wbt latency | Alexandre Frade (XanMod) | ✗ | ✓ | Not ours to sign off on |
| 0011 | rq_affinity force | Alexandre Frade (XanMod) | ✗ | ✓ | Not ours to sign off on |
| 0012 | mq-deadline front_merges | Alexandre Frade (XanMod) | ✗ | ✓ | Not ours to sign off on |
| 0013 | mq-deadline write priority | Alexandre Frade (XanMod) | ✗ | ✓ | Not ours to sign off on |
| 0014 | PME_TIMEOUT 4000ms | Arjan van de Ven (TKG) | ✗ | ✓ | Not ours to sign off on |
| 0015 | nvme APST latency default | Ferran | ✓ (needs broader evidence first — see `proposals.md`) | ✓ | Ours, but is a local default change, not yet a send candidate |
| 0016 | THP defrag default | Ferran | ✓ (needs broader evidence first — see `proposals.md`) | ✓ | Ours, but is a local default change, not yet a send candidate |
| 0017 | kbuild UBSAN extmod opt-in | Ferran | ✓ (plausible, one archive check first) | ✓ | Ours, plausible candidate |
| 0018 | PCI ACS override | Mark Weiman (TKG, fork of Alex Williamson) | ✗ | ✓ | Not ours to sign off on; also historically contentious upstream in its own right |
| 0019 | dma-buf reclaim-priority hint | Ferran | ✓ **ready to send** | ✓ | Ours, checkpatch-clean, real consumer |
| 0020 | dma-buf compressed-content descriptor | Ferran | ✓ (land the GreenBoost consumer first — see `proposals.md`) | ✓ | Ours, checkpatch-clean, consumer not yet landed |

Regenerate this table from `README.md`'s catalog whenever a patch is
added, removed, or reclassified — do not hand-edit the two out of sync.

---

## Track A — LKML submission (originals only)

`UPSTREAMING.md` remains the canonical single-patch walkthrough (written
against `0019`, applies unchanged to `0017` or `0020`) — steps 3–6 there
(final review, `get_maintainer.pl`, format-and-send, after-sending) are not
repeated here. What changed since it was written: **`b4` is now the
maintainer-preferred tool** for anything beyond a single trivial patch, and
`0019`+`0020` are a natural 2-patch series rather than two independent
sends. This section covers both.

### Why b4 over bare git-send-email for a series

`git send-email` (covered in `UPSTREAMING.md`) still works and is the right
tool for a genuinely standalone one-off patch. `b4` adds, on top of the same
underlying SMTP send:

- **Series threading** — a cover letter plus N patches sent as one properly
  threaded set, without hand-managing `In-Reply-To` headers.
- **Message-ID tracking** — `b4 prep` records what was sent, so a `v2` reroll
  after review feedback references the right thread automatically.
- **`b4 trailers`** — pulls `Reviewed-by:`/`Acked-by:` trailers a maintainer
  posts as a reply, back into your local commits, instead of manually
  copy-pasting them into a `v2`.

### 1. Install

```bash
sudo apt install b4
```

(`0.14.3-1` is the current Ubuntu-archive version as of this session —
confirmed via `apt-cache policy b4`; not yet installed on this box.) `b4`
uses the same `git send-email`/SMTP configuration `UPSTREAMING.md`'s step 1
already set up — no separate credentials needed.

### 2. Prepare a series (example: `0019` + `0020` together)

```bash
cd ~/Dev/kernel_inference/build/linux-7.1.8   # or wherever the two commits live
b4 prep -n dma-buf-hints -c HEAD~2..HEAD
```

`-c` marks it as a cover-letter series; `b4 prep` opens an editor for the
cover letter text — explain the shared motivation (an exporter's opinion
about a buffer it has decided to keep: `0019` says *whether*, `0020` says
*how it's stored*) the way both patches' own commit messages already do
individually.

### 3. Final review — unchanged from `UPSTREAMING.md` step 3

```bash
perl scripts/checkpatch.pl --no-tree <patch-file>
```

Re-run fresh against the exact files about to be sent, every time, even if
already checked during drafting (both `0019` and `0020` are 0 errors/0
warnings as of this session, but re-verify, don't trust a stale result).

### 4. Recipients — unchanged from `UPSTREAMING.md` step 4, derive, never guess

```bash
perl scripts/get_maintainer.pl <patch-file>
```

For the `0019`+`0020` dma-buf series specifically, already derived this
session: `Sumit Semwal <sumit.semwal@linaro.org>`,
`Christian König <christian.koenig@amd.com>`,
`linux-media@vger.kernel.org`, `dri-devel@lists.freedesktop.org`,
`linaro-mm-sig@lists.linaro.org` (moderated), `linux-kernel@vger.kernel.org`
— see `upstream-candidates/dma-buf-compressed-descriptor/README.md` for the
exact command that produced this.

### 5. Send

```bash
b4 send
```

Shows the composed series for confirmation before sending, the same
`confirm = always` safety net `git send-email` provides directly. For a
single non-series patch, plain `git send-email` per `UPSTREAMING.md` step 5
remains perfectly fine — `b4` earns its keep on a series or when you expect
a review round-trip, not for every send.

### 6. After sending, and handling review feedback

Same expectations as `UPSTREAMING.md` step 6 (permanent public archive,
moderation delay possible, Patchwork tracking, no reply within weeks being
normal). Where `b4` earns its keep is the reroll:

```bash
b4 trailers -u                          # pull any posted Reviewed-by/Acked-by
# make the requested changes, amend commits
b4 prep --edit-cover                    # update cover letter if needed
b4 send                                 # sends as v2, threaded correctly
```

vs. the manual `UPSTREAMING.md` step 6 equivalent
(`git format-patch ... --subject-prefix="RFC PATCH v2"` +
`git send-email --in-reply-to=<message-id>`), which still works and is
documented there as the fallback if `b4` isn't installed or behaves
unexpectedly.

### DCO and the AI-disclosure note — unchanged, not repeated here

Both carry over verbatim from `UPSTREAMING.md`'s existing sections — the
`Signed-off-by:` DCO attestation explanation and the note about disclosing
AI assistance are correct as written there and apply identically whether
sent via `b4` or bare `git send-email`.

---

## Track B — public downloadable patch collection

This is the actual answer to "so people can download them" for the full
set, not just the 5 originals. Structured to match the convention this
repo's own `kernel-patches`/XanMod local mirrors already use, so applying
the collection is exactly `git am` or `git apply` against a matching kernel
tree — no custom tooling required on the consumer's side.

### Layout

```
hyphaed-patches/
├── README.md          # what this is, provenance table (= the attribution
│                       # matrix above), apply instructions
├── LICENSE             # GPL-2.0 — with a note that each patch individually
│                        # carries its original author's copyright/terms;
│                        # this collection's own scaffolding (README, series
│                        # files) is GPL-2.0, the patches keep their own
├── 7.1/
│   ├── series          # ordered list, same format hyphaed's own
│   │                    # patches/kernel-org-7.1/series already uses
│   ├── sched/0001-cachyos-bore-7.1.8.patch
│   ├── net/0002-xanmod-bbr3.patch
│   ├── mm/000{3,4,5}-xanmod-*.patch
│   ├── ...              # every file, original From: header intact,
│   │                     # unchanged content — this is redistribution,
│   │                     # not a rewrite
│   ├── pci/0018-tkg-pci-acs-override.patch
│   ├── dma-buf/0019-dma-buf-priority-hint.patch
│   ├── dma-buf/0020-dma-buf-compressed-descriptor.patch
│   └── sha256sums.txt   # matches patches/VENDOR-kernel-org-7.1.lock's
│                         # existing hashes — one source of truth, not two
└── configs/              # configs/fragments/*.config, so a consumer can
                           # reproduce a full preset, not just individual
                           # patches
```

### What NOT to change when copying a patch into this collection

- **Never edit the `From:`/`Author:` line.** The whole point of Track B
  being legitimate is that attribution stays intact.
- **Never add your own `Signed-off-by:`** to an imported patch, even in the
  public collection — that trailer means something specific (DCO
  attestation) and doesn't belong on a patch you're merely redistributing.
  A `Co-developed-by:` or attestation of your own is only appropriate on
  the 5 originals.
- **Do carry forward each patch's original license header/SPDX tag** if
  it has one; don't relabel anything as your own.

### Publishing mechanics

1. **One git repo, tagged per kernel point release** the way
   CachyOS's `kernel-patches` and XanMod's tree already do it:
   ```bash
   git tag v7.1.8-hyphaed1
   git push origin v7.1.8-hyphaed1
   ```
2. **A GitHub Release per tag**, tarball attached, release notes linking
   back to this repo's own `docs/ferran_custom_patches/README.md` for the
   full catalog context and to each original's `upstream-candidates/`
   writeup for the ones that are also LKML candidates.
3. **`AUTHORS.md`** at the collection root — a flattened version of the
   attribution matrix above (name, patch numbers, source project, upstream
   link where one exists), so a consumer of the collection alone, without
   this repo's full context, still sees who wrote what at a glance.
4. **Applying the collection** — documented in the collection's own
   `README.md`, but the short version for any consumer:
   ```bash
   git clone --branch v7.1.8-hyphaed1 <collection-url> hyphaed-patches
   cd path/to/a/matching/kernel-source/tree
   git am hyphaed-patches/7.1/sched/0001-cachyos-bore-7.1.8.patch \
          hyphaed-patches/7.1/dma-buf/0019-dma-buf-priority-hint.patch \
          ...   # or loop over hyphaed-patches/7.1/series
   ```

### Keeping it in sync with this repo

`patches/kernel-org-7.1/series` and `patches/VENDOR-kernel-org-7.1.lock`
remain the single source of truth for what's active and what its verified
sha256 is. The Track B collection is a **publication artifact derived from
them**, not a second place either file gets hand-edited — regenerate the
collection's `series`/`sha256sums.txt` from those two files (a small script
reading `VENDOR-kernel-org-7.1.lock` and copying by path is enough; not
written yet, add it if the collection actually gets published) rather than
maintaining a third copy by hand.

---

## Nothing here has been sent or published

As of this writing: `b4` is not installed, no series has been prepared, no
collection repo exists yet, and no `git send-email`/`b4 send` has run.
Every command on this page is something you run yourself, deliberately,
when ready — consistent with `UPSTREAMING.md`'s standing note that sending
is irreversible and permanently public.
