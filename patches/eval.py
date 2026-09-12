#!/usr/bin/env python3
"""
Evaluate a patch series against a kernel source tree.

Usage:
  python -m patches.eval --against <source-tree> --series <series-file>
  python -m patches.eval --against <source-tree> --sauce   # SAUCE discovery

Each patch is classified non-mutating (`git apply --check`) as:
  CLEAN          — applies cleanly; safe to include in the series.
  ALREADY-APPLIED — already in the tree (reverse-applies cleanly); upstreamed
                    or previously applied.
  CONFLICT       — has unresolvable conflicts; needs forward-porting.

In --sauce mode the tool runs `apt source linux`, evaluates every patch in
debian/patches/ against the given kernel.org tree, and copies the CLEAN
ones into patches/<series-dir>/sauce/ (default kernel-org-7.1, override with
--series-dir) with a generated sauce.series for human review and curation
before committing.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from enum import Enum
from pathlib import Path
from typing import NamedTuple

PATCH_DIR = Path(__file__).resolve().parent
REPO_ROOT  = PATCH_DIR.parent
OUT_DIR    = REPO_ROOT / "out"


# ── classification ─────────────────────────────────────────────────────────────

class Status(str, Enum):
    CLEAN          = "CLEAN"
    ALREADY_APPLIED = "ALREADY-APPLIED"
    CONFLICT       = "CONFLICT"


class PatchResult(NamedTuple):
    name:            str
    status:          Status
    forward_stderr:  str
    note:            str = ""


def classify_apply_result(
    forward_rc:     int,
    forward_stderr: str,
    reverse_rc:     int | None,
    reverse_stderr: str,
) -> Status:
    """Pure classification from `git apply` return codes — unit-testable.

    reverse_rc is None when not attempted (we only try reverse if forward fails).
    """
    if forward_rc == 0:
        return Status.CLEAN
    if reverse_rc is not None and reverse_rc == 0:
        return Status.ALREADY_APPLIED
    return Status.CONFLICT


# ── git interaction (non-mutating) ─────────────────────────────────────────────

def _git_apply_check(tree: Path, patch: Path, *, reverse: bool = False) -> tuple[int, str]:
    """Run `git apply --check [-R] -3 <patch>` against `tree` without touching the
    working tree. Returns (returncode, combined stderr+stdout)."""
    cmd = ["git", "-C", str(tree), "apply", "--check", "-3"]
    if reverse:
        cmd.append("-R")
    cmd.append(str(patch))
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode, (r.stderr + r.stdout).strip()


def eval_patch(tree: Path, patch: Path) -> PatchResult:
    """Classify one patch against `tree`. Non-mutating."""
    fwd_rc, fwd_err = _git_apply_check(tree, patch, reverse=False)
    rev_rc:  int | None = None
    rev_err: str        = ""
    if fwd_rc != 0:
        rev_rc, rev_err = _git_apply_check(tree, patch, reverse=True)
    status = classify_apply_result(fwd_rc, fwd_err, rev_rc, rev_err)
    note = ""
    if status == Status.ALREADY_APPLIED:
        note = "already in tree (upstreamed or previously applied)"
    elif status == Status.CONFLICT:
        first = fwd_err.splitlines()[0] if fwd_err else ""
        note = first[:120]
    return PatchResult(name=patch.name, status=status, forward_stderr=fwd_err, note=note)


def eval_series(tree: Path, patches: list[Path]) -> list[PatchResult]:
    results: list[PatchResult] = []
    for p in patches:
        # Transient progress line; the final table overwrites it.
        print(f"  checking {p.name[:56]:<56} …", end="\r", flush=True)
        results.append(eval_patch(tree, p))
    print(" " * 72, end="\r")
    return results


# ── series parsing ─────────────────────────────────────────────────────────────

def parse_series(series_file: Path) -> list[Path]:
    base = series_file.parent
    patches: list[Path] = []
    for line in series_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        p = base / line
        if not p.exists():
            print(f"  WARNING: listed but missing: {p}", file=sys.stderr)
            continue
        patches.append(p)
    return patches


# ── output ─────────────────────────────────────────────────────────────────────

_STATUS_FMT = {
    Status.CLEAN:           "\033[32mCLEAN         \033[0m",
    Status.ALREADY_APPLIED: "\033[33mALREADY-APPLIED\033[0m",
    Status.CONFLICT:        "\033[31mCONFLICT      \033[0m",
}


def print_table(results: list[PatchResult]) -> None:
    print(f"\n{'PATCH':<52}  {'STATUS':<18}  NOTE")
    print("─" * 115)
    for r in results:
        print(f"{r.name:<52}  {_STATUS_FMT[r.status]}  {r.note}")
    clean    = sum(1 for r in results if r.status == Status.CLEAN)
    applied  = sum(1 for r in results if r.status == Status.ALREADY_APPLIED)
    conflict = sum(1 for r in results if r.status == Status.CONFLICT)
    print(
        f"\n{len(results)} patches: "
        f"\033[32m{clean} CLEAN\033[0m, "
        f"\033[33m{applied} ALREADY-APPLIED\033[0m, "
        f"\033[31m{conflict} CONFLICT\033[0m"
    )


def write_json(results: list[PatchResult], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = out_dir / f"patch-eval-{date.today().isoformat()}.json"
    data = [{"name": r.name, "status": r.status, "note": r.note} for r in results]
    fname.write_text(json.dumps(data, indent=2))
    return fname


# ── SAUCE discovery mode ────────────────────────────────────────────────────────

def _deb_src_enabled() -> bool:
    """Replicates source.py logic without importing it (standalone script)."""
    for f in (
        list(Path("/etc/apt").glob("sources.list*")) +
        list(Path("/etc/apt/sources.list.d").glob("*.sources")) +
        list(Path("/etc/apt/sources.list.d").glob("*.list"))
    ):
        try:
            text = f.read_text()
        except OSError:
            continue
        if "deb-src" in text or re.search(r"^Types:.*\bdeb-src\b", text, re.MULTILINE):
            return True
    r = subprocess.run(["apt-cache", "policy"], capture_output=True, text=True)
    return "deb-src" in r.stdout.lower() or "/source/" in r.stdout.lower()


def _apt_source_linux(work_dir: Path) -> Path | None:
    """Run `apt source linux` in work_dir, return the extracted source tree."""
    print("  running apt source linux (downloads ~250 MB; one-time operation) …", flush=True)
    r = subprocess.run(
        ["apt", "source", "linux"],
        cwd=str(work_dir), capture_output=False,
    )
    if r.returncode != 0:
        return None
    dirs = sorted(
        p for p in work_dir.iterdir()
        if p.is_dir() and re.match(r"linux-[0-9]", p.name)
    )
    return dirs[-1] if dirs else None


def sauce_dir_for(series_dir: str) -> Path:
    """Where --sauce copies CLEAN patches for a given series directory name."""
    return PATCH_DIR / series_dir / "sauce"


def cmd_sauce(tree: Path, dry_run: bool = False, series_dir: str = "kernel-org-7.1") -> int:
    """Discover which Ubuntu SAUCE patches apply cleanly to the given tree."""
    sauce_dir = sauce_dir_for(series_dir)
    if not _deb_src_enabled():
        print(
            "error: deb-src is not enabled — Ubuntu source packages are unavailable.\n"
            "  Fix: sudo sed -i '/^# *deb-src /s/^# *//' /etc/apt/sources.list && sudo apt update",
            file=sys.stderr,
        )
        return 2

    with tempfile.TemporaryDirectory(prefix="hyphaed-sauce-") as td:
        work_dir = Path(td)
        src_tree = _apt_source_linux(work_dir)
        if src_tree is None:
            print("error: apt source linux failed", file=sys.stderr)
            return 1

        series_file = src_tree / "debian" / "patches" / "series"
        if not series_file.exists():
            # Some Ubuntu versions put patches directly in debian/patches/
            all_patches = sorted((src_tree / "debian" / "patches").glob("*.patch"))
        else:
            all_patches = parse_series(series_file)

        if not all_patches:
            print("no patches found in Ubuntu source debian/patches/", file=sys.stderr)
            return 1

        print(f"  evaluating {len(all_patches)} Ubuntu SAUCE patches against {tree.name} …")
        results = eval_series(tree, all_patches)
        print_table(results)

        clean_results = [r for r in results if r.status == Status.CLEAN]
        if not clean_results:
            print("\nno SAUCE patches apply cleanly to this tree.")
            return 0

        if dry_run:
            print(f"\n(dry-run) would copy {len(clean_results)} CLEAN patches to {sauce_dir}")
            return 0

        sauce_dir.mkdir(parents=True, exist_ok=True)
        for r in clean_results:
            # Find the source patch file
            src = next((p for p in all_patches if p.name == r.name), None)
            if src:
                shutil.copy2(src, sauce_dir / r.name)

        sauce_series = sauce_dir / "sauce.series"
        sauce_series.write_text(
            "# Auto-generated by patches/eval.py --sauce\n"
            f"# Review and curate before appending to patches/{series_dir}/series.\n"
            f"# Each entry here is CLEAN against the {tree.name} tree — but check:\n"
            "#   1. Is it Ubuntu-infrastructure-only? (skip those)\n"
            "#   2. Is it genuinely useful for the AI/gaming/VM workload?\n"
            "#   3. Does it interact with any existing patch in the series?\n"
            "# After curation: pin sha256s via patches/fetch.py --discover\n\n"
            + "".join(f"{r.name}\n" for r in clean_results)
        )
        print(
            f"\n{len(clean_results)} CLEAN patches copied to {sauce_dir}\n"
            f"Review and curate {sauce_series}\n"
            f"then append chosen entries to patches/{series_dir}/series\n"
            f"and pin sha256s: python patches/fetch.py --discover"
        )

    # Write JSON report
    out_path = write_json(results, OUT_DIR)
    print(f"Full eval report: {out_path}")
    return 0


# ── CLI entry ───────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--against", required=True, metavar="SOURCE_TREE",
        help="path to the kernel source tree to test patches against",
    )
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--series", metavar="SERIES_FILE",
        help="evaluate patches listed in this series file",
    )
    mode.add_argument(
        "--sauce", action="store_true",
        help="SAUCE discovery: apt source Ubuntu linux, eval, copy CLEAN patches to "
             "patches/<series-dir>/sauce/ for review (see --series-dir)",
    )
    ap.add_argument(
        "--series-dir", default="kernel-org-7.1",
        help="--sauce only: series directory name under patches/ to copy CLEAN "
             "patches into and to reference in the printed follow-up advice "
             "(default: kernel-org-7.1). E.g. --series-dir kernel-org-7.2.",
    )
    ap.add_argument(
        "--out-dir", default=None,
        help=f"directory for patch-eval-<date>.json (default: {OUT_DIR})",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="print what would be done without writing files (--sauce only)",
    )
    args = ap.parse_args(argv)

    tree = Path(args.against).resolve()
    if not (tree / "Makefile").exists():
        print(
            f"error: {tree} does not look like a kernel source tree (no Makefile)",
            file=sys.stderr,
        )
        return 2

    if args.sauce:
        return cmd_sauce(tree, dry_run=args.dry_run, series_dir=args.series_dir)

    series_file = Path(args.series).resolve()
    patches = parse_series(series_file)
    if not patches:
        print(f"no patches found in {series_file}", file=sys.stderr)
        return 1

    print(f"evaluating {len(patches)} patches against {tree.name} …")
    results = eval_series(tree, patches)
    print_table(results)

    out_dir = Path(args.out_dir) if args.out_dir else OUT_DIR
    json_path = write_json(results, out_dir)
    print(f"\nresults written to {json_path}")

    return 0 if all(r.status == Status.CLEAN for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
