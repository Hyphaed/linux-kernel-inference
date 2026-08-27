"""Two fragment invariants found by the 2026-08-27 boot audit of 7.1.10.

Both are the same failure shape, and it is the one this repo keeps hitting:
a fragment sets a symbol, the symbol lands in the built .config, and the
capability still never arrives, because what actually decides lives somewhere
the fragment never touched.

1. CONFIG_BPF_LSM=y compiles the BPF LSM hooks in. CONFIG_LSM is the string
   that decides which LSMs get initialised. Ubuntu's base config omits `bpf`
   from it, so 7.1.10 booted with the symbol set and systemd reporting
   "BPF LSM hook not enabled in the kernel" on every boot.

2. Disabling CONFIG_KEXEC_HANDOVER removes CONFIG_CMA's only x86 selector
   (kernel/liveupdate/Kconfig). CMA falling takes DMA_CMA and DMABUF_HEAPS_CMA
   with it, on a machine built around dma-buf.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRAGMENTS_DIR = ROOT / "configs" / "fragments"
FRAGMENT_42 = FRAGMENTS_DIR / "42-io-uring-bpf.config"
FRAGMENT_05 = FRAGMENTS_DIR / "05-ubuntu-26-boot.config"


def _assignments(path: Path) -> dict[str, str]:
    """CONFIG_* assignments in a fragment, comments and blanks dropped."""
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^(CONFIG_[A-Z0-9_]+)=(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


# ── 1. BPF_LSM is useless without `bpf` in the CONFIG_LSM string ─────────────

def test_bpf_lsm_symbol_comes_with_a_matching_lsm_string():
    cfg = _assignments(FRAGMENT_42)
    if cfg.get("CONFIG_BPF_LSM") != "y":
        return  # fragment no longer asks for it; nothing to pair

    assert "CONFIG_LSM" in cfg, (
        "42-io-uring-bpf.config sets CONFIG_BPF_LSM=y but never sets "
        "CONFIG_LSM. The symbol alone does nothing — Ubuntu's inherited "
        "CONFIG_LSM has no `bpf` in it, and the LSM is silently never "
        "initialised. This is the exact state 7.1.10 shipped in."
    )

    lsms = [s for s in cfg["CONFIG_LSM"].strip('"').split(",") if s]
    assert "bpf" in lsms, (
        f"CONFIG_LSM={cfg['CONFIG_LSM']} does not list `bpf`, so "
        "CONFIG_BPF_LSM=y compiles hooks that never get initialised."
    )


def test_lsm_string_keeps_the_lsms_the_box_actually_relies_on():
    """Adding `bpf` must not drop anything already load-bearing here.

    apparmor confines snaps, landlock is used by systemd services, and
    lockdown/yama are security floor. Dropping one to make room would be a
    silent regression, so pin the whole set rather than just `bpf`.
    """
    cfg = _assignments(FRAGMENT_42)
    if "CONFIG_LSM" not in cfg:
        return
    lsms = set(cfg["CONFIG_LSM"].strip('"').split(","))
    for required in ("landlock", "lockdown", "yama", "integrity", "apparmor"):
        assert required in lsms, (
            f"CONFIG_LSM dropped `{required}`. That is a live LSM on this "
            f"box — check /sys/kernel/security/lsm before removing it."
        )


# ── 2. KEXEC_HANDOVER=n must not take CMA down with it ──────────────────────

def test_disabling_kexec_handover_pins_cma_explicitly():
    cfg = _assignments(FRAGMENT_05)
    if cfg.get("CONFIG_KEXEC_HANDOVER") != "n":
        return  # KHO is back on; the cascade cannot happen

    for symbol in ("CONFIG_CMA", "CONFIG_DMA_CMA", "CONFIG_DMABUF_HEAPS_CMA"):
        assert cfg.get(symbol) == "y", (
            f"05-ubuntu-26-boot.config sets CONFIG_KEXEC_HANDOVER=n without "
            f"pinning {symbol}=y. On x86, KEXEC_HANDOVER is the only thing "
            f"that selects CONFIG_CMA, and CMA falling cascades into DMA_CMA "
            f"and then DMABUF_HEAPS_CMA — a dma-buf heap lost as a side "
            f"effect of switching off a kexec feature nobody uses."
        )


# ── 3. a fragment nobody references is decoration ───────────────────────────

def test_every_fragment_is_referenced_by_at_least_one_preset():
    """The orphan check. Found 2026-08-27: 05-ubuntu-26-boot.config was in no
    preset at all, so it had never been merged into any build.

    _select_fragments() reads the preset's `fragments:` list when a preset is
    set, and every real run sets one (PRESET defaults to gaming-ai-vm). A
    fragment absent from all four presets is therefore never passed to
    merge_config.sh — it reads as though it enforces something and enforces
    nothing. 05's own header claims its symbols are "explicitly pinned here so
    any fragment that accidentally disables them is caught at configure-time",
    which was false for the whole life of the file. Its contents happened to
    match the base config, so nothing ever looked broken.

    Same failure family as the CONFIG_INTEL_ITMT pitfall in CLAUDE.md: the
    line was decoration, and merge_config.sh will not tell you.
    """
    import yaml

    on_disk = {p.name for p in FRAGMENTS_DIR.glob("*.config")}
    referenced: set[str] = set()
    presets_dir = ROOT / "configs" / "presets"
    for y in presets_dir.glob("*.yaml"):
        data = yaml.safe_load(y.read_text()) or {}
        referenced.update(data.get("fragments") or [])

    orphans = sorted(on_disk - referenced)
    assert not orphans, (
        "these fragments exist but no preset lists them, so they are never "
        f"merged into any build: {orphans}. Either add them to a preset or "
        "delete them — an unreferenced fragment is decoration that reads as "
        "policy."
    )


def test_no_preset_references_a_fragment_that_does_not_exist():
    """The mirror image, and it fails silently too.

    _select_fragments() filters with `if p.exists()`, so a typo'd or deleted
    fragment name is dropped with no error and no log line. The preset still
    looks complete.
    """
    import yaml

    on_disk = {p.name for p in FRAGMENTS_DIR.glob("*.config")}
    presets_dir = ROOT / "configs" / "presets"
    for y in sorted(presets_dir.glob("*.yaml")):
        data = yaml.safe_load(y.read_text()) or {}
        missing = sorted(set(data.get("fragments") or []) - on_disk)
        assert not missing, (
            f"{y.name} lists fragments that are not on disk: {missing}. "
            "_select_fragments() drops these silently via its .exists() "
            "filter, so the build simply never gets them."
        )


# ── 4. the Strix Point laptop's AMD levers ──────────────────────────────────

FRAGMENT_83 = FRAGMENTS_DIR / "83-strixpoint-amd.config"


def test_amd_pmf_dependencies_are_pinned_with_it():
    """AMD_PMF=m alone does not make AMD_PMF build.

    It `depends on ACPI && PCI && POWER_SUPPLY && AMD_NODE && TEE && AMDTEE`.
    Every one of those is =y/=m in Canonical's base config today, so the
    fragment worked by inheritance and would have kept working right up until
    Ubuntu changed the base. Pitfall #2 with a twist: the symbol here is real,
    it is the DEPENDENCY that goes quietly missing.

    PMF drives the firmware-side platform profile on Strix Point — the half of
    powerprofilesctl that is not CPU frequency.
    """
    cfg = _assignments(FRAGMENT_83)
    if cfg.get("CONFIG_AMD_PMF") not in ("m", "y"):
        return  # fragment no longer asks for PMF

    for dep, want in (("CONFIG_TEE", ("m", "y")),
                      ("CONFIG_AMDTEE", ("m", "y")),
                      ("CONFIG_AMD_NODE", ("y",))):
        assert cfg.get(dep) in want, (
            f"83-strixpoint-amd.config sets CONFIG_AMD_PMF but not {dep}. "
            f"PMF depends on it; without it the =m is a silent no-op."
        )


def test_itmt_is_pinned_for_amd_not_only_for_intel():
    """SCHED_MC_PRIO stopped being Intel-only in practice at 7.2.

    drivers/cpufreq/amd-pstate.c calls sched_set_itmt_core_prio() with each
    CPU's prefcore ranking and sched_set_itmt_support() once all CPUs are up.
    Strix Point is 4 Zen5 + 6 Zen5c, so the scheduler needs the ranking for
    the same reason machine 1 needs it for its P/E split.

    It is `default y` and would land regardless. It is pinned because "would
    land anyway" is exactly how CLAUDE.md's ITMT/HFI lesson started.
    """
    cfg = _assignments(FRAGMENT_83)
    assert cfg.get("CONFIG_SCHED_MC_PRIO") == "y", (
        "83-strixpoint-amd.config does not pin CONFIG_SCHED_MC_PRIO. On 7.2 "
        "amd-pstate feeds ITMT, so the AMD laptop wants it as much as the "
        "Intel desktop does."
    )
    assert cfg.get("CONFIG_SCHED_MC") == "y", "SCHED_MC_PRIO depends on SCHED_MC"


def test_amd_pstate_runs_in_active_epp_mode():
    """Mode 3 is what gives amd-pstate an energy_performance_preference knob.

    1=Disabled 2=Passive 3=Active(EPP) 4=Guided. The 2026-08-27 boot audit
    found machine 1 in power-saver with EPP=power on all 32 threads, fixed
    with `powerprofilesctl set performance`. That fix only means the same
    thing on the laptop if amd-pstate is in active mode.
    """
    cfg = _assignments(FRAGMENT_83)
    assert cfg.get("CONFIG_X86_AMD_PSTATE_DEFAULT_MODE") == "3", (
        f"amd-pstate default mode is "
        f"{cfg.get('CONFIG_X86_AMD_PSTATE_DEFAULT_MODE')!r}, want '3' (Active/EPP) "
        f"so powerprofilesctl controls the same knob on both machines."
    )


def test_laptop_fragment_stays_filename_gated_to_amd():
    """These symbols must never reach machine 1.

    _select_fragments() gates on `amd`/`strixpoint` tokens in the FILENAME,
    which is the sanctioned mechanism from pitfall #4 — a machine-specific
    value belongs behind that gate or in build/generated/, never in a
    fragment every preset merges.
    """
    tokens = set(FRAGMENT_83.stem.split("-"))
    assert tokens & {"amd", "strixpoint"}, (
        "83-strixpoint-amd.config lost its amd/strixpoint filename token, so "
        "_select_fragments() would merge it on the Intel desktop too."
    )
