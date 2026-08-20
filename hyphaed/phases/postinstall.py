from __future__ import annotations
import shutil
import subprocess
from pathlib import Path

from ..util import log
from ..util.run import run_sudo, run
from ..version import hyphaed_uname_r

NAME = "postinstall"


def _hyphaed_kernel_ver(ctx) -> str:
    running_kernel = ctx.profile.running_kernel or "7.0.0-15-generic"
    return hyphaed_uname_r(running_kernel, ctx.profile.flavour, getattr(ctx, "source_mode", "ubuntu"))


def _update_firmware(ctx) -> None:
    """Refresh in-kernel firmware blobs + CPU microcode (safe, apt-managed,
    reloaded at next boot — no hardware flashing). Then check fwupd/LVFS for
    real hardware firmware (BIOS, SSD, ...) but only report what's available;
    actually flashing hardware firmware is left to the user to trigger and
    watch by hand, since an interrupted flash can brick the board.
    """
    log.info("checking for firmware updates...")

    run_sudo(["apt-get", "update"], check=False)
    cpu_vendor = getattr(ctx.profile, "cpu_vendor", "unknown")
    microcode_pkg = "amd64-microcode" if cpu_vendor == "amd" else "intel-microcode"
    run_sudo(
        ["apt-get", "install", "--only-upgrade", "-y", "linux-firmware", microcode_pkg],
        check=False,
    )
    log.ok(f"linux-firmware / {microcode_pkg} packages up to date (effective on next boot)")

    if not shutil.which("fwupdmgr"):
        log.warn("fwupdmgr not installed — skipping hardware (BIOS/SSD) firmware check")
        return

    run(["fwupdmgr", "refresh"], check=False)
    r = run(["fwupdmgr", "get-updates"], check=False)
    output = (r.stdout or "") + (r.stderr or "")
    if "No updates available" in output or not output.strip():
        log.ok("no hardware (BIOS/SSD) firmware updates available")
    else:
        log.console.print(output)
        log.warn(
            "hardware firmware updates are available above. NOT applied automatically — "
            "a failed BIOS/SSD flash can brick the board. Run `sudo fwupdmgr update` "
            "yourself when ready, with power/battery secured, then re-run `hyphaed status`."
        )


def _check_nvidia_endbr(kver: str, nv_ko: Path) -> None:
    """Verify nvidia.ko carries endbr64 landing pads when the kernel has IBT on.

    CONFIG_X86_KERNEL_IBT=y enforces Intel CET Indirect Branch Tracking: the
    kernel faults any module whose functions lack endbr64 at their entry points.
    The custom DKMS build injects -fcf-protection=branch to emit ENDBR; this
    check catches a future compiler/kernel combo silently dropping that flag.

    If IBT is off (Ubuntu -generic baseline), no ENDBR requirement — skip.
    """
    config_path = Path(f"/boot/config-{kver}")
    if not config_path.exists():
        return

    ibt_on = "CONFIG_X86_KERNEL_IBT=y" in config_path.read_text()
    if not ibt_on:
        return  # IBT off: no ENDBR required, no check needed

    if not shutil.which("objdump"):
        log.warn(
            f"objdump not found — cannot verify ENDBR in nvidia.ko "
            f"(IBT kernel {kver}). Install binutils."
        )
        return

    # Handle both plain .ko and compressed .ko.zst
    ko_path = str(nv_ko)
    if ko_path.endswith(".ko.zst"):
        decompress_cmd = f"zstd -d -q --stdout {ko_path} 2>/dev/null"
        cmd = f"{decompress_cmd} | objdump -d /dev/stdin 2>/dev/null | grep -c endbr64"
    else:
        cmd = f"objdump -d {ko_path} 2>/dev/null | grep -c endbr64"

    try:
        result = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, timeout=60)
        n = int(result.stdout.strip() or "0")
    except (ValueError, subprocess.TimeoutExpired, OSError):
        n = 0

    if n > 0:
        log.ok(
            f"ENDBR verified: {n} endbr64 instructions in nvidia.ko "
            f"(IBT kernel {kver} — module will load cleanly)"
        )
    else:
        log.err(
            f"nvidia.ko lacks endbr64 (IBT kernel {kver}): module will fault at load!\n"
            f"  Root cause: DKMS build dropped -fcf-protection=branch.\n"
            f"  Fix: rebuild DKMS with KCFLAGS+EXTRA_CFLAGS=-fcf-protection=branch\n"
            f"  or set '# CONFIG_X86_KERNEL_IBT is not set' in "
            f"configs/fragments/20-mitigations-on.config and rebuild the kernel."
        )


def _check_nvidia_fs() -> None:
    """Verify GPUDirect Storage's kernel module (nvidia-fs.ko) is actually
    loaded, not just that the userspace GDS tools are installed.

    Found missing on this box during the 2026-08-10 audit: `gds-tools-*`
    and `libcufile.so.*` were installed via apt, but `lsmod` showed no
    `nvidia_fs` and `/dev/nvidia-fs*` didn't exist. GDS silently falls
    back to POSIX/compat mode in that state — every NVMe<->GPU byte
    bounces through a host RAM staging buffer, crossing PCIe TWICE
    instead of once via true peer-to-peer DMA (CONFIG_PCI_P2PDMA=y is
    already compiled in; the kernel side is ready, only the module was
    absent). This mirrors _check_nvidia_endbr's role: catch a real,
    already-seen misconfiguration at postinstall time instead of letting
    it silently degrade GDS performance.

    Non-fatal either way (GDS is optional; plenty of setups don't use
    it), but log.err if gds-tools is installed and the module isn't
    loaded, since that combination usually means someone intended GDS to
    work and it silently isn't.
    """
    gds_installed = shutil.which("gdscheck") is not None or Path("/usr/local/cuda/lib64/libcufile.so").exists()
    if not gds_installed:
        return  # GDS not in use on this box — nothing to check

    r = run(["lsmod"], check=False)
    loaded = "nvidia_fs" in (r.stdout or "")
    dev_present = any(Path("/dev").glob("nvidia-fs*"))

    if loaded and dev_present:
        log.ok("nvidia-fs.ko loaded, /dev/nvidia-fs present — GDS can use true P2P DMA")
        if shutil.which("gdscheck"):
            gc = run(["gdscheck", "-p"], check=False)
            out = (gc.stdout or "") + (gc.stderr or "")
            if "DMA" in out or "GPUDirect Storage supported" in out:
                log.ok("gdscheck -p confirms DMA mode (not compat/POSIX fallback)")
            else:
                log.warn(
                    "nvidia-fs.ko is loaded but `gdscheck -p` didn't clearly "
                    "confirm DMA mode — run it yourself to check:\n"
                    "  sudo gdscheck -p"
                )
    elif gds_installed:
        log.err(
            "GDS tools (gdscheck/libcufile) are installed but nvidia-fs.ko "
            "is NOT loaded — GDS is silently running in POSIX/compat mode, "
            "bouncing every NVMe<->GPU transfer through host RAM instead of "
            "true peer-to-peer DMA. Fix:\n"
            "  1. Build+register: clone github.com/NVIDIA/gds-nvidia-fs, "
            "     `sudo dkms install` against the new kernel's headers\n"
            "  2. Load it: `sudo modprobe nvidia_fs`\n"
            "  3. Verify: `sudo gdscheck -p` should report DMA mode"
        )


def run_phase(ctx) -> None:
    log.banner("Phase 9/9 — DKMS rebuild & smoke checks")
    kver = _hyphaed_kernel_ver(ctx)
    log.info(f"target kernel for DKMS: {kver}")

    # DKMS autoinstall
    if shutil.which("dkms"):
        run_sudo(["dkms", "autoinstall", "-k", kver], check=False)
        r = run_sudo(["dkms", "status", "-k", kver], check=False)
        log.console.print(r.stdout or "(empty dkms status)")
    else:
        log.warn("dkms not installed — skipping module rebuilds")

    mod_root = Path(f"/lib/modules/{kver}")

    # GreenBoost smoke + explicit rebuild attempt
    headers = mod_root / "build"
    if not headers.exists():
        log.warn(f"no headers symlink at {headers}; GreenBoost will rebuild on first boot")
    else:
        gb_candidates = list(mod_root.rglob("greenboost.ko*"))
        if not gb_candidates and shutil.which("dkms"):
            # Find the registered DKMS package, attempt an explicit install
            r = run(["dkms", "status"], check=False)
            registered = next(
                (ln for ln in r.stdout.splitlines() if ln.startswith("greenboost")),
                None,
            )
            if registered:
                # parse "greenboost, <version>: …"
                ver_match = registered.split(",", 1)[1].strip().split(":", 1)[0].strip()
                log.info(f"trying explicit dkms install greenboost/{ver_match} for {kver}")
                run_sudo(["dkms", "install", f"greenboost/{ver_match}", "-k", kver], check=False)
                gb_candidates = list(mod_root.rglob("greenboost.ko*"))

        if gb_candidates:
            r = run(["modinfo", str(gb_candidates[0])], check=False)
            if r.ok():
                log.ok(f"greenboost module present and readable ({gb_candidates[0].name})")
            else:
                log.warn(f"{gb_candidates[0].name} present but modinfo failed")
        else:
            log.warn(
                f"greenboost.ko not built for {kver} — run from "
                f"{Path.home()/'Dev/greenboost_all/greenboost'}: `sudo ./greenboost_setup.sh install`"
            )

    # NVIDIA smoke + IBT/ENDBR compatibility check
    nv_candidates = list(mod_root.rglob("nvidia.ko*"))
    if nv_candidates:
        log.ok(f"nvidia kernel module installed for new kernel ({nv_candidates[0].name})")
        _check_nvidia_endbr(kver, nv_candidates[0])
        _check_nvidia_fs()
    else:
        log.warn(
            f"nvidia kernel module not built for {kver} — "
            f"run `sudo dkms install nvidia/<ver> -k {kver}` and retry"
        )

    # VMware
    if ctx.profile.has_vmware:
        vmmon_candidates = list(Path(f"/lib/modules/{kver}").rglob("vmmon.ko*"))
        if not vmmon_candidates:
            log.warn(
                "VMware Workstation detected but vmmon/vmnet not built for new kernel.\n"
                "  install community module package: https://github.com/mkubecek/vmware-host-modules\n"
                "  pick the git tag matching your VMware Workstation version"
            )
        else:
            log.ok("VMware host modules built for new kernel")

    # Final initramfs + module-tree sanity
    initrd = Path(f"/boot/initrd.img-{kver}")
    if initrd.exists() and initrd.stat().st_size > 5 * 1024 * 1024:
        log.ok(f"initramfs verified: {initrd.name} ({initrd.stat().st_size // (1024*1024)} MiB)")
    else:
        log.err(f"initramfs missing or too small at {initrd} — DO NOT reboot; run install phase again")
        return

    if mod_root.is_dir() and any(mod_root.iterdir()):
        log.ok(f"/lib/modules/{kver}/ populated")
    else:
        log.err(f"/lib/modules/{kver}/ is empty or missing — modules won't load")

    _update_firmware(ctx)

    log.ok("post-install complete — reboot and pick the new kernel from GRUB")
