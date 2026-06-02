"""Tests for language-consistency filter in resolve_callee."""
import os, tempfile, textwrap, sqlite3
import pytest

from buer.callgraph import (
    SymbolIndex, resolve_callee, _lang_of_file, _candidate_lang,
    build_symbol_index, compute_call_edges,
)
from buer.store import Store
from buer.reconcile import reconcile


# ── helpers ───────────────────────────────────────────────────────────────────

def _idx_with(entries):
    """Build a SymbolIndex from a list of (fqn, file_path) tuples."""
    idx = SymbolIndex()
    for fqn, fp in entries:
        idx.qualified.add(fqn)
        name = fqn.rsplit(".", 1)[-1]
        idx.simple.setdefault(name, []).append(fqn)
        idx.loc[fqn] = (fp, fqn.split(".", 1)[-1] if "." in fqn else fqn)
    return idx


# ── unit: _lang_of_file ───────────────────────────────────────────────────────

def test_lang_of_file_py():
    assert _lang_of_file("/proj/utils.py") == "py"

def test_lang_of_file_ts():
    assert _lang_of_file("/proj/src/auth.ts") == "js_ts"

def test_lang_of_file_tsx():
    assert _lang_of_file("/proj/components/Foo.tsx") == "js_ts"

def test_lang_of_file_other():
    assert _lang_of_file("/proj/README.md") == "other"


# ── 1. cross-language simple match rejected ───────────────────────────────────

def test_cross_lang_simple_match_rejected():
    # TS caller, unique Python 'foo' in index → must return None (false edge)
    idx = _idx_with([("pkg.foo", "/proj/pkg/utils.py")])
    result = resolve_callee("foo", {}, idx, "src/auth", None, caller_lang="js_ts")
    assert result is None, "cross-language simple match must be rejected"


# ── 2. same-language simple match accepted ────────────────────────────────────

def test_same_lang_simple_match_accepted():
    # TS caller, unique TS 'foo' in index → must resolve normally
    idx = _idx_with([("src/utils.foo", "/proj/src/utils.ts")])
    result = resolve_callee("foo", {}, idx, "src/auth", None, caller_lang="js_ts")
    assert result == "src/utils.foo"


# ── 3. cross-language import_map match rejected ───────────────────────────────

def test_cross_lang_import_match_rejected():
    # TS caller, import_map resolves to a Python symbol → None
    idx = _idx_with([("pkg.helper", "/proj/pkg/helper.py")])
    import_map = {"helper": "pkg.helper"}
    result = resolve_callee("helper", import_map, idx, "src/comp", None, caller_lang="js_ts")
    assert result is None, "cross-language import_map match must be rejected"


# ── 4. py-to-py simple match resolves normally ────────────────────────────────

def test_py_to_py_resolves():
    # Python caller, unique Python 'compute' → must resolve (filter must not break same-lang)
    idx = _idx_with([("pkg.math.compute", "/proj/pkg/math.py")])
    result = resolve_callee("compute", {}, idx, "pkg.runner", None, caller_lang="py")
    assert result == "pkg.math.compute"


# ── 4b. same-module (branch a) never filtered ────────────────────────────────

def test_same_module_not_filtered():
    # Same-module match is returned immediately before lang check — must always pass
    idx = _idx_with([("src/auth.validate", "/proj/src/auth.ts")])
    result = resolve_callee("validate", {}, idx, "src/auth", None, caller_lang="js_ts")
    assert result == "src/auth.validate"


# ── 4c. caller_lang=None skips filter (backward compat) ──────────────────────

def test_no_caller_lang_skips_filter():
    # When caller_lang not provided, cross-language match is still returned
    idx = _idx_with([("pkg.foo", "/proj/pkg/utils.py")])
    result = resolve_callee("foo", {}, idx, "src/auth", None)  # no caller_lang
    assert result == "pkg.foo", "absent caller_lang must not filter"


# ── 5. end-to-end: mixed .py + .ts project — no cross-language call edges ─────

def test_e2e_no_cross_lang_call_edges(tmp_path):
    # One Python file defining `handle`, one TS file with a function that
    # textually calls `handle` — the only `handle` in the project lives in .py.
    # After reconcile, call_edges must NOT contain a ts→py edge for `handle`.
    py_file = tmp_path / "utils.py"
    py_file.write_text(textwrap.dedent("""\
        def handle():
            pass
    """))

    ts_file = tmp_path / "app.ts"
    ts_file.write_text(textwrap.dedent("""\
        export function caller(): void {
            handle();
        }
    """))

    db_path = str(tmp_path / "test.sqlite")
    store = Store(db_path)
    pid = store.get_or_create_project(str(tmp_path))

    reconcile(store, pid, [str(py_file), str(ts_file)])

    rows = store.con.execute(
        "SELECT caller, callee FROM call_edges WHERE project_id=?", (pid,)
    ).fetchall()
    # caller FQN will contain "/" (TS), callee FQN would contain "." (Py) if false edge existed
    cross = [r for r in rows
             if "/" in r["caller"] and "/" not in r["callee"]
             or "/" not in r["caller"] and "/" in r["callee"]]
    assert cross == [], f"unexpected cross-language call edges: {cross}"
    store.close()
