"""Tests for git integration batch 2: snapshots table + post_bash git commit detection."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from buer.mcp.server import _is_git_commit, mcp, _set_store_for_testing
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


# ══════════════════════════════════════════════════════════════════════════════
# 1-3. create_snapshot / latest / has
# ══════════════════════════════════════════════════════════════════════════════

def test_create_snapshot(tmp_path):
    """create_snapshot stores row with correct fields."""
    s = _store()
    pid = s.get_or_create_project(str(tmp_path), branch="main")
    snap_id = s.create_snapshot(pid, "abc1234" * 5 + "abcd", "main", 42, "parent0", "commit")
    row = s.con.execute("SELECT * FROM snapshots WHERE id=?", (snap_id,)).fetchone()
    assert row["project_id"] == pid
    assert row["commit_hash"].startswith("abc1234")
    assert row["branch"] == "main"
    assert row["snapshot_at_seq"] == 42
    assert row["parent_commit"] == "parent0"
    assert row["reason"] == "commit"
    s.close()


def test_multiple_snapshots_same_commit(tmp_path):
    """Append-only: same (pid, commit) can have multiple records; latest returns most recent."""
    import time
    s = _store()
    pid = s.get_or_create_project(str(tmp_path))
    h = "a" * 40
    s.create_snapshot(pid, h, "main", 10, None, "commit")
    time.sleep(0.01)
    s.create_snapshot(pid, h, "main", 20, None, "commit")
    rows = s.con.execute(
        "SELECT id FROM snapshots WHERE project_id=? AND commit_hash=?", (pid, h)
    ).fetchall()
    assert len(rows) == 2
    latest = s.latest_snapshot_for_commit(pid, h)
    assert latest["snapshot_at_seq"] == 20  # most recent has seq=20
    s.close()


def test_has_snapshot(tmp_path):
    """has_snapshot_for_commit correctly reflects presence/absence."""
    s = _store()
    pid = s.get_or_create_project(str(tmp_path))
    h = "b" * 40
    assert s.has_snapshot_for_commit(pid, h) is False
    s.create_snapshot(pid, h, None, 5)
    assert s.has_snapshot_for_commit(pid, h) is True
    s.close()


def test_snapshot_at_seq(tmp_path):
    """snapshot_at_seq equals max_seq at snapshot time."""
    from buer.reconcile import reconcile
    (tmp_path / "a.py").write_text("def f(): pass\n")
    s = _store()
    pid = s.get_or_create_project(str(tmp_path))
    reconcile(s, pid, [str(tmp_path / "a.py")])
    seq = s.max_seq(pid)
    assert seq > 0
    snap_id = s.create_snapshot(pid, "c" * 40, "main", seq)
    row = s.con.execute("SELECT snapshot_at_seq FROM snapshots WHERE id=?", (snap_id,)).fetchone()
    assert row["snapshot_at_seq"] == seq
    s.close()


# ══════════════════════════════════════════════════════════════════════════════
# 4-6. _is_git_commit
# ══════════════════════════════════════════════════════════════════════════════

def test_is_git_commit_basic():
    assert _is_git_commit('git commit -m "x"') is True


def test_is_git_commit_variants():
    assert _is_git_commit("git commit -am 'x'") is True
    assert _is_git_commit("git -C /some/path commit -m 'x'") is True
    assert _is_git_commit("git commit --amend") is True
    assert _is_git_commit("git commit") is True


def test_is_git_commit_negatives():
    assert _is_git_commit("git log") is False
    assert _is_git_commit("git log --oneline") is False
    assert _is_git_commit("git show HEAD") is False
    assert _is_git_commit("git status") is False
    assert _is_git_commit("git diff") is False
    assert _is_git_commit("echo git commit") is False
    assert _is_git_commit("git log --grep=commit") is False


# ══════════════════════════════════════════════════════════════════════════════
# 7-10. Integration: post_bash endpoint
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def reset_store():
    yield
    _set_store_for_testing(None)


@pytest.fixture
def http_app():
    return mcp.streamable_http_app()


@pytest.fixture
def git_client(tmp_path, http_app):
    """TestClient with a real git repo + in-memory store."""
    _init_git_repo(tmp_path)
    s = Store(":memory:")
    pid = s.get_or_create_project(str(tmp_path), branch="main")
    _set_store_for_testing(s)
    yield TestClient(http_app), s, pid, tmp_path
    s.close()


def test_post_bash_commit_creates_snapshot(git_client):
    """post_bash receives git commit command + valid git repo → snapshot created."""
    tc, store, pid, tmp_path = git_client

    resp = tc.post("/buer/post-bash", json={
        "cwd": str(tmp_path),
        "tool_input": {"command": 'git commit -m "test"'},
        "tool_response": {"stdout": "ok", "stderr": "", "interrupted": False},
    })
    assert resp.status_code == 200
    rows = store.con.execute(
        "SELECT * FROM snapshots WHERE project_id=?", (pid,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["reason"] == "commit"
    assert len(rows[0]["commit_hash"]) == 40


def test_post_bash_non_commit_no_snapshot(git_client):
    """post_bash receives 'git status' → no snapshot created."""
    tc, store, pid, tmp_path = git_client

    resp = tc.post("/buer/post-bash", json={
        "cwd": str(tmp_path),
        "tool_input": {"command": "git status"},
        "tool_response": {"stdout": "ok", "stderr": ""},
    })
    assert resp.status_code == 200
    rows = store.con.execute(
        "SELECT * FROM snapshots WHERE project_id=?", (pid,)
    ).fetchall()
    assert len(rows) == 0


def test_commit_no_git_repo_graceful(tmp_path, http_app):
    """Non-git dir receives 'git commit' → no crash, no snapshot (graceful)."""
    s = Store(":memory:")
    pid = s.get_or_create_project(str(tmp_path))
    _set_store_for_testing(s)

    tc = TestClient(http_app)
    resp = tc.post("/buer/post-bash", json={
        "cwd": str(tmp_path),
        "tool_input": {"command": 'git commit -m "x"'},
        "tool_response": {"stdout": "ok", "stderr": ""},
    })
    assert resp.status_code == 200
    rows = s.con.execute("SELECT * FROM snapshots WHERE project_id=?", (pid,)).fetchall()
    assert len(rows) == 0
    s.close()
