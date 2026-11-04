from pathlib import Path
from types import SimpleNamespace

from hyphaed import phases, presets
from hyphaed.phases import desktop, nvidia


def _ctx(tmp_path, preset="gaming-ai-vm", vendor="nvidia"):
    return SimpleNamespace(preset=preset, profile=SimpleNamespace(gpu_vendor=vendor),
                           state_dir=tmp_path, repo_root=tmp_path, source_dir=None, dry_run=True)


def test_post_reboot_stages_come_after_the_kernel_stages():
    assert phases.ORDER[-4:] == ["postinstall", "nvidia", "desktop", "proton"]


def test_preset_blocks_load():
    p = presets.load("gaming-ai-vm")
    assert p.nvidia["version"] == "615.78.08" and p.nvidia["commit"] == "ca2d03e6"
    assert p.desktop["enabled"] and p.desktop["mutter"].startswith("50.1")


def test_nvidia_skips_without_nvidia_gpu(tmp_path, capsys):
    nvidia.run_phase(_ctx(tmp_path, vendor="amd"))
    assert not list(tmp_path.iterdir())


def test_nvidia_skips_when_preset_has_no_block(tmp_path):
    nvidia.run_phase(_ctx(tmp_path, preset="safe"))
    assert not list(tmp_path.iterdir())


def test_desktop_skips_when_disabled(tmp_path):
    desktop.run_phase(_ctx(tmp_path, preset="safe"))
    assert not list(tmp_path.iterdir())


def test_desktop_flags_use_native_lto():
    f = desktop._flags()
    assert "optimize=+lto" in f["DEB_BUILD_MAINT_OPTIONS"]
    assert "-O3 -march=native" in f["DEB_CFLAGS_MAINT_APPEND"] and "nocheck" in f["DEB_BUILD_OPTIONS"]


def test_trim_rules_only_known_options(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, "_TRIM", ("profiler", "tests"))
    (tmp_path / "debian").mkdir()
    (tmp_path / "meson.options").write_text(
        "option('profiler',\n  type: 'boolean',\n  description: 'x')\noption('tests',\n  type: 'feature',\n  value: 'auto')\n"
        "option('xwayland', type: 'boolean')\n")
    (tmp_path / "debian/rules").write_text("override_dh_auto_configure:\n\tdh_auto_configure -- -Dfoo=bar\n")
    assert desktop._trim_rules(tmp_path)
    text = (tmp_path / "debian/rules").read_text()
    assert "-Dprofiler=false" in text and "-Dtests=disabled" in text and "xwayland" not in text
    assert desktop._trim_rules(tmp_path)  # idempotent
    assert text == (tmp_path / "debian/rules").read_text()


def test_proton_block_and_skip(tmp_path):
    from hyphaed.phases import proton
    assert presets.load("gaming-ai-vm").proton["rebuild_gb_nr"] is True
    proton.run_phase(_ctx(tmp_path, preset="safe"))
    assert not list(tmp_path.iterdir())


def test_nvidia_build_uses_runresult_code(tmp_path, monkeypatch):
    """Regression: the phases read RunResult.returncode, which does not exist (the field is .code)."""
    from hyphaed.util.run import RunResult
    kt = tmp_path / "linux-7.2.14"
    kt.mkdir()
    (kt / ".config").write_text('CONFIG_CC_VERSION_TEXT="gcc"\n')
    ctx = SimpleNamespace(repo_root=tmp_path, dry_run=False)
    cfg = {"commit": "abc", "version": "1"}
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[0] == "make":
            work = Path(cmd[2])
            (work / "kernel-open").mkdir(parents=True, exist_ok=True)
            for m in nvidia._MODULES:
                (work / "kernel-open" / f"{m}.ko").write_text("x")
            return RunResult(0, "", "", "make")
        if cmd[0] == "git" and "clone" in cmd:
            Path(cmd[-1]).mkdir(parents=True, exist_ok=True)
        return RunResult(0, "", "", "x")

    monkeypatch.setattr(nvidia, "run", fake_run)
    out = nvidia._build(ctx, cfg, tmp_path / "src", kt, "7.2.14-hyphaed")
    assert out and (out / "nvidia.ko").exists()
    monkeypatch.setattr(nvidia, "run", lambda cmd, **kw: RunResult(2, "", "", "make") if cmd[0] == "make" else fake_run(cmd, **kw))
    assert nvidia._build(ctx, cfg, tmp_path / "src", kt, "7.2.14-hyphaed") is None


def test_rollback_list_survives_a_rerun_after_the_purge(tmp_path, monkeypatch):
    """Regression: a second run saw zero nvidia packages and overwrote the saved rollback list with nothing."""
    from hyphaed.util.run import RunResult
    ctx = SimpleNamespace(state_dir=tmp_path, dry_run=False)
    (tmp_path / "nvidia-rollback.txt").write_text("libnvidia-gl=615.71.09-2ubuntu1\n")
    monkeypatch.setattr(nvidia, "run", lambda cmd, **kw: RunResult(0, "", "", "x"))
    assert nvidia._save_rollback(ctx) == []
    assert "libnvidia-gl" in (tmp_path / "nvidia-rollback.txt").read_text()


def test_rollback_selects_egl_platform_packages_regardless_of_version(tmp_path, monkeypatch):
    """Regression 2026-11-01: libnvidia-egl-* kept their own versions, survived the purge, and a later
    apt autoremove deleted the runfile's EGL json files, breaking gnome-shell."""
    from hyphaed.util.run import RunResult
    ctx = SimpleNamespace(state_dir=tmp_path, dry_run=False)
    listing = ("libnvidia-egl-gbm1=1.1.4-2ubuntu1\nlibnvidia-egl-wayland21=1.0.2-1ubuntu1\n"
               "libnvidia-egl-xcb1=1.0.6-1ubuntu1\nlibnvidia-egl-xlib1=1.0.6-1ubuntu1\n"
               "libnvidia-gl=615.71.09-2ubuntu1\nlibnvidia-egl-wayland-dev=1:1.1.21-1\n"
               "libnvidia-egl-wayland1=1:1.1.21-1\nnvidia-cuda-toolkit=12.4.131\n")
    monkeypatch.setattr(nvidia, "run", lambda cmd, **kw: RunResult(0, listing, "", "x"))
    assert sorted(nvidia._save_rollback(ctx)) == [
        "libnvidia-egl-gbm1", "libnvidia-egl-wayland21", "libnvidia-egl-xcb1", "libnvidia-egl-xlib1", "libnvidia-gl"]


def test_failed_installer_restores_apt(tmp_path, monkeypatch):
    from hyphaed.util.run import RunResult
    ctx = SimpleNamespace(state_dir=tmp_path, dry_run=False, repo_root=tmp_path)
    (tmp_path / "nvidia-rollback.txt").write_text("libnvidia-gl=615.71.09-2ubuntu1\n")
    seen = []
    monkeypatch.setattr(nvidia, "run_sudo", lambda cmd, **kw: (seen.append(cmd), RunResult(0, "", "", "x"))[1])
    nvidia._restore_apt(ctx)
    assert seen and seen[0][:2] == ["apt-get", "install"] and "libnvidia-gl=615.71.09-2ubuntu1" in seen[0]


def test_gpu_job_check_works_without_nvidia_smi(monkeypatch):
    """Regression: after the userspace purge nvidia-smi is gone and the check crashed with FileNotFoundError."""
    def boom(cmd, **kw):
        raise FileNotFoundError(cmd[0])
    monkeypatch.setattr(nvidia, "run", boom)
    monkeypatch.setattr(nvidia, "_driver_loaded", lambda: True)
    monkeypatch.setattr(nvidia, "_procs_on_gpu", lambda: ["1, gnome-shell", "2, nautilus"])
    assert nvidia._gpu_jobs_clear() is True
    monkeypatch.setattr(nvidia, "_procs_on_gpu", lambda: ["3, llama-server"])
    assert nvidia._gpu_jobs_clear() is False


def test_desktop_build_env_ignores_user_tool_dirs(monkeypatch):
    monkeypatch.setenv("PATH", "/home/x/.local/bin:/home/x/.miniforge3/bin:/usr/bin")
    monkeypatch.setenv("PYTHONPATH", "/home/x/lib")
    env = desktop._clean_env()
    assert ".local" not in env["PATH"] and "miniforge" not in env["PATH"] and "PYTHONPATH" not in env


def test_no_meson_trim_that_changes_the_packages():
    """Regression: `tests` made dh_install abort (libmutter-test-18), `profiler` dropped cogl_trace_* symbols
    and failed dh_makeshlibs. Add an option here only after a full package build passed with it."""
    assert desktop._TRIM == ()


def test_gpu_job_check_passes_when_no_driver_is_loaded(monkeypatch):
    """Regression 2026-11-04: booted a kernel with no NVIDIA module, nvidia-smi's driver error was read as a running job."""
    monkeypatch.setattr(nvidia, "_driver_loaded", lambda: False)
    monkeypatch.setattr(nvidia, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("nvidia-smi must not run")))
    assert nvidia._gpu_jobs_clear() is True


def test_install_nvidia_fs_forces_when_missing(tmp_path, monkeypatch):
    from hyphaed.util.run import RunResult
    calls = []
    monkeypatch.setattr(nvidia.shutil, "which", lambda c: "/usr/bin/dkms")
    monkeypatch.setattr(nvidia, "run", lambda cmd, **kw: RunResult(0, "nvidia-fs/2.30.2, 7.2.14-hyphaed, x86_64: installed\n", "", "x"))
    monkeypatch.setattr(nvidia, "run_sudo", lambda cmd, **kw: calls.append(cmd) or RunResult(0, "", "", "x"))
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(nvidia, "nvidia_fs_module_missing", lambda k: True)
    nvidia._install_nvidia_fs(ctx, "7.2.15-hyphaed")
    assert calls == [["dkms", "install", "nvidia-fs/2.30.2", "-k", "7.2.15-hyphaed", "--force"]]
    calls.clear()
    monkeypatch.setattr(nvidia, "nvidia_fs_module_missing", lambda k: False)
    nvidia._install_nvidia_fs(ctx, "7.2.15-hyphaed")
    assert calls == []
