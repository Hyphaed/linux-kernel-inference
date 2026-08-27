# Boot audit: 7.1.10-hyphaed, 2026-08-27

First boot after the 2026-08-26 rebuild that carried the 0023 kfunc fix.
Everything below was read off the running machine: `journalctl`, `hyphaed
verify`, `dkms status`, `nvidia-smi`, sysfs, and the `.config` the kernel
actually shipped with. Nothing here comes from a build log.

## The kernel is fine

No failed units. `systemctl is-system-running` says `running`. No oops, no
panic, no lockup, no soft lockup. Taint is 14336 — firmware workaround (the
`[Firmware Bug]: Overriding NUMA node to 0` messages), out-of-tree module, and
unsigned module (vmmon). No warn bit, no oops bit. All seven DKMS modules
built for 7.1.10.

Three things that were broken before this boot, and held:

**0023's kfunc sets register.** Every earlier 7.1.10 boot threw `WARNING:
kernel/bpf/btf.c:9004` at `register_btf_kfunc_id_set+0x4b/0x60` followed by
`cache_ext: failed to register kfunc sets (-22)`. This boot has neither, and
`/proc/page_cache_ext_enabled_cgroup` is present. The `BTF_SET8_START` →
`BTF_KFUNCS_START` fix landed.

**nvidia-fs loads.** GDS 1.18.0.62, real peer-to-peer DMA. The 0-byte module
from the killed 2026-08-25 DKMS build is gone.

**`pcie_aspm=performance` became `pcie_aspm.policy=performance`.** The old
form isn't a valid value for that parameter — it takes only `off` and `force`
— so it had been doing nothing at all.

## What was actually wrong

### The CPU was in power-saver

The one measurable finding, and the only red row in `hyphaed verify`.

`power-profiles-daemon` was set to **power-saver**, which put
`energy_performance_preference = power` on all 32 threads and
`energy_perf_bias = 15` — the maximum power-saving end of 0–15. On an
i9-14900KF used for local inference and gaming. The kernel had already
stepped EPB down from the BIOS value at boot (`ENERGY_PERF_BIAS: Set to
'normal', was 'performance'`); the daemon took it the rest of the way.

Turbo was on and the driver is `intel_pstate` in active mode, so this didn't
cap peak clocks outright. What it does is make HWP ramp lazily under load,
which is the wrong shape for burst decode and for frame pacing.

Fixed:

```bash
powerprofilesctl set performance
```

Measured after: EPP `performance` on cpu0/8/16/24, EPB 15 → **0**. It
persists across reboots.

### CONFIG_BPF_LSM=y was inert

`configs/fragments/42-io-uring-bpf.config` set `CONFIG_BPF_LSM=y` and the
built kernel had it. But `CONFIG_LSM`, inherited from Canonical's base config
and touched by no fragment, read
`"landlock,lockdown,yama,integrity,apparmor"` — no `bpf`. So the hooks were
compiled in and never initialised. `/sys/kernel/security/lsm` confirmed it,
and systemd said so every boot:

```
bpf-restrict-fs: BPF LSM hook not enabled in the kernel, BPF LSM not supported.
```

This is the fragment pitfalls' shape exactly: the fragment set its symbol, the
symbol landed, and the capability never arrived, because what decides is a
*string* the fragment didn't touch.

Fixed by pinning `CONFIG_LSM="landlock,lockdown,yama,integrity,apparmor,bpf"`
next to `CONFIG_BPF_LSM=y`, with `tests/test_fragment_wiring.py` failing if
either appears without the other.

### KEXEC_HANDOVER failed and disabled itself every boot

```
KHO: Failed to reserve lowmem scratch buffer
KHO: Failed to reserve scratch area, disabling kexec handover
```

`CONFIG_KEXEC_HANDOVER=y` with `KEXEC_HANDOVER_ENABLE_DEFAULT=y`, both from
Canonical's baseline. KHO needs a `kho_scratch=` boot parameter; there isn't
one, so it failed, printed two errors, and turned itself off.
`/sys/kernel/kho` never appeared. Nothing here uses it — kdump uses ordinary
`kexec -p`, which is `CONFIG_KEXEC`/`KEXEC_FILE` and unaffected.

Pinned `=n` in `05-ubuntu-26-boot.config`. Verified nothing `select`s it, so
`=n` sticks; the built `.config` now says `# CONFIG_KEXEC_HANDOVER is not
set` and its three dependents are gone with it.

**A claim in that fragment's comment was wrong, and got corrected by
measurement.** On x86, KEXEC_HANDOVER is CMA's only selector — every other one
is s390, powerpc, arm, etnaviv or aspeed. The first version of the comment
said dropping KHO would cascade `CMA=n → DMA_CMA=n → DMABUF_HEAPS_CMA=n`, on a
machine built around dma-buf. Tested by commenting the three CMA pins out and
re-running configure: **all three stayed `=y`.** `olddefconfig` carries the
inherited `=y` forward, so the select was never the only thing holding CMA on
— the same behaviour CLAUDE.md already records for `X86_AMD_PSTATE`. The pins
stay anyway, because that survival depends entirely on Canonical's base config
continuing to set `CONFIG_CMA=y`, and nothing would report it if that changed.

### 05-ubuntu-26-boot.config had never been applied. Not once.

Found while checking why `CONFIG_KEXEC_HANDOVER=n` didn't take: it was still
`=y` after configure. Nothing selects it, no later fragment sets it, and
`merge_config.sh` never saw it — because **`05-ubuntu-26-boot.config` is
listed in no preset**, and `_select_fragments()` reads the preset's
`fragments:` list.

The file's own header says its symbols are "explicitly pinned here so any
fragment that accidentally disables them is caught at configure-time". That
was false for the whole life of the file. Its contents happened to match
Canonical's base config, so nothing ever looked broken. `RD_ZSTD`,
`DEVTMPFS_MOUNT`, `OVERLAY_FS`, `SQUASHFS`, `EFI_STUB` — all correct by
inheritance, none of them actually guarded.

Same family as the `CONFIG_INTEL_ITMT` pitfall: the line was decoration, and
`merge_config.sh` will not tell you. Added to all four presets as the first
entry. `tests/test_fragment_wiring.py` now fails if any fragment on disk is in
no preset, or if any preset names a fragment that isn't on disk (the second
one is silent too — `_select_fragments()` drops it via `.exists()`).

### Patch 0008 became provable, and passes

`test_0008_wbt_latency_is_2ms_but_unprovable_here` had been written to fail
deliberately if a rotational queue ever appeared, on the grounds that it would
finally make the patch measurable. It appeared: `/dev/sdb`, a 2 TB drive in
the ASUS ROG STRIX Arion USB enclosure, presents `rotational=1` — the
enclosure doesn't pass the non-rotational hint through, and `rotational=1` is
exactly what the branch 0008 deletes keys on.

Measured `wbt_lat_usec = 2000` on that queue. Stock gives a rotational queue
**75000**. That is an observable nothing but the patch produces, so 0008 moves
from "cannot be proven on this hardware" to live, and the test is now a real
assertion.

## The previous boot's disk event, explained

Boot -1 ends with `EXT4-fs error`, `Aborting journal`, `Remounting filesystem
read-only` on `sda1`. The sequence:

```
[12720.382] usb 2-1: SuperSpeed Plus Gen 2x1 — ASUS ROG STRIX Arion (0b05:1932)
[12726.004] sda: 1953525168 blocks (1.00 TB) — Attached SCSI disk
[12734.340] EXT4-fs (sda1): mounted filesystem 8563871e-… r/w
[12767.742] usb 2-1: USB disconnect, device number 2
[12767.744] device offline error … Buffer I/O error … Aborting journal
[12775.578] sda: 3907029168 blocks (2.00 TB) — Attached SCSI disk
```

An enclosure was unplugged 33 seconds after being mounted read-write with
dirty pages outstanding, and a different, larger drive was attached 2.3
seconds later. That's a live drive swap. Not a hardware fault, not a kernel
bug — the kernel refused further writes, which is correct.

The cost is bounded: writes from those last 33 seconds are gone, and
filesystem `8563871e-aed4-460a-a1bb-7f3881af2d75` on the 1 TB drive has an
aborted journal. It isn't attached now.
`diagnostics/fsck-orphaned-usb-fs.sh` reports on it and, with `--go`, fscks it
the moment that drive comes back.

## Userspace defects, all in one script

`diagnostics/fix-boot-userspace-7.1.10.sh`, dry-run by default. Needs root,
which is why it's a script and not something already applied.

1. `/etc/systemd/resolved.conf:47` is the literal line `nameserver 127.0.2.1`
   — resolv.conf syntax in a systemd config file. One parse error per boot.
   **Corrected after applying the fix:** this was not stray junk. 127.0.2.1 is
   dnscrypt-proxy, and pointing resolved at it is deliberate — but the working
   configuration was already in `/etc/systemd/resolved.conf.d/10-dnscrypt.conf`
   (`DNS=127.0.2.1`, `Domains=~.`), installed by `fix-boot-2026-08-17.sh`. The
   line in `resolved.conf` was a second, malformed attempt at the same thing;
   resolved ignored it every boot and used the drop-in. Deleting it was right
   and nothing was lost, but "someone pasted resolv.conf syntax into the wrong
   file" understates it — read the `.d` directory before judging a line in the
   main config.
2. **NetworkManager cannot apply DNS at all.** It has no `dns=` key, so it
   falls back to calling `resolvconf`. On this box `/usr/sbin/resolvconf` is a
   symlink to `resolvectl` (from systemd-resolved; the resolvconf package
   isn't installed), and NM calls it with the pseudo-interface name
   `NetworkManager`, which resolvectl rejects as not a device. DNS works today
   only because `/etc/resolv.conf` is a hand-made *copy* of resolved's stub
   file that happens to still be right. Change networks or join a VPN and it
   goes stale — which presents as "the internet is broken on this network",
   not as a DNS error.

   **Applied and verified 2026-08-27.** NetworkManager now reports
   `dns=systemd-resolved rc-manager=unmanaged (auto), plugin=systemd-resolved`
   and has logged no `resolvconf failed` since. `/etc/resolv.conf` is a symlink
   again and queries route to dnscrypt-proxy on 127.0.2.1 as the drop-in
   intends. Note the tradeoff that drop-in already documents and this change
   does not alter: `Domains=~.` makes DNSCrypt the default route for every
   name, so VPN split-horizon DNS and internal names served by
   docker0/vmnet1/vmnet8/br-* will not resolve via those interfaces' own
   resolvers.
3. `cpufrequtils` and `loadcpufreq` are enabled and ask for the `ondemand`
   governor, which does not exist under `intel_pstate` in active mode. Three
   deprecation warnings per boot for a service that cannot do anything. Same
   lesson as the removed Build-options menu: a control that cannot change the
   outcome is not a control.
4. `sssd` enabled and unconfigured — six socket units fail their dependency
   every boot.
5. `/etc/bluetooth` is 0755; `bluetooth.service` declares
   `ConfigurationDirectoryMode=555`.
6. `/usr/src/nvidia-fs-2.29.4/Makefile.orig` is a 0-byte backup written the
   minute the DKMS build was killed. The real fix is in place but will be
   reverted by the next package upgrade.

## Noise, confirmed as noise

Recorded so a future audit doesn't re-derive them.

- `[Firmware Bug]: Overriding NUMA node to 0` ×20 and `nvidia-fs:warning:
  error retrieving numa node` ×4 — one NUMA node, and every GPU/NVMe function
  reads `numa_node = 0`. Cosmetic.
- `PEFILE: Unsigned PE binary` — kdump loading our unsigned image. Secure Boot
  is disabled (platform in Setup Mode) and kdump reports `loaded kdump
  kernel`.
- Two `systemd-cryptsetup: Failed to activate with specified passphrase` —
  typos at the LUKS prompt. Third attempt worked.
- chronyd NTS TLS failures against 4 of 5 Canonical servers (expired
  certificate chain). Server-side. The fifth works and the clock is synced to
  0.8 ms. Costs redundancy, not accuracy.
- GNOME/gsd/portal D-Bus warnings, `pool-1 uses wireless extensions`. Not our
  code.

## What is still unverified

Everything in the fragment and patch work is verified as *configuration*, not
as behaviour. `CONFIG_LSM` carrying `bpf` and KHO being off are both confirmed
in the built `.config` by a real `--phase configure` run; neither has been
booted. Until then they are correct-looking, not proven.

0023's call sites in `mm/filemap.c` and `mm/vmscan.c` remain unproven on
7.1.10 and on 7.2. A registered struct_ops whose hooks never fire is
indistinguishable from a working one from userspace, and a clean `git am` says
nothing about it either way.
