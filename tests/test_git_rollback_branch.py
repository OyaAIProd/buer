"""Tests for git integration batch 3: rollback/branch-switch command recognition
and end-to-end full-reconcile + snapshot via post_bash."""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from buer.mcp.server import (
    _is_git_rollback,
    _is_git_branch_switch,
    mcp,
    _set_store_for_testing,
    _full_ingest_in_background,
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
# 1. _is_git_rollback — positive cases
# ══════════════════════════════════════════════════════════════════════════════

def test_rollback_commands():
    assert _is_git_rollback("git reset --hard") is True
    assert _is_git_rollback("git reset --hard HEAD~1") is True
    assert _is_git_rollback("git reset --mixed HEAD~1") is True
    assert _is_git_rollback("git revert abc1234") is True
    assert _is_git_rollback("git stash pop") is True
    assert _is_git_rollback("git stash apply") is True
    assert _is_git_rollback("git checkout -- file.py") is True
    assert _is_git_rollback("git merge feature") is True
    assert _is_git_rollback("git rebase main") is True
    assert _is_git_rollback("git pull") is True
    assert _is_git_rollback("git restore src/foo.py") is True
    assert _is_git_rollback("git clean -fd") is True


# ══════════════════════════════════════════════════════════════════════════════
# 2. _is_git_rollback — reset --soft excluded
# ══════════════════════════════════════════════════════════════════════════════

def test_rollback_excludes_soft():
    assert _is_git_rollback("git reset --soft HEAD~1") is False
    assert _is_git_rollback("git reset --soft") is False


# ══════════════════════════════════════════════════════════════════════════════
# 3. _is_git_branch_switch — positive cases
# ══════════════════════════════════════════════════════════════════════════════

def test_branch_switch_commands():
    assert _is_git_branch_switch("git checkout feature") is not None
    assert _is_git_branch_switch("git checkout main") is not None
    assert _is_git_branch_switch("git checkout -b new-branch") is not None
    assert _is_git_branch_switch("git checkout -B new-branch") is not None
    assert _is_git_branch_switch("git switch main") is not None
    assert _is_git_branch_switch("git switch -c new-branch") is not None


# ══════════════════════════════════════════════════════════════════════════════
# 4. _is_git_branch_switch — checkout -- file is NOT branch switch
# ══════════════════════════════════════════════════════════════════════════════

def test_checkout_file_not_branch_switch():
    assert _is_git_branch_switch("git checkout -- file.py") is None
    assert _is_git_branch_switch("git checkout -- .") is None


# ══════════════════════════════════════════════════════════════════════════════
# 5. git commit does not trigger rollback/branch-switch
# ══════════════════════════════════════════════════════════════════════════════

def test_commit_not_rollback_not_switch():
    assert _is_git_rollback('git commit -m "x"') is False
    assert _is_git_branch_switch('git commit -m "x"') is None


# ══════════════════════════════════════════════════════════════════════════════
# 6. rollback triggers reconcile + snapshot (end-to-end)
# ══════════════════════════════════════════════════════════════════════════════

def test_rollback_triggers_reconcile_snapshot(tmp_path, http_app):
    """git reset --hard via post_bash → reconcile runs + reason='rollback' snapshot created."""
    import buer.mcp.server as srv

    _init_git_repo(tmp_path)
    (tmp_path / "a.py").write_text("def f(): pass\n")

    db_file = str(tmp_path / "test.db")
    s = Store(db_file)
    pid = s.get_or_create_project(str(tmp_path), branch="main")
    s.close()

    orig_db = srv._db_path
    srv._db_path = db_file
    # _set_store_for_testing with a new Store so post_bash can find the project
    s2 = Store(db_file)
    _set_store_for_testing(s2)
    try:
        tc = TestClient(http_app)
        resp = tc.post("/buer/post-bash", json={
            "cwd": str(tmp_path),
            "tool_input": {"command": "git reset --hard HEAD"},
            "tool_response": {"stdout": "ok", "stderr": "", "interrupted": False},
        })
        assert resp.status_code == 200

        # Give background thread time to complete
        deadline = time.time() + 5.0
        while time.time() < deadline:
            s3 = Store(db_file)
            rows = s3.con.execute(
                "SELECT * FROM snapshots WHERE project_id=? AND reason='rollback'", (pid,)
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
        "SELECT * FROM snapshots WHERE project_id=? AND reason='rollback'", (pid,)
    ).fetchall()
    s3.close()
    assert len(rows) >= 1
    assert len(rows[0]["commit_hash"]) == 40


# ══════════════════════════════════════════════════════════════════════════════
# 7. branch switch creates new project + snapshot
# ══════════════════════════════════════════════════════════════════════════════

def test_branch_switch_creates_project_and_snapshot(tmp_path, http_app):
    """checkout feature branch via post_bash → new (root,feature) pid + branch_switch snapshot."""
    import buer.mcp.server as srv

    _init_git_repo(tmp_path)
    # Create and switch to feature branch so get_current_branch returns 'feature'
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=str(tmp_path),
                   check=True, capture_output=True)

    db_file = str(tmp_path / "test.db")
    s = Store(db_file)
    pid_main = s.get_or_create_project(str(tmp_path), branch="main")
    s.close()

    orig_db = srv._db_path
    srv._db_path = db_file
    s2 = Store(db_file)
    _set_store_for_testing(s2)
    try:
        tc = TestClient(http_app)
        resp = tc.post("/buer/post-bash", json={
            "cwd": str(tmp_path),
            "tool_input": {"command": "git checkout feature"},
            "tool_response": {"stdout": "ok", "stderr": "", "interrupted": False},
        })
        assert resp.status_code == 200

        # Wait for background thread
        deadline = time.time() + 5.0
        while time.time() < deadline:
            s3 = Store(db_file)
            rows = s3.con.execute(
                "SELECT * FROM snapshots WHERE reason='branch_switch'"
            ).fetchall()
            s3.close()
            if rows:
                break
            time.sleep(0.05)
    finally:
        srv._db_path = orig_db
        s2.close()

    s3 = Store(db_file)
    rows = s3.con.execute("SELECT * FROM snapshots WHERE reason='branch_switch'").fetchall()
    assert len(rows) >= 1

    snap_pid = rows[0]["project_id"]
    proj = s3.con.execute("SELECT branch FROM projects WHERE id=?", (snap_pid,)).fetchone()
    assert proj["branch"] == "feature"
    assert snap_pid != pid_main
    s3.close()


# ══════════════════════════════════════════════════════════════════════════════
# 8. switching back to existing branch reuses project_id
# ══════════════════════════════════════════════════════════════════════════════

def test_branch_switch_back_reuses_project(tmp_path):
    """checkout feature → pid_f; checkout main → pid_m; checkout feature → same pid_f."""
    _init_git_repo(tmp_path)
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=str(tmp_path),
                   check=True, capture_output=True)
    subprocess.run(["git", "checkout", "main"], cwd=str(tmp_path),
                   check=True, capture_output=True)

    s = Store(":memory:")
    # First switch: feature branch
    subprocess.run(["git", "checkout", "feature"], cwd=str(tmp_path),
                   check=True, capture_output=True)
    pid_f1 = s.get_or_create_project(str(tmp_path), branch="feature")

    # Back to main
    subprocess.run(["git", "checkout", "main"], cwd=str(tmp_path),
                   check=True, capture_output=True)
    pid_m = s.get_or_create_project(str(tmp_path), branch="main")

    # Second switch back to feature → same pid
    subprocess.run(["git", "checkout", "feature"], cwd=str(tmp_path),
                   check=True, capture_output=True)
    pid_f2 = s.get_or_create_project(str(tmp_path), branch="feature")

    assert pid_f1 == pid_f2
    assert pid_f1 != pid_m
    s.close()


# ══════════════════════════════════════════════════════════════════════════════
# 9. non-git directory: no trigger, no crash
# ══════════════════════════════════════════════════════════════════════════════

def test_non_git_no_trigger(tmp_path, http_app):
    """Non-git dir receives git reset → no crash, no snapshot."""
    s = Store(":memory:")
    pid = s.get_or_create_project(str(tmp_path))
    _set_store_for_testing(s)

    tc = TestClient(http_app)
    resp = tc.post("/buer/post-bash", json={
        "cwd": str(tmp_path),
        "tool_input": {"command": "git reset --hard HEAD"},
        "tool_response": {"stdout": "ok", "stderr": ""},
    })
    assert resp.status_code == 200
    time.sleep(0.1)
    rows = s.con.execute("SELECT * FROM snapshots WHERE project_id=?", (pid,)).fetchall()
    assert len(rows) == 0
    s.close()


# ══════════════════════════════════════════════════════════════════════════════
# 10. _full_ingest_in_background without reason → no snapshot
# ══════════════════════════════════════════════════════════════════════════════

def test_full_ingest_default_no_snapshot(tmp_path):
    """_full_ingest_in_background(reason=None) does not create a snapshot."""
    _init_git_repo(tmp_path)
    (tmp_path / "b.py").write_text("def g(): pass\n")

    # Use a real file-backed DB so background thread can connect
    import tempfile, os
    db_file = tmp_path / "test.db"
    s = Store(str(db_file))
    pid = s.get_or_create_project(str(tmp_path), branch="main")
    s.close()

    # Patch _db_path temporarily
    import buer.mcp.server as srv
    orig = srv._db_path
    srv._db_path = str(db_file)
    try:
        _full_ingest_in_background(pid, str(tmp_path))  # no snapshot_reason
    finally:
        srv._db_path = orig

    s2 = Store(str(db_file))
    rows = s2.con.execute("SELECT * FROM snapshots WHERE project_id=?", (pid,)).fetchall()
    assert len(rows) == 0
    s2.close()
