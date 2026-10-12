#!/usr/bin/env bash
# make bump VER=7.2.9  -> every stage up to (not including) install. Re-runnable.
. "$(dirname "$0")/common.sh"
H="$(dirname "$0")"
bash "$H/00-preflight.sh"; bash "$H/10-sync-sources.sh"; $PY "$H/20-sweep.py"
[ "${SWEEP_ONLY:-0}" = 1 ] && { echo "sweep only: triage $OUT/candidates.md"; exit 0; }
bash "$H/30-validate-series.sh"; bash "$H/40-build.sh"; $PY "$H/50-config-audit.py"
printf '\nBuilt. Install and reboot yourself:\n  ! make install\n  (reboot, pick %s-hyphaed)  then:  make post-boot-audit VER=%s\n' "$VER" "$VER"
