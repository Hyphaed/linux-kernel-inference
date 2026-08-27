# Boot audit — 7.2.0-hyphaed, 2026-08-27

First real boot of `7.2.0-hyphaed` on `ncore` (uptime ~10 min at audit time).
`systemctl is-system-running` = `running`, no failed units. One real finding.

## AppArmor unprivileged-userns restriction (0024) was silently OFF

`sysctl kernel.apparmor_restrict_unprivileged_userns` read **0**, even though
`/usr/lib/sysctl.d/10-apparmor.conf` sets it to 1 and patch 0024 wires the
real enforcement into the kernel. The journal showed the culprit directly:

```
apparmor.systemd[4004]: kernel.apparmor_restrict_unprivileged_userns = 0
```

That's `apparmor.service` itself overriding the sysctl during boot, after
`systemd-sysctl.service` already set it to 1.

### Root cause

`/lib/apparmor/rc.apparmor.functions::check_and_set_userns()` runs on every
boot and checks:

```
/sys/kernel/security/apparmor/features/policy/unconfined_restrictions/userns
```

If that securityfs boolean is missing, it concludes the running kernel
doesn't support unconfined-userns restriction at all and force-sets the
sysctl back to 0, logging exactly the line above.

That file **is missing** on this kernel. `security/apparmor/apparmorfs.c`'s
`aa_sfs_entry_unconfined[]` table only carries `change_profile`:

```c
static struct aa_sfs_entry aa_sfs_entry_unconfined[] = {
	AA_SFS_FILE_BOOLEAN("change_profile", 1),
	{ }
};
```

Patch 0024 (the forward-ported Ubuntu SAUCE) wired the real enforcement —
`apparmor_userns_create()`, `aa_profile_ns_perm()`, `AA_USERNS_CREATE` — but
never added a `userns` entry to this table. It shipped the mechanism without
the advertisement Ubuntu's own boot script checks for.

Crucially, the kernel-side enforcement does **not** depend on that file.
Confirmed by reading `security/apparmor/{lsm,task,policy}.c` in the built
7.2 tree: `aa_unprivileged_userns_restricted` is a plain `int`, fed directly
from the `kernel.apparmor_restrict_unprivileged_userns` sysctl via a normal
`proc_dointvec` handler, and `task.c` reads that global directly at
enforcement time. The securityfs node is pure userspace signaling — nothing
in the kernel gates on it.

This is exactly the "not booted, verify before trusting" caveat CLAUDE.md
already carried for 0024. Verified now: it does **not** hold, with a
diagnosed, fixable cause.

### Fix — two tracks

**Immediate, no rebuild:** `hyphaed/phases/postinstall.py::_check_apparmor_userns()`
installs a systemd drop-in,
`/etc/systemd/system/apparmor.service.d/90-hyphaed-userns.conf`:

```ini
[Service]
ExecStartPost=/usr/sbin/sysctl -w kernel.apparmor_restrict_unprivileged_userns=1
```

This re-asserts the real, working restriction after `apparmor.service`'s own
logic clobbers it, every boot. Runs automatically as part of the postinstall
phase; also applied once by hand on this boot to close the immediate gap.

**Correct fix, next kernel build:** `patches/custom/0028-apparmor-userns-sfs-advertise.patch`
adds the missing entry:

```c
static struct aa_sfs_entry aa_sfs_entry_unconfined[] = {
	AA_SFS_FILE_BOOLEAN("change_profile", 1),
	AA_SFS_FILE_BOOLEAN("userns", 1),
	{ }
};
```

Advertisement only — no enforcement-path change, so no behavior change
beyond letting `apparmor.service` stop overriding the sysctl on its own.
Verified: compiles clean (`security/apparmor/apparmorfs.o`), applies clean
via `git am --3way` on top of the pinned 0024 commit. Referenced from
`patches/kernel-org-7.2/series` right after 0024. Once this lands in a built
kernel, the systemd drop-in above becomes redundant belt-and-suspenders
rather than load-bearing.

### Re-verify after either fix

```bash
sysctl kernel.apparmor_restrict_unprivileged_userns   # must read 1
unshare -Ur true                                      # confined, not denied
aa-status | grep unprivileged_userns
# then actually start chromium, a flatpak, and a podman container
```

This is a mediation path — a wrong transition degrades confinement quietly,
so the four checks above (not just the sysctl read) are all required, per
0024's own original caveat.

## Minor / no-action findings

- **Bluetooth + Wi-Fi both `rfkill` soft-blocked** (`hci0`, `phy0`).
  Consistent with `bluetoothd: Failed to set mode (0x03)` in the journal.
  Not touched — could be an intentional airplane-mode key state, not a
  kernel regression. Confirm intent before `rfkill unblock all`.
- **chronyd NTS/TLS handshake fails** against all four `*.ntp.ubuntu.com:4460`
  ("certificate chain uses expired certificate"). Plain NTP fallback works —
  `chronyc tracking` shows stratum 3, synced, sub-millisecond offset. Time is
  correct; only the NTS auth layer is broken. Worth `apt list --upgradable
  ca-certificates` at some point, not urgent.
- **`out/security/hardening-unknown.json`** — the report filename didn't
  resolve `kernel_pkgver` for this run (should read `hardening-7.2.0.json`).
  Cosmetic. 111 FAIL / 147 OK matches the already-documented, already-accepted
  tradeoff set (BPF_JIT_ALWAYS_ON, IBT off, etc.) — no new hardening
  regression versus prior builds.
- **UFW logging repeated IGMP multicast blocks** from the router
  (`224.0.0.1`, PROTO=2, ~every 20s). Harmless default-deny noise.
- **Checked and ruled out, not assumed:**
  - `scaling_governor=powersave` is the *expected* label under
    `intel_pstate=active` (confirmed: `/sys/devices/system/cpu/intel_pstate/status`
    = `active`) — EPP (`performance`, confirmed via `powerprofilesctl get`)
    is what actually governs behavior here, not the legacy governor name.
  - DAMON, `page_cache_ext`, dma-buf (0019/0020) patches all match their
    documented "compiled in, inert by design" state — no regression.
  - No kernel Oops/BUG/WARNING beyond the known-benign `[Firmware Bug]
    Overriding NUMA node` (single-NUMA-node box, cosmetic, documented
    elsewhere in this repo) and one `NOHZ tick-stop error` pair (transient,
    not currently reproducible, worth a dmesg diff if it recurs on a future
    boot).
