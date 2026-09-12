from hyphaed import tuning as tn


def test_tune_item_dataclass_defaults():
    item = tn.TuneItem("name", "evidence", lambda: (True, ""))
    assert item.apply is None
    assert item.needs_root is False


def test_power_profile_reports_ok_when_performance(monkeypatch):
    monkeypatch.setattr(tn.shutil, "which", lambda name: "/usr/bin/powerprofilesctl")
    monkeypatch.setattr(tn, "_cmd", lambda args: "performance\n")
    ok, detail = tn._check_power_profile()
    assert ok
    assert "performance" in detail


def test_power_profile_reports_fail_when_balanced(monkeypatch):
    monkeypatch.setattr(tn.shutil, "which", lambda name: "/usr/bin/powerprofilesctl")
    monkeypatch.setattr(tn, "_cmd", lambda args: "balanced\n")
    ok, detail = tn._check_power_profile()
    assert not ok
    assert "balanced" in detail


def test_power_profile_na_when_not_installed(monkeypatch):
    monkeypatch.setattr(tn.shutil, "which", lambda name: None)
    ok, detail = tn._check_power_profile()
    assert ok
    assert "n/a" in detail


def test_nm_dns_plugin_passes_when_set(monkeypatch):
    monkeypatch.setattr(tn.shutil, "which", lambda name: "/usr/bin/nmcli")
    monkeypatch.setattr(tn, "_read", lambda p: "[main]\ndns=systemd-resolved\n" if "conf.d" in p else "")
    ok, detail = tn._check_nm_dns_plugin()
    assert ok


def test_nm_dns_plugin_fails_when_absent(monkeypatch):
    monkeypatch.setattr(tn.shutil, "which", lambda name: "/usr/bin/nmcli")
    monkeypatch.setattr(tn, "_read", lambda p: "")
    ok, detail = tn._check_nm_dns_plugin()
    assert not ok


def test_vmware_requires_passes_when_clean(monkeypatch):
    monkeypatch.setattr(tn, "_read", lambda p: "[Unit]\nWants=systemd-modules-load.service\n")
    ok, detail = tn._check_vmware_requires()
    assert ok


def test_vmware_requires_fails_when_present(monkeypatch):
    monkeypatch.setattr(
        tn, "_read",
        lambda p: "[Unit]\nRequires=systemd-modules-load.service\nAfter=network.target systemd-modules-load.service\n",
    )
    ok, detail = tn._check_vmware_requires()
    assert not ok
    assert "systemd-modules-load.service" in detail


def test_vmware_requires_na_when_not_installed(monkeypatch):
    monkeypatch.setattr(tn, "_read", lambda p: "")
    ok, detail = tn._check_vmware_requires()
    assert ok
    assert "n/a" in detail


def test_apply_vmware_requires_removes_line_and_adds_wants(monkeypatch):
    original = "[Unit]\nRequires=systemd-modules-load.service\nAfter=network.target systemd-modules-load.service\n\n[Service]\nType=oneshot\n"
    written = {}

    monkeypatch.setattr(tn, "_read", lambda p: original)

    def fake_run_sudo(cmd, **kw):
        if cmd[0] == "tee":
            written["content"] = kw.get("input_str", "")
        return None

    monkeypatch.setattr(tn, "run_sudo", fake_run_sudo)
    tn._apply_vmware_requires()
    assert "Requires=systemd-modules-load.service" not in written["content"]
    assert "Wants=systemd-modules-load.service" in written["content"]


def test_nvidia_peermem_conf_passes_when_absent(monkeypatch, tmp_path):
    fake = tmp_path / "nvidia-peermem.conf"
    monkeypatch.setattr(tn, "_NVIDIA_PEERMEM_CONF", fake)
    ok, detail = tn._check_nvidia_peermem_conf()
    assert ok
    assert detail == "absent"


def test_nvidia_peermem_conf_fails_when_present(monkeypatch, tmp_path):
    fake = tmp_path / "nvidia-peermem.conf"
    fake.write_text("nvidia-peermem\n")
    monkeypatch.setattr(tn, "_NVIDIA_PEERMEM_CONF", fake)
    ok, detail = tn._check_nvidia_peermem_conf()
    assert not ok


def test_nvidia_fs_currency_ok_when_fresh_or_missing(monkeypatch):
    monkeypatch.setattr(tn, "_running_kernel", lambda: "")
    ok, detail = tn._check_nvidia_fs_currency()
    assert ok
    assert "n/a" in detail


def test_nvidia_fs_currency_detects_stale_module(monkeypatch, tmp_path):
    kernel = "7.2.2-hyphaed"
    d = tmp_path / f"lib/modules/{kernel}/updates/dkms"
    d.mkdir(parents=True)
    nv = d / "nvidia.ko"
    nvfs = d / "nvidia-fs.ko"
    nv.write_text("x")
    nvfs.write_text("y")
    import os
    import time
    now = time.time()
    os.utime(nvfs, (now - 1000, now - 1000))
    os.utime(nv, (now, now))

    monkeypatch.setattr(tn, "_running_kernel", lambda: kernel)
    monkeypatch.setattr(
        tn, "_dkms_module_paths",
        lambda module, k: sorted((tmp_path / f"lib/modules/{k}/updates/dkms").glob(f"{module}.ko*")),
    )
    ok, detail = tn._check_nvidia_fs_currency()
    assert not ok
    assert "stale" in detail


def test_run_report_returns_tuples_for_every_item():
    results = tn.run_report()
    assert len(results) == len(tn.ITEMS)
    for item, ok, detail in results:
        assert isinstance(item, tn.TuneItem)
        assert isinstance(ok, bool)
        assert isinstance(detail, str)


def test_advisories_returns_list():
    advisories = tn.advisories()
    assert isinstance(advisories, list)
    for a in advisories:
        assert isinstance(a, tn.Advisory)


def _mock_read_and_cmd(monkeypatch, reads=None, cmds=None):
    reads = reads or {}
    cmds = cmds or {}
    monkeypatch.setattr(tn, "_read", lambda p: reads.get(p, ""))
    monkeypatch.setattr(tn, "_cmd", lambda args: cmds.get(tuple(args), ""))


def test_zswap_over_zram_advisory_fires_when_both_active(monkeypatch):
    _mock_read_and_cmd(
        monkeypatch,
        reads={"/sys/module/zswap/parameters/enabled": "Y\n"},
        cmds={("swapon", "--show=NAME", "--noheadings"): "/dev/zram0\n"},
    )
    names = [a.name for a in tn.advisories()]
    assert any("zswap" in n and "zram" in n for n in names)


def test_zswap_over_zram_advisory_absent_when_zram_only(monkeypatch):
    _mock_read_and_cmd(
        monkeypatch,
        reads={"/sys/module/zswap/parameters/enabled": "N\n"},
        cmds={("swapon", "--show=NAME", "--noheadings"): "/dev/zram0\n"},
    )
    names = [a.name for a in tn.advisories()]
    assert not any("zswap" in n and "zram" in n for n in names)


def test_thermald_advisory_fires_when_inactive(monkeypatch):
    _mock_read_and_cmd(
        monkeypatch,
        cmds={("systemctl", "is-active", "thermald"): "inactive\n"},
    )
    names = [a.name for a in tn.advisories()]
    assert "thermald disabled" in names


def test_thermald_advisory_absent_when_active(monkeypatch):
    _mock_read_and_cmd(
        monkeypatch,
        cmds={("systemctl", "is-active", "thermald"): "active\n"},
    )
    names = [a.name for a in tn.advisories()]
    assert "thermald disabled" not in names


def test_pci_numa_advisory_fires_when_warning_present(monkeypatch):
    _mock_read_and_cmd(
        monkeypatch,
        cmds={
            ("journalctl", "-k", "-b", "0", "--no-pager", "-g", "numa node"):
                "nvidia-fs:warning: error retrieving numa node for device 0000:01:00.0\n"
        },
    )
    names = [a.name for a in tn.advisories()]
    assert any("NUMA node" in n for n in names)


def test_pci_numa_advisory_absent_when_no_warning(monkeypatch):
    _mock_read_and_cmd(monkeypatch)
    names = [a.name for a in tn.advisories()]
    assert not any("NUMA node" in n for n in names)
