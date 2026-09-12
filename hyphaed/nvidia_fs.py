"""Shared nvidia-fs (GPUDirect Storage) DKMS-build diagnostics.

Moved out of `phases/postinstall.py` on 2026-09-12 so `hyphaed verify` can
name the same causes postinstall already knew how to diagnose, instead of
reporting the bare "NOT loaded" symptom and making the operator re-derive the
cause from an errno (as happened on 7.2.5-hyphaed: the real cause was one
line in nvidia-fs's own DKMS make.log the whole time).
"""
from __future__ import annotations
import re
from pathlib import Path


def nvidia_fs_module_is_truncated(kver: str) -> Path | None:
    """Return the built nvidia-fs.ko if DKMS left it empty, else None.

    Found on 7.1.10, 2026-08-26. A DKMS build killed part-way — the machine
    was shut down at 22:35 during it — leaves a zero-byte nvidia-fs.ko, a
    zero-byte Module.symvers and a zero-byte make.log behind, and
    `dkms status` still says `installed`. modprobe then fails with a bare
    EINVAL, which reads exactly like the symvers-mismatch bug below and is
    not it: there are no symbols to disagree about.

    Worth checking before anything else, because every other diagnosis in
    this module assumes a module that at least exists.
    """
    candidates = sorted(Path(f"/lib/modules/{kver}").rglob("nvidia-fs.ko*"))
    for ko in candidates:
        try:
            if ko.stat().st_size == 0:
                return ko
        except OSError:
            continue
    return None


def nvidia_fs_built_against_wrong_kernel(kver: str) -> str | None:
    """Return the foreign kernel version nvidia-fs took its nvidia symbols
    from, or None if the build looks consistent.

    nvidia-fs's DKMS build logs the Module.symvers it resolved:

        Using nvidia DKMS Module.symvers: \
            /var/lib/dkms/nvidia/<ver>/<KVER>/x86_64/module/Module.symvers

    When <KVER> is not the kernel being built for, the resulting module
    carries the wrong nvidia_p2p_* CRCs and fails to insert with EINVAL.
    Reading the log is exact; guessing from the errno is not. Confirmed
    2026-09-12 on 7.2.5-hyphaed: nvidia-fs was built for 7.2.5-hyphaed
    against 7.2.3-hyphaed's nvidia.ko because the box was still booted on
    7.2.3 during the DKMS autoinstall.
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
