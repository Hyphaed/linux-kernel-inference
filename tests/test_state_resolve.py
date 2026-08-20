"""Auto-prerequisite resolution: --phase X should run earlier phases that
weren't in state.phases_completed. Covered by inspecting the deps dict.
"""
from hyphaed import phases
from hyphaed.cli import _phase_deps


def test_phase_order_is_consistent():
    expected = ["detect", "source", "patch", "configure", "security", "build", "package", "install", "postinstall"]
    assert phases.ORDER == expected


def test_all_phases_have_modules():
    for name in phases.ORDER:
        mod = phases.MODULES[name]
        assert hasattr(mod, "run_phase"), f"{name} has no run_phase"
        assert mod.NAME == name, f"{name} module NAME mismatch"


def test_every_non_detect_phase_has_deps_entry():
    deps = _phase_deps()
    for name in phases.ORDER:
        if name == "detect":
            continue
        assert name in deps, f"{name} missing from _phase_deps()"
        assert deps[name], f"{name} has an empty prerequisite list"


def test_security_is_a_prerequisite_of_build_and_later():
    deps = _phase_deps()
    for name in ("build", "package", "install", "postinstall"):
        assert "security" in deps[name], f"security missing from {name} prerequisites"


def test_patch_is_a_prerequisite_of_configure_and_later():
    # Regression guard for the 2026-07-09 bug: patch omitted from this dict
    # meant `--phase package` could silently build a vanilla, unpatched kernel.
    deps = _phase_deps()
    for name in ("configure", "build", "package", "install", "postinstall"):
        assert "patch" in deps[name], f"patch missing from {name} prerequisites"
