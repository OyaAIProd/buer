"""Tests for proactive high-impact caller warning (8 tests).

Coverage:
  1. test_no_warning_below_threshold   — < HIGH_IMPACT_CALLERS → no warning
  2. test_warning_at_threshold         — exactly HIGH_IMPACT_CALLERS → fires
  3. test_warning_above_threshold      — > HIGH_IMPACT_CALLERS → count reflected in message
  4. test_session_dedup                — same define, same session → warn once only
  5. test_different_session_rewarn     — same define, different session → fires again
  6. test_message_format               — [BUER] prefix, "at least", define name present
  7. test_caller_names_top3            — at most 3 caller names listed, not more
  8. test_no_gd_edges_returns_empty    — graph not built → silent []

Mutation guard: test_warning_at_threshold must go red if check_high_impact_defines
returns [] when n == HIGH_IMPACT_CALLERS.
"""
from __future__ import annotations

import pytest

from buer.signals import HIGH_IMPACT_CALLERS, check_high_impact_defines
from buer.store import Store


# ── helpers ────────────────────────────────────────────────────────────────────

def _store() -> Store:
    return Store(":memory:")


def _project(store: Store) -> int:
    return store.get_or_create_project("/testproj")


def _add_define(store: Store, pid: int, file_path: str, define_name: str) -> int:
    det_id, _ = store.insert_determination_atomic(
        pid, file_path, define_name, f"fp_{define_name}", "modify"
    )
    return det_id


def _add_callers(store: Store, pid: int, det_id: int, n: int) -> list[int]:
    """Insert n caller determinations and gd_edges from det_id to each caller."""
    caller_ids = []
    for i in range(n):
        caller_det, _ = store.insert_determination_atomic(
            pid, f"src/caller_{i}.py", f"caller_{i}", f"fp_c{i}", "modify"
        )
        store.insert_gd_edge(pid, det_id, caller_det, "cross_define")
        caller_ids.append(caller_det)
    return caller_ids


# ── 1. below threshold → silent ───────────────────────────────────────────────

def test_no_warning_below_threshold():
    store = _store()
    pid = _project(store)
    det = _add_define(store, pid, "src/foo.py", "my_func")
    _add_callers(store, pid, det, HIGH_IMPACT_CALLERS - 1)

    warned: set = set()
    result = check_high_impact_defines(store, pid, "src/foo.py", "sess1", warned)
    assert result == [], f"Expected no warning for {HIGH_IMPACT_CALLERS - 1} callers"


# ── 2. at threshold → fires ───────────────────────────────────────────────────

def test_warning_at_threshold():
    store = _store()
    pid = _project(store)
    det = _add_define(store, pid, "src/foo.py", "my_func")
    _add_callers(store, pid, det, HIGH_IMPACT_CALLERS)

    warned: set = set()
    result = check_high_impact_defines(store, pid, "src/foo.py", "sess1", warned)
    assert len(result) == 1, f"Expected 1 warning at threshold={HIGH_IMPACT_CALLERS}"


# ── 3. above threshold → count shown correctly ────────────────────────────────

def test_warning_above_threshold():
    store = _store()
    pid = _project(store)
    n = HIGH_IMPACT_CALLERS + 3
    det = _add_define(store, pid, "src/foo.py", "my_func")
    _add_callers(store, pid, det, n)

    warned: set = set()
    result = check_high_impact_defines(store, pid, "src/foo.py", "sess1", warned)
    assert len(result) == 1
    assert str(n) in result[0], f"Caller count {n} must appear in warning message"


# ── 4. session dedup — same session warns once ────────────────────────────────

def test_session_dedup():
    store = _store()
    pid = _project(store)
    det = _add_define(store, pid, "src/foo.py", "my_func")
    _add_callers(store, pid, det, HIGH_IMPACT_CALLERS)

    warned: set = set()
    first = check_high_impact_defines(store, pid, "src/foo.py", "sess1", warned)
    assert len(first) == 1, "First call must warn"

    second = check_high_impact_defines(store, pid, "src/foo.py", "sess1", warned)
    assert second == [], "Same session must not warn again"


# ── 5. different session → warns again ───────────────────────────────────────

def test_different_session_rewarn():
    store = _store()
    pid = _project(store)
    det = _add_define(store, pid, "src/foo.py", "my_func")
    _add_callers(store, pid, det, HIGH_IMPACT_CALLERS)

    warned: set = set()
    first = check_high_impact_defines(store, pid, "src/foo.py", "sess1", warned)
    assert len(first) == 1

    second = check_high_impact_defines(store, pid, "src/foo.py", "sess2", warned)
    assert len(second) == 1, "New session must warn again for same define"


# ── 6. message format ─────────────────────────────────────────────────────────

def test_message_format():
    store = _store()
    pid = _project(store)
    det = _add_define(store, pid, "src/foo.py", "my_func")
    _add_callers(store, pid, det, HIGH_IMPACT_CALLERS)

    warned: set = set()
    result = check_high_impact_defines(store, pid, "src/foo.py", "sess1", warned)
    assert len(result) == 1
    msg = result[0]
    assert "[BUER]" in msg, "Message must start with [BUER] prefix"
    assert "at least" in msg, "Lower-bound language 'at least' must be present"
    assert "my_func" in msg, "Define name must appear in message"


# ── 7. at most 3 caller names listed ─────────────────────────────────────────

def test_caller_names_top3():
    store = _store()
    pid = _project(store)
    n = HIGH_IMPACT_CALLERS + 4  # many callers
    det = _add_define(store, pid, "src/foo.py", "my_func")
    _add_callers(store, pid, det, n)

    warned: set = set()
    result = check_high_impact_defines(store, pid, "src/foo.py", "sess1", warned)
    assert len(result) == 1
    msg = result[0]
    # Count how many "caller_N" names appear (gd_caller_names is LIMIT 3)
    listed = [f"caller_{i}" for i in range(n) if f"caller_{i}" in msg]
    assert len(listed) <= 3, f"At most 3 caller names should appear; got {len(listed)}: {listed}"


# ── 8. no gd_edges → silent ───────────────────────────────────────────────────

def test_no_gd_edges_returns_empty():
    store = _store()
    pid = _project(store)
    # Define exists but no gd_edges inserted at all
    _add_define(store, pid, "src/foo.py", "my_func")

    warned: set = set()
    result = check_high_impact_defines(store, pid, "src/foo.py", "sess1", warned)
    assert result == [], "No gd_edges → must return [] immediately"
