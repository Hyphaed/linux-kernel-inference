#!/usr/bin/env python3
"""The wizard's version list: four defects that each, alone, emptied it.

`install_wizard.sh` sat forever on "Fetching latest kernel.org stable
releases…" on 2026-08-20. Four independent bugs were behind it, and fixing
any three would still have left an operator staring at a blank menu:

  1. `run(..., timeout=)` did not bound a capture-mode subprocess at all.
  2. `_git_stable_tags` fetched `origin` (torvalds/linux), which carries no
     stable point-release tags, then fetched again — 80 s total.
  3. `hyphaed list-versions --json` printed the banner to stdout, so the JSON
     could not be parsed.
  4. The wizard's own parser was a SyntaxError on Python 3.13.

These tests pin 1, 2 and 3. The fourth is bash and is covered by
`bash -n install_wizard.sh` plus the parse being exercised end to end.
"""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hyphaed.phases import source as S
from hyphaed.util.run import run


# ── 1. run() must actually bound a hanging capture-mode child ────────────────

def test_run_timeout_bounds_a_capture_mode_child():
    """The capture path drains stdout/stderr until EOF, which only happens
    when the child exits, and only THEN called proc.wait(timeout=...). The
    timeout was therefore applied after the wait was already over."""
    import time
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run(["sleep", "30"], check=False, force=True, timeout=2)
    assert time.monotonic() - t0 < 10, "timeout did not bound the child"


# ── 2. tag refresh: kernel.org, and only when there is something to get ──────

class _FakeRun:
    """Records every git invocation and answers from a scripted tag list."""

    def __init__(self, remote_tags, local_tags, remotes=("origin", "stable")):
        self.remote_tags, self.local_tags = remote_tags, local_tags
        self.remotes, self.calls = remotes, []

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        from hyphaed.util.run import RunResult
        if "remote" in cmd and "ls-remote" not in cmd:
            return RunResult(0, "\n".join(self.remotes), "", "")
        if "ls-remote" in cmd:
            body = "\n".join(f"deadbeef\trefs/tags/{t}" for t in self.remote_tags)
            return RunResult(0, body, "", "")
        if "tag" in cmd:
            return RunResult(0, "\n".join(self.local_tags), "", "")
        return RunResult(0, "", "", "")

    def fetched(self):
        return [c for c in self.calls if "fetch" in c]

    def probed(self):
        return [c for c in self.calls if "ls-remote" in c]


def test_refresh_probes_kernel_org_not_the_github_mirror(monkeypatch):
    """kernel.org is the source of truth. github.com/gregkh/linux is a mirror
    and can lag, so it must not be what answers 'what is the newest release'."""
    fake = _FakeRun(["v7.1.9"], ["v7.1.9"])
    monkeypatch.setattr(S, "run", fake)
    S._refresh_stable_tags(Path("/nonexistent"))
    assert fake.probed(), "no probe was issued at all"
    assert S.KERNEL_ORG_GIT in fake.probed()[0], \
        "the first probe must go to kernel.org, not to a named remote"


def test_refresh_never_fetches_origin(monkeypatch):
    """origin is torvalds/linux: vX.Y and -rc tags, no point releases. It was
    fetched every run and could not contribute one stable version."""
    fake = _FakeRun(["v7.1.10"], ["v7.1.9"])
    monkeypatch.setattr(S, "run", fake)
    S._refresh_stable_tags(Path("/nonexistent"))
    for call in fake.fetched():
        assert "origin" not in call, f"fetched origin: {call}"


def test_refresh_skips_the_fetch_when_nothing_is_newer(monkeypatch):
    """The expensive part is the fetch. A 0.25 s ls-remote decides whether it
    is worth running, and in the steady state it never is."""
    fake = _FakeRun(["v7.1.8", "v7.1.9"], ["v7.1.9", "v7.1.8"])
    monkeypatch.setattr(S, "run", fake)
    S._refresh_stable_tags(Path("/nonexistent"))
    assert fake.probed(), "should still probe"
    assert not fake.fetched(), "fetched despite having the newest tag already"


def test_refresh_does_fetch_when_upstream_is_ahead(monkeypatch):
    """The cheap probe must not become a reason to never update."""
    fake = _FakeRun(["v7.1.9", "v7.2"], ["v7.1.9"])
    monkeypatch.setattr(S, "run", fake)
    S._refresh_stable_tags(Path("/nonexistent"))
    assert fake.fetched(), "upstream had v7.2 and no fetch was issued"


def test_newest_stable_ignores_release_candidates():
    assert S._newest_stable(["v7.1.9", "v7.2-rc4", "v7.1.10"]) == (7, 1, 10)
    assert S._newest_stable(["v6.6.152", "v7.1.9"]) == (7, 1, 9)
    assert S._newest_stable(["not-a-tag", "v7.2-rc1"]) == ()


def test_a_dot_zero_release_is_a_real_release():
    """kernel.org tags a .0 release as vX.Y, not vX.Y.0 — v7.2 IS the tag.

    All three version regexes required three components, so the version list
    showed 7.1.9/7.1.8/... and silently omitted 7.2, which had been out since
    2026-08-16. The blind spot opened exactly at a major bump, which is the
    one moment the list has to be right.
    """
    assert S._newest_stable(["v7.1.9", "v7.2"]) == (7, 2, 0)
    assert S._newest_stable(["v7.2", "v7.2.1"]) == (7, 2, 1)
    assert S._newest_stable(["v7.2"]) > S._newest_stable(["v7.1.9"])


def test_the_release_list_includes_dot_zero_releases():
    """End to end against the real mirror: v7.2 must be offered."""
    import json
    repo = Path(__file__).resolve().parent.parent
    if not (repo / "github/kernelorg/linux").is_dir():
        pytest.skip("no local kernelorg mirror")
    proc = subprocess.run(
        [sys.executable, "-m", "hyphaed", "list-versions", "--count", "8", "--json"],
        cwd=repo, capture_output=True, text=True, timeout=300)
    versions = [r["version"] for r in json.loads(proc.stdout)["releases"]]
    two_component = [v for v in versions if v.count(".") == 1]
    assert two_component, f"no .0 release offered, got {versions}"


# ── 3. --json owns stdout ────────────────────────────────────────────────────

def test_list_versions_json_stdout_is_parseable():
    """The banner, the rule and every `$ git …` echo shared one console with
    the JSON payload. json.load() failed at char 0, and the wizard's parser
    swallowed the exception and showed an empty list."""
    import json
    repo = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [sys.executable, "-m", "hyphaed", "list-versions", "--count", "3", "--json"],
        cwd=repo, capture_output=True, text=True, timeout=300)
    data = json.loads(proc.stdout)          # must not raise
    assert data["releases"], "no releases returned"
    assert all({"version", "released"} <= set(r) for r in data["releases"])


def test_list_versions_json_still_reports_diagnostics_on_stderr():
    """Routing diagnostics away from stdout must not silence them."""
    repo = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [sys.executable, "-m", "hyphaed", "list-versions", "--count", "3", "--json"],
        cwd=repo, capture_output=True, text=True, timeout=300)
    assert "hyphaed" in proc.stderr, "diagnostics vanished instead of moving"
