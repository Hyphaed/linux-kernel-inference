"""Idempotent runtime + userspace tuning — re-run anytime, safe every time.

Every item here fixes something this repo found wrong on a REAL boot, with a
real incident or measurement behind it (the `evidence` field points at the
boot audit or session that found it). This is deliberately not a place to
add speculative performance levers: mTHP defaults, nvme.poll_queues,
nvme.max_host_mem_size_mb and IRQ affinity all need real A/B evidence from
diagnostics/bench-*.sh first, per this repo's MUST-RULE ("no evidence of
benefit, no change"). Those show up in `advisories()` — reported, never
auto-applied.

Wired as `python -m hyphaed tune` (report) / `python -m hyphaed --dry-run
tune` behaves the same as report, since apply already checks the global
dry-run flag through `run()`/`run_sudo()` — see cli.py's `_cmd_tune`.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .util import log
from .util.run import run, run_sudo


@dataclass
class TuneItem:
    name: str
    evidence: str
    check: Callable[[], tuple[bool, str]]
    apply: Callable[[], None] | None = None  # None => report-only
    needs_root: bool = False


@dataclass
class Advisory:
    name: str
    detail: str
    why_not_applied: str


def _cmd(args: list[str]) -> str:
    try:
        r = subprocess.run(args, capture_output=True, text=True, check=False)
        return r.stdout
    except FileNotFoundError:
        return ""


def _read(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# 1. power-profiles-daemon -> performance
#
# 7.1.10 boot audit (2026-08-27): found in power-saver, pinning EPP to
# "power" and EPB to 15 on all 32 threads on a box used for inference and
# gaming. `powerprofilesctl set performance` took EPB to 0. Confirmed live
# again this session (2026-09-03): had regressed back to "balanced" —
# nothing in this repo made it persistent, which is exactly why this needs
# to be a re-runnable check rather than a one-time fix.
# ---------------------------------------------------------------------------

def _check_power_profile() -> tuple[bool, str]:
    if not shutil.which("powerprofilesctl"):
        return True, "power-profiles-daemon not installed — n/a"
    out = _cmd(["powerprofilesctl", "get"]).strip()
    if not out:
        return True, "powerprofilesctl present but returned nothing — n/a"
    return out == "performance", f"active profile: {out}"


def _apply_power_profile() -> None:
    run(["powerprofilesctl", "set", "performance"], check=False)


# ---------------------------------------------------------------------------
# 2. NetworkManager DNS via systemd-resolved
#
# 7.1.10 boot audit: no dns= key meant NM fell back to calling resolvconf,
# which on this box is a symlink to resolvectl that rejects NM's pseudo
# interface name — DNS silently never got applied, masked by a stale
# hand-made /etc/resolv.conf.
# ---------------------------------------------------------------------------

_NM_DNS_CONF = Path("/etc/NetworkManager/conf.d/90-dns-systemd-resolved.conf")
_NM_DNS_BODY = "[main]\ndns=systemd-resolved\n"


def _check_nm_dns_plugin() -> tuple[bool, str]:
    if not shutil.which("nmcli"):
        return True, "NetworkManager not installed — n/a"
    for f in ("/etc/NetworkManager/NetworkManager.conf", str(_NM_DNS_CONF)):
        if re.search(r"^\s*dns\s*=\s*systemd-resolved\s*$", _read(f), re.M):
            return True, f"dns=systemd-resolved set in {f}"
    return False, "no dns=systemd-resolved found — NM may fall back to resolvconf"


def _apply_nm_dns_plugin() -> None:
    run_sudo(["mkdir", "-p", str(_NM_DNS_CONF.parent)])
    run_sudo(["tee", str(_NM_DNS_CONF)], input_str=_NM_DNS_BODY, capture=True)
    run_sudo(["systemctl", "restart", "NetworkManager"], check=False)


# ---------------------------------------------------------------------------
# 3. vmware.service must not hard-require systemd-modules-load.service
#
# 7.2.2 boot audit (2026-08-31): nvidia-peermem structurally cannot load on
# this box (no Mellanox/IB stack), so systemd-modules-load.service fails
# every boot, and vmware.service's Requires= on it took VMware down too.
# A *.service.d/ override with an empty Requires= does NOT clear a
# Requires= already set in the unit's own [Unit] section on this systemd
# version (259) — confirmed with systemd-analyze verify against both the
# real unit and a synthetic throwaway pair. Only editing the line out of
# vmware.service itself works.
# ---------------------------------------------------------------------------

_VMWARE_SVC = Path("/etc/systemd/system/vmware.service")
_VMWARE_OVERRIDE = Path("/etc/systemd/system/vmware.service.d/override.conf")


def _check_vmware_requires() -> tuple[bool, str]:
    text = _read(str(_VMWARE_SVC))
    if not text:
        return True, "vmware.service not installed — n/a"
    bad = re.search(r"^Requires=.*systemd-modules-load\.service.*$", text, re.M)
    return not bad, (bad.group(0) if bad else "clean")


def _apply_vmware_requires() -> None:
    text = _read(str(_VMWARE_SVC))
    if not text:
        return
    lines = text.splitlines()
    out_lines = []
    for line in lines:
        if re.match(r"^Requires=.*systemd-modules-load\.service.*$", line):
            continue  # drop it
        out_lines.append(line)
        if re.match(r"^After=.*systemd-modules-load\.service.*$", line):
            out_lines.append("Wants=systemd-modules-load.service")
    new_text = "\n".join(out_lines) + "\n"
    run_sudo(["tee", str(_VMWARE_SVC)], input_str=new_text, capture=True)
    if _VMWARE_OVERRIDE.exists():
        run_sudo(["rm", "-f", str(_VMWARE_OVERRIDE)])
    run_sudo(["systemctl", "daemon-reload"])
    run_sudo(["systemctl", "reset-failed", "systemd-modules-load.service", "vmware.service"], check=False)


# ---------------------------------------------------------------------------
# 4. Stale nvidia-peermem boot-time loader config
#
# 7.2.2 boot audit: postinstall used to write /etc/modules-load.d/
# nvidia-peermem.conf without checking modprobe's real exit status.
# nv_mem_client_init() hard-returns -EINVAL without an IB peer-memory
# stack, so this file guarantees a failed unit every boot forever.
# ---------------------------------------------------------------------------

_NVIDIA_PEERMEM_CONF = Path("/etc/modules-load.d/nvidia-peermem.conf")


def _check_nvidia_peermem_conf() -> tuple[bool, str]:
    present = _NVIDIA_PEERMEM_CONF.exists()
    return not present, (
        f"{_NVIDIA_PEERMEM_CONF} present — will fail every boot without an IB peer-memory stack"
        if present else "absent"
    )


def _apply_nvidia_peermem_conf() -> None:
    if _NVIDIA_PEERMEM_CONF.exists():
        run_sudo(["rm", "-f", str(_NVIDIA_PEERMEM_CONF)])


# ---------------------------------------------------------------------------
# 5. nvidia-fs built stale against a driver that has since moved on
#
# Found live 2026-09-03: BUILD_DEPENDS[0]="nvidia" in nvidia-fs's dkms.conf
# orders the FIRST install, it does not re-trigger a rebuild of an
# already-installed sibling module when the depended-on driver's version
# changes. `dkms autoinstall` rebuilt nvidia.ko against 610.57.04 and left
# nvidia-fs.ko untouched, still linked against 610.43.02's symbol versions
# — with CONFIG_MODVERSIONS on, that's a hard "disagrees about version of
# symbol" load failure at the next boot, one step worse than the 0-byte
# regression documented on 2026-08-25 (that one at least degraded to POSIX
# compat; a symbol mismatch means nvidia-fs doesn't load at all).
#
# Only safe to auto-fix for the CURRENTLY RUNNING kernel: nvidia-fs's own
# build script always reads Module.symvers for `uname -r`, not the kernel
# it's being built for (see diagnostics/fix-nvidia-fs-symvers.sh) — running
# kernel == target kernel is the one case that bug can't corrupt. Any other
# registered kernel gets reported, not auto-fixed; fixing it needs an
# actual boot into that kernel first.
# ---------------------------------------------------------------------------

def _dkms_module_paths(module: str, kernel: str) -> list[Path]:
    return sorted(Path(f"/lib/modules/{kernel}/updates/dkms").glob(f"{module}.ko*"))


def _running_kernel() -> str:
    return _cmd(["uname", "-r"]).strip()


def _check_nvidia_fs_currency() -> tuple[bool, str]:
    kernel = _running_kernel()
    if not kernel:
        return True, "could not determine running kernel — n/a"
    nv = _dkms_module_paths("nvidia", kernel)
    nvfs = _dkms_module_paths("nvidia-fs", kernel)
    if not nv or not nvfs:
        return True, f"nvidia or nvidia-fs not DKMS-built for {kernel} — n/a"
    nv_mtime = max(p.stat().st_mtime for p in nv)
    nvfs_mtime = max(p.stat().st_mtime for p in nvfs)
    if nvfs_mtime >= nv_mtime:
        return True, f"nvidia-fs.ko is not older than nvidia.ko for {kernel}"
    return False, (
        f"nvidia-fs.ko for {kernel} predates the current nvidia.ko build — "
        "likely stale symbol versions, will probably fail to load at next boot"
    )


def _apply_nvidia_fs_currency() -> None:
    kernel = _running_kernel()
    dkms_ver = None
    m = re.search(r"nvidia-fs/(\S+),", _cmd(["dkms", "status", "nvidia-fs"]))
    if m:
        dkms_ver = m.group(1)
    if not kernel or not dkms_ver:
        log.warn("tune: couldn't determine nvidia-fs dkms version/kernel — skipping rebuild")
        return
    spec = f"nvidia-fs/{dkms_ver}"
    run_sudo(["dkms", "build", spec, "-k", kernel, "--force"])
    run_sudo(["dkms", "install", spec, "-k", kernel, "--force"])


ITEMS: list[TuneItem] = [
    TuneItem(
        "power-profiles-daemon: performance",
        "boot audit 2026-08-27, EPP power->0 / EPB 15->0; regressed live 2026-09-03",
        _check_power_profile, _apply_power_profile,
    ),
    TuneItem(
        "NetworkManager: dns=systemd-resolved",
        "boot audit 2026-08-27, DNS silently never applied via resolvconf",
        _check_nm_dns_plugin, _apply_nm_dns_plugin, needs_root=True,
    ),
    TuneItem(
        "vmware.service: no hard Requires= on modules-load",
        "boot audit 2026-08-31, vmware.service failing every boot via a doomed dependency",
        _check_vmware_requires, _apply_vmware_requires, needs_root=True,
    ),
    TuneItem(
        "no stale nvidia-peermem boot-load config",
        "boot audit 2026-08-31, guaranteed-fail module load on every boot",
        _check_nvidia_peermem_conf, _apply_nvidia_peermem_conf, needs_root=True,
    ),
    TuneItem(
        "nvidia-fs current with the running kernel's nvidia build",
        "live incident 2026-09-03, nvidia driver bump left nvidia-fs stale (also 2026-08-25, 0-byte variant)",
        _check_nvidia_fs_currency, _apply_nvidia_fs_currency, needs_root=True,
    ),
]


# ---------------------------------------------------------------------------
# Advisory-only: real levers, no measured evidence on THIS box yet. Reported
# for visibility, never applied by `tune`. Each needs diagnostics/bench-*.sh
# before it earns a TuneItem — see docs/tasks_patches.md T19/T20 and
# docs/research/platform-kernel-levers-2026-08-31.md for the reasoning.
# ---------------------------------------------------------------------------

def advisories() -> list[Advisory]:
    out = []
    thp = _read("/sys/kernel/mm/transparent_hugepage/enabled")
    if thp:
        out.append(Advisory(
            "Transparent Huge Pages", f"current: {thp}",
            "changing this without diagnostics/bench-mm.sh A/B evidence repeats patch 0016's mistake (an unmeasured default change)",
        ))
    poll = _read("/sys/module/nvme_core/parameters/poll_queues") or "0"
    out.append(Advisory(
        "nvme.poll_queues", f"current: {poll}",
        "1912.06998's CPU-cost finding argues this is a likely loss on this box; unmeasured here — needs diagnostics/bench-nvme.sh",
    ))
    zswap_on = _read("/sys/module/zswap/parameters/enabled").strip().upper() in ("Y", "1")
    zram_swap = "zram" in _cmd(["swapon", "--show=NAME", "--noheadings"])
    if zswap_on and zram_swap:
        out.append(Advisory(
            "zswap (zstd) stacked on top of a zram (zstd) swap device",
            f"zswap enabled: {zswap_on}; a zram device is an active swap target",
            "a swapped-out page gets compressed twice (zswap's own zstd pass, "
            "then zram's zstd pass on whatever zswap evicts to it) — cost "
            "unmeasured; needs diagnostics/bench-mm.sh A/B with zswap on vs "
            "off while zram-only, per 7.2.5 bump plan Phase 5 Q1. "
            "configs/fragments/31-mm-zswap-zstd.config is what turns zswap on.",
        ))
    thermald_active = _cmd(["systemctl", "is-active", "thermald"]).strip()
    if thermald_active and thermald_active != "active":
        out.append(Advisory(
            "thermald disabled", f"systemctl is-active thermald: {thermald_active}",
            "a 14900KF in a mini-ITX board (ASRock B760M-ITX/D4) is more VRM/"
            "thermal-constrained than a full-ATX board; whether thermald's "
            "DPTF-driven throttling helps or just adds latency under a "
            "sustained load is unmeasured — needs turbostat --show "
            "PkgWatt,CoreTmp,Busy% A/B per 7.2.5 bump plan Phase 5 Q5",
        ))
    numa_warn = "error retrieving numa node" in _cmd(
        ["journalctl", "-k", "-b", "0", "--no-pager", "-g", "numa node"]
    ).lower()
    if numa_warn:
        out.append(Advisory(
            "PCI devices report '[Firmware Bug]: Overriding NUMA node to 0'",
            "nvidia-fs and other PCI drivers warn 'error retrieving numa node' "
            "this boot — BIOS/ACPI doesn't associate the root bus with a node "
            "on this single-NUMA-node box",
            "a drafted-but-unsent fix exists at "
            "upstream-candidates/x86-pci-single-node-root-bus/ (defaults a "
            "root bus to the only online node when firmware won't say) — "
            "compile-tested, not authored/numbered/sent; connect the two "
            "rather than re-deriving this",
        ))
    return out


def run_report() -> list[tuple[TuneItem, bool, str]]:
    return [(item, *item.check()) for item in ITEMS]
