"""Parse and validate Linux kernel .config files.

Used post-merge to assert the GreenBoost floor flags survived the
fragment layering — otherwise greenboost.ko won't load on the resulting
kernel and we ship broken bits.
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from pathlib import Path

# CONFIG_<NAME>=<y|n|m|"string"|number> or `# CONFIG_<NAME> is not set`
_SET_RE = re.compile(r"^(CONFIG_[A-Z0-9_]+)=(.*)$")
_NOT_SET_RE = re.compile(r"^#\s*(CONFIG_[A-Z0-9_]+) is not set$")


def parse_config(path: Path) -> dict[str, str]:
    """Return {CONFIG_FOO: 'y'|'m'|'n'|<string-or-num>}."""
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        m = _SET_RE.match(line)
        if m:
            out[m.group(1)] = m.group(2)
            continue
        m = _NOT_SET_RE.match(line)
        if m:
            out[m.group(1)] = "n"
    return out


# GreenBoost requires these to be built-in (=y) or it can't load.
# CONFIG_DMA_BUF was renamed to CONFIG_DMA_SHARED_BUFFER in kernel 7.0;
# only the new name exists as a Kconfig option — old name is dropped by olddefconfig.
GREENBOOST_FLOOR_Y = {
    "CONFIG_DMA_SHARED_BUFFER",
    "CONFIG_HUGETLBFS",
    "CONFIG_HUGETLB_PAGE",
    "CONFIG_IOMMU_SUPPORT",
    "CONFIG_IOMMU_API",
    "CONFIG_EVENTFD",
    "CONFIG_MODULES",
    "CONFIG_MODULE_UNLOAD",
    "CONFIG_CPU_FREQ",
    "CONFIG_TRANSPARENT_HUGEPAGE",
}

# These can be =m (loadable) but must not be =n.
GREENBOOST_FLOOR_M_OR_Y = set()

# Must NOT be set.
GREENBOOST_FORBID = {
    # nothing currently — but we keep the slot for future regressions
}

# Ubuntu 26.04 LTS ("resolute") boot-critical flags.
# Any of these missing = kernel panic before userspace. Always enforced.
UBUNTU_BOOT_FLOOR_Y = {
    "CONFIG_BLK_DEV_INITRD",
    "CONFIG_RD_ZSTD",       # Ubuntu 26.04 dracut generates zstd initramfs
    "CONFIG_DEVTMPFS",
    "CONFIG_DEVTMPFS_MOUNT",
    "CONFIG_EFI",
    "CONFIG_EFI_STUB",
}

# These must be =y or =m; =n panics or breaks snaps at runtime.
UBUNTU_BOOT_FLOOR_M_OR_Y = {
    "CONFIG_OVERLAY_FS",    # snapd confinement + containers
    "CONFIG_SQUASHFS",      # snap .snap image mounting
}

# Vendor-specific IOMMU requirements (selected at validate() call time).
VENDOR_REQUIRED_Y: dict[str, set[str]] = {
    "intel": {"CONFIG_INTEL_IOMMU"},
    "amd":   {"CONFIG_AMD_IOMMU"},
}

# General hyphaed sanity (preset-agnostic).
HYPHAED_REQUIRED_Y = {
    "CONFIG_BPF_SYSCALL",
    "CONFIG_IO_URING",
    # AI/GPU memory substrate (NVIDIA-UVM HMM, P2P DMA, dma-buf heaps) — ships
    # =y in the Ubuntu base config; enforced here so nothing can silently
    # demote it, even on presets that don't include 21-ai-gpu-mem.config.
    "CONFIG_HMM_MIRROR",
    "CONFIG_DEVICE_PRIVATE",
    "CONFIG_ZONE_DEVICE",
    "CONFIG_PCI_P2PDMA",
    "CONFIG_DMABUF_HEAPS",
    "CONFIG_USERFAULTFD",
    "CONFIG_PSI",
    # eBPF observability substrate for greenboost_all/greenboost's
    # ebpf/gb_trace.bpf.c tracer (CO-RE via /sys/kernel/btf/vmlinux) and any
    # other CO-RE BPF program. Silently absent from 7.1.2-hyphaed (verified
    # 2026-07-09: /sys/kernel/btf/vmlinux didn't exist on the running
    # kernel, so `bpftool btf dump` had nothing to dump and the tracer could
    # never build) — CONFIG_BPF_SYSCALL alone is not sufficient, BTF
    # generation needs the pahole DWARF→BTF pass, which needs debug info
    # present at compile time even though vmlinux itself ships stripped.
    "CONFIG_DEBUG_INFO_BTF",
    "CONFIG_DEBUG_INFO",
    # MGLRU + NUMA + huge-page substrate for AI inference workloads (audited
    # 2026-07-09 against CachyOS/Xanmod/TKG patch catalogs + upstream 7.1
    # Kconfig — these are all upstream, no patch needed, just floor
    # enforcement). Confirmed present as real symbols in the CachyOS 7.1
    # reference .config (linux-cachyos-bore/config). MGLRU cuts page-reclaim
    # stalls when loading large model weights; cgroup hugetlb + read-only THP
    # for fs back safetensors/GGUF mmap paths.
    "CONFIG_LRU_GEN",
    "CONFIG_LRU_GEN_ENABLED",
    # This floor runs identically for every hyphaed build, including any
    # future remote GreenBoost feeder/cluster-node build — NUMA_BALANCING=y
    # must stay compiled in for whichever box needs its own multi-node
    # topology. This box itself (i9-14900KF, single socket) has exactly 1
    # NUMA node (confirmed via `numactl --hardware`, 2026-07-30), so it gets
    # no direct benefit from the mechanism locally — that's why the runtime
    # default is off (see next comment), not a contradiction with this floor.
    "CONFIG_NUMA_BALANCING",
    # NOT _DEFAULT_ENABLED — configs/fragments/30-mm-thp-mglru.config sets
    # that to =n deliberately (auto NUMA migration adds jitter workloads
    # other than AI inference care about); the mechanism being compiled in
    # (NUMA_BALANCING=y) is the actual floor requirement, forcing the
    # runtime default on is a separate tuning call that fragment already
    # makes correctly. Reverted 2026-07-09 after a real build run failed
    # validation over this.
    "CONFIG_CGROUP_HUGETLB",
    "CONFIG_READ_ONLY_THP_FOR_FS",
    # sched_ext substrate — required for CachyOS BORE (already patched in)
    # and any future scx scheduler swap via `make scx-run`.
    "CONFIG_SCHED_CLASS_EXT",
}

# These must be loadable or built-in; =n is not acceptable.
# KVM is normally shipped as =m on Ubuntu and works fine as a module.
HYPHAED_REQUIRED_M_OR_Y = {
    "CONFIG_KVM",
}


@dataclass
class ValidationResult:
    missing: list[str]      # required =y but resolved to !=y
    forbidden: list[str]    # appeared but should not have
    warnings: list[str]     # informational

    @property
    def ok(self) -> bool:
        return not self.missing and not self.forbidden


def validate(config: dict[str, str], *, with_greenboost: bool = True, cpu_vendor: str = "unknown") -> ValidationResult:
    missing: list[str] = []
    forbidden: list[str] = []
    warnings: list[str] = []

    # Ubuntu 26.04 boot-critical — always checked, regardless of preset.
    for key in sorted(UBUNTU_BOOT_FLOOR_Y):
        val = config.get(key)
        if val != "y":
            missing.append(f"{key} = {val!r} (expected y)  ← Ubuntu 26.04 boot requirement")

    for key in sorted(UBUNTU_BOOT_FLOOR_M_OR_Y):
        val = config.get(key)
        if val not in ("y", "m"):
            missing.append(f"{key} = {val!r} (expected y or m)  ← Ubuntu 26.04 boot requirement")

    required_y = set(HYPHAED_REQUIRED_Y)
    required_y |= VENDOR_REQUIRED_Y.get(cpu_vendor, set())
    if with_greenboost:
        required_y |= GREENBOOST_FLOOR_Y

    for key in sorted(required_y):
        val = config.get(key)
        if val != "y":
            missing.append(f"{key} = {val!r} (expected y)")

    for key in sorted(HYPHAED_REQUIRED_M_OR_Y):
        val = config.get(key)
        if val not in ("y", "m"):
            missing.append(f"{key} = {val!r} (expected y or m)")

    if with_greenboost:
        for key in sorted(GREENBOOST_FLOOR_M_OR_Y):
            val = config.get(key)
            if val not in ("y", "m"):
                missing.append(f"{key} = {val!r} (expected y or m)")

    for key in sorted(GREENBOOST_FORBID):
        if config.get(key) and config[key] != "n":
            forbidden.append(f"{key} = {config[key]!r} (should not be set)")

    # Soft warnings
    if config.get("CONFIG_DEBUG_INFO") == "y" and config.get("CONFIG_DEBUG_INFO_REDUCED") != "y":
        warnings.append(
            "CONFIG_DEBUG_INFO=y without CONFIG_DEBUG_INFO_REDUCED — modules .deb will be ~600 MB"
        )
    if config.get("CONFIG_PREEMPT_VOLUNTARY") == "y" and config.get("CONFIG_PREEMPT") != "y":
        warnings.append("preemption is voluntary, not full — gaming latency may suffer")

    return ValidationResult(missing=missing, forbidden=forbidden, warnings=warnings)
