#!/usr/bin/env bash
# install_wizard.sh — hyphaed kernel build & install wizard
# Run from repo root: bash install_wizard.sh
set -euo pipefail

bash -n "$0" 2>/dev/null || { printf 'ERROR: syntax error in %s — run: bash -n %s\n' "$0" "$0" >&2; exit 1; }

if [[ "$EUID" -eq 0 ]]; then
    printf 'ERROR: do not run this wizard with sudo.\n' >&2
    printf '  Only the install/postinstall phases need root, and the wizard prompts\n' >&2
    printf '  for sudo itself exactly when those run. Running the whole pipeline as\n' >&2
    printf '  root leaves every build/ and state/ file root-owned, breaking every\n' >&2
    printf '  later run for your normal user.\n' >&2
    printf '  Re-run as your normal user:  bash %s\n' "$(basename "${BASH_SOURCE[0]}")" >&2
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

WIZARD_VERSION="1.0.1"
command -v python3 &>/dev/null || { printf 'ERROR: python3 not found\n' >&2; exit 1; }

# ── Colour palette ────────────────────────────────────────────────────────────
if [[ "$(tput colors 2>/dev/null || echo 8)" -ge 256 ]]; then
    C_VIOLET=$'\033[38;2;108;113;196m'
    C_LIME=$'\033[38;2;166;226;46m'
    C_CYAN=$'\033[38;2;48;200;255m'
    C_AMBER=$'\033[38;2;255;191;0m'
    C_RED=$'\033[38;2;255;92;50m'
    C_GRAY=$'\033[38;2;180;175;170m'
    C_WHITE=$'\033[38;2;255;255;255m'
    C_PURPLE=$'\033[38;2;167;139;250m'
    C_DIM=$'\033[2m'
    C_BOLD=$'\033[1m'
    C_RESET=$'\033[0m'
else
    C_VIOLET=$'\033[0;34m'; C_LIME=$'\033[0;32m'; C_CYAN=$'\033[0;36m'
    C_AMBER=$'\033[1;33m';  C_RED=$'\033[0;31m';  C_GRAY=$'\033[0;37m'
    C_WHITE=$'\033[1;37m';  C_PURPLE=$'\033[0;35m'
    C_DIM=$'\033[2m'; C_BOLD=$'\033[1m'; C_RESET=$'\033[0m'
fi

# ── Layout ────────────────────────────────────────────────────────────────────
COLS=$(tput cols 2>/dev/null || echo 72)
W=$(( COLS < 78 ? COLS : 78 ))

# ── UI primitives ─────────────────────────────────────────────────────────────
_fill() {
    local ch="$1" n="$2" s
    (( n < 1 )) && n=1
    printf -v s '%*s' "$n" ''
    printf '%s' "${s// /$ch}"
}

sep() {
    printf "  %s%s%s\n" "$C_DIM" "$(_fill ─ $(( W - 4 )))" "$C_RESET"
}

sep_label() {
    local label=" $1 " rest
    rest=$(( W - 6 - ${#label} ))
    (( rest < 2 )) && rest=2
    printf "  %s──%s%s%s%s\n" \
        "$C_CYAN" "$label" "$C_DIM" "$(_fill ─ $rest)" "$C_RESET"
}

ok()     { printf "  %s✓%s  %s%s%s\n" "$C_LIME"   "$C_RESET" "$C_GRAY"  "$*" "$C_RESET"; }
fail()   { printf "  %s✗%s  %s%s%s\n" "$C_RED"    "$C_RESET" "$C_WHITE" "$*" "$C_RESET"; }
warn()   { printf "  %s⚠%s  %s%s%s\n" "$C_AMBER"  "$C_RESET" "$C_WHITE" "$*" "$C_RESET"; }
info()   { printf "  %s◈%s  %s%s%s\n" "$C_CYAN"   "$C_RESET" "$C_GRAY"  "$*" "$C_RESET"; }
note()   { printf "  %s·  %s%s\n"     "$C_DIM"    "$*"       "$C_RESET"; }
bullet() { printf "  %s○%s  %s%s%s\n" "$C_VIOLET" "$C_RESET" "$C_GRAY"  "$*" "$C_RESET"; }

kv() {
    local key="$1" val="$2" w=24
    printf "  %s%-${w}s%s  %s%s%s\n" \
        "$C_CYAN" "$key" "$C_RESET" "$C_WHITE" "$val" "$C_RESET"
}

menu_item() {
    # [key]  Label            hint
    printf "  %s%s[%s]%s  %s%-26s%s  %s%s%s\n" \
        "$C_LIME" "$C_BOLD" "$1" "$C_RESET" \
        "$C_WHITE" "$2" "$C_RESET" \
        "$C_DIM" "$3" "$C_RESET"
}

prompt_choice() {
    printf "\n  %s%s❯%s  %s%s%s  " \
        "$C_AMBER" "$C_BOLD" "$C_RESET" \
        "$C_GRAY" "${1:-Choice}" "$C_RESET"
    read -r REPLY
}

print_header() {
    printf "\n"
    sep
    printf "  %s%shyphaed%s  %s·%s  %sKernel Build Wizard%s  %s(v%s)%s\n" \
        "$C_PURPLE" "$C_BOLD" "$C_RESET" \
        "$C_DIM" "$C_RESET" \
        "$C_CYAN" "$C_RESET" \
        "$C_DIM" "$WIZARD_VERSION" "$C_RESET"
    sep
    printf "\n"
}

# ── Spinner ───────────────────────────────────────────────────────────────────
SPIN_FRAMES=("⠋" "⠙" "⠹" "⠸" "⠼" "⠴" "⠦" "⠧" "⠇" "⠏")
_spin_pid="" _spin_msg=""

spinner_start() {
    _spin_msg="$1"
    (
        local i=0
        while true; do
            printf "\r  %s%s%s  %s%s%s" \
                "$C_VIOLET" "${SPIN_FRAMES[$i]}" "$C_RESET" \
                "$C_GRAY" "$_spin_msg" "$C_RESET"
            i=$(( (i + 1) % 10 ))
            sleep 0.08
        done
    ) &
    _spin_pid=$!
}

spinner_stop() {
    local status=${1:-0}
    if [[ -n "$_spin_pid" ]]; then
        kill "$_spin_pid" 2>/dev/null
        wait "$_spin_pid" 2>/dev/null || true
        _spin_pid=""
    fi
    if (( status == 0 )); then
        printf "\r  %s✓%s  %s%s%s\n" "$C_LIME" "$C_RESET" "$C_GRAY" "$_spin_msg" "$C_RESET"
    else
        printf "\r  %s✗%s  %s%s%s\n" "$C_RED"  "$C_RESET" "$C_WHITE" "$_spin_msg" "$C_RESET"
    fi
}

# ── Ctrl-C trap ───────────────────────────────────────────────────────────────
trap '_wizard_interrupt' INT
_wizard_interrupt() {
    if [[ -n "$_spin_pid" ]]; then
        kill "$_spin_pid" 2>/dev/null
        wait "$_spin_pid" 2>/dev/null || true
        _spin_pid=""
    fi
    printf "\n\n  %s⚠%s  interrupted\n\n" "$C_AMBER" "$C_RESET"
    exit 130
}

# ── Hardware detection ────────────────────────────────────────────────────────
_detect_cpu() {
    grep -m1 'model name' /proc/cpuinfo 2>/dev/null | sed 's/.*: //' || echo unknown
}
_detect_gpu() {
    lspci 2>/dev/null \
        | grep -Ei 'VGA|3D controller|Display controller' \
        | sed 's/^[0-9a-f.:]*  *//' \
        | head -2 \
        | paste -sd'  ·  ' \
        || echo unknown
}
_detect_ram_gb() {
    awk '/MemTotal/{printf "%d", int($2/1024/1024+0.5)}' /proc/meminfo 2>/dev/null || echo '?'
}
_detect_boot_free_mb() {
    df -BM /boot 2>/dev/null | awk 'NR==2{gsub(/M/,"",$4); print $4}' || echo '?'
}

# ── State helpers ─────────────────────────────────────────────────────────────
STATE_FILE="$REPO_ROOT/state/machines/$(hostname)/ctx.json"
[[ -f "$STATE_FILE" ]] || STATE_FILE="$REPO_ROOT/state/ctx.json"

PHASES=(detect source patch configure security build package install postinstall nvidia desktop proton)

_phase_index() {
    local p="$1" i
    for i in "${!PHASES[@]}"; do
        [[ "${PHASES[$i]}" == "$p" ]] && printf '%s' "$i" && return
    done
    printf '%s' "-1"
}

_phases_from() {
    local start="$1" idx i result=()
    idx=$(_phase_index "$start")
    (( idx < 0 )) && idx=0
    for i in "${!PHASES[@]}"; do
        (( i >= idx )) && result+=("${PHASES[$i]}")
    done
    printf '%s\n' "${result[@]}"
}


# ── Stops: what the client does, in order ─────────────────────────────────────
# Kernel stages run in one go. The NVIDIA driver and the GNOME rebuild are separate
# stops that only make sense AFTER a reboot (driver) / inside the desktop (GNOME),
# so the full build never runs them by itself; menu 7 and 8 do.
KERNEL_PHASES=(detect source patch configure security build package install postinstall)
POST_PHASES=(nvidia desktop proton)

_completed() {
    python3 - "$STATE_FILE" <<'EOF' 2>/dev/null || true
import json, sys
try:
    print(" ".join(json.load(open(sys.argv[1])).get("phases_completed", [])))
except Exception:
    pass
EOF
}
_newest_kernel() { ls /boot/vmlinuz-*-hyphaed 2>/dev/null | sed 's#.*/vmlinuz-##' | sort -V | tail -1; }
_nvidia_pinned() {
    sed -n '/^nvidia:/,/^[a-z]/{s/^  version: *"\{0,1\}\([0-9.]*\)"\{0,1\}.*/\1/p}' \
        "$REPO_ROOT/configs/presets/${PRESET:-gaming-ai-vm}.yaml" 2>/dev/null | head -1
}
_nvidia_loaded() { grep -o '[0-9]\{3\}\.[0-9]\{2,3\}\.[0-9]\{2\}' /proc/driver/nvidia/version 2>/dev/null | head -1; }

_stop() {   # state(done|next|todo)  number  title  hint-lines...
    local st="$1" n="$2" title="$3" mark color
    shift 3
    case "$st" in
        done) mark="✓"; color="$C_LIME"  ;;
        next) mark="►"; color="$C_AMBER" ;;
        *)    mark="○"; color="$C_DIM"   ;;
    esac
    printf "  %s%s%s  %s%s.%s %s%s%s\n" "$color" "$mark" "$C_RESET" "$C_CYAN" "$n" "$C_RESET" "$C_WHITE" "$title" "$C_RESET"
    local h
    for h in "$@"; do printf "        %s%s%s\n" "$C_DIM" "$h" "$C_RESET"; done
}

screen_roadmap() {
    local done_p newest running pinned loaded
    done_p=" $(_completed) "; newest=$(_newest_kernel); running=$(uname -r)
    pinned=$(_nvidia_pinned); loaded=$(_nvidia_loaded)
    local -a st=(todo todo todo todo todo todo)
    [[ "$done_p" == *" install "* ]]                         && st[0]=done
    [[ "${st[0]}" == done && "$running" == "$newest" ]]      && st[1]=done
    if [[ -n "$pinned" ]]; then
        [[ "$done_p" == *" nvidia "* || "$loaded" == "$pinned" ]] && st[2]=done
        [[ "$loaded" == "$pinned" ]]                         && st[3]=done
    else
        st[2]=done; st[3]=done
    fi
    [[ "$done_p" == *" desktop "* ]]                         && st[4]=done
    [[ "$done_p" == *" proton "* ]]                          && st[5]=done
    local i; for i in 0 1 2 3 4 5; do [[ "${st[$i]}" != done ]] && { st[$i]=next; break; }; done

    sep_label "Stops to follow (in this order)"
    _stop "${st[0]}" 1 "Build and install the kernel   [menu 1]" \
        "About 40 minutes. Installs ${newest:-the new kernel} next to your current one; nothing else changes."
    _stop "${st[1]}" 2 "Reboot into the new kernel" \
        "Pick ${newest:-the new kernel} in GRUB (it is the default), then open this wizard again."
    if [[ -n "$pinned" ]]; then
        _stop "${st[2]}" 3 "NVIDIA driver ${pinned}, built for this CPU   [menu 7]" \
            "Builds, then asks to install. Installing before the reboot works but nvidia-smi shows a mismatch until you reboot." \
            "Install replaces the apt driver with ours and asks first; undo: sudo bash diagnostics/nvidia-restore-apt.sh" \
            "Stop GPU jobs first (CUDA, llama-server, ComfyUI). Desktop apps are fine."
        _stop "${st[3]}" 4 "Reboot to load the new driver" \
            "Until you do, new OpenGL/Vulkan windows can fail to open. Reboot right after step 3."
    fi
    _stop "${st[4]}" 5 "GNOME Shell + Mutter rebuild   [menu 8]" \
        "Run it inside your GNOME session. 30 to 60 minutes of building, asks before installing." \
        "Takes effect when you log out and in. Undo list: state/machines/$(hostname)/desktop-rollback.txt"
    _stop "${st[5]}" 6 "Proton: GreenBoost wrapper + newest-source rebuild   [menu 9]" \
        "Refreshes the GreenBoost wrapper, then fetches the newest Valve Proton, re-applies our patches, builds in Docker (1+ hour, 25 GB)." \
        "Saves what changed upstream (state/machines/<host>/proton-changes-*.md). Asks before the build and before installing."
    printf "\n"
}

# ── Screen: welcome ───────────────────────────────────────────────────────────
screen_welcome() {
    clear
    print_header

    sep_label "Hardware"
    kv "CPU"        "$(_detect_cpu)"
    kv "GPU"        "$(_detect_gpu)"
    kv "RAM"        "$(_detect_ram_gb) GB"
    kv "Kernel"     "$(uname -r)"
    kv "/boot free" "$(_detect_boot_free_mb) MB"
    printf "\n"

    local last
    last=$(python3 - "$STATE_FILE" <<'EOF' 2>/dev/null || true
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    order = ["detect", "source", "patch", "configure", "security", "build", "package", "install", "postinstall", "nvidia", "desktop", "proton"]
    completed = set(d.get('phases_completed', []))
    v = ''
    for p in order:
        if p in completed:
            v = p
    if v: print(v)
except Exception:
    pass
EOF
)
    if [[ -n "$last" ]]; then
        info "Previous run detected — last completed: ${C_WHITE}${last}${C_RESET}"
        printf "\n"
    fi

    screen_roadmap

    sep_label "Mode"
    menu_item 1 "Full build"           "kernel only: detect → … → install → postinstall (steps 1)"
    menu_item 2 "Resume"               "continue from last checkpoint"
    menu_item 3 "Single phase"         "run exactly one phase"
    menu_item 4 "Fetch patches only"   "python3 -m hyphaed fetch-patches"
    menu_item 5 "Checks & diagnostics" "verify · status · tune · doctor · security scan · deps — no kernel build"
    menu_item 6 "Harden workstation"   "firewall · fail2ban · rootkit scanners · unattended updates · DNSCrypt · WAN exposure"
    menu_item 7 "NVIDIA driver"        "stop 3: after rebooting into the new kernel (optimized module + matching userspace)"
    menu_item 8 "GNOME Shell + Mutter" "stop 5: optimized rebuild, run it inside your GNOME session"
    menu_item 9 "GreenBoost Proton"    "stop 6: refresh GreenBoost Proton, then rebuild proton-gb-nr from the newest Valve sources (1+ hour, 25 GB)"
    menu_item 0 "Quit"                 ""
    printf "\n"

    while true; do
        prompt_choice "Select mode"
        case "$REPLY" in
            1) MODE="full";   return ;;
            2) MODE="resume"; return ;;
            3) MODE="single"; return ;;
            4) MODE="fetch";  return ;;
            5) MODE="checks"; return ;;
            6) MODE="harden"; return ;;
            7) MODE="nvidia"; return ;;
            8) MODE="desktop"; return ;;
            9) MODE="proton"; return ;;
            0) printf "\n"; exit 0 ;;
            *) warn "Enter 0–9" ;;
        esac
    done
}

# ── Preset: not a screen any more ─────────────────────────────────────────────
# The preset is fixed at gaming-ai-vm (set with the other defaults in main()).
# It was a three-way menu, and the answer was always [1] — a prompt whose
# outcome never varies is not a choice, it is a keystroke between the operator
# and the thing they came to do. `--preset` still overrides it for a scripted
# run, and the summary screen still prints which preset is in effect, so the
# value stays visible rather than becoming a hidden default.
#
# ai-only and safe remain valid presets: `python -m hyphaed --preset safe …`.

# ── Screen: kernel source mode ────────────────────────────────────────────────
# kernel.org is the only source (no more Ubuntu apt-source option) — always
# fetch the last 5 real stable releases (with release dates) from kernel.org
# via `hyphaed list-versions` so the choices shown are never stale, plus a
# free-text "custom version" entry for anything not in that list.
screen_source_mode() {
    clear
    print_header
    sep_label "Kernel Source (kernel.org)"
    note "Fetching latest kernel.org stable releases…"

    local versions_json
    versions_json="$(python3 -m hyphaed list-versions --count 5 --json 2>/dev/null || true)"

    local -a VER_LIST=()
    local -a DATE_LIST=()
    if [[ -n "$versions_json" ]]; then
        while IFS=$'\t' read -r v d; do
            [[ -n "$v" ]] && VER_LIST+=("$v") && DATE_LIST+=("$d")
        done < <(python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for r in data.get("releases", []):
    # No f-string: this program is embedded in a single-quoted bash string,
    # so a nested quote cannot be escaped as \" — Python 3.13 rejects that
    # with "unexpected character after line continuation character" before
    # the try/except above is ever reached, and the wizard then read zero
    # versions with no visible cause. Plain concatenation needs no escapes.
    print(r["version"] + "\t" + r["released"])
' <<< "$versions_json")
    fi

    clear
    print_header
    sep_label "Kernel Source (kernel.org)"

    local i
    if (( ${#VER_LIST[@]} > 0 )); then
        for i in "${!VER_LIST[@]}"; do
            menu_item "$(( i + 1 ))" "${VER_LIST[$i]}" "released ${DATE_LIST[$i]}"
        done
    else
        # `hyphaed list-versions` now falls back to a bare `git ls-remote`
        # against kernel.org when no local mirror exists (github/kernelorg
        # was never cloned) — that fallback needs only network, not a
        # mirror. An empty VER_LIST here means kernel.org itself was
        # unreachable, not "no local mirror" (found 2026-09-14: this message
        # blamed the mirror even when the network round-trip took ~1s).
        warn "could not reach kernel.org — enter a version manually below"
    fi
    menu_item "c" "Custom version" "type any kernel.org version, e.g. 7.1.7"
    menu_item 0 "Back" ""
    printf "\n"

    SOURCE_MODE="kernel-org"

    while true; do
        prompt_choice "Select kernel version"
        case "$REPLY" in
            c|C)
                prompt_choice "Version (e.g. 7.1.7)"
                if [[ -z "$REPLY" ]]; then
                    warn "version cannot be empty"
                    continue
                fi
                TARGET="$REPLY"
                return
                ;;
            0) screen_welcome; return ;;
            *)
                if [[ "$REPLY" =~ ^[0-9]+$ ]] && (( ${#VER_LIST[@]} > 0 && REPLY >= 1 && REPLY <= ${#VER_LIST[@]} )); then
                    TARGET="${VER_LIST[$(( REPLY - 1 ))]}"
                    return
                fi
                warn "Enter 1–${#VER_LIST[@]}, c, or 0"
                ;;
        esac
    done
}

# ── Screen: single phase picker ───────────────────────────────────────────────
screen_single_phase() {
    clear
    print_header
    sep_label "Select Phase"

    local i
    for i in "${!PHASES[@]}"; do
        case "${PHASES[$i]}" in
            nvidia)  menu_item "$(( i + 1 ))" "nvidia"  "after rebooting into the new kernel" ;;
            desktop) menu_item "$(( i + 1 ))" "desktop" "inside your GNOME session" ;;
            proton)  menu_item "$(( i + 1 ))" "proton"  "fetch newest Proton sources + rebuild proton-gb-nr" ;;
            *)       menu_item "$(( i + 1 ))" "${PHASES[$i]}" "" ;;
        esac
    done
    menu_item 0 "Back" ""
    printf "\n"

    while true; do
        prompt_choice "Phase number"
        if [[ "$REPLY" == "0" ]]; then
            screen_welcome; return
        fi
        if [[ "$REPLY" =~ ^[0-9]+$ ]] && (( REPLY >= 1 && REPLY <= ${#PHASES[@]} )); then
            SINGLE_PHASE="${PHASES[$(( REPLY - 1 ))]}"
            return
        fi
        warn "Enter 1–${#PHASES[@]} or 0"
    done
}

# ── Screen: build options ─────────────────────────────────────────────────────
JOBS=$(nproc 2>/dev/null || echo 4)
USE_CCACHE=1
# Dry-run has no prompt any more (see the build-options note below); it is
# taken from the environment so the capability survives the menu's removal:
#     DRY_RUN=1 ./install_wizard.sh
DRY_RUN=${DRY_RUN:-0}

_ccache_str() { (( USE_CCACHE )) && printf 'enabled' || printf 'disabled'; }

# ── Build options: not a screen any more ──────────────────────────────────────
# It offered three knobs and two of them did nothing.
#
#   Parallel jobs — settable, displayed twice, and never reached the command.
#                   build.py:439 hardcodes `jobs = max(1, os.cpu_count())` and
#                   the CLI has no --jobs flag, so choosing 4 still built -j32.
#   ccache        — the old comment in run_phase() said it outright:
#                   "there is no --no-ccache flag in the CLI, so USE_CCACHE=0
#                   is display-only for now."  Toggling it changed a label.
#   dry-run       — the only one wired, and it duplicates the documented
#                   top-level flag: `python3 -m hyphaed --dry-run`.
#
# Two inert controls presented as working ones is the same failure the Kconfig
# fragments taught in the project notes: no error, no warning, and it simply does not
# do what it says. Removed rather than left to mislead. Both values are
# auto-detected correctly (nproc / shutil.which) and are still reported on the
# Build Plan summary as FACTS about what will happen, not as choices.
#
# If a real -j override is ever wanted, wire `--jobs` through cli.py into
# build.py:439 first, then give it a prompt — in that order.


# ── Screen: confirm plan ──────────────────────────────────────────────────────
screen_confirm() {
    clear
    print_header
    sep_label "Build Plan"

    kv "Mode"    "$MODE"
    kv "Preset"  "$PRESET"
    kv "Source"  "kernel.org $TARGET"
    kv "Jobs"    "$JOBS (auto: nproc)"
    kv "ccache"  "$(_ccache_str) (auto-detected)"
    printf "\n"

    sep_label "Phases"
    local ph
    while IFS= read -r ph <&3; do
        bullet "$ph"
    done 3<<< "$PLAN_PHASES"
    printf "\n"

    while true; do
        prompt_choice "Proceed? [y / n / back]"
        case "${REPLY,,}" in
            y|yes|"") return ;;
            n|no)     printf "\n"; exit 0 ;;
            b|back)   screen_source_mode; return ;;
            *) warn "y · n · back" ;;
        esac
    done
}

# ── Phase execution ───────────────────────────────────────────────────────────
_elapsed() {
    local s="$1"
    printf '%dm%02ds' "$(( s / 60 ))" "$(( s % 60 ))"
}

run_phase() {
    local phase="$1"
    local log_dir="$REPO_ROOT/out/logs"
    mkdir -p "$log_dir"
    local logfile="$log_dir/${phase}_wizard.log"

    local cmd=(python3 -m hyphaed --phase "$phase")
    [[ -n "$PRESET" ]] && cmd+=(--preset "$PRESET")
    cmd+=(--source-mode kernel-org)
    [[ -n "$TARGET" ]] && cmd+=(--target "$TARGET")
    # NOTE: ccache is auto-detected by build.py (shutil.which) — there is no
    # --no-ccache flag in the CLI, so USE_CCACHE=0 is display-only for now.
    (( DRY_RUN    )) && cmd+=(--dry-run)

    local start rc=0
    start=$(date +%s)

    if [[ "$phase" == "build" || "$phase" == "install" || "$phase" == "detect" \
          || "$phase" == "source" || "$phase" == "patch" || "$phase" == "configure" \
          || "$phase" == "nvidia" || "$phase" == "desktop" || "$phase" == "proton" ]]; then
        # build: raw output because it's long-running and user wants progress.
        # install: raw output because install.py has an unbypassable "hard"
        # confirm before dpkg -i + GRUB changes.
        # detect/source/patch/configure: each of these can call confirm() too
        # (missing build deps, stale checkouts, git am --3way, menuconfig) —
        # any phase that can prompt needs live output, since a spinner-wrapped
        # phase redirects stdout to a logfile while stdin stays attached to
        # the terminal: the prompt becomes invisible but still blocks on
        # input, so the wizard just hangs looking "stuck".
        printf "\n"
        sep_label "$phase  (live output)"
        "${cmd[@]}" 2>&1 || rc=$?
        local elapsed; elapsed=$(( $(date +%s) - start ))
        printf "\n"
        if (( rc == 0 )); then
            ok "$phase done  [$(_elapsed $elapsed)]"
        else
            fail "$phase failed  [$(_elapsed $elapsed)]"
        fi
    else
        spinner_start "$phase"
        "${cmd[@]}" >"$logfile" 2>&1 || rc=$?
        local elapsed; elapsed=$(( $(date +%s) - start ))
        spinner_stop $rc
        note "$(_elapsed $elapsed)  →  $logfile"
        if (( rc != 0 )); then
            printf "\n"
            sep_label "log tail"
            tail -10 "$logfile" 2>/dev/null | while IFS= read -r line; do
                printf "  %s%s%s\n" "$C_DIM" "$line" "$C_RESET"
            done
            printf "\n"
        fi
    fi
    return $rc
}

# ── Screen: execution ─────────────────────────────────────────────────────────
screen_execute() {
    clear
    print_header
    sep_label "Progress"
    printf "\n"

    local ph rc
    # Read phase names from fd 3, not stdin (fd 0) — a plain `<<< "$PLAN_PHASES"`
    # on this loop would rebind fd 0 to the here-string for the whole loop
    # body, leaving no real terminal stdin for the nested retry/skip/abort
    # `read` below, or for any interactive prompt inside the spawned
    # `python3 -m hyphaed` process (e.g. install.py's unbypassable hard
    # confirm) — both would see EOF/non-tty instead of your keyboard.
    while IFS= read -r ph <&3; do
        rc=0
        run_phase "$ph" || rc=$?

        if (( rc != 0 )); then
            printf "\n"
            local retry_done=0
            while (( ! retry_done )); do
                prompt_choice "[r]etry  [s]kip  [a]bort"
                case "${REPLY,,}" in
                    r|retry)
                        rc=0
                        run_phase "$ph" || rc=$?
                        (( rc == 0 )) && retry_done=1
                        ;;
                    s|skip)
                        warn "skipping $ph"
                        retry_done=1
                        ;;
                    a|abort)
                        fail "Aborted at phase: $ph"
                        printf "\n"
                        exit 1
                        ;;
                    *) warn "r · s · a" ;;
                esac
            done
        fi
    done 3<<< "$PLAN_PHASES"
}

# ── Screen: fetch patches only ────────────────────────────────────────────────
screen_fetch() {
    clear
    print_header
    sep_label "Fetch Patches"
    printf "\n"

    mkdir -p "$REPO_ROOT/out/logs"
    local logfile="$REPO_ROOT/out/logs/fetch_wizard.log"
    local rc=0

    # Goes through `hyphaed fetch-patches`, NOT a bare `patches/fetch.py`
    # call — the bare form defaults to patches/VENDOR.lock, the LEGACY
    # ubuntu-source-mode lock (dead since --source-mode kernel-org became
    # the sole mode, 2026-08-06) — confirmed live 2026-09-22: its stale pin
    # for a shared patch file false-alarmed a sha mismatch, while the real
    # kernel-org lock for the SAME on-disk file was correct the whole time.
    # `hyphaed fetch-patches` resolves the right VENDOR-kernel-org-<ver>.lock
    # itself (patch.py::resolve_vendor_lock — source_dir, then --target,
    # then newest-on-disk) so this stays correct automatically whenever a
    # newer series (7.3, …) becomes what --target's default points at —
    # no version number to keep in sync here in bash.
    spinner_start "running hyphaed fetch-patches"
    python3 -m hyphaed fetch-patches >"$logfile" 2>&1 || rc=$?
    spinner_stop $rc

    if (( rc == 0 )); then
        ok "All patches fetched"
        note "Log: $logfile"
    else
        fail "fetch.py exited $rc — see $logfile"
        sep_label "log tail"
        tail -10 "$logfile" 2>/dev/null | while IFS= read -r line; do
            printf "  %s%s%s\n" "$C_DIM" "$line" "$C_RESET"
        done
    fi

    printf "\n"
    printf "  %sPress Enter to continue…%s" "$C_DIM" "$C_RESET"
    read -r
}

# ── Checks & diagnostics hub ─────────────────────────────────────────────────
# One home base for everything that reads/reports on an already-installed
# system, rather than each living as its own easy-to-forget top-level menu
# entry. None of these build a kernel; all run live (never spinner-wrapped —
# their whole output IS the result the operator came here to read) and loop
# back to this same menu afterward, so several checks can be run in one
# sitting without re-launching the wizard.
#
# _run_check <label> <cmd...> — the shared "run live, wait, return" body
# every entry below is built from, so a new check is one line to add, not a
# new screen function.
#
# Every command in this menu uses its exit code to report a VERDICT, not
# process health: verify/tune/security-scan/check-deps all return non-zero
# to mean "found something worth reviewing", the normal and expected
# outcome on a system that hasn't had every knob turned yet — not a crash.
# A real crash (an unhandled exception) already prints its own traceback in
# the output just above this line, which the operator can see directly.
# Framing a working report as "✗ exited 1" reads as the wizard breaking
# when it did exactly its job — so this never uses the alarming fail()
# styling, only a neutral note distinguishing "clean" from "found something".
_run_check() {
    local label="$1"; shift
    clear
    print_header
    sep_label "$label"
    printf "\n"
    local rc=0
    "$@" || rc=$?
    printf "\n"
    if (( rc == 0 )); then
        ok "$label — clean, nothing to review"
    else
        warn "$label — found something to review above (this is the check working, not a crash)"
    fi
    printf "\n"
    printf "  %sPress Enter to continue…%s" "$C_DIM" "$C_RESET"
    read -r
    return 0
}

screen_checks() {
    while true; do
        clear
        print_header
        sep_label "Checks & Diagnostics"
        printf "\n"
        note "None of these build a kernel — safe to run any time, in any order."
        printf "\n"
        menu_item 1 "Verify"              "post-reboot health check (mitigations, BORE, cmdline, DKMS, …)"
        menu_item 2 "Status"              "is the running kernel hyphaed-built + module health"
        menu_item 3 "Doctor"              "wide self-diagnostic: detect + verify + repo + driver/module health"
        menu_item 4 "Tune — report"       "idempotent runtime/userspace tuning: what's wrong"
        menu_item 5 "Tune — apply"        "fix what tune-report found"
        menu_item 6 "Security scan"       "kernel-hardening-checker: kconfig + cmdline + sysctl"
        menu_item 7 "Security scan apply" "writes the free-win sysctl fixes; proposes (never writes) cmdline/kconfig changes"
        menu_item 8 "Check build deps"    "dpkg transaction health + missing build deps (read-only)"
        menu_item 0 "Back"                ""
        printf "\n"

        prompt_choice "Select a check"
        case "$REPLY" in
            1) _run_check "Verify"              python3 -m hyphaed verify || true ;;
            2) _run_check "Status"               python3 -m hyphaed status || true ;;
            3) _run_check "Doctor"                python3 -m hyphaed doctor || true ;;
            4) _run_check "Tune — report"        python3 -m hyphaed tune report || true ;;
            5) _run_check "Tune — apply"         python3 -m hyphaed tune apply || true ;;
            6) _run_check "Security scan"        python3 -m hyphaed security-scan report || true ;;
            7)
                if (( DRY_RUN )); then
                    _run_check "Security scan apply (dry-run)" python3 -m hyphaed --dry-run security-scan apply || true
                else
                    _run_check "Security scan apply" python3 -m hyphaed security-scan apply || true
                fi
                ;;
            8) _run_check "Check build deps"     python3 -m hyphaed check-deps || true ;;
            0) return ;;
            *) warn "Enter 0–9" ;;
        esac
    done
}

# ── Harden workstation ───────────────────────────────────────────────────────
# Firewall, fail2ban, rootkit scanners, unattended security updates, DNSCrypt
# status, and a WAN-exposure review — see hyphaed/hardening.py for the full
# reasoning on what's included and what's deliberately NOT (no "anti-
# fingerprint" package, no MAC randomization on a wired box, no claim that
# hardening speeds anything up). `report` is read-only; `apply` runs real
# `apt-get install` / `ufw` / `systemctl` commands via sudo, which is why
# this screen — unlike the checks hub — needs the live-output branch: it can
# genuinely prompt for a sudo password.
screen_harden() {
    while true; do
        clear
        print_header
        sep_label "Harden Workstation"
        printf "\n"
        note "Quick report runs unprivileged — some items can't be fully"
        note "confirmed without root (marked accordingly)."
        printf "\n"
        menu_item 1 "Quick report"                "hyphaed harden report — no sudo, no changes"
        menu_item 2 "Full root diagnostic scan"    "diagnostics/harden-check-*.sh via sudo — ufw ruleset, real port ownership, auth.log, rootkit scan; installs its own scanner deps"
        menu_item 3 "Apply fixes now"              "installs/configures whatever the quick report found missing (needs sudo)"
        menu_item 0 "Back"                         ""
        printf "\n"

        prompt_choice "Select"
        case "$REPLY" in
            1)
                printf "\n"
                python3 -m hyphaed harden report || true
                printf "\n"
                printf "  %sPress Enter to continue…%s" "$C_DIM" "$C_RESET"
                read -r
                ;;
            2)
                printf "\n"
                sep_label "Full root diagnostic scan  (live output — will prompt for sudo)"
                sudo bash "$REPO_ROOT/diagnostics/harden-check-2026-09-22.sh" || true
                printf "\n"
                printf "  %sPress Enter to continue…%s" "$C_DIM" "$C_RESET"
                read -r
                ;;
            3)
                printf "\n"
                sep_label "Harden Workstation — apply  (live output — will prompt for sudo)"
                python3 -m hyphaed harden apply || true
                printf "\n"
                printf "  %sPress Enter to continue…%s" "$C_DIM" "$C_RESET"
                read -r
                ;;
            0) return ;;
            *) warn "Enter 0–3" ;;
        esac
    done
}

# ── Screen: done ──────────────────────────────────────────────────────────────
screen_done() {
    clear
    print_header
    sep_label "Complete"
    printf "\n"

    ok "All phases finished"
    printf "\n"

    screen_roadmap
    sep_label "After the reboot, quick checks"
    note "python3 -m hyphaed status   ·   nvidia-smi   ·   cat /sys/class/greenboost/greenboost/status"
    printf "\n"
    note "Rollback: reboot and pick -generic from GRUB"
    printf "\n"
    sep
    printf "\n"
}


# ── Screen: one post-reboot stop (menu 7 / 8) ─────────────────────────────────
screen_stage_intro() {
    local stage="$1" newest running
    newest=$(_newest_kernel); running=$(uname -r)
    clear
    print_header
    screen_roadmap
    if [[ "$stage" == "proton" ]]; then
        sep_label "Proton: what this does"
        note "1. Refreshes the GreenBoost Proton wrapper (it follows Valve's Proton at launch); asks first, warns about uncommitted edits."
        note "2. Fetches the newest Valve Proton (bleeding-edge), saves what changed since our last build, re-applies our patches."
        note "3. Builds proton-gb-nr in Valve's Docker SDK image with all cores: 1+ hour, 25 GB. Asks before starting and before installing."
        note "4. Records the kernel + Proton pair (ADR-011). Do not run it while a kernel build is going."
        printf "\n"
        local free_gb; free_gb=$(df -Pk "$HOME/Dev/github_gaming" | awk 'NR==2{print int($4/1048576)}')
        (( free_gb < 45 )) && { warn "Only ${free_gb} GB free; need 45. Free space first: sudo bash scripts/clean-space.sh --go"; printf "\n"; exit 0; }
    elif [[ "$stage" == "nvidia" ]]; then
        sep_label "NVIDIA driver: what this does"
        note "1. Builds the NVIDIA open modules for $newest with this CPU's flags (nothing installed; works before the reboot)."
        note "2. Asks you to confirm, removes the apt NVIDIA 615.71.09 packages, installs the new userspace and modules."
        note "3. Installs our modules; they load at the next boot. Rollback: sudo bash diagnostics/nvidia-restore-apt.sh"
        printf "\n"
        if [[ "$running" != "$newest" ]]; then
            info "You are running $running; the new kernel is $newest."
            info "It builds for $newest, then asks to install. Until you reboot, nvidia-smi and new GL windows can fail (version mismatch). Reboot right after."
            printf "\n"
        fi
        if [[ -n "$(nvidia-smi --query-compute-apps=process_name --format=csv,noheader 2>/dev/null | grep -Ev 'gnome|gjs|nautilus|Xwayland|firefox|chrome|steam|code|ptyxis|kitty' || true)" ]]; then
            warn "GPU compute jobs are running. Stop them first (the step will refuse to continue otherwise)."
        fi
    else
        sep_label "GNOME Shell + Mutter: what this does"
        note "1. Records a 2 minute baseline of the stock packages (CPU, memory, GPU)."
        note "2. Rebuilds mutter and gnome-shell for this CPU, with Ubuntu's own patches kept."
        note "3. Asks you to confirm, installs them and holds the versions. Active after you log out and in."
        note "4. Afterwards: bash scripts/measure_desktop.sh <out.json>, and --compare against the baseline."
        printf "\n"
        if ! pgrep -u "$USER" -x gnome-shell >/dev/null 2>&1; then
            warn "No GNOME Shell session found for $USER. Run this from a terminal inside your desktop."
            printf "\n"; exit 0
        fi
    fi
    while true; do
        prompt_choice "Start? [y / n]"
        case "${REPLY,,}" in
            y|yes) return ;;
            n|no|"") printf "\n"; exit 0 ;;
            *) warn "y · n" ;;
        esac
    done
}

screen_stage_done() {
    clear
    print_header
    sep_label "Complete"
    printf "\n"
    ok "$1 finished"
    printf "\n"
    screen_roadmap
    sep
    printf "\n"
}

# ── Main ──────────────────────────────────────────────────────────────────────
main() {
    MODE="" PRESET="gaming-ai-vm" SINGLE_PHASE="" PLAN_PHASES=""
    SOURCE_MODE="ubuntu" TARGET=""

    screen_welcome

    if [[ "$MODE" == "fetch" ]]; then
        screen_fetch
        exit 0
    fi

    if [[ "$MODE" == "checks" ]]; then
        screen_checks
        exit 0
    fi

    if [[ "$MODE" == "harden" ]]; then
        screen_harden
        exit 0
    fi

    if [[ "$MODE" == "nvidia" || "$MODE" == "desktop" || "$MODE" == "proton" ]]; then
        TARGET="$(uname -r | cut -d- -f1)"
        screen_stage_intro "$MODE"
        PLAN_PHASES="$MODE"
        screen_execute
        screen_stage_done "$MODE"
        exit 0
    fi

    screen_source_mode

    if [[ "$MODE" == "single" ]]; then
        screen_single_phase
        PLAN_PHASES="$SINGLE_PHASE"

    elif [[ "$MODE" == "resume" ]]; then
        local last
        last=$(python3 - "$STATE_FILE" <<'EOF' 2>/dev/null || true
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    order = ["detect", "source", "patch", "configure", "security", "build", "package", "install", "postinstall", "nvidia", "desktop", "proton"]
    completed = set(d.get('phases_completed', []))
    v = ''
    for p in order:
        if p in completed:
            v = p
    print(v)
except Exception:
    print('')
EOF
)
        if [[ -z "$last" ]]; then
            warn "No checkpoint found — starting full build"
            PLAN_PHASES="$(printf '%s\n' "${PHASES[@]}")"
        else
            local idx; idx=$(_phase_index "$last")
            if (( idx >= ${#PHASES[@]} - 1 )); then
                info "All phases already completed — nothing to resume"
                printf "\n"
                exit 0
            fi
            local next="${PHASES[$(( idx + 1 ))]}"
            PLAN_PHASES="$(_phases_from "$next" | grep -vxE 'nvidia|desktop|proton' || true)"
            if [[ -z "$PLAN_PHASES" ]]; then
                info "Kernel stages are complete."
                screen_roadmap
                exit 0
            fi
            info "Resuming from: ${C_WHITE}${next}${C_RESET}"
        fi

    else
        PLAN_PHASES="$(printf '%s\n' "${KERNEL_PHASES[@]}")"
    fi

    screen_confirm
    screen_execute
    screen_done
}

main "$@"
