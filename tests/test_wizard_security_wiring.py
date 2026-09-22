"""install_wizard.sh had no way to reach the `security` build phase, and no
way to reach any of the standalone diagnostic commands (`verify`, `status`,
`doctor`, `tune`, `security-scan`, `check-deps`) short of exiting the wizard
and typing `python3 -m hyphaed <cmd>` by hand. Fixed in two parts:

  1. `security` added to `PHASES` (and both duplicated inline Python
     phase-order literals used for checkpoint resolution) so a full/resume/
     single-phase run covers it, same position as
     hyphaed/phases/__init__.py::ORDER.
  2. A new "Checks & diagnostics" hub (`screen_checks`, welcome-menu item 5)
     groups every read-only/self-diagnostic command behind one looping menu
     — pick a check, it runs live, press Enter, back to the same menu — so
     an operator can run several in one sitting without re-launching the
     wizard or remembering the CLI incantations.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WIZARD = ROOT / "install_wizard.sh"


def _source() -> str:
    return WIZARD.read_text()


def test_security_is_in_the_phases_array():
    src = _source()
    assert "PHASES=(detect source patch configure security build package install postinstall)" in src, (
        "PHASES must include 'security' between configure and build — same "
        "position as hyphaed/phases/__init__.py::ORDER."
    )


def test_both_inline_checkpoint_orderings_include_security():
    """Two separate embedded Python snippets duplicate the phase order for
    checkpoint/resume logic (screen_welcome's last-completed lookup, and
    main()'s resume-mode lookup). Both must agree with PHASES or resume
    logic silently mis-detects the last completed phase."""
    src = _source()
    occurrences = src.count(
        'order = ["detect", "source", "patch", "configure", "security", '
        '"build", "package", "install", "postinstall"]'
    )
    assert occurrences == 2, (
        f"expected both inline phase-order literals to include 'security', "
        f"found {occurrences} matching occurrences (want 2)"
    )


def test_welcome_menu_offers_a_checks_and_diagnostics_entry():
    src = _source()
    assert 'menu_item 5 "Checks & diagnostics"' in src
    assert '5) MODE="checks"; return ;;' in src


def test_checks_mode_is_dispatched_before_source_mode_screen():
    """Like `fetch`, a diagnostic check needs no kernel version picked — it
    must short-circuit in main() before screen_source_mode, not after."""
    src = _source()
    dispatch = src.split("main() {")[1]
    fetch_idx = dispatch.index('MODE" == "fetch"')
    checks_idx = dispatch.index('MODE" == "checks"')
    source_mode_idx = dispatch.index("screen_source_mode")
    assert fetch_idx < checks_idx < source_mode_idx


def test_screen_checks_offers_every_standalone_diagnostic():
    """Every read-only self-check this repo exposes via the CLI should be
    reachable from this one menu — the whole point of the request that
    produced it ('user can easily run all what is offered through the
    wizard')."""
    src = _source()
    assert "screen_checks()" in src
    body = src.split("screen_checks()")[1].split("\n}\n")[0]
    expected_commands = [
        "hyphaed verify",
        "hyphaed status",
        "hyphaed doctor",
        "hyphaed tune report",
        "hyphaed tune apply",
        "hyphaed security-scan report",
        "hyphaed security-scan apply",
        "hyphaed check-deps",
    ]
    for cmd in expected_commands:
        assert cmd in body, f"screen_checks is missing: {cmd}"


def test_checks_hub_loops_back_to_itself_not_exits():
    """A single-shot screen (like screen_fetch) exits after one action.
    This hub is explicitly a home base for running SEVERAL checks in one
    sitting, so its menu loop must only return on the explicit 'Back'
    choice (0), never fall through and exit after running one check."""
    src = _source()
    body = src.split("screen_checks() {")[1].split("\n}\n")[0]
    assert "while true; do" in body
    assert '0) return ;;' in body


def test_run_check_guards_against_set_dash_e_killing_the_wizard():
    """install_wizard.sh runs under `set -euo pipefail`. Every diagnostic
    command here can legitimately return non-zero (verify/tune/security-scan
    all use a non-zero exit to mean "found something to fix", not "crashed")
    — every call site must guard with `|| true` or a single failed check
    would silently kill the whole wizard process."""
    src = _source()
    body = src.split("screen_checks() {")[1].split("\n}\n")[0]
    for line in body.splitlines():
        if "_run_check " in line and "case" not in line:
            assert "|| true" in line, (
                f"_run_check call isn't guarded with '|| true', a non-zero "
                f"exit here would kill the whole wizard under set -e: {line!r}"
            )


def test_a_report_finding_something_is_not_shown_as_a_crash():
    """Confirmed live 2026-09-22: running 'Security scan' from the wizard
    (148 unwaived FAILs, the expected first-run result) rendered as
    '✗  Security scan — exited 1 (see output above)' — a working report
    tool doing exactly its job, styled identically to a genuine crash.
    Every command _run_check drives (verify/tune/security-scan/check-deps)
    uses its exit code as a VERDICT ("found something to review"), not a
    process-health signal — a real crash already shows its own traceback
    in the output just above this line. Pin the honest framing."""
    src = _source()
    body = src.split("_run_check() {")[1].split("\n}\n")[0]
    assert "fail(" not in body, (
        "_run_check must not use the alarming fail() styling — none of its "
        "callers' non-zero exits represent a crash, only 'found something'."
    )
    assert "clean" in body and "review" in body, (
        "expected neutral wording distinguishing 'clean' from 'found "
        "something to review', not an error framing"
    )


def test_screen_fetch_delegates_to_hyphaed_fetch_patches_not_a_bare_call():
    """`patches/fetch.py` with no --lock defaults to patches/VENDOR.lock —
    the LEGACY ubuntu-source-mode lock, dead since --source-mode kernel-org
    became the sole mode (2026-08-06). Confirmed live 2026-09-22: running
    the wizard's 'Fetch patches only' hit a sha mismatch because the legacy
    lock's pin had drifted stale, while the real kernel-org lock had the
    correct hash for the exact same on-disk file the whole time.

    First fix hardcoded "7.2" in bash — flagged live the same day: the
    user would soon pick a 7.3 series, and a hardcoded version string
    would go stale again exactly the same way. Real fix: screen_fetch
    delegates to `hyphaed fetch-patches`, which resolves the right lock
    itself via patch.py::resolve_vendor_lock() (source_dir, then --target,
    then newest-on-disk) — no version number lives in bash to go stale."""
    src = _source()
    body = src.split("screen_fetch() {")[1].split("\n}\n")[0]
    assert "python3 -m hyphaed fetch-patches" in body, (
        "screen_fetch must delegate to `hyphaed fetch-patches`, which "
        "resolves the correct series lock dynamically — not call "
        "patches/fetch.py directly (bare, or with a hardcoded --lock path)"
    )
    # The actual command line, not prose in a comment explaining why —
    # only the real invocation matters for "does it bypass the resolver".
    exec_lines = [l for l in body.splitlines() if "patches/fetch.py" in l and "#" not in l.split("patches/fetch.py")[0]]
    assert not exec_lines, f"a direct patches/fetch.py call remains: {exec_lines}"


def test_welcome_menu_offers_a_harden_workstation_entry():
    """Explicit instruction: 'all of those new features I asked for, must
    be part of the wizard' + 'we need a new option to setUp and check
    hardened security configurations' — a distinct top-level entry, not
    folded into the checks hub."""
    src = _source()
    assert 'menu_item 6 "Harden workstation"' in src
    assert '6) MODE="harden"; return ;;' in src


def test_harden_mode_is_dispatched_before_source_mode_screen():
    src = _source()
    dispatch = src.split("main() {")[1]
    harden_idx = dispatch.index('MODE" == "harden"')
    source_mode_idx = dispatch.index("screen_source_mode")
    assert harden_idx < source_mode_idx


def test_screen_harden_calls_the_real_hyphaed_harden_subcommand():
    src = _source()
    assert "screen_harden()" in src
    body = src.split("screen_harden()")[1].split("\n}\n")[0]
    assert "hyphaed harden report" in body
    assert "hyphaed harden apply" in body
    # apply is opt-in via a menu choice, never fired unconditionally.
    assert "menu_item 1" in body


def test_screen_harden_offers_the_full_root_diagnostic_script():
    """'harden check must be part of the wizard also' — the exhaustive
    diagnostics/harden-check-*.sh script (real ufw ruleset, real port
    ownership, auth.log, rootkit scan) must be reachable from the wizard,
    not just the unprivileged `hyphaed harden report`. It needs a real
    sudo prompt, so it goes through `sudo bash <script>` directly rather
    than a spinner-wrapped call."""
    src = _source()
    body = src.split("screen_harden() {")[1].split("\n}\n")[0]
    assert "sudo bash" in body and "harden-check-" in body


def test_wizard_still_parses():
    import subprocess
    r = subprocess.run(["bash", "-n", str(WIZARD)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
