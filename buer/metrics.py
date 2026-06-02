"""BUER 𝒢_D structural metrics — ω, d_J, Γ_R, and connected components.

Implements SDT ancestor-cone and lateral-relation operations.
Pure functions — no DB writes, no signal logic.

  ω(u,v)   = |↓u ∩ ↓v|                                  [Math Ext §11.1b]
  d_J(u,v) = |anc(u)△anc(v)| / |anc(u)∪anc(v)|          [Math Ext §12.1.2]
  Γ_R(v)   = {u ≠ v | ∃w: v ∈ pred(w) ∧ u ∈ pred(w)}    [GD Def 7.1]
  CC(v)    = undirected connected component of v in 𝒢_D   [GD Prop 9.2]

where ↓v is the strict predecessor cone (not including v),
anc(v) = ↓v ∪ {v} (including v), and pred(w) is the set of DIRECT
(immediate) predecessors of w — not transitive closure.

Version-chain closed form [Math Ext Ex 12.1.6]:
  u ≺ v  →  d_J(u,v) = 1 − |anc(u)| / |anc(v)|
  Pure chain d_J(v1,v2)=1/2, d_J(v1,v3)=2/3, d_J(v1,v4)=3/4.

Runtime note: Γ_R and CC are structurally rigorous per the GD theorems.
The input graph (𝒢_D) is built from Python call-graph analysis which is
lossy — dynamic calls and runtime-dispatched methods are not captured
(§3.7 honest scope).  "运算严格、输入有损" — the math holds; the data
has gaps.  Do not read absence of a Γ_R edge as structural independence;
read it as "no static evidence of shared merger found."
"""
from __future__ import annotations

from buer.store import Store


# ── ↓v and anc(v) ─────────────────────────────────────────────────────────────

def strict_down(store: Store, project_id: int, det_id: int) -> frozenset[int]:
    """Strict predecessor cone ↓v = {u | u ≺ v}, not including v.  [Math Ext §11.1b]"""
    return store.gd_strict_ancestors(project_id, det_id)


def anc(store: Store, project_id: int, det_id: int) -> frozenset[int]:
    """Extended ancestor set anc(v) = ↓v ∪ {v}, including v.  [Math Ext §12.1.2]"""
    return store.gd_strict_ancestors(project_id, det_id) | {det_id}


# ── ω: shared strict ancestry ─────────────────────────────────────────────────

def omega(store: Store, project_id: int, u: int, v: int) -> int:
    """Shared strict predecessors ω(u,v) = |↓u ∩ ↓v|.  [Math Ext §11.1b]

    Uses strict ↓, NOT anc — ω(v,v) = 0 for any v (↓v ∩ ↓v = ↓v, but
    self-comparison returns |∅| = 0 only when v has no predecessors;
    use omega for lateral comparison, not self-distance).
    """
    return len(strict_down(store, project_id, u) & strict_down(store, project_id, v))


# ── d_J: Jaccard distance on ancestor sets ───────────────────────────────────

def d_J(store: Store, project_id: int, u: int, v: int) -> float:
    """Jaccard distance on extended ancestor sets.  [Math Ext §12.1.2]

    d_J(u,v) = |anc(u)△anc(v)| / |anc(u)∪anc(v)|

    Properties: symmetric, d_J(v,v)=0, antichain nodes → 1,
    version chain u≺v → 1 − |anc(u)|/|anc(v)|.
    """
    anc_u = anc(store, project_id, u)
    anc_v = anc(store, project_id, v)
    union = anc_u | anc_v
    if not union:
        return 0.0
    return len(anc_u ^ anc_v) / len(union)


# ── Γ_R: shared direct merger (GD Def 7.1) ───────────────────────────────────

def gamma_r_neighbors(store: Store, project_id: int, v: int) -> frozenset[int]:
    """Direct Γ_R neighbors of v — nodes sharing a common direct merger with v.

    [GD Def 7.1]  Edge {u,v} ∈ Γ_R iff ∃w such that v ∈ pred(w) ∧ u ∈ pred(w),
    where pred(w) = DIRECT (immediate) predecessors of w in 𝒢_D, not transitive
    closure.

    Algorithm:
      1. Find all direct successors w of v  (v ∈ pred(w))
      2. For each w: collect pred(w) (direct predecessors)
      3. Union, remove v itself → Γ_R direct neighbors

    Critical: pred(w) is direct only.  A node reachable via v→w→…→z is NOT
    a direct predecessor of z unless there is also a direct edge to z.
    This is what makes Γ_R structurally precise (not a transitive closure).

    Prop 7.2 note: path-connectivity in Γ_R is separately transitive;
    this function builds edges first, then gamma_r_connected handles paths.
    """
    neighbors: set[int] = set()
    for w in store.gd_successors(project_id, v):
        for u in store.gd_predecessors(project_id, w):
            if u != v:
                neighbors.add(u)
    return frozenset(neighbors)


def gamma_r_connected(
    store: Store,
    project_id: int,
    u: int,
    v: int,
    max_depth: int = 4,
) -> bool:
    """True if u and v are path-connected in Γ_R within max_depth hops.

    [GD Prop 7.2]  Path-connectivity in Γ_R is transitive: edges built by
    gamma_r_neighbors form a graph; this function checks reachability.

    max_depth limits BFS fan-out (§3.2: "受深度限制").  At depth=1 this is
    equivalent to checking u ∈ gamma_r_neighbors(v) (direct adjacency).
    """
    if u == v:
        return True
    visited: set[int] = {u}
    frontier: set[int] = {u}
    for _ in range(max_depth):
        next_frontier: set[int] = set()
        for node in frontier:
            for nb in gamma_r_neighbors(store, project_id, node):
                if nb == v:
                    return True
                if nb not in visited:
                    next_frontier.add(nb)
                    visited.add(nb)
        if not next_frontier:
            break
        frontier = next_frontier
    return False


# ── CC: undirected connected component (GD Prop 9.2) ─────────────────────────

def connected_component(store: Store, project_id: int, v: int) -> frozenset[int]:
    """Undirected connected component containing v in 𝒢_D.

    [GD Prop 9.2]  Traverses both predecessors and successors, ignoring edge
    direction.  Two nodes in different components are structurally causally
    independent — no shared ancestry (ω=0), no shared downstream production.

    Use for negative filtering: if a suspected root cause is in a different
    component from the stuck define, it cannot be structurally related.
    """
    visited: set[int] = {v}
    stack: list[int] = [v]
    while stack:
        cur = stack.pop()
        for nb in store.gd_predecessors(project_id, cur) + store.gd_successors(project_id, cur):
            if nb not in visited:
                visited.add(nb)
                stack.append(nb)
    return frozenset(visited)


def connected_components_all(store: Store, project_id: int) -> list[frozenset[int]]:
    """Global connected-component partition of the full project graph.  [GD Prop 9.2]

    Partitions ALL determination nodes into disjoint undirected connected
    components.  Single-pass DFS with a global visited set: O(V+E) total —
    each node is visited exactly once across all component traversals.

    CRITICAL: Do NOT call connected_component() per-node — that is O(V*(V+E))
    and will time out on large projects.  This function shares one global
    `visited` set so no node is processed twice.

    Returns a list of frozensets; each frozenset is a set of determination ids.
    Isolated nodes (no gd_edges) each form their own singleton component.

    Large-repo note: runs synchronously (one call per SessionStart, O(V+E)).
    If latency becomes a concern on 100k+ node graphs, cache the partition
    and invalidate on next reconcile.
    """
    rows = store.con.execute(
        "SELECT id FROM determinations WHERE project_id = ?",
        (project_id,),
    ).fetchall()
    all_nodes = [r["id"] for r in rows]

    if not all_nodes:
        return []

    visited: set[int] = set()
    components: list[frozenset[int]] = []

    for node in all_nodes:
        if node in visited:
            continue
        component: set[int] = set()
        stack: list[int] = [node]
        visited.add(node)
        component.add(node)
        while stack:
            cur = stack.pop()
            for nb in store.gd_predecessors(project_id, cur) + store.gd_successors(project_id, cur):
                if nb not in visited:
                    visited.add(nb)
                    component.add(nb)
                    stack.append(nb)
        components.append(frozenset(component))

    return components
