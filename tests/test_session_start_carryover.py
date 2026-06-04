"""Tests for session_start_handler carry-over alert injection.

When Stop hook didn't fire last session (abnormal exit), pending user-channel
alerts accumulate. SessionStart drains them at the start of the new session
so they're never silently lost.

Coverage:
  1. pending delivery + graph → content starts with alert prefix, followed by overview
  2. no pending → content equals original overview (backward compatible, no prefix)
  3. no gd_edges + pending → alert prefix + placeholder; queue cleared after take
  4. deliver-once: second SessionStart sees no prefix (queue already drained)
  5. integrity alert (test_tampering user delivery) correctly forwarded
  6. multiple pending messages joined with \\n\\n in prefix
  7. no pid → no take_user_deliveries call, no prefix (pid-gated guard)
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from buer.mcp.server import _set_store_for_testing, mcp
from buer.store import Store


def _ac(r) -> str:
    """Extract additionalContext from hook JSON, or '' when body is {}."""
    return r.json().get("hookSpecificOutput", {}).get("additionalContext", "")


# ── helpers ───────────────────────────────────────────────────────────────────

def _mock_store(
    *,
    pid: int | None = 1,
    has_gd: bool = True,
    user_deliveries: list | None = None,
    drain_result: list | None = None,
) -> MagicMock:
    store = MagicMock(spec=Store)
    store.find_project_for_file.return_value = pid
    store.has_gd_edges.return_value = has_gd
    store.take_user_deliveries.return_value = user_deliveries or []
    store.take_agent_deliveries.return_value = []
    store.drain_recompute_queue.return_value = drain_result or []
    store.pending_recompute_count.return_value = 0
    return store


def _client(store: MagicMock) -> TestClient:
    _set_store_for_testing(store)
    return TestClient(mcp.streamable_http_app(), raise_server_exceptions=True)


_START = {"source": "startup", "cwd": "/proj", "session_id": "s1"}

_OVERVIEW_PATCH = (
    patch("buer.mcp.server.health.project_overview", return_value="[BUER] overview"),
    patch("buer.mcp.server.health.coarse_structure_map", return_value=""),
)


# ── 1. pending + graph → prefix + overview ───────────────────────────────────

class TestPendingWithGraph:
    def test_content_starts_with_alert_prefix(self):
        store = _mock_store(user_deliveries=[{"message": "regression: fn broke test"}])
        with _OVERVIEW_PATCH[0], _OVERVIEW_PATCH[1]:
            r = _client(store).post("/buer/session-start", json=_START)
        assert r.status_code == 200
        assert _ac(r).startswith("[BUER] 上一轮结束时有未处理的检测告警")

    def test_overview_still_present_after_prefix(self):
        store = _mock_store(user_deliveries=[{"message": "some alert"}])
        with _OVERVIEW_PATCH[0], _OVERVIEW_PATCH[1]:
            r = _client(store).post("/buer/session-start", json=_START)
        assert "[BUER] overview" in _ac(r)

    def test_message_in_prefix(self):
        msg = "regression: fn broke test_fn"
        store = _mock_store(user_deliveries=[{"message": msg}])
        with _OVERVIEW_PATCH[0], _OVERVIEW_PATCH[1]:
            r = _client(store).post("/buer/session-start", json=_START)
        assert msg in _ac(r)

    def test_prefix_clarifies_not_user_rejection(self):
        store = _mock_store(user_deliveries=[{"message": "alert"}])
        with _OVERVIEW_PATCH[0], _OVERVIEW_PATCH[1]:
            r = _client(store).post("/buer/session-start", json=_START)
        assert "补送" in _ac(r)  # carry-over framing, not block framing


# ── 2. no pending → backward compatible, no prefix ───────────────────────────

class TestNoPendingBackwardCompat:
    def test_no_prefix_when_no_deliveries(self):
        store = _mock_store(user_deliveries=[])
        with _OVERVIEW_PATCH[0], _OVERVIEW_PATCH[1]:
            r = _client(store).post("/buer/session-start", json=_START)
        assert not _ac(r).startswith("[BUER] 上一轮")
        assert "[BUER] overview" in _ac(r)

    def test_text_equals_overview_exactly(self):
        store = _mock_store(user_deliveries=[])
        with _OVERVIEW_PATCH[0], _OVERVIEW_PATCH[1]:
            r = _client(store).post("/buer/session-start", json=_START)
        assert _ac(r) == "[BUER] overview"


# ── 3. no gd_edges + pending → prefix + placeholder; queue cleared ────────────

class TestPendingNoGraph:
    def test_prefix_present_without_graph(self):
        store = _mock_store(has_gd=False,
                            user_deliveries=[{"message": "stuck_region: fn looping"}])
        r = _client(store).post("/buer/session-start", json=_START)
        assert "[BUER] 上一轮结束时有未处理的检测告警" in _ac(r)
        assert "stuck_region: fn looping" in _ac(r)

    def test_take_called_even_without_graph(self):
        store = _mock_store(has_gd=False,
                            user_deliveries=[{"message": "alert"}])
        _client(store).post("/buer/session-start", json=_START)
        # called twice: once for alert-kind, once for suggestion-kind
        assert store.take_user_deliveries.call_count == 2

    def test_no_graph_no_pending_returns_empty(self):
        store = _mock_store(has_gd=False, user_deliveries=[])
        r = _client(store).post("/buer/session-start", json=_START)
        assert _ac(r) == ""


# ── 4. deliver-once: second SessionStart sees no prefix ───────────────────────

class TestDeliverOnce:
    def test_second_call_returns_no_prefix(self):
        """Simulate take() clearing the queue: second call gets empty deliveries."""
        deliveries = [{"message": "regression alert"}]

        call_count = 0

        def take_once(_pid, kinds=None):
            nonlocal call_count
            call_count += 1
            return deliveries if call_count == 1 else []

        store = _mock_store(has_gd=True)
        store.take_user_deliveries.side_effect = take_once

        with _OVERVIEW_PATCH[0], _OVERVIEW_PATCH[1]:
            r1 = _client(store).post("/buer/session-start", json=_START)
            r2 = _client(store).post("/buer/session-start", json=_START)

        assert "上一轮结束时有未处理" in _ac(r1)
        assert "上一轮结束时有未处理" not in _ac(r2)


# ── 5. integrity alert (test_tampering) correctly forwarded ──────────────────

class TestIntegrityAlertForwarded:
    def test_tampering_alert_in_prefix(self):
        msg = (
            "test_tampering: test_login 改为通过后检测到可疑，"
            "请确认测试改动是否正确？"
        )
        store = _mock_store(user_deliveries=[{"message": msg}])
        with _OVERVIEW_PATCH[0], _OVERVIEW_PATCH[1]:
            r = _client(store).post("/buer/session-start", json=_START)
        assert "test_tampering" in _ac(r)
        assert "test_login" in _ac(r)

    def test_tampering_alert_precedes_overview(self):
        msg = "test_tampering: suspicious"
        store = _mock_store(user_deliveries=[{"message": msg}])
        with _OVERVIEW_PATCH[0], _OVERVIEW_PATCH[1]:
            r = _client(store).post("/buer/session-start", json=_START)
        prefix_pos = _ac(r).index("上一轮结束时")
        overview_pos = _ac(r).index("[BUER] overview")
        assert prefix_pos < overview_pos


# ── 6. multiple messages joined ───────────────────────────────────────────────

class TestMultipleMessages:
    def test_all_messages_present(self):
        store = _mock_store(user_deliveries=[
            {"message": "alert A"},
            {"message": "alert B"},
            {"message": "alert C"},
        ])
        with _OVERVIEW_PATCH[0], _OVERVIEW_PATCH[1]:
            r = _client(store).post("/buer/session-start", json=_START)
        assert "alert A" in _ac(r)
        assert "alert B" in _ac(r)
        assert "alert C" in _ac(r)

    def test_messages_separated_by_double_newline(self):
        store = _mock_store(user_deliveries=[
            {"message": "X"},
            {"message": "Y"},
        ])
        with _OVERVIEW_PATCH[0], _OVERVIEW_PATCH[1]:
            r = _client(store).post("/buer/session-start", json=_START)
        assert "X\n\nY" in _ac(r)


# ── 7. no pid → take not called ──────────────────────────────────────────────

class TestNoPidGate:
    def test_no_take_when_pid_none(self):
        store = _mock_store(pid=None)
        _client(store).post("/buer/session-start", json=_START)
        store.take_user_deliveries.assert_not_called()

    def test_no_cwd_no_take(self):
        store = _mock_store()
        _client(store).post("/buer/session-start", json={"source": "startup"})
        store.take_user_deliveries.assert_not_called()
