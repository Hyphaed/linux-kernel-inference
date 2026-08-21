#!/usr/bin/env bash
# Read-only diagnostic probe for the hyphaed target workstation.
#
# Gathers the data points that needed root and were otherwise unavailable
# to the wizard/assistant session: NVMe identify-controller (HMB sizing),
# SMART health, PCIe ASPM/link state, CPU MSR-level turbo/HWP/EPP state,
# and full dmesg. Nothing here writes, flashes, or changes any setting —
# it only reads. Safe to run repeatedly.
#
# Usage: sudo ./diagnostics/workstation-probe.sh > probe-output.txt 2>&1
# Then share probe-output.txt (or paste it) back for analysis.

set -uo pipefail

# Detach stdin entirely — nothing in this script should ever read from the
# terminal. A foreground apt/nala run that tries to read stdin for any
# reason (or gets a stray SIGTTIN/SIGTSTP) otherwise stops silently when
# stdout/stderr are redirected to a file, which looks exactly like a hang.
exec < /dev/null
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a

sep() { printf '\n========== %s ==========\n' "$1"; }

if [[ $EUID -ne 0 ]]; then
    echo "Re-run with sudo: sudo $0" >&2
    exit 1
fi

NVME_DEV="${1:-/dev/nvme0}"

sep "step 0: installing required packages (nala)"
PKG_MGR="apt-get"
if command -v nala >/dev/null 2>&1; then
    PKG_MGR="nala"
fi
echo "using: $PKG_MGR"
"$PKG_MGR" update -q </dev/null || true
"$PKG_MGR" install -y nvme-cli msr-tools pciutils </dev/null >/dev/null \
    && echo "nvme-cli, msr-tools, pciutils: ok" \
    || echo "WARNING: package install failed — affected sections below will be skipped/partial"

sep "nvme-cli availability"
if ! command -v nvme >/dev/null 2>&1; then
    echo "nvme-cli still not available after install attempt"
else
    echo "nvme-cli: $(command -v nvme)"
fi

sep "NVMe identify-controller (HMPRE / HMMIN / firmware rev)"
if command -v nvme >/dev/null 2>&1; then
    nvme id-ctrl "$NVME_DEV" 2>&1 | grep -iE "^fr |^mn |^sn |hmpre|hmmin|hmminds|hmmaxd|^vid|^ssvid" \
        || echo "(no matching fields — full dump below)"
    echo "--- full id-ctrl dump ---"
    nvme id-ctrl "$NVME_DEV" 2>&1
else
    echo "skipped (nvme-cli missing)"
fi

sep "NVMe SMART / health log"
if command -v nvme >/dev/null 2>&1; then
    nvme smart-log "$NVME_DEV" 2>&1
else
    echo "skipped (nvme-cli missing)"
fi

sep "NVMe current HMB sysfs state (per-controller)"
for d in /sys/class/nvme/nvme*/; do
    [[ -d "$d" ]] || continue
    name="$(basename "$d")"
    echo "-- $name --"
    for attr in hmb; do
        [[ -f "$d$attr" ]] && echo "  $attr: $(cat "$d$attr" 2>/dev/null)"
    done
done

sep "dmesg (full ring buffer — HMB / microcode / GSP / NVMe lines)"
dmesg 2>&1 | grep -iE "hmb|host memory buffer|microcode|nvidia|nvme|gsp" || echo "(no matches)"

sep "dmesg (full, unfiltered — for anything the grep above missed)"
dmesg 2>&1

sep "PCIe link state + ASPM capability (NVMe + GPU)"
if command -v lspci >/dev/null 2>&1; then
    lspci -vvv -d 144d: 2>&1 | grep -iE "lnksta|lnkcap|aspm|^[0-9a-f]+:"
    echo "---"
    lspci -vvv -d 10de: 2>&1 | grep -iE "lnksta|lnkcap|aspm|^[0-9a-f]+:"
else
    echo "lspci missing"
fi

sep "CPU microcode + MSR-level turbo/HWP/EPP (rdmsr, read-only)"
modprobe msr 2>/dev/null || true
if command -v rdmsr >/dev/null 2>&1; then
    echo "MSR_IA32_MISC_ENABLE (0x1a0, turbo enable bit 38 should be 0):"
    rdmsr -p 0 0x1a0 2>&1
    echo "MSR_HWP_CAPABILITIES (0x771):"
    rdmsr -p 0 0x771 2>&1
    echo "MSR_HWP_REQUEST (0x774) per P-core (0-7) and E-core sample (16,24):"
    for cpu in 0 1 2 3 4 5 6 7 16 24; do
        echo "  cpu$cpu: $(rdmsr -p "$cpu" 0x774 2>&1)"
    done
    echo "IA32_THERM_STATUS (0x19c) cpu0 (bit 0 = throttling active):"
    rdmsr -p 0 0x19c 2>&1
else
    echo "msr-tools not installed — run: apt-get install -y msr-tools"
fi

sep "intel_pstate / cpufreq driver + governor + EPP (per-core summary)"
echo "scaling_driver: $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_driver 2>/dev/null)"
echo "current_governor (cpu0): $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null)"
for cpu in /sys/devices/system/cpu/cpu*/cpufreq; do
    n=$(basename "$(dirname "$cpu")")
    gov=$(cat "$cpu/scaling_governor" 2>/dev/null)
    epp=$(cat "$cpu/energy_performance_preference" 2>/dev/null)
    cur=$(cat "$cpu/scaling_cur_freq" 2>/dev/null)
    echo "  $n: governor=$gov epp=$epp cur_freq=$cur"
done

sep "Thermal / throttle counters"
for tz in /sys/class/thermal/thermal_zone*/; do
    [[ -d "$tz" ]] || continue
    echo "$(basename "$tz"): type=$(cat "$tz/type" 2>/dev/null) temp=$(cat "$tz/temp" 2>/dev/null)"
done
echo "core_throttle_count (per package, if exposed):"
grep -r . /sys/devices/system/cpu/cpu*/thermal_throttle/* 2>/dev/null

sep "sysctl tunables: CONFIGURED vs LIVE"
# A tunable that is written to /etc/sysctl.d and silently does not apply is
# invisible in every other view: the file says one thing, /proc says another,
# and nothing compares them. Real case, 2026-08-20: this project's
# 95-greenboost-t2.conf asked for vm.min_free_kbytes=1048576 and the box was
# running 524288, because GreenBoost's own installer writes
# 99-zzz-greenboost.conf, which sorts LAST and wins every conflict by design.
# The T2 direct-reclaim guard was running at half strength for weeks with
# nothing reporting it.
#
# Every key any sysctl.d file sets is checked here , not a curated list, so a
# tunable added later is covered without editing this script.
{
    declare -A _cfg_file _cfg_val
    for f in /usr/lib/sysctl.d/*.conf /run/sysctl.d/*.conf /etc/sysctl.d/*.conf /etc/sysctl.conf; do
        [[ -r "$f" ]] || continue
        while IFS= read -r line; do
            line="${line%%#*}"
            [[ "$line" == *=* ]] || continue
            key="${line%%=*}"; val="${line#*=}"
            key="$(echo "$key" | tr -d '[:space:]' | tr '/' '.')"
            val="$(echo "$val" | xargs)"
            [[ -n "$key" && -n "$val" ]] || continue
            # Later file wins, exactly as systemd-sysctl applies them.
            _cfg_file["$key"]="$(basename "$f")"
            _cfg_val["$key"]="$val"
        done < "$f"
    done

    drift=0
    printf '%-42s %-14s %-14s %s\n' "KEY" "CONFIGURED" "LIVE" "SOURCE (last writer)"
    for key in $(printf '%s\n' "${!_cfg_val[@]}" | sort); do
        path="/proc/sys/$(echo "$key" | tr '.' '/')"
        [[ -r "$path" ]] || continue
        live="$(tr -s '[:space:]' ' ' < "$path" | xargs)"
        want="${_cfg_val[$key]}"
        want_n="$(echo "$want" | xargs)"
        if [[ "$live" != "$want_n" ]]; then
            printf '%-42s %-14s %-14s %s   <-- DRIFT\n' "$key" "$want_n" "$live" "${_cfg_file[$key]}"
            drift=$((drift + 1))
        else
            printf '%-42s %-14s %-14s %s\n' "$key" "$want_n" "$live" "${_cfg_file[$key]}"
        fi
    done
    echo
    if (( drift > 0 )); then
        echo "$drift tunable(s) do NOT match what the files ask for."
        echo "What that costs: each one is a guard or a policy this project"
        echo "ships that is not actually in effect. Nothing is broken and no"
        echo "data is at risk , the box just is not tuned the way the files say."
        echo "To see which file loses a conflict:  systemd-analyze cat-config sysctl.d"
        echo "To re-apply everything now:          sudo sysctl --system"
        echo "(a value that reverts after that is being written by something at"
        echo " runtime, not by a file , that is the case worth chasing.)"
        echo
        echo "Caveat on the SOURCE column: it names the file that would win"
        echo "NOW. If a sysctl.d file was edited since the last boot, the"
        echo "value in /proc came from the older version of it, so the winner"
        echo "at boot may have been a different file. Compare mtimes against"
        echo "\`uptime -s\` before concluding which file is at fault."
    else
        echo "No drift: every configured sysctl matches its live value."
    fi
} 2>/dev/null

sep "Done"
echo "Paste the full output back for analysis."
