# PCIe link speed idle power-cycling (investigation, LIKELY NOT A BUG)

Status: **investigation only, corrected after running the diagnostic script,
NOT a patch, NOT sent anywhere.** The plan this investigation belongs to
(GreenBoost Speed Program) has a standing rule: "only patch what you can
prove helps." That rule was never satisfied, and the evidence below now
argues against writing a kernel patch here at all, see "CORRECTION" below.

## CORRECTION (after running `diagnose_and_fix.sh` for real, same session)

The original framing below, "a stale cache, one write fixes it, credible
upstream RFC", **does not survive contact with more data and is likely
wrong.** What actually happened running the script:

- **Before** the fix: `current_link_speed` (live `LnkSta` read) was
  **5.0 GT/s**, not 16.0 GT/s as every earlier single-snapshot read in this
  investigation happened to show.
- **After** writing `cur_state=0` (requesting max speed): `current_link_speed`
  read **2.5 GT/s**, `cur_state` read back `2`, both *worse* than before the
  "fix".
- **Seconds later**, with nothing else done: `current_link_speed` was back to
  **16.0 GT/s** on its own, `cur_state` back to `3`.
- A direct 10-sample, 1 Hz poll of `current_link_speed` alone (no fix applied)
  showed it cycling through **2.5 / 5.0 / 16.0 GT/s within a 10-second idle
  window**, with GPU utilization 1-21% throughout, no `sudo`/no writes
  involved at all.
- A second poll correlating link speed with `nvidia-smi`'s reported GPU
  P-state (`pstate`) showed the **GPU's own power state actively cycling
  too** (P3/P5/P8, not fixed), confirming the GPU is idling aggressively via
  its own power management. The link-speed/P-state correlation isn't clean
  at 1 Hz (both signals likely change faster than 1 Hz), but the overall
  picture is unambiguous: **the link is not stuck, it's continuously,
  legitimately retraining as part of normal GPU idle power management.**

**What this means:** `current_link_speed` really is a live, fresh
`PCI_EXP_LNKSTA` register read every time (confirmed: `pci-sysfs.c`'s
`current_link_speed_show()` calls `pcie_capability_read_word()` on every
read, no caching), so every earlier "16.0 GT/s the whole time" claim in this
doc was real for the moments it was read, just not representative, the link
is actively cycling, and different single-point reads simply landed on
different phases of that cycle. `bwctrl.c`'s own header comment says its job
is to keep the cache in sync via a Bandwidth Notification *interrupt*
handler, i.e. it is REACTIVE to speed changes something else initiates, not
the thing deciding to downtrain. The actual driver of the downtraining is
almost certainly the NVIDIA GPU's own runtime power management (well
documented behavior on NVIDIA cards, PCIe link state tracks GPU P-state),
not a Linux `bwctrl` bug at all.

**Consequence for this investigation:** there is currently no evidence of a
kernel bug here. The apparent "staleness" earlier in this document was an
artifact of comparing two snapshots taken moments apart of a system that
was, correctly, continuously changing, not of one value being frozen wrong.
**Do not write the sketched kernel patch.** The original findings below are
kept for the record (they're not fabricated, they're real single-snapshot
readings), but the conclusion drawn from them was premature.

**What IS still real and useful, a GreenBoost-side lever, not a kernel fix:**
if sustained full PCIe speed during active inference matters (avoiding the
retrain latency incurred when the link has to train back up from an idle
low-power state at the start of a burst of requests), GreenBoost can hold
`cooling_device0/cur_state=0` for the duration of a `gb-synapse serve()`
session (set at serve start, released at stop, so idle power-saving resumes
between sessions). That is a legitimate, actionable Speed Program item;
it is NOT a kernel patch, and it is NOT "fixing a bug," it is deliberately
overriding normal power management for the duration of a workload that
wants it, the same tradeoff `gaming_mode` already makes elsewhere in this
project.

---

## Original investigation (kept for the record, conclusion above supersedes it)

## What was found (precise, verified against live kernel source in this tree)

On this machine (`7.1.5-hyphaed`, the GPU's root port `0000:00:01.0`,
`Raptor Lake PCI Express 5.0 Graphics Port`), two different views of "what
speed is the PCIe link running at" disagree:

| Source | What it reads | Live value observed |
|---|---|---|
| `/sys/bus/pci/devices/0000:00:01.0/current_link_speed` | `LnkSta` register, read live from hardware | `16.0 GT/s PCIe` |
| `/sys/class/thermal/cooling_device0/cur_state` (this port's PCIe cooling device) | `bus->cur_bus_speed`, a cached `struct pci_bus` field | drifted from `2` to `3` across a few minutes of observation |

`cooling_device0`'s `cur_state` decodes (via the arithmetic in
`drivers/thermal/pcie_cooling.c`, quoted below) to the kernel's *cached*
belief that the link is running at 5.0 GT/s (`cur_state=2`) or even
2.5 GT/s (`cur_state=3`, the *lowest* possible speed, `max_state`), while the
hardware itself, read directly via `LnkSta`, is actually at the full
16.0 GT/s ceiling the whole time. The cached value is not just wrong, it
drifted further from reality (2→3, i.e. the kernel's belief got *worse*)
while the real link stayed at full speed.

## The source, read directly from this tree (not assumed)

`~/Dev/kernel_inference/build/linux-7.1.5/drivers/thermal/pcie_cooling.c`:

```c
static int pcie_cooling_get_cur_level(struct thermal_cooling_device *cdev, unsigned long *state)
{
	struct pci_dev *port = cdev->devdata;
	/* cooling state 0 is same as the maximum PCIe speed */
	*state = cdev->max_state - (port->subordinate->cur_bus_speed - PCIE_SPEED_2_5GT);
	return 0;
}
```

`cur_state` is a pure function of `port->subordinate->cur_bus_speed`, a
cached `enum pci_bus_speed` field on `struct pci_bus`. Tracing where that
field gets written (`~/Dev/kernel_inference/build/linux-7.1.5/drivers/pci/pcie/bwctrl.c`):

```c
int pcie_set_target_speed(struct pci_dev *port, enum pci_bus_speed speed_req, bool use_lt)
{
	struct pci_bus *bus = port->subordinate;
	...
	if (bus && bus->cur_bus_speed == speed_req)
		return 0;                       /* <-- trusts the CACHE, doesn't re-read hardware */
	...
	ret = pcie_bwctrl_change_speed(port, target_speed, use_lt);
	...
	if (!ret && bus && bus->cur_bus_speed != speed_req && !list_empty(&bus->devices))
		ret = -EAGAIN;                  /* <-- also compares against the CACHE, not LnkSta */
}
```

`bus->cur_bus_speed` is written during bus probe/enumeration
(`drivers/pci/probe.c:913`) and, presumably, whenever this driver's own
retrain path completes. **It is not re-read from `LnkSta` on every access.**
If the link changes speed through any path this driver doesn't own (ASPM
transparently downtraining/uptraining the link, a spontaneous retrain from a
signal-integrity event, anything outside `pcie_bwctrl_change_speed`), the
cached field can go stale relative to the real, live hardware state, exactly
the disagreement measured above.

## Why this matters beyond one desktop's link-speed sysfs

Two consumers already trust this exact stale field, on this exact machine:

1. **The Linux thermal-cooling framework itself** (`cooling_device0`).
   `pcie_cooling_set_cur_level()` calls `pcie_set_target_speed()`, whose
   early-return (`bus->cur_bus_speed == speed_req`) means a request to pin
   the link at full speed can silently no-op if the cache already (wrongly)
   believes it's there, or can trigger unnecessary retraining if the cache
   wrongly believes the opposite.
2. **GreenBoost's own PCIe telemetry** (`gb_telemetry.py`'s `GpuTopology`,
   a sibling project on this machine using T2/DDR-as-VRAM tiering over this
   same link) reads `current_link_speed`/`max_link_speed` from sysfs
   directly, the *correct*, live-hardware path, not the cached one, so
   GreenBoost's own telemetry is unaffected. But anything that instead reads
   `port->subordinate->cur_bus_speed` in-kernel, or drives policy off
   `cooling_device0`'s reported state, is trusting a value that this
   investigation shows can drift from reality.

## What's verified, and what isn't yet

**Verified, from this session:**
- `cooling_device0`'s type string, `cur_state`/`max_state` values, read live
  from `/sys/class/thermal/cooling_device0/` on this exact box.
- `current_link_speed`/`max_link_speed` sysfs values for the same port,
  read live, same session.
- The `pcie_cooling_get_cur_level()`/`pcie_set_target_speed()` source, read
  directly from the actual built tree (`build/linux-7.1.5`), not assumed
  from a description.
- `drivers/pci/probe.c:913` is where `cur_bus_speed` gets written during
  bus scan, confirmed present in this tree.

**NOT yet verified, the actual next step:**
- `lspci -vv -s 00:01.0` requires root. This box's current session has no
  passwordless sudo, so `LnkCtl2`'s **Target Link Speed** field (distinct
  from `LnkSta`'s **negotiated** speed, distinct again from the cached
  `cur_bus_speed`) has not been read. That field would show what the kernel
  most recently *asked* the link to train to, disambiguating "the cache
  never updated after a legitimate autonomous downtrain-then-retrain" from
  "something wrote a stale target and never re-read the result."
- Whether writing `cur_state=0` (asking the cooling framework to request
  full speed) actually clears the cache, or whether the early-return in
  `pcie_set_target_speed` (`bus->cur_bus_speed == speed_req`) means it
  silently no-ops because the STALE cache already claims to be at that
  speed. This is the single most informative test available and needs only
  the command below.
- Whether this is Intel's own `pcie_cooling.c`/`bwctrl.c` (added
  2023-2024, comparatively new code) or a distro/vendor BIOS quirk specific
  to this Raptor Lake PEG010 port. No upstream bug tracker search has been
  done yet.

## The exact next command, RUN, see CORRECTION above for the result

`diagnose_and_fix.sh` in this directory runs everything below in one pass
(before-state, the diagnostic, the fix, after-state) and prints a summary.
**Already run once, see the CORRECTION section at the top for the actual
result and why it changed the conclusion.** Kept runnable for anyone who
wants to reproduce it or watch the cycling live:

```bash
sudo bash diagnose_and_fix.sh
```

Or the individual commands, if you'd rather run them one at a time:

```bash
lspci -vv -s 00:01.0 | grep -A3 "LnkCtl2"
```

Then, separately, observe whether a cooling-state write actually changes
anything:

```bash
cat /sys/class/thermal/cooling_device0/cur_state          # before
echo 0 | sudo tee /sys/class/thermal/cooling_device0/cur_state
sleep 1
cat /sys/class/thermal/cooling_device0/cur_state           # after: did it change?
cat /sys/bus/pci/devices/0000:00:01.0/current_link_speed   # still 16.0 GT/s? (expected, it never left)
lspci -vv -s 00:01.0 | grep -A3 "LnkCtl2"                   # did Target Link Speed change?
```

If `cur_state` after the write still doesn't read back `0`, that's the
early-return in `pcie_set_target_speed` confirmed live, the cache genuinely
never gets corrected by a normal write, which is the strongest possible
grounds for an upstream fix: **the cooling framework's own control knob
cannot repair the exact staleness it's supposed to prevent.**

## What a fix would look like (SUPERSEDED, kept for the record only, do not implement)

The reasoning below assumed a stale-cache bug. The CORRECTION section above
found no evidence of one, this GPU's link genuinely, continuously cycles as
part of normal idle power management. Not implementing either shape.

Two shapes, in order of how invasive they are, deferred until the command
above tells us which is actually needed:

1. **Cheapest, if it's purely a stale-read problem**: have
   `pcie_cooling_get_cur_level()` (or `pcie_set_target_speed`'s early-return
   check) re-derive the comparison from a live `LnkSta` read instead of
   trusting `bus->cur_bus_speed` unconditionally, at least for the
   early-return fast path. Small, narrow, easy to review.
2. **If the root cause is that nothing re-syncs `cur_bus_speed` after an
   autonomous link-speed change**: a hook wherever the kernel detects an
   unsolicited link speed change (bandwidth-notification IRQ, ASPM
   transition) that refreshes `bus->cur_bus_speed` from the live register at
   that point, rather than only on this driver's own explicit retrain path.
   Bigger, needs to find that detection point first.

Neither is written. Writing either before the diagnostic command above would
be guessing at which bug this actually is.

## Attribution, for whenever a patch exists

`From: Ferran Duarri <ferran.duarri@me.com>` /
`Signed-off-by: Ferran Duarri <ferran.duarri@me.com>`, matching this tree's
existing convention (`patches/custom/0019-dma-buf-priority-hint.patch`),
full name so it's ready for a real kernel-list submission.
