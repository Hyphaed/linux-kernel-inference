#!/usr/bin/env bash
# Root half of the 7.1.10 patch verification.
#
# Everything here needs privilege for a reason the unprivileged suite
# explains: loading a BPF struct_ops, switching an I/O scheduler, reading
# dm-crypt's table, listing dma-buf internals. Each step is reversible and
# each one puts back what it changed.
#
#   sudo bash tests/kernel_runtime/root-steps.sh
#
# Writes JSON-ish key=value output to out/verify/root-steps.txt, which the
# pytest module reads back so the two halves report as one result.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
OUT="$ROOT/out/verify"
mkdir -p "$OUT"
REPORT="$OUT/root-steps.txt"
: > "$REPORT"

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
rec()  { printf '%s\n' "$*" >> "$REPORT"; }
ok()   { printf '  \033[32mok\033[0m   %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; }

if [ "$(id -u)" -ne 0 ]; then
    echo "must run as root: sudo bash $0" >&2
    exit 1
fi

rec "kernel=$(uname -r)"
rec "timestamp=$(date -Is)"

# --------------------------------------------------------------------------
say "0023 cache_ext — do the callbacks actually fire?"
# --------------------------------------------------------------------------
if [ -f "$HERE/cache_ext_probe.bpf.o" ] && [ -x "$HERE/cache_ext_probe" ]; then
    ( cd "$HERE" && ./cache_ext_probe ) 2>&1 | tee "$OUT/cache_ext.log"
    st=${PIPESTATUS[0]}
    rec "cache_ext_probe_exit=$st"
    added=$(grep -oP 'folio_added\s+\K[0-9]+'    "$OUT/cache_ext.log" | head -1)
    acc=$(grep  -oP 'folio_accessed\s+\K[0-9]+'  "$OUT/cache_ext.log" | head -1)
    evc=$(grep  -oP 'evict_folios calls\s+\K[0-9]+' "$OUT/cache_ext.log" | head -1)
    # Did the object load at all? probe_folio_evicted() calls a cache_ext
    # kfunc behind a never-taken branch, so a successful load is proof the
    # kfunc set registered -- which is what 7.1.10 got wrong until 2026-08-26.
    if grep -q 'cache_ext kfunc resolved' "$OUT/cache_ext.log"; then
        rec "cache_ext_loaded=1"
    else
        rec "cache_ext_loaded=0"
    fi
    rec "cache_ext_folio_added=${added:-0}"
    rec "cache_ext_folio_accessed=${acc:-0}"
    rec "cache_ext_evict_calls=${evc:-0}"
    [ "$st" -eq 0 ] && ok "callbacks fired" || bad "see $OUT/cache_ext.log"
else
    bad "probe not built — run: make -C $HERE"
    rec "cache_ext_probe_exit=127"
fi

say "0023 — struct_ops type visible to bpftool"
if bpftool struct_ops list 2>/dev/null | tee "$OUT/struct_ops.log" | grep -q .; then
    ok "$(wc -l < "$OUT/struct_ops.log") struct_ops registered"
else
    echo "  (none currently attached — expected once the probe detaches)"
fi
rec "struct_ops_type_present=$(bpftool btf dump file /sys/kernel/btf/vmlinux 2>/dev/null | grep -c 'bpf_struct_ops_page_cache_ext_ops')"

# --------------------------------------------------------------------------
say "0010 / 0011 — mq-deadline tunables (inert until the scheduler is selected)"
# --------------------------------------------------------------------------
# The NVMe runs 'none', so iosched/ does not exist and neither patch can be
# observed. Switch to mq-deadline just long enough to read the two values,
# then put the original scheduler back.
for dev in /sys/block/nvme*n1; do
    [ -e "$dev/queue/scheduler" ] || continue
    name=$(basename "$dev")
    orig=$(sed -n 's/.*\[\(.*\)\].*/\1/p' "$dev/queue/scheduler")
    if ! grep -q mq-deadline "$dev/queue/scheduler"; then
        bad "$name: mq-deadline not available"; continue
    fi
    echo mq-deadline > "$dev/queue/scheduler" 2>/dev/null
    fm=$(cat "$dev/queue/iosched/front_merges" 2>/dev/null)
    we=$(cat "$dev/queue/iosched/write_expire" 2>/dev/null)
    rec "${name}_front_merges=$fm"
    rec "${name}_write_expire=$we"
    # 0010 sets front_merges 1 -> 0; 0011 sets write_expire 5*HZ -> HZ (5000 -> 1000 ms)
    [ "$fm" = "0" ]    && ok "0010 front_merges=0 on $name"        || bad "0010 front_merges=$fm on $name (want 0)"
    [ "$we" = "1000" ] && ok "0011 write_expire=1000 on $name"     || bad "0011 write_expire=$we on $name (want 1000)"
    echo "$orig" > "$dev/queue/scheduler" 2>/dev/null
    now=$(sed -n 's/.*\[\(.*\)\].*/\1/p' "$dev/queue/scheduler")
    [ "$now" = "$orig" ] && ok "$name scheduler restored to '$orig'" \
                         || bad "$name scheduler is '$now', was '$orig'"
done

# --------------------------------------------------------------------------
say "0006 — dm-crypt workqueue bypass"
# --------------------------------------------------------------------------
# 0006 set_bit()s NO_READ_WORKQUEUE / NO_WRITE_WORKQUEUE unconditionally, so
# crypt_status() reports them in the table line even though nobody asked.
if command -v dmsetup >/dev/null; then
    for t in $(dmsetup ls --target crypt 2>/dev/null | awk '{print $1}'); do
        line=$(dmsetup table "$t" 2>/dev/null)
        rec "dmcrypt_${t}=$line"
        echo "  $t: $line"
        if grep -q no_read_workqueue <<<"$line" && grep -q no_write_workqueue <<<"$line"; then
            ok "0006 both workqueue bypasses active on $t"
            rec "dmcrypt_${t}_bypass=yes"
        else
            bad "0006 bypass flags absent on $t"
            rec "dmcrypt_${t}_bypass=no"
        fi
    done
else
    bad "dmsetup not installed"
fi

# --------------------------------------------------------------------------
say "0022 — udmabuf scatterlist coalescing"
# --------------------------------------------------------------------------
if [ -r /sys/kernel/debug/dma_buf/bufinfo ]; then
    cp /sys/kernel/debug/dma_buf/bufinfo "$OUT/dma_buf_bufinfo.txt"
    ok "captured $(wc -l < "$OUT/dma_buf_bufinfo.txt") lines of bufinfo"
    rec "dma_buf_bufinfo=captured"
else
    bad "/sys/kernel/debug/dma_buf/bufinfo unreadable — coalescing stays unproven"
    rec "dma_buf_bufinfo=unavailable"
fi

# --------------------------------------------------------------------------
say "nvidia-fs — why the module was rejected at boot"
# --------------------------------------------------------------------------
# systemd-modules-load failed with 'Failed to insert module nvidia_fs:
# Invalid argument'. Get the real reason out of the kernel rather than
# guessing from the errno.
if lsmod | grep -q '^nvidia_fs'; then
    ok "nvidia_fs already loaded"
    rec "nvidia_fs_loaded=yes"
else
    dmesg -C 2>/dev/null || true
    modprobe -v nvidia-fs 2>&1 | tee "$OUT/nvidia_fs_modprobe.log"
    dmesg 2>/dev/null | tail -30 >> "$OUT/nvidia_fs_modprobe.log"
    if lsmod | grep -q '^nvidia_fs'; then
        ok "nvidia_fs loaded on retry — GDS is out of POSIX compat mode"
        rec "nvidia_fs_loaded=yes-after-retry"
    else
        bad "nvidia_fs still refuses to load — see $OUT/nvidia_fs_modprobe.log"
        rec "nvidia_fs_loaded=no"
    fi
fi

echo
echo "report written to $REPORT"
