"""nvidia-fs symvers-mismatch detection.

Found on 7.1.10 (2026-08-25): nvidia_fs refused to insert with EINVAL and
`disagrees about version of symbol nvidia_p2p_dma_map_pages`, because
nvidia-fs's Makefile runs `./create_nv.symvers.sh` with no argument and that
script defaults KVER to `uname -r`. Building for a kernel you have not booted
yet resolves the RUNNING kernel's nvidia Module.symvers.

The old guard only said "not loaded", which is true and useless. These pin
the diagnosis.
"""
from __future__ import annotations

from pathlib import Path

from hyphaed.phases.postinstall import (
    _nvidia_fs_built_against_wrong_kernel,
    _nvidia_fs_module_is_truncated,
)


def _make_log(root: Path, fs_ver: str, kver: str, symvers_kver: str) -> None:
    d = root / fs_ver / kver / "x86_64" / "log"
    d.mkdir(parents=True)
    (d / "make.log").write_text(
        "DKMS make.log for nvidia-fs\n"
        "Using nvidia DKMS Module.symvers: "
        f"/var/lib/dkms/nvidia/595.84/{symvers_kver}/x86_64/module/Module.symvers\n"
        "Created: /var/lib/dkms/nvidia-fs/2.29.4/build/nv.symvers\n"
        "  MODPOST Module.symvers\n"
    )


def test_detects_symvers_from_a_different_kernel(tmp_path, monkeypatch):
    _make_log(tmp_path, "2.29.4", "7.1.10-hyphaed", "7.1.9-hyphaed")
    monkeypatch.setattr(
        "hyphaed.phases.postinstall.Path",
        lambda p: tmp_path if "dkms/nvidia-fs" in str(p) else Path(p),
    )
    assert _nvidia_fs_built_against_wrong_kernel("7.1.10-hyphaed") == "7.1.9-hyphaed"


def test_consistent_build_reports_nothing(tmp_path, monkeypatch):
    _make_log(tmp_path, "2.29.4", "7.1.9-hyphaed", "7.1.9-hyphaed")
    monkeypatch.setattr(
        "hyphaed.phases.postinstall.Path",
        lambda p: tmp_path if "dkms/nvidia-fs" in str(p) else Path(p),
    )
    assert _nvidia_fs_built_against_wrong_kernel("7.1.9-hyphaed") is None


def test_missing_log_is_not_an_error(tmp_path, monkeypatch):
    """No DKMS log at all must not be reported as a mismatch — nvidia-fs may
    simply not be installed for that kernel."""
    monkeypatch.setattr(
        "hyphaed.phases.postinstall.Path",
        lambda p: tmp_path if "dkms/nvidia-fs" in str(p) else Path(p),
    )
    assert _nvidia_fs_built_against_wrong_kernel("7.1.10-hyphaed") is None


def test_the_version_sort_trap(tmp_path, monkeypatch):
    """7.1.9 sorts AFTER 7.1.10 as a string. A detector that picked the
    'newest-looking' directory instead of matching the exact kver would get
    this backwards, which is the same class of bug as the .0-release regex
    already recorded in CLAUDE.md."""
    _make_log(tmp_path, "2.29.4", "7.1.10-hyphaed", "7.1.9-hyphaed")
    _make_log(tmp_path, "2.29.4", "7.1.9-hyphaed", "7.1.9-hyphaed")
    monkeypatch.setattr(
        "hyphaed.phases.postinstall.Path",
        lambda p: tmp_path if "dkms/nvidia-fs" in str(p) else Path(p),
    )
    assert _nvidia_fs_built_against_wrong_kernel("7.1.9-hyphaed") is None
    assert _nvidia_fs_built_against_wrong_kernel("7.1.10-hyphaed") == "7.1.9-hyphaed"


# ── a build that was killed leaves a 0-byte module and a happy dkms status ───
#
# Found on 7.1.10 (2026-08-26). The machine was shut down at 22:35 during a
# DKMS build. It left nvidia-fs.ko, Module.symvers and make.log all at 0
# bytes; `dkms status` still said `installed`; modprobe returned a bare
# EINVAL. The symvers detector above finds nothing, correctly — there are no
# symbols to disagree about — so without this check the operator gets the
# generic "not loaded" message and has to work out why themselves.


def _make_module(root: Path, kver: str, size: int) -> Path:
    d = root / kver / "updates" / "dkms"
    d.mkdir(parents=True, exist_ok=True)
    ko = d / "nvidia-fs.ko"
    ko.write_bytes(b"\x7fELF" + b"\0" * (size - 4) if size else b"")
    return ko


def _patch_modules_root(monkeypatch, tmp_path, kver):
    real = Path
    monkeypatch.setattr(
        "hyphaed.phases.postinstall.Path",
        lambda p: tmp_path / kver if str(p) == f"/lib/modules/{kver}" else real(p),
    )


def test_zero_byte_module_is_detected(tmp_path, monkeypatch):
    ko = _make_module(tmp_path, "7.1.10-hyphaed", 0)
    _patch_modules_root(monkeypatch, tmp_path, "7.1.10-hyphaed")
    assert _nvidia_fs_module_is_truncated("7.1.10-hyphaed") == ko


def test_a_real_module_is_not_flagged(tmp_path, monkeypatch):
    _make_module(tmp_path, "7.1.10-hyphaed", 255004)
    _patch_modules_root(monkeypatch, tmp_path, "7.1.10-hyphaed")
    assert _nvidia_fs_module_is_truncated("7.1.10-hyphaed") is None


def test_no_module_at_all_is_not_a_truncation(tmp_path, monkeypatch):
    """nvidia-fs simply not being built for this kernel is a different thing,
    and claiming truncation would send the operator down the wrong path."""
    (tmp_path / "7.1.10-hyphaed").mkdir(parents=True)
    _patch_modules_root(monkeypatch, tmp_path, "7.1.10-hyphaed")
    assert _nvidia_fs_module_is_truncated("7.1.10-hyphaed") is None


def test_missing_modules_dir_does_not_raise(tmp_path, monkeypatch):
    """A postinstall check must never be the thing that kills the run."""
    _patch_modules_root(monkeypatch, tmp_path, "7.1.99-nope")
    assert _nvidia_fs_module_is_truncated("7.1.99-nope") is None


def test_compressed_module_is_covered(tmp_path, monkeypatch):
    """Some distros ship nvidia-fs.ko.zst / .xz. The glob has to catch those
    too, or the check silently passes on exactly the systems it is for."""
    d = tmp_path / "7.1.10-hyphaed" / "updates" / "dkms"
    d.mkdir(parents=True)
    ko = d / "nvidia-fs.ko.zst"
    ko.write_bytes(b"")
    _patch_modules_root(monkeypatch, tmp_path, "7.1.10-hyphaed")
    assert _nvidia_fs_module_is_truncated("7.1.10-hyphaed") == ko
