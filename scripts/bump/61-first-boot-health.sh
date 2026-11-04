#!/usr/bin/env bash
# Run right after booting a new kernel (read-only, no sudo needed except where noted). Complements 60-post-boot-audit.sh:
# 60 writes the audit document, this one answers "is it safe to keep this kernel as the default?" with PASS/WARN/FAIL
# lines and a rollback hint. Exit status is the number of FAILs.
#
#   scripts/bump/61-first-boot-health.sh [VER]        # VER defaults to the running kernel's version
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"; cd "$ROOT"
REL="$(uname -r)"; VER="${1:-${REL%%-*}}"
fails=0; warns=0
ok()   { printf 'PASS  %s\n' "$*"; }
warn() { printf 'WARN  %s\n' "$*"; warns=$((warns + 1)); }
fail() { printf 'FAIL  %s\n' "$*"; fails=$((fails + 1)); }

[[ "$REL" == "$VER"* ]] && ok "running $REL" || fail "running $REL, expected $VER"
T=$(cat /proc/sys/kernel/tainted); { [ "$T" = 0 ] || [ "$T" = 12288 ]; } && ok "taint $T (12288 = out-of-tree + unsigned modules, the known baseline)" || warn "taint $T differs from the 12288 baseline: decode with /proc/sys/kernel/tainted bits"
n=$(journalctl -b -k -p err --no-pager 2>/dev/null | grep -vE 'vmwgfx|firmware|ACPI BIOS|nvrm|NVRM' | wc -l); [ "$n" -le 3 ] && ok "kernel errors this boot: $n" || warn "kernel errors this boot: $n (journalctl -b -k -p err)"
bad=$(journalctl -b -k --no-pager 2>/dev/null | grep -ciE 'BUG:|general protection|Oops|RCU.*stall|hung task|soft lockup|kernel panic|UBSAN|refcount_t|Call Trace')
[ "$bad" = 0 ] && ok "no BUG/Oops/stall/hung-task/Call Trace in the kernel log" || fail "$bad serious kernel log lines (journalctl -b -k | grep -iE 'BUG:|Oops|stall|hung|Call Trace')"
f=$(systemctl --failed --no-legend 2>/dev/null | wc -l); [ "$f" = 0 ] && ok "no failed units" || warn "$f failed units: $(systemctl --failed --no-legend | awk '{print $2}' | tr '\n' ' ')"

# driver stack
if command -v nvidia-smi >/dev/null; then v=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>&1 | head -1); [[ "$v" =~ ^[0-9]+\. ]] && ok "NVIDIA driver $v loaded" || fail "nvidia-smi: $v"; fi
for m in nvidia nvidia_uvm nvidia_drm greenboost vmmon vmnet v4l2loopback; do
  grep -q "^$m " /proc/modules && ok "module $m loaded" || { modinfo -k "$REL" "$m" >/dev/null 2>&1 && warn "module $m built for $REL but not loaded" || warn "module $m not built for $REL (dkms status)"; }
done
dk=$(dkms status 2>/dev/null | grep "$REL" | grep -vc installed); [ "$dk" = 0 ] && ok "DKMS: everything for $REL installed" || fail "DKMS: $dk modules for $REL not installed (dkms status | grep $REL)"
xid=$(journalctl -b -k --no-pager 2>/dev/null | grep -c 'NVRM: Xid'); [ "$xid" = 0 ] && ok "no NVIDIA Xid errors" || fail "$xid NVIDIA Xid errors"

# scheduler: on this box scx_bpfland owns every normal task
if [ -r /sys/kernel/sched_ext/state ]; then
  st=$(cat /sys/kernel/sched_ext/state); ops=$(cat /sys/kernel/sched_ext/root/ops 2>/dev/null)
  [ "$st" = enabled ] && ok "sched_ext enabled: $ops (switch_all=$(cat /sys/kernel/sched_ext/switch_all))" || fail "sched_ext is '$st' (expected enabled; systemctl status hyphaed-scx.service)"
  [ "$(cat /sys/kernel/sched_ext/nr_rejected 2>/dev/null)" = 0 ] && ok "no tasks rejected by sched_ext" || warn "sched_ext rejected tasks: $(cat /sys/kernel/sched_ext/nr_rejected)"
fi
systemctl is-active --quiet hyphaed-scx.service 2>/dev/null && ok "hyphaed-scx.service active" || warn "hyphaed-scx.service not active"

# storage, network, memory features
ls /dev/nvme0n1 >/dev/null 2>&1 && ok "NVMe present" || fail "no /dev/nvme0n1"
ip -o link show up 2>/dev/null | grep -qv ' lo:' && ok "a network link is up: $(ip -o link show up | grep -v ' lo:' | awk -F': ' '{print $2}' | head -2 | tr '\n' ' ')" || fail "no network link up"
[ "$(cat /sys/kernel/mm/lru_gen/enabled 2>/dev/null)" = 0x0007 ] && ok "MGLRU on" || warn "MGLRU: $(cat /sys/kernel/mm/lru_gen/enabled 2>/dev/null)"
grep -q '\[always\]' /sys/kernel/mm/transparent_hugepage/enabled && ok "THP always" || warn "THP: $(cat /sys/kernel/mm/transparent_hugepage/enabled)"
[ "$(cat /sys/module/zswap/parameters/enabled 2>/dev/null)" = Y ] && ok "zswap on" || warn "zswap off"
[ "$(cat /sys/fs/cgroup/cgroup.controllers 2>/dev/null | wc -w)" -ge 4 ] && ok "cgroup v2 controllers present" || warn "cgroup controllers: $(cat /sys/fs/cgroup/cgroup.controllers)"

# the hyphaed wizard's own verifier
if python3 -m hyphaed verify >/tmp/hyphaed-verify.$$ 2>&1; then ok "hyphaed verify"; else warn "hyphaed verify reported problems (python3 -m hyphaed verify)"; fi; rm -f /tmp/hyphaed-verify.$$

echo; echo "$fails FAIL, $warns WARN"
if [ "$fails" -gt 0 ]; then
  echo "ROLLBACK: this kernel is the saved GRUB default (make install pins it). Reboot, pick the previous kernel under"
  echo "          GRUB > Advanced options, then make it the default again:"
  echo "            sudo grub-set-default \"Advanced options for Ubuntu>Ubuntu, with Linux <previous>-hyphaed\""
fi
exit "$fails"
