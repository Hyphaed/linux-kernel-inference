#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
#
# Boot-log remediation for this box (Ubuntu 26.04 on kernel 7.1.9-hyphaed).
#
# Diagnosed 2026-08-20 from `journalctl -b`. Reports every finding; only two
# of them are actually broken and only those two are acted on. Everything
# else is printed with an explanation so it stops looking alarming.
#
# Default is a DRY RUN. Nothing is modified until you pass --apply.
#
#   ./fix-boot-issues.sh                      # report only, no root needed
#   sudo ./fix-boot-issues.sh --apply         # fix the two real problems
#   sudo ./fix-boot-issues.sh --apply --disable-livepatch
#
# Re-runnable and idempotent: a second --apply on a clean system changes
# nothing and says so.

set -uo pipefail

APPLY=0
DISABLE_LIVEPATCH=0
BACKUP_DIR="/var/backups/greenboost-boot-fix"
SNAPD_PROFILE_DIR="/var/lib/snapd/apparmor/profiles"
STRAY_RE='libgreenboost_audit'
CHANGED=0

for arg in "$@"; do
    case "$arg" in
        --apply)             APPLY=1 ;;
        --disable-livepatch) DISABLE_LIVEPATCH=1 ;;
        -h|--help)           sed -n '3,20p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *) echo "unknown argument: $arg (try --help)" >&2; exit 2 ;;
    esac
done

if (( APPLY )) && [[ $EUID -ne 0 ]]; then
    echo "--apply needs root: sudo $0 --apply" >&2
    exit 1
fi

c_hdr=$'\033[1;36m'; c_ok=$'\033[32m'; c_warn=$'\033[33m'
c_bad=$'\033[31m'; c_dim=$'\033[2m'; c_off=$'\033[0m'
[[ -t 1 ]] || { c_hdr=""; c_ok=""; c_warn=""; c_bad=""; c_dim=""; c_off=""; }

hdr()  { printf '\n%s== %s ==%s\n' "$c_hdr" "$1" "$c_off"; }
ok()   { printf '  %s+%s %s\n' "$c_ok" "$c_off" "$1"; }
warn() { printf '  %s!%s %s\n' "$c_warn" "$c_off" "$1"; }
bad()  { printf '  %sx%s %s\n' "$c_bad" "$c_off" "$1"; }
info() { printf '    %s%s%s\n' "$c_dim" "$1" "$c_off"; }
would() {
    if (( APPLY )); then printf '  %s>%s %s\n' "$c_ok" "$c_off" "$1"
    else printf '  %s>%s would: %s\n' "$c_dim" "$c_off" "$1"; fi
}

printf '%sBoot-log remediation%s  kernel %s  %s\n' \
    "$c_hdr" "$c_off" "$(uname -r)" "$(date '+%Y-%m-%d %H:%M')"
if (( APPLY )); then
    printf '%sMODE: APPLY , changes will be made%s\n' "$c_warn" "$c_off"
else
    printf '%sMODE: dry run , nothing will be modified (pass --apply to act)%s\n' \
        "$c_dim" "$c_off"
fi
printf 'system state: %s\n' "$(systemctl is-system-running 2>/dev/null || true)"

# ── 1. REAL: GreenBoost breaks snapd's AppArmor profiles ─────────────────────
#
# gb_supervisor.py::_patch_apparmor() appends two bare rules with open("a").
# That is correct for /etc/apparmor.d/local/* , an include fragment pulled in
# INSIDE a profile block , but a snapd-generated profile is a complete file
# with its own closing brace, so the appended rules land at top level where
# AppArmor expects `profile NAME {`. Result: TOK_MODE where TOK_OPEN was
# expected, the whole profile fails to parse, and snapd.apparmor.service dies.
hdr "1. snapd AppArmor profiles (REAL , this is why the system is degraded)"

mapfile -t broken < <(
    shopt -s nullglob
    for f in "$SNAPD_PROFILE_DIR"/snap-confine.snapd.* "$SNAPD_PROFILE_DIR"/snap-confine.*; do
        [[ -f "$f" ]] || continue
        # A stray rule is one matching the pattern AFTER the final top-level '}'.
        last_close=$(grep -n '^}' "$f" 2>/dev/null | tail -1 | cut -d: -f1)
        [[ -n "$last_close" ]] || continue
        if awk -v n="$last_close" -v re="$STRAY_RE" \
             'NR>n && $0 ~ re {found=1} END{exit !found}' "$f"; then
            echo "$f"
        fi
    done | sort -u
)

if (( ${#broken[@]} == 0 )); then
    ok "no stray rules outside a profile block"
else
    bad "${#broken[@]} profile(s) carry rules appended past the closing brace"
    for f in "${broken[@]}"; do
        info "$(basename "$f")"
    done
    info "these rules never took effect. They sit PAST the profile's final"
    info "closing brace, at top level, where apparmor_parser expects"
    info "'profile NAME {' , so they grant nothing, and the profile they"
    info "contaminate fails to parse in its entirety. Removing them loses no"
    info "access that was ever in force."
    info ""
    info "NOTE: do not justify this by 'abstractions/greenboost-audit already"
    info "grants it via abstractions/base' , verified false on this box"
    info "2026-08-20: base carries no greenboost-audit include, and the"
    info "supported override /etc/apparmor.d/local/usr.lib.snapd.snap-confine.real"
    info "does not exist. The conclusion holds; that reason did not."
    would "back up to $BACKUP_DIR, strip the stray lines, re-parse, restart snapd.apparmor"

    if (( APPLY )); then
        mkdir -p "$BACKUP_DIR"
        for f in "${broken[@]}"; do
            stamp=$(date +%Y%m%d-%H%M%S)
            cp -a "$f" "$BACKUP_DIR/$(basename "$f").$stamp.bak" || {
                bad "backup failed for $f , skipping it"; continue; }
            last_close=$(grep -n '^}' "$f" | tail -1 | cut -d: -f1)
            tmp=$(mktemp)
            awk -v n="$last_close" -v re="$STRAY_RE" '
                NR<=n { print; next }
                $0 ~ re { next }
                $0 ~ /^# GreenBoost:/ { next }
                { print }
            ' "$f" > "$tmp"
            # Drop any trailing blank lines the removal left behind.
            printf '%s\n' "$(cat "$tmp")" > "$tmp.trimmed" && mv "$tmp.trimmed" "$tmp"

            if apparmor_parser -Q "$tmp" >/dev/null 2>&1; then
                cat "$tmp" > "$f" && ok "cleaned $(basename "$f")"
                CHANGED=1
            else
                bad "$(basename "$f") still fails to parse after cleaning , left untouched"
                info "backup kept; nothing was written to the live profile"
            fi
            rm -f "$tmp"
        done

        if (( CHANGED )); then
            systemctl restart snapd.apparmor.service 2>/dev/null \
                && ok "snapd.apparmor.service restarted" \
                || bad "snapd.apparmor.service still failing , see: systemctl status snapd.apparmor"
        fi
    fi

    warn "this WILL come back unless the source is fixed too:"
    info "gb_supervisor.py::_patch_apparmor() re-appended the same lines on its"
    info "next remediation pass. The durable fix is to stop it writing to"
    info "\$SNAPD_PROFILE_DIR at all , snapd regenerates those files on every"
    info "snap refresh, so appending to them was never going to hold, and the"
    info "append lands outside the profile block where it breaks the parse."
    info "That fix landed in gb_supervisor.py on 2026-08-21; if this script"
    info "finds contamination again afterwards, something else is writing it."
    info ""
    info "On a machine with GreenBoost installed, the same legacy cleanup is"
    info "already available as: greenboost apparmor-uninstall"
    info "(greenboost_setup.sh::_do_purge_apparmor items 4 and 5 strip these"
    info " exact lines from both local/ and the snapd profile dir). The inline"
    info "stripper above is the no-GreenBoost-installed fallback , one"
    info "mechanism, two entry points."
fi

# ── 1b. The SUPPORTED override, which is what should have been there ─────────
hdr "1b. snap-confine local/ override (the supported injection point)"
SC_LOCAL=/etc/apparmor.d/local/usr.lib.snapd.snap-confine.real
SC_HEADER="# GreenBoost: allow snap-confine to mmap the LD_AUDIT stub"
if [[ ! -d /etc/apparmor.d/local ]]; then
    info "no /etc/apparmor.d/local directory , nothing supported to write to"
elif grep -q libgreenboost_audit "$SC_LOCAL" 2>/dev/null; then
    ok "already present: $SC_LOCAL"
else
    bad "missing: $SC_LOCAL"
    info "snapd never rewrites local/, and apparmor includes it INSIDE the"
    info "profile block , so this is the one place the rule both persists and"
    info "actually grants. Its absence is why the unsupported path kept getting"
    info "reached for. Byte-for-byte identical to greenboost_setup.sh:3229-3233,"
    info "including the comment line, which _do_purge_apparmor greps for on"
    info "removal , change one and the uninstall stops finding it."
    would "create $SC_LOCAL with the two mr, rules"
    if (( APPLY )); then
        {
            echo "$SC_HEADER"
            echo "/usr/local/lib/libgreenboost_audit.so mr,"
            echo "/usr/local/lib/x86_64-linux-gnu/libgreenboost_audit.so mr,"
        } >> "$SC_LOCAL" && { ok "created $SC_LOCAL"; CHANGED=1; } \
                         || bad "could not write $SC_LOCAL"
        if apparmor_parser -r /etc/apparmor.d/usr.lib.snapd.snap-confine.real 2>/dev/null; then
            ok "snap-confine profile re-parsed with the override in place"
        else
            bad "re-parse failed , check: apparmor_parser -Q /etc/apparmor.d/usr.lib.snapd.snap-confine.real"
        fi
    fi
fi

# ── 1c. Layer A/B coverage, reported not injected ────────────────────────────
hdr "1c. abstractions coverage (report only)"
if grep -rq greenboost-audit /etc/apparmor.d/abstractions/base 2>/dev/null; then
    ok "abstractions/base includes greenboost-audit (installer Layer A applied)"
else
    info "abstractions/base does NOT include greenboost-audit , installer"
    info "Layer A was never applied on this machine. That is a finding, not"
    info "something this script fixes: injecting into abstractions/base is the"
    info "installer's job (greenboost_setup.sh Layer A), and running a Full"
    info "Install is a much larger action than a boot-log repair."
fi

# ── 2. REAL but self-healing: nvidia_fs missing at boot ──────────────────────
hdr "2. nvidia_fs module (was broken at boot, resolved since)"
if [[ -f /lib/modules/$(uname -r)/updates/dkms/nvidia-fs.ko ]]; then
    if modinfo nvidia-fs >/dev/null 2>&1; then
        ok "nvidia-fs resolvable via modules.dep"
        if lsmod | grep -q '^nvidia_fs'; then
            ok "loaded now"
        else
            info "not loaded, but resolvable , modules-load.d will get it next boot"
        fi
        info "the boot error is an INITRD artifact, not a stale modules.dep."
        info "'Failed to find module nvidia_fs' is logged by the initramfs's own"
        info "systemd-modules-load, which reads /etc/modules-load.d/ but only"
        info "sees the modules baked into the boot image. nvidia-fs is not one"
        info "of them. The real-root load succeeds afterwards, which is why the"
        info "module is present now."
        info "(verified 2026-08-21 by timestamp: the initrd's modules-load ran"
        info " ~7s before initrd-switch-root.target on this box)"
        info "(modules.dep mtime $(stat -c %y /lib/modules/$(uname -r)/modules.dep 2>/dev/null | cut -d. -f1))"
    else
        bad "nvidia-fs.ko present but not in modules.dep"
        would "run depmod -a"
        (( APPLY )) && { depmod -a && ok "depmod -a done" && CHANGED=1; }
    fi
else
    warn "nvidia-fs.ko not built for this kernel"
    info "DKMS has not built it for $(uname -r). Fix: dkms autoinstall"
    info "Only matters if you use GPUDirect Storage; nothing else needs it."
fi

# ── 3. POLICY: livepatch cannot ever work on this kernel ─────────────────────
hdr "3. canonical-livepatch (works on Ubuntu-signed kernels only)"
if command -v canonical-livepatch >/dev/null 2>&1; then
    lp_kernel=$(canonical-livepatch status 2>/dev/null | grep -m1 'kernel:' || true)
    if [[ "$lp_kernel" == *unsupported* ]]; then
        warn "livepatch reports this kernel as unsupported:${lp_kernel#*kernel:}"
        info "It cannot patch a self-built kernel, so it checks in, gets"
        info "nothing, and logs a timeout when the network is slow. That is"
        info "the 'livepatch check failed ... context deadline exceeded' line."
        info "Nothing is unprotected that was ever protected , it has never"
        info "patched this kernel."
        if (( DISABLE_LIVEPATCH )); then
            would "disable the livepatch daemon"
            if (( APPLY )); then
                systemctl disable --now snap.canonical-livepatch.canonical-livepatchd.service \
                    2>/dev/null && ok "livepatch daemon disabled" && CHANGED=1 \
                    || warn "could not disable the unit (is it snap-managed?)"
            fi
        else
            info "left alone. Pass --disable-livepatch to turn it off."
        fi
    else
        ok "livepatch reports a supported kernel"
    fi
else
    ok "canonical-livepatch not installed"
fi

# ── 4. Explained, not fixed ──────────────────────────────────────────────────
hdr "4. Noise that is not a fault (no action taken or needed)"
cat <<'EXPLAIN'
  · [Firmware Bug]: Overriding NUMA node to 0        (13 lines)
      Your ASRock B760M board's ACPI tables omit _PXM for these PCI devices,
      so the kernel assigns node 0. This box has exactly one NUMA node, so
      node 0 is the right answer. Costs nothing. Only a BIOS update removes
      the message, and there is nothing to gain by chasing it.

  · Failed to resolve group 'greenboost': Unknown group
      The INITRD's systemd-tmpfiles, not a groupadd race. Corrected
      2026-08-21: the group is not missing (getent returns gid 972); the
      initrd is a different root filesystem with its own /etc/group and the
      group is not in it. Timestamps settle it , the failure lands ~7s
      BEFORE initrd-switch-root.target. The real-root run seconds later
      succeeds and gaming_mode ends up root:greenboost 0664. Verified.
      Cosmetic.
      Do NOT "fix" this with a sysusers.d fragment: a fragment installed on
      the real root is not in the initrd either, so it cannot help. The
      rule acts on /sys/module/greenboost/, which cannot exist that early,
      so the fix is to keep it out of the boot image.
      Corrected 2026-08-21 (later the same day): greenboost_gaming's
      install.sh did ship an exclusion for this, and it had never run. It
      was an initramfs-tools hook, installed on the strength of
      /etc/initramfs-tools/hooks/ existing , which it does here, shipped by
      initramfs-tools-core, while dracut is what actually builds the image.
      Both installers now detect the generator from the initramfs-tools
      META-package and kernel-install's 50-dracut.install, and dracut gets a
      module rather than a conf line, because omit_drivers omits kernel
      modules and this is a tmpfiles fragment. Verified out of the image.

  · Failed to resolve interface "NetworkManager": No such device
      resolvconf handing systemd-resolved a tag, not an interface name.
      A long-standing Ubuntu NM/resolvconf integration quirk. DNS resolves
      correctly, which is the only thing that matters here.

  · bluetoothd: Failed to set mode / Failed to add device 3C:FA:...
      The adapter is powered off (Powered: no), so restoring a paired
      device at boot had nothing to talk to. Turn Bluetooth on and it goes
      away. Not a fault.

  · systemd-modules-load: Failed to find module 'nvidia_fs'
      Covered in section 2 above. Same class as the tmpfiles line: the
      fragment asking for the module ships in the boot image while the
      module itself (correctly) does not. omit_drivers cannot remove a
      modules-load fragment, so 99hyphaed-no-nvidia deletes it from the
      image. Apply it without rebuilding a kernel:
          sudo python3 -m hyphaed update-boot-config

  · KHO: Failed to reserve lowmem scratch buffer
      Kexec HandOver is compiled in but no scratch region was reserved
      because you did not ask for one on the command line. Unused feature,
      no effect on anything.

  · virt/tdx: TDX not supported by the host platform
      Informational. Consumer silicon has no Intel TDX. Nothing to fix.

  · gkr-pam: unable to locate daemon control file
      gnome-keyring at login, before its daemon is up. Harmless.
EXPLAIN

# ── 5. The big one, which no script should pretend to fix ────────────────────
hdr "5. 461,714 NVIDIA NVKMS allocation errors , read this"
cat <<'STORM'
  Between 17:03:13 and 17:39:09 the display driver failed to allocate GPU
  memory 461,714 times, about 214 times a second, then stopped.

  What actually happened: something filled VRAM and parked there. GreenBoost's
  own flight recorder has the window at 97.9-98.1% VRAM used, 227-258 MiB
  free, with the GPU 1-2% busy and 1.1 GB/s coming in over PCIe. With VRAM
  that full, every GEM allocation the compositor and Thunderbird tried failed,
  and each failure logged a line.

  What it cost: 36 minutes of a desktop that could not allocate new graphics
  buffers. Nothing was damaged and nothing leaked , VRAM is back to normal
  and the errors stopped when that process exited.

  Worth knowing: t2_allocated_mb was 0 and shim_phase was INIT for the whole
  window, so whatever filled VRAM did NOT go through GreenBoost tiering. A
  plain CUDA process took the last of the card. GreenBoost's Rule #1 targets
  ~90% fill precisely to leave the system headroom; this went past that
  because nothing was managing it.

  No config change fixes this, which is why this script does not try. If it
  recurs, the question to ask is which process, and the answer is in:
      gb semantics answer "is VRAM underfilled or overcommitted"
      nvidia-smi --query-compute-apps=pid,used_memory --format=csv
STORM

# ── summary ──────────────────────────────────────────────────────────────────
hdr "Result"
if (( APPLY )); then
    if (( CHANGED )); then
        ok "changes applied. Re-check with: systemctl is-system-running"
        info "backups (if any) in $BACKUP_DIR"
    else
        ok "nothing needed changing"
    fi
    printf '  system state now: %s\n' "$(systemctl is-system-running 2>/dev/null || true)"
else
    info "dry run , re-run with: sudo $0 --apply"
fi
