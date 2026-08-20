from __future__ import annotations
import re
import shutil
import sys
from pathlib import Path

from ..util import log
from ..util.run import run
from ..topology import HardwareProfile, detect as topology_detect

NAME = "detect"
MIN_BOOT_FREE_MB = 1200
REQUIRED_TOOLS = [
    "gcc", "make", "dpkg-buildpackage", "pahole", "bison", "flex", "git",
]
BUILD_DEPS_PKGS = [
    # Core kernel build toolchain
    "build-essential", "dpkg-dev", "debhelper", "fakeroot",
    "libncurses-dev", "libssl-dev", "libelf-dev", "libdw-dev",
    "bison", "flex", "dwarves", "rsync", "kmod", "cpio", "bc",
    "zstd", "python3", "git", "pahole",
    # Kernel tools package (perf, bpftool, cpupower, turbostat, …)
    "libslang2-dev", "libcap-dev", "libbpf-dev", "libpci-dev", "binutils-dev",
]

# What each package does — shown when a package is missing so the user
# understands why it's needed before agreeing to install it.
_PKG_DESCRIPTIONS: dict[str, str] = {
    "build-essential":  "gcc, g++, make, libc-dev — the base C build toolchain",
    "dpkg-dev":         "dpkg-source, dpkg-buildpackage — unpacks and repackages the kernel source .dsc",
    "debhelper":        "dh_* helpers — drives the debian/rules binary target",
    "fakeroot":         "simulates root ownership during dpkg-deb --build without actually being root",
    "libncurses-dev":   "required by make menuconfig / nconfig TUI",
    "libssl-dev":       "openssl headers — kernel module signing (Secure Boot MOK flow)",
    "libelf-dev":       "libelf.h — btf/dwarf consumers: pahole, bpftool, perf",
    "libdw-dev":        "DWARF unwinding library — perf call-graph unwinding",
    "bison":            "parser generator — kernel's Kconfig and flex/bison grammars",
    "flex":             "lexer generator — Kconfig scanner",
    "dwarves":          "provides pahole — computes BTF (BPF Type Format) from DWARF, needed for CONFIG_DEBUG_INFO_BTF",
    "rsync":            "used by headers_install to stage linux-headers .deb",
    "kmod":             "modprobe/depmod — DKMS and modules_install call depmod after install",
    "cpio":             "initramfs packing — needed by make bindeb-pkg for the image .deb",
    "bc":               "arbitrary precision calculator — kernel version arithmetic in build scripts",
    "zstd":             "fast compression — kernel .deb uses zstd for modules (CONFIG_MODULE_COMPRESS_ZSTD)",
    "python3":          "required by several kernel scripts (Kconfig helpers, generate_rust_target.py)",
    "git":              "git am --3way patch application (patch phase)",
    "pahole":           "generates BTF from DWARF; needed when CONFIG_DEBUG_INFO_BTF=y (required for BPF/sched_ext)",
    "libslang2-dev":    "S-Lang TUI library — perf's interactive browser (perf top, perf report --tui)",
    "libcap-dev":       "POSIX capabilities headers — perf and turbostat read CAP_SYS_RAWIO/CAP_PERFMON",
    "libbpf-dev":       "libbpf headers+library — bpftool links against this for BTF-aware BPF loading",
    "libpci-dev":       "PCI enumeration library — cpupower uses it for MSR/ACPI platform detection",
    "binutils-dev":     "libbfd/libopcodes — perf disassembler and objdump-based annotation",
}


def _missing_packages(pkgs: list[str]) -> list[str]:
    """Return the subset of pkgs that dpkg reports as not fully installed."""
    r = run(
        ["dpkg-query", "-W", "--showformat=${Package} ${db:Status-Status}\\n", *pkgs],
        check=False, force=True,
    )
    installed = {
        parts[0]
        for line in r.stdout.splitlines()
        if len(parts := line.split()) >= 2 and parts[1] == "installed"
    }
    return [p for p in pkgs if p not in installed]


def _ensure_build_deps(ctx) -> None:
    """Check all build-time packages and offer to install missing ones via nala.

    Called at the very start of the detect phase so the user sees this before
    anything else. Skipped in dry-run mode.
    """
    if ctx.dry_run:
        return

    missing = _missing_packages(BUILD_DEPS_PKGS)
    if not missing:
        return

    log.warn(f"missing {len(missing)} build dep(s):")
    for pkg in missing:
        desc = _PKG_DESCRIPTIONS.get(pkg, "")
        suffix = f"  [dim]— {desc}[/dim]" if desc else ""
        log.console.print(f"  [yellow]·[/yellow] [bold]{pkg}[/bold]{suffix}")

    from ..util.prompts import confirm
    if not confirm(
        f"install {len(missing)} missing package(s) via nala now?", default=True
    ):
        log.warn("continuing without them — the build will likely fail")
        return

    from ..util.run import run_sudo
    if not shutil.which("nala"):
        log.info("nala not found — bootstrapping via apt first")
        run_sudo(["apt", "install", "-y", "nala"])

    run_sudo(["nala", "install", "-y", *missing])
    log.ok(f"installed {len(missing)} package(s)")


# Minimum NVIDIA proprietary driver versions per kernel major.
# The proprietary driver chases mainline; older drivers won't build/load
# against a kernel that's much newer than they were cut for.
_NVIDIA_KERNEL_FLOOR = {
    "6.6": 535, "6.7": 535, "6.8": 545, "6.9": 550,
    "6.10": 550, "6.11": 555, "6.12": 560,
    "7.0": 555, "7.1": 565, "7.2": 570,
}


def _check_nvidia_driver(profile) -> None:
    if profile.gpu_vendor != "nvidia":
        return
    from ..util.run import run
    r = run(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"], check=False, force=True)
    ver = r.stdout.strip().splitlines()[0] if r.ok() and r.stdout.strip() else None
    if not ver:
        log.warn("nvidia-smi failed — can't check driver version compatibility")
        return
    try:
        major = int(ver.split(".")[0])
    except (ValueError, IndexError):
        return
    # Pick a floor based on the *running* kernel's major.minor
    parts = (profile.running_kernel or "").split(".")
    if len(parts) >= 2:
        key = f"{parts[0]}.{parts[1].split('-')[0]}"
        floor = _NVIDIA_KERNEL_FLOOR.get(key)
        if floor and major < floor:
            log.warn(
                f"NVIDIA driver {ver} is below the recommended floor ({floor}+) "
                f"for kernel {key}. The build will succeed but the proprietary "
                f"module may fail to load. Consider upgrading nvidia-dkms first, "
                f"or switching to nvidia-open-dkms (more forward-compatible)."
            )
        else:
            log.info(f"NVIDIA driver {ver} (floor for kernel {key}: {floor or '?'}+) — OK")


def run_phase(ctx) -> HardwareProfile:
    log.banner("Phase 1/9 — Detect hardware & environment")
    _ensure_build_deps(ctx)
    profile = topology_detect()

    log.info(f"Machine: {profile.hostname} ({profile.chassis}) id={profile.machine_id}")
    log.info(f"CPU:     {profile.cpu_brand}")
    log.info(f"         vendor={profile.cpu_vendor} codename={profile.cpu_codename} fam={profile.cpu_family} mod={profile.cpu_model}")
    log.info(f"         P-cores={profile.p_cores} ({profile.p_thread_mask})  E-cores={profile.e_cores} ({profile.e_thread_mask})  SMT={profile.smt}  threads={profile.total_threads}")
    log.info(f"RAM:     {profile.ram_gb} GB on {profile.numa_nodes} NUMA node(s)")
    log.info(f"GPU:     {profile.gpu_vendor} {profile.gpu_model} ({profile.gpu_arch}) {profile.gpu_vram_gb} GB VRAM")
    if profile.has_amd_gpu:
        log.info(f"         + AMD iGPU present")
    log.info(f"IOMMU:   present={profile.iommu_present}")
    log.info(f"Session: {profile.session_type} / {profile.desktop}")
    log.info(f"OS:      Ubuntu {profile.ubuntu_release}  kernel={profile.running_kernel}")
    log.info(f"/boot:   {profile.boot_free_mb} MB free")
    log.info(f"SB:      {'enabled' if profile.secure_boot else 'disabled'}")
    log.info(f"VMware:  {'installed' if profile.has_vmware else 'not installed'}")
    log.info(f"Greenboost: {'present' if profile.has_greenboost else 'not detected'}")
    log.info(f"Tag:     {profile.tag()}")

    # Hard checks
    if profile.boot_free_mb < MIN_BOOT_FREE_MB:
        log.err(f"/boot has only {profile.boot_free_mb} MB free (need at least {MIN_BOOT_FREE_MB} MB)")
        log.err("run `sudo apt autoremove --purge` to drop old kernels, or grow /boot")
        raise SystemExit(2)

    _check_nvidia_driver(profile)
    _check_deb_src()

    # If greenboost is installed and has a profile, cross-check it.
    if profile.has_greenboost:
        try:
            from .. import greenboost as gb_mod
            gb_mod.maybe_log_summary(profile)
        except Exception as e:
            log.warn(f"greenboost cross-check failed (non-fatal): {e}")

    # Save profile
    profile_path = ctx.state_dir / f"profile-{profile.running_kernel or 'current'}.json"
    profile.save(profile_path)
    log.ok(f"profile saved -> {profile_path}")

    ctx.profile = profile

    # Resolve 'auto' preset now that we have the hardware profile
    if getattr(ctx, "preset", None) in (None, "auto"):
        from ..cli import resolve_preset
        ctx.preset = resolve_preset("auto", profile)
        log.info(f"preset: {ctx.preset}")

    return profile


def _check_deb_src() -> None:
    """apt source requires a deb-src entry to be enabled somewhere."""
    from ..util.run import run
    r = run(["apt-cache", "policy"], check=False, force=True)
    if "deb-src" in r.stdout.lower() or "/source/" in r.stdout.lower():
        return
    # Modern Ubuntu uses deb822 format under /etc/apt/sources.list.d/
    for f in list(Path("/etc/apt").glob("sources.list*")) + list(Path("/etc/apt/sources.list.d").glob("*.sources")) + list(Path("/etc/apt/sources.list.d").glob("*.list")):
        try:
            text = f.read_text()
        except OSError:
            continue
        if "deb-src" in text or re.search(r"^Types:.*\bdeb-src\b", text, re.MULTILINE):
            return
    log.warn("deb-src sources don't appear enabled — `apt source` will fail in phase 2")
    log.info("enable with: sudo sed -i '/^# *deb-src /s/^# *//' /etc/apt/sources.list && sudo apt update")
