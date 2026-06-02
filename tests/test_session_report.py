"""Tests for BUER Recap (session_report.py) — 11 tests.

Coverage:
  1.  test_no_last_session            — no ended session → build_recap returns None
  2.  test_empty_session_no_output    — session with no changes, no incidents → None
  3.  test_recap_has_changes          — changed files appear in recap
  4.  test_recap_has_unresolved       — unresolved incident → plain-language description
  5.  test_resolved_not_in_unresolved — resolved incident does NOT appear in "still unresolved"
  6.  test_buer_work_record           — N flagged M resolved → work-record line present
  7.  test_relative_time              — session 2 days ago → "2 days ago" in output
  8.  test_teaser_shorter             — teaser is significantly shorter than full recap
  9.  test_no_fabricated_savings      — report must not contain token/saved/省 etc.
  10. test_signal_not_leaked          — signal names / technical terms must not appear in output
  11. test_files_truncated            — >3 changed files → "等N处" shown
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta

import pytest

from buer.session_report import build_recap, build_teaser
from buer.store import Store


# ── helpers ────────────────────────────────────────────────────────────────────

def _store() -> Store:
    return Store(":memory:")


def _project(store: Store, root: str) -> int:
    return store.get_or_create_project(root)


def _write_py(path: str, content: str = "def fn(): pass\n") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _open_close_session(
    store: Store,
    pid: int,
    session_id: str,
    started_at: datetime,
    ended_at: datetime,
) -> None:
    """Directly insert a completed session row for testing."""
    store.con.execute(
        "INSERT OR IGNORE INTO sessions "
        "(session_id, project_id, start_seq, end_seq, started_at, ended_at) "
        "VALUES (?, ?, 0, 0, ?, ?)",
        (session_id, pid, _fmt(started_at), _fmt(ended_at)),
    )
    store.con.commit()


def _insert_incident(
    store: Store,
    pid: int,
    signal: str,
    target_node: str,
    state: str,
    created_at: datetime,
) -> None:
    store.con.execute(
        "INSERT INTO incidents (project_id, signal, target_node, state, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (pid, signal, target_node, state, _fmt(created_at), _fmt(created_at)),
    )
    store.con.commit()


def _insert_determination(
    store: Store,
    pid: int,
    file_path: str,
    define_name: str,
    seq: int,
    edit_type: str = "create",
) -> None:
    store.con.execute(
        "INSERT INTO determinations "
        "(project_id, seq, file_path, define_name, edit_type, created_at) "
        "VALUES (?, ?, ?, ?, ?, datetime('now'))",
        (pid, seq, file_path, define_name, edit_type),
    )
    store.con.commit()


# ── 1. no ended session → None ─────────────────────────────────────────────────

def test_no_last_session():
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)
        assert build_recap(store, pid) is None


# ── 2. empty session → None ────────────────────────────────────────────────────

def test_empty_session_no_output():
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)
        now = datetime.utcnow()
        _open_close_session(store, pid, "sess-empty", now - timedelta(hours=2), now - timedelta(hours=1))
        # No determinations, no incidents
        assert build_recap(store, pid) is None


# ── 3. changed files appear in recap ───────────────────────────────────────────

def test_recap_has_changes():
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)
        fa = os.path.join(root, "alpha.py")
        _write_py(fa)

        now = datetime.utcnow()
        start = now - timedelta(hours=3)
        end = now - timedelta(hours=1)
        _open_close_session(store, pid, "sess-changes", start, end)

        # Determination inside the session's seq range — store uses seq not time for changes_for_session
        # Use start_seq=0, end_seq=10, insert det with seq=5
        store.con.execute(
            "UPDATE sessions SET start_seq=0, end_seq=10 WHERE session_id='sess-changes' AND project_id=?",
            (pid,),
        )
        store.con.commit()
        _insert_determination(store, pid, fa, "fn", seq=5)

        recap = build_recap(store, pid)
        assert recap is not None
        assert "alpha.py" in recap
        assert "changed" in recap


# ── 4. unresolved incident → plain-language description ───────────────────────

def test_recap_has_unresolved():
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)
        now = datetime.utcnow()
        sess_start = now - timedelta(hours=4)
        sess_end = now - timedelta(hours=2)
        _open_close_session(store, pid, "sess-unres", sess_start, sess_end)

        store.con.execute(
            "UPDATE sessions SET start_seq=0, end_seq=10 WHERE session_id='sess-unres' AND project_id=?",
            (pid,),
        )
        store.con.commit()
        _insert_determination(store, pid, os.path.join(root, "mod.py"), "fn", seq=5)

        mid = sess_start + timedelta(hours=1)
        _insert_incident(store, pid, "stuck_region", "src/mod.py", "open", mid)

        recap = build_recap(store, pid)
        assert recap is not None
        assert "still unresolved" in recap
        assert "mod.py" in recap


# ── 5. resolved incident not in "still unresolved" ───────────────────────────

def test_resolved_not_in_unresolved():
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)
        now = datetime.utcnow()
        sess_start = now - timedelta(hours=4)
        sess_end = now - timedelta(hours=2)
        _open_close_session(store, pid, "sess-resolved", sess_start, sess_end)

        store.con.execute(
            "UPDATE sessions SET start_seq=0, end_seq=10 WHERE session_id='sess-resolved' AND project_id=?",
            (pid,),
        )
        store.con.commit()
        _insert_determination(store, pid, os.path.join(root, "x.py"), "fn", seq=5)

        mid = sess_start + timedelta(hours=1)
        _insert_incident(store, pid, "regression", "src/x.py", "resolved", mid)

        recap = build_recap(store, pid)
        # May or may not have content (work-record line), but "还没解决" must be absent
        if recap is not None:
            assert "still unresolved" not in recap


# ── 6. work-record line present ───────────────────────────────────────────────

def test_buer_work_record():
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)
        now = datetime.utcnow()
        sess_start = now - timedelta(hours=5)
        sess_end = now - timedelta(hours=1)
        _open_close_session(store, pid, "sess-work", sess_start, sess_end)

        store.con.execute(
            "UPDATE sessions SET start_seq=0, end_seq=20 WHERE session_id='sess-work' AND project_id=?",
            (pid,),
        )
        store.con.commit()
        _insert_determination(store, pid, os.path.join(root, "a.py"), "fn", seq=5)

        mid = sess_start + timedelta(hours=1)
        mid2 = sess_start + timedelta(hours=2)
        _insert_incident(store, pid, "stuck_region", "a.py", "open", mid)
        _insert_incident(store, pid, "debug_loop", "b.py", "resolved", mid2)

        recap = build_recap(store, pid)
        assert recap is not None
        # Work-record line: 2 total, 1 resolved
        assert "flagged by BUER" in recap
        assert "2 of these" in recap


# ── 7. relative time ──────────────────────────────────────────────────────────

def test_relative_time():
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)
        now = datetime.utcnow()
        sess_start = now - timedelta(days=2, hours=1)
        sess_end = now - timedelta(days=2)
        _open_close_session(store, pid, "sess-2days", sess_start, sess_end)

        store.con.execute(
            "UPDATE sessions SET start_seq=0, end_seq=10 WHERE session_id='sess-2days' AND project_id=?",
            (pid,),
        )
        store.con.commit()
        _insert_determination(store, pid, os.path.join(root, "f.py"), "fn", seq=5)

        recap = build_recap(store, pid)
        assert recap is not None
        assert "2 days ago" in recap


# ── 8. teaser shorter than full recap ─────────────────────────────────────────

def test_teaser_shorter():
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)
        now = datetime.utcnow()
        sess_start = now - timedelta(hours=3)
        sess_end = now - timedelta(hours=1)
        _open_close_session(store, pid, "sess-teaser", sess_start, sess_end)

        store.con.execute(
            "UPDATE sessions SET start_seq=0, end_seq=10 WHERE session_id='sess-teaser' AND project_id=?",
            (pid,),
        )
        store.con.commit()
        _insert_determination(store, pid, os.path.join(root, "x.py"), "fn", seq=5)

        mid = sess_start + timedelta(minutes=30)
        _insert_incident(store, pid, "stuck_region", "x.py", "open", mid)

        recap = build_recap(store, pid)
        teaser = build_teaser(store, pid)
        assert recap is not None
        assert teaser is not None
        assert len(teaser) < len(recap), (
            f"teaser ({len(teaser)}) must be shorter than recap ({len(recap)})"
        )


# ── 9. no fabricated savings ──────────────────────────────────────────────────

def test_no_fabricated_savings():
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)
        now = datetime.utcnow()
        sess_start = now - timedelta(hours=3)
        sess_end = now - timedelta(hours=1)
        _open_close_session(store, pid, "sess-nosave", sess_start, sess_end)

        store.con.execute(
            "UPDATE sessions SET start_seq=0, end_seq=10 WHERE session_id='sess-nosave' AND project_id=?",
            (pid,),
        )
        store.con.commit()
        _insert_determination(store, pid, os.path.join(root, "a.py"), "fn", seq=5)

        recap = build_recap(store, pid) or ""
        teaser = build_teaser(store, pid) or ""
        combined = (recap + teaser).lower()

        for forbidden in ("token", "saved", "节省", "省了", "省去"):
            assert forbidden not in combined, (
                f"fabricated savings word '{forbidden}' must not appear in recap/teaser"
            )


# ── 10. signal names not leaked ───────────────────────────────────────────────

def test_signal_not_leaked():
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)
        now = datetime.utcnow()
        sess_start = now - timedelta(hours=3)
        sess_end = now - timedelta(hours=1)
        _open_close_session(store, pid, "sess-leak", sess_start, sess_end)

        store.con.execute(
            "UPDATE sessions SET start_seq=0, end_seq=10 WHERE session_id='sess-leak' AND project_id=?",
            (pid,),
        )
        store.con.commit()
        _insert_determination(store, pid, os.path.join(root, "b.py"), "fn", seq=5)

        for sig in ("stuck_region", "debug_loop", "regression", "dangling_reference",
                    "test_tampering", "boundary_breach", "task_scope_breach"):
            mid = sess_start + timedelta(minutes=30)
            _insert_incident(store, pid, sig, "b.py", "open", mid)

        recap = build_recap(store, pid) or ""
        teaser = build_teaser(store, pid) or ""
        combined = recap + teaser

        for term in ("stuck_region", "debug_loop", "dangling_reference",
                     "test_tampering", "boundary_breach", "task_scope_breach",
                     "incident", "determination", "signal"):
            assert term not in combined, (
                f"technical term '{term}' must not be leaked into recap/teaser"
            )


# ── 11. >3 files → "等N处" ────────────────────────────────────────────────────

def test_files_truncated():
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)
        now = datetime.utcnow()
        sess_start = now - timedelta(hours=3)
        sess_end = now - timedelta(hours=1)
        _open_close_session(store, pid, "sess-trunc", sess_start, sess_end)

        store.con.execute(
            "UPDATE sessions SET start_seq=0, end_seq=50 WHERE session_id='sess-trunc' AND project_id=?",
            (pid,),
        )
        store.con.commit()

        # 5 distinct files, each with a determination inside seq range
        files = [os.path.join(root, f"file{i}.py") for i in range(5)]
        for i, fp in enumerate(files):
            _write_py(fp)
            _insert_determination(store, pid, fp, "fn", seq=i + 1)

        recap = build_recap(store, pid)
        assert recap is not None
        assert "more" in recap, (
            f"truncation marker 'and N more' must appear for 5 files; got: {recap}"
        )
