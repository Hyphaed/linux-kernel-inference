#!/usr/bin/env python3
"""Triage new patch candidates for a kernel bump. Writes out/sweep/<VER>/candidates.md.

Read-only on every repo. `--commit-state` records current HEADs as "seen" so the
next sweep only lists what's new since this one. Items whose sha already appears
in a docs/research/*patch-sweep*.md are tagged [seen] (already ruled on).
"""
import argparse, json, os, re, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ap = argparse.ArgumentParser()
ap.add_argument("--ver", default=os.environ.get("VER"), required=not os.environ.get("VER"))
ap.add_argument("--commit-state", action="store_true")
a = ap.parse_args()
ver = a.ver; mm = ver.rsplit(".", 1)[0]
out = ROOT / "out" / "sweep" / ver; out.mkdir(parents=True, exist_ok=True)
state_f = ROOT / "out" / "sweep" / "state.json"
state = json.loads(state_f.read_text()) if state_f.exists() else {}

def git(repo, *args, check=False):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 or not check else ""

seen = set()
for d in (ROOT / "docs" / "research").glob("*patch-sweep*.md"):
    seen |= set(re.findall(r"\b[0-9a-f]{12}\b", d.read_text()))
def tag(line): return " [seen]" if line.split()[0][:12] in seen else ""
def log_since(name, repo, rng_end, paths=(), limit=60, extra=()):
    head = git(repo, "rev-parse", rng_end)
    if not head: return [f"_{name}: ref {rng_end} not available_"]
    old = state.get(name)
    if old and git(repo, "cat-file", "-t", old) == "commit":
        sel = [f"{old}..{head}"]
    else:
        sel = [f"-n{limit}", head]
    lines = git(repo, "log", "--no-merges", "--format=%h %an: %s", *extra, *sel, "--", *paths).splitlines()
    state_new[name] = head
    hdr = f"{len(lines)} commits" + (f" since {old[:12]}" if old else f" (first sweep, last {limit})")
    return [f"**{name}** {hdr}"] + [f"- {l}{tag(l)}" for l in lines[:limit]]

state_new = {}
md = [f"# Patch sweep candidates for {ver}\n"]
KO = ROOT / "github/kernelorg/linux"

# 1. series pins vs upstream
lock = (ROOT / f"patches/VENDOR-kernel-org-{mm}.lock").read_text()
pins = re.findall(r"git\+file://(\S+)@([0-9a-f]{40})", lock)
md.append("## Series pins\n")
for repo, sha in sorted(set(pins)):
    tip = git(repo, "rev-parse", f"origin/{mm}") or git(repo, "rev-parse", "HEAD")
    behind = git(repo, "rev-list", "--count", f"{sha}..{tip}") if tip else "?"
    md.append(f"- `{Path(repo).name}` pin {sha[:12]} -> {mm} tip {tip[:12]}: {behind} newer commits")
bore = ROOT / "kernel-patches" / mm / "sched"
md.append(f"- CachyOS BORE: `git -C kernel-patches log -3 -- {mm}/sched/` -> " +
          "; ".join(git(ROOT / "kernel-patches", "log", "-3", "--format=%h %cs %s", "--", f"{mm}/sched/").splitlines()))

# 2. new upstream work in the sources
md += ["\n## New in each source\n"]
for n, repo, ref, paths in [
    ("xanmod", ROOT / "linux", f"origin/{mm}", ()),
    ("cachyos-kernel-patches", ROOT / "kernel-patches", "origin/master", (mm,)),
    ("cachyos-pkgbuild", ROOT / "linux-cachyos", "origin/master", ()),
    ("tkg", ROOT / "linux-tkg", "origin/master", ()),
    ("intel-overlay", ROOT / "github/intel-overlay", f"origin/mainline-tracking/v{mm}", ()),
    ("steamos", ROOT / "github/steamos", f"origin/{mm}/integration", ()),
]:
    md += log_since(n, repo, ref, paths) + [""]

# 3. stable queue
sq = ROOT / "github/stable-queue" / f"queue-{mm}"
md.append(f"## stable-queue queue-{mm} (already coming in the next point release)\n")
md += [f"- {p.name}" for p in sorted(sq.glob("*.patch"))] if sq.is_dir() else ["_not cloned_"]

# 4. mainline window, only subsystems this box exercises
rc = sorted(git(KO, "tag", "-l", "v7.3-rc*").split(), key=lambda t: int(re.sub(r"\D", "", t.split("rc")[1] or "0")))
md.append(f"\n## v{mm}..{rc[-1] if rc else '?'} (mainline), hardware-relevant paths, fixes only\n")
if rc:
    paths = ["arch/x86/kernel", "arch/x86/mm", "arch/x86/pci", "drivers/pci", "drivers/nvme", "kernel/sched",
             "mm", "drivers/dma-buf", "drivers/net/ethernet/realtek", "drivers/thermal/intel", "drivers/cpufreq",
             "drivers/iommu/intel", "block", "io_uring", "security/apparmor"]
    lines = git(KO, "log", "--no-merges", "-i", "--grep=fix", "--format=%h %s", f"v{mm}..{rc[-1]}", "--", *paths).splitlines()
    md.append(f"{len(lines)} candidates (first 150):")
    md += [f"- {l}{tag(l)}" for l in lines[:150]]

# 5. Kconfig churn inside the series (point release) -> fragment audit input
prev = os.environ.get("PREV")
if prev and git(KO, "rev-parse", "-q", "--verify", f"v{prev}^{{commit}}"):
    kc = git(KO, "diff", "--stat", f"v{prev}", f"v{ver}", "--", "*Kconfig*").splitlines()
    md.append(f"\n## Kconfig changes v{prev}..v{ver}\n" + ("\n".join(f"    {l}" for l in kc) if kc else "none"))

(out / "candidates.md").write_text("\n".join(md) + "\n")
print(f"wrote {out/'candidates.md'} ({sum(1 for l in md if l.startswith('- '))} bullets)")
if a.commit_state:
    state.update(state_new); state_f.write_text(json.dumps(state, indent=1)); print("state recorded")
