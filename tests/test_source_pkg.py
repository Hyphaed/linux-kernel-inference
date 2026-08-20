from pathlib import Path
from unittest.mock import MagicMock, patch

from hyphaed.phases.source import (
    _binary_to_source_pkg,
    _clear_canonical_cert_refs,
    _existing_baseline_config_kver,
    _retarget_running_kernel,
)

# Literal output of `apt-cache showsrc linux-image-7.0.0-15-generic` on Ubuntu 26.04.
_SHOWSRC_OUTPUT = """\
Package: linux
Binary: linux-headers-7.0.0-15, linux-tools-7.0.0-15, linux-cloud-tools-7.0.0-15, linux-libc-dev, linux-tools-common, linux-cloud-tools-common, linux-tools-host, linux-source-7.0.0, linux-doc, linux-bpf-dev, bpftool, linux-perf, linux-image-unsigned-7.0.0-15-generic, linux-image-unsigned-7.0.0-15-generic-dbgsym, linux-image-7.0.0-15-generic, linux-image-7.0.0-15-generic-dbgsym, linux-modules-7.0.0-15-generic, linux-headers-7.0.0-15-generic, linux-lib-rust-7.0.0-15-generic, linux-tools-7.0.0-15-generic, linux-cloud-tools-7.0.0-15-generic, linux-buildinfo-7.0.0-15-generic, linux-image-unsigned-7.0.0-15-generic-64k, linux-image-unsigned-7.0.0-15-generic-64k-dbgsym, linux-modules-7.0.0-15-generic-64k, linux-headers-7.0.0-15-generic-64k, linux-lib-rust-7.0.0-15-generic-64k, linux-tools-7.0.0-15-generic-64k, linux-cloud-tools-7.0.0-15-generic-64k, linux-buildinfo-7.0.0-15-generic-64k
Package: linux-signed
Binary: linux-image-7.0.0-15-generic, linux-image-7.0.0-15-generic-64k, linux-image-uc-7.0.0-15-generic, linux-image-uc-7.0.0-15-generic-64k, linux-image-fb-7.0.0-15-generic, linux-image-fb-7.0.0-15-generic-64k, linux-image-7.0.0-15-generic-dbgsym, linux-image-7.0.0-15-generic-64k-dbgsym
"""


def _mock_run(stdout):
    r = MagicMock()
    r.ok.return_value = True
    r.stdout = stdout
    return r


def test_prefers_linux_source_stanza():
    with patch("hyphaed.phases.source.run", return_value=_mock_run(_SHOWSRC_OUTPUT)):
        assert _binary_to_source_pkg("linux-image-7.0.0-15-generic") == "linux"


def test_skips_signed_when_no_source_stanza():
    # Only linux-signed present, no linux-source-* in binaries → fall back to non-signed name.
    output = "Package: linux-signed\nBinary: linux-image-7.0.0-15-generic\n"
    with patch("hyphaed.phases.source.run", return_value=_mock_run(output)):
        assert _binary_to_source_pkg("linux-image-7.0.0-15-generic") == "linux-signed"


def test_single_source_no_signed():
    output = "Package: linux\nBinary: linux-source-7.0.0, linux-headers-7.0.0-15\n"
    with patch("hyphaed.phases.source.run", return_value=_mock_run(output)):
        assert _binary_to_source_pkg("linux-image-7.0.0-15-generic") == "linux"


def test_fallback_on_apt_failure():
    r = MagicMock()
    r.ok.return_value = False
    with patch("hyphaed.phases.source.run", return_value=r):
        assert _binary_to_source_pkg("linux-image-7.0.0-15-generic") == "linux-image-7.0.0-15-generic"


def test_clear_canonical_cert_refs_blanks_both_keys():
    # Real lines as shipped in Canonical's mainline v7.1.1 generic config.
    text = (
        'CONFIG_MODULE_SIG_KEY="certs/signing_key.pem"\n'
        'CONFIG_SYSTEM_TRUSTED_KEYS="debian/canonical-certs.pem"\n'
        'CONFIG_SYSTEM_REVOCATION_KEYS="debian/canonical-revoked-certs.pem"\n'
    )
    out = _clear_canonical_cert_refs(text)
    assert 'CONFIG_SYSTEM_TRUSTED_KEYS=""' in out
    assert 'CONFIG_SYSTEM_REVOCATION_KEYS=""' in out
    assert "canonical-certs.pem" not in out
    assert "canonical-revoked-certs.pem" not in out
    # Unrelated lines (incl. the kernel's own self-generated signing key) untouched.
    assert 'CONFIG_MODULE_SIG_KEY="certs/signing_key.pem"' in out


def test_clear_canonical_cert_refs_noop_when_absent():
    text = 'CONFIG_MODULE_SIG_KEY="certs/signing_key.pem"\n'
    assert _clear_canonical_cert_refs(text) == text


class _FakeProfile:
    def __init__(self, running_kernel):
        self.running_kernel = running_kernel


def test_retarget_running_kernel_falls_back_to_bare_when_no_snapshot(tmp_path):
    """With no configs/base/ snapshot for this version yet, fall back to the
    bare '{mk_ver}-generic' guess — still overrides the stale real kernel a
    prior `detect` re-run left there (the value that produced Ubuntu-ABI-
    shaped version strings for a kernel-org build)."""
    dest = tmp_path / "linux-7.1.3"
    dest.mkdir()
    (dest / "Makefile").write_text("VERSION = 7\nPATCHLEVEL = 1\nSUBLEVEL = 3\n")

    class Ctx:
        repo_root = tmp_path
        profile = _FakeProfile("7.0.0-27-generic")

    ctx = Ctx()
    _retarget_running_kernel(ctx, dest, "kernel-org")
    assert ctx.profile.running_kernel == "7.1.3-generic"


def test_retarget_running_kernel_prefers_existing_abi_suffixed_snapshot(tmp_path):
    """When a fresh clone already snapshotted configs/base/config-7.1.3-070103-generic
    (Canonical's ABI-suffixed form), reusing the checkout must resolve to that
    SAME string, not the bare guess — configure.py's base-config lookup is an
    exact filename match, so a mismatched guess breaks `configure` entirely
    (this was a real regression: 'no base config found at .../config-7.1.3-generic')."""
    dest = tmp_path / "linux-7.1.3"
    dest.mkdir()
    (dest / "Makefile").write_text("VERSION = 7\nPATCHLEVEL = 1\nSUBLEVEL = 3\n")
    base_dir = tmp_path / "configs" / "base"
    base_dir.mkdir(parents=True)
    (base_dir / "config-7.1.3-070103-generic").touch()

    class Ctx:
        repo_root = tmp_path
        profile = _FakeProfile("7.0.0-27-generic")

    ctx = Ctx()
    _retarget_running_kernel(ctx, dest, "kernel-org")
    assert ctx.profile.running_kernel == "7.1.3-070103-generic"


def test_retarget_running_kernel_noop_when_profile_missing(tmp_path):
    dest = tmp_path / "linux-7.1.3"
    dest.mkdir()

    class Ctx:
        repo_root = tmp_path
        profile = None

    ctx = Ctx()
    _retarget_running_kernel(ctx, dest, "kernel-org")  # must not raise
    assert ctx.profile is None


def test_existing_baseline_config_kver_returns_none_when_absent(tmp_path):
    class Ctx:
        repo_root = tmp_path

    assert _existing_baseline_config_kver(Ctx(), "7.1.3") is None
