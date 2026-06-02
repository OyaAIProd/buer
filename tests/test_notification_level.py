"""Tests for user-facing notification level (8 tests).

Coverage:
  1. test_medium_equals_current          — medium == existing THETA_2 (backward-compat)
  2. test_high_more_alerts               — high lowers escalation threshold by 1
  3. test_low_fewer_alerts               — low raises escalation threshold by 2
  4. test_silent_efficiency_no_escalation— silent returns None for efficiency signals
  5. test_silent_integrity_still_notifies— silent preserves base_theta2 for integrity signals
  6. test_silent_agent_channel_works     — silent: agent injection still fires
  7. test_silent_pull_tools_work         — silent: check_drift / open_incidents still return data
  8. test_set_level_validates            — invalid level raises ValueError

Mutation guard: test_silent_integrity_still_notifies must go red if
effective_escalation_theta2("silent", integrity_signal, k) returns None.
"""
from __future__ import annotations

import json

import pytest

from buer.sensitivity import (
    EFFICIENCY_SIGNALS,
    INTEGRITY_SIGNALS,
    effective_escalation_theta2,
)
from buer.signals import THETA_2, THETA_2_DEFAULT, advance_incidents
from buer.store import Store


# ── helpers ────────────────────────────────────────────────────────────────────

def _store() -> Store:
    return Store(":memory:")


def _project(store: Store) -> int:
    return store.get_or_create_project("/testproj")


def _open_incident(store: Store, pid: int, signal: str) -> int:
    store.write_incident(pid, signal=signal, target_node="fn_a", details=json.dumps({}))
    inc = store.open_incidents(pid)[-1]
    return inc["id"]


# ── 1. medium == current THETA_2 (backward compat) ───────────────────────────

def test_medium_equals_current():
    for signal, base in THETA_2.items():
        assert effective_escalation_theta2("medium", signal, base) == base, (
            f"medium must equal base THETA_2 for signal {signal!r}"
        )
    # default too
    assert effective_escalation_theta2("medium", "unknown_signal", THETA_2_DEFAULT) == THETA_2_DEFAULT


# ── 2. high tightens threshold by 1 (min 1) ──────────────────────────────────

def test_high_more_alerts():
    base = THETA_2.get("stuck_region", THETA_2_DEFAULT)
    eff = effective_escalation_theta2("high", "stuck_region", base)
    assert isinstance(eff, int)
    assert eff == max(1, base - 1)
    assert eff < base  # strictly fewer recurrences needed → more alerts


# ── 3. low raises threshold by 2 ─────────────────────────────────────────────

def test_low_fewer_alerts():
    base = THETA_2.get("stuck_region", THETA_2_DEFAULT)
    eff = effective_escalation_theta2("low", "stuck_region", base)
    assert isinstance(eff, int)
    assert eff == base + 2  # more recurrences needed → fewer alerts


# ── 4. silent → None for efficiency signals ──────────────────────────────────

def test_silent_efficiency_no_escalation():
    for signal in EFFICIENCY_SIGNALS:
        base = THETA_2.get(signal, THETA_2_DEFAULT)
        eff = effective_escalation_theta2("silent", signal, base)
        assert eff is None, (
            f"silent must return None for efficiency signal {signal!r}, got {eff!r}"
        )


# ── 5. silent → base_theta2 for integrity signals (safety rail) ──────────────

def test_silent_integrity_still_notifies():
    for signal in INTEGRITY_SIGNALS:
        base = THETA_2.get(signal, THETA_2_DEFAULT)
        eff = effective_escalation_theta2("silent", signal, base)
        # Must not be None — integrity signals must surface even in silent mode
        assert eff is not None, (
            f"silent must NOT silence integrity signal {signal!r} (safety rail)"
        )
        assert eff == base, (
            f"silent must preserve base_theta2 for integrity signal {signal!r}"
        )


# ── 6. silent: agent channel still fires ─────────────────────────────────────

def test_silent_agent_channel_works():
    """In silent mode, advance_incidents still sends to agent channel for efficiency signals."""
    store = _store()
    pid = _project(store)
    store.set_notification_level(pid, "silent")

    # Open a stuck_region incident (efficiency signal)
    store.write_incident(
        pid, signal="stuck_region", target_node="fn_a",
        details=json.dumps({"consecutive_stable": 0}),
    )
    advance_incidents(store, pid, affected=[])

    # Incident must have moved to notified_agent (agent was notified)
    incs = store.con.execute(
        "SELECT state FROM incidents WHERE project_id=?", (pid,)
    ).fetchall()
    states = [r["state"] for r in incs]
    assert "notified_agent" in states, "Agent channel must work in silent mode"
    # Must NOT have been escalated to user
    assert "escalated_user" not in states, "User must not be escalated in silent mode"

    # Agent deliveries present
    agent_deliveries = store.con.execute(
        "SELECT * FROM pending_deliveries WHERE project_id=? AND channel='agent'", (pid,)
    ).fetchall()
    assert len(agent_deliveries) > 0, "Agent injection must fire in silent mode"


# ── 7. silent: pull tools (open_incidents) still return data ─────────────────

def test_silent_pull_tools_work():
    """In silent mode, check_drift / open_incidents return full incident data."""
    store = _store()
    pid = _project(store)
    store.set_notification_level(pid, "silent")

    store.write_incident(
        pid, signal="stuck_region", target_node="fn_a",
        details=json.dumps({"consecutive_stable": 0}),
    )

    # open_incidents backs check_drift — must still return data
    incs = store.open_incidents(pid)
    assert len(incs) == 1, "open_incidents must return data regardless of notification level"
    assert incs[0]["signal"] == "stuck_region"


# ── 8. set_notification_level rejects invalid input ──────────────────────────

def test_set_level_validates():
    store = _store()
    pid = _project(store)

    with pytest.raises(ValueError, match="Invalid notification level"):
        store.set_notification_level(pid, "aggressive")  # not a valid level

    with pytest.raises(ValueError):
        store.set_notification_level(pid, "")

    # Valid levels must succeed
    for level in ("high", "medium", "low", "silent"):
        store.set_notification_level(pid, level)
        assert store.get_notification_level(pid) == level

    # Unknown project defaults to medium
    assert store.get_notification_level(9999) == "medium"
