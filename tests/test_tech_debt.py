"""Tests for structural concern hints — v2.1 §3 路 A.

Coverage (maps to spec verification criteria):
  A. Define with callers >= THETA_DEBT_CALLERS → enters concerns, labeled "被 N 处依赖（高耦合枢纽）"
  B. Define with callees >= THETA_DEBT_CALLEES → enters concerns, labeled "依赖 N 个其他模块（牵涉广）"
  C. Define with churn >= THETA_DEBT_CHURN → enters concerns, labeled "已改动 N 轮（反复修改）"
  D. Define below all thresholds → not in concerns
  E. project_overview contains "结构关注点" section + positioning declaration (不判断/仅覆盖耦合维)
  F. Only top N returned, not full list
  G. No incidents written, no delivery queued (not an alert, not interrupting)
  H. Multi-dim define ranks above single-dim defines
  I. format_debt_section returns "" when no concerns
  J. threshold boundary: callers == THETA_DEBT_CALLERS-1 → not in, == THETA_DEBT_CALLERS → in
  K. Λ dimension: node with lambda_ >= THETA_DEBT_LAMBDA enters via "lambda" hit_dim
  L. version_chain edges excluded from Λ predecessor cone
  M. FQN deduplication in cone (same FQN via multiple det_ids counted once)
  N. Four-dim orthogonality: callers-high/lambda-zero vs callers-low/lambda-high enter via different dims
"""
from __future__ import annotations

import json

import pytest

from buer import tech_debt
from buer.health import project_overview
from buer.store import Store
from buer.tech_debt import (
    LAMBDA_PCT,
    MIN_DEFINES_FOR_LAMBDA_PCT,
    THETA_DEBT_CALLERS,
    THETA_DEBT_CALLEES,
    THETA_DEBT_CHURN,
    THETA_DEBT_LAMBDA,
    TOP_N_DEBT,
    _percentile,
    format_debt_section,
    structural_concerns,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_store(tmp_path) -> tuple[Store, int, str]:
    store = Store(":memory:")
    root = str(tmp_path)
    pid = store.get_or_create_project(root)
    return store, pid, root


def _add_define(store: Store, pid: int, tmp_path, define_name: str, n_edits: int = 1) -> str:
    """Insert n_edits determinations for define_name in src.py under tmp_path."""
    fp = str(tmp_path / "src.py")
    for i in range(n_edits):
        seq = store.next_seq(pid)
        store.insert_determination(pid, seq, fp, define_name, f"fp_{define_name}_{i}", "modify")
    return fp


def _add_callers(store: Store, pid: int, callee_fqn: str, count: int) -> None:
    """Insert `count` distinct call_edges pointing TO callee_fqn."""
    for i in range(count):
        store.upsert_call_edge(pid, f"other.caller_{callee_fqn}_{i}", callee_fqn, "call")


def _add_callees(store: Store, pid: int, caller_fqn: str, count: int) -> None:
    """Insert `count` distinct call_edges going FROM caller_fqn."""
    for i in range(count):
        store.upsert_call_edge(pid, caller_fqn, f"other.dep_{caller_fqn}_{i}", "call")


# FQN as call_edges use: src.py is Python → lang prefix "py::", module "src"
def _fqn(define_name: str) -> str:
    return f"py::src.{define_name}"


# ── A: callers dimension ───────────────────────────────────────────────────────

class TestCallersDimension:
    def test_enters_when_callers_at_threshold(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        _add_define(store, pid, tmp_path, "hub")
        _add_callers(store, pid, _fqn("hub"), THETA_DEBT_CALLERS)
        concerns = structural_concerns(store, pid, root)
        assert any(c["define_name"] == "hub" for c in concerns)

    def test_labeled_high_coupling_hub(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        _add_define(store, pid, tmp_path, "hub")
        _add_callers(store, pid, _fqn("hub"), THETA_DEBT_CALLERS)
        concerns = structural_concerns(store, pid, root)
        c = next(c for c in concerns if c["define_name"] == "hub")
        assert "callers" in c["hit_dims"]
        assert c["callers"] == THETA_DEBT_CALLERS

    def test_formatted_text_contains_hub_label(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        _add_define(store, pid, tmp_path, "hub")
        _add_callers(store, pid, _fqn("hub"), 6)
        concerns = structural_concerns(store, pid, root)
        text = format_debt_section(concerns)
        assert "depended on by 6 locations (high-coupling hub)" in text


# ── B: callees dimension ──────────────────────────────────────────────────────

class TestCalleesDimension:
    def test_enters_when_callees_at_threshold(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        _add_define(store, pid, tmp_path, "fat_fn")
        _add_callees(store, pid, _fqn("fat_fn"), THETA_DEBT_CALLEES)
        concerns = structural_concerns(store, pid, root)
        assert any(c["define_name"] == "fat_fn" for c in concerns)

    def test_labeled_wide_dependency(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        _add_define(store, pid, tmp_path, "fat_fn")
        _add_callees(store, pid, _fqn("fat_fn"), THETA_DEBT_CALLEES)
        concerns = structural_concerns(store, pid, root)
        c = next(c for c in concerns if c["define_name"] == "fat_fn")
        assert "callees" in c["hit_dims"]
        assert c["callees"] == THETA_DEBT_CALLEES

    def test_formatted_text_contains_callees_label(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        _add_define(store, pid, tmp_path, "fat_fn")
        _add_callees(store, pid, _fqn("fat_fn"), 9)
        concerns = structural_concerns(store, pid, root)
        text = format_debt_section(concerns)
        assert "depends on 9 other modules (broad reach)" in text


# ── C: churn dimension ────────────────────────────────────────────────────────

class TestChurnDimension:
    def test_enters_when_churn_at_threshold(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        _add_define(store, pid, tmp_path, "unstable", n_edits=THETA_DEBT_CHURN)
        concerns = structural_concerns(store, pid, root)
        assert any(c["define_name"] == "unstable" for c in concerns)

    def test_labeled_repeated_modification(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        _add_define(store, pid, tmp_path, "unstable", n_edits=11)
        concerns = structural_concerns(store, pid, root)
        c = next(c for c in concerns if c["define_name"] == "unstable")
        assert "churn" in c["hit_dims"]
        assert c["churn"] == 11

    def test_formatted_text_contains_churn_label(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        _add_define(store, pid, tmp_path, "unstable", n_edits=11)
        concerns = structural_concerns(store, pid, root)
        text = format_debt_section(concerns)
        assert "modified 11 times (high churn)" in text


# ── D: below all thresholds → not in concerns ─────────────────────────────────

class TestBelowThresholds:
    def test_not_in_concerns_when_all_dims_low(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        _add_define(store, pid, tmp_path, "clean", n_edits=2)
        _add_callers(store, pid, _fqn("clean"), THETA_DEBT_CALLERS - 1)
        _add_callees(store, pid, _fqn("clean"), THETA_DEBT_CALLEES - 1)
        concerns = structural_concerns(store, pid, root)
        assert not any(c["define_name"] == "clean" for c in concerns)

    def test_empty_project_returns_empty(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        assert structural_concerns(store, pid, root) == []


# ── E: project_overview section + positioning declaration ────────────────────

class TestProjectOverviewSection:
    def test_section_header_present(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        _add_define(store, pid, tmp_path, "hub")
        _add_callers(store, pid, _fqn("hub"), 6)
        text = project_overview(store, pid, root)
        assert "structural concerns" in text

    def test_positioning_declaration_present(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        _add_define(store, pid, tmp_path, "hub")
        _add_callers(store, pid, _fqn("hub"), 6)
        text = project_overview(store, pid, root)
        assert "BUER does not judge whether they actually have problems" in text

    def test_semantic_debt_exclusion_declared(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        _add_define(store, pid, tmp_path, "hub")
        _add_callers(store, pid, _fqn("hub"), 6)
        text = project_overview(store, pid, root)
        assert "semantic issues" in text

    def test_no_section_when_no_concerns(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        _add_define(store, pid, tmp_path, "clean", n_edits=1)
        text = project_overview(store, pid, root)
        assert "structural concerns" not in text


# ── F: top N only ─────────────────────────────────────────────────────────────

class TestTopNLimit:
    def test_returns_at_most_top_n(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        # Create TOP_N_DEBT+3 defines, all exceeding churn threshold
        for i in range(TOP_N_DEBT + 3):
            _add_define(store, pid, tmp_path, f"def_{i}", n_edits=THETA_DEBT_CHURN + i)
        concerns = structural_concerns(store, pid, root)
        assert len(concerns) <= TOP_N_DEBT

    def test_custom_top_n_respected(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        for i in range(10):
            _add_define(store, pid, tmp_path, f"def_{i}", n_edits=THETA_DEBT_CHURN + i)
        concerns = structural_concerns(store, pid, root, top_n=3)
        assert len(concerns) <= 3

    def test_format_section_lists_only_top_n(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        for i in range(TOP_N_DEBT + 2):
            _add_define(store, pid, tmp_path, f"def_{i}", n_edits=THETA_DEBT_CHURN + i)
        concerns = structural_concerns(store, pid, root)
        text = format_debt_section(concerns)
        # Number of "建议关注下" lines == len(concerns)
        assert text.count("worth checking for") == len(concerns)


# ── G: no incidents, no delivery queue ────────────────────────────────────────

class TestNoIncidentsNoDelivery:
    def test_no_incidents_written(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        _add_define(store, pid, tmp_path, "hub")
        _add_callers(store, pid, _fqn("hub"), 6)
        structural_concerns(store, pid, root)
        incs = store.open_incidents(pid)
        assert len(incs) == 0

    def test_no_delivery_queued(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        _add_define(store, pid, tmp_path, "hub")
        _add_callers(store, pid, _fqn("hub"), 6)
        structural_concerns(store, pid, root)
        deliveries = store.peek_deliveries(pid)
        assert len(deliveries) == 0

    def test_project_overview_does_not_enqueue(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        _add_define(store, pid, tmp_path, "hub")
        _add_callers(store, pid, _fqn("hub"), 6)
        project_overview(store, pid, root)
        deliveries = store.peek_deliveries(pid)
        assert len(deliveries) == 0


# ── H: multi-dim ranks above single-dim ──────────────────────────────────────

class TestRankingOrder:
    def test_multi_dim_ranks_first(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        # single_dim: only churn
        _add_define(store, pid, tmp_path, "single_dim", n_edits=THETA_DEBT_CHURN + 5)
        # multi_dim: callers + churn
        _add_define(store, pid, tmp_path, "multi_dim", n_edits=THETA_DEBT_CHURN)
        _add_callers(store, pid, _fqn("multi_dim"), THETA_DEBT_CALLERS)
        concerns = structural_concerns(store, pid, root)
        names = [c["define_name"] for c in concerns]
        assert names.index("multi_dim") < names.index("single_dim")


# ── I: format_debt_section with empty list ────────────────────────────────────

class TestFormatDebtSection:
    def test_empty_list_returns_empty_string(self):
        assert format_debt_section([]) == ""

    def test_single_concern_callers_format(self):
        concerns = [{
            "define_name": "auth_handler",
            "callers": 7,
            "callees": 2,
            "churn": 3,
            "hit_dims": ["callers"],
        }]
        text = format_debt_section(concerns)
        assert "auth_handler" in text
        assert "depended on by 7 locations (high-coupling hub)" in text
        assert "worth checking for potential refactoring or bug fixes" in text

    def test_all_three_dims_in_description(self):
        concerns = [{
            "define_name": "monster",
            "callers": 6,
            "callees": 9,
            "churn": 11,
            "hit_dims": ["callers", "callees", "churn"],
        }]
        text = format_debt_section(concerns)
        assert "depended on by 6 locations (high-coupling hub)" in text
        assert "depends on 9 other modules (broad reach)" in text
        assert "modified 11 times (high churn)" in text


# ── J: threshold boundary ─────────────────────────────────────────────────────

class TestThresholdBoundary:
    def test_callers_just_below_threshold_excluded(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        _add_define(store, pid, tmp_path, "borderline")
        _add_callers(store, pid, _fqn("borderline"), THETA_DEBT_CALLERS - 1)
        concerns = structural_concerns(store, pid, root)
        assert not any(c["define_name"] == "borderline" for c in concerns)

    def test_callers_at_threshold_included(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        _add_define(store, pid, tmp_path, "borderline")
        _add_callers(store, pid, _fqn("borderline"), THETA_DEBT_CALLERS)
        concerns = structural_concerns(store, pid, root)
        assert any(c["define_name"] == "borderline" for c in concerns)

    def test_churn_just_below_threshold_excluded(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        _add_define(store, pid, tmp_path, "borderline", n_edits=THETA_DEBT_CHURN - 1)
        concerns = structural_concerns(store, pid, root)
        assert not any(c["define_name"] == "borderline" for c in concerns)

    def test_churn_at_threshold_included(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        _add_define(store, pid, tmp_path, "borderline", n_edits=THETA_DEBT_CHURN)
        concerns = structural_concerns(store, pid, root)
        assert any(c["define_name"] == "borderline" for c in concerns)


# ── helpers for gd-edge tests ─────────────────────────────────────────────────

def _add_define_get_det(store: Store, pid: int, tmp_path, define_name: str,
                         filename: str = "src.py", seq: int = 1) -> tuple[str, int]:
    """Insert one determination; return (file_path, det_id)."""
    fp = str(tmp_path / filename)
    det_id = store.insert_determination(pid, seq, fp, define_name,
                                         f"fp_{define_name}_{seq}", "modify")
    return fp, det_id


# ── K: Λ dimension ────────────────────────────────────────────────────────────

class TestLambdaDimension:
    def test_enters_when_lambda_at_threshold(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp_a, det_a = _add_define_get_det(store, pid, tmp_path, "node_a", "a.py", seq=1)
        fp_b, det_b = _add_define_get_det(store, pid, tmp_path, "node_b", "b.py", seq=2)
        # B is a structural predecessor of A (cross_define edge)
        store.insert_gd_edge(pid, from_det=det_b, to_det=det_a, edge_class="cross_define_callgraph")
        # B has THETA_DEBT_LAMBDA callers — enough to push A's Λ to threshold
        for i in range(THETA_DEBT_LAMBDA):
            store.upsert_call_edge(pid, f"x.caller_{i}", "py::b.node_b", "call")
        concerns = structural_concerns(store, pid, root)
        assert any(c["define_name"] == "node_a" for c in concerns)

    def test_lambda_hit_dim_labeled(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp_a, det_a = _add_define_get_det(store, pid, tmp_path, "node_a", "a.py", seq=1)
        fp_b, det_b = _add_define_get_det(store, pid, tmp_path, "node_b", "b.py", seq=2)
        store.insert_gd_edge(pid, from_det=det_b, to_det=det_a, edge_class="cross_define_callgraph")
        for i in range(THETA_DEBT_LAMBDA + 5):
            store.upsert_call_edge(pid, f"x.caller_{i}", "py::b.node_b", "call")
        concerns = structural_concerns(store, pid, root)
        c = next(c for c in concerns if c["define_name"] == "node_a")
        assert "lambda" in c["hit_dims"]
        assert c["lambda_"] == THETA_DEBT_LAMBDA + 5

    def test_lambda_value_sums_cone(self, tmp_path):
        """Λ sums callers of all ancestors in cone, not just direct predecessor."""
        store, pid, root = _make_store(tmp_path)
        fp_a, det_a = _add_define_get_det(store, pid, tmp_path, "node_a", "a.py", seq=1)
        fp_b, det_b = _add_define_get_det(store, pid, tmp_path, "node_b", "b.py", seq=2)
        fp_c, det_c = _add_define_get_det(store, pid, tmp_path, "node_c", "c.py", seq=3)
        # chain: C → B → A
        store.insert_gd_edge(pid, from_det=det_b, to_det=det_a, edge_class="cross_define_dataflow")
        store.insert_gd_edge(pid, from_det=det_c, to_det=det_b, edge_class="cross_define_dataflow")
        # B has 15 callers, C has 10 callers → Λ(A) = 25
        for i in range(15):
            store.upsert_call_edge(pid, f"x.cb_{i}", "py::b.node_b", "call")
        for i in range(10):
            store.upsert_call_edge(pid, f"x.cc_{i}", "py::c.node_c", "call")
        concerns = structural_concerns(store, pid, root)
        c = next((c for c in concerns if c["define_name"] == "node_a"), None)
        assert c is not None
        assert c["lambda_"] == 25

    def test_lambda_below_threshold_not_in_lambda_dim(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp_a, det_a = _add_define_get_det(store, pid, tmp_path, "node_a", "a.py", seq=1)
        fp_b, det_b = _add_define_get_det(store, pid, tmp_path, "node_b", "b.py", seq=2)
        store.insert_gd_edge(pid, from_det=det_b, to_det=det_a, edge_class="cross_define_callgraph")
        # Only THETA_DEBT_LAMBDA - 1 callers on B → Λ(A) < threshold
        for i in range(THETA_DEBT_LAMBDA - 1):
            store.upsert_call_edge(pid, f"x.caller_{i}", "py::b.node_b", "call")
        concerns = structural_concerns(store, pid, root)
        c = next((c for c in concerns if c["define_name"] == "node_a"), None)
        # node_a should not appear (no threshold met)
        assert c is None or "lambda" not in c["hit_dims"]

    def test_formatted_text_contains_lambda_label(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp_a, det_a = _add_define_get_det(store, pid, tmp_path, "deep_node", "a.py", seq=1)
        fp_b, det_b = _add_define_get_det(store, pid, tmp_path, "upstream", "b.py", seq=2)
        store.insert_gd_edge(pid, from_det=det_b, to_det=det_a, edge_class="cross_define_callgraph")
        for i in range(25):
            store.upsert_call_edge(pid, f"x.c_{i}", "py::b.upstream", "call")
        concerns = structural_concerns(store, pid, root)
        text = format_debt_section(concerns)
        assert "accumulated constraint load Λ=25" in text
        assert "deep dependency chain" in text


# ── M: FQN deduplication in cone ─────────────────────────────────────────────

class TestFqnDeduplication:
    def test_same_fqn_two_det_ids_counted_once(self, tmp_path):
        """If cone contains two det_ids mapping to same FQN, callers counted once."""
        store, pid, root = _make_store(tmp_path)
        fp_a, det_a = _add_define_get_det(store, pid, tmp_path, "node_a", "a.py", seq=1)
        fp_b = str(tmp_path / "b.py")
        # Two det_ids for node_b (simulating two determinations with cross_define to A)
        det_b1 = store.insert_determination(pid, 2, fp_b, "node_b", "fp_b1", "create")
        det_b2 = store.insert_determination(pid, 3, fp_b, "node_b", "fp_b2", "modify")
        store.insert_gd_edge(pid, from_det=det_b1, to_det=det_a, edge_class="cross_define_callgraph")
        store.insert_gd_edge(pid, from_det=det_b2, to_det=det_a, edge_class="cross_define_callgraph")
        # b.node_b has 30 callers
        for i in range(30):
            store.upsert_call_edge(pid, f"x.c_{i}", "py::b.node_b", "call")
        concerns = structural_concerns(store, pid, root)
        # latest_det for (fp_b, node_b) = det_b2; but A's cone has both det_b1 and det_b2
        # FQN dedup: both map to "py::b.node_b" → counted once → Λ(A) = 30
        c = next((c for c in concerns if c["define_name"] == "node_a"), None)
        assert c is not None
        assert c["lambda_"] == 30


# ── N: four-dim orthogonality ─────────────────────────────────────────────────

class TestFourDimOrthogonality:
    def test_high_callers_zero_lambda_enters_via_callers_only(self, tmp_path):
        """Leaf node with many callers but no gd predecessors: callers dim, not lambda."""
        store, pid, root = _make_store(tmp_path)
        fp, det = _add_define_get_det(store, pid, tmp_path, "leaf_hub", "src.py", seq=1)
        # Many callers, but no gd predecessors → Λ = 0
        for i in range(THETA_DEBT_CALLERS + 10):
            store.upsert_call_edge(pid, f"x.c_{i}", "py::src.leaf_hub", "call")
        concerns = structural_concerns(store, pid, root)
        c = next(c for c in concerns if c["define_name"] == "leaf_hub")
        assert "callers" in c["hit_dims"]
        assert "lambda" not in c["hit_dims"]
        assert c["lambda_"] == 0

    def test_low_callers_high_lambda_enters_via_lambda_only(self, tmp_path):
        """Deep node: callers=1 (below threshold), but large Λ → lambda dim only."""
        store, pid, root = _make_store(tmp_path)
        fp_a, det_a = _add_define_get_det(store, pid, tmp_path, "deep_node", "a.py", seq=1)
        # One direct caller — below THETA_DEBT_CALLERS
        store.upsert_call_edge(pid, "x.one_caller", "py::a.deep_node", "call")
        # Build upstream cone with enough aggregate callers to exceed THETA_DEBT_LAMBDA
        for j in range(3):
            fp_b = str(tmp_path / f"b{j}.py")
            det_b = store.insert_determination(pid, j + 2, fp_b, f"upstream_{j}",
                                                f"fp_b{j}", "create")
            store.insert_gd_edge(pid, from_det=det_b, to_det=det_a,
                                   edge_class="cross_define_dataflow")
            # Each upstream has THETA_DEBT_LAMBDA callers
            for i in range(THETA_DEBT_LAMBDA):
                store.upsert_call_edge(pid, f"x.c{j}_{i}", f"py::b{j}.upstream_{j}", "call")
        concerns = structural_concerns(store, pid, root)
        c = next((c for c in concerns if c["define_name"] == "deep_node"), None)
        assert c is not None
        assert "lambda" in c["hit_dims"]
        assert "callers" not in c["hit_dims"]  # callers=1 < THETA_DEBT_CALLERS

    def test_lambda_key_present_in_all_concerns(self, tmp_path):
        """Every concern dict returned by structural_concerns has a lambda_ key."""
        store, pid, root = _make_store(tmp_path)
        _add_define(store, pid, tmp_path, "hub")
        _add_callers(store, pid, _fqn("hub"), THETA_DEBT_CALLERS)
        concerns = structural_concerns(store, pid, root)
        for c in concerns:
            assert "lambda_" in c

    def test_three_dim_regression_values_unchanged(self, tmp_path):
        """Batch-fetch callers/callees/churn values match per-node query values."""
        store, pid, root = _make_store(tmp_path)
        _add_define(store, pid, tmp_path, "fn_x", n_edits=12)
        _add_callers(store, pid, _fqn("fn_x"), 7)
        _add_callees(store, pid, _fqn("fn_x"), 9)
        concerns = structural_concerns(store, pid, root)
        c = next(c for c in concerns if c["define_name"] == "fn_x")
        assert c["callers"] == 7
        assert c["callees"] == 9
        assert c["churn"] == 12


# ── _percentile helper ────────────────────────────────────────────────────────

class TestPercentileHelper:
    def test_basic_correctness(self):
        vals = [0.0, 1.0, 2.0, 3.0, 4.0]
        assert _percentile(vals, 50) == 2.0
        assert _percentile(vals, 75) == 3.0
        assert _percentile(vals, 100) == 4.0

    def test_single_element(self):
        assert _percentile([7.0], 50) == 7.0

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            _percentile([], 50)


# ── Λ scale-gate: small project fallback vs large project P95 ─────────────────

class TestLambdaScaleGate:
    def test_small_project_uses_fixed_threshold(self, tmp_path):
        """< MIN_DEFINES_FOR_LAMBDA_PCT defines → fallback to THETA_DEBT_LAMBDA; lambda=15 does not fire."""
        store, pid, root = _make_store(tmp_path)
        fp_a, det_a = _add_define_get_det(store, pid, tmp_path, "target", "a.py", seq=1)
        fp_b, det_b = _add_define_get_det(store, pid, tmp_path, "upstream", "b.py", seq=2)
        store.insert_gd_edge(pid, from_det=det_b, to_det=det_a, edge_class="cross_define_callgraph")
        for i in range(THETA_DEBT_LAMBDA - 5):  # 15 callers → lambda=15 < fallback=20
            store.upsert_call_edge(pid, f"c_{i}.x", "py::b.upstream", "call")
        concerns = structural_concerns(store, pid, root)
        c = next((c for c in concerns if c["define_name"] == "target"), None)
        assert c is None or "lambda" not in c["hit_dims"]

    def test_large_project_p95_below_fixed_threshold(self, tmp_path):
        """≥ MIN_DEFINES_FOR_LAMBDA_PCT defines → P95 threshold; node above P95 but below 20 fires."""
        store, pid, root = _make_store(tmp_path)
        # 46 flat filler defines (lambda=0)
        for i in range(46):
            _add_define(store, pid, tmp_path, f"filler_{i}")
        # 4 mid-defines with lambda=12 each (pushes P95 to ~12 across 56 total defines)
        for j in range(4):
            fp_m, det_m = _add_define_get_det(store, pid, tmp_path, f"mid_{j}", f"mid{j}.py", seq=100 + j)
            fp_s, det_s = _add_define_get_det(store, pid, tmp_path, f"src_{j}", f"src{j}.py", seq=200 + j)
            store.insert_gd_edge(pid, from_det=det_s, to_det=det_m, edge_class="cross_define_callgraph")
            for k in range(12):
                store.upsert_call_edge(pid, f"c{j}_{k}.x", f"py::src{j}.src_{j}", "call")
        # Target with lambda=14 (above P95≈12, below THETA_DEBT_LAMBDA=20) — fires only via P95
        fp_t, det_t = _add_define_get_det(store, pid, tmp_path, "target", "target.py", seq=300)
        fp_u, det_u = _add_define_get_det(store, pid, tmp_path, "big_up", "bigup.py", seq=301)
        store.insert_gd_edge(pid, from_det=det_u, to_det=det_t, edge_class="cross_define_callgraph")
        for k in range(14):
            store.upsert_call_edge(pid, f"tc_{k}.x", "py::bigup.big_up", "call")
        concerns = structural_concerns(store, pid, root)
        target = next((c for c in concerns if c["define_name"] == "target"), None)
        assert target is not None and "lambda" in target["hit_dims"]


# ── CALLEES new threshold = 6 ─────────────────────────────────────────────────

class TestCalleesNewThreshold:
    def test_callees_six_fires(self, tmp_path):
        """THETA_DEBT_CALLEES=6: exactly 6 callees now enters concerns (was threshold=8)."""
        store, pid, root = _make_store(tmp_path)
        _add_define(store, pid, tmp_path, "fn_wide")
        _add_callees(store, pid, _fqn("fn_wide"), 6)
        concerns = structural_concerns(store, pid, root)
        assert any(c["define_name"] == "fn_wide" and "callees" in c["hit_dims"]
                   for c in concerns)


class TestLambdaZeroGuard:
    """Λ=0 full-hit guard: sparse/all-zero graphs must not trigger lambda dim for every define."""

    def _make_n_defines(self, store, pid, tmp_path, n: int):
        """Insert n defines in separate files, all with Λ=0 (no GD edges)."""
        for i in range(n):
            fp = str(tmp_path / f"src{i}.py")
            seq = store.next_seq(pid)
            store.insert_determination(pid, seq, fp, f"func_{i}", f"fp_{i}", "create")

    def test_all_zero_lambda_no_lambda_dim_hits(self, tmp_path):
        """Project with Λ=0 for all defines → lambda dimension never fires."""
        store, pid, root = _make_store(tmp_path)
        self._make_n_defines(store, pid, tmp_path, MIN_DEFINES_FOR_LAMBDA_PCT)
        # Add callers to force some defines into concerns via callers dim only
        store.upsert_call_edge(pid, "other.c1", "py::src0.func_0", "call")
        store.upsert_call_edge(pid, "other.c2", "py::src0.func_0", "call")
        store.upsert_call_edge(pid, "other.c3", "py::src0.func_0", "call")
        store.upsert_call_edge(pid, "other.c4", "py::src0.func_0", "call")
        store.upsert_call_edge(pid, "other.c5", "py::src0.func_0", "call")
        concerns = structural_concerns(store, pid, root)
        lambda_hits = [c for c in concerns if "lambda" in c.get("hit_dims", [])]
        assert len(lambda_hits) == 0, f"Expected 0 lambda hits, got {len(lambda_hits)}: {lambda_hits}"

    def test_all_zero_lambda_callers_dim_still_fires(self, tmp_path):
        """Λ=0 guard doesn't suppress other dimensions — callers dim still fires."""
        store, pid, root = _make_store(tmp_path)
        self._make_n_defines(store, pid, tmp_path, MIN_DEFINES_FOR_LAMBDA_PCT)
        _add_callers(store, pid, "py::src0.func_0", THETA_DEBT_CALLERS)
        concerns = structural_concerns(store, pid, root)
        hub = next((c for c in concerns if c["define_name"] == "func_0"), None)
        assert hub is not None
        assert "callers" in hub["hit_dims"]
        assert "lambda" not in hub["hit_dims"]

    def test_lambda_zero_threshold_no_full_hit(self, tmp_path):
        """P95 of all-zeros is 0.0; with guard, 0 >= 0.0 AND 0 > 0 is False → no hit."""
        store, pid, root = _make_store(tmp_path)
        # Exactly at MIN threshold: P95 path is taken
        self._make_n_defines(store, pid, tmp_path, MIN_DEFINES_FOR_LAMBDA_PCT)
        concerns = structural_concerns(store, pid, root)
        # No concern should have lambda dim when all lambdas are 0
        for c in concerns:
            assert "lambda" not in c.get("hit_dims", []), \
                f"{c['define_name']} got lambda hit with lambda_=0"

    def test_nonzero_lambda_still_fires(self, tmp_path):
        """A define with real Λ > P95 still hits lambda dim after the guard."""
        store, pid, root = _make_store(tmp_path)
        # Create a deep node with real Λ
        fp_deep = str(tmp_path / "deep.py")
        det_deep = store.insert_determination(pid, store.next_seq(pid), fp_deep,
                                              "deep_node", "fp_d", "create")
        # Build upstream cone: enough callers to give Λ > threshold
        for j in range(3):
            fp_up = str(tmp_path / f"up{j}.py")
            det_up = store.insert_determination(pid, store.next_seq(pid),
                                                fp_up, f"upstream_{j}", f"fp_u{j}", "create")
            store.insert_gd_edge(pid, from_det=det_up, to_det=det_deep,
                                 edge_class="cross_define_dataflow")
            for i in range(THETA_DEBT_LAMBDA):
                store.upsert_call_edge(pid, f"x.c{j}_{i}", f"py::up{j}.upstream_{j}", "call")
        # Pad to MIN_DEFINES_FOR_LAMBDA_PCT so P95 path is used
        self._make_n_defines(store, pid, tmp_path, MIN_DEFINES_FOR_LAMBDA_PCT - 4)
        concerns = structural_concerns(store, pid, root)
        deep = next((c for c in concerns if c["define_name"] == "deep_node"), None)
        assert deep is not None, "deep_node should appear in concerns"
        assert "lambda" in deep["hit_dims"]
        assert deep["lambda_"] > 0
