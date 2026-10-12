#!/usr/bin/env bash
# One process for source->package (Makefile:76: chaining --phase calls corrupts the retarget).
# Re-run safe: skips if debs for $VER exist, FORCE=1 rebuilds.
. "$(dirname "$0")/common.sh"
if ls out/debs/linux-image-"$VER"-hyphaed_*.deb >/dev/null 2>&1 && [ "${FORCE:-0}" != 1 ]; then
  echo "debs for $VER already in out/debs (FORCE=1 to rebuild)"; exit 0
fi
step "invalidate cached phases (the phase cache is state-based, not content-based)"
$PY - <<PY
from pathlib import Path
from hyphaed.state import PersistedState
from hyphaed.phases import ORDER
d = Path("state/machines/$(hostname)"); st = PersistedState.load(d)
st.invalidate_from("source", ORDER); st.save(d); print("invalidated from source")
PY
step "build $VER (~40 min)"
mkdir -p out/logs
$PY -m hyphaed --target "$VER" --phase package -y 2>&1 | tee "out/logs/bump-$VER-$(date +%Y%m%d-%H%M).log"
ls -la out/debs/ | grep "$VER"
