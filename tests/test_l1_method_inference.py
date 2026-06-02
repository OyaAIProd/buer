"""Tests for L1 object-method-call type inference (function-scope receiver types).

L1 resolves obj.method() when the caller has a function-scope type annotation
for 'obj' (typed parameter, annotated variable, or constructor assignment).
It translates var→type, then resolves type.method via the existing b-step
(import_map) or the same-module class definition.
"""
from __future__ import annotations

import os

import pytest

from buer.callgraph import SymbolIndex, resolve_callee
from buer.parse import extract_defines
from buer.reconcile import reconcile
from buer.store import Store


# ── helpers ───────────────────────────────────────────────────────────────────

def _ts(tmp_path, rel: str, body: str) -> str:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return str(p)


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
# 1. Typed parameter → resolves via import_map
#    c-step ambiguity is manufactured: OtherClass also has execute().
# ══════════════════════════════════════════════════════════════════════════════

def test_param_annotated_beats_c_step_ambiguity(tmp_path):
    """L1 resolves store: Store via import_map, ignoring OtherClass.execute ambiguity."""
    _py(tmp_path, "db.py", "class Store:\n    def execute(self): pass\n")
    _py(tmp_path, "other.py", "class OtherClass:\n    def execute(self): pass\n")
    _py(tmp_path, "runner.py",
        "from db import Store\n"
        "def run(store: Store):\n"
        "    store.execute()\n")

    store, pid = _reconcile(
        str(tmp_path / "db.py"),
        str(tmp_path / "other.py"),
        str(tmp_path / "runner.py"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "py::db.Store.execute")
    assert any("runner" in c for c in callers), \
        f"L1 must connect to db.Store.execute (not OtherClass); callers={callers}"
    # Must NOT connect to OtherClass.execute (no type evidence for that)
    callers_other = store.callers_of(pid, "py::other.OtherClass.execute")
    assert not any("runner" in c for c in callers_other), \
        f"Must not produce a false edge to OtherClass.execute; callers={callers_other}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# 2. Annotated variable assignment
# ══════════════════════════════════════════════════════════════════════════════

def test_var_annotated(tmp_path):
    """store: Store = get_store() → L1 connects store.execute() to Store.execute."""
    _py(tmp_path, "db.py", "class Store:\n    def execute(self): pass\n")
    _py(tmp_path, "runner.py",
        "from db import Store\n"
        "def get_store(): pass\n"
        "def run():\n"
        "    store: Store = get_store()\n"
        "    store.execute()\n")

    store, pid = _reconcile(
        str(tmp_path / "db.py"),
        str(tmp_path / "runner.py"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "py::db.Store.execute")
    assert any("run" in c for c in callers), \
        f"Annotated variable must resolve store.execute; callers={callers}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# 3. Constructor assignment
# ══════════════════════════════════════════════════════════════════════════════

def test_construct_assign(tmp_path):
    """store = Store() → L1 infers type from constructor, connects store.execute()."""
    _py(tmp_path, "db.py", "class Store:\n    def execute(self): pass\n")
    _py(tmp_path, "runner.py",
        "from db import Store\n"
        "def run():\n"
        "    store = Store()\n"
        "    store.execute()\n")

    store, pid = _reconcile(
        str(tmp_path / "db.py"),
        str(tmp_path / "runner.py"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "py::db.Store.execute")
    assert any("run" in c for c in callers), \
        f"Constructor assignment must resolve store.execute; callers={callers}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# 4. Same-module class (no import_map needed)
# ══════════════════════════════════════════════════════════════════════════════

def test_local_module_class(tmp_path):
    """Class defined in the same file → L1 resolves via caller_module.TypeName.method."""
    _py(tmp_path, "app.py",
        "class Store:\n"
        "    def execute(self): pass\n"
        "def run(store: Store):\n"
        "    store.execute()\n")

    store, pid = _reconcile(str(tmp_path / "app.py"), tmp_path=tmp_path)
    callers = store.callers_of(pid, "py::app.Store.execute")
    assert any("run" in c for c in callers), \
        f"Same-module class must resolve via L1; callers={callers}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# 5. Unknown type → falls back to c-step behaviour (no regression)
# ══════════════════════════════════════════════════════════════════════════════

def test_unknown_type_falls_back(tmp_path):
    """No type annotation on receiver: L1 is silent, dotted call stays unresolved.

    c-step only resolves plain names (no dot); it cannot resolve obj.method()
    without type info.  This test confirms:
      1. Unannotated dotted call is NOT connected (conservative, no false edge).
      2. L1 does not regress simple-name (no-dot) c-step connections.
    """
    _py(tmp_path, "db.py",
        "class Store:\n    def execute(self): pass\n"
        "def helper(): pass\n")
    _py(tmp_path, "runner.py",
        "def run(store):\n"       # no annotation — L1 can't infer type
        "    store.execute()\n"   # dotted, no type → correctly unresolved
        "    helper()\n")         # simple name, unique → c-step connects

    store, pid = _reconcile(
        str(tmp_path / "db.py"),
        str(tmp_path / "runner.py"),
        tmp_path=tmp_path,
    )
    # Dotted call with no type info: must NOT produce a guessed edge
    callers_exec = store.callers_of(pid, "py::db.Store.execute")
    assert not any("run" in c for c in callers_exec), \
        f"Unannotated dotted call must not produce a false edge; callers={callers_exec}"
    # Simple-name call (no dot): c-step must still fire for unique symbols
    callers_helper = store.callers_of(pid, "py::db.helper")
    assert any("run" in c for c in callers_helper), \
        f"Simple-name c-step must still work; callers={callers_helper}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# 6. No false edge: method not in idx → L1 returns None
# ══════════════════════════════════════════════════════════════════════════════

def test_no_false_edge(tmp_path):
    """store: Store but store.nonexistent() is not in idx → no edge created."""
    _py(tmp_path, "db.py", "class Store:\n    def execute(self): pass\n")
    _py(tmp_path, "runner.py",
        "from db import Store\n"
        "def run(store: Store):\n"
        "    store.nonexistent()\n")

    store, pid = _reconcile(
        str(tmp_path / "db.py"),
        str(tmp_path / "runner.py"),
        tmp_path=tmp_path,
    )
    # db.Store.nonexistent is not in idx; L1 must not create a phantom edge
    edges = store.con.execute(
        "SELECT callee FROM call_edges WHERE project_id=? AND callee LIKE '%nonexistent%'",
        (pid,),
    ).fetchall()
    assert not edges, f"No false edge for unknown method; got edges={edges}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# 7. Optional[Store] wrapper → extract Store, resolve correctly
# ══════════════════════════════════════════════════════════════════════════════

def test_optional_wrapper(tmp_path):
    """def f(store: Optional[Store]) → L1 unwraps Optional and resolves Store.execute."""
    _py(tmp_path, "db.py", "class Store:\n    def execute(self): pass\n")
    _py(tmp_path, "runner.py",
        "from typing import Optional\n"
        "from db import Store\n"
        "def run(store: Optional[Store]):\n"
        "    store.execute()\n")

    store, pid = _reconcile(
        str(tmp_path / "db.py"),
        str(tmp_path / "runner.py"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "py::db.Store.execute")
    assert any("run" in c for c in callers), \
        f"Optional[Store] must unwrap to Store and resolve; callers={callers}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# 8. extract_defines returns correct receiver_types
# ══════════════════════════════════════════════════════════════════════════════

def test_receiver_types_extraction(tmp_path):
    """extract_defines correctly populates receiver_types for all three sources."""
    src = (
        "from db import Store, Other\n"
        "def run(store: Store, n: int):\n"   # typed param, int skipped
        "    conn: Other = get()\n"           # annotated var
        "    cache = Store()\n"               # constructor assign
        "    store.execute()\n"
        "    conn.query()\n"
        "    cache.flush()\n"
    )
    fp = _py(tmp_path, "runner.py", src)
    defines = extract_defines(fp)
    run_def = next(d for d in defines if d.name == "run")
    rtypes = dict(run_def.receiver_types)

    assert rtypes.get("store") == "Store", f"typed param missed; got {rtypes}"
    assert rtypes.get("conn") == "Other", f"annotated var missed; got {rtypes}"
    assert rtypes.get("cache") == "Store", f"constructor assign missed; got {rtypes}"
    assert "n" not in rtypes, f"int param must not be recorded; got {rtypes}"
