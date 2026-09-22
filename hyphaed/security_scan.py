"""Runtime security-hardening scan: kconfig + cmdline + sysctl, with waivers.

`hyphaed/phases/security.py` (the build-pipeline phase) and
`configure.py::_run_hardening_check` both already run kernel-hardening-checker,
but **only in `-c` (kconfig) mode**. Measured live 2026-09-22 on this box with
`-l /proc/cmdline` and `-s <sysctl -a dump>` added: 36 cmdline FAILs and 17
sysctl FAILs on top of the 112 kconfig FAILs already tracked — two-thirds of
the tool's real coverage had never once been looked at here.

Most of those FAILs are deliberate tradeoffs for this workload (`iommu=pt`
not `force`, io_uring left on, unprivileged userns for Chrome/flatpak/podman,
`perf_event_paranoid=2` for profiling) — a wizard that "fixes" all of them
blindly would break the box. `configs/security-waivers.yaml` is the mechanism
that makes that distinction reviewable rather than silent: every accepted
FAIL needs a written reason there, or it counts as unwaived.

This module is intentionally separate from `hyphaed/phases/security.py`: that
phase is a build-pipeline gate (CVE-only, non-blocking on hardening) that
needs a fresh `.config` from the current build. This is a standalone,
re-runnable command — `hyphaed security-scan report|apply` — that also reads
the RUNNING system's cmdline and sysctl state, the way `hyphaed tune` already
does for other runtime checks.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .phases.security import _find_hardening_checker
from .util import log
from .util.run import run, run_sudo

WAIVER_FILE = Path(__file__).resolve().parents[1] / "configs" / "security-waivers.yaml"
SYSCTL_DROPIN = Path("/etc/sysctl.d/96-hyphaed-hardening.conf")

# check_result is never the bare string "FAIL" — see
# hyphaed/phases/security.py's _run_hardening_checker docstring for the full
# story of why an == comparison here would silently find nothing.
def _failed(entry: dict) -> bool:
    b = entry.get("check_result_bool")
    if isinstance(b, bool):
        return not b
    return str(entry.get("check_result", "")).startswith("FAIL")


@dataclass
class ScanResult:
    mode: str  # "kconfig" | "cmdline" | "sysctl"
    entries: list[dict] = field(default_factory=list)
    ran: bool = False


def scan_all(config_path: Path | None, out_dir: Path, *,
             cmdline_path: Path = Path("/proc/cmdline")) -> dict[str, ScanResult]:
    """Run all three axes the tool supports, in ONE invocation.

    The checker's `-l` (cmdline) mode hard-requires `-c` alongside it —
    `[-] ERROR: checking cmdline depends on checking Kconfig` — confirmed
    live 2026-09-22; an earlier version of this function called `-c`, `-l`,
    `-s` as three separate invocations and `-l` alone always failed to
    parse. `-s` alone does work standalone, but running everything together
    is both correct for `-l` and cheaper (one process, one parse), and the
    `type` field on each returned entry ("kconfig"/"cmdline"/"sysctl")
    is what splits the single result list back into three axes.

    Missing inputs report as NOT RUN, never as a silent clean pass — same
    invariant `security.py` already established for kconfig mode alone.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    checker = _find_hardening_checker()
    if checker is None:
        log.warn(f"kernel-hardening-checker not found — NOT RUN (this is not a pass)")
        return {
            "kconfig": ScanResult("kconfig", ran=False),
            "cmdline": ScanResult("cmdline", ran=False),
            "sysctl": ScanResult("sysctl", ran=False),
        }

    have_config = config_path is not None and config_path.exists()
    have_cmdline = cmdline_path.exists()

    cmd = [str(checker), "-m", "json"]
    if have_config:
        cmd += ["-c", str(config_path)]
    else:
        log.warn("no .config given — kconfig axis NOT RUN")
    if have_cmdline:
        if not have_config:
            log.warn(f"{cmdline_path} found but no .config given — cmdline mode "
                     f"requires -c alongside -l, cmdline axis NOT RUN too")
            have_cmdline = False
        else:
            cmd += ["-l", str(cmdline_path)]
    else:
        log.warn(f"{cmdline_path} not found — cmdline axis NOT RUN")

    sysctl_dump = out_dir / "sysctl-dump.txt"
    r = run(["sysctl", "-a"], check=False, force=True)
    have_sysctl = bool(r.stdout.strip())
    if have_sysctl:
        sysctl_dump.write_text(r.stdout)
        cmd += ["-s", str(sysctl_dump)]
    else:
        log.warn("`sysctl -a` produced no output — sysctl axis NOT RUN")

    results = {
        "kconfig": ScanResult("kconfig", ran=False),
        "cmdline": ScanResult("cmdline", ran=False),
        "sysctl": ScanResult("sysctl", ran=False),
    }
    if not (have_config or have_sysctl):
        return results

    r = run(cmd, check=False, force=True)
    try:
        data = json.loads(r.stdout)
    except (json.JSONDecodeError, ValueError):
        log.warn("kernel-hardening-checker: could not parse JSON output — NOT RUN")
        return results

    out_json = out_dir / "security-scan.json"
    out_json.write_text(json.dumps(data, indent=2))

    requested = {"kconfig": have_config, "cmdline": have_cmdline, "sysctl": have_sysctl}
    for mode in ("kconfig", "cmdline", "sysctl"):
        entries = [e for e in data if e.get("type") == mode]
        # ran means "we asked for this axis and got entries tagged with it
        # back" — asked-but-empty would otherwise look identical to
        # not-asked, which is the same NOT-RUN-vs-clean-pass ambiguity
        # security.py's docstring already warns about.
        results[mode] = ScanResult(mode=mode, entries=entries, ran=requested[mode] and len(entries) > 0)

    return results


def load_waivers() -> dict[str, dict]:
    """Return {option_name: {reason: str, decision: str|None}}. Missing file
    is not an error — an empty waiver set means every FAIL is unwaived.
    """
    if not WAIVER_FILE.exists():
        return {}
    data = yaml.safe_load(WAIVER_FILE.read_text()) or {}
    waivers = data.get("waivers", [])
    out = {}
    for w in waivers:
        name = w.get("option_name")
        if name and w.get("reason"):
            out[name] = w
    return out


def classify(results: dict[str, ScanResult], waivers: dict[str, dict]) -> dict:
    """Cross-reference every FAIL against the waiver file. Returns a summary
    dict with per-mode fail/ok/waived counts and the unwaived FAIL list —
    the thing a wizard should actually act on or show.
    """
    summary = {}
    unwaived: list[dict] = []
    for mode, res in results.items():
        if not res.ran:
            summary[mode] = {"ran": False}
            continue
        fails = [e for e in res.entries if _failed(e)]
        ok = len(res.entries) - len(fails)
        waived = [e for e in fails if e.get("option_name") in waivers]
        not_waived = [e for e in fails if e.get("option_name") not in waivers]
        unwaived.extend(not_waived)
        summary[mode] = {
            "ran": True,
            "total": len(res.entries),
            "ok": ok,
            "fail": len(fails),
            "waived": len(waived),
            "unwaived": len(not_waived),
        }
    return {"by_mode": summary, "unwaived": unwaived}


# ---------------------------------------------------------------------------
# apply: runtime sysctl only, by default. cmdline/kconfig are propose-only —
# see module docstring and the plan this was built from for why: both need a
# reboot or a rebuild, so a wizard that wrote them without a human looking at
# the diff first would be the same "wizard fixed something and now the box
# doesn't boot the way you expect" failure this repo's other tools go out of
# their way to avoid (grub.py's own confirm-gated write_dropin is the
# precedent).
# ---------------------------------------------------------------------------

# Genuinely free wins measured on this box 2026-09-22: no stated functional
# cost, reversible with no reboot. Anything NOT in this list that shows up as
# an unwaived sysctl FAIL is reported, never auto-applied — it needs a human
# to add either a waiver (with a reason) or a line here (with evidence).
_SYSCTL_SAFE_FIXES: dict[str, str] = {
    "fs.suid_dumpable": "0",
    "fs.protected_fifos": "2",
    "kernel.oops_limit": "100",
    "kernel.warn_limit": "100",
}


def propose_sysctl_fixes(unwaived: list[dict]) -> dict[str, str]:
    """Return {sysctl_key: desired_value} for the subset of unwaived sysctl
    FAILs this module considers a free win. Everything else is reported only.
    """
    names = {e.get("option_name") for e in unwaived}
    return {k: v for k, v in _SYSCTL_SAFE_FIXES.items() if k in names}


def render_sysctl_dropin(fixes: dict[str, str]) -> str:
    lines = [
        "# Written by `hyphaed security-scan apply`.",
        "# Each key here is a kernel-hardening-checker sysctl FAIL this repo",
        "# has verified has no functional cost on this workload. See",
        "# configs/security-waivers.yaml for the FAILs deliberately NOT fixed.",
        "",
    ]
    for k, v in sorted(fixes.items()):
        lines.append(f"{k} = {v}")
    return "\n".join(lines) + "\n"


def apply_sysctl_fixes(fixes: dict[str, str], *, dry_run: bool = False) -> bool:
    """Write the sysctl drop-in and load it. Returns True if anything changed."""
    if not fixes:
        return False
    content = render_sysctl_dropin(fixes)
    if SYSCTL_DROPIN.exists() and SYSCTL_DROPIN.read_text() == content:
        log.info(f"{SYSCTL_DROPIN} already up to date — skipping write")
        return False
    if dry_run:
        log.info(f"would write {SYSCTL_DROPIN}:")
        for line in content.splitlines():
            log.console.print(f"    {line}")
        return True
    run_sudo(["mkdir", "-p", str(SYSCTL_DROPIN.parent)])
    run_sudo(["tee", str(SYSCTL_DROPIN)], input_str=content, capture=True)
    run_sudo(["sysctl", "--system"], check=False)
    log.ok(f"wrote {SYSCTL_DROPIN} and reloaded")
    return True


def propose_cmdline_diff(unwaived: list[dict]) -> list[str]:
    """Return the list of cmdline tokens an unwaived FAIL suggests adding.
    Report-only — the caller decides whether to hand this to grub.py's
    existing diff/confirm machinery. Never writes anything itself.
    """
    tokens = []
    for e in unwaived:
        name = (e.get("option_name") or "").strip()
        want = str(e.get("desired_val", ""))
        if not name:
            continue
        if want in ("is present",):
            tokens.append(name)
        elif want and want not in ("is not off", "is not found"):
            tokens.append(f"{name}={want}")
    return tokens


def generate_kconfig_suggestions(arch: str, out_dir: Path) -> Path | None:
    """Run the checker's own `-g` generate mode and write the result for
    manual comparison against configs/fragments/20-mitigations-on.config.
    Never edits a fragment directly — a Kconfig change needs a rebuild
    either way, so this is propose-only like the cmdline path.
    """
    checker = _find_hardening_checker()
    if checker is None:
        return None
    r = run([str(checker), "-g", arch], check=False, force=True)
    if not r.stdout.strip():
        return None
    out_path = out_dir / "kconfig-hardening-suggestions.diff"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(r.stdout)
    return out_path
