import subprocess
import sys
from pathlib import Path

# Add patches/ to path so we can import fetch
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "patches"))
import fetch as patches_fetch


def test_candidates_swaps_branches():
    url = "https://example.com/repo/master/patches/0001-foo.patch"
    out = patches_fetch._candidates(url)
    # First entry is the original
    assert out[0] == url
    # And we've offered all fallback branches
    assert any("/main/" in u for u in out)
    assert any("/v7.0/" in u for u in out)
    assert any("/7.0/" in u for u in out)
    assert any("/linux-7.0.y/" in u for u in out)


def test_candidates_no_branch_marker_returns_only_original():
    url = "https://example.com/static/patch.patch"
    out = patches_fetch._candidates(url)
    assert out == [url]


def test_candidates_dedupes_when_already_target_branch():
    url = "https://example.com/repo/v7.0/foo.patch"
    out = patches_fetch._candidates(url)
    # /v7.0/ → /v7.0/ swap is filtered out
    assert sum(1 for u in out if u == url) == 1


# ── local-file kind ────────────────────────────────────────────────────────

def test_local_file_reads_bytes(tmp_path):
    content = b"From abc123\nSubject: test patch\n"
    patch = tmp_path / "test.patch"
    patch.write_bytes(content)
    result = patches_fetch._fetch_local_file(f"file://{patch}")
    assert result == content


def test_local_file_missing_raises(tmp_path):
    import pytest
    with pytest.raises(FileNotFoundError, match="local-file not found"):
        patches_fetch._fetch_local_file(f"file://{tmp_path}/nonexistent.patch")


def test_local_file_wrong_scheme_raises():
    import pytest
    with pytest.raises(ValueError, match="expected file://"):
        patches_fetch._fetch_local_file("https://example.com/foo.patch")


# ── git-commit kind ────────────────────────────────────────────────────────

def _make_git_repo(path: Path) -> str:
    """Init a throwaway git repo with one commit; return the commit SHA."""
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@test.com"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"],
                   check=True, capture_output=True)
    (path / "hello.txt").write_text("hello\n")
    subprocess.run(["git", "-C", str(path), "add", "hello.txt"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "Add hello"],
                   check=True, capture_output=True)
    r = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                       check=True, capture_output=True, text=True)
    return r.stdout.strip()


def test_git_commit_produces_bytes(tmp_path):
    sha = _make_git_repo(tmp_path)
    data = patches_fetch._fetch_git_commit(f"git+file://{tmp_path}@{sha}")
    assert isinstance(data, bytes)
    assert len(data) > 0
    # format-patch output starts with "From <sha>"
    assert data.startswith(b"From ")


def test_git_commit_is_deterministic(tmp_path):
    sha = _make_git_repo(tmp_path)
    url = f"git+file://{tmp_path}@{sha}"
    assert patches_fetch._fetch_git_commit(url) == patches_fetch._fetch_git_commit(url)


def test_git_commit_bogus_sha_raises(tmp_path):
    import pytest
    _make_git_repo(tmp_path)
    with pytest.raises(RuntimeError, match="not found in"):
        patches_fetch._fetch_git_commit(f"git+file://{tmp_path}@deadbeef1234")


def test_git_commit_missing_repo_raises(tmp_path):
    import pytest
    with pytest.raises(FileNotFoundError, match="git repo not found"):
        patches_fetch._fetch_git_commit(f"git+file://{tmp_path}/norepo@abc1234")


# ── fetch_entry dispatch ──────────────────────────────────────────────────

def test_fetch_entry_unknown_kind_raises():
    import pytest
    with pytest.raises(ValueError, match="unknown kind"):
        patches_fetch.fetch_entry("file:///anything", "bad-kind")
