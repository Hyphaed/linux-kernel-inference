"""diagnostics/harden-check-2026-09-22.sh — the root-requiring half of
workstation hardening checks (ufw ruleset, wildcard-port ownership,
auth.log, non-LAN UFW-blocked IPs, rootkit/tamper indicators). Requires
real root to run for real, so this only pins structure/syntax — the same
limit every other diagnostics/*.sh test in this repo has.
"""
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "diagnostics" / "harden-check-2026-09-22.sh"


def test_script_exists_and_is_executable():
    assert SCRIPT.exists()
    assert SCRIPT.stat().st_mode & 0o100, "script must be executable (chmod +x)"


def test_script_parses():
    r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_script_requires_root_and_refuses_to_silently_no_op():
    src = SCRIPT.read_text()
    assert "EUID -ne 0" in src
    assert 'exit 1' in src.split("EUID -ne 0")[1].split("fi")[0]


def test_script_installs_its_own_scanner_deps_and_fail2ban():
    """The script self-installs rkhunter/chkrootkit/debsums when missing —
    deliberate: it already runs as root (checked above), so a missing
    dependency is a blocked prerequisite for the check, not a system
    change to separately opt into.

    fail2ban was NOT originally in this list — confirmed live 2026-09-22
    (twice: the report kept showing it as 'not installed' after a Step 0
    run that only handled the three scanners). It genuinely IS one of this
    script's own dependencies (the report table above reads it directly),
    so it belongs in Step 0 too. Delegated to
    hardening.py::_apply_fail2ban() rather than duplicating the jail
    config in bash — one source of truth for what 'fail2ban configured'
    means, shared with `harden apply`.

    Still out of scope: anything this script's OWN sections don't consume
    (ufw rule changes, unattended-upgrades reconfiguration) — those stay
    behind the separate, explicit 'sudo python3 -m hyphaed harden apply'."""
    code_lines = "\n".join(
        line for line in SCRIPT.read_text().splitlines()
        if not line.strip().startswith("#")
    )
    assert "apt-get install -y \"$pkg\"" in code_lines
    for pkg in ("rkhunter", "chkrootkit", "debsums"):
        assert f'_ensure_scanner {pkg} {pkg}' in code_lines
    assert "_apply_fail2ban" in code_lines, (
        "fail2ban setup must be handled in Step 0 too, via the same "
        "hardening.py._apply_fail2ban() harden-apply already uses"
    )
    forbidden = ("ufw enable", "ufw default", "systemctl enable unattended",
                 "rm -f", "> /etc/")
    for token in forbidden:
        assert token not in code_lines, f"out of scope for the check script, found: {token!r}"


def test_dependency_install_runs_first_as_step_0_not_buried_later():
    """Confirmed live 2026-09-22: the install call originally sat inside
    the rootkit-scan section, after several slow `find /` sweeps — it
    looked like nothing had happened, because by the time it would have
    run the operator had already been staring at output for a while with
    no visible install step. Moved to run immediately after the root
    check, before any other section, with its own 'Step 0' header."""
    src = SCRIPT.read_text()
    step0_idx = src.index('sep "Step 0')
    first_ensure_call_idx = src.index("_ensure_scanner rkhunter rkhunter")
    root_check_idx = src.index("EUID -ne 0")
    first_other_section_idx = src.index('sep "hyphaed harden report')

    assert root_check_idx < step0_idx < first_ensure_call_idx < first_other_section_idx, (
        "dependency installation must run as the very first visible step, "
        "right after the root check — not after any report/scan section"
    )


def test_script_covers_the_items_the_unprivileged_report_cannot_resolve():
    src = SCRIPT.read_text()
    for expected in (
        "ufw status verbose",
        "auth.log",
        "UFW BLOCK",
        "SUID/SGID",
        "deleted",  # the "running from a deleted binary" backdoor tell
        "rkhunter",
        "chkrootkit",
        "debsums",
    ):
        assert expected in src, f"expected coverage of: {expected!r}"


def test_rkhunter_baseline_refreshed_every_run_not_just_first_install():
    """Confirmed live 2026-09-22: /usr/bin/mail and /usr/bin/bsd-mailx both
    warned 'exists on the system, but is not present in the rkhunter.dat
    file' — the baseline seeded once at first install had gone stale.
    --propupd must run every time, unconditionally, right before --check,
    not gated on the baseline file merely existing."""
    src = SCRIPT.read_text()
    assert "rkhunter --propupd" in src
    assert "[[ -f /var/lib/rkhunter/db/rkhunter.dat ]] ||" not in src, (
        "propupd must run unconditionally every time, not only when the "
        "baseline file is entirely absent — that's what let it go stale"
    )


def test_chkrootkit_section_explains_the_uutils_false_positive():
    """Confirmed live 2026-09-22 and verified with dpkg -S / dpkg -V:
    basename/date/dirname/echo/env show INFECTED on this box every run
    because it uses coreutils-from-uutils (a real, different
    implementation), not because of tampering. The scary raw chkrootkit
    output needs this context printed right above it, not left to alarm
    silently on every future run."""
    src = SCRIPT.read_text()
    body = src.split('echo "-- chkrootkit --"')[1].split("fi\n")[0]
    assert "coreutils-from-uutils" in body
    assert "dpkg -V coreutils-from-uutils" in body


def test_ssh_root_login_section_checks_real_reachability_not_just_the_directive():
    """rkhunter's 'PermitRootLogin has not been set, default may be yes'
    warning is imprecise (OpenSSH has defaulted to prohibit-password since
    7.0, 2015) but the real open question — does root actually have any
    authorized_keys — needs root to check (/root/.ssh is 700) and couldn't
    be answered in the unprivileged conversation. This section is
    report-only: it surfaces the real answer, it does not write
    PermitRootLogin itself — that's a human decision with a real (if
    narrow) risk of locking out an intentional root key."""
    src = SCRIPT.read_text()
    assert "sshd -T" in src
    assert "authorized_keys" in src
    body = src.split('sep "SSH root login')[1].split('sep "')[0]
    assert ">" not in body.replace("2>/dev/null", "").replace("2>&1", ""), (
        "this section must stay read-only — no file redirects writing "
        "PermitRootLogin or anything else, only echo/report output"
    )


def test_ufw_blocked_ip_filter_excludes_private_and_multicast_ranges():
    """Pins the non-LAN filter so it doesn't regress into flooding the
    operator with router/broadcast noise (192.168.50.1 fired 352 times in
    the real 8-day window this script's filter was built against)."""
    src = SCRIPT.read_text()
    grep_line = [l for l in src.splitlines() if "grep -vE" in l and "10\\." in l]
    assert grep_line, "expected a private/multicast-range exclusion filter"
    pattern = grep_line[0]
    for private_prefix in ("10\\.", "192\\.168\\.", "127\\.", "224\\."):
        assert private_prefix in pattern
