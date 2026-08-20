"""Tests for mainline preflight functions in hyphaed/phases/source.py."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hyphaed.phases.source import (
    _latest_mainline_stable,
    _mainline_available_tags,
)


# ── _latest_mainline_stable ────────────────────────────────────────────────────

def test_latest_stable_picks_highest_version():
    tags = ["v7.0", "v7.1", "v7.1.1", "v7.0.6"]
    assert _latest_mainline_stable(tags) == "v7.1.1"


def test_latest_stable_excludes_rc_tags():
    tags = ["v7.1.1", "v7.2-rc1", "v7.2-rc3"]
    assert _latest_mainline_stable(tags) == "v7.1.1"


def test_latest_stable_all_rc_returns_none():
    tags = ["v7.2-rc1", "v7.2-rc2"]
    assert _latest_mainline_stable(tags) is None


def test_latest_stable_empty_list_returns_none():
    assert _latest_mainline_stable([]) is None


def test_latest_stable_single_stable():
    assert _latest_mainline_stable(["v7.0.6"]) == "v7.0.6"


def test_latest_stable_major_minor_only():
    # Some mainline tags omit the patch component, e.g. "v7.1" (first release)
    tags = ["v7.0", "v7.1", "v6.12"]
    assert _latest_mainline_stable(tags) == "v7.1"


def test_latest_stable_handles_multi_digit_minor():
    tags = ["v6.9", "v6.10", "v6.11", "v7.0"]
    assert _latest_mainline_stable(tags) == "v7.0"


# ── _mainline_available_tags HTML parsing ──────────────────────────────────────

def _fake_html(tags: list[str]) -> str:
    links = "\n".join(f'<a href="{t}/">{t}</a>' for t in tags)
    return f"<html><body>{links}</body></html>"


def test_parse_tags_from_html_mock(monkeypatch):
    """Monkeypatch run() so _mainline_available_tags parses our synthetic HTML."""
    from unittest.mock import MagicMock
    import hyphaed.phases.source as src_mod

    html = _fake_html(["v7.0", "v7.1", "v7.1.1", "v7.2-rc1"])
    mock_result = MagicMock()
    mock_result.ok.return_value = True
    mock_result.stdout = html
    monkeypatch.setattr(src_mod, "run", lambda *a, **kw: mock_result)

    tags = _mainline_available_tags()
    assert "v7.1.1" in tags
    assert "v7.2-rc1" in tags
    assert "v7.0" in tags


def test_parse_tags_returns_empty_on_network_error(monkeypatch):
    from unittest.mock import MagicMock
    import hyphaed.phases.source as src_mod

    mock_result = MagicMock()
    mock_result.ok.return_value = False
    monkeypatch.setattr(src_mod, "run", lambda *a, **kw: mock_result)

    assert _mainline_available_tags() == []


def test_latest_stable_derived_from_html(monkeypatch):
    from unittest.mock import MagicMock
    import hyphaed.phases.source as src_mod

    html = _fake_html(["v7.0.6", "v7.1", "v7.1.1", "v7.2-rc2"])
    mock_result = MagicMock()
    mock_result.ok.return_value = True
    mock_result.stdout = html
    monkeypatch.setattr(src_mod, "run", lambda *a, **kw: mock_result)

    latest = _latest_mainline_stable()
    assert latest == "v7.1.1"


# ── _mainline_has_amd64_build ──────────────────────────────────────────────────

def test_has_amd64_build_true_when_deb_present(monkeypatch):
    from unittest.mock import MagicMock
    import hyphaed.phases.source as src_mod

    mock_result = MagicMock()
    mock_result.ok.return_value = True
    mock_result.stdout = (
        '<a href="linux-modules-7.1.1-070101-generic_7.1.1-070101.202506251801_amd64.deb">'
        "linux-modules-7.1.1-070101-generic_7.1.1-070101.202506251801_amd64.deb</a>"
    )
    monkeypatch.setattr(src_mod, "run", lambda *a, **kw: mock_result)

    from hyphaed.phases.source import _mainline_has_amd64_build
    assert _mainline_has_amd64_build("v7.1.1") is True


def test_has_amd64_build_false_when_absent(monkeypatch):
    from unittest.mock import MagicMock
    import hyphaed.phases.source as src_mod

    mock_result = MagicMock()
    mock_result.ok.return_value = True
    mock_result.stdout = "<html><body>no debs here</body></html>"
    monkeypatch.setattr(src_mod, "run", lambda *a, **kw: mock_result)

    from hyphaed.phases.source import _mainline_has_amd64_build
    assert _mainline_has_amd64_build("v9.9.9") is False


def test_has_amd64_build_false_on_404(monkeypatch):
    from unittest.mock import MagicMock
    import hyphaed.phases.source as src_mod

    mock_result = MagicMock()
    mock_result.ok.return_value = False
    monkeypatch.setattr(src_mod, "run", lambda *a, **kw: mock_result)

    from hyphaed.phases.source import _mainline_has_amd64_build
    assert _mainline_has_amd64_build("v9.9.9") is False


def test_has_amd64_build_normalizes_tag_without_v_prefix(monkeypatch):
    """_mainline_has_amd64_build("7.1.1") should prepend "v" internally."""
    calls: list[str] = []
    from unittest.mock import MagicMock
    import hyphaed.phases.source as src_mod

    def fake_run(cmd, *a, **kw):
        calls.append(" ".join(str(c) for c in cmd))
        m = MagicMock()
        m.ok.return_value = False
        m.stdout = ""
        return m

    monkeypatch.setattr(src_mod, "run", fake_run)

    from hyphaed.phases.source import _mainline_has_amd64_build
    _mainline_has_amd64_build("7.1.1")
    assert any("v7.1.1" in c for c in calls), f"expected v7.1.1 in calls: {calls}"


# ── _nearest_mainline_tag_in_series ─────────────────────────────────────────────

def test_nearest_in_series_picks_newest_built_at_or_below_target(monkeypatch):
    import hyphaed.phases.source as src_mod
    from hyphaed.phases.source import _nearest_mainline_tag_in_series

    monkeypatch.setattr(src_mod, "_mainline_available_tags",
                         lambda: ["v7.1.1", "v7.1.3", "v7.1.5", "v7.1.6", "v7.0.9"])
    monkeypatch.setattr(src_mod, "_mainline_has_amd64_build", lambda t: t != "v7.1.6")

    assert _nearest_mainline_tag_in_series("v7.1.6") == "v7.1.5"


def test_nearest_in_series_excludes_rc_tags(monkeypatch):
    import hyphaed.phases.source as src_mod
    from hyphaed.phases.source import _nearest_mainline_tag_in_series

    monkeypatch.setattr(src_mod, "_mainline_available_tags",
                         lambda: ["v7.1.4", "v7.1.5-rc1"])
    monkeypatch.setattr(src_mod, "_mainline_has_amd64_build", lambda t: True)

    assert _nearest_mainline_tag_in_series("v7.1.5") == "v7.1.4"


def test_nearest_in_series_none_when_series_absent(monkeypatch):
    import hyphaed.phases.source as src_mod
    from hyphaed.phases.source import _nearest_mainline_tag_in_series

    monkeypatch.setattr(src_mod, "_mainline_available_tags", lambda: ["v7.0.9", "v8.0.0"])
    monkeypatch.setattr(src_mod, "_mainline_has_amd64_build", lambda t: True)

    assert _nearest_mainline_tag_in_series("v7.1.6") is None


def test_nearest_in_series_none_when_none_built(monkeypatch):
    import hyphaed.phases.source as src_mod
    from hyphaed.phases.source import _nearest_mainline_tag_in_series

    monkeypatch.setattr(src_mod, "_mainline_available_tags", lambda: ["v7.1.1", "v7.1.6"])
    monkeypatch.setattr(src_mod, "_mainline_has_amd64_build", lambda t: False)

    assert _nearest_mainline_tag_in_series("v7.1.6") is None
