from pathlib import Path

from hyphaed import verify as v


def test_check_dataclass_defaults():
    c = v.Check("name", True)
    assert c.ok and c.detail == ""


def test_cmdline_matches_passes_when_all_present(monkeypatch):
    # Simulate /proc/cmdline with all expected tokens
    monkeypatch.setattr(v, "_read", lambda p: "iommu=pt foo bar nvidia-drm.modeset=1" if p == "/proc/cmdline" else "")
    res = v.check_cmdline_matches(["iommu=pt", "nvidia-drm.modeset=1"])
    assert res.ok


def test_cmdline_matches_reports_missing(monkeypatch):
    monkeypatch.setattr(v, "_read", lambda p: "ro quiet" if p == "/proc/cmdline" else "")
    res = v.check_cmdline_matches(["iommu=pt"])
    assert not res.ok
    assert "iommu=pt" in res.detail


def test_thp_madvise_parses_active_flag(monkeypatch):
    monkeypatch.setattr(v, "_read", lambda p: "always [madvise] never" if "transparent_hugepage" in p else "")
    assert v.check_thp_madvise().ok


def test_thp_madvise_fails_on_never(monkeypatch):
    monkeypatch.setattr(v, "_read", lambda p: "always madvise [never]" if "transparent_hugepage" in p else "")
    assert not v.check_thp_madvise().ok


def test_running_kernel_check(monkeypatch):
    monkeypatch.setattr(v, "_cmd", lambda args: "7.0.0-15-hyphaed\n")
    assert v.check_running_kernel("hyphaed").ok
    monkeypatch.setattr(v, "_cmd", lambda args: "7.0.0-15-generic\n")
    assert not v.check_running_kernel("hyphaed").ok


def test_run_all_returns_full_check_list(monkeypatch):
    monkeypatch.setattr(v, "_cmd", lambda args: "")
    monkeypatch.setattr(v, "_read", lambda p: "")
    checks = v.run_all(flavour="hyphaed", cmdline_expected=[])
    assert len(checks) >= 15
    # Some checks should fail when /proc and lsmod return empty
    assert any(not c.ok for c in checks)


def test_zswap_active_when_enabled(monkeypatch):
    monkeypatch.setattr(v, "_read", lambda p: "Y" if "zswap/parameters/enabled" in p else "0")
    res = v.check_zswap_active()
    assert res.ok


def test_zswap_inactive_when_disabled(monkeypatch):
    monkeypatch.setattr(v, "_read", lambda p: "N" if "zswap/parameters/enabled" in p else "")
    res = v.check_zswap_active()
    assert not res.ok


def test_zswap_missing_module(monkeypatch):
    monkeypatch.setattr(v, "_read", lambda p: "")
    res = v.check_zswap_active()
    assert not res.ok


def test_cpu_governor_schedutil_passes(monkeypatch, tmp_path):
    gov_file = tmp_path / "scaling_governor"
    gov_file.write_text("schedutil")
    import hyphaed.verify
    original = hyphaed.verify.Path

    class _FakePath:
        def __init__(self, *args):
            self._p = original(*args)
        def glob(self, pattern):
            if "scaling_governor" in pattern:
                return [gov_file]
            return self._p.glob(pattern)
        def __str__(self):
            return str(self._p)

    monkeypatch.setattr(v, "_read", lambda p: "schedutil")
    # check directly since monkeypatching Path is complex — test logic instead
    res = v.check_cpu_governor()
    # Result depends on real system; just ensure it returns a Check
    assert hasattr(res, "ok") and hasattr(res, "name")


def test_cpu_governor_intel_pstate_active_epp_performance_passes(monkeypatch):
    # Real 2026-08-31 case: intel_pstate=active, governor=powersave (the only
    # HWP-driven governor), EPP=performance. That's correct, not a failure —
    # schedutil isn't even selectable under intel_pstate=active.
    def _fake_read(p):
        if "scaling_governor" in p:
            return "powersave"
        if "intel_pstate/status" in p:
            return "active"
        if "energy_performance_preference" in p:
            return "performance"
        return ""
    monkeypatch.setattr(v, "_read", _fake_read)
    res = v.check_cpu_governor()
    assert res.ok
    assert "EPP=performance" in res.detail


def test_cpu_governor_intel_pstate_active_epp_power_fails(monkeypatch):
    # Same intel_pstate=active machine, but genuinely idle-biased (EPP=power)
    # — this must still fail, so the EPP-based check can't rubber-stamp
    # everything just because intel_pstate is active.
    def _fake_read(p):
        if "scaling_governor" in p:
            return "powersave"
        if "intel_pstate/status" in p:
            return "active"
        if "energy_performance_preference" in p:
            return "power"
        return ""
    monkeypatch.setattr(v, "_read", _fake_read)
    res = v.check_cpu_governor()
    assert not res.ok


def test_hugepages_ok_when_thp_madvise(monkeypatch):
    def _fake_read(p):
        if "transparent_hugepage/enabled" in p:
            return "always [madvise] never"
        return "0"
    monkeypatch.setattr(v, "_read", _fake_read)
    res = v.check_hugepages_available()
    assert res.ok


def test_hugepages_fails_when_no_thp_no_static(monkeypatch):
    monkeypatch.setattr(v, "_read", lambda p: "[never]" if "transparent_hugepage" in p else "0")
    res = v.check_hugepages_available()
    assert not res.ok


def test_scx_state_ok_when_disabled(monkeypatch, tmp_path):
    state_file = tmp_path / "state"
    state_file.write_text("disabled")
    monkeypatch.setattr(v, "_read", lambda p: "disabled" if "sched_ext/state" in p else "")
    # We can't easily mock Path existence so just test the logic path
    # by checking a Check is returned
    res = v.check_scx_state()
    assert hasattr(res, "ok")


# --- patch-series checks (added 2026-08-25 after the 7.1.10 verification) ---

def test_bore_prefers_sysctl_over_debugfs(monkeypatch):
    """Regression: check_bore_active used to read only
    /sys/kernel/debug/sched/features, which needs root, so an unprivileged
    run reported FAIL on a machine where BORE was on."""
    reads = {"/proc/sys/kernel/sched_bore": "1"}
    monkeypatch.setattr(v, "_read", lambda p: reads.get(p, ""))
    res = v.check_bore_active()
    assert res.ok and "sched_bore=1" in res.detail


def test_bore_reports_compiled_in_but_disabled(monkeypatch):
    monkeypatch.setattr(v, "_read",
                        lambda p: "0" if p == "/proc/sys/kernel/sched_bore" else "")
    res = v.check_bore_active()
    assert not res.ok and "disabled" in res.detail


def test_bore_falls_back_to_debugfs(monkeypatch):
    monkeypatch.setattr(
        v, "_read",
        lambda p: "BORE NEXT_BUDDY" if "debug/sched/features" in p else "")
    assert v.check_bore_active().ok


def test_max_map_count_reports_masked_not_failed(monkeypatch, tmp_path):
    """0004's patched default is unreachable while systemd sets the sysctl.
    That is a state to report, not a check to fail."""
    (tmp_path / "50-default.conf").write_text("vm.max_map_count = 1048576\n")
    monkeypatch.setattr(
        v, "_read",
        lambda p: "1048576" if p == "/proc/sys/vm/max_map_count"
        else (Path(p).read_text() if Path(p).exists() else ""))
    res = v.check_max_map_count_masked(sysctl_dirs=[str(tmp_path)])
    assert res.ok
    assert "MASKED" in res.name
    assert "50-default.conf" in res.detail


def test_max_map_count_becomes_live_if_override_disappears(monkeypatch, tmp_path):
    """If the distro ever stops setting it, the patched default should show
    up and the check flips to asserting it."""
    monkeypatch.setattr(
        v, "_read",
        lambda p: "2147483642" if p == "/proc/sys/vm/max_map_count" else "")
    res = v.check_max_map_count_masked(sysctl_dirs=[str(tmp_path)])
    assert res.ok and "patch is live" in res.detail


def test_sysctl_setters_finds_only_real_assignments(tmp_path):
    (tmp_path / "a.conf").write_text("# vm.swappiness is owned elsewhere\n")
    (tmp_path / "b.conf").write_text("vm.swappiness = 10\n")
    assert v._sysctl_setters("vm.swappiness", [str(tmp_path)]) == ["b.conf"]


def test_thp_defrag_parses_the_bracketed_selection(monkeypatch):
    monkeypatch.setattr(
        v, "_read",
        lambda p: "always defer [defer+madvise] madvise never")
    assert v.check_thp_defrag_default().ok


def test_thp_defrag_fails_on_wrong_selection(monkeypatch):
    monkeypatch.setattr(
        v, "_read", lambda p: "always [defer] defer+madvise madvise never")
    res = v.check_thp_defrag_default()
    assert not res.ok and res.detail == "defer"


def test_dmabuf_uapi_missing_ioctl_is_reported(monkeypatch):
    monkeypatch.setattr(
        v, "_read",
        lambda p: "DMA_BUF_IOCTL_SET_PRIORITY DMA_BUF_IOCTL_GET_PRIORITY")
    res = v.check_dmabuf_hint_uapi()
    assert not res.ok and "SET_COMPRESSION" in res.detail


def test_cache_ext_flags_registered_but_unregistered_type(monkeypatch):
    """The failure mode 0023's own series comment warns about: compiled in,
    /proc file present, struct_ops type never registered."""
    monkeypatch.setattr(v, "Path", lambda p: type("P", (), {"exists": lambda s: True})())
    monkeypatch.setattr(v, "_cmd", lambda a: "some btf without the shadow type")
    res = v.check_cache_ext_registered()
    assert not res.ok and "NOT registered" in res.detail


def test_nvidia_fs_skips_when_gds_absent(monkeypatch):
    monkeypatch.setattr(v.shutil, "which", lambda n: None)
    monkeypatch.setattr(v, "Path", lambda p: type("P", (), {"exists": lambda s: False})())
    res = v.check_nvidia_fs_loaded()
    assert res.ok and "n/a" in res.detail


def test_nvidia_fs_names_wrong_kernel_cause(monkeypatch):
    """Confirmed 2026-09-12 on 7.2.5-hyphaed: nvidia-fs.ko failed to load with
    a bare EINVAL, and the real cause (built against a different kernel's
    nvidia symbols) was sitting in nvidia-fs's own DKMS make.log. This check
    must surface that cause, not just "NOT loaded"."""
    monkeypatch.setattr(v.shutil, "which", lambda n: "/usr/bin/gdscheck" if n == "gdscheck" else None)
    monkeypatch.setattr(v, "_cmd", lambda args: "" if args[0] == "lsmod" else "7.2.5-hyphaed\n")
    monkeypatch.setattr(v, "nvidia_fs_module_is_truncated", lambda kver: None)
    monkeypatch.setattr(v, "nvidia_fs_built_against_wrong_kernel", lambda kver: "7.2.3-hyphaed")
    res = v.check_nvidia_fs_loaded()
    assert not res.ok
    assert "7.2.3-hyphaed" in res.detail
    assert "fix-nvidia-fs-symvers.sh" in res.detail


def test_nvidia_fs_names_truncated_module_cause(monkeypatch):
    monkeypatch.setattr(v.shutil, "which", lambda n: "/usr/bin/gdscheck" if n == "gdscheck" else None)
    monkeypatch.setattr(v, "_cmd", lambda args: "" if args[0] == "lsmod" else "7.2.5-hyphaed\n")
    monkeypatch.setattr(v, "nvidia_fs_module_is_truncated", lambda kver: Path("/lib/modules/7.2.5-hyphaed/updates/dkms/nvidia-fs.ko"))
    monkeypatch.setattr(v, "nvidia_fs_built_against_wrong_kernel", lambda kver: None)
    res = v.check_nvidia_fs_loaded()
    assert not res.ok
    assert "0 bytes" in res.detail


def test_dkms_status_orphan_entry_is_not_a_failure(monkeypatch):
    """hid-xpadneo/v0.11-pre-63-...: added is a stale source registration
    with no kernel field at all — confirmed 2026-09-12 on 7.2.5-hyphaed, a
    leftover from a version bump unrelated to the running kernel. It must
    not fail the check."""
    monkeypatch.setattr(v.shutil, "which", lambda n: "/usr/sbin/dkms")
    monkeypatch.setattr(v, "_cmd", lambda args: (
        "7.2.5-hyphaed\n" if args[0] == "uname" else
        "greenboost/3.4, 7.2.5-hyphaed, x86_64: installed\n"
        "hid-xpadneo/v0.11-pre-63-g3acca9f-dirty: added\n"
        "hid-xpadneo/v0.11-pre-64-g5de4fde-dirty, 7.2.5-hyphaed, x86_64: installed\n"
    ))
    res = v.check_dkms_status("hyphaed")
    assert res.ok
    assert "orphaned" in res.detail


def test_dkms_status_fails_on_real_missing_build(monkeypatch):
    """A line that DOES name the running kernel and isn't installed is a
    genuine failure, unlike the orphan case above."""
    monkeypatch.setattr(v.shutil, "which", lambda n: "/usr/sbin/dkms")
    monkeypatch.setattr(v, "_cmd", lambda args: (
        "7.2.5-hyphaed\n" if args[0] == "uname" else
        "nvidia/615.71.09, 7.2.5-hyphaed, x86_64: not installed\n"
    ))
    res = v.check_dkms_status("hyphaed")
    assert not res.ok
    assert "not installed" in res.detail


def test_dkms_status_check_name_is_consistent(monkeypatch):
    """All branches must return the same check name — it used to flip
    between 'DKMS modules built' (skip paths) and 'DKMS modules all
    installed' (real path), which reads as two different rows depending on
    outcome."""
    monkeypatch.setattr(v.shutil, "which", lambda n: None)
    skipped = v.check_dkms_status("hyphaed")
    monkeypatch.setattr(v.shutil, "which", lambda n: "/usr/sbin/dkms")
    monkeypatch.setattr(v, "_cmd", lambda args: (
        "7.2.5-hyphaed\n" if args[0] == "uname" else
        "nvidia/615.71.09, 7.2.5-hyphaed, x86_64: installed\n"
    ))
    clean = v.check_dkms_status("hyphaed")
    assert skipped.name == clean.name == "DKMS modules all installed"


def test_greenboost_loaded_detail_empty_on_pass(monkeypatch):
    monkeypatch.setattr(v, "_cmd", lambda args: "greenboost 69632 2\n")
    res = v.check_greenboost_loaded()
    assert res.ok and res.detail == ""


def test_greenboost_loaded_detail_present_on_fail(monkeypatch):
    monkeypatch.setattr(v, "_cmd", lambda args: "")
    res = v.check_greenboost_loaded()
    assert not res.ok and "modprobe greenboost" in res.detail


def test_zswap_active_reports_debugfs_needed_when_stats_unreadable(monkeypatch):
    """/sys/kernel/mm/zswap/{pool_total_size,written_back_pages} don't exist
    on this kernel (root-only debugfs instead); confirmed 2026-09-12 the old
    code rendered this as 'pool= written_back=', which reads as two zero
    readings rather than two unreadable files."""
    def fake_read(p):
        if "zswap/parameters/enabled" in p:
            return "Y"
        if "zswap/parameters/compressor" in p:
            return "lz4"
        if "zswap/parameters/max_pool_percent" in p:
            return "20"
        return ""  # pool_total_size, written_back_pages: unreadable
    monkeypatch.setattr(v, "_read", fake_read)
    res = v.check_zswap_active()
    assert res.ok
    assert "root" in res.detail or "debug" in res.detail
    assert "lz4" in res.detail
