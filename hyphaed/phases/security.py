from __future__ import annotations
import json
import sys
from pathlib import Path

from ..util import log
from ..util.run import run

NAME = "security"

_REPO_ROOT = Path(__file__).resolve().parents[2]
def _find_hardening_checker() -> "Path | None":
    """Locate the kernel-hardening-checker entry point.

    It lives at `kernel-hardening-checker/bin/kernel-hardening-checker` — the
    repo is a Python project, and `bin/` is where its console script sits.
    This module looked for `kernel-hardening-checker/kernel-hardening-checker`
    (the directory, twice) and therefore **never once ran**, on any build,
    while `configure.py:21` had the same path right the whole time.

    Both candidates are tried, and the caller distinguishes "not found" from
    "found and clean" — see _run_hardening_checker's return contract.
    """
    base = _REPO_ROOT / "kernel-hardening-checker"
    for cand in (base / "bin" / "kernel-hardening-checker",
                 base / "kernel-hardening-checker"):
        if cand.is_file():
            return cand
    return None
_VEX_CHECKER = _REPO_ROOT / "vex-kernel-checker" / "vex-kernel-checker.py"
_VEX_FILE = _REPO_ROOT / "security" / "linux-kernel.vex.json"

# Symbol from CVE-2026-31431 primary fix commit — must be present in vmlinux.
# Its absence means the algif_aead revert patch didn't land.
_COPYFAIL_FIX_HINT = "algif_aead_copy_sgl"


def _run_hardening_checker(config_path: Path, out_dir: Path) -> tuple[int | None, int | None]:
    """Return (fail_count, ok_count), or (None, None) if the check DID NOT RUN.

    None is not a detail. The previous version returned (0, 0) when the tool
    was missing or its output unparseable, and the caller printed
    "security phase complete: hardening 0 FAIL" — a clean bill of health from
    a check that never executed. Since the binary path was also wrong, that is
    exactly what every build reported.

    A gate that cannot run must say so. Silence that looks like a pass is the
    failure mode this repo keeps rediscovering.
    """
    checker = _find_hardening_checker()
    if checker is None:
        log.warn(f"kernel-hardening-checker not found under "
                 f"{_REPO_ROOT / 'kernel-hardening-checker'} — NOT RUN "
                 f"(this is not a pass)")
        return None, None

    out_json = out_dir / "hardening-checker.json"
    r = run([str(checker), "-c", str(config_path), "-m", "json"],
            check=False, force=True)
    if not r.ok():
        log.warn(f"kernel-hardening-checker exited non-zero — output may be partial")

    try:
        data = json.loads(r.stdout)
    except (json.JSONDecodeError, ValueError):
        log.warn("kernel-hardening-checker: could not parse JSON output — "
                 "NOT RUN (this is not a pass)")
        return None, None

    out_json.write_text(json.dumps(data, indent=2))

    # `check_result` is never the bare string "FAIL". Real values on a 7.1.9
    # .config: 'FAIL: "y"' (60), 'FAIL: "m"' (22), 'FAIL: "is not set"' (17),
    # 'FAIL: is not found' (4), 'FAIL: CONFIG_KSTACK_ERASE is not "y"' … — the
    # suffix carries the value that was actually found. An `== "FAIL"` test
    # matches none of them and reported 0 FAIL against a real 111, while
    # `configure.py:61` had used `.startswith("FAIL")` correctly all along.
    # "WARN" never appears as a value at all.
    #
    # check_result_bool is the authoritative field; the string prefix is the
    # fallback for an older tool that predates it.
    def _failed(x: dict) -> bool:
        b = x.get("check_result_bool")
        if isinstance(b, bool):
            return not b
        return str(x.get("check_result", "")).startswith("FAIL")

    fails = [x for x in data if _failed(x)]

    # First 12, in the tool's own order. There is no `weight` key in this
    # output — sorting by `int(x.get("weight", 0))` silently sorted every
    # entry as 0, which is an arbitrary order wearing the label "top".
    for item in fails[:12]:
        name = item.get("option_name", "?")
        want = item.get("desired_val", "?")
        # The found value lives in the check_result suffix, e.g. 'FAIL: "m"'.
        got = str(item.get("check_result", "")).split(":", 1)
        got = got[1].strip() if len(got) > 1 else "not set"
        why = item.get("reason", "")
        log.warn(f"  hardening FAIL  {name:42s} = {got:22s} (want {want!r}) [{why}]")
    if len(fails) > 12:
        log.info(f"  … {len(fails) - 12} more in {out_json.name}")

    ok_count = len(data) - len(fails)
    log.ok(f"hardening-checker: {len(fails)} FAIL / {ok_count} OK of {len(data)} "
           f"checks → {out_json.name}")
    return len(fails), ok_count


def _run_vex_checker(config_path: Path, source_dir: Path | None, out_dir: Path) -> tuple[int, int]:
    """Return (exploitable_count, in_triage_count). Saves annotated VEX to out_dir."""
    if not _VEX_CHECKER.exists():
        log.warn(f"vex-kernel-checker not at {_VEX_CHECKER} — skip")
        return 0, 0
    if not _VEX_FILE.exists():
        log.warn(f"VEX file not at {_VEX_FILE} — skip")
        return 0, 0

    vex_out = out_dir / "vex-analysis.json"
    cmd = [
        sys.executable, str(_VEX_CHECKER),
        "--vex-file", str(_VEX_FILE),
        "--kernel-config", str(config_path),
        "--output", str(vex_out),
        "--config-only",
    ]
    if source_dir and source_dir.exists():
        cmd += ["--kernel-source", str(source_dir)]

    run(cmd, check=False, force=True)

    if not vex_out.exists():
        log.warn("vex-kernel-checker produced no output file")
        return 0, 0

    try:
        data = json.loads(vex_out.read_text())
    except (json.JSONDecodeError, ValueError):
        log.warn("vex-kernel-checker: could not parse output JSON")
        return 0, 0

    vulns = data.get("vulnerabilities", [])
    exploitable = [v for v in vulns if v.get("analysis", {}).get("state") == "exploitable"]
    in_triage = [v for v in vulns if v.get("analysis", {}).get("state") in ("in_triage", "under_investigation")]

    for v in exploitable:
        cve = v.get("id", "?")
        score = (v.get("ratings") or [{}])[0].get("score", "?")
        detail = (v.get("analysis") or {}).get("detail", "")[:80]
        desc = (v.get("description") or "")[:80]
        log.err(f"  CVE EXPLOITABLE {cve} (CVSS {score}): {desc}")
        if detail:
            log.warn(f"    → {detail}")

    for v in in_triage:
        cve = v.get("id", "?")
        log.warn(f"  CVE in-triage   {cve}")

    log.ok(f"vex-checker: {len(exploitable)} exploitable, {len(in_triage)} in-triage → {vex_out.name}")
    return len(exploitable), len(in_triage)


def run_phase(ctx) -> None:
    log.banner("Phase 5/9 — Security (hardening-checker + CVE scan)")

    config_path = (ctx.source_dir / ".config") if ctx.source_dir else None
    if config_path is None or (not ctx.dry_run and not config_path.exists()):
        log.warn("no .config found — run configure phase first; skipping security checks")
        return

    if ctx.dry_run:
        log.info("(dry-run) would run kernel-hardening-checker + vex-kernel-checker")
        return

    out_dir = ctx.repo_root / "out" / "security"
    out_dir.mkdir(parents=True, exist_ok=True)

    log.info("── kernel-hardening-checker ─────────────────────────────")
    fail_count, _ok_count = _run_hardening_checker(config_path, out_dir)

    log.info("── vex-kernel-checker (config-only, no network) ─────────")
    exploitable, in_triage = _run_vex_checker(config_path, ctx.source_dir, out_dir)

    if exploitable:
        log.err(f"{exploitable} exploitable CVE(s) with current .config — fix before building")
        log.err(f"Full report: {out_dir / 'vex-analysis.json'}")
        raise SystemExit(2)

    hardening_str = ("NOT RUN" if fail_count is None
                     else f"{fail_count} FAIL")
    summary = (f"hardening {hardening_str} | "
               f"CVE {exploitable} exploitable {in_triage} in-triage")
    log.ok(f"security phase complete: {summary}")
    log.info(f"reports → {out_dir}/")
