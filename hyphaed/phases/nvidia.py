"""Phase 10/11: build the NVIDIA open kernel modules from source for the new kernel.

Runs after `install`. Owner decision: the module comes from
~/Dev/github_gaming/open-gpu-kernel-modules (pinned commit) built with the
kernel's own compiler and flags; the userspace comes from the official .run
installed with --no-kernel-modules, so both halves are the same version.
Rollback to the apt packages: diagnostics/nvidia-restore-apt.sh.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path

from ..nvidia_fs import nvidia_fs_dkms_version, nvidia_fs_module_missing
from ..util import log
from ..util.prompts import confirm
from ..util.run import run, run_sudo

NAME = "nvidia"

_MODULES = ("nvidia", "nvidia-modeset", "nvidia-drm", "nvidia-uvm", "nvidia-peermem")
_DEPMOD_CONF = Path("/etc/depmod.d/hyphaed-nvidia.conf")
_EXTRA_DIR = "extra/nvidia-hyphaed"
# The .run writes these EGL platform files into paths apt packages own, and the packages carry their own
# versions (not the driver's). Left installed, the next `apt autoremove` deletes the runfile's copies and
# gnome-shell/Xwayland lose EGL (found 2026-11-01). Purge them with the driver so the runfile owns the paths.
_EGL_PLATFORM_PKGS = re.compile(r"libnvidia-egl-((gbm|xcb|xlib)\d*|wayland2\d*)=")
_EGL_GBM_JSON = Path("/usr/share/egl/egl_external_platform.d/15_nvidia_gbm.json")
_DESKTOP_CLIENTS = ("gnome-shell", "gjs", "nautilus", "Xwayland", "mutter", "firefox", "chrome",
                    "chromium", "code", "gnome-", "xdg-", "ibus", "gsd-", "evolution", "kitty",
                    "ptyxis", "gnome-terminal", "steamwebhelper", "steam", "nvidia-persist", "WebKit", "greenboost-gami")


def _cfg(ctx) -> dict:
    from .. import presets
    if not ctx.preset:
        return {}
    try:
        return presets.load(ctx.preset).nvidia
    except Exception:
        return {}


def _booted_into_target(kernel_tree: Path) -> bool:
    ver = kernel_tree.name.removeprefix("linux-")
    return os.uname().release.startswith(ver + "-")


def _rollback_file(ctx) -> Path:
    return ctx.state_dir / "nvidia-rollback.txt"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _preflight(ctx, cfg: dict, src: Path, run_file: Path) -> bool:
    ok = True
    if not src.is_dir():
        log.err(f"NVIDIA source tree missing: {src}")
        return False
    head = run(["git", "-C", str(src), "rev-parse", "--short=8", "HEAD"], check=False, force=True).stdout.strip()
    if head != cfg["commit"][:8]:
        log.err(f"{src} is at {head}, preset pins {cfg['commit']} ({cfg['version']})")
        ok = False
    dirty = run(["git", "-C", str(src), "status", "--porcelain"], check=False, force=True).stdout.strip()
    if dirty:
        log.err(f"{src} has local changes; the pinned build must be reproducible")
        ok = False
    if not run_file.exists():
        log.err(f"userspace installer missing: {run_file} (wizard 'fetch' mode downloads it)")
        ok = False
    elif cfg.get("run_sha256") and _sha256(run_file) != cfg["run_sha256"]:
        log.err(f"{run_file.name} sha256 does not match the preset pin")
        ok = False
    elif not cfg.get("run_sha256"):
        log.warn(f"no run_sha256 pinned in the preset; local sha256 is {_sha256(run_file)}. Pin it.")
    return ok


def _procs_on_gpu() -> list[str]:
    """Processes of this user holding /dev/nvidia*, found without nvidia-smi."""
    found = []
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            if any(os.readlink(f"/proc/{pid}/fd/{fd}").startswith("/dev/nvidia") for fd in os.listdir(f"/proc/{pid}/fd")):
                found.append(f"{pid}, {Path(f'/proc/{pid}/comm').read_text().strip()}")
        except OSError:
            continue
    return found


def _driver_loaded() -> bool:
    return Path("/proc/driver/nvidia").exists()


def _gpu_jobs_clear() -> bool:
    if not _driver_loaded():
        # No kernel module loaded (booted a kernel without one): nothing can be using the GPU, and
        # nvidia-smi's "couldn't communicate with the driver" error must not be read as a running job.
        return True
    try:
        apps = run(["nvidia-smi", "--query-compute-apps=pid,process_name", "--format=csv,noheader"],
                   check=False, force=True).stdout.strip().splitlines()
    except FileNotFoundError:   # userspace already removed by an interrupted earlier attempt
        apps = _procs_on_gpu()
    # Desktop clients (shell, nautilus, gjs, browsers) hold the GPU for drawing only; they
    # keep the old libraries loaded and survive until you reboot. Real compute jobs
    # (CUDA, llama-server, ComfyUI, ...) would be broken by the driver swap, so block on those.
    jobs = [a for a in apps if not any(d in a for d in _DESKTOP_CLIENTS)]
    if jobs:
        log.err("GPU compute jobs are running; stop them first, then re-run this step:\n" + "\n".join(jobs))
        return False
    return True


def _cache_dir(ctx, kver: str) -> Path:
    return ctx.repo_root / "build" / "nvidia" / f"built-{kver}"


def _cache_valid(cache: Path, cfg: dict) -> bool:
    try:
        meta = json.loads((cache / "built.json").read_text())
    except (OSError, ValueError):
        return False
    return meta.get("commit") == cfg["commit"] and all((cache / f"{m}.ko").exists() for m in _MODULES[:4])


def _build(ctx, cfg: dict, src: Path, kernel_tree: Path, kver: str) -> Path | None:
    """Compile the modules against the target kernel's build tree (the kernel need not be running)."""
    from .build import _build_jobs   # owner rule 2026-11-04: at most 75% of the CPU while building
    jobs = str(_build_jobs())
    cc = run(["sed", "-n", "s/^CONFIG_CC_VERSION_TEXT=\"\\(.*\\)\"/\\1/p", str(kernel_tree / ".config")],
             check=False, force=True).stdout.strip()
    log.info(f"kernel compiler: {cc or 'unknown'}; the module is built with plain gcc to match (no LTO/clang)")
    work = ctx.repo_root / "build" / "nvidia" / f"src-{kver}"
    cache = _cache_dir(ctx, kver)
    if ctx.dry_run:
        log.info(f"(dry-run) would build {cfg['version']} in {work} against {kernel_tree}")
        return cache
    shutil.rmtree(work, ignore_errors=True)
    run(["git", "clone", "-q", "--shared", str(src), str(work)])
    run(["git", "-C", str(work), "checkout", "-q", cfg["commit"]])
    r = run(
        ["make", "-C", str(work), f"-j{jobs}", "modules",
         f"SYSSRC={kernel_tree}", f"SYSOUT={kernel_tree}", "CC=gcc"],
        check=False, tee_log=log.phase_log_path("nvidia"),
    )
    if r.code != 0:
        log.err("module build failed; see the phase log. Nothing was installed or changed.")
        return None
    shutil.rmtree(cache, ignore_errors=True)
    cache.mkdir(parents=True)
    for m in _MODULES:
        ko = work / "kernel-open" / f"{m}.ko"
        if ko.exists():
            shutil.copy2(ko, cache / ko.name)
    return cache


def _sign(ctx, kernel_tree: Path, mods: list[Path]) -> None:
    cfgtxt = (kernel_tree / ".config").read_text(errors="ignore") if (kernel_tree / ".config").exists() else ""
    if "CONFIG_MODULE_SIG=y" not in cfgtxt:
        return
    signer = kernel_tree / "scripts/sign-file"
    key, crt = kernel_tree / "certs/signing_key.pem", kernel_tree / "certs/signing_key.x509"
    mok = Path("/var/lib/shim-signed/mok")
    if mok.joinpath("MOK.priv").exists():
        key, crt = mok / "MOK.priv", mok / "MOK.der"
    if not (signer.exists() and key.exists() and crt.exists()):
        log.warn("MODULE_SIG is on but no signing key found; modules will not load under lockdown/Secure Boot")
        return
    for m in mods:
        run_sudo([str(signer), "sha512", str(key), str(crt), str(m)], dry_run=ctx.dry_run)


def _save_rollback(ctx) -> list[str]:
    r = run(["dpkg-query", "-W", "-f=${Package}=${Version}\n"], check=False, force=True)
    pkgs = [ln for ln in r.stdout.splitlines()
            if re.match(r"(nvidia-|libnvidia-|xserver-xorg-video-nvidia)", ln) or ln.startswith("nvidia-driver-pinning")]
    pkgs = [p for p in pkgs if "615.71.09" in p or "pinning" in p or _EGL_PLATFORM_PKGS.match(p)]
    rb = _rollback_file(ctx)
    if not pkgs and rb.exists() and rb.read_text().strip():
        log.info(f"apt NVIDIA packages already removed; keeping the saved rollback list {rb}")
        return []
    if not ctx.dry_run:
        ctx.state_dir.mkdir(parents=True, exist_ok=True)
        rb.write_text("\n".join(pkgs) + "\n")
    log.ok(f"rollback list saved: {rb} ({len(pkgs)} packages)")
    return [p.split("=")[0] for p in pkgs]


def _restore_apt(ctx) -> None:
    """The .run installer failed after the apt packages were purged: put the old driver back at once."""
    rb = _rollback_file(ctx)
    lines = [ln for ln in rb.read_text().split() if ln] if rb.exists() else []
    log.err("the NVIDIA installer failed; restoring the apt driver so the system keeps a working userspace")
    if not lines:
        log.err("no rollback list found. Manual fix: sudo apt-get install nvidia-open")
        return
    r = run_sudo(["apt-get", "install", "-y", "--allow-downgrades", *lines], check=False)
    if r.code == 0:
        log.ok("apt NVIDIA packages restored")
    else:
        log.err(f"restore failed; run: sudo bash {ctx.repo_root}/diagnostics/nvidia-restore-apt.sh")


def _merge_depmod_conf(existing: str, kver: str, modules: list[str]) -> str:
    """Replace the target kernel's override lines, keep every other kernel's (rollback needs them)."""
    header = "# hyphaed: optimized NVIDIA modules win over updates/dkms\n"
    kept = [ln for ln in existing.splitlines()
            if ln.startswith("override ") and len(ln.split()) >= 3 and ln.split()[2] != kver]
    new = [f"override {m.replace('-', '_')} {kver} {_EXTRA_DIR}" for m in modules]
    return header + "\n".join(kept + new) + "\n"


def _install(ctx, cfg: dict, src: Path, run_file: Path, kver: str, built: Path) -> None:
    pkgs = _save_rollback(ctx)
    if pkgs:
        run_sudo(["apt-mark", "unhold", *pkgs], check=False, dry_run=ctx.dry_run)
        run_sudo(["apt-get", "purge", "-y", *pkgs], dry_run=ctx.dry_run)
    # --no-x-check: the installer aborts in silent mode when it sees a running X/Xwayland session
    # (found 2026-10-31); the reboot that follows is the point of this step anyway.
    r = run_sudo(["sh", str(run_file), "--silent", "--no-kernel-modules", "--no-questions", "--no-x-check"],
                 check=False, dry_run=ctx.dry_run)
    if r.code != 0:
        log.err(f"installer output tail:\n{(r.stderr or r.stdout)[-1500:]}")
        _restore_apt(ctx)
        raise SystemExit("nvidia: the userspace installer failed (see /var/log/nvidia-installer.log); the apt driver was restored")
    if not ctx.dry_run and not _EGL_GBM_JSON.exists():
        raise SystemExit(f"nvidia: {_EGL_GBM_JSON} is missing after the installer; the desktop would lose EGL. "
                         f"Re-run: sudo sh {run_file} --silent --no-kernel-modules --no-questions --no-x-check")

    # DKMS source for the stock -generic kernel (our hyphaed kernel uses the .ko below).
    ver = cfg["version"]
    usrsrc = Path(f"/usr/src/nvidia-{ver}")
    if shutil.which("dkms"):
        run_sudo(["rm", "-rf", str(usrsrc)], dry_run=ctx.dry_run)
        run_sudo(["cp", "-a", str(src / "kernel-open"), str(usrsrc)], dry_run=ctx.dry_run)
        run_sudo(["dkms", "add", "-m", "nvidia", "-v", ver], check=False, dry_run=ctx.dry_run)

    dest = Path(f"/lib/modules/{kver}/{_EXTRA_DIR}")
    kos = [built / f"{m}.ko" for m in _MODULES if (built / f"{m}.ko").exists()]
    if len(kos) < 4:
        raise RuntimeError(f"expected nvidia/modeset/drm/uvm modules in {built}, found {[k.name for k in kos]}")
    run_sudo(["mkdir", "-p", str(dest)], dry_run=ctx.dry_run)
    for ko in kos:
        run_sudo(["install", "-m", "644", str(ko), str(dest / ko.name)], dry_run=ctx.dry_run)
    existing = _DEPMOD_CONF.read_text() if _DEPMOD_CONF.exists() else ""
    run_sudo(["tee", str(_DEPMOD_CONF)], input_str=_merge_depmod_conf(existing, kver, [k.stem for k in kos]),
             dry_run=ctx.dry_run)
    run_sudo(["depmod", "-a", kver], dry_run=ctx.dry_run)
    if shutil.which("dkms"):
        for k in sorted(d.name for d in Path("/lib/modules").iterdir() if (d / "build").exists()):
            run_sudo(["dkms", "autoinstall", "-k", k], check=False, dry_run=ctx.dry_run)
        _install_nvidia_fs(ctx, kver)


def _install_nvidia_fs(ctx, kver: str) -> None:
    """Build nvidia-fs (GDS) for `kver` when DKMS autoinstall skipped it (BUILD_DEPENDS=nvidia, see nvidia_fs.py)."""
    if not shutil.which("dkms") or not nvidia_fs_module_missing(kver):
        return
    ver = nvidia_fs_dkms_version(run(["dkms", "status"], check=False, force=True).stdout or "")
    if not ver:
        return
    r = run_sudo(["dkms", "install", f"nvidia-fs/{ver}", "-k", kver, "--force"], check=False, dry_run=ctx.dry_run)
    if r.code != 0:
        log.warn(f"nvidia-fs {ver} did not build for {kver} (GDS stays in POSIX compat mode); "
                 f"log: /var/lib/dkms/nvidia-fs/{ver}/{kver}/x86_64/log/make.log")


def run_phase(ctx) -> None:
    log.banner("Phase 10/12 — NVIDIA open modules from source")
    cfg = _cfg(ctx)
    if not cfg.get("from_source"):
        log.info("preset has no nvidia.from_source; skipping (apt/DKMS driver stays)")
        return
    if getattr(ctx.profile, "gpu_vendor", "none") != "nvidia":
        log.info("no NVIDIA GPU detected; skipping")
        return

    src = Path(cfg["src"]).expanduser()
    run_file = ctx.repo_root / cfg.get("run_file", f"build/nvidia/NVIDIA-Linux-x86_64-{cfg['version']}.run")
    kernel_tree = ctx.source_dir
    if kernel_tree is None or not (kernel_tree / ".config").exists():
        raise SystemExit("nvidia: no configured kernel build tree; run the build phase first")
    kver = f"{kernel_tree.name.removeprefix('linux-')}-hyphaed"   # the TARGET kernel, not the running one
    if not _preflight(ctx, cfg, src, run_file):
        raise SystemExit("nvidia: preflight failed (see above). Nothing was changed.")

    # Build (and sign) needs only the kernel's build tree, so it works before the reboot. Cached per kernel + commit.
    cache = _cache_dir(ctx, kver)
    if _cache_valid(cache, cfg):
        log.ok(f"modules for {kver} already built ({cache}); reusing")
        built = cache
    else:
        built = _build(ctx, cfg, src, kernel_tree, kver)
        if built is None:
            raise SystemExit("nvidia: module build failed. Nothing was installed.")
        if not ctx.dry_run:
            _sign(ctx, kernel_tree, [built / f"{m}.ko" for m in _MODULES if (built / f"{m}.ko").exists()])
            (built / "built.json").write_text(json.dumps({"commit": cfg["commit"], "kver": kver, "version": cfg["version"]}))
            (ctx.state_dir / "nvidia-built.json").write_text(json.dumps({"kver": kver, "cache": str(built)}))

    booted = _booted_into_target(kernel_tree)
    if not _gpu_jobs_clear():
        raise SystemExit("nvidia: stop the GPU compute jobs listed above, then run this step again. Nothing was changed.")
    log.info(
        f"Installing NVIDIA {cfg['version']}: purges apt nvidia 615.71.09, installs userspace "
        f"from the .run, puts optimized modules in /lib/modules/{kver}/{_EXTRA_DIR}. "
        f"The running driver stays 615.71.09 until you REBOOT: until then nvidia-smi reports a version "
        f"mismatch and NEW OpenGL/Vulkan windows may fail to open, so reboot right after this step. "
        f"Rollback: diagnostics/nvidia-restore-apt.sh"
    )
    if not booted:
        log.warn(f"You are running {os.uname().release}, not {kver}. The modules are installed for {kver} "
                 "(and rebuilt by DKMS for your other kernels, so booting any of them keeps a working driver).")
    if not ctx.dry_run and not confirm("Proceed with the NVIDIA install?", default=False, hard=True):
        raise SystemExit("nvidia: install declined; modules were built but nothing was installed")
    _install(ctx, cfg, src, run_file, kver, built)

    if not ctx.dry_run:
        (ctx.state_dir / "nvidia-installed.json").write_text(
            json.dumps({"version": cfg["version"], "kver": kver, "commit": cfg["commit"]}, indent=2))
    log.ok(f"NVIDIA {cfg['version']} installed for {kver}. REBOOT NOW (pick {kver}); then check nvidia-smi.")
