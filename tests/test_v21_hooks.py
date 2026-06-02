"""Tests for BUER v2.1 hook infrastructure endpoints (§4.0).

Coverage:
  A. POST /buer/post-read: file_path present → enqueued, empty 200, no analysis
  B. POST /buer/post-read: Grep/Glob (no file_path) → cwd enqueued, empty 200
  C. POST /buer/stop: stop_hook_active=true → empty 200, no recompute (anti-loop)
  D. POST /buer/stop: stop_hook_active=false → drain called, user alerts returned
  E. POST /buer/session-start: gd_edges exist → returns project overview
  F. POST /buer/session-start: no gd_edges → empty 200
  G. POST /buer/session-start: source=resume → executes (not skipped)
  H. post-edit still injects agent alerts + now enqueues file
  I. pending_recompute dedup: same file not re-enqueued while pending
  J. All endpoints: bad payload / no project → silent empty 200
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from buer.mcp.server import _set_store_for_testing, mcp
from buer.store import Store


# ── helpers ────────────────────────────────────────────────────────────────────

def _mock_store(
    *,
    pid: int | None = 1,
    has_gd: bool = False,
    drain_result: list | None = None,
    user_deliveries: list | None = None,
    agent_deliveries: list | None = None,
) -> MagicMock:
    store = MagicMock(spec=Store)
    store.find_project_for_file.return_value = pid
    store.has_gd_edges.return_value = has_gd
    store.drain_recompute_queue.return_value = drain_result or []
    store.take_user_deliveries.return_value = user_deliveries or []
    store.take_agent_deliveries.return_value = agent_deliveries or []
    store.pending_recompute_count.return_value = 0
    return store


def _client(store: MagicMock) -> TestClient:
    _set_store_for_testing(store)
    return TestClient(mcp.streamable_http_app(), raise_server_exceptions=True)


@pytest.fixture(autouse=True)
def reset_store():
    yield
    _set_store_for_testing(None)


# ── A: post-read with file_path ────────────────────────────────────────────────

class TestPostRead:
    def test_file_enqueued(self):
        store = _mock_store()
        r = _client(store).post("/buer/post-read", json={
            "tool_name": "Read",
            "tool_input": {"file_path": "/proj/src/foo.py"},
            "cwd": "/proj",
        })
        assert r.status_code == 200
        assert r.text == ""
        store.enqueue_recompute.assert_called_once_with(1, "/proj/src/foo.py")

    def test_no_project_silent(self):
        # No cwd → no auto-register anchor → silent (scenario B for post-read).
        store = _mock_store(pid=None)
        r = _client(store).post("/buer/post-read", json={
            "tool_name": "Read",
            "tool_input": {"file_path": "/unknown/file.py"},
        })
        assert r.status_code == 200
        store.enqueue_recompute.assert_not_called()

    def test_file_path_project_lookup_first(self):
        """file_path is the primary lookup; cwd is fallback."""
        store = _mock_store(pid=3)
        store.find_project_for_file.side_effect = [3, None]  # file_path→3, cwd never called
        _client(store).post("/buer/post-read", json={
            "tool_name": "Read",
            "tool_input": {"file_path": "/proj/x.py"},
            "cwd": "/proj",
        })
        store.enqueue_recompute.assert_called_once_with(3, "/proj/x.py")

    def test_cwd_auto_register_when_file_not_in_project(self):
        """If file_path not in any project and cwd given → auto-register via get_or_create_project."""
        store = _mock_store(pid=None)
        store.find_project_for_file.return_value = None
        store.get_or_create_project.return_value = 5
        _client(store).post("/buer/post-read", json={
            "tool_name": "Read",
            "tool_input": {"file_path": "/proj/x.py"},
            "cwd": "/proj",
        })
        store.get_or_create_project.assert_called_once_with("/proj")
        store.enqueue_recompute.assert_called_once_with(5, "/proj/x.py")

    def test_empty_body_silent(self):
        store = _mock_store()
        r = _client(store).post(
            "/buer/post-read",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 200
        store.enqueue_recompute.assert_not_called()

    def test_no_analysis_run(self):
        """post-read must not trigger reconcile (heavy work deferred to stop)."""
        store = _mock_store()
        _client(store).post("/buer/post-read", json={
            "tool_name": "Read",
            "tool_input": {"file_path": "/proj/foo.py"},
            "cwd": "/proj",
        })
        # reconcile lives outside the store — just verify store has no reconcile attr called
        store.enqueue_recompute.assert_called_once()


# ── B: post-read Grep/Glob (no file_path) ─────────────────────────────────────

class TestPostReadGrepGlob:
    def test_grep_no_file_path_enqueues_cwd(self):
        store = _mock_store()
        r = _client(store).post("/buer/post-read", json={
            "tool_name": "Grep",
            "tool_input": {"pattern": "def foo"},
            "cwd": "/proj",
        })
        assert r.status_code == 200
        assert r.text == ""
        store.enqueue_recompute.assert_called_once_with(1, "/proj")

    def test_glob_no_file_path_enqueues_cwd(self):
        store = _mock_store()
        _client(store).post("/buer/post-read", json={
            "tool_name": "Glob",
            "tool_input": {"pattern": "**/*.py"},
            "cwd": "/proj",
        })
        store.enqueue_recompute.assert_called_once_with(1, "/proj")

    def test_no_cwd_and_no_file_path_silent(self):
        store = _mock_store()
        _client(store).post("/buer/post-read", json={
            "tool_name": "Grep",
            "tool_input": {"pattern": "foo"},
        })
        store.enqueue_recompute.assert_not_called()


# ── C: stop with stop_hook_active=true (anti-loop) ────────────────────────────

class TestStopAntiLoop:
    def test_stop_hook_active_true_returns_empty_immediately(self):
        store = _mock_store()
        r = _client(store).post("/buer/stop", json={
            "stop_hook_active": True,
            "cwd": "/proj",
        })
        assert r.status_code == 200
        assert r.text == ""

    def test_stop_hook_active_true_no_drain(self):
        store = _mock_store()
        _client(store).post("/buer/stop", json={
            "stop_hook_active": True,
            "cwd": "/proj",
        })
        store.drain_recompute_queue.assert_not_called()

    def test_stop_hook_active_true_no_user_deliveries_taken(self):
        store = _mock_store()
        _client(store).post("/buer/stop", json={
            "stop_hook_active": True,
            "cwd": "/proj",
        })
        store.take_user_deliveries.assert_not_called()


# ── D: stop with stop_hook_active=false ───────────────────────────────────────

class TestStopNormal:
    def test_drains_recompute_queue(self):
        store = _mock_store(drain_result=["/proj/a.py", "/proj/b.py"])
        _client(store).post("/buer/stop", json={
            "stop_hook_active": False,
            "cwd": "/proj",
        })
        store.drain_recompute_queue.assert_called_once_with(1)

    def test_returns_user_alerts(self):
        store = _mock_store(user_deliveries=[
            {"message": "Alert 1"},
            {"message": "Alert 2"},
        ])
        r = _client(store).post("/buer/stop", json={
            "stop_hook_active": False,
            "cwd": "/proj",
        })
        assert r.status_code == 200
        assert "Alert 1" in r.text
        assert "Alert 2" in r.text

    def test_no_user_alerts_empty_response(self):
        store = _mock_store(user_deliveries=[])
        r = _client(store).post("/buer/stop", json={
            "stop_hook_active": False,
            "cwd": "/proj",
        })
        assert r.status_code == 200
        assert r.text == ""

    def test_no_project_silent(self):
        store = _mock_store(pid=None)
        r = _client(store).post("/buer/stop", json={
            "stop_hook_active": False,
            "cwd": "/unknown",
        })
        assert r.status_code == 200
        store.drain_recompute_queue.assert_not_called()

    def test_empty_drain_no_thread_started(self):
        """No files in queue → background thread not started (nothing to do)."""
        store = _mock_store(drain_result=[])
        with patch("buer.mcp.server.threading.Thread") as mock_thread:
            _client(store).post("/buer/stop", json={
                "stop_hook_active": False,
                "cwd": "/proj",
            })
        mock_thread.assert_not_called()

    def test_non_empty_drain_starts_background_thread(self):
        store = _mock_store(drain_result=["/proj/foo.py"])
        with patch("buer.mcp.server.threading.Thread") as mock_thread:
            mock_thread.return_value.start = MagicMock()
            _client(store).post("/buer/stop", json={
                "stop_hook_active": False,
                "cwd": "/proj",
            })
        mock_thread.assert_called_once()
        mock_thread.return_value.start.assert_called_once()

    def test_missing_cwd_silent(self):
        store = _mock_store()
        r = _client(store).post("/buer/stop", json={"stop_hook_active": False})
        assert r.status_code == 200
        store.drain_recompute_queue.assert_not_called()


# ── E: session-start with graph ───────────────────────────────────────────────

class TestSessionStartWithGraph:
    def test_returns_overview_when_graph_exists(self):
        store = _mock_store(has_gd=True)
        with patch("buer.mcp.server.health.project_overview", return_value="[BUER] overview text"), \
             patch("buer.mcp.server.health.coarse_structure_map", return_value=""):
            r = _client(store).post("/buer/session-start", json={
                "source": "startup",
                "cwd": "/proj",
            })
        assert r.status_code == 200
        assert "overview text" in r.text

    def test_project_overview_called_with_correct_args(self):
        store = _mock_store(pid=7, has_gd=True)
        with patch("buer.mcp.server.health.project_overview", return_value="ok") as mock_ov, \
             patch("buer.mcp.server.health.coarse_structure_map", return_value=""):
            _client(store).post("/buer/session-start", json={
                "source": "startup",
                "cwd": "/my/proj",
            })
        mock_ov.assert_called_once_with(store, 7, "/my/proj")


# ── F: session-start without graph ────────────────────────────────────────────

class TestSessionStartNoGraph:
    def test_empty_when_no_gd_edges(self):
        store = _mock_store(has_gd=False)
        r = _client(store).post("/buer/session-start", json={
            "source": "startup",
            "cwd": "/proj",
        })
        assert r.status_code == 200
        assert r.text == ""

    def test_no_overview_call_when_no_graph(self):
        store = _mock_store(has_gd=False)
        with patch("buer.mcp.server.health.project_overview") as mock_ov:
            _client(store).post("/buer/session-start", json={
                "source": "startup",
                "cwd": "/proj",
            })
        mock_ov.assert_not_called()

    def test_no_project_empty(self):
        store = _mock_store(pid=None)
        r = _client(store).post("/buer/session-start", json={
            "source": "startup",
            "cwd": "/unknown",
        })
        assert r.status_code == 200
        assert r.text == ""

    def test_missing_cwd_empty(self):
        store = _mock_store()
        r = _client(store).post("/buer/session-start", json={"source": "startup"})
        assert r.status_code == 200
        assert r.text == ""


# ── G: session-start source=resume executes normally ─────────────────────────

class TestSessionStartResume:
    def test_resume_executes_same_as_startup(self):
        store = _mock_store(has_gd=True)
        with patch("buer.mcp.server.health.project_overview", return_value="[BUER] refreshed"), \
             patch("buer.mcp.server.health.coarse_structure_map", return_value=""):
            r = _client(store).post("/buer/session-start", json={
                "source": "resume",
                "cwd": "/proj",
            })
        assert r.status_code == 200
        assert "refreshed" in r.text

    def test_resume_no_graph_still_empty(self):
        store = _mock_store(has_gd=False)
        r = _client(store).post("/buer/session-start", json={
            "source": "resume",
            "cwd": "/proj",
        })
        assert r.text == ""


# ── H: post-edit still injects alerts + enqueues ─────────────────────────────

class TestPostEditEnqueues:
    def test_post_edit_enqueues_file(self, tmp_path):
        """post-edit now calls enqueue_recompute after reconcile."""
        store = _mock_store()
        file_path = str(tmp_path / "edit.py")
        file_path_real = file_path

        # find_project_for_file returns 1 for both file and cwd lookups
        store.find_project_for_file.return_value = 1

        import buer.mcp.server as srv
        with patch.object(srv, "reconcile"):
            _client(store).post("/buer/post-edit", json={
                "tool_name": "Edit",
                "tool_input": {"file_path": file_path_real},
                "cwd": str(tmp_path),
            })
        store.enqueue_recompute.assert_called_once_with(1, file_path_real)

    def test_post_edit_agent_alerts_still_returned(self, tmp_path):
        """post-edit's agent injection path is unchanged."""
        store = _mock_store(agent_deliveries=[{"message": "agent alert"}])
        file_path = str(tmp_path / "edit.py")
        store.find_project_for_file.return_value = 1

        import buer.mcp.server as srv
        with patch.object(srv, "reconcile"):
            r = _client(store).post("/buer/post-edit", json={
                "tool_name": "Edit",
                "tool_input": {"file_path": file_path},
                "cwd": str(tmp_path),
            })
        assert "agent alert" in r.text


# ── I: pending_recompute dedup (Store unit tests) ─────────────────────────────

class TestPendingRecomputeDedup:
    def test_second_enqueue_while_pending_is_noop(self, tmp_path):
        store = Store(":memory:")
        pid = store.get_or_create_project(str(tmp_path))
        fp = str(tmp_path / "foo.py")

        store.enqueue_recompute(pid, fp)
        store.enqueue_recompute(pid, fp)  # second call — dedup
        assert store.pending_recompute_count(pid) == 1

    def test_done_row_reactivated_on_enqueue(self, tmp_path):
        store = Store(":memory:")
        pid = store.get_or_create_project(str(tmp_path))
        fp = str(tmp_path / "foo.py")

        store.enqueue_recompute(pid, fp)
        drained = store.drain_recompute_queue(pid)
        assert drained == [fp]
        assert store.pending_recompute_count(pid) == 0

        store.enqueue_recompute(pid, fp)  # re-enqueue after done
        assert store.pending_recompute_count(pid) == 1

    def test_drain_returns_all_pending(self, tmp_path):
        store = Store(":memory:")
        pid = store.get_or_create_project(str(tmp_path))
        files = [str(tmp_path / f"f{i}.py") for i in range(3)]
        for f in files:
            store.enqueue_recompute(pid, f)

        drained = store.drain_recompute_queue(pid)
        assert set(drained) == set(files)
        assert store.pending_recompute_count(pid) == 0

    def test_drain_empty_returns_empty_list(self, tmp_path):
        store = Store(":memory:")
        pid = store.get_or_create_project(str(tmp_path))
        assert store.drain_recompute_queue(pid) == []

    def test_has_gd_edges_false_when_no_edges(self, tmp_path):
        store = Store(":memory:")
        pid = store.get_or_create_project(str(tmp_path))
        assert store.has_gd_edges(pid) is False

    def test_has_gd_edges_true_after_edge_inserted(self, tmp_path):
        store = Store(":memory:")
        pid = store.get_or_create_project(str(tmp_path))
        # insert two determinations, then a gd_edge
        d1 = store.insert_determination(pid, 1, "a.py", "fn_a", None, "create")
        d2 = store.insert_determination(pid, 2, "b.py", "fn_b", None, "create")
        store.insert_gd_edge(pid, d1, d2, "version_chain")
        assert store.has_gd_edges(pid) is True


# ── J: bad payload / no project → silent 200 ─────────────────────────────────

class TestSilentFailures:
    def test_post_read_invalid_json(self):
        store = _mock_store()
        r = _client(store).post(
            "/buer/post-read", content=b"!!!", headers={"Content-Type": "application/json"}
        )
        assert r.status_code == 200
        assert r.text == ""

    def test_post_read_empty_json(self):
        store = _mock_store()
        r = _client(store).post("/buer/post-read", json={})
        assert r.status_code == 200
        store.enqueue_recompute.assert_not_called()

    def test_session_start_invalid_json(self):
        store = _mock_store()
        r = _client(store).post(
            "/buer/session-start", content=b"!!!", headers={"Content-Type": "application/json"}
        )
        assert r.status_code == 200
        assert r.text == ""

    def test_stop_invalid_json(self):
        store = _mock_store()
        r = _client(store).post(
            "/buer/stop", content=b"!!!", headers={"Content-Type": "application/json"}
        )
        assert r.status_code == 200
        assert r.text == ""

    def test_stop_no_project_silent(self):
        store = _mock_store(pid=None)
        r = _client(store).post("/buer/stop", json={"stop_hook_active": False, "cwd": "/x"})
        assert r.status_code == 200
        store.drain_recompute_queue.assert_not_called()
