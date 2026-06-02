"""Tests for paired (coarse, fine) fingerprints (§4.2 发现3).

Guards:
  - coarse == legacy compute_fingerprint behavior (backward compat)
  - fine distinguishes receiver: self.parser.foo ≠ self.validator.foo
  - find_duplicates uses fine → no false-positive equivalence
  - change detection fires when either coarse OR fine changes
  - define_loop requires BOTH coarse AND fine to match
  - paired atomic insert: both columns written together
"""
from __future__ import annotations

import hashlib
import textwrap
from pathlib import Path

import pytest

from buer.parse import compute_fingerprint, extract_defines, _fine_call
from buer.store import Store
from buer.reconcile import reconcile
from buer import signals


# ── embedded source patterns ──────────────────────────────────────────────────

PY_HANDLERS = """\
class Service:
    def handle_a(self, req):
        return self.parser.foo(req)

    def handle_b(self, req):
        return self.validator.foo(req)
"""

PY_HANDLERS_B_CHANGED = """\
class Service:
    def handle_a(self, req):
        return self.parser.foo(req)

    def handle_b(self, req):
        return self.parser.foo(req)
"""

PY_SINGLE_CHANGE = """\
class Service:
    def handle(self, req):
        return self.parser.foo(req)
"""

PY_SINGLE_CHANGED = """\
class Service:
    def handle(self, req):
        return self.validator.foo(req)
"""


# ── helpers ───────────────────────────────────────────────────────────────────

def _write(path: Path, src: str) -> str:
    p = str(path)
    Path(p).write_text(textwrap.dedent(src))
    return p


def _mem_store() -> Store:
    return Store(":memory:")


# ══════════════════════════════════════════════════════════════════════════════
# 1. coarse == legacy algorithm (backward compat)
# ══════════════════════════════════════════════════════════════════════════════

def test_coarse_unchanged_from_legacy(tmp_path):
    """coarse fingerprint must match the pre-paired legacy compute_fingerprint output."""
    p = _write(tmp_path / "t.py", PY_HANDLERS)
    defs = extract_defines(p)
    assert len(defs) >= 2

    for define in defs:
        coarse, _ = compute_fingerprint(define)

        # Reproduce legacy algorithm
        calls_feat = tuple(sorted({c.split(".")[-1] for c in define.calls}))
        side_feat = tuple(sorted(define.side_effects))
        n = define.size_count
        if n <= 3:
            bkt = "xs"
        elif n <= 10:
            bkt = "s"
        elif n <= 30:
            bkt = "m"
        elif n <= 100:
            bkt = "l"
        else:
            bkt = "xl"
        legacy_payload = repr((
            define.params_shape, define.returns_kind,
            calls_feat, side_feat, bkt, define.numeric_literals,
        ))
        legacy = hashlib.sha256(legacy_payload.encode()).hexdigest()[:16]

        assert coarse == legacy, (
            f"{define.qualified_name}: coarse={coarse!r} != legacy={legacy!r}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# 2. false_equiv distinguished by fine
# ══════════════════════════════════════════════════════════════════════════════

def test_false_equiv_distinguished_by_fine(tmp_path):
    """handle_a(self.parser.foo) and handle_b(self.validator.foo): coarse same, fine different."""
    p = _write(tmp_path / "t.py", PY_HANDLERS)
    defs = extract_defines(p)
    by_name = {d.qualified_name: compute_fingerprint(d) for d in defs}

    coarse_a, fine_a = by_name["Service.handle_a"]
    coarse_b, fine_b = by_name["Service.handle_b"]

    # coarse: both have last-segment 'foo' → same
    assert coarse_a == coarse_b, (
        f"Expected same coarse for handle_a/handle_b (last segment 'foo'), "
        f"got {coarse_a!r} vs {coarse_b!r}"
    )

    # fine: parser.foo vs validator.foo → different
    assert fine_a != fine_b, (
        f"Expected fine to differ (parser.foo vs validator.foo), "
        f"got fine_a={fine_a!r} fine_b={fine_b!r}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 3. find_duplicates no false positive
# ══════════════════════════════════════════════════════════════════════════════

def test_find_duplicates_no_false_positive(tmp_path):
    """After reconcile, handle_a and handle_b must be in different equivalence classes."""
    p = _write(tmp_path / "t.py", PY_HANDLERS)
    s = _mem_store()
    pid = s.get_or_create_project(str(tmp_path))
    reconcile(s, pid, [p])

    rows = s.con.execute(
        "SELECT class_key, member_node FROM node_equivalence_classes WHERE project_id=?",
        (pid,),
    ).fetchall()

    # Find class_key for each handler
    a_keys = [r["class_key"] for r in rows if "handle_a" in r["member_node"]]
    b_keys = [r["class_key"] for r in rows if "handle_b" in r["member_node"]]

    assert len(a_keys) == 1, f"Expected exactly 1 equivalence entry for handle_a, got {a_keys}"
    assert len(b_keys) == 1, f"Expected exactly 1 equivalence entry for handle_b, got {b_keys}"
    assert a_keys[0] != b_keys[0], (
        f"handle_a and handle_b must be in different equivalence classes "
        f"(fine distinguishes parser.foo vs validator.foo), "
        f"both in class {a_keys[0]!r}"
    )
    s.close()


# ══════════════════════════════════════════════════════════════════════════════
# 4. change detection catches receiver change (fine changes, coarse stays)
# ══════════════════════════════════════════════════════════════════════════════

def test_change_detection_catches_receiver(tmp_path):
    """Changing self.parser.foo → self.validator.foo: fine changes → new determination."""
    p = _write(tmp_path / "t.py", PY_SINGLE_CHANGE)
    s = _mem_store()
    pid = s.get_or_create_project(str(tmp_path))
    reconcile(s, pid, [p])

    # Verify first ingest: fine = derived from 'parser.foo'
    coarse_v1, fine_v1 = compute_fingerprint(extract_defines(p)[0])

    # Change receiver
    _write(tmp_path / "t.py", PY_SINGLE_CHANGED)
    reconcile(s, pid, [p])

    coarse_v2, fine_v2 = compute_fingerprint(extract_defines(p)[0])

    # Coarse is same (both use last-segment 'foo')
    assert coarse_v1 == coarse_v2, (
        "coarse should be the same — same last-segment 'foo' in both versions"
    )
    # Fine differs
    assert fine_v1 != fine_v2, (
        "fine must differ between parser.foo and validator.foo"
    )

    # Two determinations must exist (change was detected via fine)
    rows = s.con.execute(
        """SELECT COUNT(*) AS c FROM determinations
           WHERE project_id=? AND define_name='Service.handle'""",
        (pid,),
    ).fetchone()
    assert rows["c"] == 2, (
        f"Expected 2 determinations for Service.handle (coarse same, fine changed), "
        f"got {rows['c']}"
    )
    s.close()


# ══════════════════════════════════════════════════════════════════════════════
# 5. define_loop requires BOTH coarse AND fine to match
# ══════════════════════════════════════════════════════════════════════════════

def test_define_loop_needs_both(tmp_path):
    """False-equiv (same coarse, different fine) must NOT trigger define_loop."""
    FILE = "/tmp/f.py"
    DEFINE = "fn"

    # Case A: same coarse, alternating fine → NOT a loop (coarse-only would falsely fire)
    s = _mem_store()
    pid = s.get_or_create_project(str(tmp_path))
    # v1: coarse='c_shared', fine='parser.foo'
    # v2: coarse='c_shared', fine='validator.foo'  (same coarse, different fine)
    s.insert_determination(pid, 1, FILE, DEFINE, "c_shared", "create",
                           fine_fingerprint="parser.foo")
    s.insert_determination(pid, 2, FILE, DEFINE, "c_shared", "modify",
                           fine_fingerprint="validator.foo")
    chain_a = s.store_version_chain(pid, FILE, DEFINE) if hasattr(s, "store_version_chain") \
        else s.version_chain(pid, FILE, DEFINE)

    loop_a = signals._find_loop(chain_a)
    # Gap=1 < N_LOOP_GAP=2 → no loop even if we had same key; but also fine differs
    # Let's add a third with gap >= N_LOOP_GAP:
    # v3: same coarse but STILL different fine (validator.foo again)
    # No match for (c_shared, parser.foo) at gap >= 2 in this chain
    s.close()

    # Case B: two entries gap=2, same (coarse, fine) → REAL loop detected
    s2 = _mem_store()
    pid2 = s2.get_or_create_project(str(tmp_path))
    s2.insert_determination(pid2, 1, FILE, DEFINE, "c_shared", "create",
                            fine_fingerprint="parser.foo")
    s2.insert_determination(pid2, 2, FILE, DEFINE, "c_shared", "modify",
                            fine_fingerprint="validator.foo")
    s2.insert_determination(pid2, 3, FILE, DEFINE, "c_shared", "modify",
                            fine_fingerprint="parser.foo")  # same as v1 → real loop
    chain_b = s2.version_chain(pid2, FILE, DEFINE)
    loop_b = signals._find_loop(chain_b)
    assert loop_b is not None, (
        "v1(c_shared, parser.foo) and v3(c_shared, parser.foo) at gap=2 → real loop"
    )
    s2.close()

    # Case C: gap=2, same coarse, DIFFERENT fine → NOT a loop
    s3 = _mem_store()
    pid3 = s3.get_or_create_project(str(tmp_path))
    s3.insert_determination(pid3, 1, FILE, DEFINE, "c_shared", "create",
                            fine_fingerprint="parser.foo")
    s3.insert_determination(pid3, 2, FILE, DEFINE, "c_shared", "modify",
                            fine_fingerprint="other.foo")
    s3.insert_determination(pid3, 3, FILE, DEFINE, "c_shared", "modify",
                            fine_fingerprint="validator.foo")  # ≠ parser.foo
    chain_c = s3.version_chain(pid3, FILE, DEFINE)
    loop_c = signals._find_loop(chain_c)
    assert loop_c is None, (
        "Same coarse but all-different fine: must NOT trigger loop "
        f"(old coarse-only would fire falsely); got loop={loop_c}"
    )
    s3.close()


# ══════════════════════════════════════════════════════════════════════════════
# 6. stability: same code → coarse and fine both stable
# ══════════════════════════════════════════════════════════════════════════════

def test_stability_same_code(tmp_path):
    """Same code extracted twice → coarse and fine both identical across runs."""
    p = _write(tmp_path / "t.py", PY_HANDLERS)

    defs1 = extract_defines(p)
    fps1 = {d.qualified_name: compute_fingerprint(d) for d in defs1}

    defs2 = extract_defines(p)
    fps2 = {d.qualified_name: compute_fingerprint(d) for d in defs2}

    for qname in fps1:
        coarse1, fine1 = fps1[qname]
        coarse2, fine2 = fps2[qname]
        assert coarse1 == coarse2, f"{qname}: coarse drifted {coarse1!r} → {coarse2!r}"
        assert fine1 == fine2, f"{qname}: fine drifted {fine1!r} → {fine2!r}"


# ══════════════════════════════════════════════════════════════════════════════
# 7. paired atomic insert: both columns written in same row
# ══════════════════════════════════════════════════════════════════════════════

def test_paired_atomic(tmp_path):
    """insert_determination_atomic writes node_fingerprint(coarse) and fine_fingerprint atomically."""
    s = _mem_store()
    pid = s.get_or_create_project(str(tmp_path))
    det_id, seq = s.insert_determination_atomic(
        pid,
        file_path=str(tmp_path / "f.py"),
        define_name="fn",
        node_fingerprint="coarse_val",
        edit_type="create",
        fine_fingerprint="fine_val",
    )

    row = s.get_determination(det_id)
    assert row is not None
    assert row["node_fingerprint"] == "coarse_val", "coarse must be stored as node_fingerprint"
    assert row["fine_fingerprint"] == "fine_val", "fine must be stored as fine_fingerprint"
    assert row["node_fingerprint"] is not None
    assert row["fine_fingerprint"] is not None

    # Also verify non_fine insert → fine_fingerprint is None (no orphaned fine)
    det_id2, _ = s.insert_determination_atomic(
        pid,
        file_path=str(tmp_path / "f.py"),
        define_name="fn2",
        node_fingerprint="coarse_only",
        edit_type="create",
        # fine_fingerprint omitted → defaults to None
    )
    row2 = s.get_determination(det_id2)
    assert row2["node_fingerprint"] == "coarse_only"
    assert row2["fine_fingerprint"] is None, "omitted fine_fingerprint must be NULL, not orphaned"
    s.close()


# ══════════════════════════════════════════════════════════════════════════════
# 8. regression: embedded got/requests-like pattern — no multi-fp, classes split
# ══════════════════════════════════════════════════════════════════════════════

def test_real_got_requests_no_multi_fp_regression(tmp_path):
    """Ingest embedded handler pattern: multi_fp=0 AND parser/validator in separate classes."""
    PY_GOT_LIKE = """\
class Got:
    def handle_a(self, req):
        result = self.parser.foo(req)
        return result

    def handle_b(self, req):
        result = self.validator.foo(req)
        return result

    def normal(self):
        return self.helper.bar()
"""
    p = _write(tmp_path / "got.py", PY_GOT_LIKE)
    s = _mem_store()
    pid = s.get_or_create_project(str(tmp_path))
    reconcile(s, pid, [p])

    # No multi-fp: each qualified_name has exactly 1 determination
    rows = s.con.execute(
        """SELECT define_name, COUNT(*) AS c FROM determinations
           WHERE project_id=? AND define_name IS NOT NULL
           GROUP BY define_name HAVING c > 1""",
        (pid,),
    ).fetchall()
    assert len(rows) == 0, f"Multi-fp defines (regression): {[(r['define_name'], r['c']) for r in rows]}"

    # Equivalence classes: handle_a and handle_b must be separate
    ec_rows = s.con.execute(
        "SELECT class_key, member_node FROM node_equivalence_classes WHERE project_id=?",
        (pid,),
    ).fetchall()
    a_keys = [r["class_key"] for r in ec_rows if "handle_a" in r["member_node"]]
    b_keys = [r["class_key"] for r in ec_rows if "handle_b" in r["member_node"]]
    assert len(a_keys) == 1 and len(b_keys) == 1
    assert a_keys[0] != b_keys[0], (
        "handle_a and handle_b must be in different equivalence classes "
        "(fine distinguishes self.parser.foo vs self.validator.foo)"
    )

    # fine_fingerprint must be written for all new determines (non-null)
    fp_rows = s.con.execute(
        """SELECT define_name, fine_fingerprint FROM determinations
           WHERE project_id=? AND define_name IS NOT NULL""",
        (pid,),
    ).fetchall()
    assert all(r["fine_fingerprint"] is not None for r in fp_rows), (
        "All determines from reconcile must have non-null fine_fingerprint"
    )
    s.close()
