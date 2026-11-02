"""Phase 12/12: keep the Steam compatibility tools current: GreenBoost Proton, and optionally proton-gb-nr.

"GreenBoost Proton" / "GreenBoost Proton Experimental" in Steam's picker are NOT builds: they are a wrapper
(greenboost_gaming/greenboost_proton) that finds Valve's own Proton in steamapps/common at launch
(channel stable = newest installed numbered Proton, experimental = Proton - Experimental). So "newest" means
two cheap things: the Valve Protons Steam has installed, and the wrapper code. This step reports the first and
re-installs the second from the greenboost_gaming sources.

The heavy path, a from-source rebuild of proton-gb-nr (own -march=alderlake build plus the ADR-010 patches),
is opt-in (`proton.rebuild_gb_nr: true`): no benchmark shows it beats Valve's builds yet (bench/results is empty),
and it costs about 25 GB and an hour. It follows grenboost_gaming's own procedure (workflow/00_START_HERE.md,
ADR-003, ADR-010, ADR-011): fresh `bleeding-edge` clone -> apply-patches.sh -> configure.sh -> `make dist` in
Valve's Docker SDK image -> install.sh. Old sources and build trees are kept for rollback.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path

from ..util import log
from ..util.prompts import confirm
from ..util.run import run

NAME = "proton"

_UPSTREAM = "https://github.com/ValveSoftware/Proton.git"
_NEED_GB = 45


def _cfg(ctx) -> dict:
    from .. import presets
    if not ctx.preset:
        return {}
    try:
        return presets.load(ctx.preset).proton
    except Exception:
        return {}


def _manifest_base(sources: Path) -> str:
    try:
        m = re.search(r"bleeding-edge\s+([0-9a-f]{7,40})", (sources / "MANIFEST.md").read_text())
        return m.group(1) if m else ""
    except OSError:
        return ""


def _upstream_tip(branch: str) -> str:
    out = run(["git", "ls-remote", _UPSTREAM, f"refs/heads/{branch}"], check=False, force=True).stdout.split()
    return out[0] if out else ""


def _kernel_build_running() -> bool:
    r = run(["pgrep", "-f", r"bindeb-pkg|hyphaed .*--phase (build|package)|make .*linux-7\."],
            check=False, force=True)
    return bool(r.stdout.strip())


def _submodule_paths(cur: Path) -> tuple[list[str], list[str]]:
    """(real submodules, orphans). Real = gitlinks in the index, the set git can actually init.
    Upstream Proton left `Vulkan-Loader` in .gitmodules after dropping its gitlink; pathspec fails on it."""
    stage = run(["git", "ls-files", "--stage"], cwd=cur, force=True).stdout.splitlines()
    mods = [ln.split("\t", 1)[1] for ln in stage if ln.startswith("160000 ")]
    cfg = run(["git", "config", "-f", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$"],
              cwd=cur, check=False, force=True).stdout.splitlines()
    declared = [ln.split(None, 1)[1] for ln in cfg if ln.strip()]
    return mods, sorted(set(declared) - set(mods))


def _fresh_sources(ctx, sources: Path, branch: str, stamp: str) -> Path:
    cur, prev = sources / "Proton", sources / f"Proton.prev-{stamp}"
    if ctx.dry_run:
        log.info(f"(dry-run) would move {cur} -> {prev} and clone {_UPSTREAM} ({branch}) fresh")
        return cur
    if prev.exists():
        # A same-day retry: the earlier attempt already moved the real sources to `prev` and died
        # part-way through its fresh clone. `cur` is that unfinished clone; `prev` is the rollback copy.
        if cur.exists():
            log.warn(f"proton: discarding the unfinished clone from the earlier attempt ({cur}); rollback copy kept in {prev}")
            shutil.rmtree(cur)
    elif cur.exists():
        cur.rename(prev)
    run(["git", "clone", "-b", branch, _UPSTREAM, str(cur)])
    # Submodules one at a time and verified: a batch `update --init --recursive` can leave a
    # tree empty while still reporting the right commit (grenboost_gaming START_HERE, rule 2).
    mods, orphans = _submodule_paths(cur)
    if orphans:
        log.warn(f"proton: .gitmodules lists {', '.join(orphans)} with no gitlink in the tree; skipped")
    for mod in mods:
        # --recursive: dxvk-nvapi, dxvk, vkd3d-proton and FEX carry their own submodules (external/Vulkan-Headers,
        # include/vulkan, ...) and the build fails at configure without them. Still one top-level module per call.
        run(["git", "submodule", "update", "--init", "--recursive", "--", mod], cwd=cur)
        if not any((cur / mod).iterdir()):
            raise SystemExit(f"proton: submodule {mod} is empty after init; previous sources are in {prev}")
        pending = [ln.split()[1] for ln in run(["git", "submodule", "status", "--recursive"], cwd=cur / mod,
                                               force=True).stdout.splitlines() if ln.startswith("-")]
        if pending:
            raise SystemExit(f"proton: {mod} has nested submodules not initialised: {', '.join(pending)}; "
                             f"previous sources are in {prev}")
    r = run(["bash", str(sources / "apply-patches.sh")], cwd=sources, check=False)
    if r.code != 0:
        raise SystemExit(
            "proton: our patches no longer apply on the newest sources (see output above). Resolve by "
            f"hand as apply-patches.sh explains; the previous sources are intact in {prev}.")
    return cur


def _changes_report(ctx, cur: Path, base: str, stamp: str) -> None:
    """What is new upstream since the commit we last built from: commits and the moved submodule pins
    (wine, dxvk, dxvk-nvapi, vkd3d-proton), saved so the build is justified by a diff, not by a hunch."""
    if not base or ctx.dry_run:
        return
    end = "origin/bleeding-edge"
    commits = run(["git", "log", "--oneline", "--no-decorate", f"{base}..{end}"], cwd=cur, check=False, force=True).stdout
    subs = run(["git", "diff", "--submodule=log", base, end], cwd=cur, check=False, force=True).stdout
    rep = ctx.state_dir / f"proton-changes-{stamp}.md"
    rep.write_text(f"# Proton bleeding-edge since {base[:8]}\n\n## Commits\n{commits}\n## Submodule pins\n{subs}\n")
    n = len(commits.splitlines())
    moved = [ln.split()[1] for ln in subs.splitlines() if ln.startswith("Submodule ")]
    log.info(f"upstream since our last build: {n} commits; submodule pins moved: {', '.join(moved) or 'none'}")
    log.info(f"full change list: {rep}")


def _build(ctx, cfg: dict, cur: Path, root: Path, stamp: str) -> Path:
    name = cfg.get("build_name", "proton-gb-nr")
    bdir = root / f"Proton-build-{stamp}"
    if ctx.dry_run:
        log.info(f"(dry-run) would configure {bdir} and run make dist in the Docker SDK image")
        return bdir / "dist"
    bdir.mkdir(parents=True, exist_ok=True)
    run([str(cur / "configure.sh"), "--enable-ccache", f"--build-name={name}", "--container-engine=docker"], cwd=bdir)
    logf = ctx.logs_dir / f"proton-build-{stamp}.log"
    log.info(f"building (long; log: {logf})")
    logf.write_text("")  # tee appends: errors from an earlier attempt must not fail this one
    run(["make", "-j16", "dist"], cwd=bdir, check=False, tee_log=logf)
    # A pipe's exit code is tee's, not make's (grenboost_gaming rule 1): trust the log and the artifact.
    # "Error N (ignored)" is make carrying on past a `-` prefixed command (ffmpeg's ffbuild/.config), not a failure.
    errs = [ln for ln in logf.read_text(errors="ignore").splitlines()
            if re.search(r"\] Error \d+", ln) and "(ignored)" not in ln]
    if errs or not (bdir / "dist" / "proton").exists():
        raise SystemExit(f"proton: build failed ({len(errs)} make errors, e.g. {errs[:1]}). Log: {logf}. Nothing was installed.")
    return bdir / "dist"


_STEAM_COMMON = Path("~/.local/share/Steam/steamapps/common").expanduser()
_TOOLS = Path("~/.local/share/Steam/compatibilitytools.d").expanduser()


def _installed_valve_protons() -> list[tuple[str, str]]:
    out = []
    for d in sorted(_STEAM_COMMON.glob("Proton*")):
        v = d / "version"
        out.append((d.name, v.read_text().split()[-1] if v.exists() else "?"))
    return out


def _refresh_wrappers(ctx, cfg: dict) -> None:
    """Install greenboost-proton (+experimental) from the greenboost_gaming sources if they differ from what Steam has."""
    src = Path(cfg.get("greenboost_proton_dir", "~/Dev/greenboost_all/greenboost_gaming/greenboost_proton")).expanduser()
    if not (src / "install.sh").exists():
        raise SystemExit(f"proton: {src}/install.sh not found; nothing was changed")
    log.info("Valve Proton builds Steam has installed (what the GreenBoost wrapper delegates to):")
    for name, ver in _installed_valve_protons():
        log.info(f"  {name:24} {ver}")
    log.info("  channel stable -> newest numbered Proton above; channel experimental -> Proton - Experimental")
    log.info("  To get a newer Valve build: Steam > Library > Tools, or pick it once in a game's Compatibility list.")

    repo = src.parent
    same = all(
        (src / f).exists() and (_TOOLS / t / f).exists() and (src / f).read_bytes() == (_TOOLS / t / f).read_bytes()
        for t in ("greenboost-proton", "greenboost-proton-experimental") for f in ("gb_proton_main.py", "proton"))
    last = run(["git", "-C", str(repo), "log", "-1", "--format=%h %cd %s", "--date=short", "--", str(src)],
               check=False, force=True).stdout.strip()
    dirty = run(["git", "-C", str(repo), "status", "--porcelain", "--", str(src)], check=False, force=True).stdout.strip()
    log.info(f"wrapper sources: last commit touching it: {last or 'unknown'}")
    if same:
        log.ok("the installed GreenBoost Proton wrappers already match the sources; nothing to refresh")
        return
    if dirty:
        stat = run(["git", "-C", str(repo), "diff", "--stat", "--", str(src)], check=False, force=True).stdout.strip()
        log.warn("the wrapper sources have UNCOMMITTED edits (another session's work?) that would ship:\n" + stat)
    if ctx.dry_run:
        log.info("(dry-run) would run greenboost_proton/install.sh")
        return
    if not confirm("Install the GreenBoost Proton wrappers from these sources into Steam?", default=not dirty, hard=bool(dirty)):
        log.info("wrapper refresh declined; the installed wrappers stay as they are")
        return
    run(["bash", str(src / "install.sh")])
    log.ok("GreenBoost Proton wrappers refreshed. Restart Steam so it re-reads them.")


def run_phase(ctx) -> None:
    log.banner("Phase 12/12 — Steam compatibility tools (GreenBoost Proton)")
    cfg = _cfg(ctx)
    if not cfg.get("enabled"):
        log.info("preset has no proton.enabled; skipping")
        return
    _refresh_wrappers(ctx, cfg)
    if not cfg.get("rebuild_gb_nr"):
        log.info("proton-gb-nr from-source rebuild is off (preset proton.rebuild_gb_nr); no measurement says it beats Valve's builds yet")
        return
    repo = Path(cfg.get("repo", "~/Dev/github_gaming/grenboost_gaming")).expanduser()
    root = Path(cfg.get("build_root", "~/Dev/github_gaming")).expanduser()
    branch = cfg.get("branch", "bleeding-edge")
    sources = repo / "proton-gb-nr" / "sources"
    if not (sources / "apply-patches.sh").exists() or not shutil.which("docker"):
        raise SystemExit(f"proton: need {sources}/apply-patches.sh and docker; neither was changed")

    base, tip = _manifest_base(sources), _upstream_tip(branch)
    log.info(f"our base {base[:8] or '?'}  ·  upstream {branch} tip {tip[:8] or 'unreachable'}")
    if not tip:
        raise SystemExit("proton: cannot reach github.com to learn the newest Proton commit. Nothing was changed.")
    if base and tip.startswith(base[:8]):
        if not ctx.dry_run and not confirm("Already on the newest Proton commit. Rebuild anyway?", default=False):
            log.ok("up to date; nothing to do")
            return
    free_gb = shutil.disk_usage(root).free // 2**30
    if free_gb < _NEED_GB:
        raise SystemExit(f"proton: {free_gb} GB free on the build disk, need {_NEED_GB}. "
                         "Free space first: sudo bash scripts/clean-space.sh --go (and old Proton-build-* trees)")
    if _kernel_build_running():
        raise SystemExit("proton: a kernel build is running; Proton needs all cores. Run this step afterwards.")
    if not ctx.dry_run and not confirm("Fetch the newest Proton sources and rebuild? (about 25 GB, 1+ hour)", default=False, hard=True):
        raise SystemExit("proton: declined; nothing was changed")

    stamp = time.strftime("%Y%m%d")
    prev_dist = next(iter(sorted(d / "dist" for d in root.glob("Proton-build-*") if (d / "dist" / "proton").exists())[-1:]), None)
    cur = _fresh_sources(ctx, sources, branch, stamp)
    _changes_report(ctx, cur, base, stamp)
    dist = _build(ctx, cfg, cur, root, stamp)
    if ctx.dry_run:
        return
    if not confirm("Install the new build as the proton-gb-nr Steam tool?", default=True, hard=True):
        raise SystemExit(f"proton: install declined; the finished build stays in {dist}")
    run(["bash", str(repo / "proton-gb-nr" / "install.sh")], env={**os.environ, "PROTON_DIST": str(dist)})

    # ADR-011: a result belongs to a kernel + Proton pair, so record both.
    (ctx.state_dir / "proton-pair.json").write_text(json.dumps(
        {"kernel": os.uname().release, "proton_dist": str(dist), "upstream": tip,
         "built": time.strftime("%Y-%m-%dT%H:%M:%S"), "rollback_dist": str(prev_dist) if prev_dist else None}, indent=2))
    if prev_dist:
        log.info(f"rollback: PROTON_DIST={prev_dist} bash {repo / 'proton-gb-nr/install.sh'}")
    log.ok("proton-gb-nr installed. Restart Steam to pick it up. Old build trees are large: "
           "remove the ones you no longer need (about 23 GB each).")
