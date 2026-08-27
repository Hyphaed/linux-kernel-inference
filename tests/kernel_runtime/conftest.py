"""Guards for the live-kernel suite.

Everything in this package reads the RUNNING machine. On a stock kernel, on
the laptop, or in CI there is nothing to assert against, so the whole
package skips rather than failing — `python -m pytest tests/ -q` has to stay
green everywhere it used to be green.
"""
from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path

import pytest

from .probe_helpers import read, sysctl  # noqa: F401  (re-exported)

FLAVOUR = "hyphaed"
HERE = Path(__file__).parent
REPO = HERE.parent.parent


def pytest_collection_modifyitems(config, items):
    """Skip the whole package unless we are booted into a -hyphaed kernel."""
    release = platform.release()
    if FLAVOUR in release:
        return
    skip = pytest.mark.skip(
        reason=f"running {release}, not a -{FLAVOUR} kernel — nothing to verify"
    )
    for item in items:
        if HERE in Path(str(item.fspath)).parents:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def kver() -> str:
    return platform.release()


@pytest.fixture(scope="session")
def kconfig(kver) -> dict[str, str]:
    """The running kernel's own /boot/config-*, parsed once."""
    path = Path(f"/boot/config-{kver}")
    if not path.exists():
        pytest.skip(f"{path} not present")
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith("CONFIG_") and "=" in line:
            k, _, v = line.partition("=")
            out[k] = v
        elif line.startswith("# CONFIG_") and line.endswith(" is not set"):
            out[line.split()[1]] = "n"
    return out


@pytest.fixture(scope="session")
def source_tree(kver) -> Path:
    """The build tree this kernel was compiled from, if it is still around."""
    base = kver.split("-")[0]
    tree = REPO / "build" / f"linux-{base}"
    if not (tree / "Makefile").exists():
        pytest.skip(f"{tree} not present — source-level checks need it")
    return tree


@pytest.fixture(scope="session")
def probes() -> Path:
    """Build the userspace probes once per session."""
    r = subprocess.run(["make", "-s"], cwd=HERE, capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip(f"probe build failed:\n{r.stderr[-2000:]}")
    return HERE


@pytest.fixture(scope="session")
def root_report() -> dict[str, str]:
    """Key=value output left behind by root-steps.sh, if it has been run."""
    path = REPO / "out" / "verify" / "root-steps.txt"
    if not path.exists():
        pytest.skip(
            "out/verify/root-steps.txt missing — run "
            "`sudo bash tests/kernel_runtime/root-steps.sh` first"
        )
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        k, _, v = line.partition("=")
        if k:
            out[k.strip()] = v.strip()

    # A run that was interrupted leaves the file behind, empty or partial.
    # Every consumer here does root_report.get(...), so a missing key reads
    # as a zero and the test fails as though the kernel misbehaved. It did
    # not: nobody asked it anything. Report NOT RUN instead.
    #
    # Not hypothetical — the 2026-08-25 22:35 shutdown truncated this file,
    # cache_ext.log and struct_ops.log to 0 bytes mid-run, alongside
    # nvidia-fs.ko, and the resulting two failures looked like patch 0023
    # regressing.
    if "kernel" not in out or "timestamp" not in out:
        pytest.skip(
            f"{path} is empty or truncated ({path.stat().st_size} bytes) — "
            "root-steps.sh did not finish. Re-run "
            "`sudo bash tests/kernel_runtime/root-steps.sh`"
        )

    # And a report from a different kernel describes a machine that is no
    # longer running. Stale numbers are worse than no numbers.
    running = os.uname().release
    if out["kernel"] != running:
        pytest.skip(
            f"root-steps.txt was recorded on {out['kernel']}, this is "
            f"{running} — re-run "
            "`sudo bash tests/kernel_runtime/root-steps.sh`"
        )
    return out
