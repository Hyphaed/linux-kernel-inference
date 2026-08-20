#!/usr/bin/env bash
# Read-only diagnostic: does the RTX 5070's PCIe link actually renegotiate
# to full Gen4 x16 (16GT/s — this board's actual ceiling, confirmed via
# `nvidia-smi --query-gpu=pcie.link.gen.max` reporting 4, not 5) under real
# GPU load, or does it stay parked at the idle-downclocked 2.5GT/s seen in
# the earlier probe? (Corrected 2026-08-10: an earlier version of this
# script checked for 32GT/s/Gen5, which this Gen4-only card can never reach
# — that would have made the "reaches full speed" branch below always look
# like a problem, a false positive baked into the script itself, not a
# real link issue.)
#
# Drives a short CUDA matmul loop (as the invoking user, not root) while
# polling `lspci -vvv` LnkSta every 0.5s. Nothing here writes any setting —
# it only reads link state and runs a throwaway torch workload.
#
# Usage: sudo ./diagnostics/gpu-link-speed-under-load.sh > gpu-link-output.txt 2>&1
# Then share gpu-link-output.txt back for analysis.

set -uo pipefail
exec < /dev/null

if [[ $EUID -ne 0 ]]; then
    echo "Re-run with sudo: sudo $0" >&2
    exit 1
fi

GPU_BDF="${1:-01:00.0}"
DURATION="${2:-20}"
RUN_AS_USER="${SUDO_USER:-$(logname 2>/dev/null || echo root)}"

sep() { printf '\n========== %s ==========\n' "$1"; }

sep "baseline (idle) link state"
lspci -s "$GPU_BDF" -vvv 2>&1 | grep -iE "lnksta|lnkcap"

sep "launching CUDA matmul load for ${DURATION}s as user '$RUN_AS_USER'"
LOAD_SCRIPT="$(mktemp /tmp/gpu-load-XXXXXX.py)"
cat > "$LOAD_SCRIPT" <<'PYEOF'
import sys
import time
import torch

duration = float(sys.argv[1])
device = torch.device("cuda")
a = torch.randn(8192, 8192, device=device, dtype=torch.float16)
b = torch.randn(8192, 8192, device=device, dtype=torch.float16)
end = time.time() + duration
n = 0
while time.time() < end:
    c = a @ b
    torch.cuda.synchronize()
    n += 1
print(f"matmul iterations: {n}", file=sys.stderr)
PYEOF
chown "$RUN_AS_USER" "$LOAD_SCRIPT" 2>/dev/null || true

PYTHON_BIN="$(eval echo "~$RUN_AS_USER")/.miniforge3/bin/python3"
if [[ ! -x "$PYTHON_BIN" ]]; then
    PYTHON_BIN="python3"
    echo "  (miniforge python not found at expected path, falling back to plain python3 — torch may be missing)"
fi
sudo -u "$RUN_AS_USER" "$PYTHON_BIN" "$LOAD_SCRIPT" "$DURATION" &
LOAD_PID=$!

sep "polling LnkSta every 0.5s while load runs"
declare -A seen_speeds
samples=0
while kill -0 "$LOAD_PID" 2>/dev/null; do
    line="$(lspci -s "$GPU_BDF" -vvv 2>&1 | grep -i 'lnksta:' | head -1)"
    speed="$(echo "$line" | grep -oP 'Speed \K[0-9.]+GT/s' | head -1)"
    echo "  sample #$samples: $line"
    [[ -n "$speed" ]] && seen_speeds["$speed"]=$(( ${seen_speeds["$speed"]:-0} + 1 ))
    samples=$((samples + 1))
    sleep 0.5
done
wait "$LOAD_PID" 2>/dev/null
rm -f "$LOAD_SCRIPT"

sep "speed distribution observed during load ($samples samples)"
for s in "${!seen_speeds[@]}"; do
    echo "  $s: ${seen_speeds[$s]} samples"
done

sep "settle (2s) + post-load idle link state"
sleep 2
lspci -s "$GPU_BDF" -vvv 2>&1 | grep -iE "lnksta|lnkcap"

sep "nvidia-smi utilization log during the window (for cross-reference, if available)"
command -v nvidia-smi >/dev/null && nvidia-smi --query-gpu=utilization.gpu,clocks.sm,clocks.mem --format=csv || echo "nvidia-smi not available"

sep "Done"
echo "If the speed distribution above never reaches 16GT/s (this board's real Gen4 x16 ceiling — see the header comment) during load, that's a real link-training problem worth chasing. If it does, the idle 2.5GT/s seen earlier was benign power management."
