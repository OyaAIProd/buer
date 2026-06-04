"""Tests for global connected-component partition + coarse structure map (v2.1 §2 形态 A).

Coverage (maps to spec verification criteria):
  A. Two independent subgraphs → 2 frozensets with correct node grouping
  B. Single-pass coverage: all nodes covered, each node in exactly one component
  C. coarse_structure_map shows component count, sizes, representative modules, hubs
  D. Too many components → top _MAX_COMPONENTS_SHOWN listed + total count shown
  E. session-start with gd_edges → returns overview + coarse map; without → silent ""
  F. Dynamic language disclaimer present in coarse_structure_map output
  G. All project nodes covered by partition (no nodes missing, no duplicates)
  H. connected_components_all on empty project → []
  I. Single connected chain → 1 component containing all nodes
  J. coarse_structure_map returns "" when no nodes
"""
from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from buer import metrics
from buer.health import _MAX_COMPONENTS_SHOWN, coarse_structure_map
from buer.metrics import connected_components_all
from buer.mcp.server import mcp, _set_store_for_testing


def _ac(r) -> str:
    """Extract additionalContext from hook JSON, or '' when body is {}."""
    return r.json().get("hookSpecificOutput", {}).get("additionalContext", "")

from buer.store import Store


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_store(tmp_path) -> tuple[Store, int, str]:
    store = Store(":memory:")
    root = str(tmp_path)
    pid = store.get_or_create_project(root)
    return store, pid, root


def _add_det(store: Store, pid: int, fp: str, define: str, seq: int) -> int:
    return store.insert_determination(pid, seq, fp, define, f"fp_{define}_{seq}", "create")


def _chain(store: Store, pid: int, fp: str, n: int, seq_start: int = 1) -> list[int]:
    """Create n determinations in a version chain. Returns list of det_ids."""
    ids = []
    for i in range(n):
        did = _add_det(store, pid, fp, f"fn_{seq_start + i}", seq_start + i)
        ids.append(did)
    for i in range(len(ids) - 1):
        store.insert_gd_edge(pid, ids[i], ids[i + 1], "version_chain")
    return ids


# ── A: two independent subgraphs → 2 components ──────────────────────────────

class TestTwoIndependentSubgraphs:
    def test_returns_two_components(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        c1 = _chain(store, pid, fp, 3, seq_start=1)   # nodes 1→2→3
        c2 = _chain(store, pid, fp, 2, seq_start=10)  # nodes 4→5 (independent)
        comps = connected_components_all(store, pid)
        assert len(comps) == 2

    def test_nodes_correctly_grouped(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        c1 = _chain(store, pid, fp, 3, seq_start=1)
        c2 = _chain(store, pid, fp, 2, seq_start=10)
        comps = connected_components_all(store, pid)
        all_ids = set(c1) | set(c2)
        assert frozenset(c1) in comps
        assert frozenset(c2) in comps
        assert set(comps[0]) | set(comps[1]) == all_ids

    def test_component_sizes_correct(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        c1 = _chain(store, pid, fp, 3, seq_start=1)
        c2 = _chain(store, pid, fp, 2, seq_start=10)
        comps = connected_components_all(store, pid)
        sizes = sorted(len(c) for c in comps)
        assert sizes == [2, 3]


# ── B + G: single-pass coverage, all nodes in exactly one component ──────────

class TestSinglePassCoverage:
    def test_all_nodes_covered(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        c1 = _chain(store, pid, fp, 4, seq_start=1)
        c2 = _chain(store, pid, fp, 3, seq_start=10)
        c3 = _chain(store, pid, fp, 2, seq_start=20)
        comps = connected_components_all(store, pid)
        covered = set()
        for c in comps:
            covered |= set(c)
        expected = set(c1) | set(c2) | set(c3)
        assert covered == expected

    def test_no_node_in_two_components(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        c1 = _chain(store, pid, fp, 4, seq_start=1)
        c2 = _chain(store, pid, fp, 3, seq_start=10)
        comps = connected_components_all(store, pid)
        all_ids: list[int] = []
        for c in comps:
            all_ids.extend(c)
        assert len(all_ids) == len(set(all_ids))  # no duplicates

    def test_total_count_matches_determinations(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        c1 = _chain(store, pid, fp, 5, seq_start=1)
        c2 = _chain(store, pid, fp, 4, seq_start=10)
        comps = connected_components_all(store, pid)
        total_in_comps = sum(len(c) for c in comps)
        assert total_in_comps == 9  # 5 + 4


# ── C: coarse_structure_map shows sizes + representative modules + hubs ───────

class TestCoarseStructureMapContent:
    def test_shows_component_count(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        _chain(store, pid, fp, 3, seq_start=1)
        _chain(store, pid, fp, 2, seq_start=10)
        text = coarse_structure_map(store, pid, root)
        assert "2 relatively independent functional subsystems" in text

    def test_shows_subsystem_node_count(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        _chain(store, pid, fp, 4, seq_start=1)
        text = coarse_structure_map(store, pid, root)
        assert "4 structural nodes" in text

    def test_shows_representative_module(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "mymodule.py")
        _chain(store, pid, fp, 3, seq_start=1)
        text = coarse_structure_map(store, pid, root)
        assert "mymodule.py" in text

    def test_shows_hub_nodes_when_present(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        _chain(store, pid, fp, 3, seq_start=1)
        hub_callee = "pkg.hub_fn"
        for i in range(6):
            store.upsert_call_edge(pid, f"pkg.caller_{i}", hub_callee, "call")
        text = coarse_structure_map(store, pid, root)
        assert "cross-subsystem hubs" in text
        assert hub_callee in text

    def test_section_header_present(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        _chain(store, pid, fp, 3, seq_start=1)
        text = coarse_structure_map(store, pid, root)
        assert "codebase structure overview (coarse skeleton)" in text

    def test_skeleton_note_present(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        _chain(store, pid, fp, 2, seq_start=1)
        text = coarse_structure_map(store, pid, root)
        assert "coarsest skeleton" in text


# ── D: too many components → top N + total ───────────────────────────────────

class TestTopNComponentsLimit:
    def test_shows_only_top_n_components(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        # Create _MAX_COMPONENTS_SHOWN + 3 independent singleton nodes
        n = _MAX_COMPONENTS_SHOWN + 3
        for i in range(n):
            _add_det(store, pid, fp, f"isolated_{i}", seq=i + 1)
        text = coarse_structure_map(store, pid, root)
        # Count "子系统 N：" lines — should be at most _MAX_COMPONENTS_SHOWN
        subsystem_lines = [l for l in text.splitlines() if "subsystem" in l and "structural nodes" in l]
        assert len(subsystem_lines) <= _MAX_COMPONENTS_SHOWN

    def test_shows_total_count_in_summary(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        n = _MAX_COMPONENTS_SHOWN + 3
        for i in range(n):
            _add_det(store, pid, fp, f"isolated_{i}", seq=i + 1)
        text = coarse_structure_map(store, pid, root)
        assert str(n) in text  # total count appears somewhere

    def test_shows_extra_count_when_overflow(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        n = _MAX_COMPONENTS_SHOWN + 4
        for i in range(n):
            _add_det(store, pid, fp, f"isolated_{i}", seq=i + 1)
        text = coarse_structure_map(store, pid, root)
        assert "smaller subsystems" in text


# ── E: session-start endpoint ─────────────────────────────────────────────────

class TestSessionStartEndpoint:
    def _client(self, store: Store) -> TestClient:
        _set_store_for_testing(store)
        return TestClient(mcp.streamable_http_app())

    def test_with_gd_edges_returns_overview_and_coarse_map(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        ids = _chain(store, pid, fp, 3, seq_start=1)
        client = self._client(store)
        resp = client.post("/buer/session-start", json={"cwd": root})
        assert resp.status_code == 200
        body = _ac(resp)
        assert "codebase structure overview" in body

    def test_without_gd_edges_returns_empty(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        # Determinations exist but no gd_edges
        fp = str(tmp_path / "src.py")
        _add_det(store, pid, fp, "fn", seq=1)
        client = self._client(store)
        resp = client.post("/buer/session-start", json={"cwd": root})
        assert resp.status_code == 200
        assert _ac(resp) == ""

    def test_overview_present_in_response(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        ids = _chain(store, pid, fp, 2, seq_start=1)
        client = self._client(store)
        resp = client.post("/buer/session-start", json={"cwd": root})
        assert "project overview" in _ac(resp)


# ── F: dynamic language disclaimer ───────────────────────────────────────────

class TestDynamicLanguageDisclaimer:
    def test_disclaimer_present(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        _chain(store, pid, fp, 2, seq_start=1)
        text = coarse_structure_map(store, pid, root)
        assert "dynamic languages may have undetected connections" in text

    def test_disclaimer_mentions_reference_nature(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        _chain(store, pid, fp, 2, seq_start=1)
        text = coarse_structure_map(store, pid, root)
        assert "treat subsystem partition as approximate" in text


# ── H: empty project ─────────────────────────────────────────────────────────

class TestEmptyProject:
    def test_connected_components_all_empty(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        assert connected_components_all(store, pid) == []

    def test_coarse_structure_map_empty_returns_empty_string(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        assert coarse_structure_map(store, pid, root) == ""


# ── I: single connected chain → 1 component ──────────────────────────────────

class TestSingleChain:
    def test_single_component(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        ids = _chain(store, pid, fp, 5, seq_start=1)
        comps = connected_components_all(store, pid)
        assert len(comps) == 1

    def test_all_nodes_in_one_component(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        ids = _chain(store, pid, fp, 5, seq_start=1)
        comps = connected_components_all(store, pid)
        assert frozenset(ids) in comps

    def test_bidirectional_traversal(self, tmp_path):
        """Component found regardless of which end we start from (undirected)."""
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        # Two nodes connected in one direction only
        a = _add_det(store, pid, fp, "fn_a", seq=1)
        b = _add_det(store, pid, fp, "fn_b", seq=2)
        store.insert_gd_edge(pid, a, b, "version_chain")  # a → b only
        comps = connected_components_all(store, pid)
        assert len(comps) == 1  # both in same component (undirected)
        assert frozenset({a, b}) in comps
