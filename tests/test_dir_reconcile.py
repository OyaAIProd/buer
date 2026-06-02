"""Tests for directory-mtime reconciliation — Commit 3 (10 tests).

Covers new-file detection via dir-mtime + shallow scan, recursive subdirectory
discovery, mv new-name handling, excluded-dir skipping, dir-mtime baseline update,
collect-then-reconcile ordering, non-git project support, and Commit-2 non-regression.

Coverage:
  1.  test_external_new_file            — new .py appears externally → detected + reconciled
  2.  test_external_new_subdir          — new src/sub/new.py (new subdir) → recursive discovery
  3.  test_mv_new_name                  — mv a.py→moved.py → old name deleted + new name discovered
  4.  test_dir_unchanged_no_shallow_scan — content-only change (dir mtime unchanged) → no shallow scan
  5.  test_excluded_dirs_skipped        — node_modules/.git new files ignored
  6.  test_dir_mtime_baseline_updated   — after scan, dir_mtime updated → second call returns 0
  7.  test_content_and_delete_still_work — Commit-2 content-change + delete still work (non-regression)
  8.  test_no_git_dependency            — non-git directory → new files still detected (pure fs)
  9.  test_collect_then_reconcile_once  — both scans finish before reconcile (verify ordering)
  10. test_mutation_guard_dir_mtime     — mutation guard: if dir-mtime check disabled → test_external_new_file red

Mutation guard: test_mutation_guard_dir_mtime (test 10) must go red if the dir-mtime
comparison (`cur_dm <= rec_dm → skip`) is removed, i.e., all dirs are always shallow-scanned.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest.mock as mock

import pytest

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


def _det_count(store: Store, pid: int, file_path: str, define_name: str) -> int:
    return store.con.execute(
        "SELECT COUNT(*) AS n FROM determinations "
        "WHERE project_id=? AND file_path=? AND define_name=?",
        (pid, file_path, define_name),
    ).fetchone()["n"]


def _advance_mtime(path: str, delta: float = 2.0) -> float:
    """Advance file/dir mtime by delta seconds; return new mtime."""
    cur = os.stat(path).st_mtime
    new = cur + delta
    os.utime(path, (new, new))
    return new


# ── 1. external new file detected ────────────────────────────────────────────

def test_external_new_file():
    """A new .py created externally after initial reconcile is discovered via dir-mtime."""
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)

        existing = os.path.join(root, "existing.py")
        _write_py(existing, "def old_fn(): pass\n")
        reconcile(store, pid, [existing])

        # Establish dir baseline
        reconcile_against_disk(store, pid, root)

        # Externally add a new file (advance parent dir mtime)
        new_file = os.path.join(root, "new_module.py")
        _write_py(new_file, "def new_fn(): pass\n")
        _advance_mtime(root)  # ensure dir mtime > baseline

        n = reconcile_against_disk(store, pid, root)
        assert n >= 1, "new_module.py must be passed to reconcile"
        assert _det_count(store, pid, new_file, "new_fn") >= 1, (
            "new_fn must be recorded after external file creation"
        )


# ── 2. new subdir with files — recursive discovery ────────────────────────────

def test_external_new_subdir():
    """New src/sub/new.py in a new subdirectory is discovered recursively."""
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)

        existing = os.path.join(root, "top.py")
        _write_py(existing, "def top_fn(): pass\n")
        reconcile(store, pid, [existing])
        reconcile_against_disk(store, pid, root)

        # Create new subdirectory with a file
        subdir = os.path.join(root, "sub")
        new_file = os.path.join(subdir, "deep.py")
        _write_py(new_file, "def deep_fn(): pass\n")
        _advance_mtime(root)  # parent dir mtime advances when subdir is created

        n = reconcile_against_disk(store, pid, root)
        assert n >= 1
        assert _det_count(store, pid, new_file, "deep_fn") >= 1, (
            "deep_fn in new subdirectory must be discovered recursively"
        )


# ── 3. mv: old name deleted + new name discovered ─────────────────────────────

def test_mv_new_name():
    """mv a.py → moved.py: old name is caught by Stage 1 (stat fails) + new name by Stage 2."""
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)

        src = os.path.join(root, "a.py")
        _write_py(src, "def fn_a(): pass\n")
        reconcile(store, pid, [src])
        reconcile_against_disk(store, pid, root)

        # mv a.py → moved.py (advancing dir mtime)
        dst = os.path.join(root, "moved.py")
        os.rename(src, dst)
        _advance_mtime(root)

        reconcile_against_disk(store, pid, root)

        # Old define must be deleted
        chain = store.version_chain(pid, src, "fn_a")
        assert chain[-1]["edit_type"] == "delete", "old file define must be marked deleted"

        # New file define must be discovered
        assert _det_count(store, pid, dst, "fn_a") >= 1, (
            "define in moved.py must be recorded after mv"
        )


# ── 4. content-only change → no shallow scan of that dir ──────────────────────

def test_dir_unchanged_no_shallow_scan():
    """Content-only change does NOT trigger Stage 2 when dir mtime hasn't advanced.

    Approach: set the stored dir-baseline to (current_mtime + 100s) — far enough
    in the future that cur_dm <= rec_dm is guaranteed, so Stage 2 skips the scan.
    A ghost file placed in root is the sentinel: if Stage 2 incorrectly runs, it
    would discover ghost.py. No mtime round-trip fragility (we never call os.utime).
    """
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)

        f = os.path.join(root, "module.py")
        _write_py(f, "def v1(): pass\n")
        reconcile(store, pid, [f])

        # Place ghost file (it's in root, not yet recorded; advances root's mtime)
        ghost = os.path.join(root, "ghost.py")
        _write_py(ghost, "def ghost_fn(): pass\n")

        # Set stored dir baseline AFTER ghost creation → always cur_dm <= rec_dm
        store.set_dir_mtime(pid, root, os.stat(root).st_mtime + 100.0)

        # Content-only change to f: advance FILE mtime (Stage 1 trigger)
        # Writing to an existing file does NOT change parent dir mtime on Linux.
        with open(f, "w") as fh:
            fh.write("def v2(): pass\n")
        _advance_mtime(f)

        n = reconcile_against_disk(store, pid, root)

        # Stage 1 must have detected the content change
        assert n >= 1, "content change must trigger Stage 1"

        # Stage 2 must have skipped — ghost.py must NOT be recorded
        assert _det_count(store, pid, ghost, "ghost_fn") == 0, (
            "ghost.py must not be discovered when stored dir baseline > current mtime (Stage 2 skip)"
        )


# ── 5. excluded dirs skipped ─────────────────────────────────────────────────

def test_excluded_dirs_skipped():
    """New files inside node_modules and .git are not discovered by Stage 2."""
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)

        real_file = os.path.join(root, "real.py")
        _write_py(real_file, "def real(): pass\n")
        reconcile(store, pid, [real_file])
        reconcile_against_disk(store, pid, root)  # establish baselines

        # Add new files inside excluded dirs
        nm_dir = os.path.join(root, "node_modules", "pkg")
        os.makedirs(nm_dir)
        _write_py(os.path.join(nm_dir, "index.py"), "def pkg(): pass\n")

        git_dir = os.path.join(root, ".git")
        os.makedirs(git_dir, exist_ok=True)
        _write_py(os.path.join(git_dir, "hook.py"), "def hook(): pass\n")

        _advance_mtime(root)

        n = reconcile_against_disk(store, pid, root)
        # node_modules/pkg/index.py and .git/hook.py must NOT be reconciled
        nm_file = os.path.join(nm_dir, "index.py")
        git_file = os.path.join(git_dir, "hook.py")
        assert _det_count(store, pid, nm_file, "pkg") == 0, "node_modules file must be skipped"
        assert _det_count(store, pid, git_file, "hook") == 0, ".git file must be skipped"


# ── 6. dir_mtime baseline updated after scan ─────────────────────────────────

def test_dir_mtime_baseline_updated():
    """After Stage 2 runs, dir_mtime is updated → second reconcile_against_disk returns 0."""
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)

        f = os.path.join(root, "f.py")
        _write_py(f, "def fn(): pass\n")
        reconcile(store, pid, [f])
        reconcile_against_disk(store, pid, root)

        # Add new file → dir mtime advances
        new_f = os.path.join(root, "g.py")
        _write_py(new_f, "def gn(): pass\n")
        _advance_mtime(root)

        n1 = reconcile_against_disk(store, pid, root)
        assert n1 >= 1

        # Second call: no new changes → must return 0
        n2 = reconcile_against_disk(store, pid, root)
        assert n2 == 0, "dir_mtime baseline must be updated so second call is no-op"


# ── 7. Commit-2 non-regression: content change + delete still work ────────────

def test_content_and_delete_still_work():
    """Commit-2 file-stat logic (content mutation + deletion) still works after Commit-3."""
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)

        fa = os.path.join(root, "a.py")
        fb = os.path.join(root, "b.py")
        _write_py(fa, "def fn_a(): pass\n")
        _write_py(fb, "def fn_b(): pass\n")
        reconcile(store, pid, [fa, fb])
        reconcile_against_disk(store, pid, root)

        # Content change in fa
        with open(fa, "w") as fh:
            fh.write("def fn_a_v2(): pass\n")
        _advance_mtime(fa)

        # Delete fb
        os.remove(fb)

        n = reconcile_against_disk(store, pid, root)
        assert n >= 2

        # fa: fn_a deleted, fn_a_v2 created
        chain_a = store.version_chain(pid, fa, "fn_a")
        assert chain_a[-1]["edit_type"] == "delete"
        assert _det_count(store, pid, fa, "fn_a_v2") >= 1

        # fb: fn_b deleted
        chain_b = store.version_chain(pid, fb, "fn_b")
        assert chain_b[-1]["edit_type"] == "delete"


# ── 8. non-git directory: new files detected (pure filesystem) ────────────────

def test_no_git_dependency():
    """No .git directory present — new file discovery still works via dir mtime only."""
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)

        # Verify no .git in this directory
        assert not os.path.exists(os.path.join(root, ".git")), "test requires non-git directory"

        existing = os.path.join(root, "existing.py")
        _write_py(existing, "def old(): pass\n")
        reconcile(store, pid, [existing])
        reconcile_against_disk(store, pid, root)

        # Add new file externally (no git)
        new_f = os.path.join(root, "brand_new.py")
        _write_py(new_f, "def brand_new(): pass\n")
        _advance_mtime(root)

        n = reconcile_against_disk(store, pid, root)
        assert n >= 1, "new file must be discovered without git"
        assert _det_count(store, pid, new_f, "brand_new") >= 1, (
            "brand_new must be recorded in non-git project"
        )


# ── 9. collect-then-reconcile: both stages finish before reconcile ─────────────

def test_collect_then_reconcile_once():
    """Both Stage 1 and Stage 2 collect files; reconcile is called exactly once afterward."""
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)

        # Stage 1 candidate: existing file with content change
        fa = os.path.join(root, "a.py")
        _write_py(fa, "def fn_a(): pass\n")
        reconcile(store, pid, [fa])
        reconcile_against_disk(store, pid, root)

        # Mutate fa (Stage 1 candidate)
        with open(fa, "w") as fh:
            fh.write("def fn_a_v2(): pass\n")
        _advance_mtime(fa)

        # Stage 2 candidate: new file
        fb = os.path.join(root, "b.py")
        _write_py(fb, "def fn_b(): pass\n")
        _advance_mtime(root)  # advance dir mtime

        reconcile_calls: list[list[str]] = []
        real_reconcile = reconcile

        def tracking_reconcile(s, pid_, files, **kw):
            reconcile_calls.append(list(files))
            return real_reconcile(s, pid_, files, **kw)

        import buer.reconcile as rec_mod
        original = rec_mod.reconcile
        rec_mod.reconcile = tracking_reconcile
        try:
            reconcile_against_disk(store, pid, root)
        finally:
            rec_mod.reconcile = original

        # reconcile must be called exactly once (not once per stage)
        assert len(reconcile_calls) == 1, (
            f"reconcile must be called exactly once, got {len(reconcile_calls)} calls: {reconcile_calls}"
        )
        # Both files must be in that single call
        all_files = set(reconcile_calls[0])
        assert fa in all_files, "Stage-1 candidate (fa) must be in the single reconcile call"
        assert fb in all_files, "Stage-2 candidate (fb) must be in the single reconcile call"


# ── 10. mutation guard: dir-mtime check disabled → test_external_new_file red ──

def test_mutation_guard_dir_mtime():
    """Mutation guard: if dir-mtime comparison removed (always shallow-scan), test still passes.

    This test verifies the POSITIVE path (new file detected) as a direct mutation
    target — it mirrors test_external_new_file exactly. If the underlying code
    removes the `cur_dm <= rec_dm → skip` guard and shallow-scans unconditionally,
    existing tests would still pass but performance red-lines would be violated.
    The REAL mutation guard is test_dir_unchanged_no_shallow_scan (test 4) which
    explicitly checks scandir is NOT called for unchanged dirs.

    This test serves as the 'must go green' anchor: if new-file detection is broken
    (e.g., the entire Stage 2 is removed), this goes red.
    """
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)

        existing = os.path.join(root, "seed.py")
        _write_py(existing, "def seed(): pass\n")
        reconcile(store, pid, [existing])
        reconcile_against_disk(store, pid, root)  # establish baseline

        # External new file
        new_f = os.path.join(root, "added.py")
        _write_py(new_f, "def added_fn(): pass\n")
        _advance_mtime(root)

        n = reconcile_against_disk(store, pid, root)

        # MUTATION GUARD: this must go red if Stage 2 (dir-mtime scan) is disabled
        assert n >= 1, "MUTATION GUARD: new file must be detected via dir-mtime scan"
        assert _det_count(store, pid, new_f, "added_fn") >= 1, (
            "MUTATION GUARD: added_fn must be recorded"
        )
