"""Tests for Define.start_line / end_line persistence through store + reconcile.

Coverage:
  1. Store insert_determination writes start_line/end_line correctly
  2. Store insert_determination_atomic writes start_line/end_line correctly
  3. Delete determinations default to 0/0
  4. Old store (schema without columns) migrates via ALTER TABLE; inserts succeed
  5. Reconcile: Python define line ranges persisted into determinations table
  6. Reconcile: JS define line ranges persisted
  7. Reconcile: delete records have 0/0 line range
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

from buer.reconcile import reconcile
from buer.store import Store


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_store() -> tuple[Store, int, str]:
    store = Store(":memory:")
    root = tempfile.mkdtemp()
    pid = store.get_or_create_project(root)
    return store, pid, root


# ── 1. insert_determination ───────────────────────────────────────────────────

class TestInsertDetermination:
    def test_writes_start_and_end_line(self):
        store, pid, _ = _make_store()
        det_id = store.insert_determination(
            pid, 1, "/tmp/f.py", "fn", "fp", "create",
            start_line=10, end_line=25,
        )
        row = store.con.execute(
            "SELECT start_line, end_line FROM determinations WHERE id=?", (det_id,)
        ).fetchone()
        assert row["start_line"] == 10
        assert row["end_line"] == 25

    def test_defaults_to_zero(self):
        store, pid, _ = _make_store()
        det_id = store.insert_determination(pid, 1, "/tmp/f.py", "fn", "fp", "create")
        row = store.con.execute(
            "SELECT start_line, end_line FROM determinations WHERE id=?", (det_id,)
        ).fetchone()
        assert row["start_line"] == 0
        assert row["end_line"] == 0


# ── 2. insert_determination_atomic ────────────────────────────────────────────

class TestInsertDeterminationAtomic:
    def test_writes_start_and_end_line(self):
        store, pid, _ = _make_store()
        det_id, _ = store.insert_determination_atomic(
            pid, "/tmp/f.py", "fn", "fp", "create",
            start_line=42, end_line=55,
        )
        row = store.con.execute(
            "SELECT start_line, end_line FROM determinations WHERE id=?", (det_id,)
        ).fetchone()
        assert row["start_line"] == 42
        assert row["end_line"] == 55

    def test_defaults_to_zero(self):
        store, pid, _ = _make_store()
        det_id, _ = store.insert_determination_atomic(
            pid, "/tmp/f.py", "fn", "fp", "create",
        )
        row = store.con.execute(
            "SELECT start_line, end_line FROM determinations WHERE id=?", (det_id,)
        ).fetchone()
        assert row["start_line"] == 0
        assert row["end_line"] == 0

    def test_delete_record_has_zero_lines(self):
        store, pid, _ = _make_store()
        det_id, _ = store.insert_determination_atomic(
            pid, "/tmp/f.py", "gone_fn", None, "delete",
        )
        row = store.con.execute(
            "SELECT start_line, end_line FROM determinations WHERE id=?", (det_id,)
        ).fetchone()
        assert row["start_line"] == 0
        assert row["end_line"] == 0


# ── 3. old DB migration ───────────────────────────────────────────────────────

class TestOldDbMigration:
    def test_schema_sql_base_migrates_correctly(self, tmp_path):
        """Open a store built from schema.sql (no start/end_line), verify ALTER adds them."""
        schema_path = Path(__file__).parent.parent / "buer" / "schema.sql"
        db_path = str(tmp_path / "old.sqlite")
        # Bootstrap with base schema (no start_line/end_line columns)
        con = sqlite3.connect(db_path)
        con.executescript(schema_path.read_text())
        con.close()
        # Store.__init__ should ADD the columns via idempotent ALTER
        store = Store(db_path)
        cols = {r[1] for r in store.con.execute("PRAGMA table_info(determinations)")}
        assert "start_line" in cols
        assert "end_line" in cols

    def test_insert_after_migration_succeeds(self, tmp_path):
        schema_path = Path(__file__).parent.parent / "buer" / "schema.sql"
        db_path = str(tmp_path / "old.sqlite")
        con = sqlite3.connect(db_path)
        con.executescript(schema_path.read_text())
        con.close()
        store = Store(db_path)
        pid = store.get_or_create_project(str(tmp_path))
        det_id = store.insert_determination(
            pid, 1, str(tmp_path / "f.py"), "fn", "fp", "create",
            start_line=5, end_line=10,
        )
        row = store.con.execute(
            "SELECT start_line, end_line FROM determinations WHERE id=?", (det_id,)
        ).fetchone()
        assert row["start_line"] == 5
        assert row["end_line"] == 10

    def test_migration_idempotent(self, tmp_path):
        """Running Store.__init__ twice on the same DB must not raise."""
        db_path = str(tmp_path / "store.sqlite")
        store1 = Store(db_path)
        store1.con.close()
        store2 = Store(db_path)  # should not raise "duplicate column"
        cols = {r[1] for r in store2.con.execute("PRAGMA table_info(determinations)")}
        assert "start_line" in cols


# ── 4. reconcile persists line ranges ────────────────────────────────────────

class TestReconcileLineRanges:
    def test_python_line_ranges_persisted(self, tmp_path):
        root = str(tmp_path)
        src = (
            "def foo(x):\n"
            "    return x + 1\n"
            "\n"
            "\n"
            "def bar():\n"
            "    pass\n"
        )
        fp = str(tmp_path / "src.py")
        Path(fp).write_text(src)
        store = Store(":memory:")
        pid = store.get_or_create_project(root)
        reconcile(store, pid, [fp])
        rows = store.con.execute(
            "SELECT define_name, start_line, end_line FROM determinations "
            "WHERE project_id=? AND file_path=? AND edit_type != 'delete'",
            (pid, fp),
        ).fetchall()
        by_name = {r["define_name"]: r for r in rows}
        assert "foo" in by_name
        assert by_name["foo"]["start_line"] == 1
        assert by_name["foo"]["end_line"] == 2
        assert "bar" in by_name
        assert by_name["bar"]["start_line"] == 5
        assert by_name["bar"]["end_line"] == 6

    def test_js_line_ranges_persisted(self, tmp_path):
        root = str(tmp_path)
        src = (
            "function hello(name) {\n"
            "  return 'hi ' + name;\n"
            "}\n"
            "\n"
            "const greet = (x) => {\n"
            "  return x;\n"
            "};\n"
        )
        fp = str(tmp_path / "src.js")
        Path(fp).write_text(src)
        store = Store(":memory:")
        pid = store.get_or_create_project(root)
        reconcile(store, pid, [fp])
        rows = store.con.execute(
            "SELECT define_name, start_line, end_line FROM determinations "
            "WHERE project_id=? AND file_path=? AND edit_type != 'delete'",
            (pid, fp),
        ).fetchall()
        by_name = {r["define_name"]: r for r in rows}
        assert "hello" in by_name
        assert by_name["hello"]["start_line"] == 1
        assert by_name["hello"]["end_line"] == 3
        assert "greet" in by_name
        assert by_name["greet"]["start_line"] == 5
        assert by_name["greet"]["end_line"] == 7

    def test_delete_determination_has_zero_lines(self, tmp_path):
        root = str(tmp_path)
        fp = str(tmp_path / "src.py")
        Path(fp).write_text("def doomed(): pass\n")
        store = Store(":memory:")
        pid = store.get_or_create_project(root)
        reconcile(store, pid, [fp])
        # Now delete the file and reconcile again
        os.remove(fp)
        reconcile(store, pid, [fp])
        row = store.con.execute(
            "SELECT start_line, end_line FROM determinations "
            "WHERE project_id=? AND file_path=? AND edit_type='delete'",
            (pid, fp),
        ).fetchone()
        assert row is not None
        assert row["start_line"] == 0
        assert row["end_line"] == 0

    def test_line_ranges_nonzero_for_real_defines(self, tmp_path):
        root = str(tmp_path)
        fp = str(tmp_path / "src.py")
        Path(fp).write_text("def fn(x, y):\n    return x + y\n")
        store = Store(":memory:")
        pid = store.get_or_create_project(root)
        reconcile(store, pid, [fp])
        row = store.con.execute(
            "SELECT start_line, end_line FROM determinations "
            "WHERE project_id=? AND define_name='fn'",
            (pid,),
        ).fetchone()
        assert row["start_line"] > 0
        assert row["end_line"] >= row["start_line"]
