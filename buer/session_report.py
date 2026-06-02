"""BUER Recap — last-session summary (presentation layer only, zero schema/detect changes).

Two tiers:
  build_teaser  — very short, injected at session-start alongside structural overview
  build_recap   — full plain-language report, served by buer_recap MCP tool on request

Content: relative time + files changed + unresolved issues (plain language, no signal-name
leakage) + honest work record (N flagged / M resolved). Never fabricates savings figures.
Returns None when there is nothing worth reporting.
"""
from __future__ import annotations

from datetime import datetime


def _relative_time(started_at: str) -> str:
    dt = datetime.strptime(started_at, "%Y-%m-%d %H:%M:%S")
    days = (datetime.utcnow() - dt).days
    date_str = dt.strftime("%b %-d")
    if days <= 0:
        return f"today ({date_str})"
    if days == 1:
        return f"yesterday ({date_str})"
    if days < 7:
        return f"{days} days ago ({date_str})"
    return date_str


_SIGNAL_HUMAN: dict[str, str] = {
    "stuck_region":       "{f} was edited many times without settling",
    "debug_loop":         "{f} is stuck in a debug loop",
    "regression":         "{f} may have introduced a regression",
    "dangling_reference": "a call points to a definition that doesn't exist ({f})",
    "test_tampering":     "tests for {f} were altered — worth a look",
    "boundary_breach":    "{f} was changed outside the project boundary",
    "task_scope_breach":  "{f} was changed outside the declared task scope",
}


def _humanize(signal: str, target_node: str) -> str:
    f = target_node.split("::")[0].split("/")[-1] if target_node else "somewhere"
    tmpl = _SIGNAL_HUMAN.get(signal, "{f} has an open issue")
    return tmpl.format(f=f)


def _last_session(store, project_id: int):
    return store.con.execute(
        "SELECT session_id, start_seq, end_seq, started_at, ended_at FROM sessions "
        "WHERE project_id=? AND ended_at IS NOT NULL ORDER BY ended_at DESC LIMIT 1",
        (project_id,),
    ).fetchone()


def _gather(store, project_id: int):
    """Return (sess, changed_files, unresolved, n_total, m_resolved) or None."""
    try:
        sess = _last_session(store, project_id)
    except Exception:
        return None
    if sess is None:
        return None

    changes = store.changes_for_session(project_id, sess["session_id"])
    changed_files: list[str] = []
    seen: set[str] = set()
    for c in changes:
        base = c["file_path"].split("/")[-1]
        if base not in seen:
            seen.add(base)
            changed_files.append(base)

    st, en = sess["started_at"], sess["ended_at"]
    unresolved_rows = store.con.execute(
        "SELECT signal, target_node FROM incidents WHERE project_id=? "
        "AND created_at>=? AND created_at<=? "
        "AND state IN ('open','notified_agent','escalated_user')",
        (project_id, st, en),
    ).fetchall()
    unresolved = [_humanize(r["signal"], r["target_node"]) for r in unresolved_rows]

    n_total = store.con.execute(
        "SELECT COUNT(*) n FROM incidents "
        "WHERE project_id=? AND created_at>=? AND created_at<=?",
        (project_id, st, en),
    ).fetchone()["n"]
    m_resolved = store.con.execute(
        "SELECT COUNT(*) n FROM incidents "
        "WHERE project_id=? AND created_at>=? AND created_at<=? AND state='resolved'",
        (project_id, st, en),
    ).fetchone()["n"]

    if not changed_files and not unresolved and n_total == 0:
        return None

    return sess, changed_files, unresolved, n_total, m_resolved


def build_recap(store, project_id: int) -> str | None:
    """Full recap (B-tier). Returns str or None."""
    g = _gather(store, project_id)
    if g is None:
        return None
    sess, files, unresolved, n_total, m_resolved = g
    rel = _relative_time(sess["started_at"])
    lines = [f"BUER Recap — last session: {rel}"]
    if files:
        shown = files[:3]
        more = f" and {len(files) - 3} more" if len(files) > 3 else ""
        lines.append(f"· changed {', '.join(shown)}{more}")
    if unresolved:
        lines.append("· still unresolved:")
        for u in unresolved:
            lines.append(f"    - {u}")
    if n_total > 0:
        lines.append(f"· ({n_total} of these were flagged by BUER, {m_resolved} resolved after the heads-up)")
    return "\n".join(lines)


def build_teaser(store, project_id: int) -> str | None:
    """Very short teaser (A-tier, session-start injection). Returns str or None."""
    g = _gather(store, project_id)
    if g is None:
        return None
    sess, files, unresolved, n_total, m_resolved = g
    rel = _relative_time(sess["started_at"])
    parts = [f"BUER Recap: this project was last touched {rel}"]
    if unresolved:
        parts.append(f", with {len(unresolved)} issue(s) still unresolved")
    parts.append(". Ask me to run buer_recap for details.")
    return "".join(parts)
