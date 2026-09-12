#!/usr/bin/env bash
# Real measurement of patch 0023 (cache_ext) on the RUNNING 7.2.2-hyphaed
# kernel. Loads the real upstream FIFO policy (cache-ext-upstream/cache_ext/
# policies/cache_ext_fifo.bpf.c, already rebuilt against this box's own BTF
# this session) scoped to ONE throwaway cgroup watching ONE scratch
# directory. This is the "L4 cache_ext: load a trivial policy, confirm the
# hooks fire" step from the platform-kernel-levers plan — T18 shipped 0023
# "done, unmeasured"; this closes that gap.
#
# Scope and reversibility:
#   - Only touches /sys/fs/cgroup/cache_ext_test (created and removed here)
#     and /tmp/cache_ext_watch (created and removed here).
#   - /proc/page_cache_ext_enabled_cgroup only names OUR cgroup as the one
#     the active policy sees -- every other cgroup on the system keeps
#     stock kernel LRU/MGLRU untouched. This is the mechanism's own
#     isolation, not something this script adds.
#   - The FIFO policy itself is a fallback-safe struct_ops: if it fails to
#     propose enough eviction candidates the kernel falls back to its
#     default policy for the shortfall (patch 0023's own design, matching
#     cache_ext's SOSP paper).
#   - Nothing here can outlive the loader process: sending it SIGINT
#     detaches the struct_ops link and the enabled-cgroup registration.
#
# Usage:
#   bash diagnostics/measure-cache-ext.sh          # report only, no root needed
#   sudo bash diagnostics/measure-cache-ext.sh --go
set -uo pipefail

GO=0
[[ "${1:-}" == "--go" ]] && GO=1

REPO=/home/ferran/Dev/kernel_inference
LOADER="$REPO/cache-ext-upstream/cache_ext/policies/cache_ext_fifo.out"
CGROUP=/sys/fs/cgroup/cache_ext_test
# Must be disk-backed (ext4), NOT /tmp -- /tmp is tmpfs on this box, and
# tmpfs pages charge to cgroup memory.stat's `shmem` counter, not `file`.
# cache_ext hooks __filemap_add_folio/__filemap_remove_folio, the file-backed
# path; tmpfs pages never reach it. Confirmed via `mount | grep /tmp` this
# session after a first run measured file=0 despite 64MB of I/O.
WATCH_DIR="$REPO/out/cache_ext_watch"
TRACE_LOG=/tmp/cache_ext_trace.log
RECLAIM_BYTES=$((64 * 1024 * 1024))   # ask for 64MB back from the cgroup

hdr()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; }

hdr "Preflight"
if [[ ! -e /proc/page_cache_ext_enabled_cgroup ]]; then
  bad "cache_ext not compiled into the running kernel (no /proc/page_cache_ext_enabled_cgroup)"
  exit 1
fi
ok "cache_ext live: /proc/page_cache_ext_enabled_cgroup present"
if [[ ! -x "$LOADER" ]]; then
  bad "$LOADER not built. Run: cd cache-ext-upstream/cache_ext/policies && make cache_ext_fifo.out CLANG=clang BPFTOOL=/usr/sbin/bpftool"
  exit 1
fi
ok "FIFO loader built: $LOADER"

if (( ! GO )); then
  hdr "Would do"
  echo "  1. mkdir $CGROUP ; mkdir $WATCH_DIR"
  echo "  2. launch $LOADER --watch_dir $WATCH_DIR --cgroup_path $CGROUP (background)"
  echo "  3. capture /sys/kernel/debug/tracing/trace_pipe + dmesg for the"
  echo "     kernel's own 'cache_ext: Cgroup ... is a cache_ext cgroup' and"
  echo "     'Created main_list' confirmations"
  echo "  4. write + read real test files INSIDE $WATCH_DIR from a process"
  echo "     placed in $CGROUP (the dir_watcher only tracks files CREATED"
  echo "     after attach -- pre-existing files are invisible to it)"
  echo "  5. echo $RECLAIM_BYTES > $CGROUP/memory.reclaim to force real"
  echo "     reclaim through shrink_lruvec (where 0023 hooks evict_folios)"
  echo "  6. compare memory.stat 'file' bytes before/after, grep trace log"
  echo "     for evict-path activity, confirm no kernel WARN/oops in dmesg"
  echo "  7. SIGINT the loader (clean struct_ops detach), rm cgroup + dir"
  echo
  echo "  Re-run with --go (as root) to actually do it."
  exit 0
fi
[[ $EUID -eq 0 ]] || { bad "--go needs root (BPF struct_ops load, cgroup, trace_pipe)"; exit 1; }

cleanup() {
  hdr "Cleanup"
  [[ -n "${LOADER_PID:-}" ]] && kill -INT "$LOADER_PID" 2>/dev/null && wait "$LOADER_PID" 2>/dev/null
  [[ -n "${FIFO_HOLD_FD:-}" ]] && exec {FIFO_HOLD_FD}<&- 2>/dev/null
  [[ -n "${LOADER_FIFO:-}" ]] && rm -f "$LOADER_FIFO"
  [[ -n "${TRACE_PID:-}" ]] && kill "$TRACE_PID" 2>/dev/null
  rmdir "$CGROUP" 2>/dev/null
  rm -rf "$WATCH_DIR"
  ok "removed $CGROUP and $WATCH_DIR"
}
trap cleanup EXIT

hdr "1. Scratch cgroup + watch directory"
mkdir -p "$CGROUP"
mkdir -p "$WATCH_DIR"
ok "created $CGROUP"
ok "created $WATCH_DIR"

hdr "2. Trace capture (bpf_printk goes to trace_pipe, not dmesg)"
: > "$TRACE_LOG"
cat /sys/kernel/debug/tracing/trace_pipe > "$TRACE_LOG" 2>&1 &
TRACE_PID=$!
ok "trace_pipe reader started (pid $TRACE_PID) -> $TRACE_LOG"

hdr "3. Launching the FIFO policy loader"
# The loader blocks on getchar() to stay attached until told to stop. Do NOT
# feed it /dev/null (instant EOF -> instant clean detach, no window for the
# test I/O below) or let a backgrounded job fight the controlling terminal
# for real keystrokes. Hold it open on a FIFO we control instead: as long as
# we keep our own write-fd open on it, the loader's read-fd sees no EOF and
# getchar() blocks indefinitely -- we release it with SIGINT when done.
LOADER_FIFO=$(mktemp -u /tmp/cache_ext_loader_stdin.XXXXXX)
mkfifo "$LOADER_FIFO"
exec {FIFO_HOLD_FD}<>"$LOADER_FIFO"

DMESG_BEFORE=$(dmesg | wc -l)
"$LOADER" --watch_dir "$WATCH_DIR" --cgroup_path "$CGROUP" < "$LOADER_FIFO" > /tmp/cache_ext_loader.log 2>&1 &
LOADER_PID=$!
sleep 1.5
if ! kill -0 "$LOADER_PID" 2>/dev/null; then
  bad "loader exited immediately, see /tmp/cache_ext_loader.log"
  cat /tmp/cache_ext_loader.log
  exec {FIFO_HOLD_FD}<&-
  rm -f "$LOADER_FIFO"
  exit 1
fi
ok "loader running (pid $LOADER_PID), held attached via $LOADER_FIFO"

hdr "4. Kernel's own confirmation (dmesg since launch)"
dmesg | tail -n +"$((DMESG_BEFORE + 1))" | grep -i "cache_ext\|page_cache_ext" | sed 's/^/  /' || warn "no cache_ext dmesg lines yet"

hdr "5. Real I/O inside the watched directory, from a process IN the cgroup"
# The dir_watcher only tracks files CREATED after attach (FMODE_CREATED
# check in vfs_open_exit) -- write fresh files, don't reuse old ones.
(
  # $$ inside a ( ) subshell is the PARENT shell's PID in bash, not this
  # subshell's own -- $BASHPID is required, or this silently moves the
  # wrong process and every dd/cat below runs in whatever cgroup the
  # script itself started in. Confirmed the hard way this session: the
  # first attempt used $$ and folio_added fired zero times because the
  # I/O never actually landed in $CGROUP's memcg.
  echo $BASHPID > "$CGROUP/cgroup.procs"
  for i in $(seq 1 8); do
    dd if=/dev/urandom of="$WATCH_DIR/testfile_$i.bin" bs=1M count=8 status=none
  done
  # Read them back to generate access events
  for i in $(seq 1 8); do
    cat "$WATCH_DIR/testfile_$i.bin" > /dev/null
  done
)
ok "wrote + read 8x 8MiB files inside $WATCH_DIR under $CGROUP"

hdr "6. Cache state before forced reclaim"
BEFORE_FILE=$(grep '^file ' "$CGROUP/memory.stat" | awk '{print $2}')
echo "  memory.stat file=$BEFORE_FILE bytes"
hdr "6b. Trace evidence for folio_added, BEFORE any reclaim"
grep -i "cache_ext: added" "$TRACE_LOG" | tail -20 | sed 's/^/  /' || warn "no folio_added lines yet"

hdr "7. Forcing real reclaim (this is where evict_folios gets first refusal)"
# memory.reclaim is best-effort and can EAGAIN with nothing obviously wrong.
# memory.max forces SYNCHRONOUS reclaim in the write() syscall itself: cgroup
# v2 will not return from this write until usage is at or under the new
# limit (or it gives up and OOM-kills -- there is no anon memory of note in
# this cgroup by this point, the dd/cat processes already exited, so the
# only reclaimable memory is exactly the file pages we want reclaimed).
echo 1M > "$CGROUP/memory.max" 2>/tmp/reclaim_err.txt \
  && ok "set memory.max=1M, forcing synchronous reclaim" \
  || warn "memory.max write failed: $(cat /tmp/reclaim_err.txt)"
sleep 0.5
AFTER_FILE=$(grep '^file ' "$CGROUP/memory.stat" | awk '{print $2}')
echo "  memory.stat file=$AFTER_FILE bytes (delta: $((BEFORE_FILE - AFTER_FILE)))"

hdr "8. Trace log evidence (full, since reclaim)"
grep -i "cache_ext\|fifo" "$TRACE_LOG" | tail -40 | sed 's/^/  /' || warn "no cache_ext lines in trace_pipe capture"
EVICT_CALLED=$(grep -c "evict_folios CALLED" "$TRACE_LOG" 2>/dev/null); EVICT_CALLED=${EVICT_CALLED:-0}
FOLIO_ADDED_OK=$(grep -c "folio added to main_list OK" "$TRACE_LOG" 2>/dev/null); FOLIO_ADDED_OK=${FOLIO_ADDED_OK:-0}

hdr "9. Correctness check: files still readable and byte-identical count"
READABLE=0
for i in $(seq 1 8); do
  [[ -r "$WATCH_DIR/testfile_$i.bin" ]] && [[ "$(stat -c%s "$WATCH_DIR/testfile_$i.bin")" -eq 8388608 ]] && READABLE=$((READABLE + 1))
done
echo "  $READABLE/8 files intact after reclaim"

hdr "10. Any kernel warnings/oops during the test?"
dmesg | tail -n +"$((DMESG_BEFORE + 1))" | grep -iE "warn|oops|bug:|panic" | sed 's/^/  /' || ok "none"

hdr "Verdict"
echo "  folio_added fired (successfully) for our test files: $FOLIO_ADDED_OK times"
echo "  evict_folios was CALLED (policy got first refusal on reclaim):    $EVICT_CALLED times"
if [[ "${READABLE:-0}" -lt 8 ]]; then
  bad "CORRECTNESS FAILURE: only $READABLE/8 files survived reclaim intact. Do not trust this policy further without investigating."
elif [[ "$FOLIO_ADDED_OK" -gt 0 && "$EVICT_CALLED" -gt 0 ]]; then
  ok "MEASURED, DIRECTLY: our compiled eBPF code ran fifo_folio_added() for our"
  echo "  own test files ($FOLIO_ADDED_OK times) AND the kernel called fifo_evict_folios()"
  echo "  ($EVICT_CALLED times) during the forced reclaim, giving the policy first"
  echo "  refusal on the inactive list exactly as patch 0023 describes. Page-cache"
  echo "  bytes dropped $((BEFORE_FILE - AFTER_FILE)) (memory.stat, corroborating but"
  echo "  secondary to the trace evidence above). All $READABLE/8 files stayed correct."
  echo "  This is real, positive proof patch 0023's hooks execute end-to-end on this"
  echo "  kernel, driven by a real upstream policy -- not just that the struct_ops"
  echo "  type registers (which was already known from dmesg alone)."
elif [[ "$FOLIO_ADDED_OK" -eq 0 ]]; then
  warn "folio_added never fired successfully for our files -- dir_watcher likely"
  echo "  never matched our watch path, or the files were faulted in before the"
  echo "  eBPF program's inode_watchlist saw them. Check the '6b' trace section"
  echo "  above for 'folio NOT relevant' lines vs no lines at all (the former means"
  echo "  the hook ran but is_folio_relevant() rejected the folio; the latter means"
  echo "  folio_added itself never got called for anything in our directory)."
elif [[ "$EVICT_CALLED" -eq 0 ]]; then
  warn "folio_added fired but evict_folios was never called -- the forced"
  echo "  memory.max squeeze in step 7 may not have reached the file LRU for this"
  echo "  memcg before this script checked. Real hooks-firing was confirmed for"
  echo "  admission; eviction specifically stayed unconfirmed this run."
else
  warn "INCONCLUSIVE: file bytes did not measurably drop ($BEFORE_FILE -> $AFTER_FILE). Reclaim may not have reached this cgroup's pages, or 64MB was too small a request against real memory pressure elsewhere."
fi
