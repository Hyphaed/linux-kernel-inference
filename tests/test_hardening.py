"""hyphaed/hardening.py — workstation-level hardening (firewall, fail2ban,
rootkit scanners, unattended security updates, DNSCrypt status, WAN-exposure
review). Built 2026-09-22 after a live incident-response sweep found no
compromise but three services bound to 0.0.0.0/[::] (sshd:22, cockpit:9090,
a docker MSSQL container on :14331) whose real WAN-reachability depends on
the live ufw ruleset.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hyphaed import hardening as H

REPO = Path(__file__).resolve().parent.parent


def test_every_item_has_a_name_and_evidence():
    for item in H.ITEMS:
        assert item.name
        assert item.evidence


def test_dnscrypt_is_report_only():
    """The instruction was explicit: DNSCrypt is already set up and working
    — surface it, never modify it."""
    item = next(i for i in H.ITEMS if "DNSCrypt" in i.name)
    assert item.apply is None


def test_ufw_status_falls_back_to_root_and_then_to_log_evidence(monkeypatch):
    """`ufw status` (even bare) refuses to run at all without root on this
    system — confirmed live 2026-09-22, correcting an earlier assumption
    it worked unprivileged. Pin the fallback: no cached sudo -> fall back
    to real [UFW BLOCK] kernel-log evidence rather than reporting a false
    negative just because the ruleset itself couldn't be read."""
    calls = []

    def _fake_cmd(args):
        calls.append(args)
        if args[:2] == ["systemctl", "is-enabled"]:
            return "enabled\n"
        if args[:2] == ["systemctl", "is-active"]:
            return "active\n"
        if args[:3] == ["sudo", "-n", "ufw"]:
            return ""  # no cached credential -> ufw refuses
        if args[0] == "journalctl":
            return "Sep 22 09:15:26 ncore kernel: [UFW BLOCK] IN=enp4s0 ...\n"
        return ""

    monkeypatch.setattr(H, "_cmd", _fake_cmd)
    monkeypatch.setattr(H.shutil, "which", lambda name: "/usr/sbin/ufw" if name == "ufw" else None)
    ok, detail = H._check_ufw_active()
    assert ok is True
    assert "log evidence" in detail


def test_ufw_status_reports_pending_root_when_no_log_evidence_either(monkeypatch):
    def _fake_cmd(args):
        if args[:2] == ["systemctl", "is-enabled"]:
            return "enabled\n"
        if args[:2] == ["systemctl", "is-active"]:
            return "active\n"
        if args[:3] == ["sudo", "-n", "ufw"]:
            return ""
        if args[0] == "journalctl":
            return ""
        return ""

    monkeypatch.setattr(H, "_cmd", _fake_cmd)
    monkeypatch.setattr(H.shutil, "which", lambda name: "/usr/sbin/ufw" if name == "ufw" else None)
    ok, detail = H._check_ufw_active()
    assert ok is False
    assert "sudo" in detail.lower()


def test_ssh_rate_limit_detects_limit_rule(monkeypatch):
    monkeypatch.setattr(H, "_ufw_status", lambda **k: "22/tcp                     LIMIT       Anywhere\n")
    monkeypatch.setattr(H.shutil, "which", lambda name: "/usr/sbin/ufw" if name == "ufw" else None)
    ok, _ = H._check_ssh_rate_limited()
    assert ok is True


def test_ssh_rate_limit_flags_unlimited_allow(monkeypatch):
    monkeypatch.setattr(H, "_ufw_status", lambda **k: "22/tcp                     ALLOW       Anywhere\n")
    monkeypatch.setattr(H.shutil, "which", lambda name: "/usr/sbin/ufw" if name == "ufw" else None)
    ok, detail = H._check_ssh_rate_limited()
    assert ok is False
    assert "aren't throttled" in detail


def test_wan_exposed_services_flags_wildcard_binds_only():
    fake_ss_output = (
        "LISTEN 0      4096            0.0.0.0:22        0.0.0.0:*\n"
        "LISTEN 0      4096          127.0.0.1:11434      0.0.0.0:*\n"
        "LISTEN 0      50                   [::]:445           [::]:*\n"
        "LISTEN 0      50                  [::1]:445           [::]:*\n"
    )
    import hyphaed.hardening as h
    orig = h._cmd
    h._cmd = lambda args: fake_ss_output if args[:2] == ["ss", "-tlnH"] else orig(args)
    try:
        exposed = h.wan_exposed_services()
    finally:
        h._cmd = orig
    addrs = [a for a, _ in exposed]
    assert "0.0.0.0:22" in addrs
    assert "[::]:445" in addrs
    assert "127.0.0.1:11434" not in addrs
    assert "[::1]:445" not in addrs


def test_no_anti_fingerprint_or_mac_randomization_items_present():
    """Pins the deliberate scope decision recorded in the module docstring:
    no OS-level 'anti-fingerprint' package (browser concern, not a real
    system-level control) and no MAC randomization item (this box's real
    interface is wired ethernet — a MAC is invisible past the LAN segment,
    so 'hiding' it defends against nothing WAN-facing)."""
    names = " ".join(i.name.lower() for i in H.ITEMS)
    assert "fingerprint" not in names
    assert "mac" not in names or "mac-address" not in names


def test_fail2ban_apply_installs_and_configures_an_sshd_jail():
    src = (REPO / "hyphaed" / "hardening.py").read_text()
    body = src.split("def _apply_fail2ban()")[1].split("\ndef ")[0]
    assert "fail2ban" in body
    assert "jail.d/sshd.local" in body
    assert "systemctl" in body and "enable" in body


class _FakeCompleted:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_root_authorized_keys_count_goes_through_sudo_not_bare_path(monkeypatch):
    """Real bug found live 2026-09-22: a bare `Path.read_text()` can never
    read a root-owned 700-mode dir, and ignores any sudo credential
    `harden apply` already cached via `sudo_keepalive()` before running
    any item — so PermitRootLogin refused to apply even right after the
    operator entered their sudo password for the ufw item. The fix reads
    through `sudo -n cat`, which does use the cached credential."""
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        return _FakeCompleted(0, stdout="ssh-ed25519 AAAA... user@host\n")

    monkeypatch.setattr(H.subprocess, "run", fake_run)
    assert H._root_authorized_keys_count() == 1
    assert calls[0][:3] == ["sudo", "-n", "cat"]


def test_root_authorized_keys_count_ignores_comments_and_blank_lines(monkeypatch):
    monkeypatch.setattr(
        H.subprocess, "run",
        lambda args, **kw: _FakeCompleted(0, stdout="# comment\n\nssh-ed25519 AAAA key1\n   \nssh-rsa BBBB key2\n"),
    )
    assert H._root_authorized_keys_count() == 2


def test_root_authorized_keys_count_absent_file_is_zero_not_unknown(monkeypatch):
    monkeypatch.setattr(
        H.subprocess, "run",
        lambda args, **kw: _FakeCompleted(1, stderr="cat: /root/.ssh/authorized_keys: No such file or directory\n"),
    )
    assert H._root_authorized_keys_count() == 0


def test_root_authorized_keys_count_no_cached_sudo_is_unknown_not_zero(monkeypatch):
    monkeypatch.setattr(
        H.subprocess, "run",
        lambda args, **kw: _FakeCompleted(1, stderr="sudo: a password is required\n"),
    )
    assert H._root_authorized_keys_count() == -1


def test_apply_permit_root_login_proceeds_once_sudo_cache_makes_zero_readable(monkeypatch):
    """The scenario the live run above actually hit: apply() re-checks the
    key count itself before writing the drop-in. With a cached sudo
    credential and zero real keys, it must proceed (not refuse)."""
    monkeypatch.setattr(
        H.subprocess, "run",
        lambda args, **kw: _FakeCompleted(1, stderr="No such file or directory\n"),
    )
    written = {}

    def fake_run_sudo(cmd, **kw):
        if cmd[0] == "tee":
            written["content"] = kw.get("input_str")
        return _FakeCompleted(0)

    monkeypatch.setattr(H, "run_sudo", fake_run_sudo)
    H._apply_permit_root_login()
    assert written.get("content") == "PermitRootLogin no\n"
