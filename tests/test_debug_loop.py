"""Tests for debug_loop signal (§2.2 — stuck_region 증강 버전).

Covers:
  - Precise tier (coverage_map → define association)
  - Heuristic tier (name-matching, §5.3 degraded)
  - No tests → fall back to stuck_region (no debug_loop)
  - Tests pass → no debug_loop
  - Debug_loop/stuck_region exclusivity (no double-reporting)
  - Root cause direction in incident details
  - State machine: open → notified_agent → escalated_user → resolved
  - Resolve when test passes (test_passed)
  - θ₂ escalation
"""
import json

import pytest

from buer.store import Store
from buer import signals

ROOT = "/test"
FILE = "/test/f.py"
DEFINE = "fn"
EMPTY_IDX = __import__("buer.callgraph", fromlist=["SymbolIndex"]).SymbolIndex({}, {})

# N=5: canonical θ₁=5 chain where last adjacent d_J(v4,v5) = 1/5 = THETA_DJ exactly.
# N=6 would give d_J(v5,v6) = 1/6 < THETA_DJ → _is_stuck returns False.
N = 5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mem_store() -> Store:
    return Store(":memory:")


def _project(store: Store) -> int:
    return store.get_or_create_project(ROOT)


def _stuck_chain(store: Store, pid: int, n: int = N) -> list[int]:
    """Pure n-node version chain for FILE::DEFINE. Returns list of det_ids."""
    det_ids = []
    for i in range(1, n + 1):
        did = store.insert_determination(
            pid, seq=i, file_path=FILE,
            define_name=DEFINE, node_fingerprint=f"fp{i}", edit_type="create",
        )
        det_ids.append(did)
    for i in range(len(det_ids) - 1):
        store.insert_gd_edge(pid, from_det=det_ids[i], to_det=det_ids[i + 1],
                             edge_class="version_chain")
    return det_ids


def _insert_failing_test_run(
    store: Store, pid: int, seq: int, classname: str, name: str
) -> int:
    run_id = store.insert_test_run(
        pid, seq=seq, source_path=f"/t/junit_{seq}.xml",
        source_mtime=f"2024-01-01T00:{seq:02d}:00Z",
        passed=0, failed=1, skipped=0,
    )
    store.insert_test_case(run_id, classname=classname, name=name,
                           file_path=None, status="failed")
    return run_id


def _insert_passing_test_run(
    store: Store, pid: int, seq: int, classname: str, name: str
) -> int:
    run_id = store.insert_test_run(
        pid, seq=seq, source_path=f"/t/junit_pass_{seq}.xml",
        source_mtime=f"2024-01-02T00:{seq:02d}:00Z",
        passed=1, failed=0, skipped=0,
    )
    store.insert_test_case(run_id, classname=classname, name=name,
                           file_path=None, status="passed")
    return run_id


def _open_incs(store: Store, pid: int, signal: str | None = None) -> list:
    incs = store.open_incidents(pid)
    if signal:
        incs = [i for i in incs if i["signal"] == signal]
    return incs


def _setup_basic(precise: bool = True):
    """Stuck chain + failing tests. Returns (store, pid, det_ids)."""
    store = _mem_store()
    pid = _project(store)
    det_ids = _stuck_chain(store, pid)

    if precise:
        store.insert_coverage_entry(pid, "tests.TestF::test_fn", "f.fn")
    # Failing test runs spanning the edit window (seq 1..N)
    for s in range(1, N + 1):
        _insert_failing_test_run(store, pid, seq=s,
                                 classname="tests.TestF", name="test_fn")
    return store, pid, det_ids


# ---------------------------------------------------------------------------
# Precise tier: coverage_map → define association
# ---------------------------------------------------------------------------

class TestDebugLoopPreciseTier:
    def test_fires_with_coverage_map_and_failing_test(self):
        store, pid, det_ids = _setup_basic(precise=True)
        affected = [(FILE, DEFINE, det_ids[-1])]
        signals.detect_debug_loop(store, pid, affected, ROOT, EMPTY_IDX)

        incs = _open_incs(store, pid, "debug_loop")
        assert len(incs) == 1
        assert incs[0]["target_node"] == f"{FILE}::{DEFINE}"

    def test_precise_tier_recorded_in_details(self):
        store, pid, det_ids = _setup_basic(precise=True)
        signals.detect_debug_loop(store, pid, [(FILE, DEFINE, det_ids[-1])], ROOT, EMPTY_IDX)

        details = json.loads(_open_incs(store, pid, "debug_loop")[0]["details"])
        assert details["test_tier"] == "precise"
        assert "tests.TestF::test_fn" in details["test_cases"]

    def test_no_trigger_when_test_passes(self):
        store = _mem_store()
        pid = _project(store)
        det_ids = _stuck_chain(store, pid)
        store.insert_coverage_entry(pid, "tests.TestF::test_fn", "f.fn")
        # Mostly failing but last one passes
        for s in range(1, N):
            _insert_failing_test_run(store, pid, seq=s,
                                     classname="tests.TestF", name="test_fn")
        _insert_passing_test_run(store, pid, seq=N,
                                 classname="tests.TestF", name="test_fn")

        signals.detect_debug_loop(store, pid, [(FILE, DEFINE, det_ids[-1])], ROOT, EMPTY_IDX)
        assert _open_incs(store, pid, "debug_loop") == []

    def test_no_trigger_when_no_test_runs_in_window(self):
        store = _mem_store()
        pid = _project(store)
        det_ids = _stuck_chain(store, pid)
        store.insert_coverage_entry(pid, "tests.TestF::test_fn", "f.fn")
        # Test run outside the edit window (seq >> N)
        _insert_failing_test_run(store, pid, seq=100,
                                 classname="tests.TestF", name="test_fn")

        signals.detect_debug_loop(store, pid, [(FILE, DEFINE, det_ids[-1])], ROOT, EMPTY_IDX)
        assert _open_incs(store, pid, "debug_loop") == []


# ---------------------------------------------------------------------------
# Heuristic tier: name-matching (§5.3 降级)
# ---------------------------------------------------------------------------

class TestDebugLoopHeuristicTier:
    def test_fires_on_name_match(self):
        store = _mem_store()
        pid = _project(store)
        det_ids = _stuck_chain(store, pid)
        # No coverage_map — name convention: test_fn ↔ fn
        for s in range(1, N + 1):
            _insert_failing_test_run(store, pid, seq=s,
                                     classname="tests.TestF", name="test_fn")

        signals.detect_debug_loop(store, pid, [(FILE, DEFINE, det_ids[-1])], ROOT, EMPTY_IDX)

        incs = _open_incs(store, pid, "debug_loop")
        assert len(incs) == 1

    def test_heuristic_tier_recorded_in_details(self):
        store = _mem_store()
        pid = _project(store)
        det_ids = _stuck_chain(store, pid)
        for s in range(1, N + 1):
            _insert_failing_test_run(store, pid, seq=s,
                                     classname="tests.TestF", name="test_fn")

        signals.detect_debug_loop(store, pid, [(FILE, DEFINE, det_ids[-1])], ROOT, EMPTY_IDX)

        details = json.loads(_open_incs(store, pid, "debug_loop")[0]["details"])
        assert details["test_tier"] == "heuristic"

    def test_no_match_no_trigger(self):
        store = _mem_store()
        pid = _project(store)
        det_ids = _stuck_chain(store, pid)
        for s in range(1, N + 1):
            _insert_failing_test_run(store, pid, seq=s,
                                     classname="tests.TestOther", name="test_completely_unrelated")

        signals.detect_debug_loop(store, pid, [(FILE, DEFINE, det_ids[-1])], ROOT, EMPTY_IDX)
        assert _open_incs(store, pid, "debug_loop") == []


# ---------------------------------------------------------------------------
# Structural gate
# ---------------------------------------------------------------------------

class TestDebugLoopStructuralGate:
    def test_not_stuck_no_trigger(self):
        store = _mem_store()
        pid = _project(store)
        # 3 modifications — below θ₁=5
        det_ids = []
        for i in range(1, 4):
            did = store.insert_determination(pid, seq=i, file_path=FILE,
                                             define_name=DEFINE, node_fingerprint=f"fp{i}",
                                             edit_type="create")
            det_ids.append(did)
        for i in range(len(det_ids) - 1):
            store.insert_gd_edge(pid, from_det=det_ids[i], to_det=det_ids[i + 1],
                                 edge_class="version_chain")

        store.insert_coverage_entry(pid, "tests.TestF::test_fn", "f.fn")
        for s in range(1, 4):
            _insert_failing_test_run(store, pid, seq=s,
                                     classname="tests.TestF", name="test_fn")

        signals.detect_debug_loop(store, pid, [(FILE, DEFINE, det_ids[-1])], ROOT, EMPTY_IDX)
        assert _open_incs(store, pid, "debug_loop") == []


# ---------------------------------------------------------------------------
# Upgrade exclusivity: debug_loop ↔ stuck_region
# ---------------------------------------------------------------------------

class TestExclusivity:
    def test_debug_loop_fires_not_stuck_region(self):
        store, pid, det_ids = _setup_basic(precise=True)
        affected = [(FILE, DEFINE, det_ids[-1])]
        signals.detect_debug_loop(store, pid, affected, ROOT, EMPTY_IDX)
        signals.detect_stuck_region(store, pid, affected, ROOT, EMPTY_IDX)

        assert len(_open_incs(store, pid, "debug_loop")) == 1
        assert len(_open_incs(store, pid, "stuck_region")) == 0

    def test_stuck_region_fires_when_no_tests(self):
        store = _mem_store()
        pid = _project(store)
        det_ids = _stuck_chain(store, pid)
        # No test data

        affected = [(FILE, DEFINE, det_ids[-1])]
        signals.detect_debug_loop(store, pid, affected, ROOT, EMPTY_IDX)
        signals.detect_stuck_region(store, pid, affected, ROOT, EMPTY_IDX)

        assert _open_incs(store, pid, "debug_loop") == []
        assert len(_open_incs(store, pid, "stuck_region")) == 1

    def test_stuck_region_fires_when_no_test_match(self):
        store = _mem_store()
        pid = _project(store)
        det_ids = _stuck_chain(store, pid)
        for s in range(1, N + 1):
            _insert_failing_test_run(store, pid, seq=s,
                                     classname="tests.X", name="test_completely_other")

        affected = [(FILE, DEFINE, det_ids[-1])]
        signals.detect_debug_loop(store, pid, affected, ROOT, EMPTY_IDX)
        signals.detect_stuck_region(store, pid, affected, ROOT, EMPTY_IDX)

        assert _open_incs(store, pid, "debug_loop") == []
        assert len(_open_incs(store, pid, "stuck_region")) == 1

    def test_idempotent_no_duplicate_incident(self):
        store, pid, det_ids = _setup_basic(precise=True)
        affected = [(FILE, DEFINE, det_ids[-1])]
        signals.detect_debug_loop(store, pid, affected, ROOT, EMPTY_IDX)
        signals.detect_debug_loop(store, pid, affected, ROOT, EMPTY_IDX)

        assert len(_open_incs(store, pid, "debug_loop")) == 1


# ---------------------------------------------------------------------------
# Root cause direction (§2.2 压测反思#1)
# ---------------------------------------------------------------------------

class TestRootCauseDirection:
    def test_details_contain_root_cause_note(self):
        store, pid, det_ids = _setup_basic(precise=True)
        signals.detect_debug_loop(store, pid, [(FILE, DEFINE, det_ids[-1])], ROOT, EMPTY_IDX)

        details = json.loads(_open_incs(store, pid, "debug_loop")[0]["details"])
        assert "root_cause_note" in details
        assert len(details["root_cause_note"]) > 0

    def test_root_cause_note_mentions_define(self):
        store, pid, det_ids = _setup_basic(precise=True)
        signals.detect_debug_loop(store, pid, [(FILE, DEFINE, det_ids[-1])], ROOT, EMPTY_IDX)

        details = json.loads(_open_incs(store, pid, "debug_loop")[0]["details"])
        assert DEFINE in details["root_cause_note"]

    def test_details_contain_chain_length_and_test_cases(self):
        store, pid, det_ids = _setup_basic(precise=True)
        signals.detect_debug_loop(store, pid, [(FILE, DEFINE, det_ids[-1])], ROOT, EMPTY_IDX)

        details = json.loads(_open_incs(store, pid, "debug_loop")[0]["details"])
        assert details["chain_length"] == N
        assert len(details["test_cases"]) >= 1


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

class TestDebugLoopStateMachine:
    def _setup(self):
        """Returns (store, pid) with an open debug_loop incident."""
        store, pid, det_ids = _setup_basic(precise=True)
        signals.detect_debug_loop(store, pid, [(FILE, DEFINE, det_ids[-1])], ROOT, EMPTY_IDX)
        return store, pid

    def test_open_to_notified_agent(self):
        store, pid = self._setup()
        # Tests still failing → recurrence on advance (open → notified_agent)
        _insert_failing_test_run(store, pid, seq=N + 1,
                                 classname="tests.TestF", name="test_fn")
        signals.advance_incidents(store, pid, [(FILE, DEFINE, 1)])
        inc = _open_incs(store, pid, "debug_loop")[0]
        assert inc["state"] == "notified_agent"

    def test_recurrence_increments_count(self):
        store, pid = self._setup()
        _insert_failing_test_run(store, pid, seq=N + 1,
                                 classname="tests.TestF", name="test_fn")
        signals.advance_incidents(store, pid, [])  # open → notified_agent

        _insert_failing_test_run(store, pid, seq=N + 2,
                                 classname="tests.TestF", name="test_fn")
        signals.advance_incidents(store, pid, [])  # recurrence: post_notify_count += 1

        inc = _open_incs(store, pid, "debug_loop")[0]
        assert inc["post_notify_count"] >= 1

    def test_resolve_when_test_passes(self):
        """Test turning green resolves with test_passed (§2.2 objective closure)."""
        store, pid = self._setup()
        _insert_failing_test_run(store, pid, seq=N + 1,
                                 classname="tests.TestF", name="test_fn")
        signals.advance_incidents(store, pid, [])  # → notified_agent

        # Test now passes
        _insert_passing_test_run(store, pid, seq=N + 2,
                                 classname="tests.TestF", name="test_fn")
        # N_STABLE=2 countdown
        signals.advance_incidents(store, pid, [])
        signals.advance_incidents(store, pid, [])

        all_incs = store.con.execute(
            "SELECT * FROM incidents WHERE project_id = ? AND signal = ?",
            (pid, "debug_loop"),
        ).fetchall()
        assert all(i["state"] == "resolved" for i in all_incs)
        assert all(i["resolved_by"] == "test_passed" for i in all_incs)

    def test_theta2_escalates_to_user(self):
        store, pid = self._setup()
        # Advance to notified first
        _insert_failing_test_run(store, pid, seq=N + 1,
                                 classname="tests.TestF", name="test_fn")
        signals.advance_incidents(store, pid, [])  # → notified_agent

        # Drive θ₂ recurrences
        for extra in range(N + 2, N + 2 + signals.THETA_2["debug_loop"]):
            _insert_failing_test_run(store, pid, seq=extra,
                                     classname="tests.TestF", name="test_fn")
            signals.advance_incidents(store, pid, [])

        open_incs = _open_incs(store, pid, "debug_loop")
        assert any(i["state"] == "escalated_user" for i in open_incs)
