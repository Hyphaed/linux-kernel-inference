from pathlib import Path
from hyphaed.gitops import check_mutual_exclusion, patch_paths, patch_marker


def _write_patch(tmp_path: Path, name: str, headers: list[str]) -> Path:
    body = "\n".join(headers) + "\n\n--- a/foo\n+++ b/foo\n@@ -0,0 +1 @@\n+x\n"
    p = tmp_path / name
    p.write_text(body)
    return p


def test_patch_paths_extracts_forward_port_notes(tmp_path):
    p = _write_patch(tmp_path, "0001-foo.patch", [
        "From: x",
        "Subject: foo",
        "Forward-Port-Notes: kernel/sched/fair.c, kernel/sched/core.c",
    ])
    assert patch_paths(p) == ["kernel/sched/fair.c", "kernel/sched/core.c"]


def test_patch_marker_extracts(tmp_path):
    p = _write_patch(tmp_path, "0001-bore.patch", [
        "From: x", "Subject: bore", "Conflict-Marker: bore",
    ])
    assert patch_marker(p) == "bore"


def test_mutual_exclusion_detects_bore_vs_prjc(tmp_path):
    a = _write_patch(tmp_path, "0001-bore.patch", ["From: x", "Conflict-Marker: bore"])
    b = _write_patch(tmp_path, "0002-prjc.patch", ["From: x", "Conflict-Marker: prjc"])
    conflicts = check_mutual_exclusion([a, b])
    assert len(conflicts) == 1
    assert "bore" in conflicts[0] and "prjc" in conflicts[0]


def test_mutual_exclusion_passes_compatible(tmp_path):
    a = _write_patch(tmp_path, "0001-bore.patch", ["Conflict-Marker: bore"])
    b = _write_patch(tmp_path, "0002-bbr3.patch", ["Conflict-Marker: bbr3"])
    assert check_mutual_exclusion([a, b]) == []


def test_preflight_diff_returns_empty_when_tags_missing(tmp_path):
    """preflight_diff should return [] gracefully when the repo has no tags yet."""
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), check=True)
    patch = _write_patch(tmp_path, "0001-bore.patch", [
        "From: x",
        "Subject: bore",
        "Forward-Port-Notes: kernel/sched/fair.c",
    ])
    from hyphaed.gitops import preflight_diff
    flagged = preflight_diff(repo, "base-7.0.0-15.15", "base-7.0.0-16.16", [patch])
    assert flagged == []


def test_patch_paths_returns_empty_when_no_forward_port_notes(tmp_path):
    p = _write_patch(tmp_path, "0001-bare.patch", ["From: x", "Subject: bare"])
    assert patch_paths(p) == []


def test_seed_baseline_then_reset_restores_pristine_tree(tmp_path):
    """seed_baseline must capture the tree as-is; reset_to_baseline must
    restore exactly that state even after further commits land on top —
    this is the core invariant the contaminated-baseline bug violated."""
    from hyphaed.gitops import seed_baseline, reset_to_baseline

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "sched.h").write_text("pristine\n")

    seed_baseline(repo, "base-7.1.3")

    # Simulate a patch landing on top of the pristine baseline.
    (repo / "sched.h").write_text("patched\n")
    (repo / "bore.c").write_text("new file\n")
    import subprocess
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "patch"], cwd=str(repo), check=True)

    reset_to_baseline(repo, "base-7.1.3")

    assert (repo / "sched.h").read_text() == "pristine\n"
    assert not (repo / "bore.c").exists()


def test_seed_baseline_removes_stale_git_dir(tmp_path):
    """A pre-existing .git (stale clone history, or a contaminated baseline
    from an earlier buggy run) must not survive re-seeding."""
    from hyphaed.gitops import seed_baseline, baseline_tag_present
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), check=True)
    (repo / "foo").write_text("old contaminated content\n")
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "old baseline"], cwd=str(repo), check=True)
    subprocess.run(["git", "tag", "old-tag"], cwd=str(repo), check=True)

    seed_baseline(repo, "base-7.1.3")

    assert not baseline_tag_present(repo, "old-tag")
    assert baseline_tag_present(repo, "base-7.1.3")


def test_reset_to_baseline_refuses_when_tag_missing(tmp_path):
    """Must never mint a baseline from the working tree — hard-fail instead."""
    import subprocess
    import pytest
    from hyphaed.gitops import reset_to_baseline

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), check=True)
    (repo / "foo").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "some commit"], cwd=str(repo), check=True)

    with pytest.raises(SystemExit):
        reset_to_baseline(repo, "base-7.1.3")
