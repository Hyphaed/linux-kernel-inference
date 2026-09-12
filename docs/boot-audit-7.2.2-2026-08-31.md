# Boot audit — 7.2.2-hyphaed, 2026-08-31

Read off the running machine (`journalctl`, `systemctl --failed`, `dmesg`,
`dkms status`, `nvidia-smi`, sysfs/sysctl), not from a build log. Up 1h27m at
audit time, taint `14336` (firmware-workaround + out-of-tree + unsigned
module — expected, matches every prior audit: vmmon/greenboost/nvidia are
all out-of-tree by design). `systemctl is-system-running` reports `degraded`
because of the one real finding below.

## One failed unit, every boot, and it's ours to fix

`systemctl --failed` on this boot:

```
UNIT                         LOAD   ACTIVE SUB    DESCRIPTION
systemd-modules-load.service loaded failed failed Load Kernel Modules
```

`journalctl --list-boots` + `journalctl -b <N> -p 3` shows the identical
failure on all 10 available boots (`-9` through `0`), not just this one:

```
systemd-modules-load[...]: Failed to insert module 'nvidia_peermem': Invalid argument
systemd[1]: Failed to start systemd-modules-load.service - Load Kernel Modules.
```

### Root cause

`/usr/src/nvidia-610.43.02/nvidia-peermem/nvidia-peermem.c`'s
`nv_mem_client_init()` only does real work
`#if defined(NV_MLNX_IB_PEER_MEM_SYMBOLS_PRESENT)`; the `#else` branch is a
bare `return -EINVAL;`. This box has no Mellanox OFED / `ib_core` stack, so
that symbol is never defined — the module structurally **cannot** insert here,
independent of GPU or driver version. It's not a kernel-patch regression;
none of the 24 vendored patches touch this file or anything near it.

`hyphaed/phases/postinstall.py::_check_dgx_parity_basics()` is what put the
machine in this state: it treated `modinfo nvidia-peermem` succeeding (the
module *exists on disk*) as license to `modprobe` it and unconditionally
write `/etc/modules-load.d/nvidia-peermem.conf` for every future boot — the
actual `modprobe` exit status was discarded, so it logged success regardless
of whether the module actually loaded.

**Cascading effect, and it needed its own fix.** `vmware.service` also
failed every boot (`Job vmware.service/start failed with result
'dependency'`) because its unit `Requires=systemd-modules-load.service`. A
hand-maintained `/etc/systemd/system/vmware.service.d/override.conf` (dated
2026-08-26, not written by any hyphaed code) already tries to fix exactly
this by setting an empty `Requires=` to clear the dependency and downgrading
to `Wants=`. Verified — twice, once against the live unit and once against a
synthetic throwaway unit pair to rule out anything vmware-specific — that
this does **not** work on this systemd version (259): an empty `Requires=`
set in a `*.service.d/` drop-in does not clear a `Requires=` already set in
the unit's own `[Unit]` section, regardless of the reset syntax being
textbook-correct. `systemctl show vmware.service -p Requires` and
`SYSTEMD_LOG_LEVEL=debug systemd-analyze verify` both confirm
`systemd-modules-load.service` stays in the effective `Requires=` list either
way. The only mechanism that actually removes it is editing the `Requires=`
line out of `vmware.service` itself — confirmed with the same debug-verify
technique against a scratch copy before touching the real file.
`vmware.service` isn't owned by any dpkg package (`dpkg -S` finds nothing;
VMware Workstation's own installer writes it directly, not apt), so a direct
edit isn't at risk of being silently reverted the way editing a vendor-shipped
unit under `/usr/lib` would be — only re-running VMware's own installer would
touch it again.

### Fix — applied 2026-08-31

1. `hyphaed/phases/postinstall.py::_check_dgx_parity_basics()` now checks the
   real `modprobe` exit status before writing the boot-time loader config or
   logging success. On failure it logs the real reason (no IB peer-memory
   stack on this box) and does not persist the load-at-boot config, so a
   fresh `postinstall` run on any future kernel won't put a new machine into
   this same state.
2. New `diagnostics/fix-boot-issues-7.2.2-2026-08-31.sh` (dry-run by default,
   `--go` to apply) removes the stale
   `/etc/modules-load.d/nvidia-peermem.conf` already on this box, edits
   `Requires=systemd-modules-load.service` out of `/etc/systemd/system/
   vmware.service` (backing it up first, replacing it with `Wants=` so
   ordering is kept but a failure no longer aborts the start job), removes
   the now-redundant `vmware.service.d/override.conf` (backed up first, since
   its Requires=/Wants= trick never worked), and `systemctl daemon-reload`s.
   **Needs root the sandboxed session this audit ran in doesn't have** — run
   it by hand:
   ```
   sudo bash diagnostics/fix-boot-issues-7.2.2-2026-08-31.sh        # dry run
   sudo bash diagnostics/fix-boot-issues-7.2.2-2026-08-31.sh --go   # apply
   ```
   After `--go`, `systemctl --failed` should come back empty and
   `systemctl status vmware.service` should read `active`/`exited` cleanly
   — confirm on the next boot as well as immediately.

### New regression guard

`hyphaed verify` had no check that would have caught this — it never once
surfaced the failure across any of the last 10 boots. Added
`check_no_failed_units()` to `hyphaed/verify.py` (`systemctl --failed`, no
privilege needed), wired into `run_all()`. Confirmed it correctly reports the
still-open failure right now:

```
no failed systemd units  ✗  systemd-modules-load.service
```

## The one confirmed bug in the vendored patches — fixed, not rebuilt

`patches/custom/0023-mm-bpf-cache-ext-page-cache-eviction-7.2.patch`'s
`bpf_page_cache_ext_init()` assigned the signed return of
`btf_find_by_name_kind()` into a `u32 type_id`, so `type_id < 0` could never
be true (independently flagged by sparse and coccinelle — see
`docs/static-analysis-findings-2026-08-21.md`'s 2026-08-28 section). Fixed
2026-08-31: `type_id` is now `s32` in both this file and the byte-comparable
7.1.10 sibling. sha256s re-pinned in `patches/VENDOR-kernel-org-7.2.lock` and
`patches/VENDOR-kernel-org-7.1.lock`.

**Verified without a full kernel build**, per this repo's own
"compile every object a forward-ported patch touches" rule: applied the
identical one-line fix directly to `build/linux-7.2.2/mm/page_cache_ext.c`
(which already carried the unfixed patch from the last full build) and
compiled just that object —

```
$ make mm/page_cache_ext.o
  CC      mm/page_cache_ext.o
```

— clean, no warnings, no errors.

Still dormant on this box (no BPF struct_ops program loaded, and MGLRU is
active so the classic-LRU-only hook path isn't even reached — see
`docs/plan_patches.md` T18), so this fixed a confirmed defect, not a live
incident. It only takes effect on hyphaed's *next* build of the series; no
rebuild or reboot was triggered by this audit.

## Everything else checked — no action, matches what's already documented

- UFW blocking periodic IGMP queries to `224.0.0.1` from the LAN router,
  every ~20s all boot — the already-documented "UFW IGMP noise," harmless.
- `bluetoothd: Failed to set mode: Failed (0x03)` every boot — `rfkill list`
  shows both `hci0` (Bluetooth) and `phy0` (Wi-Fi) **soft blocked**, matching
  the 7.2.0 audit's "Confirmed intentional, 2026-08-28 — leave as is." Not
  new.
- `PEFILE: Unsigned PE binary` — NVIDIA's own GSP firmware blob load message
  (immediately adjacent to `NVRM: loading NVIDIA UNIX Open Kernel Module`),
  benign, unrelated to any vendored patch.
- `gkr-pam: unable to locate daemon control file` — routine GNOME-keyring
  desktop noise.
- `CONFIG_X86_KERNEL_IBT` unset (expected — 610.43.02-open compat, see
  CLAUDE.md), `sched_ext`/BORE/`scx_bpfland` all healthy
  (`kernel.sched_bore=1`, `state=enabled nr_rejected=0`),
  `kernel.apparmor_restrict_unprivileged_userns=1` (0024/0028 both working),
  `vm.anon_min_ratio=15`/`vm.clean_min_ratio=15` (0025 as configured), zswap
  and zram both active — all verified live, all fine.
- kdump: enabled, active, `ready to kdump`. Lockdown: `none` (expected,
  Secure Boot disabled / setup mode).
- GreenBoost: module loaded clean, no dmesg errors beyond the expected
  startup banner + one apparmor DENIED for `wsdd` reading
  `/usr/local/lib/greenboost/` (unrelated userspace ACL, not a kernel-patch
  concern). `dataflux_summary`/`dataflux_errors` (last 5 days) show only two
  pre-existing `synapse_serve` OOM-load failures from 2026-08-28 (unrelated
  model, already stale) and one `vram_headroom_exhausted` warning from the
  same window — nothing from today's boot.

## Noted, not fixed in this pass (low severity, not patch-related)

- `systemd-tmpfiles[...]: Failed to create symlink
  '/run/initramfs/lib64/ld-linux-x86-64.so.2': Not a directory` — present on
  5 of the last 6 boots, **absent on this boot**; `/run/initramfs/lib64`
  doesn't exist post-boot and no `tmpfiles.d` rule on this box or in this
  repo targets that path — Ubuntu's own `initramfs-tools` early-boot
  plumbing, not anything hyphaed's `install.py`/`postinstall.py` write.
  Intermittent, self-resolving, out of hyphaed's scope; flagged here rather
  than silently dropped.
- `systemd-cryptsetup[...]: Failed to deactivate 'dm_crypt-0': Device or
  resource busy` — seen on some past shutdowns, not in this boot's up-log,
  unrelated to any vendored patch.

## What is still unverified

- The vmware.service fix (item above) — the effective dependency graph is
  confirmed clean via `systemd-analyze verify`, but the script itself hasn't
  been run on this box yet (needs root this session doesn't have) and vmware
  hasn't actually been started against the edited unit. Run
  `diagnostics/fix-boot-issues-7.2.2-2026-08-31.sh --go`, then confirm on the
  next real reboot that `vmware.service` starts clean.
- 0023's actual reclaim behavior remains unproven regardless of this fix —
  see `docs/tasks_patches.md` T18: MGLRU is still active, so
  `page_cache_ext_isolate_and_reclaim()` still isn't reached by any real
  workload on this box.
