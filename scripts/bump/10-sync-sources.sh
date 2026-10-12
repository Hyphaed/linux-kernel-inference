#!/usr/bin/env bash
# Refresh EVERY patch source. Clones the missing ones on first run. Safe to re-run.
. "$(dirname "$0")/common.sh"
T="${FETCH_TIMEOUT:-600}"
mkdir -p "$OUT"; : > "$OUT/sources.txt"
rec() { printf '%-22s %s  %s\n' "$1" "$(git -C "$2" rev-parse --short=12 HEAD 2>/dev/null || echo -)" "$(date -Is)" >> "$OUT/sources.txt"; }
f() { timeout "$T" git -C "$1" fetch "${@:2}" || echo "WARN: fetch failed: $1 ${*:2}"; }

step "kernel.org stable + mainline into the kernelorg mirror"
f "$KO" --tags origin; f "$KO" --tags stable
git -C "$KO" rev-parse -q --verify "v$VER^{commit}" >/dev/null \
  || { echo "mirror still lacks v$VER; trying kernel.org directly"; f "$KO" --tags https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git "refs/tags/v$VER:refs/tags/v$VER"; }
git -C "$KO" rev-parse -q --verify "v$VER^{commit}" >/dev/null || { echo "FATAL: v$VER unavailable"; exit 1; }
rec kernelorg "$KO"

step "hyphaed sync-sources (XanMod, CachyOS kernel-patches + PKGBUILDs, tkg, kernel-research)"
$PY -m hyphaed sync-sources || echo "WARN: some sync-sources repos failed"
for r in linux kernel-patches linux-cachyos linux-tkg kernel-research; do rec "$r" "$ROOT/$r"; done

step "Intel linux-kernel-overlay (mainline-tracking/v$SERIES_MM)"
f "$ROOT/github/intel-overlay" --all --tags; rec intel-overlay "$ROOT/github/intel-overlay"

step "stable-queue (what 7.$((${SERIES_MM#*.})) point releases already have queued)"
SQ="$ROOT/github/stable-queue"
[ -d "$SQ/.git" ] || timeout "$T" git clone --depth 1 https://git.kernel.org/pub/scm/linux/kernel/git/stable/stable-queue.git "$SQ"
f "$SQ" --depth 1 origin; git -C "$SQ" reset -q --hard FETCH_HEAD 2>/dev/null || true; rec stable-queue "$SQ"

step "SteamOS evlaV/linux-integration $SERIES_MM/integration (blobless, shallow)"
SV="$ROOT/github/steamos"
[ -d "$SV/.git" ] || timeout "$T" git clone --no-checkout --filter=blob:none --depth 400 -b "$SERIES_MM/integration" \
    https://github.com/evlaV/linux-integration "$SV" || echo "WARN: steamos clone failed (gitlab/github flaky)"
[ -d "$SV/.git" ] && { f "$SV" --depth 400 origin "$SERIES_MM/integration"; rec steamos "$SV"; }

step "patch-source inventory (patches/sources.tsv): verify every URL, clone hardware-relevant carriers"
$PY scripts/bump/source-inventory.py --fetch --candidates || echo "WARN: some inventory sources unreachable (see $OUT/inventory.md)"

step "Ubuntu SAUCE (debian/patches of the archive kernel)"
echo "run on demand: $PY -m patches.eval --against build/linux-$VER --sauce   (empty on 7.2.8; ~250 MB download)" | tee -a "$OUT/sources.txt"
cat "$OUT/sources.txt"
