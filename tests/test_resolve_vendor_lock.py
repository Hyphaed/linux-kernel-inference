"""`hyphaed fetch-patches` (and the wizard's "Fetch patches only") used to
call `patches/fetch.py` with no `--lock`, which defaults to the LEGACY
ubuntu-source-mode lock (patches/VENDOR.lock) — dead since --source-mode
kernel-org became the sole mode (2026-08-06). Confirmed live 2026-09-22:
this produced a false-alarm sha mismatch on a patch whose real,
currently-used lock entry (patches/VENDOR-kernel-org-7.2.lock) was correct
the whole time.

A first fix hardcoded "7.2" directly in install_wizard.sh — immediately
flagged as its own staleness trap: "user could select 7.3 soon ;; hence
the patch fetching must be intelligent and dynamic". `resolve_vendor_lock()`
(hyphaed/phases/patch.py) is the real fix: it derives the target series
from ctx.source_dir (what's actually checked out) or ctx.args.target
(--target's value, which always has a default), falling back to the
newest VENDOR-kernel-org-*.lock on disk if neither resolves — the same
resolution order _series_file_for() already uses for the real patch-apply
path, so fetch verification and patch application can never disagree about
which series is current.
"""
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hyphaed.phases.patch import resolve_vendor_lock

REPO = Path(__file__).resolve().parent.parent


@dataclass
class _FakeArgs:
    target: str | None = None


@dataclass
class _FakeCtx:
    repo_root: Path
    source_dir: Path | None = None
    args: _FakeArgs | None = None


def _ctx(source_dir=None, target=None) -> _FakeCtx:
    return _FakeCtx(repo_root=REPO, source_dir=source_dir, args=_FakeArgs(target=target))


def test_resolves_from_source_dir_first():
    lock = resolve_vendor_lock(_ctx(source_dir=Path("build/linux-7.2.7")))
    assert lock == REPO / "patches" / "VENDOR-kernel-org-7.2.lock"


def test_falls_back_to_target_when_no_source_dir():
    lock = resolve_vendor_lock(_ctx(source_dir=None, target="7.1.5"))
    assert lock == REPO / "patches" / "VENDOR-kernel-org-7.1.lock"


def test_source_dir_wins_over_target_when_both_present():
    """source_dir is what git am will actually run in — the only value
    that cannot disagree with reality, same priority _target_series_version
    already established."""
    lock = resolve_vendor_lock(_ctx(source_dir=Path("build/linux-7.1.10"), target="7.2.7"))
    assert lock == REPO / "patches" / "VENDOR-kernel-org-7.1.lock"


def test_falls_back_to_newest_on_disk_when_nothing_resolves():
    lock = resolve_vendor_lock(_ctx(source_dir=None, target=None))
    existing = sorted(
        (REPO / "patches").glob("VENDOR-kernel-org-*.lock"),
        key=lambda p: [int(x) for x in __import__("re").findall(r"\d+", p.stem)],
    )
    assert lock == existing[-1]


def test_falls_back_to_newest_when_target_points_at_a_missing_series():
    """A --target for a series with no lock yet (the exact '7.3 soon'
    scenario) must not hard-fail fetch-patches — it should fall back to
    the newest real series rather than erroring, since fetching patches is
    advisory/verification tooling, not the build-blocking series
    resolution _series_file_for() does for the actual patch-apply phase."""
    lock = resolve_vendor_lock(_ctx(source_dir=None, target="9.9.9"))
    assert lock is not None
    assert lock.exists()


def test_a_future_series_is_picked_up_with_zero_code_changes(tmp_path, monkeypatch):
    """The whole point of this function: bump cli.py's --target default (or
    fetch a new source tree) to a hypothetical future series, and the
    resolver must follow — no hardcoded version string anywhere to update."""
    fake_repo = tmp_path
    (fake_repo / "patches").mkdir()
    (fake_repo / "patches" / "VENDOR-kernel-org-7.2.lock").write_text("# 7.2\n")
    (fake_repo / "patches" / "VENDOR-kernel-org-7.3.lock").write_text("# 7.3\n")

    ctx = _FakeCtx(repo_root=fake_repo, source_dir=None, args=_FakeArgs(target="7.3.1"))
    lock = resolve_vendor_lock(ctx)
    assert lock == fake_repo / "patches" / "VENDOR-kernel-org-7.3.lock"

    # And with no target at all, auto-discover picks the newest (7.3, not 7.2).
    ctx2 = _FakeCtx(repo_root=fake_repo, source_dir=None, args=_FakeArgs(target=None))
    lock2 = resolve_vendor_lock(ctx2)
    assert lock2 == fake_repo / "patches" / "VENDOR-kernel-org-7.3.lock"


def test_returns_none_when_no_lock_exists_at_all(tmp_path):
    fake_repo = tmp_path
    (fake_repo / "patches").mkdir()
    ctx = _FakeCtx(repo_root=fake_repo, source_dir=None, args=_FakeArgs(target=None))
    assert resolve_vendor_lock(ctx) is None


def test_the_real_kernel_org_7_2_lock_verifies_clean():
    """Regression guard for the stale-pin find this session: 0013/0014's
    entries (../custom/0015-.../0016-...) carried sha256 pins from before
    the 2026-08-20 authorship-address rewrite. Re-pinned to match what
    patches/VENDOR.lock (the ubuntu-mode lock) already had right. This
    test doesn't re-derive the hash — it just fails loudly if the two
    files drift again without the lock being updated."""
    import hashlib
    lock_text = (REPO / "patches" / "VENDOR-kernel-org-7.2.lock").read_text()
    for name in ("0015-nvme-lower-default-apst-latency.patch",
                 "0016-mm-thp-defrag-defer-madvise-default.patch"):
        path = REPO / "patches" / "custom" / name
        real_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        assert real_sha in lock_text, (
            f"{name}'s real sha256 ({real_sha}) isn't in "
            f"VENDOR-kernel-org-7.2.lock — the pin has drifted again"
        )
