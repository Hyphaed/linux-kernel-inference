"""HYPHAED_SERIES_VARIANT picks series.<name> beside the default one (ADR-009); sched73 must stay coherent."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyphaed.phases import patch

ROOT = Path(__file__).resolve().parent.parent
D72 = ROOT / "patches" / "kernel-org-7.2"


def _ctx():
    return SimpleNamespace(source_dir=Path("build/linux-7.2.15"), args=SimpleNamespace(target="7.2"))


def _entries(path):
    return [ln.strip() for ln in path.read_text().splitlines() if ln.strip() and not ln.strip().startswith("#")]


def test_default_is_plain_series(monkeypatch):
    monkeypatch.delenv("HYPHAED_SERIES_VARIANT", raising=False)
    assert patch._series_file_for(ROOT, "kernel-org", _ctx()) == D72 / "series"


def test_variant_selects_its_file(monkeypatch):
    monkeypatch.setenv("HYPHAED_SERIES_VARIANT", "sched73")
    assert patch._series_file_for(ROOT, "kernel-org", _ctx()) == D72 / "series.sched73"


def test_unknown_variant_is_an_error_not_a_silent_fallback(monkeypatch):
    monkeypatch.setenv("HYPHAED_SERIES_VARIANT", "nope")
    with pytest.raises(SystemExit):
        patch._series_file_for(ROOT, "kernel-org", _ctx())


def test_sched73_entries_exist_and_swap_bore_after_the_transplant():
    e = _entries(D72 / "series.sched73")
    for name in e:
        assert (D72 / name).resolve().is_file(), name
    assert [x.split("/")[-1][:4] for x in e[:3]] == ["0036", "0037", "0038"]
    assert e[3].endswith("0001-cachyos-bore-7.3-on-7.2.patch")
    assert not any("cachyos-bore-7.2" in x for x in e), "7.2 BORE must not be stacked on the 7.3 fair class"


def test_sched73_keeps_the_rest_of_the_default_series():
    default = [x for x in _entries(D72 / "series") if "cachyos-bore" not in x]
    variant = [x for x in _entries(D72 / "series.sched73") if x.split("/")[-1][:4] not in {"0036", "0037", "0038"} and "cachyos-bore" not in x]
    assert variant == default


def test_experimental_backports_are_not_in_the_transplant_variant_by_default():
    # 0031-0035 are already contained in 0036; the variant file itself must not list them
    assert not any(x.split("/")[-1][:4] in {"0031", "0032", "0033", "0034", "0035"} for x in _entries(D72 / "series.sched73"))
