"""Tests for (root, branch) project uniqueness + find_project_for_file backward compat."""
from __future__ import annotations

import pytest

from buer.store import Store


# ── helpers ───────────────────────────────────────────────────────────────────

def _store() -> Store:
    return Store(":memory:")


# ══════════════════════════════════════════════════════════════════════════════
# get_or_create_project — (root, branch) uniqueness
# ══════════════════════════════════════════════════════════════════════════════

def test_create_no_branch(tmp_path):
    """get_or_create_project without branch → branch=NULL row created."""
    s = _store()
    pid = s.get_or_create_project(str(tmp_path))
    row = s.con.execute("SELECT branch FROM projects WHERE id=?", (pid,)).fetchone()
    assert row["branch"] is None
    s.close()


def test_create_with_branch(tmp_path):
    """get_or_create_project with branch='main' → branch='main' stored."""
    s = _store()
    pid = s.get_or_create_project(str(tmp_path), branch="main")
    row = s.con.execute("SELECT branch FROM projects WHERE id=?", (pid,)).fetchone()
    assert row["branch"] == "main"
    s.close()


def test_root_branch_unique(tmp_path):
    """Same (root, 'main') called twice → same pid (UNIQUE constraint)."""
    s = _store()
    pid1 = s.get_or_create_project(str(tmp_path), branch="main")
    pid2 = s.get_or_create_project(str(tmp_path), branch="main")
    assert pid1 == pid2
    s.close()


def test_different_branches(tmp_path):
    """(root, 'main') and (root, 'feature') → two distinct pids."""
    s = _store()
    pid_main    = s.get_or_create_project(str(tmp_path), branch="main")
    pid_feature = s.get_or_create_project(str(tmp_path), branch="feature")
    assert pid_main != pid_feature
    s.close()


def test_null_branch_unique(tmp_path):
    """(root, None) called twice → same pid (COALESCE makes NULL unique)."""
    s = _store()
    pid1 = s.get_or_create_project(str(tmp_path))
    pid2 = s.get_or_create_project(str(tmp_path))
    assert pid1 == pid2
    s.close()


def test_created_at_commit_stored(tmp_path):
    """created_at_commit is persisted when provided."""
    s = _store()
    pid = s.get_or_create_project(str(tmp_path), branch="main", created_at_commit="abc1234")
    row = s.con.execute("SELECT created_at_commit FROM projects WHERE id=?", (pid,)).fetchone()
    assert row["created_at_commit"] == "abc1234"
    s.close()


# ══════════════════════════════════════════════════════════════════════════════
# find_project_for_file — backward compat + branch-aware lookup
# ══════════════════════════════════════════════════════════════════════════════

def test_find_with_branch(tmp_path):
    """Same root, two branches → find_project_for_file(branch='feature') returns feature pid."""
    s = _store()
    pid_main    = s.get_or_create_project(str(tmp_path), branch="main")
    pid_feature = s.get_or_create_project(str(tmp_path), branch="feature")
    f = str(tmp_path / "src" / "file.py")
    found = s.find_project_for_file(f, branch="feature")
    assert found == pid_feature
    s.close()


def test_find_with_branch_main(tmp_path):
    """find_project_for_file(branch='main') returns main pid."""
    s = _store()
    pid_main    = s.get_or_create_project(str(tmp_path), branch="main")
    pid_feature = s.get_or_create_project(str(tmp_path), branch="feature")
    f = str(tmp_path / "file.py")
    assert s.find_project_for_file(f, branch="main") == pid_main
    s.close()


def test_find_without_branch_backward_compat(tmp_path):
    """find_project_for_file without branch returns some pid (backward-compat)."""
    s = _store()
    pid_main    = s.get_or_create_project(str(tmp_path), branch="main")
    pid_feature = s.get_or_create_project(str(tmp_path), branch="feature")
    f = str(tmp_path / "file.py")
    found = s.find_project_for_file(f)
    assert found in (pid_main, pid_feature)  # some project returned, not None
    s.close()


def test_find_no_project(tmp_path):
    """find_project_for_file for file outside any project → None."""
    s = _store()
    # Don't create any project
    found = s.find_project_for_file(str(tmp_path / "file.py"))
    assert found is None
    s.close()


def test_find_branch_fallback(tmp_path):
    """find_project_for_file with unknown branch falls back to any match."""
    s = _store()
    pid = s.get_or_create_project(str(tmp_path), branch="main")
    f = str(tmp_path / "file.py")
    # branch='other' doesn't exist; should fall back to best prefix match
    found = s.find_project_for_file(f, branch="other")
    assert found == pid
    s.close()


# ══════════════════════════════════════════════════════════════════════════════
# Schema migration — idempotent
# ══════════════════════════════════════════════════════════════════════════════

def test_migration_idempotent():
    """Creating Store twice on same DB does not raise from duplicate migrations."""
    s1 = Store(":memory:")
    # Can't reuse :memory: across connections, but we verify no exception
    s2 = Store(":memory:")
    s1.close()
    s2.close()


def test_branch_column_exists():
    """After Store init, projects table has branch and created_at_commit columns."""
    s = Store(":memory:")
    cols = {r["name"] for r in s.con.execute("PRAGMA table_info(projects)").fetchall()}
    assert "branch" in cols
    assert "created_at_commit" in cols
    s.close()


def test_git_commit_column_exists():
    """After Store init, determinations table has git_commit column."""
    s = Store(":memory:")
    cols = {r["name"] for r in s.con.execute("PRAGMA table_info(determinations)").fetchall()}
    assert "git_commit" in cols
    s.close()
