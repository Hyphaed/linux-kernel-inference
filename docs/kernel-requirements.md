# What every hyphaed kernel build must contain, and where each rule lives

Checked on 2026-10-28 against `/boot/config-7.2.12-hyphaed` and the running
machine (`ncore`, i9-14900KF, RTX 5070). The source of truth is
`configs/fragments/` plus `configs/presets/`. This file says why each
requirement exists, where it's pinned, and how to check it. "Verified" means I
compared it with the built config or the live system; "inferred" means I
didn't.

The laptop (Ryzen AI 9 365) builds from the same fragments with
`gaming-ai-laptop-amd`. Section 5 lists what differs.

## 1. How a requirement gets into a build

```
configs/base/<snapshot>        Ubuntu's config for the running kernel
  + fragments, in preset order 05 first ... 44 ... 70 last
  + configs/generated/90-machine-topology.config   (hardware symbols, never hand-written)
  -> merge_config.sh -> olddefconfig -> floor validation (hyphaed/kconfig.py)
```

A new requirement needs four things, not one:

1. A line in a fragment (`CONFIG_X=y`, or `# CONFIG_X is not set`).
2. The fragment listed in every preset that should carry it. A fragment in no
   preset is a comment (`tests/test_fragment_wiring.py` catches orphans).
3. If a missing symbol would stop boot or break GreenBoost, an entry in
   `UBUNTU_BOOT_FLOOR_*` or `GREENBOOST_FLOOR_Y` in `hyphaed/kconfig.py`, so
   configure refuses to ship instead of discovering it at boot.
4. A test, when the failure was subtle.

Hardware-specific symbols (`CPU_SUP_AMD`, vendor drivers) are generated from the
detected `HardwareProfile`. Putting one in a hand-written fragment can leave
the other machine unbootable.

## 2. Requirements by what depends on them

### 2.1 Boot and GreenBoost floors (code-enforced)

| Symbol | Value | Needed by | Pinned in |
|---|---|---|---|
| `BLK_DEV_INITRD`, `RD_ZSTD` | y | Ubuntu 26.04 initramfs is zstd; without it, "cannot decompress initramfs" before userspace | 05 |
| `DEVTMPFS`, `DEVTMPFS_MOUNT` | y | /dev for the initramfs | 05 |
| `DMA_SHARED_BUFFER`, `DMABUF_HEAPS` | y | greenboost.ko (dma-buf heaps). There is no `CONFIG_DMA_BUF` in 7.x | 70, 21 |
| `HUGETLBFS`, `HUGETLB_PAGE`, `HUGETLB_PAGE_OPTIMIZE_VMEMMAP` | y | GreenBoost T2 allocator | 70 |
| `IOMMU_SUPPORT`, `IOMMU_API`, `EVENTFD` | y | greenboost.ko | 70 |
| `MODULES`, `MODULE_UNLOAD`, `MODVERSIONS` | y | every DKMS module below | 70 |
| `CPU_FREQ` + performance/powersave/userspace/ondemand/schedutil governors | y | GreenBoost profiles switch governors | 70 |

All verified present in 7.2.12. DKMS modules built per kernel: `greenboost`,
`nvidia`, `nvidia-fs`, `vmmon`, `vmnet`, `v4l2loopback`, `hid-xpadneo`
(`/var/lib/dkms`). They need matching headers and a MOK-signed build
(`MODULE_SIG=y`).

### 2.2 Block layer: kyber, bfq, mq-deadline (new fragment 44)

| Symbol | Value | Why |
|---|---|---|
| `MQ_IOSCHED_DEADLINE` | y | xanmod patches 0010 and 0011 tune it; fallback for SATA and USB |
| `MQ_IOSCHED_KYBER` | m | candidate for NVMe under a heavy writer (perf A/B `nvme_sched`) |
| `IOSCHED_BFQ` | m | the USB/spinning external drives, not the NVMe |

Also verified present and unpinned: `BLK_WBT`, `BLK_WBT_MQ`, `BLK_CGROUP_IOCOST`,
`BLK_DEV_THROTTLING`, `NVME_MULTIPATH`, `NVME_HWMON`. `BLK_DEV_NVME` is a module,
so the initramfs must carry it (it does today).

Kyber was never missing. It was built as `kyber-iosched.ko` and not loaded,
and `/sys/block/*/queue/scheduler` lists only loaded schedulers. Writing
`kyber` to that file autoloads the module. Fragment 44 exists so a base-config
change can't drop it silently. NVMe still boots with `none`; the default
changes only if `perf_tune.py ab nvme_sched` shows a win.

### 2.3 CPU scheduling and power

| Symbol | Value | Why | Runtime half |
|---|---|---|---|
| `PREEMPT`, `PREEMPT_DYNAMIC`, `HZ_1000` | y | desktop and streaming latency | 10, 11 |
| `SCHED_BORE`, `SCHED_CORE`, `SCHED_AUTOGROUP`, `SCHED_MM_CID` | y | BORE patch 0001, core scheduling | 12 |
| `SCHED_CLASS_EXT` | y | sched_ext. `hyphaed-scx.service` runs `scx_bpfland` 1.1.2 right now | 12 |
| `BPF_SYSCALL`, `BPF_JIT`, `BPF_JIT_ALWAYS_ON`, `DEBUG_INFO_BTF`, `DEBUG_INFO_BTF_MODULES` | y | sched_ext loaders and CO-RE need BTF | 42 |
| `SCHED_MC`, `SCHED_CLUSTER`, `SCHED_SMT` | y | hybrid P/E topology, E-core L2 clusters | 80 |
| `SCHED_MC_PRIO` | y | this is ITMT; there are no `INTEL_ITMT` or `INTEL_THREAD_DIRECTOR` symbols | 80 |
| `INTEL_HFI_THERMAL` | y | the HFI driver | 80 |
| `X86_INTEL_PSTATE` | y | `intel_pstate=active` on the cmdline | 13, 80, 70 |
| `CPU_FREQ_DEFAULT_GOV_SCHEDUTIL`, `ENERGY_MODEL` | y | | 13 |
| `NO_HZ_FULL`, `RCU_NOCB_CPU` | y | cmdline `nohz_full=4-7`, `rcu_nocbs=16-31` do nothing without them | 11 |

Verified. ITMT's switch is debugfs-only in 7.2 (`sched_itmt_enabled` is not the
procfs knob), and ITMT is on by default.

### 2.4 Memory

| Symbol | Value | Why |
|---|---|---|
| `TRANSPARENT_HUGEPAGE`, `_MADVISE` | y | compile-time default is madvise; the cmdline `transparent_hugepage=always` overrides it at boot |
| `TRANSPARENT_HUGEPAGE_ALWAYS` | n | see above, deliberate |
| `LRU_GEN`, `LRU_GEN_ENABLED`, `LRU_GEN_STATS` | y | MGLRU |
| `NUMA_BALANCING_DEFAULT_ENABLED` | n | one NUMA node; cmdline also has `numa_balancing=disable` |
| `ZSWAP`, `ZSWAP_DEFAULT_ON`, zstd default | y | zswap on by default |
| `ZRAM` (m), `ZRAM_DEF_COMP_ZSTD`, `ZRAM_WRITEBACK` | | zram 8G swap is in use |
| `DAMON`, `_VADDR`, `_PADDR`, `_SYSFS`, `_STAT` | y | access monitoring; `DAMON_RECLAIM` and `DAMON_LRU_SORT` stay off |
| `PSI` | y | GreenBoost orchestrator reads pressure stalls |
| `HMM_MIRROR`, `DEVICE_PRIVATE`, `ZONE_DEVICE`, `PCI_P2PDMA`, `USERFAULTFD`, `UDMABUF` | y | nvidia-uvm managed memory, GPU to NVMe P2P, GreenBoost T3 |
| `PCI_REALLOC_ENABLE_AUTO` | y | resizable BAR; a 16 GB prefetchable BAR on the 12 GB card was seen live |

Per-size (mTHP) controls exist at runtime (`hugepages-64kB` etc., default
`never`); no config change is needed to use them.

### 2.5 GPU and NVIDIA

| Symbol | Value | Why |
|---|---|---|
| `X86_KERNEL_IBT` | n (unset) | the NVIDIA open module lacks ENDBR; with IBT on, GPU init faults and the screen stays black at boot. A deliberate security trade, set in fragment 60 and verified in the built config |
| `DRM`, `DRM_KMS_HELPER`, `DRM_FBDEV_EMULATION`, `FRAMEBUFFER_CONSOLE` | y | NVIDIA + GNOME/Wayland plumbing |
| `DRM_NOUVEAU` | n | nouveau stays out (61, 62) |
| `INPUT_JOYDEV`, `HID_STEAM`, `HID_NINTENDO`, `HID_PLAYSTATION` | m | controllers |
| `NTSYNC` | m | Proton sync primitive (43) |

Cmdline half: `nvidia-drm.modeset=1`, `nvidia.NVreg_PreserveVideoMemoryAllocations=1`,
`nvidia.NVreg_EnableGpuFirmware=1`, `pcie_aspm.policy=performance`.

### 2.6 Virtualisation (VMware Windows 11 guest, QEMU/KVM macOS guest)

| Symbol | Value | Why |
|---|---|---|
| `KVM`, `KVM_INTEL` | m | the macOS guest (`/home/ferran/vmware/OSX-KVM`) |
| `VHOST_NET`, `VHOST_VSOCK`, `VSOCKETS`, `TUN`, `TAP`, `MACVTAP` | m / y | guest networking (`tapmac0`) |
| `VFIO`, `VFIO_PCI`, `VFIO_IOMMU_TYPE1` | m | passthrough |
| `INTEL_IOMMU`, `INTEL_IOMMU_DEFAULT_ON`, `INTEL_IOMMU_SVM` | y | with cmdline `intel_iommu=on,igfx_off iommu=pt` |
| `VMWARE_VMCI`, `PARAVIRT`, `KVM_GUEST` | m / y | VMware and paravirt |

Runtime half: `/etc/modprobe.d/kvm.conf` sets `kvm_intel nested=1`,
`emulate_invalid_guest_state=0`, `kvm ignore_msrs=1 report_ignored_msrs=0`.
`vmmon` and `vmnet` come from `vmware-host-modules` through DKMS. The macOS
guest needs the CPU model `Skylake-Client` (OSX-KVM), not `-cpu host`.

### 2.6b Containers, networking and the firewall tooling

All verified present:

| Symbol | Value | Why |
|---|---|---|
| `CGROUPS`, `MEMCG`, `CGROUP_PIDS`, `CGROUP_BPF`, `CGROUP_SCHED`, `USER_NS` | y | Docker, systemd |
| `OVERLAY_FS` (m), `SQUASHFS` (y) | | Docker and snap (05) |
| `BRIDGE`, `VETH` | m | Docker bridges (`br-*`) |
| `NETFILTER`, `NF_CONNTRACK`, `NF_NAT`, `NF_TABLES`, `NF_TABLES_INET`, `NFT_CT`, `NFT_NAT`, `NFT_MASQ`, `NFT_FIB`, `NFT_LIMIT`, `NFT_LOG`, `NFT_REJECT` | y / m | ufw logging, and the CrowdSec nftables bouncer (verified live: its `ip crowdsec` table filled with a timed entry) |
| `IP_SET`, `IP_SET_HASH_IP`, `IP_SET_HASH_NET` | m | set-based blocklists |
| `TCP_CONG_BBR`, `DEFAULT_TCP_CONG="bbr"` | y | 50 |
| `NET_SCH_FQ_CODEL` (default), `NET_SCH_FQ`, `NET_SCH_CAKE`, `NET_SCH_HTB` | y / m | qdisc choices for Sunshine streaming |
| `TUN`, `WIREGUARD` | y / m | Tailscale uses `tun`; WireGuard is there if a VPN comes back |
| `MPTCP` | y | 50 |

The firewall and Docker/VMware forwarding are not tuned by the perf script and
must keep working across any config change.

### 2.7 Security

| Symbol | Value | Why |
|---|---|---|
| all `MITIGATION_*` that exist for this CPU | y | project rule: mitigations stay on at Kconfig and cmdline; `grub.py` refuses `mitigations=off` |
| `CRYPTO_USER_API_AEAD` | n | Copy Fail (CVE-2026-31431), defence in depth |
| `BPF_LSM` and `CONFIG_LSM` containing `bpf` | y / string | the symbol alone does nothing. Built: `landlock,lockdown,yama,integrity,apparmor,bpf` |
| `SECURITY_APPARMOR`, `DEFAULT_SECURITY_APPARMOR` | y | AppArmor, plus patches 0024 and 0028 (unprivileged userns restriction) |
| `MODULE_SIG` | y | DKMS modules are signed with the MOK key |
| `CRASH_DUMP`, `KEXEC`, `KEXEC_FILE`, `PROC_VMCORE` | y | kdump-tools is enabled |

This CPU's reported status (verified): spectre_v2 mitigated (eIBRS, BHI_DIS_S),
spec_store_bypass, vmscape and reg_file_data_sampling mitigated, spectre_v1
mitigated; meltdown, mds, l1tf, retbleed, gds, tsa, srbds, mmio_stale_data not
affected.

## 3. What config alone doesn't deliver

| Kernel feature | Config | Also needed at runtime |
|---|---|---|
| THP always | `TRANSPARENT_HUGEPAGE` | cmdline `transparent_hugepage=always` (99-greenboost.cfg) |
| tickless P-cores | `NO_HZ_FULL` | `nohz_full=4-7` |
| RCU offload on E-cores | `RCU_NOCB_CPU` | `rcu_nocbs=16-31` |
| IRQ layout | none | `irqaffinity=0-3,8-15` (irqbalance stays off) |
| BPF LSM | `BPF_LSM` | `bpf` inside `CONFIG_LSM` |
| sched_ext | `SCHED_CLASS_EXT` | `hyphaed-scx.service` starts `scx_bpfland` |
| kyber | `MQ_IOSCHED_KYBER=m` | a write to `queue/scheduler` (autoloads), or a tmpfiles line via `perf_tune.py keep nvme_sched` |
| GPU power | none | `nvidia-gaming-power.service` pins 300 W |

The GRUB cmdline is composed by `hyphaed/grub.py` into
`/etc/default/grub.d/90-hyphaed.cfg`; GreenBoost writes `99-greenboost.cfg`.
Never edit `/etc/default/grub` directly.

## 4. Fragment lines that don't do what they say

Found by comparing 317 fragment lines in the 22 `gaming-ai-vm` fragments to the
built config. The mismatches are explained; none is a security gap.

| Fragment line | Built | Why |
|---|---|---|
| `61: DRM_I915_GVT=n` | y | hidden `bool` with no prompt; only a `select` sets it, so the line can't act. The 14900KF has no iGPU anyway |
| `82: GAMEPORT=n` | m | selected by the joystick drivers (`select GAMEPORT`); a Kconfig select beats `=n`. Harmless |
| `41: KVM_AMD=m`, `20: MITIGATION_SRSO`, `MITIGATION_UNRET_ENTRY` | absent | `CPU_SUP_AMD` is off in the Intel build (generated `90-machine-topology.config`). Correct on the desktop; the laptop preset builds them |
| `10: PREEMPT_NONE/VOLUNTARY=n`, `20: MITIGATION_GDS_FORCE=n`, `61`/`82` old DRM and legacy-bus lines | absent | those symbols no longer exist in 7.2. Stale, harmless |
| (no line) `MITIGATION_GDS` | unset | mainline defaults it on. This CPU reports gather_data_sampling "Not affected" |

## 5. Where the two machines differ

Desktop: Intel build, `CPU_SUP_AMD=n`, `intel_pstate`, `INTEL_HFI_THERMAL`,
`KVM_INTEL`. Laptop (`gaming-ai-laptop-amd`): `83-strixpoint-amd` and
`84-laptop` fragments, `KVM_AMD`, `amd_pstate`, AMD mitigations. Anything that
differs per machine is generated, never added to a shared fragment.

## 6. Decisions waiting on you

Nothing below is applied. Each lacks evidence or needs your call.

1. **Prune the stale and ineffective fragment lines in section 4.** One cleanup
   commit; no behaviour change.
2. **Pin `MITIGATION_GDS=y`.** Costs nothing on unaffected CPUs and matches the
   "all mitigations on" rule at Kconfig level.
3. **NVMe default scheduler `kyber`.** Only after `perf_tune.py ab nvme_sched`
   shows a win; the config already allows it.
4. **Lazy preemption as the build default.** Mainline defaults to
   `PREEMPT_LAZY` on x86; fragment 10 pins full. `perf_tune.py ab preempt_lazy`
   switches it live (`/sys/kernel/debug/sched/preempt`); only a measured win
   justifies changing the fragment, or adding `preempt=lazy` to the cmdline.
5. **`MQ_IOSCHED_KYBER=y` instead of `m`.** Only if a udev rule must pick it
   before the root filesystem is mounted. Nothing needs that today.

## 7. Checklist for every build

```bash
# 1. After editing a fragment: confirm the symbol exists and nothing selects it away
grep -rE "^[[:space:]]*(menu)?config[[:space:]]+FOO\b" build/linux-*/

# 2. Merge and validate (floors run here)
python -m hyphaed --phase configure
grep -E 'CONFIG_MQ_IOSCHED_KYBER|CONFIG_IOSCHED_BFQ' build/linux-*/.config

# 3. The phase cache is state-based. After a .config change, invalidate it
#    (PersistedState.invalidate_from("build", ORDER)), or build silently does nothing.

# 4. Bump pipeline: audits every fragment line against the built .config
make bump VER=<x.y.z>

# 5. After the reboot
make post-boot-audit VER=<x.y.z>
zgrep -E 'CONFIG_MQ_IOSCHED_KYBER|CONFIG_SCHED_CLASS_EXT|CONFIG_BPF_LSM' /proc/config.gz /boot/config-$(uname -r) 2>/dev/null
cat /sys/kernel/sched_ext/root/ops
sudo modprobe kyber-iosched && cat /sys/block/nvme0n1/queue/scheduler    # expect: [none] mq-deadline kyber
python -m pytest tests/ -q
```

Two traps to remember: a Kconfig `select` beats a fragment's `=n`, so break the
selector and state `=n`; and a symbol in a fragment that no preset lists is a
comment.
