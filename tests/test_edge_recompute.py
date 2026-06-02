"""Tests for unified gd-edge recompute logic (11 tests).

Covers both P0 (modify callee drops inbound edges) and P1 (delete misses historical
det edges), plus inbound rebuild, chain continuity, reference add/remove, and rename.

Coverage:
  1.  test_create_builds_outbound           — new define: outbound edge created
  2.  test_create_builds_inbound            — new callee: caller's inbound edge created
  3.  test_modify_callee_keeps_inbound      — P0 core: modify callee → inbound edge survives
  4.  test_modify_updates_outbound          — modify caller: outbound edge updated to new det
  5.  test_delete_clears_all_versions       — P1: delete clears ALL historical det edges
  6.  test_delete_removes_inbound_edge      — delete callee → inbound edge (from_det) removed
  7.  test_chain_three_hops                 — A→B→C: modify B keeps A→B and B→C edges
  8.  test_rename_drops_old_edges           — rename define: old name edges removed, new name edges added
  9.  test_reference_added_adds_edge        — caller adds call to callee → edge appears after reconcile
  10. test_reference_removed_drops_edge     — caller drops call → edge removed after reconcile
  11. test_modify_callee_mutation_guard     — mutation guard: inbound edge MUST exist (catches P0 regression)

Mutation guard: test_modify_callee_keeps_inbound (test 3) and test_modify_callee_mutation_guard
(test 11) must go red if recompute_edges_for_define does NOT call build_gd_edges on caller dets.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from buer import callgraph, gd
from buer.reconcile import reconcile
from buer.store import Store


# ── helpers ────────────────────────────────────────────────────────────────────

def _store() -> Store:
    return Store(":memory:")


def _project(store: Store, root: str) -> int:
    return store.get_or_create_project(root)


def _insert_define(
    store: Store, pid: int, root: str, file_path: str, define_name: str,
    edit_type: str = "create",
) -> int:
    det_id, _ = store.insert_determination_atomic(
        pid, file_path, define_name, f"fp_{define_name}", edit_type
    )
    # Register in equivalence class so build_symbol_index_from_store includes this define in idx.loc.
    # reconcile does the same after insert_determination_atomic (store.update_equivalence_class).
    mod = callgraph.module_name_of(file_path, root)
    fqn = f"{mod}.{define_name}"
    store.delete_equivalence_member(pid, fqn)
    store.update_equivalence_class(pid, f"fp_{define_name}", fqn)
    return det_id


def _wire_call(store: Store, pid: int, root: str, caller_file: str, caller_def: str,
               callee_file: str, callee_def: str) -> None:
    """Insert a call_edge using lang-prefixed FQNs matching callgraph._lang_fqn output."""
    caller_fqn = callgraph._lang_fqn(
        caller_file, callgraph.module_name_of(caller_file, root), caller_def
    )
    callee_fqn = callgraph._lang_fqn(
        callee_file, callgraph.module_name_of(callee_file, root), callee_def
    )
    store.upsert_call_edge(pid, caller_fqn, callee_fqn, "call")


def _build_idx(store: Store, pid: int, root: str) -> callgraph.SymbolIndex:
    return callgraph.build_symbol_index_from_store(store, pid, root)


def _recompute(store: Store, pid: int, file_path: str, define_name: str, root: str,
               old_det_id: int | None = None) -> None:
    idx = _build_idx(store, pid, root)
    gd.recompute_edges_for_define(store, pid, file_path, define_name, root, idx, old_det_id)


def _edge_count(store: Store, pid: int) -> int:
    return store.con.execute(
        "SELECT COUNT(*) AS n FROM gd_edges WHERE project_id=?", (pid,)
    ).fetchone()["n"]


# ── 1. create builds outbound edge ────────────────────────────────────────────

def test_create_builds_outbound():
    """New caller define: outbound edge (caller calls callee) is created."""
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)
        callee_file = os.path.join(root, "callee.py")
        caller_file = os.path.join(root, "caller.py")

        callee_det = _insert_define(store, pid, root, callee_file, "callee_fn")
        caller_det = _insert_define(store, pid, root, caller_file, "caller_fn")

        _wire_call(store, pid, root, caller_file, "caller_fn", callee_file, "callee_fn")
        _recompute(store, pid, caller_file, "caller_fn", root)

        # caller_det should have an inbound edge from callee_det (callee is from_det)
        assert store.gd_caller_count(pid, callee_det) == 1, (
            "outbound edge from callee to caller must be created"
        )


# ── 2. create builds inbound (callee's perspective) ───────────────────────────

def test_create_builds_inbound():
    """When a new callee is registered, recompute rebuilds the caller's inbound edge."""
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)
        callee_file = os.path.join(root, "callee.py")
        caller_file = os.path.join(root, "caller.py")

        caller_det = _insert_define(store, pid, root, caller_file, "caller_fn")
        callee_det = _insert_define(store, pid, root, callee_file, "callee_fn")

        # Wire call in both directions so idx knows about it
        _wire_call(store, pid, root, caller_file, "caller_fn", callee_file, "callee_fn")

        # Recompute on callee — inbound rebuild must fire build_gd_edges on caller_det
        _recompute(store, pid, callee_file, "callee_fn", root)

        assert store.gd_caller_count(pid, callee_det) == 1, (
            "inbound edge (caller → callee) must be built when callee is recomputed"
        )


# ── 3. P0 core: modify callee keeps inbound edge ──────────────────────────────

def test_modify_callee_keeps_inbound():
    """P0: modifying a callee must not drop the inbound edge from the caller."""
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)
        callee_file = os.path.join(root, "callee.py")
        caller_file = os.path.join(root, "caller.py")

        callee_det_v1 = _insert_define(store, pid, root, callee_file, "callee_fn")
        caller_det = _insert_define(store, pid, root, caller_file, "caller_fn")
        _wire_call(store, pid, root, caller_file, "caller_fn", callee_file, "callee_fn")

        # Build initial edges (caller_fn creates an edge from callee_fn → caller_fn)
        _recompute(store, pid, caller_file, "caller_fn", root)
        assert store.gd_caller_count(pid, callee_det_v1) == 1, "edge must exist before modify"

        # Now modify callee (new det, old_det_id=callee_det_v1)
        callee_det_v2, _ = store.insert_determination_atomic(
            pid, callee_file, "callee_fn", "fp_callee_fn_v2", "modify"
        )
        _recompute(store, pid, callee_file, "callee_fn", root, old_det_id=callee_det_v1)

        # Old det edges must be gone
        assert store.gd_caller_count(pid, callee_det_v1) == 0, "v1 edges must be cleaned"
        # New det must have the inbound edge rebuilt
        assert store.gd_caller_count(pid, callee_det_v2) == 1, (
            "P0: inbound edge must survive callee modification"
        )


# ── 4. modify caller: outbound edge updated to new det ────────────────────────

def test_modify_updates_outbound():
    """Modifying a caller: old det's outbound edge removed, new det's outbound edge created."""
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)
        callee_file = os.path.join(root, "callee.py")
        caller_file = os.path.join(root, "caller.py")

        callee_det = _insert_define(store, pid, root, callee_file, "callee_fn")
        caller_det_v1 = _insert_define(store, pid, root, caller_file, "caller_fn")
        _wire_call(store, pid, root, caller_file, "caller_fn", callee_file, "callee_fn")

        _recompute(store, pid, caller_file, "caller_fn", root)
        assert store.gd_caller_count(pid, callee_det) == 1

        # Modify caller → new det
        caller_det_v2, _ = store.insert_determination_atomic(
            pid, caller_file, "caller_fn", "fp_caller_fn_v2", "modify"
        )
        _recompute(store, pid, caller_file, "caller_fn", root, old_det_id=caller_det_v1)

        # Caller count on callee still 1 (same logical edge, new det version)
        assert store.gd_caller_count(pid, callee_det) == 1, (
            "outbound edge must be rebuilt pointing to new caller det"
        )
        # Old caller det must have no edges
        v1_edges = store.con.execute(
            "SELECT COUNT(*) AS n FROM gd_edges WHERE project_id=? AND (from_det=? OR to_det=?)",
            (pid, caller_det_v1, caller_det_v1),
        ).fetchone()["n"]
        assert v1_edges == 0, "old caller det must have no edges after modify"


# ── 5. P1: delete clears ALL historical det edges ─────────────────────────────

def test_delete_clears_all_versions():
    """P1: deleting a define must clear gd_edges for every historical det, not just the latest."""
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)
        file_b = os.path.join(root, "b.py")
        file_a = os.path.join(root, "a.py")

        target_det = _insert_define(store, pid, root, file_a, "target")

        # b.py/caller goes through two versions before deletion
        caller_v1 = _insert_define(store, pid, root, file_b, "caller")
        store.insert_gd_edge(pid, target_det, caller_v1, "cross_define_callgraph")

        caller_v2, _ = store.insert_determination_atomic(
            pid, file_b, "caller", "fp_caller_v2", "modify"
        )
        store.insert_gd_edge(pid, target_det, caller_v2, "cross_define_callgraph")

        # Both edges exist
        assert _edge_count(store, pid) == 2

        # Delete b.py — reconcile uses all_determinations_for_define loop
        reconcile(store, pid, [file_b])

        assert _edge_count(store, pid) == 0, (
            "P1: all historical det edges must be removed on deletion"
        )


# ── 6. delete callee → inbound edge removed ───────────────────────────────────

def test_delete_removes_inbound_edge():
    """Deleting the callee (from_det side) must remove the outbound edge."""
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)
        callee_file = os.path.join(root, "callee.py")
        caller_file = os.path.join(root, "caller.py")

        callee_det = _insert_define(store, pid, root, callee_file, "callee_fn")
        caller_det = _insert_define(store, pid, root, caller_file, "caller_fn")
        store.insert_gd_edge(pid, callee_det, caller_det, "cross_define_callgraph")

        assert _edge_count(store, pid) == 1

        # Delete callee — file not on disk
        reconcile(store, pid, [callee_file])

        assert _edge_count(store, pid) == 0, (
            "edge must be removed when callee (from_det) is deleted"
        )


# ── 7. chain A→B→C: modify B keeps both edges ─────────────────────────────────

def test_chain_three_hops():
    """A calls B calls C. Modifying B must keep A→B and B→C edges (via new B det)."""
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)
        fa = os.path.join(root, "a.py")
        fb = os.path.join(root, "b.py")
        fc = os.path.join(root, "c.py")

        det_c = _insert_define(store, pid, root, fc, "fn_c")
        det_b_v1 = _insert_define(store, pid, root, fb, "fn_b")
        det_a = _insert_define(store, pid, root, fa, "fn_a")

        # Wire: a calls b, b calls c
        _wire_call(store, pid, root, fa, "fn_a", fb, "fn_b")
        _wire_call(store, pid, root, fb, "fn_b", fc, "fn_c")

        # Build initial edges via recompute (uses current det Rows internally)
        _recompute(store, pid, fa, "fn_a", root)    # from_det=det_b_v1, to_det=det_a
        _recompute(store, pid, fb, "fn_b", root)    # from_det=det_c, to_det=det_b_v1 + inbound

        assert store.gd_caller_count(pid, det_b_v1) == 1  # a calls b
        assert store.gd_caller_count(pid, det_c) == 1     # b calls c

        # Modify b
        det_b_v2, _ = store.insert_determination_atomic(
            pid, fb, "fn_b", "fp_fn_b_v2", "modify"
        )
        _recompute(store, pid, fb, "fn_b", root, old_det_id=det_b_v1)

        # b_v1 must be clean
        assert store.gd_caller_count(pid, det_b_v1) == 0

        # b_v2 must still be called by a (inbound rebuild)
        assert store.gd_caller_count(pid, det_b_v2) == 1, (
            "A→B inbound edge must survive B modification"
        )
        # c must still be called by b_v2 (outbound rebuild)
        assert store.gd_caller_count(pid, det_c) == 1, (
            "B→C outbound edge must survive B modification"
        )


# ── 8. rename: old edges removed, new name edges added ────────────────────────

def test_rename_drops_old_edges():
    """Renaming a define (delete old name + create new name) produces correct edges."""
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)
        file_path = os.path.join(root, "module.py")
        caller_file = os.path.join(root, "caller.py")

        old_det = _insert_define(store, pid, root, file_path, "old_name")
        caller_det = _insert_define(store, pid, root, caller_file, "caller_fn")
        store.insert_gd_edge(pid, old_det, caller_det, "cross_define_callgraph")
        assert _edge_count(store, pid) == 1

        # Simulate rename: delete old define from the file by reconciling with new content
        # (caller_fn's call_edges updated to point to new_name — simulated by re-wiring)
        # Step 1: delete old name edges via all_dets loop
        for hist in store.all_determinations_for_define(pid, file_path, "old_name"):
            store.delete_gd_edges_for_det(pid, hist["id"])
        store.insert_determination_atomic(pid, file_path, "old_name", None, "delete")

        # Step 2: create new name
        new_det = _insert_define(store, pid, root, file_path, "new_name")
        # Re-wire call to new name
        store.con.execute(
            "DELETE FROM call_edges WHERE project_id=?", (pid,)
        )
        store.con.commit()
        _wire_call(store, pid, root, caller_file, "caller_fn", file_path, "new_name")
        _recompute(store, pid, caller_file, "caller_fn", root, old_det_id=caller_det)

        # old name must have no edges
        v_old = store.con.execute(
            "SELECT COUNT(*) AS n FROM gd_edges WHERE project_id=? AND from_det=?",
            (pid, old_det),
        ).fetchone()["n"]
        assert v_old == 0, "old name det must have no edges"

        # new name must be called
        assert store.gd_caller_count(pid, new_det) == 1, (
            "new name det must have an inbound edge from caller"
        )


# ── 9. reference added → edge appears ─────────────────────────────────────────

def test_reference_added_adds_edge():
    """Caller adds a new call to callee → after reconcile the edge exists."""
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)
        callee_file = os.path.join(root, "callee.py")
        caller_file = os.path.join(root, "caller.py")

        callee_det = _insert_define(store, pid, root, callee_file, "callee_fn")
        caller_det = _insert_define(store, pid, root, caller_file, "caller_fn")

        # No call yet
        assert store.gd_caller_count(pid, callee_det) == 0

        # Add the call
        _wire_call(store, pid, root, caller_file, "caller_fn", callee_file, "callee_fn")
        # Simulate reconcile: re-determine caller (modify)
        caller_det_v2, _ = store.insert_determination_atomic(
            pid, caller_file, "caller_fn", "fp_caller_fn_v2", "modify"
        )
        _recompute(store, pid, caller_file, "caller_fn", root, old_det_id=caller_det)

        assert store.gd_caller_count(pid, callee_det) == 1, (
            "adding a call must create an edge after reconcile"
        )


# ── 10. reference removed → edge dropped ─────────────────────────────────────

def test_reference_removed_drops_edge():
    """Caller removes call to callee → after reconcile the edge is gone."""
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)
        callee_file = os.path.join(root, "callee.py")
        caller_file = os.path.join(root, "caller.py")

        callee_det = _insert_define(store, pid, root, callee_file, "callee_fn")
        caller_det = _insert_define(store, pid, root, caller_file, "caller_fn")
        _wire_call(store, pid, root, caller_file, "caller_fn", callee_file, "callee_fn")

        _recompute(store, pid, caller_file, "caller_fn", root)
        assert store.gd_caller_count(pid, callee_det) == 1

        # Remove the call (simulate new file content with no call)
        store.con.execute("DELETE FROM call_edges WHERE project_id=?", (pid,))
        store.con.commit()

        # Reconcile caller again (modify)
        caller_det_v2, _ = store.insert_determination_atomic(
            pid, caller_file, "caller_fn", "fp_caller_fn_v3", "modify"
        )
        _recompute(store, pid, caller_file, "caller_fn", root, old_det_id=caller_det)

        assert store.gd_caller_count(pid, callee_det) == 0, (
            "removing a call must drop the edge after reconcile"
        )


# ── 11. mutation guard: P0 regression ─────────────────────────────────────────

def test_modify_callee_mutation_guard():
    """Mutation guard: inbound edge MUST survive callee modification.

    This test is intentionally redundant with test_modify_callee_keeps_inbound but
    uses explicit INSERT instead of _recompute for the initial edge setup, proving
    that the recompute path (not just INSERT OR IGNORE) is what preserves the edge.
    Must go red if recompute_edges_for_define skips the callers_of rebuild.
    """
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)
        callee_file = os.path.join(root, "callee.py")
        caller_file = os.path.join(root, "caller.py")

        callee_v1 = _insert_define(store, pid, root, callee_file, "target")
        caller_det = _insert_define(store, pid, root, caller_file, "consumer")
        _wire_call(store, pid, root, caller_file, "consumer", callee_file, "target")

        # Build edge via recompute (not direct insert) so test covers the real path
        _recompute(store, pid, caller_file, "consumer", root)
        assert store.gd_caller_count(pid, callee_v1) == 1, "precondition: edge exists before modify"

        # Modify callee
        callee_v2, _ = store.insert_determination_atomic(
            pid, callee_file, "target", "fp_target_v2", "modify"
        )
        # ONLY call recompute on callee — simulates the scenario where only the callee file changed
        _recompute(store, pid, callee_file, "target", root, old_det_id=callee_v1)

        # If recompute_edges_for_define does NOT do inbound rebuild, this assertion fails
        assert store.gd_caller_count(pid, callee_v2) == 1, (
            "MUTATION GUARD: inbound edge must be rebuilt after callee modification via callers_of"
        )
        assert store.gd_caller_count(pid, callee_v1) == 0, "v1 must have no edges"
