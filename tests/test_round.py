"""The two-run flow: mode detection, capture bookkeeping, A/B report honesty. No root, no build."""
import json
from pathlib import Path

from hyphaed import abreport, round as rnd, workload


def test_mode_detection():
    assert rnd.detect_mode("7.2.9-hyphaed", "7.2.10") == "run1"
    assert rnd.detect_mode("7.2.10-hyphaed", "7.2.10") == "run2"
    assert rnd.detect_mode("7.2.10", "7.2.10") == "run2"
    # 7.2.1 must not read as 7.2.10 and vice versa
    assert rnd.detect_mode("7.2.1-hyphaed", "7.2.10") == "run1"
    assert rnd.detect_mode("7.2.10-hyphaed", "7.2.1") == "run1"


def test_default_target_is_the_bump():
    from hyphaed.cli import build_parser
    assert build_parser().parse_args([]).target == "7.2.15"


def test_spread_carries_n_and_empty_is_not_zero():
    assert workload.spread([]) == {"n": 0}
    s = workload.spread([1.0, 2.0, 3.0, 10.0])
    assert s["n"] == 4 and s["min"] == 1.0 and s["max"] == 10.0 and s["median"] == 2.5


def test_pass_latency_parses_log(tmp_path, monkeypatch):
    log = tmp_path / "engine-run.log"
    log.write_text(
        '2026-10-12 01:38:28,517 INFO [v] invest.ultra: ultra_stage_timing {"pass": "ultra", "stages": {"ctx": 1.6, "shadow": 0.1, "tick:lab1": 0.05}}\n'
        'noise line\n'
        '2026-10-12 01:39:00,748 INFO [v] invest.ultra: ultra_stage_timing {"pass": "ultra", "stages": {"ctx": 30.0, "shadow": 0.1}}\n')
    monkeypatch.setattr(workload, "ENGINE_LOG", log)
    r = workload.pass_latency()
    assert r["status"] == "ok" and r["passes"] == 2 and r["over_20s"] == 1
    assert r["ctx_s"]["max"] == 30.0


def test_missing_sources_say_not_run_never_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(workload, "ENGINE_LOG", tmp_path / "nope.log")
    monkeypatch.setattr(workload, "FUNDING_DIR", tmp_path / "nofunding")
    monkeypatch.setattr(workload, "GB_STATUS", tmp_path / "nostatus")
    assert workload.pass_latency()["status"] == "not_run"
    assert workload.funding_file_cost()["status"] == "not_run"
    assert workload.greenboost_status()["status"] == "not_run"
    assert workload.tok_s(None)["status"] == "not_run"


def test_tok_s_never_runs_with_lease_held(tmp_path, monkeypatch):
    lease = tmp_path / "lease"
    lease.write_text("pid")
    monkeypatch.setattr(workload, "LEASE", lease)
    r = workload.tok_s("some-model")
    assert r["status"] == "not_run" and "lease" in r["reason"]


def test_cold_read_only_touches_funding_files(tmp_path, monkeypatch):
    (tmp_path / "BTC.json").write_text("[]" * 1000)
    monkeypatch.setattr(workload, "FUNDING_DIR", tmp_path)
    r = workload.cold_funding_read()
    assert r["status"] == "ok" and r["bytes"] == 2000


def _cap(kernel, hour, total_med, pressure=True):
    c = {"meta": {"kernel": kernel, "hour_local": hour},
         "pass_latency": {"status": "ok", "total_s": {"median": total_med, "p95": 9.0}, "ctx_s": {"p95": 5.0}, "over_20s": 1},
         "tok_s_700": {"status": "not_run", "reason": "no --model named"},
         "root_traces": {"runqlat": {"status": "not_run", "reason": "no sudo"}}}
    if pressure:
        c["pressure"] = {"status": "ok", "loadavg1": {"median": 2.0}, "psi_avg10": {}, "vmstat_delta": {"allocstall_normal": 0}}
    return c


def test_report_lists_not_run_and_flags_hour_mismatch():
    md = abreport.render(_cap("7.2.9-hyphaed", 9, 2.6), _cap("7.2.10-hyphaed", 22, 2.4, pressure=False))
    assert "5.7%" in md
    assert "NOT RUN" in md                      # B had no pressure data
    assert "no --model named" in md and "root_traces.runqlat" in md
    assert "Different hours" in md


def test_report_write_roundtrip(tmp_path):
    for n, k in (("a", "7.2.9-hyphaed"), ("b", "7.2.10-hyphaed")):
        (tmp_path / n).mkdir()
        (tmp_path / n / "capture.json").write_text(json.dumps(_cap(k, 9, 2.5)))
    dest = abreport.write(tmp_path / "a", tmp_path / "b", tmp_path / "ab.md")
    assert "# A/B: 7.2.9-hyphaed vs 7.2.10-hyphaed" in dest.read_text()


def test_dry_run_prebuild_mutates_nothing(tmp_path):
    rc = rnd.run1_prebuild(tmp_path, "7.2.10", running="7.2.9-hyphaed", skip_baseline=False, cold=False,
                           model=None, dry_run=True)
    assert rc == 0 and not list(tmp_path.iterdir())


def test_retarget_invalidates_stale_phase_cache(tmp_path, monkeypatch):
    from hyphaed.phases import ORDER
    from hyphaed.state import PersistedState
    monkeypatch.setattr(rnd.os, "uname", lambda: type("U", (), {"nodename": "box"})())
    d = tmp_path / "state" / "machines" / "box"
    st = PersistedState.load(d)
    st.kernel_pkgver = "7.2.9-1+rptr-nvbw-wl"
    for n in ORDER:
        st.mark_complete(n, 1.0)
    st.save(d)
    rnd._invalidate_if_retargeted(tmp_path, "7.2.10")
    assert PersistedState.load(d).phases_completed == ["detect"]
