"""Every patch we wrote is authored to Ferran Duarri, and signed off.

Project rule (2026-08-20). Two separate requirements, both mandatory:

1. **Authorship.** A patch we wrote carries our name, whether or not it is
   ever sent upstream. Before this rule the six originals carried three
   different identities between them , `Ferran <ferran.duarri@me.com>`,
   `Ferran Duarri <ferran.duarri@pm.me>`, and two attributed to
   `hyphaed workstation tuning <noreply@local>`, which credits nobody for
   work we did. The kernel also wants a real full name, so the single-word
   `Ferran` form would not have been acceptable on submission either.

   The address is `@me.com` because that is the account patches are SENT
   from. iCloud publishes a strict DMARC policy, so a patch whose `From:`
   said `@pm.me` leaving an `smtp.mail.me.com` server would be misaligned,
   and vger's lists may reject or rewrite it. Author and envelope sender
   must be the same address, and it is the address that goes into Linux git
   history permanently, so it has to be one that receives mail.

2. **Signed-off-by.** This is not style. It is the attestation required by
   the kernel's Developer's Certificate of Origin, and a patch without it
   cannot be applied by a maintainer. `0017` had none at all and would have
   been rejected on contact.

Third-party patches under `patches/` keep their ORIGINAL authorship and are
deliberately not covered here , rewriting an upstream author's name would be
misattribution, which is the opposite of what this rule is for.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
AUTHOR = "Ferran Duarri <ferran.duarri@me.com>"
SOB = f"Signed-off-by: {AUTHOR}"

# Ours, decided by CONTENT rather than by location. An earlier version of this
# globbed two specific directories, and on 2026-08-20 that let three files keep
# a `From: Ferran <...>` short-name form the rule explicitly forbids , the same
# patch carried under patches/, patches/kernel-org-7.1/ and patches/xanmod-7.1/
# was simply outside the glob. Any file we authored is now checked wherever it
# sits, including the outbox and sent/ copies, which the glob also missed.
#
# Selection deliberately matches the ADDRESS only, not the full name: a patch
# with our address but a malformed name must be SELECTED so it can FAIL below.
_ADDRESS = "ferran.duarri@"

def _authored_by_us(path: Path) -> bool:
    for line in path.read_text(errors="replace").splitlines():
        if line.startswith("From: "):
            return _ADDRESS in line
        if line.startswith("---"):
            break
    return False

OURS = sorted(
    p
    for root in ("patches", "upstream-candidates")
    for p in (REPO / root).rglob("*.patch")
    if _authored_by_us(p)
)

# Third-party patches carried under patches/custom/ because they were
# forward-ported by us but AUTHORED elsewhere. Named explicitly so the
# exemption is a decision on the record, not a silent gap.
_NOT_OURS = {
    "0001-cachyos-bore-7.1.5.patch",      # CachyOS
    "0007-xanmod-zen-sched-latency.patch",  # XanMod
    "0014-tkg-pci-pme-timeout.patch",     # TKG / Clear Linux
    "0018-tkg-pci-acs-override.patch",    # TKG, and dropped from the series
}


def _ours() -> list[Path]:
    return [p for p in OURS if p.name not in _NOT_OURS]


def test_there_are_patches_to_check():
    """A glob that silently matches nothing would make every test below pass."""
    assert len(_ours()) >= 5


@pytest.mark.parametrize("patch", _ours(), ids=lambda p: p.name)
def test_authored_to_ferran_duarri(patch: Path):
    text = patch.read_text(errors="ignore")
    m = re.search(r"^From: (.*)$", text, flags=re.M)
    assert m, f"{patch.name} has no From: header"
    assert m.group(1).strip() == AUTHOR, (
        f"{patch.name} is authored to {m.group(1).strip()!r}, expected {AUTHOR!r}")


@pytest.mark.parametrize("patch", _ours(), ids=lambda p: p.name)
def test_has_exactly_one_correct_signoff(patch: Path):
    text = patch.read_text(errors="ignore")
    sobs = re.findall(r"^Signed-off-by: .*$", text, flags=re.M)
    assert sobs, (
        f"{patch.name} has no Signed-off-by. The kernel's DCO makes this "
        f"mandatory; a maintainer cannot apply the patch without it.")
    assert SOB in text, f"{patch.name} sign-off identities: {sobs}"


@pytest.mark.parametrize("patch", _ours(), ids=lambda p: p.name)
def test_signoff_is_inside_the_commit_message(patch: Path):
    """`git am` only reads trailers ABOVE the '---' separator. A sign-off
    below it lands in the diff comment area and is silently dropped."""
    text = patch.read_text(errors="ignore")
    sep = re.search(r"^---\s*$", text, flags=re.M)
    assert sep, f"{patch.name} has no '---' separator, not a format-patch mbox"
    assert SOB in text[:sep.start()], (
        f"{patch.name} sign-off is below the '---' separator and would be "
        f"dropped by git am")


ASSISTED = "Assisted-by: Claude:claude-opus-5"

#: Patches still in the outbox , the only ones an edit can still reach.
#: Anything under sent/ is a historical record of what actually went to a
#: mailing list; rewriting it would make our archive disagree with lore's,
#: which is worse than the missing tag. Those get the tag in their NEXT
#: revision (that is what 0021 v3 is), never retroactively.
def _sendable() -> list[Path]:
    outbox = REPO / "upstream-candidates" / "outbox"
    return sorted(p for p in outbox.glob("*.patch") if p.name not in _NOT_OURS)


def test_there_are_sendable_patches_to_check():
    assert _sendable(), "no patches in the outbox , this gate would pass vacuously"


@pytest.mark.parametrize("patch", _sendable(), ids=lambda p: p.name)
def test_ai_assisted_patches_carry_the_assisted_by_tag(patch: Path):
    """Every patch here was written with an AI coding assistant, and
    Documentation/process/coding-assistants.rst requires that to be declared.

    This test exists because it was not declared. `0021` went to linux-pci as
    v1 and v2 with no tag, and the maintainer asked "Did you forget the
    Assisted-by: tag?" before anyone here noticed. The rule was already in the
    tree we build against , checkpatch.pl even validates the format , and
    nothing on our side checked it.

    Format is AGENT_NAME:MODEL_VERSION, NOT an email address; checkpatch warns
    BAD_SIGN_OFF otherwise. Basic tools (git, gcc, make) are not listed.
    """
    text = patch.read_text(errors="ignore")
    assert ASSISTED in text, (
        f"{patch.name} has no Assisted-by tag. Every patch in this tree was "
        f"AI-assisted; declaring it is required, not optional.")


@pytest.mark.parametrize("patch", _sendable(), ids=lambda p: p.name)
def test_assisted_by_sits_above_the_signoff_and_inside_the_message(patch: Path):
    """Trailer order, and inside the commit message.

    `git am` silently drops anything below the `---` separator, so a tag there
    is a tag that never reaches the tree , the same trap the sign-off test
    already guards.
    """
    text = patch.read_text(errors="ignore")
    sep = re.search(r"^---$", text, flags=re.M)
    assert sep, f"{patch.name} has no '---' separator, not a format-patch mbox"
    body = text[:sep.start()]
    assert ASSISTED in body, (
        f"{patch.name}: Assisted-by is below the '---' separator, where git am "
        f"drops it")
    assert body.index(ASSISTED) < body.index(SOB), (
        f"{patch.name}: Assisted-by must come before Signed-off-by , the "
        f"human's DCO attestation is the last word on the patch")


def test_no_ai_signed_off_by():
    """AI agents MUST NOT add Signed-off-by (coding-assistants.rst).

    Only a human can certify the DCO. A sign-off naming a model would be a
    false legal attestation, not a style problem.
    """
    for p in _ours():                      # every patch, sent or not
        for line in p.read_text(errors="ignore").splitlines():
            if line.startswith("Signed-off-by:"):
                assert "claude" not in line.lower() and "gpt" not in line.lower(), (
                    f"{p.name}: an AI must never carry a Signed-off-by , "
                    f"only humans can certify the DCO")


def test_no_placeholder_authorship_anywhere_in_ours():
    """`noreply@local` credits nobody for work we did."""
    for p in _ours():
        assert "noreply@local" not in p.read_text(errors="ignore"), p.name


# ── Assisted-by , tool disclosure (project decision, 2026-08-21) ────────────
#
# Requirement 3, added the day it was earned. A maintainer replied to 0016
# with "Post walls of text that read exactly like they are AI-generated" and
# pointed at Documentation/process/generated-content.rst. He was right about
# the process: that document is In Scope when "a meaningful amount of content
# in a kernel contribution was not written by a person in the Signed-off-by
# chain", and it names this exact case in its examples , "The changelog was
# generated by handing the patch to a generative AI tool and asking it to
# write the changelog". A coding assistant was used on these changelogs and
# no patch said so.
#
# Documentation/process/coding-assistants.rst specifies the tag, and
# scripts/checkpatch.pl (the `Assisted-by:` branch) validates the format and
# WARNs on anything else , so a substituted tool name is not just dishonest
# to a third party, it fails their linter too.
#
# The decision is disclosure going forward, not retroactive rewriting of what
# was already posted. Patches sent before the decision are listed below, on
# the record, so the exemption is a decision rather than a silent gap , and
# so that anything NEW cannot skip it by accident.

ASSISTED_BY_RE = re.compile(
    r"^Assisted-by: (?P<agent>[^\s:]+):(?P<model>[^\s]+)(?P<tools>(?: [^\s]+)*)\s*$",
    flags=re.M,
)

# Posted before the 2026-08-21 disclosure decision. Do NOT add to this list;
# it exists to freeze the past, not to permit new omissions.
_PREDATES_DISCLOSURE_RULE = {
    "0015-nvme-lower-default-apst-latency.patch",
    "0016-mm-thp-defrag-defer-madvise-default.patch",
    "0017-kbuild-ubsan-extmod-opt-in.patch",
    "0019-dma-buf-priority-hint.patch",
    "0020-dma-buf-compressed-descriptor.patch",
    "0021-pci-sysfs-document-link-speed-width-attrs.patch",
    "0021-v2-pci-sysfs-document-link-speed-width-attrs.patch",
    "0021-v3-pci-sysfs-document-link-speed-width-attrs.patch",
    "0015-kbuild-ubsan-extmod-opt-in.patch",
    "0005-kbuild-ubsan-extmod-opt-in.patch",
    "0001-dma-buf-add-a-generic-compressed-content-descriptor.patch",
    "0001-dma-buf-add-a-generic-reclaim-priority-hint.patch",
}


def _needs_disclosure() -> list[Path]:
    return [p for p in _ours() if p.name not in _PREDATES_DISCLOSURE_RULE]


@pytest.mark.parametrize("patch", _ours(), ids=lambda p: p.name)
def test_assisted_by_format_is_valid_when_present(patch: Path):
    """A malformed tag is worse than none: checkpatch WARNs on it.

    Applies to every patch, grandfathered or not , if the tag is there at
    all it has to be in the format the kernel specifies.
    """
    text = patch.read_text(errors="ignore")
    for line in re.findall(r"^Assisted-by:.*$", text, flags=re.M):
        assert ASSISTED_BY_RE.match(line), (
            f"{patch.name}: {line!r} is not the format "
            f"coding-assistants.rst specifies and checkpatch.pl validates "
            f"('Assisted-by: AGENT_NAME:MODEL_VERSION [TOOL1] [TOOL2]')")


@pytest.mark.parametrize("patch", _needs_disclosure(), ids=lambda p: p.name)
def test_new_patches_disclose_tool_assistance(patch: Path):
    """Every patch authored after 2026-08-21 carries the tag."""
    text = patch.read_text(errors="ignore")
    assert ASSISTED_BY_RE.search(text), (
        f"{patch.name} has no Assisted-by tag. Project decision 2026-08-21: "
        f"patches we author disclose tool assistance, per "
        f"Documentation/process/generated-content.rst. Add e.g. "
        f"'Assisted-by: Claude:claude-opus-5' above the '---' separator.")


@pytest.mark.parametrize("patch", _ours(), ids=lambda p: p.name)
def test_assisted_by_is_inside_the_commit_message(patch: Path):
    """Same trap as Signed-off-by: `git am` drops trailers below '---'."""
    text = patch.read_text(errors="ignore")
    sep = re.search(r"^---\s*$", text, flags=re.M)
    if not sep:
        return
    for m in re.finditer(r"^Assisted-by:.*$", text, flags=re.M):
        assert m.start() < sep.start(), (
            f"{patch.name} has an Assisted-by below the '---' separator, "
            f"where git am will silently drop it")
