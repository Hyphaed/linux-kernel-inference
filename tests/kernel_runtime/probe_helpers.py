"""Small read-only accessors shared by the live-kernel test modules."""
from __future__ import annotations

import subprocess
from pathlib import Path


def sysctl(name: str) -> str | None:
    """Current value of a sysctl, or None if it does not exist."""
    r = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def read(path: str) -> str | None:
    """Contents of a /proc or /sys file, stripped, or None if unreadable."""
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None
