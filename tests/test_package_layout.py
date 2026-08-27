"""out/debs/ must contain packages and nothing else.

`sudo dpkg -i *` from out/debs/ used to hit three "not a Debian format
archive" errors, because the package phase wrote manifest.json and
System.map-<pkgver> into the same directory as the .debs. dpkg refuses
them and carries on, so nothing breaks — but the errors are the loudest
thing on screen at the end of a kernel install, which is exactly the
wrong place to put harmless noise. The manifest and the map now go one
level up, into out/.
"""
import json
from pathlib import Path

from hyphaed.phases import package


class _FakeProfile:
    flavour = "hyphaed"

    def tag(self) -> str:
        return "rptr-nvbw-wl"


class _FakeCtx:
    dry_run = False
    kernel_pkgver = "7.1.10-generic.generichyphaed1+rptr-nvbw-wl"

    def __init__(self, repo_root: Path, source_dir: Path, built_debs: list[Path]):
        self.repo_root = repo_root
        self.source_dir = source_dir
        self.built_debs = built_debs
        self.packaged_debs: list[Path] = []
        self.profile = _FakeProfile()


def _run(tmp_path: Path) -> tuple[Path, _FakeCtx]:
    """A minimal but real package-phase run: two .debs to move, a System.map
    to copy, and no vmlinux (so the kxdb snapshot is skipped)."""
    repo_root = tmp_path / "repo"
    build = repo_root / "build"
    source = repo_root / "src"
    build.mkdir(parents=True)
    source.mkdir(parents=True)

    debs = []
    for name in (
        "linux-image-7.1.10-hyphaed_7.1.10-generic_amd64.deb",
        "linux-headers-7.1.10-hyphaed_7.1.10-generic_amd64.deb",
    ):
        d = build / name
        d.write_bytes(b"not really a deb, but a real file with a real sha256")
        debs.append(d)
    (source / "System.map").write_text("ffffffff81000000 T _stext\n")

    ctx = _FakeCtx(repo_root, source, debs)
    return package.run_phase(ctx), ctx


def test_out_debs_holds_only_packages(tmp_path):
    _run(tmp_path)
    debs_dir = tmp_path / "repo" / "out" / "debs"
    stray = [p.name for p in debs_dir.iterdir() if p.suffix != ".deb"]
    assert not stray, f"non-package files left in out/debs/: {stray}"
    assert len(list(debs_dir.glob("*.deb"))) == 2


def test_manifest_and_system_map_land_in_out(tmp_path):
    manifest_path, ctx = _run(tmp_path)
    out = tmp_path / "repo" / "out"

    assert manifest_path == out / "manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["kernel_pkgver"] == ctx.kernel_pkgver
    assert len(manifest["packages"]) == 2
    assert all(len(p["sha256"]) == 64 for p in manifest["packages"])

    maps = list(out.glob("System.map-*"))
    assert len(maps) == 1
    assert maps[0].name == f"System.map-{ctx.kernel_pkgver}"


def test_packaged_debs_still_points_into_out_debs(tmp_path):
    """The install phase reads ctx.packaged_debs; moving the artefacts must
    not move the packages themselves."""
    _, ctx = _run(tmp_path)
    debs_dir = tmp_path / "repo" / "out" / "debs"
    assert ctx.packaged_debs
    assert all(p.parent == debs_dir and p.exists() for p in ctx.packaged_debs)


def test_dry_run_with_no_debs_returns_the_out_level_manifest_path(tmp_path):
    """The early return has its own copy of the path and drifted from the
    real write once already."""
    repo_root = tmp_path / "repo"
    ctx = _FakeCtx(repo_root, repo_root / "src", [])
    ctx.dry_run = True
    assert package.run_phase(ctx) == repo_root / "out" / "manifest.json"
