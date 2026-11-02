# What the kernel can and can't do for GreenBoost's tok/s on the RTX 5070 (2026-10-30)

Read-only study. A local sweep of `~/Dev/greenboost_all/greenboost` plus web research (NVIDIA open-gpu-kernel-modules, llama.cpp, EMOGI, lore). Nothing was built, loaded on the GPU or changed. Every percentage below is an estimate, not a measurement, and is marked that way. It builds on `greenboost-architecture-map-2026-10-28.md` and `../greenboost-pcie-plan-2026-10-28.md`, which it doesn't repeat.

## There is no kernel patch here that moves tok/s

We looked for one and didn't find one. Decode on this box is bounded by two pipes the kernel can't widen: the PCIe link, which is Gen4 x16 under load (~24 GB/s) and carries the spilled weights, and dual-channel DDR4-3600 (57.6 GB/s peak, ~45 GB/s assumed in practice), which feeds the CPU-resident experts and also serves the GPU's zero-copy reads. The best measured run, `--n-cpu-moe 28` at 49.7 tok/s (`workflow/known-issues.md:27-31` in GreenBoost), already sits close to the DDR4 ceiling for that split by our own arithmetic (~0.85 GB of expert bytes per token at 45 GB/s is about 53 tok/s). All the levers below together are worth maybe 5 to 20% on decode, and most of that is runtime settings or the NVIDIA module, not Linux.

## Things that look like levers and aren't

- **IOMMU.** All 15 groups are `identity` (passthrough). There is no translation cost to remove.
- **THP or hugetlb for the GPU's view of spilled weights.** The NVIDIA driver describes `cuMemHostRegister`'d memory as non-contiguous 4 KiB pages (`osmemdesc.c`, `NV_MEMORY_NONCONTIGUOUS`) and the mapping code cuts the GPU page size down to that (`virt_mem_allocator_gm107.c`, ~line 730). Read from GitHub `main`; the local 615.71.09 RM is a binary blob, so we assume it behaves the same. Prediction to test: the 4k, 2m and 1g rows of GreenBoost's `gb_pathbench.cu` section 3b come out equal within noise. If they do, the shim's `MADV_HUGEPAGE` (shim line 1631) only helps load time. 1 GiB hugetlb and mTHP sizes are dead for the same reason.
- **`pci=pcie_bus_perf`.** It makes MRRS equal MPS (`drivers/pci/probe.c:2981`), which drops the GPU's 512 B read request to 256 B, and zero-copy SM reads are 128 B or less anyway. `grub.py` keeps it off by default (`enable_pcie_bus_perf=False`) and `docs/tasks_patches.md:374` already says to leave it. This confirms that call with a code-level reason.
- **MPS, ASPM, Gen5.** MPS is at the root port's 256 B maximum, `pcie_aspm.policy=[performance]`, the GPU's `l1_aspm` is 0, and the board link is Gen4.
- **Memlock.** `ulimit -l` is 8 MiB, but the NVIDIA driver pins with `pin_user_pages(FOLL_LONGTERM)` and doesn't charge it (`os-mlock.c:229-253`).
- **greenboost.ko on Blackwell.** `cudaImportExternalMemory(OpaqueFd)` isn't supported on CC 12 (shim `:3378`). That limit is in closed CUDA code, so no kernel patch lifts it.
- **0019 and 0020.** Neither touches weights on this card (kernel module path count is 0). They cost carry on every bump and buy zero tok/s. Whether to park them in `series.experimental` is your call; nothing was changed.

## What is actually worth testing, in order

Each of these needs a measured before/after before anything ships, per the evidence gate. Run them with the GPU idle, after reading the GreenBoost contract docs.

| # | Lever | Owner | Estimate | A/B |
|---|---|---|---|---|
| 1 | One llama thread per P-core: `-t 8 -C 0x5555 --cpu-strict 1`, and give `scx_bpfland` a P-core primary domain. Today `llama-server` gets `--threads 16` with no affinity. | GreenBoost gb-synapse flags; `hyphaed-scx` unit here | 0 to +15% decode (guess; a fork measured +13% on other hardware) | `llama-bench -p 0 -n 128 -r 5`, alternating arms, same boot; `ps -eLo psr,comm` for where threads land; once with scx stopped |
| 2 | Keep CPU-side expert weights in ordinary THP memory: `GGML_CUDA_NO_PINNED=1` / `--no-host`. llama.cpp puts them in `cudaMallocHost` memory, which the driver maps into the CPU one 4 KiB page at a time (`nv-mmap.c:474,513`), so THP can't apply. | GreenBoost | +3 to +11% decode (one Cascade Lake fork measured +11.5%); prefill may drop | decode (`-p 0 -n 128`) and prefill (`-p 512 -n 0`) separately; `perf stat -e cpu_core/dtlb_load_misses.walk_active/`; `AnonHugePages` in `smaps_rollup` |
| 3 | `vm.compaction_proactiveness` reads 0 live. `95-greenboost-t2.conf:21` and `90-invest-memory.conf` both set 20; something resets it. It is not the investing desk: `33-zswap-tune.sh` sets 20, and `/etc/invest-owner/33-previous.conf` is only its rollback snapshot of the already-drifted 0 (ADR-105 in that repo). The proc file was last written 2026-10-29 23:15:48.99, inside a `greenboost_setup.sh` sudo run (started 23:14:32); unproven. Only 715 free 2 MiB blocks, and compaction succeeds 4.5% of the time. | system config, report only | enabler for #2, no tok/s of its own | `/proc/buddyinfo` order 9 and `AnonHugePages` after model load, with and without |
| 4 | NVIDIA module patch P1 (below), only if #2 wins | new DKMS patch, experimental | #2's gain without its prefill cost | `KernelPageSize: 2048 kB` on the `/dev/nvidia*` mappings |
| 5 | `NVreg_RegistryDwords=RmEnableLargePageSizeSysmemDefault=1` plus the shim using `cuMemHostAlloc` for 2 MiB GPU mappings (`nv.c:3485`, off by default) | GreenBoost shim + modprobe.d | unknown; nobody has measured whether 4 KiB GPU mappings limit zero-copy reads | `gb_pathbench` SM-read kernel, regkey on/off vs `cuMemHostRegister`, 1 GiB, 5 runs |

The `perf_tune.py ab` harness is a fio disk benchmark; it can't carry these, so they're procedures here rather than new knobs.

## Two new patches, both for the NVIDIA open module, neither for Linux

We couldn't find a Linux mm, PCI or IOMMU change whose mechanism reaches either bottleneck.

**P1, 2 MiB CPU mappings for pinned host memory.** In `nvidia_mmap_sysmem()` (`nv-mmap.c`), when the allocation is order 9, install pages from a fault handler with `vmf_insert_folio_pmd()` instead of a `vm_insert_page()` loop. That function is GPL-exported in this tree (`mm/huge_memory.c:1733,1748`) and only DAX uses it today; we haven't compiled the change, and the module is dual MIT/GPL so it should be allowed to call it. Nobody has done it because pinned memory exists for DMA, and CPU TLB reach only matters when the CPU streams gigabytes out of it per token, which is the new CPU-expert case. Upstreamable only with #2's numbers.

**P2, 2 MiB GPU mappings for registered memory.** In `os_lock_user_pages()`, if the pinned pages form aligned 2 MiB runs, build the descriptor at 2 MiB granularity (the dynamic-granularity path in `osmemdesc.c` already exists). Worth writing only if lever 5 shows GPU translation, not the PCIe link, limits reads. If lever 5 shows nothing, drop it.

## Where GreenBoost and hyphaed fight over the same setting

- **THP mode.** `grub.py:54` and fragment 30 say `madvise`; GreenBoost writes `always` on the cmdline (`setup.sh:7557`). GreenBoost wins live because `grub.py` skips keys already set.
- **`watermark_scale_factor`, `swappiness`.** `95-greenboost-t2.conf` sets both, and `gb_control.py:311-313` can rewrite them when `GB_OS_TUNE=1`.
- **Governor and EPP.** Written by GreenBoost's `cpu-perf.service` (`setup.sh:3492`), by `gb_control` while inference runs, and by `perf_tune` M2. EPP is already `performance`, so an M2 A/B shows nothing unless the GreenBoost writers are off.
- **IRQ affinity.** `gb_control.py:680` rewrites `/proc/irq/*/smp_affinity_list` at runtime and can override `irqaffinity=0-3,8-15`.
- **`nohz_full=4-7`, `rcu_nocbs=16-31`.** GreenBoost writes them; `grub.py:286-296` skips those keys. Unpinned llama threads rarely sit on 4-7, so the isolation buys nothing. An A/B removing them is expected to be within ±2%.
- **Mitigations.** GreenBoost writes `mitigations=off`; `grub.py:298-302` strips it and forces `auto`.

## What we couldn't determine

- The GPU-side page size for `cuMemHostRegister` memory on the actual 615.71.09 blob (we read `main`).
- What the T2 spill got in practice: no `llama-server` was running, and the 2 MiB option is off by default, so its log line has never fired.
- The GPU's relaxed-ordering setting (needs root).
- Whether `GB_OS_TUNE` is on, and whether `_pcore_threads()` returns 8 or 16.
- Why the live daily tok/s median fell from about 50 to about 30. Nobody has investigated it, and it's a bigger number than any lever above.
- Whether `vmf_insert_folio_pmd()` accepts the driver's compound pages. P1 hasn't been compiled.

Sources: `/usr/src/nvidia-615.71.09/nvidia/{os-mlock.c,nv-mmap.c,nv-vm.c,nv.c}`; `build/linux-7.2.12/{drivers/pci/probe.c,mm/huge_memory.c}`; `~/Dev/llama.cpp/src/llama-model.cpp`; [osmemdesc.c](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/main/src/nvidia/arch/nvalloc/unix/src/osmemdesc.c), [virt_mem_allocator_gm107.c](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/main/src/nvidia/src/kernel/gpu/mem_mgr/arch/maxwell/virt_mem_allocator_gm107.c), [nvrm_registry.h](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/main/src/nvidia/interface/nvrm_registry.h), [gamozolabs/llama.cpp](https://github.com/gamozolabs/llama.cpp), [EMOGI](https://arxiv.org/abs/2006.06890).
