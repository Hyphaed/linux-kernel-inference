#!/usr/bin/env python3
"""One command that gathers everything needed to keep tuning this workstation.

  sudo python3 diagnostics/optimize_loop.py                 # probe + audit + bench + GPU baseline
  sudo python3 diagnostics/optimize_loop.py --ab dirty_bytes,hwp_dynamic_boost
  sudo python3 diagnostics/optimize_loop.py --skip probe,bench

Writes out/optimize/<timestamp>/ with a SUMMARY.md and the raw files, and points
out/optimize/latest at it. Then ask the assistant to read
out/optimize/latest/SUMMARY.md: nothing needs pasting.

It changes nothing on the system. Benchmarks refuse to run while the machine or
the GPU is busy, and say so in the summary instead of recording noise. Only
`--ab` flips a knob, and perf_tune.py restores it afterwards.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DIAG = REPO / "diagnostics"
OUT = REPO / "out" / "optimize"
STEPS = ["probe", "audit", "pcie", "power", "bench", "gpu", "ab"]

# Facts the GreenBoost PCIe plan needs (docs/greenboost-pcie-plan-2026-10-28.md, Phase 0).
# MaxReadReq, MaxPayload and DIMM speed need root; the rest reads fine without.
PCIE_SNIPPET = r"""
for d in 00:01.0 01:00.0; do echo "== lspci $d"; lspci -vv -s $d 2>&1 | grep -E 'LnkCap:|LnkSta:|LnkCtl:|MaxPayload|MaxReadReq|ASPM|Region 1'; done
echo "== sysfs link"; for d in 0000:00:01.0 0000:01:00.0; do for f in max_link_speed current_link_speed current_link_width; do printf '%s %s=%s\n' $d $f "$(cat /sys/bus/pci/devices/$d/$f 2>&1)"; done; done
echo "== memory"; dmidecode -t memory 2>&1 | grep -E 'Locator:|Size:|Speed:|Manufacturer|Part Number' | grep -v 'No Module'
echo "== huge pages"; grep -E 'AnonHugePages|FileHugePages|HugePages_Total' /proc/meminfo; grep -E '^(thp_fault_alloc|thp_fault_fallback|thp_fault_fallback_charge) ' /proc/vmstat
echo "== nvidia"; nvidia-smi --query-gpu=pcie.link.gen.current,pcie.link.gen.max,pcie.link.width.current --format=csv
"""
# 2026-10-28 turbostat showed C6 and package C-states never used (a deliberate BIOS choice) and
# ~60 W package power next to the 65 W long-term limit. Track both between runs.
POWER_CMD = ("turbostat --quiet --summary --show Busy%,Bzy_MHz,CPU%c1,CPU%c6,Pkg%pc2,PkgWatt,PkgTmp "
             "--interval 5 --num_iterations 2 2>&1 | tail -4")
PCIE_KEEP = re.compile(r"LnkSta:|MaxReadReq|MaxPayload|Speed:|current_link|max_link|thp_fault|AnonHuge|Part Number")


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, DIAG / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


PT = load("perf_tune")
GB = load("gpu_baseline")

STATE_PATHS = {
    "thp": "/sys/kernel/mm/transparent_hugepage/enabled",
    "thp_defrag": "/sys/kernel/mm/transparent_hugepage/defrag",
    "hwp_dynamic_boost": "/sys/devices/system/cpu/intel_pstate/hwp_dynamic_boost",
    "governor": "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor",
    "epp": "/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference",
    "scx_ops": "/sys/kernel/sched_ext/root/ops",
    "nmi_watchdog": "/proc/sys/kernel/nmi_watchdog",
    "swappiness": "/proc/sys/vm/swappiness", "dirty_ratio": "/proc/sys/vm/dirty_ratio",
    "dirty_bytes": "/proc/sys/vm/dirty_bytes", "vfs_cache_pressure": "/proc/sys/vm/vfs_cache_pressure",
    "min_free_kbytes": "/proc/sys/vm/min_free_kbytes", "tcp_cc": "/proc/sys/net/ipv4/tcp_congestion_control",
    "qdisc": "/proc/sys/net/core/default_qdisc",
}


def run_step(name: str, cmd: list[str], outdir: Path, timeout: int) -> tuple[str, str]:
    """-> (status, detail). A refusal to benchmark a busy machine is SKIPPED, not FAILED."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "FAILED", f"timed out after {timeout}s"
    (outdir / f"{name}.txt").write_text(r.stdout + ("\n--- stderr ---\n" + r.stderr if r.stderr else ""))
    text = (r.stderr or r.stdout).strip()
    if r.returncode and any(k in text for k in ("isn't idle", "GPU is in use")):
        return "SKIPPED", " / ".join(l.strip() for l in text.splitlines()[:5] if l.strip())
    if r.returncode:
        return "FAILED", text.splitlines()[-1] if text else f"exit {r.returncode}"
    return "OK", ""


def snapshot_state() -> dict:
    st = {k: PT.read_val(p) for k, p in STATE_PATHS.items()}
    st["kernel"] = os.uname().release
    st["cmdline"] = Path("/proc/cmdline").read_text().strip()
    nvme = PT._nvme()
    if nvme:
        st["nvme_scheduler"] = PT.read_val(f"/sys/block/{nvme}/queue/scheduler")
        st["nvme_read_ahead_kb"] = PT.read_val(f"/sys/block/{nvme}/queue/read_ahead_kb")
    return st


def latest_json(pattern: str, directory: Path, before: float) -> dict | None:
    files = sorted((f for f in directory.glob(pattern) if f.stat().st_mtime < before), key=lambda f: f.stat().st_mtime)
    return json.loads(files[-1].read_text()) if files else None


def newest(pattern: str, directory: Path, after: float) -> Path | None:
    files = sorted((f for f in directory.glob(pattern) if f.stat().st_mtime >= after), key=lambda f: f.stat().st_mtime)
    return files[-1] if files else None


def summary(ts: str, statuses: dict, started: float, prev_dir: Path | None, state: dict) -> str:
    L = [f"# Optimization loop {ts}", "", f"Kernel {state['kernel']}. Raw files sit next to this one.", "",
         "| step | status | note |", "|---|---|---|"]
    L += [f"| {s} | {st} | {d} |" for s, (st, d) in statuses.items()]

    L += ["", "## State drift since the last run"]
    drift = []
    if prev_dir and (prev_dir / "state.json").exists():
        old = json.loads((prev_dir / "state.json").read_text())
        drift = [f"- `{k}`: {old.get(k)!r} -> {state.get(k)!r}" for k in state if old.get(k) != state.get(k) and k != "cmdline"]
        if old.get("cmdline") != state.get("cmdline"):
            drift.append("- kernel command line changed")
    L += drift or ["- none (or this is the first run)"]

    pcie_txt = OUT / ts / "pcie.txt"
    if pcie_txt.exists():
        L += ["", "## PCIe link, DIMMs and huge pages"]
        L += [f"- {l.strip()}" for l in pcie_txt.read_text().splitlines() if PCIE_KEEP.search(l)]
        L += ["- (MaxReadReq, MaxPayload and DIMM speed only appear when run with sudo)"]

    power_txt = OUT / ts / "power.txt"
    if power_txt.exists() and power_txt.read_text().strip():
        L += ["", "## CPU power and idle states (turbostat, summary rows)"]
        L += [f"- {l}" for l in power_txt.read_text().splitlines() if l.strip()]

    bench = newest("bench-loop-*.json", PT.OUT_DIR, started)
    if bench:
        new = json.loads(bench.read_text())
        L += ["", "## CPU, memory and disk (medians)"]
        prev = latest_json("bench-loop-*.json", PT.OUT_DIR, started)
        rows = {r["metric"]: r for r in PT.compare(prev["reps"], new["reps"], prev["reps"])} if prev else {}
        for k, vals in new["reps"].items():
            if vals:
                tail = f"  ({rows[k]['gain_pct']:+.1f}% vs last run, {rows[k]['verdict']})" if k in rows else ""
                L.append(f"- {k}: {statistics.median(vals):.4g} {PT.METRICS[k][0]}{tail}")
        L.append("- context: " + json.dumps(new["ctx"]))

    gpu = newest("baseline-loop-*.json", GB.OUT, started)
    if gpu:
        res = json.loads(gpu.read_text())
        L += ["", "## GPU: tensor rate and headroom"] + [f"- {l}" for l in GB.headline(res)]
        L += ["", "Findings:"] + [f"- {l}" for l in GB.findings(res)]
        L += ["", "Against the previous baseline:"] + [f"- {l}" for l in GB.compare_prev(res, "baseline-*.json", gpu)]
        L += ["", "Tensor unlock: no GB205 payload exists as of 2026-10-28; this baseline is the 'before' "
              "(docs/research/rtx5070-tensor-unlock-2026-10-28.md)."]

    ab = newest("ab-*.md", PT.OUT_DIR, started)
    if ab:
        L += ["", "## A/B results", "", ab.read_text()]
    measured = {m for f in PT.OUT_DIR.glob("ab-*.md") for m in PT.MEASURE if f"## {m} (" in f.read_text()}
    todo = [m for m in PT.MEASURE if m not in measured]
    L += ["", "## Still to measure", "- " + (", ".join(todo) if todo else "every A/B knob has a result"),
          "", "## Waiting on a decision", "- docs/kernel-requirements.md section 6",
          "", "Tell the assistant: read out/optimize/latest/SUMMARY.md and decide the next tuning step."]
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--skip", default="", help=f"comma list of {STEPS}")
    ap.add_argument("--ab", default="", help="comma list of perf_tune A/B knobs to measure (about 20 min each)")
    ap.add_argument("--allow-nonroot", action="store_true", help="testing only: cold-cache numbers are then invalid")
    a = ap.parse_args()
    if os.geteuid() != 0 and not a.allow_nonroot:
        sys.exit("Run with sudo: sudo python3 diagnostics/optimize_loop.py")
    skip = set(filter(None, a.skip.split(",")))
    user = os.environ.get("SUDO_USER")
    py = f"/home/{user}/.miniforge3/bin/python" if user and Path(f"/home/{user}/.miniforge3/bin/python").exists() else "python3"
    as_user = ["sudo", "-u", user, "-H"] if user and os.geteuid() == 0 else []

    ts = time.strftime("%Y%m%d-%H%M%S")
    outdir = OUT / ts
    outdir.mkdir(parents=True, exist_ok=True)
    prev_dirs = sorted(d for d in OUT.iterdir() if d.is_dir() and d.name != ts and not d.is_symlink()) if OUT.exists() else []
    started = time.time()

    plan = {
        "probe": (["bash", str(DIAG / "workstation-probe.sh")], 600),
        "audit": ([sys.executable, str(DIAG / "perf_tune.py"), "audit"], 120),
        "pcie": (["bash", "-c", PCIE_SNIPPET], 60),
        "power": (["bash", "-c", POWER_CMD], 60),
        "bench": ([sys.executable, str(DIAG / "perf_tune.py"), "bench", "--label", "loop"], 1800),
        "gpu": (as_user + [py, str(DIAG / "gpu_baseline.py"), "--label", "loop"], 900),
    }
    if a.ab:
        plan["ab"] = ([sys.executable, str(DIAG / "perf_tune.py"), "ab", *a.ab.split(",")], 4 * 3600)

    statuses: dict[str, tuple[str, str]] = {}
    for name, (cmd, timeout) in plan.items():
        if name in skip:
            statuses[name] = ("SKIPPED", "--skip")
            continue
        print(f"[{name}] running ...", flush=True)
        statuses[name] = run_step(name, cmd, outdir, timeout)
        print(f"[{name}] {statuses[name][0]} {statuses[name][1]}", flush=True)

    state = snapshot_state()
    (outdir / "state.json").write_text(json.dumps(state, indent=2))
    (outdir / "SUMMARY.md").write_text(summary(ts, statuses, started, prev_dirs[-1] if prev_dirs else None, state))

    latest = OUT / "latest"
    if latest.is_symlink() or latest.exists():
        latest.unlink()
    latest.symlink_to(outdir.name)
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if uid and gid:
        for root in (OUT, PT.OUT_DIR, GB.OUT):
            for p in [root, *root.rglob("*")]:
                os.chown(p, int(uid), int(gid), follow_symlinks=False)
    print(f"\nDone: {outdir / 'SUMMARY.md'}\nNext: tell the assistant to read out/optimize/latest/SUMMARY.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
