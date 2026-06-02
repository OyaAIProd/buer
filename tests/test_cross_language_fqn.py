"""Tests for 発見6 fix: cross-language FQN collision prevention.

py::mod.fn and js_ts::mod.fn must never clobber each other in SymbolIndex,
call_edges, or gd_edges even when a Python file and a JS/TS file share the
same bare module name (e.g. src/util.py and src/util.ts at the same root).

Regression baseline: pure-Python and pure-TS workloads are completely unchanged
in edge counts — the lang:: prefix is transparent to each language in isolation.

Also covers _with_lang consolidation: the helper is the single construction
point for all lang:: prefixing — verified via direct unit test.
"""
from __future__ import annotations

import os

import pytest

from buer.callgraph import _with_lang, _lang_fqn
from buer.parse import extract_defines
from buer.reconcile import reconcile
from buer.store import Store


# ══════════════════════════════════════════════════════════════════════════════
# 0. _with_lang unit test (consolidation verification)
# ══════════════════════════════════════════════════════════════════════════════

def test_with_lang_consolidation(tmp_path):
    """_with_lang is the single prefix constructor; _lang_fqn delegates to it."""
    assert _with_lang("py", "mod.f") == "py::mod.f"
    assert _with_lang("js_ts", "src/lib.Fn") == "js_ts::src/lib.Fn"
    # Empty lang → bare unchanged (no-lang fallback used in resolve_callee)
    assert _with_lang("", "mod.f") == "mod.f"
    assert _with_lang(None, "mod.f") == "mod.f"
    # _lang_fqn wraps _with_lang — must produce same result
    py_file = str(tmp_path / "mod.py")
    assert _lang_fqn(py_file, "mod", "f") == _with_lang("py", "mod.f")
    ts_file = str(tmp_path / "src" / "lib.ts")
    assert _lang_fqn(ts_file, "src/lib", "Fn") == _with_lang("js_ts", "src/lib.Fn")


# ── helpers ───────────────────────────────────────────────────────────────────

def _write(tmp_path, rel: str, body: str) -> str:
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
# 1. Same-name Python and TS modules do not collide in SymbolIndex
# ══════════════════════════════════════════════════════════════════════════════

def test_same_name_py_ts_both_ingested(tmp_path):
    """mod.py and mod.ts at same root: both fn_py and fn_ts must appear in call_edges."""
    _write(tmp_path, "mod.py", "def fn_py(): pass\ndef caller_py():\n    fn_py()\n")
    _write(tmp_path, "mod.ts", "export function fn_ts() {}\nexport function caller_ts() { fn_ts(); }")

    store, pid = _reconcile(
        str(tmp_path / "mod.py"),
        str(tmp_path / "mod.ts"),
        tmp_path=tmp_path,
    )
    callers_py = store.callers_of(pid, "py::mod.fn_py")
    callers_ts = store.callers_of(pid, "js_ts::mod.fn_ts")

    assert any("caller_py" in c for c in callers_py), \
        f"py mod must have callers; callers_py={callers_py}"
    assert any("caller_ts" in c for c in callers_ts), \
        f"ts mod must have callers; callers_ts={callers_ts}"
    store.close()


def test_same_name_no_cross_language_edge(tmp_path):
    """caller_py calling fn_py must not create an edge to js_ts::mod.fn_py."""
    _write(tmp_path, "mod.py", "def fn_py(): pass\ndef caller_py():\n    fn_py()\n")
    _write(tmp_path, "mod.ts", "export function fn_py() {}")

    store, pid = _reconcile(
        str(tmp_path / "mod.py"),
        str(tmp_path / "mod.ts"),
        tmp_path=tmp_path,
    )
    # py caller must not bleed into ts edge
    callers_ts = store.callers_of(pid, "js_ts::mod.fn_py")
    assert not any("caller_py" in c for c in callers_ts), \
        f"py caller must not create ts edge; callers_ts={callers_ts}"

    callers_py = store.callers_of(pid, "py::mod.fn_py")
    assert any("caller_py" in c for c in callers_py), \
        f"py edge must exist for py::mod.fn_py; callers_py={callers_py}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# 2. Pure-Python baseline: edge counts unchanged by lang-prefix change
# ══════════════════════════════════════════════════════════════════════════════

def test_pure_python_edge_count_baseline(tmp_path):
    """Pure Python project: edge exists exactly once, no duplication."""
    _write(tmp_path, "a.py", "def fn(): pass\n")
    _write(tmp_path, "b.py", "from a import fn\ndef run():\n    fn()\n")

    store, pid = _reconcile(
        str(tmp_path / "a.py"),
        str(tmp_path / "b.py"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "py::a.fn")
    assert len(callers) == 1, f"exactly one caller edge; callers={callers}"
    assert any("run" in c for c in callers)
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# 3. Pure-TS baseline: edge counts unchanged by lang-prefix change
# ══════════════════════════════════════════════════════════════════════════════

def test_pure_ts_edge_count_baseline(tmp_path):
    """Pure TypeScript project: edge exists exactly once, no duplication."""
    _write(tmp_path, "src/a.ts", "export function fn() {}")
    _write(tmp_path, "src/b.ts",
           "import { fn } from './a';\nexport function run() { fn(); }")

    store, pid = _reconcile(
        str(tmp_path / "src" / "a.ts"),
        str(tmp_path / "src" / "b.ts"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "js_ts::src/a.fn")
    assert len(callers) == 1, f"exactly one caller edge; callers={callers}"
    assert any("run" in c for c in callers)
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# 4. Different-name mixed: no interference
# ══════════════════════════════════════════════════════════════════════════════

def test_different_name_mixed_no_interference(tmp_path):
    """util.py and util.ts: different defines don't share call_edge slots."""
    _write(tmp_path, "util.py", "def py_helper(): pass\ndef py_caller():\n    py_helper()\n")
    _write(tmp_path, "util.ts",
           "export function tsHelper() {}\nexport function tsCaller() { tsHelper(); }")

    store, pid = _reconcile(
        str(tmp_path / "util.py"),
        str(tmp_path / "util.ts"),
        tmp_path=tmp_path,
    )
    callers_py = store.callers_of(pid, "py::util.py_helper")
    callers_ts = store.callers_of(pid, "js_ts::util.tsHelper")
    assert any("py_caller" in c for c in callers_py), f"py edge present; {callers_py}"
    assert any("tsCaller" in c for c in callers_ts), f"ts edge present; {callers_ts}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# 5. Incremental update: deleting one language file doesn't remove the other's edges
# ══════════════════════════════════════════════════════════════════════════════

def test_delete_ts_file_preserves_py_edges(tmp_path):
    """Delete mod.ts; py::mod.fn_py edges from mod.py must survive."""
    py_file = _write(tmp_path, "mod.py", "def fn_py(): pass\ndef caller_py():\n    fn_py()\n")
    ts_file = _write(tmp_path, "mod.ts", "export function fn_ts() {}\nexport function caller_ts() { fn_ts(); }")

    store, pid = _reconcile(py_file, ts_file, tmp_path=tmp_path)

    # Delete the TS file and reconcile deletion
    os.remove(ts_file)
    reconcile(store, pid, [ts_file])

    callers_py = store.callers_of(pid, "py::mod.fn_py")
    callers_ts = store.callers_of(pid, "js_ts::mod.fn_ts")

    assert any("caller_py" in c for c in callers_py), \
        f"py edges must survive ts deletion; callers_py={callers_py}"
    assert callers_ts == [], \
        f"ts edges must be removed; callers_ts={callers_ts}"
    store.close()
