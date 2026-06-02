"""Tests for L2 class-level self-attribute type inference.

L2 resolves self.attr.method() when the class has a binding for self.attr
(via constructor assignment or explicit annotation) found anywhere in the
class body.  It is strictly complementary to a-step (self.method()) and L1
(local-var.method()).
"""
from __future__ import annotations

import pytest

from buer.callgraph import SymbolIndex, resolve_callee
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
# 1. Constructor assignment: self.store = Store() → resolves self.store.execute()
#    c-step ambiguity manufactured: OtherClass also has execute().
# ══════════════════════════════════════════════════════════════════════════════

def test_self_attr_construct(tmp_path):
    """self.store = Store() in __init__ → L2 connects self.store.execute() to Store.execute."""
    _py(tmp_path, "db.py", "class Store:\n    def execute(self): pass\n")
    _py(tmp_path, "other.py", "class OtherClass:\n    def execute(self): pass\n")
    _py(tmp_path, "app.py",
        "from db import Store\n"
        "class App:\n"
        "    def __init__(self):\n"
        "        self.store = Store()\n"
        "    def run(self):\n"
        "        self.store.execute()\n")

    store, pid = _reconcile(
        str(tmp_path / "db.py"),
        str(tmp_path / "other.py"),
        str(tmp_path / "app.py"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "py::db.Store.execute")
    assert any("run" in c for c in callers), \
        f"L2 must connect self.store.execute() to db.Store.execute; callers={callers}"
    # Must NOT connect to OtherClass.execute
    callers_other = store.callers_of(pid, "py::other.OtherClass.execute")
    assert not any("run" in c for c in callers_other), \
        f"Must not produce false edge to OtherClass.execute; callers={callers_other}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# 2. Annotated assignment: self.store: Store → same resolution
# ══════════════════════════════════════════════════════════════════════════════

def test_self_attr_annotated(tmp_path):
    """self.store: Store annotation → L2 connects self.store.execute() to Store.execute."""
    _py(tmp_path, "db.py", "class Store:\n    def execute(self): pass\n")
    _py(tmp_path, "app.py",
        "from db import Store\n"
        "class App:\n"
        "    def __init__(self):\n"
        "        self.store: Store = Store()\n"
        "    def run(self):\n"
        "        self.store.execute()\n")

    store, pid = _reconcile(
        str(tmp_path / "db.py"),
        str(tmp_path / "app.py"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "py::db.Store.execute")
    assert any("run" in c for c in callers), \
        f"Annotated self.store: Store must resolve execute; callers={callers}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# 3. a-step still handles self.method() — single-dot self calls unaffected
# ══════════════════════════════════════════════════════════════════════════════

def test_self_method_still_a_step(tmp_path):
    """self.compute() (single dot) must still resolve via a-step, not L2."""
    _py(tmp_path, "app.py",
        "class App:\n"
        "    def compute(self): pass\n"
        "    def run(self):\n"
        "        self.compute()\n")

    store, pid = _reconcile(str(tmp_path / "app.py"), tmp_path=tmp_path)
    callers = store.callers_of(pid, "py::app.App.compute")
    assert any("run" in c for c in callers), \
        f"self.compute() must still resolve via a-step; callers={callers}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# 4. No false edge: method not in idx → L2 returns None
# ══════════════════════════════════════════════════════════════════════════════

def test_no_false_edge(tmp_path):
    """self.store = Store() but self.store.nonexistent() not in idx → no edge."""
    _py(tmp_path, "db.py", "class Store:\n    def execute(self): pass\n")
    _py(tmp_path, "app.py",
        "from db import Store\n"
        "class App:\n"
        "    def __init__(self):\n"
        "        self.store = Store()\n"
        "    def run(self):\n"
        "        self.store.nonexistent()\n")

    store, pid = _reconcile(
        str(tmp_path / "db.py"),
        str(tmp_path / "app.py"),
        tmp_path=tmp_path,
    )
    edges = store.con.execute(
        "SELECT callee FROM call_edges WHERE project_id=? AND callee LIKE '%nonexistent%'",
        (pid,),
    ).fetchall()
    assert not edges, f"No false edge for unknown method; got edges={edges}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# 5. Dynamic assignment not inferred: self.x = get_thing() → no binding
# ══════════════════════════════════════════════════════════════════════════════

def test_dynamic_self_attr_not_inferred(tmp_path):
    """self.store = get_store() (factory call) must NOT produce a type binding."""
    _py(tmp_path, "db.py", "class Store:\n    def execute(self): pass\n")
    _py(tmp_path, "app.py",
        "from db import Store\n"
        "def get_store(): pass\n"
        "class App:\n"
        "    def __init__(self):\n"
        "        self.store = get_store()\n"  # lowercase → dynamic, not constructor
        "    def run(self):\n"
        "        self.store.execute()\n")

    store, pid = _reconcile(
        str(tmp_path / "db.py"),
        str(tmp_path / "app.py"),
        tmp_path=tmp_path,
    )
    # No type evidence → dotted call must remain unresolved (no false edge)
    callers = store.callers_of(pid, "py::db.Store.execute")
    assert not any("run" in c for c in callers), \
        f"Dynamic factory assign must not produce L2 binding; callers={callers}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# 6. Conflicting bindings: self.x bound to two different types → not recorded
# ══════════════════════════════════════════════════════════════════════════════

def test_inconsistent_attr_not_inferred(tmp_path):
    """self.store bound to Store in one branch, Other in another → conflict, no edge."""
    _py(tmp_path, "db.py", "class Store:\n    def execute(self): pass\n")
    _py(tmp_path, "other.py", "class Other:\n    def execute(self): pass\n")
    _py(tmp_path, "app.py",
        "from db import Store\n"
        "from other import Other\n"
        "class App:\n"
        "    def setup_a(self):\n"
        "        self.store = Store()\n"
        "    def setup_b(self):\n"
        "        self.store = Other()\n"  # conflict: two types for same attr
        "    def run(self):\n"
        "        self.store.execute()\n")

    store, pid = _reconcile(
        str(tmp_path / "db.py"),
        str(tmp_path / "other.py"),
        str(tmp_path / "app.py"),
        tmp_path=tmp_path,
    )
    # Conflict → no binding → no edge to either class
    callers_store = store.callers_of(pid, "py::db.Store.execute")
    callers_other = store.callers_of(pid, "py::other.Other.execute")
    assert not any("run" in c for c in callers_store), \
        f"Conflicting attr must not bind to Store; callers={callers_store}"
    assert not any("run" in c for c in callers_other), \
        f"Conflicting attr must not bind to Other; callers={callers_other}"
    store.close()
