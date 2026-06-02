"""Tests for operator dimension in fingerprint (§4.2 feature 7, 発見4).

Guards:
  - arithmetic/comparison/logical/augmented ops: coarse same, fine different
  - coarse unchanged from pre-operator algorithm (backward compat)
  - operators feature stable across runs
  - change detection catches operator-level edits (was blind before)
  - regression fires on operator-change bug (core 発見4 guard)
  - JS/TS operator extraction
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from buer.parse import compute_fingerprint, extract_defines
from buer.store import Store
from buer.reconcile import reconcile


# ── helpers ───────────────────────────────────────────────────────────────────

def _write(path: Path, src: str) -> str:
    p = str(path)
    Path(p).write_text(textwrap.dedent(src))
    return p


def _mem_store() -> Store:
    return Store(":memory:")


def _fp(tmp_path: Path, src: str, name: str, filename: str = "t.py"):
    """Extract defines from src, return compute_fingerprint for 'name'."""
    p = _write(tmp_path / filename, src)
    defs = {d.qualified_name: d for d in extract_defines(p)}
    assert name in defs, f"{name!r} not found in {list(defs)}"
    return compute_fingerprint(defs[name])


# ── embedded sources ──────────────────────────────────────────────────────────

PY_ADD = """
def fn(a, b):
    return a + b
"""

PY_SUB = """
def fn(a, b):
    return a - b
"""

PY_GT = """
def fn(a, b):
    return a > b
"""

PY_LT = """
def fn(a, b):
    return a < b
"""

PY_AND = """
def fn(a, b):
    return a and b
"""

PY_OR = """
def fn(a, b):
    return a or b
"""

PY_AUG_ADD = """
def fn(x):
    x += 1
    return x
"""

PY_AUG_SUB = """
def fn(x):
    x -= 1
    return x
"""

TS_ADD = """\
function fn(a: number, b: number): number { return a + b; }
"""

TS_SUB = """\
function fn(a: number, b: number): number { return a - b; }
"""


# ══════════════════════════════════════════════════════════════════════════════
# 1. Arithmetic operators distinguished (a+b vs a-b)
# ══════════════════════════════════════════════════════════════════════════════

def test_arithmetic_ops_distinguished(tmp_path):
    """a+b vs a-b: coarse same, fine different (operator dimension in fine only)."""
    coarse_add, fine_add = _fp(tmp_path, PY_ADD, "fn", "add.py")
    coarse_sub, fine_sub = _fp(tmp_path, PY_SUB, "fn", "sub.py")

    assert coarse_add == coarse_sub, (
        f"coarse must be same (operator not in coarse): {coarse_add!r} vs {coarse_sub!r}"
    )
    assert fine_add != fine_sub, (
        f"fine must differ (Add vs Sub in operator tuple): {fine_add!r} vs {fine_sub!r}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 2. Comparison operators distinguished (a>b vs a<b)
# ══════════════════════════════════════════════════════════════════════════════

def test_comparison_ops_distinguished(tmp_path):
    """a>b vs a<b: coarse same, fine different."""
    coarse_gt, fine_gt = _fp(tmp_path, PY_GT, "fn", "gt.py")
    coarse_lt, fine_lt = _fp(tmp_path, PY_LT, "fn", "lt.py")

    assert coarse_gt == coarse_lt
    assert fine_gt != fine_lt, (
        f"Gt vs Lt must differ in fine: {fine_gt!r} vs {fine_lt!r}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 3. Logical operators distinguished (and vs or)
# ══════════════════════════════════════════════════════════════════════════════

def test_logical_ops_distinguished(tmp_path):
    """a and b vs a or b: coarse same, fine different."""
    coarse_and, fine_and = _fp(tmp_path, PY_AND, "fn", "and.py")
    coarse_or, fine_or = _fp(tmp_path, PY_OR, "fn", "or.py")

    assert coarse_and == coarse_or
    assert fine_and != fine_or, (
        f"And vs Or must differ in fine: {fine_and!r} vs {fine_or!r}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 4. Augmented assignment operators distinguished (+=  vs -=)
# ══════════════════════════════════════════════════════════════════════════════

def test_aug_assign_distinguished(tmp_path):
    """x += 1 vs x -= 1: coarse same, fine different."""
    coarse_p, fine_p = _fp(tmp_path, PY_AUG_ADD, "fn", "augadd.py")
    coarse_m, fine_m = _fp(tmp_path, PY_AUG_SUB, "fn", "augsub.py")

    assert coarse_p == coarse_m
    assert fine_p != fine_m, (
        f"AugAdd vs AugSub must differ in fine: {fine_p!r} vs {fine_m!r}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 5. coarse unchanged — operators not in coarse (backward compat)
# ══════════════════════════════════════════════════════════════════════════════

def test_coarse_unchanged_by_operators(tmp_path):
    """coarse must equal the pre-operator algorithm (operators not in coarse_payload)."""
    import hashlib

    p = _write(tmp_path / "t.py", PY_ADD)
    defs = extract_defines(p)
    assert len(defs) == 1
    define = defs[0]

    coarse, _ = compute_fingerprint(define)

    # Reproduce exact coarse algorithm (no operators)
    calls_feat = tuple(sorted({c.split(".")[-1] for c in define.calls}))
    side_feat = tuple(sorted(define.side_effects))
    n = define.size_count
    if n <= 3:   bkt = "xs"
    elif n <= 10: bkt = "s"
    elif n <= 30: bkt = "m"
    elif n <= 100: bkt = "l"
    else:         bkt = "xl"
    payload = repr((
        define.params_shape, define.returns_kind,
        calls_feat, side_feat, bkt, define.numeric_literals,
    ))
    expected = hashlib.sha256(payload.encode()).hexdigest()[:16]

    assert coarse == expected, (
        f"coarse drifted from pre-operator algorithm: {coarse!r} != {expected!r}. "
        "Mutation: adding operators to coarse_payload would cause this to fail."
    )


# ══════════════════════════════════════════════════════════════════════════════
# 6. Stability: same code → operators feature stable across runs
# ══════════════════════════════════════════════════════════════════════════════

def test_operators_stable(tmp_path):
    """Same code extracted twice → operators tuple and both fingerprints identical."""
    p = _write(tmp_path / "t.py", PY_ADD)

    defs1 = extract_defines(p)
    defs2 = extract_defines(p)

    assert len(defs1) == 1 and len(defs2) == 1
    assert defs1[0].operators == defs2[0].operators
    assert compute_fingerprint(defs1[0]) == compute_fingerprint(defs2[0])


# ══════════════════════════════════════════════════════════════════════════════
# 7. Change detection catches operator change (a+b → a-b)
# ══════════════════════════════════════════════════════════════════════════════

def test_change_detection_catches_operator(tmp_path):
    """Changing a+b → a-b: reconcile must produce a new determination (fine changes)."""
    p = _write(tmp_path / "t.py", PY_ADD)
    s = _mem_store()
    pid = s.get_or_create_project(str(tmp_path))
    result1 = reconcile(s, pid, [p])

    assert len(result1.affected) == 1, "First ingest must find fn"

    # Change: + → -
    _write(tmp_path / "t.py", PY_SUB)
    result2 = reconcile(s, pid, [p])

    assert len(result2.affected) == 1, (
        "Operator change (Add→Sub) must produce a new determination. "
        "Mutation: removing operators from fine_payload causes fine to stay the same "
        "→ no new determination → affected is empty."
    )
    assert result2.affected[0][1] == "fn"


# ══════════════════════════════════════════════════════════════════════════════
# 8. regression fires on operator-change bug (核心守护 — 発見4 root cause)
# ══════════════════════════════════════════════════════════════════════════════

def test_regression_fires_on_operator_change(tmp_path):
    """Production define changes operator (a+b→a-b), test goes green→red → regression fires.

    This is the 発見4 core guard: before the fix, operators were absent from fine,
    so the operator change made affected empty, and regression requires affected to
    fire — so regression never triggered on operator-level bugs.
    """
    from buer.store import Store

    # Set up project with test infra
    s = Store(":memory:")
    pid = s.get_or_create_project(str(tmp_path))

    py_file = str(tmp_path / "calc.py")
    Path(py_file).write_text(textwrap.dedent(PY_ADD))

    # Pass 1: initial ingest (define exists, no test results yet)
    result1 = reconcile(s, pid, [py_file])
    assert len(result1.affected) == 1

    det_id_v1 = result1.affected[0][2]

    # Simulate: test passed at v1
    s.con.execute(
        """INSERT INTO test_runs (project_id, seq, source_path, passed, failed, skipped)
           VALUES (?, ?, ?, 1, 0, 0)""",
        (pid, 1, str(tmp_path / "test_calc.xml")),
    )
    run_id = s.con.execute("SELECT last_insert_rowid()").fetchone()[0]
    s.con.execute(
        """INSERT INTO test_cases (test_run_id, classname, name, file_path, status)
           VALUES (?, 'test_calc', 'test_add', ?, 'passed')""",
        (run_id, str(tmp_path / "test_calc.py")),
    )
    # Coverage: test_add covers fn
    s.con.execute(
        """INSERT INTO coverage_map (project_id, test_case, define_name)
           VALUES (?, 'test_calc::test_add', 'fn')""",
        (pid,),
    )
    s.con.commit()

    # Pass 2: operator change (+ → -), test NOW fails
    Path(py_file).write_text(textwrap.dedent(PY_SUB))
    result2 = reconcile(s, pid, [py_file])

    # Key assertion: change detection must fire (affected non-empty)
    assert len(result2.affected) == 1, (
        "Operator change Add→Sub must produce a new determination. "
        "If affected is empty, regression cannot fire (it requires affected). "
        "Mutation: removing operators from fine_payload → affected empty → this fails."
    )

    # Simulate: test now fails (after the operator change)
    s.con.execute(
        """INSERT INTO test_runs (project_id, seq, source_path, passed, failed, skipped)
           VALUES (?, ?, ?, 0, 1, 0)""",
        (pid, 2, str(tmp_path / "test_calc.xml")),
    )
    run_id2 = s.con.execute("SELECT last_insert_rowid()").fetchone()[0]
    s.con.execute(
        """INSERT INTO test_cases (test_run_id, classname, name, file_path, status)
           VALUES (?, 'test_calc', 'test_add', ?, 'failed')""",
        (run_id2, str(tmp_path / "test_calc.py")),
    )
    s.con.commit()

    det_id_v2 = result2.affected[0][2]

    # regression signal: test was passing, now failing, and define was just edited
    from buer import signals
    incidents_before = s.con.execute(
        "SELECT COUNT(*) AS c FROM incidents WHERE project_id=? AND signal='regression'",
        (pid,),
    ).fetchone()["c"]

    signals.detect_regression(s, pid, result2.affected, str(tmp_path), None)

    incidents_after = s.con.execute(
        "SELECT COUNT(*) AS c FROM incidents WHERE project_id=? AND signal='regression'",
        (pid,),
    ).fetchone()["c"]

    assert incidents_after > incidents_before, (
        "regression incident must be created when a production define with operator change "
        "(Add→Sub) causes a test to go green→red. "
        "Before 発見4 fix: affected empty → regression never fires on operator bugs."
    )

    s.close()


# ══════════════════════════════════════════════════════════════════════════════
# 9. JS/TS operator extraction: operators non-empty, fine distinguishes +/-
# ══════════════════════════════════════════════════════════════════════════════

def test_js_operator_extraction(tmp_path):
    """TS defines with operators: operators tuple non-empty, fine distinguishes + vs -."""
    add_path = _write(tmp_path / "add.ts", TS_ADD)
    sub_path = _write(tmp_path / "sub.ts", TS_SUB)

    add_defs = extract_defines(add_path)
    sub_defs = extract_defines(sub_path)

    assert len(add_defs) == 1 and len(sub_defs) == 1
    add_d = add_defs[0]
    sub_d = sub_defs[0]

    assert len(add_d.operators) > 0, f"TS add: operators must be non-empty; got {add_d.operators!r}"
    assert len(sub_d.operators) > 0, f"TS sub: operators must be non-empty; got {sub_d.operators!r}"
    assert add_d.operators != sub_d.operators, (
        f"TS add vs sub: operators must differ; add={add_d.operators!r} sub={sub_d.operators!r}"
    )

    add_c, add_f = compute_fingerprint(add_d)
    sub_c, sub_f = compute_fingerprint(sub_d)

    assert add_c == sub_c, f"coarse must be same for TS add/sub: {add_c!r} vs {sub_c!r}"
    assert add_f != sub_f, f"fine must differ for TS add/sub: {add_f!r} vs {sub_f!r}"


PY_BARE = """
def fn(a):
    return a
"""

PY_NOT = """
def fn(a):
    return not a
"""

PY_NOT_NOT = """
def fn(a):
    return not not a
"""


# ══════════════════════════════════════════════════════════════════════════════
# 10. logical not distinguished (not_operator — tree-sitter separate type)
# ══════════════════════════════════════════════════════════════════════════════

def test_logical_not_distinguished(tmp_path):
    """'return a' vs 'return not a': coarse same, fine different.

    not_operator is a distinct tree-sitter node type (not unary_operator).
    Mutation: removing the not_operator branch → operators empty → fine same → test red.
    """
    coarse_bare, fine_bare = _fp(tmp_path, PY_BARE, "fn", "bare.py")
    coarse_not, fine_not = _fp(tmp_path, PY_NOT, "fn", "not.py")

    assert coarse_bare == coarse_not, (
        f"coarse must be same (not_operator not in coarse): {coarse_bare!r} vs {coarse_not!r}"
    )
    assert fine_bare != fine_not, (
        f"fine must differ (bare vs Not): {fine_bare!r} vs {fine_not!r}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 11. double-not distinguished (condition-inversion bug guard)
# ══════════════════════════════════════════════════════════════════════════════

def test_double_not_distinguished(tmp_path):
    """'return not a' vs 'return not not a': fine different.

    not not a = condition inversion — a common semantic bug.
    operators multiset: ('Not',) vs ('Not','Not') → fine differs.
    Mutation: removing not_operator branch → both have () → fine same → test red.
    """
    coarse_not, fine_not = _fp(tmp_path, PY_NOT, "fn", "not.py")
    coarse_notnot, fine_notnot = _fp(tmp_path, PY_NOT_NOT, "fn", "notnot.py")

    assert coarse_not == coarse_notnot
    assert fine_not != fine_notnot, (
        f"single vs double not must differ in fine: {fine_not!r} vs {fine_notnot!r}"
    )
