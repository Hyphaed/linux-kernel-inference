"""Selecting fragments must not write anything into the build tree.

Real incident, 2026-08-18: build/generated/90-machine-topology.config on this
Intel i9-14900KF desktop was found containing

    # host=unknown vendor=amd codename=strixpoint threads=0
    CONFIG_CPU_SUP_AMD=y
    CONFIG_CPU_SUP_INTEL=n

which is machine 2's fragment, and would produce an unbootable kernel on
machine 1. Nothing malicious wrote it: _select_fragments() used to call
_write_machine_topology_fragment() itself, and tests/test_pcie_substrate.py
calls _select_fragments() with the REAL repo_root plus a deliberately fake
Strix Point profile. Running `pytest` was enough.

The generator is now the caller's job (configure.run_phase), so selection is
pure. These tests pin both halves of that: the selector writes nothing, and
the generator still derives the right vendor from whatever profile it is
handed.
"""
from pathlib import Path

from hyphaed.phases.configure import (
    _select_fragments,
    _write_machine_topology_fragment,
)
from hyphaed.topology import HardwareProfile

ROOT = Path(__file__).resolve().parent.parent
GENERATED = ROOT / "build" / "generated" / "90-machine-topology.config"


def _fake_ctx(repo_root, **profile_kw):
    defaults = dict(
        cpu_codename="strixpoint",
        gpu_vendor="amd",
        cpu_vendor="amd",
        gpu_arch="rdna",
        session_type="wayland",
        p_thread_mask="0-7",
        e_thread_mask="",
        flavour="hyphaed",
        has_amd_gpu=True,
        chassis="desktop",
    )
    defaults.update(profile_kw)

    class FakeCtx:
        preset = "ai-only"
        profile = HardwareProfile(**defaults)

    FakeCtx.repo_root = repo_root
    return FakeCtx()


def test_selecting_fragments_writes_nothing_to_the_real_repo():
    """The exact call shape test_pcie_substrate.py uses."""
    before = GENERATED.read_bytes() if GENERATED.exists() else None
    before_mtime = GENERATED.stat().st_mtime_ns if GENERATED.exists() else None

    _select_fragments(_fake_ctx(ROOT))

    after = GENERATED.read_bytes() if GENERATED.exists() else None
    after_mtime = GENERATED.stat().st_mtime_ns if GENERATED.exists() else None
    assert after == before, "selection rewrote the machine-topology fragment"
    assert after_mtime == before_mtime, "selection touched the fragment"


def test_selection_still_includes_a_generated_fragment_when_given_one(tmp_path):
    ctx = _fake_ctx(tmp_path)
    (tmp_path / "configs" / "fragments").mkdir(parents=True)
    generated = _write_machine_topology_fragment(ctx)
    assert generated is not None
    assert generated in _select_fragments(ctx, generated=generated)


def test_generator_derives_the_vendor_from_the_profile_it_is_given(tmp_path):
    """Cross-building for the other machine stays possible — the fix is about
    *who* writes, not about pinning one vendor."""
    intel = _write_machine_topology_fragment(
        _fake_ctx(tmp_path / "intel", cpu_vendor="intel", cpu_codename="raptorlake-r",
                  gpu_vendor="nvidia", gpu_arch="blackwell", has_amd_gpu=False))
    body = intel.read_text()
    assert "CONFIG_CPU_SUP_INTEL=y" in body
    assert "CONFIG_CPU_SUP_AMD=n" in body
    assert "CONFIG_X86_AMD_PSTATE=n" in body   # the select-breaking half

    amd = _write_machine_topology_fragment(_fake_ctx(tmp_path / "amd"))
    body = amd.read_text()
    assert "CONFIG_CPU_SUP_AMD=y" in body
    assert "CONFIG_CPU_SUP_INTEL=n" in body


def test_unknown_vendor_narrows_nothing(tmp_path):
    assert _write_machine_topology_fragment(
        _fake_ctx(tmp_path, cpu_vendor="unknown")) is None
