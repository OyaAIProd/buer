"""Tests for run_tests inline assist (§4.11 extension, §4.5 extension).

Verification coverage (A–G from design spec):
  (A) Precise tier: covered define, all covering tests stale (run_seq < det_seq)
      → fire, list specific test_case names.
  (B) Precise tier: covering test ran AFTER the edit (run_seq >= det_seq)
      → no fire (tests are fresh).
  (C) Heuristic tier: no coverage_map + project has tests + no test_run since edit
      → fire, heuristic wording, marked as 启发档.
  (D) Heuristic tier: no coverage_map + NO test files
      → no fire (that's safety_net territory, not run_tests).
  (E) Arbitration: commit + run_tests + blast_radius all want to fire
      → PRIORITY order, commit wins (index 0 < 1 < 2).
  (F) No incidents created — assist is advisory only, does NOT go through the
      incidents state machine.
  (G) Message uses 建议/疑问 language; no test command strings (no pytest/npm).
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from buer import assists, health
from buer.assists import (
    InlineAssist,
    PRIORITY,
    _build_run_tests_assist,
    _stale_covered_defines,
    arbitrate_inline_assists,
    run_inline_assists,
)
from buer.store import Store


# ── helpers ────────────────────────────────────────────────────────────────────

def _store() -> Store:
    return Store(":memory:")


def _project(store: Store, root: str = "/test") -> int:
    return store.get_or_create_project(root)


def _det(store: Store, pid: int, file_path: str, define_name: str, seq: int):
    did = store.insert_determination(
        pid, seq=seq, file_path=file_path, define_name=define_name,
        node_fingerprint=f"fp_{define_name}_{seq}", edit_type="modify",
    )
    return store.con.execute("SELECT * FROM determinations WHERE id=?", (did,)).fetchone()


def _add_coverage(store: Store, pid: int, test_case: str, define_name: str) -> None:
    store.insert_coverage_entry(pid, test_case, define_name)


def _add_test_run(store: Store, pid: int, seq: int, passed: int = 1) -> int:
    return store.insert_test_run(
        pid, seq=seq, source_path="test_report.xml",
        source_mtime="2026-01-01T00:00:00",
        passed=passed, failed=0, skipped=0,
    )


def _add_test_case(
    store: Store, run_id: int, classname: str, name: str, status: str = "passed"
) -> None:
    store.insert_test_case(run_id, classname=classname, name=name, file_path=None, status=status)


TS_FILE = "/test/src/handler.ts"
ROOT = "/test"


# ── _stale_covered_defines unit tests ─────────────────────────────────────────

class TestStaleCoveredDefines:
    def test_all_stale_when_no_history(self):
        store = _store()
        pid = _project(store)
        det = _det(store, pid, TS_FILE, "handle", seq=3)
        _add_coverage(store, pid, "TestH::test_basic", "handle")
        # No test_case_history → stale
        result = _stale_covered_defines(store, pid, [(TS_FILE, "handle", det["id"])])
        assert len(result) == 1
        assert result[0][0] == "handle"
        assert "TestH::test_basic" in result[0][1]

    def test_all_stale_when_run_seq_before_det(self):
        store = _store()
        pid = _project(store)
        det = _det(store, pid, TS_FILE, "handle", seq=5)
        _add_coverage(store, pid, "TestH::test_basic", "handle")
        run_id = _add_test_run(store, pid, seq=3)  # ran at seq=3, before det seq=5
        _add_test_case(store, run_id, "TestH", "test_basic")
        result = _stale_covered_defines(store, pid, [(TS_FILE, "handle", det["id"])])
        assert len(result) == 1

    def test_not_stale_when_run_seq_after_det(self):
        store = _store()
        pid = _project(store)
        det = _det(store, pid, TS_FILE, "handle", seq=3)
        _add_coverage(store, pid, "TestH::test_basic", "handle")
        run_id = _add_test_run(store, pid, seq=5)  # ran at seq=5, after det seq=3
        _add_test_case(store, run_id, "TestH", "test_basic")
        result = _stale_covered_defines(store, pid, [(TS_FILE, "handle", det["id"])])
        assert result == []

    def test_not_stale_when_run_seq_equals_det(self):
        store = _store()
        pid = _project(store)
        det = _det(store, pid, TS_FILE, "handle", seq=4)
        _add_coverage(store, pid, "TestH::test_eq", "handle")
        run_id = _add_test_run(store, pid, seq=4)  # same seq → counts as "after"
        _add_test_case(store, run_id, "TestH", "test_eq")
        result = _stale_covered_defines(store, pid, [(TS_FILE, "handle", det["id"])])
        assert result == []

    def test_one_fresh_test_makes_define_not_stale(self):
        """If any one covering test ran after edit → define is not stale."""
        store = _store()
        pid = _project(store)
        det = _det(store, pid, TS_FILE, "handle", seq=3)
        _add_coverage(store, pid, "TestH::test_a", "handle")
        _add_coverage(store, pid, "TestH::test_b", "handle")
        # test_a ran at seq=2 (stale), test_b ran at seq=5 (fresh)
        run_a = _add_test_run(store, pid, seq=2)
        _add_test_case(store, run_a, "TestH", "test_a")
        run_b = _add_test_run(store, pid, seq=5)
        _add_test_case(store, run_b, "TestH", "test_b")
        result = _stale_covered_defines(store, pid, [(TS_FILE, "handle", det["id"])])
        assert result == []

    def test_define_with_no_coverage_skipped(self):
        store = _store()
        pid = _project(store)
        det = _det(store, pid, TS_FILE, "handle", seq=3)
        # No coverage_map entry for "handle"
        result = _stale_covered_defines(store, pid, [(TS_FILE, "handle", det["id"])])
        assert result == []


# ── _build_run_tests_assist: precise tier ─────────────────────────────────────

class TestPreciseTier:
    def _setup(self, det_seq: int, run_seq: int | None):
        store = _store()
        pid = _project(store)
        det = _det(store, pid, TS_FILE, "handle", seq=det_seq)
        _add_coverage(store, pid, "TestH::test_basic", "handle")
        if run_seq is not None:
            run_id = _add_test_run(store, pid, seq=run_seq)
            _add_test_case(store, run_id, "TestH", "test_basic")
        return store, pid, det

    def test_fires_when_tests_stale(self):
        """(A) Covered define, all tests stale → fire."""
        store, pid, det = self._setup(det_seq=5, run_seq=3)
        assist = _build_run_tests_assist(store, pid, [(TS_FILE, "handle", det["id"])], ROOT)
        assert assist.should_fire is True
        assert assist.kind == "run_tests"

    def test_lists_test_case_names_in_message(self):
        """(A) Specific test_case names appear in precise-tier message."""
        store, pid, det = self._setup(det_seq=5, run_seq=3)
        assist = _build_run_tests_assist(store, pid, [(TS_FILE, "handle", det["id"])], ROOT)
        assert "TestH::test_basic" in assist.message

    def test_no_fire_when_test_ran_after_edit(self):
        """(B) Covering test ran after edit → no fire."""
        store, pid, det = self._setup(det_seq=3, run_seq=5)
        assist = _build_run_tests_assist(store, pid, [(TS_FILE, "handle", det["id"])], ROOT)
        assert assist.should_fire is False

    def test_no_fire_when_no_coverage_for_affected(self):
        """Precise tier but define not in coverage_map → no fire."""
        store = _store()
        pid = _project(store)
        # Insert some unrelated coverage to make has_coverage=True
        _add_coverage(store, pid, "OtherTest::test_x", "otherDefine")
        det = _det(store, pid, TS_FILE, "handle", seq=3)
        assist = _build_run_tests_assist(store, pid, [(TS_FILE, "handle", det["id"])], ROOT)
        assert assist.should_fire is False

    def test_precise_tier_message_label(self):
        """Precise-tier message includes 精确档 label."""
        store, pid, det = self._setup(det_seq=5, run_seq=3)
        assist = _build_run_tests_assist(store, pid, [(TS_FILE, "handle", det["id"])], ROOT)
        assert "precise tier" in assist.message

    def test_multiple_stale_defines_all_listed(self):
        store = _store()
        pid = _project(store)
        det1 = _det(store, pid, TS_FILE, "handle", seq=5)
        det2 = _det(store, pid, TS_FILE, "process", seq=6)
        _add_coverage(store, pid, "TestH::test_handle", "handle")
        _add_coverage(store, pid, "TestH::test_process", "process")
        # Both stale (no runs)
        assist = _build_run_tests_assist(
            store, pid,
            [(TS_FILE, "handle", det1["id"]), (TS_FILE, "process", det2["id"])],
            ROOT,
        )
        assert assist.should_fire is True
        assert "test_handle" in assist.message
        assert "test_process" in assist.message


# ── _build_run_tests_assist: heuristic tier ───────────────────────────────────

class TestHeuristicTier:
    def test_fires_when_no_coverage_has_tests_no_run(self):
        """(C) No coverage_map + test files exist + no test_run since edit → fire."""
        store = _store()
        pid = _project(store)
        det = _det(store, pid, TS_FILE, "handle", seq=5)
        with patch.object(health, "_has_test_files", return_value=True):
            assist = _build_run_tests_assist(
                store, pid, [(TS_FILE, "handle", det["id"])], ROOT
            )
        assert assist.should_fire is True
        assert assist.kind == "run_tests"

    def test_heuristic_message_label(self):
        """(C) Heuristic-tier message includes 启发档 label."""
        store = _store()
        pid = _project(store)
        det = _det(store, pid, TS_FILE, "handle", seq=5)
        with patch.object(health, "_has_test_files", return_value=True):
            assist = _build_run_tests_assist(
                store, pid, [(TS_FILE, "handle", det["id"])], ROOT
            )
        assert "heuristic tier" in assist.message

    def test_no_fire_when_no_test_files(self):
        """(D) No coverage_map + no test files → no fire (safety_net handles this)."""
        store = _store()
        pid = _project(store)
        det = _det(store, pid, TS_FILE, "handle", seq=5)
        with patch.object(health, "_has_test_files", return_value=False):
            assist = _build_run_tests_assist(
                store, pid, [(TS_FILE, "handle", det["id"])], ROOT
            )
        assert assist.should_fire is False

    def test_no_fire_when_test_ran_after_edit(self):
        """Heuristic tier: test_run seq >= edit seq → tests ran, no fire."""
        store = _store()
        pid = _project(store)
        det = _det(store, pid, TS_FILE, "handle", seq=3)
        _add_test_run(store, pid, seq=5)  # ran after edit
        with patch.object(health, "_has_test_files", return_value=True):
            assist = _build_run_tests_assist(
                store, pid, [(TS_FILE, "handle", det["id"])], ROOT
            )
        assert assist.should_fire is False

    def test_no_fire_when_test_ran_at_same_seq_as_edit(self):
        """Heuristic tier: test_run seq == edit seq → counts as "after", no fire."""
        store = _store()
        pid = _project(store)
        det = _det(store, pid, TS_FILE, "handle", seq=5)
        _add_test_run(store, pid, seq=5)
        with patch.object(health, "_has_test_files", return_value=True):
            assist = _build_run_tests_assist(
                store, pid, [(TS_FILE, "handle", det["id"])], ROOT
            )
        assert assist.should_fire is False

    def test_fires_when_test_ran_before_edit(self):
        """Heuristic tier: only old test_run (seq < edit) → fire."""
        store = _store()
        pid = _project(store)
        det = _det(store, pid, TS_FILE, "handle", seq=5)
        _add_test_run(store, pid, seq=2)  # old run, before edit
        with patch.object(health, "_has_test_files", return_value=True):
            assist = _build_run_tests_assist(
                store, pid, [(TS_FILE, "handle", det["id"])], ROOT
            )
        assert assist.should_fire is True


# ── message quality (G) ───────────────────────────────────────────────────────

class TestMessageQuality:
    def _precise_assist(self):
        store = _store()
        pid = _project(store)
        det = _det(store, pid, TS_FILE, "handle", seq=5)
        _add_coverage(store, pid, "TestH::test_basic", "handle")
        run_id = _add_test_run(store, pid, seq=3)
        _add_test_case(store, run_id, "TestH", "test_basic")
        return _build_run_tests_assist(store, pid, [(TS_FILE, "handle", det["id"])], ROOT)

    def _heuristic_assist(self):
        store = _store()
        pid = _project(store)
        det = _det(store, pid, TS_FILE, "handle", seq=5)
        with patch.object(health, "_has_test_files", return_value=True):
            return _build_run_tests_assist(store, pid, [(TS_FILE, "handle", det["id"])], ROOT)

    def test_precise_message_is_question(self):
        """(G) Message uses 建议/疑问 phrasing."""
        assist = self._precise_assist()
        assert "?" in assist.message

    def test_heuristic_message_is_question(self):
        """(G) Heuristic message uses 建议/疑问 phrasing."""
        assist = self._heuristic_assist()
        assert "?" in assist.message

    def test_no_pytest_in_message(self):
        """(G) No specific test command in either message."""
        p = self._precise_assist()
        h = self._heuristic_assist()
        for msg in (p.message, h.message):
            assert "pytest" not in msg
            assert "npm test" not in msg
            assert "npm run" not in msg
            assert "cargo test" not in msg

    def test_no_mandatory_language(self):
        """(G) Message doesn't use '必须'."""
        p = self._precise_assist()
        h = self._heuristic_assist()
        for msg in (p.message, h.message):
            assert "必须" not in msg


# ── arbitration (E) ───────────────────────────────────────────────────────────

class TestArbitration:
    def test_priority_order(self):
        """(E) PRIORITY = ['commit', 'run_tests', 'blast_radius']."""
        assert PRIORITY == ["commit", "run_tests", "blast_radius"]

    def test_commit_beats_run_tests(self):
        candidates = [
            InlineAssist(kind="commit", should_fire=True, message="commit"),
            InlineAssist(kind="run_tests", should_fire=True, message="tests"),
        ]
        winner = arbitrate_inline_assists(candidates)
        assert winner is not None
        assert winner.kind == "commit"

    def test_run_tests_beats_blast_radius(self):
        candidates = [
            InlineAssist(kind="run_tests", should_fire=True, message="tests"),
            InlineAssist(kind="blast_radius", should_fire=True, message="blast"),
        ]
        winner = arbitrate_inline_assists(candidates)
        assert winner is not None
        assert winner.kind == "run_tests"

    def test_all_three_commit_wins(self):
        """(E) All three want to fire → commit wins (index 0)."""
        candidates = [
            InlineAssist(kind="blast_radius", should_fire=True, message="blast"),
            InlineAssist(kind="run_tests", should_fire=True, message="tests"),
            InlineAssist(kind="commit", should_fire=True, message="commit"),
        ]
        winner = arbitrate_inline_assists(candidates)
        assert winner is not None
        assert winner.kind == "commit"

    def test_run_tests_fires_when_only_active(self):
        candidates = [
            InlineAssist(kind="commit", should_fire=False, message=""),
            InlineAssist(kind="run_tests", should_fire=True, message="tests"),
            InlineAssist(kind="blast_radius", should_fire=False, message=""),
        ]
        winner = arbitrate_inline_assists(candidates)
        assert winner is not None
        assert winner.kind == "run_tests"


# ── not in incidents (F) ──────────────────────────────────────────────────────

class TestNotInIncidents:
    def test_no_incident_created_by_assist(self):
        """(F) run_tests fires via delivery only — incidents table stays empty."""
        store = _store()
        pid = _project(store)
        det = _det(store, pid, TS_FILE, "handle", seq=5)
        _add_coverage(store, pid, "TestH::test_basic", "handle")
        run_id = _add_test_run(store, pid, seq=3)
        _add_test_case(store, run_id, "TestH", "test_basic")

        assist = _build_run_tests_assist(store, pid, [(TS_FILE, "handle", det["id"])], ROOT)
        assert assist.should_fire is True

        # No incident written
        n = store.con.execute(
            "SELECT COUNT(*) AS n FROM incidents WHERE project_id = ?", (pid,)
        ).fetchone()["n"]
        assert n == 0

    def test_run_inline_assists_routes_to_agent_channel(self):
        """(F) run_tests uses agent channel (not user)."""
        store = _store()
        pid = _project(store)
        det = _det(store, pid, TS_FILE, "handle", seq=5)
        _add_coverage(store, pid, "TestH::test_basic", "handle")
        run_id = _add_test_run(store, pid, seq=3)
        _add_test_case(store, run_id, "TestH", "test_basic")

        run_inline_assists(store, pid, [(TS_FILE, "handle", det["id"])], ROOT)

        deliveries = store.con.execute(
            "SELECT channel FROM pending_deliveries WHERE project_id = ?", (pid,)
        ).fetchall()
        # Should have exactly one delivery on agent channel
        assert len(deliveries) == 1
        assert deliveries[0]["channel"] == "agent"


# ── store.latest_test_run_seq ─────────────────────────────────────────────────

class TestLatestTestRunSeq:
    def test_returns_none_when_no_runs(self):
        store = _store()
        pid = _project(store)
        assert store.latest_test_run_seq(pid) is None

    def test_returns_max_seq(self):
        store = _store()
        pid = _project(store)
        _add_test_run(store, pid, seq=3)
        _add_test_run(store, pid, seq=7)
        _add_test_run(store, pid, seq=5)
        assert store.latest_test_run_seq(pid) == 7

    def test_ignores_null_seq(self):
        store = _store()
        pid = _project(store)
        store.insert_test_run(
            pid, seq=None, source_path="x.xml", source_mtime="2026-01-01T00:00:00",
            passed=1, failed=0, skipped=0,
        )
        assert store.latest_test_run_seq(pid) is None

    def test_ignores_null_seq_returns_max_of_non_null(self):
        store = _store()
        pid = _project(store)
        _add_test_run(store, pid, seq=4)
        store.insert_test_run(
            pid, seq=None, source_path="null.xml", source_mtime="2026-01-01T00:00:00",
            passed=0, failed=0, skipped=0,
        )
        assert store.latest_test_run_seq(pid) == 4
