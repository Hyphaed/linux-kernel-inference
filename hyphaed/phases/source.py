from __future__ import annotations
import functools
import os
import pwd
import re
import shlex
import shutil
import tarfile
import types
import subprocess
from pathlib import Path

from rich.markup import escape
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

from .. import gitops
from ..util import log
from ..util.run import run, run_sudo
from ..util.checksum import sha256_file
from ..util.prompts import select, confirm

_DPKG_EXTRACT_RE = re.compile(r"^dpkg-source: info: extracting ")
_DPKG_APPLY_RE = re.compile(r"^dpkg-source: info: applying (\S+)")

NAME = "source"
DEB_SRC_HELPER_LINE = (
    "Run: sudo sed -i '/^# *deb-src /s/^# *//' /etc/apt/sources.list "
    "&& sudo apt update"
)

KERNEL_ORG_GIT = "https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git"
KERNEL_STABLE_MIRROR = "https://github.com/gregkh/linux"  # GitHub mirror of linux-stable
MAINLINE_PPA = "https://kernel.ubuntu.com/mainline"

# Local github mirrors — populated by the user running `git clone` into github/
# kernelorg/linux may be either mainline (no stable tags) or linux-stable.
# xanmod's repo tracks linux-stable and has the most useful stable tags.
_GITHUB_DIR = Path(__file__).resolve().parents[2] / "github"
_GITHUB_REPOS = {
    "kernelorg": _GITHUB_DIR / "kernelorg" / "linux",
    "xanmod":    _GITHUB_DIR / "linux",
    "cachyos":   _GITHUB_DIR / "linux-cachyos",
    "tkg":       _GITHUB_DIR / "linux-tkg",
}


#: Per-remote wall-clock ceiling for the tag refresh in _git_stable_tags().
#: Refreshing tags is a nicety — the local mirror already knows every release
#: it has ever fetched — so it must never be the reason a prompt does not
#: appear. 20 s is generous for a tags-only fetch on a working link and short
#: enough that a human does not conclude the wizard is dead.
_TAG_FETCH_TIMEOUT_S = 20.0

#: Ceiling for the cheap `ls-remote` probe that decides whether the fetch
#: above is worth running at all. Measured 0.6 s against gregkh/linux.
_TAG_PROBE_TIMEOUT_S = 10.0


def _newest_stable(tags) -> tuple[int, ...]:
    """Highest vX.Y.Z (non-rc) in `tags`, as a comparable tuple. () if none."""
    best: tuple[int, ...] = ()
    for tag in tags:
        # kernel.org tags a .0 release as vX.Y, NOT vX.Y.0 — v7.2 is the real
        # tag for 7.2. Requiring three components made every major release
        # invisible to the version list at exactly the moment it mattered.
        m = re.match(r"^v?(\d+)\.(\d+)(?:\.(\d+))?$", tag.strip())
        if not m:
            continue
        best = max(best, tuple(int(g or 0) for g in m.groups()))
    return best


def _refresh_stable_tags(repo_dir: Path) -> None:
    """Pull new stable tags into the mirror, from kernel.org, only when there
    are any.

    **Source of truth is kernel.org**, `KERNEL_ORG_GIT` — the same URL
    `_fetch_kernel_org()` already clones the actual source from. The mirror's
    `stable` remote is github.com/gregkh/linux, which is a mirror and can lag;
    it stays as a fallback for when kernel.org is unreachable, not as the
    primary answer to "what is the newest release".

    Two things this deliberately does NOT do, both measured 2026-08-20 after
    `install_wizard.sh` sat on "Fetching latest kernel.org stable releases…"
    and `hyphaed list-versions` took 80 seconds.

    **It does not fetch `origin`.** The mirror's `origin` is torvalds/linux,
    which carries `vX.Y` and `-rc` tags and *no point releases at all* —
    `git ls-remote --tags --refs origin 'v7.1.*'` returns nothing. Fetching it
    enumerated the entire Linux tag namespace and could not contribute one
    stable release.

    **It does not fetch unconditionally.** `git fetch --tags` has no
    "newest N" form; it negotiates the whole namespace, ~5,700 tags here.
    `git ls-remote` answers "is there anything newer than what I have" in
    **0.25 s** against kernel.org without transferring a single object. So the
    common case (nothing new) costs one cheap round trip and the real fetch
    runs only when a release we lack actually exists.

    Every network step is bounded and degrades to the local tag list. Listing
    releases is a convenience and must never be the reason a prompt fails to
    appear. GIT_TERMINAL_PROMPT=0 stops git blocking on a credential read,
    which no timeout of ours could observe.
    """
    net_env = {"GIT_TERMINAL_PROMPT": "0"}

    # Authoritative first, mirror second. A named remote is used only as a
    # fallback source, and only if the repo actually has one.
    sources: list[str] = [KERNEL_ORG_GIT]
    remotes_r = run(["git", "-C", str(repo_dir), "remote"], check=False, force=True)
    if "stable" in (remotes_r.stdout or "").splitlines():
        sources.append("stable")

    local_r = run(["git", "-C", str(repo_dir), "tag", "--sort=-version:refname"],
                  check=False, force=True)
    local_newest = _newest_stable(local_r.stdout.splitlines() if local_r.ok() else [])

    for src in sources:
        label = "kernel.org" if src == KERNEL_ORG_GIT else f"'{src}'"
        try:
            probe = run(["git", "-C", str(repo_dir), "ls-remote", "--tags", "--refs", src],
                        check=False, force=True, timeout=_TAG_PROBE_TIMEOUT_S, env=net_env)
        except subprocess.TimeoutExpired:
            log.warn(f"tag probe against {label} exceeded {_TAG_PROBE_TIMEOUT_S:.0f}s")
            continue
        if not probe.ok():
            log.warn(f"tag probe against {label} failed — trying the next source")
            continue

        remote_newest = _newest_stable(
            line.rsplit("refs/tags/", 1)[-1] for line in probe.stdout.splitlines()
            if "refs/tags/" in line)
        if remote_newest <= local_newest:
            return      # up to date; skip the expensive part entirely

        try:
            run(["git", "-C", str(repo_dir), "fetch", "--tags", "--quiet", src],
                check=False, force=True, timeout=_TAG_FETCH_TIMEOUT_S, env=net_env)
        except subprocess.TimeoutExpired:
            log.warn(f"tag fetch from {label} exceeded {_TAG_FETCH_TIMEOUT_S:.0f}s — "
                     f"using the mirror's local tags "
                     f"(v{'.'.join(map(str, remote_newest))} exists upstream but "
                     f"was not retrieved)")
        return

    log.warn("no tag source reachable — using the mirror's local tags "
             "(a release newer than the last fetch may be missing)")


def _git_stable_tags(repo_dir: Path, n: int = 3, refresh: bool = True) -> list[str]:
    """Return the latest N stable (non-rc) version tags from a local git repo.

    Tags are read from the LOCAL mirror. When `refresh` is set, a bounded
    fetch runs first so a release published since the last clone shows up —
    but a fetch that is slow, stalled or offline degrades to the local tag
    list instead of blocking.

    Why bounded, found 2026-08-20: this function fetched `origin` and then
    `stable` with no ceiling, and both are full tag fetches of the Linux
    tree. `hyphaed list-versions` measured **80 seconds** on a healthy link,
    and `install_wizard.sh` sat on "Fetching latest kernel.org stable
    releases…" with no output and no way to tell whether it had hung. On a
    dropped link it would have waited forever. `_stable_tags_with_dates()`
    already documented itself as working "fully offline once the mirror has
    been cloned/fetched" — that was true of the dates and false of the tags.

    GIT_TERMINAL_PROMPT=0 is set because a git that decides to ask for
    credentials blocks on a terminal read, which no timeout on our side can
    see coming and which turns a hang into an invisible one.
    """
    if not repo_dir.is_dir():
        return []
    if refresh:
        _refresh_stable_tags(repo_dir)
    r = run(["git", "-C", str(repo_dir), "tag", "--sort=-version:refname"],
            check=False, force=True)
    if not r.ok():
        return []
    tags = []
    seen_ver: set[str] = set()
    for t in r.stdout.splitlines():
        t = t.strip()
        # Match X.Y.Z and vX.Y.Z, with optional distro suffix (xanmod1, -rc not included)
        # vX.Y (a .0 release, e.g. v7.2) as well as vX.Y.Z — see
        # _newest_stable() for why the three-component form is not enough.
        m = re.match(r"^v?(\d+\.\d+(?:\.\d+)?)([.-]\w+\d+)?$", t)
        if not m:
            continue
        if "rc" in t:
            continue
        base_ver = m.group(1)
        if base_ver in seen_ver:
            continue  # one entry per base version (prefer distro variant if sorted first)
        seen_ver.add(base_ver)
        tags.append(t)
        if len(tags) >= n:
            break
    return tags


def _ls_remote_stable_tags(n: int = 5) -> list[str]:
    """Fetch the newest N stable (non-rc) base versions straight from
    kernel.org via `git ls-remote`, no local clone required.

    Found 2026-09-14: `list-versions` returned `{"releases": []}` and the
    wizard printed "no local kernel.org mirror / no network" even though the
    network was fine — `_git_stable_tags()` is local-mirror-only by
    construction (`if not repo_dir.is_dir(): return []`), and
    `github/kernelorg/linux` had simply never been cloned. A bare
    `ls-remote --tags` against kernel.org answers in ~1s and transfers no
    objects, so there is no reason a missing mirror should force manual
    entry. Bounded by the same `_TAG_PROBE_TIMEOUT_S` and
    `GIT_TERMINAL_PROMPT=0` as the probe in `_refresh_stable_tags()`.
    """
    net_env = {"GIT_TERMINAL_PROMPT": "0"}
    try:
        r = run(["git", "ls-remote", "--tags", "--refs", KERNEL_ORG_GIT],
                check=False, force=True, timeout=_TAG_PROBE_TIMEOUT_S, env=net_env)
    except subprocess.TimeoutExpired:
        log.warn(f"tag probe against kernel.org exceeded {_TAG_PROBE_TIMEOUT_S:.0f}s")
        return []
    if not r.ok():
        return []

    versions: list[tuple[tuple[int, ...], str]] = []
    for line in r.stdout.splitlines():
        if "refs/tags/" not in line:
            continue
        tag = line.rsplit("refs/tags/", 1)[-1].strip()
        if "rc" in tag:
            continue
        # Same two shapes as _newest_stable(): vX.Y (a .0 release, e.g. v7.2
        # for 7.2.0) and vX.Y.Z.
        m = re.match(r"^v?(\d+)\.(\d+)(?:\.(\d+))?$", tag)
        if not m:
            continue
        major, minor, patch = (int(g or 0) for g in m.groups())
        base = f"{major}.{minor}.{patch}" if m.group(3) else f"{major}.{minor}"
        versions.append(((major, minor, patch), base))

    versions.sort(key=lambda pair: pair[0], reverse=True)
    seen: set[str] = set()
    out: list[str] = []
    for _, base in versions:
        if base in seen:
            continue
        seen.add(base)
        out.append(base)
        if len(out) >= n:
            break
    return out


def _kernel_org_release_dates() -> dict[str, str]:
    """Best-effort {base_version: isodate} from kernel.org's releases.json.

    Only exposes the CURRENT stable/longterm/mainline tips, not a full
    history — see `_stable_tags_with_dates()`'s docstring for why the local
    mirror's tag commit date is preferred when it's available. This is the
    fallback used to date the tags `_ls_remote_stable_tags()` returns, since
    ls-remote itself carries no dates. Returns {} on any network or parse
    failure — a missing date must never be the reason a version fails to
    show up in the list.
    """
    import json as _json
    r = run(["curl", "-sf", "--max-time", "15", "https://www.kernel.org/releases.json"],
            check=False, force=True)
    if not r.ok() or not r.stdout.strip():
        return {}
    try:
        data = _json.loads(r.stdout)
    except ValueError:
        return {}
    out: dict[str, str] = {}
    for rel in data.get("releases", []):
        ver = rel.get("version")
        date = (rel.get("released") or {}).get("isodate")
        if ver and date:
            out[str(ver).lstrip("v")] = date
    return out


_KERNEL_SOURCE_REPOS = {"kernelorg", "xanmod"}  # cachyos/tkg are script/config repos, not kernel trees


def _github_latest_stable_tags(n: int = 3) -> dict[str, list[str]]:
    """Fetch and return the latest N stable tags from kernel-source mirrors only.

    linux-cachyos and linux-tkg are config/PKGBUILD repos — their tags don't
    represent buildable kernel trees and are excluded.
    """
    result: dict[str, list[str]] = {}
    for name, path in _GITHUB_REPOS.items():
        if name not in _KERNEL_SOURCE_REPOS:
            continue
        tags = _git_stable_tags(path, n)
        if tags:
            result[name] = tags
    return result


def _stable_tags_with_dates(repo_dir: Path, n: int = 5) -> list[tuple[str, str]]:
    """Return the latest N kernel.org stable (non-rc) tags with release dates.

    Each entry is (base_semver, isodate), newest first. Dates come from the
    local mirror's own tag commit date (`git log -1 --format=%aI`) — accurate
    for kernel.org stable tags (pushed at release time) and works fully
    offline once the mirror has been cloned/fetched, unlike querying
    kernel.org's releases.json, which only exposes the CURRENT stable/
    longterm/mainline pointers, not a history of the last N point releases.

    When no local mirror exists (`repo_dir` was never cloned), falls back to
    `_ls_remote_stable_tags()` + `_kernel_org_release_dates()` — a missing
    mirror is a convenience gap, not a reason to report zero releases (see
    `_ls_remote_stable_tags()`'s docstring for the bug this fixes). A date
    kernel.org's releases.json doesn't cover (it only carries the current
    stable/longterm/mainline tips) comes back as "" rather than "unknown",
    to distinguish "not in that feed" from "git log failed on a tag we have
    locally".
    """
    tags = _git_stable_tags(repo_dir, n)
    if not tags:
        remote_vers = _ls_remote_stable_tags(n)
        if not remote_vers:
            return []
        dates = _kernel_org_release_dates()
        return [(v, dates.get(v, "")) for v in remote_vers]

    out: list[tuple[str, str]] = []
    for tag in tags:
        m = re.match(r"^v?(\d+\.\d+(?:\.\d+)?)", tag)
        base = m.group(1) if m else tag.lstrip("v")
        r = run(["git", "-C", str(repo_dir), "log", "-1", "--format=%aI", tag],
                check=False, force=True)
        date = r.stdout.strip()[:10] if r.ok() and r.stdout.strip() else "unknown"
        out.append((base, date))
    return out


def _pick_kernel_version_interactive(n: int = 5) -> tuple[str, str] | None:
    """Show the last N kernel.org stable releases (with release dates) plus a
    'custom version' option, and prompt for selection.

    kernel-org is the sole source mode, so this only ever offers kernel.org
    releases — no other repo is shown. Tags come from the local kernelorg
    mirror when it exists; `_stable_tags_with_dates()` falls back to a
    direct `git ls-remote` against kernel.org when it doesn't, so a missing
    mirror alone does not return None here — only mirror-absent-AND-
    network-unreachable does.

    Returns (base_semver, "kernelorg") or None if neither source produced a
    release list. base_semver is the X.Y.Z part only (e.g. "7.1.7").
    """
    from ..util.prompts import select as prompt_select, text as prompt_text

    log.info("Fetching latest kernel.org stable releases…")
    repo_dir = _GITHUB_REPOS["kernelorg"]
    entries = _stable_tags_with_dates(repo_dir, n)
    if not entries:
        log.warn("no local kernelorg mirror and kernel.org is unreachable — pass --target explicitly")
        return None

    from rich.table import Table
    table = Table(title=f"Latest {len(entries)} kernel.org stable releases", box=None, pad_edge=False)
    table.add_column("#", style="cyan", no_wrap=True)
    table.add_column("Version", style="white")
    table.add_column("Released", style="white")

    choices: list[str] = []
    choice_map: dict[str, str] = {}  # "1".."N" -> base_semver
    for i, (ver, date) in enumerate(entries, start=1):
        key = str(i)
        table.add_row(key, ver, date)
        choices.append(key)
        choice_map[key] = ver
    custom_key = str(len(entries) + 1)
    table.add_row(custom_key, "(enter a custom version)", "")
    choices.append(custom_key)

    log.console.print(table)
    log.console.print()

    chosen = prompt_select("Select kernel version to build", choices=choices, default=choices[0])
    if chosen == custom_key:
        custom = prompt_text("Enter kernel.org version (e.g. 7.1.7)")
        return (custom.lstrip("v"), "kernelorg")
    return (choice_map[chosen], "kernelorg")


def _clear_canonical_cert_refs(text: str) -> str:
    """Canonical's mainline .config references their own debian.master/ cert
    files (debian/canonical-certs.pem, debian/canonical-revoked-certs.pem) for
    the trusted/revocation keyrings. Those only exist in Canonical's real
    packaging tree, not in vanilla kernel.org's auto-generated debian/
    skeleton (scripts/package/mkdebian) — building with them set fails at
    `certs/x509_certificate_list` with "No rule to make target". Clearing
    them just means "no extra trusted keys beyond the kernel's own
    self-generated certs/signing_key.pem" — irrelevant here since Secure
    Boot is already disabled on this box and we're not Canonical's chain.
    """
    text = re.sub(r'^CONFIG_SYSTEM_TRUSTED_KEYS=.*$', 'CONFIG_SYSTEM_TRUSTED_KEYS=""', text, flags=re.MULTILINE)
    text = re.sub(r'^CONFIG_SYSTEM_REVOCATION_KEYS=.*$', 'CONFIG_SYSTEM_REVOCATION_KEYS=""', text, flags=re.MULTILINE)
    return text


def _nearest_mainline_tag_in_series(tag: str) -> str | None:
    """`v7.1.6` -> newest built non-rc `v7.1.x` tag <= v7.1.6 (e.g. `v7.1.5`).

    Used when the exact requested tag has no amd64 build on
    kernel.ubuntu.com/mainline yet — we still want a same-series Canonical
    baseline config rather than none at all. Returns None if the archive is
    unreachable or nothing in the series has a build.
    """
    norm = tag if tag.startswith("v") else f"v{tag}"
    m = re.match(r"^v(\d+)\.(\d+)", norm)
    if not m:
        return None
    major, minor = m.group(1), m.group(2)
    tags = _mainline_available_tags()
    series = [t for t in tags if "-rc" not in t and t.startswith(f"v{major}.{minor}.")]
    if not series:
        return None

    def _ver(t: str) -> tuple[int, ...]:
        return tuple(int(x) for x in t.lstrip("v").split(".") if x.isdigit())

    target_ver = _ver(norm)
    not_newer = [t for t in series if _ver(t) <= target_ver]
    candidates = not_newer or series
    built = [t for t in candidates if _mainline_has_amd64_build(t)]
    if not built:
        return None
    return max(built, key=_ver)


def _fetch_mainline_baseline_config(
    tag: str, repo_root: Path, allow_series_fallback: bool = True
) -> str | None:
    """Download Canonical's official mainline build for `tag` (e.g. "v7.1.1")
    and recover the .config they used — closes the "no baseline to merge our
    fragments onto" gap that a raw kernel.org checkout would otherwise leave.
    Verifies against their published CHECKSUMS. Returns the synthetic
    "<version>-<abi>-generic" string (matches Ubuntu's own naming convention,
    e.g. "7.1.1-070101-generic") so callers can reuse it as profile.running_kernel.

    When `allow_series_fallback` and mainline has no build for `tag` itself,
    falls back once to the newest built tag in the same major.minor series
    (e.g. v7.1.6 -> v7.1.5) — a same-series Canonical config is a valid
    `make olddefconfig` baseline for a point release it wasn't built for.
    """
    base_url = f"{MAINLINE_PPA}/{tag}/amd64"
    r = run(["curl", "-sf", "--max-time", "15", f"{base_url}/"], check=False, force=True)
    if not r.ok():
        if allow_series_fallback:
            fallback = _nearest_mainline_tag_in_series(tag)
            if fallback and fallback != tag:
                log.info(f"{tag} not reachable on kernel.ubuntu.com/mainline — trying nearest in-series build {fallback}")
                return _fetch_mainline_baseline_config(fallback, repo_root, allow_series_fallback=False)
        log.warn(f"could not reach {base_url}/ — is this tag actually built on kernel.ubuntu.com/mainline?")
        return None
    m = re.search(r'href="(linux-modules-([\d.]+-\d+)-generic_[^"]+_amd64\.deb)"', r.stdout)
    if not m:
        if allow_series_fallback:
            fallback = _nearest_mainline_tag_in_series(tag)
            if fallback and fallback != tag:
                log.info(f"no amd64 build for {tag} — trying nearest in-series build {fallback}")
                return _fetch_mainline_baseline_config(fallback, repo_root, allow_series_fallback=False)
        log.warn(f"no linux-modules amd64 .deb found for {tag} on kernel.ubuntu.com/mainline")
        return None
    deb_name, kver = m.group(1), m.group(2)

    checksums_r = run(["curl", "-sf", "--max-time", "15", f"{base_url}/CHECKSUMS"], check=False, force=True)
    expected_sha256 = None
    if checksums_r.ok():
        for line in checksums_r.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == deb_name and len(parts[0]) == 64:
                expected_sha256 = parts[0]
                break

    tmp_dir = repo_root / "build" / ".mainline-baseline"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    deb_path = tmp_dir / deb_name
    log.info(f"fetching Canonical mainline baseline config for {tag} ({deb_name})")
    dl = run(["curl", "-sfL", "--max-time", "120", "-o", str(deb_path), f"{base_url}/{deb_name}"], check=False, force=True)
    if not dl.ok():
        log.warn(f"download failed for {deb_name}")
        return None

    if expected_sha256:
        actual = sha256_file(deb_path)
        if actual.lower() != expected_sha256.lower():
            log.err(f"checksum mismatch for {deb_name}: expected {expected_sha256}, got {actual}")
            deb_path.unlink(missing_ok=True)
            return None
        log.ok("CHECKSUMS verified")
    else:
        log.warn("no matching CHECKSUMS entry found — proceeding unverified")

    extract_dir = tmp_dir / "extract"
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    r = run(["dpkg-deb", "-x", str(deb_path), str(extract_dir)], check=False, force=True)
    if not r.ok():
        log.warn(f"dpkg-deb extraction failed for {deb_name}")
        return None
    cfg_name = f"config-{kver}-generic"
    cfg_src = extract_dir / "boot" / cfg_name
    if not cfg_src.exists():
        log.warn(f"{cfg_src} not found inside {deb_name}")
        return None
    base_dir = repo_root / "configs" / "base"
    base_dir.mkdir(parents=True, exist_ok=True)
    dest = base_dir / cfg_name
    shutil.copy2(cfg_src, dest)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    dest.write_text(_clear_canonical_cert_refs(dest.read_text()))

    log.ok(f"recovered Canonical baseline config -> {dest} (cleared debian/canonical-*.pem refs)")
    return f"{kver}-generic"


@functools.lru_cache(maxsize=1)
def _mainline_available_tags() -> list[str]:
    """Fetch kernel.ubuntu.com/mainline index and return all version tags found.

    Returns an empty list when the archive is unreachable (network down, CI).
    Tags look like "v7.1.1" or "v7.2-rc3".

    Cached (single process, one `source` phase run only lists the archive
    once): `_fetch_kernel_org`'s preflight and `_fetch_mainline_baseline_config`'s
    series-fallback search both call this — uncached, a single `source` phase
    was observed issuing the same 7-request mainline probe sequence twice.
    """
    r = run(["curl", "-sf", "--max-time", "15", f"{MAINLINE_PPA}/"], check=False, force=True)
    if not r.ok():
        return []
    return sorted(set(re.findall(r'href="(v[\d]+\.[\d]+(?:\.[\d]+)?(?:-rc\d+)?)/?"', r.stdout)))


def _latest_mainline_stable(tags: list[str] | None = None) -> str | None:
    """Return the newest non-RC tag from the mainline archive (e.g. "v7.1.1")."""
    if tags is None:
        tags = _mainline_available_tags()
    stable = [t for t in tags if "-rc" not in t and re.match(r"^v\d+\.\d+", t)]
    if not stable:
        return None

    def _ver(t: str) -> tuple[int, ...]:
        return tuple(int(x) for x in t.lstrip("v").split(".") if x.isdigit())

    return max(stable, key=_ver)


@functools.lru_cache(maxsize=None)
def _mainline_has_amd64_build(tag: str) -> bool:
    """Lightweight check: does the mainline archive have an amd64 .deb for `tag`?

    Only fetches the directory listing (< 2 kB), not the .deb itself. Cached
    per tag — see `_mainline_available_tags` docstring.
    """
    norm = tag if tag.startswith("v") else f"v{tag}"
    base_url = f"{MAINLINE_PPA}/{norm}/amd64"
    r = run(["curl", "-sf", "--max-time", "15", f"{base_url}/"], check=False, force=True)
    if not r.ok():
        return False
    return bool(re.search(r"linux-modules-[\d.]+-\d+-generic_[^\"]+_amd64\.deb", r.stdout))


def _fetch_xanmod(ctx, target: str) -> Path:
    """Clone the xanmod github mirror at the given version into build/.

    target should be a semver string like "7.1.2".  We find the matching
    xanmod tag (e.g. "7.1.2-xanmod1") and clone from github/linux.
    The xanmod-7.1 patch series is recorded on ctx so the patch phase uses it.
    """
    xanmod_repo = _GITHUB_REPOS["xanmod"]
    if not xanmod_repo.is_dir():
        log.err("github/linux (xanmod mirror) not found — clone it first")
        raise SystemExit(2)

    # Find the best matching xanmod tag for target
    run(["git", "-C", str(xanmod_repo), "fetch", "--tags", "--quiet", "origin"],
        check=False, force=True)
    r = run(["git", "-C", str(xanmod_repo), "tag", "--sort=-version:refname"],
            check=False, force=True)
    tag = None
    for t in (r.stdout or "").splitlines():
        t = t.strip()
        m = re.match(r"^v?(\d+\.\d+\.\d+)", t)
        if m and m.group(1) == target:
            tag = t
            break
    if tag is None:
        # Fallback: use the target verbatim
        tag = target

    ver = target
    build_dir = ctx.build_dir
    build_dir.mkdir(parents=True, exist_ok=True)
    dest = build_dir / f"linux-{ver}"

    if dest.exists():
        if confirm(f"existing xanmod checkout {dest.name} present — remove and re-clone?", default=True):
            shutil.rmtree(dest)
        else:
            _reuse_baseline(ctx, dest)
            ctx.patch_series_dir = ctx.repo_root / "patches" / "xanmod-7.1"
            ctx.patch_vendor_lock = ctx.repo_root / "patches" / "VENDOR-xanmod-7.1.lock"
            _retarget_running_kernel(ctx, dest, "xanmod")
            ctx.source_dir = dest
            return dest

    log.info(f"cloning xanmod {tag} from local mirror")
    r = run(
        ["git", "clone", "--local", "--no-hardlinks", "--branch", tag,
         str(xanmod_repo), str(dest)],
        check=False, capture=False,
    )
    if not r.ok():
        raise RuntimeError(f"git clone of xanmod @ {tag} failed")

    # Record which patch series the patch phase should use
    ctx.patch_series_dir = ctx.repo_root / "patches" / "xanmod-7.1"
    ctx.patch_vendor_lock = ctx.repo_root / "patches" / "VENDOR-xanmod-7.1.lock"

    mk_ver = _kernel_ver_from_makefile(dest) or ver

    # Seed baseline config from Canonical's mainline PPA (same as kernel-org path)
    synthetic_kver = _fetch_mainline_baseline_config(f"v{ver}", ctx.repo_root)
    if synthetic_kver is None:
        log.warn("proceeding without a Canonical baseline config")
        synthetic_kver = f"{mk_ver}-generic"
    else:
        synthetic_kver = f"{mk_ver}-generic"

    if ctx.profile is not None:
        ctx.profile.running_kernel = synthetic_kver
        log.info(f"targeting xanmod build as {synthetic_kver}")

    _seed_baseline(ctx, dest)
    ctx.source_dir = dest
    return dest


def _ensure_stable_tag_in_mirror(mirror: Path, tag: str) -> Path | None:
    """Ensure `tag` (e.g. 'v7.1.2') is present in the local kernelorg mirror.

    github/kernelorg/linux is typically torvalds/linux (mainline) which lacks
    stable point-release tags.  When the tag is absent, this function adds a
    'stable' remote pointing to Greg KH's GitHub mirror of linux-stable and
    fetches just that tag — updating the local mirror in place rather than
    pulling from the internet at clone time.

    Returns the mirror path on success, None if we could not obtain the tag.
    """
    tag_check = run(
        ["git", "-C", str(mirror), "rev-parse", "--verify", f"refs/tags/{tag}"],
        check=False, force=True,
    )
    if tag_check.ok():
        return mirror  # already present

    log.info(f"local kernelorg mirror missing {tag} (mainline tree) — fetching from stable remote")

    # Add 'stable' remote if not already configured
    remotes_r = run(["git", "-C", str(mirror), "remote"], check=False, force=True)
    remotes = (remotes_r.stdout or "").splitlines()
    if "stable" not in remotes:
        log.info(f"adding 'stable' remote → {KERNEL_STABLE_MIRROR}")
        run(["git", "-C", str(mirror), "remote", "add", "stable", KERNEL_STABLE_MIRROR],
            check=False, force=True)

    # Fetch just the specific tag (no full history needed for git am --3way)
    log.info(f"fetching {tag} from stable remote into local mirror…")
    r = run(
        ["git", "-C", str(mirror), "fetch", "--depth", "1", "stable", f"refs/tags/{tag}:refs/tags/{tag}"],
        check=False, capture=False,
    )
    if not r.ok():
        log.warn(f"stable fetch of {tag} failed — will fall back to internet clone")
        return None

    log.ok(f"fetched {tag} into local mirror at {mirror}")
    return mirror


def _fetch_kernel_org(ctx, target: str) -> Path:
    """Opt-in source path: shallow-clone kernel.org's stable tree at the
    given tag and seed configs/base/ from Canonical's matching mainline
    build, instead of `apt source` against the running Ubuntu kernel.

    Unlike the ubuntu path, our 16-patch series and Kconfig fragments have
    never been validated against this tree — patch/configure will surface
    any incompatibility loudly (git am failures, floor-validation failures),
    not silently at boot.
    """
    tag = target if target.startswith("v") else f"v{target}"
    ver = tag[1:]
    build_dir = ctx.build_dir
    build_dir.mkdir(parents=True, exist_ok=True)
    dest = build_dir / f"linux-{ver}"

    if dest.exists():
        if confirm(f"existing kernel.org checkout {dest.name} present — remove and re-clone?", default=True):
            shutil.rmtree(dest)
        else:
            _reuse_baseline(ctx, dest)
            _retarget_running_kernel(ctx, dest, "kernel-org")
            ctx.source_dir = dest
            return dest

    # Preflight: validate the target tag is actually built on kernel.ubuntu.com/mainline.
    # The baseline .config recovery depends on it — failing here is much cheaper
    # than discovering the gap after a 250 MB clone. A same-series fallback (e.g.
    # v7.1.5 for a v7.1.6 target) covers most misses silently; only prompt when
    # nothing in the whole series is built.
    if not ctx.dry_run:
        if not _mainline_has_amd64_build(tag):
            fallback = _nearest_mainline_tag_in_series(tag)
            if fallback:
                log.info(
                    f"kernel.ubuntu.com/mainline has no amd64 build for {tag} — "
                    f"will use {fallback}'s Canonical baseline config instead (same series)"
                )
            else:
                latest = _latest_mainline_stable()
                latest_hint = f"; newest built is {latest}" if latest else ""
                log.warn(
                    f"kernel.ubuntu.com/mainline has no amd64 build for {tag}{latest_hint}, "
                    "and no other build in this series either"
                )
                log.warn(
                    "without a Canonical baseline .config, configure will have no "
                    "same-series snapshot to fall back to — you'll need to pre-stage "
                    "one manually (cp /boot/config-<ver> configs/base/)"
                )
                if not confirm(f"continue anyway with {tag}?", default=False):
                    raise SystemExit(0)

    local_mirror = _GITHUB_REPOS.get("kernelorg")
    if local_mirror and local_mirror.is_dir():
        local_mirror = _ensure_stable_tag_in_mirror(local_mirror, tag)

    if local_mirror and local_mirror.is_dir():
        # `git clone --local` refuses its fast hardlink/copy path against a
        # shallow source repo (git silently falls back to a full smart-protocol
        # pack negotiation even though both sides are on the same filesystem —
        # observed taking 5-6 minutes for the kernel tree and prone to being
        # killed mid-transfer, corrupting the partial clone). `git archive` reads
        # the tree directly with no pack negotiation, so it's a few seconds
        # instead. `_seed_baseline` below creates the git repo dest actually
        # needs (a single pristine baseline commit) — the archive export
        # itself never needs to be a git clone with history.
        log.info(f"exporting {tag} from local github/kernelorg mirror (no network needed)")
        dest.mkdir(parents=True, exist_ok=True)
        r = run(
            f"git -C {shlex.quote(str(local_mirror))} archive {shlex.quote(tag)} "
            f"| tar -x -C {shlex.quote(str(dest))}",
            check=False, capture=False,
        )
        if not r.ok():
            raise RuntimeError(f"git archive of kernel source @ {tag} from local mirror failed")
    else:
        log.info(f"no local kernelorg mirror — shallow-cloning from kernel.org stable")
        r = run(
            ["git", "clone", "--depth", "1", "--branch", tag, KERNEL_ORG_GIT, str(dest)],
            check=False, capture=False,
        )
        if not r.ok():
            raise RuntimeError(f"git clone of kernel source @ {tag} failed")

    # Read the actual kernel version from the cloned Makefile — this is the
    # authoritative source regardless of whether the mainline PPA has this version.
    mk_ver = _kernel_ver_from_makefile(dest)
    if mk_ver:
        log.info(f"kernel Makefile reports version {mk_ver}")
    else:
        mk_ver = ver  # fallback to the tag we cloned

    synthetic_kver = _fetch_mainline_baseline_config(tag, ctx.repo_root)
    if synthetic_kver is None:
        log.warn(
            "proceeding without a Canonical baseline config — `configure` will "
            "look for the newest same-series snapshot already in configs/base/, "
            "or prompt before using a cross-series one"
        )
        # Use the Makefile version so downstream phases get the right uname -r
        # (e.g. "7.1.2-generic" → hyphaed_uname_r → "7.1.2-hyphaed")
        synthetic_kver = f"{mk_ver}-generic"
    else:
        # Replace the ABI prefix with the exact Makefile version so we don't
        # accidentally embed the PPA's ABI in our version string.
        kver_base = synthetic_kver.split("-")[0]
        if kver_base != mk_ver:
            log.info(f"PPA kver base {kver_base} → overriding with Makefile {mk_ver}")
            synthetic_kver = f"{mk_ver}-generic"

    # Mirror the `rebase` command's established pattern: downstream phases
    # (patch/configure/build/install/postinstall) all key off
    # profile.running_kernel uniformly, so retargeting it here is enough to
    # make the whole pipeline consistent without touching those phases.
    if ctx.profile is not None:
        ctx.profile.running_kernel = synthetic_kver
        log.info(f"targeting kernel-org build as {synthetic_kver}")

    _seed_baseline(ctx, dest)
    ctx.source_dir = dest
    return dest


def _kernel_ver_from_makefile(source_dir: Path) -> str | None:
    """Read VERSION.PATCHLEVEL.SUBLEVEL from the kernel Makefile.

    Returns e.g. "7.1.2" or None if the file can't be parsed.
    Used to derive the correct synthetic_kver for kernel-org / xanmod modes
    without depending on whether kernel.ubuntu.com/mainline has that version.
    """
    mk = source_dir / "Makefile"
    if not mk.exists():
        return None
    ver = pl = sl = ""
    for line in mk.read_text(errors="replace").splitlines()[:30]:
        if line.startswith("VERSION =") or line.startswith("VERSION="):
            ver = line.split("=", 1)[1].strip()
        elif line.startswith("PATCHLEVEL =") or line.startswith("PATCHLEVEL="):
            pl = line.split("=", 1)[1].strip()
        elif line.startswith("SUBLEVEL =") or line.startswith("SUBLEVEL="):
            sl = line.split("=", 1)[1].strip()
        if ver and pl and sl:
            break
    if ver and pl and sl:
        return f"{ver}.{pl}.{sl}"
    if ver and pl:
        return f"{ver}.{pl}"
    return None


def _baseline_tag_for(dest: Path) -> str:
    """Deterministic, tree-derived baseline tag, e.g. 'base-7.1.3'.

    Keyed on the extracted Makefile version (authoritative for the tree
    actually being built), NOT on profile.running_kernel — so the source and
    patch phases always agree on one tag regardless of which kernel happens
    to be running when the wizard is invoked.
    """
    ver = _kernel_ver_from_makefile(dest) or dest.name.replace("linux-", "")
    return f"base-{ver}"


def _seed_baseline(ctx, dest: Path) -> str:
    """Seed a pristine baseline in a freshly-extracted tree and record it on ctx."""
    tag = _baseline_tag_for(dest)
    gitops.seed_baseline(dest, tag)
    ctx.baseline_tag = tag
    return tag


def _existing_baseline_config_kver(ctx, mk_ver: str) -> str | None:
    """Find the running_kernel string an earlier fresh clone already
    established for this Makefile version, by matching configs/base/
    snapshot filenames — e.g. 'config-7.1.3-070103-generic' -> the ABI-
    suffixed '7.1.3-070103-generic' Canonical's mainline PPA assigned it.

    configure.py's _snapshot_base_config does an EXACT filename match
    against profile.running_kernel, so a reused checkout must resolve to
    the SAME string the fresh-clone path already fetched and snapshotted —
    not a freshly-guessed bare '{mk_ver}-generic' that matches no file on
    disk (that guess is only correct when nothing has been snapshotted yet).
    """
    base_dir = ctx.repo_root / "configs" / "base"
    matches = sorted(base_dir.glob(f"config-{mk_ver}-*-generic"))
    if matches:
        return matches[-1].name.removeprefix("config-")
    bare = base_dir / f"config-{mk_ver}-generic"
    if bare.exists():
        return bare.name.removeprefix("config-")
    return None


def _retarget_running_kernel(ctx, dest: Path, reason: str) -> None:
    """Point ctx.profile.running_kernel at the tree actually checked out at
    `dest`, with no network calls.

    kernel-org/xanmod modes are the sole authority for profile.running_kernel
    (see the "fresh clone" code paths below) — every exit point of the
    fetch functions must (re-)establish it, or a later `detect` re-run (which
    always happens in a bare/default full pipeline invocation) silently
    reverts it to the live machine's real kernel, and downstream version
    strings quietly become Ubuntu-ABI-shaped even though the tree being
    built is kernel-org/xanmod.
    """
    if ctx.profile is None:
        return
    mk_ver = _kernel_ver_from_makefile(dest) or dest.name.replace("linux-", "")
    synthetic_kver = _existing_baseline_config_kver(ctx, mk_ver) or f"{mk_ver}-generic"
    ctx.profile.running_kernel = synthetic_kver
    log.info(f"targeting {reason} build as {synthetic_kver} (reusing existing checkout)")


def _reuse_baseline(ctx, dest: Path) -> None:
    """Keep-existing path: don't reseed (the tree may already be patched —
    reseeding from it would mint a contaminated baseline, exactly the bug
    this whole mechanism exists to prevent). Just verify the pristine
    baseline tag is present and record it; refuse if it's missing, since
    that means the tree predates this fix or is otherwise untrusted.
    """
    tag = _baseline_tag_for(dest)
    if not gitops.baseline_tag_present(dest, tag):
        log.err(
            f"existing tree {dest.name} has no pristine baseline tag '{tag}' — "
            "it predates the baseline fix or is contaminated. Re-run and choose "
            "to remove/re-clone so a clean baseline can be seeded."
        )
        raise SystemExit(2)
    ctx.baseline_tag = tag
    log.info(f"reusing pristine baseline {tag} in existing tree {dest.name}")


def _deb_src_enabled() -> bool:
    """Same check as detect._check_deb_src — needed here too because detect
    may have only warned, and user may want us to auto-enable."""
    r = run(["apt-cache", "policy"], check=False, force=True)
    if "deb-src" in r.stdout.lower() or "/source/" in r.stdout.lower():
        return True
    for f in list(Path("/etc/apt").glob("sources.list*")) + list(Path("/etc/apt/sources.list.d").glob("*.sources")) + list(Path("/etc/apt/sources.list.d").glob("*.list")):
        try:
            text = f.read_text()
        except OSError:
            continue
        if "deb-src" in text or re.search(r"^Types:.*\bdeb-src\b", text, re.MULTILINE):
            return True
    return False


def _enable_deb_src(ctx) -> bool:
    """Try to enable deb-src in /etc/apt/sources.list. Returns True on success."""
    src = Path("/etc/apt/sources.list")
    if not src.exists():
        log.warn("/etc/apt/sources.list missing; can't auto-enable (deb822 format used)")
        return False
    if not confirm("enable deb-src in /etc/apt/sources.list and apt-update?", default=True):
        return False
    # Uncomment commented `# deb-src` lines
    run_sudo(["sed", "-i", "/^# *deb-src /s/^# *//", str(src)])
    run_sudo(["apt", "update"])
    return _deb_src_enabled()


def _running_base_pkg(kver: str) -> str:
    # e.g. "7.0.0-15-generic" -> "linux-image-7.0.0-15-generic"
    return f"linux-image-{kver}"


def _binary_to_source_pkg(binary_pkg: str) -> str:
    """Map an apt binary package to its source package.

    On Ubuntu, linux-image-*-generic is shipped by BOTH `linux` (the real
    kernel tree, also ships linux-source-*) AND `linux-signed` (a tiny
    signing wrapper).  `apt source` picks linux-signed by default, which
    produces a useless debian/download-signed directory instead of the tree.
    Prefer the source stanza that ships a linux-source-N binary.
    """
    r = run(["apt-cache", "showsrc", binary_pkg], check=False, force=True)
    if not r.ok():
        return binary_pkg
    stanzas: list[tuple[str, str]] = []
    cur_pkg, cur_bin = "", ""
    for line in r.stdout.splitlines():
        if line.startswith("Package:"):
            if cur_pkg:
                stanzas.append((cur_pkg, cur_bin))
            cur_pkg, cur_bin = line.split(":", 1)[1].strip(), ""
        elif line.startswith("Binary:"):
            cur_bin = line.split(":", 1)[1].strip()
    if cur_pkg:
        stanzas.append((cur_pkg, cur_bin))
    for pkg, bins in stanzas:
        if re.search(r"\blinux-source-\d", bins):
            return pkg
    for pkg, _ in stanzas:
        if not pkg.endswith("-signed"):
            return pkg
    return stanzas[0][0] if stanzas else binary_pkg


def _detect_latest_apt_source(ctx, target: str | None) -> str:
    """Return apt source package name for the kernel image we want to base on."""
    if target:
        bin_name = target if target.startswith("linux-image-") else f"linux-image-{target}"
        return _binary_to_source_pkg(bin_name)
    if ctx.profile and ctx.profile.running_kernel:
        return _binary_to_source_pkg(f"linux-image-{ctx.profile.running_kernel}")
    r = run(["uname", "-r"])
    return _binary_to_source_pkg(_running_base_pkg(r.stdout.strip() or "generic"))


def _apt_source(pkgname: str, build_dir: Path) -> Path:
    log.info(f"fetching apt source for {pkgname} into {build_dir}")
    build_dir.mkdir(parents=True, exist_ok=True)

    state = types.SimpleNamespace(task=None, total=None, applied=0)
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[cyan]dpkg-source[/cyan]"),
        TextColumn("{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=log.console,
        transient=False,
    )

    def _on_line(line: str) -> None:
        if state.task is None and _DPKG_EXTRACT_RE.match(line):
            state.task = progress.add_task("[dim]extracting…[/dim]", total=None)
            return
        if state.task is None:
            return
        m = _DPKG_APPLY_RE.match(line)
        if not m:
            return
        if state.total is None:
            series = next(build_dir.glob("linux-*/debian/patches/series"), None)
            if series is not None:
                try:
                    count = sum(
                        1 for ln in series.read_text(errors="replace").splitlines()
                        if ln.strip() and not ln.startswith("#")
                    )
                    if count > 0:
                        state.total = count
                        progress.update(state.task, total=count)
                except OSError:
                    pass
        state.applied += 1
        patch_name = m.group(1)
        # Trim to 48 chars so description column stays stable
        if len(patch_name) > 48:
            patch_name = "…" + patch_name[-47:]
        progress.update(
            state.task,
            completed=state.applied if state.total else None,
            description=f"applying [dim]{escape(patch_name)}[/dim]",
        )

    with progress:
        r = run(["apt", "source", pkgname], cwd=build_dir, check=False, on_line=_on_line)

    if not r.ok():
        raise RuntimeError(
            f"`apt source {pkgname}` failed. Make sure deb-src is enabled in /etc/apt/sources.list."
        )
    # Find the extracted source dir (linux-7.0.0/ or similar)
    dirs = [p for p in build_dir.iterdir() if p.is_dir() and re.match(r"linux-[0-9]", p.name)]
    if not dirs:
        extracted = sorted(p.name for p in build_dir.iterdir() if p.is_dir())
        tail = "\n".join((r.stdout or "").splitlines()[-5:])
        raise RuntimeError(
            f"apt source {pkgname} produced no linux-N.M.* directory in {build_dir}. "
            f"Got dirs: {extracted!r}.\napt tail:\n{tail}"
        )
    return sorted(dirs)[-1]


def _adopt_pre_extracted(build_dir: Path, ver: str) -> Path | None:
    """Reuse a kernel source tree already extracted adjacent to build_dir.

    Handles the case where the user ran `sudo apt source linux` manually in
    the repo root before invoking the wizard — leaving a root-owned linux-N.M.P/
    directory there.  Chowns it to the invoking user and moves it into build_dir.
    """
    candidate = build_dir.parent / f"linux-{ver}"
    if not (candidate.is_dir() and (candidate / "Makefile").exists()):
        return None
    if not confirm(
        f"adopt existing source tree at {candidate}? (will chown + move into {build_dir})",
        default=True,
    ):
        return None
    # If invoked via sudo, chown to the real invoking user, not root
    sudo_uid = os.environ.get("SUDO_UID")
    if sudo_uid:
        user_entry = pwd.getpwuid(int(sudo_uid))
        user = user_entry.pw_name
    else:
        user = pwd.getpwuid(os.getuid()).pw_name
    run_sudo(["chown", "-R", f"{user}:{user}", str(candidate)])
    dest = build_dir / candidate.name
    shutil.move(str(candidate), str(dest))
    log.ok(f"adopted pre-extracted source tree at {dest}")
    return dest


def _linux_source_pkg(build_dir: Path) -> Path | None:
    """Fallback: use /usr/src/linux-source-*.tar.bz2 if present."""
    candidates = sorted(Path("/usr/src").glob("linux-source-*.tar*"))
    if not candidates:
        return None
    src_tar = candidates[-1]
    log.info(f"extracting {src_tar} -> {build_dir}")
    build_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(src_tar) as tf:
        tf.extractall(build_dir)
    dirs = [p for p in build_dir.iterdir() if p.is_dir() and p.name.startswith("linux-source")]
    if not dirs:
        return None
    return sorted(dirs)[-1]


def run_phase(ctx) -> Path:
    prior_source_dir = ctx.persisted.source_dir
    result = _run_phase_impl(ctx)
    new_source_dir = str(ctx.source_dir) if ctx.source_dir else None
    if prior_source_dir and new_source_dir and prior_source_dir != new_source_dir:
        # The resolved target changed since the last invocation (e.g. a
        # different --target, or a re-pick in the interactive version
        # selector). phases_completed/built_debs/packaged_debs from the
        # prior source_dir are stale for patch onward — without this, the
        # dependency resolver in cli.py would treat build/package/install
        # as already satisfied and silently reuse artefacts built against
        # the old source tree.
        from . import ORDER
        log.info(
            f"source target changed ({Path(prior_source_dir).name} -> "
            f"{Path(new_source_dir).name}) — invalidating stale patch..postinstall state"
        )
        ctx.persisted.invalidate_from("patch", ORDER)
    return result


def _run_phase_impl(ctx) -> Path:
    log.banner("Phase 2/9 — Fetch kernel source")
    target = getattr(ctx.args, "target", None)  # e.g. "7.0.0-16-generic" or None

    source_mode = getattr(ctx, "source_mode", "ubuntu")
    if source_mode in ("kernel-org", "xanmod"):
        if not target:
            # No explicit --target: fetch latest tags from local mirrors and let user pick
            picked = _pick_kernel_version_interactive(n=5)
            if picked is None:
                log.err("--source-mode kernel-org/xanmod requires --target (e.g. --target 7.1.2) "
                        "or local github mirrors in github/")
                raise SystemExit(2)
            base_ver, repo_name = picked
            target = base_ver
            # If the user picked an xanmod tag, auto-select xanmod mode
            if repo_name == "xanmod" and source_mode != "xanmod":
                log.info(f"auto-switching source mode to xanmod (picked from xanmod repo)")
                ctx.source_mode = source_mode = "xanmod"
            log.ok(f"selected kernel version: {target} (from {repo_name})")

        if source_mode == "xanmod":
            return _fetch_xanmod(ctx, target)

        if ctx.dry_run:
            log.info(f"(dry-run) would clone kernel.org @ v{target.lstrip('v')} "
                      f"and seed configs/base/ from Canonical's matching mainline build")
            stub = ctx.build_dir / f"linux-{target.lstrip('v')}"
            ctx.source_dir = stub
            return stub
        return _fetch_kernel_org(ctx, target)

    pkgname = _detect_latest_apt_source(ctx, target)

    build_dir = ctx.build_dir
    build_dir.mkdir(parents=True, exist_ok=True)

    if not ctx.dry_run and not _deb_src_enabled():
        log.warn("deb-src not enabled — `apt source` will fail")
        if not _enable_deb_src(ctx):
            log.err(f"can't continue without deb-src.\n  {DEB_SRC_HELPER_LINE}")
            raise SystemExit(2)

    if ctx.dry_run:
        # Don't run apt source; stamp a plausible directory name so later
        # phases (configure/build) can still report what they'd do.
        kver = ctx.profile.running_kernel.split("-")[0] if ctx.profile else "X.Y.Z"
        stub = build_dir / f"linux-{kver}"
        log.info(f"(dry-run) would fetch apt source for {pkgname} into {build_dir}")
        log.info(f"(dry-run) would extract source tree at {stub}")
        ctx.source_dir = stub
        return stub

    # Clean prior extraction if user agrees
    for old in list(build_dir.glob("linux-*")):
        if old.is_dir():
            if confirm(f"existing build tree {old.name} present — remove?", default=True):
                shutil.rmtree(old)

    # Offer to reuse a pre-extracted tree adjacent to build_dir (e.g. from a
    # prior manual `sudo apt source linux` in the project root).
    ver = ctx.profile.running_kernel.split("-")[0] if ctx.profile else ""
    src_dir: Path | None = _adopt_pre_extracted(build_dir, ver) if ver else None

    if src_dir is None:
        try:
            src_dir = _apt_source(pkgname, build_dir)
        except RuntimeError as e:
            log.warn(str(e))
            log.info("trying fallback: /usr/src/linux-source-*.tar.*")
            src_dir = _linux_source_pkg(build_dir)

    if src_dir is None:
        log.err("no kernel source available — install with: sudo apt install dpkg-dev linux-source")
        raise SystemExit(2)

    log.ok(f"source tree ready at {src_dir}")
    _seed_baseline(ctx, src_dir)
    ctx.source_dir = src_dir
    return src_dir
