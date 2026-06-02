"""End-to-end lifecycle regression tests.

Guards the two parse-bug fixes at integration level:
  - @overload stubs must NOT appear as defines (PY_AUTH pattern)
  - TS getter/setter must produce distinct qualified_names (TS_OPTIONS pattern)

Key ordering invariant: source files MUST be committed to the git repo before
session-start is called, because session-start triggers a background ingest that
scans the filesystem immediately.

Correct payload format for post-bash:
  {"tool_input": {"command": "..."}, "tool_response": {"stdout": "..."}, "cwd": ...}
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import buer.mcp.server as srv
from buer.mcp.server import (
    mcp,
    _set_store_for_testing,
    _full_ingest_in_progress,
    _full_ingest_lock,
)
from buer.store import Store


# ── embedded code patterns ────────────────────────────────────────────────────

PY_AUTH = """\
from typing import overload

class Auth:
    @overload
    def __init__(self, user: str, pw: str) -> None: ...
    @overload
    def __init__(self, user: bytes, pw: bytes) -> None: ...
    def __init__(self, user, pw):
        self.user = user
        self.pw = pw

    def apply(self, r):
        return self._encode(r)

    def _encode(self, r):
        return r
"""

PY_API = """\
from .auth import Auth

def make_auth(u, p):
    return Auth(u, p)
"""

TS_OPTIONS = """\
export class Options {
    private _timeout: number = 0;
    get timeout(): number { return this._timeout; }
    set timeout(v: number) { assertNumber(v); this._timeout = v; }
    get retry(): number { return this._retry; }
    set retry(v: number) { assertNumber(v); this._retry = v; }
    merge(other: Options): void { this.normalize(); }
    normalize(): void { return; }
}
function assertNumber(v: any): void { }
"""


# ── helpers ───────────────────────────────────────────────────────────────────

def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(path), check=True, capture_output=True)
    (path / ".gitkeep").write_text("")
    subprocess.run(["git", "add", "."], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(path), check=True, capture_output=True)


def _wait_ingest(project_id: int, timeout: float = 8.0) -> None:
    """Block until background ingest for project_id finishes (or timeout).

    Safe to call even if ingest has already completed — returns immediately.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _full_ingest_lock:
            if project_id not in _full_ingest_in_progress:
                return
        time.sleep(0.05)
    raise TimeoutError(f"Background ingest for project {project_id} did not finish within {timeout}s")


def _read_defines(db_file: str, pid: int) -> list[str]:
    """Open a fresh store connection and return all define_names for the project."""
    s = Store(db_file)
    rows = s.con.execute(
        "SELECT define_name FROM determinations WHERE project_id=? AND define_name IS NOT NULL",
        (pid,),
    ).fetchall()
    s.close()
    return [r["define_name"] for r in rows]


@pytest.fixture(autouse=True)
def reset_store():
    yield
    _set_store_for_testing(None)


@pytest.fixture
def http_app():
    return mcp.streamable_http_app()


# ══════════════════════════════════════════════════════════════════════════════
# 1. Full Python lifecycle: @overload stubs absent, real impls present
# ══════════════════════════════════════════════════════════════════════════════

def test_e2e_full_lifecycle_python(tmp_path, http_app):
    """Files committed BEFORE session-start; post-bash uses correct tool_input format."""
    _init_git_repo(tmp_path)

    db_file = str(tmp_path / ".buer_test.sqlite")
    orig_db = srv._db_path
    srv._db_path = db_file

    s = Store(db_file)
    _set_store_for_testing(s)

    try:
        # Step 1: write source files and commit BEFORE session-start triggers ingest
        (tmp_path / "auth.py").write_text(PY_AUTH)
        (tmp_path / "api.py").write_text(PY_API)
        subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "add auth"],
            cwd=str(tmp_path), check=True, capture_output=True,
        )

        tc = TestClient(http_app)

        # Step 2: session-start — files are on disk, ingest will find them
        resp = tc.post("/buer/session-start", json={
            "cwd": str(tmp_path),
            "session_id": "e2e-py-1",
        })
        assert resp.status_code == 200

        pid = s.get_or_create_project(str(tmp_path), "main")

        # Step 3: wait for background ingest (triggered by session-start)
        _wait_ingest(pid)

        # Step 4: read defines via fresh connection (avoid stale cache)
        qnames = _read_defines(db_file, pid)

        # Non-vacuous guard: ingest must have found the source files
        assert len(qnames) > 0, (
            f"No defines in DB — ingest ran before files were written. pid={pid}"
        )
        assert any("Auth" in q for q in qnames), (
            f"Auth defines not found — file write ordering may still be wrong. Got: {qnames}"
        )

        # @overload guard: exactly one __init__ (no stubs)
        assert qnames.count("Auth.__init__") == 1, (
            f"@overload regression: Auth.__init__ appears {qnames.count('Auth.__init__')} times"
        )

        # Real methods present
        assert "Auth.apply" in qnames, f"Auth.apply missing; defines: {qnames}"
        assert "Auth._encode" in qnames
        assert "make_auth" in qnames

        # Step 5: post-bash with correct tool_input/tool_response format
        resp2 = tc.post("/buer/post-bash", json={
            "tool_input": {"command": "git commit -m 'add auth'"},
            "tool_response": {"stdout": "[main abc1234] add auth\n 2 files changed"},
            "cwd": str(tmp_path),
            "session_id": "e2e-py-1",
        })
        assert resp2.status_code == 200

        # Snapshot created (either from initial ingest or Feature 3 commit detection)
        s2 = Store(db_file)
        snaps = s2.con.execute(
            "SELECT * FROM snapshots WHERE project_id=?", (pid,)
        ).fetchall()
        s2.close()
        assert len(snaps) >= 1, "Expected at least one snapshot after git ingest lifecycle"

    finally:
        srv._db_path = orig_db
        s.close()


# ══════════════════════════════════════════════════════════════════════════════
# 2. Full TS lifecycle: getter/setter distinct qualified_names, no multi-fp
# ══════════════════════════════════════════════════════════════════════════════

def test_e2e_full_lifecycle_typescript(tmp_path, http_app):
    """Files committed BEFORE session-start; get/set accessors are distinct after ingest."""
    _init_git_repo(tmp_path)

    db_file = str(tmp_path / ".buer_test.sqlite")
    orig_db = srv._db_path
    srv._db_path = db_file

    s = Store(db_file)
    _set_store_for_testing(s)

    try:
        # Step 1: write and commit TS file BEFORE session-start
        (tmp_path / "options.ts").write_text(TS_OPTIONS)
        subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "add options"],
            cwd=str(tmp_path), check=True, capture_output=True,
        )

        tc = TestClient(http_app)

        # Step 2: session-start with files already on disk
        resp = tc.post("/buer/session-start", json={
            "cwd": str(tmp_path),
            "session_id": "e2e-ts-1",
        })
        assert resp.status_code == 200

        pid = s.get_or_create_project(str(tmp_path), "main")

        # Step 3: wait for ingest
        _wait_ingest(pid)

        # Step 4: read defines via fresh connection
        qnames = _read_defines(db_file, pid)

        # Non-vacuous guard
        assert len(qnames) > 0, (
            f"No defines in DB — ingest ran before files were written. pid={pid}"
        )
        assert "Options.get timeout" in qnames, (
            f"Options.get timeout not found — file write ordering may still be wrong. Got: {qnames}"
        )

        # getter/setter must be distinct qualified_names
        assert "Options.set timeout" in qnames, f"Missing Options.set timeout; got: {qnames}"
        assert "Options.get retry" in qnames
        assert "Options.set retry" in qnames

        # Normal methods and module-level function present
        assert "Options.merge" in qnames
        assert "Options.normalize" in qnames
        assert "assertNumber" in qnames

        # Zero multi-define qualified_names (no fingerprint thrash)
        by_qname: dict[str, int] = {}
        for q in qnames:
            by_qname[q] = by_qname.get(q, 0) + 1
        multi = {k: v for k, v in by_qname.items() if v > 1}
        assert len(multi) == 0, f"Multi-define qnames (getter/setter collision): {multi}"

        # Step 5: post-bash with correct format
        resp2 = tc.post("/buer/post-bash", json={
            "tool_input": {"command": "git commit -m 'add options'"},
            "tool_response": {"stdout": "[main def5678] add options\n 1 file changed"},
            "cwd": str(tmp_path),
            "session_id": "e2e-ts-1",
        })
        assert resp2.status_code == 200

        # Snapshot present
        s2 = Store(db_file)
        snaps = s2.con.execute(
            "SELECT * FROM snapshots WHERE project_id=?", (pid,)
        ).fetchall()
        s2.close()
        assert len(snaps) >= 1, "Expected at least one snapshot after git ingest lifecycle"

    finally:
        srv._db_path = orig_db
        s.close()


# ══════════════════════════════════════════════════════════════════════════════
# 3. Fingerprint stability: 3 reconcile passes on same file → no drift
# ══════════════════════════════════════════════════════════════════════════════

def test_e2e_reexport_penetration(tmp_path, http_app):
    """Files committed before session-start; 3 total reconcile passes → get/set stable."""
    _init_git_repo(tmp_path)

    db_file = str(tmp_path / ".buer_test.sqlite")
    orig_db = srv._db_path
    srv._db_path = db_file

    s = Store(db_file)
    _set_store_for_testing(s)

    try:
        # Step 1: write and commit TS file BEFORE session-start
        ts_file = tmp_path / "options.ts"
        ts_file.write_text(TS_OPTIONS)
        subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "add options"],
            cwd=str(tmp_path), check=True, capture_output=True,
        )

        tc = TestClient(http_app)

        # Step 2: session-start → pass 1 (initial ingest)
        tc.post("/buer/session-start", json={
            "cwd": str(tmp_path),
            "session_id": "e2e-repro-1",
        })

        pid = s.get_or_create_project(str(tmp_path), "main")

        # Step 3: wait for pass 1 to complete
        _wait_ingest(pid)

        # Non-vacuous: confirm accessor defines are present after pass 1
        qnames_pass1 = _read_defines(db_file, pid)
        assert "Options.get timeout" in qnames_pass1, (
            f"Pass 1: Options.get timeout not found — file write ordering wrong. Got: {qnames_pass1}"
        )
        assert "Options.set timeout" in qnames_pass1

        # Steps 4–5: two more direct reconcile passes (simulates repeated ingest)
        from buer.reconcile import reconcile
        for _ in range(2):
            s_pass = Store(db_file)
            reconcile(s_pass, pid, [str(ts_file)])
            s_pass.close()

        # Step 6: read full determination history
        s2 = Store(db_file)
        rows = s2.con.execute(
            """SELECT define_name, node_fingerprint
               FROM determinations
               WHERE project_id=? AND define_name IS NOT NULL
               ORDER BY define_name, seq""",
            (pid,),
        ).fetchall()
        s2.close()

        by_name: dict[str, set] = {}
        for r in rows:
            by_name.setdefault(r["define_name"], set()).add(r["node_fingerprint"])

        # Each accessor must have exactly 1 unique fingerprint across all 3 passes (no drift)
        for accessor in (
            "Options.get timeout", "Options.set timeout",
            "Options.get retry", "Options.set retry",
        ):
            fps = by_name.get(accessor, set())
            assert len(fps) == 1, (
                f"{accessor} fingerprint drifted across 3 passes: {fps}"
            )

        # getter and setter must have DIFFERENT fingerprints from each other
        get_fp = next(iter(by_name.get("Options.get timeout", set())), None)
        set_fp = next(iter(by_name.get("Options.set timeout", set())), None)
        assert get_fp is not None and set_fp is not None
        assert get_fp != set_fp, (
            "Options.get timeout and Options.set timeout must have distinct fingerprints"
        )

    finally:
        srv._db_path = orig_db
        s.close()


# ══════════════════════════════════════════════════════════════════════════════
# 4. Portability: extract_defines alone (no server) also satisfies both fixes
# ══════════════════════════════════════════════════════════════════════════════

def test_e2e_no_external_clone_needed(tmp_path):
    """extract_defines on embedded patterns is self-contained (no network, no external repos)."""
    from buer.parse import extract_defines

    # Write and parse Python auth pattern
    auth_path = tmp_path / "auth.py"
    auth_path.write_text(PY_AUTH)
    py_defs = extract_defines(str(auth_path))
    py_qnames = [d.qualified_name for d in py_defs]

    # @overload guard
    assert py_qnames.count("Auth.__init__") == 1, (
        f"@overload fix regression: Auth.__init__ appears {py_qnames.count('Auth.__init__')} times"
    )
    assert "Auth.apply" in py_qnames
    assert "Auth._encode" in py_qnames

    # Write and parse TS options pattern
    ts_path = tmp_path / "options.ts"
    ts_path.write_text(TS_OPTIONS)
    ts_defs = extract_defines(str(ts_path))
    ts_qnames = [d.qualified_name for d in ts_defs]

    # getter/setter guard
    assert "Options.get timeout" in ts_qnames, f"getter/setter fix regression; got: {ts_qnames}"
    assert "Options.set timeout" in ts_qnames
    assert ts_qnames.count("Options.get timeout") == 1
    assert ts_qnames.count("Options.set timeout") == 1

    # getter ≠ setter (distinct names, not same qname)
    assert "Options.get timeout" != "Options.set timeout"

    # no duplicates at all
    for qname in set(ts_qnames):
        assert ts_qnames.count(qname) == 1, f"{qname} appears multiple times: regression"


# ══════════════════════════════════════════════════════════════════════════════
# 5. Mutation guard — @overload fix: if _has_overload → False, stubs re-appear
# ══════════════════════════════════════════════════════════════════════════════

def test_mutation_overload_fix(tmp_path):
    """Verify that the @overload guard is load-bearing: if bypassed, duplicates appear."""
    from buer.parse import extract_defines
    import buer.parse as parse_mod

    auth_path = tmp_path / "auth.py"
    auth_path.write_text(PY_AUTH)

    # Baseline: fix active — no duplicates
    defs = extract_defines(str(auth_path))
    qnames = [d.qualified_name for d in defs]
    assert qnames.count("Auth.__init__") == 1

    # Mutation: disable _has_overload (simulate the bug)
    orig_has_overload = parse_mod._has_overload
    parse_mod._has_overload = lambda node: False
    try:
        defs_mut = extract_defines(str(auth_path))
        qnames_mut = [d.qualified_name for d in defs_mut]
        # Without the fix, stubs are included → duplicates
        assert qnames_mut.count("Auth.__init__") > 1, (
            "Mutation did not produce duplicate __init__ — the overload fix may not be guarding this path"
        )
    finally:
        parse_mod._has_overload = orig_has_overload


# ══════════════════════════════════════════════════════════════════════════════
# 6. Mutation guard — getter/setter fix: if no prefix, get/set collapse to same qname
# ══════════════════════════════════════════════════════════════════════════════

def test_mutation_getter_setter_fix(tmp_path):
    """Verify that _js_method_name prefix is load-bearing: if stripped, get/set collide."""
    from buer.parse import extract_defines
    import buer.parse as parse_mod

    ts_path = tmp_path / "options.ts"
    ts_path.write_text(TS_OPTIONS)

    # Baseline: fix active — distinct qnames
    defs = extract_defines(str(ts_path))
    qnames = [d.qualified_name for d in defs]
    assert "Options.get timeout" in qnames
    assert "Options.set timeout" in qnames

    # Mutation: strip the get/set prefix from _js_method_name
    orig_fn = parse_mod._js_method_name

    def _no_prefix(method_node) -> str:
        result = orig_fn(method_node)
        if result.startswith("get ") or result.startswith("set "):
            return result.split(" ", 1)[1]
        return result

    parse_mod._js_method_name = _no_prefix
    try:
        defs_mut = extract_defines(str(ts_path))
        qnames_mut = [d.qualified_name for d in defs_mut]
        # Without prefix, "get timeout" and "set timeout" both become "timeout"
        assert "Options.get timeout" not in qnames_mut, (
            "Mutation should have removed the 'get ' prefix — check mutation logic"
        )
        # Bare name appears (both get+set map to same qname)
        timeout_count = qnames_mut.count("Options.timeout")
        assert timeout_count >= 1, "Expected Options.timeout to appear after stripping prefix"
    finally:
        parse_mod._js_method_name = orig_fn


# ══════════════════════════════════════════════════════════════════════════════
# 7. Define deletion: diff_snapshots shows removed define (SDT §2.2.5)
# ══════════════════════════════════════════════════════════════════════════════

PY_TWO_DEFINES = """\
def alpha(x):
    return x + 1

def beta(x):
    return x * 2
"""

PY_ONE_DEFINE = """\
def alpha(x):
    return x + 1
"""


def test_e2e_define_deletion_in_diff(tmp_path):
    """Delete a define → diff_snapshots shows it as Removed (not silently absent).

    Mutation guard: the delete determination is the only path that makes
    define_fingerprints_at_seq exclude beta at snapshot B's seq.
    Removing the insert_determination_atomic(edit_type='delete') call causes
    beta to still appear alive in the reconstruction → diff shows nothing removed.
    """
    from buer.reconcile import reconcile
    from buer.mcp.server import diff_snapshots, _set_store_for_testing

    _init_git_repo(tmp_path)
    s = Store(":memory:")
    _set_store_for_testing(s)

    try:
        pid = s.get_or_create_project(str(tmp_path), branch="main")
        py_file = tmp_path / "funcs.py"

        # Pass 1: both defines exist → snapshot A
        py_file.write_text(PY_TWO_DEFINES)
        subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "v1"], cwd=str(tmp_path),
                       check=True, capture_output=True)
        commit_a = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(tmp_path)
        ).decode().strip()

        reconcile(s, pid, [str(py_file)])
        seq_a = s.max_seq(pid)
        s.create_snapshot(pid, commit_a, "main", seq_a, None, reason="commit")

        # Verify both defines are alive at snapshot A
        fps_a = s.define_fingerprints_at_seq(pid, seq_a)
        a_defines = {dn for (_, dn) in fps_a}
        assert "alpha" in a_defines, f"alpha must be alive at snapshot A; got {a_defines}"
        assert "beta" in a_defines, f"beta must be alive at snapshot A; got {a_defines}"

        # Pass 2: beta deleted → snapshot B
        py_file.write_text(PY_ONE_DEFINE)
        subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "v2 drop beta"], cwd=str(tmp_path),
                       check=True, capture_output=True)
        commit_b = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(tmp_path)
        ).decode().strip()

        reconcile(s, pid, [str(py_file)])
        seq_b = s.max_seq(pid)
        s.create_snapshot(pid, commit_b, "main", seq_b, commit_a, reason="commit")

        # DB guard: a delete determination must exist for beta
        del_rows = s.con.execute(
            """SELECT seq, edit_type FROM determinations
               WHERE project_id=? AND define_name='beta' AND edit_type='delete'""",
            (pid,),
        ).fetchall()
        assert len(del_rows) == 1, (
            f"Expected exactly 1 delete determination for beta; got {len(del_rows)}. "
            "Mutation: removing insert_determination_atomic(edit_type='delete') causes this to fail."
        )

        # Reconstruction guard: beta must be absent at snapshot B's seq
        fps_b = s.define_fingerprints_at_seq(pid, seq_b)
        b_defines = {dn for (_, dn) in fps_b}
        assert "alpha" in b_defines, f"alpha must still be alive at snapshot B; got {b_defines}"
        assert "beta" not in b_defines, (
            f"beta must be absent at snapshot B (delete recorded); got {b_defines}. "
            "Mutation: without delete determination, reconstruction shows beta as alive."
        )

        # diff_snapshots guard: Removed section must mention beta
        result = diff_snapshots(str(tmp_path), commit_a, commit_b)
        assert "Removed" in result, (
            f"diff_snapshots must report Removed defines when beta is deleted; got:\n{result}"
        )
        assert "beta" in result, (
            f"diff_snapshots Removed section must name 'beta'; got:\n{result}"
        )

    finally:
        _set_store_for_testing(None)
        s.close()
