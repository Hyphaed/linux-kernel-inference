#!/usr/bin/env python3
"""Did every fragment line land in the built .config? (pitfalls #1/#2/#3/#7 in CLAUDE.md)

Uses the real _select_fragments() with the live HardwareProfile, so it audits what a
configure run on THIS machine would merge, including the generated topology fragment.
Exit 1 on any mismatch. Also diffs the .config against the previous build's.
"""
import os, re, subprocess, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]; sys.path.insert(0, str(ROOT))
ver = os.environ["VER"]; prev = os.environ.get("PREV")
from hyphaed.cli import _make_ctx, build_parser, resolve_preset
from hyphaed.phases.configure import _select_fragments
from hyphaed.kconfig import parse_config
from hyphaed.topology import detect

args = build_parser().parse_args(["--dry-run", "detect"])
ctx = _make_ctx(args); ctx.profile = detect(); ctx.preset = resolve_preset(ctx.preset, ctx.profile)
gen = ROOT / "build/generated/90-machine-topology.config"
frags = _select_fragments(ctx, generated=gen if gen.exists() else None)
cfg_path = ROOT / f"build/linux-{ver}/.config"
cfg = parse_config(cfg_path); tree = ROOT / f"build/linux-{ver}"
line_re = re.compile(r"^(CONFIG_\w+)=(.*)$|^# (CONFIG_\w+) is not set")
bad = []
def kdecl(sym):
    r = subprocess.run(["grep", "-rEl", f"^[[:space:]]*(menu)?config[[:space:]]+{sym[7:]}\\b", "--include=Kconfig*", str(tree)],
                       capture_output=True, text=True)
    return bool(r.stdout.strip())
for f in frags:
    for ln in f.read_text().splitlines():
        m = line_re.match(re.sub(r"\s+#(?!\s*CONFIG_).*$", "", ln.strip()) if not ln.lstrip().startswith("#") else ln.strip())
        if not m: continue
        sym, want = (m.group(1), m.group(2).strip('"')) if m.group(1) else (m.group(3), "n")
        got = cfg.get(sym, "n").strip('"')
        if got == want: continue
        if not kdecl(sym):
            bad.append((f.name, sym, want, got, "symbol does not exist in this tree (decoration, #2)"))
        else:
            sel = subprocess.run(["grep", "-rn", f"select {sym[7:]}\\b", "--include=Kconfig*", str(tree)],
                                 capture_output=True, text=True).stdout.strip().splitlines()[:2]
            why = "forced by select (#1): " + "; ".join(s.split(":", 2)[0].split("/linux-")[-1] + ":" + s.split(":")[1] for s in sel) if sel \
                  else "declared but did not land: check depends-on / prompt `if` clause (#3)"
            bad.append((f.name, sym, want, got, why))
# bool-without-its-string (#7)
if cfg.get("CONFIG_BPF_LSM") == "y" and "bpf" not in cfg.get("CONFIG_LSM", ""):
    bad.append(("(derived)", "CONFIG_LSM", "contains bpf", cfg.get("CONFIG_LSM", ""), "BPF_LSM=y but not in LSM= string (#7)"))
# Triaged 2026-10-12: lines that can't land on THIS hardware or are forced by a select, each with
# its reason. A mismatch NOT in here is new and fails the run. Don't add to it without a reason.
EXPECTED = {
    "CONFIG_MITIGATION_UNRET_ENTRY": "AMD-only (needs CPU_SUP_AMD); the Intel desktop build has CPU_SUP_AMD=n, the laptop preset gets it",
    "CONFIG_MITIGATION_SRSO": "AMD-only (needs CPU_SUP_AMD), same as UNRET_ENTRY",
    "CONFIG_KVM_AMD": "AMD-only; absent on the Intel desktop build by design",
    "CONFIG_DRM_I915_GVT": "forced y by a select in drivers/gpu/drm/i915/Kconfig; flipping it changes the .config and no measurement asks for that",
    "CONFIG_GAMEPORT": "forced m by selects in drivers/input/joystick/Kconfig; harmless module",
}
expected = [b for b in bad if b[1] in EXPECTED]
bad = [b for b in bad if b[1] not in EXPECTED]
print(f"audited {len(frags)} fragments against {cfg_path.relative_to(ROOT)}")
for b in expected: print("expected  %s: %s want=%s got=%s  <- %s" % (*b[:4], EXPECTED[b[1]]))
for b in bad: print("MISMATCH %s: %s want=%s got=%s  <- %s" % b)
if prev and (ROOT / f"build/linux-{prev}/.config").exists():
    old = parse_config(ROOT / f"build/linux-{prev}/.config")
    d = {k: (old.get(k), cfg.get(k)) for k in sorted(set(old) | set(cfg)) if old.get(k) != cfg.get(k)}
    print(f"\n.config vs {prev}: {len(d)} symbol differences" + ("" if len(d) else " (identical)"))
    for k, (o, n) in list(d.items())[:60]: print(f"  {k}: {o} -> {n}")
sys.exit(1 if bad else 0)
