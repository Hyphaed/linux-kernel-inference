"""diagnostics/perf_tune.py: the pieces that decide what gets kept.

Pure functions only. Nothing here writes to /proc, /sys or /etc.
"""
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("perf_tune", REPO / "diagnostics" / "perf_tune.py")
P = importlib.util.module_from_spec(spec)
sys.modules["perf_tune"] = P
spec.loader.exec_module(P)


def reps(*vals):
    return list(vals)


def test_only_two_knobs_are_adopted_and_the_rest_need_an_ab():
    """The research file's verdicts: A1 and A2 adopted, everything else measured first."""
    assert P.ADOPT == ["nmi_watchdog", "cuda_cache"]
    assert "nmi_watchdog" not in P.MEASURE and len(P.MEASURE) == 10


def test_registry_never_touches_firewall_forwarding_or_mitigations():
    for knob in P.KNOBS.values():
        blob = " ".join(p for p, _ in knob.targets()) + knob.name + knob.proposed()
        assert not any(bad in blob.lower() for bad in P.FORBIDDEN), knob.name


def test_a_win_must_clear_the_noise_floor():
    base_a = {"seq_read_cold_MBps": reps(1000, 1005, 995)}
    base_b = {"seq_read_cold_MBps": reps(1002, 998, 1000)}
    clear = {"seq_read_cold_MBps": reps(1100, 1105, 1095)}       # +10%, noise ~0.5%
    row = P.compare(base_a, clear, base_b)[0]
    assert row["verdict"] == "better" and row["gain_pct"] > 9


def test_a_gain_inside_the_noise_is_not_a_win():
    noisy = {"seq_read_cold_MBps": reps(800, 1200, 1000)}        # +-20% spread
    test = {"seq_read_cold_MBps": reps(1080, 1070, 1090)}        # +8%, below 2x noise
    row = P.compare(noisy, test, noisy)[0]
    assert row["verdict"] == "no change"


def test_drift_between_the_two_baselines_raises_the_floor():
    a = {"stream_bogo_s": reps(1000, 1000, 1000)}
    b = {"stream_bogo_s": reps(900, 900, 900)}                   # box drifted 10% between baselines
    t = {"stream_bogo_s": reps(1040, 1040, 1040)}                # +9.5% over mean baseline
    assert P.compare(a, t, b)[0]["verdict"] == "no change"


def test_lower_is_better_metrics_flip_the_sign():
    a = {"dirty_read_p99_us": reps(1000, 1000, 1000)}
    faster = {"dirty_read_p99_us": reps(500, 500, 500)}
    slower = {"dirty_read_p99_us": reps(2000, 2000, 2000)}
    assert P.compare(a, faster, a)[0]["verdict"] == "better"
    assert P.compare(a, slower, a)[0]["verdict"] == "worse"


def test_overall_verdict_wording():
    row = lambda v: {"verdict": v}
    assert P.overall([row("better"), row("no change")]).startswith("WIN")
    assert P.overall([row("worse")]).startswith("LOSS")
    assert P.overall([row("better"), row("worse")]).startswith("MIXED")
    assert P.overall([row("no change")]).startswith("NO MEASURABLE")


def test_missing_metrics_are_skipped_not_zeroed():
    assert P.compare({}, {}, {}) == []


def test_sysctl_paths_become_sysctl_keys_and_sysfs_paths_do_not():
    assert P.sysctl_key("/proc/sys/vm/dirty_bytes") == "vm.dirty_bytes"
    assert P.sysctl_key("/sys/block/nvme0n1/queue/read_ahead_kb") is None


def test_dropin_renderers():
    assert "vm.dirty_bytes = 268435456" in P.render_sysctl({"vm.dirty_bytes": "268435456"})
    line = "w- /sys/kernel/mm/transparent_hugepage/hugepages-64kB/enabled - - - - inherit"
    assert line in P.render_tmpfiles({"/sys/kernel/mm/transparent_hugepage/hugepages-64kB/enabled": "inherit"})
    assert "CUDA_CACHE_MAXSIZE=4294967296" in P.render_env({"CUDA_CACHE_MAXSIZE": "4294967296"})
    assert P.HEADER in P.render_sysctl({})


def test_read_val_collapses_bracketed_selection(tmp_path):
    f = tmp_path / "scheduler"
    f.write_text("[none] mq-deadline\n")
    assert P.read_val(str(f)) == "none"
    assert P.available_choices(str(f)) == ["none", "mq-deadline"]
    f.write_text("always inherit madvise [never]\n")
    assert P.read_val(str(f)) == "never"
    assert P.read_val(str(tmp_path / "missing")) is None


def test_dirty_bytes_snapshot_also_captures_the_ratios_it_zeroes():
    paths = P.KNOBS["dirty_bytes"].extra_restore()
    assert "/proc/sys/vm/dirty_ratio" in paths and "/proc/sys/vm/dirty_background_ratio" in paths


def test_unloaded_scheduler_with_a_module_counts_as_available(tmp_path, monkeypatch):
    """kyber isn't in the scheduler file until first use, but its module exists."""
    f = tmp_path / "scheduler"
    f.write_text("[none] mq-deadline\n")

    class R:
        returncode = 0
    monkeypatch.setattr(P, "run", lambda cmd, timeout=None: R())
    assert P.scheduler_available(str(f), "kyber")
    R.returncode = 1
    assert not P.scheduler_available(str(f), "kyber")
    assert P.scheduler_available(str(f), "mq-deadline")      # already listed


def test_preempt_mode_is_read_from_the_parenthesised_entry(tmp_path):
    f = tmp_path / "preempt"
    f.write_text("(full) lazy\n")
    k = P.PreemptLazy(str(f))
    assert k.get() == "full" and k.available()
    f.write_text("full (lazy)\n")
    assert k.get() == "lazy"
    assert P.PreemptLazy(str(tmp_path / "missing")).available() is False      # debugfs unreadable as user


def test_rapl_pl1_target_comes_from_the_hardware_maximum(tmp_path):
    for name, val in (("constraint_0_name", "long_term"), ("constraint_0_power_limit_uw", "65000000"),
                      ("constraint_0_max_power_uw", "125000000")):
        (tmp_path / name).write_text(val)
    k = P.RaplPl1(str(tmp_path))
    assert k.available() and k.get() == 65000000
    k.set()
    assert (tmp_path / "constraint_0_power_limit_uw").read_text() == "125000000"
    k.restore(65000000)
    assert k.get() == 65000000


def test_rapl_pl1_unavailable_without_the_interface(tmp_path):
    assert P.RaplPl1(str(tmp_path / "nope")).available() is False


def test_ab_with_scx_off_restores_the_scheduler_even_when_the_run_fails(monkeypatch):
    calls = []

    class FakeScx:
        def get(self): return "active"
        def set(self): calls.append("stop")
        def restore(self, orig): calls.append(f"restore:{orig}")

    def boom(a): raise RuntimeError("bench failed")
    monkeypatch.setattr(P, "ScxOff", FakeScx)
    monkeypatch.setattr(P, "need_root", lambda: None)
    monkeypatch.setattr(P, "_cmd_ab", boom)

    class A: scx_off = True
    try:
        P.cmd_ab(A())
    except RuntimeError:
        pass
    assert calls == ["stop", "restore:active"]


def test_sysctl_dropin_loads_after_greenboosts_which_resets_dirty_bytes():
    """GreenBoost's 99-zzz-greenboost.conf sets vm.dirty_ratio, which zeroes vm.dirty_bytes."""
    assert P.SYSCTL_DROPIN.name > "99-zzz-greenboost.conf"
    assert P.SYSCTL_DROPIN.name.endswith(".conf")


def test_dry_run_writes_nothing(tmp_path, capsys):
    target = tmp_path / "knob"
    target.write_text("1")
    P.DRY = True
    try:
        P.write_val(str(target), "0")
    finally:
        P.DRY = False
    assert target.read_text() == "1" and "dry-run" in capsys.readouterr().out
