"""Tests for delivery kind dimension (alert vs suggestion).

Coverage:
  1. stop_handler calls take_user_deliveries with kinds=('alert',)
  2. stop_handler: alert present → decision:block
  3. stop_handler: suggestion-only → allow ({})
  4. stop_handler: suggestion does not block even when alert absent
  5. session_start_handler calls take_user_deliveries twice (alert + suggestion)
  6. session_start_handler: pending alert → alert_prefix present
  7. session_start_handler: pending suggestion → suggestion_suffix appended
  8. session_start_handler: no pending → neither prefix nor suffix
  9. session_start_handler: both pending → prefix + suffix both present
 10. suggestion_suffix follows overview (not prepended as urgent)
 11. enqueue_delivery default kind is 'alert'
 12. enqueue_delivery kind='suggestion' stored correctly
 13. take_user_deliveries(kinds=('alert',)) returns only alerts
 14. take_user_deliveries(kinds=('suggestion',)) returns only suggestions
 15. take_user_deliveries(kinds=None) returns all
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest
from starlette.testclient import TestClient

from buer.mcp.server import _set_store_for_testing, mcp
from buer.store import Store


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_real_store(tmp_path) -> tuple[Store, int]:
    db = str(tmp_path / "store.sqlite")
    store = Store(db)
    pid = store.get_or_create_project(str(tmp_path))
    return store, pid


def _mock_store_split(
    *,
    pid: int | None = 1,
    has_gd: bool = True,
    alert_deliveries: list | None = None,
    suggestion_deliveries: list | None = None,
) -> MagicMock:
    """Mock where take_user_deliveries returns different values per kinds kwarg."""
    store = MagicMock(spec=Store)
    store.find_project_for_file.return_value = pid
    store.has_gd_edges.return_value = has_gd
    store.take_agent_deliveries.return_value = []
    store.drain_recompute_queue.return_value = []
    store.pending_recompute_count.return_value = 0

    alerts = alert_deliveries or []
    suggestions = suggestion_deliveries or []

    def _take(project_id, kinds=None):
        if kinds == ("alert",):
            return alerts
        if kinds == ("suggestion",):
            return suggestions
        return alerts + suggestions

    store.take_user_deliveries.side_effect = _take
    return store


def _mock_store_simple(
    *,
    pid: int | None = 1,
    user_deliveries: list | None = None,
) -> MagicMock:
    """Simple mock: take_user_deliveries always returns the same list."""
    store = MagicMock(spec=Store)
    store.find_project_for_file.return_value = pid
    store.take_user_deliveries.return_value = user_deliveries or []
    store.take_agent_deliveries.return_value = []
    store.drain_recompute_queue.return_value = []
    store.pending_recompute_count.return_value = 0
    return store


def _http_client(store: MagicMock) -> TestClient:
    _set_store_for_testing(store)
    return TestClient(mcp.streamable_http_app(), raise_server_exceptions=True)


_STOP = {"stop_hook_active": False, "cwd": "/proj", "session_id": "s1"}
_START = {"source": "startup", "cwd": "/proj", "session_id": "s1"}

_OVERVIEW_PATCH = (
    __import__("unittest.mock", fromlist=["patch"]).patch(
        "buer.mcp.server.health.project_overview", return_value="[BUER] overview"
    ),
    __import__("unittest.mock", fromlist=["patch"]).patch(
        "buer.mcp.server.health.coarse_structure_map", return_value=""
    ),
)


# ── 1-4. stop_handler kind filtering ─────────────────────────────────────────

class TestStopHandlerKind:
    def test_calls_take_user_deliveries_with_alert_kinds(self):
        store = _mock_store_simple(user_deliveries=[])
        _http_client(store).post("/buer/stop", json=_STOP)
        store.take_user_deliveries.assert_called_once_with(1, kinds=("alert",))

    def test_alert_delivery_blocks(self):
        store = _mock_store_split(alert_deliveries=[{"message": "regression alert"}])
        r = _http_client(store).post("/buer/stop", json=_STOP)
        assert r.json()["decision"] == "block"

    def test_suggestion_only_allows(self):
        store = _mock_store_split(
            alert_deliveries=[],
            suggestion_deliveries=[{"message": "commit your changes now"}],
        )
        r = _http_client(store).post("/buer/stop", json=_STOP)
        assert r.json() == {}

    def test_suggestion_does_not_appear_in_block_reason(self):
        store = _mock_store_split(
            alert_deliveries=[{"message": "regression alert"}],
            suggestion_deliveries=[{"message": "commit suggestion"}],
        )
        r = _http_client(store).post("/buer/stop", json=_STOP)
        reason = r.json()["reason"]
        assert "regression alert" in reason
        # suggestions are not taken by stop_handler — they stay for SessionStart
        assert "commit suggestion" not in reason


# ── 5-10. session_start_handler kind routing ──────────────────────────────────

class TestSessionStartKind:
    def test_take_called_twice_different_kinds(self):
        store = _mock_store_split(has_gd=False)
        _http_client(store).post("/buer/session-start", json=_START)
        calls = store.take_user_deliveries.call_args_list
        kinds_args = [c.kwargs.get("kinds") or (c.args[1] if len(c.args) > 1 else None)
                      for c in calls]
        assert ("alert",) in kinds_args
        assert ("suggestion",) in kinds_args

    def test_pending_alert_produces_prefix(self):
        store = _mock_store_split(alert_deliveries=[{"message": "stuck_region fire"}])
        with _OVERVIEW_PATCH[0], _OVERVIEW_PATCH[1]:
            r = _http_client(store).post("/buer/session-start", json=_START)
        assert "上一轮结束时有未处理的检测告警" in r.text
        assert "stuck_region fire" in r.text

    def test_pending_suggestion_produces_suffix(self):
        store = _mock_store_split(
            suggestion_deliveries=[{"message": "consider committing your changes"}]
        )
        with _OVERVIEW_PATCH[0], _OVERVIEW_PATCH[1]:
            r = _http_client(store).post("/buer/session-start", json=_START)
        assert "上一轮遗留建议" in r.text
        assert "consider committing your changes" in r.text

    def test_no_pending_no_prefix_no_suffix(self):
        store = _mock_store_split()
        with _OVERVIEW_PATCH[0], _OVERVIEW_PATCH[1]:
            r = _http_client(store).post("/buer/session-start", json=_START)
        assert "上一轮" not in r.text

    def test_both_alert_and_suggestion_present(self):
        store = _mock_store_split(
            alert_deliveries=[{"message": "regression: fn broke test"}],
            suggestion_deliveries=[{"message": "commit hint"}],
        )
        with _OVERVIEW_PATCH[0], _OVERVIEW_PATCH[1]:
            r = _http_client(store).post("/buer/session-start", json=_START)
        assert "上一轮结束时有未处理的检测告警" in r.text
        assert "regression: fn broke test" in r.text
        assert "上一轮遗留建议" in r.text
        assert "commit hint" in r.text

    def test_suggestion_suffix_comes_after_overview(self):
        store = _mock_store_split(
            suggestion_deliveries=[{"message": "commit hint"}]
        )
        with _OVERVIEW_PATCH[0], _OVERVIEW_PATCH[1]:
            r = _http_client(store).post("/buer/session-start", json=_START)
        overview_pos = r.text.index("[BUER] overview")
        suffix_pos = r.text.index("上一轮遗留建议")
        assert overview_pos < suffix_pos

    def test_alert_prefix_comes_before_overview(self):
        store = _mock_store_split(
            alert_deliveries=[{"message": "critical alert"}]
        )
        with _OVERVIEW_PATCH[0], _OVERVIEW_PATCH[1]:
            r = _http_client(store).post("/buer/session-start", json=_START)
        prefix_pos = r.text.index("上一轮结束时有未处理")
        overview_pos = r.text.index("[BUER] overview")
        assert prefix_pos < overview_pos


# ── 11-15. Store.enqueue_delivery + take_user_deliveries kind filter ──────────

class TestStoreKindFilter:
    def test_default_kind_is_alert(self, tmp_path):
        store, pid = _make_real_store(tmp_path)
        store.enqueue_delivery(pid, None, "user", "some alert")
        rows = store.con.execute(
            "SELECT kind FROM pending_deliveries WHERE project_id=?", (pid,)
        ).fetchall()
        assert rows[0]["kind"] == "alert"

    def test_explicit_kind_suggestion_stored(self, tmp_path):
        store, pid = _make_real_store(tmp_path)
        store.enqueue_delivery(pid, None, "user", "commit hint", kind="suggestion")
        rows = store.con.execute(
            "SELECT kind FROM pending_deliveries WHERE project_id=?", (pid,)
        ).fetchall()
        assert rows[0]["kind"] == "suggestion"

    def test_take_alert_kinds_returns_only_alerts(self, tmp_path):
        store, pid = _make_real_store(tmp_path)
        store.enqueue_delivery(pid, None, "user", "alert msg", kind="alert")
        store.enqueue_delivery(pid, None, "user", "suggest msg", kind="suggestion")
        taken = store.take_user_deliveries(pid, kinds=("alert",))
        assert len(taken) == 1
        assert taken[0]["message"] == "alert msg"

    def test_take_suggestion_kinds_returns_only_suggestions(self, tmp_path):
        store, pid = _make_real_store(tmp_path)
        store.enqueue_delivery(pid, None, "user", "alert msg", kind="alert")
        store.enqueue_delivery(pid, None, "user", "suggest msg", kind="suggestion")
        taken = store.take_user_deliveries(pid, kinds=("suggestion",))
        assert len(taken) == 1
        assert taken[0]["message"] == "suggest msg"

    def test_take_no_kinds_filter_returns_all(self, tmp_path):
        store, pid = _make_real_store(tmp_path)
        store.enqueue_delivery(pid, None, "user", "alert msg", kind="alert")
        store.enqueue_delivery(pid, None, "user", "suggest msg", kind="suggestion")
        taken = store.take_user_deliveries(pid)
        assert len(taken) == 2

    def test_taken_alert_not_returned_by_suggestion_take(self, tmp_path):
        store, pid = _make_real_store(tmp_path)
        store.enqueue_delivery(pid, None, "user", "alert msg", kind="alert")
        # take alert first
        alerts = store.take_user_deliveries(pid, kinds=("alert",))
        assert len(alerts) == 1
        # now take suggestion — should be empty
        suggestions = store.take_user_deliveries(pid, kinds=("suggestion",))
        assert len(suggestions) == 0

    def test_kind_column_present_in_schema(self, tmp_path):
        store, pid = _make_real_store(tmp_path)
        cols = [row[1] for row in store.con.execute(
            "PRAGMA table_info(pending_deliveries)"
        ).fetchall()]
        assert "kind" in cols
