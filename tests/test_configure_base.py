"""Tests for hyphaed/phases/configure.py::_resolve_base_config.

Covers the fallback chain when no exact configs/base/config-<running_kernel>
snapshot exists: same-series match (preferring Canonical *-generic over our
own *-hyphaed), cross-series with confirm(), and the no-candidates miss.
"""
from unittest.mock import patch

from hyphaed.phases.configure import _resolve_base_config


class _FakeProfile:
    def __init__(self, running_kernel):
        self.running_kernel = running_kernel


class _Ctx:
    def __init__(self, tmp_path, running_kernel):
        self.repo_root = tmp_path
        self.profile = _FakeProfile(running_kernel)


def _touch(base_dir, name):
    base_dir.mkdir(parents=True, exist_ok=True)
    (base_dir / name).touch()


def test_exact_match_wins(tmp_path):
    base_dir = tmp_path / "configs" / "base"
    _touch(base_dir, "config-7.1.6-generic")
    _touch(base_dir, "config-7.1.5-070105-generic")

    ctx = _Ctx(tmp_path, "7.1.6-generic")
    result = _resolve_base_config(ctx)
    assert result.name == "config-7.1.6-generic"


def test_same_series_fallback_prefers_generic_over_hyphaed(tmp_path):
    base_dir = tmp_path / "configs" / "base"
    _touch(base_dir, "config-7.1.5-070105-generic")
    _touch(base_dir, "config-7.1.5-hyphaed")
    _touch(base_dir, "config-7.0.0-15-generic")

    ctx = _Ctx(tmp_path, "7.1.6-generic")
    result = _resolve_base_config(ctx)
    assert result.name == "config-7.1.5-070105-generic"


def test_same_series_fallback_picks_highest_at_or_below_target(tmp_path):
    base_dir = tmp_path / "configs" / "base"
    _touch(base_dir, "config-7.1.3-070103-generic")
    _touch(base_dir, "config-7.1.5-070105-generic")

    ctx = _Ctx(tmp_path, "7.1.6-generic")
    result = _resolve_base_config(ctx)
    assert result.name == "config-7.1.5-070105-generic"


def test_cross_series_prompts_and_uses_on_accept(tmp_path):
    base_dir = tmp_path / "configs" / "base"
    _touch(base_dir, "config-7.0.0-15-generic")

    ctx = _Ctx(tmp_path, "8.0.0-generic")
    with patch("hyphaed.phases.configure.confirm", return_value=True) as m:
        result = _resolve_base_config(ctx)
    m.assert_called_once()
    assert result.name == "config-7.0.0-15-generic"


def test_cross_series_refused_returns_missing_path(tmp_path):
    base_dir = tmp_path / "configs" / "base"
    _touch(base_dir, "config-7.0.0-15-generic")

    ctx = _Ctx(tmp_path, "8.0.0-generic")
    with patch("hyphaed.phases.configure.confirm", return_value=False):
        result = _resolve_base_config(ctx)
    assert not result.exists()


def test_no_candidates_returns_missing_path(tmp_path):
    ctx = _Ctx(tmp_path, "7.1.6-generic")
    result = _resolve_base_config(ctx)
    assert not result.exists()
    assert result.name == "config-7.1.6-generic"
