"""Tests for git integration batch 4: session_start missed-event fallback + .gitignore check."""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from buer.mcp.server import (
    _buer_in_gitignore,
    mcp,
    _set_store_for_testing,
)
from buer.store import Store


# ── helpers ───────────────────────────────────────────────────────────────────

def _store() -> Store:
    return Store(":memory:")


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(path), check=True, capture_output=True)
    (path / "f.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(path), check=True, capture_output=True)


@pytest.fixture(autouse=True)
def reset_store():
    yield
    _set_store_for_testing(None)


@pytest.fixture
def http_app():
    return mcp.streamable_http_app()


# ══════════════════════════════════════════════════════════════════════════════
# 1. Known HEAD → no fallback trigger (idempotent)
# ══════════════════════════════════════════════════════════════════════════════

def test_known_head_no_trigger(tmp_path, http_app):
    """Project already has snapshot for current HEAD → session_start does NOT re-trigger."""
    _init_git_repo(tmp_path)

    s = _store()
    pid = s.get_or_create_project(str(tmp_path), branch="main")
    # Simulate existing snapshot for current HEAD
    import subprocess as sp
    head = sp.check_output(["git", "rev-parse", "HEAD"], cwd=str(tmp_path)).decode().strip()
    s.create_snapshot(pid, head, "main", 0, None, reason="commit")
    _set_store_for_testing(s)

    tc = TestClient(http_app)
    resp = tc.post("/buer/session-start", json={
        "cwd": str(tmp_path),
        "session_id": "test-session-1",
    })
    assert resp.status_code == 200

    # No additional snapshot should have been created (background thread would have)
    time.sleep(0.1)
    rows = s.con.execute(
        "SELECT * FROM snapshots WHERE project_id=? AND reason='session_start'", (pid,)
    ).fetchall()
    assert len(rows) == 0
    s.close()


# ══════════════════════════════════════════════════════════════════════════════
# 2. Unknown HEAD → fallback triggers reconcile + snapshot
# ══════════════════════════════════════════════════════════════════════════════

def test_unknown_head_triggers(tmp_path, http_app):
    """Project has no snapshot for current HEAD → session_start triggers session_start snapshot."""
    import buer.mcp.server as srv

    _init_git_repo(tmp_path)
    # Make a second commit (not snapshotted) to simulate a missed git event
    (tmp_path / "g.py").write_text("def g(): pass\n")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "second"], cwd=str(tmp_path),
                   check=True, capture_output=True)

    db_file = str(tmp_path / "test.db")
    s = Store(db_file)
    pid = s.get_or_create_project(str(tmp_path), branch="main")
    # No snapshot for this HEAD
    s.close()

    orig_db = srv._db_path
    srv._db_path = db_file
    s2 = Store(db_file)
    _set_store_for_testing(s2)
    try:
        tc = TestClient(http_app)
        resp = tc.post("/buer/session-start", json={
            "cwd": str(tmp_path),
            "session_id": "test-session-2",
        })
        assert resp.status_code == 200

        deadline = time.time() + 5.0
        while time.time() < deadline:
            s3 = Store(db_file)
            rows = s3.con.execute(
                "SELECT * FROM snapshots WHERE project_id=? AND reason='session_start'", (pid,)
            ).fetchall()
            s3.close()
            if rows:
                break
            time.sleep(0.05)
    finally:
        srv._db_path = orig_db
        s2.close()

    s3 = Store(db_file)
    rows = s3.con.execute(
        "SELECT * FROM snapshots WHERE project_id=? AND reason='session_start'", (pid,)
    ).fetchall()
    s3.close()
    assert len(rows) >= 1
    assert len(rows[0]["commit_hash"]) == 40


# ══════════════════════════════════════════════════════════════════════════════
# 3. First-seen branch → creates project + initial snapshot
# ══════════════════════════════════════════════════════════════════════════════

def test_first_seen_branch_initial(tmp_path, http_app):
    """session_start on never-seen (root, branch) → new project + reason='initial' snapshot."""
    import buer.mcp.server as srv

    _init_git_repo(tmp_path)

    db_file = str(tmp_path / "test.db")
    s = Store(db_file)
    # Don't create any project — this simulates first session ever
    s.close()

    orig_db = srv._db_path
    srv._db_path = db_file
    s2 = Store(db_file)
    _set_store_for_testing(s2)
    try:
        tc = TestClient(http_app)
        resp = tc.post("/buer/session-start", json={
            "cwd": str(tmp_path),
            "session_id": "test-session-3",
        })
        assert resp.status_code == 200

        deadline = time.time() + 5.0
        while time.time() < deadline:
            s3 = Store(db_file)
            rows = s3.con.execute(
                "SELECT * FROM snapshots WHERE reason='initial'"
            ).fetchall()
            s3.close()
            if rows:
                break
            time.sleep(0.05)
    finally:
        srv._db_path = orig_db
        s2.close()

    s3 = Store(db_file)
    rows = s3.con.execute("SELECT * FROM snapshots WHERE reason='initial'").fetchall()
    assert len(rows) >= 1
    # Verify a project was created for the branch
    snap_pid = rows[0]["project_id"]
    proj = s3.con.execute("SELECT branch FROM projects WHERE id=?", (snap_pid,)).fetchone()
    assert proj["branch"] == "main"
    s3.close()


# ══════════════════════════════════════════════════════════════════════════════
# 4. Non-git project: old logic unchanged, no crash
# ══════════════════════════════════════════════════════════════════════════════

def test_non_git_unchanged(tmp_path, http_app):
    """Non-git project session_start → falls back to old find_project_for_file logic."""
    s = _store()
    pid = s.get_or_create_project(str(tmp_path))
    _set_store_for_testing(s)

    tc = TestClient(http_app)
    resp = tc.post("/buer/session-start", json={
        "cwd": str(tmp_path),
        "session_id": "test-session-4",
    })
    assert resp.status_code == 200
    # No snapshot created (no git, no trigger)
    rows = s.con.execute("SELECT * FROM snapshots WHERE project_id=?", (pid,)).fetchall()
    assert len(rows) == 0
    s.close()


# ══════════════════════════════════════════════════════════════════════════════
# 5-7. _buer_in_gitignore unit tests
# ══════════════════════════════════════════════════════════════════════════════

def test_gitignore_present(tmp_path):
    """_buer_in_gitignore returns True when .buer/ is in .gitignore."""
    (tmp_path / ".gitignore").write_text(".buer/\nnode_modules/\n")
    assert _buer_in_gitignore(str(tmp_path)) is True


def test_gitignore_absent(tmp_path):
    """_buer_in_gitignore returns False when .buer/ is NOT in .gitignore."""
    (tmp_path / ".gitignore").write_text("node_modules/\n__pycache__/\n")
    assert _buer_in_gitignore(str(tmp_path)) is False


def test_gitignore_no_file(tmp_path):
    """_buer_in_gitignore returns False when .gitignore doesn't exist."""
    assert _buer_in_gitignore(str(tmp_path)) is False


# ══════════════════════════════════════════════════════════════════════════════
# 8. Branch disambiguation: session_start resolves to correct branch project
# ══════════════════════════════════════════════════════════════════════════════

def test_branch_switch_back_via_session(tmp_path, http_app):
    """session_start on main resolves to main pid, not feature pid."""
    _init_git_repo(tmp_path)
    # Create feature branch but stay on main
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=str(tmp_path),
                   check=True, capture_output=True)
    subprocess.run(["git", "checkout", "main"], cwd=str(tmp_path),
                   check=True, capture_output=True)

    s = _store()
    pid_main = s.get_or_create_project(str(tmp_path), branch="main")
    pid_feature = s.get_or_create_project(str(tmp_path), branch="feature")
    # Give main a snapshot for current HEAD so no fallback trigger
    import subprocess as sp
    head = sp.check_output(["git", "rev-parse", "HEAD"], cwd=str(tmp_path)).decode().strip()
    s.create_snapshot(pid_main, head, "main", 0, None, reason="commit")
    _set_store_for_testing(s)

    tc = TestClient(http_app)
    resp = tc.post("/buer/session-start", json={
        "cwd": str(tmp_path),
        "session_id": "test-session-8",
    })
    assert resp.status_code == 200

    # Verify session was recorded under pid_main, not pid_feature
    rows_main = s.con.execute(
        "SELECT * FROM sessions WHERE project_id=? AND session_id=?",
        (pid_main, "test-session-8"),
    ).fetchall()
    rows_feature = s.con.execute(
        "SELECT * FROM sessions WHERE project_id=? AND session_id=?",
        (pid_feature, "test-session-8"),
    ).fetchall()
    assert len(rows_main) == 1
    assert len(rows_feature) == 0
    s.close()
