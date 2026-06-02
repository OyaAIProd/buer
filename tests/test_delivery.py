"""Tests for delivery layer (§4.4): text generation + pending_deliveries queue."""
import json

import pytest

from buer.store import Store
from buer import delivery

ROOT = "/test"


def _mem_store() -> Store:
    return Store(":memory:")


def _project(store: Store) -> int:
    return store.get_or_create_project(ROOT)


def _fake_inc(signal: str, target: str, details: dict, state: str = "notified_agent"):
    """Minimal incident-like dict matching what agent_message/user_message expect."""
    class FakeRow(dict):
        def __getitem__(self, k):
            return super().__getitem__(k)
    return FakeRow({
        "id": 1,
        "signal": signal,
        "target_node": target,
        "state": state,
        "details": json.dumps(details),
    })


# ---------------------------------------------------------------------------
# agent_message text generation
# ---------------------------------------------------------------------------

class TestAgentMessage:
    def test_stuck_region_contains_chain_length(self):
        inc = _fake_inc("stuck_region", "/f.py::fn", {
            "chain_length": 5,
            "define_name": "fn",
        })
        msg = delivery.agent_message(inc)
        assert "[BUER] stuck_region" in msg
        assert "5" in msg

    def test_stuck_region_includes_lateral(self):
        inc = _fake_inc("stuck_region", "/f.py::fn", {
            "chain_length": 5,
            "lateral": {"shared_ancestry": [{"define": "g.bar", "omega": 3.0}]},
        })
        msg = delivery.agent_message(inc)
        assert "g.bar" in msg

    def test_debug_loop_includes_root_cause_note(self):
        inc = _fake_inc("debug_loop", "/f.py::fn", {
            "chain_length": 5,
            "test_tier": "precise",
            "test_cases": ["tests.T::test_fn"],
            "root_cause_note": "检查上游 dependency",
        })
        msg = delivery.agent_message(inc)
        assert "[BUER] debug_loop" in msg
        assert "检查上游 dependency" in msg
        assert "tests.T::test_fn" in msg

    def test_regression_uses_question(self):
        inc = _fake_inc("regression", "tests.T::test_fn", {
            "question": "是预期的吗？",
        })
        msg = delivery.agent_message(inc)
        assert "[BUER] regression" in msg
        assert "是预期的吗？" in msg

    def test_define_loop_uses_loop_question(self):
        inc = _fake_inc("define_loop", "/f.py::fn", {
            "loop_question": "fn 第 3 版和第 1 版结构等价，是有意的吗",
        })
        msg = delivery.agent_message(inc)
        assert "[BUER] define_loop" in msg
        assert "结构等价" in msg

    def test_boundary_breach_includes_note(self):
        inc = _fake_inc("boundary_breach", "/outside/f.py", {
            "note": "你编辑了项目目录以外的文件",
        })
        msg = delivery.agent_message(inc)
        assert "[BUER] boundary_breach" in msg
        assert "项目目录以外" in msg

    def test_test_tampering_agent_path_includes_question(self):
        inc = _fake_inc("test_tampering", "tests.T::test_fn", {
            "question": "改的是测试本身，不是被测代码，确认测试改对了吗？",
            "test_define": "/test/test_f.py::test_fn",
        })
        msg = delivery.agent_message(inc)
        assert "[BUER] test_tampering" in msg
        assert "改的是测试本身" in msg


# ---------------------------------------------------------------------------
# user_message text generation
# ---------------------------------------------------------------------------

class TestUserMessage:
    def test_user_message_has_escalation_header(self):
        inc = _fake_inc("stuck_region", "/f.py::fn", {"chain_length": 5})
        msg = delivery.user_message(inc)
        assert "⚠" in msg
        assert "escalated to user" in msg
        assert "stuck_region" in msg

    def test_test_tampering_user_message_has_question(self):
        inc = _fake_inc("test_tampering", "tests.T::test_fn", {
            "question": "改的是测试本身，不是被测代码，确认测试改对了吗？",
            "test_define": "/test/test_f.py::test_fn",
        }, state="escalated_user")
        msg = delivery.user_message(inc)
        assert "escalated to user" in msg
        assert "改的是测试本身" in msg

    def test_regression_user_message_includes_testcase(self):
        inc = _fake_inc("regression", "tests.T::test_fn", {
            "question": "是预期的吗？",
            "testcase": "tests.T::test_fn",
        })
        msg = delivery.user_message(inc)
        assert "tests.T::test_fn" in msg
        assert "是预期的吗？" in msg

    def test_debug_loop_user_message_includes_root_cause(self):
        inc = _fake_inc("debug_loop", "/f.py::fn", {
            "chain_length": 6,
            "root_cause_note": "检查上游 bar",
        })
        msg = delivery.user_message(inc)
        assert "检查上游 bar" in msg


# ---------------------------------------------------------------------------
# queue helpers + store round-trip
# ---------------------------------------------------------------------------

class TestQueueHelpers:
    def test_queue_agent_injection_stores_message(self):
        store = _mem_store()
        pid = _project(store)
        inc_id = store.write_incident(pid, signal="stuck_region",
                                      target_node="/f.py::fn",
                                      details=json.dumps({"chain_length": 5}))
        inc = store.con.execute("SELECT * FROM incidents WHERE id=?", (inc_id,)).fetchone()

        delivery.queue_agent_injection(store, pid, inc)

        rows = store.peek_deliveries(pid, channel="agent")
        assert len(rows) == 1
        assert "[BUER] stuck_region" in rows[0]["message"]
        assert rows[0]["channel"] == "agent"

    def test_queue_user_notification_stores_user_message(self):
        store = _mem_store()
        pid = _project(store)
        inc_id = store.write_incident(pid, signal="test_tampering",
                                      target_node="tests.T::test_fn",
                                      details=json.dumps({
                                          "question": "改的是测试本身，确认测试改对了吗？",
                                          "test_define": "/t/test_f.py::test_fn",
                                      }))
        inc = store.con.execute("SELECT * FROM incidents WHERE id=?", (inc_id,)).fetchone()

        delivery.queue_user_notification(store, pid, inc)

        rows = store.peek_deliveries(pid, channel="user")
        assert len(rows) == 1
        assert "⚠" in rows[0]["message"]
        assert rows[0]["channel"] == "user"

    def test_take_agent_deliveries_marks_taken(self):
        store = _mem_store()
        pid = _project(store)
        inc_id = store.write_incident(pid, signal="stuck_region",
                                      target_node="/f.py::fn",
                                      details=json.dumps({"chain_length": 5}))
        inc = store.con.execute("SELECT * FROM incidents WHERE id=?", (inc_id,)).fetchone()
        delivery.queue_agent_injection(store, pid, inc)

        taken = store.take_agent_deliveries(pid)
        assert len(taken) == 1
        # Second take returns nothing
        assert store.take_agent_deliveries(pid) == []

    def test_take_user_deliveries_marks_taken(self):
        store = _mem_store()
        pid = _project(store)
        inc_id = store.write_incident(pid, signal="stuck_region",
                                      target_node="/f.py::fn",
                                      details=json.dumps({"chain_length": 5}))
        inc = store.con.execute("SELECT * FROM incidents WHERE id=?", (inc_id,)).fetchone()
        delivery.queue_user_notification(store, pid, inc)

        taken = store.take_user_deliveries(pid)
        assert len(taken) == 1
        assert store.take_user_deliveries(pid) == []

    def test_peek_is_non_destructive(self):
        store = _mem_store()
        pid = _project(store)
        inc_id = store.write_incident(pid, signal="stuck_region",
                                      target_node="/f.py::fn",
                                      details=json.dumps({"chain_length": 5}))
        inc = store.con.execute("SELECT * FROM incidents WHERE id=?", (inc_id,)).fetchone()
        delivery.queue_agent_injection(store, pid, inc)

        rows1 = store.peek_deliveries(pid)
        rows2 = store.peek_deliveries(pid)
        assert len(rows1) == len(rows2) == 1

    def test_channels_are_independent(self):
        store = _mem_store()
        pid = _project(store)
        inc_id = store.write_incident(pid, signal="stuck_region",
                                      target_node="/f.py::fn",
                                      details=json.dumps({"chain_length": 5}))
        inc = store.con.execute("SELECT * FROM incidents WHERE id=?", (inc_id,)).fetchone()
        delivery.queue_agent_injection(store, pid, inc)
        delivery.queue_user_notification(store, pid, inc)

        assert len(store.take_agent_deliveries(pid)) == 1
        assert len(store.take_user_deliveries(pid)) == 1

    def test_find_project_for_file_matches(self):
        store = _mem_store()
        pid = store.get_or_create_project("/projects/myapp")
        assert store.find_project_for_file("/projects/myapp/src/f.py") == pid

    def test_find_project_for_file_no_match(self):
        store = _mem_store()
        store.get_or_create_project("/projects/myapp")
        assert store.find_project_for_file("/other/f.py") is None

    def test_find_project_for_file_longest_match(self):
        store = _mem_store()
        pid_parent = store.get_or_create_project("/projects")
        pid_child = store.get_or_create_project("/projects/myapp")
        assert store.find_project_for_file("/projects/myapp/f.py") == pid_child
