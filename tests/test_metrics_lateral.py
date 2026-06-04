"""Tests for Γ_R, connected_component, and three-dimensional lateral context.

Covers:
  - gamma_r_neighbors: strict pred(w) semantics (direct predecessors only)
  - gamma_r_connected: BFS path-connectivity over Γ_R edges
  - connected_component: undirected 𝒢_D reachability
  - Causal independence: different components ↔ no shared 𝒢_D path
  - The "indirect but not Γ_R adjacent" case: A→B→C←D, A not Γ_R neighbor of D
  - _lateral_context: all three dimensions populated correctly
  - _direction_note: priority cascade over three dimensions
  - _lateral_lines: renders both shared_ancestry and gamma_r
"""
import json

import pytest

from buer.store import Store
from buer import metrics, signals, delivery


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def store_and_project(tmp_path):
    store = Store(":memory:")
    pid = store.get_or_create_project(str(tmp_path))
    return store, pid


# ── helpers ───────────────────────────────────────────────────────────────────

def _det(store, pid, seq, *, file_path="/f.py", define_name="fn", fp=None):
    return store.insert_determination(
        pid, seq=seq, file_path=file_path,
        define_name=define_name,
        node_fingerprint=fp or f"fp{seq}",
        edit_type="create",
    )


def _edge(store, pid, from_id, to_id):
    store.insert_gd_edge(pid, from_id, to_id, "version_chain")


# ═══════════════════════════════════════════════════════════════════════════════
# TestGammaRNeighbors — GD Def 7.1 strict pred(w) semantics
# ═══════════════════════════════════════════════════════════════════════════════

class TestGammaRNeighbors:
    def test_two_nodes_sharing_merger_are_neighbors(self, store_and_project):
        """X→W←Y: X and Y both in pred(W), so they are Γ_R neighbors."""
        store, pid = store_and_project
        x = _det(store, pid, 1, define_name="x")
        y = _det(store, pid, 2, define_name="y")
        w = _det(store, pid, 3, define_name="w")
        _edge(store, pid, x, w)   # x ∈ pred(w)
        _edge(store, pid, y, w)   # y ∈ pred(w)

        assert y in metrics.gamma_r_neighbors(store, pid, x)
        assert x in metrics.gamma_r_neighbors(store, pid, y)

    def test_no_neighbors_when_no_shared_merger(self, store_and_project):
        """X→W, Y→V (different mergers): X and Y are not Γ_R neighbors."""
        store, pid = store_and_project
        x = _det(store, pid, 1, define_name="x")
        y = _det(store, pid, 2, define_name="y")
        w = _det(store, pid, 3, define_name="w")
        v = _det(store, pid, 4, define_name="v")
        _edge(store, pid, x, w)
        _edge(store, pid, y, v)

        assert y not in metrics.gamma_r_neighbors(store, pid, x)
        assert x not in metrics.gamma_r_neighbors(store, pid, y)

    def test_self_excluded_from_neighbors(self, store_and_project):
        """A node is never its own Γ_R neighbor."""
        store, pid = store_and_project
        x = _det(store, pid, 1, define_name="x")
        y = _det(store, pid, 2, define_name="y")
        w = _det(store, pid, 3, define_name="w")
        _edge(store, pid, x, w)
        _edge(store, pid, y, w)

        assert x not in metrics.gamma_r_neighbors(store, pid, x)

    def test_isolated_node_has_no_neighbors(self, store_and_project):
        store, pid = store_and_project
        x = _det(store, pid, 1, define_name="x")
        assert metrics.gamma_r_neighbors(store, pid, x) == frozenset()

    def test_three_way_merger(self, store_and_project):
        """X→W, Y→W, Z→W: X, Y, Z all mutually Γ_R neighbors."""
        store, pid = store_and_project
        x = _det(store, pid, 1, define_name="x")
        y = _det(store, pid, 2, define_name="y")
        z = _det(store, pid, 3, define_name="z")
        w = _det(store, pid, 4, define_name="w")
        _edge(store, pid, x, w)
        _edge(store, pid, y, w)
        _edge(store, pid, z, w)

        assert y in metrics.gamma_r_neighbors(store, pid, x)
        assert z in metrics.gamma_r_neighbors(store, pid, x)
        assert x in metrics.gamma_r_neighbors(store, pid, y)
        assert z in metrics.gamma_r_neighbors(store, pid, y)

    def test_strict_pred_not_transitive_closure(self, store_and_project):
        """KEY TEST: A→B→C←D — A is indirect predecessor of C, not direct.

        pred(C) = {B, D} — A is NOT in pred(C).
        Therefore A is NOT a Γ_R neighbor of D (they don't share a direct merger).
        B and D ARE Γ_R neighbors (both in pred(C)).
        """
        store, pid = store_and_project
        a = _det(store, pid, 1, define_name="a")
        b = _det(store, pid, 2, define_name="b")
        c = _det(store, pid, 3, define_name="c")
        d = _det(store, pid, 4, define_name="d")
        _edge(store, pid, a, b)   # A→B (A direct pred of B)
        _edge(store, pid, b, c)   # B→C (B direct pred of C)
        _edge(store, pid, d, c)   # D→C (D direct pred of C)

        # A is NOT a Γ_R neighbor of D (A not in pred(C))
        assert d not in metrics.gamma_r_neighbors(store, pid, a)
        assert a not in metrics.gamma_r_neighbors(store, pid, d)

        # B and D ARE Γ_R neighbors (both in pred(C))
        assert d in metrics.gamma_r_neighbors(store, pid, b)
        assert b in metrics.gamma_r_neighbors(store, pid, d)

        # A has no Γ_R neighbors: A's only successor is B, and pred(B)={A} only
        assert metrics.gamma_r_neighbors(store, pid, a) == frozenset()

    def test_multiple_mergers_union(self, store_and_project):
        """X→W1, X→W2, Y→W1, Z→W2: X neighbors include both Y and Z."""
        store, pid = store_and_project
        x  = _det(store, pid, 1, define_name="x")
        y  = _det(store, pid, 2, define_name="y")
        z  = _det(store, pid, 3, define_name="z")
        w1 = _det(store, pid, 4, define_name="w1")
        w2 = _det(store, pid, 5, define_name="w2")
        _edge(store, pid, x, w1)
        _edge(store, pid, y, w1)
        _edge(store, pid, x, w2)
        _edge(store, pid, z, w2)

        nb = metrics.gamma_r_neighbors(store, pid, x)
        assert y in nb
        assert z in nb


# ═══════════════════════════════════════════════════════════════════════════════
# TestGammaRConnected — GD Prop 7.2 path-connectivity
# ═══════════════════════════════════════════════════════════════════════════════

class TestGammaRConnected:
    def test_self_connected(self, store_and_project):
        store, pid = store_and_project
        x = _det(store, pid, 1, define_name="x")
        assert metrics.gamma_r_connected(store, pid, x, x)

    def test_direct_neighbors_connected_at_depth_1(self, store_and_project):
        """X→W←Y: X and Y connected at depth ≥ 1."""
        store, pid = store_and_project
        x = _det(store, pid, 1, define_name="x")
        y = _det(store, pid, 2, define_name="y")
        w = _det(store, pid, 3, define_name="w")
        _edge(store, pid, x, w)
        _edge(store, pid, y, w)

        assert metrics.gamma_r_connected(store, pid, x, y, max_depth=1)

    def test_two_hop_connected_at_depth_2(self, store_and_project):
        """A-B via W1, B-C via W2: A and C connected at depth 2.

        A→W1←B, B→W2←C.
        gamma_r_neighbors(A) = {B}
        gamma_r_neighbors(B) = {A, C}
        So A and C are 2-hop Γ_R connected.
        """
        store, pid = store_and_project
        a  = _det(store, pid, 1, define_name="a")
        b  = _det(store, pid, 2, define_name="b")
        c  = _det(store, pid, 3, define_name="c")
        w1 = _det(store, pid, 4, define_name="w1")
        w2 = _det(store, pid, 5, define_name="w2")
        _edge(store, pid, a, w1)
        _edge(store, pid, b, w1)   # A-B via W1
        _edge(store, pid, b, w2)
        _edge(store, pid, c, w2)   # B-C via W2

        assert metrics.gamma_r_connected(store, pid, a, c, max_depth=2)
        assert not metrics.gamma_r_connected(store, pid, a, c, max_depth=1)  # not direct

    def test_indirect_but_not_gammar_connected_is_false(self, store_and_project):
        """A→B→C←D: A and D not Γ_R connected (strict pred test).

        A→B: A's only successor B has pred={A}, so A has no Γ_R neighbors.
        B→C←D: B and D are Γ_R neighbors via C.
        A is not reachable from D via Γ_R edges.
        """
        store, pid = store_and_project
        a = _det(store, pid, 1, define_name="a")
        b = _det(store, pid, 2, define_name="b")
        c = _det(store, pid, 3, define_name="c")
        d = _det(store, pid, 4, define_name="d")
        _edge(store, pid, a, b)
        _edge(store, pid, b, c)
        _edge(store, pid, d, c)

        # A has no Γ_R neighbors → not connected to anything
        assert not metrics.gamma_r_connected(store, pid, a, d, max_depth=4)

    def test_not_connected_no_shared_mergers(self, store_and_project):
        store, pid = store_and_project
        x = _det(store, pid, 1, define_name="x")
        y = _det(store, pid, 2, define_name="y")
        # No edges at all
        assert not metrics.gamma_r_connected(store, pid, x, y, max_depth=4)

    def test_depth_zero_only_self(self, store_and_project):
        store, pid = store_and_project
        x = _det(store, pid, 1, define_name="x")
        y = _det(store, pid, 2, define_name="y")
        w = _det(store, pid, 3, define_name="w")
        _edge(store, pid, x, w)
        _edge(store, pid, y, w)

        # gamma_r_connected(x, y, max_depth=0) — loop body never runs
        assert not metrics.gamma_r_connected(store, pid, x, y, max_depth=0)


# ═══════════════════════════════════════════════════════════════════════════════
# TestConnectedComponent — GD Prop 9.2 undirected reachability
# ═══════════════════════════════════════════════════════════════════════════════

class TestConnectedComponent:
    def test_isolated_node_is_singleton(self, store_and_project):
        store, pid = store_and_project
        x = _det(store, pid, 1, define_name="x")
        comp = metrics.connected_component(store, pid, x)
        assert comp == frozenset({x})

    def test_chain_all_in_same_component(self, store_and_project):
        """A→B→C: all three are in the same undirected component."""
        store, pid = store_and_project
        a = _det(store, pid, 1, define_name="a")
        b = _det(store, pid, 2, define_name="b")
        c = _det(store, pid, 3, define_name="c")
        _edge(store, pid, a, b)
        _edge(store, pid, b, c)

        comp = metrics.connected_component(store, pid, a)
        assert a in comp
        assert b in comp
        assert c in comp

    def test_two_independent_chains_different_components(self, store_and_project):
        """A→B and C→D: {A,B} and {C,D} are different components."""
        store, pid = store_and_project
        a = _det(store, pid, 1, define_name="a")
        b = _det(store, pid, 2, define_name="b")
        c = _det(store, pid, 3, define_name="c")
        d = _det(store, pid, 4, define_name="d")
        _edge(store, pid, a, b)
        _edge(store, pid, c, d)

        comp_a = metrics.connected_component(store, pid, a)
        comp_c = metrics.connected_component(store, pid, c)

        assert a in comp_a and b in comp_a
        assert c not in comp_a and d not in comp_a
        assert c in comp_c and d in comp_c
        assert a not in comp_c and b not in comp_c

    def test_component_traverses_upstream(self, store_and_project):
        """Component includes predecessors (traverses backward)."""
        store, pid = store_and_project
        a = _det(store, pid, 1, define_name="a")
        b = _det(store, pid, 2, define_name="b")
        _edge(store, pid, a, b)

        # Start from b, should reach a going backward
        comp = metrics.connected_component(store, pid, b)
        assert a in comp

    def test_causal_independence_different_components(self, store_and_project):
        """Two nodes in different components are causally independent."""
        store, pid = store_and_project
        x = _det(store, pid, 1, define_name="x")
        y = _det(store, pid, 2, define_name="y")

        comp_x = metrics.connected_component(store, pid, x)
        comp_y = metrics.connected_component(store, pid, y)

        # No shared nodes → causally independent (§ Prop 9.2)
        assert comp_x & comp_y == frozenset()

    def test_fork_topology_all_connected(self, store_and_project):
        """A→B, A→C (fork): A, B, C all in same component."""
        store, pid = store_and_project
        a = _det(store, pid, 1, define_name="a")
        b = _det(store, pid, 2, define_name="b")
        c = _det(store, pid, 3, define_name="c")
        _edge(store, pid, a, b)
        _edge(store, pid, a, c)

        comp = metrics.connected_component(store, pid, b)
        assert a in comp and c in comp

    def test_diamond_topology_all_connected(self, store_and_project):
        """A→B, A→C, B→D, C→D: all four in same component."""
        store, pid = store_and_project
        a = _det(store, pid, 1, define_name="a")
        b = _det(store, pid, 2, define_name="b")
        c = _det(store, pid, 3, define_name="c")
        d = _det(store, pid, 4, define_name="d")
        _edge(store, pid, a, b)
        _edge(store, pid, a, c)
        _edge(store, pid, b, d)
        _edge(store, pid, c, d)

        comp = metrics.connected_component(store, pid, a)
        assert {a, b, c, d} == set(comp)


# ═══════════════════════════════════════════════════════════════════════════════
# TestLateralContextIntegration — _lateral_context returns all three dimensions
# ═══════════════════════════════════════════════════════════════════════════════

class TestLateralContextIntegration:
    def test_gamma_r_in_lateral_when_edges_present(self, store_and_project, tmp_path):
        """Two defines whose determinations share a merger → gamma_r in lateral."""
        store, pid = store_and_project
        root = str(tmp_path)

        # Insert two defines
        file_a = str(tmp_path / "a.py")
        file_b = str(tmp_path / "b.py")
        file_w = str(tmp_path / "w.py")
        for f in (file_a, file_b, file_w):
            (tmp_path / f.split("/")[-1]).write_text("def fn(): pass\n")

        det_a = _det(store, pid, 1, file_path=file_a, define_name="fn_a")
        det_b = _det(store, pid, 2, file_path=file_b, define_name="fn_b")
        det_w = _det(store, pid, 3, file_path=file_w, define_name="fn_w")
        _edge(store, pid, det_a, det_w)   # fn_a → fn_w
        _edge(store, pid, det_b, det_w)   # fn_b → fn_w

        from buer import callgraph
        idx = callgraph.SymbolIndex()

        lateral = signals._lateral_context(store, pid, file_a, "fn_a", root, idx)
        assert "gamma_r" in lateral
        gr_defines = [g["define"] for g in lateral["gamma_r"]]
        # fn_b should appear as a Γ_R neighbor of fn_a
        assert any("fn_b" in d for d in gr_defines)

    def test_component_size_in_lateral(self, store_and_project, tmp_path):
        """component_size is always present in lateral."""
        store, pid = store_and_project
        root = str(tmp_path)
        file_a = str(tmp_path / "a.py")
        (tmp_path / "a.py").write_text("def fn_a(): pass\n")

        _det(store, pid, 1, file_path=file_a, define_name="fn_a")

        from buer import callgraph
        idx = callgraph.SymbolIndex()
        lateral = signals._lateral_context(store, pid, file_a, "fn_a", root, idx)
        assert "component_size" in lateral
        assert lateral["component_size"] >= 1

    def test_no_gamma_r_when_no_merger_edges(self, store_and_project, tmp_path):
        """No gd_edges → gamma_r absent from lateral (not an empty list)."""
        store, pid = store_and_project
        root = str(tmp_path)
        file_a = str(tmp_path / "a.py")
        (tmp_path / "a.py").write_text("def fn_a(): pass\n")

        _det(store, pid, 1, file_path=file_a, define_name="fn_a")

        from buer import callgraph
        idx = callgraph.SymbolIndex()
        lateral = signals._lateral_context(store, pid, file_a, "fn_a", root, idx)
        assert "gamma_r" not in lateral

    def test_recently_modified_flag_set_in_window(self, store_and_project, tmp_path):
        """Γ_R neighbor determined in same edit window → recently_modified=True."""
        store, pid = store_and_project
        root = str(tmp_path)

        file_a = str(tmp_path / "a.py")
        file_b = str(tmp_path / "b.py")
        file_w = str(tmp_path / "w.py")
        for fname in ("a.py", "b.py", "w.py"):
            (tmp_path / fname).write_text("def fn(): pass\n")

        # fn_a has a version chain starting at seq=1
        det_a1 = _det(store, pid, 1, file_path=file_a, define_name="fn_a", fp="fp1")
        det_a2 = _det(store, pid, 3, file_path=file_a, define_name="fn_a", fp="fp2")
        _edge(store, pid, det_a1, det_a2)  # version chain

        # fn_b was determined at seq=2 (within edit window [1..3])
        det_b = _det(store, pid, 2, file_path=file_b, define_name="fn_b")
        det_w = _det(store, pid, 4, file_path=file_w, define_name="fn_w")
        _edge(store, pid, det_a2, det_w)
        _edge(store, pid, det_b, det_w)

        from buer import callgraph
        idx = callgraph.SymbolIndex()
        lateral = signals._lateral_context(store, pid, file_a, "fn_a", root, idx)

        gamma_r = lateral.get("gamma_r") or []
        fn_b_entry = next((g for g in gamma_r if "fn_b" in g["define"]), None)
        assert fn_b_entry is not None
        assert fn_b_entry["recently_modified"] is True


# ═══════════════════════════════════════════════════════════════════════════════
# TestDirectionNoteThreeDimensions — priority cascade
# ═══════════════════════════════════════════════════════════════════════════════

class TestDirectionNoteThreeDimensions:
    def test_callee_and_gamma_r_recent_highest_priority(self):
        lateral = {
            "gamma_r": [{"define": "b.hub", "recently_modified": True}],
            "shared_ancestry": [{"define": "c.dep", "omega": 3}],
        }
        note = signals._direction_note("fn", ["a.dep"], lateral)
        assert "a.dep" in note
        assert "b.hub" in note
        assert "two converging signals" in note

    def test_callee_only_second_priority(self):
        lateral = {"gamma_r": [], "shared_ancestry": []}
        note = signals._direction_note("fn", ["a.dep"], lateral)
        assert "a.dep" in note
        assert "change window" in note
        assert "two converging signals" not in note

    def test_gamma_r_recent_third_priority(self):
        lateral = {
            "gamma_r": [{"define": "b.hub", "recently_modified": True}],
            "shared_ancestry": [],
        }
        note = signals._direction_note("fn", [], lateral)
        assert "b.hub" in note
        assert "Γ_R" in note
        assert "recently modified" in note

    def test_gamma_r_no_time_fourth_priority(self):
        lateral = {
            "gamma_r": [{"define": "b.hub", "recently_modified": False}],
            "shared_ancestry": [],
        }
        note = signals._direction_note("fn", [], lateral)
        assert "b.hub" in note
        assert "Γ_R" in note

    def test_omega_only_fifth_priority(self):
        lateral = {
            "shared_ancestry": [{"define": "c.dep", "omega": 5}],
        }
        note = signals._direction_note("fn", [], lateral)
        assert "c.dep" in note
        assert "ω>0" in note

    def test_fallback_when_all_empty(self):
        note = signals._direction_note("fn", [], {})
        assert "fn" in note
        assert len(note) > 10  # not empty

    def test_gamma_r_recent_beats_omega_alone(self):
        """Γ_R recent neighbors outrank pure ω when no callees modified."""
        lateral = {
            "gamma_r": [{"define": "b.hub", "recently_modified": True}],
            "shared_ancestry": [{"define": "c.dep", "omega": 10}],
        }
        note = signals._direction_note("fn", [], lateral)
        # Should use Γ_R recent, not ω
        assert "b.hub" in note
        assert "recently modified" in note


# ═══════════════════════════════════════════════════════════════════════════════
# TestLateralLinesRendering — delivery._lateral_lines
# ═══════════════════════════════════════════════════════════════════════════════

class TestLateralLinesRendering:
    def test_renders_shared_ancestry(self):
        details = {"lateral": {
            "shared_ancestry": [{"define": "a.fn", "omega": 3.0}],
        }}
        lines = delivery._lateral_lines(details)
        assert any("Shared structural ancestors" in l for l in lines)
        assert any("a.fn" in l for l in lines)

    def test_renders_gamma_r(self):
        details = {"lateral": {
            "gamma_r": [{"define": "b.hub", "recently_modified": False}],
        }}
        lines = delivery._lateral_lines(details)
        assert any("Shared merge point" in l or "Γ_R" in l for l in lines)
        assert any("b.hub" in l for l in lines)

    def test_renders_gamma_r_recently_modified_label(self):
        details = {"lateral": {
            "gamma_r": [{"define": "b.hub", "recently_modified": True}],
        }}
        lines = delivery._lateral_lines(details)
        line = next(l for l in lines if "b.hub" in l)
        assert "recently changed" in line

    def test_renders_both_dimensions(self):
        details = {"lateral": {
            "shared_ancestry": [{"define": "a.fn", "omega": 2.0}],
            "gamma_r": [{"define": "b.hub", "recently_modified": False}],
        }}
        lines = delivery._lateral_lines(details)
        assert len(lines) == 2
        assert any("Shared structural ancestors" in l for l in lines)
        assert any("Shared merge point" in l or "Γ_R" in l for l in lines)

    def test_empty_lateral_returns_empty(self):
        assert delivery._lateral_lines({}) == []
        assert delivery._lateral_lines({"lateral": {}}) == []
