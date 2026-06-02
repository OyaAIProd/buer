"""Guard tests for numeric_literals fingerprint feature (§4.2 feature 6).

Prevents silent regression of pure-numeric edit blindness: if the extraction
or fingerprint inclusion is removed, these tests fail before reconcile silently
stops tracking numeric changes.
"""
import os
import textwrap

import pytest

from buer.parse import compute_fingerprint, extract_defines
from buer.store import Store


# ── helpers ───────────────────────────────────────────────────────────────────

def _write(path: str, src: str) -> str:
    with open(path, "w") as f:
        f.write(textwrap.dedent(src))
    return path


def _mem_store() -> Store:
    return Store(":memory:")


# ── 1. Python pure-numeric change alters fingerprint ─────────────────────────

class TestPythonNumericFingerprint:
    def test_pure_numeric_change_changes_fingerprint(self, tmp_path):
        p = str(tmp_path / "t.py")
        _write(p, "def foo(x):\n    return clamp(x, 0, 100)\n")
        a = extract_defines(p)[0]
        fpa = compute_fingerprint(a)

        _write(p, "def foo(x):\n    return clamp(x, 0, 1)\n")
        b = extract_defines(p)[0]
        fpb = compute_fingerprint(b)

        assert a.numeric_literals == ("0", "100")
        assert b.numeric_literals == ("0", "1")
        assert fpa != fpb

    def test_identical_code_same_fingerprint(self, tmp_path):
        p = str(tmp_path / "t.py")
        src = "def foo(x):\n    return clamp(x, 0, 100)\n"
        _write(p, src)
        a = extract_defines(p)[0]
        _write(p, src)
        b = extract_defines(p)[0]
        assert compute_fingerprint(a) == compute_fingerprint(b)


# ── 2. Python extraction correctness (order preserved, no dedup/sort) ─────────

class TestPythonNumericExtraction:
    def test_order_and_completeness(self, tmp_path):
        p = str(tmp_path / "t.py")
        _write(p, """\
            def f():
                a = 30
                b = 0.5
                return clamp(a, 0, 100)
        """)
        d = extract_defines(p)[0]
        assert d.numeric_literals == ("30", "0.5", "0", "100")

    def test_no_dedup(self, tmp_path):
        p = str(tmp_path / "t.py")
        _write(p, "def f():\n    return x + 1 + 1\n")
        d = extract_defines(p)[0]
        assert d.numeric_literals == ("1", "1")

    def test_empty_body_no_numerics(self, tmp_path):
        p = str(tmp_path / "t.py")
        _write(p, "def f():\n    pass\n")
        d = extract_defines(p)[0]
        assert d.numeric_literals == ()


# ── 3. JS pure-numeric change alters fingerprint ─────────────────────────────

class TestJsNumericFingerprint:
    def test_pure_numeric_change_changes_fingerprint(self, tmp_path):
        p = str(tmp_path / "t.js")
        _write(p, "function foo(x) { return clamp(x, 0, 100); }\n")
        a = extract_defines(p)[0]
        fpa = compute_fingerprint(a)

        _write(p, "function foo(x) { return clamp(x, 0, 1); }\n")
        b = extract_defines(p)[0]
        fpb = compute_fingerprint(b)

        assert a.numeric_literals == ("0", "100")
        assert b.numeric_literals == ("0", "1")
        assert fpa != fpb


# ── 4. Order-sensitive: value swap is visible ─────────────────────────────────

class TestNumericOrder:
    def test_swap_changes_fingerprint(self, tmp_path):
        p = str(tmp_path / "t.py")
        _write(p, "def foo():\n    return f(1, 2)\n")
        a = extract_defines(p)[0]

        _write(p, "def foo():\n    return f(2, 1)\n")
        b = extract_defines(p)[0]

        assert a.numeric_literals == ("1", "2")
        assert b.numeric_literals == ("2", "1")
        assert compute_fingerprint(a) != compute_fingerprint(b)


# ── 5. End-to-end: reconcile no longer blind to pure-numeric edits ────────────

class TestReconcileNumericBlindness:
    def test_pure_numeric_edit_produces_new_determination(self, tmp_path):
        from buer.reconcile import reconcile

        store = _mem_store()
        root = str(tmp_path)
        pid = store.get_or_create_project(root)
        p = str(tmp_path / "f.py")

        # Version A: foo returns x + 1
        _write(p, "def foo(x):\n    return x + 1\n")
        reconcile(store, pid, [p])

        dets_after_v1 = store.version_chain(pid, p, "foo")
        assert len(dets_after_v1) == 1

        # Version B: pure numeric change (1 → 100) — must produce a new determination
        _write(p, "def foo(x):\n    return x + 100\n")
        reconcile(store, pid, [p])

        dets_after_v2 = store.version_chain(pid, p, "foo")
        assert len(dets_after_v2) == 2, (
            "Pure numeric change must produce a new determination; "
            "got same count — numeric_literals not in fingerprint?"
        )

    def test_non_numeric_non_structural_change_still_invisible(self, tmp_path):
        """Sanity: renaming a local variable with same structure stays same fingerprint."""
        from buer.reconcile import reconcile

        store = _mem_store()
        root = str(tmp_path)
        pid = store.get_or_create_project(root)
        p = str(tmp_path / "f.py")

        _write(p, "def foo(x):\n    result = x\n    return result\n")
        reconcile(store, pid, [p])

        _write(p, "def foo(x):\n    val = x\n    return val\n")
        reconcile(store, pid, [p])

        # Same structure, same numerics (none) → should still be 1 determination
        dets = store.version_chain(pid, p, "foo")
        assert len(dets) == 1


# ── 6. Does not descend into nested defs ─────────────────────────────────────

class TestNumericNoNestedDescent:
    def test_nested_def_numerics_excluded(self, tmp_path):
        p = str(tmp_path / "t.py")
        _write(p, """\
            def foo():
                x = 5
                def inner():
                    y = 999
                return x
        """)
        d = next(d for d in extract_defines(p) if d.name == "foo")
        assert "5" in d.numeric_literals
        assert "999" not in d.numeric_literals
