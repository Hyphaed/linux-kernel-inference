"""0024 wired the real unprivileged-userns enforcement into the kernel but
never added the securityfs advertisement (`unconfined_restrictions/userns`)
that Ubuntu's own apparmor.service checks before trusting the sysctl. Found
on the 7.2.0-hyphaed boot audit, 2026-08-27: `sysctl
kernel.apparmor_restrict_unprivileged_userns` read 0 despite
10-apparmor.conf setting 1, because apparmor.service force-disabled it,
believing (wrongly) that the kernel didn't support the restriction.

Two fixes, both pinned here:
  1. patches/custom/0028-apparmor-userns-sfs-advertise.patch adds the
     missing securityfs boolean, referenced from the 7.2 series after 0024.
  2. hyphaed/phases/postinstall.py::_check_apparmor_userns() installs a
     systemd drop-in as an immediate, no-rebuild stopgap.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERIES = ROOT / "patches" / "kernel-org-7.2" / "series"
LOCK = ROOT / "patches" / "VENDOR-kernel-org-7.2.lock"
PATCH_0028 = ROOT / "patches" / "custom" / "0028-apparmor-userns-sfs-advertise.patch"
POSTINSTALL = ROOT / "hyphaed" / "phases" / "postinstall.py"


def _series_entries() -> list[str]:
    out = []
    for line in SERIES.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def test_0028_patch_file_exists():
    assert PATCH_0028.exists(), (
        "patches/custom/0028-apparmor-userns-sfs-advertise.patch is missing — "
        "this is the fix for 0024's missing securityfs advertisement node."
    )


def test_0028_referenced_in_series_after_0024():
    entries = _series_entries()
    idx_0024 = next(
        (i for i, e in enumerate(entries) if "0024-apparmor-restrict-unprivileged-userns" in e),
        None,
    )
    idx_0028 = next(
        (i for i, e in enumerate(entries) if "0028-apparmor-userns-sfs-advertise" in e),
        None,
    )
    assert idx_0024 is not None, "0024 dropped out of the 7.2 series unexpectedly"
    assert idx_0028 is not None, "0028 is not referenced in patches/kernel-org-7.2/series"
    assert idx_0028 > idx_0024, "0028 must apply after 0024 (it patches the same file)"


def test_0028_pinned_in_vendor_lock_with_matching_sha256():
    import hashlib

    lock_line = next(
        (ln for ln in LOCK.read_text().splitlines() if "0028-apparmor-userns-sfs-advertise" in ln),
        None,
    )
    assert lock_line is not None, "0028 is not pinned in VENDOR-kernel-org-7.2.lock"
    fields = lock_line.split()
    pinned_sha = fields[2]
    real_sha = hashlib.sha256(PATCH_0028.read_bytes()).hexdigest()
    assert pinned_sha == real_sha, (
        "0028's pinned sha256 doesn't match the file on disk — "
        "re-run the pin after any edit to the patch."
    )


def test_0028_adds_the_userns_securityfs_boolean():
    text = PATCH_0028.read_text()
    assert 'AA_SFS_FILE_BOOLEAN("userns", 1)' in text, (
        "0028 no longer adds the userns boolean to aa_sfs_entry_unconfined[] — "
        "that's the entire point of this patch."
    )
    assert "apparmorfs.c" in text


def test_0028_does_not_touch_enforcement_files():
    """This patch is advertisement-only. If a future edit starts touching
    lsm.c/task.c/policy.c here, it has drifted from being a small addendum
    into re-implementing 0024's job — a sign something is wrong.
    """
    text = PATCH_0028.read_text()
    diff_files = re.findall(r"^diff --git a/(\S+)", text, re.MULTILINE)
    assert diff_files == ["security/apparmor/apparmorfs.c"], (
        f"expected 0028 to touch only apparmorfs.c, got {diff_files}"
    )


def test_postinstall_wires_the_apparmor_userns_check():
    text = POSTINSTALL.read_text()
    assert "_check_apparmor_userns" in text
    assert re.search(r"^\s*_check_apparmor_userns\(\)\s*$", text, re.MULTILINE), (
        "_check_apparmor_userns() is defined but never called from run_phase()"
    )


def test_apparmor_check_targets_the_right_sysctl_and_dropin():
    text = POSTINSTALL.read_text()
    assert "kernel.apparmor_restrict_unprivileged_userns" in text
    assert "apparmor.service.d" in text
    assert "90-hyphaed-userns.conf" in text
