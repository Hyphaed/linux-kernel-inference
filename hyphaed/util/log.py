from __future__ import annotations
import datetime as _dt
import os
from pathlib import Path
from rich.console import Console
from rich.markup import escape
from rich.theme import Theme

THEME = Theme({
    "info": "cyan",
    "ok": "bold green",
    "warn": "bold yellow",
    "err": "bold red",
    "phase": "bold magenta",
    "muted": "grey50",
    "kbd": "bold white on grey30",
})

console = Console(theme=THEME, highlight=False)


def route_diagnostics_to_stderr() -> None:
    """Send every diagnostic (banner, `$ cmd` echoes, info/warn/err) to
    stderr, leaving stdout for machine-readable output alone.

    Needed because the JSON payload and the banner shared one console, so
    `hyphaed list-versions --json` emitted a rule, two prose lines and every
    `$ git …` echo ahead of the JSON. `json.load()` fails at char 0 on that,
    and `install_wizard.sh` catches the failure and exits its parser quietly,
    which showed the operator an empty release list rather than an error.
    Found 2026-08-20 alongside the fetch hang; either bug alone was enough to
    leave the wizard with nothing to display.

    Every helper in this module looks `console` up at call time, so
    reassigning it here redirects callers that already hold `log` too.
    """
    global console
    console = Console(theme=THEME, highlight=False, stderr=True)

_LOG_DIR: Path | None = None


def set_log_dir(path: Path) -> None:
    global _LOG_DIR
    _LOG_DIR = path
    path.mkdir(parents=True, exist_ok=True)


def log_dir() -> Path:
    if _LOG_DIR is None:
        d = Path(__file__).resolve().parents[2] / "out" / "logs"
        d.mkdir(parents=True, exist_ok=True)
        return d
    return _LOG_DIR


def phase_log_path(phase: str) -> Path:
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return log_dir() / f"{phase}-{ts}.log"


def banner(text: str) -> None:
    console.rule(f"[phase]{text}[/phase]")


def info(msg: str) -> None:
    console.print(f"[info]i[/info]  {escape(msg)}")


def ok(msg: str) -> None:
    console.print(f"[ok]✓[/ok]  {escape(msg)}")


def warn(msg: str) -> None:
    console.print(f"[warn]![/warn]  {escape(msg)}")


def err(msg: str) -> None:
    console.print(f"[err]✗[/err]  {escape(msg)}")


def step(n: int, total: int, msg: str) -> None:
    console.print(f"[muted][{n}/{total}][/muted] {msg}")


def kbd(s: str) -> str:
    return f"[kbd] {s} [/kbd]"
