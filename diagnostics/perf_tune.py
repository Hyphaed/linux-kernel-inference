#!/usr/bin/env python3
"""Evidence-gated runtime tuning for the ncore workstation.

Findings and sources: docs/research/workstation-perf-tuning-2026-10-28.md
(tags A1/A2/M1..M12 below point at that file; N1 is newer).

Nothing here is applied on faith. Two ADOPT items go in with `apply`. Every
MEASURE-FIRST knob goes through `ab`, which benches the box, flips the knob,
benches again, restores it, benches a second baseline, and only calls a result
real when it clears the run-to-run noise. `keep` persists a knob you have seen
win; `revert` undoes anything this script wrote.

Never touched by design: firewall, forwarding, bridges, mitigations, Docker and
VMware networking, GPU clocks and the 300 W power limit.

  python3 diagnostics/perf_tune.py audit               # no root, no changes
  sudo python3 diagnostics/perf_tune.py apply          # A1 + A2 only
  sudo python3 diagnostics/perf_tune.py ab hwp_dynamic_boost dirty_bytes
  sudo python3 diagnostics/perf_tune.py keep dirty_bytes
  sudo python3 diagnostics/perf_tune.py revert all
  sudo python3 diagnostics/perf_tune.py bench --label before-reboot

Run `ab` with the machine idle: no GPU job, no compile, no game. Add --dry-run
to any mutating command to print what would be written and change nothing.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "out" / "perf"
STATE_DIR = Path("/var/lib/perf-tune")
STATE_FILE = STATE_DIR / "state.json"
# Must sort AFTER every other sysctl.d file. GreenBoost's installer rewrites
# 99-zzz-greenboost.conf with vm.dirty_ratio = 40, and writing a ratio zeroes
# vm.dirty_bytes, so a kept dirty_bytes in an earlier-sorting file is silently undone
# at boot. "-" sorts before "z", so this name loads last.
SYSCTL_DROPIN = Path("/etc/sysctl.d/99-zzzz-perf-tune.conf")
TMPFILES_DROPIN = Path("/etc/tmpfiles.d/97-perf-tune.conf")
ENV_DROPIN = Path("/etc/environment.d/90-perf-tune.conf")
HEADER = "# managed by diagnostics/perf_tune.py, remove with: perf_tune.py revert all\n"

DRY = False


def say(msg: str = "") -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(f"WARNING: {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# low-level file access
# ---------------------------------------------------------------------------

def read_val(path: str) -> str | None:
    """Current value of a sysfs/procfs file. A `[selected]` list (schedulers,
    THP) collapses to the selected word."""
    try:
        text = Path(path).read_text().strip()
    except OSError:
        return None
    m = re.search(r"\[([^\]]+)\]", text)
    return m.group(1) if m else text


def available_choices(path: str) -> list[str]:
    try:
        return re.sub(r"[\[\]]", "", Path(path).read_text()).split()
    except OSError:
        return []


def write_val(path: str, value: str) -> None:
    if DRY:
        say(f"  [dry-run] write {value!r} -> {path}")
        return
    with open(path, "w") as fh:
        fh.write(value)


def run(cmd: list[str], timeout: int | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout)


# ---------------------------------------------------------------------------
# knob registry
# ---------------------------------------------------------------------------

@dataclass
class Knob:
    name: str
    tag: str
    verdict: str                      # ADOPT | MEASURE
    why: str
    files: Callable[[], list[tuple[str, str]]] | None = None   # (path, proposed)
    extra_restore: Callable[[], list[str]] | None = None       # paths snapshotted too
    persistable: bool = True
    custom: "CustomKnob | None" = None

    def targets(self) -> list[tuple[str, str]]:
        return self.files() if self.files else []

    def available(self) -> bool:
        if self.custom:
            return self.custom.available()
        t = self.targets()
        return bool(t) and all(Path(p).exists() for p, _ in t)

    def snapshot(self) -> dict:
        if self.custom:
            return {"custom": self.custom.get()}
        paths = [p for p, _ in self.targets()]
        if self.extra_restore:
            paths = self.extra_restore() + paths
        return {"files": {p: read_val(p) for p in paths}}

    def apply(self) -> None:
        if self.custom:
            self.custom.set()
            return
        for path, value in self.targets():
            write_val(path, value)

    def restore(self, snap: dict) -> None:
        if self.custom:
            self.custom.restore(snap["custom"])
            return
        for path, value in snap["files"].items():
            if value is not None:
                write_val(path, value)

    def current(self) -> str:
        if self.custom:
            return str(self.custom.get())
        return ", ".join(f"{Path(p).name}={read_val(p)}" for p, _ in self.targets())

    def proposed(self) -> str:
        if self.custom:
            return self.custom.proposed
        return ", ".join(f"{Path(p).name}={v}" for p, v in self.targets())


class CustomKnob:
    proposed = ""

    def available(self) -> bool: ...
    def get(self): ...
    def set(self) -> None: ...
    def restore(self, orig) -> None: ...


class EppPerformance(CustomKnob):
    """M2. power-profiles-daemon owns EPP, a raw sysfs write is overwritten."""
    proposed = "power profile = performance"

    def available(self) -> bool:
        return shutil.which("powerprofilesctl") is not None

    def get(self) -> str:
        return run(["powerprofilesctl", "get"]).stdout.strip()

    def set(self) -> None:
        if DRY:
            say("  [dry-run] powerprofilesctl set performance")
            return
        r = run(["powerprofilesctl", "set", "performance"])
        if r.returncode:
            raise RuntimeError(f"powerprofilesctl set performance: {r.stderr.strip()}")

    def restore(self, orig: str) -> None:
        if DRY:
            say(f"  [dry-run] powerprofilesctl set {orig}")
        elif orig:
            run(["powerprofilesctl", "set", orig])


class RootNoatime(CustomKnob):
    """M7. Runtime remount only. Persisting needs an fstab edit, left to you."""
    proposed = "/ mounted noatime"

    def available(self) -> bool:
        return shutil.which("findmnt") is not None

    def get(self) -> str:
        opts = run(["findmnt", "-no", "OPTIONS", "/"]).stdout.strip().split(",")
        return next((o for o in ("noatime", "strictatime", "relatime") if o in opts), "relatime")

    def _remount(self, mode: str) -> None:
        if DRY:
            say(f"  [dry-run] mount -o remount,{mode} /")
            return
        r = run(["mount", "-o", f"remount,{mode}", "/"])
        if r.returncode:
            raise RuntimeError(f"remount,{mode}: {r.stderr.strip()}")

    def set(self) -> None:
        self._remount("noatime")

    def restore(self, orig: str) -> None:
        self._remount(orig)


class ScxOff(CustomKnob):
    """M12. hyphaed-scx.service runs scx_bpfland; stopping it falls back to
    the stock EEVDF scheduler live."""
    proposed = "hyphaed-scx stopped (EEVDF)"

    def available(self) -> bool:
        return run(["systemctl", "cat", "hyphaed-scx.service"]).returncode == 0

    def get(self) -> str:
        return run(["systemctl", "is-active", "hyphaed-scx.service"]).stdout.strip()

    def set(self) -> None:
        if DRY:
            say("  [dry-run] systemctl stop hyphaed-scx.service")
            return
        run(["systemctl", "stop", "hyphaed-scx.service"])
        time.sleep(2)

    def restore(self, orig: str) -> None:
        if DRY:
            say("  [dry-run] systemctl start hyphaed-scx.service")
        elif orig == "active":
            run(["systemctl", "start", "hyphaed-scx.service"])
            time.sleep(2)


class PreemptLazy(CustomKnob):
    """N1. This kernel is PREEMPT_DYNAMIC on an arch with lazy preemption, so the
    mode switches live through debugfs. Mainline defaults to lazy; fragment 10
    pins full. Lazy gives a task up to one tick (1 ms at HZ=1000) to yield before
    it is forced off the CPU, which cuts preemptions at a bounded latency cost.
    Runtime only: persist with the boot parameter preempt=lazy."""
    proposed = "preempt mode = lazy"

    def __init__(self, path: str = "/sys/kernel/debug/sched/preempt") -> None:
        self.path = path

    def available(self) -> bool:
        return self.get() is not None

    def get(self) -> str | None:
        try:
            m = re.search(r"\((\w+)\)", Path(self.path).read_text())
        except OSError:                      # debugfs is root-only
            return None
        return m.group(1) if m else None

    def set(self) -> None:
        write_val(self.path, "lazy")

    def restore(self, orig: str) -> None:
        if orig:
            write_val(self.path, orig)


class RaplPl1(CustomKnob):
    """N2. Sustained package power limit. Live reading on 2026-10-28: PL1 65 W
    (window 28 s), PL2 135 W, while the same sysfs entry reports max 125 W. The
    target is read from the hardware's own constraint_0_max_power_uw, never
    hard-coded. Firmware can lock the limit, in which case the write is refused
    and the A/B skips this knob. Heat and fan noise are the cost."""
    proposed = "PL1 = the chip's reported maximum"

    def __init__(self, base: str = "/sys/class/powercap/intel-rapl:0") -> None:
        self.base = Path(base)

    def _int(self, name: str) -> int | None:
        try:
            return int((self.base / name).read_text())
        except (OSError, ValueError):
            return None

    def available(self) -> bool:
        try:
            ok = (self.base / "constraint_0_name").read_text().strip() == "long_term"
        except OSError:
            return False
        return ok and bool(self._int("constraint_0_max_power_uw")) and self.get() is not None

    def get(self) -> int | None:
        return self._int("constraint_0_power_limit_uw")

    def set(self) -> None:
        target = self._int("constraint_0_max_power_uw")
        if DRY:
            say(f"  [dry-run] PL1 -> {target} uW")
            return
        try:
            (self.base / "constraint_0_power_limit_uw").write_text(str(target))
        except OSError as e:
            raise RuntimeError(f"PL1 write refused ({e}); the firmware may have locked it") from e

    def restore(self, orig: int) -> None:
        if DRY:
            say(f"  [dry-run] PL1 -> {orig} uW")
        elif orig:
            (self.base / "constraint_0_power_limit_uw").write_text(str(orig))


def _root_dm() -> str | None:
    src = run(["findmnt", "-no", "SOURCE", "/"]).stdout.strip()
    real = os.path.realpath(src) if src else ""
    return os.path.basename(real) if real.startswith("/dev/dm-") else None


def _nvme() -> str | None:
    devs = sorted(glob.glob("/sys/block/nvme*n1"))
    return os.path.basename(devs[0]) if devs else None


def _read_ahead_files() -> list[tuple[str, str]]:
    # Buffered readahead is set by the device the filesystem sits on, which
    # here is the LVM dm device, so both layers must change together.
    out = []
    for dev in (_nvme(), _root_dm()):
        if dev:
            out.append((f"/sys/block/{dev}/queue/read_ahead_kb", "1024"))
    return out


def scheduler_available(path: str, name: str) -> bool:
    """The scheduler file lists only loaded schedulers. Writing the name of one
    whose module exists autoloads it, so kyber is usable before its first use
    (7.2.12 builds it as kyber-iosched.ko, CONFIG_MQ_IOSCHED_KYBER=m)."""
    return name in available_choices(path) or run(["modinfo", "-n", f"{name}-iosched"]).returncode == 0


def _sched_files() -> list[tuple[str, str]]:
    dev = _nvme()
    if not dev:
        return []
    path = f"/sys/block/{dev}/queue/scheduler"
    pick = next((c for c in ("kyber", "mq-deadline") if scheduler_available(path, c)), None)
    return [(path, pick)] if pick else []


def _dirty_extra() -> list[str]:
    return ["/proc/sys/vm/dirty_ratio", "/proc/sys/vm/dirty_background_ratio"]


KNOBS: dict[str, Knob] = {k.name: k for k in [
    Knob("nmi_watchdog", "A1", "ADOPT",
         "frees a PMU counter and drops the periodic lockup NMI (CachyOS-Settings does too). "
         "Costs hard-lockup detection while kdump-tools is enabled.",
         files=lambda: [("/proc/sys/kernel/nmi_watchdog", "0")]),
    Knob("cuda_cache", "A2", "ADOPT",
         "CUDA JIT cache 1 GiB -> 4 GiB so big FLUX/torch kernels stop recompiling. Zero risk, "
         "benefit unproven here. Applies on next login.",
         persistable=True),
    Knob("hwp_dynamic_boost", "M1", "MEASURE",
         "raises the P-state briefly after an I/O wait (model loads, compiles). Slightly higher idle power.",
         files=lambda: [("/sys/devices/system/cpu/intel_pstate/hwp_dynamic_boost", "1")]),
    Knob("epp_performance", "M2", "MEASURE",
         "EPP balance_performance -> performance. Heat and E-core power; no desktop numbers in the docs.",
         persistable=False, custom=EppPerformance()),
    Knob("dirty_bytes", "M3", "MEASURE",
         "256 MiB / 64 MiB dirty limits instead of 40% of 61 GiB (about 24 GiB of dirty cache before throttling).",
         files=lambda: [("/proc/sys/vm/dirty_bytes", "268435456"),
                        ("/proc/sys/vm/dirty_background_bytes", "67108864")],
         extra_restore=_dirty_extra),
    Knob("read_ahead_1024", "M5", "MEASURE",
         "readahead 4096 -> 1024 kB on the NVMe and its LVM device. A blog suggests smaller for NVMe; "
         "4096 may be right for mmap'd GGUF loads.",
         files=_read_ahead_files),
    Knob("nvme_sched", "M6", "MEASURE",
         "none -> kyber (autoloads from kyber-iosched.ko; mq-deadline only if the module is missing). "
         "Only helps read latency under a heavy writer.",
         files=_sched_files),
    Knob("noatime", "M7", "MEASURE",
         "/ remounted noatime (relatime is already on, so the gain is small). Runtime only.",
         persistable=False, custom=RootNoatime()),
    Knob("mthp_64k", "M8", "MEASURE",
         "enable 64 kB multi-size THP as 'inherit'. Mixed evidence, all 2024 arm64; watch zram swap-outs.",
         files=lambda: [("/sys/kernel/mm/transparent_hugepage/hugepages-64kB/enabled", "inherit")]),
    Knob("preempt_lazy", "N1", "MEASURE",
         "preemption full -> lazy (mainline's default; the kernel supports it live). Fewer preemptions, so throughput may "
         "rise, at up to 1 ms extra latency at HZ=1000. Evidence is thin and mixed; run it as `ab --scx-off preempt_lazy` "
         "because sched_ext bypasses lazy preemption. Runtime only.",
         persistable=False, custom=PreemptLazy()),
    Knob("rapl_pl1", "N2", "MEASURE",
         "sustained CPU power limit 65 W -> the chip's reported 125 W maximum (PL2 untouched). More all-core speed for "
         "CPU-side expert decode, builds and CPU stages; costs heat and fan noise on an ITX cooler. Firmware may lock it. "
         "Runtime only; persist through BIOS PL1.",
         persistable=False, custom=RaplPl1()),
    Knob("scx_off", "M12", "MEASURE",
         "stop scx_bpfland and run stock EEVDF. No measured result for this CPU exists; this is the A/B. Runtime only.",
         persistable=False, custom=ScxOff()),
]}

ADOPT = [k.name for k in KNOBS.values() if k.verdict == "ADOPT"]
MEASURE = [k.name for k in KNOBS.values() if k.verdict == "MEASURE"]
FORBIDDEN = ("forward", "mitigations", "ufw", "iptables", "nft", "bridge", "br_netfilter", "ip_forward")


# ---------------------------------------------------------------------------
# persistence: state file plus the three drop-ins, regenerated from the state
# ---------------------------------------------------------------------------

def sysctl_key(path: str) -> str | None:
    return path[len("/proc/sys/"):].replace("/", ".") if path.startswith("/proc/sys/") else None


def render_sysctl(entries: dict[str, str]) -> str:
    return HEADER + "".join(f"{k} = {v}\n" for k, v in sorted(entries.items()))


def render_tmpfiles(entries: dict[str, str]) -> str:
    return HEADER + "".join(f"w- {p} - - - - {v}\n" for p, v in sorted(entries.items()))


def render_env(entries: dict[str, str]) -> str:
    return HEADER + "".join(f"{k}={v}\n" for k, v in sorted(entries.items()))


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {"persisted": {}, "pending": {}}


def save_state(state: dict) -> None:
    if DRY:
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def regenerate_dropins(state: dict) -> None:
    sysctl, tmpf, env = {}, {}, {}
    for name in state["persisted"]:
        knob = KNOBS.get(name)
        if not knob:
            continue
        if name == "cuda_cache":
            env["CUDA_CACHE_MAXSIZE"] = "4294967296"
            continue
        for path, value in knob.targets():
            key = sysctl_key(path)
            if key:
                sysctl[key] = value
            else:
                tmpf[path] = value
    for dropin, entries, render in ((SYSCTL_DROPIN, sysctl, render_sysctl),
                                    (TMPFILES_DROPIN, tmpf, render_tmpfiles),
                                    (ENV_DROPIN, env, render_env)):
        if DRY:
            if entries:
                say(f"  [dry-run] {dropin}:\n" + render(entries).rstrip())
            continue
        if entries:
            dropin.parent.mkdir(parents=True, exist_ok=True)
            dropin.write_text(render(entries))
        elif dropin.exists():
            dropin.unlink()


def persist(name: str, snap: dict) -> None:
    state = load_state()
    state["persisted"][name] = {"orig": snap, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    save_state(state)
    regenerate_dropins(state)


# ---------------------------------------------------------------------------
# benchmark harness
# ---------------------------------------------------------------------------

# name -> (unit, higher_is_better)
METRICS: dict[str, tuple[str, bool]] = {
    "seq_read_cold_MBps": ("MB/s", True),
    "randread_4k_iops": ("IOPS", True),
    "randread_4k_p99_us": ("us", False),
    "mixed_read_p99_us": ("us", False),
    "dirty_write_total_s": ("s", False),
    "dirty_read_p99_us": ("us", False),
    "dirty_read_p999_us": ("us", False),
    "cpu_p16_bogo_s": ("ops/s", True),
    "cpu_all32_bogo_s": ("ops/s", True),
    "stream_bogo_s": ("ops/s", True),
    "sched_messaging_s": ("s", False),
    "sched_pipe_s": ("s", False),
}


def preflight(force: bool) -> None:
    load1 = float(Path("/proc/loadavg").read_text().split()[0])
    gpu = run(["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"]).stdout.strip()
    gpu_util = float(gpu) if gpu.replace(".", "", 1).isdigit() else 0.0
    problems = []
    if load1 > 3.0:
        problems.append(f"1-minute load is {load1:.1f} (> 3.0)")
    if gpu_util > 10:
        problems.append(f"GPU utilisation is {gpu_util:.0f}% (> 10%)")
    if problems and not force:
        sys.exit("Machine isn't idle: " + "; ".join(problems) +
                 ". Numbers taken now would be noise. Stop the job, wait, or pass --force.")
    for p in problems:
        warn(p + " (forced)")


def drop_caches() -> bool:
    if os.geteuid() != 0:
        warn("not root: page cache not dropped, cold-read numbers are not comparable")
        return False
    os.sync()
    Path("/proc/sys/vm/drop_caches").write_text("3")
    return True


def fio_json(args: list[str]) -> dict:
    r = run(["fio", "--output-format=json", *args])
    try:
        return json.loads(r.stdout[r.stdout.index("{"):])
    except (ValueError, json.JSONDecodeError):
        raise RuntimeError(f"fio failed: {r.stderr.strip() or r.stdout[-300:]}")


def _p(job: dict, side: str, pct: str) -> float:
    return job[side].get("clat_ns", {}).get("percentile", {}).get(pct, 0) / 1000.0


def one_rep(workdir: Path, runtime: int, size_gb: float) -> dict[str, float]:
    m: dict[str, float] = {}
    big = workdir / "pt-read.bin"
    size = f"{int(size_gb * 1024)}M"
    if not big.exists() or big.stat().st_size < size_gb * 2**30 * 0.99:
        fio_json(["--name=prep", f"--filename={big}", f"--size={size}", "--rw=write",
                  "--bs=1M", "--direct=1", "--end_fsync=1"])

    drop_caches()
    j = fio_json(["--name=seq", f"--filename={big}", f"--size={size}", "--rw=read", "--bs=1M",
                  "--ioengine=psync", "--direct=0"])["jobs"][0]
    m["seq_read_cold_MBps"] = j["read"]["bw"] / 1024.0

    j = fio_json(["--name=rr", f"--filename={big}", f"--size={size}", "--rw=randread", "--bs=4k",
                  "--iodepth=32", "--ioengine=libaio", "--direct=1", "--time_based",
                  f"--runtime={runtime}"])["jobs"][0]
    m["randread_4k_iops"] = j["read"]["iops"]
    m["randread_4k_p99_us"] = _p(j, "read", "99.000000")

    j = fio_json(["--name=mix", f"--filename={big}", f"--size={size}", "--rw=randrw",
                  "--rwmixread=70", "--bs=4k", "--iodepth=16", "--ioengine=libaio", "--direct=1",
                  "--time_based", f"--runtime={runtime}"])["jobs"][0]
    m["mixed_read_p99_us"] = _p(j, "read", "99.000000")

    # buffered writer (dirty-page behaviour) with a concurrent small random reader
    wfile = workdir / "pt-write.bin"
    d = fio_json(["--name=w", f"--filename={wfile}", f"--size={int(size_gb * 768)}M", "--rw=write",
                  "--bs=1M", "--ioengine=psync", "--direct=0", "--end_fsync=1",
                  "--name=r", f"--filename={big}", f"--size={size}", "--rw=randread", "--bs=4k",
                  "--iodepth=4", "--ioengine=libaio", "--direct=1", "--time_based",
                  f"--runtime={max(runtime, 20)}"])
    by = {jb["jobname"]: jb for jb in d["jobs"]}
    m["dirty_write_total_s"] = by["w"]["job_runtime"] / 1000.0
    m["dirty_read_p99_us"] = _p(by["r"], "read", "99.000000")
    m["dirty_read_p999_us"] = _p(by["r"], "read", "99.900000")
    wfile.unlink(missing_ok=True)

    def bogo(cmd: list[str], tmp: Path) -> float:
        run(cmd + ["--metrics", "--yaml", str(tmp)])
        mt = re.search(r"bogo-ops-per-second-real-time:\s*([0-9.]+)", tmp.read_text())
        return float(mt.group(1)) if mt else 0.0

    y = workdir / "stress.yaml"
    m["cpu_p16_bogo_s"] = bogo(["taskset", "-c", "0-15", "stress-ng", "--cpu", "16",
                                "--cpu-method", "matrixprod", "-t", str(runtime)], y)
    m["cpu_all32_bogo_s"] = bogo(["stress-ng", "--cpu", "32", "--cpu-method", "matrixprod",
                                  "-t", str(runtime)], y)
    m["stream_bogo_s"] = bogo(["stress-ng", "--stream", "8", "-t", str(runtime)], y)

    for key, cmd in (("sched_messaging_s", ["perf", "bench", "sched", "messaging", "-g", "20", "-l", "5000"]),
                     ("sched_pipe_s", ["perf", "bench", "sched", "pipe", "-l", "500000"])):
        r = run(cmd)
        mt = re.search(r"Total time:\s*([0-9.]+)", r.stdout + r.stderr)
        m[key] = float(mt.group(1)) if mt else 0.0
    return m


def bench_context() -> dict:
    def sysfs(p: str) -> str | None:
        return read_val(p)
    gpu = run(["nvidia-smi", "--query-gpu=temperature.gpu,clocks.sm,pstate,power.draw",
               "--format=csv,noheader"]).stdout.strip()
    return {
        "kernel": os.uname().release,
        "scx_ops": sysfs("/sys/kernel/sched_ext/root/ops"),
        "governor": sysfs("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"),
        "epp": sysfs("/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference"),
        "ppd": run(["powerprofilesctl", "get"]).stdout.strip() if shutil.which("powerprofilesctl") else None,
        "hwp_dynamic_boost": sysfs("/sys/devices/system/cpu/intel_pstate/hwp_dynamic_boost"),
        "dirty": [sysfs(f"/proc/sys/vm/{n}") for n in ("dirty_ratio", "dirty_bytes")],
        "nvme_sched": sysfs(f"/sys/block/{_nvme()}/queue/scheduler") if _nvme() else None,
        "gpu_temp_clk_pstate_pw": gpu,
        "cpu_pkg_temp_c": next((int(t.read_text()) / 1000 for t in sorted(Path("/sys/class/hwmon").glob("hwmon*/temp1_input"))
                                if (t.parent / "name").read_text().strip() == "coretemp"), None),
        "loadavg": Path("/proc/loadavg").read_text().split()[:3],
    }


def run_bench(label: str, reps: int, runtime: int, workdir: Path, size_gb: float) -> dict:
    workdir.mkdir(parents=True, exist_ok=True)
    say(f"[bench:{label}] {reps} rep(s), {runtime}s per timed job, test files in {workdir}")
    reps_out: dict[str, list[float]] = {k: [] for k in METRICS}
    try:
        for i in range(reps):
            t0 = time.time()
            for k, v in one_rep(workdir, runtime, size_gb).items():
                reps_out[k].append(v)
            say(f"  rep {i + 1}/{reps} done in {time.time() - t0:.0f}s")
    finally:
        for f in workdir.glob("pt-*.bin"):
            f.unlink(missing_ok=True)
    return {"label": label, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "ctx": bench_context(), "reps": reps_out}


# ---------------------------------------------------------------------------
# comparison: a delta only counts if it clears the measured noise
# ---------------------------------------------------------------------------

MIN_EFFECT_PCT = 3.0


def rel_stdev(vals: list[float]) -> float:
    if len(vals) < 2 or statistics.mean(vals) == 0:
        return 0.0
    return statistics.stdev(vals) / abs(statistics.mean(vals)) * 100.0


def compare(base_a: dict, test: dict, base_b: dict) -> list[dict]:
    """base_a/test/base_b are {metric: [reps]}. Positive `gain_pct` is better."""
    rows = []
    for name, (unit, higher) in METRICS.items():
        a, t, b = base_a.get(name, []), test.get(name, []), base_b.get(name, [])
        if not (a and t and b):
            continue
        ma, mt, mb = statistics.median(a), statistics.median(t), statistics.median(b)
        base = (ma + mb) / 2.0
        if base == 0:
            continue
        gain = (mt - base) / base * 100.0
        if not higher:
            gain = -gain
        drift = abs(ma - mb) / base * 100.0
        noise = max(rel_stdev(a), rel_stdev(t), rel_stdev(b), drift)
        floor = max(MIN_EFFECT_PCT, 2.0 * noise)
        verdict = "better" if gain > floor else "worse" if gain < -floor else "no change"
        rows.append({"metric": name, "unit": unit, "base": base, "test": mt, "gain_pct": gain,
                     "noise_pct": noise, "floor_pct": floor, "verdict": verdict})
    return rows


def overall(rows: list[dict]) -> str:
    better = [r for r in rows if r["verdict"] == "better"]
    worse = [r for r in rows if r["verdict"] == "worse"]
    if better and not worse:
        return "WIN: improved without regressions"
    if worse and not better:
        return "LOSS: regressions only"
    if better and worse:
        return "MIXED: trade-off, your call"
    return "NO MEASURABLE EFFECT: not worth keeping"


def format_report(knob: Knob, rows: list[dict]) -> str:
    lines = [f"## {knob.name} ({knob.tag})", f"{knob.why}", "",
             f"Verdict: **{overall(rows)}**", "",
             "| metric | baseline | with knob | gain | noise | verdict |", "|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['metric']} | {r['base']:.4g} {r['unit']} | {r['test']:.4g} | "
                     f"{r['gain_pct']:+.1f}% | ±{r['noise_pct']:.1f}% | {r['verdict']} |")
    lines.append("")
    lines.append(f"A gain only counts above max({MIN_EFFECT_PCT:.0f}%, 2x noise), where noise is the largest of the rep "
                 "spread and the drift between the two baselines bracketing the test.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def need_root() -> None:
    if os.geteuid() != 0 and not DRY:
        sys.exit("This needs root: sudo python3 diagnostics/perf_tune.py ...")


def own_outputs(path: Path) -> None:
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if uid and gid and path.exists():
        os.chown(path, int(uid), int(gid))


def cmd_audit(_: argparse.Namespace) -> int:
    state = load_state()
    say(f"{'knob':<20}{'tag':<5}{'verdict':<9}{'current':<46}proposed")
    for k in KNOBS.values():
        if k.name == "cuda_cache":
            cur = os.environ.get("CUDA_CACHE_MAXSIZE") or ("persisted" if ENV_DROPIN.exists() else "default (1 GiB)")
            prop = "CUDA_CACHE_MAXSIZE=4 GiB"
        elif not k.available():
            cur, prop = "unavailable (or needs root)", "-"
        else:
            cur, prop = k.current(), k.proposed()
        mark = " *" if k.name in state["persisted"] else ""
        say(f"{k.name:<20}{k.tag:<5}{k.verdict:<9}{cur[:44]:<46}{prop}{mark}")
    say("\n* = persisted by this script. ADOPT items go in with `apply`; MEASURE items need `ab` first.")
    soft = Path("/proc/net/softnet_stat")
    if soft.exists():
        drops = sum(int(line.split()[1], 16) for line in soft.read_text().splitlines())
        say(f"\nM11 netdev_max_backlog: {drops} backlog drops since boot "
            f"({'raise it' if drops else 'nothing to fix, skip it'}).")
    say(f"M12 scheduler: {read_val('/sys/kernel/sched_ext/root/ops') or 'none (EEVDF)'}")
    say("Research: docs/research/workstation-perf-tuning-2026-10-28.md")
    return 0


def cmd_bench(a: argparse.Namespace) -> int:
    preflight(a.force)
    res = run_bench(a.label, a.reps, a.runtime, Path(a.dir), a.size_gb)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"bench-{a.label}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(res, indent=2))
    own_outputs(path)
    for k, vals in res["reps"].items():
        if vals:
            say(f"  {k:<24}{statistics.median(vals):>14.4g} {METRICS[k][0]:<7} (±{rel_stdev(vals):.1f}%)")
    say(f"saved {path}")
    return 0


def cmd_ab(a: argparse.Namespace) -> int:
    """With --scx-off the whole A/B runs with sched_ext stopped, because scx replaces the
    fair class and bypasses lazy preemption, EEVDF slices and ITMT priority. A scheduler or
    preemption A/B taken with scx running measures nothing."""
    need_root()
    scx = ScxOff() if a.scx_off else None
    orig = scx.get() if scx else None
    if scx:
        say("stopping hyphaed-scx for the whole A/B (restored afterwards)")
        scx.set()
    try:
        return _cmd_ab(a)
    finally:
        if scx:
            scx.restore(orig)
            say("hyphaed-scx restored")


def _cmd_ab(a: argparse.Namespace) -> int:
    need_root()
    names = MEASURE if a.knobs == ["all"] else a.knobs
    bad = [n for n in names if n not in KNOBS or KNOBS[n].verdict != "MEASURE"]
    if bad:
        sys.exit(f"Unknown or non-A/B knob(s): {', '.join(bad)}. A/B knobs: {', '.join(MEASURE)}")
    preflight(a.force)
    est = 3 * a.reps * (6 * a.runtime + 45) / 60
    say(f"~{est:.0f} min per knob (3 bench passes). Machine must stay idle.\n")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = OUT_DIR / f"ab-{time.strftime('%Y%m%d-%H%M%S')}.md"
    sections = []
    for name in names:
        knob = KNOBS[name]
        if not knob.available():
            say(f"skip {name}: not available on this system")
            continue
        snap = knob.snapshot()
        state = load_state()
        state["pending"][name] = snap
        save_state(state)
        wd = Path(a.dir)
        base1 = run_bench(f"{name}-base1", a.reps, a.runtime, wd, a.size_gb)
        say(f"\napplying {name}: {knob.proposed()}")
        failed = ""
        try:
            knob.apply()
            test = run_bench(f"{name}-test", a.reps, a.runtime, wd, a.size_gb)
        except (OSError, RuntimeError) as e:
            failed = str(e)
        finally:
            knob.restore(snap)
            say(f"restored {name}")
        if failed:
            say(f"skip {name}: {failed}")
            state = load_state()
            state["pending"].pop(name, None)
            save_state(state)
            continue
        base2 = run_bench(f"{name}-base2", a.reps, a.runtime, wd, a.size_gb)
        state = load_state()
        state["pending"].pop(name, None)
        save_state(state)
        rows = compare(base1["reps"], test["reps"], base2["reps"])
        text = format_report(knob, rows)
        say("\n" + text + "\n")
        sections.append(text)
        if not DRY:
            report_path.write_text("# A/B results\n\n" + "\n\n".join(sections) + "\n")
            own_outputs(report_path)
    say(f"report: {report_path}\nKeep a winner with: sudo python3 diagnostics/perf_tune.py keep <knob>")
    return 0


def cmd_apply(_: argparse.Namespace) -> int:
    need_root()
    for name in ADOPT:
        knob = KNOBS[name]
        snap = knob.snapshot() if name != "cuda_cache" else {"files": {}}
        say(f"apply {name} ({knob.tag}): {knob.proposed() if name != 'cuda_cache' else 'CUDA_CACHE_MAXSIZE=4 GiB'}")
        if name != "cuda_cache":
            knob.apply()
        persist(name, snap)
    say("\n" + ("(dry-run: nothing was changed) " if DRY else "nmi_watchdog is live now. ") +
        "CUDA_CACHE_MAXSIZE applies after your next login "
        "(export it in the FLUX launcher to use it sooner).")
    say("Undo: sudo python3 diagnostics/perf_tune.py revert all")
    return 0


def cmd_keep(a: argparse.Namespace) -> int:
    need_root()
    knob = KNOBS.get(a.knob)
    if not knob or not knob.persistable:
        sys.exit(f"{a.knob}: not persistable here. "
                 + {"noatime": "Edit /etc/fstab yourself (add noatime to the / line).",
                    "scx_off": "Run: sudo systemctl disable --now hyphaed-scx.service",
                    "epp_performance": "Use powerprofilesctl set performance."}.get(a.knob, "Unknown knob."))
    snap = knob.snapshot()
    knob.apply()
    persist(a.knob, snap)
    say(f"kept {a.knob}: live now and persisted. Undo: perf_tune.py revert {a.knob}")
    return 0


def cmd_revert(a: argparse.Namespace) -> int:
    need_root()
    state = load_state()
    targets = list(state["persisted"]) + list(state["pending"]) if a.knob == "all" else [a.knob]
    for name in dict.fromkeys(targets):
        entry = state["persisted"].get(name, {}).get("orig") or state["pending"].get(name)
        knob = KNOBS.get(name)
        if knob and entry:
            say(f"revert {name}")
            knob.restore(entry)
        elif a.knob != "all":
            warn(f"{name}: nothing recorded to revert")
        state["persisted"].pop(name, None)
        state["pending"].pop(name, None)
    save_state(state)
    regenerate_dropins(state)
    say("done. Drop-ins re-rendered from remaining state.")
    return 0


def main(argv: list[str] | None = None) -> int:
    global DRY
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dry-run", action="store_true", help="print writes, change nothing")
    sub = p.add_subparsers(dest="cmd", required=True)

    def bench_opts(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--reps", type=int, default=3)
        sp.add_argument("--runtime", type=int, default=15, help="seconds per timed job")
        sp.add_argument("--size-gb", type=float, default=8.0, help="test file size")
        sp.add_argument("--dir", default="/var/tmp/perf-tune", help="test file directory (must be on the NVMe, not tmpfs)")
        sp.add_argument("--force", action="store_true", help="bench even if the machine isn't idle")

    sub.add_parser("audit").set_defaults(fn=cmd_audit)
    sp = sub.add_parser("bench"); bench_opts(sp); sp.add_argument("--label", default="manual"); sp.set_defaults(fn=cmd_bench)
    sp = sub.add_parser("ab"); bench_opts(sp); sp.add_argument("knobs", nargs="+", help="knob names, or 'all'")
    sp.add_argument("--scx-off", action="store_true", help="run the whole A/B with sched_ext stopped (use for preempt_lazy)")
    sp.set_defaults(fn=cmd_ab)
    sub.add_parser("apply").set_defaults(fn=cmd_apply)
    sp = sub.add_parser("keep"); sp.add_argument("knob"); sp.set_defaults(fn=cmd_keep)
    sp = sub.add_parser("revert"); sp.add_argument("knob", help="knob name or 'all'"); sp.set_defaults(fn=cmd_revert)

    args = p.parse_args(argv)
    DRY = args.dry_run
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
