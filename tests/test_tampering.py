"""Tests for test_tampering signal (§2.7).

Covers:
  - Fires when test define modified + production not modified + red→green
  - escalate_user_directly bypass: advance_incidents goes directly to escalated_user
  - Does NOT fire when production code was also modified (real fix case)
  - Does NOT fire when no test-path define is in affected
  - Does NOT fire when testcase always passing (no red→green flip)
  - Does NOT fire when testcase always failing (no prior pass)
  - Idempotent (no duplicate incidents)
  - Question wording
  - Complement with regression (mutual exclusion in typical case)
  - θ₂=1: after 1 post-notify recurrence in notified_agent, escalates user
  - Resolve with really_fixed when testcase stops passing
"""
import json

import pytest

from buer.store import Store
from buer import signals

ROOT = "/test"
FILE = "/test/f.py"           # production file
TEST_FILE = "/test/test_f.py" # test file (matches test_*.py → excluded)
DEFINE = "fn"
TEST_DEFINE = "test_fn"
CLASSNAME = "tests.TestF"
TC_NAME = "test_fn"
TC = f"{CLASSNAME}::{TC_NAME}"

EMPTY_IDX = __import__("buer.callgraph", fromlist=["SymbolIndex"]).SymbolIndex({}, {})


def _mem_store() -> Store:
    return Store(":memory:")


def _project(store: Store) -> int:
    return store.get_or_create_project(ROOT)


def _insert_run(store: Store, pid: int, seq: int, status: str) -> int:
    run_id = store.insert_test_run(
        pid, seq=seq, source_path=f"/t/junit_{seq}.xml",
        source_mtime=f"2024-01-01T00:{seq:02d}:00Z",
        passed=1 if status == "passed" else 0,
        failed=1 if status in ("failed", "error") else 0,
        skipped=0,
    )
    store.insert_test_case(run_id, classname=CLASSNAME, name=TC_NAME,
                           file_path=None, status=status)
    return run_id


def _test_define_affected(store: Store, pid: int, seq: int = 10):
    """Return affected list with a test-file define modification."""
    det_id = store.insert_determination(
        pid, seq=seq, file_path=TEST_FILE, define_name=TEST_DEFINE,
        node_fingerprint="fp_test_1", edit_type="modify",
    )
    return [(TEST_FILE, TEST_DEFINE, det_id)]


def _prod_define_affected(store: Store, pid: int, seq: int = 11):
    """Return affected list with a production-file define modification."""
    det_id = store.insert_determination(
        pid, seq=seq, file_path=FILE, define_name=DEFINE,
        node_fingerprint="fp_prod_1", edit_type="modify",
    )
    return [(FILE, DEFINE, det_id)]


def _open_incs(store: Store, pid: int) -> list:
    return [i for i in store.open_incidents(pid) if i["signal"] == "test_tampering"]


# ---------------------------------------------------------------------------
# Basic firing condition
# ---------------------------------------------------------------------------

class TestTamperingFires:
    def test_fires_when_test_define_modified(self):
        store = _mem_store()
        pid = _project(store)
        _insert_run(store, pid, 1, "failed")
        _insert_run(store, pid, 2, "passed")   # red→green

        affected = _test_define_affected(store, pid)
        signals.detect_test_tampering(store, pid, affected, ROOT, EMPTY_IDX)

        incs = _open_incs(store, pid)
        assert len(incs) == 1
        assert incs[0]["target_node"] == TC

    def test_details_contain_expected_fields(self):
        store = _mem_store()
        pid = _project(store)
        _insert_run(store, pid, 1, "failed")
        _insert_run(store, pid, 2, "passed")

        affected = _test_define_affected(store, pid)
        signals.detect_test_tampering(store, pid, affected, ROOT, EMPTY_IDX)

        details = json.loads(_open_incs(store, pid)[0]["details"])
        assert details["classname"] == CLASSNAME
        assert details["name"] == TC_NAME
        assert details["testcase"] == TC
        assert "test_define" in details
        assert details["escalate_user_directly"] is True

    def test_question_wording(self):
        store = _mem_store()
        pid = _project(store)
        _insert_run(store, pid, 1, "failed")
        _insert_run(store, pid, 2, "passed")

        affected = _test_define_affected(store, pid)
        signals.detect_test_tampering(store, pid, affected, ROOT, EMPTY_IDX)

        details = json.loads(_open_incs(store, pid)[0]["details"])
        assert "the change is to the test itself" in details["question"]
        assert "confirm the test change is correct?" in details["question"]

    def test_fires_with_error_to_passed_flip(self):
        store = _mem_store()
        pid = _project(store)
        _insert_run(store, pid, 1, "error")
        _insert_run(store, pid, 2, "passed")

        affected = _test_define_affected(store, pid)
        signals.detect_test_tampering(store, pid, affected, ROOT, EMPTY_IDX)

        assert len(_open_incs(store, pid)) == 1


# ---------------------------------------------------------------------------
# escalate_user_directly bypass
# ---------------------------------------------------------------------------

class TestEscalateDirectly:
    def test_first_advance_goes_to_escalated_user(self):
        store = _mem_store()
        pid = _project(store)
        _insert_run(store, pid, 1, "failed")
        _insert_run(store, pid, 2, "passed")

        affected = _test_define_affected(store, pid)
        signals.detect_test_tampering(store, pid, affected, ROOT, EMPTY_IDX)
        signals.advance_incidents(store, pid, affected)

        all_incs = store.con.execute(
            "SELECT * FROM incidents WHERE project_id=? AND signal=?",
            (pid, "test_tampering"),
        ).fetchall()
        assert len(all_incs) == 1
        assert all_incs[0]["state"] == "escalated_user"

    def test_never_passes_through_notified_agent(self):
        store = _mem_store()
        pid = _project(store)
        _insert_run(store, pid, 1, "failed")
        _insert_run(store, pid, 2, "passed")

        affected = _test_define_affected(store, pid)
        signals.detect_test_tampering(store, pid, affected, ROOT, EMPTY_IDX)
        signals.advance_incidents(store, pid, affected)

        row = store.con.execute(
            "SELECT agent_notified_at FROM incidents WHERE project_id=? AND signal=?",
            (pid, "test_tampering"),
        ).fetchone()
        assert row["agent_notified_at"] is None


# ---------------------------------------------------------------------------
# Non-firing conditions
# ---------------------------------------------------------------------------

class TestTamperingNoFire:
    def test_no_fire_when_prod_also_modified(self):
        """Production code was also changed → ambiguous, not tampering."""
        store = _mem_store()
        pid = _project(store)
        _insert_run(store, pid, 1, "failed")
        _insert_run(store, pid, 2, "passed")

        # Both test and production modified
        test_det = store.insert_determination(
            pid, seq=10, file_path=TEST_FILE, define_name=TEST_DEFINE,
            node_fingerprint="fp_t", edit_type="modify",
        )
        prod_det = store.insert_determination(
            pid, seq=11, file_path=FILE, define_name=DEFINE,
            node_fingerprint="fp_p", edit_type="modify",
        )
        # Add coverage entry so production define is linked to this testcase
        store.insert_coverage_entry(pid, TC, f"f.{DEFINE}")
        affected = [(TEST_FILE, TEST_DEFINE, test_det), (FILE, DEFINE, prod_det)]

        signals.detect_test_tampering(store, pid, affected, ROOT, EMPTY_IDX)
        assert _open_incs(store, pid) == []

    def test_no_fire_when_no_test_define_in_affected(self):
        """Only production code changed — no test-path define in affected."""
        store = _mem_store()
        pid = _project(store)
        _insert_run(store, pid, 1, "failed")
        _insert_run(store, pid, 2, "passed")

        affected = _prod_define_affected(store, pid)
        signals.detect_test_tampering(store, pid, affected, ROOT, EMPTY_IDX)
        assert _open_incs(store, pid) == []

    def test_no_fire_when_always_passing(self):
        """Test was always green — no red→green flip."""
        store = _mem_store()
        pid = _project(store)
        _insert_run(store, pid, 1, "passed")
        _insert_run(store, pid, 2, "passed")

        affected = _test_define_affected(store, pid)
        signals.detect_test_tampering(store, pid, affected, ROOT, EMPTY_IDX)
        assert _open_incs(store, pid) == []

    def test_no_fire_when_always_failing(self):
        """Test was always red — no prior pass to flip from."""
        store = _mem_store()
        pid = _project(store)
        _insert_run(store, pid, 1, "failed")
        _insert_run(store, pid, 2, "failed")

        affected = _test_define_affected(store, pid)
        signals.detect_test_tampering(store, pid, affected, ROOT, EMPTY_IDX)
        assert _open_incs(store, pid) == []

    def test_no_fire_when_only_one_run(self):
        """First-ever run — no prior history to compare against."""
        store = _mem_store()
        pid = _project(store)
        _insert_run(store, pid, 1, "passed")

        affected = _test_define_affected(store, pid)
        signals.detect_test_tampering(store, pid, affected, ROOT, EMPTY_IDX)
        assert _open_incs(store, pid) == []

    def test_no_fire_when_test_define_name_unrelated(self):
        """Test define name doesn't match the testcase."""
        store = _mem_store()
        pid = _project(store)
        _insert_run(store, pid, 1, "failed")
        _insert_run(store, pid, 2, "passed")

        det_id = store.insert_determination(
            pid, seq=10, file_path=TEST_FILE, define_name="setup_unrelated_fixture",
            node_fingerprint="fp_t", edit_type="modify",
        )
        affected = [(TEST_FILE, "setup_unrelated_fixture", det_id)]
        signals.detect_test_tampering(store, pid, affected, ROOT, EMPTY_IDX)
        assert _open_incs(store, pid) == []


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

class TestIdempotent:
    def test_no_duplicate_on_double_call(self):
        store = _mem_store()
        pid = _project(store)
        _insert_run(store, pid, 1, "failed")
        _insert_run(store, pid, 2, "passed")

        affected = _test_define_affected(store, pid)
        signals.detect_test_tampering(store, pid, affected, ROOT, EMPTY_IDX)
        signals.detect_test_tampering(store, pid, affected, ROOT, EMPTY_IDX)

        all_incs = store.con.execute(
            "SELECT * FROM incidents WHERE project_id=? AND signal=?",
            (pid, "test_tampering"),
        ).fetchall()
        assert len(all_incs) == 1


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

class TestTamperingStateMachine:
    def _setup(self):
        store = _mem_store()
        pid = _project(store)
        _insert_run(store, pid, 1, "failed")
        _insert_run(store, pid, 2, "passed")
        affected = _test_define_affected(store, pid)
        signals.detect_test_tampering(store, pid, affected, ROOT, EMPTY_IDX)
        return store, pid

    def test_resolve_when_prod_code_fixed_and_test_passes(self):
        """Production code modified + testcase stays green → resolved immediately (§4.3 state-based)."""
        store, pid = self._setup()
        signals.advance_incidents(store, pid, [])  # → escalated_user (bypass)

        # Later: production code is genuinely fixed; testcase stays green
        prod_affected = _prod_define_affected(store, pid, seq=11)
        _insert_run(store, pid, 3, "passed")
        signals.advance_incidents(store, pid, prod_affected)  # state confirmed → resolve immediately

        all_incs = store.con.execute(
            "SELECT * FROM incidents WHERE project_id=? AND signal=?",
            (pid, "test_tampering"),
        ).fetchall()
        assert all(i["state"] == "resolved" for i in all_incs)
        assert all(i["resolved_by"] == "really_fixed" for i in all_incs)

    def test_no_resolve_when_test_goes_red(self):
        """Test goes red (bug still present) → suspicion active → stays escalated_user."""
        store, pid = self._setup()
        signals.advance_incidents(store, pid, [])  # → escalated_user

        _insert_run(store, pid, 3, "failed")  # bug still there
        signals.advance_incidents(store, pid, [])
        signals.advance_incidents(store, pid, [])

        all_incs = store.con.execute(
            "SELECT * FROM incidents WHERE project_id=? AND signal=?",
            (pid, "test_tampering"),
        ).fetchall()
        assert all(i["state"] != "resolved" for i in all_incs)

    def test_no_resolve_when_test_still_passing_no_prod_fix(self):
        """Test stays green but no production code change → suspicion persists."""
        store, pid = self._setup()
        signals.advance_incidents(store, pid, [])  # → escalated_user

        _insert_run(store, pid, 3, "passed")  # still passing, production untouched
        signals.advance_incidents(store, pid, [])
        signals.advance_incidents(store, pid, [])

        all_incs = store.con.execute(
            "SELECT * FROM incidents WHERE project_id=? AND signal=?",
            (pid, "test_tampering"),
        ).fetchall()
        assert all(i["state"] != "resolved" for i in all_incs)

    def test_no_resolve_when_test_made_green_again_by_another_test_edit(self):
        """Test goes red then green via another test edit (no prod fix) → stays open."""
        store, pid = self._setup()
        signals.advance_incidents(store, pid, [])  # → escalated_user

        _insert_run(store, pid, 3, "failed")  # went red
        signals.advance_incidents(store, pid, [])

        _insert_run(store, pid, 4, "passed")  # green again — but test file still changed
        test_affected2 = _test_define_affected(store, pid, seq=20)
        signals.advance_incidents(store, pid, test_affected2)  # only test change → still suspicious
        signals.advance_incidents(store, pid, test_affected2)

        all_incs = store.con.execute(
            "SELECT * FROM incidents WHERE project_id=? AND signal=?",
            (pid, "test_tampering"),
        ).fetchall()
        assert all(i["state"] != "resolved" for i in all_incs)


# ---------------------------------------------------------------------------
# Complement with regression (§2.6 vs §2.7)
# ---------------------------------------------------------------------------

class TestRegressionComplementarity:
    def test_tampering_fires_not_regression_when_test_modified_only(self):
        """Test change → green: test_tampering, not regression."""
        store = _mem_store()
        pid = _project(store)
        _insert_run(store, pid, 1, "failed")
        _insert_run(store, pid, 2, "passed")

        affected = _test_define_affected(store, pid)
        signals.detect_regression(store, pid, affected, ROOT, EMPTY_IDX)
        signals.detect_test_tampering(store, pid, affected, ROOT, EMPTY_IDX)

        reg_incs = [i for i in store.open_incidents(pid) if i["signal"] == "regression"]
        tam_incs = _open_incs(store, pid)
        # regression requires production code change → no regression here
        assert reg_incs == []
        assert len(tam_incs) == 1

    def test_regression_fires_not_tampering_when_prod_modified_only(self):
        """Production change → red: regression, not test_tampering."""
        store = _mem_store()
        pid = _project(store)
        # Start green, go red (regression territory)
        _insert_run(store, pid, 1, "passed")
        _insert_run(store, pid, 2, "failed")

        affected = _prod_define_affected(store, pid)
        # Add coverage so production define is linked to testcase
        store.insert_coverage_entry(pid, TC, f"f.{DEFINE}")
        signals.detect_regression(store, pid, affected, ROOT, EMPTY_IDX)
        signals.detect_test_tampering(store, pid, affected, ROOT, EMPTY_IDX)

        reg_incs = [i for i in store.open_incidents(pid) if i["signal"] == "regression"]
        tam_incs = _open_incs(store, pid)
        assert len(reg_incs) == 1
        assert tam_incs == []
