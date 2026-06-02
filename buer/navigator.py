"""BUER large-codebase navigator — v2.1 §2 形态 B/C.

形态 B: structural_briefing_text — pre-edit local structural map for one define.
形态 C: structure_guide_hint — mid-session concrete hint injected via post-read
        once the graph is clear enough to say something specific.

Clarity threshold (form C): THETA_CLARITY_NODES = 20 (待校准).
BUER is a mid-session structural co-pilot, not an opening navigator.  It builds
the graph quietly until the graph is clear enough, then offers one concrete hint
based on what it has already seen — not generic methodology.

SDT note:
  ω / Γ_R / connected_component — GD/Math Ext, strict structural operations.
  callers / callees (blast direction / dependency) — engineering proxy,
  no SDT counterpart (like node_fingerprint ↔ §2.4.4).

Dynamic-language caveat: callers/callees from call_edges may undercount for
Python/JS (dynamic dispatch not captured statically).  TS/TSX coverage higher.

Public API
----------
THETA_CLARITY_NODES: int = 20
resolve_target(store, project_id, target) -> (file_path, define_name) | (None, None)
structural_briefing_text(store, project_id, root, file_path, define_name) -> str
structure_guide_hint(store, project_id) -> str
"""
from __future__ import annotations

import os

from buer import callgraph, metrics
from buer.health import _hub_nodes
from buer.store import Store

# ── form-C clarity threshold (待校准 — pending dogfooding calibration) ────────
THETA_CLARITY_NODES: int = 20   # 待校准


# ── target resolution ─────────────────────────────────────────────────────────

def resolve_target(
    store: Store,
    project_id: int,
    target: str,
) -> tuple[str, str] | tuple[None, None]:
    """Parse target into (file_path, define_name).

    Accepts:
      "file_path::define_name"  — preferred full form (same as incidents target_node)
      "define_name"             — fallback: searches most-recently-edited define
    Returns (None, None) if the define is not found in determinations.
    """
    if "::" in target:
        file_path, define_name = target.split("::", 1)
        det = store.current_version_determination(project_id, file_path, define_name)
        if det:
            return file_path, define_name
        return None, None
    else:
        row = store.con.execute(
            """SELECT file_path, define_name
               FROM determinations
               WHERE project_id = ? AND define_name = ?
               ORDER BY seq DESC LIMIT 1""",
            (project_id, target),
        ).fetchone()
        if row:
            return row["file_path"], row["define_name"]
        return None, None


# ── internal: det_id → display label ─────────────────────────────────────────

def _det_labels(store: Store, det_ids: list[int]) -> list[str]:
    """Look up display labels (define_name or file basename) for det_ids."""
    if not det_ids:
        return []
    placeholders = ",".join("?" * len(det_ids))
    rows = store.con.execute(
        f"SELECT id, define_name, file_path FROM determinations WHERE id IN ({placeholders})",
        det_ids,
    ).fetchall()
    by_id = {
        r["id"]: (r["define_name"] if r["define_name"] else os.path.basename(r["file_path"]))
        for r in rows
    }
    return [by_id[did] for did in det_ids if did in by_id]


# ── form B: pre-edit local map ────────────────────────────────────────────────

def structural_briefing_text(
    store: Store,
    project_id: int,
    root: str,
    file_path: str,
    define_name: str,
) -> str:
    """Generate pre-edit local structural map for one define (v2.1 §2 形态 B).

    Combines callers_of, callees_of, Γ_R neighbors, and connected_component
    into a concise local map.  Pure read — no computation or DB writes triggered.

    Dynamic-language note: callers/callees from call_edges (static analysis).
    Python/JS may undercount; TS/TSX coverage is higher.
    """
    mod = callgraph.module_name_of(file_path, root)
    fqn_call = callgraph._lang_fqn(file_path, mod, define_name)

    callers = store.callers_of(project_id, fqn_call)
    callees = store.callees_of(project_id, fqn_call)

    # Γ_R neighbors + component size from 𝒢_D (require latest determination)
    det = store.current_version_determination(project_id, file_path, define_name)
    gamma_r_names: list[str] = []
    comp_size = 1
    if det:
        det_id = det["id"]
        gamma_r_ids = sorted(metrics.gamma_r_neighbors(store, project_id, det_id))[:5]
        gamma_r_names = _det_labels(store, gamma_r_ids)
        comp = metrics.connected_component(store, project_id, det_id)
        comp_size = len(comp)

    lines = [f"[BUER] structural context for {define_name}:"]

    if callers:
        sample = ", ".join(callers[:5]) + (" …" if len(callers) > 5 else "")
        lines.append(f"  depended on by: {len(callers)} locations ({sample}) — changes here will ripple to these.")
    else:
        lines.append("  depended on by: 0 locations (no callers detected).")

    if callees:
        sample = ", ".join(callees[:5]) + (" …" if len(callees) > 5 else "")
        lines.append(f"  depends on: {len(callees)} defines ({sample}) — understanding this requires looking at these first.")
    else:
        lines.append("  depends on: 0 defines (no dependencies detected).")

    if gamma_r_names:
        lines.append(f"  structurally related: {', '.join(gamma_r_names)} (shared history or downstream — may need to be changed together).")
    else:
        lines.append("  structurally related: (no Γ_R neighbors detected).")

    lines.append(f"  subsystem: functional region of {comp_size} nodes.")
    lines.append("  (local map only; judgment is yours. Dynamic-language deps may be incomplete.)")

    return "\n".join(lines)


# ── form C: mid-session structural hint ──────────────────────────────────────

def structure_guide_hint(store: Store, project_id: int) -> str:
    """Generate the form-C structural hint when clarity threshold is met (v2.1 §2 形态 C).

    Returns concrete already-identified hubs — not generic methodology.
    Returns "" if no hubs found (silence is better than empty platitudes).
    """
    hubs = _hub_nodes(store, project_id, top_n=5)
    if not hubs:
        return ""
    hub_str = ", ".join(f"{callee} ({indegree} callers)" for callee, indegree in hubs)
    return (
        "(BUER structural observation) You are exploring this codebase. Identified structural hubs: "
        + hub_str
        + " — these are critical nodes depended on by many locations."
        " See the session-start structure overview for the functional subsystem partition."
    )
