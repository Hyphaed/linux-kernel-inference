#!/usr/bin/env bash
# Read-only. Fails if the bump can't succeed: bad tag, no space, broken dpkg, red tests.
. "$(dirname "$0")/common.sh"
step "tag v$VER exists on kernel.org"
git ls-remote --tags --refs https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git "v$VER" | grep -q . \
  || { echo "v$VER not on kernel.org"; exit 1; }
step "disk: /boot >= 400 MB, build fs >= 40 GB"
boot_mb=$(df -Pm /boot | awk 'NR==2{print $4}'); fs_gb=$(df -Pk "$ROOT/build" | awk 'NR==2{print int($4/1048576)}')
echo "/boot ${boot_mb} MB free, build fs ${fs_gb} GB free; build/ is $(du -sh build | cut -f1) (old trees: make prune-build)"
[ "$boot_mb" -ge 400 ] && [ "$fs_gb" -ge 40 ] || { echo "not enough space"; exit 1; }
step "dpkg state"
bash scripts/fix-dpkg-state.sh --check
step "tests"
$PY -m pytest tests/ -q
echo "preflight OK (VER=$VER PREV=${PREV:-none})"
