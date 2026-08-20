from __future__ import annotations
import argparse
import platform
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__, phases, presets
from .state import PersistedState, hydrate_ctx, snapshot_ctx
from .util import log
from .util.prompts import set_mode
from .util.run import set_dry_run

REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Ctx:
    repo_root: Path
    build_dir: Path
    state_dir: Path
    logs_dir: Path
    args: argparse.Namespace
    dry_run: bool
    non_interactive: bool
    preset: str | None
    profile: object | None = None
    source_dir: Path | None = None
    kernel_pkgver: str = ""
    source_mode: str = "kernel-org"
    built_debs: list[Path] = field(default_factory=list)
    packaged_debs: list[Path] = field(default_factory=list)
    persisted: PersistedState = field(default_factory=PersistedState)
    phases_this_run: list[str] = field(default_factory=list)


def _banner_intro() -> None:
    log.console.rule("[bold magenta]hyphaed[/bold magenta]")
    log.console.print("[muted]custom Linux kernel wizard — AI · gaming · virtualization[/muted]")
    log.console.print(f"[muted]version {__version__}[/muted]\n")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hyphaed",
        description="Build a custom Linux kernel optimized for AI inference, Steam/Proton, and VMware on Intel hybrid + NVIDIA workstations.",
    )
    p.add_argument("-v", "--version", action="version", version=f"hyphaed {__version__}")
    p.add_argument("--dry-run", action="store_true", help="print actions without executing them")
    p.add_argument("--non-interactive", action="store_true", help="never prompt")
    p.add_argument("-y", "--yes", action="store_true", help="auto-accept non-hard prompts")
    p.add_argument("--preset", default="auto", help=f"preset name (default: auto — detected from hardware). Available: {','.join(presets.list_available()) or 'none'}")
    p.add_argument("--phase", choices=phases.ORDER + ["all"], default="all", help="run a single phase (default: all)")
    # Default target/source-mode as of 2026-07-09: kernel.org 7.1.3 stable —
    # newer than any Ubuntu-packaged source (apt's linux-source tops out at
    # 7.0.0 on 26.04/resolute) and the version this exact combination
    # (gaming-ai-vm preset + CONFIG_DEBUG_INFO_BTF floor) was validated
    # against end-to-end (source fetch -> patch -> configure -> build ->
    # package, all green). `hyphaed` with NO flags is meant to be the one
    # good command — bump --target here when a newer kernel.org stable
    # lands and has been validated the same way, rather than expecting
    # every invocation to pass it explicitly.
    p.add_argument("--target", default="7.1.3", help="target kernel.org stable version, e.g. 7.1.3. Bump this default after validating a newer kernel.org stable release, or run `hyphaed list-versions` to see the last 5 releases with dates.")
    p.add_argument(
        "--source-mode", choices=["kernel-org"], default="kernel-org",
        help="kernel.org is the only source: clones the stable tag given by --target from the "
             "local github/kernelorg mirror and applies the full kernel-org-7.1 patch series — "
             "always newer than any distro-packaged source and BTF-floor-validated (see --target). "
             "Kept as an explicit flag for forward compatibility; there is currently no other value.",
    )
    p.add_argument("--from-phase", choices=phases.ORDER, default=None, help="resume from this phase")

    sub = p.add_subparsers(dest="cmd")
    # Subparsers intentionally do NOT redefine --target / --dry-run / --yes /
    # --non-interactive — those live at the top level only. Pass them BEFORE
    # the subcommand:  hyphaed --dry-run --target X rebase
    sub.add_parser("rebase", help="re-apply patches against a newer Ubuntu kernel (use --target)")
    sub.add_parser("detect", help="just show detected hardware profile and exit")
    sub.add_parser("list-presets", help="list available presets")
    sub.add_parser("status", help="report whether running kernel is hyphaed-built + module health")
    sub.add_parser("clean", help="wipe build/, out/debs/, out/logs/, state/")
    sub.add_parser("print-config", help="print composed cmdline + selected fragments (no actions)")
    sub.add_parser("uninstall", help="dpkg --purge all installed hyphaed kernels")
    sp_prune = sub.add_parser("prune", help="remove old hyphaed kernels keeping the newest N (default 2)")
    sp_prune.add_argument("--keep", type=int, default=2, help="how many newest kernels to keep (default 2)")
    sub.add_parser("verify", help="post-reboot health check (mitigations, BORE, cmdline, DKMS, …)")
    sub.add_parser("doctor", help="wide self-diagnostic: detect + verify + repo + driver/module health")
    sub.add_parser("update-cmdline", help="write/update GRUB cmdline drop-in only (no kernel build)")
    sub.add_parser("bisect", help="binary-search the fragment list to find a boot-breaking fragment")
    sub.add_parser("completion", help="emit bash/zsh completion script — `eval \"$(hyphaed completion)\"`")
    sp_compare = sub.add_parser("compare", help="diff two kernel .config files")
    sp_compare.add_argument("a", help="path to first .config (e.g. /boot/config-…-generic)")
    sp_compare.add_argument("b", help="path to second .config (e.g. /boot/config-…-hyphaed)")
    sp_compare.add_argument("--only-changed", action="store_true", help="omit keys present in only one file")
    sub.add_parser("install-deps", help="apt-install all kernel build dependencies in one shot")
    sp_scx = sub.add_parser("scx", help="install + switch sched_ext userspace schedulers")
    sp_scx.add_argument("action", choices=["install", "status", "run", "stop", "enable", "disable"],
                        help="install/status/run/stop — one-shot; enable/disable — persistent systemd unit")
    sp_scx.add_argument("scheduler", nargs="?", default="lavd",
                        help="lavd / bpfland / rusty / simple / nest (for `run` and `enable`)")
    sub.add_parser("snapshot", help="export current build state as state/snapshot-<date>.yaml")
    sub.add_parser("fetch-patches", help="download + verify vendored patches (wraps patches/fetch.py)")
    sub.add_parser("eval-patches", help="test-apply patch series against a source tree; classify CLEAN/CONFLICT/ALREADY-APPLIED (wraps patches/eval.py)")
    sp_mainline = sub.add_parser("check-mainline", help="query kernel.ubuntu.com/mainline: show newest stable tag and verify --target is built there")
    sp_mainline.add_argument("--json", action="store_true", dest="mainline_json", help="emit machine-readable JSON")
    sp_versions = sub.add_parser("list-versions", help="show the last N kernel.org stable releases with dates (default 5) — the same list the interactive wizard offers")
    sp_versions.add_argument("--count", "-n", type=int, default=5, help="how many releases to show (default 5)")
    sp_versions.add_argument("--json", action="store_true", dest="versions_json", help="emit machine-readable JSON")
    sp_sync = sub.add_parser("sync-sources", help="git fetch (or --pull) all local kernel source repos: linux, linux-cachyos, kernel-patches, linux-tkg, kernel-research")
    sp_sync.add_argument("--pull", action="store_true", help="git pull --ff-only instead of git fetch (integrates changes; may break VENDOR.lock SHAs)")

    return p


def _machine_state_dir() -> Path:
    """Return a per-machine state directory keyed by hostname.

    This keeps desktop and laptop state isolated when the repo is shared.
    Legacy state/ctx.json at the repo root is left untouched.
    """
    host = re.sub(r"[^a-z0-9-]", "-", platform.node().lower()) or "unknown"
    return REPO_ROOT / "state" / "machines" / host


def resolve_preset(preset_arg: str | None, profile=None) -> str:
    """Resolve 'auto' to a concrete preset name based on detected hardware."""
    if preset_arg and preset_arg != "auto":
        return preset_arg
    if profile is None:
        return "gaming-ai-vm"
    cpu_vendor = getattr(profile, "cpu_vendor", "unknown")
    chassis = getattr(profile, "chassis", "unknown")
    codename = getattr(profile, "cpu_codename", "")
    if cpu_vendor == "amd" and chassis == "laptop":
        resolved = "gaming-ai-laptop-amd"
    elif codename.startswith("raptorlake"):
        resolved = "gaming-ai-vm"
    else:
        resolved = "safe"
    log.info(f"auto preset → {resolved!r} (cpu={cpu_vendor} chassis={chassis} codename={codename})")
    return resolved


def _make_ctx(args: argparse.Namespace) -> Ctx:
    machine_state = _machine_state_dir()
    legacy_root_state = REPO_ROOT / "state"

    # First run on this machine: inform the user if legacy state exists
    if not machine_state.exists() and (legacy_root_state / "ctx.json").exists():
        log.info(
            f"per-machine state layout: using state/machines/{machine_state.name}/ "
            f"(legacy state/ctx.json is from an earlier run on a different machine — ignored)"
        )

    ctx = Ctx(
        repo_root=REPO_ROOT,
        build_dir=REPO_ROOT / "build",
        state_dir=machine_state,
        logs_dir=REPO_ROOT / "out" / "logs",
        args=args,
        dry_run=bool(getattr(args, "dry_run", False)),
        non_interactive=bool(getattr(args, "non_interactive", False)),
        preset=getattr(args, "preset", "auto"),
        source_mode=getattr(args, "source_mode", "kernel-org"),
    )
    ctx.state_dir.mkdir(parents=True, exist_ok=True)
    ctx.logs_dir.mkdir(parents=True, exist_ok=True)
    log.set_log_dir(ctx.logs_dir)
    set_mode(ctx.non_interactive, getattr(args, "yes", False))
    set_dry_run(ctx.dry_run)

    # Hydrate from prior runs so single-phase / resume modes work
    ctx.persisted = PersistedState.load(ctx.state_dir)
    if ctx.persisted.preset and (ctx.preset is None or ctx.preset == "auto"):
        ctx.preset = ctx.persisted.preset
    hydrate_ctx(ctx, ctx.persisted)

    # Resolve 'auto' preset using persisted profile if already detected
    if ctx.preset == "auto" and ctx.profile is not None:
        ctx.preset = resolve_preset("auto", ctx.profile)

    return ctx


def _maybe_keepalive_sudo(ctx: Ctx, phase_names: list[str]) -> None:
    needs_sudo = any(n in {"install", "postinstall"} for n in phase_names)
    if not needs_sudo or ctx.dry_run:
        return
    from .util.run import sudo_keepalive
    log.info("priming sudo credentials (one-time prompt)")
    sudo_keepalive()


def _run_phases(ctx: Ctx, phase_names: list[str]) -> None:
    _maybe_keepalive_sudo(ctx, phase_names)
    for name in phase_names:
        mod = phases.MODULES[name]
        log.console.print()
        start = time.monotonic()
        try:
            mod.run_phase(ctx)
        except SystemExit:
            raise
        except Exception as e:
            log.err(f"phase '{name}' failed: {e}")
            raise
        elapsed = time.monotonic() - start
        ctx.persisted.mark_complete(name, elapsed)
        ctx.phases_this_run.append(name)
        snapshot_ctx(ctx, ctx.persisted)
        if not ctx.dry_run:
            ctx.persisted.save(ctx.state_dir)
        log.info(f"phase '{name}' completed in {elapsed:.1f}s")


def _cmd_clean(ctx: Ctx) -> int:
    from .util.prompts import confirm
    targets = [ctx.build_dir, ctx.repo_root / "out" / "debs", ctx.repo_root / "out" / "logs", ctx.state_dir]
    log.info("will remove contents of:")
    for t in targets:
        log.console.print(f"  • {t}")
    if not confirm("proceed?", default=False):
        return 0
    for t in targets:
        if not t.exists():
            continue
        for child in t.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        log.ok(f"cleaned {t}")
    return 0


def _cmd_status(ctx: Ctx) -> int:
    from .topology import detect as topo_detect
    from .util.run import run
    profile = topo_detect()
    log.info(f"running kernel: {profile.running_kernel}")
    is_hyphaed = profile.running_kernel.endswith(f"-{profile.flavour}")
    if is_hyphaed:
        log.ok(f"this is a -{profile.flavour} kernel")
    else:
        log.warn(f"this is NOT a -{profile.flavour} kernel — boot one to verify the install")

    # cmdline as actually running
    cmdline = Path("/proc/cmdline").read_text().strip() if Path("/proc/cmdline").exists() else ""
    log.info(f"/proc/cmdline: {cmdline}")

    # Installed hyphaed packages
    r = run(["dpkg", "-l"], check=False)
    matching = [ln for ln in r.stdout.splitlines() if profile.flavour in ln and "linux-image" in ln]
    if matching:
        log.info("installed hyphaed kernel packages:")
        for ln in matching:
            log.console.print(f"  {ln.strip()}")
    else:
        log.warn(f"no linux-image-*-{profile.flavour} package installed")

    # GRUB default-entry staleness check (found live 2026-07-30: grubenv's
    # saved_entry can point at a kernel version that's since been removed —
    # GRUB then only boots the current kernel via fallback-to-newest-entry
    # ordering, not an explicit pin, which is fragile against a future menu
    # regeneration silently changing what boots by default). grubenv is
    # world-readable (unlike grub.cfg, root-only 0600), so this is a
    # read-only check.
    grubenv = Path("/boot/grub/grubenv")
    if grubenv.exists():
        try:
            saved = next(
                (ln.split("=", 1)[1] for ln in grubenv.read_text().splitlines()
                 if ln.startswith("saved_entry=")),
                None,
            )
        except OSError:
            saved = None
        if saved:
            m = re.search(r"with Linux (\S+)", saved)
            pinned_kver = m.group(1) if m else None
            if pinned_kver:
                pkg = run(["dpkg-query", "-W", "-f", "${Status}", f"linux-image-{pinned_kver}"], check=False)
                if "install ok installed" in pkg.stdout:
                    log.ok(f"GRUB default pin: {pinned_kver} (installed)")
                else:
                    log.warn(
                        f"grubenv's saved default points at linux-image-{pinned_kver}, "
                        f"which is NOT currently installed — GRUB is booting the running "
                        f"kernel only via fallback ordering, not an explicit pin. Fix: "
                        f"sudo grub-set-default 'Advanced options for Ubuntu>Ubuntu, "
                        f"with Linux {profile.running_kernel}'"
                    )
            else:
                log.info(f"grubenv saved_entry: {saved!r} (not a recognized title-path format, skipping check)")

    # Scheduler check (BORE)
    try:
        feats = Path("/sys/kernel/debug/sched/features")
        if feats.exists():
            log.info(f"sched features: {feats.read_text().strip()[:200]}")
    except PermissionError:
        log.info("(sched features: needs root to read)")

    # Mitigations summary
    vuln = Path("/sys/devices/system/cpu/vulnerabilities")
    if vuln.exists():
        vuln_count = sum(1 for f in vuln.iterdir() if "Vulnerable" in f.read_text())
        log.info(f"CPU vulnerabilities: {vuln_count} marked 'Vulnerable' (0 = all mitigated)")

    # Microcode revision — this box's BIOS (stock ASRock 11.02) does NOT embed
    # the fixed revision; the running 0x133 comes entirely from Ubuntu's
    # intel-microcode package via early-load. Warn if that path ever regresses
    # below Intel's first fix for the 13th/14th-gen voltage-instability erratum
    # (0x129) — see the project's security notes / CVE-2026-31431 context.
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.exists():
        for line in cpuinfo.read_text().splitlines():
            if line.startswith("microcode"):
                rev_str = line.split(":", 1)[1].strip()
                try:
                    rev = int(rev_str, 16)
                except ValueError:
                    rev = None
                if rev is not None and rev < 0x129:
                    log.warn(
                        f"microcode {rev_str} is OLDER than 0x129 (Raptor Lake "
                        f"voltage-instability fix) — check `intel-microcode` package "
                        f"is installed and up to date"
                    )
                else:
                    log.ok(f"microcode {rev_str} (>= 0x129 fix floor)")
                break

    return 0


def _cmd_print_config(ctx: Ctx) -> int:
    from .topology import detect as topo_detect
    from . import grub
    profile = topo_detect()
    preset_name = resolve_preset(ctx.preset, profile)
    try:
        preset = presets.load(preset_name)
    except FileNotFoundError:
        log.err(f"unknown preset: {preset_name}")
        return 2
    log.info(f"preset: {preset.name}")
    log.info(f"description: {preset.description.strip()}")
    log.console.print()
    log.info("config fragments (in merge order):")
    for f in preset.fragments:
        log.console.print(f"  • {f}")
    log.console.print()
    cmdline = grub.compose_cmdline(profile, preset.cmdline_extra)
    log.info("composed cmdline append:")
    for tok in cmdline:
        log.console.print(f"  {tok}")
    log.console.print()
    log.info("drop-in contents that would be written:")
    log.console.print(grub.render_dropin(cmdline))
    return 0


def _cmd_update_cmdline(ctx: Ctx) -> int:
    """Write the GRUB drop-in based on detected hardware — no kernel build."""
    from .topology import detect as topo_detect
    from . import grub, presets as presets_mod
    profile = topo_detect()
    extra: list[str] = []
    if ctx.preset:
        try:
            extra = presets_mod.load(ctx.preset).cmdline_extra
        except FileNotFoundError:
            pass
    current = grub.read_current_cmdline_config()
    cmdline = grub.compose_cmdline(profile, extra, skip_keys_present_in=current)
    log.info("cmdline diff:")
    grub.print_cmdline_diff(current, cmdline)
    path, changed = grub.write_dropin(cmdline, dry_run=ctx.dry_run)
    if changed and not ctx.dry_run:
        grub.update_grub(dry_run=ctx.dry_run)
        log.ok("cmdline updated — reboot for it to take effect")
    elif not changed:
        log.info("no changes needed")
    return 0


def _cmd_doctor(ctx: Ctx) -> int:
    """Unified self-diagnostic across the whole stack."""
    from rich.table import Table
    from .topology import detect as topo_detect
    from . import verify as verify_mod, greenboost as gb_mod
    from .util.run import run

    profile = topo_detect()
    log.banner("doctor — full system diagnostic")

    issues = 0

    # 1. Hardware profile
    log.info(f"CPU: {profile.cpu_brand}  P={profile.p_cores} E={profile.e_cores} threads={profile.total_threads}")
    log.info(f"GPU: {profile.gpu_model} ({profile.gpu_arch}) {profile.gpu_vram_gb} GB")
    log.info(f"Session: {profile.session_type}/{profile.desktop}, kernel {profile.running_kernel}")

    # 2. Repo integrity
    log.info("repo integrity:")
    repo_checks = [
        ("patches/series", (ctx.repo_root / "patches" / "series").exists()),
        ("configs/fragments/70-greenboost-required.config",
         (ctx.repo_root / "configs" / "fragments" / "70-greenboost-required.config").exists()),
        ("configs/presets/gaming-ai-vm.yaml",
         (ctx.repo_root / "configs" / "presets" / "gaming-ai-vm.yaml").exists()),
    ]
    for name, ok in repo_checks:
        if ok:
            log.ok(f"  {name}")
        else:
            log.err(f"  {name} MISSING")
            issues += 1

    # Count vendored patches actually present
    patches_dir = ctx.repo_root / "patches"
    patch_files = list(patches_dir.glob("*.patch"))
    log.info(f"  patches present: {len(patch_files)} (run `make fetch-patches` to populate)")

    # 3. Build toolchain
    log.info("build toolchain:")
    from .phases.detect import REQUIRED_TOOLS, BUILD_DEPS_PKGS
    import shutil as _sh
    missing_tools = [t for t in REQUIRED_TOOLS if not _sh.which(t)]
    if missing_tools:
        log.err(f"  missing: {' '.join(missing_tools)}  →  `hyphaed install-deps`")
        issues += 1
    else:
        log.ok(f"  all {len(REQUIRED_TOOLS)} tools present")

    # 4. NVIDIA driver floor
    if profile.gpu_vendor == "nvidia":
        from .phases.detect import _check_nvidia_driver
        _check_nvidia_driver(profile)

    # 5. Greenboost
    if profile.has_greenboost:
        gb_mod.maybe_log_summary(profile)
    else:
        log.info("  greenboost not detected (optional)")

    # 6. Run verify checks
    log.info("runtime state:")
    checks = verify_mod.run_all(flavour="hyphaed")
    for c in checks:
        if c.ok:
            log.ok(f"  {c.name}")
        else:
            log.warn(f"  {c.name}: {c.detail}")
            # boot-related checks count toward issues only if we're actually
            # on a -hyphaed kernel
            if profile.running_kernel.endswith("-hyphaed") and c.name != "expected cmdline tokens present":
                issues += 1

    # 7. /boot pressure
    free_mb = profile.boot_free_mb
    if free_mb < 400:
        log.err(f"/boot has only {free_mb} MB free — run `hyphaed prune --keep 2`")
        issues += 1
    elif free_mb < 800:
        log.warn(f"/boot {free_mb} MB free — getting tight; consider `hyphaed prune`")
    else:
        log.ok(f"/boot: {free_mb} MB free")

    log.console.print()
    if issues == 0:
        log.ok("doctor: all systems nominal")
        return 0
    log.warn(f"doctor: {issues} issue(s) found — see warnings above")
    return 1


def _cmd_bisect(ctx: Ctx) -> int:
    """Binary-search fragment list to find a boot-breaking fragment.

    Workflow (interactive):
      1. user runs `bisect` after a kernel that boots fails
      2. wizard takes the current fragment list, splits in half
      3. user picks "left set boots / right set boots / both fail"
      4. wizard narrows the suspect set, rebuild + reinstall + reboot
      5. repeat until 1 fragment remains
    """
    from .util.prompts import confirm, text
    state_file = ctx.state_dir / "bisect.json"
    log.info("bisect mode — binary search fragments to find a culprit")
    log.warn("each round requires a full rebuild + reboot + manual report")

    fragments: list[str] = []
    if ctx.preset:
        from . import presets as presets_mod
        try:
            preset = presets_mod.load(ctx.preset)
            fragments = list(preset.fragments)
        except FileNotFoundError:
            pass
    if not fragments:
        log.err("no preset loaded; specify one with --preset")
        return 2

    # Load existing state if any
    import json as _json
    if state_file.exists():
        st = _json.loads(state_file.read_text())
        suspect = st.get("suspect", fragments)
        confirmed_safe = st.get("safe", [])
    else:
        suspect = list(fragments)
        confirmed_safe = []

    log.info(f"current suspect set ({len(suspect)} fragments):")
    for f in suspect:
        log.console.print(f"  • {f}")
    if len(suspect) <= 1:
        log.ok(f"bisect converged → likely culprit: {suspect[0] if suspect else '(none)'}")
        return 0

    half = len(suspect) // 2
    left, right = suspect[:half], suspect[half:]
    log.info(f"next test split:")
    log.console.print(f"  LEFT  ({len(left)}): {', '.join(left)}")
    log.console.print(f"  RIGHT ({len(right)}): {', '.join(right)}")

    side = text("which side did you boot with? (left/right/both-failed)", default="left")
    booted = text("did it boot? (y/n)", default="y").lower().startswith("y")

    if side == "left":
        if booted:
            suspect = right
            confirmed_safe.extend(left)
        else:
            suspect = left
    elif side == "right":
        if booted:
            suspect = left
            confirmed_safe.extend(right)
        else:
            suspect = right
    else:
        log.warn("both-failed — culprit may be in 'safe' set or interaction effect; widening")

    state_file.write_text(_json.dumps({"suspect": suspect, "safe": confirmed_safe}, indent=2))
    log.ok(f"saved bisect state; suspect set now {len(suspect)} fragments")
    log.info("rebuild with the new suspect set and reboot, then re-run `hyphaed bisect`")
    return 0


def _cmd_completion() -> int:
    """Emit a bash completion script."""
    from . import phases, presets
    sub_cmds = "rebase detect list-presets status clean print-config uninstall prune verify doctor update-cmdline bisect completion compare install-deps scx snapshot fetch-patches"
    phases_str = " ".join(phases.ORDER)
    presets_str = " ".join(presets.list_available())
    script = f'''# hyphaed bash completion — `eval "$(hyphaed completion)"` or save to /etc/bash_completion.d/hyphaed
_hyphaed() {{
    local cur prev words cword
    _init_completion || return
    local subcommands="{sub_cmds}"
    local phases="{phases_str}"
    local presets="{presets_str}"
    case "$prev" in
        --phase|--from-phase) COMPREPLY=( $(compgen -W "$phases" -- "$cur") ); return 0 ;;
        --preset)             COMPREPLY=( $(compgen -W "$presets" -- "$cur") ); return 0 ;;
        --target)             COMPREPLY=( $(compgen -W "" -- "$cur") ); return 0 ;;
        compare)              COMPREPLY=( $(compgen -f -- "$cur") ); return 0 ;;
        scx)                  COMPREPLY=( $(compgen -W "install status run stop" -- "$cur") ); return 0 ;;
    esac
    if [[ "$cur" == -* ]]; then
        COMPREPLY=( $(compgen -W "--dry-run --non-interactive --yes --preset --phase --from-phase --target --help" -- "$cur") )
        return 0
    fi
    if [[ $cword -eq 1 ]]; then
        COMPREPLY=( $(compgen -W "$subcommands" -- "$cur") )
    fi
}}
complete -F _hyphaed hyphaed
complete -F _hyphaed python3
'''
    print(script)
    return 0


def _cmd_compare(a: str, b: str, only_changed: bool) -> int:
    from rich.table import Table
    from .kconfig import parse_config
    pa, pb = Path(a), Path(b)
    if not pa.exists() or not pb.exists():
        log.err(f"missing: {pa if not pa.exists() else pb}")
        return 2
    ca, cb = parse_config(pa), parse_config(pb)
    keys = sorted(set(ca) | set(cb))
    t = Table(title=f"{pa.name}  vs  {pb.name}", header_style="bold magenta")
    t.add_column("CONFIG_*", style="cyan", no_wrap=True)
    t.add_column(pa.name, justify="right")
    t.add_column(pb.name, justify="right")
    t.add_column("Δ", justify="center")
    rows_added = 0
    for k in keys:
        va = ca.get(k, "·")
        vb = cb.get(k, "·")
        if va == vb:
            if only_changed:
                continue
            mark = ""
        elif va == "·":
            mark = "[ok]+[/ok]"
        elif vb == "·":
            mark = "[err]-[/err]"
        else:
            mark = "[warn]~[/warn]"
        t.add_row(k, va, vb, mark)
        rows_added += 1
    log.console.print(t)
    log.info(f"{rows_added} rows, {len(keys)} symbols in union")
    return 0


def _cmd_install_deps() -> int:
    import shutil
    from .phases.detect import BUILD_DEPS_PKGS
    from .util.run import run_sudo, sudo_keepalive
    sudo_keepalive()
    if not shutil.which("nala"):
        log.info("nala not found — bootstrapping via apt first")
        run_sudo(["apt", "install", "-y", "nala"])
    log.info(f"installing {len(BUILD_DEPS_PKGS)} build dependency packages via nala")
    run_sudo(["nala", "update"])
    run_sudo(["nala", "install", "-y", *BUILD_DEPS_PKGS])
    log.ok("build dependencies installed")
    return 0


def _cmd_scx(action: str, scheduler: str) -> int:
    """Manage sched_ext userspace schedulers (BPF-based)."""
    from .util.run import run, run_sudo, sudo_keepalive
    if action == "install":
        sudo_keepalive()
        # Ubuntu 25.04+ ships the umbrella package as `scx-scheds` (verified
        # 2026-08-07 against a real 26.04 box — the earlier `scx`/`scx-loader`
        # names don't exist in the archive; `check=False` below previously
        # swallowed that failure and printed a false "installed" message
        # regardless of outcome).
        run_sudo(["apt", "update"], check=False)
        r = run_sudo(["apt", "install", "-y", "scx-scheds"], check=False)
        if not r.ok():
            log.err("apt install scx-scheds failed — see output above")
            return 2
        log.ok("scx-scheds installed — `hyphaed scx run lavd` to switch scheduler")
        return 0
    if action == "status":
        if not Path("/sys/kernel/sched_ext").exists():
            log.err("sched_ext sysfs not found — kernel was built without CONFIG_SCHED_CLASS_EXT")
            return 1
        cur = Path("/sys/kernel/sched_ext/state")
        if cur.exists():
            log.info(f"sched_ext state: {cur.read_text().strip()}")
        nr_running = Path("/sys/kernel/sched_ext/nr_rejected")
        if nr_running.exists():
            log.info(f"nr_rejected: {nr_running.read_text().strip()}")
        log.info("available userspace schedulers:")
        for name in ("scx_lavd", "scx_bpfland", "scx_rusty", "scx_simple", "scx_nest", "scx_central"):
            import shutil as _sh
            if _sh.which(name):
                log.console.print(f"  • {name}")
        return 0
    if action == "stop":
        sudo_keepalive()
        # Each scheduler is one binary; killing it returns to CFS.
        for name in ("scx_lavd", "scx_bpfland", "scx_rusty", "scx_simple", "scx_nest", "scx_central"):
            run_sudo(["pkill", "-x", name], check=False)
        log.ok("stopped all scx schedulers; system back on CFS/EEVDF")
        return 0
    if action == "run":
        sudo_keepalive()
        bin_name = scheduler if scheduler.startswith("scx_") else f"scx_{scheduler}"
        import shutil as _sh
        if not _sh.which(bin_name):
            log.err(f"{bin_name} not on PATH — `hyphaed scx install` first")
            return 2
        # Stop any current scheduler first
        _cmd_scx("stop", scheduler)
        log.info(f"launching {bin_name} as a daemon (Ctrl-C to stop)")
        # Run in foreground — user can Ctrl-C; for persistent boot use `enable`
        run_sudo([bin_name])
        return 0

    if action in ("enable", "disable"):
        return _cmd_scx_systemd(action, scheduler)

    return 2


_SCX_UNIT = "hyphaed-scx.service"
_SCX_UNIT_PATH = Path("/etc/systemd/system") / _SCX_UNIT


def _cmd_scx_systemd(action: str, scheduler: str) -> int:
    """Install or remove a systemd unit that starts a sched_ext scheduler at boot."""
    from .util.run import run_sudo, sudo_keepalive
    sudo_keepalive()

    if action == "disable":
        run_sudo(["systemctl", "disable", "--now", _SCX_UNIT], check=False)
        run_sudo(["rm", "-f", str(_SCX_UNIT_PATH)], check=False)
        run_sudo(["systemctl", "daemon-reload"], check=False)
        log.ok(f"disabled and removed {_SCX_UNIT}")
        return 0

    # action == "enable"
    bin_name = scheduler if scheduler.startswith("scx_") else f"scx_{scheduler}"
    import shutil as _sh
    if not _sh.which(bin_name):
        log.err(f"{bin_name} not on PATH — `hyphaed scx install` first, then `enable`")
        return 2

    unit_text = f"""\
[Unit]
Description=hyphaed sched_ext scheduler ({bin_name})
Documentation=https://github.com/sched-ext/scx
After=multi-user.target
ConditionPathExists=/sys/kernel/sched_ext

[Service]
Type=simple
ExecStart={_sh.which(bin_name)}
Restart=on-failure
RestartSec=5
OOMScoreAdjust=-500

[Install]
WantedBy=multi-user.target
"""
    _SCX_UNIT_PATH.write_text(unit_text)
    log.ok(f"wrote {_SCX_UNIT_PATH}")
    run_sudo(["systemctl", "daemon-reload"])
    run_sudo(["systemctl", "enable", "--now", _SCX_UNIT])
    log.ok(f"{bin_name} enabled as a systemd service (starts on boot)")
    log.info("  disable: hyphaed scx disable")
    log.info("  status:  systemctl status hyphaed-scx")
    return 0


def _cmd_prune(ctx: Ctx, keep: int) -> int:
    """Remove old hyphaed kernels keeping the newest `keep` by version."""
    from .util.run import run, run_sudo
    from .util.prompts import confirm
    if keep < 1:
        log.err("--keep must be >= 1")
        return 2
    r = run(["dpkg-query", "-f", "${Package}\\t${Version}\\n", "-W", "linux-image-*"], check=False)
    rows = []
    for ln in r.stdout.splitlines():
        if "-hyphaed" not in ln:
            continue
        try:
            pkg, ver = ln.split("\t", 1)
        except ValueError:
            continue
        rows.append((pkg.strip(), ver.strip()))
    if not rows:
        log.info("no hyphaed kernels installed; nothing to prune")
        return 0
    # Don't touch the currently-running kernel
    from .topology import detect as topo_detect
    profile = topo_detect()
    running_uname = profile.running_kernel
    running_pkg = f"linux-image-{running_uname}"

    # Sort by version, newest last (dpkg version semantics — defer to dpkg --compare-versions if needed)
    rows.sort(key=lambda r: r[1])
    keepers = rows[-keep:]
    casualties = [p for p, _v in rows[:-keep] if p != running_pkg]
    protected_running = running_pkg in (p for p, _v in rows[:-keep])

    if not casualties:
        log.info(f"have {len(rows)} hyphaed kernel(s); keep={keep}; nothing to prune")
        return 0

    log.info(f"keeping {len(keepers)} newest:")
    for p, v in keepers:
        log.console.print(f"  • {p}  ({v})")
    log.info("would purge:")
    for p in casualties:
        log.console.print(f"  ✗ {p}")
    if protected_running:
        log.warn(f"running kernel {running_pkg} is in the casualty list — protecting it")

    if not confirm("proceed?", default=False, hard=True):
        return 0
    # Also purge matching headers
    pkgs: list[str] = []
    for c in casualties:
        pkgs.append(c)
        headers = c.replace("linux-image-", "linux-headers-")
        pkgs.append(headers)
    run_sudo(["dpkg", "--purge", *pkgs], check=False)
    run_sudo(["update-grub"])
    log.ok(f"pruned {len(casualties)} kernel(s)")
    return 0


def _cmd_uninstall(ctx: Ctx) -> int:
    from .util.run import run, run_sudo
    from .util.prompts import confirm
    r = run(["dpkg-query", "-f", "${Package}\\n", "-W", "linux-image-*"], check=False)
    pkgs = [ln.strip() for ln in r.stdout.splitlines() if "-hyphaed" in ln]
    pkgs += [
        ln.strip() for ln in
        run(["dpkg-query", "-f", "${Package}\\n", "-W", "linux-headers-*"], check=False).stdout.splitlines()
        if "-hyphaed" in ln
    ]
    if not pkgs:
        log.info("no hyphaed-* kernel packages installed")
        return 0
    log.info("will purge:")
    for p in pkgs:
        log.console.print(f"  • {p}")
    if not confirm("proceed?", default=False, hard=True):
        return 0
    run_sudo(["dpkg", "--purge", *pkgs])
    run_sudo(["update-grub"])
    log.ok("hyphaed kernels purged")
    return 0


def _cmd_snapshot(ctx: Ctx) -> int:
    """Export current build state as a portable YAML snapshot."""
    import datetime, json as _json
    try:
        import yaml as _yaml
        _have_yaml = True
    except ImportError:
        _have_yaml = False

    from .topology import detect as topo_detect
    from . import grub as grub_mod, presets as presets_mod
    profile = topo_detect()
    preset_name = resolve_preset(ctx.preset, profile)

    config_sha = ""
    config_sha_file = ctx.state_dir / "config.sha256"
    if config_sha_file.exists():
        config_sha = config_sha_file.read_text().strip()

    fragments: list[str] = []
    try:
        preset = presets_mod.load(preset_name)
        fragments = list(preset.fragments)
    except FileNotFoundError:
        pass

    current_cmdline = grub_mod.read_current_cmdline_config()
    composed_cmdline = grub_mod.compose_cmdline(profile, [], skip_keys_present_in=current_cmdline)

    data = {
        "snapshot_version": 1,
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "hardware": {
            "cpu": profile.cpu_brand,
            "cpu_codename": profile.cpu_codename,
            "p_cores": profile.p_cores,
            "e_cores": profile.e_cores,
            "total_threads": profile.total_threads,
            "p_thread_mask": profile.p_thread_mask,
            "e_thread_mask": profile.e_thread_mask,
            "ram_gb": profile.ram_gb,
            "gpu": profile.gpu_model,
            "gpu_arch": profile.gpu_arch,
            "gpu_vram_gb": profile.gpu_vram_gb,
            "iommu": profile.iommu_present,
            "secure_boot": profile.secure_boot,
        },
        "session": {
            "type": profile.session_type,
            "desktop": profile.desktop,
            "running_kernel": profile.running_kernel,
            "ubuntu_release": profile.ubuntu_release,
        },
        "build": {
            "preset": preset_name,
            "fragments": fragments,
            "config_sha256": config_sha,
            "kernel_pkgver": ctx.persisted.kernel_pkgver or "",
            "phases_completed": list(ctx.persisted.phases_completed),
            "built_debs": [str(p) for p in (ctx.persisted.packaged_debs or [])],
        },
        "cmdline": {
            "current_tokens": current_cmdline,
            "hyphaed_tokens": composed_cmdline,
        },
    }

    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = ctx.state_dir / f"snapshot-{ts}.yaml"
    if _have_yaml:
        out.write_text(_yaml.dump(data, default_flow_style=False, sort_keys=False))
    else:
        out.write_text(_json.dumps(data, indent=2))
        log.info("pyyaml not installed — wrote JSON instead (pip install pyyaml for YAML)")
    log.ok(f"snapshot written to {out}")
    return 0


def _cmd_sync_sources(ctx: Ctx, pull: bool) -> int:
    """git fetch --all --tags (or pull --ff-only) on all local kernel source repos."""
    from .util.run import run as _run

    repos = {
        "linux":           ctx.repo_root / "linux",
        "linux-cachyos":   ctx.repo_root / "linux-cachyos",
        "kernel-patches":  ctx.repo_root / "kernel-patches",
        "linux-tkg":       ctx.repo_root / "linux-tkg",
        "kernel-research": ctx.repo_root / "kernel-research",
    }
    action = "pull" if pull else "fetch"
    errors: list[str] = []

    for name, path in repos.items():
        if not (path / ".git").exists():
            log.warn(f"  {name}: not a git repo at {path} — skip")
            continue
        log.info(f"  {name}: git {action} …")
        if pull:
            cmd = ["git", "-C", str(path), "pull", "--ff-only"]
        else:
            cmd = ["git", "-C", str(path), "fetch", "--all", "--tags"]
        r = _run(cmd, check=False, force=True)
        if r.ok():
            tail = (r.stdout or "").strip().splitlines()
            summary = tail[-1] if tail else "ok"
            if "already up to date" in summary.lower() or "already up-to-date" in summary.lower():
                log.ok(f"  {name}: already up to date")
            else:
                log.ok(f"  {name}: {summary}")
        else:
            first_err = ((r.stderr or "") + (r.stdout or "")).strip().splitlines()
            log.warn(f"  {name}: {first_err[0] if first_err else 'git error'}")
            errors.append(name)

    vendor_lock = ctx.repo_root / "patches" / "VENDOR.lock"
    if pull and vendor_lock.exists() and not errors:
        log.warn("patches/VENDOR.lock references specific git SHAs — verify they still resolve:")
        log.warn("  python patches/fetch.py  (or `hyphaed fetch-patches`)")

    if errors:
        log.warn(f"git {action} failed for: {', '.join(errors)}")
        return 1

    log.ok(f"sync-sources complete ({action})")
    return 0


def _cmd_fetch_patches(ctx: Ctx) -> int:
    """Download + verify vendored patches (wraps patches/fetch.py)."""
    import subprocess, sys
    patches_py = ctx.repo_root / "patches" / "fetch.py"
    if not patches_py.exists():
        log.err(f"patches/fetch.py not found at {patches_py}")
        return 2
    cmd = [sys.executable, str(patches_py)]
    log.info(f"running: {' '.join(cmd)}")
    r = subprocess.run(cmd, cwd=str(ctx.repo_root))
    if r.returncode != 0:
        log.err("patch fetch had failures — see output above")
        log.info("hint: run `python patches/fetch.py --discover` to compute sha256 values for VENDOR.lock")
    else:
        log.ok("all patches downloaded and verified")
    return r.returncode


def _cmd_eval_patches(ctx: Ctx) -> int:
    """Test-apply patch series against a source tree (wraps patches/eval.py)."""
    import subprocess, sys
    eval_py = ctx.repo_root / "patches" / "eval.py"
    if not eval_py.exists():
        log.err(f"patches/eval.py not found at {eval_py}")
        return 2
    # Pass-through remaining argv so the user can supply --against / --series / --sauce
    extra = sys.argv[sys.argv.index("eval-patches") + 1:] if "eval-patches" in sys.argv else []
    if not extra:
        log.err(
            "eval-patches requires arguments — e.g.:\n"
            "  hyphaed eval-patches --against build/linux-7.1.1 "
            "--series patches/kernel-org-7.1/series\n"
            "  hyphaed eval-patches --against build/linux-7.1.1 --sauce"
        )
        return 2
    cmd = [sys.executable, str(eval_py)] + extra
    log.info(f"running: {' '.join(cmd)}")
    r = subprocess.run(cmd, cwd=str(ctx.repo_root))
    return r.returncode


def _cmd_check_mainline(ctx: Ctx, emit_json: bool = False) -> int:
    """Query kernel.ubuntu.com/mainline: newest stable + --target availability."""
    import json as _json
    from .phases.source import (
        MAINLINE_PPA,
        _mainline_available_tags,
        _latest_mainline_stable,
        _mainline_has_amd64_build,
    )

    target = getattr(ctx.args, "target", None)
    log.info(f"querying {MAINLINE_PPA} …")
    tags = _mainline_available_tags()
    if not tags:
        log.err(f"could not reach {MAINLINE_PPA} — check network")
        return 1

    latest = _latest_mainline_stable(tags)
    target_built: bool | None = None

    if target:
        norm = target if target.startswith("v") else f"v{target}"
        target_built = _mainline_has_amd64_build(norm)

    if emit_json:
        data: dict = {"latest_stable": latest, "total_tags": len(tags)}
        if target:
            data["target"] = target
            data["target_has_amd64_build"] = target_built
        print(_json.dumps(data, indent=2))
        return 0 if (target_built is not False) else 1

    log.console.print(f"  latest stable: [bold]{latest}[/bold]  ({len(tags)} tags indexed)")
    if target:
        norm = target if target.startswith("v") else f"v{target}"
        if target_built:
            log.ok(f"{norm} has an amd64 build on kernel.ubuntu.com/mainline ✓")
        else:
            log.err(f"{norm} has NO amd64 build on kernel.ubuntu.com/mainline")
            if latest:
                log.info(f"suggest: python -m hyphaed --target {latest.lstrip('v')}")
            return 1

    return 0


def _cmd_list_versions(count: int, emit_json: bool = False) -> int:
    """Show the last N kernel.org stable releases with dates — the same data
    the interactive wizard (install_wizard.sh and the Python source-fetch
    picker) shows, exposed as a standalone command so the bash wizard can
    shell out to it (`python -m hyphaed list-versions --json`) instead of
    duplicating the tag/date logic."""
    import json as _json
    from .phases.source import _GITHUB_REPOS, _stable_tags_with_dates

    entries = _stable_tags_with_dates(_GITHUB_REPOS["kernelorg"], n=count)
    if not entries:
        if emit_json:
            print(_json.dumps({"releases": []}))
        else:
            log.err("no local kernelorg mirror found at github/kernelorg/linux")
        return 1

    if emit_json:
        data = [{"version": ver, "released": date} for ver, date in entries]
        print(_json.dumps({"releases": data}, indent=2))
        return 0

    from rich.table import Table
    t = Table(title=f"Last {len(entries)} kernel.org stable releases", box=None, pad_edge=False)
    t.add_column("Version", style="cyan")
    t.add_column("Released", style="white")
    for ver, date in entries:
        t.add_row(ver, date)
    log.console.print(t)
    return 0


def _phase_deps() -> dict[str, list[str]]:
    """Transitive prerequisites per phase, used by `--phase X` to auto-run
    whatever `phases_completed` doesn't already cover.

    NOTE: "patch" must be a transitive prerequisite of configure/build/
    package/install/postinstall — omitting it (as this dict did until
    2026-07-09) means `--phase package` silently builds a VANILLA kernel with
    the entire vendored BORE/xanmod/ACS-override series never applied, no
    error, no warning. Confirmed via a real build-package run: source→configure
    ran back-to-back with no patch-phase banner in between at all. "security"
    was similarly missing until this fix — same class of silent-skip bug.
    """
    return {
        "detect":      [],
        "source":      ["detect"],
        "patch":       ["detect", "source"],
        "configure":   ["detect", "source", "patch"],
        "security":    ["detect", "source", "patch", "configure"],
        "build":       ["detect", "source", "patch", "configure", "security"],
        "package":     ["detect", "source", "patch", "configure", "security", "build"],
        "install":     ["detect", "source", "patch", "configure", "security", "build", "package"],
        "postinstall": ["detect", "source", "patch", "configure", "security", "build", "package", "install"],
    }


def _summary_table(ctx: Ctx) -> None:
    """Render phase status, distinguishing phases that actually ran THIS
    invocation from ones only known complete from a prior run's persisted
    state — otherwise e.g. `--phase detect` alone would print a previous
    build's multi-minute `build`/`install` timings as if they just happened."""
    from rich.table import Table
    t = Table(title="hyphaed run summary", header_style="bold magenta")
    t.add_column("phase", style="cyan")
    t.add_column("status")
    t.add_column("time (s)", justify="right")
    for name in phases.ORDER:
        secs = ctx.persisted.phase_timings_sec.get(name)
        if name in ctx.phases_this_run:
            status, style = "✓", "ok"
        elif name in ctx.persisted.phases_completed:
            status, style = "· (cached)", "muted"
        else:
            status, style = "-", "muted"
            secs = None
        t.add_row(
            name,
            f"[{style}]{status}[/{style}]",
            f"{secs:.1f}" if secs is not None else "",
        )
    log.console.print(t)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Any --json subcommand owns stdout exclusively. Do this BEFORE the
    # banner, or the banner is the first thing a JSON consumer parses.
    if getattr(args, "versions_json", False) or getattr(args, "mainline_json", False):
        log.route_diagnostics_to_stderr()

    _banner_intro()

    if args.cmd == "list-presets":
        for n in presets.list_available():
            log.console.print(f"  • {n}")
        return 0

    ctx = _make_ctx(args)

    if args.cmd == "detect":
        phases.detect.run_phase(ctx)
        return 0

    if args.cmd == "clean":
        return _cmd_clean(ctx)

    if args.cmd == "status":
        return _cmd_status(ctx)

    if args.cmd == "print-config":
        return _cmd_print_config(ctx)

    if args.cmd == "uninstall":
        return _cmd_uninstall(ctx)

    if args.cmd == "prune":
        return _cmd_prune(ctx, args.keep)

    if args.cmd == "verify":
        from . import verify as verify_mod
        return verify_mod.render(verify_mod.run_all(flavour="hyphaed"))

    if args.cmd == "compare":
        return _cmd_compare(args.a, args.b, args.only_changed)

    if args.cmd == "install-deps":
        return _cmd_install_deps()

    if args.cmd == "scx":
        return _cmd_scx(args.action, args.scheduler)

    if args.cmd == "update-cmdline":
        return _cmd_update_cmdline(ctx)

    if args.cmd == "doctor":
        return _cmd_doctor(ctx)

    if args.cmd == "bisect":
        return _cmd_bisect(ctx)

    if args.cmd == "completion":
        return _cmd_completion()

    if args.cmd == "snapshot":
        return _cmd_snapshot(ctx)

    if args.cmd == "fetch-patches":
        return _cmd_fetch_patches(ctx)

    if args.cmd == "eval-patches":
        return _cmd_eval_patches(ctx)

    if args.cmd == "check-mainline":
        return _cmd_check_mainline(ctx, emit_json=getattr(args, "mainline_json", False))

    if args.cmd == "list-versions":
        return _cmd_list_versions(getattr(args, "count", 5), emit_json=getattr(args, "versions_json", False))

    if args.cmd == "sync-sources":
        return _cmd_sync_sources(ctx, pull=getattr(args, "pull", False))

    if args.cmd == "rebase":
        if not args.target:
            log.err("rebase requires --target (e.g. --target 7.0.0-16-generic) BEFORE the `rebase` keyword")
            return 2
        phases.detect.run_phase(ctx)
        phases.source.run_phase(ctx)

        # Pre-flight: show which patches touch files changed since previous base
        series_file = ctx.repo_root / "patches" / "series"
        if series_file.exists() and ctx.source_dir and ctx.source_dir.exists():
            from . import gitops
            from .version import base_git_tag
            old_tag = base_git_tag(ctx.profile)
            # New target base tag (from --target or detected)
            from .topology import HardwareProfile
            import copy
            new_profile = copy.copy(ctx.profile)
            target_str = args.target.lstrip("linux-image-")
            parts = target_str.split("-")
            if len(parts) >= 2:
                new_profile.running_kernel = target_str
            new_tag = base_git_tag(new_profile)
            patches_for_preflight = []
            for line in series_file.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    p = ctx.repo_root / "patches" / line
                    if p.exists():
                        patches_for_preflight.append(p)
            if patches_for_preflight:
                gitops.rebase_preflight_report(ctx.source_dir, old_tag, new_tag, patches_for_preflight)

        phases.patch.run_phase(ctx)
        log.ok("rebase complete — review state/applied-series.json and run `hyphaed --from-phase configure` to continue")
        return 0

    if args.phase != "all":
        # Resolve dependencies: if requested phase needs earlier outputs we
        # don't have, auto-run them first (instead of just warning).
        deps = _phase_deps()
        to_run: list[str] = []
        for d in deps.get(args.phase, []):
            if d not in ctx.persisted.phases_completed:
                to_run.append(d)
        if to_run:
            log.info(f"auto-running missing prerequisites: {', '.join(to_run)}")
        to_run.append(args.phase)
        _run_phases(ctx, to_run)
        _summary_table(ctx)
        return 0

    start = args.from_phase or phases.ORDER[0]
    idx = phases.ORDER.index(start)
    _run_phases(ctx, phases.ORDER[idx:])
    log.console.rule("[ok]done[/ok]")
    log.ok("kernel build & install pipeline complete")
    _summary_table(ctx)
    return 0


if __name__ == "__main__":
    sys.exit(main())
