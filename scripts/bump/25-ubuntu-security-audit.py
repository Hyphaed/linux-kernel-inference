#!/usr/bin/env python3
"""Read-only audit: which Ubuntu security/SAUCE commits are NOT in kernel.org vVER?

Ubuntu ships no 7.2 kernel (26.10 uses 7.3), so there is no Ubuntu 7.2 branch to
rebase on. This lists candidates from Ubuntu's newest tree that a human can
triage into patches/kernel-org-<MM>/series.experimental. It never applies anything.

Needs the Ubuntu tree pre-fetched (no per-run fetch, same rule as the other
bump stages):
  git clone --filter=blob:none --no-checkout \
      https://git.launchpad.net/~ubuntu-kernel/ubuntu/+source/linux/+git/stonking \
      github/ubuntu-stonking
Usage: VER=7.2.14 python3 scripts/bump/25-ubuntu-security-audit.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VER = os.environ.get("VER") or sys.exit("set VER, e.g. VER=7.2.14")
UB = ROOT / "github" / "ubuntu-stonking"
KO = ROOT / "github" / "kernelorg" / "linux"
OUT = ROOT / "out" / "sweep" / VER / "ubuntu-security.md"
SEC_PATHS = ("security/", "crypto/", "net/", "fs/", "kernel/", "mm/", "drivers/net/")


def git(repo: Path, *a: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, check=False).stdout


def patch_ids(repo: Path, rev: str, since: str) -> set[str]:
    p = subprocess.run(f"git -C {repo} log -p --no-merges {since}..{rev} | git patch-id --stable",
                       shell=True, capture_output=True, text=True)
    return {ln.split()[0] for ln in p.stdout.splitlines() if ln.strip()}


def main() -> int:
    if not (UB / ".git").exists() and not (UB / "objects").exists():
        print(__doc__)
        print(f"MISSING: {UB}. Clone it with the command above, then re-run.", file=sys.stderr)
        return 1
    base = "v7.2"
    ko_ids = patch_ids(KO, f"v{VER}", base)
    rows = []
    log = git(UB, "log", "--no-merges", "--format=%H%x09%s", f"{base}..HEAD").splitlines()
    for ln in log:
        sha, _, subj = ln.partition("\t")
        body = git(UB, "show", "-s", "--format=%B", sha)
        files = git(UB, "show", "--name-only", "--format=", sha).split()
        cves = sorted(set(re.findall(r"CVE-\d{4}-\d+", body)))
        sauce = subj.startswith("UBUNTU: SAUCE")
        touches = any(f.startswith(SEC_PATHS) for f in files)
        if not (cves or (sauce and touches)):
            continue
        pid = subprocess.run(f"git -C {UB} show {sha} | git patch-id --stable", shell=True,
                             capture_output=True, text=True).stdout.split()[:1]
        if pid and pid[0] in ko_ids:
            continue  # already in kernel.org vVER
        rows.append((sha[:12], subj, ",".join(cves) or "-", "SAUCE" if sauce else "upstream-backport"))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w") as f:
        f.write(f"# Ubuntu security/SAUCE candidates not in v{VER}\n\n{len(rows)} candidates. "
                "Triage by hand; apply only real security fixes that apply cleanly.\n\n"
                "| commit | kind | CVE | subject |\n|---|---|---|---|\n")
        for sha, subj, cve, kind in rows:
            f.write(f"| {sha} | {kind} | {cve} | {subj.replace('|', '/')} |\n")
    print(f"wrote {OUT} ({len(rows)} candidates)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
