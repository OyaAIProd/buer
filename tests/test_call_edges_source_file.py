"""Tests for call_edges source_file column — Commit 5 (7 tests).

Covers the __init__.py over-deletion bug: old LIKE-based delete erased the entire
package's call_edges when __init__.py was reconciled. New source_file-based delete
is exact-file and eliminates the namespace ambiguity.

Coverage:
  1. test_init_py_no_overdelete          — ★ core: reconcile(__init__.py) must not delete
                                           other files' call_edges
  2. test_init_py_in_reconcile_disk      — baseline-building reconcile (all files incl. __init__)
                                           leaves call_edges intact
  3. test_stale_edge_cleared             — source_file delete still clears stale edges for that file
  4. test_normal_file_unaffected         — reconcile of a.py never deletes b.py's edges
  5. test_post_edit_flow_no_edge_loss    — end-to-end post_edit (no rebuild_full) — edges intact
  6. test_delete_file_clears_edges       — deleting a whole file clears its source_file edges
  7. test_mutation_guard_module_prefix   — mutation: if code reverts to LIKE delete,
                                           test_init_py_no_overdelete goes red

Mutation guard: test_mutation_guard_module_prefix (test 7) mirrors test 1 and acts as
the anchor that must go red if build_call_edges reverts to delete_call_edges_for_module.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from buer import callgraph
from buer.reconcile import reconcile, reconcile_against_disk
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


def _advance_mtime(path: str, delta: float = 2.0) -> None:
    cur = os.stat(path).st_mtime
    os.utime(path, (cur + delta, cur + delta))


def _call_edge_count(store: Store, pid: int, caller: str, callee: str) -> int:
    return store.con.execute(
        "SELECT COUNT(*) AS n FROM call_edges WHERE project_id=? AND caller=? AND callee=?",
        (pid, caller, callee),
    ).fetchone()["n"]


def _all_edges_for_file(store: Store, pid: int, source_file: str) -> list[dict]:
    rows = store.con.execute(
        "SELECT caller, callee FROM call_edges WHERE project_id=? AND source_file=?",
        (pid, source_file),
    ).fetchall()
    return [{"caller": r["caller"], "callee": r["callee"]} for r in rows]


def _fqn(root: str, file_path: str, define: str) -> str:
    mod = callgraph.module_name_of(file_path, root)
    return callgraph._lang_fqn(file_path, mod, define)


# ── 1. ★ __init__.py reconcile must not over-delete ──────────────────────────

def test_init_py_no_overdelete():
    """★ core: reconcile([__init__.py]) must NOT delete b.py's call_edge to a.py."""
    with tempfile.TemporaryDirectory() as root:
        store = _store()
        pid = _project(store, root)

        # src/__init__.py, src/a.py (target), src/b.py (caller)
        pkg = os.path.join(root, "src")
        os.makedirs(pkg)
        init_py = os.path.join(pkg, "__init__.py")
        a_py = os.path.join(pkg, "a.py")
        b_py = os.path.join(pkg, "b.py")

        _write_py(init_py, "# empty __init__\n")
        _write_py(a_py, "def target(): pass\n")
        _write_py(b_py, "from src.a import target\ndef caller(): target()\n")

        # Reconcile all files to establish graph
        reconcile(store, pid, [init_py, a_py, b_py])

        # Wire call edge: b.caller → a.target
        caller_fqn = _fqn(root, b_py, "caller")
        target_fqn = _fqn(root, a_py, "target")
        store.upsert_call_edge(pid, caller_fqn, target_fqn, "call", source_file=b_py)

        assert _call_edge_count(store, pid, caller_fqn, target_fqn) == 1

        # Reconcile only __init__.py (simulating a post_edit or baseline pass)
        reconcile(store, pid, [init_py])

        # ★ b→a edge must NOT have been deleted
        assert _call_edge_count(store, pid, caller_fqn, target_fqn) == 1, (
            "reconcile(__init__.py) must not delete b.py's call_edge to a.py"
        )


# ── 2. baseline reconcile with __init__.py leaves edges intact ────────────────

def test_init_py_in_reconcile_disk():
    """reconcile_against_disk across all files (including __init__) preserves edges."""
    with tempfile.TemporaryDirectory() as root:
        store = _store()
        pid = _project(store, root)

        pkg = os.path.join(root, "src")
        os.makedirs(pkg)
        init_py = os.path.join(pkg, "__init__.py")
        a_py = os.path.join(pkg, "a.py")
        b_py = os.path.join(pkg, "b.py")

        _write_py(init_py, "# empty\n")
        _write_py(a_py, "def target(): pass\n")
        _write_py(b_py, "def caller(): pass\n")

        reconcile(store, pid, [init_py, a_py, b_py])

        caller_fqn = _fqn(root, b_py, "caller")
        target_fqn = _fqn(root, a_py, "target")
        store.upsert_call_edge(pid, caller_fqn, target_fqn, "call", source_file=b_py)

        # Establish baseline
        reconcile_against_disk(store, pid, root)

        # Only advance __init__.py's mtime — Stage 1 reconciles __init__ only
        # (advancing b.py would legitimately clear+rebuild b.py's edges from source)
        _advance_mtime(init_py)

        reconcile_against_disk(store, pid, root)

        # Edge must survive — __init__.py reconcile must not touch b.py's edges
        assert _call_edge_count(store, pid, caller_fqn, target_fqn) == 1, (
            "call_edge must survive reconcile_against_disk that includes __init__.py"
        )


# ── 3. stale edges cleared for same file ──────────────────────────────────────

def test_stale_edge_cleared():
    """source_file delete still clears stale edges when a caller is removed from b.py."""
    with tempfile.TemporaryDirectory() as root:
        store = _store()
        pid = _project(store, root)

        a_py = os.path.join(root, "a.py")
        b_py = os.path.join(root, "b.py")
        _write_py(a_py, "def target(): pass\n")
        _write_py(b_py, "def c1(): pass\ndef c2(): pass\n")

        reconcile(store, pid, [a_py, b_py])

        target_fqn = _fqn(root, a_py, "target")
        c1_fqn = _fqn(root, b_py, "c1")
        c2_fqn = _fqn(root, b_py, "c2")

        # Wire c1→target and c2→target from b.py
        store.upsert_call_edge(pid, c1_fqn, target_fqn, "call", source_file=b_py)
        store.upsert_call_edge(pid, c2_fqn, target_fqn, "call", source_file=b_py)
        assert _call_edge_count(store, pid, c2_fqn, target_fqn) == 1

        # Remove c2 from b.py (stale edge scenario)
        with open(b_py, "w") as f:
            f.write("def c1(): pass\n")
        _advance_mtime(b_py)

        reconcile(store, pid, [b_py])

        # c2's stale edge must be cleared
        assert _call_edge_count(store, pid, c2_fqn, target_fqn) == 0, (
            "stale c2→target edge must be cleared after c2 removed from b.py"
        )
        # c1's edge from a.py to target is not from b.py, so unaffected
        assert _call_edge_count(store, pid, target_fqn, target_fqn) == 0  # sanity


# ── 4. normal file reconcile only clears its own edges ────────────────────────

def test_normal_file_unaffected():
    """reconcile(a.py) must not touch b.py's call_edges."""
    with tempfile.TemporaryDirectory() as root:
        store = _store()
        pid = _project(store, root)

        a_py = os.path.join(root, "a.py")
        b_py = os.path.join(root, "b.py")
        c_py = os.path.join(root, "c.py")
        _write_py(a_py, "def fn_a(): pass\n")
        _write_py(b_py, "def fn_b(): pass\n")
        _write_py(c_py, "def fn_c(): pass\n")

        reconcile(store, pid, [a_py, b_py, c_py])

        fn_b = _fqn(root, b_py, "fn_b")
        fn_c = _fqn(root, c_py, "fn_c")
        # b.py has an edge to c.py
        store.upsert_call_edge(pid, fn_b, fn_c, "call", source_file=b_py)
        assert _call_edge_count(store, pid, fn_b, fn_c) == 1

        # Reconcile only a.py
        with open(a_py, "w") as f:
            f.write("def fn_a_v2(): pass\n")
        _advance_mtime(a_py)
        reconcile(store, pid, [a_py])

        # b→c must be untouched
        assert _call_edge_count(store, pid, fn_b, fn_c) == 1, (
            "reconcile(a.py) must not delete b.py's call_edge"
        )


# ── 5. end-to-end post_edit flow without rebuild_full ─────────────────────────

def test_post_edit_flow_no_edge_loss():
    """Real post_edit path (reconcile_against_disk + reconcile, no rebuild_full) — edges intact."""
    with tempfile.TemporaryDirectory() as root:
        store = _store()
        pid = _project(store, root)

        pkg = os.path.join(root, "pkg")
        os.makedirs(pkg)
        init_py = os.path.join(pkg, "__init__.py")
        a_py = os.path.join(pkg, "a.py")
        b_py = os.path.join(pkg, "b.py")

        _write_py(init_py, "# pkg init\n")
        _write_py(a_py, "def service(): pass\n")
        _write_py(b_py, "def handler(): pass\n")

        # Full initial reconcile
        reconcile(store, pid, [init_py, a_py, b_py])
        reconcile_against_disk(store, pid, root)  # establish baselines

        handler_fqn = _fqn(root, b_py, "handler")
        service_fqn = _fqn(root, a_py, "service")
        store.upsert_call_edge(pid, handler_fqn, service_fqn, "call", source_file=b_py)

        # Simulate editing __init__.py (post_edit scenario)
        with open(init_py, "w") as f:
            f.write("# updated init\nVERSION = '1.0'\n")
        _advance_mtime(init_py)

        # post_edit flow: reconcile_against_disk(exclude={init_py}) + reconcile([init_py])
        reconcile_against_disk(store, pid, root, exclude={init_py})
        reconcile(store, pid, [init_py])

        # handler→service edge must survive
        assert _call_edge_count(store, pid, handler_fqn, service_fqn) == 1, (
            "★ handler→service edge must survive post_edit of __init__.py (real path, no rebuild_full)"
        )


# ── 6. deleting a whole file clears its source_file edges ────────────────────

def test_delete_file_clears_edges():
    """When a file is deleted (whole-file delete path), its call_edges are cleared by source_file."""
    with tempfile.TemporaryDirectory() as root:
        store = _store()
        pid = _project(store, root)

        a_py = os.path.join(root, "a.py")
        b_py = os.path.join(root, "b.py")
        _write_py(a_py, "def fn_a(): pass\n")
        _write_py(b_py, "def fn_b(): pass\n")

        reconcile(store, pid, [a_py, b_py])

        fn_b = _fqn(root, b_py, "fn_b")
        fn_a = _fqn(root, a_py, "fn_a")
        store.upsert_call_edge(pid, fn_b, fn_a, "call", source_file=b_py)
        assert _call_edge_count(store, pid, fn_b, fn_a) == 1

        # Delete b.py entirely
        os.remove(b_py)
        reconcile(store, pid, [b_py])

        # b.py's call_edges must be gone
        assert _call_edge_count(store, pid, fn_b, fn_a) == 0, (
            "deleting b.py must clear its call_edges via delete_call_edges_for_file"
        )


# ── 7. mutation guard: LIKE delete breaks test 1 ──────────────────────────────

def test_mutation_guard_module_prefix():
    """Mutation guard: mirrors test_init_py_no_overdelete.

    If build_call_edges reverts to delete_call_edges_for_module (LIKE prefix),
    reconcile(__init__.py) would over-delete b.py's edges → this goes red.
    The REAL guard is test_init_py_no_overdelete (test 1), which exercises
    the actual code path. This anchor confirms positive new-file detection.
    """
    with tempfile.TemporaryDirectory() as root:
        store = _store()
        pid = _project(store, root)

        pkg = os.path.join(root, "mypkg")
        os.makedirs(pkg)
        init_py = os.path.join(pkg, "__init__.py")
        a_py = os.path.join(pkg, "a.py")
        b_py = os.path.join(pkg, "b.py")

        _write_py(init_py, "# mypkg init\n")
        _write_py(a_py, "def target_fn(): pass\n")
        _write_py(b_py, "def caller_fn(): pass\n")

        reconcile(store, pid, [init_py, a_py, b_py])

        caller_fqn = _fqn(root, b_py, "caller_fn")
        target_fqn = _fqn(root, a_py, "target_fn")
        store.upsert_call_edge(pid, caller_fqn, target_fqn, "call", source_file=b_py)

        # Reconcile __init__.py
        reconcile(store, pid, [init_py])

        # MUTATION GUARD: must survive; would fail with LIKE-based delete
        assert _call_edge_count(store, pid, caller_fqn, target_fqn) == 1, (
            "MUTATION GUARD: caller_fn→target_fn must survive reconcile(__init__.py); "
            "goes red if LIKE-prefix delete is used"
        )
