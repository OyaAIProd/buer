"""Tests for stop_handler decision:block response format (Claude Code HTTP hook spec).

Coverage:
  1. deliveries present → {"decision":"block","reason":...} with prefix + messages
  2. stop_hook_active=true → {} (allow, anti-loop)
  3. no deliveries → {} (allow)
  4. no pid / no cwd → {} (allow)
  5. test_tampering delivery → enters block reason correctly
  6. multiple deliveries joined with \\n\\n
  7. reason prefix明示 BUER 自动检测，不是用户拒绝
"""
from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient
from unittest.mock import MagicMock

from buer.mcp.server import _set_store_for_testing, mcp
from buer.store import Store


def _mock_store(
    *,
    pid: int | None = 1,
    user_deliveries: list | None = None,
    drain_result: list | None = None,
) -> MagicMock:
    store = MagicMock(spec=Store)
    store.find_project_for_file.return_value = pid
    store.take_user_deliveries.return_value = user_deliveries or []
    store.take_agent_deliveries.return_value = []
    store.drain_recompute_queue.return_value = drain_result or []
    store.pending_recompute_count.return_value = 0
    return store


def _client(store: MagicMock) -> TestClient:
    _set_store_for_testing(store)
    return TestClient(mcp.streamable_http_app(), raise_server_exceptions=True)


_STOP = {"stop_hook_active": False, "cwd": "/proj", "session_id": "s1"}
_STOP_ACTIVE = {"stop_hook_active": True, "cwd": "/proj", "session_id": "s1"}


# ── 1. decision:block when deliveries exist ───────────────────────────────────

class TestDecisionBlock:
    def test_returns_decision_block(self):
        store = _mock_store(user_deliveries=[{"message": "regression: fn1 broke test_fn1"}])
        r = _client(store).post("/buer/stop", json=_STOP)
        assert r.status_code == 200
        body = r.json()
        assert body["decision"] == "block"

    def test_reason_contains_delivery_message(self):
        msg = "regression: fn1 broke test_fn1"
        store = _mock_store(user_deliveries=[{"message": msg}])
        r = _client(store).post("/buer/stop", json=_STOP)
        assert msg in r.json()["reason"]

    def test_reason_has_buer_prefix(self):
        store = _mock_store(user_deliveries=[{"message": "some alert"}])
        r = _client(store).post("/buer/stop", json=_STOP)
        reason = r.json()["reason"]
        assert reason.startswith("[BUER]")

    def test_reason_not_user_rejection(self):
        """Prefix must clarify this is BUER auto-detection, not a user-initiated block."""
        store = _mock_store(user_deliveries=[{"message": "alert"}])
        reason = _client(store).post("/buer/stop", json=_STOP).json()["reason"]
        assert "不是用户拒绝" in reason
        assert "自动检测" in reason

    def test_content_type_is_json(self):
        store = _mock_store(user_deliveries=[{"message": "x"}])
        r = _client(store).post("/buer/stop", json=_STOP)
        assert "application/json" in r.headers["content-type"]


# ── 2. anti-loop: stop_hook_active=true → {} (allow) ─────────────────────────

class TestAntiLoopAllow:
    def test_stop_hook_active_returns_allow(self):
        store = _mock_store(user_deliveries=[{"message": "should not block"}])
        r = _client(store).post("/buer/stop", json=_STOP_ACTIVE)
        assert r.status_code == 200
        assert r.json() == {}

    def test_stop_hook_active_no_decision_key(self):
        store = _mock_store(user_deliveries=[{"message": "msg"}])
        body = _client(store).post("/buer/stop", json=_STOP_ACTIVE).json()
        assert "decision" not in body

    def test_stop_hook_active_does_not_consume_deliveries(self):
        store = _mock_store(user_deliveries=[{"message": "pending"}])
        _client(store).post("/buer/stop", json=_STOP_ACTIVE)
        store.take_user_deliveries.assert_not_called()


# ── 3. no deliveries → {} (allow) ────────────────────────────────────────────

class TestNoDeliveries:
    def test_empty_deliveries_returns_allow(self):
        store = _mock_store(user_deliveries=[])
        r = _client(store).post("/buer/stop", json=_STOP)
        assert r.json() == {}

    def test_no_decision_key_when_allow(self):
        store = _mock_store(user_deliveries=[])
        body = _client(store).post("/buer/stop", json=_STOP).json()
        assert "decision" not in body


# ── 4. no pid / no cwd → {} (allow) ─────────────────────────────────────────

class TestNoPidNoCwd:
    def test_no_cwd_returns_allow(self):
        store = _mock_store()
        r = _client(store).post("/buer/stop", json={"stop_hook_active": False})
        assert r.json() == {}

    def test_no_pid_returns_allow(self):
        store = _mock_store(pid=None)
        r = _client(store).post("/buer/stop", json=_STOP)
        assert r.json() == {}

    def test_no_pid_no_drain(self):
        store = _mock_store(pid=None)
        _client(store).post("/buer/stop", json=_STOP)
        store.drain_recompute_queue.assert_not_called()


# ── 5. test_tampering delivery in block reason ────────────────────────────────

class TestTamperingDelivery:
    def test_tampering_message_in_block_reason(self):
        msg = (
            "test_tampering: test_login changed from failed→passed after test file edit; "
            "confirm the test change is correct?"
        )
        store = _mock_store(user_deliveries=[{"message": msg}])
        r = _client(store).post("/buer/stop", json=_STOP)
        body = r.json()
        assert body["decision"] == "block"
        assert "test_tampering" in body["reason"]
        assert "test_login" in body["reason"]


# ── 6. multiple deliveries joined ────────────────────────────────────────────

class TestMultipleDeliveries:
    def test_all_messages_in_reason(self):
        store = _mock_store(user_deliveries=[
            {"message": "first alert"},
            {"message": "second alert"},
            {"message": "third alert"},
        ])
        reason = _client(store).post("/buer/stop", json=_STOP).json()["reason"]
        assert "first alert" in reason
        assert "second alert" in reason
        assert "third alert" in reason

    def test_messages_separated_by_double_newline(self):
        store = _mock_store(user_deliveries=[
            {"message": "A"},
            {"message": "B"},
        ])
        reason = _client(store).post("/buer/stop", json=_STOP).json()["reason"]
        assert "A\n\nB" in reason
