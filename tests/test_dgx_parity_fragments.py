"""DGX-OS-parity port, 2026-08-27 — see docs/dgx-os-parity-2026-08-27.md for
the full decision table (what's adopted, what's rejected, and why).

This pins the shape of the port, not its runtime behavior (the diagnostics
scripts touch real hardware and can't be exercised meaningfully without
root/real PCIe devices in CI):

  1. The three ADOPT items land in postinstall.py, idempotently.
  2. The opt-in scripts are dry-run by default and never auto-applied.
  3. The explicitly REJECTed DGX defaults (mitigations=off,
     transparent_hugepage=madvise) never sneak into anything this port
     touches — a regression here would silently violate this repo's hard
     constraints.
"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POSTINSTALL = ROOT / "hyphaed" / "phases" / "postinstall.py"
DIAG = ROOT / "diagnostics"
PARITY_CHECK = DIAG / "dgx-parity-check.sh"
ACS_TOGGLE = DIAG / "dgx-acs-toggle.sh"
RO_TOGGLE = DIAG / "dgx-pci-relaxed-ordering-toggle.sh"
DOC = ROOT / "docs" / "dgx-os-parity-2026-08-27.md"

SCRIPTS = [PARITY_CHECK, ACS_TOGGLE, RO_TOGGLE]


def test_all_three_scripts_exist_and_are_executable():
    for s in SCRIPTS:
        assert s.exists(), f"{s} is missing"
        assert s.stat().st_mode & 0o111, f"{s} is not executable"


def test_all_three_scripts_are_valid_bash():
    for s in SCRIPTS:
        r = subprocess.run(["bash", "-n", str(s)], capture_output=True, text=True)
        assert r.returncode == 0, f"{s} failed bash -n: {r.stderr}"


def test_toggle_scripts_default_to_dry_run():
    """--go must be a real, separate flag — not implied by --enable/--disable
    alone. A script that mutates PCIe/NVMe state by default would violate
    this repo's dry-run-by-default convention (nvidia-driver-switch.sh sets
    the precedent).
    """
    for s in (ACS_TOGGLE, RO_TOGGLE):
        text = s.read_text()
        assert 'GO=0' in text, f"{s} doesn't default GO to 0"
        assert "--go" in text, f"{s} has no --go flag"


def test_parity_check_never_mutates():
    """dgx-parity-check.sh is pure report — grep for the mutating verbs the
    adopt-path uses elsewhere (modprobe, apt-get install, systemctl enable,
    setpci ...=, nvme set-feature) as ACTUAL COMMANDS, not as advice printed
    in a "Fix: sudo modprobe ..." string (which the script legitimately
    prints to tell the operator what to run by hand).
    """
    mutating_markers = [
        "modprobe ", "apt-get install", "apt install",
        "systemctl enable", "systemctl start",
        "ECAP_ACS+0x6.w=", "set-feature",
    ]
    advisory_hints = ("echo ", "warn ", "printf ", "#")
    for lineno, line in enumerate(PARITY_CHECK.read_text().splitlines(), 1):
        stripped = line.strip()
        if not stripped or any(h in stripped for h in advisory_hints):
            continue  # comment or printed advice, not an executed command
        for marker in mutating_markers:
            assert marker not in stripped, (
                f"dgx-parity-check.sh:{lineno} executes '{marker}' — "
                f"it must stay report-only: {stripped!r}"
            )


def test_acs_toggle_reads_before_v_flag_bug_is_not_reintroduced():
    """Found and fixed 2026-08-27: `setpci -v` prints the bus address on
    stdout even when the ACS extended capability is ABSENT (the "not found"
    error goes to stderr), so parsing -v's stdout misreported every device
    as ACS-capable/enabled. The fix is to probe without -v and gate on exit
    code. Guard against the bug coming back.
    """
    text = ACS_TOGGLE.read_text()
    assert "setpci -v -s \"$BDF\" ECAP_ACS+0x6.w 2>/dev/null | awk" not in text, (
        "dgx-acs-toggle.sh reintroduced the -v/stdout-parsing ACS detection bug"
    )
    assert 'setpci -s "$BDF" ECAP_ACS+0x6.w' in text


def test_postinstall_wires_dgx_parity_basics():
    text = POSTINSTALL.read_text()
    assert "_check_dgx_parity_basics" in text
    import re
    assert re.search(r"^\s*_check_dgx_parity_basics\(\)\s*$", text, re.MULTILINE), (
        "_check_dgx_parity_basics() is defined but never called from run_phase()"
    )


def test_rejected_dgx_defaults_never_appear_as_applied():
    """mitigations=off and transparent_hugepage=madvise are explicitly
    REJECTed in the parity doc, and both the doc and postinstall.py's
    docstring legitimately NAME them to explain the reject — that's
    documentation, not application. What would actually be a regression is
    either string appearing inside a real GRUB_CMDLINE / cmdline-composing
    assignment, the only mechanism that could make either take effect. None
    of this port's files touch GRUB at all, so this checks for that specific
    shape rather than bare substring mention (which false-positives on the
    port's own explanatory comments).
    """
    import re

    for f in (POSTINSTALL, PARITY_CHECK, ACS_TOGGLE, RO_TOGGLE):
        text = f.read_text()
        for banned in ("mitigations=off", "transparent_hugepage=madvise"):
            for m in re.finditer(re.escape(banned), text):
                line_start = text.rfind("\n", 0, m.start()) + 1
                line_end = text.find("\n", m.end())
                line = text[line_start:line_end if line_end != -1 else None]
                assert "GRUB_CMDLINE" not in line and "cmdline" not in line.lower(), (
                    f"{f} applies '{banned}' via a cmdline assignment: {line.strip()!r}"
                )


def test_dgx_parity_doc_exists_and_documents_the_rejects():
    assert DOC.exists()
    text = DOC.read_text()
    for must_mention in ("REJECT", "nv-mitigations-off", "nv-hugepage", "404"):
        assert must_mention in text, f"parity doc is missing expected content: {must_mention}"
