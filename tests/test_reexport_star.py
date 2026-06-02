"""Tests for re-export star penetration (export * / from .x import *).

Wildcard re-exports: store B.*→X.* record; _follow_reexport dynamically resolves
B.foo → X.foo at query time.  JS + Python both covered.
"""
from __future__ import annotations

import pytest

from buer.callgraph import SymbolIndex, _follow_reexport
from buer.parse import extract_reexports
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
# JS
# ══════════════════════════════════════════════════════════════════════════════

def test_js_star_extract(tmp_path):
    """export * from './x' → extract_reexports contains ('*', 'src/x', '*')."""
    barrel = _ts(tmp_path, "src/barrel.ts", "export * from './x';")
    _ts(tmp_path, "src/x.ts", "export function fn() {}")
    result = extract_reexports(str(barrel), root=str(tmp_path))
    assert ("*", "src/x", "*") in result, f"star record missing; result={result}"


def test_js_star_penetrate(tmp_path):
    """x.ts def fn; barrel.ts export * from './x'; caller import {fn} → caller→src/x.fn."""
    _ts(tmp_path, "src/x.ts",      "export function fn() { return 1; }")
    _ts(tmp_path, "src/barrel.ts", "export * from './x';")
    _ts(tmp_path, "src/caller.ts", "import { fn } from './barrel';\nexport function run() { fn(); }")

    store, pid = _reconcile(
        str(tmp_path / "src" / "x.ts"),
        str(tmp_path / "src" / "barrel.ts"),
        str(tmp_path / "src" / "caller.ts"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "js_ts::src/x.fn")
    assert any("run" in c for c in callers), \
        f"star must penetrate to src/x.fn; callers={callers}"
    store.close()


def test_js_star_chained(tmp_path):
    """x.ts fn; barrel export * from x; barrel2 export * from barrel → caller→src/x.fn."""
    _ts(tmp_path, "src/x.ts",       "export function fn() { return 1; }")
    _ts(tmp_path, "src/barrel.ts",  "export * from './x';")
    _ts(tmp_path, "src/barrel2.ts", "export * from './barrel';")
    _ts(tmp_path, "src/caller.ts",  "import { fn } from './barrel2';\nexport function run() { fn(); }")

    store, pid = _reconcile(
        str(tmp_path / "src" / "x.ts"),
        str(tmp_path / "src" / "barrel.ts"),
        str(tmp_path / "src" / "barrel2.ts"),
        str(tmp_path / "src" / "caller.ts"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "js_ts::src/x.fn")
    assert any("run" in c for c in callers), \
        f"chained star must penetrate; callers={callers}"
    store.close()


def test_js_star_as_ns_skipped(tmp_path):
    """export * as ns from './x' — namespace form, first version: no record, no crash."""
    barrel = _ts(tmp_path, "src/barrel.ts", "export * as ns from './x';")
    result = extract_reexports(str(barrel), root=str(tmp_path))
    # Must not contain ("*", ...) and must not raise
    assert not any(r[0] == "*" for r in result), \
        f"namespace re-export must be skipped; result={result}"


# ══════════════════════════════════════════════════════════════════════════════
# Python
# ══════════════════════════════════════════════════════════════════════════════

def test_py_star_extract(tmp_path):
    """pkg/__init__.py with 'from .x import *' → ('*', 'pkg.x', '*')."""
    _py(tmp_path, "pkg/__init__.py", "from .x import *\n")
    init = str(tmp_path / "pkg" / "__init__.py")
    result = extract_reexports(init, root=str(tmp_path))
    assert ("*", "pkg.x", "*") in result, f"star record missing; result={result}"


def test_py_star_penetrate(tmp_path):
    """pkg/x.py def fn; pkg/__init__.py from .x import *;
    caller from pkg import fn; fn() → edge to pkg.x.fn."""
    _py(tmp_path, "pkg/__init__.py", "from .x import *\n")
    _py(tmp_path, "pkg/x.py", "def fn(): pass\n")
    _py(tmp_path, "caller.py",
        "from pkg import fn\n"
        "def run():\n"
        "    fn()\n")

    store, pid = _reconcile(
        str(tmp_path / "pkg" / "__init__.py"),
        str(tmp_path / "pkg" / "x.py"),
        str(tmp_path / "caller.py"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "py::pkg.x.fn")
    assert any("run" in c for c in callers), \
        f"Python star must penetrate to pkg.x.fn; callers={callers}"
    store.close()


def test_py_star_chained(tmp_path):
    """pkg/__init__ from .sub import *; pkg/sub/__init__ from .real import *
    → caller→pkg.sub.real.fn."""
    _py(tmp_path, "pkg/__init__.py",      "from .sub import *\n")
    _py(tmp_path, "pkg/sub/__init__.py",  "from .real import *\n")
    _py(tmp_path, "pkg/sub/real.py",      "def fn(): pass\n")
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
        f"chained Python star must penetrate; callers={callers}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# Shared: no-false-edge, mixed, anti-cycle
# ══════════════════════════════════════════════════════════════════════════════

def test_star_no_false_edge(tmp_path):
    """barrel export * from x, but caller calls nonexistent → x.nonexist not in idx → no edge."""
    _ts(tmp_path, "src/x.ts",      "export function fn() { return 1; }")
    _ts(tmp_path, "src/barrel.ts", "export * from './x';")
    _ts(tmp_path, "src/caller.ts",
        "import { nonexist } from './barrel';\nexport function run() { nonexist(); }")

    store, pid = _reconcile(
        str(tmp_path / "src" / "x.ts"),
        str(tmp_path / "src" / "barrel.ts"),
        str(tmp_path / "src" / "caller.ts"),
        tmp_path=tmp_path,
    )
    edges = store.con.execute(
        "SELECT callee FROM call_edges WHERE project_id=? AND callee LIKE '%nonexist%'",
        (pid,),
    ).fetchall()
    assert not edges, f"star must not produce false edge for absent symbol; got {edges}"
    store.close()


def test_star_mixed_with_named(tmp_path):
    """barrel: export {a} from x (named) + export * from y (star).
    a → x.a via named; other symbol b → y.b via star."""
    _ts(tmp_path, "src/x.ts",      "export function a() {}")
    _ts(tmp_path, "src/y.ts",      "export function b() {}")
    _ts(tmp_path, "src/barrel.ts",
        "export { a } from './x';\nexport * from './y';")
    _ts(tmp_path, "src/caller.ts",
        "import { a, b } from './barrel';\n"
        "export function run() { a(); b(); }")

    store, pid = _reconcile(
        str(tmp_path / "src" / "x.ts"),
        str(tmp_path / "src" / "y.ts"),
        str(tmp_path / "src" / "barrel.ts"),
        str(tmp_path / "src" / "caller.ts"),
        tmp_path=tmp_path,
    )
    callers_a = store.callers_of(pid, "js_ts::src/x.a")
    callers_b = store.callers_of(pid, "js_ts::src/y.b")
    assert any("run" in c for c in callers_a), \
        f"named a must connect via x; callers_a={callers_a}"
    assert any("run" in c for c in callers_b), \
        f"star b must connect via y; callers_b={callers_b}"
    store.close()


def test_star_anticycle(tmp_path):
    """barrel * → x, x * → barrel (cycle) → _follow_reexport does not loop."""
    idx = SymbolIndex()
    idx.reexport["barrel.*"] = "x.*"
    idx.reexport["x.*"] = "barrel.*"
    # Neither foo is in qualified — must return without hanging
    result = _follow_reexport("barrel.foo", idx)
    assert result not in idx.qualified  # dead end, not an exception
