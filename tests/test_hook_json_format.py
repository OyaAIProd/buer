"""Tests for Claude Code HTTP hook JSON response format (all four endpoints).

Coverage:
  post-edit   — Content-Type, empty→{}, content→hookSpecificOutput
  post-read   — Content-Type, empty→{}, content→hookSpecificOutput
  session-start — Content-Type, empty→{}, content→hookSpecificOutput (SessionStart event)
  post-bash   — Content-Type, empty→{}, content→hookSpecificOutput
  stop        — regression: still returns {} / decision:block (application/json, unchanged)
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from buer.mcp.server import _set_store_for_testing, mcp
from buer.store import Store


@pytest.fixture(autouse=True)
def reset_store():
    yield
    _set_store_for_testing(None)


# ── helpers ───────────────────────────────────────────────────────────────────

def _mock_store(
    *,
    pid: int | None = 1,
    agent_deliveries: list | None = None,
    user_deliveries: list | None = None,
    drain_result: list | None = None,
    has_gd_edges: bool = False,
    gd_node_count: int = 0,
    guide_hint: str = "",
) -> MagicMock:
    store = MagicMock(spec=Store)
    store.find_project_for_file.return_value = pid
    store.get_or_create_project.return_value = pid or 1
    store.take_agent_deliveries.return_value = agent_deliveries or []
    store.take_user_deliveries.return_value = user_deliveries or []
    store.drain_recompute_queue.return_value = drain_result or []
    store.has_gd_edges.return_value = has_gd_edges
    store.gd_node_count.return_value = gd_node_count
    store.get_project.return_value = {"root_path": "/proj"}
    store.open_session.return_value = None
    store.close_session.return_value = None
    store.enqueue_recompute.return_value = None
    store.pending_recompute_count.return_value = 0
    store.max_seq.return_value = 0
    store.recent_crash_stacks.return_value = []
    store.recent_xml_run_exists.return_value = False
    store.recent_stdout_run_for_cmd.return_value = False
    store.insert_test_run.return_value = None
    return store


def _client(store: MagicMock) -> TestClient:
    _set_store_for_testing(store)
    return TestClient(mcp.streamable_http_app(), raise_server_exceptions=True)


# ── post-edit ─────────────────────────────────────────────────────────────────

class TestPostEditJsonFormat:
    _EDIT = {"tool_input": {"file_path": "/proj/foo.py"}, "cwd": "/proj", "session_id": "s1"}

    _PATCHES = (
        "buer.mcp.server.reconcile_against_disk",
        "buer.mcp.server.reconcile",
        "buer.mcp.server._check_gitignore_protection",
    )

    def test_content_type_is_json(self):
        store = _mock_store()
        with patch(self._PATCHES[0]), patch(self._PATCHES[1]), patch(self._PATCHES[2]), \
             patch("buer.mcp.server.signals.check_high_impact_defines", return_value=[]):
            r = _client(store).post("/buer/post-edit", json=self._EDIT)
        assert "application/json" in r.headers["content-type"]

    def test_no_deliveries_returns_empty_object(self):
        store = _mock_store()
        with patch(self._PATCHES[0]), patch(self._PATCHES[1]), patch(self._PATCHES[2]), \
             patch("buer.mcp.server.signals.check_high_impact_defines", return_value=[]):
            r = _client(store).post("/buer/post-edit", json=self._EDIT)
        assert r.json() == {}

    def test_no_file_path_returns_empty_object(self):
        store = _mock_store()
        r = _client(store).post("/buer/post-edit", json={"cwd": "/proj"})
        assert r.json() == {}
        assert "application/json" in r.headers["content-type"]

    def test_with_delivery_returns_hook_specific_output(self):
        msg = "regression: fn1 broke"
        store = _mock_store(agent_deliveries=[{"message": msg}])
        with patch("buer.mcp.server.reconcile_against_disk"), \
             patch("buer.mcp.server.reconcile"), \
             patch("buer.mcp.server._check_gitignore_protection"), \
             patch("buer.mcp.server.signals.check_high_impact_defines", return_value=[]):
            r = _client(store).post("/buer/post-edit", json=self._EDIT)
        body = r.json()
        assert body["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
        assert body["hookSpecificOutput"]["additionalContext"] == msg

    def test_malformed_body_returns_empty_object(self):
        store = _mock_store()
        r = _client(store).post("/buer/post-edit", content=b"not-json",
                                headers={"Content-Type": "application/json"})
        assert r.json() == {}
        assert "application/json" in r.headers["content-type"]


# ── post-read ─────────────────────────────────────────────────────────────────

class TestPostReadJsonFormat:
    _READ = {"tool_input": {"file_path": "/proj/foo.py"}, "cwd": "/proj", "session_id": "s1"}

    def test_content_type_is_json(self):
        store = _mock_store()
        r = _client(store).post("/buer/post-read", json=self._READ)
        assert "application/json" in r.headers["content-type"]

    def test_no_hint_returns_empty_object(self):
        store = _mock_store()
        r = _client(store).post("/buer/post-read", json=self._READ)
        assert r.json() == {}

    def test_malformed_body_returns_empty_object(self):
        store = _mock_store()
        r = _client(store).post("/buer/post-read", content=b"bad",
                                headers={"Content-Type": "application/json"})
        assert r.json() == {}
        assert "application/json" in r.headers["content-type"]

    def test_with_hint_returns_hook_specific_output(self):
        hint_text = "structural hint: hub node foo has 50 callers"
        from buer.navigator import THETA_CLARITY_NODES
        store = _mock_store(has_gd_edges=True, gd_node_count=THETA_CLARITY_NODES + 1)
        with patch("buer.mcp.server.navigator.structure_guide_hint", return_value=hint_text):
            r = _client(store).post("/buer/post-read", json=self._READ)
        body = r.json()
        assert body["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
        assert body["hookSpecificOutput"]["additionalContext"] == hint_text


# ── session-start ─────────────────────────────────────────────────────────────

class TestSessionStartJsonFormat:
    _START = {"cwd": "/proj", "session_id": "s1", "source": "startup"}

    def _make_client(self, *, has_gd_edges=False, overview="overview text") -> tuple[TestClient, MagicMock]:
        store = _mock_store(has_gd_edges=has_gd_edges)
        with patch("buer.mcp.server.git_utils.is_git_repo", return_value=False), \
             patch("buer.mcp.server._check_gitignore_protection"), \
             patch("buer.mcp.server._maybe_trigger_full_ingest", return_value=False), \
             patch("buer.mcp.server.health.project_overview", return_value=overview), \
             patch("buer.mcp.server.health.coarse_structure_map", return_value=""), \
             patch("buer.mcp.server.build_teaser" if False else "buer.session_report.build_teaser", return_value=""):
            return _client(store), store

    def test_content_type_is_json(self):
        store = _mock_store()
        with patch("buer.mcp.server.git_utils.is_git_repo", return_value=False), \
             patch("buer.mcp.server._check_gitignore_protection"), \
             patch("buer.mcp.server._maybe_trigger_full_ingest", return_value=False):
            r = _client(store).post("/buer/session-start", json=self._START)
        assert "application/json" in r.headers["content-type"]

    def test_no_cwd_returns_empty_object(self):
        store = _mock_store()
        r = _client(store).post("/buer/session-start", json={"session_id": "s1"})
        assert r.json() == {}
        assert "application/json" in r.headers["content-type"]

    def test_no_pid_returns_empty_object(self):
        store = _mock_store(pid=None)
        with patch("buer.mcp.server.git_utils.is_git_repo", return_value=False):
            r = _client(store).post("/buer/session-start", json=self._START)
        assert r.json() == {}
        assert "application/json" in r.headers["content-type"]

    def test_malformed_body_returns_empty_object(self):
        store = _mock_store()
        r = _client(store).post("/buer/session-start", content=b"bad",
                                headers={"Content-Type": "application/json"})
        assert r.json() == {}
        assert "application/json" in r.headers["content-type"]

    def test_no_gd_edges_no_triggered_empty_alert_returns_empty_object(self):
        """alert_prefix="" + suggestion_suffix="" → helper returns {}."""
        store = _mock_store(has_gd_edges=False, user_deliveries=[])
        with patch("buer.mcp.server.git_utils.is_git_repo", return_value=False), \
             patch("buer.mcp.server._check_gitignore_protection"), \
             patch("buer.mcp.server._maybe_trigger_full_ingest", return_value=False):
            r = _client(store).post("/buer/session-start", json=self._START)
        assert r.json() == {}

    def test_with_overview_returns_hook_specific_output(self):
        overview = "[BUER] project overview: 100 files"
        store = _mock_store(has_gd_edges=True)
        with patch("buer.mcp.server.git_utils.is_git_repo", return_value=False), \
             patch("buer.mcp.server._check_gitignore_protection"), \
             patch("buer.mcp.server._maybe_trigger_full_ingest", return_value=False), \
             patch("buer.mcp.server.health.project_overview", return_value=overview), \
             patch("buer.mcp.server.health.coarse_structure_map", return_value=""), \
             patch("buer.session_report.build_teaser", return_value=""):
            r = _client(store).post("/buer/session-start", json=self._START)
        body = r.json()
        assert body["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert overview in body["hookSpecificOutput"]["additionalContext"]

    def test_event_name_is_session_start(self):
        overview = "some overview"
        store = _mock_store(has_gd_edges=True)
        with patch("buer.mcp.server.git_utils.is_git_repo", return_value=False), \
             patch("buer.mcp.server._check_gitignore_protection"), \
             patch("buer.mcp.server._maybe_trigger_full_ingest", return_value=False), \
             patch("buer.mcp.server.health.project_overview", return_value=overview), \
             patch("buer.mcp.server.health.coarse_structure_map", return_value=""), \
             patch("buer.session_report.build_teaser", return_value=""):
            r = _client(store).post("/buer/session-start", json=self._START)
        assert r.json()["hookSpecificOutput"]["hookEventName"] == "SessionStart"


# ── post-bash ─────────────────────────────────────────────────────────────────

class TestPostBashJsonFormat:
    _BASH_NOOP = {
        "tool_input": {"command": "echo hello"},
        "tool_response": {"stdout": "hello\n", "stderr": ""},
        "cwd": "/proj",
        "session_id": "s1",
    }

    def test_content_type_is_json(self):
        store = _mock_store()
        with patch("buer.mcp.server._is_git_commit", return_value=False), \
             patch("buer.mcp.server._is_git_rollback", return_value=False), \
             patch("buer.mcp.server._is_git_branch_switch", return_value=None):
            r = _client(store).post("/buer/post-bash", json=self._BASH_NOOP)
        assert "application/json" in r.headers["content-type"]

    def test_no_output_returns_empty_object(self):
        store = _mock_store()
        r = _client(store).post("/buer/post-bash", json={
            "tool_input": {"command": "x"},
            "tool_response": {"stdout": ""},
            "cwd": "/proj",
        })
        assert r.json() == {}
        assert "application/json" in r.headers["content-type"]

    def test_no_pid_returns_empty_object(self):
        store = _mock_store(pid=None)
        r = _client(store).post("/buer/post-bash", json=self._BASH_NOOP)
        assert r.json() == {}
        assert "application/json" in r.headers["content-type"]

    def test_malformed_body_returns_empty_object(self):
        store = _mock_store()
        r = _client(store).post("/buer/post-bash", content=b"bad",
                                headers={"Content-Type": "application/json"})
        assert r.json() == {}
        assert "application/json" in r.headers["content-type"]

    def test_no_crash_returns_empty_object(self):
        store = _mock_store()
        with patch("buer.mcp.server._is_git_commit", return_value=False), \
             patch("buer.mcp.server._is_git_rollback", return_value=False), \
             patch("buer.mcp.server._is_git_branch_switch", return_value=None):
            r = _client(store).post("/buer/post-bash", json=self._BASH_NOOP)
        assert r.json() == {}



# ── stop regression ───────────────────────────────────────────────────────────

class TestStopHandlerRegression:
    """stop_handler must remain unchanged: {} / decision:block, application/json."""

    _STOP = {"stop_hook_active": False, "cwd": "/proj", "session_id": "s1"}

    def _mock_stop_store(self, deliveries):
        store = _mock_store(user_deliveries=deliveries)
        store.drain_recompute_queue.return_value = []
        return store

    def test_no_deliveries_returns_empty_object(self):
        store = self._mock_stop_store([])
        r = _client(store).post("/buer/stop", json=self._STOP)
        assert r.json() == {}
        assert "application/json" in r.headers["content-type"]

    def test_with_delivery_returns_decision_block(self):
        store = self._mock_stop_store([{"message": "alert"}])
        r = _client(store).post("/buer/stop", json=self._STOP)
        body = r.json()
        assert body["decision"] == "block"
        assert "application/json" in r.headers["content-type"]

    def test_stop_hook_active_returns_empty_object(self):
        store = self._mock_stop_store([{"message": "x"}])
        r = _client(store).post("/buer/stop", json={**self._STOP, "stop_hook_active": True})
        assert r.json() == {}
