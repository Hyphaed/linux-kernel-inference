#!/usr/bin/env python3
"""log.ok/info/warn/err escape their argument — on purpose. Don't feed them markup.

The escaping is a feature: these helpers carry paths, config values and tool
output, none of which should be able to inject Rich markup or break rendering.
The cost is that a caller which pre-formats markup into its message gets the
tags printed literally, and that is exactly what the configure phase did:

    ✓  hardening report → hardening-7.1.9.json  (147/258 OK  [red]111 FAIL[/red])

Anything that genuinely needs colour goes through log.console.print and escapes
its own untrusted values — which is what detect.py and package.py already do.
"""
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console
from rich.markup import escape

from hyphaed.util import log


def _render(fn) -> str:
    """Run `fn` with log.console swapped for a capturing console."""
    cap = Console(file=io.StringIO(), theme=log.THEME, force_terminal=False, width=140)
    original = log.console
    log.console = cap
    try:
        fn()
    finally:
        log.console = original
    return cap.file.getvalue()


def test_log_ok_escapes_markup_and_that_is_intended():
    """Documents WHY the bug happened, so nobody 'fixes' it by removing escape()."""
    out = _render(lambda: log.ok("value is [red]dangerous[/red]"))
    assert "[red]" in out, "escaping was removed — untrusted content can now inject markup"


def test_log_helpers_escape_untrusted_content():
    """A config value containing brackets must not corrupt the line."""
    out = _render(lambda: log.info("CONFIG_LSM=[landlock,yama]"))
    assert "CONFIG_LSM=[landlock,yama]" in out


def test_hardening_summary_renders_colour_not_tags():
    """The real line from the configure phase, built the way configure.py
    builds it, must not show literal tags."""
    def emit():
        ok_n, fail, total, name = 147, 111, 258, "hardening-7.1.9.json"
        msg = f"{ok_n}/{total} OK"
        if fail:
            msg += f"  [red]{fail} FAIL[/red]"
        log.console.print(
            f"[ok]✓[/ok]  hardening report → {escape(name)}  ({msg})")

    out = _render(emit)
    assert "111 FAIL" in out
    assert "[red]" not in out and "[/red]" not in out, "markup printed literally"


def test_configure_does_not_hand_markup_to_an_escaping_helper():
    """Source-level guard on the specific regression."""
    src = (Path(__file__).resolve().parent.parent
           / "hyphaed" / "phases" / "configure.py").read_text()
    assert "log.ok(f\"hardening report" not in src, (
        "the hardening summary is back on log.ok(), which escapes its markup")
    assert "log.console.print(" in src
