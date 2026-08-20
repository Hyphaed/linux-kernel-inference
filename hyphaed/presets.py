from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]
PRESET_DIR = ROOT / "configs" / "presets"


@dataclass
class Preset:
    name: str
    description: str = ""
    fragments: list[str] = field(default_factory=list)
    cmdline_extra: list[str] = field(default_factory=list)
    patches: list[str] = field(default_factory=list)
    dkms_modules: list[str] = field(default_factory=list)


def load(name: str) -> Preset:
    path = PRESET_DIR / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"preset not found: {path}")
    data = yaml.safe_load(path.read_text()) or {}
    return Preset(
        name=data.get("name", name),
        description=data.get("description", ""),
        fragments=list(data.get("fragments", [])),
        cmdline_extra=list(data.get("cmdline_extra", [])),
        patches=list(data.get("patches", [])),
        dkms_modules=list(data.get("dkms_modules", [])),
    )


def list_available() -> list[str]:
    if not PRESET_DIR.exists():
        return []
    return sorted(p.stem for p in PRESET_DIR.glob("*.yaml"))
