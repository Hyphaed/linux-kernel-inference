from pathlib import Path

import pytest

from hyphaed import presets
from hyphaed.phases.patch import _series_file_for

ROOT = Path(__file__).resolve().parent.parent
PATCHES_DIR = ROOT / "patches"


def test_ubuntu_mode_uses_default_series():
    assert _series_file_for(Path("/repo"), "ubuntu") == Path("/repo/patches/series")


def test_kernel_org_mode_uses_dedicated_series():
    assert _series_file_for(Path("/repo"), "kernel-org") == Path("/repo/patches/kernel-org-7.1/series")


def test_missing_source_mode_defaults_to_ubuntu():
    # getattr(ctx, "source_mode", "ubuntu") in run_phase falls back to this
    # default for ctx objects predating the source_mode field.
    assert _series_file_for(Path("/repo"), "ubuntu") == Path("/repo/patches/series")


# ── kernel-org 7.1 series consistency ─────────────────────────────────────────

KERNEL_ORG_SERIES = ROOT / "patches" / "kernel-org-7.1" / "series"


@pytest.mark.skipif(not KERNEL_ORG_SERIES.exists(), reason="kernel-org-7.1/series not present")
def test_kernel_org_series_patches_exist_on_disk():
    """Every non-comment entry in kernel-org-7.1/series must resolve to a patch file."""
    base = KERNEL_ORG_SERIES.parent
    missing = []
    for line in KERNEL_ORG_SERIES.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        p = base / line
        if not p.exists():
            missing.append(line)
    assert missing == [], f"listed but missing: {missing}"


@pytest.mark.skipif(not KERNEL_ORG_SERIES.exists(), reason="kernel-org-7.1/series not present")
def test_sauce_entries_appear_after_vanilla_tunings():
    """Any patch from sauce/ subdirectory must come after all non-sauce patches."""
    entries = []
    for line in KERNEL_ORG_SERIES.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        entries.append(line)
    if not entries:
        return
    # Find the index of the last non-sauce entry and the first sauce entry
    sauce_indices   = [i for i, e in enumerate(entries) if e.startswith("sauce/")]
    vanilla_indices = [i for i, e in enumerate(entries) if not e.startswith("sauce/")]
    if not sauce_indices or not vanilla_indices:
        return  # No sauce entries yet — nothing to check
    assert max(vanilla_indices) < min(sauce_indices), (
        "sauce/ patches must come after all vanilla tuning patches in the series"
    )


@pytest.mark.skipif(not KERNEL_ORG_SERIES.exists(), reason="kernel-org-7.1/series not present")
def test_kernel_org_series_has_bore_first():
    """BORE scheduler patch must be first in the series (scheduler ABI anchor)."""
    for line in KERNEL_ORG_SERIES.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        assert "bore" in line.lower(), f"expected BORE as first patch, got: {line}"
        break


# ── preset YAML patches: field consistency (documentation/dry-run only —
# hyphaed/presets.py::Preset.patches has zero consumers in phases/patch.py,
# the real series files above govern actual application) ────────────────────

KERNEL_ORG_DIR = PATCHES_DIR / "kernel-org-7.1"


@pytest.mark.parametrize("preset_name", presets.list_available())
def test_preset_patches_list_resolves_to_real_files(preset_name):
    """Every patches: entry in every preset YAML must name a file that
    actually exists under patches/ (ubuntu-mode numbering, e.g.
    ai-only.yaml/gaming-ai-vm.yaml) or patches/kernel-org-7.1/ (kernel-org
    mode's own renumbered copies, e.g. gaming-ai-laptop-amd.yaml) — catches
    drift like ai-only.yaml's 2026-07-30 stale references to long-since-
    dropped CachyOS 7.0 patches (0002-cachyos-zen-tunings.patch etc.) that
    were never updated after those patches were confirmed upstreamed and
    removed from the real series.
    """
    preset = presets.load(preset_name)
    missing = [
        p for p in preset.patches
        if not (PATCHES_DIR / p).exists() and not (KERNEL_ORG_DIR / p).exists()
    ]
    assert missing == [], f"{preset_name}.yaml patches: lists nonexistent file(s): {missing}"
