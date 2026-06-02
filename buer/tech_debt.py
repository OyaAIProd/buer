"""BUER structural concern hints — v2.1 §3 路 A.

Four observable dimensions:
  callers  — how many places depend on this define (high → broad blast-radius)
  callees  — how many other defines this depends on (high → high maintenance cost)
  churn    — how many edit rounds (high → repeatedly modified, possibly unstable)
  lambda_  — Λ: constraint-load accumulation (GD Def 6.4 / Thm 6.7)
             = Σ_{w in gd_predecessor_cone(v)} callers_count(w)
             Identifies "deep but not wide" structural coupling: a define with few
             direct callers but many upstream cross-define dependencies whose
             callers could all be affected by changes here.
             gd_edges contains only current-version cross_define edges; version_chain
             edges were removed at source (gd.py) and old-version det edges are
             deleted on each supersede (reconcile.py → delete_gd_edges_for_det).

Batch pre-fetch implementation: all four dimensions are computed in a single
pass over pre-fetched data (call_edges, determinations, gd_edges).  Zero
per-node DB queries after the initial bulk loads — faster than the old
3-dim per-node approach (17ms vs 142ms on a ~1000-node real-world graph).

Dynamic-language caveat: callers/callees come from call_edges, which may
undercount for Python/JS (dynamic dispatch, runtime imports not captured
statically).  TS/TSX coverage is higher.
"""
from __future__ import annotations

import collections

from buer import callgraph
from buer.store import Store

# ── thresholds (待校准 — pending dogfooding calibration) ─────────────────────
THETA_DEBT_CALLERS: int = 5    # 待校准
THETA_DEBT_CALLEES: int = 8    # 待校准
THETA_DEBT_CHURN: int   = 10   # 待校准
THETA_DEBT_LAMBDA: int  = 20   # 待校准 — Λ=61/60 for deep capability-cluster nodes on a real-world graph
TOP_N_DEBT: int = 5


def structural_concerns(
    store: Store,
    project_id: int,
    root: str,
    top_n: int = TOP_N_DEBT,
) -> list[dict]:
    """Compute top-N structural concerns for the project (four dimensions).

    Returns dicts sorted by hit-dim count then raw magnitude:
      {target_node, define_name, callers, callees, churn, lambda_, hit_dims}

    Only defines meeting at least one threshold are included.
    All data pre-fetched once; computation is pure-Python dict lookups.
    """
    con = store.con

    # ── (A) call_edges: callers and callees counts per FQN ────────────────────
    callers_count: collections.Counter = collections.Counter()
    callees_count: collections.Counter = collections.Counter()
    for r in con.execute(
        "SELECT caller, callee FROM call_edges WHERE project_id=?", (project_id,)
    ):
        callers_count[r["callee"]] += 1
        callees_count[r["caller"]] += 1

    # ── (B) churn: version count per (file_path, define_name) ─────────────────
    churn_map: dict[tuple[str, str], int] = {}
    for r in con.execute(
        "SELECT file_path, define_name, COUNT(*) AS cnt "
        "FROM determinations "
        "WHERE project_id=? AND define_name IS NOT NULL "
        "GROUP BY file_path, define_name",
        (project_id,),
    ):
        churn_map[(r["file_path"], r["define_name"])] = r["cnt"]

    if not churn_map:
        return []

    # ── (C) det_id → FQN mapping + latest det_id per (fp, dn) ────────────────
    det_fqn: dict[int, str] = {}
    latest_det: dict[tuple[str, str], int] = {}
    for r in con.execute(
        "SELECT id, file_path, define_name "
        "FROM determinations "
        "WHERE project_id=? AND define_name IS NOT NULL",
        (project_id,),
    ):
        fp, dn = r["file_path"], r["define_name"]
        try:
            mod = callgraph.module_name_of(fp, root)
            fqn = callgraph._lang_fqn(fp, mod, dn)
        except Exception:
            fqn = dn
        det_fqn[r["id"]] = fqn
        if r["id"] > latest_det.get((fp, dn), -1):
            latest_det[(fp, dn)] = r["id"]

    # ── (D) gd predecessor adjacency (current-version cross_define edges only) ──
    gd_pred: dict[int, list[int]] = collections.defaultdict(list)
    for r in con.execute(
        "SELECT from_det, to_det FROM gd_edges "
        "WHERE project_id=?",
        (project_id,),
    ):
        gd_pred[r["to_det"]].append(r["from_det"])

    # ── (E) per-define computation — zero additional DB queries ───────────────
    concerns: list[dict] = []
    for (fp, dn), det_id in latest_det.items():
        try:
            mod = callgraph.module_name_of(fp, root)
            fqn = callgraph._lang_fqn(fp, mod, dn)
        except Exception:
            fqn = dn

        callers = callers_count.get(fqn, 0)
        callees = callees_count.get(fqn, 0)
        churn   = churn_map.get((fp, dn), 1)

        # BFS predecessor cone (only current-version cross_define edges in gd_pred)
        visited: set[int] = set()
        stack = list(gd_pred.get(det_id, []))
        while stack:
            cur = stack.pop()
            if cur not in visited:
                visited.add(cur)
                stack.extend(gd_pred.get(cur, []))
        # Deduplicate by FQN before summing. After old-version edge deletion
        # (reconcile supersede), each define has only its current-version det in
        # the cone, so this is normally a no-op. Kept as a defensive guard against
        # any missed old-version det edges leaking into the cone.
        cone_fqns: set[str] = {det_fqn[d] for d in visited if d in det_fqn}
        lambda_ = sum(callers_count.get(f, 0) for f in cone_fqns)

        hit_dims: list[str] = []
        if callers >= THETA_DEBT_CALLERS:
            hit_dims.append("callers")
        if callees >= THETA_DEBT_CALLEES:
            hit_dims.append("callees")
        if churn >= THETA_DEBT_CHURN:
            hit_dims.append("churn")
        if lambda_ >= THETA_DEBT_LAMBDA:
            hit_dims.append("lambda")

        if not hit_dims:
            continue

        concerns.append({
            "target_node": f"{fp}::{dn}",
            "define_name": dn,
            "callers":     callers,
            "callees":     callees,
            "churn":       churn,
            "lambda_":     lambda_,
            "hit_dims":    hit_dims,
        })

    concerns.sort(
        key=lambda c: (
            -len(c["hit_dims"]),
            -(c["callers"] + c["callees"] + c["churn"] + c["lambda_"]),
        )
    )
    return concerns[:top_n]


def _dim_description(c: dict) -> str:
    parts: list[str] = []
    if "callers" in c["hit_dims"]:
        parts.append(f"depended on by {c['callers']} locations (high-coupling hub)")
    if "callees" in c["hit_dims"]:
        parts.append(f"depends on {c['callees']} other modules (broad reach)")
    if "churn" in c["hit_dims"]:
        parts.append(f"modified {c['churn']} times (high churn)")
    if "lambda" in c["hit_dims"]:
        lam = c.get("lambda_", 0)
        parts.append(f"accumulated constraint load Λ={lam} (deep dependency chain; upstream changes propagate widely)")
    return ", ".join(parts)


def format_debt_section(concerns: list[dict]) -> str:
    """Format the '结构关注点' section for project_overview."""
    if not concerns:
        return ""
    lines = [
        "",
        "  structural concerns (based on coupling and churn; not a full SDT Λ implementation):",
        "  the following are structurally prominent and worth inspecting (high coupling / repeated churn); BUER does not judge whether they actually have problems"
        " — targeted review by agent or user is suggested. Covers only coupling and churn dimensions; semantic issues (naming / duplication / logic debt) require separate investigation.",
    ]
    for c in concerns:
        dim_desc = _dim_description(c)
        lines.append(
            f"    {c['define_name']}: {dim_desc}; worth checking for potential refactoring or bug fixes."
        )
    return "\n".join(lines)
