# Memory speed, BIOS and memory-side OS levers on ncore (round 2, 2026-10-28)

Scope: ASRock B760M-ITX/D4 WiFi (BIOS 12.02), i9-14900KF, 2 DIMMs DDR4, RTX 5070 on Gen4 x16, Linux 7.2.12-hyphaed. Research only, nothing was changed. Tags: VERIFIED (read off this machine or a primary source), INFERENCE (arithmetic or reasoning from verified facts), UNVERIFIED (forum, secondary, or unknown).

Read first and not repeated here: `B760M-ITXD4_BIOS/BIOS_TUNING_AI_INFERENCE.md`, `bios_targets.py`, `pcie-gen5-platform-2026-10-28.md` (slot is Gen4 by design).

## 0. Read this before anything else: two earlier notes disagree

- The BIOS note (checked 2026-08-18, VERIFIED by that run) says the kit is Crucial Ballistix `BL32G36C16U4B.M16FB1`, 32 GB x 2, XMP 2.0 profile 1, configured 3600 MT/s, 16-18-18-38 at 1.35 V. The platform note (pcie-gen5, today) says the speed is UNKNOWN and assumes DDR4-3200. The BIOS note's evidence is the better one. Today's check on this machine could not repeat it because `dmidecode` needs root (`/sys/firmware/dmi/tables/DMI` is mode 0400 root). So: INFERENCE that it runs 3600 today, to be confirmed with the root command in section 3.
- Rank count is still unknown. A web search found no source for the rank or die of this exact SKU (UNVERIFIED; one review hints Micron 16Gb, rev unknown). 32 GB per stick of a 16Gb-die module is normally 2 ranks (16 chips x 16Gb = 32 GB), so INFERENCE: dual rank. `dmidecode -t memory` prints `Rank: 2` and settles it.
- The same BIOS note has a stale power-limit statement. It says "Auto resolves to 65 W / PL2 135 W" and recommends PL1 95 / PL2 190. Section 5 below says why that must be re-read before anyone trusts it.

## 1. How fast can this CPU and board run DDR4

| Fact | Value | Status |
|---|---|---|
| Intel official | "Up to DDR4 3200", 2 channels | VERIFIED (cited in pcie-gen5 note from Intel ARK) |
| ASRock manual | 1DPC 1R up to 5333+ OC, 1DPC 2R up to 4266+ OC, 3200 native, XMP 2.0 | VERIFIED (from the same note), but 5333 and 4266 are marketing OC ceilings |
| Typical Gear 1 ceiling on 12th to 14th gen DDR4 | 3600 easy, 3800 to 4000 chip-dependent, above 4000 needs a strong IMC | UNVERIFIED (forum anecdotes: TechPowerUp, hwbot, hardwareluxx; mostly Alder Lake) |
| Voltage seen for 4000 Gear 1 | VCCSA about 1.35 to 1.40 V, VDDQ about 1.4 V | UNVERIFIED anecdote |
| Dual rank Gear 1 vs single rank Gear 2 | Dual rank Gear 1 3200/3600 stayed ahead of single rank 4000/4400 Gear 2 on 12900K | UNVERIFIED secondary (TechPowerUp forum summary) |
| Dual rank vs single rank | Dual rank gives a small write and mixed-access gain, a small AIDA read loss, similar latency | UNVERIFIED (Igor's Lab on Alder Lake) |

Gear trade-off (INFERENCE from how gears work): Gear 1 runs the IMC at the DRAM clock, lowest latency, hardest on the controller above about 3800 to 4000. Gear 2 halves the IMC clock, allows 4200 to 4800 and more but adds latency and usually gives no more bandwidth for a streaming read, because the IMC rate then limits. For a bandwidth-bound consumer the realistic ceiling is about 3600 to 3800 Gear 1. The existing 3600 XMP is already at the sweet spot.

Stable range for two dual-rank 32 GB sticks: UNVERIFIED, no 14900K DDR4 dual-rank 2x32 data found. Reasonable expectation from the anecdotes: 3600 stable with XMP, 3733 to 3800 possible with VCCSA raised and hours of memory testing, 4000 Gear 1 unlikely on dual rank. A 2 DIMM ITX board has short, clean traces (INFERENCE: favourable for 2 DIMMs), but the board's own SPI flash has no recovery path (see section 4).

## 2. Bandwidth and latency numbers, and what the GPU leaves

- Theoretical (VERIFIED arithmetic): DDR4-3200 dual channel 51.2 GB/s, DDR4-3600 57.6 GB/s, DDR4-3800 60.8 GB/s.
- Measured STREAM or MLC on 14900K DDR4: none found. INFERENCE: read-heavy bandwidth is 80 to 90 percent of peak, so 3600 gives about 46 to 52 GB/s all-core read, copy and triad lower (about 40 to 46). Single-thread bandwidth is lower and needs several cores to saturate. Measure it (section 3).
- Latency: no 14900K DDR4 number found. UNVERIFIED expectation for DDR4-3600 CL16 Gear 1 on Raptor Lake: roughly 55 to 70 ns in AIDA64-style pointer chase. Quasarzone's 14900K test (4 x 8 GB SR DDR4-3600 G1 vs DDR5) found DDR4 lower latency than DDR5, but gave no ns in the text.
- Left for the CPU when the GPU pulls over PCIe (INFERENCE): the GPU reading at 25 GB/s takes about half of 51 GB/s (DDR4-3200) or 43 percent of 57.6 GB/s. CPU and GPU contend for the same two channels, so a CPU-expert decode plus a host-RAM-paged GPU path cannot both run at full speed. Leftover at 3600 about 20 to 25 GB/s theoretical, less after efficiency and contention. Each extra 400 MT/s adds about 6.4 GB/s peak, i.e. about one quarter of the GPU stream, which is the entire value of a memory overclock here.
- The BIOS note argues memory is five times faster than the 11 to 12 GB/s weight path. That holds when only the GPU path runs. It does not hold once CPU MoE experts run at the same time, which is the case this round is about. Memory speed is therefore not "buys nothing" under CPU-expert plus GPU-stream load, but its size is unmeasured.
- MoE CPU offload scales with RAM bandwidth (INFERENCE, plausible, widely stated: hardware-corner, llmhardware.io, Doctor-Shotgun guide). No published DDR4 speed sweep exists, so the gain from 3600 to 3800 is UNKNOWN, expected single-digit percent on CPU-resident expert decode.

## 3. Find out what runs now, without changing anything

### Without root (all tried on this machine today)

| Check | Result today | Notes |
|---|---|---|
| `cat /sys/class/dmi/id/{bios_version,board_name}` | 12.02, B760M-ITX/D4 WiFi | VERIFIED |
| `grep -m1 microcode /proc/cpuinfo` | 0x137 | VERIFIED. Loaded by the OS, the BIOS copy may be older (ASRock forum Feb 2026 says the board's shipped microcode was 0x12F, UNVERIFIED) |
| `lscpu`, `/sys/devices/system/edac/mc` | no memory controller listed | EDAC has no non-ECC DIMM info, dead end |
| `dmesg`, `/sys/firmware/dmi/tables` | root-only | dead end |
| `/usr/bin/stream` | is ImageMagick `stream`, NOT the STREAM benchmark | do not run it |
| `stress-ng --stream 8 --stream-l3-size 36M --stream-madvise hugepage --metrics-brief -t 10` | runs, reports bogo-ops only (about 1138 ops/s), not GB/s | usable only for relative A/B on a quiet machine, the box was loaded (load 4.4) |
| `perf` with `uncore_imc_free_running_0/data_read/` | failed: perf_event_paranoid=2 | needs root or `sysctl kernel.perf_event_paranoid=-1` (a system change, so not done). The events exist: data_read, data_write, data_total with scale in MiB |

Real STREAM, user-space, no root (not run yet, it loads all cores for seconds):
```
mkdir -p ~/stream && cd ~/stream
curl -O https://www.cs.virginia.edu/stream/FTP/Code/stream.c
gcc -O3 -march=native -fopenmp -mcmodel=medium -DSTREAM_ARRAY_SIZE=200000000 -DNTIMES=20 stream.c -o stream_c
OMP_NUM_THREADS=8 OMP_PROC_BIND=close OMP_PLACES="{0},{2},{4},{6},{8},{10},{12},{14}" ./stream_c   # 8 P cores one thread each
OMP_NUM_THREADS=16 ./stream_c
```
Run on an idle box (stop the llama server first or the numbers include it). Compile flags match the workstation rule. 200M doubles x 3 arrays is 4.8 GB.

### With root

```
sudo dmidecode -t memory | grep -E "Locator|Size|Rank|Speed|Part Number|Manufacturer|Voltage|Type Detail"
```
Expect: `Configured Memory Speed: 3600 MT/s` (the earlier `Speed: 2600` was a known ASRock SMBIOS misreport), `Rank: 2`, voltage 1.35.
```
sudo perf stat -a -e uncore_imc_free_running_0/data_read/,uncore_imc_free_running_0/data_write/ -- sleep 10
```
This gives real DRAM traffic in MiB, with the box under the inference load. It is the best single number for "how much of the 51 to 58 GB/s do we actually use": run it during decode with and without the GPU-stream path. Free-running IMC counters only exist on some client parts, they are present here.
```
sudo modprobe msr; sudo ./mlc --idle_latency; sudo ./mlc --peak_injection_bandwidth; sudo ./mlc --loaded_latency
```
MLC (Intel, v3.12 on Intel's site, free download) needs root because it writes the prefetcher MSR; `-e` skips that (VERIFIED from Intel's page). Run `mlc --bandwidth_matrix` is meaningless on one NUMA node.
Optional: `sudo decode-dimms` needs `i2c-dev` and `ee1004` and SPD readable on the SMBus. Often works on Intel B760, ASRock boards sometimes hide SPD (UNVERIFIED).
Optional for die and timing: `sudo lshw -class memory`, and Typhoon Burner or Thaiphoon Burner on Windows (not available here).

## 4. BIOS items from the earlier notes: verified vs speculation, cost and risk

| Item (earlier note) | Status of the evidence | Cost and risk | Verdict |
|---|---|---|---|
| XMP 2.0 profile 1, 3600 CL16, Apply All Timing, ASRock Timing Optimization on | Measured 2026-08-18 (dmidecode) | none | ADOPT already done, re-confirm with root dmidecode |
| DRAM Gear Mode Auto to Gear 1 | The note itself says "may not be measurable". At 3600 Auto is almost certainly already Gear 1 (INFERENCE: Gear 2 at 3600 would be odd on this CPU) | If it does not POST: clear CMOS. The board has a single 128 Mb SPI and no Flashback or dual BIOS (from the note), and CMOS clear does not reflash it, so a bad BIOS is the unrecoverable case, a bad memory setting is not | MEASURE-FIRST: only read what Auto resolved to (`perf` or AIDA-style latency below 70 ns means Gear 1). Do not change it if it is already 1 |
| Raise frequency to 3733 to 4000 | No evidence for this CPU. Forum anecdotes only | Needs VCCSA/VDDQ raised on the IMC of a CPU with a known degradation history; crashes can look like Vmin shift and cost days of triage | REJECT for now. Revisit only after STREAM shows memory-bound CPU decode |
| PCIe ASPM off, package C-state limit C2 | Reasoned, not measured in the note ("low single digit % at best") | C2 raises idle power a little, none for stability | MEASURE-FIRST (A/B: same prompt, 5 runs) |
| Above 4G Decoding, Resizable BAR, VT-d on | Verified by the note | none | ADOPT already done |
| PL1 95 W, PL2 190 W (note) | The premise "Auto resolves to 65 W" was a runtime read, not an Intel profile | see section 5 | MEASURE-FIRST and read the real value first |
| Dynamic Memory Boost, Realtime Memory Frequency/Timing off | reasonable | none | ADOPT (keep off) |

## 5. Raptor Lake instability, microcode, power profiles

- 0x12B (Sept 2024) was called Intel's final Vmin shift fix, then Intel followed with 0x12F (VERIFIED, HotHardware, Intel community post, 2025). Intel still recommends the Intel Default Settings profile (VERIFIED, same sources). Chips already degraded are not repaired.
- Running microcode here is 0x137 (VERIFIED, `/proc/cpuinfo`). It came with Intel's 20260811 package, replacing 0x133 on RPL-S (VERIFIED secondary: cloudnews.tech, Red Hat changelog). Intel's own release note for the 0x137 contents was NOT found, so what it fixes is UNVERIFIED. Do not describe it as "the Vmin fix".
- Where 0x137 comes from on this box: the OS early loader (`patch_microcode.py` and the BIOS note say BIOS build 12.4 carries 0x137). The board's BIOS 12.02 microcode is UNVERIFIED (one ASRock forum post, Feb 2026, says the board's latest BIOS still carried 0x12F). The OS update covers the running kernel but the very early boot, and memory training, runs on the BIOS microcode. A BIOS update path is not offered without a programmer, so no action.
- Intel Default values (UNVERIFIED sources mixed): PL1 125 W for 14900K, PL2 253 W ("Performance" profile), Baseline PL2 188 W for the 13900K. Our note's claim that Auto is 65 W / 135 W is not a standard Intel profile for a 14900KF, it looks like an ASRock Auto result, possibly a degraded-voltage or "Intel Default" variant on this ITX. Read the running limits (no root): `grep . /sys/class/powercap/intel-rapl:0/constraint_*_power_limit_uw` and `constraint_*_name`. If they read 65 W and 135 W, the CPU is running below Intel's own spec.
- Safety judgement (INFERENCE): raising PL1/PL2 to 95/190 W stays under Intel's spec, so it does not push beyond what the part supports and does not affect Vmin shift, which is a voltage and temperature wear issue. The real risk on a Mini-ITX board is the cooler: the fan note (Silent to Performance) is a prerequisite, and a sustained 190 W burst on a small VRM is thermal, not instability. Memory overclocking (raising VCCSA) is the item that touches the same IMC and voltage rails that the degradation history involves. Nothing recommended here changes core voltage or ICCMAX. Do NOT touch Core Current Limit upward, do NOT use "Unlimited" or "Extreme" power profiles, do NOT disable the ASRock voltage protections.
- CPU temperature and long-run stability evidence needed before any change: `sensors` during a 30 minute decode, and a clean `memtester`/TM5 pass if memory is touched.

## 6. Memory-side OS levers (evidence from this machine)

Read off the machine today (all non-root VERIFIED):
- `thp enabled=always`, `defrag=defer+madvise`, `khugepaged defrag=1`, `max_ptes_none=511`, `pages_to_scan=4096`.
- vmstat since boot (uptime 2 h 34 m): `thp_fault_alloc 112793`, `thp_fault_fallback 118997` (fallback is 105 percent of alloc, so more than half, not "about half"), `thp_fault_fallback_charge 0`, `compact_stall 12228`, `compact_fail 6280`, `compact_success 5948`, `thp_collapse_alloc 1658`, `thp_collapse_alloc_failed 14`.
- `vm.min_free_kbytes=1048576` (1 GiB), `watermark_scale_factor=150`, `swappiness=10`, `compaction_proactiveness=0`, `page-cluster=0`, `zone_reclaim_mode=0`.
- Memory: 62.9 GB total, 50 GB used, 16.9 GB shared, 5.6 GB free, 12.9 GB available, `HugePages_Total 0`, `Mlocked 248 kB`. zram0 8 GiB zstd (priority 100): `mm_stat` orig 5.94 GB, compressed 1.82 GB, nearly full; `/swap.img` 64 GiB (prio 10), 6.3 GB in use. So 14.5 GB already swapped.

Interpretation:
- The kernel doc says `thp_fault_fallback` rises when a fault can't get or charge a huge page and uses small pages (VERIFIED, docs.kernel.org). With `defrag=defer+madvise`, non-madvised regions never stall to compact, they just fall back. So a 50 percent fallback is the designed cost of `defer`, not a fault. INFERENCE: fragmentation is real (14 MB-order free blocks are scarce with 5.6 GB free and 17 GB shmem), and the evidence it hurts is `compact_stall 12228` ( stalls are only counted for madvise regions in this mode, i.e. real inference processes that opted in).
- Phoronix (Jan 2026, kernel 6.18, llama.cpp CPU backend) measured `madvise` marginally faster than `always` on most prompt-processing tests and `always` ahead on one text-generation test, so differences are small and mixed (VERIFIED secondary). Expected gain from changing `enabled=always` to `madvise` on this workload: unknown, plausible within 0 to 3 percent. The kernel docs recommend `madvise` plus `MADV_HUGEPAGE` for performance-critical mappings.
- `min_free_kbytes` 1 GiB is large and already on the high side. It keeps a reserve so GPU DMA and pinned allocations don't fail, but with `available` at 12.9 GB it holds 8 percent of what is left. UNVERIFIED benefit either way. Leave.
- Interleaving: nothing to tune. 2 DIMMs in 2 channels is channel interleaved by the controller, one NUMA node means `numactl`/`zone_reclaim` are irrelevant (VERIFIED by the CLAUDE.md note and `zone_reclaim_mode=0`).
- Pinned memory: hugetlb reservation is unused (`HugePages_Total 0`). INFERENCE: reserving 1 GiB hugepages for a shim's pinned buffers would cut TLB misses and guarantees contiguity at boot, but it removes that RAM from the 12.9 GB available pool, and no 2026 benchmark of mmap/mlock with huge pages for llama.cpp was found. Do not do this until `perf stat -e dTLB-load-misses` shows a TLB problem.
- zram plus zswap: `zswap` is enabled (`Y`) AND a zram device holds 8 GiB, and zram is nearly full. INFERENCE: zswap in front of a zram swap compresses twice, the combination is generally discouraged because zswap then rejects pages into a compressed device. Under heavy pinned usage the swapped 14.5 GB is mostly cold pages pushed out by the pinned and shared memory. The first question is whether it is thrashing: `vmstat 5` columns si/so, and `grep -E "pswpin|pswpout|pgmajfault" /proc/vmstat` over 60 s. If si/so is near 0 the 14.5 GB is just parked cold pages and costs nothing.

## 7. Verdict table

| # | Lever | Verdict | Source | Expected gain on this box | Risk | Check or A/B | Revert |
|---|---|---|---|---|---|---|---|
| 1 | Confirm real memory config (speed, rank, voltage) | ADOPT (read only) | BIOS note 2026-08-18; dmidecode | none, it removes the 3200 vs 3600 doubt | none | `sudo dmidecode -t memory` | n/a |
| 2 | Measure real bandwidth with user-space STREAM, then IMC counters under load | ADOPT | STREAM, perf uncore_imc_free_running present here | tells whether memory speed matters at all | none | commands in section 3 | n/a |
| 3 | Measure whether CPU-expert decode is bandwidth-bound: sweep `--n-cpu-moe` and threads while watching `perf stat` IMC reads | MEASURE-FIRST | hardware-corner, Doctor-Shotgun guide (INFERENCE-level) | unknown | none | llama-bench sweep, record GB/s from IMC counter | n/a |
| 4 | New llama.cpp `--moe-cache-mib N` GPU expert cache (merged 2026-10-07, PR 29887) | MEASURE-FIRST | developersdigest.tech, PR cited | up to 1.6x to 2.2x on 24 to 32 GB GPUs; UNKNOWN on 12 GB (cache would be only a few GiB, hit rate low, misses cost PCIe); 3090 gained only 1 to 5 percent over tuned `--n-cpu-moe`; one post-merge regression halved speed | cache steals VRAM from the 10 GB budget | compare `-cmoe` vs `--n-cpu-moe K` vs `--moe-cache-mib 2000` at the same context, print hit rate | drop the flag |
| 5 | Read `intel-rapl` power limits, then decide on PL1/PL2 | MEASURE-FIRST | powercap sysfs | none for GPU decode; helps CPU-side prefill and CPU experts | thermal on ITX, not stability | `grep . /sys/class/powercap/intel-rapl:0/constraint_*`; `turbostat` during run | BIOS back to Auto |
| 6 | THP `always` to `madvise` | MEASURE-FIRST | kernel doc, Phoronix 6.18 | 0 to 3 percent, may be zero; reduces fallback noise only | madvise needs the app to ask, other apps lose THP | `echo madvise \| sudo tee /sys/kernel/mm/transparent_hugepage/enabled`, diff `/proc/vmstat` thp counters and tok/s, 5 runs each | echo always back |
| 7 | Check swap thrash (zswap on top of zram, zram full) | MEASURE-FIRST | local read | zero if si/so is 0; real if inference pages go to swap | none to read | `vmstat 5 12`; `grep pswp /proc/vmstat` before and after | n/a |
| 8 | Package C-state limit C2, ASPM off in BIOS | MEASURE-FIRST | BIOS note (reasoned, unmeasured) | low single digit at best | idle power up a few W | A/B decode tok/s | BIOS back |
| 9 | Raise DDR4 to 3733 to 3800 manually | REJECT for now | forum anecdotes | up to about 4 to 6 percent more peak bandwidth, effect on tok/s unknown | needs VCCSA up on the CPU with a degradation history; memory errors look like random crashes | memtest (TM5/OCCT on Windows, `memtester` is weak), no ECC to see errors | CMOS clear (reverts settings, not firmware) |
| 10 | Gear 2 with a high clock (4200 plus) | REJECT | reasoning, forum | IMC at half clock tends to erase the bandwidth gain, latency worse | same as 9 | n/a | n/a |
| 11 | 1 GiB hugetlb pool for pinned memory | REJECT until a TLB cost is shown | no 2026 benchmark found | unknown | removes RAM from a box with 12.9 GB available | `perf stat -e dTLB-load-misses,dtlb_load_misses.walk_active` | `vm.nr_hugepages=0` |
| 12 | Lower `min_free_kbytes` | REJECT | no evidence | none | pinned allocation failures | n/a | n/a |
| 13 | Edit the BIOS ROM defaults | REJECT | BIOS note (no programmer, no dual BIOS) | none | unrecoverable brick | n/a | needs CH341A |

## 8. Anything newly released in 2026 for DDR4 platform tuning on Linux

Nothing aimed at DDR4 itself was found. Related 2026 items:
- Intel microcode 20260811 with 0x137 for Raptor Lake S (VERIFIED secondary). Content of the Raptor Lake fix in it is not documented in what I found.
- llama.cpp expert cache `--moe-cache-mib`, merged 2026-10-07 (PR 29887), multi-GPU 2026-10-08 (PR 30112) (VERIFIED via a secondary article citing them, primary PRs not opened). It is the one 2026 change that attacks the host-memory ceiling directly, by cutting how often experts are read from DDR4 across PCIe.
- Phoronix THP madvise vs always on 6.18 LTS (Jan 2026), see section 6.
- No Linux 7.x kernel work specific to DDR4 timing or Raptor Lake IMC found. UNVERIFIED that none exists.

## 9. Sources

- ASRock B760M-ITX/D4 WiFi manual (memory table) as cited in pcie-gen5 note; ASRock forum TID=58726 (microcode thread)
- Intel Memory Latency Checker v3.12, https://www.intel.com/content/www/us/en/developer/articles/tool/intelr-memory-latency-checker.html
- STREAM source, https://www.cs.virginia.edu/stream/FTP/Code/stream.c
- Kernel THP docs, https://docs.kernel.org/admin-guide/mm/transhuge.html
- Phoronix THP madvise vs always, https://www.phoronix.com/review/thp-madvise-always/4 (Jan 2026)
- Intel 0x12F: https://hothardware.com/news/intel-new-firmware-fix-vmin-shift ; Intel community post on 0x12F
- 0x137 and the 20260811 package: https://cloudnews.tech/intel-patches-nine-vulnerabilities-with-microcode-20260811/
- MoE expert cache: https://www.developersdigest.tech/blog/llama-cpp-moe-offload-n-cpu-moe-vs-moe-cache-2026 ; PR 29887 and 30112 in ggml-org/llama.cpp
- DDR4 Gear 1 anecdotes: TechPowerUp forum threads, hwbot topic 213308, hardwareluxx DDR4 OC thread (older, possibly stale, mostly 12th gen)
- Igor's Lab single vs dual rank on Alder Lake (older, possibly stale)
- Quasarzone 14900K DDR4 vs DDR5 test
