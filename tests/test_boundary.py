"""Tests for buer/boundary.py — unified should_ingest decision."""
import os
import textwrap

import pytest

from buer.boundary import (
    EXCLUDED_DIR_NAMES,
    _nested_repo_root,
    in_nested_independent_project,
    is_excluded_dir,
    should_ingest,
)
from buer.reconcile import reconcile
from buer.store import Store


# ── 1. Blacklisted directory names are excluded ───────────────────────────────

def test_excluded_dir(tmp_path):
    root_real = os.path.realpath(str(tmp_path))

    # node_modules anywhere in path
    nm = os.path.realpath(str(tmp_path / "node_modules" / "lodash" / "index.js"))
    assert not should_ingest(nm, root_real)

    # dist/
    dist = os.path.realpath(str(tmp_path / "dist" / "bundle.js"))
    assert not should_ingest(dist, root_real)

    # __pycache__
    cache = os.path.realpath(str(tmp_path / "pkg" / "__pycache__" / "foo.pyc"))
    assert not should_ingest(cache, root_real)

    # .git directory itself (file inside .git)
    git_config = os.path.realpath(str(tmp_path / ".git" / "config"))
    assert not should_ingest(git_config, root_real)

    # .next (Next.js build output)
    next_file = os.path.realpath(str(tmp_path / ".next" / "server" / "app.js"))
    assert not should_ingest(next_file, root_real)


# ── 2. Normal source files are ingested ──────────────────────────────────────

def test_normal_file_ingested(tmp_path):
    root_real = os.path.realpath(str(tmp_path))
    py_file = os.path.realpath(str(tmp_path / "src" / "utils.py"))
    assert should_ingest(py_file, root_real)

    ts_file = os.path.realpath(str(tmp_path / "src" / "components" / "Button.tsx"))
    assert should_ingest(ts_file, root_real)


# ── 3. Nested independent project is excluded ─────────────────────────────────

def test_nested_independent_project(tmp_path):
    sub = tmp_path / "subproj"
    sub.mkdir()
    (sub / ".git").mkdir()  # nested independent git repo
    (sub / "foo.py").write_text("def foo(): pass\n")

    root_real = os.path.realpath(str(tmp_path))
    file_real = os.path.realpath(str(sub / "foo.py"))
    assert not should_ingest(file_real, root_real)


# ── 4. Project's own .git does NOT exclude files under the project root ───────

def test_project_own_git_not_excluded(tmp_path):
    (tmp_path / ".git").mkdir()   # project's own VCS root
    src = tmp_path / "src"
    src.mkdir()
    (src / "foo.py").write_text("def foo(): pass\n")

    root_real = os.path.realpath(str(tmp_path))
    file_real = os.path.realpath(str(src / "foo.py"))
    assert should_ingest(file_real, root_real)

    # File directly at project root (no subdirectory) is also fine
    top_file = os.path.realpath(str(tmp_path / "main.py"))
    assert should_ingest(top_file, root_real)


# ── 5. Deep nested .git only excludes its subtree, not siblings ───────────────

def test_nested_git_deep(tmp_path):
    # a/b/subproj has its own .git
    subproj = tmp_path / "a" / "b" / "subproj"
    subproj.mkdir(parents=True)
    (subproj / ".git").mkdir()
    (subproj / "x.ts").touch()

    # a/b/other.ts is a sibling — NOT in the nested repo
    other = tmp_path / "a" / "b" / "other.ts"
    other.touch()

    root_real = os.path.realpath(str(tmp_path))
    assert not should_ingest(os.path.realpath(str(subproj / "x.ts")), root_real)
    assert should_ingest(os.path.realpath(str(other)), root_real)

    # File deeper inside the nested subproj is also excluded
    deep = subproj / "lib" / "deep.py"
    deep.parent.mkdir()
    deep.touch()
    assert not should_ingest(os.path.realpath(str(deep)), root_real)


# ── 6. End-to-end: reconcile excludes nested subproject defines ───────────────

def test_e2e_reconcile_excludes_nested(tmp_path):
    # Project root with its own .git
    (tmp_path / ".git").mkdir()
    main_py = tmp_path / "main.py"
    main_py.write_text(textwrap.dedent("""\
        def main_func():
            pass
    """))

    # Nested independent subproject
    sub = tmp_path / "subproj"
    sub.mkdir()
    (sub / ".git").mkdir()
    sub_py = sub / "sub_module.py"
    sub_py.write_text(textwrap.dedent("""\
        def sub_func():
            pass
    """))

    db_path = str(tmp_path / "test.sqlite")
    store = Store(db_path)
    pid = store.get_or_create_project(str(tmp_path))

    result = reconcile(store, pid, [str(main_py), str(sub_py)])

    names = {
        r["define_name"]
        for r in store.con.execute(
            "SELECT define_name FROM determinations WHERE project_id=?", (pid,)
        ).fetchall()
    }
    assert "main_func" in names, "main project define must be ingested"
    assert "sub_func" not in names, "nested subproject define must NOT be ingested"

    assert str(sub_py) in result.boundary_violations, (
        "nested subproject file must appear in boundary_violations"
    )

    store.close()
