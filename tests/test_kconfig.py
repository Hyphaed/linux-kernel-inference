from pathlib import Path
from hyphaed.kconfig import (
    parse_config, validate,
    GREENBOOST_FLOOR_Y, HYPHAED_REQUIRED_Y,
    UBUNTU_BOOT_FLOOR_Y, UBUNTU_BOOT_FLOOR_M_OR_Y,
)

# Minimal set of keys needed to pass validate() regardless of with_greenboost.
_UBUNTU_BOOT_LINES = (
    [f"{k}=y" for k in UBUNTU_BOOT_FLOOR_Y]
    + [f"{k}=m" for k in UBUNTU_BOOT_FLOOR_M_OR_Y]
)
# CONFIG_KVM=m reflects the fragment default; validator now accepts m or y.
_HYPHAED_LINES = [f"{k}=y" for k in HYPHAED_REQUIRED_Y] + ["CONFIG_KVM=m"]


def _write_config(tmp_path: Path, lines: list[str]) -> Path:
    p = tmp_path / ".config"
    p.write_text("\n".join(lines) + "\n")
    return p


def test_parse_config_handles_y_n_m_strings(tmp_path):
    p = _write_config(tmp_path, [
        "CONFIG_FOO=y",
        "CONFIG_BAR=m",
        "# CONFIG_BAZ is not set",
        'CONFIG_QUX="some string"',
        "CONFIG_NUM=42",
    ])
    cfg = parse_config(p)
    assert cfg["CONFIG_FOO"] == "y"
    assert cfg["CONFIG_BAR"] == "m"
    assert cfg["CONFIG_BAZ"] == "n"
    assert cfg["CONFIG_QUX"] == '"some string"'
    assert cfg["CONFIG_NUM"] == "42"


def test_validate_passes_when_floor_is_y(tmp_path):
    lines = (
        [f"{k}=y" for k in GREENBOOST_FLOOR_Y]
        + _UBUNTU_BOOT_LINES
        + _HYPHAED_LINES
    )
    p = _write_config(tmp_path, lines)
    r = validate(parse_config(p))
    assert r.ok, r.missing


def test_validate_fails_when_dmabuf_demoted(tmp_path):
    lines = (
        [f"{k}=y" for k in GREENBOOST_FLOOR_Y]
        + _UBUNTU_BOOT_LINES
        + _HYPHAED_LINES
    )
    p = _write_config(tmp_path, lines)
    # Kernel 7.0 renamed DMA_BUF → DMA_SHARED_BUFFER; test the live option.
    p.write_text(p.read_text().replace("CONFIG_DMA_SHARED_BUFFER=y", "CONFIG_DMA_SHARED_BUFFER=m"))
    r = validate(parse_config(p))
    assert not r.ok
    assert any("CONFIG_DMA_SHARED_BUFFER" in m for m in r.missing)


def test_validate_fails_when_kvm_disabled(tmp_path):
    lines = _UBUNTU_BOOT_LINES + [l for l in _HYPHAED_LINES if "CONFIG_KVM" not in l]
    lines.append("# CONFIG_KVM is not set")
    p = _write_config(tmp_path, lines)
    r = validate(parse_config(p), with_greenboost=False)
    assert not r.ok
    assert any("CONFIG_KVM" in m for m in r.missing)


def test_validate_can_skip_greenboost(tmp_path):
    p = _write_config(tmp_path, _UBUNTU_BOOT_LINES + _HYPHAED_LINES)
    r = validate(parse_config(p), with_greenboost=False)
    assert r.ok, r.missing


def test_validate_fails_when_rd_zstd_missing(tmp_path):
    lines = _UBUNTU_BOOT_LINES + _HYPHAED_LINES
    p = _write_config(tmp_path, [l for l in lines if "RD_ZSTD" not in l])
    r = validate(parse_config(p), with_greenboost=False)
    assert not r.ok
    assert any("CONFIG_RD_ZSTD" in m for m in r.missing)


def test_validate_fails_when_devtmpfs_mount_missing(tmp_path):
    lines = _UBUNTU_BOOT_LINES + _HYPHAED_LINES
    p = _write_config(tmp_path, [l for l in lines if "DEVTMPFS_MOUNT" not in l])
    r = validate(parse_config(p), with_greenboost=False)
    assert not r.ok
    assert any("CONFIG_DEVTMPFS_MOUNT" in m for m in r.missing)


def test_validate_fails_when_ai_gpu_mem_demoted(tmp_path):
    lines = (
        [f"{k}=y" for k in GREENBOOST_FLOOR_Y]
        + _UBUNTU_BOOT_LINES
        + _HYPHAED_LINES
    )
    p = _write_config(tmp_path, lines)
    p.write_text(p.read_text().replace("CONFIG_HMM_MIRROR=y", "# CONFIG_HMM_MIRROR is not set"))
    r = validate(parse_config(p))
    assert not r.ok
    assert any("CONFIG_HMM_MIRROR" in m for m in r.missing)


def test_validate_fails_when_overlay_fs_disabled(tmp_path):
    lines = [l for l in _UBUNTU_BOOT_LINES if "OVERLAY_FS" not in l]
    lines += ["# CONFIG_OVERLAY_FS is not set"] + _HYPHAED_LINES
    p = _write_config(tmp_path, lines)
    r = validate(parse_config(p), with_greenboost=False)
    assert not r.ok
    assert any("CONFIG_OVERLAY_FS" in m for m in r.missing)
