#!/usr/bin/env python3
"""The security phase reported a clean bill of health it had never earned.

Three stacked defects, each of which alone produced "hardening 0 FAIL" on a
config with 111 real failures:

  1. The binary path was wrong, so the checker never ran on any build.
  2. The not-found path returned (0, 0), indistinguishable from a clean pass.
  3. Even once it ran, `check_result == "FAIL"` matched nothing, because the
     tool emits 'FAIL: "y"', 'FAIL: "m"', 'FAIL: "is not set"' and so on.

`configure.py` had run the same tool correctly the whole time (147/258 OK,
111 FAIL), so the two phases disagreed about the same file and nobody noticed
— the wrong one was the reassuring one.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hyphaed.phases import security as S

REPO = Path(__file__).resolve().parent.parent


def test_the_checker_is_actually_found():
    """It lives in bin/. The old constant pointed at the directory twice:
    kernel-hardening-checker/kernel-hardening-checker."""
    found = S._find_hardening_checker()
    if not (REPO / "kernel-hardening-checker").is_dir():
        assert found is None
        return
    assert found is not None, "checker present in the repo but not located"
    assert found.is_file()
    assert found.name == "kernel-hardening-checker"
    assert found.parent.name == "bin"


def test_not_run_is_not_reported_as_zero_failures(monkeypatch, tmp_path):
    """A gate that cannot run must say so. (0, 0) reads as 'checked, clean'."""
    monkeypatch.setattr(S, "_find_hardening_checker", lambda: None)
    fails, oks = S._run_hardening_checker(tmp_path / "config", tmp_path)
    assert fails is None and oks is None, "missing checker must not look like a pass"


def test_fail_results_carry_a_suffix_and_must_still_count(monkeypatch, tmp_path):
    """The real shapes, taken verbatim from a 7.1.9 run."""
    data = [
        {"option_name": "CONFIG_BUG", "check_result": "OK", "check_result_bool": True},
        {"option_name": "CONFIG_WERROR", "check_result": 'FAIL: "is not set"',
         "check_result_bool": False, "desired_val": "y", "reason": "self_protection"},
        {"option_name": "CONFIG_SLAB_MERGE_DEFAULT", "check_result": 'FAIL: "y"',
         "check_result_bool": False, "desired_val": "is not set", "reason": "self_protection"},
        {"option_name": "CONFIG_LSM", "check_result": 'OK: in "landlock,yama"',
         "check_result_bool": True},
        {"option_name": "CONFIG_CFI", "check_result": "FAIL: is not found",
         "check_result_bool": False, "desired_val": "y", "reason": "self_protection"},
    ]

    class _R:
        stdout = json.dumps(data)
        def ok(self): return True

    monkeypatch.setattr(S, "_find_hardening_checker", lambda: Path("/bin/true"))
    monkeypatch.setattr(S, "run", lambda *a, **k: _R())
    fails, oks = S._run_hardening_checker(tmp_path / "config", tmp_path)
    assert fails == 3, f"expected 3 FAILs across suffixed results, got {fails}"
    assert oks == 2
    assert S._run_hardening_checker.__doc__


def test_equality_against_bare_FAIL_would_find_nothing():
    """Pins the root cause, so the old comparison cannot quietly return."""
    real = ['FAIL: "y"', 'FAIL: "m"', 'FAIL: "is not set"', "FAIL: is not found",
            'FAIL: CONFIG_KSTACK_ERASE is not "y"']
    assert not [r for r in real if r == "FAIL"], "the buggy test matched nothing"
    assert len([r for r in real if r.startswith("FAIL")]) == 5
