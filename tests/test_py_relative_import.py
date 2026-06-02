"""Tests for Python relative import resolution + __init__ module name normalization.

Foundation for __init__ re-export penetration (next batch).
"""
from __future__ import annotations

import pytest

from buer.callgraph import module_name_of, build_symbol_index, compute_call_edges
from buer.parse import extract_module_imports
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
# __init__ module name normalization
# ══════════════════════════════════════════════════════════════════════════════

def test_init_normalized(tmp_path):
    """pkg/__init__.py → 'pkg', not 'pkg.__init__'."""
    assert module_name_of(str(tmp_path / "pkg" / "__init__.py"), str(tmp_path)) == "pkg"


def test_init_sub(tmp_path):
    """pkg/sub/__init__.py → 'pkg.sub'."""
    assert module_name_of(str(tmp_path / "pkg" / "sub" / "__init__.py"), str(tmp_path)) == "pkg.sub"


def test_normal_py_unchanged(tmp_path):
    """Ordinary .py files are not affected by __init__ normalization."""
    assert module_name_of(str(tmp_path / "pkg" / "auth.py"), str(tmp_path)) == "pkg.auth"


# ══════════════════════════════════════════════════════════════════════════════
# Relative import resolution (extract_module_imports)
# ══════════════════════════════════════════════════════════════════════════════

def test_relative_single(tmp_path):
    """from .real import fn → import_map['fn'] == 'pkg.real.fn'."""
    _py(tmp_path, "pkg/__init__.py", "")
    _py(tmp_path, "pkg/real.py", "def fn(): pass\n")
    svc = _py(tmp_path, "pkg/svc.py", "from .real import fn\n")
    imap = extract_module_imports(svc, root=str(tmp_path))
    assert imap.get("fn") == "pkg.real.fn", f"import_map={imap}"


def test_relative_parent(tmp_path):
    """from ..util import h (in pkg/sub/x.py) → import_map['h'] == 'pkg.util.h'."""
    _py(tmp_path, "pkg/__init__.py", "")
    _py(tmp_path, "pkg/sub/__init__.py", "")
    _py(tmp_path, "pkg/util.py", "def h(): pass\n")
    x = _py(tmp_path, "pkg/sub/x.py", "from ..util import h\n")
    imap = extract_module_imports(x, root=str(tmp_path))
    assert imap.get("h") == "pkg.util.h", f"import_map={imap}"


def test_relative_submodule(tmp_path):
    """from . import mod (in pkg/svc.py) → import_map['mod'] == 'pkg.mod'."""
    _py(tmp_path, "pkg/__init__.py", "")
    _py(tmp_path, "pkg/mod.py", "")
    svc = _py(tmp_path, "pkg/svc.py", "from . import mod\n")
    imap = extract_module_imports(svc, root=str(tmp_path))
    assert imap.get("mod") == "pkg.mod", f"import_map={imap}"


def test_relative_aliased(tmp_path):
    """from .real import fn as g → import_map['g'] == 'pkg.real.fn'."""
    _py(tmp_path, "pkg/__init__.py", "")
    _py(tmp_path, "pkg/real.py", "def fn(): pass\n")
    svc = _py(tmp_path, "pkg/svc.py", "from .real import fn as g\n")
    imap = extract_module_imports(svc, root=str(tmp_path))
    assert imap.get("g") == "pkg.real.fn", f"import_map={imap}"


def test_normal_import_unchanged(tmp_path):
    """Absolute from pkg.x import y is unaffected."""
    _py(tmp_path, "pkg/__init__.py", "")
    _py(tmp_path, "pkg/x.py", "def y(): pass\n")
    caller = _py(tmp_path, "caller.py", "from pkg.x import y\n")
    imap = extract_module_imports(caller, root=str(tmp_path))
    assert imap.get("y") == "pkg.x.y", f"import_map={imap}"


# ══════════════════════════════════════════════════════════════════════════════
# End-to-end: call edges via b-step (not c-step fallback)
# ══════════════════════════════════════════════════════════════════════════════

def test_relative_call_connects(tmp_path):
    """pkg/svc.py uses 'from .real import helper; helper()' → edge to pkg.real.helper via b-step."""
    _py(tmp_path, "pkg/__init__.py", "")
    _py(tmp_path, "pkg/real.py", "def helper(): pass\n")
    _py(tmp_path, "pkg/svc.py",
        "from .real import helper\n"
        "def run():\n"
        "    helper()\n")

    store, pid = _reconcile(
        str(tmp_path / "pkg" / "real.py"),
        str(tmp_path / "pkg" / "svc.py"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "py::pkg.real.helper")
    assert any("run" in c for c in callers), \
        f"relative import must connect via b-step; callers={callers}"
    store.close()


def test_init_with_define(tmp_path):
    """pkg/__init__.py defines top(); from pkg import top; caller calls top()
    → FQN is pkg.top (normalized), edge connects."""
    _py(tmp_path, "pkg/__init__.py", "def top(): pass\n")
    _py(tmp_path, "caller.py",
        "from pkg import top\n"
        "def run():\n"
        "    top()\n")

    store, pid = _reconcile(
        str(tmp_path / "pkg" / "__init__.py"),
        str(tmp_path / "caller.py"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "py::pkg.top")
    assert any("run" in c for c in callers), \
        f"__init__ normalization must expose pkg.top; callers={callers}"
    store.close()
