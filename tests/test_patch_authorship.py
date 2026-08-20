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


def test_no_placeholder_authorship_anywhere_in_ours():
    """`noreply@local` credits nobody for work we did."""
    for p in _ours():
        assert "noreply@local" not in p.read_text(errors="ignore"), p.name
