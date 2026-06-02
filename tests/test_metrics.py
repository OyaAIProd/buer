"""Tests for buer.metrics — ω / d_J canonical values.

Canonical anchors from SDT Math Ext §12.1.6 (d_J pure-chain values)
and §11.1b (ω definition). All assertions against closed-form values
from the primary source, not from BUER_Design.md (which had a transcription
error: used ↓ instead of anc, yielding d_J(v1,v2)=1 instead of 1/2).
"""
import pytest

from buer.store import Store
from buer import metrics


def _mem_store() -> Store:
    return Store(":memory:")


def _chain(n: int):
    """Pure chain v1→v2→…→vn.  Returns (store, project_id, [id_1,…,id_n])."""
    store = _mem_store()
    pid = store.get_or_create_project("/test")
    dets = []
    for i in range(1, n + 1):
        det_id = store.insert_determination(
            pid, seq=i, file_path="/test/f.py",
            define_name=f"fn{i}", node_fingerprint=f"fp{i}", edit_type="create",
        )
        dets.append(det_id)
    for i in range(len(dets) - 1):
        store.insert_gd_edge(pid, from_det=dets[i], to_det=dets[i + 1],
                             edge_class="version_chain")
    return store, pid, dets


def _antichain():
    """Two isolated nodes (no edges)."""
    store = _mem_store()
    pid = store.get_or_create_project("/test")
    a = store.insert_determination(pid, seq=1, file_path="/test/f.py",
                                   define_name="a", node_fingerprint="fpa", edit_type="create")
    b = store.insert_determination(pid, seq=2, file_path="/test/f.py",
                                   define_name="b", node_fingerprint="fpb", edit_type="create")
    return store, pid, a, b


# ── strict_down / anc ────────────────────────────────────────────────────────

class TestStrictDown:
    def test_root_has_empty_strict_down(self):
        store, pid, dets = _chain(1)
        assert metrics.strict_down(store, pid, dets[0]) == frozenset()

    def test_strict_down_v2(self):
        store, pid, (v1, v2, v3) = _chain(3)
        assert metrics.strict_down(store, pid, v2) == frozenset({v1})

    def test_strict_down_v3(self):
        store, pid, (v1, v2, v3) = _chain(3)
        assert metrics.strict_down(store, pid, v3) == frozenset({v1, v2})

    def test_does_not_include_self(self):
        store, pid, (v1, v2) = _chain(2)
        assert v1 not in metrics.strict_down(store, pid, v1)
        assert v2 not in metrics.strict_down(store, pid, v2)


class TestAnc:
    def test_anc_includes_self(self):
        store, pid, (v1, v2) = _chain(2)
        assert v1 in metrics.anc(store, pid, v1)
        assert v2 in metrics.anc(store, pid, v2)

    def test_anc_v3_full_set(self):
        store, pid, (v1, v2, v3) = _chain(3)
        assert metrics.anc(store, pid, v3) == frozenset({v1, v2, v3})

    def test_anc_v1_singleton(self):
        store, pid, (v1, v2) = _chain(2)
        assert metrics.anc(store, pid, v1) == frozenset({v1})


# ── ω ────────────────────────────────────────────────────────────────────────

class TestOmega:
    def test_chain_v2_v3(self):
        # ω(v2,v3) = |↓v2 ∩ ↓v3| = |{v1} ∩ {v1,v2}| = 1  [§11.1b]
        store, pid, (v1, v2, v3) = _chain(3)
        assert metrics.omega(store, pid, v2, v3) == 1

    def test_antichain_omega_zero(self):
        store, pid, a, b = _antichain()
        assert metrics.omega(store, pid, a, b) == 0

    def test_omega_root_zero(self):
        # v1 has no predecessors — ↓v1 = ∅
        store, pid, (v1, v2) = _chain(2)
        assert metrics.omega(store, pid, v1, v2) == 0

    def test_omega_uses_strict_not_anc(self):
        # ω(v1,v1) = |↓v1 ∩ ↓v1| = |∅| = 0 (↓ excludes self)
        store, pid, (v1, _) = _chain(2)
        assert metrics.omega(store, pid, v1, v1) == 0


# ── d_J ──────────────────────────────────────────────────────────────────────

class TestDJ:
    def test_canonical_chain_values(self):
        # Math Ext Ex 12.1.6: d_J(v1,vk) = (k-1)/k for pure chain
        store, pid, (v1, v2, v3, v4) = _chain(4)
        assert metrics.d_J(store, pid, v1, v2) == pytest.approx(1 / 2)
        assert metrics.d_J(store, pid, v1, v3) == pytest.approx(2 / 3)
        assert metrics.d_J(store, pid, v1, v4) == pytest.approx(3 / 4)

    def test_self_distance_zero(self):
        store, pid, (v1, v2) = _chain(2)
        assert metrics.d_J(store, pid, v1, v1) == pytest.approx(0.0)
        assert metrics.d_J(store, pid, v2, v2) == pytest.approx(0.0)

    def test_symmetry(self):
        store, pid, (v1, v2, v3) = _chain(3)
        assert metrics.d_J(store, pid, v1, v3) == pytest.approx(metrics.d_J(store, pid, v3, v1))
        assert metrics.d_J(store, pid, v2, v3) == pytest.approx(metrics.d_J(store, pid, v3, v2))

    def test_antichain_distance_one(self):
        # anc(a)={a}, anc(b)={b} → d_J = |{a}△{b}| / |{a}∪{b}| = 2/2 = 1
        store, pid, a, b = _antichain()
        assert metrics.d_J(store, pid, a, b) == pytest.approx(1.0)

    def test_adjacent_chain_closed_form(self):
        # u≺v: d_J = 1 - |anc(u)|/|anc(v)|
        # d_J(v2,v3): anc(v2)={v1,v2} size 2, anc(v3)={v1,v2,v3} size 3 → 1-2/3=1/3
        store, pid, (v1, v2, v3) = _chain(3)
        assert metrics.d_J(store, pid, v2, v3) == pytest.approx(1 / 3)
