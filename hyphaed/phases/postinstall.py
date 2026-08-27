from __future__ import annotations
import re
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


def _nvidia_fs_module_is_truncated(kver: str) -> Path | None:
    """Return the built nvidia-fs.ko if DKMS left it empty, else None.

    Found on 7.1.10, 2026-08-26. A DKMS build killed part-way — the machine
    was shut down at 22:35 during it — leaves a zero-byte nvidia-fs.ko, a
    zero-byte Module.symvers and a zero-byte make.log behind, and
    `dkms status` still says `installed`. modprobe then fails with a bare
    EINVAL, which reads exactly like the symvers-mismatch bug above and is
    not it: there are no symbols to disagree about.

    Worth checking before anything else, because every other diagnosis in
    this function assumes a module that at least exists.
    """
    candidates = sorted(Path(f"/lib/modules/{kver}").rglob("nvidia-fs.ko*"))
    for ko in candidates:
        try:
            if ko.stat().st_size == 0:
                return ko
        except OSError:
            continue
    return None


def _nvidia_fs_built_against_wrong_kernel(kver: str) -> str | None:
    """Return the foreign kernel version nvidia-fs took its nvidia symbols
    from, or None if the build looks consistent.

    nvidia-fs's DKMS build logs the Module.symvers it resolved:

        Using nvidia DKMS Module.symvers: \
            /var/lib/dkms/nvidia/<ver>/<KVER>/x86_64/module/Module.symvers

    When <KVER> is not the kernel being built for, the resulting module
    carries the wrong nvidia_p2p_* CRCs and fails to insert with EINVAL.
    Reading the log is exact; guessing from the errno is not.
    """
    logs = sorted(Path("/var/lib/dkms/nvidia-fs").glob(
        f"*/{kver}/*/log/make.log"))
    for log_path in logs:
        try:
            text = log_path.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if "Module.symvers" not in line or "/var/lib/dkms/nvidia/" not in line:
                continue
            m = re.search(r"/var/lib/dkms/nvidia/[^/]+/([^/]+)/", line)
            if m and m.group(1) != kver:
                return m.group(1)
    return None


def _check_nvidia_fs(kver: str) -> None:
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
        truncated = _nvidia_fs_module_is_truncated(kver)
        wrong_kver = _nvidia_fs_built_against_wrong_kernel(kver)
        if truncated:
            log.err(
                f"nvidia-fs.ko is 0 bytes at {truncated} — the DKMS build "
                f"never finished (a killed build or a shutdown mid-build "
                f"leaves the file behind empty), yet `dkms status` still "
                f"reports it installed. modprobe fails with a bare `Invalid "
                f"argument`, systemd-modules-load.service fails with it, and "
                f"that one failure is enough to make the whole system report "
                f"degraded.\n"
                f"GDS is meanwhile in POSIX/compat mode — every NVMe<->GPU "
                f"byte crosses PCIe twice.\n"
                f"Fix: sudo bash diagnostics/fix-boot-7.1.10.sh --go"
            )
        elif wrong_kver:
            # The specific, recurring cause — worth naming instead of making
            # the operator re-derive it from an errno. Found on 7.1.10,
            # 2026-08-25.
            log.err(
                f"nvidia-fs.ko is built for {kver} but against {wrong_kver}'s "
                f"nvidia symbols, so it cannot load (`disagrees about version "
                f"of symbol nvidia_p2p_dma_map_pages`) and GDS is silently in "
                f"POSIX/compat mode — every NVMe<->GPU byte crosses PCIe "
                f"twice.\n"
                f"Cause: nvidia-fs's own Makefile runs "
                f"`./create_nv.symvers.sh` with no argument, and that script "
                f"defaults KVER to `uname -r`. Building for a kernel you "
                f"haven't booted picks the running kernel's nvidia CRCs.\n"
                f"Fix: sudo bash diagnostics/fix-nvidia-fs-symvers.sh"
            )
        else:
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


def _check_apparmor_userns() -> None:
    """Verify patch 0024's unprivileged-userns restriction is actually live,
    not just compiled in.

    Found on the 7.2.0-hyphaed boot audit (2026-08-27):
    `sysctl kernel.apparmor_restrict_unprivileged_userns` read 0, even though
    `/usr/lib/sysctl.d/10-apparmor.conf` sets it to 1 and 0024 wires the real
    enforcement (`apparmor_userns_create()`, `aa_profile_ns_perm()`,
    `AA_USERNS_CREATE`) into the kernel.

    Root cause: `/lib/apparmor/rc.apparmor.functions::check_and_set_userns()`
    (run by apparmor.service on every boot) checks for
    `/sys/kernel/security/apparmor/features/policy/unconfined_restrictions/userns`.
    That file is Ubuntu userspace's way of asking "does this kernel support
    unconfined-userns restriction at all" — 0024 never added a `userns` entry
    to `aa_sfs_entry_unconfined[]` in security/apparmor/apparmorfs.c, so the
    file doesn't exist, so the boot script concludes support is missing and
    force-sets the sysctl back to 0 (logged verbatim: "disabling unprivileged
    userns restrictions since unconfined userns is not supported / enabled").

    The kernel-side enforcement does NOT depend on that file — confirmed by
    reading security/apparmor/{lsm,task,policy}.c in the built 7.2 tree:
    `aa_unprivileged_userns_restricted` is a plain int fed straight from this
    sysctl via proc_dointvec, and task.c reads that global directly. So
    forcing the sysctl back to 1 after apparmor.service runs is a real fix,
    not a cosmetic one — it just needs to happen after the service's own
    boot-time override, every boot.

    This installs that override as a systemd drop-in. The correct long-term
    fix is a small addendum patch adding the missing securityfs advertisement
    node (tracked separately in patches/custom/); this check exists so the
    restriction is actually enforced on every boot in the meantime, and so a
    future regression here isn't silent again.
    """
    dropin_dir = Path("/etc/systemd/system/apparmor.service.d")
    dropin_path = dropin_dir / "90-hyphaed-userns.conf"
    dropin_body = (
        "[Service]\n"
        "ExecStartPost=/usr/sbin/sysctl -w "
        "kernel.apparmor_restrict_unprivileged_userns=1\n"
    )

    r = run(["sysctl", "-n", "kernel.apparmor_restrict_unprivileged_userns"], check=False)
    live_value = (r.stdout or "").strip()

    if live_value == "1" and dropin_path.exists():
        log.ok("kernel.apparmor_restrict_unprivileged_userns=1 — 0024's restriction is live")
        return

    log.err(
        "kernel.apparmor_restrict_unprivileged_userns="
        f"{live_value or '?'} — apparmor.service's own boot script is "
        "force-disabling patch 0024's userns restriction (it can't see the "
        "securityfs advertisement node 0024 doesn't add). The kernel-side "
        "mediation is fully wired and does not need that node to work.\n"
        f"Installing a drop-in to re-assert it after every boot: {dropin_path}"
    )
    if dropin_path.exists() and dropin_path.read_text() == dropin_body:
        log.warn(
            "drop-in already present but sysctl still reads "
            f"{live_value or '?'} — check `systemctl status apparmor.service` "
            "and re-run `sudo sysctl -w "
            "kernel.apparmor_restrict_unprivileged_userns=1` by hand"
        )
        return

    run_sudo(["mkdir", "-p", str(dropin_dir)], check=False)
    run_sudo(["tee", str(dropin_path)], input_str=dropin_body, check=False, capture=True)
    run_sudo(["systemctl", "daemon-reload"], check=False)
    run_sudo(
        ["sysctl", "-w", "kernel.apparmor_restrict_unprivileged_userns=1"],
        check=False,
    )
    log.warn(
        "drop-in installed and sysctl re-asserted for this boot. Verify after "
        "the NEXT reboot too: `sysctl kernel.apparmor_restrict_unprivileged_userns` "
        "should read 1, `unshare -Ur true` should still succeed (confined, not "
        "denied), and Chromium/a flatpak/a podman container should still start."
    )


def _check_dgx_parity_basics() -> None:
    """Apply the small subset of NVIDIA DGX OS tuning judged safe and
    applicable to this box, per docs/dgx-os-parity-2026-08-27.md.

    DGX OS's own baseos apt repo only publishes for Ubuntu 24.04 (noble) --
    confirmed 404 for this box's 26.04 (resolute) release, 2026-08-27 -- so
    the DGX metapackages themselves can't be installed here. This ports the
    three items from that repo's real package contents (pulled and
    inspected directly, not read off a doc page) that are low-risk and
    clearly applicable:

      - nvidia-peermem-loader: load the nvidia-peermem module (DKMS builds
        it, nothing loads it) — matters if GreenBoost's cluster/feeder ever
        does GPUDirect RDMA.
      - nvidia-pci-bridge-power: force PCIe bridge power/control=on, same
        latency-over-power-saving goal this box already applies via
        pcie_aspm.policy=performance on the cmdline.
      - DCGM: generic GPU health/Xid monitoring, available today from the
        already-configured CUDA repo (datacenter-gpu-manager-4-cuda13).

    Deliberately NOT ported here (see the parity doc for the full table):
    nv-mitigations-off (violates this repo's hard mitigations-on constraint),
    transparent_hugepage=madvise (opposite of this box's tuning goal), NVSM
    (repo not published for this release), nv-cpu-governor/nv-limits/nv-iommu
    (already covered by existing powerprofilesctl fix and cmdline). ACS and
    PCIe Relaxed Ordering toggles stay opt-in diagnostics
    (diagnostics/dgx-acs-toggle.sh, diagnostics/dgx-pci-relaxed-ordering-
    toggle.sh) — never auto-applied, since both trade away isolation or need
    a real before/after measurement first.
    """
    log.info("checking DGX-OS-parity basics (peermem, PCIe bridge power, DCGM)...")

    r = run(["lsmod"], check=False)
    if "nvidia_peermem" in (r.stdout or ""):
        log.ok("nvidia_peermem already loaded")
    elif run(["modinfo", "nvidia-peermem"], check=False).ok():
        run_sudo(["modprobe", "nvidia-peermem"], check=False)
        conf = Path("/etc/modules-load.d/nvidia-peermem.conf")
        if not conf.exists():
            run_sudo(["tee", str(conf)], input_str="nvidia-peermem\n", check=False, capture=True)
        log.ok("nvidia_peermem loaded and set to load at boot (only matters for GPUDirect RDMA)")
    else:
        log.warn("nvidia-peermem module not built for this kernel — DKMS may not have registered it")

    bridge_unit = Path("/etc/systemd/system/nvidia-pci-bridge-power.service")
    if not bridge_unit.exists():
        unit_body = (
            "[Unit]\n"
            "Description=Force PCIe bridge power control to 'on' (hyphaed DGX-parity port)\n"
            "DefaultDependencies=no\n"
            "After=sysinit.target local-fs.target\n"
            "Before=basic.target\n\n"
            "[Service]\n"
            "Type=oneshot\n"
            "ExecStart=/bin/sh -c "
            "'for b in $(lspci -D -d ::0604 | awk \"{print \\$1}\"); "
            "do echo on > /sys/bus/pci/devices/$b/power/control 2>/dev/null || true; done'\n\n"
            "[Install]\n"
            "WantedBy=basic.target\n"
        )
        run_sudo(["tee", str(bridge_unit)], input_str=unit_body, check=False, capture=True)
        run_sudo(["systemctl", "daemon-reload"], check=False)
        run_sudo(["systemctl", "enable", "--now", "nvidia-pci-bridge-power.service"], check=False)
        log.ok("nvidia-pci-bridge-power.service installed and enabled (PCIe bridges won't power-down)")
    else:
        log.ok("nvidia-pci-bridge-power.service already installed")

    if shutil.which("dcgmi"):
        log.ok("DCGM already installed")
        run_sudo(["systemctl", "enable", "--now", "nvidia-dcgm"], check=False)
    else:
        cand = run(["apt-cache", "policy", "datacenter-gpu-manager-4-cuda13"], check=False)
        if "Candidate: (none)" not in (cand.stdout or "") and cand.ok():
            run_sudo(["apt-get", "install", "-y", "datacenter-gpu-manager-4-cuda13"], check=False)
            run_sudo(["systemctl", "enable", "--now", "nvidia-dcgm"], check=False)
            if shutil.which("dcgmi"):
                log.ok("DCGM installed and nvidia-dcgm enabled")
            else:
                log.warn("DCGM install attempted but dcgmi still not on PATH — check apt output above")
        else:
            log.warn(
                "datacenter-gpu-manager-4-cuda13 has no candidate in the configured "
                "CUDA repo — skipping DCGM install"
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
        _check_nvidia_fs(kver)
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

    _check_apparmor_userns()
    _check_dgx_parity_basics()

    _update_firmware(ctx)

    log.ok("post-install complete — reboot and pick the new kernel from GRUB")
