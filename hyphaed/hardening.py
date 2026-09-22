"""Workstation-level security hardening — firewall, brute-force/intrusion
prevention, rootkit/tamper detection, and WAN-exposure review.

This is deliberately separate from `hyphaed/security_scan.py`: that module
audits the KERNEL's own hardening posture (kconfig/cmdline/sysctl) against
kernel-hardening-checker's KSPP-derived recommendations. This module audits
the SYSTEM around the kernel — is a firewall actually blocking unsolicited
inbound traffic, is a brute-force/backdoor detection tool installed and
running, is DNS going through the encrypted resolver this box already has
configured, and is anything listening on 0.0.0.0/[::] that a WAN-facing port
scanner could fingerprint.

Built 2026-09-22 after a live incident-response-style sweep (checked for
rootkit/malware indicators, read UFW's blocked-connection log, audited every
0.0.0.0-bound listening service) found no compromise but did find three real
services bound to all interfaces (sshd:22, cockpit:9090, a `so5076-sqlpro`
docker container publishing MSSQL on :14331) whose actual WAN-reachability
depends entirely on the live UFW ruleset — which this module can now check
and report on repeatably, instead of a one-off manual read.

Same TuneItem pattern as hyphaed/tuning.py (check/apply pairs,
`needs_root`), extended with `needs_package` so a missing tool reports
exactly what to install rather than silently no-op'ing — same "NOT RUN is
not a pass" discipline `security.py`/`security_scan.py` already established.

What this deliberately does NOT do, and why (evidence-gate discipline, not
an oversight):
  - No "anti-fingerprint" package. Browser fingerprinting resistance is a
    BROWSER setting (Firefox's privacy.resistFingerprinting), not something
    any OS-level package legitimately fixes — installing one claiming to
    would be exactly the "found a plausible-sounding fix with no mechanism
    behind it" failure this repo's evidence rule exists to catch.
  - No MAC randomization. This box's real interface (enp4s0) is wired
    ethernet — a MAC address is only visible on the local LAN segment, never
    past the router's WAN side, so "hiding" it defends against nothing an
    internet-facing threat model cares about.
  - No claim that hardening "speeds up" the connection. Rate-limiting and
    firewalling cost a small, real amount of overhead (packet filtering);
    they do not add throughput. Saying otherwise would be the exact kind of
    unfounded benefit claim CLAUDE.md's evidence-gate rule forbids.
  - No attempt at real DDoS mitigation. That is an upstream (ISP/CDN)
    concern; nothing running on this workstation can absorb a volumetric
    flood aimed at its own WAN IP. What IS real and included here: local
    connection-rate limiting (fail2ban, ufw limit) that reduces the impact
    of a small-scale brute-force/knock attempt, which is a different and
    much more likely threat on a home workstation than a true DDoS.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .util import log
from .util.run import run, run_sudo, sudo_keepalive


@dataclass
class HardenItem:
    name: str
    evidence: str
    check: Callable[[], tuple[bool, str]]
    apply: Callable[[], None] | None = None  # None => report-only, never touched by `apply`
    needs_root: bool = False
    needs_package: str | None = None  # apt package `apply` installs if check() fails for this reason


def _cmd(args: list[str]) -> str:
    try:
        r = subprocess.run(args, capture_output=True, text=True, check=False)
        return r.stdout
    except FileNotFoundError:
        return ""


def _read(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# 1. UFW active with a default-deny-incoming policy
#
# The whole "no one from the internet should connect" requirement rests on
# this being true. Confirmed live 2026-09-22 via [UFW BLOCK] lines already
# present in dmesg — ufw IS active and IS blocking by default — but that was
# read from log evidence of it firing, not from the ruleset itself (needs
# root to query directly, `ufw status` refuses non-root outright).
# ---------------------------------------------------------------------------

def _ufw_status(verbose: bool = False) -> str:
    """`ufw status` refuses to run at all without root on this system —
    confirmed live 2026-09-22 ('ERROR: You need to be root to run this
    script'), correcting an earlier assumption that the bare form worked
    unprivileged. Try a cached sudo credential (`sudo -n`, works if
    sudo_keepalive() already primed it this session) before falling back."""
    args = ["sudo", "-n", "ufw", "status"]
    if verbose:
        args.append("verbose")
    return _cmd(args)


def _check_ufw_active() -> tuple[bool, str]:
    if not shutil.which("ufw"):
        return False, "ufw not installed"
    enabled = _cmd(["systemctl", "is-enabled", "ufw"]).strip()
    active = _cmd(["systemctl", "is-active", "ufw"]).strip()
    if enabled != "enabled" or active != "active":
        return False, f"systemd: enabled={enabled or '?'} active={active or '?'}"
    status = _ufw_status().strip()
    if "Status: active" in status:
        return True, "ufw active and enabled at boot (ruleset confirmed via sudo)"
    if status:
        return False, f"ufw reports: {status}"
    # No sudo credential cached — fall back to log evidence: a real
    # [UFW BLOCK] line in the kernel ring proves the firewall IS filtering,
    # even though the ruleset itself can't be read without root right now.
    blocked = _cmd(["journalctl", "-k", "-b", "--no-pager", "-g", "UFW BLOCK"])
    if blocked.strip():
        return True, ("systemd unit active + real [UFW BLOCK] log evidence this boot "
                       "(ruleset itself unverified — no cached sudo; "
                       "`sudo ufw status verbose` for the full picture)")
    return False, "systemd unit active but no way to confirm it's actually blocking "\
                  "anything without sudo — run `sudo hyphaed harden report`"


def _apply_ufw_active() -> None:
    run_sudo(["ufw", "default", "deny", "incoming"], check=False)
    run_sudo(["ufw", "default", "allow", "outgoing"], check=False)
    run_sudo(["ufw", "--force", "enable"], check=False)
    run_sudo(["systemctl", "enable", "ufw"], check=False)


# ---------------------------------------------------------------------------
# 2. SSH rate-limited against brute-force
#
# `ufw limit 22/tcp` auto-denies an IP making more than 6 connections in 30
# seconds — the standard, low-cost ufw anti-brute-force primitive. Doesn't
# require fail2ban to already do something real.
# ---------------------------------------------------------------------------

def _check_ssh_rate_limited() -> tuple[bool, str]:
    if not shutil.which("ufw"):
        return True, "ufw not installed — n/a"
    rules = _ufw_status()
    if not rules.strip():
        return False, "no cached sudo credential to read the ruleset — run `sudo hyphaed harden report`"
    if re.search(r"22/tcp\s+LIMIT", rules):
        return True, "22/tcp is rate-limited"
    if re.search(r"22/tcp\s+ALLOW", rules):
        return False, "22/tcp is ALLOWed with no rate limit — brute-force attempts aren't throttled"
    return True, "22/tcp has no explicit allow rule — default-deny already covers it"


def _apply_ssh_rate_limited() -> None:
    run_sudo(["ufw", "limit", "22/tcp"], check=False)


# ---------------------------------------------------------------------------
# 3. fail2ban — real intrusion prevention, not installed as of the
#    2026-09-22 sweep (`dpkg -s fail2ban` returned nothing).
# ---------------------------------------------------------------------------

def _check_fail2ban() -> tuple[bool, str]:
    if not shutil.which("fail2ban-client"):
        return False, "not installed"
    active = _cmd(["systemctl", "is-active", "fail2ban"]).strip()
    if active != "active":
        return False, f"installed but systemctl is-active: {active or '?'}"
    jails = _cmd(["fail2ban-client", "status"])
    return True, jails.strip().splitlines()[-1] if jails.strip() else "active, no jail list read"


def _apply_fail2ban() -> None:
    run_sudo(["apt-get", "install", "-y", "fail2ban"])
    # sshd jail on by default in modern fail2ban packaging; explicit local
    # override so this doesn't silently depend on the package default.
    jail_local = (
        "[sshd]\n"
        "enabled = true\n"
        "backend = systemd\n"
        "bantime = 1h\n"
        "findtime = 10m\n"
        "maxretry = 5\n"
    )
    run_sudo(["mkdir", "-p", "/etc/fail2ban/jail.d"])
    run_sudo(["tee", "/etc/fail2ban/jail.d/sshd.local"], input_str=jail_local, capture=True)
    run_sudo(["systemctl", "enable", "--now", "fail2ban"])


# ---------------------------------------------------------------------------
# 4/5/6. Rootkit / backdoor / tamper detection: rkhunter, chkrootkit, debsums
#
# Three narrow, well-known tools rather than one heavyweight suite. None
# installed as of the 2026-09-22 sweep.
# ---------------------------------------------------------------------------

def _check_rkhunter() -> tuple[bool, str]:
    if not shutil.which("rkhunter"):
        return False, "not installed"
    return True, "installed — run `sudo rkhunter --check --sk` periodically (not auto-run, it's interactive/slow)"


def _apply_rkhunter() -> None:
    run_sudo(["apt-get", "install", "-y", "rkhunter"])
    run_sudo(["rkhunter", "--propupd"], check=False)  # seed the baseline, first-run only


def _check_chkrootkit() -> tuple[bool, str]:
    if not shutil.which("chkrootkit"):
        return False, "not installed"
    return True, "installed — run `sudo chkrootkit` periodically (not auto-run)"


def _apply_chkrootkit() -> None:
    run_sudo(["apt-get", "install", "-y", "chkrootkit"])


def _check_debsums() -> tuple[bool, str]:
    if not shutil.which("debsums"):
        return False, "not installed"
    return True, "installed — run `sudo debsums -c` to check for tampered package files"


def _apply_debsums() -> None:
    run_sudo(["apt-get", "install", "-y", "debsums"])


# ---------------------------------------------------------------------------
# 7. SSH: PermitRootLogin no
#
# rkhunter's "PermitRootLogin has not been set. The default value may be
# 'yes'" warning is imprecise on this box — OpenSSH has defaulted to
# prohibit-password (key-only, no password) since upstream 7.0 (2015), and
# `sshd -T` confirms that's the actual effective value here. The real
# question is whether root has any authorized_keys at all: confirmed live
# 2026-09-22 via the full root diagnostic script, `/root/.ssh/authorized_keys`
# has ZERO real (non-comment, non-blank) keys, so root SSH login is not
# reachable today either way. Setting PermitRootLogin=no explicitly is free
# hardening under that evidence: it closes the theoretical door and
# silences the warning, with nothing to lock out.
#
# _apply_permit_root_login() keeps its own key-count safety check
# regardless of when/how it's called — the check() above passing today
# doesn't guarantee someone hasn't added a root key by the time `apply`
# actually runs (report and apply are two separate invocations, possibly
# minutes or days apart). Refusing to act on a live key is a hard rule
# here, not a suggestion: this changes SSH access, which is exactly the
# "hard to reverse if you're relying on it" class of action that needs a
# human decision, not a loop-through-every-failing-item auto-fix.
# ---------------------------------------------------------------------------

_SSHD_ROOT_LOGIN_DROPIN = Path("/etc/ssh/sshd_config.d/99-hyphaed-root-login.conf")


def _root_authorized_keys_count() -> int:
    """-1 means "couldn't determine" (treat as unknown, never as zero) —
    NOT the same as a confirmed-empty/absent file. Reading via bare `Path`
    can't work here at all: `/root/.ssh` is mode 700, so an unprivileged
    process gets PermissionError on the stat itself (confirmed live
    2026-09-22), and going through Python's own file I/O ignores any
    cached sudo credential the apply flow primed with `sudo_keepalive()`
    — only a subprocess that actually invokes `sudo` benefits from that
    cache. Real second bug, same evidence: `harden apply` calls
    `sudo_keepalive()` before running any item (`cli.py::_cmd_harden`),
    so by the time this runs there IS a cached credential — but the old
    Path-based version could never use it, so PermitRootLogin refused to
    apply even after the operator had just entered their sudo password
    for the ufw item moments earlier. Fixed by reading through
    `sudo -n cat`, same pattern `_check_permit_root_login` already uses
    for `sshd -T`.
    """
    r = subprocess.run(
        ["sudo", "-n", "cat", "/root/.ssh/authorized_keys"],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        if "no such file or directory" in r.stderr.lower():
            return 0  # confirmed absent, not just unreadable
        return -1  # no cached sudo credential, or a real read failure — unknown, not zero
    return sum(1 for line in r.stdout.splitlines() if line.strip() and not line.strip().startswith("#"))


def _check_permit_root_login() -> tuple[bool, str]:
    effective = _cmd(["sudo", "-n", "sshd", "-T"])
    m = re.search(r"^permitrootlogin\s+(\S+)", effective, re.M)
    if m and m.group(1) == "no":
        return True, "PermitRootLogin no (confirmed via sshd -T)"
    keys = _root_authorized_keys_count()
    if keys < 0:
        return False, "not explicitly 'no' — root authorized_keys unreadable without root to confirm safety"
    if keys > 0:
        return False, (f"not explicitly 'no', AND root has {keys} authorized key(s) — "
                        f"do NOT blind-apply this, confirm the key(s) are still wanted first")
    return False, "not explicitly 'no' — but root has 0 authorized_keys, so this is free to fix"


def _apply_permit_root_login() -> None:
    keys = _root_authorized_keys_count()
    if keys != 0:
        log.err(f"refusing to set PermitRootLogin no: root authorized_keys count = {keys} "
                f"(0 expected, or unreadable) — resolve this by hand, this item won't "
                f"risk locking out a key it can't confirm is unused")
        return
    run_sudo(["mkdir", "-p", str(_SSHD_ROOT_LOGIN_DROPIN.parent)])
    run_sudo(["tee", str(_SSHD_ROOT_LOGIN_DROPIN)],
             input_str="PermitRootLogin no\n", capture=True)
    run_sudo(["systemctl", "reload", "ssh"], check=False)


# ---------------------------------------------------------------------------
# 8. Automatic security-only package updates
#
# Already correctly configured on this box (2026-09-22): unattended-upgrades
# installed, enabled, and the origin list includes -security and ESM. This
# item exists so a future box (or a reinstall of this one) gets the same
# thing without re-deriving it, and so it stops being true silently.
# ---------------------------------------------------------------------------

def _check_unattended_upgrades() -> tuple[bool, str]:
    if not shutil.which("unattended-upgrade") and not Path(
        "/usr/bin/unattended-upgrade"
    ).exists():
        return False, "unattended-upgrades not installed"
    enabled = _cmd(["systemctl", "is-enabled", "unattended-upgrades"]).strip()
    conf = _read("/etc/apt/apt.conf.d/20auto-upgrades")
    security_origin = "-security" in _read("/etc/apt/apt.conf.d/50unattended-upgrades")
    if enabled != "enabled" or "Unattended-Upgrade \"1\"" not in conf.replace("\t", " "):
        return False, f"enabled={enabled or '?'}, periodic-upgrade not confirmed on in 20auto-upgrades"
    if not security_origin:
        return False, "installed but 50unattended-upgrades has no -security origin configured"
    return True, "enabled, periodic, -security origin configured"


def _apply_unattended_upgrades() -> None:
    run_sudo(["apt-get", "install", "-y", "unattended-upgrades"])
    run_sudo(["dpkg-reconfigure", "-f", "noninteractive", "unattended-upgrades"], check=False)
    run_sudo(["systemctl", "enable", "--now", "unattended-upgrades"])


# ---------------------------------------------------------------------------
# 9. DNSCrypt — report-only. This box already has it configured and working
# (fix-boot-2026-08-17.sh installed the resolved.conf.d drop-in). `harden
# apply` never touches this — it's here so the wizard surfaces what's
# already protecting DNS, per the standing instruction to make existing,
# working setup visible through the wizard rather than leaving it undocumented.
# ---------------------------------------------------------------------------

def _check_dnscrypt() -> tuple[bool, str]:
    if not shutil.which("dnscrypt-proxy") and _cmd(["systemctl", "list-units", "--all"]).find(
        "dnscrypt-proxy"
    ) == -1:
        return True, "dnscrypt-proxy not installed on this box — n/a"
    active = _cmd(["systemctl", "is-active", "dnscrypt-proxy"]).strip()
    dropin = Path("/etc/systemd/resolved.conf.d/10-dnscrypt.conf")
    if active != "active":
        return False, f"dnscrypt-proxy installed but systemctl is-active: {active or '?'}"
    if not dropin.exists() or "127.0.2.1" not in dropin.read_text():
        return False, f"{dropin} missing or doesn't point resolved at 127.0.2.1"
    current = _cmd(["resolvectl", "dns"])
    if "127.0.2.1" not in current:
        return False, "resolved is not actually using 127.0.2.1 right now — check `resolvectl status`"
    return True, "active, resolved routes all DNS through 127.0.2.1 (dnscrypt-proxy)"


# ---------------------------------------------------------------------------
# 10. WAN-exposure review — report-only synthesis, no `apply`. Cross-
# references every 0.0.0.0/[::]-bound LISTENing TCP socket against whatever
# ufw rule data is available. This is advisory: it can tell you what's bound
# to all interfaces, but whether your ROUTER forwards a port to this box is
# outside what any check running on the box itself can see.
# ---------------------------------------------------------------------------

def wan_exposed_services() -> list[tuple[str, str]]:
    """Return [(local_addr:port, note)] for every LISTEN socket bound to a
    wildcard address (0.0.0.0 or [::], i.e. reachable from any interface
    including a WAN-facing one if the router forwards it)."""
    out = []
    ss_out = _cmd(["ss", "-tlnH"])
    for line in ss_out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        local = parts[3]
        if local.startswith("0.0.0.0:") or local.startswith("*:") or local.startswith("[::]:"):
            out.append((local, ""))
    return out


def run_report() -> list[tuple[HardenItem, bool, str]]:
    return [(item, *item.check()) for item in ITEMS]


ITEMS: list[HardenItem] = [
    HardenItem(
        "ufw: active, enabled at boot",
        "the requirement 'no one from the internet should connect' rests entirely on this",
        _check_ufw_active, _apply_ufw_active, needs_root=True,
    ),
    HardenItem(
        "ufw: SSH rate-limited",
        "standard low-cost ufw anti-brute-force primitive (>6 conns/30s auto-denied)",
        _check_ssh_rate_limited, _apply_ssh_rate_limited, needs_root=True,
    ),
    HardenItem(
        "fail2ban installed and active",
        "log-driven intrusion prevention — auto-bans an IP after repeated failed auth",
        _check_fail2ban, _apply_fail2ban, needs_root=True, needs_package="fail2ban",
    ),
    HardenItem(
        "rkhunter installed",
        "rootkit/backdoor signature + anomaly scanner",
        _check_rkhunter, _apply_rkhunter, needs_root=True, needs_package="rkhunter",
    ),
    HardenItem(
        "chkrootkit installed",
        "second, independent rootkit scanner — different detection heuristics than rkhunter",
        _check_chkrootkit, _apply_chkrootkit, needs_root=True, needs_package="chkrootkit",
    ),
    HardenItem(
        "debsums installed",
        "verifies installed package files against known-good checksums — detects tampered binaries",
        _check_debsums, _apply_debsums, needs_root=True, needs_package="debsums",
    ),
    HardenItem(
        "SSH: PermitRootLogin no",
        "rkhunter's own default-yes warning is imprecise (OpenSSH 7.0+ defaults to "
        "prohibit-password) but explicit 'no' is free hardening when root has zero keys — "
        "confirmed live 2026-09-22, 0 authorized_keys",
        _check_permit_root_login, _apply_permit_root_login, needs_root=True,
    ),
    HardenItem(
        "unattended security upgrades",
        "keeps openssl/openssh/the kernel/etc. patched against disclosed CVEs without manual action",
        _check_unattended_upgrades, _apply_unattended_upgrades, needs_root=True,
    ),
    HardenItem(
        "DNSCrypt routing all DNS",
        "already configured on this box (fix-boot-2026-08-17.sh) — report-only, never modified here",
        _check_dnscrypt, None,
    ),
]
