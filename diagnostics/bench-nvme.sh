#!/usr/bin/env bash
# NVMe A/B harness for the platform-kernel-levers sweep (Phase 3, L2/L3 in
# docs/plan_patches.md's successor). Every job is READ-ONLY (fio --readonly
# refuses anything with a write component) against the raw block device
# /dev/nvme0n1 — this is the LUKS-encrypted root disk (dm_crypt-0 -> LVM ->
# /), so bytes read are ciphertext, which is fine: fio measures I/O latency
# and bandwidth, not content.
#
# Three profiles, run against whatever poll_queues / APST setting is live
# right now, so the SAME script is the before AND the after — run it, flip
# the lever, run it again, diff the JSON.
#
#   sudo bash diagnostics/bench-nvme.sh                    # all profiles, JSON to stdout+file
#   sudo bash diagnostics/bench-nvme.sh --quick             # smoke test, ~20s total
#   sudo bash diagnostics/bench-nvme.sh --label after-poll4 # tag the output file
#
# Needs root: O_DIRECT raw block device access, drop_caches before each run.
set -uo pipefail

DEV="${NVME_BENCH_DEV:-/dev/nvme0n1}"
LABEL="baseline"
QUICK=0
RUNTIME=30
while [[ $# -gt 0 ]]; do
  case "$1" in
    --label) LABEL="${2:-baseline}"; shift 2 ;;
    --quick) QUICK=1; RUNTIME=5; shift ;;
    --device) DEV="${2:-$DEV}"; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

hdr()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
  echo "Needs root: O_DIRECT raw device access + drop_caches. Re-run: sudo $0" >&2
  exit 1
fi
if ! command -v fio >/dev/null 2>&1; then
  bad "fio not installed. Run: bash diagnostics/bench-install-deps.sh --go"
  exit 1
fi
[[ -b "$DEV" ]] || { bad "$DEV is not a block device"; exit 1; }

OUT_DIR="out/bench"
mkdir -p "$OUT_DIR"
TS="$(date +%Y%m%d-%H%M%S)"
OUT_JSON="$OUT_DIR/nvme-${LABEL}-${TS}.json"

hdr "Context — what this run is actually testing"
echo "  device              : $DEV ($(lsblk -no MODEL "$DEV" 2>/dev/null | head -1 || echo unknown))"
echo "  label               : $LABEL"
NR_POLL="$(cat /sys/module/nvme/parameters/poll_queues 2>/dev/null || echo unknown)"
echo "  nvme.poll_queues     : $NR_POLL"
APST="$(cat /sys/module/nvme_core/parameters/default_ps_max_latency_us 2>/dev/null || echo unknown)"
echo "  default_ps_max_latency_us : $APST"
HMB="$(cat /sys/module/nvme/parameters/max_host_mem_size_mb 2>/dev/null || echo unknown)"
echo "  max_host_mem_size_mb : $HMB"
echo "  scheduler            : $(cat /sys/block/"$(basename "$DEV")"/queue/scheduler 2>/dev/null)"

hdr "Dropping page cache before each job (echo 3 > drop_caches)"
sync
echo 3 > /proc/sys/vm/drop_caches 2>/dev/null && ok "dropped" || echo "  (could not drop — /proc/sys/vm/drop_caches write failed)"

# --readonly is fio's own safety gate: it REFUSES to run any job whose rw=
# implies a write, even if one somehow got past this script.
COMMON=(--readonly --direct=1 --ioengine=io_uring --group_reporting
        --output-format=json --filename="$DEV")

hdr "Job 1/3 — 4k randread QD1 (single-request latency, the number that matters for cold model-load stalls)"
fio "${COMMON[@]}" --name=randread-qd1 --rw=randread --bs=4k --iodepth=1 \
    --numjobs=1 --time_based --runtime="$RUNTIME" \
    > "$OUT_DIR/.job1.json" 2>"$OUT_DIR/.job1.err"
if [[ $? -ne 0 ]]; then bad "job 1 failed"; cat "$OUT_DIR/.job1.err" >&2; fi
python3 -c "
import json
d=json.load(open('$OUT_DIR/.job1.json'))['jobs'][0]['read']
print(f'  IOPS {d[\"iops\"]:.0f}  p50 {d[\"clat_ns\"][\"percentile\"].get(\"50.000000\",0)/1e6:.3f}ms  p99 {d[\"clat_ns\"][\"percentile\"].get(\"99.000000\",0)/1e6:.3f}ms')
" 2>/dev/null || true

echo 3 > /proc/sys/vm/drop_caches 2>/dev/null || true
hdr "Job 2/3 — 4k randread QD32 (queue-depth-saturated random, exercises poll_queues / IOPOLL if enabled)"
POLL_FLAG=()
if [[ "$NR_POLL" != "0" && "$NR_POLL" != "unknown" ]]; then
  POLL_FLAG=(--hipri)
  echo "  poll_queues=$NR_POLL nonzero — adding --hipri (IOPOLL) to this job"
fi
fio "${COMMON[@]}" "${POLL_FLAG[@]}" --name=randread-qd32 --rw=randread --bs=4k --iodepth=32 \
    --numjobs=1 --time_based --runtime="$RUNTIME" \
    > "$OUT_DIR/.job2.json" 2>"$OUT_DIR/.job2.err"
if [[ $? -ne 0 ]]; then bad "job 2 failed"; cat "$OUT_DIR/.job2.err" >&2; fi
python3 -c "
import json
d=json.load(open('$OUT_DIR/.job2.json'))['jobs'][0]['read']
print(f'  IOPS {d[\"iops\"]:.0f}  p50 {d[\"clat_ns\"][\"percentile\"].get(\"50.000000\",0)/1e6:.3f}ms  p99 {d[\"clat_ns\"][\"percentile\"].get(\"99.000000\",0)/1e6:.3f}ms  BW {d[\"bw\"]/1024:.1f}MB/s')
" 2>/dev/null || true

echo 3 > /proc/sys/vm/drop_caches 2>/dev/null || true
hdr "Job 3/3 — 1M seqread (bandwidth ceiling, the model-load path)"
fio "${COMMON[@]}" --name=seqread-1m --rw=read --bs=1M --iodepth=8 \
    --numjobs=1 --time_based --runtime="$RUNTIME" \
    > "$OUT_DIR/.job3.json" 2>"$OUT_DIR/.job3.err"
if [[ $? -ne 0 ]]; then bad "job 3 failed"; cat "$OUT_DIR/.job3.err" >&2; fi
python3 -c "
import json
d=json.load(open('$OUT_DIR/.job3.json'))['jobs'][0]['read']
print(f'  BW {d[\"bw\"]/1024:.1f}MB/s  IOPS {d[\"iops\"]:.0f}')
" 2>/dev/null || true

hdr "Merging into $OUT_JSON"
python3 -c "
import json
merged = {
    'label': '$LABEL', 'device': '$DEV', 'timestamp': '$TS',
    'lever_state': {
        'nvme.poll_queues': '$NR_POLL',
        'default_ps_max_latency_us': '$APST',
        'max_host_mem_size_mb': '$HMB',
    },
    'randread_qd1': json.load(open('$OUT_DIR/.job1.json'))['jobs'][0],
    'randread_qd32': json.load(open('$OUT_DIR/.job2.json'))['jobs'][0],
    'seqread_1m': json.load(open('$OUT_DIR/.job3.json'))['jobs'][0],
}
json.dump(merged, open('$OUT_JSON', 'w'), indent=2)
print('  wrote', '$OUT_JSON')
"
rm -f "$OUT_DIR"/.job*.json "$OUT_DIR"/.job*.err

echo 3 > /proc/sys/vm/drop_caches 2>/dev/null || true

hdr "Also: cold-read APST latency (delegates to nvme-apst-cold-read.py)"
if [[ -x "$(command -v python3)" ]]; then
  echo "  Run separately (takes minutes at full --trials):"
  echo "    sudo python3 diagnostics/nvme-apst-cold-read.py --preflight"
  echo "    sudo python3 diagnostics/nvme-apst-cold-read.py --json-out $OUT_DIR/apst-${LABEL}-${TS}.json"
fi

hdr "Done"
echo "  $OUT_JSON"
echo "  Re-run with a different --label after flipping a lever, then diff the two JSON files."
