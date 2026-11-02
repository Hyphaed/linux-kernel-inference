"""diagnostics/gpu_baseline.py and optimize_loop.py: the parts that decide what is reported.

No GPU needed. Also guards the promise in the docstring: the baseline reads the
card and never changes clocks, power limit or firmware.
"""
import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, REPO / "diagnostics" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


G = _load("gpu_baseline")


def _res(**over):
    base = {"ctx": {"driver": "615.71.09", "cuda_runtime": "13.4", "size": 8192},
            "mma": {"fp16_acc16": {"tflops_median": 100.0, "reps": [100, 100, 100]},
                    "fp16_acc32": {"tflops_median": 60.0, "reps": [60, 60, 60]},
                    "bf16_acc32": {"tflops_median": 60.0, "reps": [60, 60, 60]}},
            "torch": {"mem_d2d_GBps": [600.0], "pcie_h2d_GBps": [24.0], "pcie_d2h_GBps": [25.0]},
            "samples": {"pcie_gen_max": 4, "pcie_width": 16, "throttle": [], "throttle_raw": "0x0",
                        "sm_mhz_median": 2780, "power_w_max": 290, "temp_c_max": 70}}
    base.update(over)
    return base


def test_throttle_decode_ignores_idle_and_undecoded_bits():
    assert G.decode_throttle(0x400) == []                       # seen on this card at idle, not a throttle
    assert G.decode_throttle(0x4 | 0x20) == ["SW power cap", "SW thermal"]
    assert G.decode_throttle(0x1) == []


def test_healthy_card_reports_no_problems():
    assert G.findings(_res())[0].startswith("No headroom problems")


def test_pcie_gen_below_4_under_load_is_flagged():
    r = _res()
    r["samples"]["pcie_gen_max"] = 3
    assert any("Gen3" in f for f in G.findings(r))


def test_throttling_is_called_out_as_clock_limited_not_hardware_limited():
    r = _res()
    r["samples"]["throttle"] = ["SW power cap"]
    assert any("clock-limited" in f for f in G.findings(r))


def test_headline_reports_the_accumulate_ratio():
    assert "ratio 0.60" in G.headline(_res())[0]


def test_baselines_with_different_driver_or_size_are_not_compared(tmp_path, monkeypatch):
    monkeypatch.setattr(G, "OUT", tmp_path)
    old = _res(); old["ctx"]["driver"] = "610.88"
    (tmp_path / "baseline-a-1.json").write_text(json.dumps(old))
    cur = tmp_path / "baseline-b-2.json"
    assert "Not comparable" in G.compare_prev(_res(), "baseline-*.json", cur)[0]
    other_size = _res(); other_size["ctx"]["size"] = 4096
    (tmp_path / "baseline-a-1.json").write_text(json.dumps(other_size))
    assert G.compare_prev(_res(), "baseline-*.json", cur) == ["No earlier baseline to compare."]


def test_a_run_is_never_compared_with_itself(tmp_path, monkeypatch):
    monkeypatch.setattr(G, "OUT", tmp_path)
    cur = tmp_path / "baseline-x-1.json"
    cur.write_text(json.dumps(_res()))
    assert G.compare_prev(_res(), "baseline-*.json", cur) == ["No earlier baseline to compare."]


def test_gpu_tools_never_change_clocks_power_or_firmware():
    for f in ("gpu_baseline.py", "gpu_mma.cu", "optimize_loop.py"):
        text = (REPO / "diagnostics" / f).read_text()
        # reads of /sys/bus/pci are fine (link speed); writes and setpci are not
        for forbidden in ("nvidia-smi\", \"-pl", "-lgc", "-rgc", "nvflash", "nvidia-settings", "NVreg",
                          "setpci", "> /sys/bus/pci", ">/sys/bus/pci"):
            assert forbidden not in text, f"{f} mentions {forbidden}"
