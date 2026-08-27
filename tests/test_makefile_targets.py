"""Every .PHONY name in the repo Makefile must have a rule.

Found 2026-08-26: `install` was listed in .PHONY and had no rule anywhere.
`make install` therefore printed

    make: Nothing to be done for 'install'.

and exited 0. That is the worst possible shape for the failure -- CLAUDE.md
tells the operator to use `make install` instead of `dpkg -i *` precisely
because the install phase also applies the GRUB drop-in and runs the NVIDIA
ENDBR/IBT and nvidia-fs guards, and 7.1.10 was installed with `dpkg -i *`
anyway.

The .PHONY entry is what made it silent. Without it make says "No rule to
make target 'install'" and fails, which anyone would have noticed. Same
lesson as the wizard's dead knobs: a control that cannot change the outcome
is not a control.
"""
from __future__ import annotations

import re
from pathlib import Path

MAKEFILE = Path(__file__).resolve().parent.parent / "Makefile"


def _phony_names() -> set[str]:
    text = MAKEFILE.read_text()
    # .PHONY can be continued across lines with a trailing backslash.
    text = re.sub(r"\\\n", " ", text)
    names: set[str] = set()
    for line in text.splitlines():
        if line.startswith(".PHONY:"):
            names.update(line.split(":", 1)[1].split())
    return names


def _rule_names() -> set[str]:
    names: set[str] = set()
    for line in MAKEFILE.read_text().splitlines():
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*:(?!=)", line)
        if m:
            names.add(m.group(1))
    return names


def test_every_phony_target_has_a_rule():
    phony, rules = _phony_names(), _rule_names()
    assert phony, "no .PHONY line found — did the Makefile change shape?"
    missing = sorted(phony - rules)
    assert not missing, (
        "these are declared .PHONY but have no rule, so `make <name>` prints "
        "\"Nothing to be done\" and exits 0 instead of failing:\n  "
        + "\n  ".join(missing)
    )

    # The other direction. Milder -- a rule missing from .PHONY works right
    # up until a file of that name lands in the tree -- but it is the same
    # list drifting out of step, and `scx` being in .PHONY while the six real
    # scx-* rules were not is what that drift looked like here.
    unlisted = sorted(rules - phony)
    assert not unlisted, (
        "these rules are not declared .PHONY, so make will skip them if a "
        "file of the same name ever exists:\n  " + "\n  ".join(unlisted)
    )


def test_install_target_runs_the_install_phase():
    """The specific one, pinned by name.

    `make install` must reach hyphaed's install+postinstall phases. Anything
    that shells out to dpkg directly would skip apply_boot_config() and the
    postinstall guards, and would not filter out/debs/ to linux-*.deb.
    """
    body: list[str] = []
    seen = False
    for line in MAKEFILE.read_text().splitlines():
        if re.match(r"^install\s*:(?!=)", line):
            seen = True
            continue
        if seen:
            if line.startswith("\t"):
                body.append(line.strip())
            elif line.strip():
                break
    assert seen, "Makefile has no `install:` target"
    joined = " ".join(body)
    assert "hyphaed" in joined and "postinstall" in joined, (
        f"`make install` does not run hyphaed's postinstall phase: {joined!r}"
    )
    assert "dpkg" not in joined, (
        "`make install` must go through hyphaed's install phase, which globs "
        "out/debs/linux-*.deb and then applies the GRUB drop-in — a bare dpkg "
        "call skips both"
    )
