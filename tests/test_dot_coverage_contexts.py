"""Tests for .coverage dynamic contexts ingestion (Coverage 阶段二).

Coverage:
  1. _context_to_testcase_key: class method, top-level function, |phase suffix strip
  2. _locate_dot_coverage: present / absent
  3. _parse_coverage_contexts: no coverage package → [], known test cases matched,
     unknown contexts ignored, line-range filtering, deduplication
  4. scan_test_results integrates .coverage block (mtime dedup, entries written)
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from buer.store import Store
from buer.testscan import (
    _context_to_testcase_key,
    _locate_dot_coverage,
    _parse_coverage_contexts,
    scan_test_results,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_store() -> tuple[Store, int, str]:
    store = Store(":memory:")
    root = tempfile.mkdtemp()
    pid = store.get_or_create_project(root)
    return store, pid, root


def _seed_test_case(store: Store, pid: int, classname: str, name: str) -> None:
    """Insert a minimal test_run + test_case so distinct_test_case_triples returns it."""
    run_id = store.insert_test_run(
        pid, seq=None, source_path="/tmp/junit.xml",
        source_mtime="2024-01-01T00:00:00Z",
        passed=1, failed=0, skipped=0,
    )
    store.insert_test_case(
        run_id=run_id, classname=classname, name=name,
        file_path=None, status="passed",
    )


def _seed_determination(store: Store, pid: int, fp: str,
                         define_name: str, start_line: int, end_line: int) -> None:
    store.insert_determination(
        pid, 1, fp, define_name, "fp", "create",
        start_line=start_line, end_line=end_line,
    )


# ── 1. _context_to_testcase_key ───────────────────────────────────────────────

class TestContextToTestcaseKey:
    def test_class_method_with_phase(self):
        cn, name = _context_to_testcase_key(
            "tests/test_foo.py::MyClass::test_bar|run"
        )
        assert cn == "tests.test_foo.MyClass"
        assert name == "test_bar"

    def test_top_level_function(self):
        cn, name = _context_to_testcase_key(
            "tests/test_foo.py::test_simple|run"
        )
        assert cn == "tests.test_foo"
        assert name == "test_simple"

    def test_strips_pipe_suffix(self):
        cn, name = _context_to_testcase_key(
            "tests/test_foo.py::Cls::method|context"
        )
        assert name == "method"

    def test_no_class_no_suffix(self):
        cn, name = _context_to_testcase_key("tests/sub/test_x.py::test_fn")
        assert cn == "tests.sub.test_x"
        assert name == "test_fn"

    def test_too_short_returns_none(self):
        cn, name = _context_to_testcase_key("")
        assert cn is None
        assert name is None

    def test_single_segment_no_sep(self):
        cn, name = _context_to_testcase_key("tests/test_foo.py")
        assert cn is None


# ── 2. _locate_dot_coverage ───────────────────────────────────────────────────

class TestLocateDotCoverage:
    def test_file_present(self, tmp_path):
        (tmp_path / ".coverage").write_bytes(b"SQLite format 3")
        result = _locate_dot_coverage(str(tmp_path))
        assert len(result) == 1
        assert result[0].endswith(".coverage")

    def test_file_absent(self, tmp_path):
        assert _locate_dot_coverage(str(tmp_path)) == []


# ── 3. _parse_coverage_contexts ──────────────────────────────────────────────

class TestParseCoverageContexts:
    def test_no_coverage_package_returns_empty(self, tmp_path):
        (tmp_path / ".coverage").write_bytes(b"")
        store, pid, _ = _make_store()
        with patch.dict("sys.modules", {"coverage": None}):
            result = _parse_coverage_contexts(
                str(tmp_path / ".coverage"), store, pid, str(tmp_path)
            )
        assert result == []

    def test_no_known_test_cases_returns_empty(self, tmp_path):
        """When test_cases table is empty, nothing can be matched."""
        store, pid, _ = _make_store()
        cov_path = str(tmp_path / ".coverage")

        mock_cd = MagicMock()
        mock_cd.measured_files.return_value = []
        mock_cls = MagicMock(return_value=mock_cd)

        with patch("buer.testscan.CoverageData", mock_cls, create=True):
            with patch("builtins.__import__", side_effect=lambda n, *a, **k:
                       mock_cls if n == "coverage" else __import__(n, *a, **k)):
                pass  # the import is patched at module level below

        # Direct mock: monkeypatch the import inside the function
        import buer.testscan as ts_mod
        with patch.object(ts_mod, "_parse_coverage_contexts",
                          wraps=ts_mod._parse_coverage_contexts):
            cov_data_mock = MagicMock()
            cov_data_mock.measured_files.return_value = []
            with patch("builtins.__import__") as mock_import:
                def side_import(name, *args, **kwargs):
                    if name == "coverage":
                        m = MagicMock()
                        m.CoverageData = MagicMock(return_value=cov_data_mock)
                        return m
                    return __import__(name, *args, **kwargs)
                mock_import.side_effect = side_import
                result = ts_mod._parse_coverage_contexts(cov_path, store, pid, str(tmp_path))
        # No test cases seeded → must be empty
        assert result == []

    def test_known_context_matched(self, tmp_path):
        """A context that aligns with a seeded test case + seeded define produces an entry."""
        store, pid, root = _make_store()
        src_fp = str(tmp_path / "src.py")
        Path(src_fp).write_text("def my_fn():\n    pass\n")

        _seed_test_case(store, pid, "tests.test_mod.MyTest", "test_it")
        _seed_determination(store, pid, src_fp, "my_fn", 1, 2)

        cov_data_mock = MagicMock()
        cov_data_mock.measured_files.return_value = [src_fp]
        cov_data_mock.contexts_by_lineno.return_value = {
            1: ["tests/test_mod.py::MyTest::test_it|run"],
        }

        import buer.testscan as ts_mod
        with patch("builtins.__import__") as mock_import:
            def side_import(name, *args, **kwargs):
                if name == "coverage":
                    m = MagicMock()
                    m.CoverageData = MagicMock(return_value=cov_data_mock)
                    return m
                return __import__(name, *args, **kwargs)
            mock_import.side_effect = side_import
            entries = ts_mod._parse_coverage_contexts(src_fp, store, pid, str(tmp_path))

        assert ("tests.test_mod.MyTest::test_it", "my_fn") in entries

    def test_unknown_context_ignored(self, tmp_path):
        store, pid, root = _make_store()
        src_fp = str(tmp_path / "src.py")
        Path(src_fp).write_text("def fn():\n    pass\n")
        _seed_determination(store, pid, src_fp, "fn", 1, 2)
        # No test cases seeded → nothing matches

        cov_data_mock = MagicMock()
        cov_data_mock.measured_files.return_value = [src_fp]
        cov_data_mock.contexts_by_lineno.return_value = {
            1: ["tests/test_other.py::SomeClass::test_x|run"],
        }

        import buer.testscan as ts_mod
        with patch("builtins.__import__") as mock_import:
            def side_import(name, *args, **kwargs):
                if name == "coverage":
                    m = MagicMock()
                    m.CoverageData = MagicMock(return_value=cov_data_mock)
                    return m
                return __import__(name, *args, **kwargs)
            mock_import.side_effect = side_import
            entries = ts_mod._parse_coverage_contexts(src_fp, store, pid, str(tmp_path))

        assert entries == []

    def test_line_outside_range_not_matched(self, tmp_path):
        store, pid, root = _make_store()
        src_fp = str(tmp_path / "src.py")
        Path(src_fp).write_text("def fn():\n    pass\n")
        _seed_test_case(store, pid, "tests.test_mod.T", "test_it")
        _seed_determination(store, pid, src_fp, "fn", 1, 2)

        cov_data_mock = MagicMock()
        cov_data_mock.measured_files.return_value = [src_fp]
        # Line 99 is outside fn's range (1–2)
        cov_data_mock.contexts_by_lineno.return_value = {
            99: ["tests/test_mod.py::T::test_it|run"],
        }

        import buer.testscan as ts_mod
        with patch("builtins.__import__") as mock_import:
            def side_import(name, *args, **kwargs):
                if name == "coverage":
                    m = MagicMock()
                    m.CoverageData = MagicMock(return_value=cov_data_mock)
                    return m
                return __import__(name, *args, **kwargs)
            mock_import.side_effect = side_import
            entries = ts_mod._parse_coverage_contexts(src_fp, store, pid, str(tmp_path))

        assert entries == []

    def test_deduplication(self, tmp_path):
        """Same (tc, define) pair from multiple lines only appears once."""
        store, pid, root = _make_store()
        src_fp = str(tmp_path / "src.py")
        Path(src_fp).write_text("def fn():\n    x = 1\n    return x\n")
        _seed_test_case(store, pid, "tests.test_mod.T", "test_it")
        _seed_determination(store, pid, src_fp, "fn", 1, 3)

        cov_data_mock = MagicMock()
        cov_data_mock.measured_files.return_value = [src_fp]
        ctx = "tests/test_mod.py::T::test_it|run"
        cov_data_mock.contexts_by_lineno.return_value = {
            1: [ctx],
            2: [ctx],
            3: [ctx],
        }

        import buer.testscan as ts_mod
        with patch("builtins.__import__") as mock_import:
            def side_import(name, *args, **kwargs):
                if name == "coverage":
                    m = MagicMock()
                    m.CoverageData = MagicMock(return_value=cov_data_mock)
                    return m
                return __import__(name, *args, **kwargs)
            mock_import.side_effect = side_import
            entries = ts_mod._parse_coverage_contexts(src_fp, store, pid, str(tmp_path))

        assert entries.count(("tests.test_mod.T::test_it", "fn")) == 1


# ── 4. scan_test_results integration ─────────────────────────────────────────

class TestScanTestResultsDotCoverage:
    def test_mtime_dedup_skips_reingest(self, tmp_path):
        """After first ingest, same mtime → skip, no duplicate entries."""
        store, pid, root = _make_store()
        src_fp = str(tmp_path / "src.py")
        Path(src_fp).write_text("def fn():\n    pass\n")
        _seed_test_case(store, pid, "tests.test_mod.T", "test_it")
        _seed_determination(store, pid, src_fp, "fn", 1, 2)

        cov_path = str(tmp_path / ".coverage")
        Path(cov_path).write_bytes(b"")

        cov_data_mock = MagicMock()
        cov_data_mock.measured_files.return_value = [src_fp]
        cov_data_mock.contexts_by_lineno.return_value = {
            1: ["tests/test_mod.py::T::test_it|run"],
        }

        import buer.testscan as ts_mod

        def fake_locate(r):
            return [cov_path] if r == root else []

        def fake_parse(cp, st, pr, rt):
            return [("tests.test_mod.T::test_it", "fn")]

        with patch.object(ts_mod, "_locate_dot_coverage", fake_locate), \
             patch.object(ts_mod, "_parse_coverage_contexts", fake_parse):
            scan_test_results(store, pid, root)
            count_after_first = store.con.execute(
                "SELECT COUNT(*) FROM coverage_map WHERE project_id=?", (pid,)
            ).fetchone()[0]
            scan_test_results(store, pid, root)
            count_after_second = store.con.execute(
                "SELECT COUNT(*) FROM coverage_map WHERE project_id=?", (pid,)
            ).fetchone()[0]

        assert count_after_first == 1
        assert count_after_second == 1  # dedup: no double insert
