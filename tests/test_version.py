from hyphaed.version import derive_kernel_version, hyphaed_uname_r, kdeb_pkgversion, base_git_tag
from hyphaed.topology import HardwareProfile


def test_derive_kernel_version_typical():
    assert derive_kernel_version("7.0.0-15-generic") == ("7.0.0", "15.15")
    assert derive_kernel_version("6.8.0-50-generic") == ("6.8.0", "50.50")


def test_derive_kernel_version_handles_missing_abi():
    # malformed; should default abi to 1
    assert derive_kernel_version("7.0.0") == ("7.0.0", "1.1")


def test_hyphaed_uname_r_strips_flavour():
    assert hyphaed_uname_r("7.0.0-15-generic", "hyphaed") == "7.0.0-15-hyphaed"


def test_hyphaed_uname_r_kernel_org_has_no_abi_segment():
    # A vanilla kernel.org build's real `uname -r` is just
    # {VERSION.PATCHLEVEL.SUBLEVEL}{LOCALVERSION} — confirmed against a real
    # install ("7.1.1-hyphaed", not "7.1.1-070101-hyphaed"). The synthetic
    # "{ver}-{canonical_abi}-generic" string source.py uses for
    # profile.running_kernel must NOT leak a fake ABI segment into the
    # actual boot-time kernel release name install.py/postinstall.py use to
    # find /boot/initrd.img-<kver> and /lib/modules/<kver>/.
    assert hyphaed_uname_r("7.1.1-070101-generic", "hyphaed", "kernel-org") == "7.1.1-hyphaed"


def test_hyphaed_uname_r_ubuntu_mode_unaffected_by_source_mode_default():
    assert hyphaed_uname_r("7.0.0-15-generic", "hyphaed", "ubuntu") == "7.0.0-15-hyphaed"


def test_kdeb_pkgversion_round_trip():
    p = HardwareProfile(
        running_kernel="7.0.0-15-generic",
        cpu_codename="raptorlake-r",
        gpu_vendor="nvidia",
        gpu_arch="blackwell",
        session_type="wayland",
        flavour="hyphaed",
    )
    assert kdeb_pkgversion(p) == "7.0.0-15.15hyphaed1+rptr-nvbw-wl"


def test_kdeb_pkgversion_kernel_org_mode_ignores_synthetic_abi():
    # source.py's kernel-org fetch path synthesizes profile.running_kernel as
    # "{mk_ver}-generic" (no real Ubuntu ABI segment — see
    # test_hyphaed_uname_r_kernel_org_has_no_abi_segment above). Every real
    # 7.2.x kernel-org build has hit kernel.ubuntu.com/mainline 404s (7.2.7,
    # 7.2.8), so this exact "-generic"-suffixed shape is the live path, not
    # an edge case. kdeb_pkgversion's kernel-org branch must not derive
    # anything from that fake ABI ("generic") — build.py's call site
    # previously omitted source_mode entirely, defaulting to "ubuntu" and
    # producing "7.2.7-generic.generichyphaed1+..." as the real installed
    # package version.
    p = HardwareProfile(
        running_kernel="7.2.7-generic",
        cpu_codename="raptorlake-r",
        gpu_vendor="nvidia",
        gpu_arch="blackwell",
        session_type="wayland",
        flavour="hyphaed",
    )
    got = kdeb_pkgversion(p, source_mode="kernel-org")
    assert "generic" not in got
    assert got == "7.2.7-1+rptr-nvbw-wl"


def test_base_git_tag_excludes_flavour():
    p = HardwareProfile(running_kernel="7.0.0-15-generic", flavour="hyphaed")
    assert base_git_tag(p) == "base-7.0.0-15.15"
