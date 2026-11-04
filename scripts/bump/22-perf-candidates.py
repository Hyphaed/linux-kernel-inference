#!/usr/bin/env python3
"""Stage 22: mainline commits in the donor release that are NOT yet in the target stable tag, filtered to
subsystems this workstation runs and ranked by how plainly they claim a performance effect.

    python3 scripts/bump/22-perf-candidates.py --target v7.2.15 --donor v7.3.1 [--out out/sweep/7.2.15]

Writes <out>/perf-candidates.tsv (one row per candidate) and prints per-subsystem counts. It does NOT decide:
`22b-perf-apply-test.py` proves what applies and compiles, and a human writes the verdicts (playbook step "Verdicts").
Donor is compared with `git cherry`, so a commit stable already backported (same patch-id) is dropped.
"""
import argparse, collections, re, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
KO = ROOT / "github" / "kernelorg" / "linux"

# Subsystems this box builds and uses (i9-14900KF, RTX 5070 + out-of-tree NVIDIA, 990 EVO Plus NVMe on LVM/ext4, r8169,
# Wayland gaming + inference + VMware). Anything else is dropped unless it matches the generic list.
KEEP = [
    r"^kernel/(sched|locking|futex|time|irq|rcu|cgroup|workqueue|fork|exit|signal|smp|cpu|bpf|events|dma|power|printk)",
    r"^kernel/[^/]+\.c$", r"^mm/", r"^block/", r"^io_uring/", r"^lib/", r"^crypto/", r"^fs/(ext4|jbd2|namei|dcache|inode|file|read_write|splice|buffer|mpage|iomap|direct-io|eventpoll|select|pipe|exec|open|namespace|locks|fs-writeback|proc|sysfs|kernfs|notify)",
    r"^fs/[^/]+\.c$", r"^net/(core|ipv4|ipv6|sched|netfilter|unix|packet|socket)", r"^drivers/net/ethernet/realtek",
    r"^drivers/(nvme|block|md|cpufreq|cpuidle|pci|iommu/(intel|dma|iommu)|acpi|base|thermal/intel|powercap|hid|input|usb/(core|host)|dma-buf|gpu/drm/(drm_|ttm|scheduler|nouveau)|char/random|clocksource|irqchip|perf|platform/x86/intel|video)",
    r"^arch/x86/(kernel|mm|lib|events|include|entry|crypto|coco|power|platform|realmode|hyperv|kvm|xen)",
    r"^include/(linux|asm-generic|trace|uapi/linux|net|crypto)/", r"^security/(apparmor|security|commoncap)",
    r"^init/", r"^virt/kvm",
]
DROP = [r"^Documentation/", r"^tools/", r"^samples/", r"^scripts/", r"^arch/(?!x86)", r"^drivers/(staging|media|net/wireless|net/ethernet/(?!realtek)|gpu/drm/(amd|i915|xe|msm|panel|bridge|imx|mediatek|rockchip|tegra|vc4))"]
PERF = re.compile(r"\b(faster|speed[- ]?up|speedup|perf(ormance)?|improv\w*|optimi[sz]\w*|reduc\w+ (overhead|latency|contention|cost|time|lock)|"
                  r"avoid\w* (unnecessary|redundant|extra|needless|expensive|walking|taking|holding)|scal(e|ab)\w*|batch\w*|lockless|lock[- ]free|"
                  r"latency|throughput|contention|hot ?path|fast ?path|cache[- ]?(line|miss|friendly)|cheaper|regress\w*|bottleneck|overhead)\b", re.I)
NUMS = re.compile(r"(\d+(\.\d+)?\s?(%|x\b|ns\b|us\b|µs\b|ms\b|MB/s|GB/s|MiB/s|GiB/s|IOPS|fps|cycles|kIOPS)|\bfrom \d+\S* to \d+\S*)", re.I)


def git(*a):
    r = subprocess.run(["git", "-C", str(KO), *a], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"git {' '.join(a)}: {r.stderr.strip()[:200]}")
    return r.stdout


def subsystem(paths):
    c = collections.Counter(re.match(r"^(kernel/[^/]+|mm|block|io_uring|lib|crypto|fs/[^/]+|net/[^/]+|drivers/[^/]+(/[^/]+)?|arch/x86/[^/]+|include|security/[^/]+|init|virt)", p).group(1)
                            for p in paths if re.match(r"^(kernel|mm|block|io_uring|lib|crypto|fs|net|drivers|arch/x86|include|security|init|virt)", p))
    return c.most_common(1)[0][0] if c else "other"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True)
    ap.add_argument("--donor", required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--base", default=None, help="release both trees share (default: major.minor of --target, e.g. v7.2)")
    a = ap.parse_args()
    base = a.base or re.match(r"(v\d+\.\d+)", a.target).group(1)
    # Stable history is often grafted at the base release, so v7.2.15 and v7.3 do not look related to git (a plain
    # `git cherry` then walks 1.5M commits). Range donor commits from the shared release, and find what stable already
    # took from the "commit <sha> upstream" / "[ Upstream commit <sha> ]" lines every backport carries.
    taken = set()
    for blk in git("log", "--no-merges", "--format=%H%x1f%B%x1e", f"{base}..{a.target}").split("\x1e"):
        taken.update(m[:12] for m in re.findall(r"(?:commit|Upstream commit)\s+([0-9a-f]{40})", blk))
    taken_subj = {x.strip() for x in git("log", "--no-merges", "--format=%s", f"{base}..{a.target}").splitlines()}
    raw = git("log", "--no-merges", "--numstat", "--format=@@%H\x1f%an\x1f%ad\x1f%s\x1f%b\x1e", "--date=short", f"{base}..{a.donor}")
    keep, drop = [re.compile(x) for x in KEEP], [re.compile(x) for x in DROP]
    rows, total, in_target = [], 0, 0
    for blk in raw.split("@@")[1:]:
        head, _, stat = blk.partition("\x1e")
        sha, author, date, subj, body = (head.split("\x1f") + [""] * 5)[:5]
        total += 1
        if sha[:12] in taken or subj.strip() in taken_subj:
            in_target += 1
            continue
        files, ins, dele = [], 0, 0
        for ln in stat.strip().splitlines():
            m = re.match(r"^(\d+|-)\t(\d+|-)\t(.+)$", ln)
            if m:
                files.append(m.group(3)); ins += int(m.group(1)) if m.group(1) != "-" else 0; dele += int(m.group(2)) if m.group(2) != "-" else 0
        rel = [f for f in files if any(k.search(f) for k in keep) and not any(d.search(f) for d in drop)]
        if not rel:
            continue
        text = subj + "\n" + body
        perf = bool(PERF.search(text))
        nums = NUMS.search(body)
        tags = len(re.findall(r"^(Reviewed-by|Tested-by|Acked-by):", body, re.M))
        fixes = bool(re.search(r"^Fixes:", body, re.M))
        stable = bool(re.search(r"^Cc: .*stable@", body, re.M))
        rows.append((sha[:12], subsystem(rel), ins + dele, len(files), tags, int(perf), int(bool(nums)), int(fixes), int(stable), date, author, subj.replace("\t", " "),
                     (nums.group(0) if nums else "")))
    out = Path(a.out) if a.out else None
    if out:
        out.mkdir(parents=True, exist_ok=True)
        with open(out / "perf-candidates.tsv", "w") as f:
            f.write("sha\tsubsystem\tlines\tfiles\ttags\tperfword\tnumbers\tfixes\tcc_stable\tdate\tauthor\tsubject\tclaim\n")
            for r in rows:
                f.write("\t".join(map(str, r)) + "\n")
    perf_rows = [r for r in rows if r[5] or r[6]]
    print(f"{a.donor} vs {a.target} (base {base}): {total} commits in range, {in_target} already in {a.target} by patch-id, "
          f"{len(rows)} touch subsystems this box runs, {len(perf_rows)} claim a performance effect, "
          f"{sum(1 for r in rows if r[6])} quote a number")
    c = collections.Counter(r[1] for r in perf_rows)
    for k, v in c.most_common(40):
        print(f"  {v:4d}  {k}")


if __name__ == "__main__":
    main()
