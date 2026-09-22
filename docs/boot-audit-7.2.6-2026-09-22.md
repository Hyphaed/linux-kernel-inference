# Boot audit: 7.2.6-hyphaed, 2026-09-22

Everything below was read off the running machine: `journalctl`, `hyphaed
verify`, `dkms status`, `nvidia-smi`, sysfs, `udevadm verify`, and the
`.config` the kernel actually shipped with. Nothing here comes from a build
log. This audit doubles as the "before" state for kernel 7.2.7's bump and
for patch 0029 (see `docs/tasks_patches.md` T20) — both change things this
audit measures, so the numbers here are the baseline they get checked
against later.

## The kernel is fine

- `systemctl --failed`: 0 loaded units. Clean.
- `systemctl is-system-running`: not captured directly this pass, but zero
  failed units and `hyphaed verify` passing all 27 checks (below) says the
  same thing.
- `cat /proc/sys/kernel/tainted` = `14336` = bits 11 (`TAINT_FIRMWARE_WORKAROUND`,
  2048) + 12 (`TAINT_OOT_MODULE`, 4096) + 13 (`TAINT_UNSIGNED_MODULE`, 8192).
  Same three bits as every prior audit in this repo. No oops, no lockup, no
  bad page (`D`/`L`/`M`/`B` bits all clear).
- `hyphaed verify`: all 27 checks pass — BORE active, sched_ext enabled,
  IOMMU on, THP=always, zswap active with zstd, CPU governor correct
  (EPP=performance), all DKMS modules clean, nvidia/nvidia_fs/greenboost/
  vmmon all loaded, no failed units, and all of this repo's own patch
  markers (0003/0004/0009/0013/0014/0019-0020/0023) present and correctly
  overridden where expected.
- NVIDIA driver: **615.71.09** (`nvidia-smi --query-gpu=driver_version`),
  not the 610.43.02 this file's own CLAUDE.md last recorded — the fifth
  instance of the documented `dpkg -l` `rc`-state drift pattern. Out of
  scope to correct in CLAUDE.md this session (no driver change made here);
  flagged so the next session doesn't trust that field either.

## What was actually wrong

### hyphaed's own systemd unit had an ExecStart= systemd couldn't parse

`nvidia-pci-bridge-power.service` (written by
`postinstall.py::_check_dgx_parity_basics`) embedded
`awk "{print \$1}"` and a bare `$b` directly inside an `ExecStart=` value.
systemd's unit-file parser does its own backslash-escape and `$VAR`
expansion on that value **before** the shell ever sees it —
`\$1` is not a systemd escape sequence, so every boot logged:

```
systemd[1]: /etc/systemd/system/nvidia-pci-bridge-power.service:9: Ignoring unknown escape sequences: "for b in $(lspci -D -d ::0604 | awk \"{print \$1}\"); do echo on > /sys/bus/pci/devices/$b/power/control 2>/dev/null || true; done"
```

Fixed in `hyphaed/phases/postinstall.py`: the shell logic now lives in a
real script (`/usr/local/sbin/hyphaed-pci-bridge-power-on.sh`), and
`ExecStart=` just invokes it — no unit-line quoting left to get wrong.
Pinned by `tests/test_pci_bridge_power_unit.py`. This is a code fix (future
builds get it automatically); the currently-installed unit on this box
still has the old broken form until
`diagnostics/fix-boot-issues-7.2.6-2026-09-22.sh --go` (step 1) is run.

### A third-party udev rule used a match-key form this systemd rejects

`udevadm verify /etc/udev/rules.d/99-esd-s1c-ssd.rules` (not just the boot
log's warning text) gives the real diagnostic:

```
/etc/udev/rules.d/99-esd-s1c-ssd.rules:20 Invalid key 'DEVTYPE'.
```

Line 20 matches `DEVTYPE=="usb_device"` as a bare key. Checked against
every real rule in `/usr/lib/udev/rules.d/` that matches on device type —
`60-block.rules`, `60-libsane1.rules`, `80-udisks2.rules` — all of them use
`ENV{DEVTYPE}=="..."`, none uses the bare form. systemd 259 (this box's
version) requires the `ENV{}` wrapper for this key. The rule's own comment
shows the intent was correct (scope to the device node, not its interface
children); only the key syntax was wrong. One-token fix, in
`diagnostics/fix-boot-issues-7.2.6-2026-09-22.sh` step 2 — this is the
user's own personal rule file (installed 2026-09-21 per its own header),
not hyphaed's, so the script proposes and backs up rather than editing
silently.

### A cross-file GOTO/LABEL never worked, and this systemd's own checker proves it

`udevadm verify` on `59-xpadneo-hidraw-preempt.rules` **and**
`70-xpadneo-disable-hidraw.rules` together — not just the one file with
the `GOTO` — still reports:

```
59-xpadneo-hidraw-preempt.rules:10 GOTO="xpadneo_hidraw_lockdown" has no matching label, ignoring.
59-xpadneo-hidraw-preempt.rules:10 The line has no effect any more, dropping.
70-xpadneo-disable-hidraw.rules:1 style: LABEL="xpadneo_hidraw_lockdown" is unused.
```

This settles a real ambiguity: udev rule files look like one concatenated
stream across `/etc/udev/rules.d/`, but `GOTO`/`LABEL` resolution in this
systemd version is scoped **per file**. The rule's intent — skip past
vendor `60-*.rules` files' unconditional `uaccess` tagging and land at
`70-xpadneo-disable-hidraw.rules`'s lockdown logic — was never reachable
the way the two files were split. The `GOTO` line has been silently
dropped (not executed) on every boot since 2026-09-14 (the file's mtime),
meaning the vendor uaccess tag was never actually preempted by this rule.
Fix: merge both files' content into the earlier-sorting filename so
`GOTO`/`LABEL` share a file — `diagnostics/fix-boot-issues-7.2.6-2026-09-22.sh`
step 3, with a backup of both originals and an explicit note to reconnect
the physical controller afterward and confirm input still works, since
this changes real ACL/input behavior this audit can't verify from sysfs
alone.

### Package-owned SysV scripts that can never do anything, still scanned every boot

`cpufrequtils`/`loadcpufreq` are `disabled` (confirmed via
`systemctl is-enabled`) but `systemd-sysv-generator` unconditionally scans
`/etc/init.d/` regardless of enable state, logging four lines per script
per boot ("lacks a native systemd unit file", "Please update package...",
"This compatibility logic is deprecated..."). `diagnostics/
fix-boot-userspace-7.1.10.sh` item 3 disabled both scripts on 2026-08-28 and
recorded that as applied-and-verified — correct as far as it went, but
disabling doesn't stop the generator's scan, only the scripts' own
execution. `dpkg -S` confirms both files are owned by the `cpufrequtils`
package; nothing else on this box depends on it (both scripts' own
`GOVERNOR="ondemand"` target doesn't exist under `intel_pstate=active`
anyway, per the 7.1.10 audit's original finding). Fix:
`apt-get purge cpufrequtils` — step 4 of the same script. Two other SysV
packages log the identical warning shape (`vmware-USBArbitrator`,
`uml-utilities`) but are genuinely in use (VMware host tooling) and are
**not** touched by this script.

## T20 / patch 0029's acceptance gate — reproduced, still open

Same four `nvidia-fs:warning: error retrieving numa node for device ...`
lines this boot as every prior audit (GPU once, each NVMe controller
once), and 19-20 `pci_alert(FW_BUG "Overriding NUMA node to 0")` lines —
one per PCI device the `62-hyphaed-pci-numa-node.rules` udev rule
rewrites, each call tainting `TAINT_FIRMWARE_WORKAROUND`. Taint stays
`14336`. This is the live "before" measurement patch 0029's acceptance
gate is checked against — see `docs/tasks_patches.md` T20. Not yet
installed or booted; that's a deliberate separate step from this session's
7.2.7 build.

## Noise, confirmed as noise

- `dev-zram0.device` and ~20 sibling `.device` units reading `1min 34s` in
  `systemd-analyze blame` — not a real stall. It's the LUKS passphrase
  prompt (two failed attempts this boot, `systemd-cryptsetup[686]: Failed
  to activate with specified passphrase`) being charged to every device
  unit ordered after `cryptsetup` completes. Real userspace cost per
  `systemd-analyze time`: 12.463s.
- `virt/tdx: TDX not supported by the host platform` — expected, this CPU
  doesn't implement Intel TDX.
- ~1500 of 1685 this-boot warnings are five repeating GNOME Shell
  `Can't update stage views actor ... needs an allocation` lines from the
  ubuntu-dock extension — a GNOME Shell/extension rendering-order quirk,
  not a kernel or hyphaed concern.
- `spice-vdagent.service:23: Unknown key 'StandardError'` — distro package
  unit file quirk, not ours.
- `/run/initramfs/lib64` tmpfiles symlink error and `dm_crypt-0 busy` on
  shutdown, both on the previous boot (`-1`) — matches
  `boot-audit-7.2.2-2026-08-31.md`'s existing "Noted, not fixed" entries;
  unchanged, still low-severity, still not addressed here.

## What is still unverified

- Patch 0029's full acceptance gate needs an actual reboot into a kernel
  carrying it — not done this session (see 7.2.7 bump notes).
- The xpadneo rule merge (fix step 3) changes real controller ACL/input
  behavior; this audit only confirms the udev syntax is now valid, not
  that the physical controller still works exactly as before. Test after
  applying.
- Whether `cpufrequtils`'s purge has any dependency this box actually uses
  it recorded elsewhere is not exhaustively checked beyond `apt-cache
  rdepends` reasoning above — a full `apt-get purge` dry run
  (`apt-get purge --simulate cpufrequtils`) is worth reading before running
  with `--go` if this is done outside this session.
