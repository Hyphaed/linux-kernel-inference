#!/usr/bin/env python3
"""Verify, fetch and rank every patch source in patches/sources.tsv for THIS machine.

  source-inventory.py            verify all URLs (git ls-remote), probe hardware,
                                 write out/sweep/<VER>/inventory.md  (never fetches)
  source-inventory.py --fetch    also blobless-shallow-clone the hardware-relevant
                                 `clone` sources into github/sources/<name>
  source-inventory.py --candidates
                                 list recent commits of fetched carriers that touch
                                 patch-worthy content, ranked, deduped against our series

Sources needing absent hardware are verified but skipped (reported, not hidden).
"""
import argparse, concurrent.futures as cf, os, platform, re, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ap = argparse.ArgumentParser()
ap.add_argument("--ver", default=os.environ.get("VER", "7.2.9"))
ap.add_argument("--fetch", action="store_true")
ap.add_argument("--candidates", action="store_true")
a = ap.parse_args()
OUT = ROOT / "out" / "sweep" / a.ver; OUT.mkdir(parents=True, exist_ok=True)
CLONES = ROOT / "github" / "sources"
mm = a.ver.rsplit(".", 1)[0]

def sh(*c, t=90):
    try:
        r = subprocess.run(c, capture_output=True, text=True, timeout=t); return r.returncode, r.stdout
    except subprocess.TimeoutExpired:
        return 124, ""

# ---- hardware probe -> tokens
lspci = sh("lspci", "-nn")[1]; lscpu = sh("lscpu")[1]
hw = {"any"}
if "GenuineIntel" in lscpu: hw.add("intel-cpu")
if "AuthenticAMD" in lscpu: hw.add("amd-cpu")
if re.search(r"(VGA|3D).*\[10de:", lspci): hw.add("nvidia-gpu")
if re.search(r"(VGA|Display).*\[1002:", lspci): hw.add("amd-gpu")
if re.search(r"(VGA|Display).*\[8086:", lspci): hw.add("intel-igpu")
if "thunderbolt" in lspci.lower(): hw.add("thunderbolt")
if "[15b3:" in lspci: hw.add("mellanox")
if platform.machine().startswith(("aarch64", "arm")): hw.add("arm-soc")

rows = []
for ln in (ROOT / "patches/sources.tsv").read_text().splitlines():
    if ln.strip() and not ln.startswith("#"):
        c = ln.split("\t"); c += [""] * (8 - len(c)); rows.append(dict(zip(
            "name url ref kernel type hw mode note".split(), c)))

def verify(r):
    ref = "HEAD" if r["ref"] == "HEAD" else f"refs/heads/{r['ref']}"
    rc, o = sh("git", "ls-remote", r["url"], ref)
    r["tip"] = o.split()[0][:12] if rc == 0 and o.strip() else None
    r["ok"] = rc == 0 and bool(o.strip())
    return r
with cf.ThreadPoolExecutor(8) as ex: rows = list(ex.map(verify, rows))

def fetch(r):
    d = CLONES / r["name"]
    ref = [] if r["ref"] == "HEAD" else ["-b", r["ref"]]
    if (d / ".git").exists():
        rc, _ = sh("git", "-C", str(d), "fetch", "--depth", "60", "origin", r["ref"], t=600)
        if rc == 0: sh("git", "-C", str(d), "reset", "-q", "--soft", "FETCH_HEAD")
        return rc == 0
    rc, _ = sh("git", "clone", "--no-checkout", "--filter=blob:none", "--depth", "60", *ref, r["url"], str(d), t=900)
    return rc == 0

wanted = [r for r in rows if r["ok"] and r["mode"] == "clone" and r["hw"] in hw]
if a.fetch:
    CLONES.mkdir(parents=True, exist_ok=True)
    with cf.ThreadPoolExecutor(4) as ex:
        for r, ok in zip(wanted, ex.map(fetch, wanted)): r["fetched"] = ok

md = [f"# Patch-source inventory for {a.ver}\n", f"Hardware tokens present: `{', '.join(sorted(hw))}`\n",
      "| name | URL | ref | kernel | type | needs-hw | status |", "|---|---|---|---|---|---|---|"]
for r in rows:
    st = ("UNREACHABLE" if not r["ok"] else f"ok {r['tip']}") + \
         ("" if r["hw"] in hw else " · skipped (hw absent)") + (" · fetched" if r.get("fetched") else "")
    md.append(f"| {r['name']} | {r['url']} | {r['ref']} | {r['kernel']} | {r['type']} | {r['hw']} | {st} |")
bad = [r["name"] for r in rows if not r["ok"]]
md.append(f"\n{len(rows)} sources, {len(rows)-len(bad)} reachable" + (f", UNREACHABLE: {', '.join(bad)}" if bad else "."))

if a.candidates:
    # subjects we already carry, normalised, so equivalent patches from other sources are flagged
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", re.sub(r"^\[[^\]]*\]\s*", "", s.lower())).strip()
    ours = set()
    for p in (ROOT / "patches").rglob("*.patch"):
        m = re.search(r"^Subject:\s*(.+(?:\n .+)*)", p.read_text(errors="ignore"), re.M)
        if m: ours.add(norm(" ".join(m.group(1).split())))
    kw = re.compile(r"sched|bore|eevdf|latency|throughput|nvme|pci|dma|mm[:/]|zram|zswap|thp|io_uring|tcp|bbr|pstate|hwp|epp|itmt|vfio|iommu|kvm|nvidia|numa", re.I)
    hot = {"intel-cpu": 3, "nvidia-gpu": 3, "any": 1}
    md.append("\n## Candidate commits from fetched carriers (ranked: hardware match, then keyword hits)\n")
    seen_subj = {}
    for r in sorted(wanted, key=lambda r: -hot.get(r["hw"], 0)):
        d = CLONES / r["name"]
        if not (d / ".git").exists(): continue
        for ln in sh("git", "-C", str(d), "log", "--no-merges", "-n60", "--format=%h\t%s")[1].splitlines():
            h, _, s = ln.partition("\t"); n = norm(s)
            if not kw.search(s): continue
            tag = " [already carried]" if n in ours else (f" [dup of {seen_subj[n]}]" if n in seen_subj else "")
            seen_subj.setdefault(n, r["name"])
            md.append(f"- ({hot.get(r['hw'],0)}) `{r['name']}` {h} {s}{tag}  <{r['url']}@{h}>")

(OUT / "inventory.md").write_text("\n".join(md) + "\n")
print(f"{len(rows)} sources, {len(rows)-len(bad)} reachable, {len(wanted)} hardware-relevant clones"
      + (f", UNREACHABLE: {bad}" if bad else "") + f"\nwrote {OUT/'inventory.md'}")
sys.exit(1 if bad else 0)
