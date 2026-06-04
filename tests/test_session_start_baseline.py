"""Tests for session_start_handler baseline fixes.

Fix 1: Non-git projects also build a baseline (project registered with branch=None,
        determinations written synchronously).

Fix 2: Baseline is synchronous — when session_start_handler returns the determinations
        are already present, eliminating the race against the agent's first edit.

Git regression: existing git-project baseline behaviour unchanged.
"""
from __future__ import annotations

import buer.mcp.server as srv
from buer.store import Store

import pytest
from starlette.testclient import TestClient


# ── helpers ───────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_store():
    yield
    srv._set_store_for_testing(None)
    srv._full_ingest_in_progress.clear()


@pytest.fixture
def http_app():
    return srv.mcp.streamable_http_app()


def _client(app) -> TestClient:
    return TestClient(app, raise_server_exceptions=True)


def _non_git_project(tmp_path):
    """Create a non-git directory with one Python source file."""
    (tmp_path / "hello.py").write_text("def greet():\n    return 'hi'\n")
    return tmp_path


def _store_at(db_file: str) -> Store:
    return Store(db_file)


# ══════════════════════════════════════════════════════════════════════════════
# Fix 1 — Non-git project, first visit: project created with branch=NULL
# ══════════════════════════════════════════════════════════════════════════════

class TestNonGitFirstVisitProjectCreated:
    def test_project_registered_after_session_start(self, tmp_path, http_app):
        """Non-git, no prior project → session_start creates the project."""
        _non_git_project(tmp_path)
        db_file = str(tmp_path / "test.db")

        orig = srv._db_path
        srv._db_path = db_file
        s = _store_at(db_file)
        srv._set_store_for_testing(s)
        try:
            _client(http_app).post(
                "/buer/session-start",
                json={"cwd": str(tmp_path), "session_id": "s-ng-1"},
            )
            row = s.con.execute(
                "SELECT branch FROM projects WHERE root_path=?", (str(tmp_path),)
            ).fetchone()
        finally:
            srv._db_path = orig
            s.close()

        assert row is not None, "project must be created for non-git directory"
        assert row["branch"] is None  # non-git → branch=NULL

    def test_project_not_duplicated_on_second_call(self, tmp_path, http_app):
        """Second session_start on same non-git dir finds the existing project, not a duplicate."""
        _non_git_project(tmp_path)
        db_file = str(tmp_path / "test.db")

        orig = srv._db_path
        srv._db_path = db_file
        s = _store_at(db_file)
        srv._set_store_for_testing(s)
        try:
            for sid in ("s-ng-2a", "s-ng-2b"):
                _client(http_app).post(
                    "/buer/session-start",
                    json={"cwd": str(tmp_path), "session_id": sid},
                )
            count = s.con.execute(
                "SELECT COUNT(*) as n FROM projects WHERE root_path=?", (str(tmp_path),)
            ).fetchone()["n"]
        finally:
            srv._db_path = orig
            s.close()

        assert count == 1

    def test_200_response_non_git_no_prior_project(self, tmp_path, http_app):
        """Non-git, first visit returns HTTP 200."""
        _non_git_project(tmp_path)
        db_file = str(tmp_path / "test.db")

        orig = srv._db_path
        srv._db_path = db_file
        s = _store_at(db_file)
        srv._set_store_for_testing(s)
        try:
            r = _client(http_app).post(
                "/buer/session-start",
                json={"cwd": str(tmp_path), "session_id": "s-ng-3"},
            )
        finally:
            srv._db_path = orig
            s.close()

        assert r.status_code == 200


# ══════════════════════════════════════════════════════════════════════════════
# Fix 1 — Non-git project, first visit: determinations written (synchronous)
# ══════════════════════════════════════════════════════════════════════════════

class TestNonGitBaselineSynchronous:
    def test_determinations_present_after_handler_returns(self, tmp_path, http_app):
        """Baseline is synchronous: determinations exist immediately after handler returns."""
        _non_git_project(tmp_path)
        db_file = str(tmp_path / "test.db")

        orig = srv._db_path
        srv._db_path = db_file
        s = _store_at(db_file)
        srv._set_store_for_testing(s)
        try:
            _client(http_app).post(
                "/buer/session-start",
                json={"cwd": str(tmp_path), "session_id": "s-sync-1"},
            )
            pid = s.con.execute(
                "SELECT id FROM projects WHERE root_path=?", (str(tmp_path),)
            ).fetchone()["id"]
            det_count = s.con.execute(
                "SELECT COUNT(*) as n FROM determinations WHERE project_id=?", (pid,)
            ).fetchone()["n"]
        finally:
            srv._db_path = orig
            s.close()

        assert det_count > 0, (
            "determinations must be written synchronously before handler returns"
        )

    def test_no_polling_required(self, tmp_path, http_app):
        """No sleep/poll needed: determinations are present without any wait after the call."""
        _non_git_project(tmp_path)
        db_file = str(tmp_path / "test.db")

        orig = srv._db_path
        srv._db_path = db_file
        s = _store_at(db_file)
        srv._set_store_for_testing(s)
        try:
            _client(http_app).post(
                "/buer/session-start",
                json={"cwd": str(tmp_path), "session_id": "s-sync-2"},
            )
            # Check immediately — no sleep, no retry loop
            pid = s.con.execute(
                "SELECT id FROM projects WHERE root_path=?", (str(tmp_path),)
            ).fetchone()["id"]
            rows = s.con.execute(
                "SELECT file_path FROM determinations WHERE project_id=?", (pid,)
            ).fetchall()
        finally:
            srv._db_path = orig
            s.close()

        assert len(rows) > 0


# ══════════════════════════════════════════════════════════════════════════════
# Fix 2 — Git project: baseline also synchronous
# ══════════════════════════════════════════════════════════════════════════════

class TestGitBaselineSynchronous:
    def test_git_first_seen_determinations_ready_on_return(self, tmp_path, http_app):
        """Git project, first session: determinations present when handler returns (no race)."""
        import subprocess
        subprocess.run(["git", "init", "-b", "main"], cwd=str(tmp_path),
                       check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(tmp_path),
                       check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=str(tmp_path),
                       check=True, capture_output=True)
        (tmp_path / "app.py").write_text("def main():\n    pass\n")
        subprocess.run(["git", "add", "."], cwd=str(tmp_path),
                       check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path),
                       check=True, capture_output=True)

        db_file = str(tmp_path / "test.db")
        orig = srv._db_path
        srv._db_path = db_file
        s = _store_at(db_file)
        srv._set_store_for_testing(s)
        try:
            _client(http_app).post(
                "/buer/session-start",
                json={"cwd": str(tmp_path), "session_id": "s-git-sync-1"},
            )
            pid = s.con.execute(
                "SELECT id FROM projects WHERE root_path=?", (str(tmp_path),)
            ).fetchone()["id"]
            det_count = s.con.execute(
                "SELECT COUNT(*) as n FROM determinations WHERE project_id=?", (pid,)
            ).fetchone()["n"]
        finally:
            srv._db_path = orig
            s.close()

        assert det_count > 0


# ══════════════════════════════════════════════════════════════════════════════
# Regression — non-git project with existing registration unchanged
# ══════════════════════════════════════════════════════════════════════════════

class TestNonGitExistingProjectUnchanged:
    def test_existing_non_git_project_not_re_registered(self, tmp_path, http_app):
        """Non-git project already registered → no extra project created, no crash."""
        _non_git_project(tmp_path)

        s = Store(":memory:")
        pid = s.get_or_create_project(str(tmp_path))
        srv._set_store_for_testing(s)

        r = _client(http_app).post(
            "/buer/session-start",
            json={"cwd": str(tmp_path), "session_id": "s-reg-1"},
        )

        count = s.con.execute(
            "SELECT COUNT(*) as n FROM projects WHERE root_path=?", (str(tmp_path),)
        ).fetchone()["n"]
        s.close()

        assert r.status_code == 200
        assert count == 1

    def test_existing_non_git_no_snapshot_created(self, tmp_path, http_app):
        """Non-git project already exists → baseline not re-triggered, no snapshot."""
        _non_git_project(tmp_path)

        s = Store(":memory:")
        pid = s.get_or_create_project(str(tmp_path))
        srv._set_store_for_testing(s)

        _client(http_app).post(
            "/buer/session-start",
            json={"cwd": str(tmp_path), "session_id": "s-reg-2"},
        )

        rows = s.con.execute(
            "SELECT * FROM snapshots WHERE project_id=?", (pid,)
        ).fetchall()
        s.close()

        assert len(rows) == 0
