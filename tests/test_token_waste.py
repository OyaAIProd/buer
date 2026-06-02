"""Tests for token_waste detection (v2.1 §1).

Coverage:
  A. 16 edits + open stuck + incident_count>=2 → triggers token_waste
  B. 16 edits but converged (no open stuck/debug) → no trigger
  C. 16 edits but incident_count=1 → no trigger (not a pattern)
  D. Already triggered token_waste → subsequent call is deduped (no re-trigger)
  E. Alert queued on BOTH agent channel and user channel
  F. rounds < THETA_WASTE → no trigger
  G. Message text: contains "建议", no "$", no "必须"; includes rounds count
  H. detect_token_waste is a pure upgrade layer (doesn't write stuck incident itself)
  I. store.count_incidents_for_target counts all states (including resolved)
  J. delivery.agent_message / user_message format token_waste correctly
"""
from __future__ import annotations

import json

import pytest

from buer import delivery, signals
from buer.store import Store


# ── helpers ────────────────────────────────────────────────────────────────────

def _setup(tmp_path, n_edits: int) -> tuple[Store, int, str, str]:
    """Create in-memory store + n_edits version chain for one define."""
    store = Store(":memory:")
    pid = store.get_or_create_project(str(tmp_path))
    fp = str(tmp_path / "src.py")
    dn = "process_request"
    for i in range(n_edits):
        store.insert_determination(pid, i + 1, fp, dn, f"fp_{i}", "modify")
    return store, pid, fp, dn


def _add_stuck_incidents(
    store: Store,
    pid: int,
    target: str,
    total: int = 2,
    leave_open: int = 1,
) -> list[int]:
    """Write `total` stuck_region incidents; resolve all but the last `leave_open`."""
    ids = []
    for _ in range(total):
        inc_id = store.write_incident(
            pid, signal="stuck_region", target_node=target,
            details=json.dumps({"chain_length": 16}),
        )
        ids.append(inc_id)
    # Resolve all except the last `leave_open`
    for inc_id in ids[:-leave_open] if leave_open else ids:
        store.update_incident(inc_id, state="resolved", resolved_by="stable_region")
    return ids


def _token_waste_incs(store: Store, pid: int) -> list:
    return [i for i in store.open_incidents(pid) if i["signal"] == "token_waste"]


# ── A: happy path ──────────────────────────────────────────────────────────────

class TestTriggersWhenAllConditionsMet:
    def test_writes_token_waste_incident(self, tmp_path):
        store, pid, fp, dn = _setup(tmp_path, 16)
        target = f"{fp}::{dn}"
        _add_stuck_incidents(store, pid, target, total=2, leave_open=1)
        signals.detect_token_waste(store, pid, [(fp, dn, 16)])
        assert len(_token_waste_incs(store, pid)) == 1

    def test_incident_details_contain_rounds(self, tmp_path):
        store, pid, fp, dn = _setup(tmp_path, 16)
        target = f"{fp}::{dn}"
        _add_stuck_incidents(store, pid, target)
        signals.detect_token_waste(store, pid, [(fp, dn, 16)])
        inc = _token_waste_incs(store, pid)[0]
        d = json.loads(inc["details"])
        assert d["rounds"] == 16
        assert d["incident_count"] == 2

    def test_debug_loop_also_satisfies_condition_b(self, tmp_path):
        """A debug_loop incident (instead of stuck_region) satisfies condition b."""
        store, pid, fp, dn = _setup(tmp_path, 16)
        target = f"{fp}::{dn}"
        # One open debug_loop + one resolved stuck → total=2, one open debug
        inc_id = store.write_incident(
            pid, signal="debug_loop", target_node=target,
            details=json.dumps({"chain_length": 16, "test_cases": []}),
        )
        inc_id2 = store.write_incident(
            pid, signal="stuck_region", target_node=target,
            details=json.dumps({"chain_length": 8}),
        )
        store.update_incident(inc_id2, state="resolved", resolved_by="stable_region")
        signals.detect_token_waste(store, pid, [(fp, dn, 16)])
        assert len(_token_waste_incs(store, pid)) == 1

    def test_exactly_theta_waste_rounds_triggers(self, tmp_path):
        """Exactly THETA_WASTE rounds is enough (>=, not >)."""
        store, pid, fp, dn = _setup(tmp_path, signals.THETA_WASTE)
        target = f"{fp}::{dn}"
        _add_stuck_incidents(store, pid, target)
        signals.detect_token_waste(store, pid, [(fp, dn, signals.THETA_WASTE)])
        assert len(_token_waste_incs(store, pid)) == 1


# ── B: converged — no trigger ─────────────────────────────────────────────────

class TestNoTriggerWhenConverged:
    def test_all_stuck_resolved_no_trigger(self, tmp_path):
        store, pid, fp, dn = _setup(tmp_path, 16)
        target = f"{fp}::{dn}"
        # 2 incidents, both resolved
        _add_stuck_incidents(store, pid, target, total=2, leave_open=0)
        signals.detect_token_waste(store, pid, [(fp, dn, 16)])
        assert len(_token_waste_incs(store, pid)) == 0

    def test_no_stuck_incidents_at_all_no_trigger(self, tmp_path):
        store, pid, fp, dn = _setup(tmp_path, 16)
        signals.detect_token_waste(store, pid, [(fp, dn, 16)])
        assert len(_token_waste_incs(store, pid)) == 0


# ── C: incident_count=1 — not a pattern ───────────────────────────────────────

class TestNoTriggerWhenOnlyOneIncident:
    def test_single_stuck_incident_no_trigger(self, tmp_path):
        store, pid, fp, dn = _setup(tmp_path, 16)
        target = f"{fp}::{dn}"
        _add_stuck_incidents(store, pid, target, total=1, leave_open=1)
        signals.detect_token_waste(store, pid, [(fp, dn, 16)])
        assert len(_token_waste_incs(store, pid)) == 0


# ── D: dedup — no re-trigger ──────────────────────────────────────────────────

class TestDedup:
    def test_second_call_does_not_create_another_incident(self, tmp_path):
        store, pid, fp, dn = _setup(tmp_path, 16)
        target = f"{fp}::{dn}"
        _add_stuck_incidents(store, pid, target)
        affected = [(fp, dn, 16)]
        signals.detect_token_waste(store, pid, affected)
        signals.detect_token_waste(store, pid, affected)  # second call
        assert len(_token_waste_incs(store, pid)) == 1

    def test_no_new_deliveries_on_second_call(self, tmp_path):
        store, pid, fp, dn = _setup(tmp_path, 16)
        target = f"{fp}::{dn}"
        _add_stuck_incidents(store, pid, target)
        affected = [(fp, dn, 16)]
        signals.detect_token_waste(store, pid, affected)
        # Take first-call deliveries
        store.take_agent_deliveries(pid)
        store.take_user_deliveries(pid)
        # Second call should produce no new deliveries
        signals.detect_token_waste(store, pid, affected)
        assert store.peek_deliveries(pid) == []


# ── E: both channels enqueued ─────────────────────────────────────────────────

class TestBothChannelsEnqueued:
    def test_agent_delivery_enqueued(self, tmp_path):
        store, pid, fp, dn = _setup(tmp_path, 16)
        _add_stuck_incidents(store, pid, f"{fp}::{dn}")
        signals.detect_token_waste(store, pid, [(fp, dn, 16)])
        agent_dels = store.peek_deliveries(pid, "agent")
        tw = [d for d in agent_dels if "token_waste" in (d["message"] or "")]
        assert len(tw) >= 1

    def test_user_delivery_enqueued(self, tmp_path):
        store, pid, fp, dn = _setup(tmp_path, 16)
        _add_stuck_incidents(store, pid, f"{fp}::{dn}")
        signals.detect_token_waste(store, pid, [(fp, dn, 16)])
        user_dels = store.peek_deliveries(pid, "user")
        tw = [d for d in user_dels if "waste" in (d["message"] or "")]
        assert len(tw) >= 1

    def test_both_channels_get_delivery(self, tmp_path):
        store, pid, fp, dn = _setup(tmp_path, 16)
        _add_stuck_incidents(store, pid, f"{fp}::{dn}")
        signals.detect_token_waste(store, pid, [(fp, dn, 16)])
        assert len(store.peek_deliveries(pid, "agent")) >= 1
        assert len(store.peek_deliveries(pid, "user")) >= 1


# ── F: rounds < THETA_WASTE ───────────────────────────────────────────────────

class TestRoundsBelowThreshold:
    def test_14_edits_no_trigger(self, tmp_path):
        store, pid, fp, dn = _setup(tmp_path, 14)
        target = f"{fp}::{dn}"
        _add_stuck_incidents(store, pid, target)
        signals.detect_token_waste(store, pid, [(fp, dn, 14)])
        assert len(_token_waste_incs(store, pid)) == 0

    def test_theta_waste_minus_one_no_trigger(self, tmp_path):
        store, pid, fp, dn = _setup(tmp_path, signals.THETA_WASTE - 1)
        target = f"{fp}::{dn}"
        _add_stuck_incidents(store, pid, target)
        signals.detect_token_waste(store, pid, [(fp, dn, signals.THETA_WASTE - 1)])
        assert len(_token_waste_incs(store, pid)) == 0

    def test_zero_edits_no_trigger(self, tmp_path):
        store, pid, fp, dn = _setup(tmp_path, 0)
        signals.detect_token_waste(store, pid, [(fp, dn, 0)])
        assert len(_token_waste_incs(store, pid)) == 0


# ── G: message text quality ───────────────────────────────────────────────────

class TestMessageTextQuality:
    def _get_all_delivery_text(self, store, pid):
        return " ".join(d["message"] for d in store.peek_deliveries(pid))

    def test_contains_jian_yi(self, tmp_path):
        """Message must contain 建议 (not directive language)."""
        store, pid, fp, dn = _setup(tmp_path, 16)
        _add_stuck_incidents(store, pid, f"{fp}::{dn}")
        signals.detect_token_waste(store, pid, [(fp, dn, 16)])
        text = self._get_all_delivery_text(store, pid)
        assert "consider" in text

    def test_no_dollar_sign(self, tmp_path):
        """Message must not contain dollar signs (not a billing tool)."""
        store, pid, fp, dn = _setup(tmp_path, 16)
        _add_stuck_incidents(store, pid, f"{fp}::{dn}")
        signals.detect_token_waste(store, pid, [(fp, dn, 16)])
        text = self._get_all_delivery_text(store, pid)
        assert "$" not in text

    def test_no_bi_xu(self, tmp_path):
        """Message must not contain 必须 (non-directive)."""
        store, pid, fp, dn = _setup(tmp_path, 16)
        _add_stuck_incidents(store, pid, f"{fp}::{dn}")
        signals.detect_token_waste(store, pid, [(fp, dn, 16)])
        text = self._get_all_delivery_text(store, pid)
        assert "必须" not in text

    def test_contains_rounds_count(self, tmp_path):
        """Message must include the rounds count for transparency."""
        store, pid, fp, dn = _setup(tmp_path, 16)
        _add_stuck_incidents(store, pid, f"{fp}::{dn}")
        signals.detect_token_waste(store, pid, [(fp, dn, 16)])
        text = self._get_all_delivery_text(store, pid)
        assert "16" in text

    def test_contains_define_name(self, tmp_path):
        """Message must reference the affected define."""
        store, pid, fp, dn = _setup(tmp_path, 16)
        _add_stuck_incidents(store, pid, f"{fp}::{dn}")
        signals.detect_token_waste(store, pid, [(fp, dn, 16)])
        text = self._get_all_delivery_text(store, pid)
        assert dn in text

    def test_contains_incident_count(self, tmp_path):
        """Message must include the signal incident count."""
        store, pid, fp, dn = _setup(tmp_path, 16)
        _add_stuck_incidents(store, pid, f"{fp}::{dn}", total=3, leave_open=1)
        signals.detect_token_waste(store, pid, [(fp, dn, 16)])
        text = self._get_all_delivery_text(store, pid)
        assert "3" in text


# ── H: detect_token_waste is pure upgrade layer ───────────────────────────────

class TestUpgradeLayerPurity:
    def test_does_not_write_stuck_incident(self, tmp_path):
        """detect_token_waste must not create stuck_region incidents."""
        store, pid, fp, dn = _setup(tmp_path, 16)
        target = f"{fp}::{dn}"
        _add_stuck_incidents(store, pid, target)
        count_before = store.count_incidents_for_target(
            pid, target, ["stuck_region", "debug_loop"]
        )
        signals.detect_token_waste(store, pid, [(fp, dn, 16)])
        count_after = store.count_incidents_for_target(
            pid, target, ["stuck_region", "debug_loop"]
        )
        assert count_after == count_before  # unchanged

    def test_empty_affected_does_nothing(self, tmp_path):
        store, pid, fp, dn = _setup(tmp_path, 16)
        signals.detect_token_waste(store, pid, [])
        assert len(_token_waste_incs(store, pid)) == 0


# ── I: count_incidents_for_target counts all states ──────────────────────────

class TestCountIncidentsForTarget:
    def test_counts_resolved_incidents(self, tmp_path):
        store = Store(":memory:")
        pid = store.get_or_create_project(str(tmp_path))
        target = "foo.py::fn"
        for _ in range(3):
            inc_id = store.write_incident(pid, signal="stuck_region", target_node=target,
                                           details="{}")
            store.update_incident(inc_id, state="resolved", resolved_by="stable_region")
        assert store.count_incidents_for_target(pid, target, ["stuck_region"]) == 3

    def test_counts_open_and_resolved(self, tmp_path):
        store = Store(":memory:")
        pid = store.get_or_create_project(str(tmp_path))
        target = "foo.py::fn"
        store.write_incident(pid, signal="stuck_region", target_node=target, details="{}")
        inc_id = store.write_incident(pid, signal="debug_loop", target_node=target, details="{}")
        store.update_incident(inc_id, state="resolved", resolved_by="test_passed")
        assert store.count_incidents_for_target(pid, target, ["stuck_region", "debug_loop"]) == 2

    def test_filters_by_signal(self, tmp_path):
        store = Store(":memory:")
        pid = store.get_or_create_project(str(tmp_path))
        target = "foo.py::fn"
        store.write_incident(pid, signal="stuck_region", target_node=target, details="{}")
        store.write_incident(pid, signal="define_loop", target_node=target, details="{}")
        assert store.count_incidents_for_target(pid, target, ["stuck_region"]) == 1

    def test_empty_signals_list_returns_zero(self, tmp_path):
        store = Store(":memory:")
        pid = store.get_or_create_project(str(tmp_path))
        target = "foo.py::fn"
        store.write_incident(pid, signal="stuck_region", target_node=target, details="{}")
        assert store.count_incidents_for_target(pid, target, []) == 0


# ── J: delivery message formatting ───────────────────────────────────────────

class TestDeliveryFormatting:
    def _make_inc(self, rounds=16, time_span=2.5, ic=3):
        return {
            "id": 99,
            "signal": "token_waste",
            "target_node": "/proj/src.py::process_request",
            "details": json.dumps({
                "rounds": rounds,
                "time_span_hours": time_span,
                "incident_count": ic,
                "proxy_note": "investment metric is an engineering proxy (rounds/time/incident count)",
            }),
        }

    def test_agent_message_format(self):
        inc = self._make_inc()
        msg = delivery.agent_message(inc)
        assert "[BUER] token_waste" in msg
        assert "process_request" in msg
        assert "16" in msg
        assert "consider" in msg
        assert "$" not in msg

    def test_user_message_format(self):
        inc = self._make_inc()
        msg = delivery.user_message(inc)
        assert "effort waste warning" in msg
        assert "process_request" in msg
        assert "16" in msg
        assert "consider" in msg
        assert "$" not in msg
        assert "proxy_note" not in msg  # raw key should not appear

    def test_agent_message_includes_time_span_when_nonzero(self):
        inc = self._make_inc(time_span=3.5)
        msg = delivery.agent_message(inc)
        assert "3.5" in msg

    def test_agent_message_omits_time_span_when_zero(self):
        inc = self._make_inc(time_span=0)
        msg = delivery.agent_message(inc)
        assert "hours" not in msg

    def test_user_message_includes_proxy_note(self):
        inc = self._make_inc()
        msg = delivery.user_message(inc)
        assert "engineering proxy" in msg

    def test_agent_message_no_bi_xu(self):
        inc = self._make_inc()
        msg = delivery.agent_message(inc)
        assert "必须" not in msg

    def test_user_message_no_dollar(self):
        inc = self._make_inc()
        msg = delivery.user_message(inc)
        assert "$" not in msg
