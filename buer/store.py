"""BUER data layer: SQLite store.

Pure data primitives only — connection, schema init, and the read/write
operations reconcile (§4.6) needs. No business logic (signal detection,
reconcile flow live in upper layers).

Design ref: BUER_Design_v2.0.md §4.1 / §4.2 / §4.6.

Anti-drift note (case-study R2): every write primitive takes only inputs
that reconcile auto-derives from files/git/tests (§1.5). No primitive
requires the agent to declare anything (contrast the old record_action's
determination_type / reasoning / predecessor_action_ids).
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Optional

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


class Store:
    """SQLite-backed BUER store. One Store per .buer/store.sqlite."""

    def __init__(self, db_path: str = ".buer/store.sqlite"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.con = sqlite3.connect(db_path, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA foreign_keys = ON")
        self.con.execute("PRAGMA journal_mode = WAL")
        self.con.execute("PRAGMA busy_timeout = 5000")
        self._defer_commit = False
        self._init_schema()

    def _init_schema(self) -> None:
        with open(_SCHEMA_PATH) as f:
            self.con.executescript(f.read())
        # Idempotent migrations for columns added after initial schema deployment.
        for stmt in (
            "ALTER TABLE test_runs ADD COLUMN source TEXT DEFAULT 'junit_xml'",
            "ALTER TABLE test_runs ADD COLUMN inserted_at TIMESTAMP DEFAULT (datetime('now'))",
            "ALTER TABLE crash_stacks ADD COLUMN error_signature TEXT",
            "ALTER TABLE projects ADD COLUMN no_define_count INTEGER DEFAULT 0",
            "ALTER TABLE determinations ADD COLUMN return_type TEXT DEFAULT ''",
            "ALTER TABLE projects ADD COLUMN branch TEXT",
            "ALTER TABLE projects ADD COLUMN created_at_commit TEXT",
            "ALTER TABLE determinations ADD COLUMN git_commit TEXT",
            "ALTER TABLE determinations ADD COLUMN fine_fingerprint TEXT",
            "ALTER TABLE projects ADD COLUMN notification_level TEXT DEFAULT 'medium'",
            "ALTER TABLE determinations ADD COLUMN file_mtime REAL",
            "ALTER TABLE call_edges ADD COLUMN source_file TEXT",
            "ALTER TABLE determinations ADD COLUMN start_line INTEGER DEFAULT 0",
            "ALTER TABLE determinations ADD COLUMN end_line INTEGER DEFAULT 0",
            "ALTER TABLE pending_deliveries ADD COLUMN kind TEXT NOT NULL DEFAULT 'alert' CHECK(kind IN ('alert', 'suggestion'))",
        ):
            try:
                self.con.execute(stmt)
                self.con.commit()
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise
        self.con.execute(
            "CREATE INDEX IF NOT EXISTS idx_call_edges_source "
            "ON call_edges(project_id, source_file)"
        )
        self.con.commit()
        # Unique index for (root_path, branch) — COALESCE treats NULL as ''
        self.con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_root_branch "
            "ON projects(root_path, COALESCE(branch, ''))"
        )
        # incidents CHECK migration: add parse_skipped signal (SQLite can't ALTER CHECK)
        row = self.con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='incidents'"
        ).fetchone()
        if row and "parse_skipped" not in (row["sql"] or ""):
            self.con.executescript("""
                PRAGMA foreign_keys = OFF;
                BEGIN;
                ALTER TABLE incidents RENAME TO incidents_old;
                CREATE TABLE incidents (
                    id                INTEGER PRIMARY KEY,
                    project_id        INTEGER REFERENCES projects(id),
                    signal            TEXT NOT NULL CHECK(signal IN (
                                          'define_loop', 'stuck_region', 'debug_loop',
                                          'regression', 'boundary_breach', 'task_scope_breach',
                                          'test_tampering', 'token_waste', 'dangling_reference',
                                          'ts_toolchain_missing', 'parse_skipped')),
                    target_node       TEXT,
                    state             TEXT DEFAULT 'open' CHECK(state IN (
                                          'open', 'notified_agent', 'resolved', 'escalated_user')),
                    agent_notified_at TIMESTAMP,
                    post_notify_count INTEGER DEFAULT 0,
                    escalated_at      TIMESTAMP,
                    resolved_by       TEXT,
                    details           TEXT,
                    created_at TIMESTAMP, updated_at TIMESTAMP
                );
                INSERT INTO incidents SELECT * FROM incidents_old
                    WHERE signal IN (
                        'define_loop', 'stuck_region', 'debug_loop',
                        'regression', 'boundary_breach', 'task_scope_breach',
                        'test_tampering', 'token_waste', 'dangling_reference',
                        'ts_toolchain_missing', 'parse_skipped');
                DROP TABLE incidents_old;
                COMMIT;
                PRAGMA foreign_keys = ON;
            """)
        # call_edges lang-prefix migration: old DBs store bare 'mod.fn' fqns;
        # new code uses 'lang::mod.fn'. Clear both tables so reconcile rebuilds
        # them correctly on the next ingest (call_edges + gd_edges are projections).
        row_ce = self.con.execute(
            "SELECT caller FROM call_edges LIMIT 1"
        ).fetchone()
        if row_ce and "::" not in (row_ce["caller"] or ""):
            self.con.executescript("""
                DELETE FROM call_edges;
                DELETE FROM gd_edges;
            """)
        # snapshots table (batch 2) — IF NOT EXISTS: idempotent on old DBs
        self.con.executescript("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id              INTEGER PRIMARY KEY,
                project_id      INTEGER REFERENCES projects(id),
                commit_hash     TEXT NOT NULL,
                branch          TEXT,
                snapshot_at_seq INTEGER NOT NULL,
                parent_commit   TEXT,
                reason          TEXT,
                taken_at        TIMESTAMP DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_snapshots_proj_commit
                ON snapshots(project_id, commit_hash);
            CREATE INDEX IF NOT EXISTS idx_snapshots_proj_taken
                ON snapshots(project_id, taken_at);
        """)
        self.con.commit()

    def close(self) -> None:
        self.con.close()

    # ---- deferred-transaction helpers ----

    def _commit(self) -> None:
        """Internal commit that respects deferred-transaction mode.
        Write methods call _commit() instead of con.commit() when they need
        to participate in an outer transaction managed by begin_deferred()."""
        if not self._defer_commit:
            self.con.commit()

    def begin_deferred(self) -> None:
        """Enter deferred-commit mode: _commit() calls become no-ops so multiple
        write operations form a single atomic transaction.
        Caller must call commit_deferred() or rollback_deferred() to finish."""
        self._defer_commit = True
        self.con.execute("BEGIN")

    def commit_deferred(self) -> None:
        self._defer_commit = False
        self.con.commit()

    def rollback_deferred(self) -> None:
        self._defer_commit = False
        self.con.rollback()

    # ---- projects ----

    def get_or_create_project(
        self,
        root_path: str,
        branch: "Optional[str]" = None,
        created_at_commit: "Optional[str]" = None,
    ) -> int:
        """Return project id for (root_path, branch), creating if absent.

        branch=None means non-git project (or git info unavailable).
        UNIQUE(root_path, COALESCE(branch, '')) ensures one project per (root, branch).
        Existing call sites without branch arg get the same behavior as before
        (branch=NULL → matches the default 'no-branch' project for that root).
        """
        row = self.con.execute(
            "SELECT id FROM projects "
            "WHERE root_path = ? AND COALESCE(branch, '') = COALESCE(?, '')",
            (root_path, branch),
        ).fetchone()
        if row:
            return row["id"]
        cur = self.con.execute(
            "INSERT INTO projects (root_path, branch, created_at_commit, created_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            (root_path, branch, created_at_commit),
        )
        self.con.commit()
        return cur.lastrowid

    def get_project(self, project_id: int) -> Optional[sqlite3.Row]:
        return self.con.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()

    def set_no_define_count(self, project_id: int, n: int) -> None:
        """Store the count of boundary files that yielded no defines (coverage denominator)."""
        self.con.execute(
            "UPDATE projects SET no_define_count=? WHERE id=?", (n, project_id)
        )
        self._commit()

    def get_no_define_count(self, project_id: int) -> int:
        """Return stored no-define file count (defaults to 0 for new/pre-migration projects)."""
        row = self.con.execute(
            "SELECT no_define_count FROM projects WHERE id=?", (project_id,)
        ).fetchone()
        return row["no_define_count"] if row and row["no_define_count"] is not None else 0

    def get_notification_level(self, project_id: int) -> str:
        """Return notification level for project (default 'medium' = current behaviour)."""
        row = self.con.execute(
            "SELECT notification_level FROM projects WHERE id=?", (project_id,)
        ).fetchone()
        val = row["notification_level"] if row else None
        return val if val else "medium"

    def set_notification_level(self, project_id: int, level: str) -> None:
        """Persist notification level. Raises ValueError for unknown levels."""
        from buer.sensitivity import NOTIFICATION_LEVELS
        if level not in NOTIFICATION_LEVELS:
            raise ValueError(
                f"Invalid notification level {level!r}. "
                f"Must be one of: {', '.join(sorted(NOTIFICATION_LEVELS))}"
            )
        self.con.execute(
            "UPDATE projects SET notification_level=? WHERE id=?", (level, project_id)
        )
        self._commit()

    # ---- determinations (§4.6: next_seq, insert_determination) ----

    def next_seq(self, project_id: int) -> int:
        """Next monotonic record number. This is a total order — a linear extension of the
        ≺ partial order (§2.3.11). The actual ≺ relation lives in gd_edges (a directed path
        D_i→D_j), queried via gd_is_ancestor; seq only provides append ordering + version numbering."""
        row = self.con.execute(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM determinations WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        return row["m"] + 1

    def insert_determination(
        self,
        project_id: int,
        seq: int,
        file_path: str,
        define_name: Optional[str],
        node_fingerprint: Optional[str],
        edit_type: Optional[str],
        return_type: str = "",
        fine_fingerprint: Optional[str] = None,
        start_line: int = 0,
        end_line: int = 0,
    ) -> int:
        """Insert one determination (one agent edit = one node). All args
        auto-derived from the changed file; none declared by the agent."""
        cur = self.con.execute(
            """INSERT INTO determinations
               (project_id, seq, file_path, define_name, node_fingerprint, return_type,
                edit_type, fine_fingerprint, start_line, end_line, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
            (project_id, seq, file_path, define_name, node_fingerprint, return_type,
             edit_type, fine_fingerprint, start_line, end_line),
        )
        self.con.commit()
        return cur.lastrowid

    def insert_determination_atomic(
        self,
        project_id: int,
        file_path: str,
        define_name: Optional[str],
        node_fingerprint: Optional[str],
        edit_type: Optional[str],
        return_type: str = "",
        fine_fingerprint: Optional[str] = None,
        file_mtime: Optional[float] = None,
        start_line: int = 0,
        end_line: int = 0,
    ) -> tuple[int, int]:
        """Atomically allocate next seq and insert the determination in one
        IMMEDIATE transaction, preventing the next_seq race between concurrent
        reconcile paths (post_edit main thread vs background recompute thread).
        Returns (det_id, seq).  node_fingerprint=coarse, fine_fingerprint=fine.
        file_mtime: st_mtime of the file at insert time; used by reconcile_against_disk
        to detect external mutations without re-parsing unchanged files.
        """
        cur = self.con.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            row = cur.execute(
                "SELECT COALESCE(MAX(seq), 0) AS m FROM determinations WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            seq = row["m"] + 1
            ins = cur.execute(
                """INSERT INTO determinations
                   (project_id, seq, file_path, define_name, node_fingerprint, return_type,
                    edit_type, fine_fingerprint, file_mtime, start_line, end_line, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                (project_id, seq, file_path, define_name, node_fingerprint, return_type,
                 edit_type, fine_fingerprint, file_mtime, start_line, end_line),
            )
            det_id = ins.lastrowid
            self.con.commit()
            return det_id, seq
        except Exception:
            self.con.rollback()
            raise

    def version_chain(self, project_id: int, file_path: str, define_name: str) -> list[sqlite3.Row]:
        """All determinations for one define, ordered by seq (the version chain)."""
        return self.con.execute(
            """SELECT * FROM determinations
               WHERE project_id = ? AND file_path = ? AND define_name = ?
               ORDER BY seq""",
            (project_id, file_path, define_name),
        ).fetchall()

    def previous_determination(
        self, project_id: int, file_path: str, define_name: str
    ) -> Optional[sqlite3.Row]:
        """Most recent prior determination for this define (for version_chain edge)."""
        return self.con.execute(
            """SELECT * FROM determinations
               WHERE project_id = ? AND file_path = ? AND define_name = ?
               ORDER BY seq DESC LIMIT 1""",
            (project_id, file_path, define_name),
        ).fetchone()

    def current_version_determination(
        self, project_id: int, file_path: str, define_name: str
    ) -> Optional[sqlite3.Row]:
        """Latest determination for a define — current producer version.

        Used by build_gd_edges to identify the cross-define producer node.
        Same query as previous_determination; explicit name for call-graph use.
        """
        return self.con.execute(
            """SELECT * FROM determinations
               WHERE project_id = ? AND file_path = ? AND define_name = ?
               ORDER BY seq DESC LIMIT 1""",
            (project_id, file_path, define_name),
        ).fetchone()

    def get_determination(self, det_id: int) -> Optional[sqlite3.Row]:
        """Fetch a single determination row by primary key."""
        return self.con.execute(
            "SELECT * FROM determinations WHERE id = ?", (det_id,)
        ).fetchone()

    def recorded_defines_for_file(
        self, project_id: int, file_path: str
    ) -> dict[str, tuple[str | None, str | None, int]]:
        """Latest recorded (coarse, fine) fingerprint pair + det_id for each define.

        Returns {define_name: (node_fingerprint, fine_fingerprint, det_id)} using
        the highest-seq determination per define_name.
        Used by reconcile to detect changed/deleted defines (§4.6 auto-derive).
        fine_fingerprint may be None for records written before the paired-fp migration.
        """
        rows = self.con.execute(
            """SELECT d.define_name, d.node_fingerprint, d.fine_fingerprint, d.id
               FROM determinations d
               WHERE d.project_id = ? AND d.file_path = ? AND d.define_name IS NOT NULL
                 AND d.seq = (
                   SELECT MAX(seq) FROM determinations
                   WHERE project_id = ? AND file_path = ? AND define_name = d.define_name
                 )""",
            (project_id, file_path, project_id, file_path),
        ).fetchall()
        return {r["define_name"]: (r["node_fingerprint"], r["fine_fingerprint"], r["id"])
                for r in rows}

    # ---- dir_mtimes (directory-level mtime baselines for new-file detection) ----

    def get_dir_mtimes(self, project_id: int) -> dict[str, float]:
        """Return {dir_path: mtime} for all directories tracked for this project."""
        rows = self.con.execute(
            "SELECT dir_path, mtime FROM dir_mtimes WHERE project_id = ?",
            (project_id,),
        ).fetchall()
        return {r["dir_path"]: r["mtime"] for r in rows}

    def set_dir_mtime(self, project_id: int, dir_path: str, mtime: float) -> None:
        """Upsert the recorded mtime for a directory (baseline for new-file detection)."""
        self.con.execute(
            """INSERT INTO dir_mtimes (project_id, dir_path, mtime)
               VALUES (?, ?, ?)
               ON CONFLICT(project_id, dir_path) DO UPDATE SET mtime=excluded.mtime""",
            (project_id, dir_path, mtime),
        )
        self._commit()

    def get_recorded_file_mtimes(self, project_id: int) -> dict[str, float | None]:
        """Return {file_path: file_mtime} for each recorded file, using the latest det per file.

        Used by reconcile_against_disk to compare recorded mtime against current disk mtime
        without re-parsing files. file_mtime may be None for dets recorded before this column
        was added (treated as unknown → trigger reconcile to establish baseline).
        """
        rows = self.con.execute(
            """SELECT file_path, file_mtime FROM determinations d1
               WHERE project_id = ?
                 AND seq = (
                   SELECT MAX(seq) FROM determinations d2
                   WHERE d2.project_id = ? AND d2.file_path = d1.file_path
                 )
               GROUP BY file_path""",
            (project_id, project_id),
        ).fetchall()
        return {r["file_path"]: r["file_mtime"] for r in rows}

    # ---- call_edges (§4.2a step 1) ----

    def delete_call_edges_for_module(self, project_id: int, module_prefix: str) -> None:
        """Delete all call_edges whose caller is or is under module_prefix.

        Covers both the module itself ("pkg.auth") and nested symbols
        ("pkg.auth.MyClass.method"). Called before re-inserting edges so
        stale edges from deleted/renamed defines are removed (§4.2a step 1).
        """
        self.con.execute(
            """DELETE FROM call_edges
               WHERE project_id = ?
                 AND (caller = ? OR caller LIKE ?)""",
            (project_id, module_prefix, f"{module_prefix}.%"),
        )
        self.con.commit()

    def delete_call_edges_for_file(self, project_id: int, source_file: str) -> None:
        """Delete all call_edges whose source_file matches exactly.

        Replaces delete_call_edges_for_module for the build_call_edges path;
        source_file is the absolute path of the file being rebuilt.
        """
        self.con.execute(
            "DELETE FROM call_edges WHERE project_id = ? AND source_file = ?",
            (project_id, source_file),
        )
        self.con.commit()

    def upsert_call_edge(
        self,
        project_id: int,
        caller: str,
        callee: str,
        edge_kind: str,
        source_file: str | None = None,
    ) -> None:
        self.con.execute(
            """INSERT INTO call_edges (project_id, caller, callee, edge_kind, source_file)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(project_id, caller, callee)
               DO UPDATE SET edge_kind=excluded.edge_kind, source_file=excluded.source_file""",
            (project_id, caller, callee, edge_kind, source_file),
        )
        self.con.commit()

    # ---- reexport_edges (barrel re-export穿透, §4.2a batch 1) ----

    def delete_reexports_for_module(self, project_id: int, barrel_module: str) -> None:
        self.con.execute(
            "DELETE FROM reexport_edges WHERE project_id = ? AND barrel_module = ?",
            (project_id, barrel_module),
        )
        self._commit()

    def upsert_reexport(
        self,
        project_id: int,
        barrel_module: str,
        exported_name: str,
        target_module: str,
        target_name: str,
    ) -> None:
        self.con.execute(
            """INSERT OR IGNORE INTO reexport_edges
               (project_id, barrel_module, exported_name, target_module, target_name)
               VALUES (?, ?, ?, ?, ?)""",
            (project_id, barrel_module, exported_name, target_module, target_name),
        )
        self._commit()

    def all_reexports(self, project_id: int) -> list:
        return self.con.execute(
            """SELECT barrel_module, exported_name, target_module, target_name
               FROM reexport_edges WHERE project_id = ?""",
            (project_id,),
        ).fetchall()

    def callees_of(self, project_id: int, caller: str) -> list[str]:
        rows = self.con.execute(
            "SELECT callee FROM call_edges WHERE project_id = ? AND caller = ?",
            (project_id, caller),
        ).fetchall()
        return [r["callee"] for r in rows]

    def callers_of(self, project_id: int, callee: str) -> list[str]:
        rows = self.con.execute(
            "SELECT caller FROM call_edges WHERE project_id = ? AND callee = ?",
            (project_id, callee),
        ).fetchall()
        return [r["caller"] for r in rows]

    # ---- gd_edges (§4.2a step 2; ω/Γ_R basis) ----

    def insert_gd_edge(self, project_id: int, from_det: int, to_det: int, edge_class: str) -> None:
        self.con.execute(
            """INSERT OR IGNORE INTO gd_edges (project_id, from_det, to_det, edge_class)
               VALUES (?, ?, ?, ?)""",
            (project_id, from_det, to_det, edge_class),
        )
        self._commit()

    def delete_gd_edges_for_det(self, project_id: int, det_id: int) -> None:
        """Delete all gd_edges incident to det_id (from_det or to_det).

        Called when a define is superseded by a new version. The old-version det's
        edges are stale (callers now point to the new version; the old det's
        outgoing calls are dead). Root fix for gd_edges inflation. version_chain
        edges no longer exist (removed in build_gd_edges); only cross_define remain.
        """
        self.con.execute(
            "DELETE FROM gd_edges WHERE project_id = ? AND (from_det = ? OR to_det = ?)",
            (project_id, det_id, det_id),
        )
        self._commit()

    def gd_predecessors(self, project_id: int, det_id: int) -> list[int]:
        rows = self.con.execute(
            "SELECT from_det FROM gd_edges WHERE project_id = ? AND to_det = ?",
            (project_id, det_id),
        ).fetchall()
        return [r["from_det"] for r in rows]

    def gd_successors(self, project_id: int, det_id: int) -> list[int]:
        rows = self.con.execute(
            "SELECT to_det FROM gd_edges WHERE project_id = ? AND from_det = ?",
            (project_id, det_id),
        ).fetchall()
        return [r["to_det"] for r in rows]

    def all_determinations_for_define(
        self, project_id: int, file_path: str, define_name: str
    ) -> list[sqlite3.Row]:
        """All determination rows for a define across its full history, ordered by seq.

        Used by deletion paths to delete gd_edges for every historical det, not just
        the latest create/modify det. A define may accumulate edge references across
        multiple rounds of create/modify/delete cycles.
        """
        return self.con.execute(
            """SELECT * FROM determinations
               WHERE project_id = ? AND file_path = ? AND define_name = ?
               ORDER BY seq""",
            (project_id, file_path, define_name),
        ).fetchall()

    def gd_caller_count(self, project_id: int, det_id: int) -> int:
        """Count direct callers of det_id (rows where from_det=det_id in gd_edges)."""
        row = self.con.execute(
            "SELECT COUNT(*) AS n FROM gd_edges WHERE project_id=? AND from_det=?",
            (project_id, det_id),
        ).fetchone()
        return row["n"] if row else 0

    def gd_caller_names(self, project_id: int, det_id: int, limit: int = 3) -> list[str]:
        """Return define_names of up to limit callers (to_det nodes) of det_id."""
        rows = self.con.execute(
            """SELECT d.define_name FROM gd_edges ge
               JOIN determinations d ON d.id = ge.to_det
               WHERE ge.project_id=? AND ge.from_det=?
                 AND d.define_name IS NOT NULL
               LIMIT ?""",
            (project_id, det_id, limit),
        ).fetchall()
        return [r["define_name"] for r in rows]

    def gd_strict_ancestors(self, project_id: int, det_id: int) -> frozenset[int]:
        """Full strict predecessor cone ↓v — all transitive predecessors, not including v.

        [Math Ext §11.1b: ↓v = {u | u ≺ v}]
        BFS via from_det edges. visited guards against accidental cycles (𝒢_D is a DAG
        by invariant, but defensive inclusion has no cost).
        """
        visited: set[int] = set()
        stack = list(self.gd_predecessors(project_id, det_id))
        while stack:
            cur = stack.pop()
            if cur not in visited:
                visited.add(cur)
                stack.extend(self.gd_predecessors(project_id, cur))
        return frozenset(visited)

    def gd_is_ancestor(self, project_id: int, anc_id: int, desc_id: int) -> bool:
        """True if anc_id is a strict ancestor of desc_id in 𝒢_D.

        Backward BFS via from_det edges. Pure data — no business logic.
        Used by gd.build_gd_edges as DAG cycle guard before inserting
        cross_define edges (§3.3 invariant: 𝒢_D must be acyclic).
        """
        visited: set[int] = {desc_id}
        stack = list(self.gd_predecessors(project_id, desc_id))
        while stack:
            cur = stack.pop()
            if cur == anc_id:
                return True
            if cur not in visited:
                visited.add(cur)
                stack.extend(self.gd_predecessors(project_id, cur))
        return False

    # ---- equivalence classes (§2.4.4) ----

    def delete_equivalence_member(self, project_id: int, member_node: str) -> None:
        """Remove all class_key memberships for member_node.

        Called before re-inserting on modify (fingerprint changed) and on delete
        (define removed). Prevents ghost members and stale cross-file collisions
        when two files share a same-named define.
        """
        self.con.execute(
            "DELETE FROM node_equivalence_classes WHERE project_id = ? AND member_node = ?",
            (project_id, member_node),
        )
        self.con.commit()

    def update_equivalence_class(self, project_id: int, class_key: str, member_node: str) -> None:
        self.con.execute(
            """INSERT OR IGNORE INTO node_equivalence_classes
               (project_id, class_key, member_node) VALUES (?, ?, ?)""",
            (project_id, class_key, member_node),
        )
        self.con.commit()

    def equivalence_members(self, project_id: int, class_key: str) -> list[str]:
        rows = self.con.execute(
            """SELECT member_node FROM node_equivalence_classes
               WHERE project_id = ? AND class_key = ?""",
            (project_id, class_key),
        ).fetchall()
        return [r["member_node"] for r in rows]

    # ---- incidents (§4.3 two-step state machine) ----

    def write_incident(
        self,
        project_id: int,
        signal: str,
        target_node: Optional[str] = None,
        details: Optional[str] = None,
    ) -> int:
        cur = self.con.execute(
            """INSERT INTO incidents
               (project_id, signal, target_node, state, details, created_at, updated_at)
               VALUES (?, ?, ?, 'open', ?, datetime('now'), datetime('now'))""",
            (project_id, signal, target_node, details),
        )
        self.con.commit()
        return cur.lastrowid

    def open_incidents(self, project_id: int) -> list[sqlite3.Row]:
        return self.con.execute(
            """SELECT * FROM incidents
               WHERE project_id = ? AND state IN ('open', 'notified_agent', 'escalated_user')
               ORDER BY created_at""",
            (project_id,),
        ).fetchall()

    def update_incident(self, incident_id: int, **fields: Any) -> None:
        """Update incident fields by name; always bumps updated_at."""
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values())
        self.con.execute(
            f"UPDATE incidents SET {cols}, updated_at = datetime('now') WHERE id = ?",
            (*vals, incident_id),
        )
        self.con.commit()

    def resolve_parse_skipped_incident(self, project_id: int, file_path: str) -> None:
        """Resolve any open parse_skipped incident for file_path (file now parses cleanly)."""
        rows = self.con.execute(
            """SELECT id FROM incidents
               WHERE project_id = ? AND signal = 'parse_skipped' AND target_node = ?
               AND state IN ('open', 'notified_agent', 'escalated_user')""",
            (project_id, file_path),
        ).fetchall()
        for row in rows:
            self.update_incident(row["id"], state="resolved", resolved_by="parse_ok")

    def get_incident(self, incident_id: int) -> Optional[sqlite3.Row]:
        """Fetch a single incident row by primary key."""
        return self.con.execute(
            "SELECT * FROM incidents WHERE id = ?", (incident_id,)
        ).fetchone()

    def count_incidents_for_target(
        self, project_id: int, target_node: str, signals: list[str]
    ) -> int:
        """Count all incidents (any state) for target_node matching any of the given signals.

        Counts historical incidents too (not just open) — used by token_waste to
        detect if a region has been repeatedly triggering stuck/debug (pattern vs one-off).
        """
        if not signals:
            return 0
        placeholders = ",".join("?" * len(signals))
        row = self.con.execute(
            f"""SELECT COUNT(*) AS n FROM incidents
                WHERE project_id = ? AND target_node = ? AND signal IN ({placeholders})""",
            (project_id, target_node, *signals),
        ).fetchone()
        return row["n"] if row else 0

    # ---- test_runs / test_cases / coverage_map (§4.5) ----

    def test_run_already_ingested(
        self, project_id: int, source_path: str, source_mtime: str
    ) -> bool:
        """True if this exact report file (path + mtime) has already been ingested."""
        row = self.con.execute(
            """SELECT id FROM test_runs
               WHERE project_id = ? AND source_path = ? AND source_mtime = ?""",
            (project_id, source_path, source_mtime),
        ).fetchone()
        return row is not None

    def insert_test_run(
        self,
        project_id: int,
        seq: Optional[int],
        source_path: str,
        source_mtime: str,
        passed: int,
        failed: int,
        skipped: int,
        source: str = "junit_xml",
    ) -> int:
        cur = self.con.execute(
            """INSERT INTO test_runs
               (project_id, seq, source_path, source_mtime, passed, failed, skipped, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (project_id, seq, source_path, source_mtime, passed, failed, skipped, source),
        )
        self.con.commit()
        return cur.lastrowid

    def insert_test_case(
        self,
        run_id: int,
        classname: str,
        name: str,
        file_path: Optional[str],
        status: str,
    ) -> int:
        cur = self.con.execute(
            """INSERT INTO test_cases (test_run_id, classname, name, file_path, status)
               VALUES (?, ?, ?, ?, ?)""",
            (run_id, classname, name, file_path, status),
        )
        self.con.commit()
        return cur.lastrowid

    def insert_coverage_entry(
        self, project_id: int, test_case: str, define_name: str
    ) -> None:
        """Insert one coverage_map row (classname::name → define_name). INSERT OR IGNORE."""
        self.con.execute(
            """INSERT OR IGNORE INTO coverage_map (project_id, test_case, define_name)
               VALUES (?, ?, ?)""",
            (project_id, test_case, define_name),
        )
        self.con.commit()

    # ---- test read-side (debug_loop will use these) ----

    def test_case_history(
        self, project_id: int, classname: str, name: str
    ) -> list[sqlite3.Row]:
        """All test_case rows for this classname/name across runs, ordered by run id.

        Used by debug_loop to check persistent failure across a define's edit window.
        """
        return self.con.execute(
            """SELECT tc.*, tr.seq AS run_seq, tr.source_mtime
               FROM test_cases tc
               JOIN test_runs tr ON tc.test_run_id = tr.id
               WHERE tr.project_id = ? AND tc.classname = ? AND tc.name = ?
               ORDER BY tr.id""",
            (project_id, classname, name),
        ).fetchall()

    def defines_covered_by(self, project_id: int, test_case: str) -> list[str]:
        """Precise-tier: define_names covered by test_case (classname::name) via coverage_map."""
        rows = self.con.execute(
            "SELECT define_name FROM coverage_map WHERE project_id = ? AND test_case = ?",
            (project_id, test_case),
        ).fetchall()
        return [r["define_name"] for r in rows]

    def test_cases_covering(self, project_id: int, define_name: str) -> list[str]:
        """Precise-tier: test_case identifiers (classname::name) that cover define_name."""
        rows = self.con.execute(
            "SELECT test_case FROM coverage_map WHERE project_id = ? AND define_name = ?",
            (project_id, define_name),
        ).fetchall()
        return [r["test_case"] for r in rows]

    def distinct_test_case_pairs(self, project_id: int) -> list[tuple[str, str]]:
        """All distinct (classname, name) pairs from test_cases for this project.

        Used by debug_loop heuristic tier to match define names against test names.
        """
        rows = self.con.execute(
            """SELECT DISTINCT tc.classname, tc.name
               FROM test_cases tc
               JOIN test_runs tr ON tc.test_run_id = tr.id
               WHERE tr.project_id = ?""",
            (project_id,),
        ).fetchall()
        return [(r["classname"], r["name"]) for r in rows]

    def distinct_test_case_triples(
        self, project_id: int
    ) -> list[tuple[str, str, Optional[str]]]:
        """All distinct (classname, name, file_path) from test_cases for this project.

        Like distinct_test_case_pairs but includes file_path for file-level matching.
        """
        rows = self.con.execute(
            """SELECT DISTINCT tc.classname, tc.name, tc.file_path
               FROM test_cases tc
               JOIN test_runs tr ON tc.test_run_id = tr.id
               WHERE tr.project_id = ?""",
            (project_id,),
        ).fetchall()
        return [(r["classname"], r["name"], r["file_path"]) for r in rows]

    def nearest_seq_for_mtime(self, project_id: int, mtime_iso: str) -> Optional[int]:
        """Return the seq of the determination whose created_at is closest to mtime_iso.

        Used by testscan to associate a test_run with the determination it follows.
        Returns None when no determinations exist yet.
        """
        row = self.con.execute(
            """SELECT seq FROM determinations
               WHERE project_id = ?
               ORDER BY ABS(julianday(created_at) - julianday(?))
               LIMIT 1""",
            (project_id, mtime_iso),
        ).fetchone()
        return row["seq"] if row else None

    # ---- pending_deliveries (§4.4 hook delivery queue) ----

    def enqueue_delivery(
        self,
        project_id: int,
        incident_id: Optional[int],
        channel: str,
        message: str,
        kind: str = "alert",
    ) -> int:
        """Enqueue one delivery message.  channel = 'agent'|'user'; kind = 'alert'|'suggestion'.

        incident_id may be None for safety-net deliveries (§2.9) that have
        no corresponding incident row.
        """
        cur = self.con.execute(
            """INSERT INTO pending_deliveries
               (project_id, incident_id, channel, kind, message, created_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'))""",
            (project_id, incident_id, channel, kind, message),
        )
        self.con.commit()
        return cur.lastrowid

    def _take_deliveries(
        self,
        project_id: int,
        channel: str,
        kinds: Optional[tuple] = None,
    ) -> list[sqlite3.Row]:
        """Take (mark as delivered) all untaken deliveries for channel.

        kinds: if provided, only take rows whose kind is in the tuple.
        Atomic: fetch then bulk-update taken_at in one transaction.
        Returns the rows as they were before marking.
        """
        if kinds:
            placeholders_k = ",".join("?" * len(kinds))
            rows = self.con.execute(
                f"""SELECT * FROM pending_deliveries
                   WHERE project_id = ? AND channel = ? AND kind IN ({placeholders_k})
                     AND taken_at IS NULL
                   ORDER BY id""",
                (project_id, channel, *kinds),
            ).fetchall()
        else:
            rows = self.con.execute(
                """SELECT * FROM pending_deliveries
                   WHERE project_id = ? AND channel = ? AND taken_at IS NULL
                   ORDER BY id""",
                (project_id, channel),
            ).fetchall()
        if rows:
            ids = tuple(r["id"] for r in rows)
            placeholders = ",".join("?" * len(ids))
            self.con.execute(
                f"""UPDATE pending_deliveries
                    SET taken_at = datetime('now')
                    WHERE id IN ({placeholders})""",
                ids,
            )
            self.con.commit()
        return rows

    def take_agent_deliveries(self, project_id: int) -> list[sqlite3.Row]:
        """Take all untaken agent-channel deliveries (marks them delivered)."""
        return self._take_deliveries(project_id, "agent")

    def take_user_deliveries(
        self, project_id: int, kinds: Optional[tuple] = None
    ) -> list[sqlite3.Row]:
        """Take all untaken user-channel deliveries (marks them delivered).

        kinds: optional filter, e.g. ('alert',) to take only alerts.
        """
        return self._take_deliveries(project_id, "user", kinds=kinds)

    def peek_deliveries(
        self, project_id: int, channel: Optional[str] = None
    ) -> list[sqlite3.Row]:
        """Non-destructive view of untaken deliveries (for check_drift)."""
        if channel:
            return self.con.execute(
                """SELECT * FROM pending_deliveries
                   WHERE project_id = ? AND channel = ? AND taken_at IS NULL
                   ORDER BY id""",
                (project_id, channel),
            ).fetchall()
        return self.con.execute(
            """SELECT * FROM pending_deliveries
               WHERE project_id = ? AND taken_at IS NULL
               ORDER BY id""",
            (project_id,),
        ).fetchall()

    # ---- safety_net_dismissals (§2.9) ----

    def trigger_safety_net(self, project_id: int, net_type: str) -> None:
        """Record that a safety-net warning was triggered for the first time.

        INSERT OR IGNORE means subsequent calls are idempotent — won't
        overwrite triggered_at or clear a prior dismissal.
        """
        self.con.execute(
            """INSERT OR IGNORE INTO safety_net_dismissals
               (project_id, net_type, triggered_at)
               VALUES (?, ?, datetime('now'))""",
            (project_id, net_type),
        )
        self.con.commit()

    def dismiss_safety_net_warning(self, project_id: int, net_type: str) -> None:
        """Permanently silence a safety-net warning (user opt-out, §2.9)."""
        row = self.con.execute(
            "SELECT id FROM safety_net_dismissals WHERE project_id = ? AND net_type = ?",
            (project_id, net_type),
        ).fetchone()
        if row:
            self.con.execute(
                "UPDATE safety_net_dismissals SET dismissed_at = datetime('now') WHERE id = ?",
                (row["id"],),
            )
        else:
            self.con.execute(
                """INSERT INTO safety_net_dismissals
                   (project_id, net_type, triggered_at, dismissed_at)
                   VALUES (?, ?, datetime('now'), datetime('now'))""",
                (project_id, net_type),
            )
        self.con.commit()

    def safety_net_state(self, project_id: int, net_type: str) -> Optional[str]:
        """Return None (never triggered), 'triggered', or 'dismissed'."""
        row = self.con.execute(
            """SELECT dismissed_at FROM safety_net_dismissals
               WHERE project_id = ? AND net_type = ?""",
            (project_id, net_type),
        ).fetchone()
        if row is None:
            return None
        return "dismissed" if row["dismissed_at"] else "triggered"

    # ---- task_scopes (§2.3 opt-in scope declaration) ----

    def set_task_scope(
        self,
        project_id: int,
        allowed_globs: list[str],
        forbidden_globs: list[str] | None = None,
    ) -> None:
        """Declare (or replace) the current task scope for a project.

        Deactivates any existing active scope first so there is always at most
        one active scope per project (single-task design, §4.3 note).
        allowed_globs: whitelist — patterns relative to the project root.
        forbidden_globs: blacklist — always out-of-scope, overrides allowed (§2.3).
        """
        self.con.execute(
            "UPDATE task_scopes SET state = 'completed' WHERE project_id = ? AND state = 'active'",
            (project_id,),
        )
        self.con.execute(
            """INSERT INTO task_scopes
                   (project_id, task_id, allowed_glob, forbidden_glob, state, created_at)
               VALUES (?, 'current', ?, ?, 'active', datetime('now'))""",
            (project_id, json.dumps(allowed_globs), json.dumps(forbidden_globs or [])),
        )
        self.con.commit()

    def clear_task_scope(self, project_id: int) -> None:
        """Deactivate the current task scope. task_scope_breach detection stops."""
        self.con.execute(
            "UPDATE task_scopes SET state = 'completed' WHERE project_id = ? AND state = 'active'",
            (project_id,),
        )
        self.con.commit()

    def get_active_task_scope(self, project_id: int) -> Optional[dict]:
        """Return {"allowed": [...], "forbidden": [...]} for the active scope, or None."""
        row = self.con.execute(
            """SELECT allowed_glob, forbidden_glob FROM task_scopes
               WHERE project_id = ? AND state = 'active'
               ORDER BY id DESC LIMIT 1""",
            (project_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "allowed": json.loads(row["allowed_glob"]),
            "forbidden": json.loads(row["forbidden_glob"] or "[]"),
        }

    # ---- assist_state (§4.9/§4.10/§4.11. 无 SDT 对位、纯工程辅助) ----

    def _ensure_assist_state(self, project_id: int) -> None:
        self.con.execute(
            "INSERT OR IGNORE INTO assist_state (project_id) VALUES (?)",
            (project_id,),
        )
        self.con.commit()

    def get_assist_state(self, project_id: int) -> sqlite3.Row:
        self._ensure_assist_state(project_id)
        return self.con.execute(
            "SELECT * FROM assist_state WHERE project_id = ?",
            (project_id,),
        ).fetchone()

    def update_assist_state(self, project_id: int, **fields: Any) -> None:
        self._ensure_assist_state(project_id)
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values())
        self.con.execute(
            f"UPDATE assist_state SET {cols} WHERE project_id = ?",
            (*vals, project_id),
        )
        self.con.commit()

    def max_seq(self, project_id: int) -> int:
        """Current maximum determination seq for a project (0 if none)."""
        row = self.con.execute(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM determinations WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        return row["m"] if row else 0

    # ---- snapshots (git batch 2) ----

    def create_snapshot(
        self,
        project_id: int,
        commit_hash: str,
        branch: Optional[str],
        snapshot_at_seq: int,
        parent_commit: Optional[str] = None,
        reason: str = "commit",
    ) -> int:
        """Insert a snapshot record (append-only). Returns the new row id."""
        cur = self.con.execute(
            """INSERT INTO snapshots
               (project_id, commit_hash, branch, snapshot_at_seq, parent_commit, reason, taken_at)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
            (project_id, commit_hash, branch, snapshot_at_seq, parent_commit, reason),
        )
        self.con.commit()
        return cur.lastrowid

    def latest_snapshot_for_commit(
        self, project_id: int, commit_hash: str
    ) -> Optional[sqlite3.Row]:
        """Most recent snapshot for a given commit (there may be multiple — append-only)."""
        return self.con.execute(
            """SELECT * FROM snapshots WHERE project_id = ? AND commit_hash = ?
               ORDER BY taken_at DESC, id DESC LIMIT 1""",
            (project_id, commit_hash),
        ).fetchone()

    def has_snapshot_for_commit(self, project_id: int, commit_hash: str) -> bool:
        """True if at least one snapshot exists for (project_id, commit_hash)."""
        row = self.con.execute(
            "SELECT 1 FROM snapshots WHERE project_id = ? AND commit_hash = ? LIMIT 1",
            (project_id, commit_hash),
        ).fetchone()
        return row is not None

    def list_snapshots_for_branch(self, project_id: int, limit: int = 50) -> list:
        """Snapshots for a project, newest first."""
        return self.con.execute(
            """SELECT * FROM snapshots WHERE project_id = ?
               ORDER BY taken_at DESC, id DESC LIMIT ?""",
            (project_id, limit),
        ).fetchall()

    def define_fingerprints_at_seq(self, project_id: int, at_seq: int) -> dict:
        """Reconstruct {(file_path, define_name): (coarse, fine)} as of `at_seq`.

        For each (file_path, define_name), take the latest determination with
        seq <= at_seq that is not a delete. Represents the live defines at that
        point in the version chain.
        fine may be None for records pre-dating the paired-fp migration.
        """
        rows = self.con.execute(
            """SELECT file_path, define_name, node_fingerprint, fine_fingerprint, edit_type
               FROM determinations
               WHERE project_id = ? AND seq <= ? AND define_name IS NOT NULL
               ORDER BY file_path, define_name, seq""",
            (project_id, at_seq),
        ).fetchall()
        # rows sorted by seq ascending; last write per (file, define) wins
        latest: dict = {}
        for r in rows:
            key = (r["file_path"], r["define_name"])
            latest[key] = (r["node_fingerprint"], r["fine_fingerprint"], r["edit_type"])
        return {k: (c, f) for k, (c, f, et) in latest.items() if et != "delete"}

    def all_current_defines(self, project_id: int) -> list[sqlite3.Row]:
        """Latest determination for each distinct (file_path, define_name) pair.

        Returns rows: file_path, define_name, latest_seq, version_count, latest_det_id.
        Used by assist_add_tests to enumerate all known defines.
        """
        return self.con.execute(
            """SELECT file_path, define_name,
                   MAX(seq) AS latest_seq,
                   COUNT(*) AS version_count,
                   MAX(id) AS latest_det_id
               FROM determinations
               WHERE project_id = ? AND define_name IS NOT NULL
               GROUP BY file_path, define_name""",
            (project_id,),
        ).fetchall()

    def covered_define_names(self, project_id: int) -> set:
        """Set of define_names with at least one coverage_map entry."""
        rows = self.con.execute(
            "SELECT DISTINCT define_name FROM coverage_map WHERE project_id = ?",
            (project_id,),
        ).fetchall()
        return {r["define_name"] for r in rows}

    def defs_changed_since(self, project_id: int, since_seq: int) -> list[sqlite3.Row]:
        """Distinct (file_path, define_name) changed after since_seq, with last_seq."""
        return self.con.execute(
            """SELECT file_path, define_name, MAX(seq) AS last_seq
               FROM determinations
               WHERE project_id = ? AND seq > ? AND define_name IS NOT NULL
               GROUP BY file_path, define_name""",
            (project_id, since_seq),
        ).fetchall()

    def production_defines_in_seq_window(
        self, project_id: int, seq_lo: int, seq_hi: int
    ) -> list[tuple[str, str]]:
        """(file_path, define_name) modified in (seq_lo, seq_hi], excluding deletes.

        Caller filters test paths via _is_excluded_path (path policy lives in signals layer).
        """
        rows = self.con.execute(
            """SELECT DISTINCT file_path, define_name FROM determinations
               WHERE project_id = ? AND seq > ? AND seq <= ?
                 AND define_name IS NOT NULL AND edit_type != 'delete'""",
            (project_id, seq_lo, seq_hi),
        ).fetchall()
        return [(r["file_path"], r["define_name"]) for r in rows]

    def has_open_regression(self, project_id: int) -> bool:
        """True if any regression incident is currently open."""
        row = self.con.execute(
            """SELECT COUNT(*) AS n FROM incidents
               WHERE project_id = ? AND signal = 'regression'
                 AND state IN ('open', 'notified_agent', 'escalated_user')""",
            (project_id,),
        ).fetchone()
        return (row["n"] if row else 0) > 0

    def caller_count_for_fqn(self, project_id: int, fqn: str) -> int:
        """Number of distinct callers for fqn in call_edges."""
        row = self.con.execute(
            """SELECT COUNT(DISTINCT caller) AS n
               FROM call_edges WHERE project_id = ? AND callee = ?""",
            (project_id, fqn),
        ).fetchone()
        return row["n"] if row else 0

    # ---- dangling_ref_observations (§2.8 dangling reference pre-incident tracking) ----

    def record_dangling_observation(
        self,
        project_id: int,
        file_path: str,
        caller_define: str,
        callee_text: str,
        det_id: int,
    ) -> int:
        """Record one observation of an unresolved call. Returns total observation count.

        Idempotent per (project_id, file_path, caller_define, callee_text, det_id):
        INSERT OR IGNORE ensures the same determination is counted at most once.
        The returned count is the number of distinct determinations that have
        observed this (file_path, caller_define, callee_text) triple as unresolved.
        """
        self.con.execute(
            """INSERT OR IGNORE INTO dangling_ref_observations
               (project_id, file_path, caller_define, callee_text, det_id, created_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'))""",
            (project_id, file_path, caller_define, callee_text, det_id),
        )
        self.con.commit()
        row = self.con.execute(
            """SELECT COUNT(*) AS n FROM dangling_ref_observations
               WHERE project_id = ? AND file_path = ? AND caller_define = ? AND callee_text = ?""",
            (project_id, file_path, caller_define, callee_text),
        ).fetchone()
        return row["n"] if row else 0

    def latest_test_run_seq(self, project_id: int) -> Optional[int]:
        """Maximum seq across all test_runs for this project (None if none recorded).

        Used by run_tests assist (heuristic tier) to determine whether any test
        ran at or after the current edit batch's earliest determination seq.
        """
        row = self.con.execute(
            "SELECT MAX(seq) AS m FROM test_runs WHERE project_id = ? AND seq IS NOT NULL",
            (project_id,),
        ).fetchone()
        m = row["m"] if row else None
        return m

    def recent_xml_run_exists(self, project_id: int, within_seconds: int = 120) -> bool:
        """True if a junit_xml test run was inserted in the last within_seconds seconds.

        Used by post-bash dedup: if a fresh XML run exists, skip the stdout run.
        XML is authoritative (per-case data + coverage_map); stdout is run-level only.
        Uses inserted_at (row creation time) rather than source_mtime to avoid
        cross-format timestamp comparison issues.
        """
        row = self.con.execute(
            """SELECT COUNT(*) AS n FROM test_runs
               WHERE project_id = ? AND source = 'junit_xml'
                 AND inserted_at > datetime('now', ? || ' seconds')""",
            (project_id, f"-{within_seconds}"),
        ).fetchone()
        return (row["n"] if row else 0) > 0

    def recent_stdout_run_for_cmd(
        self, project_id: int, cmd_fp: str, within_seconds: int = 30
    ) -> bool:
        """True if a stdout run for cmd_fp was inserted in the last within_seconds seconds.

        Prevents double-recording when the Bash hook fires for the same command
        within a short window.  source_path for stdout runs is 'stdout:<cmd_fingerprint>'.
        """
        row = self.con.execute(
            """SELECT COUNT(*) AS n FROM test_runs
               WHERE project_id = ? AND source = 'stdout'
                 AND source_path = ?
                 AND inserted_at > datetime('now', ? || ' seconds')""",
            (project_id, f"stdout:{cmd_fp}", f"-{within_seconds}"),
        ).fetchone()
        return (row["n"] if row else 0) > 0

    # ---- pending_recompute (§4.0 v2.1 async rebuild queue) ─────────────────

    def enqueue_recompute(self, project_id: int, file_path: str) -> None:
        """Queue file for structural recomputation. Idempotent for pending rows.

        Dedup: if (project_id, file_path) already 'pending', no-op.
        If 'done', re-activate to 'pending' (file changed/read again).
        """
        existing = self.con.execute(
            "SELECT id, status FROM pending_recompute WHERE project_id = ? AND file_path = ?",
            (project_id, file_path),
        ).fetchone()
        if existing is None:
            self.con.execute(
                """INSERT INTO pending_recompute (project_id, file_path, enqueued_at)
                   VALUES (?, ?, datetime('now'))""",
                (project_id, file_path),
            )
            self.con.commit()
        elif existing["status"] == "done":
            self.con.execute(
                "UPDATE pending_recompute SET status='pending', enqueued_at=datetime('now') WHERE id=?",
                (existing["id"],),
            )
            self.con.commit()
        # status='pending' already → no-op (dedup)

    def drain_recompute_queue(self, project_id: int) -> list[str]:
        """Atomically take all pending recompute entries. Returns file_path list.

        Marks rows 'done' before returning; SQLite single-writer ensures atomicity.
        """
        rows = self.con.execute(
            """SELECT id, file_path FROM pending_recompute
               WHERE project_id = ? AND status = 'pending'
               ORDER BY enqueued_at""",
            (project_id,),
        ).fetchall()
        if rows:
            ids = tuple(r["id"] for r in rows)
            placeholders = ",".join("?" * len(ids))
            self.con.execute(
                f"UPDATE pending_recompute SET status = 'done' WHERE id IN ({placeholders})",
                ids,
            )
            self.con.commit()
        self.prune_append_only_tables(project_id)
        return [r["file_path"] for r in rows]

    def pending_recompute_count(self, project_id: int) -> int:
        """Count pending recompute entries for this project."""
        row = self.con.execute(
            "SELECT COUNT(*) AS n FROM pending_recompute WHERE project_id = ? AND status='pending'",
            (project_id,),
        ).fetchone()
        return row["n"] if row else 0

    def has_gd_edges(self, project_id: int) -> bool:
        """True if 𝒢_D has been built (at least one gd_edge exists for this project)."""
        row = self.con.execute(
            "SELECT id FROM gd_edges WHERE project_id = ? LIMIT 1",
            (project_id,),
        ).fetchone()
        return row is not None

    def gd_node_count(self, project_id: int) -> int:
        """Count of distinct determination nodes referenced in gd_edges (from_det or to_det).

        Used by form-C clarity threshold: graph must cover enough nodes before BUER
        injects a structural hint (宁漏不误报 — wait until the map is meaningful).
        """
        row = self.con.execute(
            """SELECT COUNT(DISTINCT node_id) AS n FROM (
                   SELECT from_det AS node_id FROM gd_edges WHERE project_id = ?
                   UNION
                   SELECT to_det   AS node_id FROM gd_edges WHERE project_id = ?
               )""",
            (project_id, project_id),
        ).fetchone()
        return row["n"] if row else 0

    # ---- project lookup helpers (§4.4 server) ----

    def find_project_for_file(
        self, file_path: str, branch: "Optional[str]" = None
    ) -> Optional[int]:
        """Return project_id whose root_path contains file_path, or None.

        Uses realpath to resolve symlinks consistently with boundary checks
        in reconcile.py.  Longest-match wins for nested projects.

        branch=None (default): backward-compatible — returns the longest-prefix
        match ignoring branch (any branch).  Used by hooks that don't have git
        context yet; batch 3 will update those callers to pass branch.
        branch specified: among prefix-matching projects, prefer the one with the
        matching branch; fall back to branch-agnostic best if none found.
        """
        real = os.path.realpath(file_path)
        rows = self.con.execute(
            "SELECT id, root_path, branch FROM projects"
        ).fetchall()
        best: Optional[tuple[int, int, "Optional[str]"]] = None  # (root_len, id, branch)
        for row in rows:
            root_real = os.path.realpath(row["root_path"])
            if real == root_real or real.startswith(root_real + os.sep):
                if best is None or len(root_real) > best[0]:
                    best = (len(root_real), row["id"], row["branch"])
        if best is None:
            return None
        if branch is None:
            # backward-compatible: return longest-prefix match regardless of branch
            return best[1]
        # branch specified: among all same-length prefix matches, prefer branch match
        best_len = best[0]
        for row in rows:
            root_real = os.path.realpath(row["root_path"])
            if (real == root_real or real.startswith(root_real + os.sep)) and len(root_real) == best_len:
                if (row["branch"] or "") == (branch or ""):
                    return row["id"]
        # no branch match at best prefix length → fall back to branch-agnostic best
        return best[1]

    # ---- sessions (变更范围记忆体 Phase 1) ──────────────────────────────────

    def open_session(self, project_id: int, session_id: str) -> None:
        """Record session start.  INSERT OR IGNORE: idempotent on resume."""
        seq = self.max_seq(project_id)
        self.con.execute(
            """INSERT OR IGNORE INTO sessions
               (project_id, session_id, start_seq, started_at)
               VALUES (?, ?, ?, datetime('now'))""",
            (project_id, session_id, seq),
        )
        self.con.commit()

    def close_session(self, project_id: int, session_id: str) -> None:
        """Record session end (end_seq = current max_seq)."""
        seq = self.max_seq(project_id)
        self.con.execute(
            """UPDATE sessions SET end_seq=?, ended_at=datetime('now')
               WHERE project_id=? AND session_id=?""",
            (seq, project_id, session_id),
        )
        self.con.commit()

    def get_session(self, project_id: int, session_id: str) -> Optional[sqlite3.Row]:
        return self.con.execute(
            "SELECT * FROM sessions WHERE project_id=? AND session_id=?",
            (project_id, session_id),
        ).fetchone()

    def recent_sessions(self, project_id: int, n: int = 5) -> list:
        return self.con.execute(
            "SELECT * FROM sessions WHERE project_id=? ORDER BY start_seq DESC LIMIT ?",
            (project_id, n),
        ).fetchall()

    def changes_in_range(
        self, project_id: int, start_seq: int, end_seq: int
    ) -> list:
        """Determinations with seq in (start_seq, end_seq], ordered by seq."""
        return self.con.execute(
            """SELECT file_path, define_name, edit_type, seq, created_at, id
               FROM determinations
               WHERE project_id=? AND seq>? AND seq<=?
               ORDER BY seq""",
            (project_id, start_seq, end_seq),
        ).fetchall()

    def changes_for_session(self, project_id: int, session_id: str) -> list:
        """All changes that occurred during a recorded session."""
        sess = self.get_session(project_id, session_id)
        if sess is None:
            return []
        end = sess["end_seq"] if sess["end_seq"] is not None else self.max_seq(project_id)
        return self.changes_in_range(project_id, sess["start_seq"], end)

    # ---- crash_stacks (§ Thm 11.10 崩溃路径 ∩ 影响锥) ───────────────────────

    def insert_crash_stack(
        self,
        project_id: int,
        seq: Optional[int],
        stack_fqns_json: str,
        command: Optional[str] = None,
        error_signature: Optional[str] = None,
    ) -> int:
        """Store parsed crash-stack FQNs + optional error signature for one bash invocation."""
        cur = self.con.execute(
            """INSERT INTO crash_stacks
               (project_id, seq, stack_fqns, command, error_signature, created_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'))""",
            (project_id, seq, stack_fqns_json, command, error_signature),
        )
        self.con.commit()
        return cur.lastrowid

    def recent_crash_stacks(self, project_id: int, n: int = 5) -> list[sqlite3.Row]:
        """Return the n most recent crash stacks for project, newest first."""
        return self.con.execute(
            "SELECT * FROM crash_stacks WHERE project_id=? ORDER BY id DESC LIMIT ?",
            (project_id, n),
        ).fetchall()

    def prune_append_only_tables(
        self,
        project_id: int,
        crash_keep: int = 200,
        test_runs_keep: int = 1000,
    ) -> None:
        """Bound unbounded append-only tables to prevent long-running growth.

        crash_stacks: keep newest `crash_keep` rows per project.
        test_runs: keep newest `test_runs_keep` rows per project; cascade-delete
                   their test_cases manually (FK has no ON DELETE CASCADE).
        Called at session boundary (drain). Best-effort; not in real-time path.
        """
        # crash_stacks: delete all but newest crash_keep
        self.con.execute(
            """DELETE FROM crash_stacks
               WHERE project_id = ? AND id NOT IN (
                   SELECT id FROM crash_stacks WHERE project_id = ?
                   ORDER BY id DESC LIMIT ?
               )""",
            (project_id, project_id, crash_keep),
        )
        # test_cases of test_runs that will be pruned (delete first, FK integrity)
        self.con.execute(
            """DELETE FROM test_cases
               WHERE test_run_id IN (
                   SELECT id FROM test_runs
                   WHERE project_id = ? AND id NOT IN (
                       SELECT id FROM test_runs WHERE project_id = ?
                       ORDER BY id DESC LIMIT ?
                   )
               )""",
            (project_id, project_id, test_runs_keep),
        )
        # test_runs: delete all but newest test_runs_keep
        self.con.execute(
            """DELETE FROM test_runs
               WHERE project_id = ? AND id NOT IN (
                   SELECT id FROM test_runs WHERE project_id = ?
                   ORDER BY id DESC LIMIT ?
               )""",
            (project_id, project_id, test_runs_keep),
        )
        self.con.commit()

    def crash_stacks_in_seq_range(
        self, project_id: int, seq_start: int, seq_end: int
    ) -> list[sqlite3.Row]:
        """Crash stacks with seq in (seq_start, seq_end] for a project, oldest first."""
        return self.con.execute(
            """SELECT * FROM crash_stacks
               WHERE project_id = ? AND seq > ? AND seq <= ?
               ORDER BY seq, id""",
            (project_id, seq_start, seq_end),
        ).fetchall()

    # ---- cost_samples (OTel telemetry, savings report) ─────────────────────────

    def record_cost_sample(
        self,
        session_id: str,
        model: str,
        cost_usd: float,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
    ) -> int:
        """Store one OTLP cost/token datapoint. project_id bridged from sessions table."""
        row = self.con.execute(
            "SELECT project_id FROM sessions WHERE session_id = ? LIMIT 1",
            (session_id,),
        ).fetchone()
        project_id = row["project_id"] if row else None
        cur = self.con.execute(
            """INSERT INTO cost_samples
               (project_id, session_id, model, cost_usd, input_tokens, output_tokens,
                cache_read_tokens, cache_creation_tokens, sample_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
            (project_id, session_id, model, cost_usd,
             input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens),
        )
        self.con.commit()
        return cur.lastrowid

    def cost_summary_for_project(self, project_id: int) -> list[dict]:
        """Return per-model cost/token totals for project, ordered by cost descending."""
        rows = self.con.execute(
            """SELECT model,
                      COUNT(*) as sample_count,
                      COALESCE(SUM(cost_usd), 0.0) as total_cost,
                      COALESCE(SUM(input_tokens), 0) as total_input,
                      COALESCE(SUM(output_tokens), 0) as total_output,
                      COALESCE(SUM(cache_read_tokens), 0) as total_cache_read,
                      COALESCE(SUM(cache_creation_tokens), 0) as total_cache_creation
               FROM cost_samples
               WHERE project_id = ?
               GROUP BY model
               ORDER BY total_cost DESC""",
            (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def total_cost_for_project(self, project_id: int) -> float:
        """Return total measured USD cost for project."""
        row = self.con.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) as total FROM cost_samples WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        return float(row["total"])

    def resolved_intervention_incidents(self, project_id: int) -> list:
        """Return resolved/escalated token_waste and define_loop incidents for the project."""
        return self.con.execute(
            """SELECT signal, target_node, details, post_notify_count
               FROM incidents
               WHERE project_id = ? AND signal IN ('token_waste', 'define_loop')
               AND state IN ('resolved', 'escalated_user')
               ORDER BY created_at""",
            (project_id,),
        ).fetchall()

    def consecutive_edit_count(
        self, project_id: int, file_path: str, define_name: str
    ) -> int:
        """Count consecutive recent edits to this define without interruption.

        Scans the global determination sequence in reverse (newest first).
        Counts how many leading entries are for (file_path, define_name) before
        a different define's determination appears.  Returns 0 if the define has
        no determinations, 1 if it was modified exactly once with no prior edits,
        etc.

        "Consecutive" means: in the global edit order, no OTHER define was modified
        between two consecutive edits of this define.  If another define was touched
        in between, the streak resets (we count from the latest group only).
        """
        rows = self.con.execute(
            """SELECT file_path, define_name FROM determinations
               WHERE project_id = ?
               ORDER BY seq DESC""",
            (project_id,),
        ).fetchall()
        count = 0
        for r in rows:
            if r["file_path"] == file_path and r["define_name"] == define_name:
                count += 1
            else:
                break
        return count
