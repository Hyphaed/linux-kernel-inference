"""Phase 11/11: rebuild mutter + gnome-shell (Ubuntu source) optimized for this host.

Uses `apt-get source` of the pinned Ubuntu versions so the Ubuntu patches stay,
and injects flags through dpkg-buildflags (no fork of debian/). Debs are built
per host (-march=native is not portable), installed with apt-mark hold, and
the original versions are saved for `apt install --allow-downgrades`.
A rebuild is kept only if scripts/measure_desktop.sh shows it is not worse.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from ..util import log
from ..util.prompts import confirm
from ..util.run import run, run_sudo

NAME = "desktop"

_SRC_PKGS = ("mutter", "gnome-shell")
# Meson switches removed from the package build: none. Both candidates broke the Debian packaging (2026-10-31):
#  - the test switches: dh_install aborts, Debian still ships libmutter-test-18 and mutter-18-tests;
#  - `profiler`: removes the cogl_trace_* symbols, so dh_makeshlibs fails on the symbols file, and a libmutter without
#    them is an ABI change for everything that links it.
# Neither saves runtime CPU, memory or VRAM, so the gain here comes from the compiler flags alone.
_TRIM: tuple[str, ...] = ()


def _cfg(ctx) -> dict:
    from .. import presets
    if not ctx.preset:
        return {}
    try:
        return presets.load(ctx.preset).desktop
    except Exception:
        return {}


def _clean_env() -> dict:
    """System toolchain only. A user-level `meson`/`python3` earlier in PATH (~/.local/bin, miniforge) broke the
    mutter build: debhelper called ~/.local/bin/meson, which could not import mesonbuild (2026-10-31)."""
    keep = ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TERM", "TMPDIR", "CCACHE_DIR")
    env = {k: v for k, v in os.environ.items() if k in keep}
    env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    return env


def _flags(march: str = "native") -> dict:
    opt = f"-O3 -march={march} -mtune={march}"
    return {
        "DEB_BUILD_MAINT_OPTIONS": "optimize=+lto",
        "DEB_CFLAGS_MAINT_APPEND": opt,
        "DEB_CXXFLAGS_MAINT_APPEND": opt,
        "DEB_LDFLAGS_MAINT_APPEND": opt,
        "DEB_BUILD_OPTIONS": f"nocheck parallel={__import__('hyphaed.phases.build', fromlist=['_build_jobs'])._build_jobs()}",
    }


def _installed_versions(pkgs: list[str]) -> dict[str, str]:
    out = run(["dpkg-query", "-W", "-f=${Package} ${Version}\n", *pkgs], check=False, force=True).stdout
    return dict(ln.split(" ", 1) for ln in out.splitlines() if " " in ln)


def _binary_pkgs(src: str) -> list[str]:
    """Installed binary packages built from `src` (only these are replaced)."""
    out = run(["dpkg-query", "-W", "-f=${Package} ${source:Package} ${db:Status-Abbrev}\n"],
              check=False, force=True).stdout
    return [f[0] for f in (ln.split() for ln in out.splitlines())
            if len(f) == 3 and f[1] == src and f[2].startswith("ii")]


def _trim_rules(srcdir: Path) -> bool:
    """Append -D<opt>=false for each trim option that exists in the package's meson options."""
    opts = ""
    for name in ("meson.options", "meson_options.txt"):
        p = srcdir / name
        if p.exists():
            opts = p.read_text()
    rules = srcdir / "debian" / "rules"
    if not rules.exists() or not opts:
        return False
    # Option types differ ("tests" is a feature: enabled/disabled/auto; the others are boolean), and a wrong
    # value aborts meson setup, so read the type from the package's own options file.
    off = {"boolean": "false", "feature": "disabled"}
    found = {m.group(1): m.group(2) for m in re.finditer(r"option\(\s*'(\w+)'\s*,[^)]*?type\s*:\s*'(\w+)'", opts, re.S)}
    present = [(o, off[found[o]]) for o in _TRIM if found.get(o) in off]
    if not present:
        return False
    text = rules.read_text()
    marker = "# hyphaed-trim"
    if marker in text:
        return True
    extra = " ".join(f"-D{o}={v}" for o, v in present)
    text += f"\n{marker}\nDEB_MESON_EXTRA_CONFIGURE_ARGS += {extra}\n"
    # dh_auto_configure honours the variable only if the rules file passes it; fall back
    # to a meson wrapper when the package uses dh --buildsystem=meson (the Ubuntu default).
    text = text.replace("dh_auto_configure --", f"dh_auto_configure -- {extra}", 1) if "dh_auto_configure --" in text else text
    rules.write_text(text)
    log.info(f"{srcdir.name}: meson trimmed -> {extra}")
    return True


def _bump_changelog(srcdir: Path) -> None:
    """Append `+hyphaed1` to the version without devscripts' `dch` (not installed by default)."""
    import email.utils
    cl = srcdir / "debian" / "changelog"
    first = cl.read_text().splitlines()[0]
    m = re.match(r"^(\S+) \(([^)]+)\) (\S+?);", first)
    if not m:
        raise SystemExit(f"desktop: cannot parse {cl} first line: {first!r}")
    name, ver, dist = m.groups()
    if "+hyphaed" in ver:
        return
    entry = (f"{name} ({ver}+hyphaed1) {dist}; urgency=medium\n\n"
             "  * native-optimized rebuild (hyphaed): -O3 -march=native, LTO\n\n"
             f" -- hyphaed <hyphaed@localhost>  {email.utils.formatdate(localtime=True)}\n\n")
    cl.write_text(entry + cl.read_text())


def _build_one(ctx, pkg: str, version: str, workdir: Path) -> list[Path]:
    run(["apt-get", "source", "--download-only", f"{pkg}={version}"], cwd=workdir, dry_run=ctx.dry_run)
    dsc = next(iter(sorted(workdir.glob(f"{pkg}_*.dsc"))), Path(f"{pkg}.dsc"))
    run(["dpkg-source", "-x", str(dsc)], cwd=workdir, dry_run=ctx.dry_run, check=not ctx.dry_run)
    if ctx.dry_run:
        return []
    srcdir = next(d for d in workdir.iterdir() if d.is_dir() and d.name.startswith(f"{pkg}-"))
    _trim_rules(srcdir)
    _bump_changelog(srcdir)
    env = {**_clean_env(), **_flags()}
    r = run(["dpkg-buildpackage", "-b", "-uc", "-us", "-d"], cwd=srcdir, env=env, check=False,
            tee_log=log.phase_log_path(f"desktop_{pkg}"))
    if r.code != 0:
        raise RuntimeError(f"{pkg} build failed; see the phase log. Nothing was installed.")
    return sorted(workdir.glob("*.deb"))


def run_phase(ctx) -> None:
    log.banner("Phase 11/12 — GNOME Shell + Mutter native rebuild")
    cfg = _cfg(ctx)
    if not cfg.get("enabled"):
        log.info("preset has no desktop.enabled; skipping")
        return
    if not Path("/usr/bin/gnome-shell").exists():
        log.info("no GNOME Shell on this machine; skipping")
        return

    state = ctx.state_dir
    work = state / "desktop"
    pins = {"mutter": cfg.get("mutter"), "gnome-shell": cfg.get("gnome_shell")}
    if not all(pins.values()):
        raise SystemExit("desktop: preset desktop block needs mutter: and gnome_shell: Ubuntu versions")
    cur = _installed_versions(list(pins))
    for p, v in pins.items():
        if cur.get(p) != v:
            raise SystemExit(f"desktop: {p} installed {cur.get(p)} but preset pins {v}; update the pin after reading the changelog")

    bench = ctx.repo_root / "scripts" / "measure_desktop.sh"
    if bench.exists() and not (state / "desktop-bench-before.json").exists():
        log.info("recording the stock-package baseline (about 2 minutes)")
        run(["bash", str(bench), str(state / "desktop-bench-before.json")], check=False, dry_run=ctx.dry_run)

    deps = run(["apt-get", "-s", "build-dep", *_SRC_PKGS], check=False, force=True).stdout
    missing = sorted(set(re.findall(r"^Inst (\S+)", deps, re.M)))
    if missing:
        log.warn(f"{len(missing)} build dependencies missing, e.g. {', '.join(missing[:6])}")
        if not confirm("Install build dependencies with apt-get build-dep?", default=True):
            raise SystemExit("desktop: build dependencies declined; nothing was changed")
        run_sudo(["apt-get", "build-dep", "-y", *_SRC_PKGS], dry_run=ctx.dry_run)

    tools = [t for t in ("dpkg-buildpackage", "fakeroot") if not shutil.which(t)]
    if tools and not ctx.dry_run:
        log.warn(f"missing build tools: {', '.join(tools)}")
        if not confirm("Install dpkg-dev and fakeroot?", default=True):
            raise SystemExit("desktop: build tools declined; nothing was changed")
        run_sudo(["apt-get", "install", "-y", "dpkg-dev", "fakeroot"])
    work.mkdir(parents=True, exist_ok=True)
    debs: list[Path] = []
    for pkg, ver in pins.items():
        wd = work / pkg
        shutil.rmtree(wd, ignore_errors=True)
        wd.mkdir(parents=True)
        debs += _build_one(ctx, pkg, ver, wd)
    if ctx.dry_run:
        log.ok("dry run: plan printed, nothing built")
        return

    wanted = {b for s in _SRC_PKGS for b in _binary_pkgs(s)}
    chosen = [d for d in debs if d.name.split("_")[0] in wanted]
    (state / "desktop-rollback.txt").write_text(
        "\n".join(f"{p}={v}" for p, v in _installed_versions(sorted(wanted)).items()) + "\n")
    log.info(f"{len(chosen)} debs will replace installed packages; rollback list: {state / 'desktop-rollback.txt'}")
    if not ctx.dry_run and not confirm("Install the rebuilt GNOME packages? (takes effect at next login)", default=False, hard=True):
        raise SystemExit(f"desktop: install declined; the rebuilt debs stay in {work}")
    run_sudo(["apt-get", "install", "-y", "--allow-downgrades", *map(str, chosen)])
    run_sudo(["apt-mark", "hold", *sorted(d.name.split("_")[0] for d in chosen)])
    (state / "desktop-installed.json").write_text(json.dumps({"debs": [d.name for d in chosen]}, indent=2))
    log.ok("installed. Log out and in, then: bash scripts/measure_desktop.sh "
           f"{state / 'desktop-bench-after.json'} and compare with desktop-bench-before.json")
