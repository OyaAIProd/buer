"""Tests for regression signal (§2.6 — testcase green→red caused by production code change).

Covers:
  - Green→red with affected production define → regression fires
  - Always-red (never passed) → no regression
  - Test file changed (excluded path) → no regression
  - No test association → no regression
  - Idempotent (no duplicate incidents)
  - Question-mark wording in details
  - State machine: open → notified_agent → escalated_user
  - Resolve: test recovers (test_recovered)
  - regression and debug_loop don't double-fire on the same testcase
"""
import json

import pytest

from buer.store import Store
from buer import signals

ROOT = "/test"
FILE = "/test/src/auth.py"           # production code (not excluded)
TEST_FILE = "/test/tests/test_auth.py"  # excluded path
DEFINE = "login"
TC_CLASS = "tests.test_auth"
TC_NAME = "test_login"
TC_ID = f"{TC_CLASS}::{TC_NAME}"
EMPTY_IDX = __import__("buer.callgraph", fromlist=["SymbolIndex"]).SymbolIndex({}, {})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mem_store() -> Store:
    return Store(":memory:")


def _project(store: Store) -> int:
    return store.get_or_create_project(ROOT)


def _insert_run(store, pid, seq, classname, name, status, src="junit.xml"):
    run_id = store.insert_test_run(
        pid, seq=seq, source_path=f"/t/{src}_{seq}.xml",
        source_mtime=f"2024-01-01T{seq:02d}:00:00Z",
        passed=(1 if status == "passed" else 0),
        failed=(1 if status in ("failed", "error") else 0),
        skipped=0,
    )
    store.insert_test_case(run_id, classname=classname, name=name,
                           file_path=None, status=status)
    return run_id


def _insert_det(store, pid, seq, file_path=FILE, define=DEFINE, fp=None):
    return store.insert_determination(
        pid, seq=seq, file_path=file_path,
        define_name=define, node_fingerprint=fp or f"fp{seq}",
        edit_type="modify",
    )


def _open_incs(store, pid, signal=None):
    incs = store.open_incidents(pid)
    if signal:
        incs = [i for i in incs if i["signal"] == signal]
    return incs


# ---------------------------------------------------------------------------
# Core detection
# ---------------------------------------------------------------------------

class TestDetectRegression:
    def test_fires_on_green_to_red_with_affected_define(self):
        store = _mem_store()
        pid = _project(store)
        # Prior passing run
        _insert_run(store, pid, seq=1, classname=TC_CLASS, name=TC_NAME, status="passed")
        # Coverage map: tc → define
        store.insert_coverage_entry(pid, TC_ID, f"src/auth.{DEFINE}")
        # Production define modified
        det_id = _insert_det(store, pid, seq=2)
        # Now failing
        _insert_run(store, pid, seq=2, classname=TC_CLASS, name=TC_NAME, status="failed")

        signals.detect_regression(store, pid, [(FILE, DEFINE, det_id)], ROOT, EMPTY_IDX)

        incs = _open_incs(store, pid, "regression")
        assert len(incs) == 1
        assert incs[0]["target_node"] == TC_ID

    def test_heuristic_tier_name_match(self):
        """No coverage map — testcase name matches define via heuristic (test_login ↔ login)."""
        store = _mem_store()
        pid = _project(store)
        _insert_run(store, pid, seq=1, classname=TC_CLASS, name="test_login", status="passed")
        det_id = _insert_det(store, pid, seq=2)
        _insert_run(store, pid, seq=2, classname=TC_CLASS, name="test_login", status="failed")

        signals.detect_regression(store, pid, [(FILE, "login", det_id)], ROOT, EMPTY_IDX)
        assert len(_open_incs(store, pid, "regression")) == 1

    def test_no_trigger_when_always_red(self):
        """Test was never passing — no regression."""
        store = _mem_store()
        pid = _project(store)
        store.insert_coverage_entry(pid, TC_ID, f"src/auth.{DEFINE}")
        det_id = _insert_det(store, pid, seq=1)
        # Only failing run, never passed
        _insert_run(store, pid, seq=1, classname=TC_CLASS, name=TC_NAME, status="failed")
        _insert_run(store, pid, seq=2, classname=TC_CLASS, name=TC_NAME, status="failed")

        signals.detect_regression(store, pid, [(FILE, DEFINE, det_id)], ROOT, EMPTY_IDX)
        assert _open_incs(store, pid, "regression") == []

    def test_no_trigger_when_still_passing(self):
        store = _mem_store()
        pid = _project(store)
        store.insert_coverage_entry(pid, TC_ID, f"src/auth.{DEFINE}")
        det_id = _insert_det(store, pid, seq=1)
        _insert_run(store, pid, seq=1, classname=TC_CLASS, name=TC_NAME, status="passed")
        _insert_run(store, pid, seq=2, classname=TC_CLASS, name=TC_NAME, status="passed")

        signals.detect_regression(store, pid, [(FILE, DEFINE, det_id)], ROOT, EMPTY_IDX)
        assert _open_incs(store, pid, "regression") == []

    def test_no_trigger_for_test_file_change(self):
        """Test file (excluded path) edited → no regression (§2.7 territory)."""
        store = _mem_store()
        pid = _project(store)
        store.insert_coverage_entry(pid, TC_ID, f"src/auth.{DEFINE}")
        # The affected define is IN THE TEST FILE, not production code
        det_id = _insert_det(store, pid, seq=1, file_path=TEST_FILE, define=TC_NAME)
        _insert_run(store, pid, seq=0, classname=TC_CLASS, name=TC_NAME, status="passed")
        _insert_run(store, pid, seq=1, classname=TC_CLASS, name=TC_NAME, status="failed")

        signals.detect_regression(store, pid, [(TEST_FILE, TC_NAME, det_id)], ROOT, EMPTY_IDX)
        assert _open_incs(store, pid, "regression") == []

    def test_no_trigger_without_test_association(self):
        """Production define changed but no test case matches → no regression."""
        store = _mem_store()
        pid = _project(store)
        det_id = _insert_det(store, pid, seq=1)
        _insert_run(store, pid, seq=0, classname="unrelated.TestX", name="test_xyz", status="passed")
        _insert_run(store, pid, seq=1, classname="unrelated.TestX", name="test_xyz", status="failed")

        signals.detect_regression(store, pid, [(FILE, DEFINE, det_id)], ROOT, EMPTY_IDX)
        assert _open_incs(store, pid, "regression") == []

    def test_first_ever_run_no_trigger(self):
        """Only one run, first time — no prior pass → no regression."""
        store = _mem_store()
        pid = _project(store)
        store.insert_coverage_entry(pid, TC_ID, f"src/auth.{DEFINE}")
        det_id = _insert_det(store, pid, seq=1)
        _insert_run(store, pid, seq=1, classname=TC_CLASS, name=TC_NAME, status="failed")

        signals.detect_regression(store, pid, [(FILE, DEFINE, det_id)], ROOT, EMPTY_IDX)
        assert _open_incs(store, pid, "regression") == []

    def test_idempotent(self):
        store = _mem_store()
        pid = _project(store)
        store.insert_coverage_entry(pid, TC_ID, f"src/auth.{DEFINE}")
        det_id = _insert_det(store, pid, seq=2)
        _insert_run(store, pid, seq=1, classname=TC_CLASS, name=TC_NAME, status="passed")
        _insert_run(store, pid, seq=2, classname=TC_CLASS, name=TC_NAME, status="failed")
        affected = [(FILE, DEFINE, det_id)]

        signals.detect_regression(store, pid, affected, ROOT, EMPTY_IDX)
        signals.detect_regression(store, pid, affected, ROOT, EMPTY_IDX)

        assert len(_open_incs(store, pid, "regression")) == 1


# ---------------------------------------------------------------------------
# Question-mark wording (§2.6)
# ---------------------------------------------------------------------------

class TestRegressionWording:
    def test_details_contain_question(self):
        store = _mem_store()
        pid = _project(store)
        store.insert_coverage_entry(pid, TC_ID, f"src/auth.{DEFINE}")
        det_id = _insert_det(store, pid, seq=2)
        _insert_run(store, pid, seq=1, classname=TC_CLASS, name=TC_NAME, status="passed")
        _insert_run(store, pid, seq=2, classname=TC_CLASS, name=TC_NAME, status="failed")

        signals.detect_regression(store, pid, [(FILE, DEFINE, det_id)], ROOT, EMPTY_IDX)
        details = json.loads(_open_incs(store, pid, "regression")[0]["details"])
        assert "question" in details
        assert "is this expected?" in details["question"]

    def test_question_mentions_testcase_name(self):
        store = _mem_store()
        pid = _project(store)
        store.insert_coverage_entry(pid, TC_ID, f"src/auth.{DEFINE}")
        det_id = _insert_det(store, pid, seq=2)
        _insert_run(store, pid, seq=1, classname=TC_CLASS, name=TC_NAME, status="passed")
        _insert_run(store, pid, seq=2, classname=TC_CLASS, name=TC_NAME, status="failed")

        signals.detect_regression(store, pid, [(FILE, DEFINE, det_id)], ROOT, EMPTY_IDX)
        details = json.loads(_open_incs(store, pid, "regression")[0]["details"])
        assert TC_NAME in details["question"]

    def test_details_record_affected_define(self):
        store = _mem_store()
        pid = _project(store)
        store.insert_coverage_entry(pid, TC_ID, f"src/auth.{DEFINE}")
        det_id = _insert_det(store, pid, seq=2)
        _insert_run(store, pid, seq=1, classname=TC_CLASS, name=TC_NAME, status="passed")
        _insert_run(store, pid, seq=2, classname=TC_CLASS, name=TC_NAME, status="failed")

        signals.detect_regression(store, pid, [(FILE, DEFINE, det_id)], ROOT, EMPTY_IDX)
        details = json.loads(_open_incs(store, pid, "regression")[0]["details"])
        assert DEFINE in details["affected_define"]

    def test_question_wording_contains_no_assertion(self):
        """Wording must be a question, not an assertion like '你改错了'."""
        store = _mem_store()
        pid = _project(store)
        store.insert_coverage_entry(pid, TC_ID, f"src/auth.{DEFINE}")
        det_id = _insert_det(store, pid, seq=2)
        _insert_run(store, pid, seq=1, classname=TC_CLASS, name=TC_NAME, status="passed")
        _insert_run(store, pid, seq=2, classname=TC_CLASS, name=TC_NAME, status="failed")

        signals.detect_regression(store, pid, [(FILE, DEFINE, det_id)], ROOT, EMPTY_IDX)
        details = json.loads(_open_incs(store, pid, "regression")[0]["details"])
        question = details["question"]
        assert "你改错了" not in question
        assert "？" in question or "?" in question or "是预期的吗" in question


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

def _setup_regression(store=None, pid=None):
    """Returns (store, pid) with one open regression incident."""
    if store is None:
        store = _mem_store()
        pid = _project(store)
    store.insert_coverage_entry(pid, TC_ID, f"src/auth.{DEFINE}")
    det_id = _insert_det(store, pid, seq=2)
    _insert_run(store, pid, seq=1, classname=TC_CLASS, name=TC_NAME, status="passed")
    _insert_run(store, pid, seq=2, classname=TC_CLASS, name=TC_NAME, status="failed")
    signals.detect_regression(store, pid, [(FILE, DEFINE, det_id)], ROOT, EMPTY_IDX)
    return store, pid


class TestRegressionStateMachine:
    def test_open_to_notified_when_still_failing(self):
        store, pid = _setup_regression()
        # Still failing → recurrence → open → notified_agent
        signals.advance_incidents(store, pid, [])
        inc = _open_incs(store, pid, "regression")[0]
        assert inc["state"] == "notified_agent"

    def test_theta2_escalates_to_user(self):
        store, pid = _setup_regression()
        signals.advance_incidents(store, pid, [])  # → notified_agent
        for _ in range(signals.THETA_2["regression"]):
            signals.advance_incidents(store, pid, [])
        open_incs = _open_incs(store, pid, "regression")
        assert any(i["state"] == "escalated_user" for i in open_incs)

    def test_resolve_when_test_recovers(self):
        """Test passing again resolves with test_recovered (§2.6 objective closure)."""
        store, pid = _setup_regression()
        signals.advance_incidents(store, pid, [])  # → notified_agent

        # Test recovers: insert a passing run (latest)
        _insert_run(store, pid, seq=3, classname=TC_CLASS, name=TC_NAME, status="passed")
        # N_STABLE=2 countdown
        signals.advance_incidents(store, pid, [])
        signals.advance_incidents(store, pid, [])

        all_incs = store.con.execute(
            "SELECT * FROM incidents WHERE project_id = ? AND signal = ?",
            (pid, "regression"),
        ).fetchall()
        assert all(i["state"] == "resolved" for i in all_incs)
        assert all(i["resolved_by"] == "test_recovered" for i in all_incs)

    def test_recurrence_while_still_failing(self):
        store, pid = _setup_regression()
        signals.advance_incidents(store, pid, [])  # → notified_agent
        signals.advance_incidents(store, pid, [])  # recurrence
        inc = _open_incs(store, pid, "regression")[0]
        assert inc["post_notify_count"] >= 1

    def test_no_resolve_with_single_stable_round(self):
        """N_STABLE=2: one stable round is not enough to resolve."""
        store, pid = _setup_regression()
        signals.advance_incidents(store, pid, [])  # open → notified

        _insert_run(store, pid, seq=3, classname=TC_CLASS, name=TC_NAME, status="passed")
        signals.advance_incidents(store, pid, [])  # stable=1 (not enough)

        inc = _open_incs(store, pid, "regression")[0]
        assert inc["state"] != "resolved"

    def test_error_status_counts_as_recurrence(self):
        """Test status 'error' is treated same as 'failed'."""
        store, pid = _setup_regression()
        _insert_run(store, pid, seq=3, classname=TC_CLASS, name=TC_NAME, status="error")
        signals.advance_incidents(store, pid, [])  # should still recur
        inc = _open_incs(store, pid, "regression")[0]
        assert inc["state"] == "notified_agent"


# ---------------------------------------------------------------------------
# Interaction: regression ↔ debug_loop (should not double-fire on same testcase)
# ---------------------------------------------------------------------------

class TestRegressionVsDebugLoop:
    def test_regression_and_debug_loop_mutually_exclusive_on_testcase(self):
        """debug_loop = always failing; regression = green→red. Mutually exclusive."""
        store = _mem_store()
        pid = _project(store)

        # Setup: define modified 5 times (stuck)
        det_ids = []
        for i in range(1, 6):
            did = store.insert_determination(
                pid, seq=i, file_path=FILE,
                define_name=DEFINE, node_fingerprint=f"fp{i}", edit_type="modify",
            )
            det_ids.append(did)
        for i in range(len(det_ids) - 1):
            store.insert_gd_edge(pid, from_det=det_ids[i], to_det=det_ids[i + 1],
                                 edge_class="version_chain")

        # Test case: always failing (debug_loop scenario)
        store.insert_coverage_entry(pid, TC_ID, f"src/auth.{DEFINE}")
        for i in range(1, 6):
            _insert_run(store, pid, seq=i, classname=TC_CLASS, name=TC_NAME, status="failed")

        signals.detect_debug_loop(store, pid, [(FILE, DEFINE, det_ids[-1])], ROOT, EMPTY_IDX)
        signals.detect_regression(store, pid, [(FILE, DEFINE, det_ids[-1])], ROOT, EMPTY_IDX)

        debug_incs = _open_incs(store, pid, "debug_loop")
        regr_incs = _open_incs(store, pid, "regression")

        assert len(debug_incs) == 1
        # regression should NOT fire: test was never green → _is_green_to_red = False
        assert len(regr_incs) == 0

    def test_regression_fires_debug_loop_does_not_for_green_to_red(self):
        """Green→red: regression fires; debug_loop doesn't (test was passing before)."""
        store = _mem_store()
        pid = _project(store)

        # Just 2 modifications (below θ₁=5 → no stuck_region / debug_loop)
        det_ids = []
        for i in range(1, 3):
            did = store.insert_determination(
                pid, seq=i, file_path=FILE,
                define_name=DEFINE, node_fingerprint=f"fp{i}", edit_type="modify",
            )
            det_ids.append(did)
        store.insert_gd_edge(pid, det_ids[0], det_ids[1], "version_chain")

        store.insert_coverage_entry(pid, TC_ID, f"src/auth.{DEFINE}")
        _insert_run(store, pid, seq=1, classname=TC_CLASS, name=TC_NAME, status="passed")
        _insert_run(store, pid, seq=2, classname=TC_CLASS, name=TC_NAME, status="failed")

        signals.detect_debug_loop(store, pid, [(FILE, DEFINE, det_ids[-1])], ROOT, EMPTY_IDX)
        signals.detect_regression(store, pid, [(FILE, DEFINE, det_ids[-1])], ROOT, EMPTY_IDX)

        assert len(_open_incs(store, pid, "debug_loop")) == 0
        assert len(_open_incs(store, pid, "regression")) == 1
