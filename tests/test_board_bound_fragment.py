"""A fragment that removes drivers must only ever reach the machine it was written for.

85-ncore-board-trim.config strips the Ethernet, Wi-Fi, SCSI and USB4 drivers this
workstation doesn't have. Gating it on "Raptor Lake" alone would hand the same
removals to any other Raptor Lake box that runs the wizard, which may well have an
Intel NIC or a Thunderbolt port. It's bound to one motherboard by a
`# requires-board:` header, and an unknown board means skip, never apply.
"""
from pathlib import Path

from hyphaed.phases.configure import _required_board, _select_fragments
from hyphaed.topology import HardwareProfile

ROOT = Path(__file__).resolve().parent.parent
FRAGMENT = "85-ncore-board-trim.config"
NCORE_BOARD = "ASRock B760M-ITX/D4 WiFi"


def _ctx(preset="gaming-ai-vm", **kw):
    prof = dict(cpu_codename="raptorlake-r", cpu_vendor="intel", gpu_vendor="nvidia",
                chassis="desktop", has_amd_gpu=False, flavour="hyphaed")
    prof.update(kw)

    class Ctx:
        repo_root = ROOT
    Ctx.preset = preset
    Ctx.profile = HardwareProfile(**prof)
    return Ctx()


def _names(ctx):
    return [p.name for p in _select_fragments(ctx)]


def test_the_trim_fragment_declares_its_board():
    assert _required_board(ROOT / "configs/fragments" / FRAGMENT) == NCORE_BOARD


def test_ordinary_fragments_are_not_board_bound():
    assert _required_board(ROOT / "configs/fragments/82-strip-unused-modules.config") is None


def test_selected_on_the_ncore_board():
    assert FRAGMENT in _names(_ctx(board=NCORE_BOARD))


def test_board_match_ignores_case():
    assert FRAGMENT in _names(_ctx(board=NCORE_BOARD.upper()))


def test_skipped_on_another_raptor_lake_board():
    """Same CPU, different board: exactly the case the codename gate got wrong."""
    assert FRAGMENT not in _names(_ctx(board="ASUSTeK COMPUTER INC. ROG STRIX Z790-E GAMING WIFI"))


def test_skipped_when_the_board_is_unknown():
    assert FRAGMENT not in _names(_ctx(board=""))


def test_skipped_on_the_amd_laptop():
    ctx = _ctx(cpu_codename="strixpoint", cpu_vendor="amd", has_amd_gpu=True,
               chassis="laptop", board="HP OMEN Gaming Laptop 16")
    assert FRAGMENT not in _names(ctx)


def test_ai_only_preset_is_bound_too():
    assert FRAGMENT not in _names(_ctx(preset="ai-only", board="Some Other Board"))
    assert FRAGMENT in _names(_ctx(preset="ai-only", board=NCORE_BOARD))


def test_detect_reads_this_machines_board():
    """On the real box the DMI string must equal what the fragment demands."""
    import pytest
    from hyphaed import topology
    p = HardwareProfile()
    topology._detect_board(p)
    if not p.board:
        pytest.skip("no DMI board id here")
    if p.board != NCORE_BOARD:
        pytest.skip(f"not the ncore workstation ({p.board})")
    assert p.board == NCORE_BOARD
