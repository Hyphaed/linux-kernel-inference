"""_summary_table must distinguish phases that ran THIS invocation from ones
only known complete from a prior run's persisted state — otherwise e.g.
`--phase detect` alone prints a previous build's multi-minute build/install
timings as if they'd just happened (real observed bug, 2026-08-06).
"""
import io
import types

from rich.console import Console

from hyphaed import cli
from hyphaed.state import PersistedState
from hyphaed.util import log


def _render(persisted: PersistedState, phases_this_run: list[str]) -> str:
    ctx = types.SimpleNamespace(persisted=persisted, phases_this_run=phases_this_run)
    buf = io.StringIO()
    original_console = log.console
    log.console = Console(file=buf, theme=log.THEME, width=200)
    try:
        cli._summary_table(ctx)
    finally:
        log.console = original_console
    return buf.getvalue()


def _row_for(out: str, phase: str) -> str:
    return next(l for l in out.splitlines() if f"│ {phase} " in l or f"│ {phase} " in l or f" {phase} " in l and "│" in l)


def test_phase_only_in_persisted_state_shows_as_cached_not_done():
    persisted = PersistedState()
    for i, name in enumerate(cli.phases.ORDER):
        persisted.mark_complete(name, 100.0 + i)  # stale prior run

    out = _render(persisted, phases_this_run=["detect"])

    assert "cached" not in _row_for(out, "detect")  # detect ran this run — plain checkmark
    # Every phase other than detect must be marked cached, not a fresh checkmark.
    for name in cli.phases.ORDER:
        if name == "detect":
            continue
        row = _row_for(out, name)
        assert "cached" in row, f"{name} should show as cached: {row!r}"


def test_phase_run_this_invocation_shows_plain_checkmark():
    persisted = PersistedState()
    persisted.mark_complete("detect", 0.2)

    out = _render(persisted, phases_this_run=["detect"])

    assert "cached" not in _row_for(out, "detect")


def test_phase_never_run_shows_dash_and_no_timing():
    persisted = PersistedState()
    out = _render(persisted, phases_this_run=[])
    row = _row_for(out, "postinstall")
    assert "cached" not in row
    assert "✓" not in row
