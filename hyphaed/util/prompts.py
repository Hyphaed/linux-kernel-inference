from __future__ import annotations
import sys
from rich.prompt import Confirm, Prompt

from . import log


class NonInteractive(Exception):
    pass


_NON_INTERACTIVE = False
_ASSUME_YES = False


def set_mode(non_interactive: bool, assume_yes: bool) -> None:
    global _NON_INTERACTIVE, _ASSUME_YES
    _NON_INTERACTIVE = non_interactive
    _ASSUME_YES = assume_yes


def is_non_interactive() -> bool:
    return _NON_INTERACTIVE


def _is_interactive() -> bool:
    """Return False when stdin is not a terminal or has been exhausted."""
    return bool(hasattr(sys.stdin, "isatty") and sys.stdin.isatty())


def confirm(question: str, default: bool = False, hard: bool = False) -> bool:
    """Ask y/N. If hard=True, --yes cannot bypass (used for destructive ops)."""
    if _NON_INTERACTIVE or not _is_interactive():
        if hard:
            raise NonInteractive(f"hard confirm cannot be bypassed in --non-interactive mode: {question}")
        return _ASSUME_YES or default
    if _ASSUME_YES and not hard:
        log.info(f"(auto-yes) {question}")
        return True
    try:
        return Confirm.ask(question, default=default, console=log.console)
    except EOFError:
        return default


def select(question: str, choices: list[str], default: str | None = None) -> str:
    if _NON_INTERACTIVE or not _is_interactive():
        if default is None:
            raise NonInteractive(f"no default for select: {question}")
        return default
    try:
        return Prompt.ask(question, choices=choices, default=default, console=log.console)
    except EOFError:
        if default is None:
            raise NonInteractive(f"no default for select: {question}")
        return default


def text(question: str, default: str | None = None) -> str:
    if _NON_INTERACTIVE or not _is_interactive():
        if default is None:
            raise NonInteractive(f"no default for text prompt: {question}")
        return default
    try:
        return Prompt.ask(question, default=default, console=log.console)
    except EOFError:
        if default is None:
            raise NonInteractive(f"no default for text prompt: {question}")
        return default
