"""Post-reboot health check.

Runs a battery of cheap assertions to confirm the freshly-booted hyphaed
kernel behaves the way the wizard intended. Useful as the very first
command after a reboot.
"""
from __future__ import annotations
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .util import log


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


def _read(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def _cmd(args: list[str]) -> str:
    try:
        r = subprocess.run(args, capture_output=True, text=True, check=False)
        return r.stdout
    except FileNotFoundError:
        return ""


def check_running_kernel(flavour: str) -> Check:
    uname = _cmd(["uname", "-r"]).strip()
    ok = uname.endswith(f"-{flavour}")
    return Check(
        "running kernel is hyphaed-built",
        ok,
        f"uname -r = {uname or '?'}",
    )


def check_mitigations() -> Check:
    base = Path("/sys/devices/system/cpu/vulnerabilities")
    if not base.exists():
        return Check("CPU mitigations", False, "vulnerabilities sysfs missing")
    vuln: list[str] = []
    for f in sorted(base.iterdir()):
        text = f.read_text().strip()
        if text.startswith("Vulnerable"):
            vuln.append(f"{f.name}={text}")
    return Check(
        "no Vulnerable CPU vulnerabilities",
        not vuln,
        f"{len(vuln)} vulnerable" + ((": " + ", ".join(vuln[:3])) if vuln else ""),
    )


def check_bore_active() -> Check:
    feats = _read("/sys/kernel/debug/sched/features")
    if not feats:
        return Check("BORE scheduler active", False, "can't read /sys/kernel/debug/sched/features (run as root)")
    has_bore = "BORE" in feats
    return Check("BORE scheduler active", has_bore, "feature flag present" if has_bore else "BORE not found in sched features")


def check_sched_ext() -> Check:
    path = Path("/sys/kernel/sched_ext")
    ok = path.exists()
    return Check(
        "sched_ext available",
        ok,
        "scx_* userspace schedulers can run" if ok else "CONFIG_SCHED_CLASS_EXT not present",
    )


def check_iommu() -> Check:
    classes = Path("/sys/class/iommu")
    enabled = classes.exists() and any(classes.iterdir())
    return Check("IOMMU enabled", enabled, "needed for VFIO + greenboost DMA-BUF")


def check_thp_madvise() -> Check:
    raw = _read("/sys/kernel/mm/transparent_hugepage/enabled")
    # kernel reports "[madvise] always never" or similar
    m = re.search(r"\[(\w+)\]", raw)
    active = m.group(1) if m else ""
    ok = active in {"madvise", "always"}
    return Check("THP enabled", ok, f"active mode = {active or '?'}")


def check_cmdline_matches(extra_expected: list[str]) -> Check:
    cmdline = _read("/proc/cmdline").split()
    missing = [tok for tok in extra_expected if tok not in cmdline]
    return Check(
        "expected cmdline tokens present",
        not missing,
        ("missing: " + " ".join(missing)) if missing else f"{len(extra_expected)} tokens matched",
    )


def check_nvidia_loaded() -> Check:
    lsmod = _cmd(["lsmod"])
    loaded = any(line.startswith(("nvidia ", "nvidia_drm", "nvidia_uvm")) for line in lsmod.splitlines())
    return Check("nvidia kernel module loaded", loaded)


def check_greenboost_loaded() -> Check:
    lsmod = _cmd(["lsmod"])
    loaded = any(line.startswith("greenboost ") for line in lsmod.splitlines())
    return Check("greenboost module loaded", loaded, "load with: sudo modprobe greenboost")


def check_dkms_status(flavour: str) -> Check:
    if not shutil.which("dkms"):
        return Check("DKMS modules built", True, "dkms not installed; skipped")
    uname = _cmd(["uname", "-r"]).strip()
    out = _cmd(["dkms", "status", "-k", uname])
    if not out.strip():
        return Check("DKMS modules built", True, f"no DKMS modules registered for {uname}")
    bad = [ln for ln in out.splitlines() if "installed" not in ln.lower() and ln.strip()]
    return Check(
        "DKMS modules all installed",
        not bad,
        ("issues: " + "; ".join(bad)) if bad else f"all clean ({len(out.splitlines())} modules)",
    )


def check_vmware_loaded() -> Check:
    if not Path("/usr/lib/vmware").exists():
        return Check("vmware host modules", True, "vmware not installed; skipped")
    lsmod = _cmd(["lsmod"])
    loaded = any(line.startswith(("vmmon", "vmnet")) for line in lsmod.splitlines())
    return Check("vmmon/vmnet loaded", loaded, "run: sudo vmware-modconfig --console --install-all" if not loaded else "")


def check_zswap_active() -> Check:
    enabled = _read("/sys/module/zswap/parameters/enabled")
    if not enabled:
        return Check("zswap enabled", False, "/sys/module/zswap not available (zswap not compiled in?)")
    ok = enabled.upper() == "Y"
    detail = ""
    if ok:
        pool = _read("/sys/kernel/mm/zswap/pool_total_size")
        written = _read("/sys/kernel/mm/zswap/written_back_pages")
        detail = f"pool={pool} written_back={written}"
    else:
        detail = f"enabled={enabled}"
    return Check("zswap active", ok, detail)


def check_cpu_governor() -> Check:
    gov_paths = list(Path("/sys/devices/system/cpu").glob("cpu0/cpufreq/scaling_governor"))
    if not gov_paths:
        return Check("CPU frequency governor", True, "cpufreq sysfs absent; likely using acpi-cpufreq HW control")
    gov = _read(str(gov_paths[0]))
    ok = gov in {"schedutil", "performance", "ondemand"}
    return Check("CPU governor suitable", ok, f"governor={gov}" + ("" if ok else "; prefer schedutil/performance"))


def check_hugepages_available() -> Check:
    nr_free = _read("/sys/kernel/mm/hugepages/hugepages-2048kB/free_hugepages")
    nr_total = _read("/sys/kernel/mm/hugepages/hugepages-2048kB/nr_hugepages")
    thp_enabled = _read("/sys/kernel/mm/transparent_hugepage/enabled")
    thp_ok = "[madvise]" in thp_enabled or "[always]" in thp_enabled
    # Either static hugepages allocated OR THP enabled is fine
    ok = thp_ok or (nr_total.isdigit() and int(nr_total) > 0)
    detail = f"static 2MiB: {nr_total or '0'} pages, THP={thp_enabled.replace('[', '').replace(']', '')[:16]}"
    return Check("hugepages available", ok, detail)


def check_scx_state() -> Check:
    state_path = Path("/sys/kernel/sched_ext/state")
    if not state_path.exists():
        return Check("sched_ext state", False, "CONFIG_SCHED_CLASS_EXT not built or sysfs absent")
    state = _read(str(state_path))
    # states: "disabled", "enabling", "enabled", "disabling"
    ok = state in {"enabled", "disabled"}  # both are valid; "enabled" means a scx scheduler is active
    detail = f"state={state}"
    if state == "enabled":
        nr_reject = _read("/sys/kernel/sched_ext/nr_rejected")
        detail += f" nr_rejected={nr_reject or '0'}"
    return Check("sched_ext kernel support", ok, detail)


def run_all(flavour: str, cmdline_expected: list[str] | None = None) -> list[Check]:
    cmdline_expected = cmdline_expected or [
        "iommu=pt",
        "intel_iommu=on,igfx_off",
        "nvidia-drm.modeset=1",
    ]
    return [
        check_running_kernel(flavour),
        check_mitigations(),
        check_bore_active(),
        check_sched_ext(),
        check_scx_state(),
        check_iommu(),
        check_thp_madvise(),
        check_hugepages_available(),
        check_zswap_active(),
        check_cpu_governor(),
        check_cmdline_matches(cmdline_expected),
        check_nvidia_loaded(),
        check_greenboost_loaded(),
        check_dkms_status(flavour),
        check_vmware_loaded(),
    ]


def render(checks: list[Check]) -> int:
    from rich.table import Table
    t = Table(header_style="bold magenta")
    t.add_column("check", style="cyan")
    t.add_column("status")
    t.add_column("detail", style="grey70")
    for c in checks:
        t.add_row(c.name, "[ok]✓[/ok]" if c.ok else "[err]✗[/err]", c.detail)
    log.console.print(t)
    failed = [c for c in checks if not c.ok]
    return 0 if not failed else 1
