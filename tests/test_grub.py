from hyphaed import grub, greenboost
from hyphaed.topology import HardwareProfile


def _profile(**kw):
    base = dict(
        cpu_codename="raptorlake-r",
        gpu_vendor="nvidia",
        gpu_arch="blackwell",
        session_type="wayland",
        p_thread_mask="0-15",
        e_thread_mask="16-31",
        flavour="hyphaed",
    )
    base.update(kw)
    return HardwareProfile(**base)


def test_compose_cmdline_basic():
    p = _profile()
    tokens = grub.compose_cmdline(p)
    assert "iommu=pt" in tokens
    assert "nvidia-drm.modeset=1" in tokens
    assert "mitigations=auto" in tokens
    # no default CPU isolation — GreenBoost owns runtime CPU/thread placement
    assert not any(t.startswith("nohz_full=") for t in tokens)
    assert not any(t.startswith("rcu_nocbs=") for t in tokens)


def test_compose_cmdline_sets_irqaffinity_to_p_cores(monkeypatch):
    # Confirmed on real hardware: with no irqaffinity set, the GPU's MSI-X
    # completion vectors land on E-core threads (CPU23/CPU25, P=0-15,
    # E=16-31) with unrestricted (0-31) affinity — directly opposite
    # 13-energy-hybrid.config's intent to keep E-cores off the
    # latency-critical path. irqaffinity= sets the kernel's
    # /proc/irq/default_smp_affinity (confirmed currently "ffffffff",
    # unrestricted) to bias all default-affinity IRQs onto P-cores.
    #
    # No greenboost profile here — isolates this test from whatever
    # /etc/greenboost/profiles/ actually contains on the machine running
    # the suite (a real profile there, as on a live dev box, would
    # otherwise silently narrow the mask via the golden-core cross-check
    # below and make this test's result machine-dependent).
    monkeypatch.setattr(greenboost, "find_active_profile", lambda: None)
    p = _profile(p_thread_mask="0-15")
    tokens = grub.compose_cmdline(p)
    assert "irqaffinity=0-15" in tokens


def test_compose_cmdline_irqaffinity_excludes_greenboost_golden_cores(monkeypatch):
    # Found live 2026-07-24: enp4s0's IRQ had smp_affinity_list 0-15,
    # overlapping nohz_full=4-7 (greenboost's "golden" core range) — a
    # cluster/RPC/NCCL NIC interrupt landing there breaks the tickless
    # guarantee mid-inference. irqaffinity is hyphaed's own key, so this is
    # the side that should exclude the golden range, not greenboost's.
    class _FakeGbProfile:
        def get_int(self, key, default=0):
            return {"golden_cpu_min": 4, "golden_cpu_max": 7}.get(key, default)
    monkeypatch.setattr(greenboost, "find_active_profile", lambda: _FakeGbProfile())
    p = _profile(p_thread_mask="0-15")
    tokens = grub.compose_cmdline(p)
    assert "irqaffinity=0-3,8-15" in tokens


def test_compose_cmdline_irqaffinity_survives_greenboost_lookup_failure(monkeypatch):
    # The cross-check is best-effort — a broken/missing profile must never
    # break cmdline composition, just fall back to the full P-thread mask.
    def _raise():
        raise RuntimeError("profile read failed")
    monkeypatch.setattr(greenboost, "find_active_profile", _raise)
    p = _profile(p_thread_mask="0-15")
    tokens = grub.compose_cmdline(p)
    assert "irqaffinity=0-15" in tokens


def test_compose_cmdline_skips_irqaffinity_without_p_thread_mask():
    p = _profile(p_thread_mask="")
    tokens = grub.compose_cmdline(p)
    assert not any(t.startswith("irqaffinity=") for t in tokens)


def test_compose_cmdline_desktop_nvidia_disables_aspm():
    """`pcie_aspm=off` tells the kernel not to touch ASPM, leaving whatever the
    BIOS programmed in place. Verified live 2026-08-18 while booted with `off`:
    policy read `[default]` and the GPU reported `LnkCtl: ASPM L1 Enabled`.
    `performance` is the value that actually disables it, which is what the
    lowest-latency intent needs on a PCIe-transfer-bound box."""
    p = _profile()
    tokens = grub.compose_cmdline(p)
    assert "pcie_aspm=performance" in tokens
    assert "pcie_aspm=off" not in tokens


def test_compose_cmdline_laptop_keeps_firmware_aspm():
    """Disabling ASPM on every link costs idle power. Free on a mains-powered
    desktop, not free on machine 2 (Ryzen AI 9 laptop)."""
    p = _profile(chassis="laptop")
    tokens = grub.compose_cmdline(p)
    assert "pcie_aspm=off" in tokens
    assert "pcie_aspm=performance" not in tokens


def test_compose_cmdline_non_nvidia_skips_gpu_perf():
    p = _profile(gpu_vendor="none")
    tokens = grub.compose_cmdline(p)
    assert "pcie_aspm=off" not in tokens
    assert not any(t.startswith("nvidia") for t in tokens)


def test_compose_cmdline_drops_mitigations_off():
    p = _profile()
    tokens = grub.compose_cmdline(p, ["mitigations=off", "transparent_hugepage=always"])
    assert "mitigations=off" not in tokens


def test_compose_cmdline_deduplicates_by_key():
    p = _profile()
    # base has transparent_hugepage=madvise; preset's cmdline_extra is meant
    # to override it (this is exactly how ai-only.yaml enables THP=always) —
    # last occurrence wins, matching read_current_cmdline_config() semantics.
    tokens = grub.compose_cmdline(p, ["transparent_hugepage=always"])
    hp = [t for t in tokens if t.startswith("transparent_hugepage")]
    assert hp == ["transparent_hugepage=always"]


def test_compose_cmdline_skips_keys_present_in_current():
    p = _profile()
    # Simulate greenboost already set iommu=pt and intel_iommu=on,igfx_off
    current = ["iommu=pt", "intel_iommu=on,igfx_off", "quiet"]
    tokens = grub.compose_cmdline(p, skip_keys_present_in=current)
    assert not any(t.startswith("iommu=") for t in tokens)
    assert not any(t.startswith("intel_iommu=") for t in tokens)


def test_diff_cmdline_categorises():
    cur = ["transparent_hugepage=always", "nohz_full=4-7", "quiet"]
    new = ["transparent_hugepage=madvise", "nohz_full=0-15", "quiet", "iommu=pt"]
    added, removed, changed = grub.diff_cmdline(cur, new)
    assert "iommu=pt" in added
    assert any(t.startswith("transparent_hugepage=always") and t.endswith("=madvise") for t in changed)
    assert removed == []


def test_render_dropin_idempotent():
    a = grub.render_dropin(["iommu=pt", "nvidia-drm.modeset=1"])
    b = grub.render_dropin(["iommu=pt", "nvidia-drm.modeset=1"])
    assert a == b


def test_pin_default_kernel_dry_run_is_noop(monkeypatch):
    calls = []
    monkeypatch.setattr(grub, "run_sudo", lambda cmd, **kw: calls.append(cmd))
    grub.pin_default_kernel("7.1.5-hyphaed", dry_run=True)
    assert calls == []


def test_pin_default_kernel_uses_title_path_addressing(monkeypatch):
    # Confirmed live 2026-07-30 via `cat /boot/grub/grubenv` (world-readable,
    # unlike grub.cfg which is root-only 0600): saved_entry on this box was
    # "Advanced options for Ubuntu>Ubuntu, with Linux 7.1.1-hyphaed" — GRUB
    # title-path addressing, not an --id string. pin_default_kernel must
    # produce the same format so the pin is a like-for-like fix.
    calls = []
    monkeypatch.setattr(grub, "run_sudo", lambda cmd, **kw: calls.append(cmd))
    grub.pin_default_kernel("7.1.5-hyphaed", dry_run=False)
    assert calls == [["grub-set-default", "Advanced options for Ubuntu>Ubuntu, with Linux 7.1.5-hyphaed"]]


def test_read_current_cmdline_config_deduplicates(tmp_path, monkeypatch):
    """Tokens set in both the base file and a drop-in should appear only once."""
    base = tmp_path / "grub"
    base.write_text('GRUB_CMDLINE_LINUX_DEFAULT="quiet rcu_nocbs=16-31 transparent_hugepage=always"\n')
    drop_d = tmp_path / "grub.d"
    drop_d.mkdir()
    # drop-in expands the base and adds more — duplicating rcu_nocbs
    (drop_d / "50-greenboost.cfg").write_text(
        'GRUB_CMDLINE_LINUX_DEFAULT="${GRUB_CMDLINE_LINUX_DEFAULT} rcu_nocbs=16-31 iommu=pt"\n'
    )
    monkeypatch.setattr(grub, "GRUB_DEFAULT", base)
    monkeypatch.setattr(grub, "GRUB_DEFAULT_DIR", drop_d)
    tokens = grub.read_current_cmdline_config()
    # rcu_nocbs should appear exactly once
    rcu_count = sum(1 for t in tokens if t.startswith("rcu_nocbs"))
    assert rcu_count == 1
    # All distinct tokens should be present
    assert "quiet" in tokens
    assert "iommu=pt" in tokens
    assert "transparent_hugepage=always" in tokens
