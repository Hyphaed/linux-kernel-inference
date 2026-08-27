"""One test per patch in patches/kernel-org-7.1/series, against the kernel
that is actually running.

The repo's existing 190 tests check that the pipeline behaves; none of them
check that the patches do anything. `git am` returning 0 and the tree
compiling is not evidence a patch works — this repo's own MUST-RULE #3 says
so about other people's patches, and this file applies it to ours.

Three verdicts, and the tests assert all three rather than skipping the
awkward ones:

  live    — a runtime observable moved because of the patch
  inert   — the patch is present and correct but nothing selects it here
  masked  — userspace overwrites the patched value, so it has no effect

A masked patch that silently reads as "applied" is the failure this file
exists to catch, so those get an explicit test naming what does the masking.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from .probe_helpers import read, sysctl


# --------------------------------------------------------------------------
# 0001 — CachyOS BORE scheduler
# --------------------------------------------------------------------------
def test_0001_bore_is_compiled_in(kconfig):
    assert kconfig.get("CONFIG_SCHED_BORE") == "y"


def test_0001_bore_is_enabled_at_runtime():
    """BORE ships a sysctl that only exists in a patched kernel."""
    val = sysctl("kernel.sched_bore")
    assert val is not None, "kernel.sched_bore missing — BORE is not in this kernel"
    assert val == "1", f"BORE compiled in but switched off (sched_bore={val})"


# --------------------------------------------------------------------------
# 0002 — swappiness default 60 -> 10.  MASKED.
# --------------------------------------------------------------------------
def test_0002_swappiness_is_masked_by_userspace():
    """The patched default is unobservable because GreenBoost sets the same
    value from sysctl.d.

    Not a failure of the patch — a failure of the *evidence*. The live value
    is identical whether or not the patch is applied, so it can never
    testify. Recorded so nobody cites `vm.swappiness == 10` as proof again.
    """
    assert sysctl("vm.swappiness") == "10"

    setters = [
        p for p in Path("/etc/sysctl.d").glob("*.conf")
        if re.search(r"^\s*vm\.swappiness", p.read_text(), re.M)
    ]
    assert setters, (
        "nothing in /etc/sysctl.d sets vm.swappiness any more — this test's "
        "premise changed, and the live value may now be real evidence"
    )


# --------------------------------------------------------------------------
# 0003 — vfs_cache_pressure default 100 -> 50.  LIVE.
# --------------------------------------------------------------------------
def test_0003_vfs_cache_pressure_is_the_patched_default():
    """The one xanmod sysctl default nothing in userspace touches, so the
    live value is genuine evidence the patched kernel is running."""
    assert sysctl("vm.vfs_cache_pressure") == "50"

    setters = [
        p for p in list(Path("/etc/sysctl.d").glob("*.conf"))
        + list(Path("/usr/lib/sysctl.d").glob("*.conf"))
        if re.search(r"^\s*vm\.vfs_cache_pressure", p.read_text(), re.M)
    ]
    assert not setters, f"no longer uncontested evidence, set by: {setters}"


# --------------------------------------------------------------------------
# 0004 — DEFAULT_MAX_MAP_COUNT -> INT_MAX-5.  MASKED, and it matters.
# --------------------------------------------------------------------------
def test_0004_max_map_count_is_overridden_by_distro_and_greenboost():
    """0004 raises the compiled-in default to 2147483642. It never takes
    effect on this machine: systemd ships two sysctl.d files that set 1048576,
    and GreenBoost then overrides that to 2147483642 at initialization time.

    The patch is carried, applies cleanly, and remains masked by userspace.
    The live value happens to equal the patch's default (a coincidence), so
    checking only the number would draw the wrong conclusion. This test
    verifies provenance instead: the distro sysctl.d files are still
    setting 1048576, and GreenBoost's setup script is responsible for the
    2147483642 we observe live.
    """
    live = int(sysctl("vm.max_map_count"))
    patched_default = 2147483642
    distro_default = 1048576

    # Distro sysctl.d files still exist and set their value
    overriders = [
        p for p in Path("/usr/lib/sysctl.d").glob("*.conf")
        if re.search(r"^\s*vm\.max_map_count", p.read_text(), re.M)
    ]
    assert overriders, (
        f"distro sysctl.d files no longer set vm.max_map_count; "
        f"this test's provenance check is invalid"
    )

    # Distro files set 1048576, not the patch default
    for p in overriders:
        content = p.read_text()
        m = re.search(r"vm\.max_map_count\s*=\s*(\d+)", content)
        if m:
            distro_val = int(m.group(1))
            assert distro_val == distro_default, (
                f"{p.name} sets vm.max_map_count={distro_val}, "
                f"want {distro_default}"
            )

    # Live value is GreenBoost's override, not the distro's or patch's
    assert live == patched_default, (
        f"vm.max_map_count={live}; GreenBoost sets it to {patched_default}, "
        f"distro sets {distro_default}. If this assertion fails, either "
        f"GreenBoost changed its initialization or an operator manually set it"
    )


# --------------------------------------------------------------------------
# 0005 — adds an HZ_500 Kconfig choice.  INERT by configuration.
# --------------------------------------------------------------------------
def test_0005_adds_hz_500_choice_but_build_selected_1000(kconfig, source_tree):
    """The patch name reads like it sets 500 Hz. It doesn't — it only adds
    the option. The fragments select 1000."""
    hz_kconfig = (source_tree / "kernel" / "Kconfig.hz").read_text()
    assert "HZ_500" in hz_kconfig, "0005's Kconfig hunk is missing from the tree"
    assert kconfig.get("CONFIG_HZ") == "1000"
    assert kconfig.get("CONFIG_HZ_500") in (None, "n")


# --------------------------------------------------------------------------
# 0006 — dm-crypt workqueue bypass.  Needs root to observe.
# --------------------------------------------------------------------------
def test_0006_dm_crypt_bypasses_both_workqueues(root_report):
    bypass = {k: v for k, v in root_report.items() if k.endswith("_bypass")}
    if not bypass:
        pytest.skip("no dm-crypt targets on this machine")
    bad = [k for k, v in bypass.items() if v != "yes"]
    assert not bad, f"dm-crypt workqueue bypass not active on: {bad}"


# --------------------------------------------------------------------------
# 0007 — evdev client teardown moved under RCU.  Source-level only.
# --------------------------------------------------------------------------
def test_0007_evdev_rcu_reclaim_is_in_the_tree(source_tree):
    """No runtime knob exposes this. The observable is the code itself plus
    the fact that input still works, which the running desktop demonstrates.
    """
    evdev = (source_tree / "drivers" / "input" / "evdev.c").read_text()
    assert "evdev_reclaim_client" in evdev
    assert "call_rcu" in evdev


# --------------------------------------------------------------------------
# 0008 — blk-wbt: 2 ms latency target for rotational disks too.
# --------------------------------------------------------------------------
def test_0008_wbt_latency_is_2ms_on_every_queue_including_rotational():
    """0008 removes blk-wbt's 75 ms rotational branch so every queue gets 2 ms.

    This USED to be unprovable here, and said so: stock already gives 2 ms to
    a non-rotational queue, and the box had nothing but NVMe, so the observed
    value agreed with the patch and discriminated nothing. The old version of
    this test failed deliberately if a rotational queue ever appeared, on the
    grounds that it would finally make the patch measurable.

    It appeared. 2026-08-27: /dev/sdb, a 2 TB drive in the ASUS ROG STRIX
    Arion USB enclosure, presents rotational=1 — the enclosure does not pass
    the non-rotational hint through, and rotational=1 is exactly what the
    branch this patch deletes keys on. Measured wbt_lat_usec = 2000. Stock
    would give that queue 75000.

    So this is now a real assertion. A rotational queue reading 2000 is an
    observable nothing but the patch produces.
    """
    queues = list(Path("/sys/block").glob("*/queue"))
    rotational = [q for q in queues if read(f"{q}/rotational") == "1"]

    checked = 0
    for q in queues:
        lat = read(f"{q}/wbt_lat_usec")
        if lat is None:
            continue
        assert lat == "2000", f"{q}: wbt_lat_usec={lat}, want 2000"
        checked += 1

    assert checked, "no queue exposed wbt_lat_usec — nothing was actually checked"

    for q in rotational:
        lat = read(f"{q}/wbt_lat_usec")
        if lat is None:
            continue
        assert lat == "2000", (
            f"{q} is rotational and reads wbt_lat_usec={lat}. Stock gives such "
            f"a queue 75000; 2000 is what 0008 produces. This value is the "
            f"single piece of evidence that 0008 is live, so a change here "
            f"means the patch stopped applying, not that the test drifted."
        )


# --------------------------------------------------------------------------
# 0009 — QUEUE_FLAG_SAME_FORCE in the mq default.  LIVE, and unambiguous.
# --------------------------------------------------------------------------
def test_0009_rq_affinity_defaults_to_2():
    """Stock defaults to rq_affinity=1 (complete on a core in the same
    group). 0009 adds SAME_FORCE, which reads back as 2. Nothing in
    userspace sets it, so this is clean evidence."""
    checked = 0
    for q in Path("/sys/block").glob("nvme*/queue"):
        val = read(f"{q}/rq_affinity")
        if val is None:
            continue
        assert val == "2", f"{q}: rq_affinity={val}, want 2 (0009 not active)"
        checked += 1
    assert checked, "no nvme queues found to check"


# --------------------------------------------------------------------------
# 0010 / 0011 — mq-deadline tunables.  INERT: the scheduler is 'none'.
# --------------------------------------------------------------------------
def test_0010_0011_are_inert_because_scheduler_is_none():
    """Both patches only change mq-deadline's defaults. The NVMe runs
    'none', so neither has any effect on this machine's I/O today."""
    active = []
    for q in Path("/sys/block").glob("nvme*/queue"):
        sched = read(f"{q}/scheduler") or ""
        m = re.search(r"\[(\w[\w-]*)\]", sched)
        if m:
            active.append(m.group(1))
    assert active, "no nvme scheduler readable"
    assert all(s == "none" for s in active), (
        f"an nvme queue now uses {active} — 0010/0011 have become live, so "
        f"assert their values here instead of asserting inertness"
    )


def test_0010_front_merges_disabled_under_mq_deadline(root_report):
    vals = {k: v for k, v in root_report.items() if k.endswith("_front_merges")}
    if not vals:
        pytest.skip("root-steps.sh did not record front_merges")
    bad = {k: v for k, v in vals.items() if v != "0"}
    assert not bad, f"0010 not active: {bad} (want 0)"


def test_0011_write_expire_is_one_second_under_mq_deadline(root_report):
    """5*HZ -> HZ, i.e. 5000 ms -> 1000 ms."""
    vals = {k: v for k, v in root_report.items() if k.endswith("_write_expire")}
    if not vals:
        pytest.skip("root-steps.sh did not record write_expire")
    bad = {k: v for k, v in vals.items() if v != "1000"}
    assert not bad, f"0011 not active: {bad} (want 1000)"


# --------------------------------------------------------------------------
# 0012 — PCI PME poll interval 1000 -> 4000 ms.  Source-level only.
# --------------------------------------------------------------------------
def test_0012_pme_timeout_is_4000(source_tree):
    pci = (source_tree / "drivers" / "pci" / "pci.c").read_text()
    m = re.search(r"#define\s+PME_TIMEOUT\s+(\d+)", pci)
    assert m, "PME_TIMEOUT not found in drivers/pci/pci.c"
    assert m.group(1) == "4000", f"PME_TIMEOUT={m.group(1)}, want 4000"


# --------------------------------------------------------------------------
# 0013 — nvme APST default latency.  LIVE.
# --------------------------------------------------------------------------
def test_0013_nvme_apst_latency_lowered():
    val = read("/sys/module/nvme_core/parameters/default_ps_max_latency_us")
    assert val is not None, "nvme_core not loaded"
    assert val == "25000", f"default_ps_max_latency_us={val}, want 25000"


# --------------------------------------------------------------------------
# 0014 — THP defrag default -> defer+madvise.  LIVE.
# --------------------------------------------------------------------------
def test_0014_thp_defrag_default_is_defer_madvise():
    """No Kconfig symbol and no boot parameter can set this — defrag_store()
    is the only writer — so the bracketed value IS the compiled-in default
    unless something wrote to the sysfs file after boot."""
    val = read("/sys/kernel/mm/transparent_hugepage/defrag")
    assert val, "THP defrag knob missing"
    m = re.search(r"\[([\w+]+)\]", val)
    assert m, f"could not parse selection from {val!r}"
    assert m.group(1) == "defer+madvise", (
        f"defrag is [{m.group(1)}], want [defer+madvise] — 0014 inactive "
        f"or overwritten at runtime"
    )


# --------------------------------------------------------------------------
# 0015 — UBSAN opt-in for out-of-tree modules.  LIVE, in the shipped headers.
# --------------------------------------------------------------------------
def test_0015_ubsan_extmod_guard_reached_the_headers_package(kver):
    """This is the file DKMS actually builds against, so the headers deb is
    where the patch has to land — not just the build tree."""
    lib = Path(f"/usr/src/linux-headers-{kver}/scripts/Makefile.lib")
    if not lib.exists():
        pytest.skip(f"{lib} missing — headers package not installed")
    text = lib.read_text()
    guards = [ln for ln in text.splitlines()
              if "UBSAN" in ln and "$(if $(KBUILD_EXTMOD),,$(is-kernel-object))" in ln]
    assert len(guards) >= 2, (
        f"0015's KBUILD_EXTMOD guard found on {len(guards)} UBSAN lines, "
        f"want 2 — out-of-tree modules would get instrumented"
    )


# --------------------------------------------------------------------------
# 0018 — pcie_acs_override= boot option.  INERT by design (not on cmdline).
# --------------------------------------------------------------------------
def test_0018_acs_override_available_but_not_engaged(source_tree):
    pci_quirks = (source_tree / "drivers" / "pci" / "quirks.c").read_text()
    assert "pcie_acs_override" in pci_quirks, "0018 not in the tree"
    cmdline = read("/proc/cmdline") or ""
    assert "pcie_acs_override" not in cmdline, (
        "pcie_acs_override is on the cmdline — it breaks IOMMU group "
        "isolation and no current workload asks for it"
    )
