from __future__ import annotations
import re
import shutil
from pathlib import Path

from .util import log
from .util.run import run

FORWARD_PORT_RE = re.compile(r"^Forward-Port-Notes:\s*(.+)$", re.MULTILINE)
MARKER_RE = re.compile(r"^Conflict-Marker:\s*(.+)$", re.MULTILINE)


def patch_paths(patch_file: Path) -> list[str]:
    """Extract Forward-Port-Notes paths from a patch header."""
    text = patch_file.read_text(errors="ignore")
    m = FORWARD_PORT_RE.search(text)
    if not m:
        return []
    return [p.strip() for p in m.group(1).split(",") if p.strip()]


def patch_marker(patch_file: Path) -> str | None:
    text = patch_file.read_text(errors="ignore")
    m = MARKER_RE.search(text)
    return m.group(1).strip() if m else None


def check_mutual_exclusion(patches: list[Path]) -> list[str]:
    """Detect known conflict markers (e.g., BORE vs prjc)."""
    markers: dict[str, Path] = {}
    conflicts: list[str] = []
    EXCLUSIONS = {"bore": {"prjc"}, "prjc": {"bore"}}
    for p in patches:
        mk = (patch_marker(p) or "").lower()
        if not mk:
            continue
        for other_marker, file in markers.items():
            if other_marker in EXCLUSIONS.get(mk, set()):
                conflicts.append(f"{p.name} ({mk}) conflicts with {file.name} ({other_marker})")
        markers[mk] = p
    return conflicts


def seed_baseline(source_dir: Path, tag: str) -> None:
    """Create a PRISTINE git baseline from a freshly-extracted tree.

    Call ONLY immediately after extraction, when the working tree is
    guaranteed to contain zero applied patches (the source phase's job).
    Any pre-existing .git (a stale clone's history/tags, or a contaminated
    baseline minted by an earlier buggy run) is removed first, so the
    baseline commit *is* the pristine tree and nothing sits above it.
    """
    git_dir = source_dir / ".git"
    if git_dir.exists():
        log.info(f"removing pre-existing .git in {source_dir} before seeding a pristine baseline")
        shutil.rmtree(git_dir, ignore_errors=True)

    log.info(f"seeding pristine git baseline in {source_dir} (tag {tag})")
    run(["git", "init", "-q"], cwd=source_dir)
    run(["git", "config", "user.email", "hyphaed@localhost"], cwd=source_dir)
    run(["git", "config", "user.name", "hyphaed"], cwd=source_dir)
    run(["git", "add", "-A"], cwd=source_dir)
    run(["git", "commit", "-q", "-m", f"baseline {tag}", "--allow-empty"], cwd=source_dir)
    run(["git", "tag", "-f", tag], cwd=source_dir, check=False)


def baseline_tag_present(source_dir: Path, tag: str) -> bool:
    r = run(
        ["git", "rev-parse", "--verify", f"refs/tags/{tag}"],
        cwd=source_dir, check=False, force=True,
    )
    return r.ok()


def reset_to_baseline(source_dir: Path, tag: str) -> None:
    """Reset the tree to the pristine baseline the SOURCE phase seeded.

    Never mints a baseline from the working tree — that was the bug this
    replaces. If the tag is missing, the tree's provenance is unknown (or it
    predates this fix), so we refuse rather than silently baseline whatever
    is currently checked out.
    """
    run(["git", "am", "--abort"], cwd=source_dir, check=False)

    if not baseline_tag_present(source_dir, tag):
        log.err(f"pristine baseline tag '{tag}' not found in {source_dir}")
        log.err(
            "this tree was not seeded by the source phase (or predates the "
            "baseline fix). Re-run `python -m hyphaed --phase source` to "
            "re-extract a pristine tree, then retry the patch phase."
        )
        raise SystemExit(2)

    log.info(f"resetting {source_dir} to pristine baseline {tag}")
    run(["git", "reset", "--hard", tag], cwd=source_dir)
    # Remove untracked, non-ignored leftovers (e.g. a stray file left by a
    # partially-applied prior `git am`). No -x: .config and *.o are
    # gitignored, so build/config artifacts are preserved.
    run(["git", "clean", "-fdq"], cwd=source_dir, check=False)


def apply_series(source_dir: Path, patches: list[Path]) -> list[tuple[Path, bool, str]]:
    """Apply each patch with git am --3way. Fail-fast: stops at first failure.

    Returns [(patch, ok, msg)] for every patch we *attempted*. Patches after
    the first failure get (patch, False, 'skipped after earlier failure').
    Later patches often depend on earlier ones, so continuing past a failure
    would mask cascading errors.
    """
    results: list[tuple[Path, bool, str]] = []
    halt = False
    for patch in patches:
        if halt:
            results.append((patch, False, "skipped after earlier failure"))
            continue
        r = run(["git", "am", "--3way", str(patch)], cwd=source_dir, check=False)
        if r.ok():
            results.append((patch, True, "applied"))
        else:
            run(["git", "am", "--abort"], cwd=source_dir, check=False)
            msg = r.stderr[-1000:] or r.stdout[-1000:]
            # `git am --3way`'s "sha1 information is lacking or useless" names
            # whichever file happens to be first in the patch, not the one that
            # actually failed — this repo's git baseline is a fresh `git init`
            # over an extracted tarball, so there are no upstream blobs for
            # --3way to recover from. `git apply --check` reports the real
            # failing file/hunk instead; append it so the message is trustworthy.
            probe = run(["git", "apply", "--check", str(patch)], cwd=source_dir, check=False)
            if not probe.ok():
                probe_msg = probe.stderr[-1000:] or probe.stdout[-1000:]
                msg = f"{msg}\ngit apply --check says: {probe_msg}"
            results.append((patch, False, msg))
            halt = True
    return results


def diff_paths(source_dir: Path, base_tag: str, new_tag: str, paths: list[str]) -> str:
    if not paths:
        return ""
    r = run(["git", "diff", f"{base_tag}..{new_tag}", "--", *paths], cwd=source_dir, check=False)
    return r.stdout


def preflight_diff(source_dir: Path, old_tag: str, new_tag: str, patches: list[Path]) -> list[tuple[Path, str]]:
    """For each patch in `patches`, diff the paths listed in its Forward-Port-Notes
    between old_tag and new_tag. Returns list of (patch, diff_stat) for patches
    that have non-empty diffs — i.e. those likely to conflict on rebase.

    The caller should surface these as warnings before running git am.
    """
    flagged: list[tuple[Path, str]] = []
    # Check if the tags exist in this repo first
    r = run(["git", "tag", "-l"], cwd=source_dir, check=False)
    existing_tags = set(r.stdout.splitlines())
    if old_tag not in existing_tags or new_tag not in existing_tags:
        return []  # can't diff — tags not yet set up
    for p in patches:
        paths = patch_paths(p)
        if not paths:
            continue
        stat = run(
            ["git", "diff", "--stat", f"{old_tag}..{new_tag}", "--", *paths],
            cwd=source_dir, check=False,
        ).stdout.strip()
        if stat:
            flagged.append((p, stat))
    return flagged


def rebase_preflight_report(source_dir: Path, old_tag: str, new_tag: str, patches: list[Path]) -> None:
    """Log a human-readable pre-flight report. Call before apply_series on a rebase."""
    from .util import log
    flagged = preflight_diff(source_dir, old_tag, new_tag, patches)
    if not flagged:
        log.ok("rebase pre-flight: no likely conflicts detected")
        return
    log.warn(f"rebase pre-flight: {len(flagged)} patch(es) touch files that changed between {old_tag} and {new_tag}:")
    for p, stat in flagged:
        log.console.print(f"  [warn]⚠[/warn] {p.name}")
        for line in stat.splitlines()[:4]:
            log.console.print(f"      {line}")
    log.warn("these may need manual review after `git am` conflicts")
