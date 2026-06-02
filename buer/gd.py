"""BUER GD-edge layer — §4.2a step 2: build_gd_edges.

Two edge classes (§3.3):
  cross_define_callgraph — callee's current version consumed by caller.
                           Dynamic-language approximation (call graph proxy).
                           Lossy: call ≠ data consumption (fire-and-forget calls
                           are included as spurious edges). Honest tag.
  cross_define_dataflow  — callee's current version consumed by caller.
                           TypeScript/TSX type-driven dataflow. Gain: eliminates
                           fire-and-forget false positives (ExpressionStatement
                           parent check). Residual limits: dynamic dispatch,
                           Proxy, string indexing still unresolvable (§3.7).

Language routing:
  Python / JavaScript / JSX  → call-graph (cross_define_callgraph)
  TypeScript / TSX            → dataflow via project's typescript package
                                (cross_define_dataflow) if toolchain available;
                                degrades to call-graph + user-channel告知 if not.
  Go / Rust / Java            → reserved in STATICALLY_TYPED; no dataflow impl yet;
                                fall through to call-graph (no告知, no崩溃).

Public API
----------
build_gd_edges(store, project_id, det, root, idx)
recompute_edges_for_define(store, project_id, file_path, define_name, root, idx, old_det_id=None)
"""
from __future__ import annotations

import json

from buer import callgraph, parse, ts_dataflow
from buer.store import Store

# Languages where dataflow track is reserved (§4.2a).
# "tsx" shares typescript's static-type semantics; only the parser grammar differs.
# Both identifiers must appear here so .tsx isn't silently routed to call-graph
# once the dataflow implementation is active.
STATICALLY_TYPED: frozenset[str] = frozenset({"go", "typescript", "tsx", "rust", "java"})

# Languages with active TS/TSX dataflow implementation (Unit B2).
# Go/Rust/Java are in STATICALLY_TYPED but not here; they fall through to call-graph.
_TS_DATAFLOW_LANGS: frozenset[str] = frozenset({"typescript", "tsx"})

# User-visible告知 signal name for missing TS toolchain (idempotent, one per project).
_TS_TOOLCHAIN_MISSING_SIGNAL = "ts_toolchain_missing"


def build_gd_edges(
    store: Store,
    project_id: int,
    det,              # sqlite3.Row (or dict-like) with id/file_path/define_name/seq
    root: str,
    idx: callgraph.SymbolIndex,
) -> None:
    """Insert GD edges for a just-recorded determination node.

    det must already be committed to the determinations table.
    All languages use call-graph approximation (cross_define_callgraph).
    Precise TS/TSX dataflow analysis is available on-demand via analyze_dangling().

    version_chain edges are no longer written. Version history is authoritative
    in the determinations table (store.version_chain()). Old-version det edges
    are deleted by reconcile on each supersede (delete_gd_edges_for_det).
    """
    # Cross-define edge: all languages use call-graph approximation.
    # TS/TSX dataflow (tsc-based) is preserved in _build_ts_cross_edges but no longer
    # called here — it runs on-demand via the analyze_dangling MCP tool (~1.5s/file).
    _build_callgraph_cross_edges(store, project_id, det, root, idx, "cross_define_callgraph")


def recompute_edges_for_define(
    store: Store,
    project_id: int,
    file_path: str,
    define_name: str,
    root: str,
    idx: callgraph.SymbolIndex,
    old_det_id: int | None = None,
) -> None:
    """Unified edge recompute for a single define: delete old edges, rebuild out+in.

    Called by reconcile Phase 2 for every create/modify determination and by
    deletion paths (where the current det is the delete record — no rebuild).

    Outbound (det is consumer, to_det=det):
      build_gd_edges(current_det) handles this — iterates callees_of(det's FQN).

    Inbound (det is producer, from_det=det):
      Cannot be built by build_gd_edges(det) alone — edges live on the caller's det.
      Fix: query callers_of(det's FQN) → for each caller_fqn, resolve its current det
      and call build_gd_edges(caller_det). INSERT OR IGNORE means existing valid edges
      survive; only the new from_det=current_det edge is added.

    Deletion path: call with is_deleted=True (no current det, no rebuild).
    The caller should pass old_det_id=None and ensure all historical det edges are
    already deleted via all_determinations_for_define before calling.
    """
    # Resolve current (post-insert) det for this define.
    current_det = store.current_version_determination(project_id, file_path, define_name)

    # Delete old_det edges (supersede case: modify replaces prior create/modify det).
    if old_det_id is not None:
        store.delete_gd_edges_for_det(project_id, old_det_id)

    # No current living det (deletion): nothing to rebuild.
    if current_det is None or current_det["edit_type"] == "delete":
        return

    # Outbound edges: build_gd_edges covers callees_of(det's FQN) → from_det=callee, to_det=det.
    build_gd_edges(store, project_id, current_det, root, idx)

    # Inbound edges: find callers of this define via call_edges, rebuild each caller's edges.
    # This ensures from_det=current_det edges exist even when caller files aren't re-reconciled.
    caller_fqn = callgraph._lang_fqn(
        file_path,
        callgraph.module_name_of(file_path, root),
        define_name,
    )
    for caller_sym in store.callers_of(project_id, caller_fqn):
        # caller_sym is a lang-prefixed FQN like "py::pkg.mod.fn"
        if caller_sym not in idx.loc:
            continue
        caller_file, caller_def = idx.loc[caller_sym]
        caller_det = store.current_version_determination(project_id, caller_file, caller_def)
        if caller_det is None or caller_det["edit_type"] == "delete":
            continue
        # Skip if same det (shouldn't happen since self-loops are guarded in build_gd_edges,
        # but defensive check avoids a spurious cycle guard traversal).
        if caller_det["id"] == current_det["id"]:
            continue
        build_gd_edges(store, project_id, caller_det, root, idx)


# ── TS/TSX dataflow branch ─────────────────────────────────────────────────────

def _build_ts_cross_edges(
    store: Store,
    project_id: int,
    det,
    root: str,
    idx: callgraph.SymbolIndex,
) -> list:
    """Build cross_define_dataflow edges for a TS/TSX define, with callgraph fallback.

    Returns unresolved_calls list when toolchain is available and analysis succeeds;
    empty list on toolchain missing or degraded (§2.8: no dangling detection without TS).
    """
    toolchain = ts_dataflow.detect_ts_toolchain(root)

    if not toolchain["available"]:
        _warn_ts_toolchain_missing(store, project_id, toolchain["reason"])
        _build_callgraph_cross_edges(
            store, project_id, det, root, idx, "cross_define_callgraph"
        )
        return []

    result = ts_dataflow.run_ts_dataflow_analysis(
        root, det["file_path"], det["define_name"]
    )

    if result["degraded"]:
        # Script crashed or timed out; fall through to call-graph silently.
        # Errors are in result["errors"] but we don't escalate transient script failures.
        _build_callgraph_cross_edges(
            store, project_id, det, root, idx, "cross_define_callgraph"
        )
        return []

    for edge in result["edges"]:
        producer_file = edge.get("producer_file", "")
        producer_define = edge.get("producer_define", "")
        if not producer_file or not producer_define:
            continue
        producer = store.current_version_determination(
            project_id, producer_file, producer_define
        )
        if producer is None:
            continue
        # Guard 1: skip self-loop
        if producer["id"] == det["id"]:
            continue
        # Guard 2: skip cycle-forming edges (𝒢_D must be a DAG — §3.3)
        if store.gd_is_ancestor(project_id, anc_id=det["id"], desc_id=producer["id"]):
            continue
        store.insert_gd_edge(
            project_id,
            from_det=producer["id"],
            to_det=det["id"],
            edge_class="cross_define_dataflow",
        )

    return result.get("unresolved_calls", [])


# ── call-graph cross-define path (Python/JS and fallback) ────────────────────

def _build_callgraph_cross_edges(
    store: Store,
    project_id: int,
    det,
    root: str,
    idx: callgraph.SymbolIndex,
    edge_class: str,
) -> None:
    """Build cross_define_callgraph edges using the call-graph archive."""
    caller_fqn = callgraph._lang_fqn(
        det['file_path'],
        callgraph.module_name_of(det['file_path'], root),
        det['define_name'],
    )
    for callee_fqn in store.callees_of(project_id, caller_fqn):
        if callee_fqn not in idx.loc:
            continue  # external lib or unresolved — no edge
        f_path, def_name = idx.loc[callee_fqn]
        producer = store.current_version_determination(project_id, f_path, def_name)
        if producer is None:
            continue
        # Guard 1: skip self-loop
        if producer["id"] == det["id"]:
            continue
        # Guard 2: skip cycle-forming edges (𝒢_D must be a DAG — §3.3)
        if store.gd_is_ancestor(project_id, anc_id=det["id"], desc_id=producer["id"]):
            continue
        store.insert_gd_edge(
            project_id,
            from_det=producer["id"],
            to_det=det["id"],
            edge_class=edge_class,
        )


# ── TS toolchain degradation notice ───────────────────────────────────────────

def _warn_ts_toolchain_missing(store: Store, project_id: int, reason: str) -> None:
    """Create a one-time user-channel advisory about missing TS toolchain.

    Idempotent: creates at most one open incident per project.
    Escalated directly to the user channel (escalated_user state).
    """
    existing = [
        i for i in store.open_incidents(project_id)
        if i["signal"] == _TS_TOOLCHAIN_MISSING_SIGNAL
    ]
    if existing:
        return

    msg = (
        "检测到 TS 项目但未找到 TS 工具链"
        f"（原因：{reason}；需 tsconfig.json + node_modules/typescript + node 可执行）。"
        "跨 define 关系已用调用图近似（cross_define_callgraph）；"
        "安装 TypeScript（npm i -D typescript）后可获精确数据流分析"
        "（消除 fire-and-forget 调用的假边，debug 根因方向更准）。"
    )
    inc_id = store.write_incident(
        project_id,
        _TS_TOOLCHAIN_MISSING_SIGNAL,
        target_node=None,
        details=json.dumps({"reason": reason, "message": msg}),
    )
    # Go directly to escalated_user — this is a system advisory, not a code quality signal.
    store.update_incident(inc_id, state="escalated_user")
