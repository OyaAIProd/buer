"""Tests for L3 return-type inference.

L3 resolves x = func(); x.method() when func has an explicit -> Type annotation.
It builds a project-level {func_fqn: return_type} table and, in compute_call_edges,
resolves x = func() assignments via that table, adding {x: return_type} to the
receiver_types dict already used by L1 — no new resolve_callee branch.
"""
from __future__ import annotations

import pytest

from buer.parse import extract_defines
from buer.reconcile import reconcile
from buer.store import Store


# ── helpers ───────────────────────────────────────────────────────────────────

def _py(tmp_path, rel: str, body: str) -> str:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return str(p)


def _reconcile(*files, tmp_path) -> tuple[Store, int]:
    store = Store(":memory:")
    pid = store.get_or_create_project(str(tmp_path))
    reconcile(store, pid, list(files))
    return store, pid


# ══════════════════════════════════════════════════════════════════════════════
# 1. Return-typed function → resolves dynamic assignment receiver
#    c-step ambiguity: OtherClass also has execute()
# ══════════════════════════════════════════════════════════════════════════════

def test_return_typed(tmp_path):
    """get_store() -> Store; s = get_store(); s.execute() → connects to Store.execute."""
    _py(tmp_path, "db.py",
        "class Store:\n    def execute(self): pass\n"
        "def get_store() -> Store: pass\n")
    _py(tmp_path, "other.py", "class OtherClass:\n    def execute(self): pass\n")
    _py(tmp_path, "runner.py",
        "from db import Store, get_store\n"
        "def run():\n"
        "    s = get_store()\n"
        "    s.execute()\n")

    store, pid = _reconcile(
        str(tmp_path / "db.py"),
        str(tmp_path / "other.py"),
        str(tmp_path / "runner.py"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "py::db.Store.execute")
    assert any("run" in c for c in callers), \
        f"L3 must connect s.execute() to db.Store.execute; callers={callers}"
    callers_other = store.callers_of(pid, "py::other.OtherClass.execute")
    assert not any("run" in c for c in callers_other), \
        f"Must not produce false edge to OtherClass.execute; callers={callers_other}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# 2. No return annotation → not inferred (falls back to c-step)
# ══════════════════════════════════════════════════════════════════════════════

def test_no_return_annotation(tmp_path):
    """get_store() with no -> annotation: L3 is silent, dotted call stays unresolved."""
    _py(tmp_path, "db.py", "class Store:\n    def execute(self): pass\n")
    _py(tmp_path, "runner.py",
        "from db import Store\n"
        "def get_store(): pass\n"   # no return annotation
        "def run():\n"
        "    s = get_store()\n"
        "    s.execute()\n")

    store, pid = _reconcile(
        str(tmp_path / "db.py"),
        str(tmp_path / "runner.py"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "py::db.Store.execute")
    assert not any("run" in c for c in callers), \
        f"No annotation → L3 must not infer; callers={callers}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# 3. Optional[Store] return annotation → unwraps to Store
# ══════════════════════════════════════════════════════════════════════════════

def test_optional_return(tmp_path):
    """def f() -> Optional[Store]: L3 unwraps Optional and resolves Store.execute."""
    _py(tmp_path, "db.py", "class Store:\n    def execute(self): pass\n")
    _py(tmp_path, "runner.py",
        "from typing import Optional\n"
        "from db import Store\n"
        "def get_store() -> Optional[Store]: pass\n"
        "def run():\n"
        "    s = get_store()\n"
        "    s.execute()\n")

    store, pid = _reconcile(
        str(tmp_path / "db.py"),
        str(tmp_path / "runner.py"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "py::db.Store.execute")
    assert any("run" in c for c in callers), \
        f"Optional[Store] return must unwrap to Store and resolve; callers={callers}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# 4. Ambiguous function name (two files each define get_store) → not inferred
# ══════════════════════════════════════════════════════════════════════════════

def test_ambiguous_func_not_inferred(tmp_path):
    """Two get_store() functions: ambiguity → L3 conservative, no edge."""
    _py(tmp_path, "db.py",
        "class Store:\n    def execute(self): pass\n"
        "def get_store() -> Store: pass\n")
    _py(tmp_path, "other.py",
        "class OtherStore:\n    def execute(self): pass\n"
        "def get_store() -> OtherStore: pass\n")   # same name, different return
    _py(tmp_path, "runner.py",
        "def run():\n"
        "    s = get_store()\n"   # ambiguous: which get_store?
        "    s.execute()\n")

    store, pid = _reconcile(
        str(tmp_path / "db.py"),
        str(tmp_path / "other.py"),
        str(tmp_path / "runner.py"),
        tmp_path=tmp_path,
    )
    callers_store = store.callers_of(pid, "py::db.Store.execute")
    callers_other = store.callers_of(pid, "py::other.OtherStore.execute")
    assert not any("run" in c for c in callers_store), \
        f"Ambiguous get_store must not bind to Store; callers={callers_store}"
    assert not any("run" in c for c in callers_other), \
        f"Ambiguous get_store must not bind to OtherStore; callers={callers_other}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# 5. Return type known but method not in idx → no false edge
# ══════════════════════════════════════════════════════════════════════════════

def test_no_false_edge(tmp_path):
    """get_store() -> Store but s.nonexistent() not in idx → no edge."""
    _py(tmp_path, "db.py",
        "class Store:\n    def execute(self): pass\n"
        "def get_store() -> Store: pass\n")
    _py(tmp_path, "runner.py",
        "from db import Store, get_store\n"
        "def run():\n"
        "    s = get_store()\n"
        "    s.nonexistent()\n")

    store, pid = _reconcile(
        str(tmp_path / "db.py"),
        str(tmp_path / "runner.py"),
        tmp_path=tmp_path,
    )
    edges = store.con.execute(
        "SELECT callee FROM call_edges WHERE project_id=? AND callee LIKE '%nonexistent%'",
        (pid,),
    ).fetchall()
    assert not edges, f"No false edge for unknown method; got edges={edges}"
    store.close()
