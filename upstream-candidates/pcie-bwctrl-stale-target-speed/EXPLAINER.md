# PCIe link-speed caching, a from-first-principles explainer

Audience: this doc assumes you can read C and know roughly what a kernel
driver is, but assumes NOTHING about PCIe link training, thermal cooling
devices, or PCIe power management. By the end you should be able to run the
diagnostic command yourself and understand what the answer means.

**CORRECTION, read this first:** this doc was originally written assuming
Part 2's snapshot comparison had found a stuck, stale kernel cache. Running
the actual diagnostic (see `README.md`'s CORRECTION section) showed
something different and, honestly, more interesting: the live link speed is
**continuously, legitimately cycling** (2.5/5.0/16.0 GT/s within a 10-second
idle window), correlated with the GPU's own power state (P3/P5/P8) also
actively cycling. There is no evidence of a stuck cache, both signals are
real-time-changing, and two snapshots taken moments apart of a changing
system will naturally disagree, that disagreement isn't a bug by itself.
**Part 1's concepts below are still accurate and worth learning from,
Part 2/3's specific conclusion is superseded, kept for the record with
inline notes rather than deleted, this IS what a real investigation
correcting itself looks like, not a failure to hide.**

---

## Part 1, the concepts, before the code

### 1.1 What does "PCIe link speed" actually mean?

A PCIe link (the wire between, say, your CPU's PCIe root port and your GPU)
doesn't run at one fixed speed forever. Every PCIe generation defines a
higher signaling rate than the last (Gen1 = 2.5 GT/s, Gen2 = 5.0 GT/s,
Gen3 = 8.0 GT/s, Gen4 = 16.0 GT/s, Gen5 = 32.0 GT/s, "GT/s" = giga-transfers
per second, not quite the same as bits/sec because of encoding overhead).
Both ends of the link negotiate, during "link training", the fastest speed
BOTH sides support and BOTH sides currently agree they can run reliably.

Two speeds matter here, and they are NOT the same thing:
- **Capability** (`LnkCap` in PCIe config space): the fastest speed this
  piece of silicon could ever theoretically do. Fixed at manufacture.
- **Current negotiated speed** (`LnkSta`, "Link Status"): what the link is
  ACTUALLY running at right now, which can be lower than capability, and
  can change at runtime.

Why would the current speed ever be lower than capability? Two legitimate
reasons: **ASPM** (Active State Power Management, the link intentionally
downtrains to save power when idle, then retrains back up when traffic
resumes), and **signal integrity** (a marginal physical link, e.g. bad
cabling/retimers/thermal issues, can force a downtrain to a speed it can
sustain reliably).

### 1.2 Why does Linux let software influence this at all?

Downtraining for power is great when the system is idle, but can hurt if
software actually needs the bandwidth right now (e.g. streaming a large AI
model's weights across the link during inference, this investigation's
actual motivating context, see the sibling GreenBoost project on this
machine). So Linux exposes a **thermal cooling device** abstraction over
PCIe link speed, normally used to intentionally REDUCE speed to shed heat
under thermal pressure, but the same knob can be used in reverse: request
"cooling state 0", the framework's convention for "no cooling applied, run
at the fastest speed this port supports."

That's `/sys/class/thermal/cooling_deviceN/cur_state` for a PCIe port,
`drivers/thermal/pcie_cooling.c` in this kernel tree. Each state maps to a
specific PCIe speed; state 0 is always the fastest, `max_state` is always
the slowest (2.5 GT/s, the original PCIe 1.0 speed, the universal fallback).

### 1.3 The part that makes this investigation interesting: WHO tracks "current speed"?

You'd think "current speed" is something the kernel re-reads from the
hardware register (`LnkSta`) every time anyone asks. It mostly is, for the
sysfs file most tools use (`current_link_speed`). But the thermal-cooling
code path doesn't read `LnkSta` directly, it reads a **cached field**,
`struct pci_bus`'s `cur_bus_speed`, that gets written when the bus is
scanned/probed and (presumably) when this driver's own retrain code
finishes. If the link's speed changes through some OTHER path the kernel
takes (ASPM transparently downtraining, a spontaneous retrain the cooling
driver didn't initiate), nothing guarantees that cached field gets updated
to match.

That's the whole bug, in one sentence: **two different "current speed"
readings exist in this kernel, one live from hardware, one cached, and
nothing forces them to agree.**

---

## Part 2, the concrete, real-world motivating case (ORIGINAL reasoning, see the correction above and Part 3 for what actually happened)

### 2.1 What was actually observed, on this exact machine (at the time, from two isolated snapshots)

Two commands, run seconds apart, on the GPU's PCIe root port
(`0000:00:01.0`):

```bash
$ cat /sys/bus/pci/devices/0000:00:01.0/current_link_speed
16.0 GT/s PCIe                    # live hardware read: full Gen4 speed

$ cat /sys/class/thermal/cooling_device0/cur_state
2                                  # cached belief: this decodes to 5.0 GT/s
```

Minutes later, without anything obviously changing:

```bash
$ cat /sys/class/thermal/cooling_device0/cur_state
3                                  # cached belief got WORSE: now 2.5 GT/s (the slowest possible)

$ cat /sys/bus/pci/devices/0000:00:01.0/current_link_speed
16.0 GT/s PCIe                    # hardware unchanged, still full speed
```

The GPU was never actually running slower. Only the kernel's cached OPINION
about the speed drifted, in the wrong direction, while nothing was reading
it wrong (both reads used the standard sysfs interfaces).

### 2.2 Why "just decode the arithmetic" isn't guessing

`drivers/thermal/pcie_cooling.c`'s comment says it plainly: *"cooling state
0 is same as the maximum PCIe speed"*, and the actual code is:

```c
*state = cdev->max_state - (port->subordinate->cur_bus_speed - PCIE_SPEED_2_5GT);
```

The five PCIe speed constants are consecutive integers (the kernel
literally asserts this with `static_assert(PCIE_SPEED_2_5GT + 1 ==
PCIE_SPEED_5_0GT)` and so on through Gen6), so `speed - PCIE_SPEED_2_5GT`
is just "how many generations above the slowest possible speed is this."
`max_state` for this port is 3 (this port's ceiling is Gen4/16.0 GT/s,
3 generations above the 2.5 GT/s floor). Plugging `cur_state=2` back in:
`2 = 3 - (cur_bus_speed - 2.5GT_index)` → `cur_bus_speed` decodes to
`PCIE_SPEED_5_0GT`. That's not a guess, it's the same formula the driver
itself uses, run in reverse.

### 2.3 Why this matters for real workloads, not just a sysfs curiosity

This investigation exists inside a larger project (GreenBoost) that streams
large AI model weights across exactly this PCIe link during inference,
measured elsewhere in that project at ~24 GB/s sustained on a healthy
Gen4 x16 link. Two consequences follow directly from Part 2.1's finding:

- **If something in-kernel ever makes a decision based on
  `cur_bus_speed`** (not just this cooling device, ANY future code that
  reads `struct pci_bus.cur_bus_speed` instead of querying `LnkSta` live),
  it inherits this staleness. The bug isn't confined to the sysfs file you
  happened to read, it's confined to which FIELD the reader trusts.
- **The cooling framework's own repair mechanism may not work.** Writing
  `cur_state=0` calls `pcie_set_target_speed()`, which starts with:
  `if (bus->cur_bus_speed == speed_req) return 0;`, an early exit that
  trusts the very cache this investigation shows can be wrong. If the stale
  cache already (incorrectly) claims to be at the requested speed, the
  "fix" silently does nothing. This is untested as of this writing (needs
  root, see the README's exact command), but if true, it means the
  standard, documented way to fix this exact problem doesn't actually fix
  it, which would make this a genuinely reportable upstream bug, not a
  configuration quirk.

---

## Part 3, what actually happened when the diagnostic ran

`diagnose_and_fix.sh` (run for real, as root, same session) did exactly the
`lspci -vv LnkCtl2` read Part 2.3 planned, then applied the fix, then
re-read everything. The result did NOT confirm either of the two failure
modes hypothesized in Part 2.3. Instead:

```
BEFORE the fix:  current_link_speed = 5.0 GT/s   cur_state = 0
                 LnkCtl2 Target Link Speed = 16GT/s   (already correct!)
AFTER the fix:   current_link_speed = 2.5 GT/s   cur_state = 2   (WORSE)
Seconds later,
nothing else done: current_link_speed = 16.0 GT/s   cur_state = 3
```

`LnkCtl2`'s Target Link Speed was ALREADY 16GT/s before anything was
touched, that rules out both of Part 2.3's hypothesized failure modes (both
assumed a wrong or stale TARGET; the target was fine the whole time). What
explains the data instead: a direct 10-sample poll of `current_link_speed`
alone, no writes, no `sudo`, nothing else changed, showed it **cycling
through 2.5/5.0/16.0 GT/s within a 10-second idle window**. A second poll
correlating link speed with `nvidia-smi`'s GPU P-state showed the **GPU's
own power state cycling too** (P3/P5/P8).

**The lesson, and it's a useful one even though the bug wasn't real:** this
system has (at least) three genuinely different, all-real, all-changing
signals, `LnkSta` (what's negotiated right now), `LnkCtl2`'s target (what's
being asked for), and the GPU's own internal power state, and a debugging
instinct that says "two readings disagree, therefore one is stale/wrong" is
only correct if the underlying reality is actually static. Here it wasn't:
the link was legitimately, continuously retraining as part of normal NVIDIA
GPU idle power management (well outside Linux's own ASPM, which
`pcie_aspm=off` on this box's boot cmdline already disables, that flag
doesn't touch the GPU's own internal power state machine at all). Two
snapshots of a genuinely dynamic system will disagree with each other AND
with a formula-derived "should be" value, without either reading being
wrong.

**No patch follows from this.** The actionable outcome is a GreenBoost-side
lever instead (holding `cur_state=0` for the duration of an inference
session, to prevent retrain latency between bursts of requests), documented
in `README.md`'s CORRECTION section, not a kernel change. This is exactly
what "prove it before patching it" is supposed to produce sometimes, a
correctly abandoned hypothesis, not a fabricated confirmation.
