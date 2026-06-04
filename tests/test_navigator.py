"""Tests for large-codebase navigator — v2.1 §2 形态 B/C.

Coverage (maps to spec verification criteria):
  A. structural_briefing("file::foo") → returns foo's callers/callees/component info
  B. Form C: gd_node_count < THETA_CLARITY_NODES → post-read returns "" (no injection)
  C. gd_node_count >= THETA_CLARITY_NODES, not given yet → post-read injects hub hint
  D. Already given this session → subsequent post-read returns "" (dedup)
  E. post-read does not call reconcile (fast queries only — no graph computation)
  F. Form-C hint content is about identified hubs, not generic scanning methodology
  G. Form-B briefing contains dynamic-language accuracy disclaimer
  H. resolve_target with "define_name" (no ::) → finds most-recent determination
  I. resolve_target with unknown define → returns (None, None)
  J. structural_briefing MCP tool: project not found → returns error message
  K. gd_node_count returns correct count of distinct gd nodes
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from buer.mcp.server import _set_store_for_testing, _structure_guide_given, mcp
from buer.navigator import (
    THETA_CLARITY_NODES,
    resolve_target,
    structural_briefing_text,
    structure_guide_hint,
)
from buer.store import Store


def _ac(r) -> str:
    """Extract additionalContext from hook JSON, or '' when body is {}."""
    return r.json().get("hookSpecificOutput", {}).get("additionalContext", "")


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_guide_state():
    """Clear per-session dedup state between tests."""
    _structure_guide_given.clear()
    yield
    _structure_guide_given.clear()


def _make_store(tmp_path) -> tuple[Store, int, str]:
    store = Store(":memory:")
    root = str(tmp_path)
    pid = store.get_or_create_project(root)
    return store, pid, root


def _client(store: Store) -> TestClient:
    _set_store_for_testing(store)
    return TestClient(mcp.streamable_http_app())


def _add_det(store, pid, fp, define_name, seq):
    return store.insert_determination(pid, seq, fp, define_name, f"fp_{define_name}_{seq}", "create")


def _chain(store, pid, fp, n, seq_start=1):
    ids = []
    for i in range(n):
        did = _add_det(store, pid, fp, f"fn_{seq_start+i}", seq_start + i)
        ids.append(did)
    for i in range(len(ids) - 1):
        store.insert_gd_edge(pid, ids[i], ids[i+1], "version_chain")
    return ids


# ── A: structural_briefing returns callers/callees/component info ─────────────

class TestStructuralBriefing:
    def test_returns_callers_section(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        _add_det(store, pid, fp, "my_fn", seq=1)
        # src.my_fn ← caller1, caller2
        store.upsert_call_edge(pid, "pkg.caller1", "py::src.my_fn", "call")
        store.upsert_call_edge(pid, "pkg.caller2", "py::src.my_fn", "call")
        text = structural_briefing_text(store, pid, root, fp, "my_fn")
        assert "depended on by: 2 locations" in text
        assert "caller1" in text or "caller2" in text

    def test_returns_callees_section(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        _add_det(store, pid, fp, "my_fn", seq=1)
        store.upsert_call_edge(pid, "py::src.my_fn", "py::dep.helper1", "call")
        store.upsert_call_edge(pid, "py::src.my_fn", "py::dep.helper2", "call")
        text = structural_briefing_text(store, pid, root, fp, "my_fn")
        assert "depends on: 2 defines" in text
        assert "helper1" in text or "helper2" in text

    def test_returns_component_size(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        ids = _chain(store, pid, fp, 4, seq_start=1)
        text = structural_briefing_text(store, pid, root, fp, "fn_1")
        assert "functional region of 4 nodes" in text

    def test_contains_define_name_in_header(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        _add_det(store, pid, fp, "process_order", seq=1)
        text = structural_briefing_text(store, pid, root, fp, "process_order")
        assert "structural context for process_order" in text

    def test_zero_callers_shown_correctly(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        _add_det(store, pid, fp, "leaf_fn", seq=1)
        text = structural_briefing_text(store, pid, root, fp, "leaf_fn")
        assert "depended on by: 0 locations" in text

    def test_zero_callees_shown_correctly(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        _add_det(store, pid, fp, "root_fn", seq=1)
        text = structural_briefing_text(store, pid, root, fp, "root_fn")
        assert "depends on: 0 defines" in text


# ── B: form C — below clarity threshold → no injection ───────────────────────

class TestFormCBelowThreshold:
    def test_post_read_silent_when_node_count_below_threshold(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        # Create fewer nodes than THETA_CLARITY_NODES
        ids = _chain(store, pid, fp, THETA_CLARITY_NODES - 1, seq_start=1)
        # Add a hub so structure_guide_hint would return non-empty if called
        for i in range(6):
            store.upsert_call_edge(pid, f"other.c{i}", "py::src.fn_1", "call")
        client = _client(store)
        resp = client.post("/buer/post-read", json={
            "tool_name": "Read",
            "tool_input": {"file_path": fp},
            "cwd": root,
            "session_id": "sess-001",
        })
        assert _ac(resp) == ""

    def test_post_read_silent_when_no_gd_edges(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        # Lots of determinations but no gd_edges
        for i in range(30):
            _add_det(store, pid, fp, f"fn_{i}", seq=i+1)
        client = _client(store)
        resp = client.post("/buer/post-read", json={
            "tool_name": "Read",
            "tool_input": {"file_path": fp},
            "cwd": root,
            "session_id": "sess-002",
        })
        assert _ac(resp) == ""


# ── C: form C — above threshold + not given → injects hint ───────────────────

class TestFormCInjectsHint:
    def _setup_clear_graph(self, tmp_path):
        """Store with THETA_CLARITY_NODES gd-graph nodes + a hub with callers."""
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        ids = _chain(store, pid, fp, THETA_CLARITY_NODES, seq_start=1)
        # Add a hub node in call_edges
        for i in range(5):
            store.upsert_call_edge(pid, f"other.c{i}", "py::src.fn_1", "call")
        return store, pid, root, fp

    def test_post_read_returns_hint_when_threshold_met(self, tmp_path):
        store, pid, root, fp = self._setup_clear_graph(tmp_path)
        client = _client(store)
        resp = client.post("/buer/post-read", json={
            "tool_name": "Read",
            "tool_input": {"file_path": fp},
            "cwd": root,
            "session_id": "sess-003",
        })
        assert resp.status_code == 200
        assert _ac(resp) != ""

    def test_hint_contains_hub_nodes(self, tmp_path):
        store, pid, root, fp = self._setup_clear_graph(tmp_path)
        client = _client(store)
        resp = client.post("/buer/post-read", json={
            "tool_name": "Read",
            "tool_input": {"file_path": fp},
            "cwd": root,
            "session_id": "sess-004",
        })
        assert "fn_1" in _ac(resp)  # the hub

    def test_hint_mentions_structural_observation(self, tmp_path):
        store, pid, root, fp = self._setup_clear_graph(tmp_path)
        client = _client(store)
        resp = client.post("/buer/post-read", json={
            "tool_name": "Read",
            "tool_input": {"file_path": fp},
            "cwd": root,
            "session_id": "sess-005",
        })
        assert "BUER structural observation" in _ac(resp)


# ── D: dedup — already given → subsequent post-read returns "" ───────────────

class TestFormCDedup:
    def test_second_post_read_same_session_is_empty(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        ids = _chain(store, pid, fp, THETA_CLARITY_NODES, seq_start=1)
        for i in range(5):
            store.upsute_call_edge = None  # won't be called
            store.upsert_call_edge(pid, f"other.c{i}", "py::src.fn_1", "call")
        client = _client(store)
        payload = {
            "tool_name": "Read",
            "tool_input": {"file_path": fp},
            "cwd": root,
            "session_id": "sess-dedup",
        }
        first = client.post("/buer/post-read", json=payload)
        second = client.post("/buer/post-read", json=payload)
        assert _ac(first) != ""    # first: hint injected
        assert _ac(second) == ""   # second: deduped

    def test_different_session_id_gets_fresh_hint(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        ids = _chain(store, pid, fp, THETA_CLARITY_NODES, seq_start=1)
        for i in range(5):
            store.upsert_call_edge(pid, f"other.c{i}", "py::src.fn_1", "call")
        client = _client(store)
        r1 = client.post("/buer/post-read", json={
            "tool_name": "Read", "tool_input": {"file_path": fp},
            "cwd": root, "session_id": "sess-A",
        })
        r2 = client.post("/buer/post-read", json={
            "tool_name": "Read", "tool_input": {"file_path": fp},
            "cwd": root, "session_id": "sess-B",
        })
        assert _ac(r1) != ""
        assert _ac(r2) != ""  # different session → fresh hint


# ── E: post-read does not call reconcile ────────────────────────────────────

class TestPostReadNoReconcile:
    def test_post_read_does_not_call_reconcile(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        _add_det(store, pid, fp, "fn", seq=1)
        client = _client(store)
        with patch("buer.mcp.server.reconcile") as mock_rec:
            client.post("/buer/post-read", json={
                "tool_name": "Read",
                "tool_input": {"file_path": fp},
                "cwd": root,
                "session_id": "sess-x",
            })
        mock_rec.assert_not_called()


# ── F: form-C content is identified hubs, not methodology ────────────────────

class TestFormCContent:
    def test_hint_content_is_hub_based_not_methodology(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        _chain(store, pid, fp, THETA_CLARITY_NODES, seq_start=1)
        for i in range(7):
            store.upsert_call_edge(pid, f"other.c{i}", "py::src.fn_1", "call")
        hint = structure_guide_hint(store, pid)
        # Should contain specific hub info
        assert "fn_1" in hint
        assert "7 callers" in hint
        # Should NOT be generic methodology
        assert "应该先看" not in hint
        assert "建议按顺序" not in hint

    def test_hint_references_session_overview(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        _chain(store, pid, fp, THETA_CLARITY_NODES, seq_start=1)
        for i in range(5):
            store.upsert_call_edge(pid, f"other.c{i}", "py::src.fn_1", "call")
        hint = structure_guide_hint(store, pid)
        assert "session-start structure overview" in hint

    def test_hint_empty_when_no_hubs(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        _chain(store, pid, root + "/src.py", 5, seq_start=1)
        hint = structure_guide_hint(store, pid)
        assert hint == ""


# ── G: form-B has dynamic language disclaimer ────────────────────────────────

class TestFormBDisclaimer:
    def test_briefing_contains_disclaimer(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        _add_det(store, pid, fp, "my_fn", seq=1)
        text = structural_briefing_text(store, pid, root, fp, "my_fn")
        assert "Dynamic-language deps may be incomplete" in text

    def test_briefing_contains_local_map_note(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        _add_det(store, pid, fp, "my_fn", seq=1)
        text = structural_briefing_text(store, pid, root, fp, "my_fn")
        assert "local map only" in text


# ── H: resolve_target with define-name only ──────────────────────────────────

class TestResolveTarget:
    def test_full_format_resolves_correctly(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        _add_det(store, pid, fp, "my_fn", seq=1)
        result = resolve_target(store, pid, f"{fp}::my_fn")
        assert result == (fp, "my_fn")

    def test_define_name_only_finds_most_recent(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        _add_det(store, pid, fp, "my_fn", seq=1)
        file_path, define_name = resolve_target(store, pid, "my_fn")
        assert file_path == fp
        assert define_name == "my_fn"

    def test_full_format_wrong_define_returns_none(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        _add_det(store, pid, fp, "my_fn", seq=1)
        result = resolve_target(store, pid, f"{fp}::nonexistent")
        assert result == (None, None)


# ── I: unknown define returns (None, None) ───────────────────────────────────

class TestResolveTargetUnknown:
    def test_unknown_define_name_returns_none(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        assert resolve_target(store, pid, "totally_unknown") == (None, None)

    def test_unknown_file_colon_define_returns_none(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        assert resolve_target(store, pid, "/no/such/file.py::fn") == (None, None)


# ── J: structural_briefing MCP tool error paths ──────────────────────────────

def _call_structural_briefing(project_root: str, target: str) -> str:
    from buer.mcp.server import structural_briefing
    return structural_briefing(project_root, target)


class TestStructuralBriefingTool:
    def test_no_project_returns_error(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        _set_store_for_testing(store)
        result = _call_structural_briefing("/no/such/project", "fn")
        assert "No project registered" in result

    def test_unknown_define_returns_error(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        _set_store_for_testing(store)
        result = _call_structural_briefing(root, "nonexistent_fn")
        assert "not found" in result

    def test_valid_target_returns_briefing(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        _add_det(store, pid, fp, "my_fn", seq=1)
        _set_store_for_testing(store)
        result = _call_structural_briefing(root, f"{fp}::my_fn")
        assert "structural context for my_fn" in result


# ── K: gd_node_count ─────────────────────────────────────────────────────────

class TestGdNodeCount:
    def test_empty_project_returns_zero(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        assert store.gd_node_count(pid) == 0

    def test_chain_of_n_returns_n_nodes(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        ids = _chain(store, pid, fp, 5, seq_start=1)
        assert store.gd_node_count(pid) == 5

    def test_counts_both_from_and_to(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        fp = str(tmp_path / "src.py")
        a = _add_det(store, pid, fp, "fn_a", seq=1)
        b = _add_det(store, pid, fp, "fn_b", seq=2)
        store.insert_gd_edge(pid, a, b, "version_chain")
        assert store.gd_node_count(pid) == 2
