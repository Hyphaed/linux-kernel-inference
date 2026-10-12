"""A/B report: two workload captures (A = old kernel, B = new) side by side.

It states what it can't conclude as plainly as what it can: items either side
couldn't run are listed NOT RUN, and every delta carries the thing that would make
it noise. The decode ceiling is restated unchanged unless a capture measured otherwise.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CEILING = ("The kernel owns about 5.7% of decode time: 14.9 ms of 263 ms/token, the other 248 ms is PCIe "
           "Gen4 transfer of non-resident weights (docs/plan_patches.md section 2). Nothing here changes "
           "that unless the tok/s row below says so.")

NOISE = {
    "pass_latency": "slow passes cluster in time and follow host load (loadavg 9+) and market-to-postgres runs; compare the same hour",
    "funding_file_cost": "CPU-bound JSON parse: sensitive to whatever else is running, so read the spread, not one number",
    "pressure": "PSI avg10 over one minute is a snapshot; a different minute can differ by more than a kernel would",
    "scope_kills": "a count over 24 h, not a rate; zero kills in both says the machine didn't testify",
    "postgres": "wait events are a sample of whatever was active, not a distribution",
}


def _g(d: dict, *path: str) -> Any:
    for p in path:
        if not isinstance(d, dict) or p not in d:
            return None
        d = d[p]
    return d


def _fmt(v: Any) -> str:
    return "NOT RUN" if v is None else (f"{v:.3f}" if isinstance(v, float) else str(v))


ROWS: list[tuple[str, str, tuple[str, ...]]] = [
    ("pass_latency", "total pass median (s)", ("pass_latency", "total_s", "median")),
    ("pass_latency", "total pass p95 (s)", ("pass_latency", "total_s", "p95")),
    ("pass_latency", "ctx stage p95 (s)", ("pass_latency", "ctx_s", "p95")),
    ("pass_latency", "passes over 20 s", ("pass_latency", "over_20s")),
    ("funding_file_cost", "funding parse, median of repeats (s)", ("funding_file_cost", "parse_all_s", "median")),
    ("funding_file_cost", "funding parse, max of repeats (s)", ("funding_file_cost", "parse_all_s", "max")),
    ("pressure", "loadavg1 median", ("pressure", "loadavg1", "median")),
    ("pressure", "io PSI some avg10 median", ("pressure", "psi_avg10", "io.some", "median")),
    ("pressure", "memory PSI some avg10 p95", ("pressure", "psi_avg10", "memory.some", "p95")),
    ("pressure", "direct-reclaim stalls (allocstall_normal)", ("pressure", "vmstat_delta", "allocstall_normal")),
    ("pressure", "major faults", ("pressure", "vmstat_delta", "pgmajfault")),
    ("scope_kills", "oomd / OOM kill lines, last 24 h", ("scope_kills", "count")),
]


def render(a: dict, b: dict) -> str:
    ka, kb = _g(a, "meta", "kernel"), _g(b, "meta", "kernel")
    out = [f"# A/B: {ka} vs {kb}", "", CEILING, "",
           "A is the capture taken before the bump, B after the reboot. A table row only means something "
           "when both sides ran, at a comparable hour and load.", "",
           "| metric | A | B | what would make this noise |", "|---|---|---|---|"]
    for key, label, path in ROWS:
        out.append(f"| {label} | {_fmt(_g(a, *path))} | {_fmt(_g(b, *path))} | {NOISE.get(key, '')} |")
    for side, cap in (("A", a), ("B", b)):
        nr = [f"{k}: {v.get('reason', '?')}" for k, v in cap.items()
              if isinstance(v, dict) and v.get("status") in ("not_run", "error")]
        for k, v in (cap.get("root_traces") or {}).items():
            if isinstance(v, dict) and v.get("status") in ("not_run", "error"):
                nr.append(f"root_traces.{k}: {v.get('reason', v.get('status'))}")
        out += ["", f"## Not run on {side}", ""] + ([f"* {x}" for x in nr] or ["* nothing"])
    hours = (_g(a, "meta", "hour_local"), _g(b, "meta", "hour_local"))
    out += ["", "## Reading this", "",
            f"Local hour of capture: A {hours[0]}, B {hours[1]}. "
            + ("Different hours: treat every pass-latency delta as unproven." if hours[0] != hours[1] else "Same hour."),
            "The scheduler traces (runqlat, offcputime, perf sched) live as text files next to each capture.json; "
            "compare the histograms by eye before believing any median."]
    return "\n".join(out) + "\n"


def write(a_dir: Path, b_dir: Path, dest: Path) -> Path:
    a = json.loads((a_dir / "capture.json").read_text())
    b = json.loads((b_dir / "capture.json").read_text())
    dest.write_text(render(a, b))
    return dest
