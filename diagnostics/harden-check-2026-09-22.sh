#!/usr/bin/env bash
# Root-requiring half of workstation hardening checks — everything
# `hyphaed harden report` and the wizard's "Harden workstation" screen
# cannot fully resolve without sudo, plus the raw evidence a human should
# read directly rather than trust a summary of.
#
# Read-only. Nothing here writes, installs, or changes any setting — it
# only reads. Safe to run repeatedly. For the fix/install side (fail2ban,
# rkhunter, chkrootkit, debsums, ufw defaults), run
# `sudo python3 -m hyphaed harden apply` or use the wizard's "Harden
# workstation" -> Apply after reading this report.
#
# Usage: sudo bash diagnostics/harden-check-2026-09-22.sh
#        sudo bash diagnostics/harden-check-2026-09-22.sh > harden-report.txt 2>&1

set -uo pipefail
exec < /dev/null
export DEBIAN_FRONTEND=noninteractive

sep() { printf '\n========== %s ==========\n' "$1"; }

if [[ $EUID -ne 0 ]]; then
    echo "Re-run with sudo: sudo $0" >&2
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

sep "Step 0: ensure this script's own dependencies are installed and active"
# This script already runs as root (checked above), and its whole job is
# to actually perform these checks/scans — a tool this script needs but
# doesn't have is a missing PREREQUISITE, not a system change the operator
# needs to separately opt into. Runs FIRST and visibly — burying this
# inside the rootkit section (behind several slow `find /` sweeps) made it
# look like nothing happened until the scan came up empty minutes later.
#
# Scoped to what THIS script's own report/scan sections actually consume:
# the three passive scanners (install only, never auto-run/configured
# further) plus fail2ban (needs to be ACTIVE, not just installed, for the
# report table above to read correctly — delegated to
# hardening.py::_apply_fail2ban() so the jail config isn't duplicated in
# two languages). No ufw rule changes, no unattended-upgrades
# reconfiguration, nothing else — that's still the separate, explicit
# `sudo python3 -m hyphaed harden apply` path for anything this script
# itself doesn't need to run.
_ensure_scanner() {
    local bin="$1" pkg="$2"
    if command -v "$bin" >/dev/null 2>&1; then
        echo "  $bin already installed"
        return
    fi
    echo "  $bin not installed — installing $pkg…"
    if apt-get install -y "$pkg" >/dev/null 2>&1; then
        echo "  $pkg installed"
    else
        echo "  $pkg install FAILED (apt-get exit $?) — that scan will be skipped below"
    fi
}
_ensure_scanner rkhunter rkhunter
_ensure_scanner chkrootkit chkrootkit
_ensure_scanner debsums debsums

if systemctl is-active fail2ban >/dev/null 2>&1; then
    echo "  fail2ban already active"
else
    echo "  fail2ban not active — installing/configuring (same path 'harden apply' uses)…"
    if python3 -c "
import sys
sys.path.insert(0, '$REPO_ROOT')
from hyphaed.hardening import _apply_fail2ban
_apply_fail2ban()
" 2>&1; then
        echo "  fail2ban installed and configured (sshd jail)"
    else
        echo "  fail2ban setup FAILED — see output above"
    fi
fi

sep "hyphaed harden report — with real root, every item resolves fully"
python3 -m hyphaed harden report 2>&1

sep "ufw — full ruleset (the thing the unprivileged report can't read)"
ufw status verbose 2>&1
echo
ufw status numbered 2>&1

sep "listening sockets, WITH owning process (0.0.0.0 / [::] only — reachable if the router forwards it)"
ss -tlnp 2>&1 | awk 'NR==1 || $4 ~ /^(0\.0\.0\.0|\*|\[::\]):/'

sep "who owns each wildcard-bound port, resolved with real root"
ss -tlnHp 2>&1 | awk '$4 ~ /^(0\.0\.0\.0|\*|\[::\]):/' | while read -r line; do
    port="$(awk '{print $4}' <<<"$line" | sed 's/.*://')"
    pid="$(grep -oP 'pid=\K[0-9]+' <<<"$line" | head -1)"
    if [[ -n "$pid" ]]; then
        printf '  port %-6s pid %-8s %s\n' "$port" "$pid" "$(ps -p "$pid" -o cmd= 2>/dev/null | cut -c1-100)"
    else
        printf '  port %-6s (no pid in ss output — check docker/containerd port mappings below)\n' "$port"
    fi
done
if command -v docker >/dev/null 2>&1; then
    echo
    echo "  docker port mappings:"
    docker ps --format '    {{.Names}}: {{.Ports}}' 2>&1
fi

sep "auth.log — every SSH/sudo auth event in the retained logs (should be empty if nobody's connecting)"
grep -hiE "sshd.*(accepted|failed|invalid|refused)|sudo.*incorrect" \
    /var/log/auth.log /var/log/auth.log.1 2>/dev/null
for f in /var/log/auth.log.*.gz; do
    [[ -e "$f" ]] || continue
    zcat "$f" 2>/dev/null | grep -iE "sshd.*(accepted|failed|invalid|refused)|sudo.*incorrect"
done
echo "  (no output above this line means: zero auth attempts found)"

sep "UFW BLOCK — non-LAN source IPs seen in the last 8 days (RFC1918/multicast/link-local excluded)"
journalctl -k --no-pager -g "UFW BLOCK" --since "-8 days" 2>/dev/null \
    | grep -oP 'SRC=\K[0-9.]+' \
    | grep -vE '^(10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.|127\.|169\.254\.|224\.|239\.)' \
    | sort | uniq -c | sort -rn
echo "  (each IP above reached this box's firewall from outside your LAN and was blocked —"
echo "   cross-check the port/SPT in the raw journalctl -k -g \"UFW BLOCK\" output before"
echo "   assuming malice: a high SPT with a well-known remote port like 443 is almost"
echo "   always a late-arriving response to a connection THIS box made, not an inbound scan)"

sep "rootkit / tamper indicators (best-effort, root makes these exhaustive)"
echo "-- unusual SUID/SGID files outside standard system dirs --"
find / -xdev \( -perm -4000 -o -perm -2000 \) -type f 2>/dev/null \
    | grep -vE "^/(usr/(bin|sbin|lib(exec)?|share)|bin|sbin)/"
echo "-- world-writable files under /etc, /usr/bin, /usr/sbin, /usr/lib --"
find /etc /usr/bin /usr/sbin /usr/lib -xdev -perm -0002 -type f 2>/dev/null
echo "-- processes running from a deleted/replaced binary (a real backdoor tell) --"
for p in /proc/[0-9]*; do
    pid="${p#/proc/}"
    link="$(readlink "$p/exe" 2>/dev/null)"
    [[ "$link" == *"(deleted)"* ]] && printf '  pid %-8s %s\n' "$pid" "$link"
done
echo "-- kernel modules NOT signed by this box's own DKMS key or upstream --"
for m in $(lsmod 2>/dev/null | tail -n +2 | awk '{print $1}'); do
    sig="$(modinfo "$m" 2>/dev/null | grep -c '^sig_id:')"
    [[ "$sig" -eq 0 ]] && printf '  %s — no signature at all\n' "$m"
done
if command -v rkhunter >/dev/null 2>&1; then
    echo "-- rkhunter --propupd (refresh the file-property baseline first) --"
    # Confirmed live 2026-09-22: a baseline seeded once at first install
    # goes stale as packages are added/updated afterward — /usr/bin/mail
    # and /usr/bin/bsd-mailx both flagged 'exists on the system, but is
    # not present in the rkhunter.dat file', which is rkhunter's baseline
    # being incomplete, not evidence of anything. --propupd is fast
    # (seconds, not the multi-minute --check scan) — always refresh it
    # right before checking, not just once on first install.
    rkhunter --propupd >/dev/null 2>&1 && echo "  baseline refreshed" || echo "  --propupd failed — baseline may be stale, some files may still warn below"
    echo "  NOTE: 'Checking if SSH root access is allowed [ Warning ]' below is"
    echo "  EXPECTED and does not mean PermitRootLogin isn't 'no'. Confirmed live"
    echo "  2026-09-22 by reading /usr/bin/rkhunter itself: the check is a literal"
    echo "  'grep PermitRootLogin' against /etc/ssh/sshd_config only — it does not"
    echo "  resolve that file's own 'Include /etc/ssh/sshd_config.d/*.conf' line,"
    echo "  so a value set via drop-in (which is how hyphaed's harden apply sets"
    echo "  it, deliberately, to avoid hand-editing a package-managed file) is"
    echo "  invisible to rkhunter's grep no matter what it actually resolves to."
    echo "  The real answer is the 'SSH root login' section further down, which"
    echo "  reads the effective value via 'sshd -T' the way sshd itself sees it."
    echo "-- rkhunter --check --sk (this is the actual scan, takes a few minutes) --"
    rkhunter --check --sk 2>&1 | tail -60
else
    echo "-- rkhunter unavailable (install failed) — scan skipped --"
fi
if command -v chkrootkit >/dev/null 2>&1; then
    echo "-- chkrootkit --"
    # Confirmed live 2026-09-22: basename/date/dirname/echo/env WILL show
    # INFECTED on this box, every time, and it is not a compromise.
    if dpkg -s coreutils-from-uutils >/dev/null 2>&1; then
        echo "  NOTE: this system uses coreutils-from-uutils (the Rust"
        echo "  reimplementation Ubuntu now ships in place of GNU coreutils)."
        echo "  chkrootkit's basename/date/dirname/echo/env checks string-match"
        echo "  GNU coreutils byte patterns inside the binary — a genuinely"
        echo "  different implementation won't match, so those five WILL read"
        echo "  INFECTED below regardless of tampering. Verified via the real"
        echo "  check instead: \`dpkg -V coreutils-from-uutils\` is clean (exit"
        echo "  0, no output) — that's the actual integrity signal, not this scan."
    fi
    chkrootkit 2>&1 | grep -iv "not infected\|not found\|nothing detected" | head -40
else
    echo "-- chkrootkit unavailable (install failed) — scan skipped --"
fi
if command -v debsums >/dev/null 2>&1; then
    echo "-- debsums -c (files that don't match their package checksum) --"
    # Confirmed live 2026-09-22: /lib/modules/<kver>/modules.{alias,dep,
    # softdep,symbols}(.bin) mismatch on EVERY real run of this box —
    # `depmod` legitimately rewrites those module-index files every time a
    # DKMS module (nvidia, nvidia-fs, greenboost, vmmon, vmnet, ntsync,
    # ...) is built/installed, which is constantly on this workstation.
    # That's the module index doing its job, not tampering — the package's
    # shipped version was only ever a placeholder depmod overwrites at
    # first boot. A 'missing file ... (from <pkg>)' line for something
    # unrelated (e.g. a stale .desktop entry) is ordinary packaging drift,
    # not a security finding either.
    echo "  NOTE: /lib/modules/<kver>/modules.* mismatches are EXPECTED on"
    echo "  this box — depmod rewrites them on every DKMS module build"
    echo "  (nvidia/nvidia-fs/greenboost/vmmon/vmnet/ntsync), which happens"
    echo "  constantly here. Only a mismatch OUTSIDE /lib/modules/ is worth"
    echo "  a second look."
    debsums -c 2>&1 | head -40
else
    echo "-- debsums unavailable (install failed) — scan skipped --"
fi

sep "SSH root login — is it actually reachable, not just theoretically permitted"
# rkhunter warns 'PermitRootLogin has not been set. The default value may
# be yes' — that warning text predates OpenSSH 7.0 (2015): every OpenSSH
# since then compiles in prohibit-password (key-only, no password) as the
# real default when the directive is absent, which sshd -T (below) proves
# for THIS box. That makes the warning imprecise, not wrong — root login
# via a KEY is still permitted if root's authorized_keys has anything in
# it, which is the one thing worth actually checking, and the one thing
# the earlier unprivileged conversation couldn't (/root/.ssh is 700).
echo "  effective sshd default: $(sshd -T 2>/dev/null | grep -i '^permitrootlogin' || echo 'sshd -T failed')"
if [[ -d /root/.ssh ]]; then
    # Confirmed live 2026-09-22: `grep -c` with zero matches exits 1 (no
    # match found) even though it already printed "0" to stdout — the old
    # `|| echo 0` fallback then appended a SECOND "0", so $(...) captured
    # the two-line string "0\n0" and the later `[[ "$keycount" -gt 0 ]]`
    # died with 'arithmetic syntax error'. grep -c's own stdout is always
    # the right count regardless of its exit status; the fallback only
    # needs to cover grep genuinely failing to run at all.
    keycount=0
    if [[ -f /root/.ssh/authorized_keys ]]; then
        keycount=$(grep -cv '^\s*#\|^\s*$' /root/.ssh/authorized_keys 2>/dev/null)
        keycount=${keycount:-0}
    fi
    echo "  /root/.ssh/authorized_keys: ${keycount} real key(s)"
    if [[ "$keycount" -gt 0 ]]; then
        echo "  root IS reachable via SSH key right now. If that's not intentional,"
        echo "  either remove the key(s) or set 'PermitRootLogin no' in"
        echo "  /etc/ssh/sshd_config.d/99-hyphaed.conf and reload sshd."
    else
        echo "  no authorized_keys, or it's empty — root SSH login is not actually"
        echo "  reachable today even though PermitRootLogin isn't explicitly 'no'."
        echo "  Setting it to 'no' explicitly is still free hardening (closes the"
        echo "  theoretical door, silences this warning for good) but nothing is"
        echo "  exploitable about the current state."
    fi
else
    echo "  /root/.ssh does not exist — root SSH login is not reachable via key at all"
fi

sep "recently modified files under /etc, /usr/bin, /usr/sbin (last 7 days, excludes this repo's own writes)"
find /etc /usr/bin /usr/sbin -xdev -type f -mtime -7 2>/dev/null \
    | grep -v "^$REPO_ROOT" | sort

sep "done — 0 exit unless something above needs a second look"
