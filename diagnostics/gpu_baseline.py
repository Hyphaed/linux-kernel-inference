#!/usr/bin/env python3
"""RTX 5070 headroom check and tensor-rate baseline. Changes nothing.

What it measures, with the clocks, power, temperature and PCIe link logged
while it runs so a number can be told apart from throttling:

  * FP16 tensor rate with FP16 accumulate vs FP32 accumulate, and BF16 (cuBLASLt,
    diagnostics/gpu_mma.cu, built for sm_120 with the newest installed CUDA)
  * torch matmul at FP16, BF16, TF32, FP32 and FP8 (e4m3)
  * device memory bandwidth and pinned host<->device copies over PCIe
  * PCIe generation/width under load, power cap, thermal state, optional soak

There is no tensor-core "unlock" for GB205 today (docs/research/
rtx5070-tensor-unlock-2026-10-28.md). This is the "before" number. It runs
nothing from the unlock repository and never touches firmware, clocks or the
power limit.

  python3 diagnostics/gpu_baseline.py --label before
  python3 diagnostics/gpu_baseline.py --label soak --soak 120

Refuses to run while another job uses the GPU (the card is shared with the
desktop and the 12 GB budget): it lists what's using it instead of killing it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "out" / "gpu"
SRC = Path(__file__).with_name("gpu_mma.cu")
SPEC_MEM_GBPS = 672.0     # 192-bit GDDR7 at 28 Gbps (public spec, inferred: verify against clocks.max.mem)
QUERY = ("clocks.sm,clocks.mem,power.draw,temperature.gpu,pcie.link.gen.current,"
         "pcie.link.width.current,clocks_throttle_reasons.active,utilization.gpu,memory.used")
THROTTLE = {0x1: "idle", 0x2: "app-clocks", 0x4: "SW power cap", 0x8: "HW slowdown", 0x20: "SW thermal",
            0x40: "HW thermal", 0x80: "HW power brake"}


def smi(*args: str) -> str:
    return subprocess.run(["nvidia-smi", *args], capture_output=True, text=True, check=False).stdout.strip()


def gpu_busy() -> tuple[float, float, list[str]]:
    util, used, total = (float(x) for x in smi("--query-gpu=utilization.gpu,memory.used,memory.total",
                                              "--format=csv,noheader,nounits").split(","))
    apps = [f"{l.split(',')[1].strip().split()[0].split('/')[-1]} {l.split(',')[2].strip()}"
            for l in smi("--query-compute-apps=pid,process_name,used_memory", "--format=csv,noheader").splitlines()
            if l.count(",") >= 2]
    return util, total - used, apps


def find_nvcc() -> Path | None:
    cands = sorted(Path("/usr/local").glob("cuda-1[3-9]*/bin/nvcc"), reverse=True)
    which = shutil.which("nvcc")
    return cands[0] if cands else (Path(which) if which else None)


def build_mma() -> Path:
    nvcc = find_nvcc()
    if not nvcc:
        sys.exit("No nvcc found (need CUDA >= 12.8 for sm_120).")
    ver = subprocess.run([str(nvcc), "--version"], capture_output=True, text=True).stdout
    tag = hashlib.sha256(SRC.read_bytes() + ver.encode()).hexdigest()[:12]
    binp = OUT / "bin" / f"gpu_mma-{tag}"
    if not binp.exists():
        binp.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run([str(nvcc), "-O3", "-arch=sm_120", "-Xcompiler", "-O3,-march=native",
                            str(SRC), "-lcublasLt", "-o", str(binp)], capture_output=True, text=True)
        if r.returncode or r.stderr.strip():
            sys.exit(f"nvcc build problem (clean build expected):\n{r.stderr}")
    return binp


class Sampler(threading.Thread):
    """200 ms nvidia-smi samples while a benchmark runs."""
    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.rows: list[dict] = []
        self.proc = subprocess.Popen(["nvidia-smi", f"--query-gpu={QUERY}", "--format=csv,noheader,nounits", "-lms", "200"],
                                     stdout=subprocess.PIPE, text=True)

    def run(self) -> None:
        for line in self.proc.stdout:
            p = [x.strip() for x in line.split(",")]
            try:
                self.rows.append({"t": time.time(), "sm": float(p[0]), "mem": float(p[1]), "w": float(p[2]),
                                  "temp": float(p[3]), "gen": int(p[4]), "width": int(p[5]),
                                  "thr": int(p[6], 16), "util": float(p[7])})
            except (ValueError, IndexError):
                continue

    def stop(self) -> list[dict]:
        self.proc.terminate()
        self.join(timeout=2)
        return self.rows


def decode_throttle(mask: int) -> list[str]:
    return [n for b, n in THROTTLE.items() if mask & b and b != 0x1]


def summarise_samples(rows: list[dict]) -> dict:
    busy = [r for r in rows if r["util"] >= 80] or rows
    if not busy:
        return {}
    mask = 0
    for r in busy:
        mask |= r["thr"]
    return {"samples": len(busy), "sm_mhz_median": statistics.median(r["sm"] for r in busy),
            "sm_mhz_min": min(r["sm"] for r in busy), "power_w_max": max(r["w"] for r in busy),
            "temp_c_max": max(r["temp"] for r in busy), "pcie_gen_max": max(r["gen"] for r in busy),
            "pcie_width": max(r["width"] for r in busy), "throttle": decode_throttle(mask),
            "throttle_raw": "0x%x" % mask}


def run_mma(binp: Path, n: int, iters: int, repeats: int) -> dict:
    r = subprocess.run([str(binp), str(n), str(iters), str(repeats)], capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"gpu_mma failed: {r.stderr.strip()}")
    return {j["mode"]: j for j in map(json.loads, r.stdout.splitlines())}


def torch_bench(n: int, repeats: int, copy_mib: int) -> dict:
    try:
        import torch
    except ImportError:
        return {"error": "torch not importable by this python"}
    out: dict = {}
    dev = "cuda"

    def timed(fn, iters: int) -> float:
        fn(); torch.cuda.synchronize()
        e0, e1 = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
        e0.record()
        for _ in range(iters):
            fn()
        e1.record(); torch.cuda.synchronize()
        return e0.elapsed_time(e1) / 1e3 / iters

    def reps(fn, iters: int, unit_work: float) -> list[float]:
        return [unit_work / timed(fn, iters) for _ in range(repeats)]

    flops = 2.0 * n ** 3 / 1e12
    for name, dtype, prec in (("fp16", torch.float16, None), ("bf16", torch.bfloat16, None),
                              ("tf32", torch.float32, "high"), ("fp32", torch.float32, "highest")):
        try:
            torch.set_float32_matmul_precision(prec or "highest")
            a = torch.randn(n, n, device=dev, dtype=dtype); b = torch.randn(n, n, device=dev, dtype=dtype)
            out[f"matmul_{name}_tflops"] = reps(lambda: a @ b, 20, flops)
            del a, b
        except Exception as e:                                   # noqa: BLE001, record and go on
            out[f"matmul_{name}_tflops"] = {"error": str(e)[:120]}
    try:
        a = torch.randn(n, n, device=dev).to(torch.float8_e4m3fn)
        b = torch.randn(n, n, device=dev).to(torch.float8_e4m3fn).t().contiguous().t()
        one = torch.tensor(1.0, device=dev)
        out["matmul_fp8_e4m3_tflops"] = reps(lambda: torch._scaled_mm(a, b, scale_a=one, scale_b=one,
                                                                       out_dtype=torch.bfloat16), 20, flops)
        del a, b
    except Exception as e:                                       # noqa: BLE001
        out["matmul_fp8_e4m3_tflops"] = {"error": str(e)[:120]}

    size = copy_mib << 20
    x = torch.empty(size // 2, dtype=torch.uint8, device=dev); y = torch.empty_like(x)
    out["mem_d2d_GBps"] = reps(lambda: y.copy_(x), 20, 2.0 * x.numel() / 1e9)     # read + write
    del x, y
    h = torch.empty(size, dtype=torch.uint8, pin_memory=True); d = torch.empty(size, dtype=torch.uint8, device=dev)
    out["pcie_h2d_GBps"] = reps(lambda: d.copy_(h, non_blocking=True), 5, size / 1e9)
    out["pcie_d2h_GBps"] = reps(lambda: h.copy_(d, non_blocking=True), 5, size / 1e9)
    return out


def med(v) -> float | None:
    return statistics.median(v) if isinstance(v, list) and v else None


def rel_sd(v) -> float:
    return statistics.stdev(v) / statistics.mean(v) * 100 if isinstance(v, list) and len(v) > 1 else 0.0


def headline(res: dict) -> list[str]:
    mma, t, s = res["mma"], res["torch"], res["samples"]
    lines = []
    a16, a32, bf = (mma.get(k, {}).get("tflops_median") for k in ("fp16_acc16", "fp16_acc32", "bf16_acc32"))
    if a16 and a32:
        line = f"fp16 tensor: {a16:.1f} TFLOP/s with FP16 accumulate, {a32:.1f} with FP32 accumulate (ratio {a32 / a16:.2f})"
        lines.append(line + (f"; bf16 {bf:.1f}" if bf else ""))
    for k in ("matmul_fp16_tflops", "matmul_bf16_tflops", "matmul_tf32_tflops", "matmul_fp32_tflops", "matmul_fp8_e4m3_tflops"):
        v = med(t.get(k))
        lines.append(f"{k}: {v:.1f}" if v else f"{k}: unavailable ({t.get(k, {}).get('error', 'n/a')})")
    bw = med(t.get("mem_d2d_GBps"))
    if bw:
        lines.append(f"memory copy: {bw:.0f} GB/s = {bw / SPEC_MEM_GBPS * 100:.0f}% of the {SPEC_MEM_GBPS:.0f} GB/s spec")
    h2d, d2h = med(t.get("pcie_h2d_GBps")), med(t.get("pcie_d2h_GBps"))
    if h2d and d2h:
        lines.append(f"PCIe pinned copy: {h2d:.1f} GB/s host-to-device, {d2h:.1f} device-to-host")
    if s:
        lines.append(f"under load: SM {s['sm_mhz_median']:.0f} MHz median, {s['power_w_max']:.0f} W max, "
                     f"{s['temp_c_max']:.0f} C max, PCIe Gen{s['pcie_gen_max']} x{s['pcie_width']}, "
                     f"throttle: {', '.join(s['throttle']) or 'none'} (raw {s['throttle_raw']}, bits beyond the known ones are not decoded)")
    return [l for l in lines if l]


def findings(res: dict) -> list[str]:
    s, t, out = res["samples"], res["torch"], []
    if s and s["pcie_gen_max"] < 4:
        out.append(f"PCIe link reached only Gen{s['pcie_gen_max']} under load (card supports Gen4): check ASPM/slot/BIOS.")
    if s and s["pcie_width"] < 16:
        out.append(f"PCIe link is x{s['pcie_width']}, not x16.")
    if s and any(x in s["throttle"] for x in ("SW power cap", "HW slowdown", "SW thermal", "HW thermal", "HW power brake")):
        out.append(f"Throttling seen during the run ({', '.join(s['throttle'])}): tensor numbers are clock-limited, not hardware-limited.")
    bw = med(t.get("mem_d2d_GBps"))
    if bw and bw < 0.8 * SPEC_MEM_GBPS:
        out.append(f"Memory copy at {bw / SPEC_MEM_GBPS * 100:.0f}% of spec bandwidth (a copy rarely exceeds ~90%).")
    h2d = med(t.get("pcie_h2d_GBps"))
    if h2d and h2d < 20:
        out.append(f"Pinned host-to-device {h2d:.1f} GB/s is low for Gen4 x16 (expect ~24-26): matters for GreenBoost tiering.")
    return out or ["No headroom problems found: link, clocks, power and memory bandwidth look healthy."]


def compare_prev(res: dict, path_glob: str, exclude: Path) -> list[str]:
    prev = sorted(f for f in OUT.glob(path_glob)
                  if f != exclude and json.loads(f.read_text())["ctx"]["size"] == res["ctx"]["size"])
    if not prev:
        return ["No earlier baseline to compare."]
    old = json.loads(prev[-1].read_text())
    if (old["ctx"]["driver"], old["ctx"]["cuda_runtime"]) != (res["ctx"]["driver"], res["ctx"]["cuda_runtime"]):
        return [f"Not comparable with {prev[-1].name}: driver/CUDA changed "
                f"({old['ctx']['driver']}/{old['ctx']['cuda_runtime']} -> {res['ctx']['driver']}/{res['ctx']['cuda_runtime']})."]
    lines = []
    for mode in ("fp16_acc16", "fp16_acc32", "bf16_acc32"):
        a, b = old["mma"].get(mode, {}), res["mma"].get(mode, {})
        if a.get("tflops_median") and b.get("tflops_median"):
            d = (b["tflops_median"] / a["tflops_median"] - 1) * 100
            noise = max(rel_sd(a.get("reps")), rel_sd(b.get("reps")), 1.0)
            lines.append(f"{mode}: {d:+.1f}% vs {prev[-1].name} ({'beyond' if abs(d) > 2 * noise else 'within'} noise ±{noise:.1f}%)")
    return lines or ["Earlier baseline had no comparable modes."]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--label", default="manual")
    p.add_argument("--size", type=int, default=8192)
    p.add_argument("--iters", type=int, default=40)
    p.add_argument("--repeats", type=int, default=5)
    p.add_argument("--soak", type=int, default=0, help="seconds of continuous load to watch clocks and temperature")
    p.add_argument("--copy-mib", type=int, default=1024, help="host<->device test buffer (VRAM use is about 1.5x this)")
    p.add_argument("--force", action="store_true")
    a = p.parse_args()

    util, free_mib, apps = gpu_busy()
    if (util > 10 or free_mib < 3000) and not a.force:
        sys.exit(f"GPU is in use ({util:.0f}% busy, {free_mib / 1024:.1f} GiB free). Numbers taken now would be noise "
                 "and would slow that job. Using the card:\n  " + "\n  ".join(apps or ["(none listed)"]) +
                 "\nStop it, wait, or pass --force.")
    binp = build_mma()
    OUT.mkdir(parents=True, exist_ok=True)
    cuda_rt = re.search(r"CUDA (?:UMD )?Version:\s*([\d.]+)", smi())
    # driver_version is deprecated (removed in CUDA 14.0); older drivers lack kmd_version.
    driver = next((v for f in ("kmd_version", "driver_version")
                   if (v := smi(f"--query-gpu={f}", "--format=csv,noheader"))[:1].isdigit()), "?")
    ctx = {"driver": driver,
           "vbios": smi("--query-gpu=vbios_version", "--format=csv,noheader"),
           "power_limit_w": smi("--query-gpu=power.limit", "--format=csv,noheader,nounits"),
           "persistence": smi("--query-gpu=persistence_mode", "--format=csv,noheader"),
           "max_sm_mhz": smi("--query-gpu=clocks.max.sm", "--format=csv,noheader,nounits"),
           "cuda_runtime": cuda_rt.group(1) if cuda_rt else "?",
           "nvcc": str(find_nvcc()), "size": a.size, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    print(f"[gpu:{a.label}] {ctx['driver']}, CUDA {ctx['cuda_runtime']}, {a.size}^3 GEMMs, {a.repeats} repeats", flush=True)

    sampler = Sampler(); sampler.start()
    mma = run_mma(binp, a.size, a.iters, a.repeats)
    tch = torch_bench(a.size, a.repeats, a.copy_mib)
    soak = {}
    if a.soak:
        print(f"soaking {a.soak}s ...", flush=True)
        end = time.time() + a.soak
        while time.time() < end:
            run_mma(binp, a.size, a.iters, 1)
    rows = sampler.stop()
    samples = summarise_samples(rows)
    if a.soak and rows:
        busy = [r for r in rows if r["util"] >= 80]
        k = max(len(busy) // 5, 1)
        soak = {"sm_mhz_first_fifth": statistics.median(r["sm"] for r in busy[:k]),
                "sm_mhz_last_fifth": statistics.median(r["sm"] for r in busy[-k:]),
                "temp_c_first": busy[0]["temp"], "temp_c_last": busy[-1]["temp"]}
    res = {"label": a.label, "ctx": ctx, "mma": mma, "torch": tch, "samples": samples, "soak": soak}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = OUT / f"baseline-{a.label}-{stamp}.json"
    path.write_text(json.dumps(res, indent=2))

    print("\n== Result")
    for l in headline(res):
        print("  " + l)
    if soak:
        print(f"  soak: SM {soak['sm_mhz_first_fifth']:.0f} -> {soak['sm_mhz_last_fifth']:.0f} MHz, "
              f"{soak['temp_c_first']:.0f} -> {soak['temp_c_last']:.0f} C")
    print("\n== Headroom findings")
    for l in findings(res):
        print("  " + l)
    print("\n== Against the previous baseline")
    for l in compare_prev(res, "baseline-*.json", path):
        print("  " + l)
    print("\nTensor unlock status: none exists for GB205 as of 2026-10-28 (docs/research/rtx5070-tensor-unlock-2026-10-28.md).\n"
          "Keep this file as the 'before' number: " + str(path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
