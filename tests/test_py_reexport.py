"""Tests for Python __init__ re-export penetration.

Builds on foundation batch (a6a6537): relative import resolution + __init__ normalization.
Verifies that from .real import fn in __init__.py registers a reexport_edge barrel_module=pkg,
and that callers using `from pkg import fn` resolve through to pkg.real.fn.
"""
from __future__ import annotations

import pytest

from buer.parse import extract_reexports
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
# 1. extract_reexports Python branch
# ══════════════════════════════════════════════════════════════════════════════

def test_init_reexport_extract(tmp_path):
    """pkg/__init__.py with 'from .real import fn' → extract_reexports returns ('fn','pkg.real','fn')."""
    _py(tmp_path, "pkg/__init__.py", "from .real import fn\n")
    init = str(tmp_path / "pkg" / "__init__.py")
    result = extract_reexports(init, root=str(tmp_path))
    assert ("fn", "pkg.real", "fn") in result, f"result={result}"


def test_init_reexport_aliased(tmp_path):
    """from .real import fn as g → ('g', 'pkg.real', 'fn')."""
    _py(tmp_path, "pkg/__init__.py", "from .real import fn as g\n")
    init = str(tmp_path / "pkg" / "__init__.py")
    result = extract_reexports(init, root=str(tmp_path))
    assert ("g", "pkg.real", "fn") in result, f"result={result}"


def test_star_produces_wildcard_record(tmp_path):
    """from .real import * → produces ('*', 'pkg.real', '*') wildcard record."""
    _py(tmp_path, "pkg/__init__.py", "from .real import *\n")
    init = str(tmp_path / "pkg" / "__init__.py")
    result = extract_reexports(init, root=str(tmp_path))
    assert ("*", "pkg.real", "*") in result, f"star must produce wildcard record; result={result}"


def test_submodule_import_excluded(tmp_path):
    """from . import mod → not registered (mod is a submodule, not a symbol)."""
    _py(tmp_path, "pkg/__init__.py", "from . import mod\n")
    init = str(tmp_path / "pkg" / "__init__.py")
    result = extract_reexports(init, root=str(tmp_path))
    assert result == [], f"submodule import must be excluded; result={result}"


# ══════════════════════════════════════════════════════════════════════════════
# 2. End-to-end penetration
# ══════════════════════════════════════════════════════════════════════════════

def test_init_reexport_penetrate(tmp_path):
    """pkg/real.py def fn; pkg/__init__.py from .real import fn;
    caller.py from pkg import fn + fn() → edge to pkg.real.fn via __init__ barrel."""
    _py(tmp_path, "pkg/__init__.py", "from .real import fn\n")
    _py(tmp_path, "pkg/real.py", "def fn(): pass\n")
    _py(tmp_path, "caller.py",
        "from pkg import fn\n"
        "def run():\n"
        "    fn()\n")

    store, pid = _reconcile(
        str(tmp_path / "pkg" / "__init__.py"),
        str(tmp_path / "pkg" / "real.py"),
        str(tmp_path / "caller.py"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "py::pkg.real.fn")
    assert any("run" in c for c in callers), \
        f"__init__ barrel must penetrate to pkg.real.fn; callers={callers}"
    store.close()


def test_init_reexport_aliased_penetrate(tmp_path):
    """pkg/__init__.py from .real import fn as g; caller from pkg import g; g()
    → edge to pkg.real.fn."""
    _py(tmp_path, "pkg/__init__.py", "from .real import fn as g\n")
    _py(tmp_path, "pkg/real.py", "def fn(): pass\n")
    _py(tmp_path, "caller.py",
        "from pkg import g\n"
        "def run():\n"
        "    g()\n")

    store, pid = _reconcile(
        str(tmp_path / "pkg" / "__init__.py"),
        str(tmp_path / "pkg" / "real.py"),
        str(tmp_path / "caller.py"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "py::pkg.real.fn")
    assert any("run" in c for c in callers), \
        f"aliased barrel must penetrate; callers={callers}"
    store.close()


def test_init_reexport_chained(tmp_path):
    """pkg/__init__ from .sub import fn; pkg/sub/__init__ from .real import fn
    → chains through to pkg.sub.real.fn."""
    _py(tmp_path, "pkg/__init__.py", "from .sub import fn\n")
    _py(tmp_path, "pkg/sub/__init__.py", "from .real import fn\n")
    _py(tmp_path, "pkg/sub/real.py", "def fn(): pass\n")
    _py(tmp_path, "caller.py",
        "from pkg import fn\n"
        "def run():\n"
        "    fn()\n")

    store, pid = _reconcile(
        str(tmp_path / "pkg" / "__init__.py"),
        str(tmp_path / "pkg" / "sub" / "__init__.py"),
        str(tmp_path / "pkg" / "sub" / "real.py"),
        str(tmp_path / "caller.py"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "py::pkg.sub.real.fn")
    assert any("run" in c for c in callers), \
        f"chained barrel must penetrate to pkg.sub.real.fn; callers={callers}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# 3. Store-level: barrel_module stored with normalized name
# ══════════════════════════════════════════════════════════════════════════════

def test_reexport_stored(tmp_path):
    """After reconcile of pkg/__init__.py, reexport_edges has barrel_module='pkg'."""
    _py(tmp_path, "pkg/__init__.py", "from .real import fn\n")
    _py(tmp_path, "pkg/real.py", "def fn(): pass\n")

    store, pid = _reconcile(
        str(tmp_path / "pkg" / "__init__.py"),
        str(tmp_path / "pkg" / "real.py"),
        tmp_path=tmp_path,
    )
    rows = store.con.execute(
        "SELECT barrel_module, exported_name, target_module, target_name "
        "FROM reexport_edges WHERE project_id=?",
        (pid,),
    ).fetchall()
    found = any(
        r["barrel_module"] == "pkg" and r["exported_name"] == "fn"
        and r["target_module"] == "pkg.real" and r["target_name"] == "fn"
        for r in rows
    )
    assert found, f"Expected barrel_module=pkg in reexport_edges; rows={[dict(r) for r in rows]}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# 4. Business-file re-export is harmless
# ══════════════════════════════════════════════════════════════════════════════

def test_business_file_reexport_harmless(tmp_path):
    """pkg/svc.py 'from .real import fn; fn()' — re-export is registered but harmless.
    Direct call fn() still resolves via b-step to pkg.real.fn."""
    _py(tmp_path, "pkg/__init__.py", "")
    _py(tmp_path, "pkg/real.py", "def fn(): pass\n")
    _py(tmp_path, "pkg/svc.py",
        "from .real import fn\n"
        "def run():\n"
        "    fn()\n")

    store, pid = _reconcile(
        str(tmp_path / "pkg" / "__init__.py"),
        str(tmp_path / "pkg" / "real.py"),
        str(tmp_path / "pkg" / "svc.py"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "py::pkg.real.fn")
    assert any("run" in c for c in callers), \
        f"b-step must still resolve fn() in svc.py; callers={callers}"
    store.close()
