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
    """0001 — BORE's own sysctl first, debugfs only as a fallback.

    This used to read /sys/kernel/debug/sched/features exclusively, which
    needs root: an unprivileged `hyphaed verify` reported "BORE scheduler
    active: FAIL — can\'t read ... (run as root)" on a machine where BORE was
    demonstrably on. A check that fails because it could not look is a false
    negative, and it sat next to twenty genuine results telling the operator
    the scheduler patch had not applied.

    kernel.sched_bore is world-readable and only exists in a BORE kernel, so
    it answers both questions (patch present, and switched on) without
    privilege.
    """
    val = _read("/proc/sys/kernel/sched_bore")
    if val:
        on = val.strip() == "1"
        return Check("BORE scheduler active", on,
                     "kernel.sched_bore=1" if on
                     else f"compiled in but disabled (sched_bore={val})")

    feats = _read("/sys/kernel/debug/sched/features")
    if not feats:
        return Check("BORE scheduler active", False,
                     "no kernel.sched_bore sysctl and debugfs unreadable — "
                     "BORE is probably not in this kernel")
    has_bore = "BORE" in feats
    return Check("BORE scheduler active", has_bore,
                 "feature flag present" if has_bore
                 else "BORE not found in sched features")


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


# ---------------------------------------------------------------------------
# Patch-series checks.
#
# Everything above verifies the ENVIRONMENT — the right kernel booted, the
# modules loaded, the cmdline composed. None of it verifies that the twenty
# patches in patches/kernel-org-7.1/series actually do anything, which is how
# the series got carried across four kernel bumps on `git am` exit status
# alone. These are the cheap unprivileged half; the full battery lives in
# tests/kernel_runtime/.
# ---------------------------------------------------------------------------

def _sysctl_setters(key: str, dirs: list[str] | None = None) -> list[str]:
    """Names of the sysctl.d files that assign `key`.

    Takes the directories as an argument so a test can point it somewhere
    real instead of monkeypatching Path out from under the module, which is
    how the first version of this got a silently-empty result.
    """
    dirs = dirs if dirs is not None else ["/usr/lib/sysctl.d", "/etc/sysctl.d"]
    pat = re.compile(rf"^\s*{re.escape(key)}\s*=", re.M)
    found = []
    for d in dirs:
        for f in sorted(Path(d).glob("*.conf")) if Path(d).is_dir() else []:
            if pat.search(_read(str(f))):
                found.append(f.name)
    return found


def check_vfs_cache_pressure() -> Check:
    """0003 — the one xanmod sysctl default nothing in userspace overrides,
    so its live value is real evidence the patched kernel is running."""
    val = _read("/proc/sys/vm/vfs_cache_pressure")
    return Check("0003 vfs_cache_pressure=50", val == "50",
                 val or "unreadable")


def check_max_map_count_masked(sysctl_dirs: list[str] | None = None) -> Check:
    """0004 raises the compiled-in default to 2147483642 and it never takes
    effect: systemd ships /usr/lib/sysctl.d/50-default.conf and
    55-map-count.conf setting 1048576, and userspace runs last.

    Reported as a real state rather than a failure — the patch applies, it
    just cannot reach the running system. Flagged so nobody cites
    vm.max_map_count as proof the series is live.
    """
    val = _read("/proc/sys/vm/max_map_count")
    setters = _sysctl_setters("vm.max_map_count", sysctl_dirs)
    if not setters:
        return Check("0004 max_map_count", val == "2147483642",
                     f"{val} (no distro override — patch is live)")
    return Check("0004 max_map_count MASKED", True,
                 f"{val}, overridden by {', '.join(setters)}")


def check_rq_affinity() -> Check:
    """0009 adds QUEUE_FLAG_SAME_FORCE to the mq default, which reads back
    as rq_affinity=2. Stock is 1. Unambiguous."""
    vals = {p.parent.parent.name: _read(str(p))
            for p in Path("/sys/block").glob("nvme*/queue/rq_affinity")}
    if not vals:
        return Check("0009 rq_affinity=2", False, "no nvme queues")
    bad = {k: v for k, v in vals.items() if v != "2"}
    return Check("0009 rq_affinity=2", not bad,
                 "all nvme" if not bad else f"wrong on {bad}")


def check_nvme_apst_latency() -> Check:
    """0013 lowers the APST entry threshold so the drive stops parking in
    deep power states between inference reads."""
    val = _read("/sys/module/nvme_core/parameters/default_ps_max_latency_us")
    return Check("0013 nvme APST 25000us", val == "25000", val or "unreadable")


def check_thp_defrag_default() -> Check:
    """0014 — no Kconfig symbol and no boot parameter can set this
    (defrag_store() is the only writer), so the bracketed value IS the
    compiled-in default unless something wrote the sysfs file after boot."""
    raw = _read("/sys/kernel/mm/transparent_hugepage/defrag")
    m = re.search(r"\[([\w+]+)\]", raw)
    sel = m.group(1) if m else ""
    return Check("0014 THP defrag=defer+madvise", sel == "defer+madvise",
                 sel or raw or "unreadable")


def check_dmabuf_hint_uapi() -> Check:
    """0019/0020 ship UAPI. If the definitions did not reach
    /usr/include/linux/dma-buf.h, nothing in userspace can use them however
    correct the kernel side is."""
    text = _read("/usr/include/linux/dma-buf.h")
    want = ("DMA_BUF_IOCTL_SET_PRIORITY", "DMA_BUF_IOCTL_GET_PRIORITY",
            "DMA_BUF_IOCTL_SET_COMPRESSION", "DMA_BUF_IOCTL_GET_COMPRESSION")
    missing = [w for w in want if w not in text]
    return Check("0019/0020 dma-buf UAPI installed", not missing,
                 "all 4 ioctls" if not missing else f"missing {missing}")


def check_cache_ext_registered() -> Check:
    """0023 — its /proc control file plus the struct_ops shadow type in BTF.

    The BTF half matters: the 6.6 X-macro registration is gone in 7.1 and was
    replaced by an explicit register_bpf_struct_ops() call added by hand
    during the forward-port. Without it the type would silently never
    register and no policy could ever attach.
    """
    if not Path("/proc/page_cache_ext_enabled_cgroup").exists():
        return Check("0023 cache_ext registered", False, "/proc file missing")
    btf = _cmd(["bpftool", "btf", "dump", "file", "/sys/kernel/btf/vmlinux"])
    if not btf:
        return Check("0023 cache_ext registered", True,
                     "/proc file present (bpftool unavailable, BTF unchecked)")
    ok = "bpf_struct_ops_page_cache_ext_ops" in btf
    return Check("0023 cache_ext registered", ok,
                 "struct_ops type in BTF" if ok
                 else "compiled in but struct_ops type NOT registered")


def check_nvidia_fs_loaded() -> Check:
    """GDS silently degrades to POSIX compat mode when nvidia_fs is absent —
    every NVMe<->GPU byte crosses PCIe twice. postinstall has caught this
    since 7.1.8, but only when the install phase actually runs; a manual
    `dpkg -i` skips it entirely.
    """
    gds = shutil.which("gdscheck") is not None or \
        Path("/usr/local/cuda/lib64/libcufile.so").exists()
    if not gds:
        return Check("nvidia_fs loaded", True, "GDS not installed — n/a")
    loaded = "nvidia_fs" in _cmd(["lsmod"])
    return Check("nvidia_fs loaded", loaded,
                 "GDS peer-to-peer active" if loaded
                 else "NOT loaded — GDS is in POSIX compat mode")


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
        check_nvidia_fs_loaded(),
        # patch series
        check_vfs_cache_pressure(),
        check_max_map_count_masked(),
        check_rq_affinity(),
        check_nvme_apst_latency(),
        check_thp_defrag_default(),
        check_dmabuf_hint_uapi(),
        check_cache_ext_registered(),
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
