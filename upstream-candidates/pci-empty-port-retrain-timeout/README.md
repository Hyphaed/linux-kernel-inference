# PCI: skip a futile link retrain on a root port with nothing behind it

Status: **drafted, compile-checked in isolation, NOT in the patch series,
NOT sent anywhere.** Full narrative, evidence, and the residual-risk
statement live in `docs/tasks_patches.md` T21 — this directory holds the
raw diff, matching the convention `x86-pci-single-node-root-bus/` used
before that fix (0029) was promoted.

## One-line summary

`drivers/pci/quirks.c::pcie_failed_link_retrain()` lifts a firmware-set
2.5GT/s speed restriction on every PCIe root port without checking the
link is actually up first, despite its own comment saying it should. On
this box's Root Port #8 (`00:1c.0`, genuinely empty — `lspci -t` confirms
no downstream device), that means every single boot wastes
`PCIE_LINK_RETRAIN_TIMEOUT_MS` (1000ms) waiting for a DLLLA bit that can
never assert, then logs a "retraining failed" line that reads like a real
problem.

## The fix

`0001-pci-skip-futile-retrain-on-empty-port.diff` — 26 lines, one hunk,
`drivers/pci/quirks.c` only. Re-reads `PCI_EXP_LNKSTA` immediately before
the restriction-lifting branch and skips it when
`!dev->subordinate && !(lnksta & PCI_EXP_LNKSTA_DLLLA)`. Scoped to
`!dev->subordinate` deliberately: this function has three callers, and
only the enumeration-time one (`pci_device_add()`, before
`pci_scan_bridge()` has created the port's downstream bus) has
`dev->subordinate == NULL`. The other two (`pci.c:1255`,
`pci.c:4657`, both post-reset link-recovery polling) call it on a port
whose subordinate bus was already created during initial scan, so this
change is a structural no-op for them — verified by reading both call
sites, not tested (this box has no active VFIO/PCI-passthrough workload
to exercise a real secondary-bus reset).

## What's verified, what isn't

- Root-caused via source reading:
  `pcie_set_target_speed(dev, speed_cap, false)` → `pcie_bwctrl_change_speed()`
  → `pcie_retrain_link(port, use_lt=false)` → waits on DLLLA via
  `pcie_wait_for_link_status()`, times out after
  `PCIE_LINK_RETRAIN_TIMEOUT_MS` (`drivers/pci/pci.h:22`, 1000ms) — not
  guessed from the log text alone.
- Compiles clean (`drivers/pci/quirks.o`, isolated `/tmp` worktree copied
  from the real patched 7.2.8 tree — see the session note below on why an
  isolated copy, not the live build dir, was used).
- `git diff | scripts/checkpatch.pl --no-tree --strict`: **0 errors, 0
  warnings, 0 checks, 32 lines checked** (scoped to the diff, not the whole
  file — `quirks.c` as a whole has 33 pre-existing style warnings unrelated
  to this hunk).
- **Not yet live-verified.** Needs a boot with this patch applied to
  confirm the "retraining failed" line and ~1s stall are gone from
  `00:1c.0`, and that nothing else on that port (or any other root port)
  regressed.
- Real hardware evidence is boot-log-only right now (present on all 4 most
  recently audited boots of 7.2.7-hyphaed) — no before/after timing
  measurement of the actual 1s stall has been taken yet (e.g. dmesg
  timestamp delta around the two log lines on a real boot).

## Process note, kept honest

This patch was drafted while a real 7.2.8 build (`hyphaed --from-phase
build`) was running in the background against `build/linux-7.2.8`. The
first edit-and-compile attempt was made directly against that live tree —
the same mistake 0029's own README warns about avoiding ("isolated git
worktree so the concurrent build was untouched"). No damage resulted
(`drivers/pci/quirks.o` had already been compiled by the in-flight build
before the edit landed, confirmed by log line order, and the edit was
reverted via `git checkout` before the tree was touched further), but the
actual compile-check that produced the "compiles clean" result above was
done against a separate `/tmp` copy, not the live tree, after that first
mistake was caught.
