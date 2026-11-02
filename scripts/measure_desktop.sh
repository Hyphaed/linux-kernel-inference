#!/usr/bin/env bash
# Usage: measure_desktop.sh OUT.json   (run inside the graphical session)
# 60 s idle sample of gnome-shell CPU/RSS/PSS + GPU memory/utilization.
# Compare two outputs with: python3 scripts/measure_desktop.sh --compare A.json B.json
set -euo pipefail
if [ "${1:-}" = "--compare" ]; then
  python3 - "$2" "$3" <<'PY'
import json,sys
a,b=(json.load(open(p)) for p in sys.argv[1:3]); bad=0
for k in a:
    if isinstance(a[k],(int,float)) and a[k]:
        d=(b[k]-a[k])/a[k]*100; bad+=d>2
        print(f"{k:24} {a[k]:>12.2f} -> {b[k]:>12.2f}  {d:+.1f}%")
print("VERDICT:", "rollback suggested (a metric is worse by >2%)" if bad else "keep")
PY
  exit 0
fi
OUT="${1:?output json}"; SECS="${SECS:-60}"
pid=$(pgrep -u "$USER" -x gnome-shell | head -1) || { echo "gnome-shell not running" >&2; exit 1; }
t0=$(awk '{print $14+$15}' /proc/$pid/stat); sleep "$SECS"; t1=$(awk '{print $14+$15}' /proc/$pid/stat)
cpu=$(python3 -c "print(($t1-$t0)/$(getconf CLK_TCK)/$SECS*100)")
rss=$(awk '/^Rss:/{print $2}' /proc/$pid/smaps_rollup); pss=$(awk '/^Pss:/{print $2}' /proc/$pid/smaps_rollup)
gpu=$(nvidia-smi --query-gpu=memory.used,utilization.gpu --format=csv,noheader,nounits 2>/dev/null | head -1 || echo "0, 0")
vram_shell=$(nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader,nounits 2>/dev/null | awk -F', ' -v p="$pid" '$1==p{print $2}')
cat >"$OUT" <<JSON
{"gnome_shell_cpu_pct": $cpu, "gnome_shell_rss_kb": $rss, "gnome_shell_pss_kb": $pss,
 "gpu_mem_used_mib": ${gpu%%,*}, "gpu_util_pct": ${gpu##*, }, "gnome_shell_vram_mib": ${vram_shell:-0}}
JSON
cat "$OUT"
