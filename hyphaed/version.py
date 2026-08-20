"""Shared helpers to derive the kernel/package version from the running kernel.

These are used by multiple phases (patch needs the base tag, build needs
KDEB_PKGVERSION, install displays it). Centralised so phase order doesn't matter.
"""
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .topology import HardwareProfile


def derive_kernel_version(running_kernel: str) -> tuple[str, str]:
    """'7.0.0-15-generic' -> ('7.0.0', '15.15')"""
    parts = running_kernel.split("-")
    base = parts[0] if parts else "0.0.0"
    abi = parts[1] if len(parts) > 1 else "1"
    return base, f"{abi}.{abi}"


def hyphaed_uname_r(running_kernel: str, flavour: str, source_mode: str = "ubuntu") -> str:
    """'7.0.0-15-generic' + 'hyphaed' -> '7.0.0-15-hyphaed' (ubuntu mode).

    kernel-org mode has no ABI segment at all — a vanilla kernel.org build's
    actual `uname -r` is just `{VERSION.PATCHLEVEL.SUBLEVEL}{LOCALVERSION}`
    (LOCALVERSION is set to "-{flavour}" in build.py's env), e.g.
    "7.1.1-hyphaed", confirmed against a real install. The synthetic
    "{ver}-{canonical_abi}-generic" string source.py uses for
    profile.running_kernel (to reuse Canonical's baseline-config filename
    convention) does NOT describe the real boot-time kernel release —
    using it here would make install.py/postinstall.py look for
    /boot/initrd.img-<wrong> and /lib/modules/<wrong>/ that don't exist.
    """
    base, _ubuntu_abi = derive_kernel_version(running_kernel)
    if source_mode == "kernel-org":
        return f"{base}-{flavour}"
    parts = running_kernel.split("-")
    abi = parts[1] if len(parts) > 1 else "1"
    return f"{base}-{abi}-{flavour}"


def kdeb_pkgversion(profile: "HardwareProfile", build_num: int = 1,
                    source_mode: str = "ubuntu") -> str:
    """e.g. '7.0.0-15.15hyphaed1+rptr-nvbw-wl' (ubuntu) or '7.1.2-1+rptr-nvbw' (kernel-org)."""
    base, ubuntu_abi = derive_kernel_version(profile.running_kernel or "")
    if source_mode == "kernel-org":
        # No Ubuntu ABI segment — just base-buildnum+tag
        return f"{base}-{build_num}+{profile.tag()}"
    return f"{base}-{ubuntu_abi}{profile.flavour}{build_num}+{profile.tag()}"


def base_git_tag(profile: "HardwareProfile") -> str:
    """'base-7.0.0-15.15' — used to tag the freshly-extracted Ubuntu source."""
    base, ubuntu_abi = derive_kernel_version(profile.running_kernel or "")
    return f"base-{base}-{ubuntu_abi}"
