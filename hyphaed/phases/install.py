from __future__ import annotations
import os
import shutil
from pathlib import Path

from ..util import log
from ..util.run import run_sudo
from ..util.prompts import confirm
from .. import grub, secureboot

NAME = "install"

_DRACUT_CONF = Path("/etc/dracut.conf.d/99-hyphaed-no-nvidia-initramfs.conf")
_DRACUT_CONF_BODY = (
    "# NVIDIA modules are runtime-only (loaded by udev after root is mounted).\n"
    "# Embedding them adds ~75 MB and breaks recovery boot: dracut tries to load\n"
    "# nvidia_drm during the initramfs phase; nomodeset (recovery cmdline) causes\n"
    "# DRM init failure, dropping the system into emergency shell instead of the\n"
    "# Ubuntu friendly-recovery menu.\n"
    "#\n"
    "# nvidia_fs added 2026-08-18. It was omitted from this list while\n"
    "# /etc/modules-load.d/nvidia-fs.conf still asks systemd-modules-load to\n"
    "# bring it up early, so every boot on this box logged eight consecutive\n"
    "# failures at t=1.65s:\n"
    "#   nvidia_fs: Unknown symbol nvidia_p2p_get_pages (err -2)   [x8]\n"
    "# nvidia_fs depends on nvidia for the nvidia_p2p_* GPUDirect Storage\n"
    "# symbols, and nvidia is (correctly) not in the initramfs, so the early\n"
    "# load can only fail. It self-heals — nvidia loads at t=60s and nvidia_fs\n"
    "# re-initialises cleanly at t=61.8s ('registered correctly with major\n"
    "# number 508') — so this is boot-log noise, not a broken GDS path. Worth\n"
    "# fixing anyway: eight error lines that are expected teach you to ignore\n"
    "# nvidia_fs errors, which is exactly the wrong reflex for the T3/GDS tier.\n"
    'omit_drivers+=" nvidia nvidia_drm nvidia_modeset nvidia_uvm nvidia_peermem nvidia_fs "\n'
)

# Bumped whenever _DRACUT_CONF_BODY changes meaning. The old check was
# `"nvidia_drm" in body and "omit_drivers" in body`, which is true of every
# version this file has ever had — so an already-deployed conf was never
# updated, and the nvidia_fs addition above would have silently never
# reached a machine that already had the file. Match on the current driver
# list instead of on any-version markers.
_DRACUT_OMIT_MARKER = "nvidia_fs "


def _ensure_dracut_no_nvidia_conf() -> None:
    try:
        body = _DRACUT_CONF.read_text()
        if "omit_drivers" in body and _DRACUT_OMIT_MARKER in body:
            log.ok(f"dracut no-nvidia conf already in place ({_DRACUT_CONF.name})")
            return
        if "omit_drivers" in body:
            log.info(f"{_DRACUT_CONF.name} predates the nvidia_fs omission — refreshing")
    except OSError:
        pass
    log.info(f"writing {_DRACUT_CONF.name} — excludes NVIDIA from initramfs")
    run_sudo(["tee", str(_DRACUT_CONF)], input_str=_DRACUT_CONF_BODY)
    log.ok("NVIDIA excluded from initramfs; recovery boot will reach Ubuntu recovery menu")


_DRACUT_MODULE_DIR = Path("/usr/lib/dracut/modules.d/99hyphaed-no-nvidia")
_DRACUT_MODULE_BODY = (
    "#!/bin/bash\n"
    "# 99hyphaed-no-nvidia , written by hyphaed/phases/install.py.\n"
    "#\n"
    "# Companion to 99-hyphaed-no-nvidia-initramfs.conf. That file's\n"
    "# omit_drivers keeps the NVIDIA modules themselves out of the image, and it\n"
    "# works , it is why the old 'nvidia_fs: Unknown symbol nvidia_p2p_get_pages'\n"
    "# storm is gone. But omit_drivers only omits KERNEL MODULES, and dracut's\n"
    "# own 00systemd module copies /etc/modules-load.d in wholesale, so the\n"
    "# fragment ASKING for nvidia_fs still shipped. The old eight-line storm was\n"
    "# simply replaced by one line, every boot:\n"
    "#\n"
    "#   systemd-modules-load[400]: Failed to find module 'nvidia_fs'\n"
    "#\n"
    "# Same reasoning as the conf it accompanies: an expected error teaches you\n"
    "# to ignore nvidia_fs errors, which is the wrong reflex for a GPUDirect\n"
    "# Storage path. The real-root load at t=60s is unaffected , that is where\n"
    "# nvidia_fs was always meant to come up.\n"
    "#\n"
    "# The 99 prefix is load-bearing: dracut runs module install() hooks in\n"
    "# numeric order, so this must sort after 00systemd or there is nothing\n"
    "# there to remove yet.\n"
    "check()   { return 0; }\n"
    "depends() { echo systemd; return 0; }\n"
    "install() {\n"
    '    rm -f "$initdir"/etc/modules-load.d/nvidia-fs.conf \\\n'
    '          "$initdir"/usr/lib/modules-load.d/nvidia-fs.conf 2>/dev/null\n'
    "    return 0\n"
    "}\n"
)


def _ensure_dracut_modules_load_exclusion() -> None:
    """Keep /etc/modules-load.d/nvidia-fs.conf out of the initramfs.

    omit_drivers cannot do this , it omits modules, not the config fragments
    that request them , so it takes a dracut module that deletes the file from
    $initdir after 00systemd has copied the directory in.
    """
    target = _DRACUT_MODULE_DIR / "module-setup.sh"
    try:
        if target.read_text() == _DRACUT_MODULE_BODY:
            log.ok(f"dracut modules-load exclusion already in place ({_DRACUT_MODULE_DIR.name})")
            return
        log.info(f"{_DRACUT_MODULE_DIR.name} differs from the shipped version , refreshing")
    except OSError:
        pass
    log.info(f"writing {_DRACUT_MODULE_DIR.name} , drops the nvidia_fs modules-load fragment from the initramfs")
    run_sudo(["mkdir", "-p", str(_DRACUT_MODULE_DIR)])
    run_sudo(["tee", str(target)], input_str=_DRACUT_MODULE_BODY)
    run_sudo(["chmod", "0755", str(target)])
    log.ok("initrd will no longer ask for a module it does not carry")


def _regenerable_kernels() -> list[str]:
    """Kernels that actually have an image and modules to build an initrd from.

    NOT `dracut --force --regenerate-all`. That walks every directory under
    /lib/modules and fails the whole run on the first one it cannot handle,
    which on a machine that has had kernels removed is a certainty: purged
    packages leave the depmod metadata behind (modules.dep and friends, no
    .ko files, no /boot/vmlinuz), and dracut dies on them with exit 6 and a
    wall of kmod_module_parse_depline errors. Observed 2026-08-21 with six
    such leftovers from 7.0.0-14 through 7.0.0-29.

    A kernel counts as real when it has both a modules tree and an installed
    image. Everything else is residue.
    """
    mods = Path("/lib/modules")
    if not mods.is_dir():
        return []
    out: list[str] = []
    for d in sorted(mods.iterdir()):
        if not d.is_dir():
            continue
        if not (d / "kernel").is_dir():
            continue  # metadata-only residue from a purged package
        if not any(Path("/boot").glob(f"vmlinuz-{d.name}")):
            continue  # modules without an image , nothing to pair an initrd with
        out.append(d.name)
    return out


def apply_boot_config(regenerate: bool = True) -> None:
    """Write the boot-image and udev config this project owns, nothing else.

    These three writers used to be reachable only from inside the install
    phase, past the "install N packages?" confirm , so applying a one-line boot
    fix meant reinstalling the kernel. They are idempotent and independent of
    any build, and `hyphaed update-boot-config` calls this.

    Regenerating the initramfs is the part that actually takes effect: dracut
    copies /etc/modules-load.d in wholesale at image-build time, so writing the
    exclusion module changes nothing until the image is rebuilt.
    """
    _ensure_dracut_no_nvidia_conf()
    _ensure_dracut_modules_load_exclusion()
    _ensure_pci_numa_rule()
    if not regenerate:
        log.info("skipping initramfs regeneration , the changes take effect on the next rebuild")
        return
    if shutil.which("dracut") is None:
        log.warn("dracut not found , regenerate your initramfs manually for this to take effect")
        return

    kernels = _regenerable_kernels()
    if not kernels:
        log.warn("no complete kernel found in /lib/modules , regenerate your initramfs manually")
        return

    log.info(f"regenerating the initramfs for {len(kernels)} kernel(s): {', '.join(kernels)}")
    failed: list[str] = []
    for kver in kernels:
        try:
            run_sudo(["dracut", "--force", "--kver", kver])
        except RuntimeError as e:
            log.warn(f"dracut failed for {kver} , continuing: {e}")
            failed.append(kver)
    if failed:
        log.warn(f"{len(failed)} kernel(s) not rebuilt: {', '.join(failed)}")
        log.info("the others are done; a kernel that fails here is usually a "
                 "half-removed package, not a problem with this config")
    else:
        log.ok("initramfs rebuilt , the excluded fragments are out of the boot image")


_NUMA_RULE = Path("/etc/udev/rules.d/62-hyphaed-pci-numa-node.rules")
_NUMA_RULE_BODY = (
    "# Consumer boards routinely omit ACPI _PXM for PCIe slots, so the kernel\n"
    "# reports numa_node=-1 (NUMA_NO_NODE) for devices that plainly do belong\n"
    "# to the only node there is. Observed on this box 2026-08-18:\n"
    "#   /sys/bus/pci/devices/0000:01:00.0/numa_node -> -1\n"
    "#\n"
    "# On a machine with exactly one online node, 0 is the only truthful\n"
    "# answer and -1 just means firmware declined to say. This rule is written\n"
    "# ONLY on single-node machines (checked at install time, see\n"
    "# _ensure_pci_numa_rule) because on a real multi-node box a fabricated\n"
    "# node would misdirect NUMA-local allocation, which is worse than the\n"
    "# warning it silences.\n"
    "#\n"
    "# Correction, 2026-08-28 boot-log audit of 7.2.1-hyphaed: this rule does\n"
    "# NOT silence the \"nvidia-fs:warning: error retrieving numa node\" lines\n"
    "# seen at boot (still 4/boot with this rule active and numa_node reading\n"
    "# 0). Read from source (/usr/src/nvidia-fs-2.29.4/nvfs-pci.h:213),\n"
    "# nvfs_get_numa_node_from_pdevinfo() calls pcibus_to_node(pdev->bus) --\n"
    "# the PCI BUS's NUMA association, a separate value from the per-device\n"
    "# ATTR{numa_node} this rule patches, set once from firmware _PXM data at\n"
    "# bus enumeration and never touched by the kernel's own \"[Firmware Bug]:\n"
    "# Overriding NUMA node to 0\" per-device fixup either. There is no\n"
    "# writable sysfs attribute for a PCI bus's NUMA node, so this warning is\n"
    "# not reachable from a udev rule. It is harmless: both call sites treat\n"
    "# a negative result as \"not NUMA aware\" and fall back to LOCAL_DISTANCE,\n"
    "# which is the correct answer on a one-node box anyway. This rule stays\n"
    "# for the consumers that DO read the per-device attribute (numactl and\n"
    "# generic PCI tooling), on its own merits.\n"
    'ACTION=="add", SUBSYSTEM=="pci", ATTR{numa_node}=="-1", ATTR{numa_node}="0"\n'
)


def _online_numa_nodes() -> int:
    """Count online NUMA nodes. 0 on a kernel without CONFIG_NUMA sysfs."""
    try:
        return len(list(Path("/sys/devices/system/node").glob("node[0-9]*")))
    except OSError:
        return 0


def _ensure_pci_numa_rule() -> None:
    nodes = _online_numa_nodes()
    if nodes != 1:
        log.info(
            f"{nodes} NUMA node(s) detected — not writing {_NUMA_RULE.name}; "
            "pinning a node is only truthful on a single-node machine"
        )
        return
    try:
        if _NUMA_RULE.read_text() == _NUMA_RULE_BODY:
            log.ok(f"PCI numa_node rule already in place ({_NUMA_RULE.name})")
            return
    except OSError:
        pass
    log.info(f"writing {_NUMA_RULE.name} — pins numa_node=0 for PCI devices reporting -1")
    run_sudo(["tee", str(_NUMA_RULE)], input_str=_NUMA_RULE_BODY)
    log.ok("PCI numa_node rule written; takes effect on next boot (or `udevadm trigger`)")


# Packages the kernel cannot boot/run without. Anything else (e.g.
# linux-hyphaed-tools — cpupower/turbostat/x86_energy_perf_policy) is a
# nice-to-have: a dpkg conflict on one of those must not abort the phase
# before the boot-critical initramfs check and GRUB drop-in run.
_REQUIRED_PREFIXES = ("linux-headers-", "linux-image-", "linux-libc-dev")


def _is_required(name: str) -> bool:
    return name.startswith(_REQUIRED_PREFIXES)


def _order(debs: list[Path]) -> list[Path]:
    """headers first, then image, then libc-dev, then anything else."""
    RANKS = {"linux-headers-": 0, "linux-image-": 1, "linux-libc-dev": 2}

    def key(p: Path) -> int:
        n = p.name
        for prefix, rank in RANKS.items():
            if n.startswith(prefix):
                return rank
        return 9

    return sorted(debs, key=key)


def _verify_or_repair_initramfs(kver: str) -> None:
    """Check /boot/initrd.img-{kver} was created; force dracut if not."""
    initrd = Path(f"/boot/initrd.img-{kver}")
    _MIN_INITRD_BYTES = 5 * 1024 * 1024  # anything under 5 MB is almost certainly broken

    if initrd.exists() and initrd.stat().st_size >= _MIN_INITRD_BYTES:
        log.ok(f"initramfs present: {initrd.name} ({initrd.stat().st_size // (1024*1024)} MiB)")
        return

    if initrd.exists():
        log.warn(f"initramfs at {initrd} is suspiciously small ({initrd.stat().st_size // 1024} KiB) — regenerating")
    else:
        log.warn(f"initramfs NOT found at {initrd} — dpkg postinst hook may have failed silently")

    # Prefer dracut (Ubuntu 26.04); fall back to update-initramfs
    if shutil.which("dracut"):
        log.info(f"running: dracut --hostonly --force {initrd} {kver}")
        run_sudo(["dracut", "--hostonly", "--force", str(initrd), kver])
    elif shutil.which("update-initramfs"):
        log.info(f"running: update-initramfs -c -k {kver}")
        run_sudo(["update-initramfs", "-c", "-k", kver])
    else:
        log.err("neither dracut nor update-initramfs found — cannot generate initramfs")
        log.err(f"  fix manually: sudo dracut --force /boot/initrd.img-{kver} {kver}")
        raise SystemExit(2)

    # Re-check after forced generation
    if not initrd.exists() or initrd.stat().st_size < _MIN_INITRD_BYTES:
        log.err(f"initramfs regeneration failed — {initrd} still missing or too small")
        log.err("this kernel WILL panic on boot — do not reboot until this is resolved")
        raise SystemExit(2)

    log.ok(f"initramfs regenerated: {initrd.name} ({initrd.stat().st_size // (1024*1024)} MiB)")


def run_phase(ctx) -> None:
    log.banner("Phase 8/9 — Install .deb + GRUB")
    profile = ctx.profile

    # Secure Boot preflight
    sb_ok, sb_reason = secureboot.preflight(profile)
    log.info(f"SecureBoot: {sb_reason}")
    if not sb_ok:
        log.err("aborting install — generate MOK keys with `sudo update-secureboot-policy` first")
        raise SystemExit(2)

    debs = ctx.packaged_debs or []
    if not debs:
        # Recover from already-built run
        out = ctx.repo_root / "out" / "debs"
        debs = sorted(out.glob("linux-*.deb"))
    if not debs:
        if ctx.dry_run:
            log.info("(dry-run) no .deb files yet; would install once build phase runs")
            return
        log.err("no .deb files found to install")
        raise SystemExit(2)

    debs = _order(debs)
    log.info("will install (in order):")
    total_bytes = 0
    for d in debs:
        total_bytes += d.stat().st_size
        log.console.print(f"  • {d.name}  ({d.stat().st_size // (1024*1024)} MB)")
    log.info(f"total: {total_bytes // (1024*1024)} MB")

    # /boot space check
    st = os.statvfs("/boot")
    free_mb = (st.f_bavail * st.f_frsize) // (1024 * 1024)
    if free_mb < 400:
        log.err(f"/boot has only {free_mb} MB free — refuse to install (need ≥400 MB)")
        raise SystemExit(2)

    # Already running -hyphaed?
    if profile.running_kernel.endswith(f"-{profile.flavour}"):
        log.warn(f"the running kernel ({profile.running_kernel}) is already a -{profile.flavour} build")

    if ctx.dry_run:
        log.info("(dry-run) would dpkg -i the packages above")
    else:
        if not confirm(
            f"proceed with installation of {len(debs)} packages?",
            default=False, hard=True,
        ):
            # Declining must not look like a crash, and must not look like the
            # build was thrown away. Real incident 2026-08-18: a 37-minute
            # build finished, the user pressed Enter a few times at what they
            # thought was a stalled progress bar, and this prompt (default=No)
            # took one of them, exited 0, and printed nothing. The .debs were
            # all present in out/debs/ and phases_completed already listed
            # build+package, but nothing on screen said so, so it read as
            # "the build died at 100%".
            log.info(f"install declined — {len(debs)} package(s) are built and "
                     f"waiting in {debs[0].parent}")
            log.info("resume with:  python -m hyphaed --from-phase install")
            log.warn("note this prompt defaults to NO, so a bare Enter declines it")
            raise SystemExit(0)
        _ensure_dracut_no_nvidia_conf()
        _ensure_dracut_modules_load_exclusion()
        _ensure_pci_numa_rule()
        failed_optional: list[str] = []
        for d in debs:
            if _is_required(d.name):
                run_sudo(["dpkg", "-i", str(d)])
                continue
            try:
                run_sudo(["dpkg", "-i", str(d)])
            except RuntimeError as e:
                log.warn(f"optional package {d.name} failed to install — continuing: {e}")
                failed_optional.append(d.name)
        if failed_optional:
            log.warn(
                f"{len(failed_optional)} optional package(s) not installed: "
                f"{', '.join(failed_optional)} — the kernel itself is unaffected"
            )

    # Verify initramfs was created by the dpkg postinst hook (dracut).
    # A missing or tiny initrd = guaranteed kernel panic on first boot.
    from ..version import hyphaed_uname_r
    kver = hyphaed_uname_r(profile.running_kernel, profile.flavour, getattr(ctx, "source_mode", "ubuntu"))
    if not ctx.dry_run:
        _verify_or_repair_initramfs(kver)

    # GRUB drop-in
    from .. import presets as presets_mod
    extra: list[str] = []
    if ctx.preset:
        try:
            preset = presets_mod.load(ctx.preset)
            extra = preset.cmdline_extra
        except Exception:
            pass
    current = grub.read_current_cmdline_config()
    # Skip keys already configured elsewhere (greenboost edits /etc/default/grub
    # directly) so we don't emit double tokens.
    cmdline = grub.compose_cmdline(profile, extra, skip_keys_present_in=current)
    log.info("kernel cmdline diff (current → after drop-in applied):")
    grub.print_cmdline_diff(current, cmdline)

    path, changed = grub.write_dropin(cmdline, dry_run=ctx.dry_run)
    label_changed = grub.write_label_dropin(dry_run=ctx.dry_run)
    if (changed or label_changed) and not ctx.dry_run:
        grub.update_grub(dry_run=ctx.dry_run)

    # Pin the just-installed kernel as the explicit GRUB default so a future
    # `update-grub` menu regeneration can't silently change what boots by
    # default (found live 2026-07-30: grubenv's saved_entry pointed at a
    # kernel version no longer installed, booting only via GRUB's
    # fallback-to-newest behavior, not an explicit pin). Downstream of the
    # "proceed with installation?" confirm gate above — same discipline as
    # the GRUB drop-in/update-grub calls it sits alongside.
    grub.pin_default_kernel(kver, dry_run=ctx.dry_run)

    log.ok(f"installed kernel: {ctx.kernel_pkgver}")
