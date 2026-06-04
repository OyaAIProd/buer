"""Tests for snapshot query interface: list_snapshots + diff_snapshots."""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from buer.store import Store
from buer.mcp.server import list_snapshots, diff_snapshots, _set_store_for_testing


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


# ══════════════════════════════════════════════════════════════════════════════
# 1. list_snapshots — empty
# ══════════════════════════════════════════════════════════════════════════════

def test_list_snapshots_empty(tmp_path):
    _init_git_repo(tmp_path)
    s = _store()
    s.get_or_create_project(str(tmp_path), branch="main")
    _set_store_for_testing(s)

    result = list_snapshots(str(tmp_path))
    assert "No snapshots" in result
    s.close()


# ══════════════════════════════════════════════════════════════════════════════
# 2. list_snapshots — ordered (newest first)
# ══════════════════════════════════════════════════════════════════════════════

def test_list_snapshots_ordered(tmp_path):
    _init_git_repo(tmp_path)
    s = _store()
    pid = s.get_or_create_project(str(tmp_path), branch="main")
    s.create_snapshot(pid, "a" * 40, "main", 10, None, reason="commit")
    time.sleep(0.01)
    s.create_snapshot(pid, "b" * 40, "main", 20, None, reason="rollback")
    time.sleep(0.01)
    s.create_snapshot(pid, "c" * 40, "main", 30, None, reason="session_start")
    _set_store_for_testing(s)

    result = list_snapshots(str(tmp_path))
    assert "cccccccc" in result
    assert "aaaaaaaa" in result
    # newest first: 'c' should appear before 'a'
    assert result.index("cccccccc") < result.index("aaaaaaaa")
    assert "commit" in result
    assert "rollback" in result
    assert "session_start" in result
    s.close()


# ══════════════════════════════════════════════════════════════════════════════
# 3. define_fingerprints_at_seq — seq boundary
# ══════════════════════════════════════════════════════════════════════════════

def test_define_fingerprints_at_seq(tmp_path):
    s = _store()
    pid = s.get_or_create_project(str(tmp_path))
    f = str(tmp_path / "m.py")
    s.con.execute(
        "INSERT INTO determinations (project_id, seq, file_path, define_name, node_fingerprint, content_hash, edit_type, created_at)"
        " VALUES (?, 1, ?, 'A', 'fp_a_old', 'ch_a_old', 'create', datetime('now'))",
        (pid, f),
    )
    s.con.execute(
        "INSERT INTO determinations (project_id, seq, file_path, define_name, node_fingerprint, content_hash, edit_type, created_at)"
        " VALUES (?, 2, ?, 'B', 'fp_b', 'ch_b', 'create', datetime('now'))",
        (pid, f),
    )
    s.con.execute(
        "INSERT INTO determinations (project_id, seq, file_path, define_name, node_fingerprint, content_hash, edit_type, created_at)"
        " VALUES (?, 3, ?, 'A', 'fp_a_new', 'ch_a_new', 'modify', datetime('now'))",
        (pid, f),
    )
    s.con.commit()

    # define_fingerprints_at_seq now returns {key: content_hash} scalars.
    at2 = s.define_fingerprints_at_seq(pid, 2)
    assert at2[(f, "A")] == "ch_a_old"
    assert at2[(f, "B")] == "ch_b"
    assert len(at2) == 2

    at3 = s.define_fingerprints_at_seq(pid, 3)
    assert at3[(f, "A")] == "ch_a_new"
    assert at3[(f, "B")] == "ch_b"
    assert len(at3) == 2
    s.close()


# ══════════════════════════════════════════════════════════════════════════════
# 4. define_fingerprints_at_seq — delete hides define
# ══════════════════════════════════════════════════════════════════════════════

def test_define_fingerprints_delete(tmp_path):
    s = _store()
    pid = s.get_or_create_project(str(tmp_path))
    f = str(tmp_path / "m.py")
    s.con.execute(
        "INSERT INTO determinations (project_id, seq, file_path, define_name, node_fingerprint, edit_type, created_at)"
        " VALUES (?, 1, ?, 'A', 'fp', 'create', datetime('now'))",
        (pid, f),
    )
    s.con.execute(
        "INSERT INTO determinations (project_id, seq, file_path, define_name, node_fingerprint, edit_type, created_at)"
        " VALUES (?, 2, ?, 'A', NULL, 'delete', datetime('now'))",
        (pid, f),
    )
    s.con.commit()

    at2 = s.define_fingerprints_at_seq(pid, 2)
    assert (f, "A") not in at2
    assert len(at2) == 0
    s.close()


# ══════════════════════════════════════════════════════════════════════════════
# 5. diff_snapshots — added
# ══════════════════════════════════════════════════════════════════════════════

def test_diff_added(tmp_path):
    _init_git_repo(tmp_path)
    s = _store()
    pid = s.get_or_create_project(str(tmp_path), branch="main")
    f = str(tmp_path / "m.py")

    s.con.execute(
        "INSERT INTO determinations (project_id, seq, file_path, define_name, node_fingerprint, edit_type, created_at)"
        " VALUES (?, 1, ?, 'A', 'fp_a', 'create', datetime('now'))",
        (pid, f),
    )
    commit_a = "a" * 40
    s.create_snapshot(pid, commit_a, "main", 1, None, reason="commit")

    s.con.execute(
        "INSERT INTO determinations (project_id, seq, file_path, define_name, node_fingerprint, edit_type, created_at)"
        " VALUES (?, 2, ?, 'B', 'fp_b', 'create', datetime('now'))",
        (pid, f),
    )
    commit_b = "b" * 40
    s.create_snapshot(pid, commit_b, "main", 2, commit_a, reason="commit")
    s.con.commit()
    _set_store_for_testing(s)

    result = diff_snapshots(str(tmp_path), commit_a, commit_b)
    assert "Added" in result
    assert "::B" in result
    s.close()


# ══════════════════════════════════════════════════════════════════════════════
# 6. diff_snapshots — removed
# ══════════════════════════════════════════════════════════════════════════════

def test_diff_removed(tmp_path):
    _init_git_repo(tmp_path)
    s = _store()
    pid = s.get_or_create_project(str(tmp_path), branch="main")
    f = str(tmp_path / "m.py")

    s.con.execute(
        "INSERT INTO determinations (project_id, seq, file_path, define_name, node_fingerprint, edit_type, created_at)"
        " VALUES (?, 1, ?, 'A', 'fp_a', 'create', datetime('now'))",
        (pid, f),
    )
    s.con.execute(
        "INSERT INTO determinations (project_id, seq, file_path, define_name, node_fingerprint, edit_type, created_at)"
        " VALUES (?, 2, ?, 'B', 'fp_b', 'create', datetime('now'))",
        (pid, f),
    )
    commit_a = "a" * 40
    s.create_snapshot(pid, commit_a, "main", 2, None, reason="commit")

    s.con.execute(
        "INSERT INTO determinations (project_id, seq, file_path, define_name, node_fingerprint, edit_type, created_at)"
        " VALUES (?, 3, ?, 'B', NULL, 'delete', datetime('now'))",
        (pid, f),
    )
    commit_b = "b" * 40
    s.create_snapshot(pid, commit_b, "main", 3, commit_a, reason="commit")
    s.con.commit()
    _set_store_for_testing(s)

    result = diff_snapshots(str(tmp_path), commit_a, commit_b)
    assert "Removed" in result
    assert "::B" in result
    s.close()


# ══════════════════════════════════════════════════════════════════════════════
# 7. diff_snapshots — changed
# ══════════════════════════════════════════════════════════════════════════════

def test_diff_changed(tmp_path):
    # Core scenario: same node_fingerprint (coarse/fine unchanged) but different
    # content_hash (body refactored) → compare_snapshots must report "Changed".
    # This validates the content_hash-based second outlet for change detection.
    _init_git_repo(tmp_path)
    s = _store()
    pid = s.get_or_create_project(str(tmp_path), branch="main")
    f = str(tmp_path / "m.py")

    s.con.execute(
        "INSERT INTO determinations (project_id, seq, file_path, define_name, node_fingerprint, content_hash, edit_type, created_at)"
        " VALUES (?, 1, ?, 'A', 'fp_stable', 'ch_old', 'create', datetime('now'))",
        (pid, f),
    )
    commit_a = "a" * 40
    s.create_snapshot(pid, commit_a, "main", 1, None, reason="commit")

    s.con.execute(
        "INSERT INTO determinations (project_id, seq, file_path, define_name, node_fingerprint, content_hash, edit_type, created_at)"
        " VALUES (?, 2, ?, 'A', 'fp_stable', 'ch_new', 'modify', datetime('now'))",
        (pid, f),
    )
    commit_b = "b" * 40
    s.create_snapshot(pid, commit_b, "main", 2, commit_a, reason="commit")
    s.con.commit()
    _set_store_for_testing(s)

    result = diff_snapshots(str(tmp_path), commit_a, commit_b)
    assert "Changed structure" in result
    assert "::A" in result
    s.close()


# ══════════════════════════════════════════════════════════════════════════════
# 8. diff_snapshots — no change
# ══════════════════════════════════════════════════════════════════════════════

def test_diff_no_change(tmp_path):
    _init_git_repo(tmp_path)
    s = _store()
    pid = s.get_or_create_project(str(tmp_path), branch="main")
    f = str(tmp_path / "m.py")

    s.con.execute(
        "INSERT INTO determinations (project_id, seq, file_path, define_name, node_fingerprint, edit_type, created_at)"
        " VALUES (?, 1, ?, 'A', 'fp_a', 'create', datetime('now'))",
        (pid, f),
    )
    commit_a = "a" * 40
    s.create_snapshot(pid, commit_a, "main", 1, None, reason="commit")
    commit_b = "b" * 40
    s.create_snapshot(pid, commit_b, "main", 1, commit_a, reason="commit")
    s.con.commit()
    _set_store_for_testing(s)

    result = diff_snapshots(str(tmp_path), commit_a, commit_b)
    assert "No define-level structural changes" in result
    s.close()


# ══════════════════════════════════════════════════════════════════════════════
# 9. diff_snapshots — commit prefix matching
# ══════════════════════════════════════════════════════════════════════════════

def test_diff_commit_prefix(tmp_path):
    _init_git_repo(tmp_path)
    s = _store()
    pid = s.get_or_create_project(str(tmp_path), branch="main")
    f = str(tmp_path / "m.py")

    s.con.execute(
        "INSERT INTO determinations (project_id, seq, file_path, define_name, node_fingerprint, content_hash, edit_type, created_at)"
        " VALUES (?, 1, ?, 'A', 'fp_stable', 'ch_old', 'create', datetime('now'))",
        (pid, f),
    )
    commit_a = "abcdef1234567890" + "0" * 24
    s.create_snapshot(pid, commit_a, "main", 1, None, reason="commit")

    s.con.execute(
        "INSERT INTO determinations (project_id, seq, file_path, define_name, node_fingerprint, content_hash, edit_type, created_at)"
        " VALUES (?, 2, ?, 'A', 'fp_stable', 'ch_new', 'modify', datetime('now'))",
        (pid, f),
    )
    commit_b = "fedcba9876543210" + "0" * 24
    s.create_snapshot(pid, commit_b, "main", 2, commit_a, reason="commit")
    s.con.commit()
    _set_store_for_testing(s)

    result = diff_snapshots(str(tmp_path), "abcdef12", "fedcba98")
    assert "Changed structure" in result
    assert "::A" in result
    s.close()


# ══════════════════════════════════════════════════════════════════════════════
# 10. diff_snapshots — unknown commit gives friendly error
# ══════════════════════════════════════════════════════════════════════════════

def test_diff_unknown_commit(tmp_path):
    _init_git_repo(tmp_path)
    s = _store()
    pid = s.get_or_create_project(str(tmp_path), branch="main")
    commit_a = "a" * 40
    s.create_snapshot(pid, commit_a, "main", 0, None, reason="commit")
    _set_store_for_testing(s)

    result = diff_snapshots(str(tmp_path), commit_a, "deadbeef")
    assert "No snapshot found" in result
    assert "list_snapshots" in result
    s.close()


# ══════════════════════════════════════════════════════════════════════════════
# 11. diff_snapshots — end-to-end with real git repo
# ══════════════════════════════════════════════════════════════════════════════

def test_diff_e2e(tmp_path):
    """Real git repo: two commits, changed function → diff shows changed define."""
    from buer.reconcile import reconcile

    _init_git_repo(tmp_path)

    py_file = tmp_path / "calc.py"
    py_file.write_text("def add(x, y):\n    return x + y\n")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "v1"], cwd=str(tmp_path),
                   check=True, capture_output=True)
    commit_a = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(tmp_path)
    ).decode().strip()

    s = _store()
    pid = s.get_or_create_project(str(tmp_path), branch="main")
    reconcile(s, pid, [str(py_file)])
    seq_a = s.max_seq(pid)
    s.create_snapshot(pid, commit_a, "main", seq_a, None, reason="commit")

    # Change function (different fingerprint)
    py_file.write_text(
        "def add(x, y):\n    result = x + y\n    print(result)\n    return result\n"
    )
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "v2"], cwd=str(tmp_path),
                   check=True, capture_output=True)
    commit_b = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(tmp_path)
    ).decode().strip()

    reconcile(s, pid, [str(py_file)])
    seq_b = s.max_seq(pid)
    s.create_snapshot(pid, commit_b, "main", seq_b, commit_a, reason="commit")
    _set_store_for_testing(s)

    list_result = list_snapshots(str(tmp_path))
    assert commit_a[:8] in list_result
    assert commit_b[:8] in list_result

    diff_result = diff_snapshots(str(tmp_path), commit_a, commit_b)
    assert "::add" in diff_result
    assert "Changed structure" in diff_result
    assert "Edge-topology diff not included" in diff_result

    s.close()
