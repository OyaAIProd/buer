"""Tests for task_scope_breach signal (§2.3) — opt-in range checking.

Covers:
  - Fires when out of scope; silent when in scope or no scope declared
  - Message format includes the file and declared scope
  - Two-step state machine: notified_agent → escalated_user on recurrence (θ₂=1)
  - Resolves when back in scope (N_STABLE stable rounds)
  - resolved_by = back_in_scope
  - Clears automatically when scope is cleared
  - Boundary independence: boundary_breach still fires on out-of-project edits
  - R2: set_task_scope is user opt-in, not agent structural metadata
  - Store CRUD: set / clear / get_active_task_scope
  - MCP tools: set_task_scope / clear_task_scope
  - Delivery text: agent_message + user_message
"""
import json
import os

import pytest

from buer.store import Store
from buer import signals, delivery
from buer.signals import N_STABLE


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def store_and_project(tmp_path):
    store = Store(":memory:")
    pid = store.get_or_create_project(str(tmp_path))
    return store, pid, str(tmp_path)


# ── helpers ───────────────────────────────────────────────────────────────────

def _affected(tmp_path, rel_path: str, define: str = "fn", det_id: int = 1):
    """Build an affected entry for a path relative to tmp_path."""
    return (str(tmp_path / rel_path), define, det_id)


def _fake_inc(signal, target, details):
    class Row(dict):
        def __getitem__(self, k):
            return super().__getitem__(k)
    return Row({
        "id": 1,
        "signal": signal,
        "target_node": target,
        "state": "notified_agent",
        "details": json.dumps(details),
    })


# ── TestDetectFires: basic fire / no-fire conditions ─────────────────────────

class TestDetectFires:
    def test_fires_on_out_of_scope_file(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        store.set_task_scope(pid, ["user/**"])
        affected = [_affected(tmp_path, "admin/role.py")]
        signals.detect_task_scope_breach(store, pid, affected, root)
        incs = store.open_incidents(pid)
        assert len(incs) == 1
        assert incs[0]["signal"] == "task_scope_breach"

    def test_no_fire_for_in_scope_file(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        store.set_task_scope(pid, ["user/**"])
        affected = [_affected(tmp_path, "user/auth.py")]
        signals.detect_task_scope_breach(store, pid, affected, root)
        assert store.open_incidents(pid) == []

    def test_no_fire_without_scope(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        # no set_task_scope → detector is a no-op
        affected = [_affected(tmp_path, "admin/role.py")]
        signals.detect_task_scope_breach(store, pid, affected, root)
        assert store.open_incidents(pid) == []

    def test_no_fire_for_in_scope_multi_glob(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        store.set_task_scope(pid, ["user/**", "lib/**"])
        affected = [_affected(tmp_path, "lib/utils.py")]
        signals.detect_task_scope_breach(store, pid, affected, root)
        assert store.open_incidents(pid) == []

    def test_fires_for_out_of_scope_multi_glob(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        store.set_task_scope(pid, ["user/**", "lib/**"])
        affected = [_affected(tmp_path, "admin/role.py")]
        signals.detect_task_scope_breach(store, pid, affected, root)
        assert len(store.open_incidents(pid)) == 1

    def test_multiple_out_of_scope_files_fire_separately(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        store.set_task_scope(pid, ["user/**"])
        affected = [
            _affected(tmp_path, "admin/role.py", det_id=1),
            _affected(tmp_path, "config/settings.py", det_id=2),
        ]
        signals.detect_task_scope_breach(store, pid, affected, root)
        assert len(store.open_incidents(pid)) == 2

    def test_mixed_in_out_only_out_fires(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        store.set_task_scope(pid, ["user/**"])
        affected = [
            _affected(tmp_path, "user/auth.py", det_id=1),
            _affected(tmp_path, "admin/role.py", det_id=2),
        ]
        signals.detect_task_scope_breach(store, pid, affected, root)
        incs = store.open_incidents(pid)
        assert len(incs) == 1
        assert "admin" in incs[0]["target_node"]

    def test_idempotent(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        store.set_task_scope(pid, ["user/**"])
        affected = [_affected(tmp_path, "admin/role.py")]
        signals.detect_task_scope_breach(store, pid, affected, root)
        signals.detect_task_scope_breach(store, pid, affected, root)
        assert len(store.open_incidents(pid)) == 1

    def test_forbidden_fires_even_when_in_allowed(self, store_and_project, tmp_path):
        """File matches both allowed and forbidden → forbidden wins → breach."""
        store, pid, root = store_and_project
        store.set_task_scope(pid, ["user/**"], forbidden_globs=["**/secrets.py"])
        affected = [_affected(tmp_path, "user/secrets.py")]
        signals.detect_task_scope_breach(store, pid, affected, root)
        incs = store.open_incidents(pid)
        assert len(incs) == 1
        details = json.loads(incs[0]["details"])
        assert details["breach_reason"] == "forbidden"

    def test_forbidden_fires_outside_allowed(self, store_and_project, tmp_path):
        """File outside allowed AND matches forbidden → forbidden breach_reason."""
        store, pid, root = store_and_project
        store.set_task_scope(pid, ["user/**"], forbidden_globs=["**/secrets.py"])
        affected = [_affected(tmp_path, "admin/secrets.py")]
        signals.detect_task_scope_breach(store, pid, affected, root)
        incs = store.open_incidents(pid)
        assert len(incs) == 1
        details = json.loads(incs[0]["details"])
        assert details["breach_reason"] == "forbidden"

    def test_not_in_allowed_breach_reason(self, store_and_project, tmp_path):
        """Out-of-allowed, no forbidden match → breach_reason = not_in_allowed."""
        store, pid, root = store_and_project
        store.set_task_scope(pid, ["user/**"], forbidden_globs=["**/secrets.py"])
        affected = [_affected(tmp_path, "admin/role.py")]
        signals.detect_task_scope_breach(store, pid, affected, root)
        incs = store.open_incidents(pid)
        assert len(incs) == 1
        details = json.loads(incs[0]["details"])
        assert details["breach_reason"] == "not_in_allowed"

    def test_in_allowed_not_forbidden_no_fire(self, store_and_project, tmp_path):
        """File in allowed and not forbidden → no incident."""
        store, pid, root = store_and_project
        store.set_task_scope(pid, ["user/**"], forbidden_globs=["**/secrets.py"])
        affected = [_affected(tmp_path, "user/auth.py")]
        signals.detect_task_scope_breach(store, pid, affected, root)
        assert store.open_incidents(pid) == []


# ── TestDetailsAndMessage ─────────────────────────────────────────────────────

class TestDetailsAndMessage:
    def test_details_include_allowed_globs(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        store.set_task_scope(pid, ["user/**"])
        affected = [_affected(tmp_path, "admin/role.py")]
        signals.detect_task_scope_breach(store, pid, affected, root)
        details = json.loads(store.open_incidents(pid)[0]["details"])
        assert details["allowed_globs"] == ["user/**"]

    def test_details_note_contains_scope_and_file(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        store.set_task_scope(pid, ["user/**"])
        affected = [_affected(tmp_path, "admin/role.py")]
        signals.detect_task_scope_breach(store, pid, affected, root)
        details = json.loads(store.open_incidents(pid)[0]["details"])
        assert "user/**" in details["note"]
        assert "admin" in details["note"]

    def test_agent_message_includes_note(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        store.set_task_scope(pid, ["user/**"])
        admin_file = str(tmp_path / "admin" / "role.py")
        inc = _fake_inc("task_scope_breach", admin_file, {
            "file_path": admin_file,
            "allowed_globs": ["user/**"],
            "note": "你改了 admin/role.py，超出本次任务范围 user/**",
        })
        msg = delivery.agent_message(inc)
        assert "[BUER] task_scope_breach" in msg
        assert "user/**" in msg
        assert "admin" in msg

    def test_user_message_has_escalation_header(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        admin_file = str(tmp_path / "admin" / "role.py")
        inc = _fake_inc("task_scope_breach", admin_file, {
            "file_path": admin_file,
            "allowed_globs": ["user/**"],
            "note": "你改了 admin/role.py，超出本次任务范围 user/**",
        })
        msg = delivery.user_message(inc)
        assert "escalated to user" in msg
        assert "task_scope_breach" in msg
        assert "user/**" in msg


# ── TestStateMachine ──────────────────────────────────────────────────────────

class TestStateMachine:
    def test_open_to_notified_agent(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        store.set_task_scope(pid, ["user/**"])
        affected = [_affected(tmp_path, "admin/role.py")]
        signals.detect_task_scope_breach(store, pid, affected, root)
        signals.advance_incidents(store, pid, affected)
        incs = store.open_incidents(pid)
        assert incs[0]["state"] == "notified_agent"

    def test_notified_agent_recurs_escalates_to_user(self, store_and_project, tmp_path):
        """θ₂=1: second recurrence while notified_agent → escalated_user."""
        store, pid, root = store_and_project
        store.set_task_scope(pid, ["user/**"])
        affected = [_affected(tmp_path, "admin/role.py")]
        signals.detect_task_scope_breach(store, pid, affected, root)
        signals.advance_incidents(store, pid, affected)   # → notified_agent
        signals.advance_incidents(store, pid, affected)   # recur → escalated_user
        incs = store.open_incidents(pid)
        assert incs[0]["state"] == "escalated_user"

    def test_resolves_after_stable_rounds(self, store_and_project, tmp_path):
        """Back in scope for N_STABLE rounds → resolved."""
        store, pid, root = store_and_project
        store.set_task_scope(pid, ["user/**"])
        admin_file = str(tmp_path / "admin" / "role.py")
        user_file = str(tmp_path / "user" / "auth.py")
        affected_bad = [_affected(tmp_path, "admin/role.py")]
        affected_good = [_affected(tmp_path, "user/auth.py")]

        signals.detect_task_scope_breach(store, pid, affected_bad, root)
        signals.advance_incidents(store, pid, affected_bad)   # → notified_agent

        for _ in range(N_STABLE):
            signals.advance_incidents(store, pid, affected_good)

        assert store.open_incidents(pid) == []

    def test_resolved_by_back_in_scope(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        store.set_task_scope(pid, ["user/**"])
        affected_bad = [_affected(tmp_path, "admin/role.py")]
        affected_good = [_affected(tmp_path, "user/auth.py")]

        signals.detect_task_scope_breach(store, pid, affected_bad, root)
        signals.advance_incidents(store, pid, affected_bad)
        for _ in range(N_STABLE):
            signals.advance_incidents(store, pid, affected_good)

        row = store.con.execute(
            "SELECT resolved_by FROM incidents WHERE project_id = ?", (pid,)
        ).fetchone()
        assert row["resolved_by"] == "back_in_scope"

    def test_scope_cleared_auto_resolves(self, store_and_project, tmp_path):
        """Clearing scope → recurred=False → N_STABLE rounds → resolved."""
        store, pid, root = store_and_project
        store.set_task_scope(pid, ["user/**"])
        affected_bad = [_affected(tmp_path, "admin/role.py")]

        signals.detect_task_scope_breach(store, pid, affected_bad, root)
        signals.advance_incidents(store, pid, affected_bad)   # → notified_agent

        store.clear_task_scope(pid)  # user lifts the scope restriction

        for _ in range(N_STABLE):
            signals.advance_incidents(store, pid, affected_bad)  # scope=None → not recurred

        assert store.open_incidents(pid) == []

    def test_escalated_user_stays_open_while_breaching(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        store.set_task_scope(pid, ["user/**"])
        affected = [_affected(tmp_path, "admin/role.py")]

        signals.detect_task_scope_breach(store, pid, affected, root)
        signals.advance_incidents(store, pid, affected)   # → notified_agent
        signals.advance_incidents(store, pid, affected)   # → escalated_user
        signals.advance_incidents(store, pid, affected)   # still breaching

        incs = store.open_incidents(pid)
        assert len(incs) == 1
        assert incs[0]["state"] == "escalated_user"


# ── TestBoundaryIndependence ──────────────────────────────────────────────────

class TestBoundaryIndependence:
    def test_boundary_breach_fires_independently_of_scope(self, store_and_project, tmp_path):
        """Out-of-project path → boundary_breach regardless of task scope."""
        store, pid, root = store_and_project
        store.set_task_scope(pid, ["user/**"])
        outside_path = "/tmp/completely-outside/f.py"
        signals.detect_boundary_breach(store, pid, [outside_path])
        incs = store.open_incidents(pid)
        assert any(i["signal"] == "boundary_breach" for i in incs)

    def test_no_task_scope_no_task_scope_breach(self, store_and_project, tmp_path):
        """Without scope: editing any in-project path doesn't fire task_scope_breach."""
        store, pid, root = store_and_project
        for rel in ["admin/role.py", "config/settings.py", "random/file.py"]:
            affected = [_affected(tmp_path, rel)]
            signals.detect_task_scope_breach(store, pid, affected, root)
        assert store.open_incidents(pid) == []


# ── TestStoreOps ──────────────────────────────────────────────────────────────

class TestStoreOps:
    def test_get_active_scope_none_initially(self, store_and_project):
        store, pid, root = store_and_project
        assert store.get_active_task_scope(pid) is None

    def test_set_and_get(self, store_and_project):
        store, pid, root = store_and_project
        store.set_task_scope(pid, ["user/**", "lib/**"])
        scope = store.get_active_task_scope(pid)
        assert scope == {"allowed": ["user/**", "lib/**"], "forbidden": []}

    def test_set_replaces_existing(self, store_and_project):
        store, pid, root = store_and_project
        store.set_task_scope(pid, ["user/**"])
        store.set_task_scope(pid, ["admin/**"])
        scope = store.get_active_task_scope(pid)
        assert scope == {"allowed": ["admin/**"], "forbidden": []}

    def test_set_and_get_with_forbidden(self, store_and_project):
        store, pid, root = store_and_project
        store.set_task_scope(pid, ["user/**"], forbidden_globs=["**/secrets.py"])
        scope = store.get_active_task_scope(pid)
        assert scope == {"allowed": ["user/**"], "forbidden": ["**/secrets.py"]}

    def test_clear_returns_none(self, store_and_project):
        store, pid, root = store_and_project
        store.set_task_scope(pid, ["user/**"])
        store.clear_task_scope(pid)
        assert store.get_active_task_scope(pid) is None

    def test_clear_without_prior_set_is_noop(self, store_and_project):
        store, pid, root = store_and_project
        store.clear_task_scope(pid)  # should not raise
        assert store.get_active_task_scope(pid) is None

    def test_set_scope_history_preserved(self, store_and_project):
        """Replacing scope marks old one 'completed' rather than deleting it."""
        store, pid, root = store_and_project
        store.set_task_scope(pid, ["user/**"])
        store.set_task_scope(pid, ["admin/**"])
        all_rows = store.con.execute(
            "SELECT state FROM task_scopes WHERE project_id = ?", (pid,)
        ).fetchall()
        states = [r["state"] for r in all_rows]
        assert "completed" in states
        assert states.count("active") == 1


# ── TestMcpTools ──────────────────────────────────────────────────────────────

class TestMcpTools:
    def test_set_task_scope_returns_ok(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        from buer.mcp.server import _set_store_for_testing, set_task_scope
        _set_store_for_testing(store)
        result = set_task_scope(root, ["user/**"])
        assert "user/**" in result
        assert store.get_active_task_scope(pid) == {"allowed": ["user/**"], "forbidden": []}
        _set_store_for_testing(None)

    def test_set_task_scope_with_forbidden_returns_both(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        from buer.mcp.server import _set_store_for_testing, set_task_scope
        _set_store_for_testing(store)
        result = set_task_scope(root, ["user/**"], forbidden_globs=["**/secrets.py"])
        assert "user/**" in result
        assert "secrets.py" in result
        scope = store.get_active_task_scope(pid)
        assert scope == {"allowed": ["user/**"], "forbidden": ["**/secrets.py"]}
        _set_store_for_testing(None)

    def test_clear_task_scope_returns_ok(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        store.set_task_scope(pid, ["user/**"])
        from buer.mcp.server import _set_store_for_testing, clear_task_scope
        _set_store_for_testing(store)
        result = clear_task_scope(root)
        assert "cleared" in result.lower()
        assert store.get_active_task_scope(pid) is None
        _set_store_for_testing(None)

    def test_set_task_scope_no_project(self):
        from buer.mcp.server import _set_store_for_testing, set_task_scope
        _set_store_for_testing(Store(":memory:"))
        result = set_task_scope("/nonexistent/root", ["user/**"])
        assert "No project" in result
        _set_store_for_testing(None)

    def test_clear_task_scope_no_project(self):
        from buer.mcp.server import _set_store_for_testing, clear_task_scope
        _set_store_for_testing(Store(":memory:"))
        result = clear_task_scope("/nonexistent/root")
        assert "No project" in result
        _set_store_for_testing(None)

    def test_set_task_scope_empty_globs(self, store_and_project, tmp_path):
        store, pid, root = store_and_project
        from buer.mcp.server import _set_store_for_testing, set_task_scope
        _set_store_for_testing(store)
        result = set_task_scope(root, [])
        assert "cannot be empty" in result.lower()
        _set_store_for_testing(None)
