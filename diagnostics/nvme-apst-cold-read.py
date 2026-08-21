#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Cold-read latency of an NVMe device at two APST latency tolerances.

This is the measurement behind
`upstream-candidates/sent/0015-nvme-lower-default-apst-latency.patch`.
That patch lowers `default_ps_max_latency_us` from 100000 to 25000, and the
claim it rests on is that the stock 100 ms bound admits a non-operational
power state whose exit latency is an order of magnitude worse than the next
state up. This script measures the resulting stall directly.

How it works. `nvme_set_latency_tolerance()` re-runs `nvme_configure_apst()`
whenever the per-device PM QOS tolerance changes, so both settings are
reachable at runtime with no reboot and no module reload:

    /sys/class/nvme/nvme0/power/pm_qos_latency_tolerance_us

Note that `default_ps_max_latency_us` is NOT the knob to poke here. It is
read once in `nvme_add_ctrl()` and writing it later does not retune a live
controller — the PM QOS file above is the live override.

For each trial the script sets a tolerance, idles long enough for APST to
descend into the deepest state that tolerance admits, then times a single
4 KiB O_DIRECT read at a fresh random offset. The two tolerances are
interleaved trial by trial so thermal drift and background load hit both
equally rather than favouring whichever ran first.

Proof the A/B compared anything: before and after the trials it records
`nvme get-feature -f 0x0c -H`, whose ITPS fields say which power state the
controller's APST table actually targets. If that does not differ between
the two tolerances, the run measured nothing and says so.

Reading the result. Cold latency minus the warm baseline is the wake cost.
Contamination runs one way only: the root filesystem shares this device, so
background I/O can wake the drive inside an idle window, which pushes a cold
sample toward the warm mode. It can shrink the measured difference, never
inflate it. If the high-tolerance tail does not approach that state's
advertised exit latency, the drive never got deep enough and the numbers
should be discarded rather than reported.

Usage (needs root for the sysfs write and the raw-device read):
    sudo python3 diagnostics/nvme-apst-cold-read.py [--trials 60]
    sudo python3 diagnostics/nvme-apst-cold-read.py --json-out /tmp/apst.json

Nothing is written to the device. Every read is O_DIRECT and read-only, and
the original tolerance is restored on exit, including on Ctrl-C.
"""
from __future__ import annotations

import argparse
import json
import mmap
import os
import random
import re
import signal
import statistics
import subprocess
import sys
import time

BLOCK = 4096  # read size and offset alignment; a page, so mmap is aligned too
DESCENT_MULT = 50  # APST enters the next state after 50 * (enlat + exlat)


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def read_text(path: str) -> str:
    with open(path) as fh:
        return fh.read().strip()


def power_states(ctrl: str) -> list[dict]:
    """Non-operational power states from `nvme id-ctrl -H`, deepest last."""
    out = subprocess.run(["nvme", "id-ctrl", ctrl, "-H"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        die(f"nvme id-ctrl {ctrl} failed: {out.stderr.strip()}")
    states = []
    pat = re.compile(
        r"^ps\s+(\d+)\s*:\s*mp:(\S+)\s+(non-operational)\s+"
        r"enlat:(\d+)\s+exlat:(\d+)", re.M)
    for m in pat.finditer(out.stdout):
        states.append({
            "ps": int(m.group(1)),
            "max_power_w": m.group(2).rstrip("Ww"),
            "enlat_us": int(m.group(4)),
            "exlat_us": int(m.group(5)),
        })
    return sorted(states, key=lambda s: s["ps"])


def apst_target_states(ctrl: str) -> dict:
    """Which power states the controller's live APST table transitions to."""
    out = subprocess.run(["nvme", "get-feature", ctrl, "-f", "0x0c", "-H"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        return {"error": out.stderr.strip() or f"rc={out.returncode}"}
    itps = sorted({int(v) for v in re.findall(r"ITPS\)?\s*:\s*(\d+)", out.stdout)})
    itpt = sorted({int(v) for v in re.findall(r"ITPT\)?\s*:\s*(\d+)", out.stdout)})
    return {"itps_targets": itps, "deepest_target": max(itps) if itps else None,
            "itpt_ms": itpt}


def set_tolerance(qos_path: str, value: int) -> None:
    with open(qos_path, "w") as fh:
        fh.write(str(value))


def timed_read(fd: int, buf: mmap.mmap, size_bytes: int, rng: random.Random,
               used: set[int]) -> float:
    """One 4 KiB O_DIRECT read at an unused aligned offset. Returns ms."""
    blocks = size_bytes // BLOCK
    for _ in range(1000):
        off = rng.randrange(blocks) * BLOCK
        if off not in used:
            used.add(off)
            break
    else:
        die("ran out of unused offsets")
    t0 = time.perf_counter_ns()
    os.preadv(fd, [buf], off)
    return (time.perf_counter_ns() - t0) / 1e6


def pct(xs: list[float], p: float) -> float:
    s = sorted(xs)
    if not s:
        return float("nan")
    k = min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1))))
    return s[k]


def summarize(xs: list[float]) -> dict:
    return {
        "n": len(xs),
        "p50_ms": round(pct(xs, 50), 3),
        "p90_ms": round(pct(xs, 90), 3),
        "p99_ms": round(pct(xs, 99), 3),
        "max_ms": round(max(xs), 3) if xs else None,
        "mean_ms": round(statistics.fmean(xs), 3) if xs else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ctrl", default="/dev/nvme0")
    ap.add_argument("--device", default="/dev/nvme0n1")
    ap.add_argument("--sysfs", default="/sys/class/nvme/nvme0")
    ap.add_argument("--trials", type=int, default=60,
                    help="cold trials per tolerance (default 60)")
    ap.add_argument("--warm", type=int, default=60,
                    help="warm baseline reads (default 60)")
    ap.add_argument("--tolerances", default="25000,100000",
                    help="PM QOS values to compare, us (default 25000,100000)")
    ap.add_argument("--idle", type=float, default=0.0,
                    help="idle seconds per cold trial (0 = derive from the "
                         "device's own APST descent time, with margin)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--preflight", action="store_true",
                    help="only check that the two tolerances actually program "
                         "different APST targets, then exit; ~20s instead of "
                         "the full run")
    ap.add_argument("--json-out")
    args = ap.parse_args()

    if os.geteuid() != 0:
        die("needs root: writes the PM QOS sysfs file and reads the raw device")

    qos = f"{args.sysfs}/power/pm_qos_latency_tolerance_us"
    for p in (qos, args.device):
        if not os.path.exists(p):
            die(f"missing {p}")

    tolerances = [int(t) for t in args.tolerances.split(",")]
    if len(tolerances) != 2:
        die("--tolerances takes exactly two values")

    blockdev = os.path.basename(args.device)
    size_bytes = int(read_text(f"/sys/block/{blockdev}/size")) * 512
    model = read_text(f"{args.sysfs}/model")
    fw = read_text(f"{args.sysfs}/firmware_rev")
    original = int(read_text(qos))

    states = power_states(args.ctrl)
    if not states:
        die("no non-operational power states parsed from nvme id-ctrl")
    deepest = states[-1]

    # Descend past every non-operational state, then add margin. The kernel
    # waits 50 * (enlat + exlat) per hop, so the total is the sum of the hops.
    derived = sum(DESCENT_MULT * (s["enlat_us"] + s["exlat_us"])
                  for s in states) / 1e6
    # Margin covers one full restart: any disk activity mid-window resets the
    # descent, and a window shorter than 2x the descent then samples a
    # partially-descended device. Measured 2026-08-20 with 1.5x: 83% of trials
    # at the deep setting stalled one state short.
    idle = args.idle if args.idle > 0 else round(derived * 2.0 + 2.0, 2)

    log: list[str] = []

    def say(line: str = "") -> None:
        # Buffered: stdout may be redirected onto the device under test, and
        # writing during an idle window would wake the drive we are timing.
        log.append(line)

    say(f"device        {model} (fw {fw})")
    say(f"              {args.device}, {size_bytes / 1e12:.2f} TB")
    say("non-operational power states, from the device's own table:")
    for s in states:
        say(f"  ps {s['ps']}  exit {s['exlat_us'] / 1000:>7.1f} ms  "
            f"enter {s['enlat_us'] / 1000:>5.1f} ms  {s['max_power_w']} W")
    say(f"idle per cold trial   {idle}s "
        f"({'derived' if args.idle <= 0 else 'requested'}; "
        f"full descent needs ~{derived:.2f}s)")
    say(f"cold trials per tolerance   {args.trials}")
    say(f"tolerances compared         {tolerances[0]} us vs {tolerances[1]} us")
    say()

    if args.preflight:
        seen = {}
        try:
            for tol in tolerances:
                set_tolerance(qos, tol)
                time.sleep(0.3)
                seen[tol] = apst_target_states(args.ctrl)
                t = seen[tol]
                say(f"  {tol:>7} us -> APST targets ps {t.get('itps_targets')} "
                    f"(deepest {t.get('deepest_target')})")
        finally:
            set_tolerance(qos, original)
        a, b = (seen[t].get("deepest_target") for t in tolerances)
        say()
        if a is None or b is None:
            say("INCONCLUSIVE: could not read the APST table.")
        elif a == b:
            say(f"STOP: both tolerances target ps {a}. The full run would "
                "compare a configuration against itself. Nothing to measure.")
        else:
            say(f"OK: the arms differ (ps {a} vs ps {b}). Run without "
                "--preflight to measure.")
        print("\n".join(log))
        return 0

    rng = random.Random(args.seed)
    used: set[int] = set()
    cold: dict[int, list[float]] = {t: [] for t in tolerances}
    warm: list[float] = []
    apst: dict[int, dict] = {}
    interrupted = False

    fd = os.open(args.device, os.O_RDONLY | os.O_DIRECT)
    buf = mmap.mmap(-1, BLOCK)  # page-aligned, so O_DIRECT accepts it

    def restore(*_a) -> None:
        try:
            set_tolerance(qos, original)
        except OSError:
            pass

    def on_signal(_sig, _frm):
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    try:
        # Warm baseline first: drive is definitely in an operational state, so
        # this is media + queue latency with no wake component.
        for _ in range(args.warm):
            warm.append(timed_read(fd, buf, size_bytes, rng, used))

        # Record the APST table each tolerance actually programs.
        for tol in tolerances:
            set_tolerance(qos, tol)
            time.sleep(0.2)
            apst[tol] = apst_target_states(args.ctrl)

        for i in range(args.trials):
            # Alternate which tolerance leads, so ordering cannot favour one.
            order = tolerances if i % 2 == 0 else list(reversed(tolerances))
            for tol in order:
                set_tolerance(qos, tol)
                time.sleep(idle)
                cold[tol].append(
                    timed_read(fd, buf, size_bytes, rng, used))
            elapsed = (i + 1) * len(tolerances) * idle
            print(f"\rtrial {i + 1}/{args.trials}  "
                  f"~{elapsed / 60:.1f} min elapsed", end="", file=sys.stderr,
                  flush=True)
    except KeyboardInterrupt:
        interrupted = True
    finally:
        print("", file=sys.stderr)
        restore()
        buf.close()
        os.close(fd)

    warm_p50 = pct(warm, 50) if warm else float("nan")
    say(f"warm baseline (no idle)   p50 {warm_p50:.3f} ms   "
        f"p90 {pct(warm, 90):.3f} ms   n={len(warm)}")
    say()
    say("cold read after idle, by APST latency tolerance:")
    say(f"  {'tolerance':>12}  {'target PS':>9}  {'p50':>8}  {'p90':>8}  "
        f"{'p99':>8}  {'max':>8}  {'wake@p90':>9}")
    for tol in tolerances:
        xs = cold[tol]
        if not xs:
            say(f"  {tol:>12}  no samples")
            continue
        tgt = apst.get(tol, {}).get("deepest_target")
        say(f"  {tol:>12}  {str(tgt):>9}  "
            f"{pct(xs, 50):>7.3f}  {pct(xs, 90):>7.3f}  "
            f"{pct(xs, 99):>7.3f}  {max(xs):>7.3f}  "
            f"{pct(xs, 90) - warm_p50:>8.3f}")
    say("  (all figures ms)")
    say()

    lo, hi = tolerances[0], tolerances[1]
    tgt_lo = apst.get(lo, {}).get("deepest_target")
    tgt_hi = apst.get(hi, {}).get("deepest_target")
    verdict: list[str] = []
    if tgt_lo is None or tgt_hi is None:
        verdict.append("INCONCLUSIVE: could not read the APST table, so there "
                       "is no proof the controller was reprogrammed.")
    elif tgt_lo == tgt_hi:
        verdict.append(
            f"INCONCLUSIVE: both tolerances target power state {tgt_lo}, so "
            "the two arms are the same configuration and any difference here "
            "is noise. Nothing to report.")
    else:
        verdict.append(f"tolerance {lo} us targets ps {tgt_lo}; "
                       f"{hi} us targets ps {tgt_hi}. The arms differ.")
        if cold[hi]:
            reached = pct(cold[hi], 90)
            want = deepest["exlat_us"] / 1000.0
            if reached < want * 0.5:
                verdict.append(
                    f"INCONCLUSIVE: p90 at {hi} us is {reached:.1f} ms, well "
                    f"under ps {deepest['ps']}'s advertised {want:.1f} ms exit "
                    "latency. The drive never idled deep enough — most likely "
                    "background I/O on a shared filesystem. Discard these "
                    "numbers rather than reporting them.")
            else:
                verdict.append(
                    f"MEASURED: p90 {pct(cold[hi], 90):.1f} ms at {hi} us vs "
                    f"{pct(cold[lo], 90):.1f} ms at {lo} us, against a warm "
                    f"baseline of {warm_p50:.3f} ms.")
    if interrupted:
        verdict.append("NOTE: interrupted before completing all trials.")
    for line in verdict:
        say(line)

    report = {
        "device": {"model": model, "firmware": fw, "path": args.device,
                   "size_bytes": size_bytes},
        "kernel": os.uname().release,
        "non_operational_states": states,
        "idle_s": idle,
        "derived_descent_s": round(derived, 3),
        "read_bytes": BLOCK,
        "warm": summarize(warm),
        "cold": {str(t): summarize(cold[t]) for t in tolerances},
        "samples_ms": {
            "warm": [round(x, 4) for x in warm],
            **{str(t): [round(x, 4) for x in cold[t]] for t in tolerances},
        },
        "apst_table": {str(t): apst.get(t) for t in tolerances},
        "wake_cost_p90_ms": {
            str(t): round(pct(cold[t], 90) - warm_p50, 3)
            for t in tolerances if cold[t]},
        "verdict": verdict,
        "interrupted": interrupted,
        "restored_tolerance_us": original,
    }

    print("\n".join(log))
    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(report, fh, indent=2)
        print(f"json written to {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
