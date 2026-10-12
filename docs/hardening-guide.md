# Hardening guide: router, Linux host, Windows 11 guest

Written 2026-10-08 after the router flagged 864 threat events, 834 of them
from this workstation. This is the record of what was found, what was changed,
what was already in place, and what is still waiting on a root shell. It is
written so the same steps can be repeated on another machine.

## What triggered it

The router's malicious-site blocker logged lookups from `ncore` since August.
Tracing them showed three different causes.

* **`olive3451.com`** was queried every 80 seconds, with an A and an HTTPS
  record each time, starting 09:20 on 2026-10-08. The gaps in that pattern
  (09:47-10:23, 10:50-11:33, 13:25-14:38) line up with the Windows 11 VMware
  guest being shut down, and the restarts line up with it resuming. The guest
  uses NAT, so the router sees the host's address. This is the real finding and
  it is not yet resolved: nobody has looked inside the guest.
* **`ethereum-rpc.publicnode.com`, `eth.llamarpc.com`** are the fallback RPC
  endpoints in the invest-desk wallet code. The router files public RPCs under
  "Malware Accomplice". False positive.
* **`raw.githubusercontent.co`, `steamcommunity.co`, `calude.ai`** are
  typo-squats. Nothing on the host references them, so the likeliest cause is a
  mistyped URL. Blocked, nothing reached them.

Method, if you need to repeat it: read the DNS query log
(`/var/log/dnscrypt-proxy/query.log`), look at the timing of one noisy domain
against what else was running, and check for a pattern that stops when a
suspect process stops.

## Layer 1: the router (ASUS RT-BE88U)

Applied through the admin pages on 2026-10-08, read back afterwards.

| Setting | Before | Now |
|---|---|---|
| DNS Privacy Protocol | none | DNS-over-TLS, Strict profile |
| DoT servers | none | Quad9 `9.9.9.9` and `149.112.112.112`, port 853, host `dns.quad9.net` |
| DNSSEC | off | on |
| DoS protection (`fw_dos_x`) | off | on |
| Admin interface (`http_enable`) | HTTP and HTTPS | HTTPS only |

The preset dropdown for DoT did not fill in the server table, so the two
Quad9 rows were entered by hand. The session expires when the firewall
restarts; log in again before the next change.

Already correct, left alone: no WAN admin, SSH, telnet, UPnP, WPS, DMZ, port
forwards, DDNS or VPN server. Wi-Fi is WPA2/WPA3 with protected management
frames, the guest network cannot reach the LAN, AiProtection (site blocking,
two-way IPS, infected-device prevention) is on, and the firmware is current.

Left for a decision: Samba is enabled with no disk attached, and WPA3-only
would drop older devices. Change the admin password after anything
containing it has been pasted into a chat.

What the router DNS does and doesn't cover: it protects devices that use the
router's resolver. `ncore` uses its own encrypted resolver, so router DNS
changes do nothing for it.

## Layer 2: the Linux host (Ubuntu 26.04, kernel 7.2.9-hyphaed)

### Already in place

* **All CPU mitigations on**, at Kconfig and on the command line. The kernel
  wizard refuses to compose `mitigations=off`.
* **Kernel hardening checker** runs after every configure and writes
  `out/security/hardening-<ver>.json`. The latest scan: 112 kconfig fails,
  36 cmdline fails (15 waived), 17 sysctl fails (10 waived). Many are
  deliberate tradeoffs listed in `configs/security-waivers.yaml`.
* **AppArmor unprivileged user-namespace restriction** (patch 0024),
  matching what the stock kernel does.
* **Copy Fail** (CVE-2026-31431) fixed in tree, and `CRYPTO_USER_API_AEAD=n`
  as defence in depth.
* **ufw** active at boot, **fail2ban** running, **rkhunter**, **chkrootkit**
  and **debsums** installed, **unattended security upgrades** on.
* **DNS**: `systemd-resolved` sends everything to `dnscrypt-proxy` on
  `127.0.2.1`. Verify with `resolvectl status`.

### Found and still to apply (needs root)

* Port 25 (postfix) listens on every interface. Fix: `postconf -e
  'inet_interfaces = loopback-only'`.
* Ports 5433, 4200 (the Angular dev server), 9090 and 9009 listen on every
  interface. Identify each, then bind to loopback or block in ufw.
* SSH on 22 is reachable on all interfaces and root login could not be
  confirmed off. `python -m hyphaed harden apply` rate-limits SSH and writes
  the `PermitRootLogin` drop-in after checking it will not lock you out.
* 140 unwaived kernel-checker fails. `python -m hyphaed security-scan apply`
  applies only the sysctl ones that cost nothing; the kconfig and cmdline ones
  stay proposals.
* Docker publishes `penedesaprop-postgres` (5433) and `so5076-sqlpro` (14331)
  on every interface, and Docker's own firewall rules run ahead of ufw, so
  ufw's default-deny probably doesn't protect them (not tested here). Stopgap
  in `host-harden.sh`: a systemd unit that drops those ports on the LAN
  interface only. It does not affect localhost, containers or the VM network,
  but it will block another LAN device that connects to those databases. The
  real fix is binding them. `penedesaprop-postgres` goes to `127.0.0.1`.
  `so5076-sqlpro` goes to `127.0.0.1` plus the VMware NAT address
  (`172.16.77.1`), because the Windows VM backend of PRL.PortalEmpleado
  reaches it at `172.16.77.1,14331`; localhost only would cut the VM off.
  `diagnostics/fix-docker.sh` does both.
* Cockpit (`:9090`) is enabled on all interfaces. `host-harden.sh` disables it;
  `systemctl enable --now cockpit.socket` brings it back.
* The browsermcp server (`:9009`) listens on all interfaces. ufw blocks it from
  outside; don't open that port.
* The DNS blocklist: `olive3451.com`, `gcrenwp.top`, `githubusercontent.co`,
  `steamcommunity.co`, `calude.ai` in `/etc/dnscrypt-proxy/blocked-names.txt`,
  enabled through `[blocked_names]` in `dnscrypt-proxy.toml`.

One script does all of it, with the ufw ruleset printed before and after so
you can read it. It has not been run yet:

```bash
sudo bash host-harden.sh
```

To audit any other Linux box the same way, without root:

```bash
python -m hyphaed harden            # report only
python -m hyphaed security-scan     # report only
ss -tlnp | awk '$4 ~ /^(0\.0\.0\.0|\*|\[::\]):/'   # what listens on everything
```

## Layer 3: the Windows 11 guest

Nothing here has been applied. The guest could not be reached from the host
session, so the work is two PowerShell scripts to run inside the VM from an
elevated prompt.

**Triage first**, because of the beacon. Before changing anything, look at the
DNS client cache for `olive3451`, the established TCP connections with their
owning process path, non-Microsoft scheduled tasks and startup commands.
Whatever makes the lookup should show up there. Suspend the VM or switch its
adapter to host-only until that is understood.

**Hardening** (`w11-harden.ps1`): firewall on with inbound blocked on every
profile; SMB1 off; LLMNR and NetBIOS off; Defender with PUA protection,
network protection, cloud block level high, signatures updated, full scan.

**DNS** (`w11-dnscrypt.ps1`): installs `dnscrypt-proxy` as a service on
`127.0.0.1:53`, requires DNSSEC, uses the same resolver as the host plus the
same blocklist, points the adapters at it and removes any DoH servers.
Two caveats. The script is untested: the release asset name and service
flags come from the project's documentation. And a DNSCrypt client inside the
guest is not what makes the guest filtered. Its lookups already pass through
the host's `dnscrypt-proxy` over the NAT resolver, so the host blocklist
covers it. The guest client matters only if the guest might be reached some
other way, and it means two blocklists to keep in step. Pick one and keep it.

## Replicating on another machine

1. Router: DoT on with a filtering resolver, DNSSEC, DoS protection, admin
   HTTPS only, nothing exposed to the WAN. Read the values back; don't trust
   the page.
2. Host: report first (`harden`, `security-scan`, the `ss` one-liner), then
   apply. Close or loopback-bind anything listening on all interfaces that you
   cannot name.
3. DNS: one encrypted resolver on the host, one blocklist, applications and
   VMs pointed at it. Log queries so a beacon can be found later.
4. Guests: triage, then harden, then decide whether they use the host
   resolver or their own.
5. Afterwards, check three things: `resolvectl status` shows the local
   resolver, the blocklisted domain fails to resolve, and the router log stops
   growing.

## Open items

* Run `host-harden.sh` as root and read the ufw output.
* Run the triage and `w11-harden.ps1` in the guest and find the process behind
  `olive3451.com`.
* Decide on guest DNSCrypt versus relying on the host resolver.
* Identify what listens on 5433, 9090 and 9009.
