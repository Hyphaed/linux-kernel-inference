#!/usr/bin/env python3
"""hyphaed/security_scan.py — the new kconfig+cmdline+sysctl scan+waiver tool.

Same discipline test_security_phase.py already established for the
kconfig-only checker invocation, extended to the three-axis combined
invocation and the waiver cross-reference this module adds on top of it.
"""
import json
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hyphaed import security_scan as S

REPO = Path(__file__).resolve().parent.parent


def test_failed_matches_bool_and_suffix_forms():
    assert S._failed({"check_result_bool": False})
    assert not S._failed({"check_result_bool": True})
    # No bool key at all -> falls back to the string-prefix form, same
    # invariant test_security_phase.py pins for the kconfig-only path.
    assert S._failed({"check_result": 'FAIL: "y"'})
    assert not S._failed({"check_result": "OK"})


def test_cmdline_alone_is_not_run_without_a_config(monkeypatch, tmp_path):
    """The real tool errors 'checking cmdline depends on checking Kconfig'
    when -l is passed without -c. Confirmed live 2026-09-22. A cmdline file
    present with no .config must report cmdline as NOT RUN, not silently
    drop the -l flag and misreport."""
    monkeypatch.setattr(S, "_find_hardening_checker", lambda: Path("/bin/true"))
    cmdline_file = tmp_path / "cmdline"
    cmdline_file.write_text("BOOT_IMAGE=/vmlinuz ro quiet\n")

    def _fake_run(cmd, **kw):
        class _R:
            stdout = "[]"
        return _R()
    monkeypatch.setattr(S, "run", _fake_run)

    out_dir = tmp_path / "out"
    results = S.scan_all(None, out_dir, cmdline_path=cmdline_file)
    assert results["cmdline"].ran is False
    assert results["kconfig"].ran is False


def test_scan_all_splits_one_combined_result_by_type(monkeypatch, tmp_path):
    """The checker returns ONE list tagged with `type`, not three separate
    calls — this pins the actual combined-invocation shape."""
    data = [
        {"option_name": "CONFIG_BUG", "type": "kconfig", "check_result": "OK",
         "check_result_bool": True},
        {"option_name": "CONFIG_WERROR", "type": "kconfig", "check_result": 'FAIL: "is not set"',
         "check_result_bool": False},
        {"option_name": "nosmt", "type": "cmdline", "check_result": "FAIL: is not present",
         "check_result_bool": False},
        {"option_name": "kernel.sysrq", "type": "sysctl", "check_result": 'FAIL: "176"',
         "check_result_bool": False},
        {"option_name": "fs.protected_regular", "type": "sysctl", "check_result": "OK",
         "check_result_bool": True},
    ]

    monkeypatch.setattr(S, "_find_hardening_checker", lambda: Path("/bin/true"))

    class _R:
        stdout = json.dumps(data)
    monkeypatch.setattr(S, "run", lambda *a, **k: _R())

    config = tmp_path / "config"
    config.write_text("CONFIG_BUG=y\n")
    cmdline_file = tmp_path / "cmdline"
    cmdline_file.write_text("nosmt\n")

    results = S.scan_all(config, tmp_path / "out", cmdline_path=cmdline_file)
    assert results["kconfig"].ran and len(results["kconfig"].entries) == 2
    assert results["cmdline"].ran and len(results["cmdline"].entries) == 1
    assert results["sysctl"].ran and len(results["sysctl"].entries) == 2


def test_not_run_is_not_a_clean_pass(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "_find_hardening_checker", lambda: None)
    results = S.scan_all(None, tmp_path / "out")
    for mode in ("kconfig", "cmdline", "sysctl"):
        assert results[mode].ran is False


def test_classify_separates_waived_from_unwaived():
    entries = [
        {"option_name": "user.max_user_namespaces", "check_result_bool": False},
        {"option_name": "kernel.sysrq", "check_result_bool": False},
        {"option_name": "CONFIG_BUG", "check_result_bool": True},
    ]
    results = {"sysctl": S.ScanResult("sysctl", entries=entries, ran=True)}
    waivers = {"user.max_user_namespaces": {"reason": "needed by Chrome sandbox"}}
    out = S.classify(results, waivers)
    assert out["by_mode"]["sysctl"]["fail"] == 2
    assert out["by_mode"]["sysctl"]["waived"] == 1
    assert out["by_mode"]["sysctl"]["unwaived"] == 1
    assert len(out["unwaived"]) == 1
    assert out["unwaived"][0]["option_name"] == "kernel.sysrq"


def test_load_waivers_requires_a_reason(tmp_path, monkeypatch):
    """An option_name with no reason is not a waiver — the module docstring
    says so; pin it so a future edit can't accidentally waive by name alone."""
    f = tmp_path / "waivers.yaml"
    f.write_text(textwrap.dedent("""
        waivers:
          - option_name: has_a_reason
            reason: "documented tradeoff"
          - option_name: no_reason_here
        """))
    monkeypatch.setattr(S, "WAIVER_FILE", f)
    waivers = S.load_waivers()
    assert "has_a_reason" in waivers
    assert "no_reason_here" not in waivers


def test_missing_waiver_file_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "WAIVER_FILE", tmp_path / "does-not-exist.yaml")
    assert S.load_waivers() == {}


def test_the_real_waiver_file_parses_and_every_entry_has_a_reason():
    """Guards configs/security-waivers.yaml itself against a future edit
    that adds an entry with no reason (which load_waivers() would then
    silently drop, undermining the whole waiver)."""
    assert S.WAIVER_FILE.exists(), "configs/security-waivers.yaml is missing"
    waivers = S.load_waivers()
    assert len(waivers) > 0
    for name, w in waivers.items():
        assert w.get("reason"), f"{name} has no reason"


def test_propose_sysctl_fixes_only_returns_the_safe_list():
    unwaived = [
        {"option_name": "fs.suid_dumpable"},
        {"option_name": "kernel.sysrq"},  # not in the safe-fix list
    ]
    fixes = S.propose_sysctl_fixes(unwaived)
    assert fixes == {"fs.suid_dumpable": "0"}


def test_apply_sysctl_fixes_dry_run_does_not_write(tmp_path, monkeypatch):
    dropin = tmp_path / "96-hyphaed-hardening.conf"
    monkeypatch.setattr(S, "SYSCTL_DROPIN", dropin)
    changed = S.apply_sysctl_fixes({"fs.suid_dumpable": "0"}, dry_run=True)
    assert changed is True
    assert not dropin.exists()


def test_apply_sysctl_fixes_is_idempotent(tmp_path, monkeypatch):
    dropin = tmp_path / "96-hyphaed-hardening.conf"
    monkeypatch.setattr(S, "SYSCTL_DROPIN", dropin)
    def _fake_run_sudo(cmd, **kw):
        if "input_str" in kw:
            dropin.write_text(kw["input_str"])
        return None
    monkeypatch.setattr(S, "run_sudo", _fake_run_sudo)
    changed = S.apply_sysctl_fixes({"fs.suid_dumpable": "0"}, dry_run=False)
    assert changed is True
    assert "fs.suid_dumpable = 0" in dropin.read_text()

    # Second call with identical content: nothing to write, reports no change.
    changed_again = S.apply_sysctl_fixes({"fs.suid_dumpable": "0"}, dry_run=False)
    assert changed_again is False


def test_propose_cmdline_diff_formats_tokens():
    unwaived = [
        {"option_name": "slab_nomerge", "desired_val": "is present"},
        {"option_name": "page_alloc.shuffle", "desired_val": "1"},
        {"option_name": "spectre_v2", "desired_val": "is not off"},
    ]
    tokens = S.propose_cmdline_diff(unwaived)
    assert "slab_nomerge" in tokens
    assert "page_alloc.shuffle=1" in tokens
    # "is not off" is a negative-form constraint on an existing kernel
    # param, not something to append as a bare token.
    assert not any(t.startswith("spectre_v2") for t in tokens)
