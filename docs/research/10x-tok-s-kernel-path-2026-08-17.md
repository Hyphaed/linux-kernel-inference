# Is there a kernel path to 10x tok/s? — grounded analysis (2026-08-17)

**Question asked:** evolve the Linux kernel to reach ~10x local-inference tok/s,
keeping GreenBoost in consideration.

**Answer, stated up front:** the 10x is real and reachable, but it is not in the
kernel. It is in *bytes moved across PCIe*. The kernel-side work that could
attack it is **already done and enabled on this machine**. This document shows
the arithmetic, what the papers actually say once read, and what remains worth
building.

Unlike `local-inference-sources-2026-08-17.md`, the papers cited here **were
fetched and read** (PDF → markdown via docling). Where a paper's own caveats
undercut its headline number, that is recorded.

---

## 1. The arithmetic that decides everything

Measured on this box (`nvidia-smi`, 2026-08-17):

| Quantity | Value |
|---|---|
| GPU | RTX 5070, **12227 MiB** VRAM, ~672 GB/s |
| PCIe link | **Gen4 ×16** (`pcie.link.gen.max=4`) → ~25 GB/s effective |
| Host RAM | 64 GB DDR4 (~40 GB/s real, dual channel) |
| Default model | Qwen3.6-27B Q4_K_M ≈ **16.2 GB** — does not fit |
| Measured headroom | 1.8 GB free at rest (`vram-residency-baseline.py`, 2026-08-10) |

Decode is memory-bandwidth-bound: every token reads the whole weight set once.

```
resident portion :  10.0 GB / 672 GB/s  =   14.9 ms
offloaded portion:   6.2 GB /  25 GB/s  =  248.0 ms   <-- 94% of the time
                                          --------
                                            263 ms/token  ->  3.8 tok/s

fully resident   :  16.2 GB / 672 GB/s  =   24.1 ms      ->  41 tok/s
```

**3.8 → 41 tok/s is 10.8x, and it is entirely the PCIe term.** Every kernel
subsystem in scope — scheduler, NUMA, cpumask width, page migration — operates
inside the 14.9 ms half. Even reducing that half to *zero* yields 263 → 248 ms,
a **5.7% gain**. That is the ceiling on kernel-side work for this workload, and
it is a hard bound from the link speed, not an implementation detail.

**Corollary:** anything that reduces bytes crossing PCIe converts almost 1:1
into tok/s. A 2:1 lossless compression of offloaded weights takes the 248 ms
term to 124 ms → 6.9 tok/s (1.8x). Full residency is the only route to 10x.

---

## 2. What the papers say once actually read

### `2204.02974` — Oversubscription Management in CPU-GPU Unified Memory

Read in full. Proposes an access-pattern classifier + Transformer page
predictor driving prefetch/pre-eviction; reports 64.4% fewer pages thrashed and
1.52x/3.66x IPC at 125%/150% oversubscription.

**Disqualifying details found on reading:**
- It is a **GPGPU-Sim simulation** ("we use a GPGPU-Sim extension", §V), not
  real hardware. The IPC figures are simulator IPC.
- The model is **fine-tuned every 50M instructions** — an online training loop
  in the memory-management hot path.
- It targets the **GMMU and the UVM runtime**. On Linux that is
  `nvidia-uvm.ko`, i.e. NVIDIA's driver — **not mainline Linux**. There is no
  mainline surface to patch.

**Verdict:** relevant to GreenBoost's *tier policy*, irrelevant as a kernel
patch. If pursued, the venue is `open-gpu-kernel-modules` or GreenBoost's own
tier manager.

### `2604.26968` — Predictive Multi-Tier Memory Management for KV Cache

Ranked #1 in the earlier survey on title alone. **It does not survive reading:**
- Headline throughput/TTFT numbers are **analytical projections**, self-declared:
  *"these cluster-scale projections are analytical and carry no error bars."*
  Only the 70–84% cache hit rates are measured (trace replay).
- **Patent-encumbered**: *"implementation details are withheld pending U.S.
  provisional patent applications."* Not reproducible.
- It optimises **KV cache**. Our default model is dense with **4 KV heads** —
  KV is not our bottleneck; weights are.
- Its DRAM tier assumes 204 GB/s (H100-class). **Ours is ~25 GB/s over PCIe
  Gen4** — our offload penalty is ~8x worse than its model assumes.

**Verdict:** reject for our workload. Useful only for its tier-latency table.

### `2508.06978` — SSD Offloading for MoE Weights *Considered Harmful*

Deliberately selected as counter-evidence to T3 (NVMe tier). Read.

Finding: offloading MoE weights to SSD raises **per-token energy by up to ~12x**
vs an HBM baseline, and *"techniques like prefetching effectively hide access
latency but cannot mitigate this fundamental energy penalty."* SSDs become
energy-viable only if Flash read energy improves ~10x.

**Critical nuance — this is an *energy* result, not a throughput result.** It
does not say T3 is bad for tok/s. It says T3 is expensive in joules, and that
GreenBoost's prefetching (which works, for latency) cannot fix that.

**Verdict for us — refines T3 rather than killing it:**
- **Desktop (`ncore`, wall power):** T3 stays justified. Throughput/capacity is
  the goal; energy is secondary.
- **Laptop (HP Omen, battery):** T3 is actively harmful. A mobile workstation
  paying 12x per-token energy for NVMe-resident experts will drain battery for
  capacity it could get by simply using a smaller model.
- **Actionable:** GreenBoost's tier policy should be **chassis-aware** —
  T3 enabled on the desktop, demoted or disabled on battery. This is a real,
  cheap change grounded in a read paper.

---

## 3. The kernel levers, checked against this machine

The one kernel mechanism that genuinely attacks the PCIe term is eliminating
the double crossing on the T3 path: NVMe → host bounce → GPU (two crossings)
versus NVMe → GPU directly via P2PDMA/GPUDirect Storage (one).

Verified on this box, 2026-08-17:

| Symbol / state | Status |
|---|---|
| `CONFIG_PCI_P2PDMA` | **=y** |
| `CONFIG_ZONE_DEVICE`, `CONFIG_HMM_MIRROR`, `CONFIG_DEVICE_PRIVATE` | **=y** |
| `CONFIG_DMA_SHARED_BUFFER`, `CONFIG_UDMABUF` | **=y** |
| `nvidia_fs` module | **loaded**, `/proc/driver/nvidia-fs/stats` live |
| `iommu=pt`, `transparent_hugepage=always` | set on cmdline |

**There is no kernel patch to write here — the path is already enabled.** The
2026-08-10 finding that `nvidia-fs.ko` was missing (GDS silently degrading to
POSIX compat, bytes crossing PCIe twice) has stayed fixed, and
`postinstall.py::_check_nvidia_fs()` guards it.

Remaining kernel-side items, with honest expected value:

| Lever | Expected gain | Note |
|---|---|---|
| PCIe MPS/MRRS tuning (`enable_pcie_bus_perf`) | ~1–3% of the PCIe term | TLP header overhead only; opt-in in `grub.py` already |
| `RT_GROUP_SCHED` off, `NR_CPUS=64`, vendor narrowing | sub-1% of decode | Done 2026-08-17. Worth having; not a throughput story |
| sched_ext BPF scheduler for offload threads | bounded by the 14.9 ms half → **<5.7%** | `CONFIG_SCHED_CLASS_EXT=y` already set; loadable with no rebuild |
| DAMON-driven host-tier policy | bounded by same ceiling | Mechanism compiled in (fragment 32); no policy wired |

---

## 4. Where the 10x actually is

Ranked by expected payoff, all outside the kernel:

1. **Residency.** Get the weights under ~11 GB. Q3_K_M ≈ 13.2 GB (still over),
   Q2_K ≈ 11.3 GB (fits, quality cost). Decisive and immediate.
2. **GreenBoost lossless compression** (`gb_moe.py`). Raises the effective
   residency ceiling — the one lever that buys capacity without quality loss.
   This is the project's real contribution and it targets the correct term.
3. **Patches 0019/0020 — compressed dma-buf descriptor.** Aimed correctly:
   fewer bytes on the wire, not faster wire. This is the kernel-adjacent work
   that *does* attack the 248 ms.
4. **Chassis-aware tiering** (from `2508.06978`): T3 on desktop, off on battery.

## 5. What would be dishonest to ship

Submitting kernel patches claiming 10x local-inference gains. Maintainers
measure. The bound in §1 is arithmetic from the PCIe link rate, and any
reviewer will derive it in minutes. Publishing a series that cannot reproduce
its claims would cost precisely the credibility that "make everyone trust and
develop our new code" depends on.

The defensible public contribution is the opposite: patches 0019/0020 with an
honest measured number for compressed-transfer byte reduction, plus the
chassis-aware energy finding backed by `2508.06978`. Smaller claim, real
evidence, reproducible — the combination that earns trust.
