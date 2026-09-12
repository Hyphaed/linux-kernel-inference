#!/usr/bin/env bash
# Multi-size THP (mTHP) A/B harness for the platform-kernel-levers sweep
# (Phase 3, L1). Walks each hugepage size's sysfs knob, runs a real
# mmap-and-touch workload under it, and reads /proc/vmstat's thp_* counters
# before and after.
#
# IMPORTANT — what this script proves and what it does not: this confirms
# whether flipping the mTHP knob actually moves a counter. That is a
# mechanism check, not a throughput claim — the same distinction that got
# patch 0016 withdrawn (its commit message asserted a stall it never
# measured; thp_fault_fallback sat at 0 across the whole run because the
# faulting condition was never reached). A counter that does not move means
# the lever was never tested, whatever the sysfs write claimed to do.
#
# Default workload is a synthetic anonymous mmap-touch (stress-ng --vm),
# which is enough to prove the MECHANISM fires. It is NOT evidence of any
# inference throughput change — for that, pass --file pointing at the real
# served GGUF (get the path from `gb synapse status` / `synapse_status`,
# never guess it) so the touched pages are the real model-load path.
#
#   sudo bash diagnostics/bench-mm.sh                          # synthetic, all sizes
#   sudo bash diagnostics/bench-mm.sh --sizes 64,128,256        # subset
#   sudo bash diagnostics/bench-mm.sh --file /path/to/model.gguf --label real-load
set -uo pipefail

SIZES="16,32,64,128,256,512,1024"
MODES="madvise,always"
LABEL="mthp-sweep"
FILE=""
DURATION=8
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sizes) SIZES="${2:-$SIZES}"; shift 2 ;;
    --modes) MODES="${2:-$MODES}"; shift 2 ;;
    --label) LABEL="${2:-$LABEL}"; shift 2 ;;
    --file)  FILE="${2:-}"; shift 2 ;;
    --duration) DURATION="${2:-8}"; shift 2 ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

hdr()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
  echo "Needs root: sysfs writes to transparent_hugepage/, drop_caches, perf counters." >&2
  echo "Re-run: sudo $0" >&2
  exit 1
fi

THP_ROOT=/sys/kernel/mm/transparent_hugepage
OUT_DIR="out/bench"; mkdir -p "$OUT_DIR"
TS="$(date +%Y%m%d-%H%M%S)"
OUT_JSON="$OUT_DIR/mm-${LABEL}-${TS}.json"

hdr "Baseline state, before this script touches anything"
echo "  hugepages-2048kB/enabled : $(cat "$THP_ROOT/enabled" 2>/dev/null)"
echo "  shmem_enabled            : $(cat "$THP_ROOT/shmem_enabled" 2>/dev/null)"
for kb in ${SIZES//,/ }; do
  f="$THP_ROOT/hugepages-${kb}kB/enabled"
  [[ -f "$f" ]] && echo "  hugepages-${kb}kB/enabled : $(cat "$f")"
done

if [[ -n "$FILE" ]]; then
  [[ -r "$FILE" ]] || { bad "--file $FILE not readable"; exit 1; }
  ok "using REAL file for the touch workload: $FILE"
else
  warn "no --file given — using a synthetic anonymous mmap (stress-ng --vm). This"
  echo "      proves the mechanism, not a real workload's benefit. See header comment."
  command -v stress-ng >/dev/null 2>&1 || { bad "stress-ng not installed; run bench-install-deps.sh --go"; exit 1; }
fi

vmstat_thp() {
  grep -E '^thp_' /proc/vmstat
}

run_touch() {
  # Touch enough memory, long enough, for khugepaged/fault-time THP to have
  # a real chance to engage. Runs as the invoking (non-root) user when
  # $SUDO_USER is set, so page ownership matches a normal process.
  local runner="${SUDO_USER:-root}"
  if [[ -n "$FILE" ]]; then
    python3 - "$FILE" "$DURATION" <<'PYEOF'
import mmap, sys, time, os
path, duration = sys.argv[1], float(sys.argv[2])
fd = os.open(path, os.O_RDONLY)
sz = os.fstat(fd).st_size
mm = mmap.mmap(fd, sz, prot=mmap.PROT_READ)
end = time.time() + duration
step = mmap.PAGESIZE * 32
touched = 0
while time.time() < end:
    for off in range(0, sz, step):
        _ = mm[off]
        touched += 1
        if time.time() >= end:
            break
mm.close(); os.close(fd)
print(f"touched {touched} pages of {path}", file=sys.stderr)
PYEOF
  else
    sudo -u "$runner" stress-ng --vm 4 --vm-bytes 25% --vm-method all \
      --timeout "${DURATION}s" --metrics-brief 2>&1 | sed 's/^/    /'
  fi
}

declare -A results
for kb in ${SIZES//,/ }; do
  f="$THP_ROOT/hugepages-${kb}kB/enabled"
  [[ -f "$f" ]] || { warn "hugepages-${kb}kB does not exist on this kernel, skipping"; continue; }
  for mode in ${MODES//,/ }; do
    hdr "size=${kb}kB mode=${mode}"
    ORIG="$(cat "$f")"
    if ! echo "$mode" > "$f" 2>/dev/null; then
      warn "could not set $f to $mode, skipping"
      continue
    fi
    ok "set hugepages-${kb}kB/enabled=$mode"
    sync; echo 1 > /proc/sys/vm/drop_caches 2>/dev/null || true

    before="$(vmstat_thp)"
    run_touch
    after="$(vmstat_thp)"

    echo "  thp_fault_alloc / thp_fault_fallback delta:"
    diff <(echo "$before") <(echo "$after") | grep -E '^[<>]' | sed 's/^/    /' || echo "    (no change)"

    key="${kb}kB_${mode}"
    results["$key"]="$(python3 -c "
import sys
b='''$before'''; a='''$after'''
def parse(s):
    d={}
    for line in s.strip().splitlines():
        k,v = line.split()
        d[k]=int(v)
    return d
bd, ad = parse(b), parse(a)
delta = {k: ad.get(k,0)-bd.get(k,0) for k in ad}
import json; print(json.dumps(delta))
")"

    echo "$ORIG" > "$f" 2>/dev/null || true
  done
done

hdr "Writing $OUT_JSON"
{
  echo "{"
  echo "  \"label\": \"$LABEL\","
  echo "  \"timestamp\": \"$TS\","
  echo "  \"workload\": \"${FILE:-synthetic-stress-ng-vm}\","
  echo "  \"results\": {"
  first=1
  for key in "${!results[@]}"; do
    [[ $first -eq 0 ]] && echo ","
    first=0
    printf '    "%s": %s' "$key" "${results[$key]}"
  done
  echo
  echo "  }"
  echo "}"
} > "$OUT_JSON"
ok "wrote $OUT_JSON"

hdr "Restored original state"
echo "  hugepages-2048kB/enabled : $(cat "$THP_ROOT/enabled" 2>/dev/null)"
for kb in ${SIZES//,/ }; do
  f="$THP_ROOT/hugepages-${kb}kB/enabled"
  [[ -f "$f" ]] && echo "  hugepages-${kb}kB/enabled : $(cat "$f")"
done

hdr "Reading the result"
echo "  Any size/mode whose thp_fault_alloc delta is 0 never got a chance to"
echo "  allocate a hugepage of that size under this workload — report that as"
echo "  'not exercised', not as 'no benefit'. A real verdict needs a nonzero"
echo "  thp_fault_alloc AND a change in the workload's own wall-clock time."
