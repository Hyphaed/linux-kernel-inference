"""nvidia-pci-bridge-power.service warned on every boot:

  Ignoring unknown escape sequences: "for b in $(lspci -D -d ::0604 | awk
  \"{print \\$1}\"); do echo on > /sys/bus/pci/devices/$b/power/control
  2>/dev/null || true; done"

systemd's unit-file parser does its own backslash-escape and $VAR expansion
on an ExecStart= value BEFORE the shell sees it. The backslash-dollar
sequence isn't a systemd escape sequence, and unescaped `$b`/`$1` risk
systemd trying to substitute
them as its own specifiers rather than leaving them for the shell. Found on
the 2026-09-22 boot audit of 7.2.6-hyphaed.

Fix: move the shell logic to a real script file
(/usr/local/sbin/hyphaed-pci-bridge-power-on.sh) and have ExecStart= just
invoke it — no unit-line quoting/escaping left to get wrong. Same pattern
this repo already uses for ssd-speed-guard.sh (a real script called from a
udev RUN+=, not inlined).
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POSTINSTALL = ROOT / "hyphaed" / "phases" / "postinstall.py"


def _source() -> str:
    return POSTINSTALL.read_text()


def test_execstart_no_longer_inlines_shell_with_backslash_dollar():
    src = _source()
    # The exact defect, verbatim: a backslash immediately before a dollar
    # sign inside the ExecStart= string literal. This is what systemd
    # warned "Ignoring unknown escape sequences" about every boot.
    assert r'\\$1' not in src, (
        "postinstall.py still builds an ExecStart= value containing "
        r"the old \$1 fragment — this is the exact sequence systemd warned "
        "'Ignoring unknown escape sequences' about every boot."
    )


def test_execstart_invokes_a_real_script_not_inline_sh_dash_c():
    src = _source()
    assert 'ExecStart={bridge_script}' in src, (
        "ExecStart= should reference the script path variable, not an "
        "inline `/bin/sh -c '...'` string — inlining is what put unescaped "
        "$ characters in the unit file in the first place."
    )
    # The unit_body f-string LITERAL itself must not contain an inline
    # shell invocation any more — only the (now separate) script_body may.
    # Stop at the closing `)` of the unit_body assignment, not at the next
    # run_sudo() call, so a comment mentioning "/bin/sh -c" in prose
    # between the two doesn't false-positive this check.
    unit_section = src.split("unit_body = (")[1].split('    )\n')[0]
    assert "/bin/sh -c" not in unit_section


def test_script_body_uses_shell_positional_dollar_unescaped():
    """Inside the standalone script file (not a systemd unit value), a bare
    $b / $1 / $(...) is correct shell syntax — only the OLD inline-in-unit
    form needed escaping it never got right. Confirms the new script body
    still does the same PCI bridge power-on work."""
    src = _source()
    assert "script_body" in src
    assert "lspci -D -d ::0604" in src
    assert "power/control" in src
    assert "echo on" in src


def test_bridge_script_path_is_under_usr_local_sbin():
    src = _source()
    assert 'bridge_script = Path("/usr/local/sbin/hyphaed-pci-bridge-power-on.sh")' in src


def test_a_preexisting_stale_unit_gets_rewritten_not_skipped():
    """The original version of this code only wrote the unit file when it
    was ABSENT — `if not bridge_unit.exists(): ...`. A box that had already
    installed a kernel before this fix landed (this box, under 7.2.6) would
    then carry the broken unit forever: a fresh install's postinstall run
    would see the file already exists and silently do nothing, so the boot
    warning this fix targets would survive every future kernel bump unless
    a separate diagnostics script ran. Confirmed live 2026-09-22 — that is
    exactly what happened. Pin the fix: staleness is decided by CONTENT
    comparison, not just existence."""
    src = _source()
    assert "if not bridge_unit.exists():" not in src, (
        "the write-gate must not be an existence-only `if`  — a stale unit "
        "with old content needs to be rewritten on the next install, not "
        "skipped just because a file happens to be present at that path."
    )
    assert "bridge_unit.read_text() != unit_body" in src, (
        "postinstall.py must compare the on-disk unit's content against "
        "the current unit_body, so an old broken unit gets overwritten "
        "instead of silently left in place."
    )
