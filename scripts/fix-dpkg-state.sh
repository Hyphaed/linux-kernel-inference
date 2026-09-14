#!/usr/bin/env bash
# scripts/fix-dpkg-state.sh — repair an interrupted dpkg transaction that
# hides itself from every dpkg-query-based check.
#
# Root cause (see plan / commit message for the full writeup): a dpkg
# transaction interrupted mid-"Preconfiguring packages" leaves packages in
# `install ok unpacked` and journal entries in /var/lib/dpkg/updates/. Once
# dpkg *replays* that journal on the next read — which `dpkg-query -W` and
# `dpkg -l` both do — those packages report as `ii`/`installed`. But
# `dpkg-checkbuilddeps` (used by `dpkg-buildpackage`, and therefore by
# `make bindeb-pkg`) reads /var/lib/dpkg/status directly and sees the
# unpacked-not-configured truth, so it rejects a package every other tool
# just told you was fine.
#
# Usage:
#   sudo bash scripts/fix-dpkg-state.sh            # stage 1 only: configure pending packages
#   sudo bash scripts/fix-dpkg-state.sh --upgrade   # stage 1, then a full `nala upgrade`
#   bash scripts/fix-dpkg-state.sh --check          # report only, no changes, no root needed
#
# Idempotent — safe to re-run, no-ops on a healthy system.
set -euo pipefail

MODE="repair"
for arg in "$@"; do
    case "$arg" in
        --check) MODE="check" ;;
        --upgrade) MODE="upgrade" ;;
        -h|--help)
            printf 'usage: %s [--check|--upgrade]\n' "$0"
            exit 0
            ;;
        *)
            printf 'ERROR: unknown argument: %s\n' "$arg" >&2
            exit 1
            ;;
    esac
done

DPKG_STATUS=/var/lib/dpkg/status
DPKG_UPDATES=/var/lib/dpkg/updates
RUNNING_KERNEL="$(uname -r)"

info()  { printf 'i  %s\n' "$*"; }
warn()  { printf '!  %s\n' "$*" >&2; }
ok()    { printf '\xe2\x9c\x93  %s\n' "$*"; }
err()   { printf '\xe2\x9c\x97  %s\n' "$*" >&2; }

# ── Detection ──────────────────────────────────────────────────────────────
# Two independent signals of an interrupted transaction:
#   1. unreplayed journal files under /var/lib/dpkg/updates/
#   2. any package stanza whose Status: line isn't "install ok installed"
#      (this also naturally covers intentionally-removed packages sitting in
#      "deinstall ok config-files", which are not broken — only "unpacked",
#      "half-configured", "half-installed" etc. are actionable here)
journal_count() {
    find "$DPKG_UPDATES" -mindepth 1 -maxdepth 1 -type f 2>/dev/null | wc -l
}

pending_packages() {
    awk '
        /^Package: / { pkg = $2 }
        /^Status: /  {
            if ($0 !~ /install ok installed/ && $0 !~ /deinstall ok config-files/ && $0 !~ /purge ok not-installed/)
                print pkg ": " $0
        }
    ' "$DPKG_STATUS"
}

report_state() {
    local jcount pending
    jcount=$(journal_count)
    pending="$(pending_packages)"
    local pcount=0
    [[ -n "$pending" ]] && pcount=$(wc -l <<< "$pending")

    info "dpkg journal: ${jcount} unreplayed file(s) in ${DPKG_UPDATES}"
    if (( pcount > 0 )); then
        warn "${pcount} package(s) not fully configured:"
        printf '%s\n' "$pending" | sed 's/^/     /'
    else
        ok "no packages stuck mid-transaction"
    fi

    if (( jcount > 0 || pcount > 0 )); then
        return 1
    fi
    return 0
}

verify_build_gate() {
    # The exact check `dpkg-buildpackage` performs before a kernel .deb build.
    # libssl-dev is the package this repo has actually hit; kept narrow and
    # fast rather than re-deriving the full BUILD_DEPS_PKGS list from Python.
    if ! command -v dpkg-checkbuilddeps >/dev/null 2>&1; then
        warn "dpkg-checkbuilddeps not found — skipping build-gate verification"
        return 0
    fi
    if dpkg-checkbuilddeps -d 'libssl-dev:native, libssl-dev' /dev/null 2>/dev/null; then
        ok "build gate check (dpkg-checkbuilddeps) passes"
        return 0
    else
        err "build gate check (dpkg-checkbuilddeps) still fails"
        return 1
    fi
}

# ── Stage 1: repair ──────────────────────────────────────────────────────────
stage1_repair() {
    info "running: dpkg --configure -a"
    if ! dpkg --configure -a; then
        warn "dpkg --configure -a exited non-zero — trying apt-get --fix-broken install"
        apt-get --fix-broken install -y
    fi

    if ! report_state >/dev/null 2>&1; then
        warn "some packages are still not fully configured after dpkg --configure -a"
    fi

    if ! verify_build_gate; then
        err "repair incomplete — refusing to continue to an upgrade on top of a broken dpkg"
        exit 1
    fi
}

# ── Stage 2: full upgrade ────────────────────────────────────────────────────
# Guarded: never proceed if the plan would remove the kernel we're running,
# or any hyphaed-built kernel — a silently broken boot entry is a much worse
# failure mode than a stale package list.
stage2_upgrade() {
    local upgrader
    if command -v nala >/dev/null 2>&1; then
        upgrader=(nala)
    else
        upgrader=(apt-get)
    fi

    info "simulating upgrade to check for kernel removals"
    local sim
    if [[ "${upgrader[0]}" == "nala" ]]; then
        sim="$(nala upgrade --assume-no 2>&1 || true)"
    else
        sim="$(apt-get -s upgrade 2>&1 || true)"
    fi

    local dangerous
    dangerous="$(grep -Ei "^(Remv|removing|autoremoving).*(${RUNNING_KERNEL}|linux-image-.*-hyphaed|linux-headers-.*-hyphaed)" <<< "$sim" || true)"
    if [[ -n "$dangerous" ]]; then
        err "planned upgrade would remove the running or a hyphaed-built kernel — aborting:"
        printf '%s\n' "$dangerous" | sed 's/^/     /'
        exit 1
    fi
    ok "upgrade plan does not touch ${RUNNING_KERNEL} or any hyphaed kernel"

    info "running: ${upgrader[*]} upgrade"
    if [[ "${upgrader[0]}" == "nala" ]]; then
        nala upgrade -y
    else
        apt-get upgrade -y
    fi

    if ! verify_build_gate; then
        err "build gate check fails after upgrade — investigate before building"
        exit 1
    fi
}

# ── Main ─────────────────────────────────────────────────────────────────────
if [[ "$MODE" == "check" ]]; then
    report_state && exit 0
    exit 1
fi

if [[ "$EUID" -ne 0 ]]; then
    err "must run as root — re-run: sudo bash $0 ${*:-}"
    exit 1
fi

report_state || true   # informational; stage1 runs regardless
stage1_repair
ok "stage 1 complete — build dependencies are configured"

if [[ "$MODE" == "upgrade" ]]; then
    stage2_upgrade
    ok "stage 2 complete — system upgraded"
fi

exit 0
