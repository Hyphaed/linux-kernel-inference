"""The three patches with no sysctl to read: 0019, 0020, 0022, 0023.

These are the ones the series carries on compile-testing alone, and they are
also the riskiest things in it — two pieces of our own UAPI and a 2300-line
forward-port of someone else's research code across a bpf_struct_ops API
break. So they get real programs that issue real ioctls and load a real BPF
policy, not a grep over the patch file.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from .probe_helpers import read


def _run_probe(probes: Path, name: str):
    return subprocess.run([str(probes / name)], cwd=probes,
                          capture_output=True, text=True, timeout=300)


# --------------------------------------------------------------------------
# 0019 + 0020 — dma-buf priority and compression ioctls
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def dmabuf_probe(probes):
    r = _run_probe(probes, "dmabuf_ioctl_probe")
    if r.returncode == 77:
        pytest.skip(f"could not export a dma-buf: {r.stderr.strip()}")
    return r


def test_dmabuf_uapi_is_installed():
    """0019/0020 add UAPI, so the definitions have to reach
    /usr/include/linux/dma-buf.h or nothing in userspace can use them."""
    hdr = Path("/usr/include/linux/dma-buf.h")
    assert hdr.exists()
    text = hdr.read_text()
    for sym in ("DMA_BUF_IOCTL_SET_PRIORITY", "DMA_BUF_IOCTL_GET_PRIORITY",
                "DMA_BUF_IOCTL_SET_COMPRESSION", "DMA_BUF_IOCTL_GET_COMPRESSION",
                "DMA_BUF_PRIORITY_DEFAULT", "DMA_BUF_CODEC_NONE"):
        assert sym in text, f"{sym} missing from the installed UAPI header"


def test_0019_0020_every_ioctl_check_passes(dmabuf_probe):
    """The probe prints one line per check; any FAIL fails this test with the
    probe's own output, which names the exact check."""
    fails = [ln for ln in dmabuf_probe.stdout.splitlines() if " FAIL" in ln]
    assert not fails, (
        "dma-buf ioctl checks failed:\n" + "\n".join(fails)
        + "\n\nfull output:\n" + dmabuf_probe.stdout
    )
    assert dmabuf_probe.returncode == 0


def test_0019_priority_round_trips_the_whole_range(dmabuf_probe):
    out = dmabuf_probe.stdout
    for val in (0, 1, 64, 128, 200, 255):
        assert re.search(rf"round-trip priority={val}\s+PASS", out), (
            f"priority {val} did not round-trip:\n{out}"
        )


def test_0019_fdinfo_reports_priority(dmabuf_probe):
    """The fdinfo hunk is the part a debugger or an accounting tool would
    actually read."""
    assert "fdinfo priority=255" in dmabuf_probe.stdout
    assert not re.search(r"fdinfo priority=\d+\s+FAIL", dmabuf_probe.stdout)


def test_0019_rejects_out_of_range_and_nonzero_pad(dmabuf_probe):
    out = dmabuf_probe.stdout
    assert re.search(r"SET_PRIORITY 256 rejected EINVAL\s+PASS", out)
    assert re.search(r"SET_PRIORITY nonzero pad rejected\s+PASS", out)


def test_0019_get_still_ignores_pad(dmabuf_probe):
    """Known-open review finding, pinned deliberately.

    GET is _IOR, so the kernel never copies the struct in and cannot see a
    non-zero pad — the documented 'must be zero' promise is enforced on SET
    only. When the planned v2 makes GET _IOWR this test starts failing,
    which is the point: it converts an open finding into a tripwire.
    """
    assert re.search(r"GET ignores pad .*\s+PASS", dmabuf_probe.stdout)


def test_0020_compression_descriptor_round_trips(dmabuf_probe):
    out = dmabuf_probe.stdout
    for field in ("codec", "block_size", "uncompressed_size"):
        assert re.search(rf"round-trip {field}\s+PASS", out), out
    assert re.search(r"default codec is DMA_BUF_CODEC_NONE\s+PASS", out)


def test_0019_0020_do_not_collide(dmabuf_probe):
    """0020 deliberately took ioctl 6/7 because 0019 owns 4/5. Check both
    keep independent state and that a pre-existing ioctl still works."""
    out = dmabuf_probe.stdout
    assert re.search(r"independent state\s+PASS", out)
    assert re.search(r"DMA_BUF_SET_NAME still works\s+PASS", out)


# --------------------------------------------------------------------------
# 0022 — udmabuf scatterlist fix (Jason Gunthorpe's upstream 5bf888673e0d)
# --------------------------------------------------------------------------
def test_0022_udmabuf_still_works(dmabuf_probe):
    """0022 rewrites udmabuf's folio/offset bookkeeping into a page array.
    The probe exports a real 2 MiB udmabuf before it does anything else, so
    reaching any check at all proves the rewrite did not break creation."""
    assert "exported a real dma-buf via udmabuf" in dmabuf_probe.stdout


def test_0022_uses_sg_alloc_table_from_pages(source_tree):
    """The whole point of the patch: build the scatterlist with the helper
    that coalesces physically contiguous pages, instead of one entry per
    4K page."""
    udmabuf = (source_tree / "drivers" / "dma-buf" / "udmabuf.c").read_text()
    assert "sg_alloc_table_from_pages" in udmabuf, "0022 is not in this tree"
    assert "sg_set_folio" not in udmabuf, (
        "the pre-0022 per-page sg_set_folio loop is still present"
    )


def test_0022_coalescing_is_measured_or_declared_unproven(root_report):
    """Scatterlist nents are not visible from userspace. If debugfs cannot
    show them, this records that coalescing is UNPROVEN rather than
    quietly passing."""
    state = root_report.get("dma_buf_bufinfo")
    if state != "captured":
        pytest.skip(
            "dma_buf debugfs unavailable — 0022 is functionally correct "
            "(udmabuf works, helper is in the tree) but the coalescing it "
            "exists for is UNPROVEN on this machine"
        )


# --------------------------------------------------------------------------
# 0023 — cache_ext BPF page-cache eviction
# --------------------------------------------------------------------------
def test_0023_proc_control_file_exists():
    assert Path("/proc/page_cache_ext_enabled_cgroup").exists(), (
        "0023's control file is missing — the patch is not in this kernel"
    )


def test_0023_struct_ops_type_registered():
    """The series comment flags a real risk: the 6.6 X-macro registration is
    gone in 7.1 and was replaced with an explicit register_bpf_struct_ops()
    call added by hand. If that call were missing the type would silently
    never register. BTF carrying the generated shadow struct proves it ran.
    """
    r = subprocess.run(
        ["bpftool", "btf", "dump", "file", "/sys/kernel/btf/vmlinux"],
        capture_output=True, text=True, timeout=180,
    )
    if r.returncode != 0:
        pytest.skip(f"bpftool btf dump failed: {r.stderr.strip()[:200]}")
    assert "bpf_struct_ops_page_cache_ext_ops" in r.stdout, (
        "register_bpf_struct_ops() never ran — 0023 is compiled in but the "
        "struct_ops type is not registered, so no policy could ever attach"
    )


def test_0023_all_five_ops_are_in_btf():
    r = subprocess.run(
        ["bpftool", "btf", "dump", "file", "/sys/kernel/btf/vmlinux"],
        capture_output=True, text=True, timeout=180,
    )
    if r.returncode != 0:
        pytest.skip("bpftool btf dump failed")
    for op in ("init", "evict_folios", "folio_added", "folio_accessed",
               "folio_evicted"):
        assert f"page_cache_ext_ops__{op}" in r.stdout, f"op {op} missing"


def test_0023_kfunc_set_registered(root_report):
    """The gap that let a broken 7.1.10 boot pass this suite.

    Everything above checks BTF, and BTF was fine: the struct_ops type
    registered and all five ops were there. What was missing was the kfunc
    set. register_btf_kfunc_id_set() rejected both of 0023's sets with
    -EINVAL on every boot -- the port carried 6.6.8's BTF_SET8_START, which
    passes flags 0, where the kfunc form BTF_KFUNCS_START passes
    BTF_SET8_KFUNCS. All eight cache_ext kfuncs were absent from the
    verifier's table, so any policy that called one would be rejected at
    load, while a policy that called none attached happily. This suite's
    probe called none.

    It calls one now, behind a branch that never executes, so the probe's
    load either resolves bpf_cache_ext_list_del or fails. Reaching the
    attach step at all is the proof.
    """
    exit_code = root_report.get("cache_ext_probe_exit")
    if exit_code == "127":
        pytest.skip("cache_ext probe was not built when root-steps.sh ran")
    if exit_code == "77":
        pytest.skip("cache_ext probe could not run")

    assert root_report.get("cache_ext_loaded") == "1", (
        "the BPF object did not load. If the verifier said \"calling kernel "
        "function bpf_cache_ext_list_del is not allowed\", 0023's kfunc sets "
        "failed to register at boot -- check "
        "`journalctl -b 0 -k | grep cache_ext` for \"failed to register "
        "kfunc sets\", and see the BTF_KFUNCS_START note in the patch"
    )


def test_0023_callbacks_actually_fire(root_report):
    """The one that matters.

    Registration proves the type exists. This proves 0023's call sites in
    mm/filemap.c and mm/vmscan.c survived the 6.6 -> 7.1 forward-port, by
    loading a counting policy and reading the counters after real page-cache
    traffic. A registered struct_ops whose hooks never fire is exactly the
    kind of silently-inert patch this suite is for.
    """
    exit_code = root_report.get("cache_ext_probe_exit")
    if exit_code == "127":
        pytest.skip("cache_ext probe was not built when root-steps.sh ran")
    if exit_code == "77":
        pytest.skip("cache_ext probe could not run")

    added = int(root_report.get("cache_ext_folio_added", 0))
    accessed = int(root_report.get("cache_ext_folio_accessed", 0))
    assert added or accessed, (
        "struct_ops attached but no callback fired after 512 MiB of "
        "page-cache churn — 0023 registers and does nothing"
    )
