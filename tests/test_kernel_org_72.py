"""The 7.2 series, its lock, and the floor-retirement mechanism the bump needed.

Created 2026-08-27 alongside patches/kernel-org-7.2/. The 7.1 equivalents live
in tests/test_patch_series.py and tests/test_pcie_substrate.py; this file only
covers what is specific to 7.2.
"""
import hashlib
import re
from pathlib import Path

import pytest

from hyphaed import kconfig

ROOT = Path(__file__).resolve().parent.parent
SERIES = ROOT / "patches" / "kernel-org-7.2" / "series"
LOCK = ROOT / "patches" / "VENDOR-kernel-org-7.2.lock"


def _series_entries() -> list[str]:
    return [ln.strip() for ln in SERIES.read_text().splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def _lock_rows() -> list[tuple[str, str, str, str]]:
    rows = []
    for ln in LOCK.read_text().splitlines():
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        f = ln.split()
        if len(f) >= 4:
            rows.append((f[0], f[1], f[2], f[3]))
    return rows


# ── series / lock agreement ─────────────────────────────────────────────────

def test_series_and_lock_list_the_same_patches_in_the_same_order():
    assert [r[0] for r in _lock_rows()] == _series_entries()


def test_every_series_entry_exists_on_disk():
    for name in _series_entries():
        assert (SERIES.parent / name).resolve().is_file(), f"missing: {name}"


def test_every_lock_sha256_matches_the_file():
    for name, _origin, sha, _kind in _lock_rows():
        p = (SERIES.parent / name).resolve()
        got = hashlib.sha256(p.read_bytes()).hexdigest()
        assert got == sha, (
            f"{name}: lock says {sha[:16]}…, file hashes to {got[:16]}…. "
            f"Re-pin it rather than editing the lock by hand."
        )


def test_0022_is_absent_because_it_landed_in_v7_2():
    """Gunthorpe's 5bf888673e0d is IN v7.2. Carrying it would re-apply an
    upstream commit. The 7.1 series needed it because 7.1.y never picked it
    up; 7.2 does not."""
    assert not any("0022" in n for n in _series_entries()), (
        "0022-udmabuf is in the 7.2 series. It is already upstream in v7.2 — "
        "verified with `git merge-base --is-ancestor 5bf888673e0d v7.2`."
    )


def test_cache_ext_applies_after_everything_that_touches_mm():
    """0023 is hand-ported from a v6.6.8 fork and has shipped broken once, so
    it applies after anything it could collide with — a conflict then surfaces
    in the patch we already distrust, not in one just pulled in clean from
    upstream. Concretely it shares mm/vmscan.c with 0025.

    It is NOT required to be dead last. 0024 trails it because that is the
    tree 0024 was authored and compile-tested against, and 0024 touches only
    security/apparmor, which overlaps nothing else in the series.
    """
    entries = _series_entries()
    idx = {n: i for i, n in enumerate(entries)}
    cache_ext = next(n for n in entries if "cache-ext" in n)
    workingset = next(n for n in entries if "workingset" in n)
    assert idx[workingset] < idx[cache_ext], (
        "0025 must precede 0023 — both touch mm/vmscan.c, and the fragile one "
        "goes second so a conflict lands where we already know to look."
    )
    for name in entries[idx[cache_ext] + 1:]:
        assert "apparmor" in name, (
            f"{name} applies after 0023. Only patches that cannot collide with "
            f"it in mm/ may do that; add the justification here if you add one."
        )


def test_bore_is_the_native_7_2_patch_not_the_7_1_5_hand_port():
    entries = _series_entries()
    bore = [n for n in entries if "bore" in n]
    assert bore == ["../custom/0001-cachyos-bore-7.2.patch"], bore


# ── floor retirement ────────────────────────────────────────────────────────

def test_retired_symbol_is_excused_only_from_its_own_version_onward():
    cfg = {}  # symbol entirely absent
    r71 = kconfig.validate(cfg, with_greenboost=False, kernel_version="7.1.10")
    r72 = kconfig.validate(cfg, with_greenboost=False, kernel_version="7.2")
    joined71 = " ".join(r71.missing)
    joined72 = " ".join(r72.missing)
    assert "READ_ONLY_THP_FOR_FS" in joined71, "7.1 still has the symbol; absence is a real failure there"
    assert "READ_ONLY_THP_FOR_FS" not in joined72, "7.2 retired it upstream; absence is expected"
    assert any("READ_ONLY_THP_FOR_FS" in w for w in r72.warnings), (
        "the exemption must still be REPORTED. A silently skipped floor check "
        "is how a symbol goes missing for a bad reason and nobody notices."
    )


def test_unknown_version_gets_no_exemption():
    """An exemption that cannot be verified against a version is not an
    exemption."""
    r = kconfig.validate({}, with_greenboost=False, kernel_version=None)
    assert any("READ_ONLY_THP_FOR_FS" in m for m in r.missing)


def test_a_symbol_set_to_n_is_never_excused():
    """Retirement covers a symbol that no longer EXISTS. One that exists and
    is switched off is a real failure on every version."""
    r = kconfig.validate({"CONFIG_READ_ONLY_THP_FOR_FS": "n"},
                         with_greenboost=False, kernel_version="7.2")
    assert any("READ_ONLY_THP_FOR_FS" in m for m in r.missing)


def test_every_retirement_documents_a_version_and_a_reason():
    for sym, entry in kconfig.RETIRED_UPSTREAM.items():
        assert len(entry) == 2, sym
        version, why = entry
        assert re.match(r"^\d+\.\d+", version), f"{sym}: bad version {version!r}"
        assert len(why) > 80, (
            f"{sym}: the reason must name the removing commit and say why the "
            f"capability survived it. Got: {why!r}"
        )


# ── APIs 7.2 removed ────────────────────────────────────────────────────────

# Functions gone in 7.2 that a patch forward-ported from an older kernel is
# likely to still be using, with the replacement. Each was verified against
# build/linux-7.2 before being listed — the check is "declared in 7.1.10's
# header, absent from 7.2's".
REMOVED_IN_7_2 = {
    "strncpy": "strscpy() — 7.1.10 declares strncpy at include/linux/string.h:71; "
               "7.2 names it only in comments as the thing to replace. strscpy "
               "always NUL-terminates, which strncpy does not when the source "
               "fills the buffer.",
}


def test_no_patch_adds_an_api_that_7_2_removed():
    """A forward-port compiles against the kernel it was written for.

    0023 shipped a `strncpy()` call that applied cleanly to 7.2, passed
    `git am`, passed patches/eval.py, and then failed `make bindeb-pkg` 14
    minutes in. Applying is not compiling, and this repo has now been bitten
    by that distinction twice on the same patch — the first time was
    BTF_SET8_START, which compiled and failed at initcall.
    """
    import re
    failures = []
    for patch in sorted((ROOT / "patches" / "kernel-org-7.2").glob("*.patch")) + \
                 [(SERIES.parent / n).resolve() for n in _series_entries()]:
        text = patch.read_text(errors="ignore")
        # only ADDED lines, and not the changelog above the first diff
        body = text.split("\ndiff --git ", 1)
        added = body[1] if len(body) > 1 else ""
        for fn, fix in REMOVED_IN_7_2.items():
            for line in re.findall(rf"^\+.*\b{fn}\s*\(", added, flags=re.M):
                failures.append(f"{patch.name}: adds {fn}() — removed in 7.2. Use {fix}")
    assert not failures, "\n".join(dict.fromkeys(failures))
