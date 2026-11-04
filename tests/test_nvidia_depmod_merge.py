from hyphaed.phases.nvidia import _merge_depmod_conf


def test_merge_keeps_other_kernels_and_replaces_target():
    old = ("# x\noverride nvidia 7.2.14-hyphaed extra/nvidia-hyphaed\n"
           "override nvidia 7.2.15-hyphaed stale\n")
    out = _merge_depmod_conf(old, "7.2.15-hyphaed", ["nvidia", "nvidia-drm"])
    assert "override nvidia 7.2.14-hyphaed extra/nvidia-hyphaed" in out
    assert "stale" not in out
    assert "override nvidia_drm 7.2.15-hyphaed extra/nvidia-hyphaed" in out
    assert out.count("# hyphaed") == 1


def test_merge_from_empty():
    assert "override nvidia 7.2.15-hyphaed" in _merge_depmod_conf("", "7.2.15-hyphaed", ["nvidia"])
