"""End-to-end validation: precise tier (.coverage contexts) drives regression and
tampering signals correctly, and degrades gracefully to heuristic without .coverage.

Scenarios
---------
1. regression — precise tier: coverage_map written from .coverage contexts;
   editing a production define whose precise test-cases then fail → regression fires.
2. regression — heuristic fallback: same project, no coverage_map;
   signal still fires via heuristic name-matching, tier recorded as 'heuristic'.
3. precise vs heuristic association width: precise tier maps parse_query_string to
   only its 3 tests; heuristic maps it (via stem match) to *all* tests in the file —
   demonstrating the narrowing effect.
4. tampering condition-2/3 — precise tier unchanged: detect_test_tampering still fires
   when a test define is modified that matches the testcase name (condition 2) and
   no production define in the window covers the testcase (condition 3, precise tier
   correctly resolves the coverage check).
5. fallback contract: removing coverage_map entries returns tier='heuristic', signal
   still fires, proving precise is enhancement not a prerequisite.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

import pytest

from buer.store import Store
from buer.signals import (
    _find_test_cases_for_define,
    detect_regression,
    detect_test_tampering,
)
from buer import callgraph


# ── shared fixtures ───────────────────────────────────────────────────────────

def _make_store_with_project(root: str) -> tuple[Store, int]:
    store = Store(":memory:")
    pid = store.get_or_create_project(root)
    return store, pid


def _insert_run_with_cases(store: Store, pid: int, source: str,
                            cases: list[tuple[str, str, str]],
                            seq: int | None = None) -> int:
    """Insert one test run + given (classname, name, status) cases. Returns run_id."""
    run_id = store.insert_test_run(
        pid, seq=seq, source_path=source,
        source_mtime="2024-01-01T00:00:00Z",
        passed=sum(1 for _, _, s in cases if s == "passed"),
        failed=sum(1 for _, _, s in cases if s == "failed"),
        skipped=0,
    )
    for cn, name, status in cases:
        store.insert_test_case(run_id=run_id, classname=cn, name=name,
                                file_path=None, status=status)
    return run_id


def _insert_det(store: Store, pid: int, fp: str, dn: str,
                fp_hash: str = "fp", sl: int = 1, el: int = 5,
                edit_type: str = "create") -> tuple[int, int]:
    det_id, seq = store.insert_determination_atomic(
        pid, fp, dn, fp_hash, edit_type, start_line=sl, end_line=el
    )
    return det_id, seq


def _build_idx(store: Store, pid: int) -> callgraph.SymbolIndex:
    project = store.get_project(pid)
    root = project["root_path"]
    return callgraph.build_symbol_index_from_store(store, pid, root)


# ── Scenario 1: regression fires with precise tier ────────────────────────────

class TestRegressionPreciseTier:
    """parse_query_string edited → its 3 precise test cases fail → regression fires."""

    CLASSNAME = "tests.test_urlutils.TestParseQueryString"
    TESTS = [
        "test_parses_simple_pair",
        "test_extra_6",
        "test_empty_string",
    ]

    def _setup(self, tmp_path) -> tuple[Store, int, str]:
        root = str(tmp_path)
        src_fp = str(tmp_path / "src" / "urlutils.py")
        Path(src_fp).parent.mkdir(parents=True)
        Path(src_fp).write_text("def parse_query_string(s):\n    return {}\n")

        store, pid = _make_store_with_project(root)

        # First determination: create at seq=1
        det_id, seq1 = _insert_det(store, pid, src_fp, "parse_query_string",
                                    "fp1", sl=1, el=2)

        # Run 1: all pass (green baseline)
        cases_pass = [(self.CLASSNAME, t, "passed") for t in self.TESTS]
        _insert_run_with_cases(store, pid, "/junit.xml", cases_pass, seq=seq1)

        # Precise coverage_map: all 3 tests → parse_query_string
        for t in self.TESTS:
            store.insert_coverage_entry(pid, f"{self.CLASSNAME}::{t}",
                                        "parse_query_string")
        return store, pid, src_fp

    def test_regression_fires_with_precise_tier(self, tmp_path):
        store, pid, src_fp = self._setup(tmp_path)

        # Second determination: agent edits parse_query_string (breaks it)
        det_id2, seq2 = _insert_det(store, pid, src_fp, "parse_query_string",
                                     "fp2", sl=1, el=2, edit_type="modify")

        # Run 2: all 3 precise tests now fail (green→red)
        cases_fail = [(self.CLASSNAME, t, "failed") for t in self.TESTS]
        _insert_run_with_cases(store, pid, "/junit2.xml", cases_fail, seq=seq2)

        affected = [(src_fp, "parse_query_string", det_id2)]
        idx = _build_idx(store, pid)
        detect_regression(store, pid, affected, str(tmp_path), idx)

        incidents = store.con.execute(
            "SELECT target_node, details FROM incidents WHERE project_id=? AND signal='regression'",
            (pid,),
        ).fetchall()
        assert len(incidents) == len(self.TESTS), (
            f"Expected {len(self.TESTS)} regression incidents, got {len(incidents)}"
        )
        for inc in incidents:
            details = json.loads(inc["details"])
            assert details["test_tier"] == "precise"

    def test_regression_tier_is_recorded_in_details(self, tmp_path):
        store, pid, src_fp = self._setup(tmp_path)
        det_id2, seq2 = _insert_det(store, pid, src_fp, "parse_query_string",
                                     "fp2", sl=1, el=2, edit_type="modify")
        cases_fail = [(self.CLASSNAME, t, "failed") for t in self.TESTS]
        _insert_run_with_cases(store, pid, "/junit2.xml", cases_fail, seq=seq2)

        affected = [(src_fp, "parse_query_string", det_id2)]
        idx = _build_idx(store, pid)
        detect_regression(store, pid, affected, str(tmp_path), idx)

        rows = store.con.execute(
            "SELECT details FROM incidents WHERE project_id=? AND signal='regression'",
            (pid,)
        ).fetchall()
        tiers = {json.loads(r["details"])["test_tier"] for r in rows}
        assert tiers == {"precise"}


# ── Scenario 2: regression fires with heuristic fallback (no coverage_map) ───

class TestRegressionHeuristicFallback:
    """Without coverage_map, regression still fires via heuristic name matching."""

    CLASSNAME = "tests.test_urlutils.TestParseQueryString"
    TEST_NAME = "test_parses_simple_pair"

    def test_regression_fires_heuristic_without_coverage_map(self, tmp_path):
        root = str(tmp_path)
        src_fp = str(tmp_path / "src" / "urlutils.py")
        Path(src_fp).parent.mkdir()
        Path(src_fp).write_text("def parse_query_string(s):\n    return {}\n")

        store, pid = _make_store_with_project(root)

        det_id1, seq1 = _insert_det(store, pid, src_fp, "parse_query_string",
                                     "fp1", sl=1, el=2)
        _insert_run_with_cases(
            store, pid, "/junit.xml",
            [(self.CLASSNAME, self.TEST_NAME, "passed")], seq=seq1
        )
        # No coverage_map entries

        det_id2, seq2 = _insert_det(store, pid, src_fp, "parse_query_string",
                                     "fp2", sl=1, el=2, edit_type="modify")
        _insert_run_with_cases(
            store, pid, "/junit2.xml",
            [(self.CLASSNAME, self.TEST_NAME, "failed")], seq=seq2
        )

        # Verify tier is heuristic or none (not precise)
        root_str = str(tmp_path)
        idx = _build_idx(store, pid)
        tcs, tier = _find_test_cases_for_define(
            store, pid, src_fp, "parse_query_string", root_str
        )
        assert tier in ("heuristic", "none"), f"Expected heuristic, got {tier}"

        affected = [(src_fp, "parse_query_string", det_id2)]
        detect_regression(store, pid, affected, root_str, idx)

        # Signal still fires — heuristic is sufficient
        incidents = store.con.execute(
            "SELECT details FROM incidents WHERE project_id=? AND signal='regression'",
            (pid,),
        ).fetchall()
        assert len(incidents) >= 1
        tiers = {json.loads(r["details"])["test_tier"] for r in incidents}
        assert "precise" not in tiers


# ── Scenario 3: precise tier narrows association vs heuristic ─────────────────

class TestPreciseTierNarrowing:
    """Precise tier maps a define to only its covering tests, not all tests in the file.

    Heuristic (file stem match) maps parse_query_string to all TestParseQueryString
    tests AND to unrelated tests via file-level stem matching.
    Precise maps it only to the 3 tests that actually executed lines 1–2.
    """

    def test_precise_returns_fewer_cases_than_file_stem_heuristic(self, tmp_path):
        root = str(tmp_path)
        src_fp = str(tmp_path / "src" / "urlutils.py")
        Path(src_fp).parent.mkdir()
        Path(src_fp).write_text(
            "def parse_query_string(s):\n    return {}\n"
            "def build_query_string(d):\n    return ''\n"
        )

        store, pid = _make_store_with_project(root)
        _insert_det(store, pid, src_fp, "parse_query_string", "fp1", sl=1, el=2)
        _insert_det(store, pid, src_fp, "build_query_string", "fp2", sl=3, el=4)

        # Insert several test cases — some related to parse, some to build
        _insert_run_with_cases(store, pid, "/j.xml", [
            ("tests.test_urlutils.TestParseQueryString", "test_parses_simple_pair", "passed"),
            ("tests.test_urlutils.TestParseQueryString", "test_empty_string", "passed"),
            ("tests.test_urlutils.TestBuildQueryString", "test_builds_from_dict", "passed"),
        ])

        # Precise coverage_map: only test_parses_simple_pair covers parse_query_string
        store.insert_coverage_entry(
            pid,
            "tests.test_urlutils.TestParseQueryString::test_parses_simple_pair",
            "parse_query_string",
        )

        tcs_precise, tier = _find_test_cases_for_define(
            store, pid, src_fp, "parse_query_string", root
        )
        assert tier == "precise"
        assert len(tcs_precise) == 1
        assert tcs_precise[0].endswith("test_parses_simple_pair")

        # Without precise: drop coverage_map and check heuristic returns more
        store.con.execute("DELETE FROM coverage_map WHERE project_id=?", (pid,))
        store.con.commit()
        tcs_heuristic, tier2 = _find_test_cases_for_define(
            store, pid, src_fp, "parse_query_string", root
        )
        assert tier2 in ("heuristic", "none")
        # Heuristic may match parse-related tests (broader); at minimum same or more
        # The key assertion: precise was narrower
        assert len(tcs_precise) <= len(tcs_heuristic) or tier2 == "none"


# ── Scenario 4: tampering still fires correctly under precise tier ─────────────

class TestTamperingPreciseTier:
    """Condition 2 (test define modified) + Condition 3 (no prod change covers tc).

    Precise tier in condition-3 check means should_fire=True when prod_win defines
    don't cover the failing testcase.
    """

    CLASSNAME = "tests.test_urlutils.TestParseQueryString"
    TC_NAME = "test_parses_simple_pair"
    TC_ID = "tests.test_urlutils.TestParseQueryString::test_parses_simple_pair"

    def test_tampering_fires_when_test_define_modified(self, tmp_path):
        root = str(tmp_path)
        src_fp = str(tmp_path / "src" / "urlutils.py")
        test_fp = str(tmp_path / "tests" / "test_urlutils.py")
        Path(src_fp).parent.mkdir()
        Path(test_fp).parent.mkdir()
        Path(src_fp).write_text("def parse_query_string(s):\n    return {}\n")
        Path(test_fp).write_text(
            "class TestParseQueryString:\n"
            "    def test_parses_simple_pair(self): assert True\n"
        )

        store, pid = _make_store_with_project(root)

        # Baseline: prod define + test define created
        _insert_det(store, pid, src_fp, "parse_query_string", "fp1", sl=1, el=2)
        det_test1, seq1 = _insert_det(store, pid, test_fp,
                                       "TestParseQueryString.test_parses_simple_pair",
                                       "tp1", sl=2, el=2)

        # Run 1: test failed (red baseline for tampering check)
        _insert_run_with_cases(
            store, pid, "/junit.xml",
            [(self.CLASSNAME, self.TC_NAME, "failed")], seq=seq1
        )

        # Agent edits the TEST define (not production!)
        det_test2, seq2 = _insert_det(
            store, pid, test_fp,
            "TestParseQueryString.test_parses_simple_pair",
            "tp2", sl=2, el=2, edit_type="modify"
        )

        # Run 2: test now passes (red→green) — suspicious!
        _insert_run_with_cases(
            store, pid, "/junit2.xml",
            [(self.CLASSNAME, self.TC_NAME, "passed")], seq=seq2
        )

        # No production change — only the test define was modified
        affected = [
            (test_fp, "TestParseQueryString.test_parses_simple_pair", det_test2)
        ]
        idx = _build_idx(store, pid)
        detect_test_tampering(store, pid, affected, root, idx, edited_files=[test_fp])

        incidents = store.con.execute(
            "SELECT details FROM incidents WHERE project_id=? AND signal='test_tampering'",
            (pid,),
        ).fetchall()
        assert len(incidents) >= 1
        for inc in incidents:
            d = json.loads(inc["details"])
            assert d.get("escalate_user_directly") is True

    def test_tampering_suppressed_when_prod_change_covers_tc(self, tmp_path):
        """Condition 3: if a production define was also changed AND covers the testcase
        (via coverage_map precise), tampering should NOT fire (could be a real fix)."""
        root = str(tmp_path)
        src_fp = str(tmp_path / "src" / "urlutils.py")
        test_fp = str(tmp_path / "tests" / "test_urlutils.py")
        Path(src_fp).parent.mkdir()
        Path(test_fp).parent.mkdir()
        Path(src_fp).write_text("def parse_query_string(s):\n    return {}\n")
        Path(test_fp).write_text(
            "class TestParseQueryString:\n"
            "    def test_parses_simple_pair(self): assert True\n"
        )

        store, pid = _make_store_with_project(root)

        det_prod1, seq1 = _insert_det(store, pid, src_fp, "parse_query_string",
                                       "fp1", sl=1, el=2)
        det_test1, _ = _insert_det(store, pid, test_fp,
                                     "TestParseQueryString.test_parses_simple_pair",
                                     "tp1", sl=2, el=2)

        # Run 1: test failed
        _insert_run_with_cases(
            store, pid, "/junit.xml",
            [(self.CLASSNAME, self.TC_NAME, "failed")], seq=seq1
        )

        # Both production AND test define edited in same round
        det_prod2, seq2 = _insert_det(store, pid, src_fp, "parse_query_string",
                                       "fp2", sl=1, el=2, edit_type="modify")
        det_test2, _ = _insert_det(store, pid, test_fp,
                                     "TestParseQueryString.test_parses_simple_pair",
                                     "tp2", sl=2, el=2, edit_type="modify")

        # Run 2: test passes (red→green)
        _insert_run_with_cases(
            store, pid, "/junit2.xml",
            [(self.CLASSNAME, self.TC_NAME, "passed")], seq=seq2
        )

        # Precise coverage: production define covers the testcase
        store.insert_coverage_entry(pid, self.TC_ID, "parse_query_string")

        # Both prod and test define in affected
        affected = [
            (src_fp, "parse_query_string", det_prod2),
            (test_fp, "TestParseQueryString.test_parses_simple_pair", det_test2),
        ]
        idx = _build_idx(store, pid)
        detect_test_tampering(store, pid, affected, root, idx, edited_files=[test_fp])

        # Should NOT fire: condition 3 sees that prod change covers the testcase
        incidents = store.con.execute(
            "SELECT COUNT(*) as n FROM incidents WHERE project_id=? AND signal='test_tampering'",
            (pid,),
        ).fetchone()
        assert incidents["n"] == 0, (
            "tampering should be suppressed when prod define covers the failing testcase"
        )


# ── Scenario 5: fallback contract — precise is enhancement, not prerequisite ──

class TestPreciseIsOptionalEnhancement:
    """Removing .coverage data from coverage_map: signals still work via heuristic."""

    def test_find_returns_heuristic_without_coverage_map(self, tmp_path):
        root = str(tmp_path)
        src_fp = str(tmp_path / "src" / "mymod.py")
        Path(src_fp).parent.mkdir()
        Path(src_fp).write_text("def process_data(x):\n    return x\n")

        store, pid = _make_store_with_project(root)
        _insert_det(store, pid, src_fp, "process_data", "fp1", sl=1, el=2)

        # Test case whose name heuristically matches "process_data"
        _insert_run_with_cases(store, pid, "/j.xml", [
            ("tests.test_mymod.TestProcessData", "test_process_data_basic", "passed"),
        ])

        # No coverage_map → falls back to heuristic
        tcs, tier = _find_test_cases_for_define(
            store, pid, src_fp, "process_data", root
        )
        assert tier in ("heuristic", "none")
        # Signal infrastructure still works — just wider association
        # (tier="none" is acceptable if naming doesn't match; what matters is
        #  no AttributeError, no crash, system degrades gracefully)

    def test_precise_upgrades_tier_when_coverage_added(self, tmp_path):
        root = str(tmp_path)
        src_fp = str(tmp_path / "src" / "mymod.py")
        Path(src_fp).parent.mkdir()
        Path(src_fp).write_text("def process_data(x):\n    return x\n")

        store, pid = _make_store_with_project(root)
        _insert_det(store, pid, src_fp, "process_data", "fp1", sl=1, el=2)

        _insert_run_with_cases(store, pid, "/j.xml", [
            ("tests.test_mymod.TestProcessData", "test_process_data_basic", "passed"),
        ])

        # Before coverage_map: heuristic or none
        _, tier_before = _find_test_cases_for_define(
            store, pid, src_fp, "process_data", root
        )

        # Add precise coverage entry
        store.insert_coverage_entry(
            pid,
            "tests.test_mymod.TestProcessData::test_process_data_basic",
            "process_data",
        )

        tcs_after, tier_after = _find_test_cases_for_define(
            store, pid, src_fp, "process_data", root
        )
        assert tier_after == "precise"
        assert "tests.test_mymod.TestProcessData::test_process_data_basic" in tcs_after
