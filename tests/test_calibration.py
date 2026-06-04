"""Unit tests for buer/calibration.py.

Coverage:
  1. Report contains all 9 signals
  2. token_waste rounds percentile correctness (known values)
  3. define_loop consec percentile correctness
  4. stuck_region chain_length percentile + threshold annotation
  5. Empty DB / zero incidents → no crash, shows "(no incidents recorded)"
  6. Single signal only → other signals show "(no incidents recorded)"
  7. Missing details field → no crash, detail_failures counted
  8. Read-only: incidents table unchanged before and after
  9. --project filter limits to correct project
  10. Sample-too-small warning (n < 20)
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from buer.store import Store
from buer.calibration import _build_report, _percentiles, main


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_store(tmp_path) -> tuple[Store, int]:
    db = str(tmp_path / "store.sqlite")
    store = Store(db)
    pid = store.get_or_create_project(str(tmp_path))
    return store, pid


def _insert_incident(store: Store, pid: int, signal: str,
                     state: str = "open", resolved_by: str | None = None,
                     post_notify_count: int = 0,
                     details: dict | None = None,
                     created_at: str = "2024-01-01 10:00:00",
                     updated_at: str = "2024-01-01 11:00:00") -> int:
    det_str = json.dumps(details) if details else None
    cur = store.con.execute(
        """INSERT INTO incidents
           (project_id, signal, target_node, state, post_notify_count,
            resolved_by, details, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (pid, signal, f"node_{signal}", state, post_notify_count,
         resolved_by, det_str, created_at, updated_at),
    )
    store.con.commit()
    return cur.lastrowid


# ── 1. Report contains all 9 signals ─────────────────────────────────────────

class TestAllSignalsPresent:
    def test_all_nine_signals_in_report(self, tmp_path):
        store, pid = _make_store(tmp_path)
        _insert_incident(store, pid, "stuck_region")
        _insert_incident(store, pid, "token_waste", details={"rounds": 20})
        report = _build_report(store, pid)
        for sig in [
            "stuck_region", "define_loop", "debug_loop", "regression",
            "test_tampering", "boundary_breach", "task_scope_breach",
            "token_waste", "dangling_reference",
        ]:
            assert f"Signal: {sig}" in report, f"Missing signal section: {sig}"

    def test_empty_db_no_crash(self, tmp_path):
        store, pid = _make_store(tmp_path)
        report = _build_report(store, pid)
        assert "no incidents recorded" in report

    def test_total_count_in_report(self, tmp_path):
        store, pid = _make_store(tmp_path)
        _insert_incident(store, pid, "stuck_region")
        _insert_incident(store, pid, "token_waste", details={"rounds": 18})
        report = _build_report(store, pid)
        assert "total incidents: 2" in report


# ── 2. token_waste rounds percentile correctness ──────────────────────────────

class TestTokenWastePercentiles:
    def test_median_correct(self, tmp_path):
        store, pid = _make_store(tmp_path)
        for rounds in [16, 17, 18, 19, 20]:
            _insert_incident(store, pid, "token_waste", details={"rounds": rounds})
        report = _build_report(store, pid)
        # median of [16,17,18,19,20] = 18
        assert "median=18" in report

    def test_threshold_annotation_present(self, tmp_path):
        store, pid = _make_store(tmp_path)
        _insert_incident(store, pid, "token_waste", details={"rounds": 20})
        report = _build_report(store, pid)
        assert "Θ_WASTE=15" in report

    def test_missing_rounds_no_crash(self, tmp_path):
        store, pid = _make_store(tmp_path)
        _insert_incident(store, pid, "token_waste", details={"other": "field"})
        report = _build_report(store, pid)
        assert "token_waste" in report  # didn't crash


# ── 3. define_loop consec percentile correctness ──────────────────────────────

class TestDefineLoopConsec:
    def test_consec_percentile(self, tmp_path):
        store, pid = _make_store(tmp_path)
        for c in [5, 6, 7, 8, 9]:
            _insert_incident(store, pid, "define_loop", details={"consec": c})
        report = _build_report(store, pid)
        # median of [5,6,7,8,9] = 7
        assert "median=7" in report

    def test_agent_user_threshold_annotation(self, tmp_path):
        store, pid = _make_store(tmp_path)
        _insert_incident(store, pid, "define_loop", details={"consec": 6})
        report = _build_report(store, pid)
        assert "N_AGENT" in report or "N_DEFINE_LOOP" in report or "agent=" in report


# ── 4. stuck_region chain_length + threshold ──────────────────────────────────

class TestStuckRegionChainLength:
    def test_chain_length_distribution(self, tmp_path):
        store, pid = _make_store(tmp_path)
        for cl in [5, 6, 8, 10, 12]:
            _insert_incident(store, pid, "stuck_region",
                             details={"chain_length": cl})
        report = _build_report(store, pid)
        # median of [5,6,8,10,12] = 8
        assert "median=8" in report

    def test_theta1_annotation(self, tmp_path):
        store, pid = _make_store(tmp_path)
        _insert_incident(store, pid, "stuck_region", details={"chain_length": 5})
        report = _build_report(store, pid)
        assert "Θ₁=5" in report


# ── 5. State distribution ─────────────────────────────────────────────────────

class TestStateDistribution:
    def test_state_counts_correct(self, tmp_path):
        store, pid = _make_store(tmp_path)
        _insert_incident(store, pid, "regression", state="open")
        _insert_incident(store, pid, "regression", state="resolved",
                         resolved_by="test_green")
        _insert_incident(store, pid, "regression", state="escalated_user")
        report = _build_report(store, pid)
        assert "open" in report
        assert "resolved" in report
        assert "escalated_user" in report

    def test_resolve_path_shown(self, tmp_path):
        store, pid = _make_store(tmp_path)
        _insert_incident(store, pid, "stuck_region", state="resolved",
                         resolved_by="test_green")
        _insert_incident(store, pid, "stuck_region", state="resolved",
                         resolved_by="test_green")
        _insert_incident(store, pid, "stuck_region", state="resolved",
                         resolved_by="no_more_equiv")
        report = _build_report(store, pid)
        assert "test_green" in report
        assert "no_more_equiv" in report

    def test_escalate_rate_shown(self, tmp_path):
        store, pid = _make_store(tmp_path)
        _insert_incident(store, pid, "test_tampering", state="escalated_user")
        _insert_incident(store, pid, "test_tampering", state="open")
        report = _build_report(store, pid)
        # 1/2 = 50%
        assert "50%" in report


# ── 6. Single signal → others show "no incidents" ────────────────────────────

class TestSingleSignal:
    def test_other_signals_no_incidents(self, tmp_path):
        store, pid = _make_store(tmp_path)
        _insert_incident(store, pid, "token_waste", details={"rounds": 20})
        report = _build_report(store, pid)
        # stuck_region has no incidents
        idx_stuck = report.index("Signal: stuck_region")
        idx_next = report.index("Signal: define_loop")
        stuck_section = report[idx_stuck:idx_next]
        assert "no incidents recorded" in stuck_section


# ── 7. Missing / unparseable details → no crash ───────────────────────────────

class TestDetailsParseFailure:
    def test_null_details_no_crash(self, tmp_path):
        store, pid = _make_store(tmp_path)
        store.con.execute(
            "INSERT INTO incidents (project_id, signal, target_node, state,"
            " post_notify_count, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (pid, "token_waste", "node", "open", 0,
             "2024-01-01 10:00:00", "2024-01-01 10:00:00"),
        )
        store.con.commit()
        report = _build_report(store, pid)
        assert "token_waste" in report

    def test_invalid_json_details_no_crash(self, tmp_path):
        store, pid = _make_store(tmp_path)
        store.con.execute(
            "INSERT INTO incidents (project_id, signal, target_node, state,"
            " post_notify_count, details, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (pid, "stuck_region", "node", "open", 0, "NOT_JSON",
             "2024-01-01 10:00:00", "2024-01-01 10:00:00"),
        )
        store.con.commit()
        report = _build_report(store, pid)
        assert "stuck_region" in report  # no crash


# ── 8. Read-only: incidents table unchanged ───────────────────────────────────

class TestReadOnly:
    def test_incidents_count_unchanged(self, tmp_path):
        store, pid = _make_store(tmp_path)
        for i in range(5):
            _insert_incident(store, pid, "regression", state="open")

        before = store.con.execute(
            "SELECT COUNT(*) AS n FROM incidents WHERE project_id=?", (pid,)
        ).fetchone()["n"]

        _build_report(store, pid)

        after = store.con.execute(
            "SELECT COUNT(*) AS n FROM incidents WHERE project_id=?", (pid,)
        ).fetchone()["n"]

        assert before == after == 5

    def test_incidents_content_unchanged(self, tmp_path):
        store, pid = _make_store(tmp_path)
        _insert_incident(store, pid, "stuck_region", details={"chain_length": 7})

        before_rows = store.con.execute(
            "SELECT id, signal, state, details FROM incidents WHERE project_id=?", (pid,)
        ).fetchall()

        _build_report(store, pid)

        after_rows = store.con.execute(
            "SELECT id, signal, state, details FROM incidents WHERE project_id=?", (pid,)
        ).fetchall()

        assert [(r["id"], r["signal"], r["state"], r["details"]) for r in before_rows] == \
               [(r["id"], r["signal"], r["state"], r["details"]) for r in after_rows]


# ── 9. --project filter ───────────────────────────────────────────────────────

class TestProjectFilter:
    def test_filter_limits_to_project(self, tmp_path):
        store, pid1 = _make_store(tmp_path)
        pid2 = store.get_or_create_project(str(tmp_path / "other"))
        _insert_incident(store, pid1, "stuck_region")
        _insert_incident(store, pid2, "token_waste", details={"rounds": 20})

        report = _build_report(store, pid1)
        # pid1 has stuck_region with 1 incident, token_waste with 0
        assert f"project_id={pid1}" in report
        assert "total incidents: 1" in report

    def test_no_project_aggregates_all(self, tmp_path):
        store, pid1 = _make_store(tmp_path)
        pid2 = store.get_or_create_project(str(tmp_path / "other"))
        _insert_incident(store, pid1, "stuck_region")
        _insert_incident(store, pid2, "token_waste", details={"rounds": 20})

        report = _build_report(store, None)
        assert "total incidents: 2" in report
        assert "all projects" in report


# ── 10. Sample-too-small warning ─────────────────────────────────────────────

class TestSmallSampleWarning:
    def test_warning_shown_for_n_lt_20(self, tmp_path):
        store, pid = _make_store(tmp_path)
        for _ in range(5):
            _insert_incident(store, pid, "regression")
        report = _build_report(store, pid)
        assert "sample too small" in report

    def test_no_warning_for_n_ge_20(self, tmp_path):
        store, pid = _make_store(tmp_path)
        for _ in range(20):
            _insert_incident(store, pid, "regression")
        report = _build_report(store, pid)
        # the specific regression section should NOT have the sample warning
        idx = report.index("Signal: regression")
        idx_end = report.find("Signal:", idx + 1)
        section = report[idx:idx_end] if idx_end != -1 else report[idx:]
        assert "sample too small" not in section


# ── _percentiles unit tests ───────────────────────────────────────────────────

class TestPercentiles:
    def test_known_values(self):
        p = _percentiles([10.0, 20.0, 30.0, 40.0, 50.0])
        assert p["min"] == 10.0
        assert p["median"] == 30.0
        assert p["max"] == 50.0

    def test_empty_returns_na(self):
        p = _percentiles([])
        assert p["median"] == "n/a"

    def test_single_value(self):
        p = _percentiles([42.0])
        assert p["min"] == p["median"] == p["max"] == 42.0


# ── CLI main() ────────────────────────────────────────────────────────────────

class TestCLIMain:
    def test_main_runs_without_error(self, tmp_path, capsys):
        store, pid = _make_store(tmp_path)
        _insert_incident(store, pid, "stuck_region", details={"chain_length": 6})
        store.con.close()
        db_path = str(tmp_path / "store.sqlite")
        main(["--db", db_path])
        out = capsys.readouterr().out
        assert "Calibration Report" in out
        assert "stuck_region" in out

    def test_main_project_flag(self, tmp_path, capsys):
        root = str(tmp_path)
        store, pid = _make_store(tmp_path)
        _insert_incident(store, pid, "regression")
        store.con.close()
        db_path = str(tmp_path / "store.sqlite")
        main(["--db", db_path, "--project", root])
        out = capsys.readouterr().out
        assert f"project_id={pid}" in out
