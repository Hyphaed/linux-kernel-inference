"""_check_nvidia_driver prefers kmd_version and falls back to the deprecated driver_version."""
from types import SimpleNamespace

from hyphaed.phases import detect
from hyphaed.util import run as run_mod
from hyphaed.util.run import RunResult


def _profile():
    return SimpleNamespace(gpu_vendor="nvidia", running_kernel="7.2.12-hyphaed")


def _fake_run(answers, seen):
    def fake(cmd, **kw):
        field = cmd[1].split("=")[1]
        seen.append(field)
        code, out = answers[field]
        return RunResult(code, out, "", " ".join(cmd))
    return fake


def test_kmd_version_used_first(monkeypatch, caplog):
    seen = []
    monkeypatch.setattr(run_mod, "run", _fake_run({"kmd_version": (0, "615.71.09\n")}, seen))
    detect._check_nvidia_driver(_profile())
    assert seen == ["kmd_version"]


def test_falls_back_when_kmd_field_unknown(monkeypatch):
    seen = []
    answers = {
        "kmd_version": (2, 'Field "kmd_version" is not a valid field to query.\n'),
        "driver_version": (0, "570.10.01\n"),
    }
    monkeypatch.setattr(run_mod, "run", _fake_run(answers, seen))
    detect._check_nvidia_driver(_profile())
    assert seen == ["kmd_version", "driver_version"]


def test_error_text_with_exit_zero_is_not_a_version(monkeypatch):
    seen = []
    answers = {
        "kmd_version": (0, 'Field "kmd_version" is not a valid field to query.\n'),
        "driver_version": (0, "570.10.01\n"),
    }
    monkeypatch.setattr(run_mod, "run", _fake_run(answers, seen))
    detect._check_nvidia_driver(_profile())
    assert seen == ["kmd_version", "driver_version"]
