from pathlib import Path
from types import SimpleNamespace

import pytest

from hyphaed import presets
from hyphaed.phases.patch import _series_file_for

ROOT = Path(__file__).resolve().parent.parent
PATCHES_DIR = ROOT / "patches"


def test_ubuntu_mode_uses_default_series():
    assert _series_file_for(Path("/repo"), "ubuntu") == Path("/repo/patches/series")


class _FakeCtx:
    """Minimal stand-in for the bits _series_file_for reads."""
    def __init__(self, source_dir=None, target=None):
        self.source_dir = source_dir
        self.patch_series_dir = None
        self.args = SimpleNamespace(target=target) if target is not None else None


def test_kernel_org_series_follows_the_kernel_being_built(tmp_path):
    """The series directory tracks the target version.

    It was hardcoded to kernel-org-7.1, which was right for exactly as long as
    7.1 was the only target anyone passed. `--target 7.2` would have applied
    the 7.1 series to a 7.2 tree and reported nothing unusual — every patch
    would still succeed or fail on its own merits.
    """
    for v in ("7.1", "7.2"):
        (tmp_path / "patches" / f"kernel-org-{v}").mkdir(parents=True)
    ctx = _FakeCtx(source_dir=tmp_path / "build" / "linux-7.2")
    assert _series_file_for(tmp_path, "kernel-org", ctx) == tmp_path / "patches" / "kernel-org-7.2" / "series"

    ctx = _FakeCtx(source_dir=tmp_path / "build" / "linux-7.1.10")
    assert _series_file_for(tmp_path, "kernel-org", ctx) == tmp_path / "patches" / "kernel-org-7.1" / "series"


def test_source_dir_wins_over_target_flag(tmp_path):
    """source_dir is the tree `git am` will actually run in, so it decides.

    A stale --target left on the command line must not send a 7.2 tree the
    7.1 series.
    """
    (tmp_path / "patches" / "kernel-org-7.2").mkdir(parents=True)
    (tmp_path / "patches" / "kernel-org-7.1").mkdir(parents=True)
    ctx = _FakeCtx(source_dir=tmp_path / "build" / "linux-7.2", target="7.1.10")
    assert _series_file_for(tmp_path, "kernel-org", ctx) == tmp_path / "patches" / "kernel-org-7.2" / "series"


def test_missing_series_directory_is_an_error_not_a_fallback(tmp_path):
    """Falling back to another version's series is the failure mode this
    guards. It must refuse loudly instead."""
    (tmp_path / "patches" / "kernel-org-7.1").mkdir(parents=True)
    ctx = _FakeCtx(source_dir=tmp_path / "build" / "linux-7.9")
    with pytest.raises(SystemExit) as e:
        _series_file_for(tmp_path, "kernel-org", ctx)
    assert "kernel-org-7.9" in str(e.value)


def test_unknown_version_refuses_to_guess(tmp_path):
    ctx = _FakeCtx()
    with pytest.raises(SystemExit):
        _series_file_for(tmp_path, "kernel-org", ctx)


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


# ── kfunc registration macros ─────────────────────────────────────────────────


def test_no_patch_uses_the_pre_6_8_kfunc_set_macro():
    """A kfunc set declared with BTF_SET8_START never registers on this kernel.

    include/linux/btf_ids.h defines BTF_SET8_START(name) as
    __BTF_SET8_START(name, local, 0) -- flags zero -- while the kfunc form,
    BTF_KFUNCS_START, passes BTF_SET8_KFUNCS. register_btf_kfunc_id_set()
    rejects a set without that flag with -EINVAL, and WARNs on top of it when
    kset->owner is NULL, which it is for anything built in.

    Before 6.8 BTF_SET8_START *was* the kfunc macro, so a patch forward-ported
    from an older tree carries it silently: the code compiles, the struct_ops
    half still registers, and only a kfunc call fails, at verifier time. That
    is what 0023 did on every 7.1.10 boot until 2026-08-26.

    Every in-tree kfunc site uses BTF_KFUNCS_START. Nothing we carry should
    use the other one.
    """
    offenders = []
    for patch in sorted((PATCHES_DIR / "custom").glob("*.patch")):
        for lineno, line in enumerate(patch.read_text().splitlines(), 1):
            # Only added source lines. The commit message is allowed to name
            # the macro -- 0023's does, explaining this exact bug.
            if line.startswith("+") and "BTF_SET8_START(" in line:
                offenders.append(f"{patch.name}:{lineno}: {line}")
    assert not offenders, (
        "these patches add a kfunc set that will never register; use "
        "BTF_KFUNCS_START/BTF_KFUNCS_END:\n  " + "\n  ".join(offenders)
    )
