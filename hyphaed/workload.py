"""Workload capture for the trading room + GreenBoost, the A and B side of every kernel claim.

One `capture()` call writes `<out_dir>/capture.json` plus the raw trace files. Every
item ends as `ok`, `not_run` (with the reason) or `error`; a gate that couldn't run
says NOT RUN, never zero. Only reads: no writes to the engine, its DB or its .env.

Finding that shaped this (read, 2026-10-12): `premiums` and `funding_history` in
invest/ultra/context.py read JSON FILES (engine/var/funding/*.json, 6.5 MB total,
underlying_*.json), not the database. So the "slow DB reads" in pass-latency-live.md
are CPU time parsing JSON under load plus whatever the page cache does, which is why
this captures load, PSI and per-file parse time rather than pg query plans only.
"""
from __future__ import annotations

import json
import os
import re
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from .util import log
from .util.run import run, run_sudo

LEASE = Path("/tmp/invest-gpu.lock")
INVESTING = Path(os.environ.get("INVEST_ROOT", str(Path.home() / "Dev" / "investing")))
ENGINE_LOG = INVESTING / "engine" / "var" / "engine-run.log"
FUNDING_DIR = INVESTING / "engine" / "var" / "funding"
PG_CONTAINER = os.environ.get("INVEST_PG_CONTAINER", "invest-postgres")
GB_STATUS = Path("/sys/class/greenboost/greenboost/status")
_STAGE_RE = re.compile(r"^(\S+ \S+) .*ultra_stage_timing (\{.*\})\s*$")


def _item(status: str, **kw: Any) -> dict[str, Any]:
    return {"status": status, **kw}


def _sh(cmd: list[str], timeout: float = 30) -> str:
    r = run(cmd, check=False, timeout=timeout, force=True)
    return r.stdout or ""


def spread(xs: list[float]) -> dict[str, float]:
    """min/median/p95/max over repeats. One run is not a measurement, so n is carried."""
    if not xs:
        return {"n": 0}
    s = sorted(xs)
    p95 = s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))]
    return {"n": len(s), "min": s[0], "median": statistics.median(s), "p95": p95, "max": s[-1]}


def lease_held() -> bool:
    """The investing GPU lease is a file whose presence means a job owns the GPU."""
    try:
        return LEASE.exists() and LEASE.stat().st_size > 0
    except OSError:
        return False


def engine_pass_in_flight() -> bool:
    """A pass logged in the last 3 s means the engine is mid-cycle (passes land every ~30 s)."""
    try:
        return time.time() - ENGINE_LOG.stat().st_mtime < 3
    except OSError:
        return False


def wait_quiet(timeout_s: float = 90, sleep: Callable[[float], None] = time.sleep) -> bool:
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if not engine_pass_in_flight():
            return True
        sleep(2)
    return False


# ---- unprivileged probes ---------------------------------------------------

def read_psi() -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for res in ("cpu", "memory", "io"):
        try:
            txt = Path(f"/proc/pressure/{res}").read_text()
        except OSError:
            continue
        for ln in txt.splitlines():
            kind, *kv = ln.split()
            d = dict(p.split("=") for p in kv)
            out[f"{res}.{kind}"] = {k: float(v) for k, v in d.items()}
    return out


def read_vmstat_keys(keys: tuple[str, ...]) -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        for ln in Path("/proc/vmstat").read_text().splitlines():
            k, _, v = ln.partition(" ")
            if k in keys:
                out[k] = int(v)
    except OSError:
        pass
    return out


_VM_KEYS = ("pgmajfault", "pgscan_kswapd", "pgscan_direct", "pgsteal_kswapd", "pgsteal_direct",
            "allocstall_normal", "compact_stall", "thp_fault_alloc", "workingset_refault_file",
            "pswpin", "pswpout")


def sample_pressure(seconds: int, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """PSI + /proc/vmstat deltas + loadavg over `seconds`. avg10 samples give the spread."""
    v0, t0 = read_vmstat_keys(_VM_KEYS), time.monotonic()
    psi_samples: list[dict] = []
    load: list[float] = []
    for _ in range(max(1, seconds)):
        sleep(1)
        psi_samples.append(read_psi())
        load.append(float(Path("/proc/loadavg").read_text().split()[0]))
    v1, dt = read_vmstat_keys(_VM_KEYS), time.monotonic() - t0
    spreads = {k: spread([s[k]["avg10"] for s in psi_samples if k in s]) for k in (psi_samples[0] if psi_samples else {})}
    return _item("ok", seconds=round(dt, 1), psi_avg10=spreads, loadavg1=spread(load),
                 vmstat_delta={k: v1.get(k, 0) - v0.get(k, 0) for k in _VM_KEYS})


def greenboost_status() -> dict[str, Any]:
    try:
        return _item("ok", text=GB_STATUS.read_text())
    except OSError as e:
        return _item("not_run", reason=f"{GB_STATUS}: {e.strerror}")


def scope_kills(since: str) -> dict[str, Any]:
    txt = _sh(["journalctl", "--no-pager", "--since", since, "-g", "systemd-oomd|Killed|oom-kill|Out of memory"], 40)
    lines = [ln for ln in txt.splitlines() if ln.strip() and not ln.startswith("--")]
    return _item("ok", count=len(lines), sample=lines[:10])


def pass_latency(n: int = 400) -> dict[str, Any]:
    """Per-stage seconds of the last `n` logged ultra passes (the engine logs ultra_stage_timing)."""
    if not ENGINE_LOG.exists():
        return _item("not_run", reason=f"{ENGINE_LOG} missing")
    rows: list[dict] = []
    try:
        with ENGINE_LOG.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 8_000_000))
            for ln in fh.read().decode("utf-8", "replace").splitlines():
                m = _STAGE_RE.match(ln)
                if m:
                    try:
                        rows.append({"at": m.group(1), **json.loads(m.group(2))})
                    except json.JSONDecodeError:
                        pass
    except OSError as e:
        return _item("error", reason=str(e))
    rows = rows[-n:]
    if not rows:
        return _item("not_run", reason="no ultra_stage_timing lines in the log tail")
    totals = [r.get("total") or sum(v for k, v in r["stages"].items() if ":" not in k) for r in rows]
    ctx = [r["stages"].get("ctx", 0.0) for r in rows]
    return _item("ok", passes=len(rows), first=rows[0]["at"], last=rows[-1]["at"],
                 total_s=spread(totals), ctx_s=spread(ctx),
                 over_20s=sum(1 for t in totals if t > 20))


def _pg(sql: str) -> str:
    return _sh(["docker", "exec", PG_CONTAINER, "psql", "-U", "invest", "-d", "invest", "-At", "-c", sql], 30)


def postgres_waits() -> dict[str, Any]:
    if not _sh(["docker", "ps", "-q", "--filter", f"name={PG_CONTAINER}"]).strip():
        return _item("not_run", reason=f"container {PG_CONTAINER} not running")
    waits = _pg("select coalesce(wait_event_type,'none')||'/'||coalesce(wait_event,'none'), count(*) "
                "from pg_stat_activity where state<>'idle' group by 1 order by 2 desc limit 10")
    stm = _pg("select left(query,60), calls, round(mean_exec_time::numeric,2), round(max_exec_time::numeric,1) "
              "from pg_stat_statements order by total_exec_time desc limit 8")
    return _item("ok", active_waits=waits.splitlines(), top_statements=stm.splitlines())


def funding_file_cost(repeats: int = 5) -> dict[str, Any]:
    """Parse the funding files the way `funding_history` does, in a fresh process, `repeats` times.
    The cost is CPU (json.loads of ~600 KB a coin); fincore says whether the pages were resident."""
    files = sorted(FUNDING_DIR.glob("*.json")) if FUNDING_DIR.exists() else []
    if not files:
        return _item("not_run", reason=f"no files under {FUNDING_DIR}")
    code = ("import json,sys,time\nt=time.perf_counter()\n"
            "for f in sys.argv[1:]:\n    json.loads(open(f).read())\nprint(time.perf_counter()-t)")
    times = []
    for _ in range(repeats):
        out = _sh(["python3", "-I", "-c", code, *map(str, files)], 120).strip()
        if out:
            times.append(float(out))
    # fincore -b -n prints "<resident bytes> <pages> <size> <file>" per file
    rows = [ln.split() for ln in _sh(["fincore", "-b", "-n", *map(str, files)]).splitlines() if ln.strip()]
    res = sum(int(r[0]) for r in rows if r and r[0].isdigit())
    size = sum(int(r[2]) for r in rows if len(r) > 2 and r[2].isdigit())
    return _item("ok", files=len(files), parse_all_s=spread(times), resident_bytes=res, total_bytes=size)


def cold_funding_read() -> dict[str, Any]:
    """Opt-in: evict the funding files from the page cache (POSIX_FADV_DONTNEED, no root, only those
    files) and time a raw read. Cold read of 6.5 MB on NVMe is the I/O share of `funding_history`."""
    files = sorted(FUNDING_DIR.glob("*.json")) if FUNDING_DIR.exists() else []
    if not files:
        return _item("not_run", reason="no funding files")
    for f in files:
        fd = os.open(f, os.O_RDONLY)
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        finally:
            os.close(fd)
    t = time.perf_counter()
    n = sum(len(f.read_bytes()) for f in files)
    cold = time.perf_counter() - t
    t = time.perf_counter()
    sum(len(f.read_bytes()) for f in files)
    return _item("ok", bytes=n, cold_read_s=round(cold, 5), warm_read_s=round(time.perf_counter() - t, 5))


# ---- root probes (via sudo; perf/bpf are closed to users: perf_event_paranoid=2) ----

def _bcc(tool: str, args: list[str], out: Path, seconds: int) -> dict[str, Any]:
    exe = f"/usr/sbin/{tool}-bpfcc"
    if not Path(exe).exists():
        return _item("not_run", reason=f"{exe} not installed")
    r = run_sudo([exe, *args], check=False, timeout=seconds + 60, force=True)
    out.write_text(r.stdout or "")
    return _item("ok" if r.returncode == 0 else "error", file=out.name, rc=r.returncode)


def root_traces(out_dir: Path, seconds: int = 60) -> dict[str, Any]:
    res: dict[str, Any] = {}
    res["runqlat"] = _bcc("runqlat", ["-m", str(seconds), "1"], out_dir / "runqlat.txt", seconds)
    res["biolatency"] = _bcc("biolatency", ["-m", str(seconds), "1"], out_dir / "biolatency.txt", seconds)
    res["offcputime"] = _bcc("offcputime", ["-f", "-m", "1", str(seconds)], out_dir / "offcputime.txt", seconds)
    if Path("/usr/bin/perf").exists():
        r = run_sudo(["perf", "sched", "record", "-o", str(out_dir / "perf-sched.data"), "--", "sleep", str(seconds)],
                     check=False, timeout=seconds + 60, force=True)
        lat = run_sudo(["perf", "sched", "latency", "-i", str(out_dir / "perf-sched.data")], check=False, force=True)
        (out_dir / "perf-sched-latency.txt").write_text(lat.stdout or "")
        res["perf_sched"] = _item("ok" if r.returncode == 0 else "error", rc=r.returncode)
    else:
        res["perf_sched"] = _item("not_run", reason="perf not installed")
    return res


# ---- opt-in ---------------------------------------------------------------

def tok_s(model: str | None) -> dict[str, Any]:
    """Only ever run with an explicitly named model; GreenBoost never loads one on its own."""
    if not model:
        return _item("not_run", reason="no --model named; GreenBoost never loads a model unprompted")
    if lease_held():
        return _item("not_run", reason=f"GPU lease {LEASE} held; one GPU job at a time")
    return _item("not_run", reason=f"{model}: run `greenboost run` at ~700 tokens by hand and record under this dir "
                                   "(150-token bursts invert, see memory); not automated yet")


def capture(out_dir: Path, *, model: str | None = None, cold: bool = False, repeats: int = 3,
            seconds: int = 60, root: bool = True) -> Path:
    """Write out_dir/capture.json. Never raises on a failed probe: that probe is recorded instead."""
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    quiet = wait_quiet()
    meta = {"kernel": os.uname().release, "started": started, "hour_local": time.localtime().tm_hour,
            "engine_quiet_at_start": quiet, "gpu_lease_held": lease_held(),
            "repeats": repeats, "cold": cold}
    cap: dict[str, Any] = {"meta": meta}
    probes: dict[str, Callable[[], dict[str, Any]]] = {
        "greenboost": greenboost_status,
        "pass_latency": pass_latency,
        "postgres": postgres_waits,
        "funding_file_cost": lambda: funding_file_cost(repeats),
        "scope_kills": lambda: scope_kills("-1 day"),
        "pressure": lambda: sample_pressure(seconds),
        "tok_s_700": lambda: tok_s(model),
    }
    if cold:
        probes["funding_cold_read"] = cold_funding_read
    for name, fn in probes.items():
        log.info(f"capture: {name}")
        try:
            cap[name] = fn()
        except Exception as e:  # a probe must never kill the run
            cap[name] = _item("error", reason=f"{type(e).__name__}: {e}")
    if root:
        log.info(f"capture: root traces ({seconds}s each, via sudo)")
        try:
            cap["root_traces"] = root_traces(out_dir, seconds)
        except Exception as e:
            cap["root_traces"] = _item("error", reason=f"{type(e).__name__}: {e}")
    else:
        cap["root_traces"] = _item("not_run", reason="--no-root-traces")
    dest = out_dir / "capture.json"
    dest.write_text(json.dumps(cap, indent=2))
    return dest


def baseline_dir(root_dir: Path, kernel: str) -> Path:
    return root_dir / "out" / f"baseline-{kernel}"
