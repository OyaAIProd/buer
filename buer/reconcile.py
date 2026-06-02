"""BUER reconcile layer — §4.6: wire parse/callgraph/gd into one update pipeline.

R2 red line: reconcile derives everything from changed_files + project state.
Agent NEVER passes edit_type / reasoning / predecessor_ids.

Public API
----------
reconcile(store, project_id, changed_files) -> ReconcileResult
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from buer import assists, boundary, callgraph, gd, health, parse, signals, testscan
from buer.store import Store

# Source file extensions recognised by BUER's extractors (mirrors _SRC_PATTERNS).
_SOURCE_EXTENSIONS: frozenset[str] = frozenset({
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs"
})


def _is_source(name: str) -> bool:
    """True if the filename has a source extension BUER can parse."""
    _, ext = os.path.splitext(name)
    return ext in _SOURCE_EXTENSIONS


@dataclass
class ReconcileResult:
    affected: list = field(default_factory=list)    # [(file_path, define_name, det_id), ...]
    boundary_violations: list = field(default_factory=list)  # reserved for future signals
    deleted: list = field(default_factory=list)     # define_names that vanished from each file


def _within_boundary(
    file_real: str,
    roots_real: list[str],
    whitelist_real: list[str],
) -> bool:
    """True if file_real is under any project root or any whitelist entry (§2.4).

    All paths must be pre-resolved by the caller (os.path.realpath).
    Accepts exact root match and directory-prefix match.  Prevents the
    /proj-backup false-positive that plain abspath + startswith causes.
    """
    for r in roots_real + whitelist_real:
        if file_real == r or file_real.startswith(r + os.sep):
            return True
    return False


def _warn_parse_skipped(store: Store, project_id: int, file_path: str, error_type: str) -> None:
    """Record that buer cannot parse file_path (monitoring blind spot).

    Idempotent: at most one open incident per file.
    Escalated directly to user channel (system advisory, not code quality signal).
    """
    existing = [
        i for i in store.open_incidents(project_id)
        if i["signal"] == "parse_skipped" and i["target_node"] == file_path
    ]
    if existing:
        return
    msg = (
        f"BUER 无法解析 {os.path.basename(file_path)}（原因：{error_type}）。"
        "该文件已跳过，BUER 暂时无法监测它的结构变化（监测盲洞）。"
        "常见原因：语法错误、超深表达式（>1000 层嵌套）、编码异常。"
    )
    inc_id = store.write_incident(
        project_id,
        "parse_skipped",
        target_node=file_path,
        details=json.dumps({"error_type": error_type, "message": msg}),
    )
    store.update_incident(inc_id, state="escalated_user")


def reconcile(
    store: Store,
    project_id: int,
    changed_files: list[str],
    exclude_tests: bool = True,
) -> ReconcileResult:
    """Main update pipeline for a set of changed source files.

    Steps (§4.6):
      Phase 1 per file —
        1. Extract current defines (parse)
        2. Compare with recorded fingerprints → detect changed/new/deleted
        3. Insert determination for each changed/new define
        4. Update equivalence class with fingerprint
        5. Rebuild call_edges (delete-then-insert)
      Phase 2 per new determination —
        6. Build GD edges (version_chain + cross_define)

    exclude_tests=True (default) skips test/spec files so the graph stays free of
    test-fixture noise (createChain/makeRequest/… false ambiguities).
    Pass exclude_tests=False when monitoring test files is explicitly needed.
    """
    project = store.get_project(project_id)
    root = project["root_path"]

    # Boundary check parameters (§2.4 misreport controls).
    # realpath resolves symlinks so /symlink-to-project/f.py is correctly in-bounds.
    # roots_real: default single root; extend to list for monorepo multi-root (§2.4 待按项目配置).
    # whitelist_real: empty default; add allowed external dirs e.g. build output (§2.4 待按项目配置).
    roots_real: list[str] = [os.path.realpath(root)]
    whitelist_real: list[str] = []

    result = ReconcileResult()
    new_dets: list = []   # det rows collected for Phase 2
    files_parsed: list[str] = []  # files that completed Phase 1 (need call_edges rebuild)

    # ── Phase 1 ───────────────────────────────────────────────────────────────
    # Steps 1–4 only: write determinations + equivalence class.
    # call_edges (step 5) deferred to Phase 1.5 so idx is fresh from the store.
    for file_path in changed_files:
        # Boundary check (§2.4): realpath resolves symlinks; _within_boundary checks
        # all roots + whitelist so monorepo sub-projects and build output don't false-trip.
        file_real = os.path.realpath(file_path)
        if not _within_boundary(file_real, roots_real, whitelist_real):
            result.boundary_violations.append(file_path)
            continue
        if not boundary.should_ingest(file_real, roots_real[0]):
            result.boundary_violations.append(file_path)
            continue

        if exclude_tests and parse.is_test_file(file_path):
            continue

        lang = parse.detect_language(file_path)

        # Files with no extractor (e.g. unknown extension) are silently skipped.
        if lang not in parse.EXTRACTORS:
            continue

        # Step 1: current defines — distinguish missing file from parse failure.
        if not os.path.exists(file_path):
            # Whole-file deletion: record each define as deleted (SDT §2.2.5 demise trace)
            # and clear stale call_edges and gd_edges.
            recorded_all = store.recorded_defines_for_file(project_id, file_path)
            mod_del = callgraph.module_name_of(file_path, root)
            for def_name in recorded_all:
                # Clear ALL historical det edges for this define before recording demise.
                # A define may have edges on multiple historical dets (create, modify, …).
                # Targeting only the latest create/modify det (recorded_all[def_name][2])
                # misses edges from earlier versions. After insert_determination_atomic the
                # "current" det becomes the delete record (no edges), so we must clear
                # historical edges first.
                for hist_det in store.all_determinations_for_define(
                    project_id, file_path, def_name
                ):
                    store.delete_gd_edges_for_det(project_id, hist_det["id"])
                store.delete_equivalence_member(project_id, f"{mod_del}.{def_name}")
                store.insert_determination_atomic(
                    project_id,
                    file_path=file_path,
                    define_name=def_name,
                    node_fingerprint=None,
                    edit_type="delete",
                )
                result.deleted.append((file_path, def_name))
            store.delete_call_edges_for_file(project_id, file_path)
            store.delete_reexports_for_module(project_id, mod_del)
            continue

        try:
            current_defines = parse.extract_defines(file_path)
        except Exception as e:
            _warn_parse_skipped(store, project_id, file_path, type(e).__name__)
            continue  # file exists but parse failed — recorded as parse_skipped incident

        # Resolve any prior parse_skipped incident for this file (blind spot cleared)
        store.resolve_parse_skipped_incident(project_id, file_path)

        # Step 2: recorded state for this file
        recorded = store.recorded_defines_for_file(project_id, file_path)
        current_names = {d.qualified_name for d in current_defines}
        mod = callgraph.module_name_of(file_path, root)

        # Deleted defines: record deletion (SDT §2.2.5 demise trace) and remove membership.
        for def_name in recorded:
            if def_name not in current_names:
                # Clear ALL historical det edges before recording demise (same symmetry
                # as whole-file path: a define may carry edges across multiple historical
                # dets; after insert_determination_atomic the current det is the delete
                # record with no edges, so historical cleanup must happen first).
                for hist_det in store.all_determinations_for_define(
                    project_id, file_path, def_name
                ):
                    store.delete_gd_edges_for_det(project_id, hist_det["id"])
                store.delete_equivalence_member(project_id, f"{mod}.{def_name}")
                store.insert_determination_atomic(
                    project_id,
                    file_path=file_path,
                    define_name=def_name,
                    node_fingerprint=None,
                    edit_type="delete",
                )
                result.deleted.append((file_path, def_name))

        # Step 3 + 4: new or changed defines
        for define in current_defines:
            coarse, fine = parse.compute_fingerprint(define)
            rec_coarse, rec_fine, old_det_id = recorded.get(
                define.qualified_name, (None, None, None)
            )

            if coarse == rec_coarse and fine == rec_fine:
                continue  # unchanged — no new determination

            # edit_type auto-derived (§4.6): "create" if never recorded, "modify" if changed
            edit_type = "create" if define.qualified_name not in recorded else "modify"

            try:
                _file_mtime: float | None = os.stat(file_path).st_mtime
            except OSError:
                _file_mtime = None

            det_id, seq = store.insert_determination_atomic(
                project_id,
                file_path=file_path,
                define_name=define.qualified_name,
                node_fingerprint=coarse,
                edit_type=edit_type,
                return_type=define.return_type,
                fine_fingerprint=fine,
                file_mtime=_file_mtime,
            )

            # Equivalence class: uses fine fingerprint so receiver-distinguished defines
            # (e.g. self.parser.foo vs self.validator.foo) land in separate classes.
            # Delete-then-insert clears prior membership on modify (§2.4.4).
            fqn = f"{mod}.{define.qualified_name}"
            store.delete_equivalence_member(project_id, fqn)
            store.update_equivalence_class(project_id, fine, fqn)

            det = store.get_determination(det_id)
            result.affected.append((file_path, define.qualified_name, det_id))
            new_dets.append((det, old_det_id))

        files_parsed.append(file_path)

    # ── Phase 1.5: project SymbolIndex from store frontier ────────────────────
    # Re-export index must be written BEFORE building idx so that idx.reexport
    # is populated when call_edges are resolved (barrel penetration depends on it).
    for file_path in files_parsed:
        mod = callgraph.module_name_of(file_path, root)
        store.delete_reexports_for_module(project_id, mod)
        for exported, tmod, tname in parse.extract_reexports(file_path, root):
            store.upsert_reexport(project_id, mod, exported, tmod, tname)

    # Build idx AFTER reexports are in DB; idx.reexport is now populated.
    # ~3ms from the store vs ~78s rglob scan — incremental-safe for single-file edits.
    idx = callgraph.build_symbol_index_from_store(store, project_id, root)

    # Step 5: rebuild call_edges using idx that includes re-export redirects.
    for file_path in files_parsed:
        callgraph.build_call_edges(project_id, file_path, root, idx, store)

    # ── Phase 2 ───────────────────────────────────────────────────────────────
    # Unified recompute: delete old + rebuild out+in edges symmetrically.
    # recompute_edges_for_define handles both outbound (det is consumer) and
    # inbound (det is producer) edges via callers_of — fixes P0 where modifying
    # a callee silently dropped inbound edges because build_gd_edges only rebuilt
    # outbound.  old_det_id passed for modify (delete prior det's edges); None for create.
    try:
        store.begin_deferred()
        for det, old_det_id in new_dets:
            gd.recompute_edges_for_define(
                store, project_id,
                det["file_path"], det["define_name"],
                root, idx,
                old_det_id=old_det_id,
            )
        store.commit_deferred()
    except Exception:
        store.rollback_deferred()
        for fp in files_parsed:
            store.enqueue_recompute(project_id, fp)

    # ── §4.5 test-artifact ingestion ──────────────────────────────────────────
    testscan.scan_test_results(store, project_id, root)

    # ── Signals + state machine ───────────────────────────────────────────────
    # debug_loop before stuck_region: upgrade relationship (§2.2) —
    # debug_loop claims defines with persistent test failures so stuck_region
    # skips them and both signals never fire on the same define simultaneously.
    signals.detect_debug_loop(store, project_id, result.affected, root, idx)
    signals.detect_stuck_region(store, project_id, result.affected, root, idx)
    # token_waste: upgrade layer above stuck/debug — reads their incidents.
    # Runs after stuck_region so newly-written stuck incidents are visible.
    signals.detect_token_waste(store, project_id, result.affected)
    signals.detect_define_loop(store, project_id, result.affected, root, idx)
    signals.detect_boundary_breach(store, project_id, result.boundary_violations)
    # task_scope_breach: same family as boundary_breach but opt-in (§2.3).
    # Only active when set_task_scope has been called; no-op otherwise.
    signals.detect_task_scope_breach(store, project_id, result.affected, root)
    # regression after scan_test_results: test data must be ingested first (§2.6)
    signals.detect_regression(store, project_id, result.affected, root, idx)
    # test_tampering after regression: same data, opposite direction (§2.7)
    signals.detect_test_tampering(store, project_id, result.affected, root, idx)
    # dangling_reference is on-demand via analyze_dangling MCP tool (not auto-run here).
    signals.advance_incidents(
        store, project_id, result.affected,
        boundary_violations=result.boundary_violations,
    )

    # §2.9 safety-net check (periodic, every HEALTH_CHECK_PERIOD edits)
    health.maybe_run_safety_net(store, project_id, root)

    # §4.11 inline-assist arbitration (commit timing + blast radius).
    # Separate channel from signal alerts and health hints — 无 SDT 对位、纯工程辅助.
    assists.run_inline_assists(store, project_id, result.affected, root)

    return result


def rebuild_call_edges_full(store: Store, project_id: int) -> int:
    """Rebuild call_edges for every ingested file using the current (complete) reexport table.

    Batch reconcile populates reexport_edges incrementally: early batches build
    call_edges with an incomplete reexport index and miss barrel-penetrated edges.
    Call this once after a full ingest loop to repair those misses.

    Returns the number of files processed.
    """
    project = store.get_project(project_id)
    root = project["root_path"]

    rows = store.con.execute(
        "SELECT DISTINCT file_path FROM determinations WHERE project_id=?",
        (project_id,),
    ).fetchall()
    files = [r[0] for r in rows if os.path.exists(r[0])]

    idx = callgraph.build_symbol_index_from_store(store, project_id, root)
    for file_path in files:
        callgraph.build_call_edges(project_id, file_path, root, idx, store)

    return len(files)


def reconcile_against_disk(
    store: Store,
    project_id: int,
    root: str,
    exclude: set[str] | None = None,
) -> int:
    """Pre-query reconciliation: stat recorded files + dir-mtime scan for new files.

    Two complementary stages — both COLLECT into to_reconcile, then reconcile ONCE:

    Stage 1 — file stat (existing records, cheap):
      - stat fails (file gone) → deletion path (Commit-1 edge cleanup)
      - mtime > recorded_mtime → content mutated (rebuild edges/dets)
      - mtime == recorded_mtime → skip (avoids reparsing)
      - recorded_mtime is None  → reconcile (establish baseline)

    Stage 2 — directory mtime (new-file detection, pure filesystem, no git):
      POSIX: adding/removing files changes the directory's mtime; modifying file
      content does NOT. So a changed dir-mtime means files were added/removed.
      For each parent directory whose mtime advanced (or was never recorded):
        shallow-scan → collect source files not yet recorded (new additions).
      Recurse into subdirectories (may be newly created).
      Deletion is NOT handled here — Stage 1 owns deletions (dir-mtime can drop
      on removal too, but the missing file is already caught by Stage 1 stat).

    exclude: paths to skip in BOTH stages. post_edit passes {file_path} here because
      reconcile([file_path]) is called immediately after and handles that file
      completely (builds in+out edges, handles new files). Excluding it prevents
      double-reconcile. The exclude is applied in Stage 2 as well because a
      Write-created file changes its parent dir's mtime; without Stage-2 exclusion
      the shallow scan would re-discover the new file and add it again.

    Both stages collect into to_reconcile, THEN reconcile is called ONCE on the
    deduplicated list so neither stage interferes with the other's recorded state.
    Dir-mtime baselines are updated after reconcile regardless of exclude (the
    directory DID change; we must record the new baseline to avoid re-scanning).

    Returns the number of unique files passed to reconcile (0 = everything up to date).
    Idempotent: consecutive calls without disk changes produce 0.
    """
    _exclude: set[str] = exclude or set()

    # ── Stage 1: file stat ────────────────────────────────────────────────────
    recorded = store.get_recorded_file_mtimes(project_id)
    to_reconcile: list[str] = []

    for file_path, rec_mtime in recorded.items():
        if file_path in _exclude:
            continue  # handled by the caller's dedicated reconcile([file_path])
        try:
            cur_mtime = os.stat(file_path).st_mtime
        except OSError:
            to_reconcile.append(file_path)  # vanished — deletion path
            continue
        if rec_mtime is None or cur_mtime > rec_mtime:
            to_reconcile.append(file_path)  # mtime advanced — reparse

    # ── Stage 2: directory mtime — new-file detection ────────────────────────
    recorded_set: set[str] = set(recorded.keys())
    recorded_dir_mtimes = store.get_dir_mtimes(project_id)

    # Seed queue from parent dirs of all already-recorded files.
    # Also include root itself so a brand-new project finds its top-level files.
    parent_dirs: set[str] = {os.path.dirname(fp) for fp in recorded_set}
    parent_dirs.add(root)

    scan_queue: list[str] = list(parent_dirs)
    seen: set[str] = set()
    dir_updates: list[tuple[str, float]] = []  # (dir_path, cur_mtime) — apply after reconcile

    while scan_queue:
        d = scan_queue.pop()
        if d in seen or not os.path.isdir(d):
            continue
        seen.add(d)

        # Exclude blacklisted dir names (node_modules, .git, __pycache__, etc.)
        if boundary.is_excluded_dir(Path(d).parts):
            continue

        try:
            cur_dm = os.stat(d).st_mtime
        except OSError:
            continue

        rec_dm = recorded_dir_mtimes.get(d)
        if rec_dm is not None and cur_dm <= rec_dm:
            continue  # dir unchanged — skip shallow scan

        # Dir mtime advanced (or no baseline yet) — shallow scan for new source files.
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue

        for e in entries:
            if e.is_file(follow_symlinks=False):
                if (
                    _is_source(e.name)
                    and not parse.is_test_file(e.path)
                    and e.path not in recorded_set
                    and e.path not in _exclude  # ★ stage-2 exclusion (Write-created file)
                ):
                    to_reconcile.append(e.path)  # newly discovered source file
            elif e.is_dir(follow_symlinks=False):
                if not boundary.is_excluded_dir(Path(e.path).parts):
                    scan_queue.append(e.path)  # recurse into subdirs (may be new)

        dir_updates.append((d, cur_dm))

    # ── Stage 3: unified reconcile (both stages done, then one call) ─────────
    unique = list(dict.fromkeys(to_reconcile))  # deduplicate, preserve first-seen order
    if unique:
        reconcile(store, project_id, unique)

    # Update dir-mtime baselines AFTER reconcile — always, regardless of exclude.
    # The directory DID change; we must record the new mtime to avoid re-scanning
    # on the next call, even if all discovered files were excluded.
    for d, m in dir_updates:
        store.set_dir_mtime(project_id, d, m)

    return len(unique)
