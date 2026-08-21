"""Invariants for patches/staticcheck.py.

The two that matter are the ones that make the output worth reading:

  1. a missing tool reports NOT RUN, never zero findings. The security phase
     already learned this the expensive way , a kernel-hardening-checker that
     could not be found returned (0, 0) and printed "0 FAIL" for months.
  2. findings are scoped to the lines the series changes. An unscoped sparse
     run over kernel/sched/ on stock 7.1.9 emits 40+ address-space warnings
     that upstream has carried for years; scoping cut a real run to 8, all of
     them attributable to patches this repo carries.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from patches import staticcheck as sc  # noqa: E402


PATCH = """From 0000 Mon Sep 17 00:00:00 2001
Subject: [PATCH] test

---
 drivers/dma-buf/dma-buf.c | 3 +++
 include/linux/dma-buf.h   | 1 +
 2 files changed, 4 insertions(+)

diff --git a/drivers/dma-buf/dma-buf.c b/drivers/dma-buf/dma-buf.c
--- a/drivers/dma-buf/dma-buf.c
+++ b/drivers/dma-buf/dma-buf.c
@@ -100,6 +100,9 @@ static void ctx(void)
 	keep_one();
 	keep_two();
 	keep_three();
+	added_a();
+	added_b();
+	added_c();
 	tail_one();
 	tail_two();
diff --git a/include/linux/dma-buf.h b/include/linux/dma-buf.h
--- a/include/linux/dma-buf.h
+++ b/include/linux/dma-buf.h
@@ -50,3 +50,4 @@
 struct thing;
+int added_decl(void);
"""


@pytest.fixture()
def series(tmp_path: Path) -> Path:
    (tmp_path / "0001-test.patch").write_text(PATCH)
    s = tmp_path / "series"
    s.write_text("# a comment\n\n0001-test.patch\n")
    return s


def test_files_touched_returns_only_c_files(series: Path):
    """Headers are real changes but there is nothing for sparse to compile."""
    assert sc.files_touched(series) == ["drivers/dma-buf/dma-buf.c"]


def test_files_touched_ignores_comments_and_blanks(tmp_path: Path):
    s = tmp_path / "series"
    s.write_text("# only comments\n\n   \n")
    assert sc.files_touched(s) == []


def test_lines_touched_covers_the_added_lines(series: Path):
    got = sc.lines_touched(series)["drivers/dma-buf/dma-buf.c"]
    # Hunk starts at post-image line 100, three context lines, then the adds.
    assert {103, 104, 105} <= got


def test_lines_touched_does_not_cover_the_whole_file(series: Path):
    got = sc.lines_touched(series)["drivers/dma-buf/dma-buf.c"]
    assert 1 not in got and 500 not in got


def test_lines_touched_is_generous_around_the_change(series: Path):
    """Sparse often reports at a call site adjacent to the changed line."""
    got = sc.lines_touched(series)["drivers/dma-buf/dma-buf.c"]
    assert 100 in got and 108 in got


def test_missing_tool_reports_not_run_not_clean(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(sc, "find_tool", lambda name: None)
    for runner in (sc.run_sparse, sc.run_smatch, sc.run_cocci):
        r = runner(tmp_path, ["drivers/dma-buf/dma-buf.c"], 1)
        assert r.ran is False, f"{r.tool} claimed to run without a binary"
        assert r.findings == []
        assert r.reason, f"{r.tool} gave no reason for not running"


def test_parser_extracts_file_line_and_message():
    text = (
        "  CHECK   drivers/dma-buf/udmabuf.c\n"
        "drivers/dma-buf/udmabuf.c:132:16: warning: dereference of noderef expression\n"
        "kernel/sched/bore.c:346:9: warning: incorrect type in argument 3\n"
        "unrelated chatter that is not a finding\n"
    )
    only = {"drivers/dma-buf/udmabuf.c"}
    got = sc._parse("sparse", text, only)
    assert len(got) == 1
    assert got[0].file == "drivers/dma-buf/udmabuf.c"
    assert got[0].line == 132
    assert "noderef" in got[0].text


def test_parser_honours_the_file_filter():
    text = "kernel/sched/core.c:905:1: warning: dereference of noderef expression\n"
    assert sc._parse("sparse", text, {"drivers/dma-buf/dma-buf.c"}) == []
    assert len(sc._parse("sparse", text, None)) == 1


def test_real_series_touches_the_dma_buf_files():
    """Guards the series against silently losing our own patches."""
    s = ROOT / "patches" / "kernel-org-7.1" / "series"
    if not s.is_file():
        pytest.skip("series not present")
    files = sc.files_touched(s)
    assert "drivers/dma-buf/dma-buf.c" in files
    assert "drivers/dma-buf/udmabuf.c" in files


def test_cocci_parser_matches_real_coccinelle_output():
    """Coccinelle does not use the sparse shape, and `_WARN` matches none of it.

    Real 2026-08-21 output: uppercase kind, a column RANGE, and often no colon
    after the kind. Parsed with the sparse pattern the tool ran and reported
    zero, which reads exactly like a clean tree. Both spellings appear in
    scripts/coccinelle/, so both are pinned.
    """
    text = (
        "drivers/dma-buf/udmabuf.c:557:7-8: WARNING opportunity for min()\n"
        "kernel/exit.c:534:2-9: ERROR: invalid reference to the index variable\n"
        "drivers/nvme/host/core.c:1398:1-9: WARNING: Consider using %pe\n"
        "make[1]: Entering directory '/x'\n"
    )
    got = sc._parse("coccinelle", text, None)
    assert len(got) == 3, [f.text for f in got]
    assert (got[0].file, got[0].line) == ("drivers/dma-buf/udmabuf.c", 557)
    assert "min()" in got[0].text
    assert got[1].line == 534 and "ERROR" in got[1].text

    # The sparse pattern must still reject it, so a future refactor that
    # collapses the two patterns fails here rather than silently reporting 0.
    assert sc._parse("sparse", text, None) == []


def test_cocci_uses_absolute_paths_because_it_runs_with_cwd_in_the_tree():
    """spatch runs with cwd=tree, so a relative tree path resolves against
    itself (build/linux-7.1.9/build/linux-7.1.9/...) and every script is
    'No such file or directory' , which also reports zero findings."""
    inc = sc._cocci_include(Path("build/linux-7.1.9"))
    assert inc, "no include flags produced"
    for flag in inc:
        if flag.startswith("-") or flag == "--include":
            continue
        assert flag.startswith("/"), f"relative path would resolve twice: {flag}"
