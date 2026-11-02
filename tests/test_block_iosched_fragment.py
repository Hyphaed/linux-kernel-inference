"""44-block-iosched.config pins the I/O schedulers the perf tooling relies on.

2026-10-28: /sys/block/nvme0n1/queue/scheduler listed only `none mq-deadline`
and kyber was wrongly written off as "not built". It was built as a module
(CONFIG_MQ_IOSCHED_KYBER=m) and simply not loaded, because the scheduler file
shows loaded schedulers only. No fragment pinned it, so a base-config change
could have dropped it unnoticed.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRAGMENT = ROOT / "configs" / "fragments" / "44-block-iosched.config"


def _assignments(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text().splitlines():
        m = re.match(r"(CONFIG_\w+)=(\S+)", line.strip())
        if m:
            out[m.group(1)] = m.group(2)
    return out


def test_fragment_pins_kyber_bfq_and_deadline():
    got = _assignments(FRAGMENT)
    assert got["CONFIG_MQ_IOSCHED_KYBER"] in ("m", "y")
    assert got["CONFIG_IOSCHED_BFQ"] in ("m", "y")
    assert got["CONFIG_MQ_IOSCHED_DEADLINE"] == "y"   # xanmod 0010/0011 patch it


def test_every_preset_lists_the_fragment():
    for preset in (ROOT / "configs" / "presets").glob("*.yaml"):
        assert "44-block-iosched.config" in preset.read_text(), preset.name


def test_no_other_fragment_turns_the_schedulers_off():
    for frag in (ROOT / "configs" / "fragments").glob("*.config"):
        if frag == FRAGMENT:
            continue
        for sym, val in _assignments(frag).items():
            if sym in ("CONFIG_MQ_IOSCHED_KYBER", "CONFIG_IOSCHED_BFQ", "CONFIG_MQ_IOSCHED_DEADLINE"):
                assert val != "n", f"{frag.name} disables {sym}"
