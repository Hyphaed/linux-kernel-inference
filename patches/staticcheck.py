#!/usr/bin/env python3
"""
Run the kernel's static analysers over only the files a patch series touches.

Usage:
  python -m patches.staticcheck --against build/linux-7.1.9 \
                                --series patches/kernel-org-7.1/series
  python -m patches.staticcheck --against build/linux-7.1.9 --files drivers/dma-buf

Why this exists, and what it is NOT for.

`checkpatch.pl` is already run before every send, and it is a style and patch
hygiene checker. It has never had anything to say about whether the code is
correct. The analysers below do:

  sparse   type and address-space checking. This is the one that matters most
           for what this repo actually writes: 0019 and 0020 add dma-buf
           ioctls, and a missing or wrong __user annotation on an ioctl
           argument is exactly the class of bug sparse exists to catch and
           that gcc will compile without a word.
  smatch   flow analysis across functions , null derefs, error paths that
           forget to unwind, locks taken on one path and not released on
           another.
  cocci    semantic patterns from the kernel's own scripts/coccinelle/, e.g.
           err_cast, kmalloc without a matching free, misuse of an API.
           Optional: needs `spatch`, which needs OCaml (`sudo apt install
           coccinelle`). Skipped with a NOT RUN line rather than silently.

Restricted to the touched files ON PURPOSE. A whole-tree run on 7.1.9 emits
tens of thousands of pre-existing warnings from code nobody here wrote, which
is indistinguishable from noise and gets ignored after the first read. The
series tells us exactly which files are ours to answer for.

A checker that could not run reports NOT RUN. It never reports zero , the
same rule the security phase learned the hard way when a missing
kernel-hardening-checker binary printed "0 FAIL" for months.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import NamedTuple

PATCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = PATCH_DIR.parent
OUT_DIR = REPO_ROOT / "out"

#: Where the tools live when built from source rather than installed.
_LOCAL_TOOLS = [
    Path.home() / "Dev/github/sparse",
    Path.home() / "Dev/github/smatch",
]


class Finding(NamedTuple):
    tool: str
    file: str
    line: int
    text: str


class ToolResult(NamedTuple):
    tool: str
    ran: bool
    reason: str               # why not, when ran is False
    findings: list[Finding]


# ── tool discovery ─────────────────────────────────────────────────────────────

def find_tool(name: str) -> str | None:
    """PATH first, then the source checkouts, so a from-source build works
    without installing anything system-wide."""
    p = shutil.which(name)
    if p:
        return p
    for d in _LOCAL_TOOLS:
        cand = d / name
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


# ── which files does the series touch ──────────────────────────────────────────

_DIFF_FILE = re.compile(r"^\+\+\+ b/(.+)$", re.M)
#: @@ -old,len +new,len @@ , we want the post-image range.
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.M)


def lines_touched(series: Path) -> dict[str, set[int]]:
    """file -> the set of post-image line numbers the series adds or changes.

    This is what makes the output readable. Running sparse over
    kernel/sched/ on a stock 7.1.9 tree emits well over a hundred
    address-space warnings that upstream has carried for years; a gate that
    reports those alongside ours is a gate nobody reads twice. Restricting to
    the lines the series is responsible for turned one real BORE finding from
    invisible into obvious.

    Deliberately generous by a few lines either side: sparse often reports a
    type error at the call site of a declaration the patch changed, which is
    adjacent to the patched line rather than on it.
    """
    out: dict[str, set[int]] = {}
    for raw in series.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        patch = (series.parent / line).resolve()
        if not patch.is_file():
            continue
        cur: str | None = None
        new_ln = 0
        for l in patch.read_text(errors="replace").splitlines():
            m = _DIFF_FILE.match(l)
            if m:
                cur = m.group(1).strip()
                continue
            h = _HUNK.match(l)
            if h:
                new_ln = int(h.group(1))
                continue
            if cur is None:
                continue
            if l.startswith("+") and not l.startswith("+++"):
                out.setdefault(cur, set()).update(
                    range(max(1, new_ln - 3), new_ln + 4))
                new_ln += 1
            elif l.startswith(" "):
                new_ln += 1
            # '-' lines consume no post-image line
    return out


def files_touched(series: Path) -> list[str]:
    """Every source path the series' patches modify, deduplicated.

    Reads the series the same way gitops does: comments and blank lines
    ignored, paths relative to the series file.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in series.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        patch = (series.parent / line).resolve()
        if not patch.is_file():
            continue
        for m in _DIFF_FILE.finditer(patch.read_text(errors="replace")):
            f = m.group(1).strip()
            if f.endswith(".c") and f not in seen:
                seen.add(f)
                out.append(f)
    return sorted(out)


# ── runners ────────────────────────────────────────────────────────────────────

_WARN = re.compile(r"^([^:\s]+\.[ch]):(\d+)(?::\d+)?:\s*(warning|error):\s*(.*)$")


def _parse(tool: str, text: str, only: set[str] | None) -> list[Finding]:
    found: list[Finding] = []
    for line in text.splitlines():
        m = _WARN.match(line.strip())
        if not m:
            continue
        f, ln, kind, msg = m.group(1), int(m.group(2)), m.group(3), m.group(4)
        if only is not None and f not in only:
            continue
        found.append(Finding(tool, f, ln, f"{kind}: {msg}"))
    return found


def _make_check(tree: Path, checker: str, targets: list[str],
                only: set[str] | None, jobs: int) -> tuple[str, str]:
    """`make C=2 CHECK=<tool>` over the given targets.

    C=2 rather than C=1: C=1 only re-checks files that need recompiling, and
    on an already-built tree that is nothing at all , the check would report a
    clean run having examined zero files.
    """
    dirs = sorted({str(Path(t).parent) + "/" for t in targets})
    cmd = ["make", f"-j{jobs}", "C=2", f"CHECK={checker}", *dirs]
    p = subprocess.run(cmd, cwd=tree, capture_output=True, text=True)
    return (p.stdout or "") + (p.stderr or ""), " ".join(cmd)


def run_sparse(tree: Path, targets: list[str], jobs: int) -> ToolResult:
    exe = find_tool("sparse")
    if not exe:
        return ToolResult("sparse", False,
                          "sparse not found on PATH or in ~/Dev/github/sparse "
                          "(build it: git clone git://git.kernel.org/pub/scm/"
                          "devel/sparse/sparse.git && make)", [])
    text, _ = _make_check(tree, exe, targets, set(targets), jobs)
    return ToolResult("sparse", True, "", _parse("sparse", text, set(targets)))


def run_smatch(tree: Path, targets: list[str], jobs: int) -> ToolResult:
    exe = find_tool("smatch")
    if not exe:
        return ToolResult("smatch", False,
                          "smatch not found on PATH or in ~/Dev/github/smatch "
                          "(build it: git clone https://github.com/error27/"
                          "smatch.git && make)", [])
    # --full-path keeps the paths comparable with the series' own paths.
    text, _ = _make_check(tree, f"{exe} --full-path", targets, set(targets), jobs)
    return ToolResult("smatch", True, "", _parse("smatch", text, set(targets)))


def run_cocci(tree: Path, targets: list[str], jobs: int) -> ToolResult:
    exe = find_tool("spatch")
    if not exe:
        return ToolResult("coccinelle", False,
                          "spatch not found (needs OCaml; `sudo apt install "
                          "coccinelle`). The kernel's own semantic checks in "
                          "scripts/coccinelle/ were not run.", [])
    dirs = sorted({str(Path(t).parent) for t in targets})
    text = ""
    for d in dirs:
        p = subprocess.run(["make", f"-j{jobs}", "coccicheck", "MODE=report",
                            f"M={d}"], cwd=tree, capture_output=True, text=True)
        text += (p.stdout or "") + (p.stderr or "")
    return ToolResult("coccinelle", True, "", _parse("coccinelle", text, set(targets)))


# ── reporting ──────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--against", required=True, type=Path,
                    help="a CONFIGURED and preferably already-built kernel tree")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--series", type=Path,
                     help="check the files this series touches")
    src.add_argument("--files", nargs="+",
                     help="check these paths instead (relative to the tree)")
    ap.add_argument("--tools", default="sparse,smatch,cocci",
                    help="comma-separated subset to run")
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 4) // 2))
    ap.add_argument("--all-lines", action="store_true",
                    help="report every finding in the touched FILES, not only "
                         "the lines the series changed. Expect heavy "
                         "pre-existing upstream noise.")
    ap.add_argument("--json", action="store_true")
    ns = ap.parse_args(argv)

    tree: Path = ns.against
    if not (tree / "Makefile").is_file():
        print(f"error: {tree} is not a kernel source tree", file=sys.stderr)
        return 2
    if not (tree / ".config").is_file():
        print(f"error: {tree}/.config missing , configure the tree first "
              f"(python -m hyphaed --phase configure)", file=sys.stderr)
        return 2

    if ns.series:
        targets = files_touched(ns.series)
        touched = lines_touched(ns.series) if not ns.all_lines else None
        origin = str(ns.series)
    else:
        targets = list(ns.files)
        touched = None
        origin = "--files"

    if not targets:
        print("nothing to check: the series touches no .c files")
        return 0

    wanted = {t.strip() for t in ns.tools.split(",") if t.strip()}
    runners = {"sparse": run_sparse, "smatch": run_smatch, "cocci": run_cocci}
    results = [runners[t](tree, targets, ns.jobs) for t in
               ("sparse", "smatch", "cocci") if t in wanted]

    if touched is not None:
        filtered = []
        for r in results:
            if not r.ran:
                filtered.append(r)
                continue
            keep = [f for f in r.findings if f.line in touched.get(f.file, ())]
            filtered.append(r._replace(findings=keep))
        results = filtered

    if ns.json:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "tree": str(tree), "origin": origin, "targets": targets,
            "tools": [{"tool": r.tool, "ran": r.ran, "reason": r.reason,
                       "findings": [f._asdict() for f in r.findings]}
                      for r in results],
        }
        out = OUT_DIR / f"patch-staticcheck-{date.today().isoformat()}.json"
        out.write_text(json.dumps(payload, indent=2))
        print(json.dumps(payload))
        print(f"results written to {out}", file=sys.stderr)
        return 0

    print(f"checking {len(targets)} file(s) from {origin}:")
    for t in targets:
        print(f"  {t}")
    print()
    worst = 0
    for r in results:
        if not r.ran:
            print(f"  {r.tool:12} NOT RUN   {r.reason}")
            continue
        if not r.findings:
            print(f"  {r.tool:12} clean     0 findings in the touched files")
            continue
        worst = 1
        print(f"  {r.tool:12} {len(r.findings)} finding(s)")
        for f in r.findings:
            print(f"      {f.file}:{f.line}  {f.text}")
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
