"""Tests for reconcile_against_disk — pre-query reconciliation mechanism (8 tests).

Covers P1 trigger (rm/external delete missed by Bash hooks) and P2 (content mutation
from git checkout / external edits), mtime-based skip of unchanged files, idempotency,
and integration with graph queries.

Coverage:
  1. test_external_delete_detected        — os.remove without BUER notification → reconcile detects + edges cleaned
  2. test_external_content_change         — file content mutated externally (mtime advanced) → reconcile re-parses
  3. test_unchanged_skipped               — mtime unchanged → no reconcile (no parse)
  4. test_mtime_updated_after_reconcile   — after reconcile, mtime baseline updated → second call is no-op
  5. test_dir_delete_detected             — all files in directory deleted → all detected
  6. test_graph_query_clean_after_reconcile — delete file, call reconcile_against_disk → high-impact count drops
  7. test_reconcile_idempotent            — two consecutive calls without disk change → second is no-op
  8. test_none_mtime_triggers_reconcile   — recorded mtime=None (legacy record) → reconcile to establish baseline

Mutation guard: test_unchanged_skipped must go red if mtime comparison uses >= instead of >.
"""
from __future__ import annotations

import os
import tempfile
import time

import pytest

from buer.reconcile import reconcile, reconcile_against_disk
from buer.signals import HIGH_IMPACT_CALLERS
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


def _det_count_for_define(store: Store, pid: int, file_path: str, define_name: str) -> int:
    return store.con.execute(
        "SELECT COUNT(*) AS n FROM determinations WHERE project_id=? AND file_path=? AND define_name=?",
        (pid, file_path, define_name),
    ).fetchone()["n"]


def _edge_count(store: Store, pid: int) -> int:
    return store.con.execute(
        "SELECT COUNT(*) AS n FROM gd_edges WHERE project_id=?", (pid,)
    ).fetchone()["n"]


# ── 1. external delete detected ───────────────────────────────────────────────

def test_external_delete_detected():
    """os.remove without notifying BUER → reconcile_against_disk detects deletion and cleans edges."""
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)

        file_a = os.path.join(root, "a.py")
        file_b = os.path.join(root, "b.py")
        _write_py(file_a, "def target(): pass\n")
        _write_py(file_b, "def caller(): pass\n")

        # Reconcile both so BUER has records
        reconcile(store, pid, [file_a, file_b])

        # Manually insert an edge (target → caller)
        target_det_row = store.current_version_determination(pid, file_a, "target")
        caller_det_row = store.current_version_determination(pid, file_b, "caller")
        store.insert_gd_edge(pid, target_det_row["id"], caller_det_row["id"], "cross_define_callgraph")
        assert _edge_count(store, pid) == 1

        # External delete of b.py — BUER never notified
        os.remove(file_b)

        # reconcile_against_disk should detect the deletion
        n = reconcile_against_disk(store, pid, root)
        assert n >= 1, "at least b.py must have been passed to reconcile"

        # Edge must be cleared
        assert _edge_count(store, pid) == 0, "gd_edge must be removed after external delete"

        # caller's define must have a delete record
        chain = store.version_chain(pid, file_b, "caller")
        assert chain[-1]["edit_type"] == "delete", "deletion must be recorded"


# ── 2. external content change detected ───────────────────────────────────────

def test_external_content_change():
    """File content mutated externally with mtime advancing → reconcile_against_disk re-parses."""
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)
        file_path = os.path.join(root, "module.py")

        _write_py(file_path, "def original(): pass\n")
        reconcile(store, pid, [file_path])

        # Verify original define recorded
        assert _det_count_for_define(store, pid, file_path, "original") == 1

        # Small sleep to ensure mtime advances (filesystem resolution ~1s on some systems)
        # Use explicit utime manipulation to guarantee mtime > recorded
        orig_mtime = os.stat(file_path).st_mtime
        with open(file_path, "w") as f:
            f.write("def newfunc(): pass\n")
        # Bump mtime by 2 seconds explicitly to guarantee > recorded
        new_mtime = orig_mtime + 2.0
        os.utime(file_path, (new_mtime, new_mtime))

        n = reconcile_against_disk(store, pid, root)
        assert n == 1, "module.py must be reconciled"

        # original define must be deleted, newfunc created
        chain_orig = store.version_chain(pid, file_path, "original")
        assert chain_orig[-1]["edit_type"] == "delete", "original must be marked deleted"

        assert _det_count_for_define(store, pid, file_path, "newfunc") >= 1, (
            "newfunc must be recorded after external content change"
        )


# ── 3. unchanged files skipped ────────────────────────────────────────────────

def test_unchanged_skipped():
    """Files with mtime unchanged must not be passed to reconcile (avoids reparsing)."""
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)
        file_path = os.path.join(root, "stable.py")
        _write_py(file_path, "def fn(): pass\n")

        reconcile(store, pid, [file_path])

        # det count before
        before = _det_count_for_define(store, pid, file_path, "fn")

        n = reconcile_against_disk(store, pid, root)

        assert n == 0, "unchanged file must NOT be passed to reconcile"
        after = _det_count_for_define(store, pid, file_path, "fn")
        assert after == before, "det count must not change for unchanged file"


# ── 4. mtime updated after reconcile ─────────────────────────────────────────

def test_mtime_updated_after_reconcile():
    """After reconcile processes a changed file, the mtime baseline is updated → second call is no-op."""
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)
        file_path = os.path.join(root, "module.py")

        _write_py(file_path, "def v1(): pass\n")
        reconcile(store, pid, [file_path])

        # Advance mtime externally
        orig_mtime = os.stat(file_path).st_mtime
        with open(file_path, "w") as f:
            f.write("def v2(): pass\n")
        os.utime(file_path, (orig_mtime + 2.0, orig_mtime + 2.0))

        # First call — detects change
        n1 = reconcile_against_disk(store, pid, root)
        assert n1 == 1

        # Second call without further disk change — must skip
        n2 = reconcile_against_disk(store, pid, root)
        assert n2 == 0, "second reconcile_against_disk must be a no-op after mtime baseline updated"


# ── 5. directory delete detected ─────────────────────────────────────────────

def test_dir_delete_detected():
    """All files in a subdirectory deleted externally → all detected by reconcile_against_disk."""
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)

        subdir = os.path.join(root, "pkg")
        os.makedirs(subdir)
        files = [os.path.join(subdir, f"m{i}.py") for i in range(3)]
        for f in files:
            _write_py(f, f"def fn_{os.path.basename(f)[:-3]}(): pass\n")

        reconcile(store, pid, files)

        # Delete the whole directory
        import shutil
        shutil.rmtree(subdir)

        n = reconcile_against_disk(store, pid, root)
        assert n == len(files), f"all {len(files)} deleted files must be detected"

        # All defines must have delete records
        for f in files:
            rows = store.con.execute(
                "SELECT edit_type FROM determinations WHERE project_id=? AND file_path=? ORDER BY seq DESC LIMIT 1",
                (pid, f),
            ).fetchone()
            assert rows is not None and rows["edit_type"] == "delete", (
                f"define in {f} must be marked deleted"
            )


# ── 6. graph query clean after reconcile ─────────────────────────────────────

def test_graph_query_clean_after_reconcile():
    """Deleting a caller file → reconcile_against_disk → gd_caller_count drops (no ghost callers)."""
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)

        file_target = os.path.join(root, "target.py")
        _write_py(file_target, "def hot_func(): pass\n")
        reconcile(store, pid, [file_target])

        target_det = store.current_version_determination(pid, file_target, "hot_func")

        # Add HIGH_IMPACT_CALLERS caller files (each exists on disk)
        caller_dets = []
        for i in range(HIGH_IMPACT_CALLERS):
            cf = os.path.join(root, f"caller_{i}.py")
            _write_py(cf, f"def caller_{i}(): pass\n")
            reconcile(store, pid, [cf])
            cd = store.current_version_determination(pid, cf, f"caller_{i}")
            store.insert_gd_edge(pid, target_det["id"], cd["id"], "cross_define_callgraph")
            caller_dets.append(cf)

        assert store.gd_caller_count(pid, target_det["id"]) == HIGH_IMPACT_CALLERS

        # External delete of all caller files
        for cf in caller_dets:
            os.remove(cf)

        # reconcile_against_disk must detect all deletions and clean edges
        n = reconcile_against_disk(store, pid, root)
        assert n >= HIGH_IMPACT_CALLERS

        assert store.gd_caller_count(pid, target_det["id"]) == 0, (
            "gd_caller_count must be 0 after external delete + reconcile_against_disk"
        )


# ── 7. idempotent — second call without disk change is no-op ─────────────────

def test_reconcile_idempotent():
    """Two consecutive reconcile_against_disk calls without disk changes → second returns 0."""
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)
        file_path = os.path.join(root, "f.py")
        _write_py(file_path, "def fn(): pass\n")

        reconcile(store, pid, [file_path])

        n1 = reconcile_against_disk(store, pid, root)
        assert n1 == 0, "first call on already-reconciled file must be 0"

        n2 = reconcile_against_disk(store, pid, root)
        assert n2 == 0, "second call must also be 0 (idempotent)"


# ── 8. None mtime triggers reconcile (legacy records) ─────────────────────────

def test_none_mtime_triggers_reconcile():
    """recorded file_mtime=None (legacy det before column) → reconcile_against_disk triggers reconcile."""
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)
        file_path = os.path.join(root, "legacy.py")
        _write_py(file_path, "def fn(): pass\n")

        # Insert a det without file_mtime (simulating legacy record)
        store.insert_determination_atomic(
            pid, file_path, "fn", "fp_fn", "create", file_mtime=None
        )
        store.update_equivalence_class(pid, "fp_fn", f"legacy.fn")

        recorded = store.get_recorded_file_mtimes(pid)
        assert recorded[file_path] is None, "recorded mtime must be None for legacy det"

        n = reconcile_against_disk(store, pid, root)
        assert n == 1, "None mtime must trigger reconcile to establish baseline"

        # After reconcile, mtime must be set
        recorded_after = store.get_recorded_file_mtimes(pid)
        assert recorded_after[file_path] is not None, (
            "mtime baseline must be established after reconcile"
        )
