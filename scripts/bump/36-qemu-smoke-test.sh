#!/usr/bin/env bash
# Stage 36: boot a kernel in QEMU/KVM with a tiny busybox initramfs and look for trouble BEFORE it touches the real
# machine. Catches: does it boot, WARN/BUG/Oops/RCU stalls/hung tasks/taint, does sched_ext load a real BPF scheduler
# (the host's /usr/sbin/scx_bpfland) and unload it again, and does a burst of futex/epoll/mmap/signal/fork/memcg
# stress (the paths the perf backports touch) run clean. It cannot prove anything about real hardware: no NVIDIA, no
# NVMe, no DKMS modules, no Wayland, and VM numbers are not performance evidence.
#
#   scripts/bump/36-qemu-smoke-test.sh <vmlinuz|bzImage|linux-image-*.deb> [label]
#
# Needs: qemu-system-x86_64 + /dev/kvm access, busybox (static), stress-ng, scx_bpfland (optional, skipped if absent).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SRC="${1:?kernel image (vmlinuz/bzImage) or linux-image-*.deb}"; LABEL="${2:-$(basename "$SRC")}"
W="$(mktemp -d "${TMPDIR:-/tmp}/smoke-XXXXXX")"; trap 'rm -rf "$W"' EXIT
OUT="${SMOKE_OUT:-$ROOT/out/smoke}"; mkdir -p "$OUT"
SMP="${SMOKE_SMP:-8}"; MEM="${SMOKE_MEM:-4G}"; DUR="${SMOKE_STRESS_S:-20}"; LIMIT="${SMOKE_TIMEOUT:-420}"
[ -w /dev/kvm ] || { echo "NOT RUN: no write access to /dev/kvm (group kvm)"; exit 2; }
for t in qemu-system-x86_64 busybox stress-ng cpio gzip; do command -v $t >/dev/null || { echo "NOT RUN: $t missing"; exit 2; }; done

case "$SRC" in
  *.deb) dpkg-deb --fsys-tarfile "$SRC" | tar -x -C "$W" --wildcards './boot/vmlinuz-*' './boot/config-*' ; KERNEL="$(ls "$W"/boot/vmlinuz-* | head -1)" ;;
  *) KERNEL="$SRC" ;;
esac
[ -r "$KERNEL" ] || { echo "NOT RUN: cannot read $KERNEL"; exit 2; }
# libbpf reads the kernel config (CO-RE kconfig externs) from /proc/config.gz or /boot/config-<release>; without it
# every sched_ext scheduler fails with "failed to open system Kconfig". Hand the guest the matching config.
REL="${KERNEL##*/vmlinuz-}"; CFG="$(dirname "$KERNEL")/config-$REL"

# ---- initramfs --------------------------------------------------------------------------------------------------
I="$W/initramfs"; mkdir -p "$I"/{bin,sbin,proc,sys,dev,tmp,lib,lib64,usr/bin,usr/sbin,usr/lib/x86_64-linux-gnu,run,sys/fs/cgroup}
cp "$(command -v busybox)" "$I/bin/busybox"; for a in sh mount umount mkdir cat echo sleep grep sed awk head tail wc ls kill ps dmesg date poweroff cut tr sort uniq printf seq id uname basename dirname; do ln -sf busybox "$I/bin/$a"; done
addbin() { local b="$1"; [ -x "$b" ] || return 0; cp -L "$b" "$I/usr/bin/$(basename "$b")"
  ldd "$b" 2>/dev/null | awk '/=>/ {print $3} /^\s*\/lib/ {print $1}' | grep '^/' | sort -u | while read -r l; do mkdir -p "$I$(dirname "$l")"; cp -L "$l" "$I$l"; done; }
[ -r "$CFG" ] && { mkdir -p "$I/boot"; cp "$CFG" "$I/boot/config-$REL"; } || echo "warning: no $CFG; BPF schedulers will fail to load"
addbin "$(command -v stress-ng)"
HAVE_SCX=0; if [ -x /usr/sbin/scx_bpfland ]; then addbin /usr/sbin/scx_bpfland; HAVE_SCX=1; fi
cat >"$I/init" <<EOF
#!/bin/sh
export PATH=/bin:/usr/bin:/usr/sbin
mount -t proc proc /proc; mount -t sysfs sysfs /sys; mount -t devtmpfs dev /dev
mount -t debugfs debugfs /sys/kernel/debug 2>/dev/null; mount -t cgroup2 none /sys/fs/cgroup 2>/dev/null
R() { echo "SMOKE: \$*"; }
R "uname=\$(uname -r)"; R "cmdline=\$(cat /proc/cmdline)"; R "cpus=\$(grep -c ^processor /proc/cpuinfo)"
bad() { dmesg | grep -ciE 'BUG:|WARNING:|Oops|general protection|RCU.*stall|hung task|Call Trace:|soft lockup|kernel panic|UBSAN|KASAN|lockdep|possible recursive|refcount_t|use-after-free'; }
R "boot_dmesg_problems=\$(bad)"; R "taint_after_boot=\$(cat /proc/sys/kernel/tainted)"
R "cgroup_ctrl=\$(cat /sys/fs/cgroup/cgroup.controllers 2>/dev/null)"
[ -e /sys/kernel/sched_ext/state ] && R "scx_state_boot=\$(cat /sys/kernel/sched_ext/state)" || R "scx_state_boot=ABSENT"
R "btf=\$([ -e /sys/kernel/btf/vmlinux ] && echo yes || echo no)"
# --- sched_ext: load the host's real BPF scheduler, run load, unload ---
if [ -x /usr/bin/scx_bpfland ]; then
  /usr/bin/scx_bpfland -v >/tmp/scx.log 2>&1 &
  SP=\$!; sleep 4
  R "scx_state_loaded=\$(cat /sys/kernel/sched_ext/state 2>/dev/null)"; R "scx_ops=\$(cat /sys/kernel/sched_ext/root/ops 2>/dev/null)"
  R "scx_switch_all=\$(cat /sys/kernel/sched_ext/switch_all 2>/dev/null)"
  stress-ng --cpu 4 --switch 2 --timeout 8s --metrics-brief >/tmp/scx-stress.log 2>&1; R "scx_stress_rc=\$?"
  R "scx_nr_rejected=\$(cat /sys/kernel/sched_ext/nr_rejected 2>/dev/null)"
  kill \$SP 2>/dev/null; sleep 3
  R "scx_state_unloaded=\$(cat /sys/kernel/sched_ext/state 2>/dev/null)"; grep -iE 'fail|error|invalid|-22|verif|denied|permiss|unsupported|not found|missing|BTF' /tmp/scx.log | sed 's/^/SMOKE: SCXERR: /' | head -25; tail -n 12 /tmp/scx.log | sed 's/^/SMOKE: SCXTAIL: /'
else R "scx=SKIPPED(no binary)"; fi
# --- the paths the backports touch, in a memory-limited cgroup so the memcg paths run too ---
mkdir -p /sys/fs/cgroup/t 2>/dev/null; echo +memory > /sys/fs/cgroup/cgroup.subtree_control 2>/dev/null
echo 900M > /sys/fs/cgroup/t/memory.max 2>/dev/null; echo 350M > /sys/fs/cgroup/t/memory.high 2>/dev/null; echo 0 > /sys/fs/cgroup/t/cgroup.procs 2>/dev/null
echo \$\$ > /sys/fs/cgroup/t/cgroup.procs 2>/dev/null
stress-ng --futex 4 --epoll 4 --mmap 2 --mmap-bytes 64M --sigq 2 --fork 2 --pipe 2 --vm 2 --vm-bytes 200M --timeout ${DUR}s --metrics-brief >/tmp/stress.log 2>&1
R "stress_rc=\$?"; sed 's/^/SMOKE: STRESS: /' /tmp/stress.log | grep -E 'stressor|futex|epoll|mmap|sigq|fork|pipe|vm ' | head -14
R "memcg_events=\$(tr '\n' ' ' < /sys/fs/cgroup/t/memory.events 2>/dev/null)"
echo 0 > /sys/fs/cgroup/cgroup.procs 2>/dev/null
R "end_dmesg_problems=\$(bad)"; R "taint_end=\$(cat /proc/sys/kernel/tainted)"
dmesg | grep -iE 'BUG:|WARNING:|Oops|RCU.*stall|hung task|Call Trace:|soft lockup|UBSAN|lockdep' | head -20 | sed 's/^/SMOKE: DMESG: /'
R "DONE"; poweroff -f
EOF
chmod +x "$I/init"
( cd "$I" && find . | cpio -o -H newc --quiet | gzip -1 > "$W/initramfs.gz" )

# ---- boot -------------------------------------------------------------------------------------------------------
LOG="$OUT/smoke-$LABEL-$(date +%Y%m%d-%H%M%S).log"
echo "booting $KERNEL ($SMP vCPU, $MEM, stress ${DUR}s) -> $LOG"
set +e
timeout "$LIMIT" taskset -c 0-15 qemu-system-x86_64 -enable-kvm -cpu host -smp "$SMP" -m "$MEM" -nographic -no-reboot \
  -kernel "$KERNEL" -initrd "$W/initramfs.gz" -append "console=ttyS0 panic=-1 loglevel=4 rdinit=/init nokaslr=0" >"$LOG" 2>&1
rc=$?; set -e
tr -d '\r' <"$LOG" | grep '^SMOKE:' | sed 's/^SMOKE: //' | tee "$LOG.summary"
verdict=PASS; why=""
chk() { grep -q "$1" "$LOG.summary" || { verdict=FAIL; why="$why [$2]"; }; }
[ $rc -eq 0 ] || { verdict=FAIL; why="$why [qemu rc=$rc (124 = timeout)]"; }
chk '^DONE' "never reached the end of the test"
grep -q '^boot_dmesg_problems=0$' "$LOG.summary" || { verdict=FAIL; why="$why [dmesg problems at boot]"; }
grep -q '^end_dmesg_problems=0$' "$LOG.summary" || { verdict=FAIL; why="$why [dmesg problems after stress]"; }
[ "$HAVE_SCX" = 1 ] && { chk '^scx_state_loaded=enabled' "scx_bpfland did not reach 'enabled'"; chk '^scx_state_unloaded=disabled' "sched_ext did not return to 'disabled'"; }
echo "SMOKE VERDICT ($LABEL): $verdict$why"; [ "$verdict" = PASS ]
