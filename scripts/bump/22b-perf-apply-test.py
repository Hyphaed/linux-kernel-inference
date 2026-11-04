#!/usr/bin/env python3
"""Stage 22b: which candidates from 22-perf-candidates.py cherry-pick cleanly onto <target> + the default series?

    python3 scripts/bump/22b-perf-apply-test.py --target v7.2.15 --series patches/kernel-org-7.2/series \
        --sweep out/sweep/7.2.15 [--only perf|all] [--variant sched73]

Builds a throwaway shared clone of the kernel.org mirror, applies the default series with `git am --3way` (the tree an
experimental patch actually lands on), then tries each candidate with `git cherry-pick -x --no-commit` and resets.
Adds an `apply` column (clean | conflict | empty) to <sweep>/perf-candidates.tsv. Clean is necessary, not sufficient:
a commit can apply and still need a helper that only 7.3 has, so the selected set is compile-checked afterwards.
"""
import argparse, csv, os, re, shutil, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
KO = ROOT / "github" / "kernelorg" / "linux"


def sh(w, *a, check=False):
    r = subprocess.run(["git", "-C", str(w), "-c", "user.name=t", "-c", "user.email=t@l", *a], capture_output=True, text=True)
    if check and r.returncode:
        sys.exit(f"git {' '.join(a)}: {r.stderr[:300]}")
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True)
    ap.add_argument("--series", required=True)
    ap.add_argument("--sweep", required=True)
    ap.add_argument("--only", default="perf", choices=["perf", "all"])
    ap.add_argument("--keep", action="store_true", help="keep the work tree and print its path")
    a = ap.parse_args()
    tsv = Path(a.sweep) / "perf-candidates.tsv"
    rows = list(csv.DictReader(open(tsv, newline=""), delimiter="\t", quoting=csv.QUOTE_NONE))
    todo = [r for r in rows if a.only == "all" or r["perfword"] == "1" or r["numbers"] == "1"]
    work = Path(tempfile.mkdtemp(prefix="perfapply-", dir=os.environ.get("TMPDIR", "/tmp")))
    print(f"work tree {work}: {len(todo)} of {len(rows)} candidates", flush=True)
    subprocess.run(["git", "clone", "-q", "--shared", "--no-checkout", str(KO), str(work)], check=True)
    sh(work, "checkout", "-q", "--detach", a.target, check=True)
    series = Path(a.series)
    for ln in series.read_text().splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        p = (series.parent / ln).resolve()
        r = sh(work, "am", "--3way", "-q", str(p))
        if r.returncode:
            sh(work, "am", "--abort")
            sys.exit(f"series patch does not apply on {a.target}: {ln}")
    base = sh(work, "rev-parse", "HEAD", check=True).stdout.strip()
    (Path(a.sweep) / "perf-apply.partial.tsv").write_text("")
    res = {}
    for i, r in enumerate(todo, 1):
        full = subprocess.run(["git", "-C", str(KO), "rev-parse", r["sha"]], capture_output=True, text=True).stdout.strip()
        c = sh(work, "cherry-pick", "-x", "--no-commit", full)
        if c.returncode == 0:
            st = "clean" if sh(work, "diff", "--cached", "--quiet").returncode else "empty"
        else:
            st = "conflict"
            sh(work, "cherry-pick", "--abort")
        sh(work, "reset", "-q", "--hard", base)
        res[r["sha"]] = st
        with open(Path(a.sweep) / "perf-apply.partial.tsv", "a") as pf:
            pf.write(f"{r['sha']}\t{st}\n")
        if i % 100 == 0:
            print(f"  {i}/{len(todo)}", flush=True)
    fields = list(rows[0].keys())
    if "apply" not in fields:
        fields.append("apply")
    with open(tsv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter="\t", quoting=csv.QUOTE_NONE, escapechar="\\", extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for r in rows:
            r["apply"] = res.get(r["sha"], "")
            w.writerow(r)
    from collections import Counter
    print(dict(Counter(res.values())))
    if a.keep:
        print("kept", work)
    else:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
