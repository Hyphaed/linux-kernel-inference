"""The two-run flow behind a bare `hyphaed`.

Run 1 (running kernel is NOT the target): baseline capture -> bump stages 00/10/20/30
(preflight, source sync, sweep, real sequential git am + compile) -> the normal phases
-> config audit. Run 2 (running kernel IS the target, the reboot happened): post-boot
audit -> capture B -> A/B report. Nothing here compiles in run 2.

scripts/bump/40-build.sh is not used: it re-invokes `hyphaed --phase package`, and the
wizard already owns the build phases, so calling it from here would recurse.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import abreport, workload
from .util import log

PREBUILD = ("00-preflight.sh", "10-sync-sources.sh", "20-sweep.py", "30-validate-series.sh")


def detect_mode(running: str, target: str) -> str:
    """'run2' when the booted kernel is the target (`7.2.10-hyphaed` starts with `7.2.10`), else 'run1'."""
    return "run2" if running == target or running.startswith(target + "-") else "run1"


def _stage(repo: Path, name: str, ver: str, *, nice: bool = True) -> int:
    script = repo / "scripts" / "bump" / name
    cmd = ["python3", str(script)] if name.endswith(".py") else ["bash", str(script)]
    if nice:
        cmd = ["nice", "-n", "10", *cmd]
    log.info(f"bump stage {name}")
    return subprocess.run(cmd, cwd=repo, env={**os.environ, "VER": ver}).returncode


def previous_kernel_version(running: str) -> str:
    return running.split("-")[0]


def run1_prebuild(repo: Path, target: str, *, running: str, skip_baseline: bool, cold: bool,
                  model: str | None, dry_run: bool) -> int:
    """Everything before the build phases. Returns non-zero to stop the run (a patch that doesn't apply
    stops here, before 40 minutes of compiling, and the stage's own output names it)."""
    base = workload.baseline_dir(repo, running)
    if dry_run:
        log.info(f"[dry-run] run 1 plan: capture A -> {base}; stages {', '.join(PREBUILD)}; "
                 f"phases detect..postinstall; 50-config-audit.py; then reboot into {target}-hyphaed")
        return 0
    if not skip_baseline and not (base / "capture.json").exists():
        log.banner(f"Baseline capture on {running} (the A side)")
        workload.capture(base, model=model, cold=cold)
        log.ok(f"baseline written: {base}/capture.json")
    else:
        log.info(f"baseline: {'skipped' if skip_baseline else 'already at ' + str(base)}")
    os.nice(10)  # the build must not skew other sessions or the trading room more than it has to
    for name in PREBUILD:
        rc = _stage(repo, name, target)
        if rc != 0:
            log.err(f"bump stage {name} failed (rc={rc}); stopping before the build")
            return rc
    _invalidate_if_retargeted(repo, target)
    return 0


def _invalidate_if_retargeted(repo: Path, target: str) -> None:
    """The phase cache is state-based: phases listed as done are skipped whatever the target. A state
    built for another version would make the build a silent no-op, so drop it from `source` on."""
    from .phases import ORDER
    from .state import PersistedState
    d = repo / "state" / "machines" / os.uname().nodename
    st = PersistedState.load(d)
    if not str(getattr(st, "kernel_pkgver", "") or "").startswith(target + "-"):
        st.invalidate_from("source", ORDER)
        st.save(d)
        log.info(f"phase cache held another version; invalidated from source for {target}")


def run1_postbuild(repo: Path, target: str, running: str) -> int:
    env = {**os.environ, "VER": target, "PREV": previous_kernel_version(running)}
    rc = subprocess.run(["python3", str(repo / "scripts" / "bump" / "50-config-audit.py")], cwd=repo, env=env).returncode
    if rc != 0:
        log.warn("config audit reported mismatches: triage before the next bump (the kernel is built and installed)")
    log.console.print(f"\nBuilt and installed. Reboot, pick {target}-hyphaed, then run `python -m hyphaed` again for the "
                      "audit, the B capture and the A/B report.")
    return 0


def run2(repo: Path, target: str, running: str, *, prev: str | None, cold: bool, model: str | None,
         dry_run: bool) -> int:
    if dry_run:
        log.info(f"[dry-run] run 2 plan: 60-post-boot-audit.sh; capture B -> {workload.baseline_dir(repo, running)}; "
                 "A/B report in docs/")
        return 0
    rc = subprocess.run(["bash", str(repo / "scripts" / "bump" / "60-post-boot-audit.sh")], cwd=repo,
                        env={**os.environ, "VER": target}).returncode
    if rc != 0:
        log.warn(f"post-boot audit rc={rc}; continuing with the capture")
    b_dir = workload.baseline_dir(repo, running)
    if not (b_dir / "capture.json").exists():
        log.banner(f"Capture on {running} (the B side)")
        workload.capture(b_dir, model=model, cold=cold)
    a_dirs = sorted(p for p in (repo / "out").glob("baseline-*") if p != b_dir and (p / "capture.json").exists())
    if prev:
        a_dirs = [p for p in a_dirs if p.name.startswith(f"baseline-{prev}")] or a_dirs
    if not a_dirs:
        log.warn("no A-side baseline found under out/: the report needs a capture taken before the bump")
        return 0
    import datetime
    dest = repo / "docs" / f"ab-{a_dirs[-1].name.removeprefix('baseline-').split('-')[0]}-vs-{target}-{datetime.date.today()}.md"
    abreport.write(a_dirs[-1], b_dir, dest)
    log.ok(f"A/B report: {dest}")
    return 0
