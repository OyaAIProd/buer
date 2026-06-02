"""Real-path edge-correctness regression suite.

Why this file exists
--------------------
The C5 bug (delete_call_edges_for_module erasing an entire package's call_edges
when reconciling an __init__.py) survived 1406 passing tests because the edge
tests used *toy* project structures — flat files, no packages, no __init__.py —
and therefore never exercised the code path where module_name == package_name.

Two disciplines are enforced here to keep that class of bug from recurring:

  1. REPRESENTATIVE FIXTURE. Tests run against a project that looks like a real
     one: a package with __init__.py, a nested sub-package, and (where relevant)
     TypeScript barrels. Toy fixtures hide structure-specific bugs.

  2. REAL PATH ONLY. Edge state is driven exclusively through the production
     entry points — reconcile([file]) and reconcile_against_disk(...). This file
     MUST NOT call rebuild_call_edges_full() to set up or repair edge state:
     that full rebuild masks incremental-maintenance bugs (it was the shortcut
     that hid C5 for several commits). The single allowed exception is a barrel
     re-export penetration case, where the production ingest itself legitimately
     calls rebuild_call_edges_full as a second pass; that call is part of the
     behavior under test, not a test-setup shortcut, and is flagged inline.

Each test corresponds to a falsification attack from the three self-review
rounds (boundaries, assumptions, graph-consumers).
"""

import os
import tempfile
import shutil
import time

import buer.reconcile as rec_mod
from buer.reconcile import reconcile, reconcile_against_disk
from buer.store import Store
import buer.influence as influence


# ── helpers ────────────────────────────────────────────────────────────────────

def _advance_mtime(path: str, delta: float = 2.0) -> None:
    """Push a file's mtime forward so reconcile_against_disk treats it as changed.

    Using utime with an explicit delta avoids tmpfs float->ns->float precision
    flakiness where a fresh write can land on the same mtime as the baseline.
    """
    cur = os.stat(path).st_mtime
    os.utime(path, (cur + delta, cur + delta))


def _edges(store: Store, pid: int) -> list:
    """gd_edges as sorted 'fromDefine(fromFile)->toDefine(toFile)' strings."""
    rows = store.con.execute(
        """SELECT fd.define_name AS fr, td.define_name AS to_,
                  fd.file_path AS ff, td.file_path AS tf
           FROM gd_edges e
           JOIN determinations fd ON e.from_det = fd.id
           JOIN determinations td ON e.to_det = td.id
           WHERE e.project_id = ?""",
        (pid,),
    ).fetchall()
    return sorted(
        f"{r['fr']}({os.path.basename(r['ff'])})->{r['to_']}({os.path.basename(r['tf'])})"
        for r in rows
    )


def _call_edges(store: Store, pid: int) -> list:
    return sorted(
        (r["caller"], r["callee"])
        for r in store.con.execute(
            "SELECT caller, callee FROM call_edges WHERE project_id=?", (pid,)
        ).fetchall()
    )


def _latest_det(store: Store, pid: int, define_name: str):
    return store.con.execute(
        "SELECT id FROM determinations WHERE define_name=? AND project_id=? "
        "ORDER BY seq DESC LIMIT 1",
        (define_name, pid),
    ).fetchone()


def _post_edit(store: Store, pid: int, root: str, file_path: str) -> None:
    """Production post_edit flow: reconcile-against-disk (excluding the edited
    file) followed by the dedicated reconcile([file_path]). No full rebuild."""
    reconcile_against_disk(store, pid, root, exclude={file_path})
    reconcile(store, pid, [file_path])


class _Pkg:
    """A representative on-disk project with a package + nested sub-package.

    Layout:
        root/pkg/__init__.py          (empty package marker)
        root/pkg/a.py                 def target()
        root/pkg/b.py                 caller() -> target()
        root/pkg/sub/__init__.py
        root/pkg/sub/deep.py          deep() -> target()
    """

    def __init__(self):
        self.root = tempfile.mkdtemp()
        self.store = Store(":memory:")
        self.pid = self.store.get_or_create_project(self.root)

    def write(self, rel: str, content: str) -> str:
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(content)
        return p

    def path(self, rel: str) -> str:
        return os.path.join(self.root, rel)

    def close(self):
        self.store.close()
        shutil.rmtree(self.root, ignore_errors=True)


def _base_package() -> _Pkg:
    pk = _Pkg()
    pk.write("pkg/__init__.py", "")
    pk.write("pkg/a.py", "def target():\n    return 1\n")
    pk.write("pkg/b.py", "from pkg.a import target\ndef caller():\n    return target()\n")
    # Real-path ingest: reconcile the files, then establish reconcile baselines.
    files = [pk.path("pkg/__init__.py"), pk.path("pkg/a.py"), pk.path("pkg/b.py")]
    reconcile(pk.store, pk.pid, files)
    reconcile_against_disk(pk.store, pk.pid, pk.root)
    return pk


# ── Round 1: structural boundaries ─────────────────────────────────────────────

def test_reconcile_init_py_does_not_overdelete_package():
    """C5 core: reconciling __init__.py must NOT erase sibling files' call_edges.

    This is the exact path that 1406 toy-structured tests missed.
    """
    pk = _base_package()
    try:
        before = _call_edges(pk.store, pk.pid)
        assert before == [("py::pkg.b.caller", "py::pkg.a.target")]
        # Reconcile the package's __init__.py (module_name == 'pkg' == package).
        reconcile(pk.store, pk.pid, [pk.path("pkg/__init__.py")])
        after = _call_edges(pk.store, pk.pid)
        assert after == before, f"__init__.py over-deleted package call_edges: {after}"
    finally:
        pk.close()


def test_nested_init_py_does_not_overdelete():
    """A nested sub-package __init__.py (module_name == 'pkg.sub') is also safe."""
    pk = _base_package()
    try:
        pk.write("pkg/sub/__init__.py", "")
        pk.write(
            "pkg/sub/deep.py",
            "from pkg.a import target\ndef deep():\n    return target()\n",
        )
        # Real path: reconciliation discovers the new files via dir-mtime scan.
        reconcile_against_disk(pk.store, pk.pid, pk.root)
        assert any("deep" in e for e in _edges(pk.store, pk.pid))
        # Reconciling the nested __init__ must not wipe deep's or b's edges.
        reconcile(pk.store, pk.pid, [pk.path("pkg/sub/__init__.py")])
        edges = _edges(pk.store, pk.pid)
        assert any("caller" in e for e in edges), edges
        assert any("deep" in e for e in edges), edges
    finally:
        pk.close()


def test_same_name_define_across_files_not_confused():
    """Two files each define foo; a caller importing one must not link to the other."""
    pk = _Pkg()
    try:
        pk.write("pkg/__init__.py", "")
        pk.write("pkg/a.py", "def foo():\n    return 1\n")
        pk.write("pkg/b.py", "def foo():\n    return 2\n")
        pk.write(
            "pkg/c.py",
            "from pkg.a import foo\ndef caller():\n    return foo()\n",
        )
        reconcile(
            pk.store,
            pk.pid,
            [pk.path("pkg/__init__.py"), pk.path("pkg/a.py"),
             pk.path("pkg/b.py"), pk.path("pkg/c.py")],
        )
        edges = _edges(pk.store, pk.pid)
        assert any("foo(a.py)->caller" in e for e in edges), edges
        assert not any("foo(b.py)->caller" in e for e in edges), edges
    finally:
        pk.close()


def test_intra_file_call_edge():
    """A define calling another define in the same file produces an edge."""
    pk = _Pkg()
    try:
        pk.write("pkg/__init__.py", "")
        pk.write("pkg/a.py", "def x():\n    return y()\ndef y():\n    return 1\n")
        reconcile(pk.store, pk.pid, [pk.path("pkg/__init__.py"), pk.path("pkg/a.py")])
        assert any("y" in e and "x" in e for e in _edges(pk.store, pk.pid))
    finally:
        pk.close()


def test_circular_call_keeps_dag():
    """a<->b circular call: call_edges keep both directions; gd_edges stay a DAG
    (the cycle-forming edge is guarded out — by design, §3.3)."""
    pk = _Pkg()
    try:
        pk.write("pkg/__init__.py", "")
        pk.write("pkg/a.py", "from pkg.b import bfn\ndef afn():\n    return bfn()\n")
        pk.write("pkg/b.py", "from pkg.a import afn\ndef bfn():\n    return afn()\n")
        reconcile(pk.store, pk.pid, [pk.path("pkg/__init__.py"),
                                     pk.path("pkg/a.py"), pk.path("pkg/b.py")])
        ce = _call_edges(pk.store, pk.pid)
        assert len(ce) == 2, f"call_edges should record both directions: {ce}"
        gd = _edges(pk.store, pk.pid)
        assert len(gd) == 1, f"gd_edges must remain acyclic (one edge): {gd}"
    finally:
        pk.close()


def test_delete_middle_node_clears_its_edges():
    """a->b->c; externally delete b; reconciliation clears all of b's edges."""
    pk = _Pkg()
    try:
        pk.write("pkg/__init__.py", "")
        pk.write("pkg/c.py", "def cfn():\n    return 1\n")
        pk.write("pkg/b.py", "from pkg.c import cfn\ndef bfn():\n    return cfn()\n")
        pk.write("pkg/a.py", "from pkg.b import bfn\ndef afn():\n    return bfn()\n")
        reconcile(pk.store, pk.pid, [pk.path("pkg/__init__.py"), pk.path("pkg/a.py"),
                                     pk.path("pkg/b.py"), pk.path("pkg/c.py")])
        reconcile_against_disk(pk.store, pk.pid, pk.root)
        os.remove(pk.path("pkg/b.py"))
        reconcile_against_disk(pk.store, pk.pid, pk.root)
        assert not any("bfn" in e for e in _edges(pk.store, pk.pid))
    finally:
        pk.close()


# ── Round 2: assumptions ────────────────────────────────────────────────────────

def test_edit_callee_keeps_inbound_edge_real_path():
    """Editing a callee (real post_edit flow, no full rebuild) keeps its inbound
    edge — the P0 + C5 combined guarantee on the production path."""
    pk = _base_package()
    try:
        _advance_mtime(pk.path("pkg/a.py"))
        pk.write("pkg/a.py", "def target():\n    return 2\n")
        _advance_mtime(pk.path("pkg/a.py"))
        _post_edit(pk.store, pk.pid, pk.root, pk.path("pkg/a.py"))
        assert any("target" in e and "caller" in e for e in _edges(pk.store, pk.pid))
    finally:
        pk.close()


def test_delete_caller_file_no_phantom_inbound():
    """Delete the caller file, then edit the callee: no edge to the gone caller."""
    pk = _base_package()
    try:
        os.remove(pk.path("pkg/b.py"))
        reconcile_against_disk(pk.store, pk.pid, pk.root)
        _advance_mtime(pk.path("pkg/a.py"))
        pk.write("pkg/a.py", "def target():\n    return 2\n")
        _advance_mtime(pk.path("pkg/a.py"))
        _post_edit(pk.store, pk.pid, pk.root, pk.path("pkg/a.py"))
        assert _edges(pk.store, pk.pid) == []
    finally:
        pk.close()


def test_excluded_file_externally_deleted_still_clears():
    """post_edit claims file edited, but it was actually deleted; reconcile([fp])
    deletion path must clear its edges despite the exclude."""
    pk = _base_package()
    try:
        os.remove(pk.path("pkg/a.py"))
        _post_edit(pk.store, pk.pid, pk.root, pk.path("pkg/a.py"))
        assert _edges(pk.store, pk.pid) == []
    finally:
        pk.close()


def test_repeated_edits_same_file_stable():
    """Several consecutive post_edits of the same callee keep the inbound edge."""
    pk = _base_package()
    try:
        for v in (2, 3, 4):
            _advance_mtime(pk.path("pkg/a.py"))
            pk.write("pkg/a.py", f"def target():\n    return {v}\n")
            _advance_mtime(pk.path("pkg/a.py"))
            _post_edit(pk.store, pk.pid, pk.root, pk.path("pkg/a.py"))
        assert any("target" in e and "caller" in e for e in _edges(pk.store, pk.pid))
    finally:
        pk.close()


def test_remove_call_from_body_clears_outedge():
    """Caller drops its call to the callee: the out-edge disappears."""
    pk = _base_package()
    try:
        _advance_mtime(pk.path("pkg/b.py"))
        pk.write("pkg/b.py", "def caller():\n    return 0\n")
        _advance_mtime(pk.path("pkg/b.py"))
        _post_edit(pk.store, pk.pid, pk.root, pk.path("pkg/b.py"))
        assert _edges(pk.store, pk.pid) == []
    finally:
        pk.close()


def test_empty_file_does_not_crash():
    """A comment-only / empty source file reconciles without error or edges."""
    pk = _Pkg()
    try:
        pk.write("pkg/__init__.py", "")
        pk.write("pkg/empty.py", "# only a comment\n")
        pk.write("pkg/a.py", "def target():\n    return 1\n")
        reconcile(pk.store, pk.pid, [pk.path("pkg/__init__.py"),
                                     pk.path("pkg/empty.py"), pk.path("pkg/a.py")])
        reconcile_against_disk(pk.store, pk.pid, pk.root)
        _advance_mtime(pk.path("pkg/empty.py"))
        pk.write("pkg/empty.py", "# changed comment\n")
        _advance_mtime(pk.path("pkg/empty.py"))
        _post_edit(pk.store, pk.pid, pk.root, pk.path("pkg/empty.py"))
        # No exception == pass; empty file contributes no edges.
        assert all("empty" not in e for e in _edges(pk.store, pk.pid))
    finally:
        pk.close()


def test_failed_parse_file_not_retried_every_reconciliation():
    """A parse-failing file must not be reconciled on every reconciliation call
    (would be an unbounded retry); reconciliation converges."""
    pk = _Pkg()
    try:
        pk.write("pkg/__init__.py", "")
        pk.write("pkg/good.py", "def g():\n    return 1\n")
        pk.write("pkg/bad.py", "def bad(:\n  SYNTAX ERROR\n")
        reconcile(pk.store, pk.pid, [pk.path("pkg/__init__.py"),
                                     pk.path("pkg/good.py"), pk.path("pkg/bad.py")])
        reconcile_against_disk(pk.store, pk.pid, pk.root)
        # After baselines, a no-change reconciliation reconciles nothing.
        n = reconcile_against_disk(pk.store, pk.pid, pk.root)
        assert n == 0, f"reconciliation should converge, reconciled {n}"
    finally:
        pk.close()


def test_circular_symlink_no_infinite_recursion():
    """A directory symlink pointing back into the tree must not hang the scan."""
    pk = _base_package()
    try:
        try:
            os.symlink(pk.path("pkg"), pk.path("pkg/loop"))
        except OSError:
            return  # symlinks unsupported on this platform — skip silently
        pk.write("pkg/new.py", "def n():\n    return 1\n")
        start = time.time()
        reconcile_against_disk(pk.store, pk.pid, pk.root)
        assert time.time() - start < 10.0, "directory scan appears to recurse forever"
    finally:
        pk.close()


# ── Round 3: graph-consumer functions ───────────────────────────────────────────

def test_influence_cone_transitive_after_edit():
    """4-level chain a->b->c->d: changing d's cone contains c,b,a; editing the
    middle of the chain (real path) keeps the transitive closure intact."""
    pk = _Pkg()
    try:
        pk.write("pkg/__init__.py", "")
        pk.write("pkg/d.py", "def dfn():\n    return 1\n")
        pk.write("pkg/c.py", "from pkg.d import dfn\ndef cfn():\n    return dfn()\n")
        pk.write("pkg/b.py", "from pkg.c import cfn\ndef bfn():\n    return cfn()\n")
        pk.write("pkg/a.py", "from pkg.b import bfn\ndef afn():\n    return bfn()\n")
        files = [pk.path(f"pkg/{n}") for n in
                 ("__init__.py", "a.py", "b.py", "c.py", "d.py")]
        reconcile(pk.store, pk.pid, files)
        reconcile_against_disk(pk.store, pk.pid, pk.root)

        seed = ["py::pkg.d.dfn"]
        cone = influence.caller_cone_with_depth(pk.store, pk.pid, seed)
        got = {k.split(".")[-1] for k in cone}
        assert {"cfn", "bfn", "afn"} <= got, got

        # Edit the middle of the chain on the real path; closure must survive.
        _advance_mtime(pk.path("pkg/c.py"))
        pk.write("pkg/c.py", "from pkg.d import dfn\ndef cfn():\n    return dfn() + 1\n")
        _advance_mtime(pk.path("pkg/c.py"))
        _post_edit(pk.store, pk.pid, pk.root, pk.path("pkg/c.py"))
        cone2 = influence.caller_cone_with_depth(pk.store, pk.pid, seed)
        got2 = {k.split(".")[-1] for k in cone2}
        assert {"cfn", "bfn", "afn"} <= got2, got2
    finally:
        pk.close()


def test_caller_count_accurate_after_edit_and_delete():
    """hub called by 6 files: caller_count is 6 after editing hub (real path),
    and drops to 5 after one caller file is deleted."""
    pk = _Pkg()
    try:
        pk.write("pkg/__init__.py", "")
        pk.write("pkg/hub.py", "def hub():\n    return 1\n")
        for i in range(6):
            pk.write(f"pkg/u{i}.py",
                     f"from pkg.hub import hub\ndef u{i}():\n    return hub()\n")
        files = [pk.path("pkg/__init__.py"), pk.path("pkg/hub.py")] + \
                [pk.path(f"pkg/u{i}.py") for i in range(6)]
        reconcile(pk.store, pk.pid, files)
        reconcile_against_disk(pk.store, pk.pid, pk.root)

        det = _latest_det(pk.store, pk.pid, "hub")
        assert pk.store.gd_caller_count(pk.pid, det["id"]) == 6

        _advance_mtime(pk.path("pkg/hub.py"))
        pk.write("pkg/hub.py", "def hub():\n    return 2\n")
        _advance_mtime(pk.path("pkg/hub.py"))
        _post_edit(pk.store, pk.pid, pk.root, pk.path("pkg/hub.py"))
        det2 = _latest_det(pk.store, pk.pid, "hub")
        assert pk.store.gd_caller_count(pk.pid, det2["id"]) == 6

        os.remove(pk.path("pkg/u0.py"))
        reconcile_against_disk(pk.store, pk.pid, pk.root)
        det3 = _latest_det(pk.store, pk.pid, "hub")
        assert pk.store.gd_caller_count(pk.pid, det3["id"]) == 5
    finally:
        pk.close()


def test_debug_localization_cone_intersect_stack():
    """Debug localization: a->b->c with the bug in c; a suspicious change to b
    plus a crash stack through c localizes to b (cone ∩ stack), and remains
    correct after editing b on the real path."""
    pk = _Pkg()
    try:
        pk.write("pkg/__init__.py", "")
        pk.write("pkg/c.py", "def cfn():\n    return 1 / 0\n")
        pk.write("pkg/b.py", "from pkg.c import cfn\ndef bfn():\n    return cfn()\n")
        pk.write("pkg/a.py", "from pkg.b import bfn\ndef afn():\n    return bfn()\n")
        files = [pk.path(f"pkg/{n}") for n in ("__init__.py", "a.py", "b.py", "c.py")]
        reconcile(pk.store, pk.pid, files)
        reconcile_against_disk(pk.store, pk.pid, pk.root)

        crash = {"py::pkg.a.afn", "py::pkg.b.bfn", "py::pkg.c.cfn"}
        changed = ["py::pkg.b.bfn"]
        cone = influence.caller_cone_with_depth(pk.store, pk.pid, changed)
        cone_fqns = set(cone) | set(changed)
        suspects = influence.intersect_cone_with_stack(cone_fqns, crash)
        assert any("bfn" in s for s in suspects), suspects

        _advance_mtime(pk.path("pkg/b.py"))
        pk.write("pkg/b.py", "from pkg.c import cfn\ndef bfn():\n    return cfn() + 0\n")
        _advance_mtime(pk.path("pkg/b.py"))
        _post_edit(pk.store, pk.pid, pk.root, pk.path("pkg/b.py"))
        cone2 = influence.caller_cone_with_depth(pk.store, pk.pid, ["py::pkg.b.bfn"])
        cone_fqns2 = set(cone2) | {"py::pkg.b.bfn"}
        suspects2 = influence.intersect_cone_with_stack(cone_fqns2, crash)
        assert any("bfn" in s for s in suspects2), suspects2
    finally:
        pk.close()
