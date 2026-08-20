from __future__ import annotations
import shutil
from pathlib import Path

from .util import log
from .util.run import run

MOK_DIR = Path("/var/lib/shim-signed/mok")
MOK_KEY = MOK_DIR / "MOK.priv"
MOK_CERT = MOK_DIR / "MOK.der"


def is_enabled() -> bool:
    if not shutil.which("mokutil"):
        return False
    r = run(["mokutil", "--sb-state"], check=False)
    return "SecureBoot enabled" in r.stdout


def has_keys() -> bool:
    return MOK_KEY.exists() and MOK_CERT.exists()


def sign_module(kernel_ver: str, module_path: Path, *, dry_run: bool = False) -> None:
    """Sign a kernel module with the local MOK using sign-file."""
    sign_file = Path(f"/usr/src/linux-headers-{kernel_ver}/scripts/sign-file")
    if not sign_file.exists():
        log.warn(f"sign-file missing at {sign_file}; cannot sign {module_path.name}")
        return
    if not has_keys():
        log.warn("no MOK keys at /var/lib/shim-signed/mok; cannot sign")
        return
    cmd = [str(sign_file), "sha256", str(MOK_KEY), str(MOK_CERT), str(module_path)]
    if dry_run:
        log.info(f"would run: {' '.join(cmd)}")
        return
    run(cmd, check=True)
    log.ok(f"signed {module_path.name}")


def preflight(profile) -> tuple[bool, str]:
    """Return (ok, reason). If SB enabled but no key, refuse install."""
    if not profile.secure_boot:
        return True, "Secure Boot disabled"
    if has_keys():
        return True, "Secure Boot enabled with MOK keys available"
    return False, "Secure Boot enabled but /var/lib/shim-signed/mok keys not found — refusing to install unsigned kernel"
