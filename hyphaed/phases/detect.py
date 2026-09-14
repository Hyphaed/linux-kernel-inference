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
    """Return the subset of pkgs that the kernel build's own dependency gate
    would reject.

    `dpkg-query -W --showformat=${db:Status-Status}` reports "installed" for
    a package that is unpacked-but-not-yet-configured, because dpkg-query
    replays the pending transaction journal (/var/lib/dpkg/updates/) before
    reading. `dpkg-checkbuilddeps` — the exact check `dpkg-buildpackage`
    (and therefore `make bindeb-pkg`) performs — reads
    /var/lib/dpkg/status directly and does NOT replay the journal, so it
    correctly rejects that same package. Found 2026-09-14: detect reported
    every build dep present, the build failed 10 minutes later on
    `libssl-dev`, which `dpkg -l` showed as `ii` the whole time — its status
    stanza was `install ok unpacked` with a stale Config-Version, left behind
    by an interrupted `nala upgrade`. Using dpkg-checkbuilddeps here makes
    detect agree with the tool that actually gates the build.
    """
    if shutil.which("dpkg-checkbuilddeps"):
        depends = ", ".join(pkgs)
        r = run(
            ["dpkg-checkbuilddeps", "-d", depends, "/dev/null"],
            check=False, force=True,
        )
        if r.ok():
            return []
        # stderr looks like:
        #   dpkg-checkbuilddeps: error: unmet build dependencies: libssl-dev:native libssl-dev
        m = re.search(r"unmet build dependencies:\s*(.+)", r.stderr)
        if not m:
            # Unexpected output shape — fall through to the dpkg-query path
            # rather than silently reporting nothing missing.
            pass
        else:
            unmet = {tok.split(":", 1)[0] for tok in m.group(1).split()}
            return [p for p in pkgs if p in unmet]

    # Fallback for systems without dpkg-dev (dpkg-checkbuilddeps ships in
    # it) — same known blind spot as before: an interrupted transaction can
    # still read as "installed" here.
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


def _dpkg_pending_count() -> int:
    """Count packages stuck mid-transaction: unreplayed journal entries in
    /var/lib/dpkg/updates/, or status stanzas not settled at
    "install ok installed" / "deinstall ok config-files" / "purge ok
    not-installed". Mirrors scripts/fix-dpkg-state.sh's detection so detect
    and the standalone repair script never disagree.
    """
    updates_dir = Path("/var/lib/dpkg/updates")
    journal = 0
    if updates_dir.is_dir():
        journal = sum(1 for p in updates_dir.iterdir() if p.is_file())

    status_path = Path("/var/lib/dpkg/status")
    pending = 0
    settled = ("install ok installed", "deinstall ok config-files", "purge ok not-installed")
    try:
        for line in status_path.read_text(errors="replace").splitlines():
            if line.startswith("Status: ") and not line[len("Status: "):].strip().startswith(settled):
                pending += 1
    except OSError:
        pass

    return journal + pending


def _check_dpkg_state(ctx) -> None:
    """Detect an interrupted dpkg transaction before scanning for missing
    packages — see `_missing_packages()` for why this matters: while dpkg is
    in this state, `nala install` of the "missing" packages below will
    itself fail (`Error: dpkg was interrupted`), so _ensure_build_deps()'s
    auto-install can't self-heal without this running first.
    """
    if ctx.dry_run:
        return
    if _dpkg_pending_count() == 0:
        return

    log.warn(
        "dpkg has an interrupted transaction: packages are unpacked but not "
        "configured. `dpkg-query`/`dpkg -l` report them installed anyway "
        "(they replay the pending journal); the kernel build's own "
        "dependency gate does not, and will fail on one of them."
    )

    from ..util.prompts import confirm
    script = Path(__file__).resolve().parents[2] / "scripts" / "fix-dpkg-state.sh"
    if not confirm(f"run {script.name} now to repair (requires sudo)?", default=True):
        log.warn("continuing with dpkg in an interrupted state — "
                  "`nala install`/the build will likely fail")
        return

    from ..util.run import run_sudo, sudo_keepalive
    # Prime the sudo credential with an uncaptured prompt BEFORE the captured
    # run_sudo() call below. run_sudo() -> run() defaults to capture=True,
    # which redirects stdout/stderr to a pipe; a password prompt inside a
    # captured subprocess is invisible on the terminal but still blocks on
    # stdin, which reads as the wizard silently hanging (the same failure
    # mode documented in install_wizard.sh's run_phase()).
    sudo_keepalive()
    r = run_sudo(["bash", str(script)], check=False)
    if not r.ok():
        log.err("dpkg repair failed — resolve manually before continuing")
        raise SystemExit(2)
    log.ok("dpkg state repaired")


def _ensure_build_deps(ctx) -> None:
    """Check all build-time packages and offer to install missing ones via nala.

    Called at the very start of the detect phase so the user sees this before
    anything else. Skipped in dry-run mode.
    """
    if ctx.dry_run:
        return

    _check_dpkg_state(ctx)

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
