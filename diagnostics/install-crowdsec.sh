#!/usr/bin/env bash
# CrowdSec firewall bouncer + LAN whitelist for this workstation.
#
# State found 2026-10-28: the crowdsec agent (1.4.6 ESM) is already installed
# and active, but crowdsec-firewall-bouncer is not, so nothing was ever
# blocked. The agent only detects; the bouncer is what drops packets.
#
# Idempotent. Doesn't upgrade the agent. Doesn't touch ufw or fail2ban.
#
# Usage: sudo bash diagnostics/install-crowdsec.sh
# Optional CTI key (kept out of the repo): sudo CROWDSEC_CTI_KEY=... bash diagnostics/install-crowdsec.sh

set -uo pipefail
exec < /dev/null
export DEBIAN_FRONTEND=noninteractive

sep() { printf '\n========== %s ==========\n' "$1"; }

if [[ $EUID -ne 0 ]]; then
    echo "Re-run with sudo: sudo $0" >&2
    exit 1
fi

sep "1. nftables bouncer"
apt-get install -y crowdsec-firewall-bouncer-nftables || { echo "bouncer install failed"; exit 1; }

BCFG=/etc/crowdsec/bouncers/crowdsec-firewall-bouncer.yaml
echo "mode / hooks in $BCFG:"
grep -nE '^mode:|nftables_hooks|^\s+- (input|forward)' "$BCFG" || true
# Docker publishes ports through FORWARD and bypasses ufw, so the bouncer
# must hook forward as well as input.
if ! grep -qE '^\s+- forward' "$BCFG"; then
    echo "WARNING: forward hook not set; Docker-published ports would not be covered." >&2
fi

sep "2. collections"
# 2026-10-28: the first run died with "invalid download hash ... please 'hub
# update'", because the local hub index was older than the hub. Refresh first.
cscli hub update 2>&1 | tail -3
cscli collections install crowdsecurity/linux crowdsecurity/sshd 2>&1 | tail -4

sep "3. LAN whitelist (the house must not ban itself)"
install -d /etc/crowdsec/parsers/s02-enrich
cat > /etc/crowdsec/parsers/s02-enrich/local-lan-whitelist.yaml <<'EOF'
name: local/lan-whitelist
description: "LAN, tailnet, Docker and VMware ranges are never banned"
whitelist:
  reason: "local ranges"
  cidr:
    - "192.168.50.0/24"
    - "100.64.0.0/10"
    - "172.16.0.0/12"
    - "192.168.77.0/24"
    - "10.0.0.0/8"
EOF

sep "4. CTI key (optional)"
if [[ -n "${CROWDSEC_CTI_KEY:-}" ]]; then
    ver="$(cscli version 2>&1 | grep -oE 'v?[0-9]+\.[0-9]+\.[0-9]+' | head -1 | tr -d v)"
    major_minor="${ver%.*}"
    if [[ "$(printf '%s\n1.5\n' "$major_minor" | sort -V | head -1)" == "1.5" ]]; then
        umask 077
        printf 'api:\n  cti:\n    enabled: true\n    key: %s\n' "$CROWDSEC_CTI_KEY" >> /etc/crowdsec/config.yaml.local
        chmod 600 /etc/crowdsec/config.yaml.local
        echo "CTI key written to /etc/crowdsec/config.yaml.local (0600)"
    else
        echo "agent is $ver; api.cti needs >= 1.5. Key NOT written. The blocklist works without it."
    fi
else
    echo "no CROWDSEC_CTI_KEY set; skipped (the community blocklist doesn't need it)"
fi

sep "5. restart + verify"
systemctl enable --now crowdsec-firewall-bouncer
systemctl restart crowdsec
sleep 3
systemctl is-active crowdsec crowdsec-firewall-bouncer
echo; cscli capi status 2>&1 | tail -3
echo; echo "community decisions loaded:"; cscli decisions list -a -o raw 2>/dev/null | wc -l
echo; nft list tables | grep -i crowdsec || echo "no crowdsec nft table yet (wait ~10s and re-run: nft list tables)"
echo
echo "Test a block, then remove it:"
echo "  sudo cscli decisions add -i 203.0.113.7 -d 2m && sleep 12 && sudo nft list ruleset | grep 203.0.113.7"
echo "  sudo cscli decisions delete -i 203.0.113.7"
