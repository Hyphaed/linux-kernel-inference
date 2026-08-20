from pathlib import Path
from hyphaed.phases.install import _order, _is_required


def test_order_headers_first():
    debs = [Path(p) for p in [
        "linux-image-7.0.0-15-hyphaed_7.0.0-15.15hyphaed1_amd64.deb",
        "linux-headers-7.0.0-15-hyphaed_7.0.0-15.15hyphaed1_amd64.deb",
        "linux-libc-dev_7.0.0-15.15hyphaed1_amd64.deb",
    ]]
    out = _order(debs)
    assert out[0].name.startswith("linux-headers")
    assert out[1].name.startswith("linux-image")
    assert out[2].name.startswith("linux-libc-dev")


def test_order_stable_when_unknown_prefix():
    debs = [Path("custom-something_1_amd64.deb"), Path("linux-image-x_1_amd64.deb")]
    out = _order(debs)
    # linux-image wins over custom (rank 1 vs 9)
    assert out[0].name.startswith("linux-image")


def test_is_required_covers_boot_critical_packages():
    assert _is_required("linux-headers-7.1.3-hyphaed_7.1.3-1_amd64.deb")
    assert _is_required("linux-image-7.1.3-hyphaed_7.1.3-1_amd64.deb")
    assert _is_required("linux-image-7.1.3-hyphaed-dbg_7.1.3-1_amd64.deb")
    assert _is_required("linux-libc-dev_7.1.3-1_amd64.deb")


def test_is_required_false_for_auxiliary_tools_package():
    """linux-hyphaed-tools (cpupower/turbostat/x86_energy_perf_policy) is not
    needed to boot or run the kernel — a dpkg conflict on it (e.g. against
    the distro's linux-tools-common, still depended on by the currently
    running kernel's linux-tools-<ver>) must not be treated as fatal."""
    assert not _is_required("linux-hyphaed-tools_7.1.3-1_amd64.deb")


# ── dracut initramfs omission list ────────────────────────────────────────────

def test_dracut_conf_omits_nvidia_fs():
    """nvidia_fs depends on nvidia for its nvidia_p2p_* GDS symbols, and nvidia
    is deliberately not in the initramfs. With /etc/modules-load.d/nvidia-fs.conf
    asking for it early, leaving it out of this list logs eight
    'Unknown symbol nvidia_p2p_* (err -2)' failures on every boot (observed
    2026-08-18, t=1.65s). It self-heals at t=61.8s once nvidia is up."""
    from hyphaed.phases.install import _DRACUT_CONF_BODY
    omit = [ln for ln in _DRACUT_CONF_BODY.splitlines() if ln.startswith("omit_drivers")]
    assert len(omit) == 1
    for mod in ("nvidia", "nvidia_drm", "nvidia_modeset", "nvidia_uvm",
                "nvidia_peermem", "nvidia_fs"):
        assert f" {mod} " in omit[0], f"{mod} missing from omit_drivers"


def test_dracut_conf_refreshes_an_outdated_deployed_file(tmp_path, monkeypatch):
    """The old idempotency check matched markers true of every version of this
    file, so a machine that already had it never received later additions."""
    from hyphaed.phases import install

    conf = tmp_path / "99-hyphaed-no-nvidia-initramfs.conf"
    conf.write_text('omit_drivers+=" nvidia nvidia_drm nvidia_modeset "\n')
    monkeypatch.setattr(install, "_DRACUT_CONF", conf)

    wrote: list[str] = []
    monkeypatch.setattr(install, "run_sudo",
                        lambda cmd, input_str=None, **kw: wrote.append(input_str))
    install._ensure_dracut_no_nvidia_conf()
    assert wrote and "nvidia_fs" in wrote[0], "stale conf was not refreshed"

    # And once current, it must not rewrite on every install.
    conf.write_text(install._DRACUT_CONF_BODY)
    wrote.clear()
    install._ensure_dracut_no_nvidia_conf()
    assert wrote == []


# ── PCI numa_node udev rule (T8) ──────────────────────────────────────────────

def test_numa_rule_written_only_on_single_node_machines(tmp_path, monkeypatch):
    """A fabricated node on a real multi-node box would misdirect NUMA-local
    allocation — worse than the nvidia-fs warning it silences."""
    from hyphaed.phases import install

    rule = tmp_path / "62-hyphaed-pci-numa-node.rules"
    monkeypatch.setattr(install, "_NUMA_RULE", rule)
    wrote: list[str] = []
    monkeypatch.setattr(install, "run_sudo",
                        lambda cmd, input_str=None, **kw: wrote.append(input_str))

    monkeypatch.setattr(install, "_online_numa_nodes", lambda: 2)
    install._ensure_pci_numa_rule()
    assert wrote == [], "rule must not be written on a multi-node machine"

    monkeypatch.setattr(install, "_online_numa_nodes", lambda: 1)
    install._ensure_pci_numa_rule()
    assert len(wrote) == 1
    assert 'ATTR{numa_node}=="-1"' in wrote[0]
    assert 'ATTR{numa_node}="0"' in wrote[0]


def test_numa_rule_is_idempotent(tmp_path, monkeypatch):
    from hyphaed.phases import install

    rule = tmp_path / "62-hyphaed-pci-numa-node.rules"
    rule.write_text(install._NUMA_RULE_BODY)
    monkeypatch.setattr(install, "_NUMA_RULE", rule)
    monkeypatch.setattr(install, "_online_numa_nodes", lambda: 1)
    wrote: list[str] = []
    monkeypatch.setattr(install, "run_sudo",
                        lambda cmd, input_str=None, **kw: wrote.append(input_str))
    install._ensure_pci_numa_rule()
    assert wrote == []


def test_online_numa_nodes_counts_real_sysfs():
    """Sanity: this box has exactly one node (i9-14900KF, single socket)."""
    from hyphaed.phases.install import _online_numa_nodes
    assert _online_numa_nodes() >= 1
