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
from buer.signals import _test_define_matches_testcase

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
        """Production code also changed inside window, linked to tc → not tampering."""
        store = _mem_store()
        pid = _project(store)
        _insert_run(store, pid, 1, "failed")

        # prod define at seq=2 — inside window (1, 2]; coverage links it to TC (precise tier)
        prod_det = store.insert_determination(
            pid, seq=2, file_path=FILE, define_name=DEFINE,
            node_fingerprint="fp_p", edit_type="modify",
        )
        store.insert_coverage_entry(pid, TC, f"f.{DEFINE}")

        _insert_run(store, pid, 2, "passed")

        test_det = store.insert_determination(
            pid, seq=10, file_path=TEST_FILE, define_name=TEST_DEFINE,
            node_fingerprint="fp_t", edit_type="modify",
        )
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


# ---------------------------------------------------------------------------
# Condition 3 window semantics (§2.7 false-positive prevention)
# ---------------------------------------------------------------------------

class TestWindowCondition3:
    def test_fires_when_no_prod_in_window(self):
        """Zero production changes in window → most reliable tampering signal; fires."""
        store = _mem_store()
        pid = _project(store)
        _insert_run(store, pid, 1, "failed")
        _insert_run(store, pid, 2, "passed")
        # No production determinations anywhere in window (1, 2]
        affected = _test_define_affected(store, pid)
        signals.detect_test_tampering(store, pid, affected, ROOT, EMPTY_IDX)
        assert len(_open_incs(store, pid)) == 1

    def test_no_fire_cross_round_prod_linked_to_tc(self):
        """Prod change in a prior cycle inside window, linked to tc → not tampering."""
        store = _mem_store()
        pid = _project(store)
        _insert_run(store, pid, 1, "failed")

        # Prod change at seq=5 — between red (1) and green (10), inside window
        store.insert_determination(pid, seq=5, file_path=FILE, define_name=DEFINE,
                                   node_fingerprint="fp_p", edit_type="modify")
        store.insert_coverage_entry(pid, TC, f"f.{DEFINE}")

        _insert_run(store, pid, 10, "passed")

        affected = _test_define_affected(store, pid, seq=10)
        signals.detect_test_tampering(store, pid, affected, ROOT, EMPTY_IDX)
        assert _open_incs(store, pid) == []

    def test_no_fire_prod_in_window_no_coverage(self):
        """Prod define in window but tier=none → unreliable, cannot exclude coverage → no fire."""
        store = _mem_store()
        pid = _project(store)
        _insert_run(store, pid, 1, "failed")
        # Prod change with no coverage entry → tier will be 'none'
        store.insert_determination(pid, seq=2, file_path=FILE, define_name="unrelated_fn",
                                   node_fingerprint="fp_p", edit_type="modify")
        _insert_run(store, pid, 2, "passed")

        affected = _test_define_affected(store, pid)
        signals.detect_test_tampering(store, pid, affected, ROOT, EMPTY_IDX)
        assert _open_incs(store, pid) == []

    def test_fires_prod_precise_unlinked_to_tc(self):
        """All prod defines in window are precise-tier but cover a different tc → fire."""
        store = _mem_store()
        pid = _project(store)

        OTHER_CLS, OTHER_NAME = "tests.TestG", "test_g"
        OTHER_TC = f"{OTHER_CLS}::{OTHER_NAME}"

        run1 = store.insert_test_run(pid, seq=1, source_path="/t/r1.xml",
                                     source_mtime="2024-01-01T00:01:00Z",
                                     passed=0, failed=2, skipped=0)
        store.insert_test_case(run1, classname=CLASSNAME, name=TC_NAME,
                               file_path=None, status="failed")
        store.insert_test_case(run1, classname=OTHER_CLS, name=OTHER_NAME,
                               file_path=None, status="passed")

        # Prod define covers OTHER_TC (not TC) — precise tier because OTHER_TC has history
        store.insert_determination(pid, seq=2, file_path=FILE, define_name=DEFINE,
                                   node_fingerprint="fp_p", edit_type="modify")
        store.insert_coverage_entry(pid, OTHER_TC, f"f.{DEFINE}")

        run2 = store.insert_test_run(pid, seq=2, source_path="/t/r2.xml",
                                     source_mtime="2024-01-01T00:02:00Z",
                                     passed=2, failed=0, skipped=0)
        store.insert_test_case(run2, classname=CLASSNAME, name=TC_NAME,
                               file_path=None, status="passed")
        store.insert_test_case(run2, classname=OTHER_CLS, name=OTHER_NAME,
                               file_path=None, status="passed")

        affected = _test_define_affected(store, pid)
        signals.detect_test_tampering(store, pid, affected, ROOT, EMPTY_IDX)
        assert len(_open_incs(store, pid)) == 1


# ---------------------------------------------------------------------------
# _test_define_matches_testcase — unit tests (Bug 3 fix)
# ---------------------------------------------------------------------------

class TestDefineMatchesTestcase:
    def test_class_method_matches(self):
        """Bug 3: qualified class-method define matches its testcase."""
        assert _test_define_matches_testcase(
            "TestJWT.test_decodes_valid_jwt",
            "tests.test_api_jwt.TestJWT",
            "test_decodes_valid_jwt",
        ) is True

    def test_cross_class_same_name_no_match(self):
        """TestA.test_init must NOT match TestB::test_init (cross-class collision guard)."""
        assert _test_define_matches_testcase(
            "TestA.test_init",
            "tests.TestB",
            "test_init",
        ) is False

    def test_module_level_function_matches(self):
        """Module-level test function: define name == testcase name."""
        assert _test_define_matches_testcase(
            "test_decodes_valid_jwt",
            "tests.test_api_jwt",
            "test_decodes_valid_jwt",
        ) is True

    def test_define_is_test_class_matches(self):
        """Define is the test class itself (rare but valid)."""
        assert _test_define_matches_testcase(
            "TestJWT",
            "tests.test_api_jwt.TestJWT",
            "test_decodes_valid_jwt",
        ) is True


# ---------------------------------------------------------------------------
# Reconcile path: exclude_tests=False lets test-file defines reach affected
# (validates the server.py fix — without it test_tampering never fires in prod)
# ---------------------------------------------------------------------------

class TestReconcilePath:
    def test_reconcile_with_exclude_tests_false_builds_affected(self, tmp_path):
        """reconcile(exclude_tests=False) on a test file records determinations."""
        import textwrap
        from buer.reconcile import reconcile as do_reconcile

        test_file = tmp_path / "test_foo.py"
        test_file.write_text(textwrap.dedent("""
            def test_bar():
                assert 1 == 1
        """))

        store = _mem_store()
        pid = store.get_or_create_project(str(tmp_path))
        do_reconcile(store, pid, [str(test_file)], exclude_tests=False)

        dets = store.con.execute(
            "SELECT define_name FROM determinations WHERE project_id=?", (pid,)
        ).fetchall()
        names = {r["define_name"] for r in dets}
        assert "test_bar" in names

    def test_reconcile_with_exclude_tests_true_skips_test_file(self, tmp_path):
        """Default exclude_tests=True silently skips test files (baseline guard)."""
        import textwrap
        from buer.reconcile import reconcile as do_reconcile

        test_file = tmp_path / "test_foo.py"
        test_file.write_text(textwrap.dedent("""
            def test_bar():
                assert 1 == 1
        """))

        store = _mem_store()
        pid = store.get_or_create_project(str(tmp_path))
        do_reconcile(store, pid, [str(test_file)])  # default exclude_tests=True

        dets = store.con.execute(
            "SELECT define_name FROM determinations WHERE project_id=?", (pid,)
        ).fetchall()
        names = {r["define_name"] for r in dets}
        assert "test_bar" not in names


# ---------------------------------------------------------------------------
# Vitest/jest file-level fallback (盲区 D fix)
# Vitest describe/it blocks are anonymous callbacks → 0 defines extracted.
# detect_test_tampering must fire via file-level match when edited_files is provided.
# ---------------------------------------------------------------------------

# Vitest test file (absolute path, .test.ts suffix → parse.is_test_file = True)
VITEST_ROOT = "/vtest"
VITEST_FILE = "/vtest/src/auth.test.ts"
VITEST_CLS  = "auth > login"          # vitest classname format
VITEST_NAME = "should reject bad password"
VITEST_TC   = f"{VITEST_CLS}::{VITEST_NAME}"
VITEST_PROD = "/vtest/src/auth.ts"    # production file


def _vitest_run(store: Store, pid: int, seq: int, status: str) -> None:
    """Insert a test run whose single testcase has file_path set (vitest pattern)."""
    run_id = store.insert_test_run(
        pid, seq=seq, source_path=f"/vtest/junit_{seq}.xml",
        source_mtime=f"2024-01-01T00:{seq:02d}:00Z",
        passed=1 if status == "passed" else 0,
        failed=1 if status in ("failed", "error") else 0,
        skipped=0,
    )
    store.insert_test_case(
        run_id, classname=VITEST_CLS, name=VITEST_NAME,
        file_path=VITEST_FILE,   # ← vitest reports absolute file_path
        status=status,
    )


class TestVitestFileLevelFallback:
    """Vitest/jest: no defines in affected, but edited_files carries the test file."""

    def test_fires_via_file_level_when_no_defines(self):
        """Core case: vitest test edited (0 defines) → file-level match → fires."""
        store = _mem_store()
        pid = store.get_or_create_project(VITEST_ROOT)
        _vitest_run(store, pid, 1, "failed")
        _vitest_run(store, pid, 2, "passed")   # red→green

        # affected is EMPTY — vitest file has no defines
        signals.detect_test_tampering(
            store, pid, affected=[], root=VITEST_ROOT, idx=EMPTY_IDX,
            edited_files=[VITEST_FILE],
        )

        incs = [i for i in store.open_incidents(pid) if i["signal"] == "test_tampering"]
        assert len(incs) == 1
        assert incs[0]["target_node"] == VITEST_TC

    def test_details_has_test_define_set(self):
        """test_define field is present (set to file path for file-level match)."""
        store = _mem_store()
        pid = store.get_or_create_project(VITEST_ROOT)
        _vitest_run(store, pid, 1, "failed")
        _vitest_run(store, pid, 2, "passed")

        signals.detect_test_tampering(
            store, pid, affected=[], root=VITEST_ROOT, idx=EMPTY_IDX,
            edited_files=[VITEST_FILE],
        )

        incs = [i for i in store.open_incidents(pid) if i["signal"] == "test_tampering"]
        details = json.loads(incs[0]["details"])
        assert "test_define" in details
        assert details["escalate_user_directly"] is True

    def test_no_fire_when_test_file_not_in_edited(self):
        """edited_files doesn't contain the test file → quick exit, no incident."""
        store = _mem_store()
        pid = store.get_or_create_project(VITEST_ROOT)
        _vitest_run(store, pid, 1, "failed")
        _vitest_run(store, pid, 2, "passed")

        # Different file in edited_files — not the test file for this testcase
        other = "/vtest/src/other.test.ts"
        signals.detect_test_tampering(
            store, pid, affected=[], root=VITEST_ROOT, idx=EMPTY_IDX,
            edited_files=[other],
        )

        incs = [i for i in store.open_incidents(pid) if i["signal"] == "test_tampering"]
        assert incs == []

    def test_no_fire_when_edited_files_empty(self):
        """No edited_files supplied (default) and no defines → quick exit."""
        store = _mem_store()
        pid = store.get_or_create_project(VITEST_ROOT)
        _vitest_run(store, pid, 1, "failed")
        _vitest_run(store, pid, 2, "passed")

        signals.detect_test_tampering(
            store, pid, affected=[], root=VITEST_ROOT, idx=EMPTY_IDX,
            # edited_files not passed → default ()
        )

        incs = [i for i in store.open_incidents(pid) if i["signal"] == "test_tampering"]
        assert incs == []

    def test_condition3_blocks_when_prod_modified(self):
        """File-level match BUT production code also changed inside window → condition 3 blocks."""
        store = _mem_store()
        pid = store.get_or_create_project(VITEST_ROOT)
        _vitest_run(store, pid, 1, "failed")

        # Production change at seq=2 (inside window), no coverage → tier=none → blocks
        store.insert_determination(
            pid, seq=2, file_path=VITEST_PROD, define_name="authenticate",
            node_fingerprint="fp_prod", edit_type="modify",
        )
        _vitest_run(store, pid, 2, "passed")

        # No coverage entry → tier=none → should_fire=False (cannot exclude coverage)
        signals.detect_test_tampering(
            store, pid, affected=[], root=VITEST_ROOT, idx=EMPTY_IDX,
            edited_files=[VITEST_FILE],
        )

        incs = [i for i in store.open_incidents(pid) if i["signal"] == "test_tampering"]
        assert incs == []

    def test_pytest_define_level_unaffected(self):
        """Existing pytest define-level path still works alongside file-level."""
        store = _mem_store()
        pid = _project(store)     # ROOT = "/test"
        _insert_run(store, pid, 1, "failed")
        _insert_run(store, pid, 2, "passed")

        affected = _test_define_affected(store, pid)
        # Pass edited_files too — define-level should take precedence
        signals.detect_test_tampering(
            store, pid, affected=affected, root=ROOT, idx=EMPTY_IDX,
            edited_files=[TEST_FILE],
        )

        incs = _open_incs(store, pid)
        assert len(incs) == 1
        details = json.loads(incs[0]["details"])
        # define-level match: test_define should be "file::define", not just file path
        assert "::" in details["test_define"]

    def test_path_normalization_abs_edited_rel_tc(self):
        """testcase.file_path relative, edited_files absolute → still matches."""
        store = _mem_store()
        pid = store.get_or_create_project(VITEST_ROOT)

        # testcase has a root-relative file_path (as some JUnit reporters emit)
        run_id = store.insert_test_run(
            pid, seq=1, source_path="/vtest/j1.xml",
            source_mtime="2024-01-01T00:01:00Z", passed=0, failed=1, skipped=0,
        )
        store.insert_test_case(
            run_id, classname=VITEST_CLS, name=VITEST_NAME,
            file_path="src/auth.test.ts",   # ← relative path
            status="failed",
        )
        run_id2 = store.insert_test_run(
            pid, seq=2, source_path="/vtest/j2.xml",
            source_mtime="2024-01-01T00:02:00Z", passed=1, failed=0, skipped=0,
        )
        store.insert_test_case(
            run_id2, classname=VITEST_CLS, name=VITEST_NAME,
            file_path="src/auth.test.ts",
            status="passed",
        )

        # edited_files has ABSOLUTE path
        signals.detect_test_tampering(
            store, pid, affected=[], root=VITEST_ROOT, idx=EMPTY_IDX,
            edited_files=[VITEST_FILE],   # "/vtest/src/auth.test.ts"
        )

        incs = [i for i in store.open_incidents(pid) if i["signal"] == "test_tampering"]
        assert len(incs) == 1
