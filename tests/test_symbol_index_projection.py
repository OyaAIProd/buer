"""Tests for callgraph.build_symbol_index_from_store projection equivalence.

Asserts that the store-based projection (𝒢_D frontier) produces a SymbolIndex
that is field-equivalent to the rglob-based build_symbol_index for the three
fields used by edge-building: qualified, loc, and simple.

Spec:
  qualified  — exact equality
  loc        — exact equality
  simple     — key-set equal; per key, fqn-set equal (order irrelevant)
  modules    — projection ⊆ rglob (rglob picks up define-free files; projection doesn't)
"""
from __future__ import annotations

import pytest

from buer import callgraph, reconcile
from buer.store import Store


# ── helpers ────────────────────────────────────────────────────────────────────

def _make_store(tmp_path):
    store = Store(":memory:")
    root = str(tmp_path)
    pid = store.get_or_create_project(root)
    return store, pid, root


def _assert_idx_equivalent(idx_proj, idx_rglob):
    """Three-field equivalence check (qualified / loc / simple)."""
    assert idx_proj.qualified == idx_rglob.qualified, (
        f"qualified mismatch:\n"
        f"  only in proj : {idx_proj.qualified - idx_rglob.qualified}\n"
        f"  only in rglob: {idx_rglob.qualified - idx_proj.qualified}"
    )
    assert idx_proj.loc == idx_rglob.loc, (
        f"loc mismatch:\n"
        f"  only in proj : {set(idx_proj.loc) - set(idx_rglob.loc)}\n"
        f"  only in rglob: {set(idx_rglob.loc) - set(idx_proj.loc)}"
    )
    assert set(idx_proj.simple.keys()) == set(idx_rglob.simple.keys()), (
        f"simple key mismatch:\n"
        f"  only in proj : {set(idx_proj.simple) - set(idx_rglob.simple)}\n"
        f"  only in rglob: {set(idx_rglob.simple) - set(idx_proj.simple)}"
    )
    for k in idx_rglob.simple:
        assert set(idx_proj.simple[k]) == set(idx_rglob.simple[k]), (
            f"simple[{k!r}] fqn-set mismatch: "
            f"proj={set(idx_proj.simple[k])!r} rglob={set(idx_rglob.simple[k])!r}"
        )
    # modules: projection is a subset (rglob includes define-free files)
    assert idx_proj.modules <= idx_rglob.modules, (
        f"projection has extra modules not in rglob: "
        f"{idx_proj.modules - idx_rglob.modules}"
    )


# ── fixtures ───────────────────────────────────────────────────────────────────

def _write_initial_files(tmp_path):
    """Write a small multi-file Python project with cross-file calls."""
    utils_py = tmp_path / "utils.py"
    utils_py.write_text(
        "def parse_line(s):\n"
        "    return s.strip()\n\n"
        "def validate(s):\n"
        "    return bool(parse_line(s))\n"
    )
    main_py = tmp_path / "main.py"
    main_py.write_text(
        "from utils import parse_line, validate\n\n"
        "def run(lines):\n"
        "    return [parse_line(l) for l in lines if validate(l)]\n\n"
        "class Pipeline:\n"
        "    def execute(self, data):\n"
        "        return run(data)\n"
    )
    helper_py = tmp_path / "helper.py"
    helper_py.write_text(
        "class Formatter:\n"
        "    def format(self, v):\n"
        "        return str(v)\n"
    )
    return [str(utils_py), str(main_py), str(helper_py)]


# ── Static equivalence ─────────────────────────────────────────────────────────

class TestProjectionEqualsRglob:
    def test_qualified_exact_match(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        files = _write_initial_files(tmp_path)
        reconcile.reconcile(store, pid, files)

        idx_proj = callgraph.build_symbol_index_from_store(store, pid, root)
        idx_rglob = callgraph.build_symbol_index(root)

        assert idx_proj.qualified == idx_rglob.qualified

    def test_loc_exact_match(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        files = _write_initial_files(tmp_path)
        reconcile.reconcile(store, pid, files)

        idx_proj = callgraph.build_symbol_index_from_store(store, pid, root)
        idx_rglob = callgraph.build_symbol_index(root)

        assert idx_proj.loc == idx_rglob.loc

    def test_simple_keys_match(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        files = _write_initial_files(tmp_path)
        reconcile.reconcile(store, pid, files)

        idx_proj = callgraph.build_symbol_index_from_store(store, pid, root)
        idx_rglob = callgraph.build_symbol_index(root)

        assert set(idx_proj.simple.keys()) == set(idx_rglob.simple.keys())

    def test_simple_fqn_sets_match(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        files = _write_initial_files(tmp_path)
        reconcile.reconcile(store, pid, files)

        idx_proj = callgraph.build_symbol_index_from_store(store, pid, root)
        idx_rglob = callgraph.build_symbol_index(root)

        for k in idx_rglob.simple:
            assert set(idx_proj.simple[k]) == set(idx_rglob.simple[k]), (
                f"simple[{k!r}] mismatch"
            )

    def test_modules_projection_is_subset_of_rglob(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        files = _write_initial_files(tmp_path)
        reconcile.reconcile(store, pid, files)

        idx_proj = callgraph.build_symbol_index_from_store(store, pid, root)
        idx_rglob = callgraph.build_symbol_index(root)

        assert idx_proj.modules <= idx_rglob.modules

    def test_all_three_fields_equivalent(self, tmp_path):
        """Composite assertion — one test that covers the full equivalence spec."""
        store, pid, root = _make_store(tmp_path)
        files = _write_initial_files(tmp_path)
        reconcile.reconcile(store, pid, files)

        idx_proj = callgraph.build_symbol_index_from_store(store, pid, root)
        idx_rglob = callgraph.build_symbol_index(root)

        _assert_idx_equivalent(idx_proj, idx_rglob)

    def test_class_methods_indexed_correctly(self, tmp_path):
        """Class method FQNs (e.g. Pipeline.execute) appear in both indexes."""
        store, pid, root = _make_store(tmp_path)
        files = _write_initial_files(tmp_path)
        reconcile.reconcile(store, pid, files)

        idx_proj = callgraph.build_symbol_index_from_store(store, pid, root)
        idx_rglob = callgraph.build_symbol_index(root)

        # Formatter.format and Pipeline.execute are class methods
        for short in ("format", "execute"):
            assert short in idx_proj.simple
            assert set(idx_proj.simple[short]) == set(idx_rglob.simple[short])

    def test_empty_project_returns_empty_index(self, tmp_path):
        store, pid, root = _make_store(tmp_path)
        # No files reconciled → no determinations → empty projection
        idx_proj = callgraph.build_symbol_index_from_store(store, pid, root)

        assert idx_proj.qualified == set()
        assert idx_proj.loc == {}
        assert idx_proj.simple == {}
        assert idx_proj.modules == set()


# ── Incremental equivalence (delete + modify path) ────────────────────────────

class TestProjectionAfterIncrementalChanges:
    def test_projection_correct_after_function_added(self, tmp_path):
        """Add a new function → projection picks it up, matches rglob."""
        store, pid, root = _make_store(tmp_path)
        files = _write_initial_files(tmp_path)
        reconcile.reconcile(store, pid, files)

        # Add a new function to utils.py
        utils_py = tmp_path / "utils.py"
        utils_py.write_text(
            utils_py.read_text() + "\ndef sanitize(s):\n    return s.replace('<', '')\n"
        )
        reconcile.reconcile(store, pid, [str(utils_py)])

        idx_proj = callgraph.build_symbol_index_from_store(store, pid, root)
        idx_rglob = callgraph.build_symbol_index(root)

        _assert_idx_equivalent(idx_proj, idx_rglob)

    def test_projection_correct_after_function_deleted(self, tmp_path):
        """Delete a function → projection drops it, matches rglob."""
        store, pid, root = _make_store(tmp_path)
        files = _write_initial_files(tmp_path)
        reconcile.reconcile(store, pid, files)

        # Remove validate() from utils.py
        utils_py = tmp_path / "utils.py"
        utils_py.write_text(
            "def parse_line(s):\n"
            "    return s.strip()\n"
        )
        reconcile.reconcile(store, pid, [str(utils_py)])

        idx_proj = callgraph.build_symbol_index_from_store(store, pid, root)
        idx_rglob = callgraph.build_symbol_index(root)

        _assert_idx_equivalent(idx_proj, idx_rglob)
        # validate is gone from both
        assert "validate" not in idx_proj.simple
        assert "validate" not in idx_rglob.simple

    def test_projection_correct_after_function_renamed(self, tmp_path):
        """Rename a function → old name gone, new name present in both."""
        store, pid, root = _make_store(tmp_path)
        files = _write_initial_files(tmp_path)
        reconcile.reconcile(store, pid, files)

        utils_py = tmp_path / "utils.py"
        utils_py.write_text(
            "def tokenize(s):\n"        # renamed from parse_line
            "    return s.strip()\n\n"
            "def validate(s):\n"
            "    return bool(tokenize(s))\n"
        )
        reconcile.reconcile(store, pid, [str(utils_py)])

        idx_proj = callgraph.build_symbol_index_from_store(store, pid, root)
        idx_rglob = callgraph.build_symbol_index(root)

        _assert_idx_equivalent(idx_proj, idx_rglob)
        assert "parse_line" not in idx_proj.simple
        assert "tokenize" in idx_proj.simple

    def test_deleted_define_not_in_projection(self, tmp_path):
        """Deleted define must not linger in projection (alive-filter correctness)."""
        store, pid, root = _make_store(tmp_path)
        files = _write_initial_files(tmp_path)
        reconcile.reconcile(store, pid, files)

        helper_py = tmp_path / "helper.py"
        # Wipe helper.py entirely — Formatter and Formatter.format should vanish
        helper_py.write_text("")
        reconcile.reconcile(store, pid, [str(helper_py)])

        idx_proj = callgraph.build_symbol_index_from_store(store, pid, root)

        # Stale defines must not survive in projection
        helper_mod = callgraph.module_name_of(str(helper_py), root)
        for dead_fqn in (f"{helper_mod}.Formatter", f"{helper_mod}.Formatter.format"):
            assert dead_fqn not in idx_proj.qualified, (
                f"Deleted define {dead_fqn!r} still in projection"
            )

    def test_multi_round_convergence(self, tmp_path):
        """Three edit rounds — projection matches rglob after each round."""
        store, pid, root = _make_store(tmp_path)
        files = _write_initial_files(tmp_path)
        reconcile.reconcile(store, pid, files)

        utils_py = tmp_path / "utils.py"

        # Round 2: add a function
        utils_py.write_text(
            utils_py.read_text() + "\ndef extra():\n    pass\n"
        )
        reconcile.reconcile(store, pid, [str(utils_py)])
        _assert_idx_equivalent(
            callgraph.build_symbol_index_from_store(store, pid, root),
            callgraph.build_symbol_index(root),
        )

        # Round 3: remove that function
        utils_py.write_text(
            "def parse_line(s):\n    return s.strip()\n\n"
            "def validate(s):\n    return bool(parse_line(s))\n"
        )
        reconcile.reconcile(store, pid, [str(utils_py)])
        _assert_idx_equivalent(
            callgraph.build_symbol_index_from_store(store, pid, root),
            callgraph.build_symbol_index(root),
        )
