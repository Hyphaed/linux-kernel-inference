# x86/PCI: default a root bus to the only online node when firmware won't say

Status: **drafted and compile-tested, DRAFT — not authored, not numbered,
not sent.** Per this repo's rule (`CLAUDE.md`), an AI agent never writes
`Signed-off-by:` without Ferran naming this specific patch in the moment.
This file is the technical case; turning it into a real numbered patch with
real authorship is a separate, deliberate step.

## The problem, observed live on this box

Every boot, four times, in `dmesg`:

```
nvidia-fs:warning: error retrieving numa node for device 0000:01:00.0
nvidia-fs:warning: error retrieving numa node for device 0000:04:00.0
nvidia-fs:warning: error retrieving numa node for device 0000:01:00.0
nvidia-fs:warning: error retrieving numa node for device 0000:02:00.0
```

Those are the GPU and both NVMe controllers. `/etc/udev/rules.d/
62-hyphaed-pci-numa-node.rules` already tried to fix this for the
per-device sysfs attribute and, per its own 2026-08-28 correction, does
**not** fix it — because nvidia-fs doesn't read the per-device attribute.
It calls `pcibus_to_node(pdev->bus)`
(`/usr/src/nvidia-fs-2.29.4/nvfs-pci.h:213`), which on x86 is
`to_pci_sysdata(bus)->node` — a value set once, at boot, per PCI **root
bus**, and never touched by udev.

## Root cause, traced to source

`arch/x86/pci/acpi.c::pci_acpi_root_get_node()`:

```c
static int pci_acpi_root_get_node(struct acpi_pci_root *root)
{
	int busnum = root->secondary.start;
	struct acpi_device *device = root->device;
	int node = acpi_get_node(device->handle);

	if (node == NUMA_NO_NODE) {
		node = x86_pci_root_bus_node(busnum);
		if (node != 0 && node != NUMA_NO_NODE)
			dev_info(&device->dev, FW_BUG "no _PXM; falling back to node %d from hardware (may be inconsistent with ACPI node numbers)\n",
				node);
	}
	if (node != NUMA_NO_NODE && !node_online(node))
		node = NUMA_NO_NODE;

	return node;
}
```

Two lookups, both come up empty on this board (ASRock B760M-ITX/D4):
`acpi_get_node()` needs an ACPI `_PXM` method the firmware doesn't provide,
and `x86_pci_root_bus_node()` (the "hardware fallback," reading Northbridge
registers) also returns `NUMA_NO_NODE` here — confirmed live,
`/sys/bus/pci/devices/0000:0{1,2,4}:00.0/numa_node` all read `-1` before the
udev rule masks the *device* attribute (the *bus* value underneath stays
`-1` regardless, which is what `pcibus_to_node()` actually reads).

This machine has exactly one online NUMA node (confirmed:
`numactl --hardware` → 1 node; also stated as a hardware fact in this repo's
`CLAUDE.md`: "the single NUMA node on machine 1 ... means NUMA-placement
tuning is a non-lever here"). On a single-node system, `NUMA_NO_NODE` is not
actually ambiguous — there is exactly one truthful non-`NUMA_NO_NODE` answer,
and the kernel already has the fact needed to give it (`num_online_nodes()`).
Every consumer of `pcibus_to_node()` — not just nvidia-fs, any NUMA-aware
driver doing `cpumask_of_node()`/local-allocation decisions off this value —
currently gets "unknown" on hardware that has a well-defined single answer.

## The fix

```diff
@@ pci_acpi_root_get_node()
 	if (node != NUMA_NO_NODE && !node_online(node))
 		node = NUMA_NO_NODE;
 
+	/*
+	 * Firmware on some consumer boards omits _PXM entirely and the
+	 * hardware fallback above also comes up empty, leaving every PCI
+	 * root bus at NUMA_NO_NODE. On a system with exactly one online
+	 * node that isn't ambiguous: there is only one truthful answer.
+	 * Leaving it at NUMA_NO_NODE instead means every NUMA-aware caller
+	 * (pcibus_to_node(), which nvidia-fs's numa lookup depends on) reads
+	 * "unknown" on hardware that has a perfectly well-defined answer.
+	 */
+	if (node == NUMA_NO_NODE && num_online_nodes() == 1)
+		node = first_online_node;
+
 	return node;
 }
```

`first_online_node` rather than a hardcoded `0` — correct even on a
single-node system whose one online node isn't numbered 0 (rare, but the
macro is free: `first_node(node_states[N_ONLINE])`, already used elsewhere
in the same header). x86-only (the function is `arch/x86/pci/acpi.c`), so it
cannot affect ARM/other-arch multi-socket NUMA systems by construction.

Full diff: `0001-x86-pci-single-node-root-bus-node.diff`.

## Second finding from the same investigation: a self-inflicted taint bit

`numa_node_store()` (`drivers/pci/pci-sysfs.c:383`) — the sysfs **write**
path the udev rule uses to patch the per-device attribute — calls
`add_taint(TAINT_FIRMWARE_WORKAROUND, ...)` and `pci_alert(FW_BUG
"Overriding NUMA node to %d")` on every write. That fires once per matched
device at every boot on this box (confirmed: 20 `Overriding NUMA node`
lines in a single boot's `dmesg`), and it's the reason every boot audit in
this repo has recorded taint `14336` and called all three of its bits
"expected" — the out-of-tree and unsigned-module bits genuinely are, but the
firmware-workaround bit is set by **our own udev rule**, not by anything
upstream. This patch, by fixing the root cause at the bus level, makes the
per-device udev rule (and the taint it causes) deletable.

## Verification done so far

- **Compiles clean**: `make arch/x86/pci/acpi.o` against a real, freshly
  cloned v7.2.3 tree with this box's actual `.config` (0 warnings, 0
  errors), done in an isolated `git worktree` so the concurrent 7.2.3
  `.deb` build in `build/linux-7.2.3` was never touched.
- **checkpatch clean**: `scripts/checkpatch.pl --no-tree`, 0 errors, 0
  warnings, run from inside the kernel tree per this repo's own
  documented gotcha (invoking it from the repo root misreads diff context
  lines as trailing-whitespace errors).
- **Not yet verified**: the four live-boot conditions this needs before it's
  more than a plausible fix —
  1. the four `nvidia-fs` numa warnings go to zero
  2. `/sys/bus/pci/devices/0000:01:00.0/numa_node` reads `0` with the udev
     rule masked/removed
  3. `/proc/sys/kernel/tainted` loses bit 11 (14336 → 12288) once the udev
     rule is also removed
  4. `pcibus_to_node()` observed actually returning `0` at the nvidia-fs
     call site (bpftrace, or just the warning's absence)

  All four need a kernel that has this patch, actually booted. That's the
  7.2.3 kernel currently building in `build/linux-7.2.3`, which does **not**
  carry this patch (it's the existing validated 24-patch series only) — this
  fix was intentionally kept out of that build so the two changes don't land
  in the same boot and confound which one did what.

## What this also lets us remove, once verified

- `/etc/udev/rules.d/62-hyphaed-pci-numa-node.rules` and its writer,
  `_ensure_pci_numa_rule` in `hyphaed/phases/postinstall.py` — both become
  unnecessary once the bus itself reports the right node.

## Recipients (not yet sent, listed for when it is)

`x86/pci` — `get_maintainer.pl` on `arch/x86/pci/acpi.c` names Bjorn Helgaas
(PCI maintainer) plus `linux-pci@vger.kernel.org` and `x86@kernel.org`; not
re-verified in this session since this patch isn't going anywhere yet.
