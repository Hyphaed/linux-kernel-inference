# Boot audit 7.2.11 (2026-10-16)

```
uname -r                           7.2.11-hyphaed
taint (7.2.8 baseline 12288)       12288
failed units                       0
0030: 'retraining failed' lines    0
0030: 00:1c.0 first/last kmsg      [    0.637051 [    0.646053 
0029: nvidia-fs numa warnings      0
0029: [Firmware Bug] NUMA lines    0
nvidia-smi driver                  615.71.09
nvidia-fs.ko bytes                 261716

greenboost/3.4, 7.2.10-hyphaed, x86_64: installed
greenboost/3.4, 7.2.11-hyphaed, x86_64: installed
hid-xpadneo/v0.11-pre-64-g5de4fde-dirty, 7.0.0-38-generic, x86_64: installed
hid-xpadneo/v0.11-pre-64-g5de4fde-dirty, 7.2.10-hyphaed, x86_64: installed
hid-xpadneo/v0.11-pre-64-g5de4fde-dirty, 7.2.11-hyphaed, x86_64: installed
nvidia/615.71.09, 7.0.0-38-generic, x86_64: installed
nvidia/615.71.09, 7.2.10-hyphaed, x86_64: installed
nvidia/615.71.09, 7.2.11-hyphaed, x86_64: installed
nvidia-fs/2.30.2, 7.0.0-38-generic, x86_64: installed
nvidia-fs/2.30.2, 7.2.10-hyphaed, x86_64: installed
nvidia-fs/2.30.2, 7.2.11-hyphaed, x86_64: installed
v4l2loopback/0.15.3, 7.0.0-38-generic, x86_64: installed
v4l2loopback/0.15.3, 7.2.10-hyphaed, x86_64: installed
v4l2loopback/0.15.3, 7.2.11-hyphaed, x86_64: installed
vmmon/26.0.1, 7.2.10-hyphaed, x86_64: installed
vmmon/26.0.1, 7.2.11-hyphaed, x86_64: installed
vmnet/26.0.1, 7.2.10-hyphaed, x86_64: installed
vmnet/26.0.1, 7.2.11-hyphaed, x86_64: installed
```

## hyphaed verify
```
─────────────────────────────────── hyphaed ────────────────────────────────────
custom Linux kernel wizard — AI · gaming · virtualization
version 0.1.0

┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ check                            ┃ status ┃ detail                           ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ running kernel is hyphaed-built  │ ✓      │ uname -r = 7.2.11-hyphaed        │
│ no Vulnerable CPU                │ ✓      │ 0 vulnerable                     │
│ vulnerabilities                  │        │                                  │
│ BORE scheduler active            │ ✓      │ kernel.sched_bore=1              │
│ sched_ext available              │ ✓      │ scx_* userspace schedulers can   │
│                                  │        │ run                              │
│ sched_ext kernel support         │ ✓      │ state=enabled nr_rejected=0      │
│ IOMMU enabled                    │ ✓      │ needed for VFIO + greenboost     │
│                                  │        │ DMA-BUF                          │
│ THP enabled                      │ ✓      │ active mode = always             │
│ hugepages available              │ ✓      │ static 2MiB: 0 pages, THP=always │
│                                  │        │ madvise n                        │
│ zswap active                     │ ✓      │ compressor=zstd                  │
│                                  │        │ max_pool_percent=8               │
│                                  │        │ (pool/written-back stats need    │
│                                  │        │ root: /sys/kernel/debug/zswap/)  │
│ CPU governor suitable            │ ✓      │ intel_pstate=active, HWP-driven; │
│                                  │        │ governor=powersave               │
│                                  │        │ EPP=balance_performance          │
│ expected cmdline tokens present  │ ✓      │ 3 tokens matched                 │
│ nvidia kernel module loaded      │ ✓      │                                  │
│ greenboost module loaded         │ ✓      │                                  │
│ DKMS modules all installed       │ ✓      │ all clean (7 modules)            │
│ vmmon/vmnet loaded               │ ✓      │                                  │
│ nvidia_fs loaded                 │ ✓      │ GDS peer-to-peer active          │
│ no failed systemd units          │ ✓      │ clean                            │
│ 0003 vfs_cache_pressure=50       │ ✓      │ 50                               │
│ 0004 max_map_count MASKED        │ ✓      │ 1048576, overridden by           │
│                                  │        │ 50-default.conf,                 │
│                                  │        │ 55-map-count.conf                │
│ 0009 rq_affinity=2               │ ✓      │ all nvme                         │
│ 0013 nvme APST 25000us           │ ✓      │ 25000                            │
│ 0014 THP defrag=defer+madvise    │ ✓      │ defer+madvise                    │
│ 0019/0020 dma-buf UAPI installed │ ✓      │ all 4 ioctls                     │
│ 0023 cache_ext registered        │ ✓      │ struct_ops type in BTF           │
└──────────────────────────────────┴────────┴──────────────────────────────────┘
```

## Notes

- `/etc/fstab` still lists `/swap.img` twice, so `systemd-fstab-generator` exits 1 each boot. Swap works; it's log noise. Fix: `sudo bash diagnostics/fix-boot-7.2.10-2026-10-16.sh`.
- Boot 15:00-15:40 froze the desktop. Sunshine ran `shield-stream.sh display-off` at 15:38:25, gnome-shell then logged 3856 `gbm_surface_lock_front_buffer failed` lines, and the journal ends with no shutdown (hard reset). No Oops, no NVRM Xid. Seen once, not on any 7.2.10 boot. Looks like mutter/NVIDIA KMS on virtual-display teardown; not enough to blame the kernel. Watch for it during the Shield pair test.
- One `NOHZ tick-stop error: softirq #10 pending` line per 7.2.11 boot (`nohz_full=4-7`), none on 7.2.10. Harmless.
- `kernel stack frame pointer ... bad value` in udev-worker during nvidia_modeset load; also on 7.2.10, not a regression.
- The 0030 `00:1c.0` line was empty in the first run: `dmesg` needs root here. The script now reads the journal instead.
