"""BUER project health safety-net checks (§2.9) + project_overview (§4.8).

Public API
----------
detect_safety_net(store, project_id, root)   → list[dict]   pure condition check
maybe_run_safety_net(store, project_id, root)               periodic trigger
project_overview(store, project_id, root)    → str          §4.8 onboarding tool
coarse_structure_map(store, project_id, root) → str         §2 形态 A: coarse-layer map
"""
from __future__ import annotations

import datetime
import os
import subprocess
from typing import Optional

from buer import metrics, tech_debt
from buer.boundary import EXCLUDED_DIR_NAMES
from buer.store import Store

# ── thresholds ────────────────────────────────────────────────────────────────

MIN_DEFINES_FOR_CHECK = 10   # don't warn on tiny prototypes
MIN_EDITS_FOR_CHECK = 5      # don't warn before any real activity
EDITS_SINCE_COMMIT_WARN = 15 # stale-git threshold
HEALTH_CHECK_PERIOD = 20     # run safety-net check every N total edits

_MAX_COMPONENTS_SHOWN = 8    # top N largest components shown; rest summarised


# ── internal helpers ──────────────────────────────────────────────────────────

def _count_total_edits(store: Store, project_id: int) -> int:
    row = store.con.execute(
        "SELECT COUNT(*) AS n FROM determinations WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    return row["n"] if row else 0


def _count_distinct_defines(store: Store, project_id: int) -> int:
    row = store.con.execute(
        """SELECT COUNT(DISTINCT define_name) AS n FROM determinations
           WHERE project_id = ? AND define_name IS NOT NULL""",
        (project_id,),
    ).fetchone()
    return row["n"] if row else 0


def _has_test_files(root: str) -> bool:
    """Walk root for test files (test_*.py, *_test.py, *.test.ts, spec.*, ...)."""
    test_patterns = ("test_", "_test.", ".test.", "spec.", "_spec.")
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if not d.startswith(".") and d not in EXCLUDED_DIR_NAMES]
            for fn in filenames:
                lower = fn.lower()
                if any(p in lower for p in test_patterns):
                    return True
    except OSError:
        pass
    return False


def _has_test_runs(store: Store, project_id: int) -> bool:
    row = store.con.execute(
        "SELECT COUNT(*) AS n FROM test_runs WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    return (row["n"] if row else 0) > 0


def _find_vcs_root(start: str) -> Optional[str]:
    """Walk up dirs looking for .git, .hg, or .svn. Return the directory or None."""
    current = os.path.realpath(start)
    while True:
        for marker in (".git", ".hg", ".svn"):
            if os.path.exists(os.path.join(current, marker)):
                return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def _edits_since_last_git_commit(store: Store, project_id: int, vcs_root: str) -> Optional[int]:
    """Determinations created after the most recent git commit, or None if unknown."""
    try:
        result = subprocess.run(
            ["git", "-C", vcs_root, "log", "-1", "--format=%ct"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        commit_ts = int(result.stdout.strip())
        commit_dt = datetime.datetime.utcfromtimestamp(commit_ts).strftime("%Y-%m-%d %H:%M:%S")
        row = store.con.execute(
            """SELECT COUNT(*) AS n FROM determinations
               WHERE project_id = ? AND created_at > ?""",
            (project_id, commit_dt),
        ).fetchone()
        return row["n"] if row else 0
    except Exception:
        return None


def _hub_nodes(store: Store, project_id: int, top_n: int = 5) -> list[tuple[str, int]]:
    """Top-N callee nodes by call_edges in-degree (most depended-upon)."""
    rows = store.con.execute(
        """SELECT callee, COUNT(*) AS indegree
           FROM call_edges
           WHERE project_id = ?
           GROUP BY callee
           ORDER BY indegree DESC
           LIMIT ?""",
        (project_id, top_n),
    ).fetchall()
    return [(r["callee"], r["indegree"]) for r in rows]


def _active_signal_names(store: Store, project_id: int) -> list[str]:
    rows = store.con.execute(
        """SELECT DISTINCT signal FROM incidents
           WHERE project_id = ? AND state IN ('open', 'notified_agent', 'escalated_user')
           ORDER BY signal""",
        (project_id,),
    ).fetchall()
    return [r["signal"] for r in rows]


# ── public API ────────────────────────────────────────────────────────────────

def detect_safety_net(
    store: Store,
    project_id: int,
    root: str,
) -> list[dict]:
    """Pure safety-net condition check (§2.9).

    Returns a list of warning dicts {type, message} for each failing check.
    Does NOT filter by dismissal state — callers decide how to act on results.
    Scale gate: returns [] if project is too small to warrant the check.
    """
    total_edits = _count_total_edits(store, project_id)
    distinct_defines = _count_distinct_defines(store, project_id)

    if total_edits < MIN_EDITS_FOR_CHECK or distinct_defines < MIN_DEFINES_FOR_CHECK:
        return []

    warnings: list[dict] = []

    # Check 1: no tests
    if not _has_test_runs(store, project_id) and not _has_test_files(root):
        warnings.append({
            "type": "no_tests",
            "message": (
                f"no tests: the agent has modified {distinct_defines} defines, but nothing is verifying they still work."
                "\n  regressions go undetected until something breaks in production."
                "\n  (BUER's debug_loop / regression / test_tampering detection all depend on tests; they cannot function without them.)"
            ),
        })

    # Check 2: no VCS or stale commits
    vcs_root = _find_vcs_root(root)
    if vcs_root is None:
        warnings.append({
            "type": "no_git",
            "message": (
                "no version control: no .git / .hg / .svn found in project dir or any parent."
                "\n  if the agent breaks something, there is no way to roll back to a known-good state."
            ),
        })
    else:
        edits_since = _edits_since_last_git_commit(store, project_id, vcs_root)
        if edits_since is not None and edits_since >= EDITS_SINCE_COMMIT_WARN:
            warnings.append({
                "type": "no_git",
                "message": (
                    f"git commits are stale: {edits_since} edits since the last commit."
                    f"\n  if the agent breaks something now, you can only roll back past those {edits_since} edits — any intermediate good state is lost."
                ),
            })

    return warnings


def maybe_run_safety_net(
    store: Store,
    project_id: int,
    root: str,
) -> None:
    """Periodic safety-net trigger. Called at end of each reconcile (§2.9).

    Fires every HEALTH_CHECK_PERIOD total edits.
    Each net_type is only delivered once (until dismissed).
    """
    total_edits = _count_total_edits(store, project_id)
    if total_edits == 0 or total_edits % HEALTH_CHECK_PERIOD != 0:
        return

    warnings = detect_safety_net(store, project_id, root)
    # Only deliver warnings that have not been shown or dismissed yet
    new_warnings = [
        w for w in warnings
        if store.safety_net_state(project_id, w["type"]) is None
    ]
    if not new_warnings:
        return

    parts = ["⚠ this project is missing a safety net\n"]
    for w in new_warnings:
        parts.append(f"· {w['message']}")
    parts.append(
        "\nthis is a systemic risk: not a one-off edit problem, but a missing safety net."
        "\nsuggestion: add some tests; commit to git incrementally as you edit."
        "\n(use dismiss_safety_net_warning to permanently silence this notice.)"
    )
    message = "\n".join(parts)

    store.enqueue_delivery(project_id, None, "user", message)
    for w in new_warnings:
        store.trigger_safety_net(project_id, w["type"])


def project_overview(
    store: Store,
    project_id: int,
    root: str,
) -> str:
    """Project structure overview for onboarding (§4.8).

    Shows: file/define counts, most-active modules, hub nodes, active
    signals, and safety-net status.  Always shows raw safety-net conditions
    regardless of dismissal state so the user can see the real picture.
    """
    file_row = store.con.execute(
        "SELECT COUNT(DISTINCT file_path) AS n FROM determinations WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    file_count = file_row["n"] if file_row else 0

    def_row = store.con.execute(
        """SELECT COUNT(DISTINCT define_name) AS n FROM determinations
           WHERE project_id = ? AND define_name IS NOT NULL""",
        (project_id,),
    ).fetchone()
    define_count = def_row["n"] if def_row else 0

    total_edits = _count_total_edits(store, project_id)

    mod_rows = store.con.execute(
        """SELECT file_path, COUNT(*) AS edits
           FROM determinations WHERE project_id = ?
           GROUP BY file_path ORDER BY edits DESC LIMIT 8""",
        (project_id,),
    ).fetchall()
    modules = [(os.path.relpath(r["file_path"], root), r["edits"]) for r in mod_rows]

    hubs = _hub_nodes(store, project_id)
    active_signals = _active_signal_names(store, project_id)
    safety_warnings = detect_safety_net(store, project_id, root)

    lines = [f"[BUER] project overview — {root}"]
    lines.append(f"  files: {file_count}  ·  defines: {define_count}  ·  total edits: {total_edits}")

    if modules:
        lines.append("\n  most active modules:")
        for path, edits in modules[:5]:
            lines.append(f"    {path}  ({edits} edits)")

    if hubs:
        lines.append("\n  hub nodes (highest in-degree):")
        for callee, indegree in hubs:
            lines.append(f"    {callee}  ← {indegree} callers")

    lines.append(
        "\n  monitored signals: stuck_region · debug_loop · define_loop"
        " · boundary_breach · regression · test_tampering"
    )
    if active_signals:
        lines.append(f"  active signals: {', '.join(active_signals)}")
    else:
        lines.append("  no active signals.")

    if safety_warnings:
        lines.append("\n  ⚠ safety net missing:")
        for w in safety_warnings:
            for wline in w["message"].splitlines():
                lines.append(f"    {wline}")
        lines.append(
            "\n  BUER transparency note: without tests, debug_loop / regression / test_tampering detection cannot function;"
            "\n  silent regressions are invisible to BUER."
        )
        for w in safety_warnings:
            state = store.safety_net_state(project_id, w["type"])
            if state == "dismissed":
                lines.append(f"  ({w['type']} warning dismissed, but the condition still applies.)")
    else:
        has_tests = _has_test_runs(store, project_id) or _has_test_files(root)
        vcs = _find_vcs_root(root)
        if define_count < MIN_DEFINES_FOR_CHECK:
            lines.append("\n  (project too small for safety-net checks to activate.)")
        elif has_tests and vcs:
            lines.append("\n  ✓ safety net OK (tests present + version control active).")

    # §3 structural concern hints (v2.1 路 A) — passive, attached to overview only.
    # Not incidents, not alerts — "有空看看" orientation, not interruption.
    concerns = tech_debt.structural_concerns(store, project_id, root)
    debt_section = tech_debt.format_debt_section(concerns)
    if debt_section:
        lines.append(debt_section)

    return "\n".join(lines)


def coarse_structure_map(store: Store, project_id: int, root: str) -> str:
    """Coarse-layer structural map — v2.1 §2 形态 A (SessionStart constant injection).

    Computes the global connected-component partition [GD Prop 9.2] of the
    full project 𝒢_D and formats a skeleton overview: subsystem count, sizes,
    representative modules, and cross-subsystem hub nodes.

    Accuracy note (§2.6): partition quality = gd_edge coverage.
    Dynamic languages (Python/JS) may have uncaptured dynamic-dispatch edges,
    causing components that should be joined to appear separate.
    TS/TSX data-flow archive coverage is higher.

    Large-repo note: global partition is O(V+E), runs once per SessionStart.
    Cache in a future batch if latency is observed on 100k+ node graphs.

    Returns "" when there are no nodes to partition.
    """
    components = metrics.connected_components_all(store, project_id)
    if not components:
        return ""

    # Bulk-fetch det_id → file_path for component label construction
    rows = store.con.execute(
        "SELECT id, file_path FROM determinations WHERE project_id = ?",
        (project_id,),
    ).fetchall()
    det_file: dict[int, str] = {r["id"]: r["file_path"] for r in rows}

    # Sort by component size descending
    components_sorted = sorted(components, key=len, reverse=True)

    lines = [
        "\n[BUER] codebase structure overview (coarse skeleton):",
        f"  {len(components)} relatively independent functional subsystems (connected components):",
    ]

    for i, comp in enumerate(components_sorted[:_MAX_COMPONENTS_SHOWN]):
        # Collect file frequencies within this component
        file_counts: dict[str, int] = {}
        for det_id in comp:
            if det_id in det_file:
                fp = det_file[det_id]
                file_counts[fp] = file_counts.get(fp, 0) + 1
        top_files = sorted(file_counts, key=lambda f: -file_counts[f])[:3]
        rep = ", ".join(os.path.relpath(fp, root) for fp in top_files) if top_files else "(unknown)"
        lines.append(f"    subsystem {i + 1}: {len(comp)} structural nodes, primarily involving {rep}")

    if len(components) > _MAX_COMPONENTS_SHOWN:
        extra = len(components) - _MAX_COMPONENTS_SHOWN
        lines.append(f"    ... ({extra} smaller subsystems, {len(components)} total)")

    # Cross-component hubs from call_edges (high in-degree = many dependents)
    hubs = _hub_nodes(store, project_id, top_n=5)
    if hubs:
        lines.append("\n  cross-subsystem hubs (depended on by many):")
        for callee, indegree in hubs:
            lines.append(f"    {callee}  ← {indegree} callers")

    lines.append("\n  (this is the coarsest skeleton; for details on any subsystem, query the local briefing at edit time.)")
    lines.append("  (based on detected dependencies; dynamic languages may have undetected connections — treat subsystem partition as approximate.)")

    return "\n".join(lines)
