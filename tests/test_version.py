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


def test_base_git_tag_excludes_flavour():
    p = HardwareProfile(running_kernel="7.0.0-15-generic", flavour="hyphaed")
    assert base_git_tag(p) == "base-7.0.0-15.15"
