"""BUER influence layer — successor cone + call-edges caller cone.

Two complementary views of "what is affected by a change":

  successor_cone / influence_cone_of_changes
    → Follows gd_edges (from_det → to_det).  Accurate for historical analysis
      (e.g., "which past determinations consumed this version's output").
      Returns det_ids.  NOT suitable for newly-created dets that have no
      outgoing gd_edges yet (gd_edges only grow when callers are re-determined).

  caller_cone_fqns / caller_cone_with_depth / caller_cone_for_debug
    → Follows call_edges (static caller map, built during reconcile).
      Answers "who calls X transitively RIGHT NOW" regardless of edit history.
      Returns FQN strings.  Correct tool for debug_range and blast-radius queries.

  dependency_cone_size / cone_priority_ranks
    → Three-dimensional priority ranking for debug triage: δ (integration
      complexity), in-degree (blast radius), distance (proximity to change).

  intersect_cone_with_stack
    → Set intersection of influence-cone FQNs ∩ crash-stack FQNs.
      Returns the precise suspects: changed blast-radius ∩ actual crash path.

Public API
----------
successor_cone(store, project_id, start_det) -> set[int]
influence_cone_of_changes(store, project_id, det_ids) -> set[int]
caller_cone_fqns(store, project_id, seed_fqns) -> set[str]
caller_cone_with_depth(store, project_id, seed_fqns) -> dict[str, int]
caller_cone_for_debug(store, project_id, root, changes) -> list[str]
dependency_cone_size(store, project_id, fqn) -> int
cone_priority_ranks(store, project_id, cone_with_depth, top_n) -> dict
intersect_cone_with_stack(cone_fqns, stack_fqns) -> set[str]
"""
from __future__ import annotations

from collections import deque

from buer.store import Store


# ── gd_edges successor cone (historical) ─────────────────────────────────────

def successor_cone(store: Store, project_id: int, start_det: int) -> set[int]:
    """BFS transitive closure of successors from start_det (via gd_edges).

    Returns reachable det_ids NOT including start_det itself.
    Only meaningful for dets that already have outgoing gd_edges (i.e., callers
    were re-determined after this det was created).  For real-time debug use
    caller_cone_fqns / caller_cone_for_debug instead.
    """
    visited: set[int] = {start_det}
    q: deque[int] = deque([start_det])
    while q:
        cur = q.popleft()
        for succ in store.gd_successors(project_id, cur):
            if succ not in visited:
                visited.add(succ)
                q.append(succ)
    return visited - {start_det}


def influence_cone_of_changes(
    store: Store, project_id: int, det_ids: list[int]
) -> set[int]:
    """Union of gd_edges successor cones for a set of determinations.

    Returns downstream det_ids affected but NOT in the input set.
    Note: returns empty for freshly-created dets (no outgoing gd_edges yet).
    Use caller_cone_for_debug for session-level debug queries.
    """
    changed = set(det_ids)
    union: set[int] = set()
    for d in det_ids:
        union |= successor_cone(store, project_id, d)
    return union - changed


# ── call_edges caller cone (current, for debug) ───────────────────────────────

def caller_cone_fqns(
    store: Store, project_id: int, seed_fqns: list[str]
) -> set[str]:
    """BFS transitive callers of seed_fqns via call_edges.

    Returns caller FQNs NOT including the seeds themselves.
    call_edges are current (rebuilt by reconcile after each file change), so
    this always reflects the live codebase state — even for freshly-modified dets.
    """
    seeds = set(seed_fqns)
    visited: set[str] = set(seeds)
    q: deque[str] = deque(seeds)
    while q:
        fqn = q.popleft()
        rows = store.con.execute(
            "SELECT caller FROM call_edges WHERE project_id=? AND callee=?",
            (project_id, fqn),
        ).fetchall()
        for r in rows:
            c = r["caller"]
            if c not in visited:
                visited.add(c)
                q.append(c)
    return visited - seeds


def caller_cone_with_depth(
    store: Store, project_id: int, seed_fqns: list[str]
) -> dict[str, int]:
    """Multi-source BFS: {caller_fqn: min_depth} for all transitive callers.

    Seeds are at depth 0 (excluded from result).
    Depth = minimum BFS hops from any seed to reach that node.
    """
    if not seed_fqns:
        return {}
    seeds = set(seed_fqns)
    visited: dict[str, int] = {s: 0 for s in seeds}
    q: deque[tuple[str, int]] = deque((s, 0) for s in seeds)
    while q:
        fqn, depth = q.popleft()
        rows = store.con.execute(
            "SELECT caller FROM call_edges WHERE project_id=? AND callee=?",
            (project_id, fqn),
        ).fetchall()
        for r in rows:
            c = r["caller"]
            if c not in visited:
                visited[c] = depth + 1
                q.append((c, depth + 1))
    return {fqn: d for fqn, d in visited.items() if fqn not in seeds}


def dependency_cone_size(store: Store, project_id: int, fqn: str) -> int:
    """BFS on callee direction: count transitive dependencies of fqn (δ).

    Returns number of reachable callees, not including fqn itself.
    """
    visited: set[str] = {fqn}
    q: deque[str] = deque([fqn])
    while q:
        cur = q.popleft()
        rows = store.con.execute(
            "SELECT callee FROM call_edges WHERE project_id=? AND caller=?",
            (project_id, cur),
        ).fetchall()
        for r in rows:
            c = r["callee"]
            if c not in visited:
                visited.add(c)
                q.append(c)
    return len(visited) - 1


def cone_priority_ranks(
    store: Store,
    project_id: int,
    cone_with_depth: dict[str, int],
    top_n: int = 5,
) -> dict:
    """Three priority rankings for influence cone nodes.

    Returns {"delta": [...], "indegree": [...], "distance": [...]},
    each a list of (fqn, value) tuples, length ≤ top_n.
    delta sorted desc (most deps first), indegree sorted desc (most callers first),
    distance sorted asc (closest to changed define first).
    """
    if not cone_with_depth:
        return {"delta": [], "indegree": [], "distance": []}

    fqns = list(cone_with_depth.keys())

    # δ: BFS per node on callee direction
    delta_vals = [(fqn, dependency_cone_size(store, project_id, fqn)) for fqn in fqns]
    delta_top = sorted(delta_vals, key=lambda x: x[1], reverse=True)[:top_n]

    # in-degree: batch SQL (SQLite ≤999 params; chunk conservatively at 500)
    indegree_map: dict[str, int] = {fqn: 0 for fqn in fqns}
    _CHUNK = 500
    for i in range(0, len(fqns), _CHUNK):
        chunk = fqns[i : i + _CHUNK]
        placeholders = ",".join("?" * len(chunk))
        rows = store.con.execute(
            f"SELECT callee, COUNT(DISTINCT caller) AS cnt "
            f"FROM call_edges WHERE project_id=? AND callee IN ({placeholders}) "
            f"GROUP BY callee",
            (project_id, *chunk),
        ).fetchall()
        for r in rows:
            indegree_map[r["callee"]] = r["cnt"]
    indegree_top = sorted(indegree_map.items(), key=lambda x: x[1], reverse=True)[:top_n]

    # distance: sort ascending (closest = lowest BFS depth from any seed)
    distance_top = sorted(cone_with_depth.items(), key=lambda x: x[1])[:top_n]

    return {
        "delta": delta_top,
        "indegree": indegree_top,
        "distance": distance_top,
    }


def intersect_cone_with_stack(
    cone_fqns: set[str], stack_fqns: set[str]
) -> set[str]:
    """Intersection of influence-cone FQNs with crash-stack FQNs.

    Returns FQNs that are (a) in the recent-change blast radius AND
    (b) present on the actual crash path.  These are the precise suspects.
    Empty set means the crash path and the change cone don't overlap.
    """
    return cone_fqns & stack_fqns


def caller_cone_for_debug(
    store: Store, project_id: int, root: str, changes: list
) -> list[str]:
    """Transitive callers of changed defines, ready for display.

    changes: list of sqlite3.Row from changes_in_range (file_path, define_name).
    Returns deduplicated FQN strings like "lib/db/postgres.getPool", sorted.
    Excludes the changed defines themselves.
    """
    from buer import callgraph

    changed_fqns: set[str] = set()
    for r in changes:
        if r["define_name"]:
            mod = callgraph.module_name_of(r["file_path"], root)
            changed_fqns.add(callgraph._lang_fqn(r["file_path"], mod, r["define_name"]))

    if not changed_fqns:
        return []

    callers = caller_cone_fqns(store, project_id, list(changed_fqns))
    return sorted(callers)
