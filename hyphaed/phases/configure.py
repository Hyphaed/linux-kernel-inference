from __future__ import annotations
import json
import shutil
import sys
import re
from pathlib import Path

from rich.markup import escape
from ..util import log
from ..util.run import run
from ..util.prompts import confirm, select
from ..util.checksum import sha256_file
from .. import kconfig

NAME = "configure"


def _run_hardening_check(ctx, config: Path) -> None:
    """Run kernel-hardening-checker against the final .config and save a JSON report.

    Non-blocking: failures here never abort the build.
    """
    khc = ctx.repo_root / "kernel-hardening-checker" / "bin" / "kernel-hardening-checker"
    if not khc.exists():
        log.info("kernel-hardening-checker not found — skipping hardening report")
        return

    out_dir = ctx.repo_root / "out" / "security"
    out_dir.mkdir(parents=True, exist_ok=True)
    # kernel_pkgver is only assigned in build.py, well after this phase runs
    # (see hyphaed/phases/build.py::run_phase) — using it here means a fresh
    # `configure` reports the PRIOR run's package version (or "unknown" on a
    # first run), mislabeling this run's hardening report. source_dir is
    # already resolved by the time `configure` runs and always matches the
    # tree actually being configured, so prefer deriving the label from it.
    source_dir = getattr(ctx, "source_dir", None)
    if source_dir:
        pkgver = Path(source_dir).name.removeprefix("linux-")
    else:
        pkgver = getattr(ctx, "kernel_pkgver", None) or "unknown"
    report_path = out_dir / f"hardening-{pkgver}.json"

    r = run(["python3", str(khc), "-c", str(config), "-m", "json"],
            check=False, force=True)
    if not r.ok() or not r.stdout.strip():
        log.warn("kernel-hardening-checker failed — skipping hardening report")
        return

    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        log.warn("kernel-hardening-checker output was not valid JSON — skipping")
        return

    report_path.write_text(json.dumps(data, indent=2))

    # Tally results
    ok = fail = warn = 0
    for entry in data if isinstance(data, list) else []:
        result = str(entry.get("check_result", ""))
        if result.startswith("OK"):
            ok += 1
        elif result.startswith("FAIL"):
            fail += 1
        else:
            warn += 1
    total = ok + fail + warn
    msg = f"{ok}/{total} OK"
    if fail:
        msg += f"  [red]{fail} FAIL[/red]"
    if warn:
        msg += f"  [yellow]{warn} warn[/yellow]"
    # console.print, NOT log.ok: log.ok() runs escape() over its argument, on
    # purpose, so that arbitrary content (paths, config values, tool output)
    # can never inject markup or break rendering. Handing it a string that
    # already contains markup therefore prints the tags literally —
    # "(147/258 OK  [red]111 FAIL[/red])" is what this line emitted.
    # The counts here are ours; report_path.name is not, so escape that one.
    log.console.print(
        f"[ok]✓[/ok]  hardening report → {escape(report_path.name)}  ({msg})")
    log.console.print(
        f"  [dim]checks Kconfig symbols against KSPP/CLIP OS/grsec hardening "
        f"recommendations (STACKPROTECTOR, FORTIFY, CFI, IOMMU isolation, …). "
        f"FAIL items are attack-surface gaps; warn items are advisory.[/dim]"
    )
    if fail:
        log.warn("  run `kernel-hardening-checker -c .config` for the full list")


def _write_machine_topology_fragment(ctx) -> Path | None:
    """Emit a machine-specific fragment derived from the detected HardwareProfile.

    Two classes of symbol genuinely depend on the box being built for, so they
    must not be hard-coded into a hand-maintained fragment:

    * `CPU_SUP_*` — x86 vendor support. Ubuntu's generic config compiles in
      Intel + AMD + Hygon + Centaur + Zhaoxin so one binary boots anything.
      A hyphaed kernel is already non-portable (81-native-cpu-opt passes
      -march=native), so keeping the four vendors this machine can never be is
      dead code in the hot CPU-init paths. Getting this wrong is unbootable,
      hence it is derived, never written by hand.

      This is also the *only* way to drop AMD P-state on an Intel build:
      arch/x86/Kconfig has `select X86_AMD_PSTATE if CPU_SUP_AMD && ACPI`, and
      a Kconfig `select` overrides a fragment's `=n`. 80-raptorlake-intel.config
      carried `CONFIG_X86_AMD_PSTATE=n` for exactly this purpose and it never
      took effect — the built .config had =y regardless.

    * `NR_CPUS` — Ubuntu's base sets MAXSMP=y, pinning NR_CPUS to 8192 and
      forcing CPUMASK_OFFSTACK. Every cpumask op then walks 128 words. At
      NR_CPUS=64 a cpumask is exactly one unsigned long. 64 (not the detected
      thread count) is deliberate: it is the largest value that still fits one
      word, so it leaves CPU-hotplug headroom for free.

    Returns the fragment path, or None when the vendor could not be detected
    (in which case nothing is narrowed and the base config's portable
    multi-vendor support is left alone).
    """
    profile = ctx.profile
    vendor = getattr(profile, "cpu_vendor", "unknown")
    threads = getattr(profile, "total_threads", 0) or 0

    if vendor not in ("intel", "amd"):
        log.warn(
            f"cpu_vendor={vendor!r} — not narrowing CPU_SUP_*; "
            "keeping the base config's portable multi-vendor support"
        )
        return None

    lines = [
        "# AUTO-GENERATED by the hyphaed configure phase — DO NOT EDIT BY HAND.",
        "# Regenerated on every run from the detected HardwareProfile.",
        f"# host={ctx.profile.running_kernel or 'unknown'} vendor={vendor} "
        f"codename={getattr(profile, 'cpu_codename', '?')} threads={threads}",
        "",
        "# CPU_SUP_* is only settable while PROCESSOR_SELECT is on.",
        "CONFIG_PROCESSOR_SELECT=y",
    ]

    # Dropping the other vendor's cpufreq driver takes BOTH of these:
    #   1. CPU_SUP_<other>=n, to break `select X86_<other>_PSTATE if CPU_SUP_<other>`
    #      in arch/x86/Kconfig — while that select fires, an =n is overruled;
    #   2. an explicit X86_<other>_PSTATE=n, because the driver's own
    #      `depends on X86 && ACPI` is still satisfied, so olddefconfig would
    #      otherwise carry forward the =y inherited from Ubuntu's base config.
    # Doing only (1) leaves the driver enabled — verified on this box.
    if vendor == "intel":
        lines += [
            "CONFIG_CPU_SUP_INTEL=y",
            "CONFIG_CPU_SUP_AMD=n",
            "CONFIG_X86_AMD_PSTATE=n",
        ]
    else:
        lines += [
            "CONFIG_CPU_SUP_AMD=y",
            "CONFIG_CPU_SUP_INTEL=n",
            "CONFIG_X86_INTEL_PSTATE=n",
        ]

    lines += [
        "# Never present on either target machine.",
        "CONFIG_CPU_SUP_HYGON=n",
        "CONFIG_CPU_SUP_CENTAUR=n",
        "CONFIG_CPU_SUP_ZHAOXIN=n",
    ]

    if 0 < threads <= 64:
        lines += [
            "",
            f"# {threads} threads detected; 64 keeps a cpumask at one unsigned long.",
            "CONFIG_MAXSMP=n",
            "CONFIG_NR_CPUS=64",
        ]
    elif threads > 64:
        log.info(f"{threads} threads detected — leaving MAXSMP/NR_CPUS at base values")

    # Deliberately under build/ (gitignored, wiped by `make clean`) and NOT in
    # configs/: it is a per-machine build artefact, so it must never be
    # committed. Note this placement is about what gets TRACKED, not about who
    # may write it — the reason pytest no longer writes a wrong-vendor copy
    # here is that _select_fragments() no longer calls this function; see its
    # docstring.
    out_dir = ctx.repo_root / "build" / "generated"
    out = out_dir / "90-machine-topology.config"
    body = "\n".join(lines) + "\n"

    if getattr(ctx, "dry_run", False):
        log.info(f"(dry-run) would write {out}")
        return None

    out_dir.mkdir(parents=True, exist_ok=True)
    out.write_text(body)
    log.info(
        f"generated 90-machine-topology.config (vendor={vendor}, "
        f"threads={threads}, NR_CPUS={'64' if 0 < threads <= 64 else 'base'})"
    )
    return out


def _select_fragments(ctx, generated: Path | None = None) -> list[Path]:
    """Pick the fragments this machine gets. PURE — it writes nothing.

    `generated` is the machine-topology fragment, produced by the caller via
    _write_machine_topology_fragment() and passed in rather than created here.
    That split is not cosmetic. This function used to call the generator
    itself, which made *selecting* fragments a writing operation, and
    tests/test_pcie_substrate.py calls it with `repo_root = ROOT` (the real
    repo) plus a deliberately fake Strix Point profile. So on this Intel
    desktop, simply running `pytest` left
    build/generated/90-machine-topology.config saying:

        # host=unknown vendor=amd codename=strixpoint threads=0
        CONFIG_CPU_SUP_AMD=y
        CONFIG_CPU_SUP_INTEL=n

    — i.e. a real build input describing the *other* target machine, and one
    that would produce an unbootable kernel here. Found on disk in exactly
    that state 2026-08-18.

    A previous pass moved the file from configs/ to build/ to stop pytest
    rewriting a *tracked* file. That fixed the git-diff noise and left the
    actual hazard in place: the artefact a human or a later code path reads
    to see what the build was configured with still described the wrong box.
    The generator's own header promises "Regenerated on every run from the
    detected HardwareProfile", which is only true of runs that go through
    the configure phase — the guarantee is now structural instead.
    """
    frag_dir = ctx.repo_root / "configs" / "fragments"
    if ctx.preset:
        from .. import presets
        preset = presets.load(ctx.preset)
        explicit = [frag_dir / n for n in preset.fragments]
        explicit = [p for p in explicit if p.exists()]
    else:
        explicit = sorted(frag_dir.glob("*.config"))

    profile = ctx.profile
    selected: list[Path] = []
    for f in explicit:
        name = f.name
        stem = f.stem
        tokens = set(stem.split("-"))

        # 61 is a "strip" fragment — it's skipped when AMD GPU IS present
        # (opposite of the generic amdgpu rule below), so handle it first.
        if name == "61-no-amdgpu-no-nouveau.config" and profile.has_amd_gpu:
            log.info(f"skipping {name}: AMD GPU present")
            continue

        if "nvidia" in tokens and profile.gpu_vendor != "nvidia":
            log.info(f"skipping {name}: no NVIDIA GPU")
            continue
        if "intel" in tokens and profile.cpu_vendor != "intel":
            log.info(f"skipping {name}: CPU is not Intel")
            continue
        # Generic amd-tagged fragments (14-energy-amd-pstate, 63-amdgpu-igpu,
        # 83-strixpoint-amd, …) are skipped on non-AMD systems.
        if "amd" in tokens and name != "61-no-amdgpu-no-nouveau.config" and profile.cpu_vendor != "amd":
            log.info(f"skipping {name}: CPU is not AMD")
            continue
        # 63-amdgpu-igpu additionally requires has_amd_gpu
        if "amdgpu" in tokens and name != "61-no-amdgpu-no-nouveau.config" and not profile.has_amd_gpu:
            log.info(f"skipping {name}: no AMD GPU")
            continue
        if "raptorlake" in tokens and not profile.cpu_codename.startswith("raptorlake"):
            log.info(f"skipping {name}: CPU is not Raptor Lake")
            continue
        if "strixpoint" in tokens and profile.cpu_codename != "strixpoint":
            log.info(f"skipping {name}: CPU is not Strix Point")
            continue
        if "laptop" in tokens and getattr(profile, "chassis", "unknown") != "laptop":
            log.info(f"skipping {name}: chassis is not laptop")
            continue
        selected.append(f)

    # Machine-derived symbols (CPU_SUP_*, NR_CPUS) — see the docstring on
    # _write_machine_topology_fragment for why these cannot be hand-written.
    # Sorts after the numbered fragments but still before 70-greenboost, which
    # the key below keeps last.
    if generated is not None:
        selected.append(generated)

    # Ensure greenboost-required fragment is applied LAST
    selected.sort(key=lambda p: (p.name.startswith("70-greenboost"), p.name))
    return selected


def _base_ver(running_kernel: str) -> tuple[int, ...]:
    """'7.1.5-070105-generic' -> (7, 1, 5). Same field-0 idiom as
    version.py::derive_kernel_version — used to compare series/versions
    across configs/base/ snapshot filenames."""
    base = running_kernel.split("-")[0]
    return tuple(int(x) for x in base.split(".") if x.isdigit())


def _resolve_base_config(ctx) -> Path:
    """Find (or recover) the .config to seed the build from.

    1. /boot/config-<running_kernel> — snapshot it (fast path, ubuntu mode).
    2. Exact configs/base/config-<running_kernel> match.
    3. Nearest same-major.minor snapshot in configs/base/ (e.g. a v7.1.6
       target reusing a v7.1.5 Canonical baseline recovered by source.py's
       series fallback) — prefer *-generic (Canonical) over *-hyphaed (ours)
       so fragments aren't double-layered, then the highest version <= target.
    4. Newest cross-series snapshot — only after an explicit confirm().
    Returns a path that may not exist; caller checks .exists() and errors.
    """
    base_dir = ctx.repo_root / "configs" / "base"
    base_dir.mkdir(parents=True, exist_ok=True)
    running_kernel = ctx.profile.running_kernel

    boot_cfg = Path(f"/boot/config-{running_kernel}")
    snap = base_dir / boot_cfg.name
    if boot_cfg.exists():
        if not snap.exists() or snap.stat().st_mtime < boot_cfg.stat().st_mtime:
            shutil.copy2(boot_cfg, snap)
            log.ok(f"snapshotted {boot_cfg} -> {snap}")
        return snap

    if snap.exists():
        return snap

    candidates = sorted(base_dir.glob("config-*"))
    if not candidates:
        return snap  # nothing at all; caller reports the miss

    target_ver = _base_ver(running_kernel)
    same_series = [c for c in candidates if c.name[len("config-"):].split("-")[0].split(".")[:2] == [str(x) for x in target_ver[:2]]]
    if same_series:
        def _rank(c: Path) -> tuple[bool, tuple[int, ...], bool]:
            v = _base_ver(c.name[len("config-"):])
            return (v <= target_ver, v, c.name.endswith("-generic"))
        chosen = max(same_series, key=_rank)
        log.warn(f"no baseline for {running_kernel} — using same-series snapshot {chosen.name}")
        return chosen

    chosen = max(candidates, key=lambda c: _base_ver(c.name[len("config-"):]))
    log.warn(f"no baseline for {running_kernel} — nearest on disk is {chosen.name} (different series)")
    if not confirm(f"use cross-series baseline {chosen.name} for {running_kernel}?", default=False):
        return snap  # not accepted; caller reports the miss
    return chosen


def run_phase(ctx) -> Path:
    log.banner("Phase 4/9 — Configure (.config layering)")
    src = ctx.source_dir
    if src is None:
        log.err("no source_dir in context; run `source` phase first")
        raise SystemExit(2)

    base_config = _resolve_base_config(ctx)
    if not base_config.exists():
        base_dir = ctx.repo_root / "configs" / "base"
        candidates = sorted(p.name for p in base_dir.glob("config-*"))
        log.err(f"no base config found for {ctx.profile.running_kernel} at {base_config}")
        if candidates:
            log.err(f"  rejected candidates on disk: {', '.join(candidates)}")
        log.err(f"  pre-stage one:  cp /boot/config-<ver> {base_dir}/")
        raise SystemExit(2)

    target_config = src / ".config"
    if ctx.dry_run and not src.exists():
        log.info(f"(dry-run) would seed .config from {base_config.name} into {target_config}")
    else:
        shutil.copy2(base_config, target_config)
        log.info(f"seeded .config from {base_config.name}")

    fragments = _select_fragments(ctx, generated=_write_machine_topology_fragment(ctx))
    log.info(f"layering {len(fragments)} fragments:")
    for f in fragments:
        log.console.print(f"  + {f.name}")

    if ctx.dry_run:
        log.info("(dry-run) would run merge_config.sh + make olddefconfig")
        return target_config

    merge_script = src / "scripts" / "kconfig" / "merge_config.sh"
    if not merge_script.exists():
        log.err(f"merge_config.sh not found at {merge_script}")
        raise SystemExit(2)

    # `merge_config.sh -m .config frag1 frag2 ...` produces merged .config
    cmd = [str(merge_script), "-m", str(target_config), *(str(f) for f in fragments)]
    run(cmd, cwd=src, check=True)

    # Resolve any new symbols
    log.info("running `make olddefconfig`")
    run(["make", "olddefconfig"], cwd=src)

    sha = sha256_file(target_config)
    (ctx.state_dir / "config.sha256").write_text(sha + "\n")
    log.ok(f"final .config sha256={sha[:16]}…")

    # Validate the resolved .config — make sure no fragment downgraded
    # the GreenBoost floor or our hard requirements.
    log.info("validating .config against floor requirements")
    parsed = kconfig.parse_config(target_config)
    # The kernel version matters to validation: a floor symbol upstream has
    # since deleted must not read as a broken fragment. Taken off the source
    # tree name, which is the tree that produced this .config.
    kver = None
    if getattr(ctx, "source_dir", None):
        m = re.match(r"linux-(\d+(?:\.\d+)*)", Path(ctx.source_dir).name)
        if m:
            kver = m.group(1)
    result = kconfig.validate(
        parsed,
        with_greenboost=ctx.profile.has_greenboost,
        cpu_vendor=ctx.profile.cpu_vendor,
        kernel_version=kver,
    )
    if result.missing or result.forbidden:
        for m in result.missing:
            log.err(f"  missing: {m}")
        for f in result.forbidden:
            log.err(f"  forbidden: {f}")
        log.err("config validation failed — fix fragments and re-run `--phase configure`")
        raise SystemExit(2)
    for w in result.warnings:
        log.warn(f"  {w}")
    log.ok(f"config passes floor validation ({len(parsed)} symbols)")

    # `ctx.non_interactive` only reflects the explicit --non-interactive flag —
    # a run launched with just -y (no controlling tty, e.g. backgrounded) still
    # passes that check, and confirm()'s own non-interactive fallback then
    # returns True (assume-yes wins over the False default), which spawns a
    # real ncurses `make menuconfig` against a non-tty stdin. That busy-spins
    # forever reading EOF from /dev/null instead of erroring — confirmed live
    # 2026-07-30 (had to SIGKILL the mconf process tree). Require an actual
    # tty before even offering the prompt, independent of the interactive
    # flags, since this is the one confirm() site that launches a blocking
    # full-screen program rather than just returning a bool.
    if not ctx.dry_run and not ctx.non_interactive and sys.stdin.isatty():
        if confirm("open menuconfig to review before build?", default=False):
            run(["make", "menuconfig"], cwd=src, capture=False)

    _run_hardening_check(ctx, target_config)
    return target_config
