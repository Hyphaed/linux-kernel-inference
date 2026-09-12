"""Coverage for the PCIe/DMA byte-reduction substrate added 2026-08-10:
configs/fragments/33-pcie-dma-substrate.config, the 0020 dma-buf
compressed-descriptor patch's series/lock entries, and grub.py's new
opt-in pci=pcie_bus_perf / pcie_acs_override= composer support.
"""
import hashlib
from pathlib import Path

import pytest

from hyphaed import grub, presets
from hyphaed.phases.configure import _select_fragments
from hyphaed.topology import HardwareProfile

ROOT = Path(__file__).resolve().parent.parent
FRAGMENTS_DIR = ROOT / "configs" / "fragments"
FRAGMENT_33 = FRAGMENTS_DIR / "33-pcie-dma-substrate.config"
KERNEL_ORG_SERIES = ROOT / "patches" / "kernel-org-7.1" / "series"
VENDOR_LOCK = ROOT / "patches" / "VENDOR-kernel-org-7.1.lock"
PATCH_0020 = ROOT / "patches" / "custom" / "0020-dma-buf-compressed-descriptor.patch"


# ── fragment 33 itself ────────────────────────────────────────────────────────

def test_fragment_33_exists_and_pins_expected_symbols():
    assert FRAGMENT_33.exists()
    text = FRAGMENT_33.read_text()
    for sym in [
        "CONFIG_PCI_P2PDMA=y",
        "CONFIG_UDMABUF=y",
        "CONFIG_IO_URING_ZCRX=y",
        "CONFIG_PCI_REALLOC_ENABLE_AUTO=y",
        "CONFIG_ZRAM_WRITEBACK=y",
        "CONFIG_CRYPTO_DEFLATE=y",
        "CONFIG_ZRAM_MULTI_COMP=y",
    ]:
        assert sym in text, f"{sym} missing from 33-pcie-dma-substrate.config"


def test_fragment_33_does_not_add_non_selectable_or_multi_numa_symbols():
    """CONFIG_DMABUF_MOVE_NOTIFY (select'ed by amdgpu, which this box disables)
    and CONFIG_MEMORY_TIERING (needs >1 NUMA node; this box has numa_nodes=1)
    were deliberately left out as active directives — the fragment's own
    trailing comment mentions both by name to explain why, so this checks
    for an actual `CONFIG_X=y`/`=m` directive line, not just any mention.
    """
    directive_lines = [
        ln.strip() for ln in FRAGMENT_33.read_text().splitlines()
        if ln.strip().startswith("CONFIG_")
    ]
    assert not any(ln.startswith("CONFIG_DMABUF_MOVE_NOTIFY=") for ln in directive_lines)
    assert not any(ln.startswith("CONFIG_MEMORY_TIERING=") for ln in directive_lines)


@pytest.mark.parametrize("preset_name", ["gaming-ai-vm", "ai-only"])
def test_fragment_33_referenced_by_ai_presets(preset_name):
    preset = presets.load(preset_name)
    assert "33-pcie-dma-substrate.config" in preset.fragments


def test_fragment_33_selected_regardless_of_cpu_gpu_vendor():
    """None of this fragment's tokens (pcie/dma/substrate) collide with
    _select_fragments' vendor-gating tokens (nvidia/intel/amd/raptorlake/
    strixpoint/laptop), so it must always survive selection once listed in
    a preset — unlike e.g. 60-nvidia-wayland.config, which is conditional.
    """
    class FakeCtx:
        preset = "ai-only"
        repo_root = ROOT
        profile = HardwareProfile(
            cpu_codename="strixpoint",  # deliberately NOT raptorlake
            gpu_vendor="amd",           # deliberately NOT nvidia
            cpu_vendor="amd",
            gpu_arch="rdna",
            session_type="wayland",
            p_thread_mask="0-7",
            e_thread_mask="",
            flavour="hyphaed",
            has_amd_gpu=True,
            chassis="desktop",
        )

    selected_names = {p.name for p in _select_fragments(FakeCtx())}
    assert "33-pcie-dma-substrate.config" in selected_names


# ── patch 0020: series + VENDOR lock consistency ───────────────────────────────

@pytest.mark.skipif(not KERNEL_ORG_SERIES.exists(), reason="kernel-org-7.1/series not present")
def test_0020_listed_in_kernel_org_series_after_0019():
    entries = [
        line.strip() for line in KERNEL_ORG_SERIES.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert any("0020-dma-buf-compressed-descriptor.patch" in e for e in entries)
    assert any("0019-dma-buf-priority-hint.patch" in e for e in entries)
    idx_0019 = next(i for i, e in enumerate(entries) if "0019-dma-buf-priority-hint.patch" in e)
    idx_0020 = next(i for i, e in enumerate(entries) if "0020-dma-buf-compressed-descriptor.patch" in e)
    assert idx_0019 < idx_0020, "0020 must come after 0019 — it's authored to apply on top of it"


def test_0020_patch_file_exists():
    assert PATCH_0020.exists()


@pytest.mark.skipif(not VENDOR_LOCK.exists(), reason="VENDOR-kernel-org-7.1.lock not present")
def test_0020_vendor_lock_sha256_matches_real_file():
    lock_text = VENDOR_LOCK.read_text()
    # Match by substring, not startswith — the entry's name field carries a
    # ../custom/ prefix (it's consumed straight from patches/custom/, not
    # copied into patches/kernel-org-7.1/), same convention the rest of this
    # test file already uses for 0019/0020 (see test_0020_ordered_after_0019).
    line = next(
        (ln for ln in lock_text.splitlines() if "0020-dma-buf-compressed-descriptor.patch" in ln),
        None,
    )
    assert line is not None, "0020 entry missing from VENDOR-kernel-org-7.1.lock"
    parts = line.split()
    # format: <name> <url> <sha256> <kind>
    locked_sha = next(p for p in parts if len(p) == 64 and all(c in "0123456789abcdef" for c in p))
    real_sha = hashlib.sha256(PATCH_0020.read_bytes()).hexdigest()
    assert locked_sha == real_sha, "VENDOR lock sha256 is stale — patch file changed since it was pinned"


def test_0020_ioctls_numbered_after_0019_to_avoid_collision():
    """0019 claims ioctl slots 4/5 (SET/GET_PRIORITY); 0020 defines its OWN
    new #define lines at 6/7, not 4/5 — it may still mention 4/5 in a
    comment explaining why (it does, deliberately), so this checks the
    actual `#define DMA_BUF_IOCTL_SET/GET_COMPRESSION` lines specifically,
    not every occurrence of the string "DMA_BUF_BASE, 4" anywhere.
    """
    define_lines = [
        ln.strip() for ln in PATCH_0020.read_text().splitlines()
        if ln.strip().startswith("+#define DMA_BUF_IOCTL_") and "COMPRESSION" in ln
    ]
    assert any("DMA_BUF_BASE, 6" in ln for ln in define_lines), define_lines
    assert any("DMA_BUF_BASE, 7" in ln for ln in define_lines), define_lines
    assert not any("DMA_BUF_BASE, 4" in ln or "DMA_BUF_BASE, 5" in ln for ln in define_lines), define_lines


# ── grub.py: opt-in PCIe cmdline levers ─────────────────────────────────────────

def _profile(**kw):
    base = dict(
        cpu_codename="raptorlake-r",
        gpu_vendor="nvidia",
        gpu_arch="blackwell",
        session_type="wayland",
        p_thread_mask="0-15",
        e_thread_mask="16-31",
        flavour="hyphaed",
    )
    base.update(kw)
    return HardwareProfile(**base)


def test_pcie_levers_off_by_default():
    """Every existing caller of compose_cmdline() omits the new kwargs —
    confirms they default to False and change nothing for current behavior.
    """
    tokens = grub.compose_cmdline(_profile())
    assert not any(t.startswith("pci=pcie_bus_perf") for t in tokens)
    assert not any(t.startswith("pcie_acs_override=") for t in tokens)


def test_enable_pcie_bus_perf_adds_the_token():
    tokens = grub.compose_cmdline(_profile(), enable_pcie_bus_perf=True)
    assert "pci=pcie_bus_perf" in tokens


def test_enable_pcie_acs_override_adds_the_token():
    tokens = grub.compose_cmdline(_profile(), enable_pcie_acs_override=True)
    assert any(t.startswith("pcie_acs_override=") for t in tokens)


def test_both_pcie_levers_can_be_enabled_together():
    tokens = grub.compose_cmdline(
        _profile(), enable_pcie_bus_perf=True, enable_pcie_acs_override=True
    )
    assert "pci=pcie_bus_perf" in tokens
    assert any(t.startswith("pcie_acs_override=") for t in tokens)


def test_pcie_levers_never_silently_replace_mitigations_policy():
    """Sanity check that enabling either new lever doesn't interact with
    the hard mitigations=off refusal — the two mechanisms are independent.
    """
    tokens = grub.compose_cmdline(
        _profile(), ["mitigations=off"], enable_pcie_bus_perf=True
    )
    assert "mitigations=off" not in tokens
    assert "pci=pcie_bus_perf" in tokens
