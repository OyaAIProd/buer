"""Tests for .gitignore directory-level exclusion in buer/boundary.py."""
import os
import textwrap

from buer.boundary import _gitignore_dir_patterns, should_ingest
from buer.reconcile import reconcile
from buer.store import Store


def _open_boundary_breaches(store: Store, pid: int) -> list[str]:
    """Return target_node values for open boundary_breach incidents."""
    rows = store.open_incidents(pid)
    return [r["target_node"] for r in rows if r["signal"] == "boundary_breach"]


def _setup(tmp_path, gitignore_text):
    """Write .gitignore and return (root_real,)."""
    (tmp_path / ".gitignore").write_text(textwrap.dedent(gitignore_text))
    # Clear lru_cache so each test reads its own .gitignore
    _gitignore_dir_patterns.cache_clear()
    return os.path.realpath(str(tmp_path))


# ── 1. Bare dir name ──────────────────────────────────────────────────────────

def test_gitignore_bare_dir(tmp_path):
    root_real = _setup(tmp_path, "coverage\n")
    f = os.path.realpath(str(tmp_path / "coverage" / "foo.py"))
    assert not should_ingest(f, root_real)


# ── 2. Trailing slash ─────────────────────────────────────────────────────────

def test_gitignore_trailing_slash(tmp_path):
    root_real = _setup(tmp_path, "build/\n")
    f = os.path.realpath(str(tmp_path / "build" / "x.ts"))
    assert not should_ingest(f, root_real)


# ── 3. Leading slash (root-anchored) ─────────────────────────────────────────

def test_gitignore_leading_slash(tmp_path):
    root_real = _setup(tmp_path, "/dist\n")
    f = os.path.realpath(str(tmp_path / "dist" / "x.js"))
    assert not should_ingest(f, root_real)


# ── 4. Negation lines are ignored (don't crash, don't mis-exclude) ───────────

def test_gitignore_negation_ignored(tmp_path):
    root_real = _setup(tmp_path, "!keep\n")
    patterns = _gitignore_dir_patterns(root_real)
    assert "keep" not in patterns  # negation not added to exclude set
    # A file under 'keep' should still be ingested
    f = os.path.realpath(str(tmp_path / "keep" / "app.py"))
    assert should_ingest(f, root_real)


# ── 5. File-glob lines are ignored (not treated as dir exclude) ───────────────

def test_gitignore_fileglob_ignored(tmp_path):
    root_real = _setup(tmp_path, "*.tmp\n*.log\n")
    patterns = _gitignore_dir_patterns(root_real)
    assert not any("*" in p for p in patterns)  # globs must not enter pattern set
    # A normal source file is not excluded
    f = os.path.realpath(str(tmp_path / "src" / "app.py"))
    assert should_ingest(f, root_real)


# ── 6. Missing .gitignore does not crash ─────────────────────────────────────

def test_no_gitignore(tmp_path):
    _gitignore_dir_patterns.cache_clear()
    root_real = os.path.realpath(str(tmp_path))
    # No .gitignore written — should not raise
    patterns = _gitignore_dir_patterns(root_real)
    assert patterns == frozenset()
    f = os.path.realpath(str(tmp_path / "src" / "main.py"))
    assert should_ingest(f, root_real)


# ── 7. .gitignore does not mis-exclude normal source files ────────────────────

def test_gitignore_does_not_override_normal(tmp_path):
    root_real = _setup(tmp_path, "coverage\nbuild/\n")
    # Unrelated file should still pass
    f = os.path.realpath(str(tmp_path / "src" / "app.py"))
    assert should_ingest(f, root_real)


# ── 8. Malformed / binary .gitignore does not crash ──────────────────────────

def test_malformed_gitignore(tmp_path):
    (tmp_path / ".gitignore").write_bytes(b"\xff\xfe" + b"\x00" * 100)
    _gitignore_dir_patterns.cache_clear()
    root_real = os.path.realpath(str(tmp_path))
    # Must not raise; may return empty or partial frozenset
    patterns = _gitignore_dir_patterns(root_real)
    assert isinstance(patterns, frozenset)
    f = os.path.realpath(str(tmp_path / "src" / "ok.py"))
    assert should_ingest(f, root_real)


# ── 9. lru_cache: same root returns same object ───────────────────────────────

def test_cache_consistency(tmp_path):
    root_real = _setup(tmp_path, "coverage\n")
    r1 = _gitignore_dir_patterns(root_real)
    r2 = _gitignore_dir_patterns(root_real)
    assert r1 is r2  # lru_cache returns cached object identity


# ── 10. End-to-end: reconcile respects .gitignore directory exclusions ────────

def test_e2e_gitignore_exclusion(tmp_path):
    (tmp_path / ".gitignore").write_text("coverage\nbuild/\n")
    _gitignore_dir_patterns.cache_clear()

    # Create source files
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "app.py").write_text("def app_func(): pass\n")

    cov_dir = tmp_path / "coverage"
    cov_dir.mkdir()
    (cov_dir / "c.py").write_text("def cov_func(): pass\n")

    build_dir = tmp_path / "build"
    build_dir.mkdir()
    (build_dir / "b.ts").write_text("export function buildFunc() {}\n")

    store = Store(str(tmp_path / "test.sqlite"))
    pid = store.get_or_create_project(str(tmp_path))

    result = reconcile(store, pid, [
        str(src_dir / "app.py"),
        str(cov_dir / "c.py"),
        str(build_dir / "b.ts"),
    ])

    names = {
        r["define_name"]
        for r in store.con.execute(
            "SELECT define_name FROM determinations WHERE project_id=?", (pid,)
        ).fetchall()
        if r["define_name"]
    }
    assert "app_func" in names
    assert "cov_func" not in names
    assert "buildFunc" not in names

    violations = result.boundary_violations
    assert str(cov_dir / "c.py") not in violations    # in-project but gitignored → not a breach
    assert str(build_dir / "b.ts") not in violations  # in-project but gitignored → not a breach

    store.close()


# ── 11. Core fix: should_ingest=False → NOT in boundary_violations, no incident ──

def test_gitignored_file_not_in_violations(tmp_path):
    """In-project gitignored files must not appear in boundary_violations or trigger boundary_breach."""
    (tmp_path / ".gitignore").write_text("build/\n")
    _gitignore_dir_patterns.cache_clear()

    build_dir = tmp_path / "build"
    build_dir.mkdir()
    py_file = build_dir / "output.py"
    py_file.write_text("x = 1\n")

    store = Store(str(tmp_path / "test.sqlite"))
    pid = store.get_or_create_project(str(tmp_path))

    result = reconcile(store, pid, [str(py_file)])

    assert str(py_file) not in result.boundary_violations
    assert _open_boundary_breaches(store, pid) == []

    store.close()


# ── 12. Regression: true out-of-project path still reported and fires incident ──

def test_true_boundary_breach_still_reported(tmp_path):
    """A file genuinely outside the project root must land in boundary_violations and open an incident."""
    _gitignore_dir_patterns.cache_clear()

    outside_file = str(tmp_path.parent / "outside_buer_test.py")

    store = Store(str(tmp_path / "test.sqlite"))
    pid = store.get_or_create_project(str(tmp_path))

    result = reconcile(store, pid, [outside_file])

    assert outside_file in result.boundary_violations
    breaches = _open_boundary_breaches(store, pid)
    assert outside_file in breaches

    store.close()
