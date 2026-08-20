from hyphaed.phases.build import _STEP_RE, _PKG_RE, _PKG_CMD_RE, _PKG_KBUILD_RE, _grow_total


# ---------- _STEP_RE ----------

def test_step_re_cc():
    m = _STEP_RE.match("  CC      drivers/foo/bar.o")
    assert m and m.group(1).strip() == "CC"

def test_step_re_cc_module():
    m = _STEP_RE.match("  CC [M]  net/ipv4/tcp.o")
    assert m and m.group(1).strip() == "CC [M]"

def test_step_re_ld():
    assert _STEP_RE.match("  LD      vmlinux")

def test_step_re_hostcc():
    assert _STEP_RE.match("  HOSTCC  scripts/fixdep")

def test_step_re_rustc():
    assert _STEP_RE.match("  RUSTC   rust/kernel/lib.o")

def test_step_re_no_match_packaging():
    assert _STEP_RE.match("  dpkg-buildpackage -b") is None
    assert _STEP_RE.match("  dh_strip") is None

def test_step_re_no_match_plain_output():
    assert _STEP_RE.match("Kernel: arch/x86/boot/bzImage is ready") is None


# ---------- _PKG_RE ----------

def test_pkg_re_dpkg():
    assert _PKG_RE.match("  dpkg-deb --build debian/linux-image-7.0.0-hyphaed")
    assert _PKG_RE.match("  dpkg-gencontrol -plinux-image-7.1.9-hyphaed")


def test_pkg_re_must_not_match_the_build_launcher():
    """dpkg-buildpackage STARTS the build; it does not start packaging.

    `make bindeb-pkg` invokes it first and it then drives the compile. On the
    real 7.1.9 log it is **line 4 of 61,047** — six lines earlier than the
    dpkg-source this file already guards against. Matching it set
    `packaging = True` immediately, which hid the compile bar and made the
    `if not packaging:` guard discard all 33,266 compile steps, leaving the
    ETA to be computed from the packaging tail alone.

    The genuine boundary is dpkg-gencontrol/dpkg-deb at line 41,196 (67.5%).
    """
    assert _PKG_RE.match("  dpkg-buildpackage -b") is None
    assert _PKG_RE.match("  dpkg-buildpackage -b -uc") is None
    assert _PKG_RE.match(
        "dpkg-buildpackage --build=binary --no-pre-clean --unsigned-changes") is None

def test_pkg_re_dh():
    assert _PKG_RE.match("  dh_strip")
    assert _PKG_RE.match("  dh_install --sourcedir=debian/tmp")
    assert _PKG_RE.match("  dh_compress")

def test_pkg_re_make_install_is_not_packaging():
    """`make[1]: Entering directory ... install` fires all over a kernel build,
    including during the tools and headers stages long before any packaging.
    It was matched as a packaging signal and is not one."""
    assert _PKG_RE.match(
        "make[1]: Entering directory '/build/linux-7.0.0' install") is None

def test_pkg_re_no_match_cc():
    assert _PKG_RE.match("  CC      drivers/foo/bar.o") is None

def test_pkg_re_no_match_dpkg_source():
    assert _PKG_RE.match("dpkg-source: info: applying ubuntu-foo.patch") is None
    assert _PKG_RE.match("dpkg-source: info: unpacking linux_7.0.0.orig.tar.xz") is None
    assert _PKG_RE.match("dpkg-source: info: extracting linux") is None

def test_pkg_re_dpkg_deb_explicit():
    assert _PKG_RE.match("  dpkg-deb: building package 'linux-image-7.0.0-hyphaed'")
    assert _PKG_RE.match("  dpkg-genchanges -b")
    assert _PKG_RE.match("  dpkg-genbuildinfo --build=binary")


# ---------- _PKG_KBUILD_RE ----------

def test_pkg_kbuild_re_install():
    m = _PKG_KBUILD_RE.match("  INSTALL arch/x86/crypto/aegis128-aesni.ko")
    assert m and m.group(1).strip() == "INSTALL" and "aegis128" in m.group(2)

def test_pkg_kbuild_re_install_module():
    m = _PKG_KBUILD_RE.match("  INSTALL [M] net/ipv4/tcp_bbr.ko")
    assert m and m.group(1).strip() == "INSTALL [M]"

def test_pkg_kbuild_re_depmod():
    m = _PKG_KBUILD_RE.match("  DEPMOD  7.0.0-15-hyphaed")
    assert m and m.group(1).strip() == "DEPMOD"

def test_pkg_kbuild_re_sign():
    m = _PKG_KBUILD_RE.match("  SIGN    arch/x86/kvm/kvm.ko")
    assert m and m.group(1).strip() == "SIGN"

def test_pkg_kbuild_re_no_match_cc():
    assert _PKG_KBUILD_RE.match("  CC      drivers/foo/bar.o") is None

def test_pkg_kbuild_re_no_match_pkg():
    assert _PKG_KBUILD_RE.match("  dh_strip") is None
    assert _PKG_KBUILD_RE.match("  dpkg-deb --build foo") is None


# ---------- _PKG_CMD_RE ----------

def test_pkg_cmd_re_dh():
    assert _PKG_CMD_RE.match("  dh_strip").group(1) == "dh_strip"
    assert _PKG_CMD_RE.match("  dh_install --sourcedir=debian/tmp").group(1) == "dh_install"
    assert _PKG_CMD_RE.match("  dh_compress -X .pod").group(1) == "dh_compress"

def test_pkg_cmd_re_dpkg():
    assert _PKG_CMD_RE.match("  dpkg-buildpackage -b").group(1) == "dpkg-buildpackage"
    assert _PKG_CMD_RE.match("  dpkg-deb --build foo").group(1) == "dpkg-deb"
    assert _PKG_CMD_RE.match("  dpkg-genchanges -b").group(1) == "dpkg-genchanges"

def test_pkg_cmd_re_no_match_make():
    assert _PKG_CMD_RE.match("make[1]: Entering directory") is None


# ---------- _grow_total ----------

def test_grow_total_no_change_below_threshold():
    # 20000/22000 = 90.9% < 95% — no growth
    assert _grow_total(22_000, 20_000) == 22_000

def test_grow_total_at_threshold():
    # 20901/22000 = 95.0% — should grow
    result = _grow_total(22_000, 20_901)
    assert result > 22_000

def test_grow_total_fixed_bump_wins_small_total():
    # 10% of 10000 = 1000, fixed bump = 2000 — bump wins
    result = _grow_total(10_000, 9_500)
    assert result == 12_000  # max(10000+2000, int(10000*1.10)) = max(12000, 11000)

def test_grow_total_pct_wins_large_total():
    # 10% of 50000 = 5000, fixed bump = 2000 — pct wins
    result = _grow_total(50_000, 47_600)
    assert result == 55_000  # max(52000, 55000)

def test_grow_total_custom_bump():
    result = _grow_total(18, 18, bump=4)
    assert result == max(22, int(18 * 1.10))

def test_grow_total_idempotent_below_threshold():
    # Multiple calls below threshold must not drift
    t = 22_000
    for c in range(0, 20_900):
        assert _grow_total(t, c) == t


# ---------- estimates: measured, not guessed ----------

def test_step_estimate_matches_a_real_build():
    """_STEP_ESTIMATE must be measured against _STEP_RE — the counter that
    actually increments the bar — not against a looser proxy.

    Replaying out/logs/build-20260820-141842.log gives 33,266 _STEP_RE
    matches. A looser grep over the same log gives 46,532, and using that
    would keep the bar reading low for the whole build. The previous 22,000
    was under by ~1.5x, so the ETA was wrong from the first second.
    """
    from hyphaed.phases.build import _STEP_ESTIMATE
    assert 30_000 <= _STEP_ESTIMATE <= 40_000, (
        f"{_STEP_ESTIMATE} is not within reach of the measured 33,266")


def test_modules_estimate_matches_a_real_build():
    """6,610 INSTALL + 6,610 SIGN + 1 DEPMOD = 13,221 module steps measured.
    The old 50 was under by ~260x, which is why the bar showed '2/50' and an
    ETA of two seconds fourteen seconds into a 37-minute build."""
    from hyphaed.phases.build import _MODULES_ESTIMATE
    assert _MODULES_ESTIMATE >= 10_000, (
        f"{_MODULES_ESTIMATE} cannot describe 13,221 module steps")


def test_module_steps_need_a_ko_target():
    """INSTALL and SIGN are real module steps and must match — but only for a
    .ko. The same log carries 31 bare INSTALL lines that are tools/UAPI
    headers, emitted at line 81 during the tools build."""
    assert _PKG_KBUILD_RE.match("  INSTALL arch/x86/crypto/aegis128-aesni.ko")
    assert _PKG_KBUILD_RE.match("  SIGN    arch/x86/kvm/kvm.ko")
    assert _PKG_KBUILD_RE.match("  INSTALL [M] drivers/net/e1000.ko")
    assert _PKG_KBUILD_RE.match("  DEPMOD  7.1.9-hyphaed")
    assert _PKG_KBUILD_RE.match(
        "  INSTALL /build/tools/bpf/libbpf//include/bpf/bpf.h") is None


# ---------- dpkg-deb goes silent for minutes ----------

def test_deb_build_line_is_recognised():
    """The one line dpkg-deb prints before compressing in silence.

    Reported 2026-08-20 as "appears stuck": the bar sat at
    "100% 14300/14300 · ETA 0:00:00" while dpkg-deb ran at 2520% CPU turning a
    7.7 GB staged tree into a 1.4 GB .deb. It was the busiest moment of the
    build and the UI had nothing to say about it.
    """
    from hyphaed.phases.build import _DEB_BUILD_RE
    m = _DEB_BUILD_RE.match(
        "dpkg-deb: building package 'linux-image-7.1.9-hyphaed-dbg' in "
        "'../linux-image-7.1.9-hyphaed-dbg_7.1.9-070109_amd64.deb'.")
    assert m
    assert m.group(1) == "linux-image-7.1.9-hyphaed-dbg"
    assert m.group(2).endswith("_amd64.deb")


def test_deb_build_line_without_a_path_still_matches():
    """Older dpkg-deb omits the `in '...'` clause; the package name alone is
    still worth showing."""
    from hyphaed.phases.build import _DEB_BUILD_RE
    m = _DEB_BUILD_RE.match("dpkg-deb: building package 'linux-libc-dev'")
    assert m and m.group(1) == "linux-libc-dev"


def test_deb_build_line_is_not_a_generic_packaging_step():
    """It must be handled by the compressing branch, which names the package
    and watches the file grow — not swallowed by the generic bar as a bare
    'dpkg-deb' tick."""
    from hyphaed.phases.build import _DEB_BUILD_RE, _PKG_CMD_RE
    line = "dpkg-deb: building package 'linux-image-7.1.9-hyphaed' in '../x.deb'."
    assert _DEB_BUILD_RE.match(line), "specific handler must claim this line"
    assert _PKG_CMD_RE.match(line).group(1) == "dpkg-deb"


def test_human_bytes_is_readable_at_every_scale():
    from hyphaed.phases.build import _human_bytes
    assert _human_bytes(0) == "0 B"
    assert _human_bytes(159848).endswith("KB")
    assert _human_bytes(432013312).endswith("MB")
    assert _human_bytes(1_388_217_566).endswith("GB")


def test_compression_progress_helpers_degrade_quietly():
    """These read /proc. Every failure mode must return None, never raise —
    a progress indicator must not be able to kill a 37-minute build."""
    from hyphaed.phases.build import _dpkg_deb_compressed_bytes, _find_dpkg_deb_pid
    assert _dpkg_deb_compressed_bytes(999_999_999) is None   # no such process
    assert _dpkg_deb_compressed_bytes(-1) is None            # not even a valid pid
    pid = _find_dpkg_deb_pid()                               # usually None; must not raise
    assert pid is None or isinstance(pid, int)


def test_the_output_deb_is_the_wrong_thing_to_watch():
    """Documents why the obvious implementation was abandoned.

    dpkg-deb streams the compressed data member into a DELETED temp file
    (/tmp/dpkg-deb.XXXXXX) and appends it to the .deb only at the very end.
    Measured 2026-08-20 on the 7.7 GB -dbg package: the output .deb held
    0.2 MB after fifteen minutes while the temp file was at 869 MB, climbing
    to a 1.39 GB final size. An indicator watching the output file would have
    read ~0 for the longest step of the build — indistinguishable from the
    frozen bar it was meant to replace.

    Kept as prose because the behaviour belongs to dpkg-deb, not to us: if a
    future dpkg-deb writes straight through, this test is the record of why
    the code looks the way it does.
    """
    from hyphaed.phases.build import _dpkg_deb_compressed_bytes
    assert _dpkg_deb_compressed_bytes.__doc__
    assert "temp file" in _dpkg_deb_compressed_bytes.__doc__.lower()
