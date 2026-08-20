from hyphaed.state import PersistedState


def test_persisted_state_round_trip(tmp_path):
    s = PersistedState(
        profile={"cpu_codename": "raptorlake-r", "p_cores": 8},
        source_dir="/tmp/linux-7.0.0",
        kernel_pkgver="7.0.0-15.15hyphaed1+rptr-nvbw-wl",
        preset="gaming-ai-vm",
        built_debs=["/tmp/a.deb"],
        packaged_debs=[],
    )
    s.mark_complete("detect", 1.23)
    s.mark_complete("source", 2.34)
    s.save(tmp_path)

    loaded = PersistedState.load(tmp_path)
    assert loaded.profile == s.profile
    assert loaded.source_dir == s.source_dir
    assert loaded.kernel_pkgver == s.kernel_pkgver
    assert loaded.phases_completed == ["detect", "source"]
    assert loaded.phase_timings_sec == {"detect": 1.23, "source": 2.34}


def test_persisted_state_mark_complete_idempotent(tmp_path):
    s = PersistedState()
    s.mark_complete("detect", 1.0)
    s.mark_complete("detect", 2.0)  # re-run; timing updates, list doesn't grow
    assert s.phases_completed == ["detect"]
    assert s.phase_timings_sec["detect"] == 2.0


def test_persisted_state_load_missing_file_is_empty(tmp_path):
    s = PersistedState.load(tmp_path)
    assert s.profile is None
    assert s.phases_completed == []


def test_persisted_state_load_corrupt_file_is_empty(tmp_path):
    (tmp_path / "ctx.json").write_text("{ not valid json")
    s = PersistedState.load(tmp_path)
    assert s.profile is None


_ORDER = ["detect", "source", "patch", "configure", "security", "build", "package", "install", "postinstall"]


def test_invalidate_from_drops_phase_and_later_only():
    s = PersistedState(built_debs=["/tmp/a.deb"], packaged_debs=["/tmp/a.deb"], kernel_pkgver="7.1.6-x")
    for i, name in enumerate(_ORDER):
        s.mark_complete(name, float(i))

    s.invalidate_from("patch", _ORDER)

    assert s.phases_completed == ["detect", "source"]
    assert set(s.phase_timings_sec) == {"detect", "source"}
    assert s.built_debs == []
    assert s.packaged_debs == []
    assert s.kernel_pkgver == ""


def test_invalidate_from_unknown_phase_is_noop():
    s = PersistedState(built_debs=["/tmp/a.deb"])
    s.mark_complete("detect", 1.0)
    s.invalidate_from("not-a-real-phase", _ORDER)
    assert s.phases_completed == ["detect"]
    assert s.built_debs == ["/tmp/a.deb"]
