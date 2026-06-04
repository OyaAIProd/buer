"""Tests for buer.signals — stuck_region detection + two-step state machine.

Topologies
----------
_stuck_chain(n): pure version chain — adjacent d_J = 1/k (>= THETA_DJ=0.2 for k≤5).
  Verifies: 5 mods with large adjacent d_J → triggers stuck_region.

_osc_chain(n, n_bg=4): chain + 4 shared background ancestors — d_J = 1/(k+4) < 0.2.
  Verifies: 5 mods with small adjacent d_J → does NOT trigger (small oscillation).

State-machine tests drive reconcile rounds by calling detect_stuck_region +
advance_incidents directly, without full reconcile (no actual source files needed).
"""
import json
import pytest

from buer.store import Store
from buer.callgraph import SymbolIndex
from buer import signals

EMPTY_IDX = SymbolIndex()    # no symbols — lateral context returns {}
ROOT = "/test"
FILE = "/test/f.py"
DEFINE = "fn"


# ── topology builders ─────────────────────────────────────────────────────────

def _mem_store() -> Store:
    return Store(":memory:")


def _stuck_chain(n: int):
    """Pure version chain: n modifications to same define, adjacent d_J = 1/k >= 0.2."""
    store = _mem_store()
    pid = store.get_or_create_project(ROOT)
    dets = []
    for i in range(1, n + 1):
        det_id = store.insert_determination(
            pid, seq=i, file_path=FILE, define_name=DEFINE,
            node_fingerprint=f"fp{i}", edit_type="create" if i == 1 else "modify",
        )
        dets.append(det_id)
    for i in range(len(dets) - 1):
        store.insert_gd_edge(pid, from_det=dets[i], to_det=dets[i + 1],
                             edge_class="version_chain")
    return store, pid, dets


def _osc_chain(n: int, n_bg: int = 4):
    """Version chain with n_bg shared background ancestors → d_J = 1/(k+n_bg) < 0.2."""
    store = _mem_store()
    pid = store.get_or_create_project(ROOT)
    # Background chain — shared by all fn versions via version chain
    bg_ids = []
    for i in range(n_bg):
        bg_id = store.insert_determination(
            pid, seq=100 + i, file_path="/test/bg.py",
            define_name=f"bg{i}", node_fingerprint=f"bgfp{i}", edit_type="create",
        )
        bg_ids.append(bg_id)
    for i in range(len(bg_ids) - 1):
        store.insert_gd_edge(pid, from_det=bg_ids[i], to_det=bg_ids[i + 1],
                             edge_class="version_chain")
    # fn chain
    dets = []
    for i in range(1, n + 1):
        det_id = store.insert_determination(
            pid, seq=i, file_path=FILE, define_name=DEFINE,
            node_fingerprint=f"fp{i}", edit_type="create" if i == 1 else "modify",
        )
        dets.append(det_id)
    for i in range(len(dets) - 1):
        store.insert_gd_edge(pid, from_det=dets[i], to_det=dets[i + 1],
                             edge_class="version_chain")
    # Shared ancestry: last bg node feeds into fn_v1
    store.insert_gd_edge(pid, from_det=bg_ids[-1], to_det=dets[0],
                         edge_class="cross_define_callgraph")
    return store, pid, dets


def _reconcile_round(store, pid, dets, idx=None):
    """Simulate one reconcile round for the latest det in dets."""
    if idx is None:
        idx = EMPTY_IDX
    affected = [(FILE, DEFINE, dets[-1])]
    signals.detect_stuck_region(store, pid, affected, ROOT, idx)
    signals.detect_define_loop(store, pid, affected, ROOT, idx)
    signals.advance_incidents(store, pid, affected)


def _no_change_round(store, pid):
    """Simulate a reconcile round where no defines changed."""
    signals.detect_stuck_region(store, pid, [], ROOT, EMPTY_IDX)
    signals.advance_incidents(store, pid, [])


def _open_incs(store, pid):
    return store.open_incidents(pid)


# ── detection tests ───────────────────────────────────────────────────────────

class TestDetectStuckRegion:
    def test_5_mods_triggers(self):
        """5 modifications with large adjacent d_J → stuck_region incident created."""
        store, pid, dets = _stuck_chain(5)
        _reconcile_round(store, pid, dets)
        incs = _open_incs(store, pid)
        assert len(incs) == 1
        assert incs[0]["signal"] == "stuck_region"
        assert incs[0]["target_node"] == f"{FILE}::{DEFINE}"

    def test_4_mods_no_trigger(self):
        """Only 4 modifications: chain length < θ₁=5, no incident."""
        store, pid, dets = _stuck_chain(4)
        _reconcile_round(store, pid, dets)
        assert _open_incs(store, pid) == []

    def test_small_oscillation_no_trigger(self):
        """5 modifications but adjacent d_J < THETA_DJ=0.2 (shared ancestors lower d_J).

        _osc_chain(5, n_bg=4): d_J(v4,v5) = 1/(5+4) = 1/9 ≈ 0.11 < 0.2.
        """
        store, pid, dets = _osc_chain(5, n_bg=4)
        _reconcile_round(store, pid, dets)
        assert _open_incs(store, pid) == []

    def test_equiv_loop_no_trigger(self):
        """5 modifications but last fingerprint repeats first → define_loop fires, not stuck_region."""
        store = _mem_store()
        pid = store.get_or_create_project(ROOT)
        fps = ["fp0", "fp1", "fp2", "fp3", "fp0"]  # fp0 repeats at non-adjacent position
        dets = []
        for i, fp in enumerate(fps, 1):
            det_id = store.insert_determination(
                pid, seq=i, file_path=FILE, define_name=DEFINE,
                node_fingerprint=fp, edit_type="create" if i == 1 else "modify",
            )
            dets.append(det_id)
        for i in range(len(dets) - 1):
            store.insert_gd_edge(pid, from_det=dets[i], to_det=dets[i + 1],
                                 edge_class="version_chain")
        _reconcile_round(store, pid, dets)
        # stuck_region requires all-distinct fingerprints — equiv loop disqualifies it
        assert all(inc["signal"] != "stuck_region" for inc in _open_incs(store, pid))
        # define_loop correctly fires for the fp0 recurrence
        assert any(inc["signal"] == "define_loop" for inc in _open_incs(store, pid))

    def test_idempotent_no_duplicate_incident(self):
        """Second reconcile with same define does not create a second incident."""
        store, pid, dets = _stuck_chain(5)
        _reconcile_round(store, pid, dets)
        _reconcile_round(store, pid, dets)
        assert len(_open_incs(store, pid)) == 1

    def test_details_contain_chain_length(self):
        store, pid, dets = _stuck_chain(5)
        _reconcile_round(store, pid, dets)
        inc = _open_incs(store, pid)[0]
        details = json.loads(inc["details"])
        assert details["chain_length"] == 5
        assert details["define_name"] == DEFINE


# ── state machine: open → notified_agent ─────────────────────────────────────

class TestAdvanceFromOpen:
    def test_open_transitions_to_notified(self):
        """First reconcile round: open incident → notified_agent."""
        store, pid, dets = _stuck_chain(5)
        _reconcile_round(store, pid, dets)
        inc = _open_incs(store, pid)[0]
        assert inc["state"] == "notified_agent"
        assert inc["agent_notified_at"] is not None

    def test_escalate_directly_skips_agent(self):
        """escalate_user_directly=True (test_tampering path): open → escalated_user."""
        store = _mem_store()
        pid = store.get_or_create_project(ROOT)
        store.write_incident(
            pid, signal="test_tampering", target_node="f.py::fn",
            details=json.dumps({"escalate_user_directly": True}),
        )
        signals.advance_incidents(store, pid, [])
        inc = store.open_incidents(pid)[0]
        assert inc["state"] == "escalated_user"
        assert inc["escalated_at"] is not None


# ── state machine: notified → escalated via θ₂ ───────────────────────────────

class TestEscalationPath:
    def _build_notified(self):
        """Return (store, pid, dets) with incident already in notified_agent state."""
        store, pid, dets = _stuck_chain(5)
        _reconcile_round(store, pid, dets)   # creates and advances to notified_agent
        return store, pid, dets

    def test_recurrence_increments_count(self):
        """6th modification: post_notify_count goes to 1, stays notified_agent."""
        store, pid, dets = self._build_notified()
        # Simulate 6th modification: add det6 to chain
        det6 = store.insert_determination(
            pid, seq=6, file_path=FILE, define_name=DEFINE,
            node_fingerprint="fp6", edit_type="modify",
        )
        store.insert_gd_edge(pid, from_det=dets[-1], to_det=det6, edge_class="version_chain")
        _reconcile_round(store, pid, dets + [det6])
        inc = _open_incs(store, pid)[0]
        assert inc["state"] == "notified_agent"
        assert inc["post_notify_count"] == 1

    def test_theta2_recurrences_escalate(self):
        """3 recurrences (θ₂=3) → escalated_user."""
        store, pid, dets = self._build_notified()
        cur_dets = list(dets)
        for i in range(6, 6 + signals.THETA_2["stuck_region"]):
            new_det = store.insert_determination(
                pid, seq=i, file_path=FILE, define_name=DEFINE,
                node_fingerprint=f"fp{i}", edit_type="modify",
            )
            store.insert_gd_edge(pid, from_det=cur_dets[-1], to_det=new_det,
                                 edge_class="version_chain")
            cur_dets.append(new_det)
            _reconcile_round(store, pid, cur_dets)
        inc = _open_incs(store, pid)[0]
        assert inc["state"] == "escalated_user"
        assert inc["escalated_at"] is not None
        assert inc["post_notify_count"] == signals.THETA_2["stuck_region"]

    def test_stable_resets_post_notify_count_path(self):
        """After notified, if define stops changing, stable counter increments."""
        store, pid, dets = self._build_notified()
        _no_change_round(store, pid)
        inc = _open_incs(store, pid)[0]
        assert inc["state"] == "notified_agent"  # still open (only 1 stable round, need 2)
        details = json.loads(inc["details"])
        assert details["consecutive_stable"] == 1


# ── state machine: resolve via stopping ──────────────────────────────────────

class TestResolvePath:
    def _build_notified(self):
        store, pid, dets = _stuck_chain(5)
        _reconcile_round(store, pid, dets)
        return store, pid, dets

    def test_n_stable_rounds_resolves(self):
        """N_STABLE=2 consecutive rounds without modification → resolved."""
        store, pid, dets = self._build_notified()
        for _ in range(signals.N_STABLE):
            _no_change_round(store, pid)
        assert _open_incs(store, pid) == []
        # Verify the incident was resolved (check via raw query)
        rows = store.con.execute(
            "SELECT state, resolved_by FROM incidents WHERE project_id = ?", (pid,)
        ).fetchall()
        assert rows[0]["state"] == "resolved"
        assert rows[0]["resolved_by"] == "stable_region"

    def test_one_stable_not_enough(self):
        """Only 1 stable round (< N_STABLE=2): incident remains open."""
        store, pid, dets = self._build_notified()
        _no_change_round(store, pid)
        assert len(_open_incs(store, pid)) == 1

    def test_resolve_after_escalated(self):
        """After escalation, N_STABLE stable rounds → resolved."""
        store, pid, dets = _stuck_chain(5)
        # Build to escalated_user
        _reconcile_round(store, pid, dets)
        cur_dets = list(dets)
        for i in range(6, 6 + signals.THETA_2["stuck_region"]):
            new_det = store.insert_determination(
                pid, seq=i, file_path=FILE, define_name=DEFINE,
                node_fingerprint=f"fp{i}", edit_type="modify",
            )
            store.insert_gd_edge(pid, from_det=cur_dets[-1], to_det=new_det,
                                 edge_class="version_chain")
            cur_dets.append(new_det)
            _reconcile_round(store, pid, cur_dets)
        inc = _open_incs(store, pid)[0]
        assert inc["state"] == "escalated_user"
        # Now stop changing
        for _ in range(signals.N_STABLE):
            _no_change_round(store, pid)
        assert _open_incs(store, pid) == []


# ── lateral context ───────────────────────────────────────────────────────────

class TestLateralContext:
    def test_omega_positive_callee_appears_in_details(self):
        """Callee that shares ancestors with stuck define shows up in incident details."""
        store, pid, dets = _stuck_chain(5)
        # callee_det depends on fn_v1 (GD edge fn_v1 → callee_det)
        callee_det_id = store.insert_determination(
            pid, seq=10, file_path="/test/callee.py",
            define_name="callee_fn", node_fingerprint="cfp", edit_type="create",
        )
        store.insert_gd_edge(pid, from_det=dets[0], to_det=callee_det_id,
                             edge_class="cross_define_callgraph")
        # fn_v5 also depends on callee_det (GD edge callee_det → fn_v5)
        store.insert_gd_edge(pid, from_det=callee_det_id, to_det=dets[4],
                             edge_class="cross_define_callgraph")
        # Call edge: FILE is /test/f.py (py::) and callee is /test/callee.py (py::)
        store.upsert_call_edge(pid, caller="py::f.fn", callee="py::callee.callee_fn", edge_kind="call")

        mock_idx = SymbolIndex(loc={"py::callee.callee_fn": ("/test/callee.py", "callee_fn")})
        affected = [(FILE, DEFINE, dets[4])]
        signals.detect_stuck_region(store, pid, affected, ROOT, mock_idx)

        inc = _open_incs(store, pid)[0]
        details = json.loads(inc["details"])
        shared = details.get("lateral", {}).get("shared_ancestry", [])
        # callee_fn shares fn_v1 with fn_v5 → ω >= 1
        assert len(shared) > 0
        assert shared[0]["omega"] >= 1
        assert "callee.callee_fn" in shared[0]["define"]

    def test_no_callees_empty_lateral(self):
        """No call edges → lateral context is empty (no error)."""
        store, pid, dets = _stuck_chain(5)
        affected = [(FILE, DEFINE, dets[4])]
        signals.detect_stuck_region(store, pid, affected, ROOT, EMPTY_IDX)
        inc = _open_incs(store, pid)[0]
        details = json.loads(inc["details"])
        # lateral is empty dict or has empty shared_ancestry
        lateral = details.get("lateral", {})
        assert lateral.get("shared_ancestry", []) == []


# ── define_loop helpers ───────────────────────────────────────────────────────

FILE2 = "/test/g.py"
DEFINE2 = "gn"


def _loop_chain():
    """v1(fp0)→v2(fp1)→v3(fp0): v3 loops back to v1, gap=2 = N_LOOP_GAP."""
    store = _mem_store()
    pid = store.get_or_create_project(ROOT)
    dets = []
    for i, fp in enumerate(["fp0", "fp1", "fp0"], 1):
        det_id = store.insert_determination(
            pid, seq=i, file_path=FILE, define_name=DEFINE,
            node_fingerprint=fp, edit_type="create" if i == 1 else "modify",
        )
        dets.append(det_id)
    for i in range(len(dets) - 1):
        store.insert_gd_edge(pid, from_det=dets[i], to_det=dets[i + 1],
                             edge_class="version_chain")
    return store, pid, dets


# ── detection tests ───────────────────────────────────────────────────────────

class TestDetectDefineLoop:
    def test_non_adjacent_equiv_triggers(self):
        """v1→v2→v3 with fp[v3]=fp[v1] (gap=2 >= N_LOOP_GAP=2) → define_loop."""
        store, pid, dets = _loop_chain()
        _reconcile_round(store, pid, dets)
        incs = _open_incs(store, pid)
        assert len(incs) == 1
        assert incs[0]["signal"] == "define_loop"
        assert incs[0]["target_node"] == f"{FILE}::{DEFINE}"

    def test_details_contain_seqs_and_question(self):
        store, pid, dets = _loop_chain()
        _reconcile_round(store, pid, dets)
        details = json.loads(_open_incs(store, pid)[0]["details"])
        assert details["earlier_seq"] == 1
        assert details["later_seq"] == 3
        # Loop trigger → "已回退到之前的结构状态" message
        assert "reverted to a structural state it held earlier" in details["loop_question"]

    def test_adjacent_same_fp_no_trigger(self):
        """Adjacent fp repeat (gap=1 < N_LOOP_GAP=2): not a loop."""
        store = _mem_store()
        pid = store.get_or_create_project(ROOT)
        dets = []
        for i, fp in enumerate(["fp0", "fp0", "fp1"], 1):
            det_id = store.insert_determination(
                pid, seq=i, file_path=FILE, define_name=DEFINE,
                node_fingerprint=fp, edit_type="create" if i == 1 else "modify",
            )
            dets.append(det_id)
        for i in range(len(dets) - 1):
            store.insert_gd_edge(pid, from_det=dets[i], to_det=dets[i + 1],
                                 edge_class="version_chain")
        _reconcile_round(store, pid, dets)
        assert _open_incs(store, pid) == []

    def test_all_distinct_no_trigger(self):
        """All distinct fingerprints (stuck_region territory): no define_loop."""
        store, pid, dets = _stuck_chain(3)
        _reconcile_round(store, pid, dets)
        assert all(inc["signal"] != "define_loop" for inc in _open_incs(store, pid))

    def test_idempotent_no_duplicate(self):
        store, pid, dets = _loop_chain()
        _reconcile_round(store, pid, dets)
        _reconcile_round(store, pid, dets)
        loop_incs = [i for i in _open_incs(store, pid) if i["signal"] == "define_loop"]
        assert len(loop_incs) == 1

    def test_lateral_attached(self):
        """Callee with shared ancestry appears in define_loop details."""
        store, pid, dets = _loop_chain()
        callee_det_id = store.insert_determination(
            pid, seq=10, file_path="/test/callee.py",
            define_name="callee_fn", node_fingerprint="cfp", edit_type="create",
        )
        store.insert_gd_edge(pid, from_det=dets[0], to_det=callee_det_id,
                             edge_class="cross_define_callgraph")
        store.insert_gd_edge(pid, from_det=callee_det_id, to_det=dets[2],
                             edge_class="cross_define_callgraph")
        store.upsert_call_edge(pid, caller="py::f.fn", callee="py::callee.callee_fn", edge_kind="call")
        mock_idx = SymbolIndex(loc={"py::callee.callee_fn": ("/test/callee.py", "callee_fn")})
        affected = [(FILE, DEFINE, dets[2])]
        signals.detect_define_loop(store, pid, affected, ROOT, mock_idx)
        details = json.loads(_open_incs(store, pid)[0]["details"])
        shared = details.get("lateral", {}).get("shared_ancestry", [])
        assert len(shared) > 0
        assert shared[0]["omega"] >= 1


# ── state machine ─────────────────────────────────────────────────────────────

class TestDefineLoopStateMachine:
    def _build_notified(self):
        store, pid, dets = _loop_chain()
        _reconcile_round(store, pid, dets)   # open → notified_agent
        return store, pid, dets

    def test_open_to_notified(self):
        store, pid, dets = self._build_notified()
        inc = _open_incs(store, pid)[0]
        assert inc["signal"] == "define_loop"
        assert inc["state"] == "notified_agent"

    def test_recurrence_increments_count(self):
        """v4 loops back to fp0 again: _latest_loops=True → post_notify_count=1."""
        store, pid, dets = self._build_notified()
        det4 = store.insert_determination(
            pid, seq=4, file_path=FILE, define_name=DEFINE,
            node_fingerprint="fp0", edit_type="modify",
        )
        store.insert_gd_edge(pid, from_det=dets[-1], to_det=det4,
                             edge_class="version_chain")
        _reconcile_round(store, pid, dets + [det4])
        inc = _open_incs(store, pid)[0]
        assert inc["signal"] == "define_loop"
        assert inc["state"] == "notified_agent"
        assert inc["post_notify_count"] == 1

    def test_theta2_recurrences_escalate(self):
        """θ₂=2 recurrences → escalated_user."""
        store, pid, dets = self._build_notified()
        cur_dets = list(dets)
        for i in range(4, 4 + signals.THETA_2["define_loop"]):
            new_det = store.insert_determination(
                pid, seq=i, file_path=FILE, define_name=DEFINE,
                node_fingerprint="fp0", edit_type="modify",
            )
            store.insert_gd_edge(pid, from_det=cur_dets[-1], to_det=new_det,
                                 edge_class="version_chain")
            cur_dets.append(new_det)
            _reconcile_round(store, pid, cur_dets)
        inc = _open_incs(store, pid)[0]
        assert inc["state"] == "escalated_user"
        assert inc["post_notify_count"] == signals.THETA_2["define_loop"]

    def test_n_stable_rounds_resolve(self):
        """N_STABLE no-change rounds → resolved / no_more_equiv."""
        store, pid, dets = self._build_notified()
        for _ in range(signals.N_STABLE):
            _no_change_round(store, pid)
        assert _open_incs(store, pid) == []
        rows = store.con.execute(
            "SELECT state, resolved_by FROM incidents "
            "WHERE project_id = ? AND signal = 'define_loop'",
            (pid,),
        ).fetchall()
        assert rows[0]["state"] == "resolved"
        assert rows[0]["resolved_by"] == "no_more_equiv"


# ── coexistence ───────────────────────────────────────────────────────────────

class TestDefineLoopCoexistence:
    def test_loop_and_stuck_on_different_defines(self):
        """define_loop on DEFINE and stuck_region on DEFINE2 don't interfere."""
        store = _mem_store()
        pid = store.get_or_create_project(ROOT)

        # DEFINE: loop chain v1(fp0)→v2(fp1)→v3(fp0)
        loop_dets = []
        for i, fp in enumerate(["fp0", "fp1", "fp0"], 1):
            det_id = store.insert_determination(
                pid, seq=i, file_path=FILE, define_name=DEFINE,
                node_fingerprint=fp, edit_type="create" if i == 1 else "modify",
            )
            loop_dets.append(det_id)
        for i in range(len(loop_dets) - 1):
            store.insert_gd_edge(pid, from_det=loop_dets[i], to_det=loop_dets[i + 1],
                                 edge_class="version_chain")

        # DEFINE2: pure chain of 5 → stuck_region territory
        stuck_dets = []
        for i in range(1, 6):
            det_id = store.insert_determination(
                pid, seq=10 + i, file_path=FILE2, define_name=DEFINE2,
                node_fingerprint=f"sfp{i}", edit_type="create" if i == 1 else "modify",
            )
            stuck_dets.append(det_id)
        for i in range(len(stuck_dets) - 1):
            store.insert_gd_edge(pid, from_det=stuck_dets[i], to_det=stuck_dets[i + 1],
                                 edge_class="version_chain")

        affected = [(FILE, DEFINE, loop_dets[-1]), (FILE2, DEFINE2, stuck_dets[-1])]
        signals.detect_define_loop(store, pid, affected, ROOT, EMPTY_IDX)
        signals.detect_stuck_region(store, pid, affected, ROOT, EMPTY_IDX)
        signals.advance_incidents(store, pid, affected)

        incs = _open_incs(store, pid)
        sigs = {inc["signal"] for inc in incs}
        assert "define_loop" in sigs
        assert "stuck_region" in sigs
        assert len(incs) == 2


# ── two-signal coexistence ────────────────────────────────────────────────────

class TestTwoSignalsCoexist:
    def test_define_loop_and_stuck_region_on_different_defines(self):
        """define_loop + stuck_region on separate defines don't interfere."""
        store = _mem_store()
        pid = store.get_or_create_project(ROOT)

        # DEFINE / FILE: loop chain → define_loop
        loop_dets = []
        for i, fp in enumerate(["fp0", "fp1", "fp0"], 1):
            det_id = store.insert_determination(
                pid, seq=i, file_path=FILE, define_name=DEFINE,
                node_fingerprint=fp, edit_type="create" if i == 1 else "modify",
            )
            loop_dets.append(det_id)
        for i in range(len(loop_dets) - 1):
            store.insert_gd_edge(pid, from_det=loop_dets[i], to_det=loop_dets[i + 1],
                                 edge_class="version_chain")

        # DEFINE2 / FILE2: stuck chain of 5 → stuck_region
        stuck_dets = []
        for i in range(1, 6):
            det_id = store.insert_determination(
                pid, seq=10 + i, file_path=FILE2, define_name=DEFINE2,
                node_fingerprint=f"sfp{i}", edit_type="create" if i == 1 else "modify",
            )
            stuck_dets.append(det_id)
        for i in range(len(stuck_dets) - 1):
            store.insert_gd_edge(pid, from_det=stuck_dets[i], to_det=stuck_dets[i + 1],
                                 edge_class="version_chain")

        affected = [
            (FILE, DEFINE, loop_dets[-1]),
            (FILE2, DEFINE2, stuck_dets[-1]),
        ]
        signals.detect_define_loop(store, pid, affected, ROOT, EMPTY_IDX)
        signals.detect_stuck_region(store, pid, affected, ROOT, EMPTY_IDX)
        signals.advance_incidents(store, pid, affected)

        incs = _open_incs(store, pid)
        sigs = {inc["signal"] for inc in incs}
        assert "define_loop" in sigs
        assert "stuck_region" in sigs
        assert len(incs) == 2


# ── _within_boundary (pure function tests) ────────────────────────────────────

from buer.reconcile import _within_boundary   # noqa: E402


class TestWithinBoundary:
    def test_inside_root(self):
        assert _within_boundary("/project/src/f.py", ["/project"], []) is True

    def test_exact_root_match(self):
        assert _within_boundary("/project", ["/project"], []) is True

    def test_outside_root(self):
        assert _within_boundary("/outside/f.py", ["/project"], []) is False

    def test_prefix_false_positive_prevented(self):
        """/project-backup should NOT match root /project."""
        assert _within_boundary("/project-backup/f.py", ["/project"], []) is False

    def test_multi_root_second_root_in_bounds(self):
        """File in second root is in-bounds (monorepo sub-project)."""
        roots = ["/mono/service-a", "/mono/service-b"]
        assert _within_boundary("/mono/service-b/f.py", roots, []) is True

    def test_outside_all_roots_is_violation(self):
        roots = ["/mono/service-a", "/mono/service-b"]
        assert _within_boundary("/mono/service-c/f.py", roots, []) is False

    def test_whitelist_allows_external_path(self):
        """Whitelisted dir (e.g. build output) is in-bounds even if outside roots."""
        assert _within_boundary("/build/out/result.js", ["/project"], ["/build"]) is True

    def test_whitelist_prefix_exact(self):
        assert _within_boundary("/build", ["/project"], ["/build"]) is True


# ── boundary_breach detection ─────────────────────────────────────────────────

FILE_OUT = "/outside/evil.py"


class TestDetectBoundaryBreach:
    def test_out_of_project_triggers(self):
        """Violated file path → boundary_breach incident, open→notified in same round."""
        store = _mem_store()
        pid = store.get_or_create_project(ROOT)
        signals.detect_boundary_breach(store, pid, [FILE_OUT])
        signals.advance_incidents(store, pid, [], boundary_violations=[FILE_OUT])
        incs = _open_incs(store, pid)
        assert len(incs) == 1
        assert incs[0]["signal"] == "boundary_breach"
        assert incs[0]["state"] == "notified_agent"

    def test_target_is_file_path_not_define(self):
        """target_node is the raw file path — no '::' separator (§2.4)."""
        store = _mem_store()
        pid = store.get_or_create_project(ROOT)
        signals.detect_boundary_breach(store, pid, [FILE_OUT])
        inc = _open_incs(store, pid)[0]
        assert inc["target_node"] == FILE_OUT
        assert "::" not in inc["target_node"]

    def test_no_violation_no_trigger(self):
        store = _mem_store()
        pid = store.get_or_create_project(ROOT)
        signals.detect_boundary_breach(store, pid, [])
        assert _open_incs(store, pid) == []

    def test_idempotent(self):
        store = _mem_store()
        pid = store.get_or_create_project(ROOT)
        signals.detect_boundary_breach(store, pid, [FILE_OUT])
        signals.detect_boundary_breach(store, pid, [FILE_OUT])
        bb_incs = [i for i in _open_incs(store, pid) if i["signal"] == "boundary_breach"]
        assert len(bb_incs) == 1

    def test_details_contain_note(self):
        store = _mem_store()
        pid = store.get_or_create_project(ROOT)
        signals.detect_boundary_breach(store, pid, [FILE_OUT])
        details = json.loads(_open_incs(store, pid)[0]["details"])
        assert "outside the project directory" in details["note"]
        assert FILE_OUT in details["note"]


# ── boundary_breach state machine ─────────────────────────────────────────────

class TestBoundaryBreachStateMachine:
    def _build_notified(self):
        store = _mem_store()
        pid = store.get_or_create_project(ROOT)
        signals.detect_boundary_breach(store, pid, [FILE_OUT])
        signals.advance_incidents(store, pid, [], boundary_violations=[FILE_OUT])
        return store, pid

    def test_open_to_notified(self):
        store, pid = self._build_notified()
        inc = _open_incs(store, pid)[0]
        assert inc["signal"] == "boundary_breach"
        assert inc["state"] == "notified_agent"

    def test_theta2_1_recurrence_escalates(self):
        """θ₂=1 for boundary_breach: one recurrence → escalated_user."""
        store, pid = self._build_notified()
        # Second reconcile round: same file still out of bounds
        signals.detect_boundary_breach(store, pid, [FILE_OUT])  # idempotent
        signals.advance_incidents(store, pid, [], boundary_violations=[FILE_OUT])
        inc = _open_incs(store, pid)[0]
        assert inc["state"] == "escalated_user"
        assert inc["post_notify_count"] == signals.THETA_2["boundary_breach"]

    def test_resolve_back_in_scope(self):
        """No more violations → N_STABLE stable rounds → resolved/back_in_scope."""
        store, pid = self._build_notified()
        for _ in range(signals.N_STABLE):
            signals.advance_incidents(store, pid, [], boundary_violations=[])
        assert _open_incs(store, pid) == []
        rows = store.con.execute(
            "SELECT resolved_by FROM incidents "
            "WHERE project_id = ? AND signal = 'boundary_breach'",
            (pid,),
        ).fetchall()
        assert rows[0]["resolved_by"] == "back_in_scope"


# ── all-four-signals coexistence ──────────────────────────────────────────────

class TestThreeSignalsCoexist:
    def test_three_signals_on_separate_defines_and_file(self):
        """stuck_region + define_loop + boundary_breach all coexist."""
        store = _mem_store()
        pid = store.get_or_create_project(ROOT)

        # define_loop: loop chain on FILE/DEFINE
        loop_dets = []
        for i, fp in enumerate(["fp0", "fp1", "fp0"], 1):
            det_id = store.insert_determination(
                pid, seq=i, file_path=FILE, define_name=DEFINE,
                node_fingerprint=fp, edit_type="create" if i == 1 else "modify",
            )
            loop_dets.append(det_id)
        for i in range(len(loop_dets) - 1):
            store.insert_gd_edge(pid, from_det=loop_dets[i], to_det=loop_dets[i + 1],
                                 edge_class="version_chain")

        # stuck_region: pure chain of 5 on FILE2/DEFINE2
        stuck_dets = []
        for i in range(1, 6):
            det_id = store.insert_determination(
                pid, seq=10 + i, file_path=FILE2, define_name=DEFINE2,
                node_fingerprint=f"sfp{i}", edit_type="create" if i == 1 else "modify",
            )
            stuck_dets.append(det_id)
        for i in range(len(stuck_dets) - 1):
            store.insert_gd_edge(pid, from_det=stuck_dets[i], to_det=stuck_dets[i + 1],
                                 edge_class="version_chain")

        violations = [FILE_OUT]
        affected = [
            (FILE, DEFINE, loop_dets[-1]),
            (FILE2, DEFINE2, stuck_dets[-1]),
        ]

        signals.detect_define_loop(store, pid, affected, ROOT, EMPTY_IDX)
        signals.detect_stuck_region(store, pid, affected, ROOT, EMPTY_IDX)
        signals.detect_boundary_breach(store, pid, violations)
        signals.advance_incidents(store, pid, affected, boundary_violations=violations)

        incs = _open_incs(store, pid)
        sigs = {inc["signal"] for inc in incs}
        assert "define_loop" in sigs
        assert "stuck_region" in sigs
        assert "boundary_breach" in sigs
        assert len(incs) == 3


# ── define_loop full version: Trigger B (consecutive) + dual trigger ──────────

def _consec_chain(n: int, file_path: str = FILE, define_name: str = DEFINE):
    """n consecutive edits to the same define, with shared background ancestors.

    Background ancestors come first (lower seq), define edits after (higher seq).
    Shared ancestors keep d_J < THETA_DJ so _is_stuck returns False — this topology
    is NOT stuck_region territory.
    """
    store = _mem_store()
    pid = store.get_or_create_project(ROOT)
    # 4 shared background ancestors (seq 1-4)
    bg_ids = []
    for i in range(4):
        bg_id = store.insert_determination(
            pid, seq=i + 1, file_path="/test/bg.py",
            define_name=f"bg{i}", node_fingerprint=f"bgfp{i}", edit_type="create",
        )
        bg_ids.append(bg_id)
    for i in range(len(bg_ids) - 1):
        store.insert_gd_edge(pid, from_det=bg_ids[i], to_det=bg_ids[i + 1],
                             edge_class="version_chain")
    # define chain (seq 5, 6, ..., 4+n)
    dets = []
    for i in range(1, n + 1):
        det_id = store.insert_determination(
            pid, seq=4 + i, file_path=file_path, define_name=define_name,
            node_fingerprint=f"unique_fp_{i}", edit_type="create" if i == 1 else "modify",
        )
        dets.append(det_id)
    for i in range(len(dets) - 1):
        store.insert_gd_edge(pid, from_det=dets[i], to_det=dets[i + 1],
                             edge_class="version_chain")
    # Shared ancestry: last bg node feeds into first define det
    store.insert_gd_edge(pid, from_det=bg_ids[-1], to_det=dets[0],
                         edge_class="cross_define_callgraph")
    return store, pid, dets


class TestDefineLoopTriggerB:
    """Trigger B: consecutive_edit_count >= N_DEFINE_LOOP_CONSEC_AGENT (5) fires without loop-back."""

    def test_5_consecutive_fires(self):
        """5 consecutive edits to same define, no fp repetition → define_loop via Trigger B."""
        store, pid, dets = _consec_chain(signals.N_DEFINE_LOOP_CONSEC_AGENT)
        affected = [(FILE, DEFINE, dets[-1])]
        signals.detect_define_loop(store, pid, affected, ROOT, EMPTY_IDX)
        incs = [i for i in _open_incs(store, pid) if i["signal"] == "define_loop"]
        assert len(incs) == 1
        details = json.loads(incs[0]["details"])
        assert details["trigger_type"] == "consec"
        assert details["consec"] == signals.N_DEFINE_LOOP_CONSEC_AGENT
        assert "escalate_user_directly" in details
        assert details["escalate_user_directly"] is False

    def test_4_consecutive_no_fire(self):
        """4 consecutive edits (< threshold): no define_loop from Trigger B."""
        store, pid, dets = _consec_chain(signals.N_DEFINE_LOOP_CONSEC_AGENT - 1)
        affected = [(FILE, DEFINE, dets[-1])]
        signals.detect_define_loop(store, pid, affected, ROOT, EMPTY_IDX)
        incs = [i for i in _open_incs(store, pid) if i["signal"] == "define_loop"]
        assert len(incs) == 0

    def test_8_consecutive_sets_escalate_user(self):
        """8 consecutive edits (>= N_DEFINE_LOOP_CONSEC_USER=8): escalate_user_directly=True."""
        store, pid, dets = _consec_chain(signals.N_DEFINE_LOOP_CONSEC_USER)
        affected = [(FILE, DEFINE, dets[-1])]
        signals.detect_define_loop(store, pid, affected, ROOT, EMPTY_IDX)
        incs = [i for i in _open_incs(store, pid) if i["signal"] == "define_loop"]
        assert len(incs) == 1
        details = json.loads(incs[0]["details"])
        assert details["escalate_user_directly"] is True

    def test_8_consecutive_advance_goes_directly_to_escalated_user(self):
        """escalate_user_directly=True → advance_incidents routes to escalated_user state."""
        store, pid, dets = _consec_chain(signals.N_DEFINE_LOOP_CONSEC_USER)
        affected = [(FILE, DEFINE, dets[-1])]
        signals.detect_define_loop(store, pid, affected, ROOT, EMPTY_IDX)
        signals.advance_incidents(store, pid, affected)
        incs = [i for i in _open_incs(store, pid) if i["signal"] == "define_loop"]
        assert incs[0]["state"] == "escalated_user"

    def test_interrupted_streak_no_trigger(self):
        """Other define edited between: streak broken, count < threshold."""
        store = _mem_store()
        pid = store.get_or_create_project(ROOT)
        # 3 edits to DEFINE, then 1 edit to DEFINE2, then 1 more edit to DEFINE
        dets = []
        for i in range(1, 4):
            det_id = store.insert_determination(
                pid, seq=i, file_path=FILE, define_name=DEFINE,
                node_fingerprint=f"fp{i}", edit_type="create" if i == 1 else "modify",
            )
            dets.append(det_id)
        # Interrupt
        store.insert_determination(
            pid, seq=4, file_path=FILE2, define_name=DEFINE2,
            node_fingerprint="fp_other", edit_type="modify",
        )
        # 1 more to DEFINE (streak reset to 1)
        last_det = store.insert_determination(
            pid, seq=5, file_path=FILE, define_name=DEFINE,
            node_fingerprint="fp5", edit_type="modify",
        )
        dets.append(last_det)
        affected = [(FILE, DEFINE, last_det)]
        signals.detect_define_loop(store, pid, affected, ROOT, EMPTY_IDX)
        incs = [i for i in _open_incs(store, pid) if i["signal"] == "define_loop"]
        assert len(incs) == 0

    def test_consec_message_content(self):
        """consec trigger without error data → soft 'N次' message."""
        store, pid, dets = _consec_chain(signals.N_DEFINE_LOOP_CONSEC_AGENT)
        affected = [(FILE, DEFINE, dets[-1])]
        signals.detect_define_loop(store, pid, affected, ROOT, EMPTY_IDX)
        inc = [i for i in _open_incs(store, pid) if i["signal"] == "define_loop"][0]
        details = json.loads(inc["details"])
        q = details["loop_question"]
        # No crash data → soft wording (not "报错始终是同类" or "报错在变化")
        assert str(signals.N_DEFINE_LOOP_CONSEC_AGENT) in q or DEFINE in q

    def test_loop_trigger_message_says_revert(self):
        """Loop-back trigger → message contains '已回退'."""
        store, pid, dets = _loop_chain()
        affected = [(FILE, DEFINE, dets[-1])]
        signals.detect_define_loop(store, pid, affected, ROOT, EMPTY_IDX)
        inc = [i for i in _open_incs(store, pid) if i["signal"] == "define_loop"][0]
        details = json.loads(inc["details"])
        assert "reverted" in details["loop_question"]


class TestDefineLoopDualTrigger:
    """Both Trigger A (loop) and Trigger B (consecutive) active simultaneously."""

    def test_both_triggers_sets_trigger_type_both(self):
        """Loop-back AND consecutive ≥ 5: trigger_type='both'."""
        store = _mem_store()
        pid = store.get_or_create_project(ROOT)
        # 6 consecutive edits, with fp[0] recurring at position 5 (non-adjacent loop)
        fps = ["fp0", "fp1", "fp2", "fp3", "fp4", "fp0"]
        dets = []
        for i, fp in enumerate(fps, 1):
            det_id = store.insert_determination(
                pid, seq=i, file_path=FILE, define_name=DEFINE,
                node_fingerprint=fp, edit_type="create" if i == 1 else "modify",
            )
            dets.append(det_id)
        affected = [(FILE, DEFINE, dets[-1])]
        signals.detect_define_loop(store, pid, affected, ROOT, EMPTY_IDX)
        incs = [i for i in _open_incs(store, pid) if i["signal"] == "define_loop"]
        assert len(incs) == 1
        details = json.loads(incs[0]["details"])
        assert details["trigger_type"] == "both"

    def test_loop_only_trigger_type_loop(self):
        """Only 3 consecutive (< 5) but has loop-back → trigger_type='loop'."""
        store, pid, dets = _loop_chain()
        # Only 3 dets → consecutive_edit_count = 3 < 5
        affected = [(FILE, DEFINE, dets[-1])]
        signals.detect_define_loop(store, pid, affected, ROOT, EMPTY_IDX)
        inc = [i for i in _open_incs(store, pid) if i["signal"] == "define_loop"][0]
        details = json.loads(inc["details"])
        assert details["trigger_type"] == "loop"


class TestDefineLoopSignalRecurred:
    """_signal_recurred for define_loop now checks consecutive OR loop-back."""

    def _build_consec_incident(self, n: int):
        store, pid, dets = _consec_chain(n)
        affected = [(FILE, DEFINE, dets[-1])]
        signals.detect_define_loop(store, pid, affected, ROOT, EMPTY_IDX)
        signals.advance_incidents(store, pid, affected)  # open → notified_agent
        return store, pid, dets

    def test_consecutive_still_meets_threshold_recurs(self):
        """After incident created, another edit keeping streak >= 5 → recurs."""
        store, pid, dets = self._build_consec_incident(signals.N_DEFINE_LOOP_CONSEC_AGENT)
        # Add 6th edit (streak becomes 6 >= 5)
        det6 = store.insert_determination(
            pid, seq=store.next_seq(pid), file_path=FILE, define_name=DEFINE,
            node_fingerprint="fp_new", edit_type="modify",
        )
        dets.append(det6)
        affected = [(FILE, DEFINE, det6)]
        signals.advance_incidents(store, pid, affected)
        inc = [i for i in _open_incs(store, pid) if i["signal"] == "define_loop"][0]
        assert inc["post_notify_count"] == 1

    def test_streak_broken_no_recurrence(self):
        """After incident, different define edited → streak broken (< 5) → not recurred."""
        store, pid, dets = self._build_consec_incident(signals.N_DEFINE_LOOP_CONSEC_AGENT)
        # Edit a different define → breaks DEFINE's streak
        store.insert_determination(
            pid, seq=store.next_seq(pid), file_path=FILE2, define_name=DEFINE2,
            node_fingerprint="fp_other", edit_type="modify",
        )
        affected = [(FILE2, DEFINE2, None)]
        signals.advance_incidents(store, pid, affected)
        inc = [i for i in _open_incs(store, pid) if i["signal"] == "define_loop"][0]
        # Streak for DEFINE now = 0, no loop: stable counter increments
        details = json.loads(inc["details"])
        assert details["consecutive_stable"] == 1


# ── _is_excluded_path: new boilerplate globs ─────────────────────────────────

class TestExclusionGlobs:
    """_is_excluded_path with the new Next.js + i18n legal content globs."""

    # ── Next.js convention files ──────────────────────────────────────────────

    def test_loading_tsx_excluded(self):
        assert signals._is_excluded_path("/proj/app/(app)/loading.tsx")

    def test_loading_jsx_excluded(self):
        assert signals._is_excluded_path("/proj/app/(public)/loading.jsx")

    def test_error_tsx_excluded(self):
        assert signals._is_excluded_path("/proj/app/(admin)/error.tsx")

    def test_error_jsx_excluded(self):
        assert signals._is_excluded_path("/proj/app/error.jsx")

    def test_not_found_tsx_excluded(self):
        assert signals._is_excluded_path("/proj/app/not-found.tsx")

    def test_not_found_jsx_excluded(self):
        assert signals._is_excluded_path("/proj/src/app/not-found.jsx")

    def test_global_error_tsx_excluded(self):
        assert signals._is_excluded_path("/proj/app/global-error.tsx")

    def test_template_tsx_excluded(self):
        assert signals._is_excluded_path("/proj/app/(app)/template.tsx")

    def test_default_tsx_excluded(self):
        assert signals._is_excluded_path("/proj/app/@modal/default.tsx")

    def test_page_tsx_excluded(self):
        assert signals._is_excluded_path("/proj/app/(admin)/admin/audit/page.tsx")

    def test_page_jsx_excluded(self):
        assert signals._is_excluded_path("/proj/app/(public)/login/page.jsx")

    def test_layout_tsx_excluded(self):
        assert signals._is_excluded_path("/proj/app/(public)/layout.tsx")

    def test_layout_jsx_excluded(self):
        assert signals._is_excluded_path("/proj/app/layout.jsx")

    def test_nested_page_tsx_excluded(self):
        assert signals._is_excluded_path("/proj/app/(admin)/admin/projects/page.tsx")

    # ── i18n legal content ────────────────────────────────────────────────────

    def test_legal_tsx_excluded(self):
        assert signals._is_excluded_path("/proj/components/legal/BetaTermsContent.tsx")

    def test_legal_other_tsx_excluded(self):
        assert signals._is_excluded_path("/proj/components/legal/PrivacyPolicyContent.tsx")

    def test_legal_tsx_deep_excluded(self):
        assert signals._is_excluded_path("/proj/components/legal/TermsOfServiceContent.tsx")

    # ── true production code NOT excluded ────────────────────────────────────

    def test_real_component_not_excluded(self):
        """A real production component must not be excluded."""
        assert not signals._is_excluded_path("/proj/components/ui/skeleton.tsx")

    def test_normalizers_not_excluded(self):
        assert not signals._is_excluded_path("/proj/lib/capability/normalizers.ts")

    def test_landing_client_not_excluded(self):
        assert not signals._is_excluded_path("/proj/app/LandingClient.tsx")

    def test_api_route_not_excluded(self):
        """API routes (route.ts) are real code, not convention boilerplate."""
        assert not signals._is_excluded_path("/proj/app/api/v3/analyze/route.ts")

    def test_trading_agents_not_excluded(self):
        """TradingAgents stubs must stay detectable (true positives)."""
        assert not signals._is_excluded_path("/proj/TradingAgents/tradingagents/agents/analysts/fundamentals_analyst.py")

    def test_components_legal_ts_not_excluded(self):
        """Only *.tsx under components/legal — a .ts helper there still fires."""
        assert not signals._is_excluded_path("/proj/components/legal/utils.ts")

    def test_non_legal_tsx_not_excluded(self):
        """TSX in other component directories is NOT covered by legal glob."""
        assert not signals._is_excluded_path("/proj/components/omni/OmniGenUiRenderer.tsx")



# ── _is_excluded_path: bare-path edge cases (prefix-bug regression) ───────────

class TestExclusionGlobsBarePathRegression:
    """Paths without a leading directory — the */X glob would miss these.

    _is_excluded_path must handle all path depths:
      - bare filename:        "loading.tsx"
      - short relative:       "app/loading.tsx"
      - relative with dirs:   "components/legal/X.tsx"
      - absolute:             "/home/.../app/loading.tsx"
    """

    # ── Next.js convention files — bare/short paths ───────────────────────────

    def test_bare_loading_excluded(self):
        assert signals._is_excluded_path("loading.tsx")

    def test_bare_page_excluded(self):
        assert signals._is_excluded_path("page.tsx")

    def test_bare_layout_excluded(self):
        assert signals._is_excluded_path("layout.tsx")

    def test_bare_error_excluded(self):
        assert signals._is_excluded_path("error.tsx")

    def test_bare_not_found_excluded(self):
        assert signals._is_excluded_path("not-found.tsx")

    def test_bare_global_error_excluded(self):
        assert signals._is_excluded_path("global-error.tsx")

    def test_bare_template_excluded(self):
        assert signals._is_excluded_path("template.tsx")

    def test_bare_default_excluded(self):
        assert signals._is_excluded_path("default.tsx")

    def test_bare_page_jsx_excluded(self):
        assert signals._is_excluded_path("page.jsx")

    def test_short_relative_loading_excluded(self):
        assert signals._is_excluded_path("app/loading.tsx")

    def test_short_relative_page_excluded(self):
        assert signals._is_excluded_path("app/(admin)/admin/audit/page.tsx")

    # ── legal content — bare/short paths ─────────────────────────────────────

    def test_bare_legal_tsx_excluded(self):
        """legal/X.tsx without any leading directory must be excluded."""
        assert signals._is_excluded_path("legal/BetaTermsContent.tsx")

    def test_relative_components_legal_tsx_excluded(self):
        """components/legal/X.tsx (relative, no leading slash) must be excluded."""
        assert signals._is_excluded_path("components/legal/BetaTermsContent.tsx")

    def test_absolute_legal_tsx_excluded(self):
        assert signals._is_excluded_path("/proj/components/legal/TermsOfServiceContent.tsx")

    def test_legal_ts_not_excluded(self):
        """Only .tsx inside legal/ is excluded — .ts helper stays detectable."""
        assert not signals._is_excluded_path("components/legal/legalUtils.ts")
        assert not signals._is_excluded_path("legal/utils.ts")

    # ── true production code NOT excluded at any depth ────────────────────────

    def test_bare_tsx_not_excluded(self):
        """A bare .tsx with a non-convention name must not be excluded."""
        assert not signals._is_excluded_path("Button.tsx")

    def test_short_relative_real_component_not_excluded(self):
        assert not signals._is_excluded_path("components/ui/skeleton.tsx")

    def test_non_legal_tsx_at_depth_not_excluded(self):
        assert not signals._is_excluded_path("components/omni/OmniGenUiRenderer.tsx")


# ---------------------------------------------------------------------------
# Test-file gate: stuck_region / debug_loop / define_loop must not fire on
# test-path defines (noise guard paired with server.py exclude_tests=False fix)
# ---------------------------------------------------------------------------

TEST_FILE_PATH = "/test/test_f.py"
TEST_DEF = "test_fn"


def _test_file_stuck_chain(n: int):
    """Version chain for a TEST-path define (n modifications, large adjacent d_J)."""
    store = _mem_store()
    pid = store.get_or_create_project(ROOT)
    dets = []
    for i in range(1, n + 1):
        det_id = store.insert_determination(
            pid, seq=i, file_path=TEST_FILE_PATH, define_name=TEST_DEF,
            node_fingerprint=f"tfp{i}", edit_type="create" if i == 1 else "modify",
        )
        dets.append(det_id)
    for i in range(len(dets) - 1):
        store.insert_gd_edge(pid, from_det=dets[i], to_det=dets[i + 1],
                             edge_class="version_chain")
    return store, pid, dets


class TestTestFileGates:
    """stuck_region / debug_loop / define_loop must silently skip test-path defines."""

    def test_stuck_region_no_fire_on_test_file(self):
        store, pid, dets = _test_file_stuck_chain(5)
        affected = [(TEST_FILE_PATH, TEST_DEF, dets[-1])]
        signals.detect_stuck_region(store, pid, affected, ROOT, EMPTY_IDX)
        incs = [i for i in store.open_incidents(pid) if i["signal"] == "stuck_region"]
        assert incs == []

    def test_define_loop_no_fire_on_test_file(self):
        store, pid, dets = _test_file_stuck_chain(5)
        affected = [(TEST_FILE_PATH, TEST_DEF, dets[-1])]
        signals.detect_define_loop(store, pid, affected, ROOT, EMPTY_IDX)
        incs = [i for i in store.open_incidents(pid) if i["signal"] == "define_loop"]
        assert incs == []

    def test_debug_loop_no_fire_on_test_file(self):
        store, pid, dets = _test_file_stuck_chain(5)
        # Add a passing test run so debug_loop would fire if gate were absent
        run_id = store.insert_test_run(pid, seq=5, source_path="/t/r.xml",
                                       source_mtime="2024-01-01T00:05:00Z",
                                       passed=0, failed=1, skipped=0)
        store.insert_test_case(run_id, classname="tests.T", name=TEST_DEF,
                               file_path=None, status="failed")
        store.insert_coverage_entry(pid, f"tests.T::{TEST_DEF}", TEST_DEF)
        affected = [(TEST_FILE_PATH, TEST_DEF, dets[-1])]
        signals.detect_debug_loop(store, pid, affected, ROOT, EMPTY_IDX)
        incs = [i for i in store.open_incidents(pid) if i["signal"] == "debug_loop"]
        assert incs == []

    def test_stuck_region_still_fires_on_prod_file(self):
        """Gate must not suppress production-file defines."""
        store, pid, dets = _stuck_chain(5)
        affected = [(FILE, DEFINE, dets[-1])]
        signals.detect_stuck_region(store, pid, affected, ROOT, EMPTY_IDX)
        incs = [i for i in store.open_incidents(pid) if i["signal"] == "stuck_region"]
        assert len(incs) == 1
