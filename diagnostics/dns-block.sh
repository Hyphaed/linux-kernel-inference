#!/bin/bash
# Run: ! sudo bash <this file>
set -e
T=/etc/dnscrypt-proxy/dnscrypt-proxy.toml
cp -a $T $T.bak-pre-blocklist
printf '%s\n' olive3451.com gcrenwp.top githubusercontent.co steamcommunity.co calude.ai > /etc/dnscrypt-proxy/blocked-names.txt
grep -q '^\[blocked_names\]' $T || cat >> $T <<'T2'

[blocked_names]
blocked_names_file = '/etc/dnscrypt-proxy/blocked-names.txt'
log_file = '/var/log/dnscrypt-proxy/blocked.log'
T2
systemctl restart dnscrypt-proxy; sleep 2
dig +short olive3451.com @127.0.2.1 || true
