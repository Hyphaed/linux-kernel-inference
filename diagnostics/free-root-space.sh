#!/usr/bin/env bash
# Reclaim space on / after a kernel build fills the disk.
#
# Everything this script deletes is regenerable: stale kernel build trees,
# package-manager download caches, rotated logs. It never touches models,
# VM disks, Steam libraries or anything under /var/lib/greenboost — those
# are reported at the end so you can decide about them yourself.
#
#   ./diagnostics/free-root-space.sh            # show what would go, delete nothing
#   sudo ./diagnostics/free-root-space.sh --go  # actually do it
#
# sudo is only needed for the /var steps. Without it the HOME steps still
# run and the script says which ones it skipped.

set -uo pipefail

KI="${KI_ROOT:-/home/ferran/Dev/kernel_inference}"
KEEP_TREE="${KEEP_TREE:-}"          # e.g. KEEP_TREE=linux-7.1.10
REAL_USER="${SUDO_USER:-$USER}"
HOME_DIR="$(getent passwd "$REAL_USER" | cut -d: -f6)"

GO=0
[[ "${1:-}" == "--go" ]] && GO=1

freed_total=0

say()  { printf '%s\n' "$*"; }
hdr()  { printf '\n\033[1m%s\033[0m\n' "$*"; }

# Size of a path in bytes, 0 if missing.
size_of() { [[ -e "$1" ]] && du -xsb "$1" 2>/dev/null | cut -f1 || echo 0; }

human() { numfmt --to=iec --suffix=B "${1:-0}" 2>/dev/null || echo "${1}B"; }

# drop <path> <why>
drop() {
  local path="$1" why="$2" bytes
  [[ -e "$path" ]] || return 0
  bytes="$(size_of "$path")"
  (( bytes == 0 )) && return 0
  freed_total=$(( freed_total + bytes ))
  if (( GO )); then
    say "  removing $(human "$bytes")  $path"
    rm -rf -- "$path"
  else
    say "  would remove $(human "$bytes")  $path   ($why)"
  fi
}

# run_as_user <cmd...> — package caches belong to the login user, not root.
run_as_user() {
  if [[ "$(id -u)" -eq 0 && "$REAL_USER" != root ]]; then
    sudo -u "$REAL_USER" -H "$@"
  else
    "$@"
  fi
}

say "root filesystem before:"
df -h / | tail -1

# ── 1. stale kernel build trees ──────────────────────────────────────────
# build/ is gitignored and rebuilt from scratch by the source phase
# (git archive of the local kernel.org mirror, ~4 seconds). Only the tree
# the wizard's saved state points at is live.
hdr "1. stale kernel build trees under $KI/build"

live=""
if [[ -z "$KEEP_TREE" && -r "$KI/state/machines/$(hostname)/ctx.json" ]]; then
  live="$(python3 -c '
import json,sys,os
p=sys.argv[1]
try:
    d=json.load(open(p))
except Exception:
    sys.exit(0)
sd=d.get("source_dir") or ""
print(os.path.basename(sd.rstrip("/")))
' "$KI/state/machines/$(hostname)/ctx.json")"
fi
[[ -n "$KEEP_TREE" ]] && live="$KEEP_TREE"

if [[ -z "$live" ]]; then
  say "  ! could not read the live tree out of state/machines/$(hostname)/ctx.json."
  say "    Skipping — rerun with KEEP_TREE=linux-X.Y.Z to say which one to keep."
else
  say "  keeping $live (the wizard's current source_dir)"
  shopt -s nullglob
  for d in "$KI"/build/linux-*/; do
    name="$(basename "$d")"
    [[ "$name" == "$live" ]] && continue
    drop "${d%/}" "superseded kernel tree, re-extracted in ~4s by --phase source"
  done
  shopt -u nullglob
fi

# ── 2. package-manager download caches ───────────────────────────────────
# Pure caches. Deleting them costs one slower reinstall, nothing else.
hdr "2. package-manager caches in $HOME_DIR/.cache"

for c in uv pip; do
  p="$HOME_DIR/.cache/$c"
  [[ -e "$p" ]] || continue
  bytes="$(size_of "$p")"
  (( bytes == 0 )) && continue
  freed_total=$(( freed_total + bytes ))
  if (( GO )); then
    say "  purging $(human "$bytes")  $p"
    case "$c" in
      uv)  run_as_user uv cache clean  >/dev/null 2>&1 || rm -rf -- "$p" ;;
      pip) run_as_user pip3 cache purge >/dev/null 2>&1 || rm -rf -- "$p" ;;
    esac
  else
    say "  would purge $(human "$bytes")  $p   (redownloaded on next install)"
  fi
done

# ── 3. system caches and logs (needs root) ───────────────────────────────
hdr "3. system caches and logs under /var"

if [[ "$(id -u)" -ne 0 ]]; then
  say "  skipped — needs root. Rerun with sudo to include these."
else
  before_var="$(size_of /var/cache/apt)"
  if (( GO )); then
    apt-get clean >/dev/null 2>&1 && say "  apt clean: freed $(human "$before_var")"
    journalctl --vacuum-size=200M 2>&1 | tail -1 | sed 's/^/  journal: /'
    find /var/tmp -mindepth 1 -maxdepth 1 -mtime +7 -exec rm -rf {} + 2>/dev/null \
      && say "  /var/tmp: removed entries older than 7 days"
  else
    say "  would apt clean: $(human "$before_var")"
    say "  would vacuum the journal to 200M (currently $(journalctl --disk-usage 2>/dev/null | grep -o '[0-9.]*[KMG]' | tail -1))"
    say "  would remove /var/tmp entries older than 7 days ($(du -xsh /var/tmp 2>/dev/null | cut -f1) total)"
  fi
  freed_total=$(( freed_total + before_var ))
fi

# ── summary ──────────────────────────────────────────────────────────────
hdr "summary"
if (( GO )); then
  say "freed roughly $(human "$freed_total")"
  say ""
  say "root filesystem after:"
  df -h / | tail -1
else
  say "would free roughly $(human "$freed_total")"
  say ""
  say "Nothing was deleted. Rerun with --go (and sudo for the /var steps):"
  say "  sudo $0 --go"
fi

# ── not touched, on purpose ──────────────────────────────────────────────
hdr "big things this script will NOT delete"
say "These are yours to judge. Sizes only, no action taken."
for p in "$HOME_DIR/.cache/huggingface" "$HOME_DIR/vmware" \
         "$HOME_DIR/.local/share/Steam" /var/lib/greenboost \
         "$HOME_DIR/.local/share/mamba" "$HOME_DIR/.cache/vmware"; do
  [[ -e "$p" ]] || continue
  printf '  %-45s %s\n' "$p" "$(du -xsh "$p" 2>/dev/null | cut -f1)"
done
say ""
say "  huggingface is downloaded model weights — deleting one means"
say "  refetching it over the network, not losing it. Check what is actually"
say "  served first:  gb synapse status"
