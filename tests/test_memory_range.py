"""Tests for 变更范围记忆体 Phase 1:
  1. sessions table — open/close/get/recent_sessions
  2. changes_in_range / changes_for_session
  3. successor_cone BFS correctness
  4. influence_cone_of_changes union
  5. debug_range MCP tool end-to-end
"""
from __future__ import annotations

import tempfile
import os

import pytest

from buer.store import Store
from buer import influence
from buer.mcp.server import _set_store_for_testing, debug_range


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "store.sqlite"))
    yield s
    s.close()


@pytest.fixture
def pid(store):
    return store.get_or_create_project("/fake/project")


def _insert_det(store, pid, seq, file_path, define_name, edit_type="create"):
    """Helper: insert a determination row directly."""
    store.con.execute(
        """INSERT INTO determinations
           (project_id, seq, file_path, define_name, node_fingerprint, edit_type, created_at)
           VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
        (pid, seq, file_path, define_name, f"fp_{define_name}_{seq}", edit_type),
    )
    store.con.commit()
    return store.con.execute(
        "SELECT id FROM determinations WHERE project_id=? AND seq=?", (pid, seq)
    ).fetchone()["id"]


def _insert_edge(store, pid, from_det, to_det):
    store.con.execute(
        """INSERT OR IGNORE INTO gd_edges
           (project_id, from_det, to_det, edge_class)
           VALUES (?, ?, ?, 'version_chain')""",
        (pid, from_det, to_det),
    )
    store.con.commit()


def _insert_call_edge(store, pid, caller_fqn, callee_fqn):
    """Insert a call_edge row (static caller → callee)."""
    store.con.execute(
        """INSERT OR IGNORE INTO call_edges (project_id, caller, callee, edge_kind)
           VALUES (?, ?, ?, 'call')""",
        (pid, caller_fqn, callee_fqn),
    )
    store.con.commit()


# ── Block 1: session open/close/get/recent ────────────────────────────────────

class TestSessionOpen:
    def test_open_records_start_seq(self, store, pid):
        _insert_det(store, pid, 1, "/f/a.py", "foo")
        _insert_det(store, pid, 2, "/f/a.py", "bar")
        store.open_session(pid, "sess-1")
        sess = store.get_session(pid, "sess-1")
        assert sess is not None
        assert sess["start_seq"] == 2   # max_seq at open time

    def test_open_is_idempotent_on_resume(self, store, pid):
        _insert_det(store, pid, 1, "/f/a.py", "foo")
        store.open_session(pid, "sess-1")
        _insert_det(store, pid, 2, "/f/a.py", "bar")
        # Second open (resume) must NOT overwrite start_seq
        store.open_session(pid, "sess-1")
        sess = store.get_session(pid, "sess-1")
        assert sess["start_seq"] == 1   # original value preserved

    def test_end_seq_none_before_close(self, store, pid):
        store.open_session(pid, "sess-1")
        sess = store.get_session(pid, "sess-1")
        assert sess["end_seq"] is None

    def test_get_returns_none_for_unknown(self, store, pid):
        assert store.get_session(pid, "no-such") is None


class TestSessionClose:
    def test_close_sets_end_seq(self, store, pid):
        store.open_session(pid, "sess-1")
        _insert_det(store, pid, 1, "/f/a.py", "foo")
        _insert_det(store, pid, 2, "/f/a.py", "bar")
        store.close_session(pid, "sess-1")
        sess = store.get_session(pid, "sess-1")
        assert sess["end_seq"] == 2

    def test_close_no_changes_sets_same_seq(self, store, pid):
        _insert_det(store, pid, 5, "/f/a.py", "foo")
        store.open_session(pid, "sess-empty")
        store.close_session(pid, "sess-empty")
        sess = store.get_session(pid, "sess-empty")
        assert sess["start_seq"] == sess["end_seq"] == 5


class TestRecentSessions:
    def test_returns_most_recent_first(self, store, pid):
        _insert_det(store, pid, 1, "/f/a.py", "foo")
        store.open_session(pid, "s1")
        store.close_session(pid, "s1")
        _insert_det(store, pid, 2, "/f/a.py", "bar")
        store.open_session(pid, "s2")
        store.close_session(pid, "s2")
        sessions = store.recent_sessions(pid, 2)
        assert sessions[0]["session_id"] == "s2"
        assert sessions[1]["session_id"] == "s1"

    def test_limit_respected(self, store, pid):
        for i in range(5):
            _insert_det(store, pid, i + 1, "/f/a.py", f"fn{i}")
            store.open_session(pid, f"s{i}")
            store.close_session(pid, f"s{i}")
        assert len(store.recent_sessions(pid, 3)) == 3

    def test_empty_when_no_sessions(self, store, pid):
        assert store.recent_sessions(pid) == []


# ── Block 2: changes_in_range / changes_for_session ──────────────────────────

class TestChangesInRange:
    def test_returns_dets_in_half_open_interval(self, store, pid):
        for seq in range(1, 6):
            _insert_det(store, pid, seq, "/f/a.py", f"fn{seq}")
        rows = store.changes_in_range(pid, 2, 4)
        seqs = [r["seq"] for r in rows]
        assert seqs == [3, 4]   # seq > 2 AND seq <= 4

    def test_empty_range(self, store, pid):
        _insert_det(store, pid, 1, "/f/a.py", "foo")
        assert store.changes_in_range(pid, 1, 1) == []

    def test_ordered_by_seq(self, store, pid):
        for seq in [3, 1, 2]:
            _insert_det(store, pid, seq, "/f/a.py", f"fn{seq}")
        rows = store.changes_in_range(pid, 0, 3)
        assert [r["seq"] for r in rows] == [1, 2, 3]

    def test_respects_project_isolation(self, store):
        pid1 = store.get_or_create_project("/proj1")
        pid2 = store.get_or_create_project("/proj2")
        _insert_det(store, pid1, 1, "/f/a.py", "foo")
        _insert_det(store, pid2, 1, "/f/b.py", "bar")
        assert len(store.changes_in_range(pid1, 0, 10)) == 1


class TestChangesForSession:
    def test_returns_changes_within_session_window(self, store, pid):
        _insert_det(store, pid, 1, "/f/a.py", "pre")   # before session
        store.open_session(pid, "s1")                    # start_seq = 1
        _insert_det(store, pid, 2, "/f/a.py", "during")
        _insert_det(store, pid, 3, "/f/a.py", "also")
        store.close_session(pid, "s1")                   # end_seq = 3
        rows = store.changes_for_session(pid, "s1")
        seqs = [r["seq"] for r in rows]
        assert seqs == [2, 3]   # "pre" at seq=1 excluded

    def test_open_session_uses_current_max(self, store, pid):
        store.open_session(pid, "s-open")
        _insert_det(store, pid, 1, "/f/a.py", "foo")
        rows = store.changes_for_session(pid, "s-open")
        assert len(rows) == 1

    def test_unknown_session_returns_empty(self, store, pid):
        assert store.changes_for_session(pid, "ghost") == []


# ── Block 3: successor_cone ───────────────────────────────────────────────────

class TestSuccessorCone:
    def test_linear_chain(self, store, pid):
        a = _insert_det(store, pid, 1, "/f/a.py", "A")
        b = _insert_det(store, pid, 2, "/f/a.py", "B")
        c = _insert_det(store, pid, 3, "/f/a.py", "C")
        _insert_edge(store, pid, a, b)
        _insert_edge(store, pid, b, c)
        cone = influence.successor_cone(store, pid, a)
        assert cone == {b, c}

    def test_excludes_start_det(self, store, pid):
        a = _insert_det(store, pid, 1, "/f/a.py", "A")
        b = _insert_det(store, pid, 2, "/f/a.py", "B")
        _insert_edge(store, pid, a, b)
        assert a not in influence.successor_cone(store, pid, a)

    def test_leaf_has_empty_cone(self, store, pid):
        a = _insert_det(store, pid, 1, "/f/a.py", "A")
        assert influence.successor_cone(store, pid, a) == set()

    def test_diamond_graph(self, store, pid):
        a = _insert_det(store, pid, 1, "/f/a.py", "A")
        b = _insert_det(store, pid, 2, "/f/a.py", "B")
        c = _insert_det(store, pid, 3, "/f/a.py", "C")
        d = _insert_det(store, pid, 4, "/f/a.py", "D")
        _insert_edge(store, pid, a, b)
        _insert_edge(store, pid, a, c)
        _insert_edge(store, pid, b, d)
        _insert_edge(store, pid, c, d)
        cone = influence.successor_cone(store, pid, a)
        assert cone == {b, c, d}

    def test_no_backward_traversal(self, store, pid):
        a = _insert_det(store, pid, 1, "/f/a.py", "A")
        b = _insert_det(store, pid, 2, "/f/a.py", "B")
        c = _insert_det(store, pid, 3, "/f/a.py", "C")
        _insert_edge(store, pid, a, b)
        _insert_edge(store, pid, c, a)   # c → a (predecessor of a)
        cone = influence.successor_cone(store, pid, a)
        assert c not in cone


class TestCallerConeFqns:
    """Tests for call_edges-based caller BFS (real-time influence)."""

    def test_direct_caller(self, store, pid):
        _insert_call_edge(store, pid, "f/caller.callerFn", "f/callee.calleeFn")
        result = influence.caller_cone_fqns(store, pid, ["f/callee.calleeFn"])
        assert "f/caller.callerFn" in result

    def test_transitive_callers(self, store, pid):
        _insert_call_edge(store, pid, "f/b.B", "f/a.A")
        _insert_call_edge(store, pid, "f/c.C", "f/b.B")
        result = influence.caller_cone_fqns(store, pid, ["f/a.A"])
        assert result == {"f/b.B", "f/c.C"}

    def test_excludes_seeds(self, store, pid):
        _insert_call_edge(store, pid, "f/b.B", "f/a.A")
        result = influence.caller_cone_fqns(store, pid, ["f/a.A"])
        assert "f/a.A" not in result

    def test_no_callers_returns_empty(self, store, pid):
        result = influence.caller_cone_fqns(store, pid, ["f/a.A"])
        assert result == set()

    def test_empty_seeds(self, store, pid):
        assert influence.caller_cone_fqns(store, pid, []) == set()

    def test_diamond_callers(self, store, pid):
        # A ← B ← D,  A ← C ← D
        _insert_call_edge(store, pid, "m.B", "m.A")
        _insert_call_edge(store, pid, "m.C", "m.A")
        _insert_call_edge(store, pid, "m.D", "m.B")
        _insert_call_edge(store, pid, "m.D", "m.C")
        result = influence.caller_cone_fqns(store, pid, ["m.A"])
        assert result == {"m.B", "m.C", "m.D"}


class TestInfluenceConeOfChanges:
    def test_union_of_cones(self, store, pid):
        a = _insert_det(store, pid, 1, "/f/a.py", "A")
        b = _insert_det(store, pid, 2, "/f/a.py", "B")
        c = _insert_det(store, pid, 3, "/f/a.py", "C")
        d = _insert_det(store, pid, 4, "/f/a.py", "D")
        _insert_edge(store, pid, a, c)
        _insert_edge(store, pid, b, d)
        cone = influence.influence_cone_of_changes(store, pid, [a, b])
        assert cone == {c, d}

    def test_changed_dets_excluded_from_cone(self, store, pid):
        a = _insert_det(store, pid, 1, "/f/a.py", "A")
        b = _insert_det(store, pid, 2, "/f/a.py", "B")
        _insert_edge(store, pid, a, b)
        # Both a and b changed — b is also in the cone of a, but must be excluded
        cone = influence.influence_cone_of_changes(store, pid, [a, b])
        assert b not in cone

    def test_empty_det_ids(self, store, pid):
        assert influence.influence_cone_of_changes(store, pid, []) == set()

    def test_no_edges_all_empty(self, store, pid):
        a = _insert_det(store, pid, 1, "/f/a.py", "A")
        b = _insert_det(store, pid, 2, "/f/a.py", "B")
        assert influence.influence_cone_of_changes(store, pid, [a, b]) == set()


# ── Block 4 / Exit D: debug_range tool ────────────────────────────────────────

class TestDebugRange:
    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self._store = Store(os.path.join(self._tmpdir, "store.sqlite"))
        _set_store_for_testing(self._store)

    def teardown_method(self):
        _set_store_for_testing(None)
        self._store.close()

    def _pid(self):
        return self._store.get_or_create_project(self._tmpdir)

    def test_no_project_returns_message(self):
        result = debug_range("/nonexistent/path")
        assert "No project registered" in result

    def test_no_sessions_returns_message(self):
        self._pid()
        result = debug_range(self._tmpdir)
        assert "No sessions" in result

    def test_shows_changed_defines(self):
        pid = self._pid()
        self._store.open_session(pid, "s1")
        _insert_det(self._store, pid, 1, "/f/a.py", "my_func")
        self._store.close_session(pid, "s1")
        result = debug_range(self._tmpdir, recent_sessions=1)
        assert "my_func" in result

    def test_shows_influence_cone(self):
        pid = self._pid()
        self._store.open_session(pid, "s1")
        _insert_det(self._store, pid, 1, os.path.join(self._tmpdir, "a.py"), "changed_fn")
        self._store.close_session(pid, "s1")
        # Insert call_edge: downstream_fn calls changed_fn
        root = self._tmpdir.rstrip("/")
        mod = root.replace("/", ".").lstrip(".")  # simplified module for test
        # Use lang-prefixed FQN format matching call_edges (py:: for .py files)
        callee_fqn = "py::a.changed_fn"
        caller_fqn = "b.downstream_fn"
        _insert_call_edge(self._store, pid, caller_fqn, callee_fqn)
        result = debug_range(self._tmpdir, recent_sessions=1)
        assert "downstream_fn" in result

    def test_merges_multiple_sessions(self):
        pid = self._pid()
        _insert_det(self._store, pid, 1, "/f/a.py", "fn_session1")
        self._store.open_session(pid, "s1")
        _insert_det(self._store, pid, 2, "/f/a.py", "fn1")
        self._store.close_session(pid, "s1")
        self._store.open_session(pid, "s2")
        _insert_det(self._store, pid, 3, "/f/b.py", "fn2")
        self._store.close_session(pid, "s2")
        result = debug_range(self._tmpdir, recent_sessions=2)
        assert "fn1" in result
        assert "fn2" in result

    def test_open_session_handled(self):
        pid = self._pid()
        self._store.open_session(pid, "s-open")
        _insert_det(self._store, pid, 1, "/f/a.py", "live_fn")
        # Session not closed yet — changes_for_session uses current max_seq
        result = debug_range(self._tmpdir, recent_sessions=1)
        assert "live_fn" in result

    def test_summary_line_present(self):
        pid = self._pid()
        self._store.open_session(pid, "s1")
        _insert_det(self._store, pid, 1, "/f/a.py", "foo")
        self._store.close_session(pid, "s1")
        result = debug_range(self._tmpdir)
        assert "focus debugging on" in result

    def test_three_dim_shown_when_cone_gt_5(self):
        pid = self._pid()
        file_a = os.path.join(self._tmpdir, "a.py")
        self._store.open_session(pid, "s1")
        _insert_det(self._store, pid, 1, file_a, "changed_fn")
        self._store.close_session(pid, "s1")
        callee_fqn = "py::a.changed_fn"
        for i in range(6):
            _insert_call_edge(self._store, pid, f"c{i}.fn", callee_fqn)
        result = debug_range(self._tmpdir, recent_sessions=1)
        assert "priority checks" in result

    def test_no_priority_section_when_cone_le_5(self):
        pid = self._pid()
        file_a = os.path.join(self._tmpdir, "a.py")
        self._store.open_session(pid, "s1")
        _insert_det(self._store, pid, 1, file_a, "changed_fn")
        self._store.close_session(pid, "s1")
        callee_fqn = "py::a.changed_fn"
        for i in range(3):
            _insert_call_edge(self._store, pid, f"c{i}.fn", callee_fqn)
        result = debug_range(self._tmpdir, recent_sessions=1)
        assert "priority checks" not in result

    def test_stack_suspects_shown_when_intersection_nonempty(self):
        """⚡ 精确嫌疑 + 完整锥出口提示 在 cone ∩ stack 非空时都出现。"""
        import json
        pid = self._pid()
        file_a = os.path.join(self._tmpdir, "a.py")
        self._store.open_session(pid, "s1")
        _insert_det(self._store, pid, 1, file_a, "changed_fn")
        self._store.close_session(pid, "s1")
        # Add two callers into the cone
        _insert_call_edge(self._store, pid, "m.callerA", "py::a.changed_fn")
        _insert_call_edge(self._store, pid, "m.callerB", "py::a.changed_fn")
        # Store a crash stack that overlaps with callerA
        self._store.insert_crash_stack(
            project_id=pid,
            seq=1,
            stack_fqns_json=json.dumps(["m.callerA", "lib/other.unrelated"]),
        )
        result = debug_range(self._tmpdir, recent_sessions=1)
        assert "⚡" in result
        assert "m.callerA" in result
        # Numbered list format
        assert "1. m.callerA" in result
        # Complete-cone fallback sentence present
        assert "if precise suspects yield nothing" in result
        assert "full influence cone" in result
        # Cone size (2 callers) mentioned in fallback sentence
        assert "influence cone (2" in result

    def test_stack_no_intersection_shows_warning(self):
        """⚠ 警告 + 完整下游指引 在有崩溃记录但 cone ∩ stack 为空时出现。"""
        import json
        pid = self._pid()
        file_a = os.path.join(self._tmpdir, "a.py")
        self._store.open_session(pid, "s1")
        _insert_det(self._store, pid, 1, file_a, "changed_fn")
        self._store.close_session(pid, "s1")
        _insert_call_edge(self._store, pid, "m.callerA", "py::a.changed_fn")
        # Crash stack is completely disjoint from the cone
        self._store.insert_crash_stack(
            project_id=pid,
            seq=1,
            stack_fqns_json=json.dumps(["lib/totally.different"]),
        )
        result = debug_range(self._tmpdir, recent_sessions=1)
        assert "⚠" in result
        assert "do not intersect" in result
        # Points toward complete cone
        assert "full downstream of changes" in result

    def test_no_stack_layer_when_no_crash_stacks(self):
        """无崩溃记录时 debug_range 不出现 ⚡ 或 ⚠。"""
        pid = self._pid()
        file_a = os.path.join(self._tmpdir, "a.py")
        self._store.open_session(pid, "s1")
        _insert_det(self._store, pid, 1, file_a, "changed_fn")
        self._store.close_session(pid, "s1")
        result = debug_range(self._tmpdir, recent_sessions=1)
        assert "⚡" not in result
        assert "⚠" not in result

    def test_three_dim_shown_alongside_suspects(self):
        """交集非空时三维排序(完整锥有序视图)仍输出——作为兜底出口。"""
        import json
        pid = self._pid()
        file_a = os.path.join(self._tmpdir, "a.py")
        self._store.open_session(pid, "s1")
        _insert_det(self._store, pid, 1, file_a, "changed_fn")
        self._store.close_session(pid, "s1")
        callee_fqn = "py::a.changed_fn"
        # 6 callers → cone > 5 → three-dim should appear
        caller_fqns = [f"m.caller{i}" for i in range(6)]
        for c in caller_fqns:
            _insert_call_edge(self._store, pid, c, callee_fqn)
        # Crash stack overlaps with caller0 only
        self._store.insert_crash_stack(
            project_id=pid,
            seq=1,
            stack_fqns_json=json.dumps(["m.caller0"]),
        )
        result = debug_range(self._tmpdir, recent_sessions=1)
        assert "⚡" in result
        assert "m.caller0" in result
        # Three-dim ranking still present as complete-cone fallback
        assert "priority checks" in result


# ── Block 5: dependency_cone_size ────────────────────────────────────────────

class TestDependencyConeSize:
    def test_leaf_size_zero(self, store, pid):
        result = influence.dependency_cone_size(store, pid, "m.A")
        assert result == 0

    def test_one_callee(self, store, pid):
        _insert_call_edge(store, pid, "m.A", "m.B")
        assert influence.dependency_cone_size(store, pid, "m.A") == 1

    def test_transitive_chain(self, store, pid):
        _insert_call_edge(store, pid, "m.A", "m.B")
        _insert_call_edge(store, pid, "m.B", "m.C")
        assert influence.dependency_cone_size(store, pid, "m.A") == 2

    def test_diamond_counts_once(self, store, pid):
        # A→B, A→C, B→D, C→D: D counted once → δ(A) = 3
        _insert_call_edge(store, pid, "m.A", "m.B")
        _insert_call_edge(store, pid, "m.A", "m.C")
        _insert_call_edge(store, pid, "m.B", "m.D")
        _insert_call_edge(store, pid, "m.C", "m.D")
        assert influence.dependency_cone_size(store, pid, "m.A") == 3


# ── Block 6: caller_cone_with_depth ──────────────────────────────────────────

class TestCallerConeWithDepth:
    def test_direct_caller_depth_one(self, store, pid):
        _insert_call_edge(store, pid, "m.B", "m.A")
        result = influence.caller_cone_with_depth(store, pid, ["m.A"])
        assert result == {"m.B": 1}

    def test_transitive_depth(self, store, pid):
        _insert_call_edge(store, pid, "m.B", "m.A")
        _insert_call_edge(store, pid, "m.C", "m.B")
        result = influence.caller_cone_with_depth(store, pid, ["m.A"])
        assert result == {"m.B": 1, "m.C": 2}

    def test_multi_seed_min_depth(self, store, pid):
        # seeds=[A, B], C calls both; min depth = 1 via either
        _insert_call_edge(store, pid, "m.C", "m.A")
        _insert_call_edge(store, pid, "m.C", "m.B")
        result = influence.caller_cone_with_depth(store, pid, ["m.A", "m.B"])
        assert result.get("m.C") == 1

    def test_seeds_excluded(self, store, pid):
        _insert_call_edge(store, pid, "m.B", "m.A")
        result = influence.caller_cone_with_depth(store, pid, ["m.A"])
        assert "m.A" not in result
        assert "m.B" in result

    def test_empty_seeds_returns_empty(self, store, pid):
        assert influence.caller_cone_with_depth(store, pid, []) == {}


# ── Block 7: cone_priority_ranks ─────────────────────────────────────────────

class TestConePriorityRanks:
    def test_empty_cone_returns_empty(self, store, pid):
        ranks = influence.cone_priority_ranks(store, pid, {})
        assert ranks == {"delta": [], "indegree": [], "distance": []}

    def test_delta_sorted_desc(self, store, pid):
        # A calls X and Y (δ=2); B calls X only (δ=1)
        _insert_call_edge(store, pid, "m.A", "m.X")
        _insert_call_edge(store, pid, "m.A", "m.Y")
        _insert_call_edge(store, pid, "m.B", "m.X")
        cone = {"m.A": 1, "m.B": 2}
        ranks = influence.cone_priority_ranks(store, pid, cone)
        delta_fqns = [fqn for fqn, _ in ranks["delta"]]
        assert delta_fqns[0] == "m.A"

    def test_distance_sorted_asc(self, store, pid):
        # A at depth 1, B at depth 2 → A comes first
        cone = {"m.A": 1, "m.B": 2}
        ranks = influence.cone_priority_ranks(store, pid, cone)
        dist_fqns = [fqn for fqn, _ in ranks["distance"]]
        assert dist_fqns[0] == "m.A"

    def test_top_n_respected(self, store, pid):
        cone = {f"m.F{i}": i for i in range(4)}
        ranks = influence.cone_priority_ranks(store, pid, cone, top_n=2)
        assert len(ranks["delta"]) <= 2
        assert len(ranks["indegree"]) <= 2
        assert len(ranks["distance"]) <= 2

    def test_indegree_sorted_desc(self, store, pid):
        # B has 2 callers (X and Y); A has 1 caller (X only)
        _insert_call_edge(store, pid, "m.X", "m.B")
        _insert_call_edge(store, pid, "m.Y", "m.B")
        _insert_call_edge(store, pid, "m.X", "m.A")
        cone = {"m.A": 1, "m.B": 1}
        ranks = influence.cone_priority_ranks(store, pid, cone)
        indegree_fqns = [fqn for fqn, _ in ranks["indegree"]]
        assert indegree_fqns[0] == "m.B"


# ── Block 8: crash_stacks store methods ──────────────────────────────────────

class TestCrashStacksStore:
    def test_insert_and_retrieve(self, store, pid):
        import json
        row_id = store.insert_crash_stack(pid, seq=5, stack_fqns_json='["a.fn"]')
        rows = store.recent_crash_stacks(pid)
        assert len(rows) == 1
        assert rows[0]["id"] == row_id
        assert json.loads(rows[0]["stack_fqns"]) == ["a.fn"]

    def test_recent_limit_respected(self, store, pid):
        for i in range(5):
            store.insert_crash_stack(pid, seq=i, stack_fqns_json=f'["fn{i}"]')
        rows = store.recent_crash_stacks(pid, n=3)
        assert len(rows) == 3

    def test_newest_first(self, store, pid):
        import json
        store.insert_crash_stack(pid, seq=1, stack_fqns_json='["fn1"]')
        store.insert_crash_stack(pid, seq=2, stack_fqns_json='["fn2"]')
        rows = store.recent_crash_stacks(pid, n=2)
        # Newest (seq=2) should come first (ORDER BY id DESC)
        assert json.loads(rows[0]["stack_fqns"]) == ["fn2"]

    def test_empty_when_none_stored(self, store, pid):
        rows = store.recent_crash_stacks(pid)
        assert rows == []

    def test_command_stored(self, store, pid):
        store.insert_crash_stack(pid, seq=1, stack_fqns_json='[]', command="npx vitest run")
        rows = store.recent_crash_stacks(pid)
        assert rows[0]["command"] == "npx vitest run"

    def test_project_isolation(self, store):
        pid1 = store.get_or_create_project("/proj1")
        pid2 = store.get_or_create_project("/proj2")
        store.insert_crash_stack(pid1, seq=1, stack_fqns_json='["a.fn"]')
        assert store.recent_crash_stacks(pid2) == []


# ── Block 9: intersect_cone_with_stack ───────────────────────────────────────

class TestIntersectConeWithStack:
    def test_overlap(self):
        cone = {"a.fn", "b.fn", "c.fn"}
        stack = {"b.fn", "d.fn"}
        result = influence.intersect_cone_with_stack(cone, stack)
        assert result == {"b.fn"}

    def test_no_overlap(self):
        cone = {"a.fn", "b.fn"}
        stack = {"x.fn", "y.fn"}
        assert influence.intersect_cone_with_stack(cone, stack) == set()

    def test_empty_cone(self):
        assert influence.intersect_cone_with_stack(set(), {"a.fn"}) == set()

    def test_empty_stack(self):
        assert influence.intersect_cone_with_stack({"a.fn"}, set()) == set()

    def test_full_overlap(self):
        cone = {"a.fn", "b.fn"}
        assert influence.intersect_cone_with_stack(cone, cone) == cone
