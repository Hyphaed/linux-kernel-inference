"""_submodule_paths: .gitmodules entries without a gitlink (upstream Proton's Vulkan-Loader) are skipped."""
import subprocess
from pathlib import Path

from hyphaed.phases import proton


def _git(cwd: Path, *a: str) -> None:
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


def test_orphan_gitmodules_entry_is_not_a_submodule(tmp_path: Path):
    _git(tmp_path, "init", "-q")
    # a real gitlink, added straight to the index (no clone needed)
    _git(tmp_path, "update-index", "--add", "--cacheinfo",
         "160000,31aa7f634b052d87ede4664053e85f3f4d1d50d3,Vulkan-Headers")
    (tmp_path / ".gitmodules").write_text(
        '[submodule "Vulkan-Headers"]\n\tpath = Vulkan-Headers\n\turl = https://example.invalid/h\n'
        '[submodule "Vulkan-Loader"]\n\tpath = Vulkan-Loader\n\turl = https://example.invalid/l\n')
    mods, orphans = proton._submodule_paths(tmp_path)
    assert mods == ["Vulkan-Headers"]
    assert orphans == ["Vulkan-Loader"]
