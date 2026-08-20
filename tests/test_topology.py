from hyphaed.topology import _parse_cpu_list, _mask, HardwareProfile


def test_parse_cpu_list_simple():
    assert _parse_cpu_list("0,1,2") == [0, 1, 2]
    assert _parse_cpu_list("0-3") == [0, 1, 2, 3]
    assert _parse_cpu_list("0-1,4,8-9") == [0, 1, 4, 8, 9]
    assert _parse_cpu_list("") == []
    assert _parse_cpu_list("17") == [17]


def test_mask_compacts_runs():
    assert _mask([0, 1, 2, 3]) == "0-3"
    assert _mask([0, 1, 3, 4]) == "0-1,3-4"
    assert _mask([5]) == "5"
    assert _mask([]) == ""
    assert _mask(list(range(16))) == "0-15"


def test_mask_idempotent_through_parse():
    for s in ["0-15", "0,2,4", "0-1,4,8-9", "16-31"]:
        assert _mask(_parse_cpu_list(s)) == s


def test_tag_composer_blackwell_wayland():
    p = HardwareProfile(
        cpu_codename="raptorlake-r",
        gpu_vendor="nvidia",
        gpu_arch="blackwell",
        session_type="wayland",
    )
    assert p.tag() == "rptr-nvbw-wl"


def test_tag_composer_amd_x11():
    p = HardwareProfile(
        cpu_codename="amd-fam19",
        gpu_vendor="amd",
        gpu_arch="rdna3",
        session_type="x11",
    )
    # codemap doesn't have amd-fam19 → falls back to first 4 chars
    assert p.tag().startswith("amd-")
    assert p.tag().endswith("-x11")
