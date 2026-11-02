"""series.experimental is opt-in and must not change the default patch list (ADR-009)."""
from types import SimpleNamespace

from hyphaed.phases import patch


def _mk(tmp_path, exp_lines):
    (tmp_path / "a.patch").write_text("x")
    (tmp_path / "b.patch").write_text("x")
    (tmp_path / "series").write_text("a.patch\n")
    if exp_lines is not None:
        (tmp_path / "series.experimental").write_text(exp_lines)
    return tmp_path / "series"


def test_default_ignores_experimental(tmp_path):
    s = _mk(tmp_path, "b.patch\n")
    assert [p.name for p in patch._read_full_series(s, False)] == ["a.patch"]


def test_opt_in_appends_after_default(tmp_path):
    s = _mk(tmp_path, "# c\nb.patch\n")
    assert [p.name for p in patch._read_full_series(s, True)] == ["a.patch", "b.patch"]


def test_missing_experimental_file_is_empty(tmp_path):
    s = _mk(tmp_path, None)
    assert [p.name for p in patch._read_full_series(s, True)] == ["a.patch"]


def test_flag_and_env(monkeypatch):
    monkeypatch.delenv("HYPHAED_EXPERIMENTAL", raising=False)
    assert not patch._experimental_enabled(SimpleNamespace(args=SimpleNamespace(experimental=False)))
    assert patch._experimental_enabled(SimpleNamespace(args=SimpleNamespace(experimental=True)))
    monkeypatch.setenv("HYPHAED_EXPERIMENTAL", "1")
    assert patch._experimental_enabled(SimpleNamespace(args=SimpleNamespace()))


def test_shipped_experimental_file_has_no_missing_patches():
    from pathlib import Path
    f = Path(__file__).resolve().parent.parent / "patches" / "kernel-org-7.2" / "series.experimental"
    for ln in f.read_text().splitlines():
        if ln.strip() and not ln.startswith("#"):
            assert (f.parent / ln.strip()).exists(), ln
