#!/usr/bin/env python3
"""
Fetch and verify vendored patches per patches/VENDOR.lock.

Usage:
  python -m patches.fetch              # fetch + verify against locked shas
  python -m patches.fetch --discover   # fetch, print computed sha (lock manually)

Each line in VENDOR.lock:
  <local-name>  <url>  <sha256-or-PINNED>  <kind>

kind:
  local-file  — URL is file:///abs/path; bytes read from disk as-is.
  git-commit  — URL is git+file:///abs/repo@<sha>; bytes produced by
                `git format-patch -1 --no-signature --stdout <sha>`.
"""
from __future__ import annotations
import argparse
import hashlib
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

PATCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = PATCH_DIR.parent
LOCK = PATCH_DIR / "VENDOR.lock"

# Lock files record where each patch's bytes came from. Those sources are
# local working trees, so a literal absolute path would pin the lock to one
# developer's home directory and leak it into a public repo. `$KI_ROOT` stands
# for the checkout root and is expanded here, so the same lock file resolves
# on any machine and the recorded sha256 still means what it says.
_ROOT_TOKEN = "$KI_ROOT"


def expand_root(url: str) -> str:
    """Expand the $KI_ROOT placeholder in a lock URL to this checkout."""
    return url.replace(_ROOT_TOKEN, str(REPO_ROOT))


def base_dir_for(lock_path: Path) -> Path:
    """Where a lock's entry names resolve from — the same base directory its
    matching series file uses. VENDOR.lock (ubuntu mode) sits in patches/ and
    its entries are bare names in patches/ directly. Each per-series lock
    (VENDOR-kernel-org-7.2.lock, VENDOR-xanmod-7.1.lock, ...) has its entries
    resolve relative to patches/<suffix>/ — the series directory alongside it
    — exactly like patches/kernel-org-7.2/series's own entries do, so a name
    like ../custom/0001-foo.patch means the same thing in both files.

    Previously every lock resolved against patches/ regardless of which one
    it was, which was only ever correct for VENDOR.lock: a per-series lock's
    bare entries landed one directory up from where the series file expected
    them, and its ../custom/ entries landed one directory up from patches/
    entirely (at the repo root).
    """
    name = lock_path.stem  # strips ".lock"
    if name.startswith("VENDOR-"):
        return PATCH_DIR / name[len("VENDOR-"):]
    return PATCH_DIR


def parse_lock(lock_path: Path = LOCK) -> list[tuple[str, str, str, str]]:
    entries = []
    for lineno, raw in enumerate(lock_path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 4:
            print(f"  WARNING: VENDOR.lock line {lineno} has fewer than 4 fields — skipped: {raw!r}")
            continue
        name, url, sha, kind = parts[0], parts[1], parts[2], parts[3]
        entries.append((name, expand_root(url), sha, kind))
    return entries


def sha256_bytes(buf: bytes) -> str:
    return hashlib.sha256(buf).hexdigest()


def _fetch_local_file(url: str) -> bytes:
    parsed = urlsplit(url)
    if parsed.scheme != "file":
        raise ValueError(f"expected file:// URL, got {url!r}")
    path = Path(parsed.path)
    if not path.exists():
        raise FileNotFoundError(f"local-file not found: {path}")
    return path.read_bytes()


def _fetch_git_commit(url: str) -> bytes:
    if not url.startswith("git+file://"):
        raise ValueError(f"expected git+file:// URL, got {url!r}")
    rest = url[len("git+file://"):]
    at = rest.rfind("@")
    if at == -1:
        raise ValueError(f"git+file URL missing @<sha>: {url!r}")
    repo_path, sha = rest[:at], rest[at + 1:]
    if not repo_path or not sha:
        raise ValueError(f"malformed git+file URL: {url!r}")
    repo = Path(repo_path)
    if not repo.exists():
        raise FileNotFoundError(f"git repo not found: {repo}")
    probe = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"],
        capture_output=True,
    )
    if probe.returncode != 0:
        raise RuntimeError(
            f"commit {sha[:12]} not found in {repo}; "
            f"check the repo is on the right branch\n"
            f"  try: git -C {repo} log --oneline | grep {sha[:8]}"
        )
    r = subprocess.run(
        ["git", "-C", str(repo), "format-patch", "-1", "--no-signature", "--stdout", sha],
        capture_output=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"git format-patch failed: {r.stderr.decode().strip()}")
    return r.stdout


def fetch_entry(url: str, kind: str) -> bytes:
    if kind == "local-file":
        return _fetch_local_file(url)
    elif kind == "git-commit":
        return _fetch_git_commit(url)
    else:
        raise ValueError(f"unknown kind {kind!r}; expected local-file or git-commit")


# Kept for backwards-compatibility with existing tests; not used in fetch flow.
FALLBACK_BRANCHES = ["master", "main", "v7.0", "7.0", "linux-7.0.y", "stable", "develop"]


def _candidates(url: str) -> list[str]:
    """Expand a URL into candidate forms by swapping branch tokens."""
    out = [url]
    for current in FALLBACK_BRANCHES:
        marker = f"/{current}/"
        if marker not in url:
            continue
        for swap in FALLBACK_BRANCHES:
            if swap == current:
                continue
            out.append(url.replace(marker, f"/{swap}/", 1))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--discover", action="store_true",
                    help="fetch and print sha256s (don't fail on PINNED placeholders)")
    ap.add_argument("--try-branches", action="store_true",
                    help="(deprecated, no effect — local-source mode has no branch fallback)")
    ap.add_argument("--lock", type=Path, default=LOCK,
                    help=f"path to the lock file to fetch/verify (default: {LOCK.name}, "
                         "the ubuntu-mode lock — pass e.g. "
                         "VENDOR-kernel-org-7.2.lock to verify a per-series lock instead; "
                         "entries resolve into patches/<suffix>/, see base_dir_for())")
    args = ap.parse_args(argv)

    if args.try_branches:
        print("  note: --try-branches is deprecated and has no effect in local-source mode")

    failures = 0
    discovered: list[tuple[str, str, str]] = []
    base_dir = base_dir_for(args.lock)

    for name, url, expected_sha, kind in parse_lock(args.lock):
        dst = base_dir / name
        print(f"→ {name}")
        try:
            data = fetch_entry(url, kind)
        except Exception as e:
            print(f"  fetch FAILED: {e}")
            failures += 1
            continue
        got = sha256_bytes(data)
        if expected_sha == "PINNED":
            if args.discover:
                discovered.append((name, got, url))
                print(f"  computed sha256 = {got}")
            else:
                print(f"  PINNED placeholder — rerun with --discover or update VENDOR.lock")
                failures += 1
                continue
        elif got != expected_sha:
            print(f"  sha mismatch! expected {expected_sha} got {got}")
            failures += 1
            continue
        dst.write_bytes(data)
        print(f"  saved → {dst.name}")

    if discovered:
        print(f"\nlock these in {args.lock.name} (replace PINNED sha with the computed value):")
        for name, sha, url in discovered:
            print(f"  {name}  {url}  {sha}")

    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
