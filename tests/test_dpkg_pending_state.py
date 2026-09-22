"""`_dpkg_pending_count()` (hyphaed/phases/detect.py) and its shell mirror
(scripts/fix-dpkg-state.sh) both reported "2 pending item(s)" on a
perfectly healthy box, found live 2026-09-22 via the wizard's "Check build
deps" screen.

Root cause: dpkg's `Status:` line is `<want> <eflag> <status>` (Debian
policy). The old check matched the WHOLE line against three fixed strings
("install ok installed" / "deinstall ok config-files" /
"purge ok not-installed") — which means only ONE `want` value was ever
recognized as valid per `status`, when `want` (unknown/install/hold/
deinstall/purge) is really an independent axis from whether the package
is actually mid-transaction. The two real false positives:
  - `slack-desktop -> Status: hold ok installed` — a deliberately
    `apt-mark hold`ed package, fully installed and fine.
  - `thunderbird -> Status: install ok not-installed` — Thunderbird is
    actually Flatpak-managed on this box (org.mozilla.thunderbird_esr);
    dpkg kept a stale never-completed tracking stanza for the apt package.

Both have `eflag=ok` and a genuinely settled `status`; only checking those
two fields (not `want`) is correct.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hyphaed.phases.detect import _dpkg_status_is_pending

REPO = Path(__file__).resolve().parent.parent


def test_held_but_installed_package_is_not_pending():
    assert not _dpkg_status_is_pending("hold ok installed")


def test_never_completed_install_tracked_by_dpkg_is_not_pending():
    """Settled terminal state — dpkg wanted to install it, it never
    unpacked, and nothing is stuck mid-transaction."""
    assert not _dpkg_status_is_pending("install ok not-installed")


def test_the_three_originally_hardcoded_combos_still_settle():
    assert not _dpkg_status_is_pending("install ok installed")
    assert not _dpkg_status_is_pending("deinstall ok config-files")
    assert not _dpkg_status_is_pending("purge ok not-installed")


def test_deinstall_ok_not_installed_is_also_settled():
    """A package fully removed (purged) after having been marked for
    deinstall — a real, valid dpkg terminal state the old three-string
    whitelist would have missed too."""
    assert not _dpkg_status_is_pending("deinstall ok not-installed")


def test_genuinely_interrupted_statuses_are_still_caught():
    for status in ("unpacked", "half-configured", "half-installed",
                   "triggers-awaited", "triggers-pending"):
        assert _dpkg_status_is_pending(f"install ok {status}"), status


def test_reinstreq_eflag_is_caught_regardless_of_status():
    assert _dpkg_status_is_pending("install reinstreq installed")


def test_malformed_line_is_conservative_not_silent():
    assert _dpkg_status_is_pending("garbage")


def test_shell_mirror_uses_the_same_eflag_status_structural_check():
    """scripts/fix-dpkg-state.sh's awk must check eflag/status structurally
    (last two fields), not the whole line against a fixed string list —
    otherwise the Python port and the shell script can disagree again."""
    src = (REPO / "scripts" / "fix-dpkg-state.sh").read_text()
    assert 'eflag  = f[n-1]' in src or 'eflag = f[n-1]' in src, (
        "pending_packages() should derive eflag/status from field position, "
        "not re-introduce a fixed 'want eflag status' string whitelist"
    )
    assert "install ok installed" not in src.split("pending_packages()")[1].split("}")[0]
