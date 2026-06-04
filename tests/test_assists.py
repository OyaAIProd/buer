"""Tests for buer/assists.py — §4.9 / §4.10 / §3.8 / §4.11 assistive functions.

無 SDT 對位、純工程輔助.
"""
import os

import pytest

from buer.store import Store
from buer import assists
from buer.assists import (
    HUB_THRESHOLD,
    CHANGE_FREQ_THRESHOLD,
    BLAST_RADIUS_THRESHOLD,
    MIN_EDITS_BEFORE_COMMIT_SUGGEST,
    STABLE_WINDOW,
    InlineAssist,
)


# ── fixtures & helpers ────────────────────────────────────────────────────────

@pytest.fixture
def store_and_project(tmp_path):
    store = Store(":memory:")
    pid = store.get_or_create_project(str(tmp_path))
    return store, pid


def _det(store, pid, seq, file_path, define_name, fp=None):
    """Insert one determination, return its id."""
    return store.insert_determination(
        pid,
        seq=seq,
        file_path=file_path,
        define_name=define_name,
        node_fingerprint=fp or f"fp_{define_name}_{seq}",
        edit_type="create",
    )


def _insert_n_defs(store, pid, root, n, *, base_file=None):
    """Insert n distinct defines so there are enough for checks."""
    for i in range(n):
        fp = base_file or os.path.join(root, f"f{i}.py")
        _det(store, pid, i + 1, fp, f"fn_{i}")


# ── TestAssistAddTests ────────────────────────────────────────────────────────

class TestAssistAddTests:
    def test_no_uncovered_when_all_covered(self, store_and_project, tmp_path):
        store, pid = store_and_project
        root = str(tmp_path)
        fp = str(tmp_path / "a.py")
        _det(store, pid, 1, fp, "fn_a")
        store.insert_coverage_entry(pid, "test_a::test_fn", "fn_a")

        result = assists.assist_add_tests(store, pid, root)
        assert "no tests to add" in result

    def test_lists_uncovered_define(self, store_and_project, tmp_path):
        store, pid = store_and_project
        root = str(tmp_path)
        fp = str(tmp_path / "a.py")
        _det(store, pid, 1, fp, "fn_uncovered")

        result = assists.assist_add_tests(store, pid, root)
        assert "fn_uncovered" in result

    def test_covered_define_excluded(self, store_and_project, tmp_path):
        store, pid = store_and_project
        root = str(tmp_path)
        fp = str(tmp_path / "a.py")
        _det(store, pid, 1, fp, "fn_covered")
        _det(store, pid, 2, fp, "fn_uncovered")
        store.insert_coverage_entry(pid, "test::test_fn", "fn_covered")

        result = assists.assist_add_tests(store, pid, root)
        assert "fn_uncovered" in result
        assert "fn_covered" not in result

    def test_ranking_by_caller_count(self, store_and_project, tmp_path):
        """Define with more callers should appear before one with fewer."""
        store, pid = store_and_project
        root = str(tmp_path)
        fp_a = str(tmp_path / "a.py")
        fp_b = str(tmp_path / "b.py")
        _det(store, pid, 1, fp_a, "fn_hub")
        _det(store, pid, 2, fp_b, "fn_leaf")

        # Add callers for fn_hub
        mod_a = "a"
        store.upsert_call_edge(pid, "x.caller1", f"py::{mod_a}.fn_hub", "call")
        store.upsert_call_edge(pid, "x.caller2", f"py::{mod_a}.fn_hub", "call")
        store.upsert_call_edge(pid, "x.caller3", f"py::{mod_a}.fn_hub", "call")

        result = assists.assist_add_tests(store, pid, root)
        # fn_hub should appear before fn_leaf
        pos_hub = result.find("fn_hub")
        pos_leaf = result.find("fn_leaf")
        assert pos_hub < pos_leaf

    def test_ranking_by_change_freq(self, store_and_project, tmp_path):
        """Define with more versions (change_freq) should appear before fresher one."""
        store, pid = store_and_project
        root = str(tmp_path)
        fp = str(tmp_path / "a.py")
        # fn_frequent modified 3 times, fn_new only once
        _det(store, pid, 1, fp, "fn_frequent", fp="fp1")
        _det(store, pid, 2, fp, "fn_frequent", fp="fp2")
        _det(store, pid, 3, fp, "fn_frequent", fp="fp3")
        _det(store, pid, 4, fp, "fn_new", fp="fp_new")

        result = assists.assist_add_tests(store, pid, root)
        pos_freq = result.find("fn_frequent")
        pos_new = result.find("fn_new")
        assert pos_freq < pos_new

    def test_change_freq_tag_shown(self, store_and_project, tmp_path):
        store, pid = store_and_project
        root = str(tmp_path)
        fp = str(tmp_path / "a.py")
        for i in range(CHANGE_FREQ_THRESHOLD):
            _det(store, pid, i + 1, fp, "fn_busy", fp=f"fp{i}")

        result = assists.assist_add_tests(store, pid, root)
        assert f"edited {CHANGE_FREQ_THRESHOLD}x" in result

    def test_hub_tag_shown(self, store_and_project, tmp_path):
        store, pid = store_and_project
        root = str(tmp_path)
        fp = str(tmp_path / "a.py")
        _det(store, pid, 1, fp, "fn_hub")
        for i in range(HUB_THRESHOLD):
            store.upsert_call_edge(pid, f"caller{i}", "py::a.fn_hub", "call")

        result = assists.assist_add_tests(store, pid, root)
        assert f"{HUB_THRESHOLD} callers" in result

    def test_commit_reminder_in_output(self, store_and_project, tmp_path):
        """Output must remind user to commit before bulk test additions."""
        store, pid = store_and_project
        root = str(tmp_path)
        _det(store, pid, 1, str(tmp_path / "a.py"), "fn_a")

        result = assists.assist_add_tests(store, pid, root)
        assert "git commit" in result

    def test_no_signals_affected(self, store_and_project, tmp_path):
        """assist_add_tests must not write any incidents."""
        store, pid = store_and_project
        root = str(tmp_path)
        _det(store, pid, 1, str(tmp_path / "a.py"), "fn_a")
        assists.assist_add_tests(store, pid, root)
        assert store.open_incidents(pid) == []

    def test_custom_target_in_output(self, store_and_project, tmp_path):
        store, pid = store_and_project
        root = str(tmp_path)
        _det(store, pid, 1, str(tmp_path / "a.py"), "fn_a")
        result = assists.assist_add_tests(store, pid, root, target="cover auth module")
        assert "cover auth module" in result


# ── TestShellTestDetection ────────────────────────────────────────────────────

class TestShellTestDetection:
    def test_flags_function_without_assert(self, tmp_path):
        (tmp_path / "test_empty.py").write_text(
            "def test_nothing():\n    pass\n"
        )
        shells = assists._detect_shell_tests(str(tmp_path))
        assert any("test_nothing" in s for s in shells)

    def test_no_flag_when_assert_present(self, tmp_path):
        (tmp_path / "test_good.py").write_text(
            "def test_real():\n    assert 1 == 1\n"
        )
        shells = assists._detect_shell_tests(str(tmp_path))
        assert not any("test_real" in s for s in shells)

    def test_shell_test_shown_in_assist_add_tests(self, store_and_project, tmp_path):
        store, pid = store_and_project
        root = str(tmp_path)
        _det(store, pid, 1, str(tmp_path / "a.py"), "fn_a")
        (tmp_path / "test_shell.py").write_text(
            "def test_empty():\n    pass\n"
        )
        result = assists.assist_add_tests(store, pid, root)
        assert "shell" in result
        assert "test_empty" in result

    def test_skips_pycache(self, tmp_path):
        cache_dir = tmp_path / "__pycache__"
        cache_dir.mkdir()
        (cache_dir / "test_cached.py").write_text(
            "def test_cached():\n    pass\n"
        )
        shells = assists._detect_shell_tests(str(tmp_path))
        assert not any("__pycache__" in s for s in shells)


# ── TestAssistCommitTiming ────────────────────────────────────────────────────

class TestAssistCommitTiming:
    def _setup_good_point(self, store, pid, root):
        """Cluster in a.py (seqs 1-3), padding in b.py (seqs 4-23).

        Current affected = last pad in b.py → cluster in a.py is stable.
        After setup: cur_max=23, edits_since=23, cooldown_gap=23, cluster_gap=20.
        All gates pass when affected = [(fp_b, "pad_19", 23)].
        """
        fp_a = os.path.join(root, "a.py")
        fp_b = os.path.join(root, "b.py")
        for i in range(3):
            _det(store, pid, i + 1, fp_a, f"fn_{i}")
        for j in range(20):
            _det(store, pid, 4 + j, fp_b, f"pad_{j}")
        return [(fp_b, "pad_19", 23)]

    def test_fires_at_good_commit_point(self, store_and_project, tmp_path):
        store, pid = store_and_project
        root = str(tmp_path)
        affected = self._setup_good_point(store, pid, root)
        assist = assists._build_commit_assist(store, pid, root, affected)
        assert assist.should_fire is True
        assert assist.kind == "commit"

    def test_no_fire_too_few_edits(self, store_and_project, tmp_path):
        store, pid = store_and_project
        root = str(tmp_path)
        fp = os.path.join(root, "a.py")
        for i in range(MIN_EDITS_BEFORE_COMMIT_SUGGEST - 1):
            _det(store, pid, i + 1, fp, f"fn_{i}")
        assist = assists._build_commit_assist(store, pid, root, [])
        assert assist.should_fire is False

    def test_no_fire_when_open_regression(self, store_and_project, tmp_path):
        store, pid = store_and_project
        root = str(tmp_path)
        affected = self._setup_good_point(store, pid, root)
        store.write_incident(pid, signal="regression", target_node="fn_a")
        assist = assists._build_commit_assist(store, pid, root, affected)
        assert assist.should_fire is False

    def test_no_fire_cluster_recently_changed(self, store_and_project, tmp_path):
        """Cluster in a.py was changed too recently — within STABLE_WINDOW."""
        store, pid = store_and_project
        root = str(tmp_path)
        fp_a = os.path.join(root, "a.py")
        fp_b = os.path.join(root, "b.py")
        # Padding first (for cooldown), then cluster (recent), then one more pad
        for j in range(20):
            _det(store, pid, j + 1, fp_b, f"pad_{j}")
        for i in range(3):
            _det(store, pid, 21 + i, fp_a, f"fn_{i}")   # cluster at seqs 21-23
        # One more unrelated pad to make cur_max = 24
        _det(store, pid, 24, fp_b, "pad_final")
        # affected = unrelated pad; cluster (fp_a, max_seq=23) is 24-23=1 < STABLE_WINDOW
        affected = [(fp_b, "pad_final", 24)]
        assist = assists._build_commit_assist(store, pid, root, affected)
        assert assist.should_fire is False

    def test_no_fire_empty_cluster(self, store_and_project, tmp_path):
        """Currently editing the only file in the cluster → cluster_defs empty."""
        store, pid = store_and_project
        root = str(tmp_path)
        fp = os.path.join(root, "a.py")
        for i in range(30):
            _det(store, pid, i + 1, fp, f"fn_{i}")
        # affected = same file as all changed_defs
        affected = [(fp, "fn_29", 30)]
        assist = assists._build_commit_assist(store, pid, root, affected)
        assert assist.should_fire is False

    def test_message_contains_edit_count(self, store_and_project, tmp_path):
        store, pid = store_and_project
        root = str(tmp_path)
        affected = self._setup_good_point(store, pid, root)
        assist = assists._build_commit_assist(store, pid, root, affected)
        assert "edits" in assist.message

    def test_message_says_looks_like(self, store_and_project, tmp_path):
        """Message must use hedged phrasing, not assert 'complete'."""
        store, pid = store_and_project
        root = str(tmp_path)
        affected = self._setup_good_point(store, pid, root)
        assist = assists._build_commit_assist(store, pid, root, affected)
        assert "stopping point" in assist.message
        # Must disclaim "逻辑完整", not assert it
        assert "semantic completeness check" in assist.message

    def test_acknowledge_commit_resets_tracking(self, store_and_project, tmp_path):
        store, pid = store_and_project
        root = str(tmp_path)
        fp = os.path.join(root, "a.py")
        for i in range(5):
            _det(store, pid, i + 1, fp, f"fn_{i}")
        assists.acknowledge_commit(store, pid)
        state = store.get_assist_state(pid)
        assert state["last_commit_seq"] == store.max_seq(pid)

    def test_acknowledge_commit_message(self, store_and_project, tmp_path):
        store, pid = store_and_project
        fp = os.path.join(str(tmp_path), "a.py")
        for i in range(3):
            _det(store, pid, i + 1, fp, f"fn_{i}")
        result = assists.acknowledge_commit(store, pid)
        assert "commit point recorded" in result and "seq=" in result


# ── TestAssistBlastRadius ─────────────────────────────────────────────────────

class TestAssistBlastRadius:
    def test_no_fire_below_threshold(self, store_and_project, tmp_path):
        store, pid = store_and_project
        root = str(tmp_path)
        fp = str(tmp_path / "a.py")
        det_id = _det(store, pid, 1, fp, "fn_small")
        # Only BLAST_RADIUS_THRESHOLD - 1 callers
        for i in range(BLAST_RADIUS_THRESHOLD - 1):
            store.upsert_call_edge(pid, f"caller{i}", "py::a.fn_small", "call")

        affected = [(fp, "fn_small", det_id)]
        assist = assists._build_blast_radius_assist(store, pid, affected, root)
        assert assist.should_fire is False

    def test_fires_at_threshold(self, store_and_project, tmp_path):
        store, pid = store_and_project
        root = str(tmp_path)
        fp = str(tmp_path / "a.py")
        det_id = _det(store, pid, 1, fp, "fn_hub")
        for i in range(BLAST_RADIUS_THRESHOLD):
            store.upsert_call_edge(pid, f"caller{i}", "py::a.fn_hub", "call")

        affected = [(fp, "fn_hub", det_id)]
        assist = assists._build_blast_radius_assist(store, pid, affected, root)
        assert assist.should_fire is True
        assert assist.kind == "blast_radius"

    def test_message_says_at_least(self, store_and_project, tmp_path):
        """Message must say '至少' to be honest about static analysis limits."""
        store, pid = store_and_project
        root = str(tmp_path)
        fp = str(tmp_path / "a.py")
        det_id = _det(store, pid, 1, fp, "fn_hub")
        for i in range(BLAST_RADIUS_THRESHOLD):
            store.upsert_call_edge(pid, f"caller{i}", "py::a.fn_hub", "call")

        affected = [(fp, "fn_hub", det_id)]
        assist = assists._build_blast_radius_assist(store, pid, affected, root)
        assert "at least" in assist.message
        assert "dynamic dispatch" in assist.message

    def test_message_no_bug_assertion(self, store_and_project, tmp_path):
        """Message must NOT assert 'will cause bugs'."""
        store, pid = store_and_project
        root = str(tmp_path)
        fp = str(tmp_path / "a.py")
        det_id = _det(store, pid, 1, fp, "fn_hub")
        for i in range(BLAST_RADIUS_THRESHOLD):
            store.upsert_call_edge(pid, f"caller{i}", "py::a.fn_hub", "call")

        affected = [(fp, "fn_hub", det_id)]
        assist = assists._build_blast_radius_assist(store, pid, affected, root)
        # Must disclaim "会出 bug", not assert it
        assert "not a 'will cause bugs' assertion" in assist.message

    def test_picks_highest_impact_define(self, store_and_project, tmp_path):
        """When multiple defines qualify, pick the one with most callers."""
        store, pid = store_and_project
        root = str(tmp_path)
        fp_a = str(tmp_path / "a.py")
        fp_b = str(tmp_path / "b.py")
        det_a = _det(store, pid, 1, fp_a, "fn_big")
        det_b = _det(store, pid, 2, fp_b, "fn_small")
        # fn_big has BLAST_RADIUS_THRESHOLD+2 callers, fn_small exactly threshold
        for i in range(BLAST_RADIUS_THRESHOLD + 2):
            store.upsert_call_edge(pid, f"big_caller{i}", "py::a.fn_big", "call")
        for i in range(BLAST_RADIUS_THRESHOLD):
            store.upsert_call_edge(pid, f"small_caller{i}", "py::b.fn_small", "call")

        affected = [(fp_a, "fn_big", det_a), (fp_b, "fn_small", det_b)]
        assist = assists._build_blast_radius_assist(store, pid, affected, root)
        assert "fn_big" in assist.message

    def test_no_fire_for_empty_affected(self, store_and_project, tmp_path):
        store, pid = store_and_project
        root = str(tmp_path)
        assist = assists._build_blast_radius_assist(store, pid, [], root)
        assert assist.should_fire is False


# ── TestArbitrateInlineAssists ────────────────────────────────────────────────

class TestArbitrateInlineAssists:
    def test_empty_candidates(self):
        assert assists.arbitrate_inline_assists([]) is None

    def test_none_should_fire(self):
        a = InlineAssist(kind="commit", should_fire=False, message="x")
        b = InlineAssist(kind="blast_radius", should_fire=False, message="y")
        assert assists.arbitrate_inline_assists([a, b]) is None

    def test_single_active(self):
        a = InlineAssist(kind="blast_radius", should_fire=True, message="msg")
        result = assists.arbitrate_inline_assists([a])
        assert result is a

    def test_commit_wins_over_blast_radius(self):
        """commit has higher priority than blast_radius (§4.11)."""
        commit = InlineAssist(kind="commit", should_fire=True, message="c")
        blast = InlineAssist(kind="blast_radius", should_fire=True, message="b")
        result = assists.arbitrate_inline_assists([commit, blast])
        assert result is commit

    def test_blast_radius_order_reversed_still_commit_wins(self):
        """Priority must not depend on list order."""
        commit = InlineAssist(kind="commit", should_fire=True, message="c")
        blast = InlineAssist(kind="blast_radius", should_fire=True, message="b")
        result = assists.arbitrate_inline_assists([blast, commit])
        assert result is commit

    def test_only_blast_radius_active(self):
        commit = InlineAssist(kind="commit", should_fire=False, message="")
        blast = InlineAssist(kind="blast_radius", should_fire=True, message="b")
        result = assists.arbitrate_inline_assists([commit, blast])
        assert result is blast


# ── TestRunInlineAssists ──────────────────────────────────────────────────────

class TestRunInlineAssists:
    def _setup_stable_project(self, store, pid, root):
        """Cluster in a.py (seqs 1-3), padding in b.py (seqs 4-23).

        Returns the affected list for the 'current' edit (last pad in b.py).
        """
        fp_a = os.path.join(root, "a.py")
        fp_b = os.path.join(root, "b.py")
        for i in range(3):
            _det(store, pid, i + 1, fp_a, f"fn_{i}")
        for j in range(20):
            _det(store, pid, 4 + j, fp_b, f"pad_{j}")
        return [(fp_b, "pad_19", 23)]

    def test_enqueues_winner_to_user_channel(self, store_and_project, tmp_path):
        store, pid = store_and_project
        root = str(tmp_path)
        affected = self._setup_stable_project(store, pid, root)

        assists.run_inline_assists(store, pid, affected, root)

        deliveries = store.peek_deliveries(pid, channel="user")
        assert any("stopping point" in d["message"] or "💡" in d["message"] for d in deliveries)

    def test_no_enqueue_when_no_winner(self, store_and_project, tmp_path):
        store, pid = store_and_project
        root = str(tmp_path)
        # Too few edits — nothing should fire
        _det(store, pid, 1, os.path.join(root, "a.py"), "fn_a")

        assists.run_inline_assists(store, pid, [], root)

        deliveries = store.peek_deliveries(pid, channel="user")
        assert deliveries == []

    def test_signals_not_cleared_by_inline_assists(self, store_and_project, tmp_path):
        """Inline assists must not affect the signal/incident channel."""
        store, pid = store_and_project
        root = str(tmp_path)
        inc_id = store.write_incident(pid, signal="stuck_region", target_node="fn_a")
        affected = self._setup_stable_project(store, pid, root)

        assists.run_inline_assists(store, pid, affected, root)

        inc = store.con.execute(
            "SELECT state FROM incidents WHERE id = ?", (inc_id,)
        ).fetchone()
        assert inc["state"] == "open"

    def test_only_one_winner_per_call(self, store_and_project, tmp_path):
        """Arbitration: at most one assist message per reconcile call."""
        store, pid = store_and_project
        root = str(tmp_path)
        fp_hub = str(tmp_path / "hub.py")
        fp_b = str(tmp_path / "b.py")

        # Set up a good commit point (cluster in a.py, padding in b.py)
        affected = self._setup_stable_project(store, pid, root)

        # Also make fn_hub a high-blast define (being edited right now in hub.py)
        det_id = _det(store, pid, 100, fp_hub, "fn_hub")
        for i in range(BLAST_RADIUS_THRESHOLD):
            store.upsert_call_edge(pid, f"c{i}", "py::hub.fn_hub", "call")

        # Both commit and blast_radius qualify; only one should be enqueued
        affected_with_hub = [(fp_hub, "fn_hub", det_id)] + affected
        pre_count = len(store.peek_deliveries(pid, channel="user"))
        assists.run_inline_assists(store, pid, affected_with_hub, root)
        post_count = len(store.peek_deliveries(pid, channel="user"))

        assert post_count - pre_count == 1


# ── TestMcpTools ──────────────────────────────────────────────────────────────

class TestMcpTools:
    def test_assist_add_tests_tool(self, tmp_path):
        from buer.mcp.server import _set_store_for_testing, assist_add_tests as tool

        store = Store(":memory:")
        pid = store.get_or_create_project(str(tmp_path))
        _det(store, pid, 1, str(tmp_path / "a.py"), "fn_a")
        _set_store_for_testing(store)
        try:
            result = tool(str(tmp_path))
            assert "fn_a" in result or "no tests to add" in result
        finally:
            _set_store_for_testing(None)

    def test_acknowledge_commit_tool(self, tmp_path):
        from buer.mcp.server import _set_store_for_testing, acknowledge_commit as tool

        store = Store(":memory:")
        pid = store.get_or_create_project(str(tmp_path))
        _det(store, pid, 1, str(tmp_path / "a.py"), "fn_a")
        _set_store_for_testing(store)
        try:
            result = tool(str(tmp_path))
            assert "commit point recorded" in result
        finally:
            _set_store_for_testing(None)

    def test_unknown_project_returns_message(self, tmp_path):
        from buer.mcp.server import _set_store_for_testing, acknowledge_commit as tool

        store = Store(":memory:")
        _set_store_for_testing(store)
        try:
            result = tool(str(tmp_path))
            assert "No project registered" in result
        finally:
            _set_store_for_testing(None)

    def test_assist_add_tests_with_target(self, tmp_path):
        from buer.mcp.server import _set_store_for_testing, assist_add_tests as tool

        store = Store(":memory:")
        pid = store.get_or_create_project(str(tmp_path))
        _det(store, pid, 1, str(tmp_path / "a.py"), "fn_a")
        _set_store_for_testing(store)
        try:
            result = tool(str(tmp_path), target="cover auth flows")
            assert "cover auth flows" in result or "no tests to add" in result
        finally:
            _set_store_for_testing(None)
