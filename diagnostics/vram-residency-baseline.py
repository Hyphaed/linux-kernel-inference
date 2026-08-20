#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Read-only baseline reporter for docs/research/spark-parity-survey.md's
"4 GB gap" objective. Does NOT run inference itself — samples nvidia-smi
and (if a GreenBoost synapse session is already running) its status ioctl,
to establish the numbers every Track-2 lever gets scored against:

  - VRAM occupancy breakdown (used vs total, context/activation overhead)
  - PCIe throughput counters (tx/rx) over the sampling window
  - PCIe link generation/width actually in use

Usage:
    python3 diagnostics/vram-residency-baseline.py [--seconds 30] [--interval 1]

No root required — everything here is read-only via `nvidia-smi` and, if
present, the GreenBoost status endpoint. Safe to run any time, including
while a real inference session is active (that's the intended use: run
this *during* a `gb-synapse serve()` session to get real numbers, not
idle-GPU numbers, which is why this stays a separate script rather than a
one-shot nvidia-smi query).
"""
import argparse
import json
import subprocess
import sys
import time


def nvidia_smi_query(fields: list[str]) -> dict[str, str] | None:
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                f"--query-gpu={','.join(fields)}",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(f"nvidia-smi query failed: {e}", file=sys.stderr)
        return None
    values = [v.strip() for v in out.split(",")]
    return dict(zip(fields, values))


def greenboost_status() -> dict | None:
    try:
        with open("/sys/class/greenboost/greenboost/status") as f:
            return json.loads(f.read())
    except (FileNotFoundError, PermissionError, json.JSONDecodeError):
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, default=30.0, help="sampling window")
    ap.add_argument("--interval", type=float, default=1.0, help="poll interval")
    args = ap.parse_args()

    fields = [
        "memory.used",
        "memory.total",
        "utilization.gpu",
        "pcie.link.gen.current",
        "pcie.link.gen.max",
        "pcie.link.width.current",
    ]
    print("=" * 70)
    print("VRAM / PCIe baseline — Track 2 (closing the 4 GB gap)")
    print("=" * 70)

    gb = greenboost_status()
    if gb is not None:
        print("\n-- GreenBoost status (live) --")
        print(json.dumps(gb, indent=2)[:2000])
    else:
        print("\n-- GreenBoost status --")
        print("(not available — module not loaded, no active session, or no")
        print(" permission to read /sys/class/greenboost/greenboost/status)")

    print(f"\n-- sampling nvidia-smi every {args.interval}s for {args.seconds}s --")
    samples: list[dict[str, str]] = []
    n = int(args.seconds / args.interval)
    for i in range(max(n, 1)):
        q = nvidia_smi_query(fields)
        if q is None:
            print("nvidia-smi unavailable — aborting baseline", file=sys.stderr)
            return 1
        samples.append(q)
        print(
            f"  [{i:3d}] mem={q['memory.used']}/{q['memory.total']} MiB  "
            f"util={q['utilization.gpu']}%  "
            f"pcie=gen{q['pcie.link.gen.current']}/max{q['pcie.link.gen.max']} "
            f"x{q['pcie.link.width.current']}"
        )
        if i < n - 1:
            time.sleep(args.interval)

    if not samples:
        print("no samples collected", file=sys.stderr)
        return 1

    used = [int(s["memory.used"]) for s in samples]
    total = int(samples[0]["memory.total"])
    gens = {s["pcie.link.gen.current"] for s in samples}

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"VRAM: {min(used)}-{max(used)} / {total} MiB used over the window")
    print(f"  headroom: {total - max(used)} MiB free at peak usage")
    print(f"PCIe generations observed: {sorted(gens)}")
    if len(gens) > 1 or "1" in gens:
        print(
            "  NOTE: link dropped to a lower generation during this window — "
            "cross-reference against diagnostics/gpu-link-speed-under-load.sh "
            "to confirm this is idle P-state behavior, not a training problem."
        )
    print(
        "\nThis script measures occupancy and link state only. Actual "
        "tok/s and bytes-crossed-per-token require running the real model "
        "under gb-synapse and reading dataflux_tok_s / dataflux_summary — "
        "see the standing GreenBoost-diagnosis rule; do not "
        "estimate those numbers from this script's output alone."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
