from __future__ import annotations
import re
from pathlib import Path

from ..util import log
from ..util.run import run
from ..util.prompts import confirm
from .. import gitops

NAME = "patch"


def _read_series(series_file: Path) -> list[Path]:
    if not series_file.exists():
        return []
    base = series_file.parent
    patches: list[Path] = []
    for line in series_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        p = base / line
        if not p.exists():
            log.warn(f"patch listed in series but missing on disk: {p}")
            continue
        patches.append(p)
    return patches


def _target_series_version(ctx) -> str | None:
    """major.minor of the kernel actually being patched, e.g. "7.2".

    Read off ctx.source_dir first (build/linux-7.2 → 7.2) because that is the
    tree `git am` will run in — it is the only value that cannot disagree with
    reality. --target is the fallback, for a --dry-run before source has run.
    """
    src = getattr(ctx, "source_dir", None)
    if src:
        m = re.match(r"linux-(\d+)\.(\d+)", Path(src).name)
        if m:
            return f"{m.group(1)}.{m.group(2)}"
    target = getattr(getattr(ctx, "args", None), "target", None)
    if target:
        m = re.match(r"(\d+)\.(\d+)", str(target))
        if m:
            return f"{m.group(1)}.{m.group(2)}"
    return None


def _series_file_for(repo_root: Path, source_mode: str, ctx=None) -> Path:
    # ctx may override series dir (e.g. source phase resolved xanmod vs kernel-org)
    if ctx is not None and hasattr(ctx, "patch_series_dir") and ctx.patch_series_dir:
        return ctx.patch_series_dir / "series"

    if source_mode == "kernel-org":
        # The series directory follows the kernel being built. It used to be
        # hardcoded to kernel-org-7.1, which was correct for exactly as long as
        # 7.1 was the only target anyone passed. `--target 7.2` would have
        # applied the 7.1 series against a 7.2 tree and said nothing about it —
        # every patch would still `git am --3way` or not on its own merits, and
        # the run would look completely normal.
        version = _target_series_version(ctx)
        if version:
            d = repo_root / "patches" / f"kernel-org-{version}"
            if d.is_dir():
                return d / "series"
            raise SystemExit(
                f"no patch series for kernel {version}: expected {d}/series.\n"
                f"Create it (and patches/VENDOR-kernel-org-{version}.lock) before "
                f"building this target — falling back to another version's series "
                f"would apply patches written for a different tree."
            )
        # No source_dir and no --target: cannot tell what is being built, so
        # do not guess a version directory.
        raise SystemExit(
            "cannot determine the kernel version to pick a patch series for. "
            "Run `python -m hyphaed --phase source` first, or pass --target."
        )

    if source_mode == "xanmod":
        return repo_root / "patches" / "xanmod-7.1" / "series"
    return repo_root / "patches" / "series"


def run_phase(ctx) -> list[Path]:
    log.banner("Phase 3/9 — Apply patch series")
    series_file = _series_file_for(ctx.repo_root, getattr(ctx, "source_mode", "ubuntu"), ctx)
    patches = _read_series(series_file)

    if not patches:
        log.warn(f"no patches listed in {series_file} — building plain kernel with config overlay only")
        return []

    log.info(f"{len(patches)} patches in series:")
    for p in patches:
        log.console.print(f"  • {p.name}")

    conflicts = gitops.check_mutual_exclusion(patches)
    if conflicts:
        for c in conflicts:
            log.err(c)
        raise SystemExit(2)

    if ctx.dry_run:
        log.info("(dry-run) would `git init` baseline and `git am --3way` each patch")
        return patches

    if not confirm("apply this series with git am --3way?", default=True):
        raise SystemExit(0)

    base_tag = getattr(ctx, "baseline_tag", None)
    if not base_tag:
        log.err(
            "no pristine baseline tag recorded on ctx/state — run "
            "`python -m hyphaed --phase source` first to seed one"
        )
        raise SystemExit(2)
    gitops.reset_to_baseline(ctx.source_dir, base_tag)

    results = gitops.apply_series(ctx.source_dir, patches)
    applied = [p.name for p, ok, _ in results if ok]
    failed = [(p.name, msg) for p, ok, msg in results if not ok]

    state = {
        "base_tag": base_tag,
        "applied": applied,
        "failed": [{"patch": n, "error": m} for n, m in failed],
    }
    (ctx.state_dir / "applied-series.json").write_text(
        __import__("json").dumps(state, indent=2)
    )

    if failed:
        log.err(f"{len(failed)} patches failed to apply:")
        for n, m in failed:
            log.console.print(f"  ✗ {n}")
            log.console.print(f"    {m[:200].splitlines()[0] if m else ''}")
        log.warn("you can edit/skip patches and re-run `python -m hyphaed --phase patch`")
        raise SystemExit(2)

    log.ok(f"applied {len(applied)} patches cleanly")
    return [p for p, ok, _ in results if ok]
