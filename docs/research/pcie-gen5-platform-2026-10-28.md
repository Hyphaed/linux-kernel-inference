# PCIe Gen5 and host memory ceiling on ncore (research, 2026-10-28)

> **Update 2026-10-28, 11:23:** `dmidecode` on this machine reports both DIMMs configured at 3600 MT/s (Crucial BL32G36C16U4B), not the 3200 assumed below. Dual-channel peak is about 57.6 GB/s, not 51.2. Everything else in this note stands.


Labels: VERIFIED = I opened the source or probed the machine. INFERENCE = reasoned from verified facts. UNVERIFIED = not confirmed.

## Short answer

The RTX 5070 runs at Gen4 x16 because this board is a Gen4 board. The CPU can do Gen5, the card can do Gen5, the board's x16 slot is specified and wired as Gen4. Nothing in Linux can fix that, and nothing is broken. What it costs: host-RAM offload over PCIe tops out near 31.5 GB/s raw instead of about 63 GB/s. Even a Gen5 board would not double your offload speed, because dual-channel DDR4-3200 (51.2 GB/s peak) is below the Gen5 ceiling.

## 1. Is the slot Gen5-capable?

- VERIFIED: ASRock manual for B760M-ITX/D4 WiFi (download.asrock.com/Manual/B760M-ITXD4%20WiFi.pdf, 46 pages, fetched 2026-10-28). Spec table: "CPU: 1 x PCIe 4.0 x16 Slot (PCIE1), supports x16 mode". Block diagram: "LGA1700 CPU ... Gen4 x16 PCIE1(GEN4 x16 Slot)"; "Gen4 x4 M2_1"; "DMI Gen4 x4". Slot section: "PCIE1 (PCIe 4.0 x16 slot)". The manual never mentions Gen5.
- VERIFIED: Intel specs page (sku 236773, the 14900K page; the KF shares the I/O spec): PCI Express Revision "5.0 and 4.0", configurations "Up to 1x16+4, 2x8+4", 20 lanes. So the CPU supports Gen5 and the board does not expose it.
- VERIFIED (local): root port 0000:00:01.0 reports max_link_speed 16.0 GT/s, current 16.0 GT/s x16. GPU reports max 32.0 GT/s, current 16.0 GT/s x16. nvidia-smi: pcie.link.gen.max 4, gen.hostmax 4. The driver sees the host side as Gen4, consistent with the board.
- VERIFIED: the kernel boot message (252.048 vs 504.112 Gb/s) is the standard "downstream device is faster than the link" warning. It is informational.
- INFERENCE: the root port advertises 16 GT/s in its own capability register because firmware (ASRock's BIOS) configures the PEG port for Gen4 on this SKU. I could not read LnkCap/LnkCap2 (lspci hides them without root), so I did not see whether the port's supported-speeds vector stops at 16 GT/s.
- VERIFIED by absence: I found no BIOS menu option for PCIE1 link speed in the manual text I grepped (no "Link Speed", "Gen", or "PEG" setting beyond the spec table; the manual has no BIOS-screen chapter in the text layer). UNVERIFIED: whether the UEFI has a hidden "PCIE1 Link Speed" (Auto/Gen3/Gen4) item. If it exists it can only lower the speed, because the slot tops out at Gen4. There is no Gen5 value to select.
- UNVERIFIED: BIOS 12.02 notes. ASRock's BIOS and product pages are script-rendered and returned empty to my fetches. A search hit showed a 12.02 entry dated 2026-07-07 for a different board (B760 Pro RS WiFi: microcode 0x133, Secure Boot key update), which is not this board's note. Your board's BIOS reports 12.02 dated 07/02/2026 (VERIFIED, DMI). Nothing I found says any BIOS changed PCIe speed or ReBAR on this board.
- Routes to Gen5: only a different board with a Gen5 x16 slot (several B760 and all Z790 boards have one; UNVERIFIED which, I did not check a list). A Gen5 riser does not help, the board's lanes and routing are specified as Gen4. INFERENCE: forcing Gen5 on a board not designed for it would risk signal-integrity errors anyway.

## 2. Software way to raise the root port speed?

- VERIFIED (kernel source and patch series, bwctrl.c, v6.13+): the in-kernel API pcie_set_target_speed() writes Target Link Speed in Link Control 2 and retrains. Its speed selection considers the supported-speed masks of both the root port and the endpoint.
- INFERENCE: since the port's own max is 16 GT/s, a target of 32 GT/s is either rejected or ignored by the hardware. The mechanism can lower a link (thermal cooling device style), not raise it past what the port advertises. Result: pointless here.
- Not safe, plainly: (a) using setpci to overwrite LnkCap or LnkCtl2 on the root port to claim 32 GT/s: LnkCap is hardware-initialised and the slot is not built for it; the best case is no effect, the worst is a link that trains badly, drops to Gen1, or hangs the GPU and the display. (b) Retraining the link or secondary-bus reset of 00:01.0 while the NVIDIA driver is loaded or the GPU drives a desktop. (c) Any boot parameter claiming to "force Gen5". None is needed and none can work.
- Safe and useful: read-only checks (section 5).

## 3. What Gen5 x16 would change for host-RAM offload

- VERIFIED (arithmetic from the kernel's own numbers): Gen4 x16 = 252.048 Gb/s = 31.5 GB/s per direction raw. Gen5 x16 = 504.112 Gb/s = 63 GB/s raw.
- UNVERIFIED for the 5070: I found no pinned cudaMemcpy measurement on an RTX 5070, or Gen4 vs Gen5 on any card. Closest numbers found: Titan RTX on Gen3 x16 pinned H2D about 11.5 GB/s of a 15.8 GB/s ceiling and an RTX 3080 on Gen3 about 12.3 GB/s (NVIDIA forum posts, old hardware). INFERENCE: real pinned copies land at about 75 to 80 percent of raw, so roughly 24 to 26 GB/s here today. Measure it: `bandwidthTest --memory=pinned` from the CUDA samples (uses the GPU, run when the card is idle).
- Search hits report RTX 5090 gaming is about 1 percent faster on Gen5 than Gen4. That is a game result, not an offload result. Streaming weights from RAM is the case where link bandwidth matters directly.
- INFERENCE: Gen5 would help offload less than 2x because the next limit is DDR4 (section 4) and because many offload paths copy from pageable memory, which makes them host-DRAM-bound.

## 4. DDR4 side

- VERIFIED: Intel specs for the 14900K family: "Up to DDR4 3200 MT/s", "Up to DDR5 5600 MT/s", 2 channels, max memory bandwidth 89.6 GB/s (the DDR5 figure).
- VERIFIED: ASRock manual: dual-channel DDR4, 2 DIMM slots, 64 GB max, "1DPC 1R up to 5333+ MHz (OC), 3200 MHz natively. 1DPC 2R up to 4266+ MHz (OC), 3200 MHz natively", XMP 2.0 supported. UNVERIFIED claim worth distrusting: 5333 is a marketing OC ceiling; typical DDR4 on 13/14th gen is stable at 3600 to 4000 in Gear 1 or higher in Gear 2. I did not source a Gear-mode table.
- Peak math (INFERENCE from spec): DDR4-3200 dual channel = 51.2 GB/s theoretical, about 40 to 45 GB/s realistic in a read test (typical 80 to 90 percent efficiency, UNVERIFIED for this system). DDR4-3600 = 57.6 GB/s peak.
- Versus PCIe: 51.2 GB/s peak is above the Gen4 x16 raw 31.5 GB/s, so today PCIe is the bottleneck, not DRAM. It would be below the Gen5 63 GB/s, so on Gen5 with DDR4-3200 the RAM becomes the cap, about 40 to 45 GB/s.
- Sharing: a GPU pulling 25 GB/s from RAM uses about half of DDR4-3200 realistic bandwidth. INFERENCE: CPU threads get what is left, roughly 15 to 20 GB/s, and both sides slow down under contention. The CPU work competes for the same two channels, so CPU-side inference with experts in RAM and GPU offload together cannot both run at full speed.
- Reading DIMM speed:
  - Without root: no reliable way found. /sys/firmware/dmi/tables/DMI is root-only (VERIFIED, permission denied here), EDAC exposes no speed (VERIFIED, /sys/devices/system/edac has only mc/power). The BIOS setup screen shows the running frequency.
  - With root: `sudo dmidecode -t memory | grep -E "Speed|Part Number|Rank|Locator"`. "Speed" is the module rating (JEDEC or XMP) and "Configured Memory Speed" is what runs. If Configured shows 2133 or 2666 or 3200 on kit rated higher, XMP is off.
- XMP enabled? UNVERIFIED. I couldn't read it. INFERENCE: unknown until dmidecode is run; many boards default to JEDEC or 3200.
- Gear mode: not readable from Linux without root MSR/MCHBAR tools. Check the BIOS (DRAM Gear Mode or similar).

## 5. Other platform checks (host to GPU throughput)

- GPU on CPU lanes: VERIFIED. Board diagram: PCIE1 is wired to the CPU (Gen4 x16), chipset connects by DMI Gen4 x4. The GPU does not share bandwidth with the chipset.
- NVMe: M2_1 is CPU-attached Gen4 x4 in the diagram (VERIFIED). Your NVMe at 0000:02:00.0 runs 16 GT/s x4 (VERIFIED local). It shares the CPU's 20 lanes but not the GPU's 16.
- Link state: GPU runs 16 GT/s x16 (VERIFIED), full width.
- ASPM: VERIFIED, kernel command line has pcie_aspm.policy=performance and the live policy is [performance]. L0s/L1 not forcing the link down. No concern.
- Resizable BAR: VERIFIED. Root port prefetchable window and the GPU BAR1 region are 16 GiB (lspci Region 1 size=16G; nvidia-smi BAR1 total 16384 MiB). ReBAR is on, and this is the correct state for a 12 GB card.
- IOMMU: VERIFIED, DMAR present, cmdline intel_iommu=on,igfx_off iommu=pt. Pass-through mode keeps DMA translation off the hot path for host devices. UNVERIFIED: any measured cost difference.
- Max Payload and Max Read Request on the root port: UNVERIFIED. lspci hides DevCtl without root. Check: `sudo lspci -vv -s 00:01.0 | grep -E "MaxPayload|MaxReadReq"` and same for 01:00.0. Typical healthy values are MaxPayload 256 and MaxReadReq 512 or higher; 128 would cost some bandwidth. Do not change them by hand.
- Riser or cable: INFERENCE only. The board has the slot directly on the PCB (mini-ITX, one PCIE1 slot). I can't see the case; if the card sits on a riser, that is a physical thing to check. The link training cleanly to x16 at 16 GT/s is circumstantial evidence there is no flaky riser. Check for AER errors: `journalctl -k | grep -iE "AER|corrected|pcieport"`.

## What I could not determine

- The BIOS 12.02 release notes and the actual BIOS menu entries for this board (ASRock pages are script-rendered).
- Whether the root port's LnkCap2 supported-speeds vector includes 32 GT/s (needs root `lspci -vv`).
- Configured DIMM speed, XMP state, gear mode, MaxPayload, MaxReadReq.
- Any pinned-copy benchmark on an RTX 5070 or Gen4 vs Gen5 offload measurement.
- Intel page fetched was for 14900K (236773), not the KF page; I/O and memory specs are shared by the two SKUs.

## Commands to run (owner, with root)

    sudo dmidecode -t memory | grep -E "Speed|Part Number|Rank|Locator"
    sudo lspci -vv -s 00:01.0 | grep -E "LnkCap|LnkCap2|LnkCtl2|MaxPayload|MaxReadReq"
    sudo lspci -vv -s 01:00.0 | grep -E "LnkCap|LnkSta|MaxPayload|MaxReadReq"

## Sources

- ASRock manual: https://download.asrock.com/Manual/B760M-ITXD4%20WiFi.pdf (opened)
- Intel specs page (sku 236773): https://www.intel.com/content/www/us/en/products/sku/236773/intel-core-i9-processor-14900kf-36m-cache-up-to-6-00-ghz/specifications.html (opened)
- Kernel PCIe bandwidth controller: drivers/pci/pcie/bwctrl.c and the "PCI/bwctrl: Add API to set PCIe Link Speed" patch series (search results only, not read in full)
- Pinned copy numbers: NVIDIA developer forum threads (search summaries only)
