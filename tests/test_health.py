"""Tests for buer/health.py — §2.9 safety-net + §4.8 project_overview."""
import os

import pytest

from buer.store import Store
from buer import health
from buer.health import (
    MIN_DEFINES_FOR_CHECK,
    MIN_EDITS_FOR_CHECK,
    EDITS_SINCE_COMMIT_WARN,
    HEALTH_CHECK_PERIOD,
)


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def store_and_project(tmp_path):
    store = Store(":memory:")
    pid = store.get_or_create_project(str(tmp_path))
    return store, pid, str(tmp_path)


# ── helpers ───────────────────────────────────────────────────────────────────

def _insert_edits(store, project_id, n_total, *, n_distinct=None, file_path="/fake/f.py"):
    """Insert n_total determinations with n_distinct unique define names."""
    if n_distinct is None:
        n_distinct = max(n_total, MIN_DEFINES_FOR_CHECK)
    for i in range(n_total):
        define = f"fn_{i % n_distinct}"
        store.insert_determination(
            project_id,
            seq=store.next_seq(project_id),
            file_path=file_path,
            define_name=define,
            node_fingerprint=f"fp_{define}_{i}",
            edit_type="create" if i < n_distinct else "modify",
        )


def _large_project(store, project_id):
    """Insert enough edits + defines to pass the scale gate."""
    n = max(MIN_EDITS_FOR_CHECK, MIN_DEFINES_FOR_CHECK)
    _insert_edits(store, project_id, n_total=n, n_distinct=MIN_DEFINES_FOR_CHECK)


# ── detect_safety_net: scale gate ─────────────────────────────────────────────

class TestScaleGate:
    def test_no_warning_when_too_few_edits(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        _insert_edits(store, pid, n_total=MIN_EDITS_FOR_CHECK - 1, n_distinct=MIN_DEFINES_FOR_CHECK)
        assert health.detect_safety_net(store, pid, root) == []

    def test_no_warning_when_too_few_defines(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        _insert_edits(store, pid, n_total=MIN_EDITS_FOR_CHECK, n_distinct=MIN_DEFINES_FOR_CHECK - 1)
        assert health.detect_safety_net(store, pid, root) == []

    def test_fires_at_threshold(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        _large_project(store, pid)
        # tmp_path has no .git above it → at least no_git warning fires
        warnings = health.detect_safety_net(store, pid, root)
        assert len(warnings) >= 1


# ── detect_safety_net: no_tests ───────────────────────────────────────────────

class TestNoTestsWarning:
    def test_fires_when_no_test_files_and_no_test_runs(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        _large_project(store, pid)
        # tmp_path is empty — no test files
        warnings = health.detect_safety_net(store, pid, root)
        types = [w["type"] for w in warnings]
        assert "no_tests" in types

    def test_suppressed_by_test_file_on_disk(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        _large_project(store, pid)
        (tmp_path / "test_something.py").write_text("def test_it(): pass\n")
        warnings = health.detect_safety_net(store, pid, root)
        types = [w["type"] for w in warnings]
        assert "no_tests" not in types

    def test_suppressed_by_test_runs_in_db(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        _large_project(store, pid)
        # Insert a test_run — no test files on disk
        store.insert_test_run(pid, seq=1, source_path="/fake/junit.xml",
                              source_mtime="2026-01-01", passed=1, failed=0, skipped=0)
        warnings = health.detect_safety_net(store, pid, root)
        types = [w["type"] for w in warnings]
        assert "no_tests" not in types

    def test_message_includes_define_count(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        _large_project(store, pid)
        warnings = health.detect_safety_net(store, pid, root)
        no_tests = next(w for w in warnings if w["type"] == "no_tests")
        assert str(MIN_DEFINES_FOR_CHECK) in no_tests["message"]

    def test_message_mentions_detection_gap(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        _large_project(store, pid)
        warnings = health.detect_safety_net(store, pid, root)
        no_tests = next(w for w in warnings if w["type"] == "no_tests")
        assert "debug_loop" in no_tests["message"]


# ── detect_safety_net: no_git ─────────────────────────────────────────────────

class TestNoGitWarning:
    def test_fires_when_no_vcs(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        _large_project(store, pid)
        # tmp_path has no .git above it (under /tmp)
        warnings = health.detect_safety_net(store, pid, root)
        types = [w["type"] for w in warnings]
        assert "no_git" in types

    def test_suppressed_by_git_dir_in_root(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        _large_project(store, pid)
        (tmp_path / ".git").mkdir()
        warnings = health.detect_safety_net(store, pid, root)
        types = [w["type"] for w in warnings]
        assert "no_git" not in types

    def test_suppressed_by_git_in_parent(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        # Create .git in tmp_path but root is a subdir
        (tmp_path / ".git").mkdir()
        subdir = tmp_path / "src"
        subdir.mkdir()
        sub_pid = store.get_or_create_project(str(subdir))
        _insert_edits(store, sub_pid, n_total=MIN_EDITS_FOR_CHECK, n_distinct=MIN_DEFINES_FOR_CHECK)
        warnings = health.detect_safety_net(store, sub_pid, str(subdir))
        types = [w["type"] for w in warnings]
        assert "no_git" not in types

    def test_suppressed_by_hg(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        _large_project(store, pid)
        (tmp_path / ".hg").mkdir()
        warnings = health.detect_safety_net(store, pid, root)
        types = [w["type"] for w in warnings]
        assert "no_git" not in types

    def test_suppressed_by_svn(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        _large_project(store, pid)
        (tmp_path / ".svn").mkdir()
        warnings = health.detect_safety_net(store, pid, root)
        types = [w["type"] for w in warnings]
        assert "no_git" not in types

    def test_no_git_message_content(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        _large_project(store, pid)
        warnings = health.detect_safety_net(store, pid, root)
        no_git = next(w for w in warnings if w["type"] == "no_git")
        assert "version control" in no_git["message"]


# ── maybe_run_safety_net ──────────────────────────────────────────────────────

class TestMaybeRunSafetyNet:
    def test_no_delivery_below_period(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        _insert_edits(store, pid, n_total=HEALTH_CHECK_PERIOD - 1, n_distinct=MIN_DEFINES_FOR_CHECK)
        health.maybe_run_safety_net(store, pid, root)
        assert store.peek_deliveries(pid, channel="user") == []

    def test_no_delivery_non_multiple(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        _insert_edits(store, pid, n_total=HEALTH_CHECK_PERIOD + 1, n_distinct=MIN_DEFINES_FOR_CHECK)
        health.maybe_run_safety_net(store, pid, root)
        assert store.peek_deliveries(pid, channel="user") == []

    def test_delivers_at_exact_period(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        _insert_edits(store, pid, n_total=HEALTH_CHECK_PERIOD, n_distinct=MIN_DEFINES_FOR_CHECK)
        health.maybe_run_safety_net(store, pid, root)
        rows = store.peek_deliveries(pid, channel="user")
        assert len(rows) == 1
        assert "⚠" in rows[0]["message"]

    def test_fires_only_once_per_net_type(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        _insert_edits(store, pid, n_total=HEALTH_CHECK_PERIOD, n_distinct=MIN_DEFINES_FOR_CHECK)
        # First check fires
        health.maybe_run_safety_net(store, pid, root)
        assert len(store.peek_deliveries(pid, channel="user")) == 1

        # Second check at 2× period — no new delivery (already triggered)
        _insert_edits(store, pid, n_total=HEALTH_CHECK_PERIOD, n_distinct=MIN_DEFINES_FOR_CHECK)
        health.maybe_run_safety_net(store, pid, root)
        assert len(store.peek_deliveries(pid, channel="user")) == 1  # unchanged

    def test_dismissed_blocks_delivery(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        store.dismiss_safety_net_warning(pid, "no_tests")
        store.dismiss_safety_net_warning(pid, "no_git")
        _insert_edits(store, pid, n_total=HEALTH_CHECK_PERIOD, n_distinct=MIN_DEFINES_FOR_CHECK)
        health.maybe_run_safety_net(store, pid, root)
        assert store.peek_deliveries(pid, channel="user") == []

    def test_delivery_message_contains_header(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        _insert_edits(store, pid, n_total=HEALTH_CHECK_PERIOD, n_distinct=MIN_DEFINES_FOR_CHECK)
        health.maybe_run_safety_net(store, pid, root)
        rows = store.peek_deliveries(pid, channel="user")
        assert "safety net" in rows[0]["message"]

    def test_incident_id_is_null(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        _insert_edits(store, pid, n_total=HEALTH_CHECK_PERIOD, n_distinct=MIN_DEFINES_FOR_CHECK)
        health.maybe_run_safety_net(store, pid, root)
        rows = store.peek_deliveries(pid, channel="user")
        assert rows[0]["incident_id"] is None


# ── safety_net_state / store methods ─────────────────────────────────────────

class TestSafetyNetStoreOps:
    def test_state_none_when_no_row(self, store_and_project):
        store, pid, root = store_and_project
        assert store.safety_net_state(pid, "no_tests") is None

    def test_state_triggered_after_trigger(self, store_and_project):
        store, pid, root = store_and_project
        store.trigger_safety_net(pid, "no_tests")
        assert store.safety_net_state(pid, "no_tests") == "triggered"

    def test_state_dismissed_after_dismiss(self, store_and_project):
        store, pid, root = store_and_project
        store.trigger_safety_net(pid, "no_tests")
        store.dismiss_safety_net_warning(pid, "no_tests")
        assert store.safety_net_state(pid, "no_tests") == "dismissed"

    def test_dismiss_without_prior_trigger(self, store_and_project):
        store, pid, root = store_and_project
        store.dismiss_safety_net_warning(pid, "no_git")
        assert store.safety_net_state(pid, "no_git") == "dismissed"

    def test_trigger_idempotent(self, store_and_project):
        store, pid, root = store_and_project
        store.trigger_safety_net(pid, "no_tests")
        store.trigger_safety_net(pid, "no_tests")  # second call should not change state
        assert store.safety_net_state(pid, "no_tests") == "triggered"

    def test_trigger_does_not_overwrite_dismiss(self, store_and_project):
        store, pid, root = store_and_project
        store.dismiss_safety_net_warning(pid, "no_tests")
        store.trigger_safety_net(pid, "no_tests")  # INSERT OR IGNORE → no-op
        assert store.safety_net_state(pid, "no_tests") == "dismissed"

    def test_types_are_independent(self, store_and_project):
        store, pid, root = store_and_project
        store.trigger_safety_net(pid, "no_tests")
        assert store.safety_net_state(pid, "no_git") is None


# ── project_overview ──────────────────────────────────────────────────────────

class TestProjectOverview:
    def test_shows_file_and_define_counts(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        _insert_edits(store, pid, n_total=5, n_distinct=3, file_path=str(tmp_path / "a.py"))
        result = health.project_overview(store, pid, root)
        assert "[BUER]" in result
        assert "define" in result

    def test_shows_hub_nodes(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        store.upsert_call_edge(pid, "a.caller", "b.hub", "call")
        store.upsert_call_edge(pid, "c.caller", "b.hub", "call")
        result = health.project_overview(store, pid, root)
        assert "b.hub" in result

    def test_shows_active_signals(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        import json
        store.write_incident(pid, signal="stuck_region",
                             target_node="/f.py::fn",
                             details=json.dumps({"chain_length": 5}))
        result = health.project_overview(store, pid, root)
        assert "stuck_region" in result

    def test_small_project_no_safety_warning(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        # Only 2 edits → below scale gate
        _insert_edits(store, pid, n_total=2, n_distinct=2)
        result = health.project_overview(store, pid, root)
        assert "⚠" not in result
        assert "project too small" in result

    def test_honest_no_test_message(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        _large_project(store, pid)
        result = health.project_overview(store, pid, root)
        # No tests → should mention BUER's own blind spot
        assert "invisible to BUER" in result

    def test_dismissed_warning_shows_note(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        _large_project(store, pid)
        store.dismiss_safety_net_warning(pid, "no_tests")
        result = health.project_overview(store, pid, root)
        # Warning still visible in overview even after dismiss (conditions still true)
        assert "no_tests" in result
        assert "dismissed" in result.lower() or "关闭" in result


# ── MCP tool wrappers ──────────────────────────────────────────────────────────

class TestMcpTools:
    def test_project_overview_no_project(self):
        from buer.mcp.server import _set_store_for_testing, project_overview
        _set_store_for_testing(Store(":memory:"))
        result = project_overview("/nonexistent/root")
        assert "No project" in result
        _set_store_for_testing(None)

    def test_project_overview_returns_summary(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        from buer.mcp.server import _set_store_for_testing, project_overview
        _set_store_for_testing(store)
        result = project_overview(root)
        assert "[BUER]" in result
        _set_store_for_testing(None)

    def test_dismiss_unknown_net_type(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        from buer.mcp.server import _set_store_for_testing, dismiss_safety_net_warning
        _set_store_for_testing(store)
        result = dismiss_safety_net_warning(root, "invalid_type")
        assert "Unknown" in result
        _set_store_for_testing(None)

    def test_dismiss_no_project(self):
        from buer.mcp.server import _set_store_for_testing, dismiss_safety_net_warning
        _set_store_for_testing(Store(":memory:"))
        result = dismiss_safety_net_warning("/nonexistent/root", "no_tests")
        assert "No project" in result
        _set_store_for_testing(None)

    def test_dismiss_success(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        from buer.mcp.server import _set_store_for_testing, dismiss_safety_net_warning
        _set_store_for_testing(store)
        result = dismiss_safety_net_warning(root, "no_tests")
        assert "dismissed" in result.lower()
        assert store.safety_net_state(pid, "no_tests") == "dismissed"
        _set_store_for_testing(None)


# ── 84a6ca6: safety_net delivery kind must be 'suggestion' ───────────────────

class TestSafetyNetKind:
    """Verify that safety net warnings are enqueued as kind='suggestion'.

    84a6ca6 changed health.py line 221 from the default kind='alert' to
    kind='suggestion'.  These two tests pin that behaviour so any revert
    causes an immediate assertion failure.
    """

    def test_safety_net_delivery_kind_is_suggestion(
        self, store_and_project, tmp_path, monkeypatch
    ):
        """maybe_run_safety_net must write kind='suggestion', not 'alert'."""
        store, pid, root = store_and_project

        # Force the period gate to fire without inserting HEALTH_CHECK_PERIOD rows.
        monkeypatch.setattr(
            health, "_count_total_edits",
            lambda s, p: health.HEALTH_CHECK_PERIOD,
        )
        # Return a deterministic warning so new_warnings is non-empty.
        monkeypatch.setattr(
            health, "detect_safety_net",
            lambda s, p, r: [{"type": "no_git", "message": "stale commits"}],
        )

        health.maybe_run_safety_net(store, pid, root)

        rows = store.con.execute(
            "SELECT kind FROM pending_deliveries WHERE project_id = ?", (pid,)
        ).fetchall()
        assert len(rows) == 1, "expected exactly one delivery row"
        assert rows[0]["kind"] == "suggestion", (
            "safety net delivery must be kind='suggestion' so Stop hook never blocks on it"
        )

    def test_safety_net_suggestion_not_taken_by_stop(self, store_and_project):
        """A safety net suggestion must be invisible to Stop's alert-only take."""
        store, pid, root = store_and_project

        store.enqueue_delivery(pid, None, "user", "safety net warning", kind="suggestion")

        # Stop handler calls take_user_deliveries(pid, kinds=("alert",)) — must return empty.
        alert_rows = store.take_user_deliveries(pid, kinds=("alert",))
        assert alert_rows == [], "Stop must not consume a safety net suggestion"

        # SessionStart handler calls take_user_deliveries(pid, kinds=("suggestion",)) — returns 1.
        suggestion_rows = store.take_user_deliveries(pid, kinds=("suggestion",))
        assert len(suggestion_rows) == 1
