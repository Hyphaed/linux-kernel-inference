#!/usr/bin/env bash
# Prove the series stacks and compiles on v$VER BEFORE the 37-minute build.
# Real sequential `git am --3way` (patches/eval.py's per-patch check misreports 0020/0023),
# then compile every object the series touches. Throwaway tree: build/validate-$VER.
. "$(dirname "$0")/common.sh"
V="$ROOT/build/validate-$VER"
step "verify vendored patches match the lock"
$PY -m hyphaed fetch-patches
step "extract v$VER into $V"
rm -rf "$V"; mkdir -p "$V"
git -C "$KO" archive "v$VER" | tar -x -C "$V"
git -C "$V" init -q; git -C "$V" add -A >/dev/null; git -C "$V" -c user.name=bump -c user.email=bump@localhost commit -qm "base v$VER"
step "sequential git am --3way"
touched=$(mktemp)
while read -r p; do
  [[ -z "$p" || "$p" == \#* ]] && continue
  f="$SERIES_DIR/$p"; echo "  $p"
  if ! git -C "$V" -c user.name=bump -c user.email=bump@localhost am --3way -q "$ROOT/$f"; then
    echo "FAIL: $p"; git -C "$V" apply --check -v "$ROOT/$f" 2>&1 | tail -8
    git -C "$V" am --abort 2>/dev/null; exit 1
  fi
  grep -E '^diff --git' "$ROOT/$f" | sed 's#.* b/##' >> "$touched"
done < "$SERIES_DIR/series"
LC_ALL=C sort -u "$touched" -o "$touched"
step "7.2-removed API scan"
$PY -m pytest tests/test_kernel_org_72.py -q -k removed
step "configure from the running kernel's config"
cp "/boot/config-$(uname -r)" "$V/.config"
make -C "$V" -s olddefconfig >/dev/null && make -C "$V" -s -j"$(nproc)" modules_prepare
objs=$(grep -E '\.c$' "$touched" | sed 's/\.c$/.o/' | while read -r o; do [ -f "$V/${o%.o}.c" ] && echo "$o"; done)
# files changed between releases that a patch also touches = where context drift hides
if [ -n "${PREV:-}" ] && git -C "$KO" rev-parse -q --verify "v$PREV^{commit}" >/dev/null; then
  git -C "$KO" diff --name-only "v$PREV" "v$VER" | LC_ALL=C sort -u | LC_ALL=C comm -12 - "$touched" | sed 's/^/  drifted under a patch: /'
fi
# kernel/sched/{core,fair,debug,...}.c are #included by build_policy.c / build_utility.c: compiling
# them as standalone objects fails on missing includes (e.g. cpu_clock in debug.c), a harness artefact.
objs=$(echo "$objs" | tr ' ' '\n' | grep -vE '^kernel/sched/(core|fair|rt|deadline|ext|idle|cputime|stop_task|syscalls|wait|wait_bit|swait|completion|loadavg|clock|cpupri|cpudeadline|topology|debug|stats|pelt|autogroup|membarrier|isolation|cpuacct|psi|build_.*)\.o$' || true)
if grep -q '^kernel/sched/' "$touched"; then objs="$objs kernel/sched/build_policy.o kernel/sched/build_utility.o"; fi
objs=$(echo $objs | tr ' ' '\n' | sort -u | tr '\n' ' ')
step "compile $(echo $objs | wc -w) objects (serial make -k: a parallel make of these targets died with fixdep ".d: No such file" errors, serial passes)"
# shellcheck disable=SC2086
if ! log=$(make -C "$V" -k $objs 2>&1); then
  # "No rule" = object not built under this .config (=n / other arch): skipped, not failed
  real=$(grep -E "error:|\*\*\* \[.*(Error|error)" <<<"$log" | grep -v "No rule to make target" || true)
  if [ -n "$real" ]; then echo "$log" | grep -E -B2 -A6 "error:" | head -60; echo "FAIL"; rm -f "$touched"; exit 1; fi
  echo "$log" | { grep "No rule to make target" || true; } | sed 's/^/  skip (not built here): /'
fi
rm -f "$touched"
[ "${KEEP:-0}" = 1 ] || rm -rf "$V"
echo "series validated on v$VER"
