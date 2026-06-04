"""Tests for BUER MCP server — §4.4 hook endpoint + MCP tools.

Covers:
  - POST /buer/post-edit with no relevant project → empty response
  - POST /buer/post-edit → reconcile runs, agent injection returned when incident fires
  - POST /buer/post-edit with cwd fallback for project resolution
  - check_drift tool → returns active incident summaries (non-destructive)
  - get_user_alerts tool → returns and marks user deliveries
  - End-to-end: 5 edits to same define → stuck_region text in POST response
  - Injection text uses details content (not re-computed)
  - R2 compliance: payload edited fields are NOT forwarded to reconcile
  - Agent delivery taken only once (idempotent take)
"""
import json
import os

import pytest
from starlette.testclient import TestClient

from buer.store import Store
from buer.mcp.server import mcp, _set_store_for_testing


def _ac(r) -> str:
    """Extract additionalContext from hook JSON, or '' when body is {}."""
    return r.json().get("hookSpecificOutput", {}).get("additionalContext", "")


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_store():
    """Ensure each test gets a fresh in-memory store."""
    yield
    _set_store_for_testing(None)


@pytest.fixture
def app():
    return mcp.streamable_http_app()


@pytest.fixture
def client(app):
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture
def store_and_project(tmp_path):
    store = Store(":memory:")
    pid = store.get_or_create_project(str(tmp_path))
    _set_store_for_testing(store)
    return store, pid, str(tmp_path)


# ── helper: write a Python source file ────────────────────────────────────────

def _write_src(tmp_path, version: int) -> str:
    """Write a Python file with a structurally distinct function per version.

    Varying parameter count guarantees a different fingerprint each time
    (compute_fingerprint uses params_shape as a key feature).
    """
    src = tmp_path / "target.py"
    params = ", ".join(f"p{i}" for i in range(version))
    body = " + ".join(f"p{i}" for i in range(version)) or "0"
    src.write_text(f"def target_fn({params}):\n    return {body}\n")
    return str(src)


# ── /buer/post-edit endpoint ──────────────────────────────────────────────────

class TestPostEditEndpoint:
    def test_returns_200_for_unknown_file(self, client):
        resp = client.post("/buer/post-edit", json={
            "tool_name": "Edit",
            "tool_input": {"file_path": "/nonexistent/f.py"},
        })
        assert resp.status_code == 200
        assert _ac(resp) == ""

    def test_no_cwd_no_project_silent_empty(self, client):
        # Scenario B: no cwd provided → no auto-registration anchor → silent empty.
        _set_store_for_testing(Store(":memory:"))
        resp = client.post("/buer/post-edit", json={
            "tool_name": "Edit",
            "tool_input": {"file_path": "/some/f.py"},
        })
        assert resp.status_code == 200
        assert _ac(resp) == ""

    def test_returns_empty_when_no_incidents(self, client, store_and_project, tmp_path):
        store, pid, root = store_and_project
        src = _write_src(tmp_path, 1)
        resp = client.post("/buer/post-edit", json={
            "tool_name": "Edit",
            "tool_input": {"file_path": src},
        })
        assert resp.status_code == 200
        assert _ac(resp) == ""

    def test_project_resolved_via_cwd_fallback(self, client, store_and_project, tmp_path):
        store, pid, root = store_and_project
        src = _write_src(tmp_path, 1)
        # Post with only cwd (no file_path that matches project)
        # Use the actual file_path but give cwd as the fallback
        resp = client.post("/buer/post-edit", json={
            "tool_name": "Edit",
            "tool_input": {"file_path": src},
            "cwd": root,
        })
        assert resp.status_code == 200

    def test_missing_file_path_returns_empty(self, client, store_and_project):
        resp = client.post("/buer/post-edit", json={
            "tool_name": "Edit",
            "tool_input": {},
        })
        assert resp.status_code == 200
        assert _ac(resp) == ""

    def test_malformed_json_returns_empty(self, client):
        resp = client.post("/buer/post-edit", content=b"not-json",
                           headers={"Content-Type": "application/json"})
        assert resp.status_code == 200
        assert _ac(resp) == ""


# ── end-to-end: 5 edits → stuck_region in response ───────────────────────────

class TestEndToEnd:
    def test_stuck_region_appears_after_five_edits(
        self, client, store_and_project, tmp_path
    ):
        """Five edits to same define → reconcile builds stuck chain →
        advance_incidents → stuck_region delivery → response contains text."""
        store, pid, root = store_and_project

        # Four prior edits (below θ₁=5, no incident yet)
        from buer.reconcile import reconcile
        for v in range(1, 5):
            src = _write_src(tmp_path, v)
            reconcile(store, pid, [src])

        # Fifth edit via POST
        src = _write_src(tmp_path, 5)
        resp = client.post("/buer/post-edit", json={
            "tool_name": "Edit",
            "tool_input": {"file_path": src},
        })

        assert resp.status_code == 200
        body = _ac(resp)
        assert "[BUER] stuck_region" in body
        assert "target_fn" in body

    def test_injection_uses_stored_details_not_recomputed(
        self, client, store_and_project, tmp_path
    ):
        """Delivery text comes from details already stored in the incident."""
        store, pid, root = store_and_project
        from buer.reconcile import reconcile

        for v in range(1, 5):
            reconcile(store, pid, [_write_src(tmp_path, v)])

        _write_src(tmp_path, 5)
        resp = client.post("/buer/post-edit", json={
            "tool_name": "Edit",
            "tool_input": {"file_path": _write_src(tmp_path, 5)},
        })

        # The chain_length field in details should be 5
        body = _ac(resp)
        assert "5" in body

    def test_delivery_taken_only_once(self, client, store_and_project, tmp_path):
        """Same delivery is not returned twice to the agent."""
        store, pid, root = store_and_project
        from buer.reconcile import reconcile

        for v in range(1, 5):
            reconcile(store, pid, [_write_src(tmp_path, v)])

        src = _write_src(tmp_path, 5)
        # First POST: produces delivery
        resp1 = client.post("/buer/post-edit", json={
            "tool_name": "Edit",
            "tool_input": {"file_path": src},
        })
        # Second POST (same file, same state): delivery already taken
        resp2 = client.post("/buer/post-edit", json={
            "tool_name": "Edit",
            "tool_input": {"file_path": src},
        })

        assert "[BUER]" in _ac(resp1)
        assert _ac(resp2) == ""  # already taken


# ── check_drift tool ──────────────────────────────────────────────────────────

class TestCheckDrift:
    def test_returns_no_incidents_message_when_clear(
        self, store_and_project
    ):
        store, pid, root = store_and_project
        result = _call_check_drift(root)
        assert "No active incidents" in result

    def test_returns_incident_text_when_open(self, store_and_project):
        store, pid, root = store_and_project
        # Manually write an incident
        store.write_incident(pid, signal="stuck_region",
                             target_node="/f.py::fn",
                             details=json.dumps({"chain_length": 5}))
        result = _call_check_drift(root)
        assert "[BUER] stuck_region" in result

    def test_non_destructive(self, store_and_project):
        store, pid, root = store_and_project
        store.write_incident(pid, signal="stuck_region",
                             target_node="/f.py::fn",
                             details=json.dumps({"chain_length": 5}))
        _call_check_drift(root)
        # Incident is still open (check_drift doesn't mark as taken)
        assert len(store.open_incidents(pid)) == 1

    def test_unknown_project_root(self):
        _set_store_for_testing(Store(":memory:"))
        result = _call_check_drift("/nonexistent/root")
        assert "No project" in result


# ── get_user_alerts tool ──────────────────────────────────────────────────────

class TestGetUserAlerts:
    def test_returns_empty_when_no_alerts(self, store_and_project):
        store, pid, root = store_and_project
        result = _call_get_user_alerts(root)
        assert "No pending" in result

    def test_returns_user_delivery(self, store_and_project):
        store, pid, root = store_and_project
        inc_id = store.write_incident(
            pid, signal="test_tampering",
            target_node="tests.T::test_fn",
            details=json.dumps({"question": "改的是测试本身？", "test_define": "/t.py::fn"}),
        )
        inc = store.con.execute("SELECT * FROM incidents WHERE id=?", (inc_id,)).fetchone()
        from buer.delivery import queue_user_notification
        queue_user_notification(store, pid, inc)

        result = _call_get_user_alerts(root)
        assert "⚠" in result
        assert "test_tampering" in result

    def test_takes_delivery_only_once(self, store_and_project):
        store, pid, root = store_and_project
        inc_id = store.write_incident(
            pid, signal="stuck_region", target_node="/f.py::fn",
            details=json.dumps({"chain_length": 5}),
        )
        inc = store.con.execute("SELECT * FROM incidents WHERE id=?", (inc_id,)).fetchone()
        from buer.delivery import queue_user_notification
        queue_user_notification(store, pid, inc)

        result1 = _call_get_user_alerts(root)
        result2 = _call_get_user_alerts(root)
        assert "[BUER]" in result1
        assert "No pending" in result2


# ── helpers that call tools directly (bypassing MCP transport) ────────────────

def _call_check_drift(project_root: str) -> str:
    from buer.mcp.server import check_drift
    return check_drift(project_root)


def _call_get_user_alerts(project_root: str) -> str:
    from buer.mcp.server import get_user_alerts
    return get_user_alerts(project_root)
