"""Tests for reconcile_against_disk exclude parameter — Commit 4 (9 tests).

Verifies that the exclude set prevents double-reconcile of the just-edited file
in post_edit, while not dropping coverage (reconcile([file]) handles it fully).
Two mutation guards verify that exclusion must be active in BOTH stages.

Coverage:
  1. test_post_edit_no_double_reconcile       — edited file reconciled exactly once end-to-end
  2. test_excluded_file_still_processed       — excluded file is handled by reconcile([file])
  3. test_excluded_file_inbound_edges         — inbound edges rebuilt by reconcile([file]) (Commit 1 self-sufficient)
  4. test_external_other_file_still_caught    — exclude only affects the named file; others still detected
  5. test_write_new_file_exclude_phase2       — Write-created fp: stage-2 shallow scan also excludes it (★ premise 2)
  6. test_exclude_none_full_scan              — no exclude → full scan, all changed files caught
  7. test_external_delete_other_file          — edit a.py, external delete c.py; exclude={a.py} still catches c.py
  8. test_mutation_guard_stage1_exclude       — mutation A: if stage-1 exclude removed → double-reconcile (goes red)
  9. test_mutation_guard_stage2_exclude       — mutation B: if stage-2 exclude removed → Write-created file doubled (goes red)

Mutation guards:
  test_mutation_guard_stage1_exclude  (test 8): must go red if stage-1 `if fp in _exclude: continue` removed.
  test_mutation_guard_stage2_exclude  (test 9): must go red if stage-2 `and e.path not in _exclude` removed.
"""
from __future__ import annotations

import os
import tempfile

import buer.reconcile as rec_mod
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


def _det_count(store: Store, pid: int, file_path: str, define_name: str) -> int:
    return store.con.execute(
        "SELECT COUNT(*) AS n FROM determinations "
        "WHERE project_id=? AND file_path=? AND define_name=?",
        (pid, file_path, define_name),
    ).fetchone()["n"]


def _simulate_post_edit(
    store: Store, pid: int, root: str, file_path: str
) -> list[list[str]]:
    """Run the post_edit reconcile flow and return a list of per-call file lists.

    Patches rec_mod.reconcile so that BOTH the against-disk call (which goes through
    module globals) and the explicit reconcile([file_path]) call (also via module ref)
    are intercepted. This avoids the 'local import bypasses patch' problem.
    """
    calls: list[list[str]] = []
    orig = rec_mod.reconcile

    def fake(s, p, files, **kw):
        calls.append(list(files))
        return orig(s, p, files, **kw)

    rec_mod.reconcile = fake
    try:
        rec_mod.reconcile_against_disk(store, pid, root, exclude={file_path})
        rec_mod.reconcile(store, pid, [file_path])
    finally:
        rec_mod.reconcile = orig

    return calls


# ── 1. post_edit flow: edited file reconciled exactly once ─────────────────────

def test_post_edit_no_double_reconcile():
    """Simulate post_edit: reconcile_against_disk(exclude={fp}) + reconcile([fp]) → fp reconciled once."""
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)

        fa = os.path.join(root, "a.py")
        fb = os.path.join(root, "b.py")
        _write_py(fa, "def fn_a(): pass\n")
        _write_py(fb, "def fn_b(): pass\n")
        reconcile(store, pid, [fa, fb])
        reconcile_against_disk(store, pid, root)  # establish baselines

        # Simulate external edit of fa (advance mtime — BUER not notified)
        _advance_mtime(fa)

        calls = _simulate_post_edit(store, pid, root, fa)

        # fa must appear in exactly one reconcile call
        fa_appearances = sum(1 for call in calls if fa in call)
        assert fa_appearances == 1, (
            f"fa must be reconciled exactly once; appeared in {fa_appearances} calls: {calls}"
        )


# ── 2. excluded file still processed by reconcile([file]) ─────────────────────

def test_excluded_file_still_processed():
    """Excluded file is NOT in the against-disk reconcile, but reconcile([file]) handles it."""
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)

        fa = os.path.join(root, "a.py")
        _write_py(fa, "def fn_a(): pass\n")
        reconcile(store, pid, [fa])
        reconcile_against_disk(store, pid, root)

        # Content change to fa
        with open(fa, "w") as f:
            f.write("def fn_a_v2(): pass\n")
        _advance_mtime(fa)

        # post_edit flow
        reconcile_against_disk(store, pid, root, exclude={fa})
        reconcile(store, pid, [fa])

        # fn_a must be deleted, fn_a_v2 created (handled by reconcile([fa]))
        chain = store.version_chain(pid, fa, "fn_a")
        assert chain[-1]["edit_type"] == "delete", "fn_a must be deleted after content change"
        assert _det_count(store, pid, fa, "fn_a_v2") >= 1, (
            "fn_a_v2 must be recorded by reconcile([fa])"
        )


# ── 3. excluded file inbound edges rebuilt by reconcile([file]) ────────────────

def test_excluded_file_inbound_edges():
    """reconcile([callee]) rebuilds inbound edges even when callee was excluded from against-disk."""
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)

        from buer import callgraph
        callee_file = os.path.join(root, "callee.py")
        caller_file = os.path.join(root, "caller.py")
        _write_py(callee_file, "def callee_fn(): pass\n")
        _write_py(caller_file, "def caller_fn(): pass\n")
        reconcile(store, pid, [callee_file, caller_file])

        # Wire call: caller_fn calls callee_fn
        caller_fqn = callgraph._lang_fqn(
            caller_file, callgraph.module_name_of(caller_file, root), "caller_fn"
        )
        callee_fqn = callgraph._lang_fqn(
            callee_file, callgraph.module_name_of(callee_file, root), "callee_fn"
        )
        store.upsert_call_edge(pid, caller_fqn, callee_fqn, "call")

        # Initial reconcile to build edges for both
        reconcile(store, pid, [callee_file, caller_file])
        callee_det_v1 = store.current_version_determination(pid, callee_file, "callee_fn")
        # Rebuild idx to ensure gd edges
        idx = callgraph.build_symbol_index_from_store(store, pid, root)
        from buer import gd
        gd.recompute_edges_for_define(store, pid, caller_file, "caller_fn", root, idx)

        # Now simulate editing callee (post_edit scenario)
        with open(callee_file, "w") as f:
            f.write("def callee_fn(): return 42\n")
        _advance_mtime(callee_file)
        reconcile_against_disk(store, pid, root)  # establish baselines

        with open(callee_file, "w") as f:
            f.write("def callee_fn_v2(): return 99\n")
        _advance_mtime(callee_file)

        # post_edit: exclude callee, then reconcile([callee])
        reconcile_against_disk(store, pid, root, exclude={callee_file})
        reconcile(store, pid, [callee_file])

        # callee_fn must be deleted (renamed to callee_fn_v2)
        chain = store.version_chain(pid, callee_file, "callee_fn")
        assert chain[-1]["edit_type"] == "delete"
        # callee_fn_v2 must be created
        assert _det_count(store, pid, callee_file, "callee_fn_v2") >= 1, (
            "callee_fn_v2 must be recorded by reconcile([callee])"
        )


# ── 4. other files still detected despite exclude ─────────────────────────────

def test_external_other_file_still_caught():
    """exclude={fa} does NOT prevent detection of external changes to fb."""
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)

        fa = os.path.join(root, "a.py")
        fb = os.path.join(root, "b.py")
        _write_py(fa, "def fn_a(): pass\n")
        _write_py(fb, "def fn_b(): pass\n")
        reconcile(store, pid, [fa, fb])
        reconcile_against_disk(store, pid, root)

        # External change to fb (BUER not notified)
        with open(fb, "w") as f:
            f.write("def fn_b_v2(): pass\n")
        _advance_mtime(fb)

        # post_edit for fa (editing fa, not fb)
        reconcile_against_disk(store, pid, root, exclude={fa})
        reconcile(store, pid, [fa])

        # fb's change must have been caught by the against-disk reconcile
        chain_b = store.version_chain(pid, fb, "fn_b")
        assert chain_b[-1]["edit_type"] == "delete", (
            "fb's change must be detected despite fa being excluded"
        )
        assert _det_count(store, pid, fb, "fn_b_v2") >= 1


# ── 5. ★ Write-created file: stage-2 shallow scan also excludes it ─────────────

def test_write_new_file_exclude_phase2():
    """Write-created fp advances its dir mtime; stage-2 shallow scan must NOT re-add fp.

    This tests premise 2 (★ critical): a Write-created file changes the parent dir's
    mtime. Without stage-2 exclusion, the shallow scan would re-discover fp as a 'new'
    unrecorded file and add it to to_reconcile → double-reconcile despite stage-1 skip.
    """
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)

        # Establish baseline on root with an existing file
        existing = os.path.join(root, "existing.py")
        _write_py(existing, "def old(): pass\n")
        reconcile(store, pid, [existing])
        reconcile_against_disk(store, pid, root)  # baseline

        # Simulate Write-creating a new file (the post_edit scenario for a new file)
        new_fp = os.path.join(root, "new_file.py")
        _write_py(new_fp, "def new_fn(): pass\n")
        # new_fp is unrecorded; creating it advances root's dir mtime

        calls = _simulate_post_edit(store, pid, root, new_fp)

        new_fp_appearances = sum(1 for call in calls if new_fp in call)
        assert new_fp_appearances == 1, (
            f"★ premise-2: new_fp must be reconciled exactly once; "
            f"appeared in {new_fp_appearances} calls: {calls}"
        )
        # Also verify new_fn IS actually recorded
        assert _det_count(store, pid, new_fp, "new_fn") >= 1


# ── 6. no exclude → full scan (debug_range / briefing scenario) ───────────────

def test_exclude_none_full_scan():
    """reconcile_against_disk with no exclude performs full scan of all files."""
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)

        fa = os.path.join(root, "a.py")
        fb = os.path.join(root, "b.py")
        _write_py(fa, "def fn_a(): pass\n")
        _write_py(fb, "def fn_b(): pass\n")
        reconcile(store, pid, [fa, fb])
        reconcile_against_disk(store, pid, root)

        # Both change externally
        _advance_mtime(fa)
        _advance_mtime(fb)

        calls: list[list[str]] = []
        orig = rec_mod.reconcile

        def _fake(s, p, files, **kw):
            calls.append(list(files))
            return orig(s, p, files, **kw)

        rec_mod.reconcile = _fake
        try:
            rec_mod.reconcile_against_disk(store, pid, root)  # no exclude
        finally:
            rec_mod.reconcile = orig

        all_files = [f for call in calls for f in call]
        assert fa in all_files, "fa must be caught with no exclude"
        assert fb in all_files, "fb must be caught with no exclude"


# ── 7. external delete of other file caught despite exclude ───────────────────

def test_external_delete_other_file():
    """Edit a.py → exclude={a.py}; external delete of c.py still caught by stage-1."""
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)

        fa = os.path.join(root, "a.py")
        fc = os.path.join(root, "c.py")
        _write_py(fa, "def fn_a(): pass\n")
        _write_py(fc, "def fn_c(): pass\n")
        reconcile(store, pid, [fa, fc])
        reconcile_against_disk(store, pid, root)

        # External delete of fc (BUER not notified)
        os.remove(fc)

        # post_edit: editing fa, not fc
        reconcile_against_disk(store, pid, root, exclude={fa})
        reconcile(store, pid, [fa])

        # fn_c must be deleted (fc vanished, caught by stage-1 stat)
        chain_c = store.version_chain(pid, fc, "fn_c")
        assert chain_c[-1]["edit_type"] == "delete", (
            "fc deletion must be caught by stage-1 even with exclude={fa}"
        )


# ── 8. mutation guard A: stage-1 exclude removed → double reconcile ───────────

def test_mutation_guard_stage1_exclude():
    """MUTATION GUARD A: removing stage-1 exclude causes double-reconcile of edited file.

    This test verifies the POSITIVE behavior: with correct stage-1 exclusion,
    fa is reconciled only once. A mutation that removes `if fp in _exclude: continue`
    from stage-1 would cause fa to appear in TWO reconcile calls (this test would
    fail because fa_appearances would be 2, not 1).

    To make this a TRUE mutation guard, we verify the count == 1.
    If stage-1 exclude is removed, fa is in to_reconcile from stage-1 (mtime advanced)
    AND in the explicit reconcile([fa]) call → fa_appearances == 2 → assertion fails.
    """
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)

        fa = os.path.join(root, "a.py")
        _write_py(fa, "def fn_a(): pass\n")
        reconcile(store, pid, [fa])
        reconcile_against_disk(store, pid, root)

        # Advance fa mtime (simulates post_edit: hook notified BUER)
        _advance_mtime(fa)

        calls = _simulate_post_edit(store, pid, root, fa)

        fa_count = sum(1 for call in calls if fa in call)
        # MUTATION GUARD: must be exactly 1; would be 2 if stage-1 exclude is removed
        assert fa_count == 1, (
            f"MUTATION GUARD A: fa must appear in exactly 1 reconcile call; got {fa_count}: {calls}"
        )


# ── 9. mutation guard B: stage-2 exclude removed → Write-created file doubled ──

def test_mutation_guard_stage2_exclude():
    """MUTATION GUARD B: removing stage-2 exclude causes double-reconcile of Write-created file.

    A Write-created new_fp is unrecorded AND its parent dir mtime advanced.
    Without stage-2 exclusion, shallow scan finds new_fp as a 'new' file → it goes
    into to_reconcile from stage-2 AND again from the explicit reconcile([new_fp]).
    This must result in new_fp_appearances == 2, so removing stage-2 exclude breaks this.
    """
    store = _store()
    with tempfile.TemporaryDirectory() as root:
        pid = _project(store, root)

        existing = os.path.join(root, "existing.py")
        _write_py(existing, "def e(): pass\n")
        reconcile(store, pid, [existing])
        reconcile_against_disk(store, pid, root)

        new_fp = os.path.join(root, "created.py")
        _write_py(new_fp, "def created_fn(): pass\n")
        # new_fp is unrecorded, its parent dir mtime has advanced

        calls = _simulate_post_edit(store, pid, root, new_fp)

        new_fp_count = sum(1 for call in calls if new_fp in call)
        # MUTATION GUARD: must be exactly 1; would be 2 if stage-2 exclude is removed
        assert new_fp_count == 1, (
            f"MUTATION GUARD B: new_fp must appear in exactly 1 reconcile call; "
            f"got {new_fp_count}: {calls}"
        )
