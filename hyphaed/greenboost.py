"""Read GreenBoost's published hardware profile and cross-check against ours.

GreenBoost writes Markdown profiles with YAML-ish key-value lines under
/etc/greenboost/profiles/. We prefer the JSON export
(`greenboost_setup.sh profile export-json`) if `greenboost_setup.sh` is
on PATH, falling back to the Markdown parser.
"""
from __future__ import annotations
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .util import log
from .util.run import run

PROFILE_DIR = Path("/etc/greenboost/profiles")
ACTIVE_LINK = Path("/etc/greenboost/active_profile.md")

_KV_RE = re.compile(r"^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*(.+?)\s*$")


@dataclass
class GreenboostProfile:
    raw: dict[str, str]
    path: Path

    def get_int(self, key: str, default: int = 0) -> int:
        v = self.raw.get(key, "")
        m = re.search(r"-?\d+", v)
        return int(m.group()) if m else default

    def get_str(self, key: str, default: str = "") -> str:
        return self.raw.get(key, default)


def _try_json_export() -> dict[str, str] | None:
    """Prefer the structured JSON export if greenboost_setup.sh is on PATH."""
    binary = shutil.which("greenboost_setup.sh")
    if not binary:
        return None
    r = run([binary, "profile", "export-json"], check=False, force=True)
    if not r.ok() or not r.stdout.strip():
        return None
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return {str(k): str(v) for k, v in data.items()}


def find_active_profile() -> GreenboostProfile | None:
    json_raw = _try_json_export()
    if json_raw is not None:
        return GreenboostProfile(raw=json_raw, path=ACTIVE_LINK)

    candidate: Path | None = None
    if ACTIVE_LINK.exists():
        candidate = ACTIVE_LINK.resolve() if ACTIVE_LINK.is_symlink() else ACTIVE_LINK
    elif PROFILE_DIR.is_dir():
        defaults = sorted(PROFILE_DIR.glob("*.md"))
        if defaults:
            candidate = defaults[0]
    if candidate is None or not candidate.exists():
        return None
    raw: dict[str, str] = {}
    try:
        text = candidate.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if line.strip().startswith("#"):
            continue
        m = _KV_RE.match(line)
        if m:
            raw[m.group(1)] = m.group(2)
    if not raw:
        return None
    return GreenboostProfile(raw=raw, path=candidate)


def cross_check(profile, gb: GreenboostProfile) -> list[str]:
    """Return a list of human-readable drift messages between our profile
    and greenboost's. Empty list = consistent.
    """
    msgs: list[str] = []
    gb_pcores = gb.get_int("DET_P_CORES", gb.get_int("p_cores"))
    gb_ecores = gb.get_int("DET_E_CORES", gb.get_int("e_cores"))
    if gb_pcores and gb_pcores != profile.p_cores:
        msgs.append(f"greenboost reports {gb_pcores} P-cores; we see {profile.p_cores}")
    if gb_ecores and gb_ecores != profile.e_cores:
        msgs.append(f"greenboost reports {gb_ecores} E-cores; we see {profile.e_cores}")

    gb_vram = gb.get_int("GB_PHYS", gb.get_int("vram_gb"))
    if gb_vram and abs(gb_vram - profile.gpu_vram_gb) > 1:
        msgs.append(f"greenboost reports {gb_vram} GB VRAM; we see {profile.gpu_vram_gb}")

    return msgs


def maybe_log_summary(profile) -> None:
    gb = find_active_profile()
    if gb is None:
        return
    log.info(f"greenboost profile: {gb.path}")
    drift = cross_check(profile, gb)
    if drift:
        for m in drift:
            log.warn(f"  drift: {m}")
    else:
        log.ok("greenboost profile agrees with detected hardware")
