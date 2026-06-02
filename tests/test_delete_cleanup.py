"""Tests for gd_edges cleanup on file/define deletion (6 tests).

Reproduces the ghost-edge bug: before the fix, deleting a file/define left
gd_edges pointing at the deleted code, inflating caller counts and polluting
influence cones, debug_range, and high-impact warnings.

Coverage:
  1. test_whole_file_delete_removes_gd_edges   — delete whole file → edges gone
  2. test_partial_delete_removes_gd_edges      — delete one define from file → that define's edges gone
  3. test_delete_target_removes_inbound_edges  — delete callee → inbound edges (to_det) also cleaned
  4. test_delete_then_caller_count_zero        — high-impact caller count drops after deletion
  5. test_demise_record_still_created          — edit_type='delete' record still written after fix
  6. test_timing_current_det_before_delete     — gd_edges cleaned from live (create/modify) det, not delete det

Mutation guard: test_whole_file_delete_removes_gd_edges must go red if
delete_gd_edges_for_det is called AFTER insert_determination_atomic.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from buer.reconcile import reconcile
from buer.signals import HIGH_IMPACT_CALLERS, check_high_impact_defines
from buer.store import Store


# ── helpers ────────────────────────────────────────────────────────────────────

def _store() -> Store:
    return Store(":memory:")


def _project(store: Store, root: str) -> int:
    return store.get_or_create_project(root)


def _insert_define(store: Store, pid: int, file_path: str, define_name: str) -> int:
    det_id, _ = store.insert_determination_atomic(
        pid, file_path, define_name, f"fp_{define_name}", "create"
    )
    return det_id


# ── 1. whole-file deletion removes gd_edges ───────────────────────────────────

def test_whole_file_delete_removes_gd_edges():
    """Deleting a whole file must clean up all gd_edges for its defines."""
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)

        # target stays, caller will be deleted
        file_a = os.path.join(root, "a.py")
        file_b = os.path.join(root, "b.py")

        target_det = _insert_define(store, pid, file_a, "target_func")
        caller_det = _insert_define(store, pid, file_b, "caller_func")

        # gd_edge: target produces, caller consumes (from_det=target, to_det=caller)
        store.insert_gd_edge(pid, target_det, caller_det, "cross_define")

        # confirm edge exists before deletion
        assert store.gd_caller_count(pid, target_det) == 1

        # file_b was never created on disk → whole-file deletion path fires
        reconcile(store, pid, [file_b])

        assert store.gd_caller_count(pid, target_det) == 0, (
            "gd_edge must be removed when the caller file is deleted"
        )


# ── 2. partial deletion removes gd_edges for deleted define ───────────────────

def test_partial_delete_removes_gd_edges():
    """Removing a define from a file must clean up that define's gd_edges."""
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)

        file_path = os.path.join(root, "module.py")

        # Insert both f1 and f2 into DB, then write file with only f1
        det_f1 = _insert_define(store, pid, file_path, "f1")
        det_f2 = _insert_define(store, pid, file_path, "f2")

        # f2 has a caller edge
        some_det = _insert_define(store, pid, os.path.join(root, "other.py"), "other")
        store.insert_gd_edge(pid, det_f2, some_det, "cross_define")
        assert store.gd_caller_count(pid, det_f2) == 1

        # Write file with only f1 — reconcile must detect f2 as deleted
        with open(file_path, "w") as fh:
            fh.write("def f1(): pass\n")

        reconcile(store, pid, [file_path])

        assert store.gd_caller_count(pid, det_f2) == 0, (
            "gd_edges for deleted define f2 must be removed"
        )
        # f1's edges (none) unaffected
        assert store.gd_caller_count(pid, det_f1) == 0


# ── 3. deleting callee removes inbound edges (to_det direction) ───────────────

def test_delete_target_removes_inbound_edges():
    """Deleting the callee (from_det) side also cleans the edge."""
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)

        file_target = os.path.join(root, "target.py")
        file_caller = os.path.join(root, "caller.py")

        target_det = _insert_define(store, pid, file_target, "target_func")
        caller_det = _insert_define(store, pid, file_caller, "caller_func")

        # Edge: target → caller (target is from_det)
        store.insert_gd_edge(pid, target_det, caller_det, "cross_define")

        total_before = store.con.execute(
            "SELECT COUNT(*) AS n FROM gd_edges WHERE project_id=?", (pid,)
        ).fetchone()["n"]
        assert total_before == 1

        # Delete target file
        reconcile(store, pid, [file_target])

        total_after = store.con.execute(
            "SELECT COUNT(*) AS n FROM gd_edges WHERE project_id=?", (pid,)
        ).fetchone()["n"]
        assert total_after == 0, (
            "edge from deleted callee (from_det direction) must be removed"
        )


# ── 4. caller count drops to zero after deletion ─────────────────────────────

def test_delete_then_caller_count_zero():
    """High-impact caller count must not be inflated by deleted code."""
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)

        file_target = os.path.join(root, "target.py")

        target_det = _insert_define(store, pid, file_target, "hot_func")

        # Add HIGH_IMPACT_CALLERS callers from separate files
        caller_files = []
        for i in range(HIGH_IMPACT_CALLERS):
            cf = os.path.join(root, f"caller_{i}.py")
            cd = _insert_define(store, pid, cf, f"caller_{i}")
            store.insert_gd_edge(pid, target_det, cd, "cross_define")
            caller_files.append(cf)

        assert store.gd_caller_count(pid, target_det) == HIGH_IMPACT_CALLERS

        # Delete all caller files (none exist on disk)
        for cf in caller_files:
            reconcile(store, pid, [cf])

        assert store.gd_caller_count(pid, target_det) == 0, (
            "caller count must be 0 after all caller files are deleted"
        )

        # High-impact warning must no longer fire
        warned: set = set()
        warnings = check_high_impact_defines(
            store, pid, file_target, "sess1", warned
        )
        assert warnings == [], "No high-impact warning after callers deleted"


# ── 5. demise record still created after fix ─────────────────────────────────

def test_demise_record_still_created():
    """The fix must not break the SDT §2.2.5 demise trace (edit_type='delete')."""
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)

        file_b = os.path.join(root, "b.py")
        caller_det = _insert_define(store, pid, file_b, "caller_func")

        # file_b not on disk
        reconcile(store, pid, [file_b])

        chain = store.version_chain(pid, file_b, "caller_func")
        assert len(chain) >= 2, "version chain must have at least create + delete records"
        assert chain[-1]["edit_type"] == "delete", (
            "last determination in chain must be edit_type='delete'"
        )


# ── 6. timing: edges cleaned from live det, not from delete det ───────────────

def test_timing_current_det_before_delete():
    """delete_gd_edges_for_det must be called on the create/modify det, not the delete det."""
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)

        file_b = os.path.join(root, "b.py")
        file_a = os.path.join(root, "a.py")

        target_det = _insert_define(store, pid, file_a, "target")
        live_det = _insert_define(store, pid, file_b, "caller")  # create det

        # Edge attached to the live (create) det
        store.insert_gd_edge(pid, target_det, live_det, "cross_define")
        assert store.gd_caller_count(pid, target_det) == 1

        reconcile(store, pid, [file_b])

        # After reconcile, a delete det was inserted.  Verify:
        # (a) gd_edges are gone (correct: cleaned from live_det before demise recorded)
        assert store.gd_caller_count(pid, target_det) == 0, (
            "edge must be cleaned regardless of which det the delete record uses"
        )

        # (b) the delete det itself has no edges (it's a new det with no edges)
        chain = store.version_chain(pid, file_b, "caller")
        delete_det_id = chain[-1]["id"]
        assert delete_det_id != live_det, "delete det must be a new row"
        delete_det_edges = store.con.execute(
            "SELECT COUNT(*) AS n FROM gd_edges WHERE project_id=? AND (from_det=? OR to_det=?)",
            (pid, delete_det_id, delete_det_id),
        ).fetchone()["n"]
        assert delete_det_edges == 0, (
            "delete det should never accumulate edges; gd_edges_for_det points at live det"
        )
