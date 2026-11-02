#!/usr/bin/env bash
# Read-only intrusion sweep: rootkits, tampered packages, persistence,
# unexpected access from outside the LAN, live connections.
#
# Nothing here writes system state. rkhunter runs without --propupd on
# purpose: that would bless whatever is on disk now as the baseline.
#
# Usage: sudo bash diagnostics/security-scan-2026-10-28.sh
# Report: out/security/scan-2026-10-28.txt (also printed)

set -uo pipefail
exec < /dev/null

sep() { printf '\n========== %s ==========\n' "$1"; }

if [[ $EUID -ne 0 ]]; then
    echo "Re-run with sudo: sudo $0" >&2
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$REPO_ROOT/out/security/scan-2026-10-28.txt"
mkdir -p "$(dirname "$OUT")"
exec > >(tee "$OUT") 2>&1

sep "rkhunter (warnings only)"
rkhunter --update --nocolors 2>&1 | tail -3
rkhunter --check --sk --rwo --nocolors 2>&1 | head -80

sep "chkrootkit (suspicious lines only)"
# uutils coreutils (Ubuntu 26.04 default) trips chkrootkit's basename/date/
# dirname/echo/env checks. debsums below is the real integrity check.
chkrootkit -q 2>&1 | head -60

sep "debsums: packaged files that changed"
debsums -c 2>&1 | head -60

sep "unhide: hidden processes / ports"
unhide sys procall 2>&1 | tail -15
unhide-tcp 2>&1 | tail -10

sep "kernel: taint, modules without a file on disk, ld.so.preload"
echo "tainted=$(cat /proc/sys/kernel/tainted)"
for m in $(lsmod | awk 'NR>1{print $1}'); do
    modinfo -n "$m" >/dev/null 2>&1 || echo "no file for module: $m"
done
ls -l /etc/ld.so.preload 2>&1

sep "SUID/SGID files not owned by any package"
find / -xdev \( -perm -4000 -o -perm -2000 \) -type f 2>/dev/null | while read -r f; do
    dpkg -S "$f" >/dev/null 2>&1 || echo "$f"
done | head -40

sep "persistence: units, cron, autostart, authorized_keys"
for f in /etc/systemd/system/*.service /etc/systemd/system/*/*.service; do
    [[ -f "$f" ]] && ! dpkg -S "$f" >/dev/null 2>&1 && echo "non-dpkg unit: $f"
done 2>/dev/null | head -40
ls -la /etc/cron.d /var/spool/cron/crontabs 2>&1 | head -30
ls -la /home/*/.config/autostart 2>&1 | head -20
for f in /root/.ssh/authorized_keys /home/*/.ssh/authorized_keys; do
    [[ -f "$f" ]] && echo "$f: $(grep -c . "$f") keys" && cut -d' ' -f1,3 "$f"
done
sshd -T 2>/dev/null | grep -iE '^(permitrootlogin|passwordauthentication|pubkeyauthentication|port) '

sep "access: logins from outside the LAN"
journalctl -u ssh --since '30 days ago' --no-pager 2>/dev/null \
    | grep -E 'Accepted|Failed' | grep -vE 'from (192\.168\.|10\.|172\.(1[6-9]|2[0-9]|3[01])\.|100\.)' | tail -30
command -v last >/dev/null && last -i -n 25 || echo "(last isn't installed; the journal check above covers ssh logins)"
fail2ban-client status sshd 2>&1 | tail -6

sep "network: connections with owners, firewall"
ss -tunp 2>/dev/null | grep -v '127.0.0' | head -50
ufw status verbose 2>&1 | head -30
nft list tables 2>&1

sep "clamav: update the virus database first"
freshclam 2>&1 | tail -4 || true
echo "(if that failed on a lock: sudo systemctl stop clamav-freshclam && sudo freshclam)"

sep "clamscan: Downloads, /tmp, /var/tmp, /dev/shm"
clamscan -ri --exclude-dir='^/sys' /home/*/Downloads /tmp /var/tmp /dev/shm 2>&1 | tail -15

sep "done"
echo "report: $OUT"
