"""Tests for BUER automatic project registration on first use (§4.4 extension).

Coverage (A-G as specified):
  A. post-edit: unregistered file + cwd → auto-registers cwd, processes edit
  B. post-edit: no cwd + no matching project → silent empty, no project created
  C. nested: deeper project already registered → file resolves to deeper project,
             cwd=parent does NOT create a new project or reassign the file
  D. post-read: same auto-register logic — file + cwd triggers registration
  E. project_overview MCP tool: unregistered root → returns "no data" message, no project created
  F. session-start hook: unregistered cwd → silent empty, no project created
  G. get_or_create idempotent: same cwd called twice → same project_id, one row in DB
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from buer.mcp.server import _set_store_for_testing, mcp, project_overview
from buer.store import Store


def _ac(r) -> str:
    """Extract additionalContext from hook JSON, or '' when body is {}."""
    return r.json().get("hookSpecificOutput", {}).get("additionalContext", "")


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_store():
    yield
    _set_store_for_testing(None)


@pytest.fixture
def client():
    return TestClient(mcp.streamable_http_app(), raise_server_exceptions=True)


@pytest.fixture
def fresh_store():
    store = Store(":memory:")
    _set_store_for_testing(store)
    return store


# ── A: post-edit auto-registers cwd ───────────────────────────────────────────

class TestAutoRegisterOnEdit:
    def test_cwd_registered_when_file_unknown(self, client, fresh_store, tmp_path):
        """Scenario A: edit arrives for an unregistered file; cwd auto-registers it."""
        src = tmp_path / "foo.py"
        src.write_text("def foo(): pass\n")
        cwd = str(tmp_path)

        assert fresh_store.find_project_for_file(cwd) is None

        resp = client.post("/buer/post-edit", json={
            "tool_name": "Edit",
            "tool_input": {"file_path": str(src)},
            "cwd": cwd,
        })
        assert resp.status_code == 200

        # Project must now exist
        pid = fresh_store.find_project_for_file(cwd)
        assert pid is not None

    def test_file_in_registered_project_after_auto_register(
        self, client, fresh_store, tmp_path
    ):
        """After auto-registration, subsequent edits resolve via normal find_project_for_file."""
        src = tmp_path / "bar.py"
        src.write_text("def bar(): pass\n")
        cwd = str(tmp_path)

        client.post("/buer/post-edit", json={
            "tool_name": "Edit",
            "tool_input": {"file_path": str(src)},
            "cwd": cwd,
        })
        pid_first = fresh_store.find_project_for_file(cwd)

        # Second edit — find_project_for_file now succeeds, no second registration
        client.post("/buer/post-edit", json={
            "tool_name": "Edit",
            "tool_input": {"file_path": str(src)},
            "cwd": cwd,
        })
        pid_second = fresh_store.find_project_for_file(cwd)

        assert pid_first == pid_second
        rows = fresh_store.con.execute("SELECT COUNT(*) AS n FROM projects").fetchone()
        assert rows["n"] == 1


# ── B: no cwd → no auto-registration ─────────────────────────────────────────

class TestNoAutoRegisterWithoutCwd:
    def test_silent_empty_when_no_cwd(self, client, fresh_store):
        """Scenario B: no cwd provided → cannot auto-register → silent empty."""
        resp = client.post("/buer/post-edit", json={
            "tool_name": "Edit",
            "tool_input": {"file_path": "/no/cwd/given.py"},
        })
        assert resp.status_code == 200
        assert _ac(resp) == ""
        rows = fresh_store.con.execute("SELECT COUNT(*) AS n FROM projects").fetchone()
        assert rows["n"] == 0

    def test_no_project_created_without_cwd(self, client, fresh_store):
        """No spurious project rows created when cwd absent."""
        for _ in range(3):
            client.post("/buer/post-edit", json={
                "tool_name": "Edit",
                "tool_input": {"file_path": "/no/cwd/f.py"},
            })
        rows = fresh_store.con.execute("SELECT COUNT(*) AS n FROM projects").fetchone()
        assert rows["n"] == 0


# ── C: nested projects — deeper match wins ───────────────────────────────────

class TestNestedProjectPriority:
    def test_deeper_project_wins_over_cwd(self, client, fresh_store, tmp_path):
        """Scenario C: sub-project already registered; file inside it resolves
        to that project, NOT to cwd parent — longest-match semantics preserved."""
        parent = tmp_path
        child = tmp_path / "sub"
        child.mkdir()

        # Register only the child (deeper) project
        child_pid = fresh_store.get_or_create_project(str(child))

        # Edit a file inside the child, but cwd = parent
        src = child / "module.py"
        src.write_text("def fn(): pass\n")

        resp = client.post("/buer/post-edit", json={
            "tool_name": "Edit",
            "tool_input": {"file_path": str(src)},
            "cwd": str(parent),  # parent is NOT registered
        })
        assert resp.status_code == 200

        # File must still resolve to child project
        resolved_pid = fresh_store.find_project_for_file(str(src))
        assert resolved_pid == child_pid

        # Parent must NOT have been auto-registered (child matched first)
        parent_pid = fresh_store.con.execute(
            "SELECT id FROM projects WHERE root_path = ?", (str(parent),)
        ).fetchone()
        assert parent_pid is None

    def test_deeper_match_prevents_parent_registration(
        self, client, fresh_store, tmp_path
    ):
        """Repeated edits to deeper-project files do not accumulate parent projects."""
        child = tmp_path / "inner"
        child.mkdir()
        fresh_store.get_or_create_project(str(child))

        for i in range(3):
            src = child / f"f{i}.py"
            src.write_text("def f(): pass\n")
            client.post("/buer/post-edit", json={
                "tool_name": "Edit",
                "tool_input": {"file_path": str(src)},
                "cwd": str(tmp_path),
            })

        rows = fresh_store.con.execute("SELECT COUNT(*) AS n FROM projects").fetchone()
        assert rows["n"] == 1  # only the child project


# ── D: post-read auto-registration ───────────────────────────────────────────

class TestAutoRegisterOnRead:
    def test_file_path_triggers_registration(self, client, fresh_store, tmp_path):
        """Scenario D: post-read with unregistered file + cwd → cwd auto-registered."""
        src = tmp_path / "read_me.py"
        src.write_text("def x(): pass\n")
        cwd = str(tmp_path)

        assert fresh_store.find_project_for_file(cwd) is None

        resp = client.post("/buer/post-read", json={
            "tool_name": "Read",
            "tool_input": {"file_path": str(src)},
            "cwd": cwd,
            "session_id": "sess-1",
        })
        assert resp.status_code == 200

        pid = fresh_store.find_project_for_file(cwd)
        assert pid is not None

    def test_grep_glob_no_file_path_registers_cwd(self, client, fresh_store, tmp_path):
        """Grep/Glob payload has no file_path; cwd path auto-registers project."""
        cwd = str(tmp_path)
        assert fresh_store.find_project_for_file(cwd) is None

        resp = client.post("/buer/post-read", json={
            "tool_name": "Glob",
            "tool_input": {},
            "cwd": cwd,
            "session_id": "sess-2",
        })
        assert resp.status_code == 200

        pid = fresh_store.find_project_for_file(cwd)
        assert pid is not None

    def test_post_read_no_cwd_no_registration(self, client, fresh_store):
        """No cwd in post-read → no auto-registration → silent empty."""
        resp = client.post("/buer/post-read", json={
            "tool_name": "Read",
            "tool_input": {"file_path": "/some/file.py"},
            "session_id": "sess-3",
        })
        assert resp.status_code == 200
        assert _ac(resp) == ""
        rows = fresh_store.con.execute("SELECT COUNT(*) AS n FROM projects").fetchone()
        assert rows["n"] == 0


# ── E: MCP query tools do not auto-register ──────────────────────────────────

class TestQueryToolsNoAutoRegister:
    def test_project_overview_returns_no_data_message(self, fresh_store, tmp_path):
        """Scenario E: project_overview for unregistered root → 'no data' message,
        no project created."""
        root = str(tmp_path)
        result = project_overview(root)

        assert "No project registered" in result
        rows = fresh_store.con.execute("SELECT COUNT(*) AS n FROM projects").fetchone()
        assert rows["n"] == 0


# ── F: session-start does not auto-register ──────────────────────────────────

class TestSessionStartNoAutoRegister:
    def test_session_start_silent_for_unregistered_cwd(self, client, fresh_store, tmp_path):
        """Scenario F: session-start with unregistered cwd → silent empty 200,
        no project created."""
        resp = client.post("/buer/session-start", json={
            "source": "startup",
            "cwd": str(tmp_path),
            "session_id": "sess-start-1",
        })
        assert resp.status_code == 200
        assert _ac(resp) == ""
        rows = fresh_store.con.execute("SELECT COUNT(*) AS n FROM projects").fetchone()
        assert rows["n"] == 0

    def test_stop_silent_for_unregistered_cwd(self, client, fresh_store, tmp_path):
        """stop handler with unregistered cwd → {} (allow), no project created."""
        resp = client.post("/buer/stop", json={
            "stop_hook_active": False,
            "cwd": str(tmp_path),
            "session_id": "sess-stop-1",
        })
        assert resp.status_code == 200
        assert resp.json() == {}  # {} = allow stop (unregistered cwd)
        rows = fresh_store.con.execute("SELECT COUNT(*) AS n FROM projects").fetchone()
        assert rows["n"] == 0


# ── G: get_or_create idempotent ───────────────────────────────────────────────

class TestGetOrCreateIdempotent:
    def test_same_cwd_multiple_calls_same_id(self, client, fresh_store, tmp_path):
        """Scenario G: repeated auto-registrations of same cwd → same project_id."""
        src = tmp_path / "g.py"
        src.write_text("def g(): pass\n")
        cwd = str(tmp_path)

        for _ in range(3):
            client.post("/buer/post-edit", json={
                "tool_name": "Edit",
                "tool_input": {"file_path": str(src)},
                "cwd": cwd,
            })

        rows = fresh_store.con.execute("SELECT id FROM projects WHERE root_path = ?", (cwd,)).fetchall()
        assert len(rows) == 1

    def test_get_or_create_direct_idempotent(self, fresh_store, tmp_path):
        """Direct store call: get_or_create_project called twice returns same id."""
        cwd = str(tmp_path)
        pid1 = fresh_store.get_or_create_project(cwd)
        pid2 = fresh_store.get_or_create_project(cwd)
        assert pid1 == pid2
        rows = fresh_store.con.execute("SELECT COUNT(*) AS n FROM projects").fetchone()
        assert rows["n"] == 1
