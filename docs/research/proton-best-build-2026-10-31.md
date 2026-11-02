# Proton build for this workstation: what the research supports (2026-10-31)

## Question

Which Proton build, and which build-time changes, give the best performance on this machine (i9-14900KF hybrid CPU, RTX 5070 12 GB,
Wayland, kernel 7.2.14-hyphaed, NVIDIA 615.78.08), and is there anything to change in the build for DLSS 5 and neural rendering?

## Short answer

Rebuild from the newest Valve `bleeding-edge`, because the base moved a lot (below). Do not expect compiler flags to be the win: no source
found measures `-march=native` or x86-64-v3 for Wine or Proton. The measurable wins are on the driver and sync side (Reflex through
`VK_NV_low_latency2`, NTSYNC), and most of those are runtime settings, not build changes. Nothing found says the Proton build must change
for DLSS 5. Whether the rebuilt `proton-gb-nr` beats Valve's own Proton 11 on this box is still **not measured**: `grenboost_gaming/bench/results`
is empty. Until it is, treat the rebuild as a candidate (ADR-004).

## What upstream changed since our last checkout (measured locally)

`git fetch` of ValveSoftware/Proton `bleeding-edge` (2026-10-31): our checkout is `a3dc129b`, upstream is `e409b02c`, 11 commits ahead.
The commits themselves are small (wineopenxr, lsteamclient, Xalia 0.5.0.1). The real movement is the four submodule pins: `wine`, `dxvk`,
`dxvk-nvapi` and `vkd3d-proton` all changed, which is where the performance and fix work lives. The fetch inside the old checkout fails
(`cd to 'external/vkroots' failed`, a submodule directory missing there), so a plain `git pull` there is not usable. A fresh clone plus
`apply-patches.sh` is the documented route (grenboost_gaming START_HERE) and is what the `proton` phase does. It now saves the exact
commit and submodule list to `state/machines/<host>/proton-changes-<date>.md`.

## Findings by topic

**Sync (NTSYNC).** Wine 11 supports NTSYNC and Proton 11 enables it by default; it needs kernel 6.14+ ([lilting.ch](https://lilting.ch/en/articles/linux-gaming-ntsync-kernel-windows-api)). Proton-CachyOS 11 made it default and dropped most old Wine-Staging patches ([PC Games Hardware](https://www.pcgameshardware.de/Linux-Software-26761/News/CachyOS-Proton-mit-Steam-Linux-Runtime-4-1530046/)).
The big numbers (40 to 200%) compare against stock Wine without fsync; against Proton's fsync the gain is small, and one CachyOS test favored fsync
in one game ([CachyOS forum](https://discuss.cachyos.org/t/proton-cachyos-ntsync-and-fsync-comparison-update/7932)). The value is correctness and
frame pacing in games that stutter. On this box `/dev/ntsync` exists and the `ntsync` module loads at boot (`/etc/modules-load.d/ntsync.conf`), so nothing is missing.

**NVIDIA driver and Reflex.** 615.71.09 added `VK_NV_low_latency` revision 2, giving Reflex to Vulkan games under Proton, and fixed low latency on some
Wayland sessions ([Phoronix](https://phoronix.com/news/NVIDIA-615.71.09-Linux-Driver), [Linuxiac](https://linuxiac.com/nvidia-615-71-linux-driver-adds-proton-support-for-nvidia-reflex/)).
615.78.08 (installed here) moves to revision 3 ([Linuxiac](https://linuxiac.com/nvidia-615-78-08-linux-driver-brings-vulkan-latency-wayland-fixes/)).
NVIDIA's driver guide says the Vulkan Reflex layer is **disabled by default** and enabled with `DXVK_NVAPI_VKREFLEX=1` ([NVIDIA](https://docs.nvidia.com/datacenter/tesla/driver-installation-guide/gaming.html)).
That is a launch option to test per game, not a build change. Forum reports show Reflex costing performance in one game (Doom The Dark Ages), so A/B it.

**Compiler flags.** CachyOS claims 5 to 20% for x86-64-v3 packages, but Phoronix's v3/v4 tests were mixed and Canonical saw large regressions in some tests
([Phoronix](https://www.phoronix.com/review/cachyos-x86-64-v3-v4)). No Wine or Proton benchmark was found. Our `-march=alderlake` (ADR-003) stays
as a measured-on-this-host choice, not a proven gain; DXVK needs its `-mno-avx -mno-avx2` override, already in place.

**vkd3d-proton and DXVK.** vkd3d-proton 3.0.1 (May 2026) added Vulkan present timing for frame pacing and decoupled NVAPI present IDs (useful for frame generation);
the developer says descriptor-heap support comes next ([Phoronix](https://www.phoronix.com/news/VKD3D-Proton-3.0.1)). DXVK 3.0 uses descriptor heaps by default
([Phoronix](https://www.phoronix.com/news/DXVK-3.0-Release)). Newer pins from the fresh clone pick these up; no patch is needed.

**Hybrid CPU scheduling.** A cluster-scheduling fix for Intel hybrid CPUs is queued for Linux 7.3 ([hwbusters](https://hwbusters.com/news/intel-says-linux-cluster-scheduling-fails-on-its-hybrid-cpus-the-fix-is-queued-for-7-3/)).
That is a kernel matter, not a Proton one. We already carry the ADR-010 thread-priority patches (opt-in) in `proton-gb-nr`.

## DLSS 5 and neural rendering

- The practical Linux path today is `DLSS5VKLayer`, a Vulkan layer for native and Proton games that sends frames over IPC to the Windows NGX neural-rendering
  helper running under Wine or a custom Proton tool, using NVIDIA Optical Flow ([Phoronix](https://phoronix.com/news/DLSS5VKLayer)). So the Proton side must be able to run that
  helper in the prefix; our wrapper's Vulkan-layer injection (ADR-001) is the matching piece. A third-party swapper for Linux also exists ([DLSS5-Swapper-Linux](https://github.com/Nicholasvoador/DLSS5-Swapper-Linux-Support/releases/tag/v2.2.9-linux.1)); unverified.
- dxvk-nvapi 0.9.2 (May 2026) added support for the R595 NVAPI driver settings; no release found names DLSS 5 ([Phoronix](https://www.phoronix.com/news/DXVK-NVAPI-0.9.2-Released)).
  Our dxvk-nvapi carries two local commits (SetReflexSync, VRAM budget logging) as patches; they must still apply on the new pin.
- Raw DirectX Shader Model 6.9 cooperative-vector games would need `VK_NV_cooperative_vector` in vkd3d-proton. It is absent there (ADR-007) and no search result says otherwise.
  NVIDIA's RTX Neural Shading samples require that extension ([RTXNS](https://github.com/NVIDIA-RTX/RTXNS)). No game we target needs it, so this stays tracked, not built.
- No finding supports a Proton build flag for DLSS 5. The things that matter are runtime: matching NVIDIA userspace and kernel module (done in the `nvidia` phase), Reflex layer on, enough VRAM headroom (12 GB), and the NGX helper running under the chosen Proton.

## Second pass, same day: what the fresh clone really contains

Checked in `proton-gb-nr/sources/Proton` after the new clone and our patches (all three apply now):
`vkd3d-proton` v3.1-1 (Shader Model 6.9, Shader Execution Reordering, long vectors, `KHR_opacity_micromap`, DXR 1.2; released 2026-10-28,
NVIDIA only so far, [GamingOnLinux](https://www.gamingonlinux.com/2026/10/vkd3d-proton-3-1-brings-a-new-stable-release-ahead-of-more-important-changes-to-come/)),
`dxvk` v3.1.1+171, `dxvk-nvapi` v0.9.3 plus our two commits, which already carries `nvofapi` (the optical flow API). So the "newest pins" part of the rebuild is real.
The driver here (615.78.08) exposes `VK_NV_optical_flow`, `VK_NV_cooperative_vector` r4, `VK_NV_low_latency2` r3 and `VK_EXT_descriptor_heap`.

**Cooperative vector is still not in vkd3d-proton.** `grep cooperative_vector` over `libs/` and `include/` finds nothing, and the 3.1 notes don't mention it.
Microsoft's own DirectX post says the original Cooperative Vector design is being deprecated in favour of a unified one, which is a reason not to build ADR-007 now.

**DLSS 5 neural rendering under Proton, what the community stack needs** ([DLSS5VKLayer](https://github.com/bmitch87/DLSS5VKLayer), [Phoronix](https://phoronix.com/news/DLSS5VKLayer), [24high bridge](https://github.com/24high/DLSS5-Reshade-Linux-Steam)):
- A runner that bundles dxvk-nvapi. Valve's Proton is "not a primary target" for that reason; `proton-gb-nr` bundles it, so it qualifies.
- `PROTON_ENABLE_NVAPI=1` (Valve spelling) and `VKLayer_DLSS5=1`; driver 610 or newer; the user supplies `nvngx_dlssnr.dll` from NVIDIA's Windows software.
- `VK_NV_optical_flow` is used when present, otherwise the helper silently falls back to zero motion vectors. The 24high bridge reports hardware optical flow refused under Proton
  (`API version 0x20 with status 1`) and uses a ReShade shader for motion vectors instead, which smears. That is the one place where Proton's own nvofapi bridge could matter. It is untested here.
- Cross-process frame sharing wants `ptrace_scope` 0 for dma-buf transfer; it falls back to host copies otherwise. This box has it at 1 (Yama default). Changing it is a hardening decision, so it's not done here.
- Cost reported: about 0.6 ms a frame on an RTX 4080 in one game. No RTX 50 or 12 GB numbers exist. Everything is labelled experimental by its authors, and one says it hasn't run in a real game yet.

**What that means for the build.** None of it needs a Proton compile flag. The build can't make DLSS 5 work or fail. What the fresh pins give us is a vkd3d-proton and dxvk-nvapi that
match what these layers were developed against. The work left is runtime and per game: layer on, NGX DLL in place, then measure frame time with it on and off on this card.

**Also tried and found nothing:** Wine/Proton P-core/E-core pinning results for Raptor Lake, and any PGO/LTO/`-O3` benchmark for Wine or Proton. No source measures them, so none of it goes into the build on faith.

## Third pass: NVIDIA, Valve, GitHub and arXiv sources

**What DLSS 5 officially is.** NVIDIA's developer post describes "3D-guided neural rendering": a one-frame-in, one-frame-out model that takes the engine's color and motion
vectors and adds lighting and material detail, with per-model choice, Structure/Tone Intensity sliders and masking. It's announced for all GeForce RTX 50 desktop and laptop GPUs, "up to 4K"
([NVIDIA](https://developer.nvidia.com/blog/whats-new-for-game-developers-dlss-5-with-3d-guided-neural-rendering-nvidia-ace-updates-and-new-rtx-kit-capabilities/)).
The 5070 qualifies. That post gives no VRAM, no millisecond cost, no Vulkan or Linux path and no integration route. Don't plan around numbers it doesn't contain.
The only cost figures are third party: a community reimplementation, OpenDLSS-NR, measured 12.6 ms at 1440p and 29.3 ms at 2160p on an RTX 4070 SUPER, and its author saw the
official DLLs roughly halve the frame rate in one game ([XDA](https://www.xda-developers.com/reading-dlss-5-source-code-made-sense-of-the-neural-rendering-complaints/)).
NVIDIA's own later post says an October model update runs 2x faster than September's ([NVIDIA](https://developer.nvidia.com/blog/nvidia-rtx-spark-powers-neural-rendering-and-ai-characters-in-games/)).
Read together: neural rendering costs real GPU time, it's a quality feature and not a speed-up, and on a 12 GB card the budget has to be measured per game.

**Proton-side facts from NVIDIA's driver guide** ([615 branch](https://docs.nvidia.com/datacenter/tesla/driver-installation-guide/615/gaming.html)):
`PROTON_ENABLE_NGX_UPDATER=1` swaps a game's bundled DLSS DLLs for newer ones; `DXVK_NVAPI_DRS_NGX_DLSS_{SR,RR,FG}_OVERRIDE=on` plus `..._RENDER_PRESET_SELECTION=render_preset_latest`
forces presets (which exist depends on the bundled dxvk-nvapi, so our newer pin helps); `NVPRESENT_ENABLE_SMOOTH_MOTION=1` is driver frame generation and must not be combined with native DLSS FG;
`DXVK_NVAPI_VKREFLEX=1` is the Vulkan Reflex layer for native-Vulkan games. Wine-NVML is not in Proton, so GPU usage readouts are wrong without it.

**Reflex needs no extra layer on this build.** dxvk-nvapi's README says Reflex over Vulkan works when the driver has `VK_NV_low_latency` revision 2 and winevulkan is 1.4.357 or newer
([dxvk-nvapi](https://github.com/jp7677/dxvk-nvapi)). Checked here: the new Wine's `vk.xml` is header 357, the driver is 615.78.08 (revision 3). The separate layer files aren't shipped in our Proton, and for D3D games they aren't needed.

**dxvk-nvapi and optical flow.** The README ships `nvofapi64.dll` but doesn't describe what it implements, and its feature list has no DLSS 5 line. Optical flow is how DLSS 3 frame generation came to Proton
on Turing through Ada ([PC Games Hardware](https://www.pcgameshardware.de/Linux-Software-26761/News/DLSS-3-Frame-Generation-mit-Proton-unter-Linux-1459522/)); whether it works on Blackwell under our build is unmeasured.

**Valve.** Proton 11.0 stable (July 2026) is Wine 11 with NTSYNC (kernel 6.14+); the coverage expects less CPU overhead and steadier frame times, not large gains, and none for GPU-bound games
([GamingOnLinux](https://gamingonlinux.com/2026/07/proton-11-0-1-officially-released-to-expand-windows-games-on-steamos-linux), [lilting.ch](https://lilting.ch/en/articles/linux-gaming-ntsync-kernel-windows-api)).
No Valve engineering post about performance work turned up.

**arXiv.** "Hardware Accelerated Neural Block Texture Compression with Cooperative Vectors" ([2506.06040](https://arxiv.org/abs/2506.06040)) reports 4k texture sets at 0.55 ms on an Intel B580 using
28 MB of VRAM per set, up to 23x faster than a plain FMA path. That's the argument for cooperative vector in games, and it's also the workload our vkd3d-proton can't run yet (ADR-007). It's about a
future game class, not a build flag today. A paper on hybrid-CPU load balancing ([2411.19542](https://arxiv.org/abs/2411.19542)) is about LLM inference, with no game or Wine result, so it doesn't support any Proton setting.
No arXiv paper measuring Wine/Proton/DXVK performance exists in the results.

## Not found

No arXiv paper measuring Wine, Proton, DXVK or vkd3d-proton performance turned up (searched arxiv.org directly). No NVIDIA blog post for Linux gaming in 2026 turned up;
NVIDIA's driver guide and forum threads were the primary sources. No official Valve changelog for the October 2026 bleeding-edge build was found.

## What the rebuild will and will not do

It will give you Valve's newest Wine, DXVK, vkd3d-proton and dxvk-nvapi pins plus our patches (alderlake flags, ADR-004 SetReflexSync, ADR-010 thread priorities, opt-in).
It will not by itself make games faster than Valve's Proton 11. To find out, run the `bench/` harness on one game with Proton 11, Proton Experimental and the new
`proton-gb-nr`, record the kernel + Proton pair (ADR-011), and compare frame-time 1% lows, not averages.
