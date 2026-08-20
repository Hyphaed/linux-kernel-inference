"""Cross-invocation state for the wizard.

Each phase emits structured outputs into state/ctx.json so a subsequent
`python -m hyphaed --phase build` doesn't need to re-run `detect` and
`source`. This is what makes `--phase X` and `--from-phase X` actually
usable for resuming a partial build.
"""
from __future__ import annotations
import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

STATE_FILE = "ctx.json"


@dataclass
class PersistedState:
    profile: dict | None = None
    source_dir: str | None = None
    kernel_pkgver: str = ""
    preset: str | None = None
    built_debs: list[str] = field(default_factory=list)
    packaged_debs: list[str] = field(default_factory=list)
    phases_completed: list[str] = field(default_factory=list)
    phase_timings_sec: dict[str, float] = field(default_factory=dict)
    source_mode: str = "kernel-org"
    patch_series_dir: str | None = None
    baseline_tag: str | None = None

    @classmethod
    def load(cls, state_dir: Path) -> "PersistedState":
        path = state_dir / STATE_FILE
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text())
            return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        except (json.JSONDecodeError, TypeError):
            return cls()

    def save(self, state_dir: Path) -> Path:
        state_dir.mkdir(parents=True, exist_ok=True)
        path = state_dir / STATE_FILE
        path.write_text(json.dumps(asdict(self), indent=2, default=str))
        return path

    def mark_complete(self, phase_name: str, elapsed_sec: float) -> None:
        if phase_name not in self.phases_completed:
            self.phases_completed.append(phase_name)
        self.phase_timings_sec[phase_name] = elapsed_sec

    def invalidate_from(self, phase_name: str, order: list[str]) -> None:
        """Drop `phase_name` and everything after it (per `order`) from the
        completed/timings record, and clear the build artefacts that a
        re-run of those phases would regenerate. Used when the resolved
        source target changes across invocations so a stale
        phases_completed doesn't let the dependency resolver skip a
        build/package that never ran against the new target."""
        if phase_name not in order:
            return
        idx = order.index(phase_name)
        stale = set(order[idx:])
        self.phases_completed = [p for p in self.phases_completed if p not in stale]
        for p in stale:
            self.phase_timings_sec.pop(p, None)
        self.built_debs = []
        self.packaged_debs = []
        self.kernel_pkgver = ""


def hydrate_ctx(ctx, state: PersistedState) -> None:
    """Populate ctx fields from persisted state, skipping anything already set."""
    from .topology import HardwareProfile

    if ctx.profile is None and state.profile:
        try:
            ctx.profile = HardwareProfile(**state.profile)
        except TypeError:
            pass

    if ctx.source_dir is None and state.source_dir:
        sp = Path(state.source_dir)
        if sp.exists():
            ctx.source_dir = sp

    if not ctx.kernel_pkgver and state.kernel_pkgver:
        ctx.kernel_pkgver = state.kernel_pkgver

    if not ctx.built_debs and state.built_debs:
        ctx.built_debs = [Path(p) for p in state.built_debs if Path(p).exists()]

    if not ctx.packaged_debs and state.packaged_debs:
        ctx.packaged_debs = [Path(p) for p in state.packaged_debs if Path(p).exists()]

    # Restore source mode and patch series dir so --phase patch works without --source-mode
    if getattr(ctx, "source_mode", "ubuntu") == "ubuntu" and state.source_mode != "ubuntu":
        ctx.source_mode = state.source_mode
    if not getattr(ctx, "patch_series_dir", None) and state.patch_series_dir:
        ctx.patch_series_dir = Path(state.patch_series_dir)
    if not getattr(ctx, "baseline_tag", None) and state.baseline_tag:
        ctx.baseline_tag = state.baseline_tag


def snapshot_ctx(ctx, state: PersistedState) -> None:
    """Copy ctx's outputs back into the persisted state object."""
    if ctx.profile is not None:
        state.profile = ctx.profile.to_dict() if hasattr(ctx.profile, "to_dict") else None
    if ctx.source_dir is not None:
        state.source_dir = str(ctx.source_dir)
    if ctx.kernel_pkgver:
        state.kernel_pkgver = ctx.kernel_pkgver
    if ctx.preset is not None:
        state.preset = ctx.preset
    state.built_debs = [str(p) for p in (ctx.built_debs or [])]
    state.packaged_debs = [str(p) for p in (ctx.packaged_debs or [])]
    if getattr(ctx, "source_mode", None):
        state.source_mode = ctx.source_mode
    psd = getattr(ctx, "patch_series_dir", None)
    if psd is not None:
        state.patch_series_dir = str(psd)
    bt = getattr(ctx, "baseline_tag", None)
    if bt:
        state.baseline_tag = bt
