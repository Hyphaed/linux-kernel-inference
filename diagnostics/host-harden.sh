#!/bin/bash
# Run: ! sudo bash <this file>      (review first; nothing here touches -generic kernel or GRUB)
set -u
cd ~ferran/Dev/kernel_inference
echo "== 1. dnscrypt blocklist (olive3451 etc.)"; bash "$(dirname "$0")/dns-block.sh"
echo "== 2. postfix: loopback only (port 25 was on all interfaces)"
postconf -e 'inet_interfaces = loopback-only' && systemctl restart postfix
echo "== 2b. cockpit (web admin on :9090) off; re-enable with: systemctl enable --now cockpit.socket"
systemctl disable --now cockpit.socket 2>&1 | tail -1
echo "== 2c. docker-published DB ports: block from the LAN NIC only (localhost, containers and vmnet8 unaffected)"
LAN=$(ip -o route show default | awk '{print $5; exit}')
cat > /etc/systemd/system/docker-lan-block.service <<UNIT
[Unit]
Description=Block LAN access to docker-published DB ports (5433, 14331)
After=docker.service
Requires=docker.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'for t in iptables ip6tables; do \$t -C DOCKER-USER -i $LAN -p tcp -m multiport --dports 5433,14331 -j DROP 2>/dev/null || \$t -I DOCKER-USER -i $LAN -p tcp -m multiport --dports 5433,14331 -j DROP; done'
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload && systemctl enable --now docker-lan-block.service 2>&1 | tail -1
iptables -S DOCKER-USER | grep -- '-j DROP'
echo "== 3. firewall ruleset as it stands"; ufw status verbose
echo "== 4. repo hardening (ufw rate-limit SSH, PermitRootLogin, etc.)"
echo "run as yourself, not root:  python -m hyphaed harden apply"
echo "== 5. free sysctl wins from kernel hardening scan"
echo "run as yourself, not root:  python -m hyphaed security-scan apply"
echo "== 6. rootkit / package integrity sweeps"
rkhunter --propupd >/dev/null 2>&1; rkhunter --check --sk --rwo; debsums -s 2>/dev/null | head -20
echo "== after: listeners (with owners)"; ss -tlnpH | awk '$4 ~ /^(0\.0\.0\.0|\*|\[::\]):/ {print $4, $6}' | sort -u
