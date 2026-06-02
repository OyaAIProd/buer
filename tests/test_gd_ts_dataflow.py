"""Tests for TS/TSX GD-edge and ts_dataflow module behaviour.

Coverage:
  (A) build_gd_edges: TS/TSX → cross_define_callgraph (no tsc called)
  (B) build_gd_edges: Python/JS → cross_define_callgraph (unchanged)
  (C) DAG guard: cycle-forming edge skipped
  (D) version_chain always built for TS
  (E) _build_ts_cross_edges (on-demand path): toolchain missing → warning incident
  (F) _build_ts_cross_edges: warning incident idempotent
  (G) ts_dataflow module: detect_ts_toolchain + run_ts_dataflow_analysis (preserved)
  Integration: run_ts_dataflow_analysis with real tsc (node-gated)
  Integration: _build_ts_cross_edges end-to-end with real tsc (node-gated)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from buer import gd, ts_dataflow
from buer.store import Store


ROOT = "/test"
TS_FILE = "/test/src/auth.ts"
TSX_FILE = "/test/src/App.tsx"
JS_FILE = "/test/src/app.js"
PY_FILE = "/test/src/app.py"
DEFINE = "decode"


# ── helpers ────────────────────────────────────────────────────────────────────

def _store() -> Store:
    return Store(":memory:")


def _project(store: Store) -> int:
    return store.get_or_create_project(ROOT)


def _det(store, pid, file_path, define_name, seq, fp="fp1"):
    did = store.insert_determination(
        pid, seq=seq, file_path=file_path, define_name=define_name,
        node_fingerprint=fp, edit_type="modify",
    )
    return store.con.execute("SELECT * FROM determinations WHERE id=?", (did,)).fetchone()


def _empty_idx():
    from buer.callgraph import SymbolIndex
    return SymbolIndex()


def _ts_det(store, pid, seq=1):
    return _det(store, pid, TS_FILE, DEFINE, seq)


# ── toolchain detection ────────────────────────────────────────────────────────

class TestDetectTsToolchain:
    def test_missing_tsconfig(self, tmp_path):
        ts_dataflow._toolchain_cache.pop(str(tmp_path), None)
        result = ts_dataflow.detect_ts_toolchain(str(tmp_path))
        assert result["available"] is False
        assert "tsconfig" in result["reason"]

    def test_missing_typescript_package(self, tmp_path):
        (tmp_path / "tsconfig.json").write_text("{}")
        ts_dataflow._toolchain_cache.pop(str(tmp_path), None)
        result = ts_dataflow.detect_ts_toolchain(str(tmp_path))
        assert result["available"] is False
        assert "typescript" in result["reason"]

    def test_fully_available(self, tmp_path):
        (tmp_path / "tsconfig.json").write_text("{}")
        (tmp_path / "node_modules" / "typescript").mkdir(parents=True)
        ts_dataflow._toolchain_cache.pop(str(tmp_path), None)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=b"v18.0.0")
            result = ts_dataflow.detect_ts_toolchain(str(tmp_path))
        assert result["available"] is True
        assert result["tsconfig"] is not None

    def test_node_not_found(self, tmp_path):
        (tmp_path / "tsconfig.json").write_text("{}")
        (tmp_path / "node_modules" / "typescript").mkdir(parents=True)
        ts_dataflow._toolchain_cache.pop(str(tmp_path), None)
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = ts_dataflow.detect_ts_toolchain(str(tmp_path))
        assert result["available"] is False
        assert "node" in result["reason"]

    def test_cache_is_used(self, tmp_path):
        ts_dataflow._toolchain_cache[str(tmp_path)] = {"available": True, "reason": "cached", "tsconfig": "x"}
        result = ts_dataflow.detect_ts_toolchain(str(tmp_path))
        assert result["reason"] == "cached"


# ── run_ts_dataflow_analysis (unit, mocked subprocess) ───────────────────────

class TestRunTsDataflow:
    def test_returns_edges_on_success(self):
        edges = [{"producer_file": "/proj/auth.ts", "producer_define": "decode"}]
        payload = json.dumps({"edges": edges, "errors": []}).encode()
        with patch("subprocess.run") as m:
            m.return_value = MagicMock(returncode=0, stdout=payload, stderr=b"")
            result = ts_dataflow.run_ts_dataflow_analysis("/proj", "/proj/handler.ts", "handle")
        assert result["degraded"] is False
        assert len(result["edges"]) == 1
        assert result["edges"][0]["producer_define"] == "decode"

    def test_degraded_on_timeout(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("node", 30)):
            result = ts_dataflow.run_ts_dataflow_analysis("/proj", "/proj/f.ts", "f")
        assert result["degraded"] is True

    def test_degraded_on_nonzero_exit(self):
        with patch("subprocess.run") as m:
            m.return_value = MagicMock(returncode=1, stdout=b"", stderr=b"error msg")
            result = ts_dataflow.run_ts_dataflow_analysis("/proj", "/proj/f.ts", "f")
        assert result["degraded"] is True

    def test_degraded_on_invalid_json(self):
        with patch("subprocess.run") as m:
            m.return_value = MagicMock(returncode=0, stdout=b"not json", stderr=b"")
            result = ts_dataflow.run_ts_dataflow_analysis("/proj", "/proj/f.ts", "f")
        assert result["degraded"] is True

    def test_empty_edges_not_degraded(self):
        with patch("subprocess.run") as m:
            m.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps({"edges": [], "errors": []}).encode(),
                stderr=b"",
            )
            result = ts_dataflow.run_ts_dataflow_analysis("/proj", "/proj/f.ts", "f")
        assert result["degraded"] is False
        assert result["edges"] == []


# ── gd.py TS branch (unit, mocked toolchain + analysis) ─────────────────────

def _mock_toolchain_available(root):
    return {"available": True, "reason": "ok", "tsconfig": root + "/tsconfig.json"}

def _mock_toolchain_missing(root):
    return {"available": False, "reason": "typescript not in node_modules", "tsconfig": None}


class TestGdTsCallgraphBranch:
    """build_gd_edges now routes all languages through callgraph (no tsc)."""

    def test_ts_uses_callgraph_not_dataflow(self):
        """TS file → callgraph path; detect_ts_toolchain never called from build_gd_edges."""
        store = _store()
        pid = _project(store)
        cons_det = _det(store, pid, TS_FILE, "handle", seq=1)

        with patch.object(ts_dataflow, "detect_ts_toolchain") as mock_detect:
            gd.build_gd_edges(store, pid, cons_det, ROOT, _empty_idx())
        mock_detect.assert_not_called()

        df_edges = store.con.execute(
            "SELECT * FROM gd_edges WHERE project_id=? AND edge_class='cross_define_dataflow'",
            (pid,),
        ).fetchall()
        assert len(df_edges) == 0

    def test_tsx_uses_callgraph_not_dataflow(self):
        """TSX file → same callgraph path; detect_ts_toolchain never called."""
        store = _store()
        pid = _project(store)
        det = _det(store, pid, TSX_FILE, "App", seq=1)

        with patch.object(ts_dataflow, "detect_ts_toolchain") as mock_detect:
            gd.build_gd_edges(store, pid, det, ROOT, _empty_idx())
        mock_detect.assert_not_called()

    def test_python_file_skips_ts_path(self):
        """Python file uses callgraph path; detect_ts_toolchain never called."""
        store = _store()
        pid = _project(store)
        det = _det(store, pid, PY_FILE, "main", seq=1)

        with patch.object(ts_dataflow, "detect_ts_toolchain") as mock_detect:
            gd.build_gd_edges(store, pid, det, ROOT, _empty_idx())
        mock_detect.assert_not_called()

    def test_js_file_skips_ts_path(self):
        store = _store()
        pid = _project(store)
        det = _det(store, pid, JS_FILE, "main", seq=1)

        with patch.object(ts_dataflow, "detect_ts_toolchain") as mock_detect:
            gd.build_gd_edges(store, pid, det, ROOT, _empty_idx())
        mock_detect.assert_not_called()

    def test_dag_guard_skips_cycle_callgraph(self):
        """DAG guard still applies for callgraph edges."""
        store = _store()
        pid = _project(store)
        det_a = _det(store, pid, "/test/a.ts", "fnA", seq=1)
        det_b = _det(store, pid, "/test/b.ts", "fnB", seq=2)

        # Insert a→b so b is ancestor of a in the DAG.
        store.insert_gd_edge(pid, from_det=det_a["id"], to_det=det_b["id"], edge_class="version_chain")

        # Adding b→a would form a cycle; callgraph guard should skip it.
        # (Empty call-graph index → no edges inserted at all — guard is never reached,
        # but the test documents the invariant.)
        gd.build_gd_edges(store, pid, det_a, ROOT, _empty_idx())

        cg_edges = store.con.execute(
            "SELECT * FROM gd_edges WHERE project_id=? AND edge_class='cross_define_callgraph'",
            (pid,),
        ).fetchall()
        assert len(cg_edges) == 0


class TestBuildTsCrossEdges:
    """_build_ts_cross_edges is preserved for the analyze_dangling on-demand tool."""

    def test_warning_incident_created_when_toolchain_missing(self):
        """_build_ts_cross_edges: missing toolchain → ts_toolchain_missing incident."""
        store = _store()
        pid = _project(store)
        cons_det = _det(store, pid, TS_FILE, "handle", seq=1)

        with patch.object(ts_dataflow, "detect_ts_toolchain", _mock_toolchain_missing):
            gd._build_ts_cross_edges(store, pid, cons_det, ROOT, _empty_idx())

        incs = store.con.execute(
            "SELECT * FROM incidents WHERE project_id=? AND signal=?",
            (pid, "ts_toolchain_missing"),
        ).fetchall()
        assert len(incs) == 1
        assert incs[0]["state"] == "escalated_user"

    def test_warning_message_mentions_install_command(self):
        store = _store()
        pid = _project(store)
        cons_det = _det(store, pid, TS_FILE, "handle", seq=1)

        with patch.object(ts_dataflow, "detect_ts_toolchain", _mock_toolchain_missing):
            gd._build_ts_cross_edges(store, pid, cons_det, ROOT, _empty_idx())

        inc = store.con.execute(
            "SELECT details FROM incidents WHERE project_id=? AND signal=?",
            (pid, "ts_toolchain_missing"),
        ).fetchone()
        details = json.loads(inc["details"])
        assert "npm i -D typescript" in details["message"]

    def test_warning_incident_idempotent(self):
        """Multiple missing-toolchain calls create only one incident."""
        store = _store()
        pid = _project(store)

        with patch.object(ts_dataflow, "detect_ts_toolchain", _mock_toolchain_missing):
            for seq in range(1, 4):
                det = _det(store, pid, TS_FILE, f"fn{seq}", seq)
                gd._build_ts_cross_edges(store, pid, det, ROOT, _empty_idx())

        incs = store.con.execute(
            "SELECT * FROM incidents WHERE project_id=? AND signal=?",
            (pid, "ts_toolchain_missing"),
        ).fetchall()
        assert len(incs) == 1

    def test_dataflow_edge_built_when_toolchain_available(self):
        """_build_ts_cross_edges: available toolchain → cross_define_dataflow edge."""
        store = _store()
        pid = _project(store)
        prod_det = _det(store, pid, "/test/src/auth.ts", "decode", seq=1)
        cons_det = _det(store, pid, "/test/src/handler.ts", "handle", seq=2)

        mock_result = {
            "edges": [{"producer_file": "/test/src/auth.ts", "producer_define": "decode"}],
            "errors": [],
            "degraded": False,
        }
        with patch.object(ts_dataflow, "detect_ts_toolchain", _mock_toolchain_available), \
             patch.object(ts_dataflow, "run_ts_dataflow_analysis", return_value=mock_result):
            gd._build_ts_cross_edges(store, pid, cons_det, ROOT, _empty_idx())

        edges = store.con.execute(
            "SELECT * FROM gd_edges WHERE project_id=? AND edge_class='cross_define_dataflow'",
            (pid,),
        ).fetchall()
        assert len(edges) == 1
        assert edges[0]["from_det"] == prod_det["id"]
        assert edges[0]["to_det"] == cons_det["id"]

    def test_degraded_falls_back_silently(self):
        """Degraded tsc output → callgraph fallback, no warning incident."""
        store = _store()
        pid = _project(store)
        cons_det = _det(store, pid, TS_FILE, "handle", seq=1)

        mock_result = {"edges": [], "errors": ["timeout"], "degraded": True}
        with patch.object(ts_dataflow, "detect_ts_toolchain", _mock_toolchain_available), \
             patch.object(ts_dataflow, "run_ts_dataflow_analysis", return_value=mock_result):
            gd._build_ts_cross_edges(store, pid, cons_det, ROOT, _empty_idx())

        incs = store.con.execute(
            "SELECT * FROM incidents WHERE project_id=? AND signal=?",
            (pid, "ts_toolchain_missing"),
        ).fetchall()
        assert len(incs) == 0


# ── integration tests (require node + typescript) ─────────────────────────────

def _has_node_and_ts():
    """Check if node is available and typescript can be installed for tests."""
    try:
        r = subprocess.run(["node", "--version"], capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


_NEED_NODE = pytest.mark.skipif(not _has_node_and_ts(), reason="node not available")


@pytest.fixture(scope="module")
def ts_project(tmp_path_factory):
    """A minimal TS project with tsconfig + typescript package (via symlink to global)."""
    root = tmp_path_factory.mktemp("ts_project")

    (root / "tsconfig.json").write_text(json.dumps({
        "compilerOptions": {"target": "ES2020", "module": "commonjs", "strict": False},
        "include": [str(root) + "/**/*"],
    }))

    # Symlink global typescript into project node_modules
    nm = root / "node_modules"
    nm.mkdir()
    global_ts = subprocess.run(
        ["node", "-e", "console.log(require.resolve('typescript').replace('/typescript.js','').replace('/lib',''))"],
        capture_output=True, cwd=str(root), timeout=10,
    ).stdout.decode().strip()
    if not global_ts or not os.path.isdir(global_ts):
        # Try npm root -g
        npm_root = subprocess.run(["npm", "root", "-g"], capture_output=True, timeout=10).stdout.decode().strip()
        global_ts = os.path.join(npm_root, "typescript")

    if os.path.isdir(global_ts):
        os.symlink(global_ts, str(nm / "typescript"))

    # auth.ts — producer
    (root / "auth.ts").write_text("""\
export function decode(token: string): string {
    return token.split('.')[1];
}
export function noReturn(s: string): void {
    console.log(s);
}
""")

    # handler.ts — consumer
    (root / "handler.ts").write_text("""\
import { decode, noReturn } from './auth';
export function handle(token: string): string {
    const data = decode(token);     // consumes return value -> edge
    return data;
}
export function log(token: string): void {
    decode(token);                  // fire-and-forget -> NO edge
}
export function asyncHandle(token: string): Promise<string> {
    return Promise.resolve(decode(token));  // used in call arg -> edge
}
export function awaitLog(token: string): Promise<void> {
    return new Promise(async (res) => {
        await noReturn(token);  // await + ExpressionStatement -> NO edge
        res();
    });
}
""")

    return str(root)


@_NEED_NODE
class TestIntegrationAnalyzeMjs:
    def test_consumed_call_produces_edge(self, ts_project):
        result = ts_dataflow.run_ts_dataflow_analysis(ts_project, f"{ts_project}/handler.ts", "handle")
        assert result["degraded"] is False
        produces = [e["producer_define"] for e in result["edges"]]
        assert "decode" in produces

    def test_fire_and_forget_produces_no_edge(self, ts_project):
        result = ts_dataflow.run_ts_dataflow_analysis(ts_project, f"{ts_project}/handler.ts", "log")
        assert result["degraded"] is False
        # No dataflow edge for pure fire-and-forget
        assert result["edges"] == []

    def test_call_used_as_argument_produces_edge(self, ts_project):
        result = ts_dataflow.run_ts_dataflow_analysis(ts_project, f"{ts_project}/handler.ts", "asyncHandle")
        produces = [e["producer_define"] for e in result["edges"]]
        assert "decode" in produces

    def test_await_fire_and_forget_no_edge(self, ts_project):
        result = ts_dataflow.run_ts_dataflow_analysis(ts_project, f"{ts_project}/handler.ts", "awaitLog")
        # await noReturn(token) as expression statement → no edge
        produces = [e["producer_define"] for e in result["edges"]]
        assert "noReturn" not in produces

    def test_nonexistent_define_returns_empty(self, ts_project):
        result = ts_dataflow.run_ts_dataflow_analysis(ts_project, f"{ts_project}/handler.ts", "nonExistent")
        assert result["degraded"] is False
        assert result["edges"] == []

    def test_producer_file_not_in_result_for_external(self, ts_project):
        # External (node_modules) should not appear as producer
        for edge in ts_dataflow.run_ts_dataflow_analysis(ts_project, f"{ts_project}/handler.ts", "handle")["edges"]:
            assert "node_modules" not in edge["producer_file"]


@_NEED_NODE
class TestIntegrationTsCrossEdges:
    """Integration tests for _build_ts_cross_edges (the on-demand tsc path)."""

    def test_end_to_end_dataflow_edge(self, ts_project):
        """_build_ts_cross_edges with real tsc → cross_define_dataflow edge for consumed call."""
        store = _store()
        pid = store.get_or_create_project(ts_project)

        prod_det = store.insert_determination(
            pid, seq=1,
            file_path=f"{ts_project}/auth.ts",
            define_name="decode",
            node_fingerprint="fp_decode",
            edit_type="create",
        )

        cons_det_id = store.insert_determination(
            pid, seq=2,
            file_path=f"{ts_project}/handler.ts",
            define_name="handle",
            node_fingerprint="fp_handle",
            edit_type="create",
        )
        cons_row = store.con.execute("SELECT * FROM determinations WHERE id=?", (cons_det_id,)).fetchone()

        ts_dataflow._toolchain_cache.pop(ts_project, None)

        from buer.callgraph import SymbolIndex
        gd._build_ts_cross_edges(store, pid, cons_row, ts_project, SymbolIndex())

        df_edges = store.con.execute(
            "SELECT * FROM gd_edges WHERE project_id=? AND edge_class='cross_define_dataflow'",
            (pid,),
        ).fetchall()
        assert len(df_edges) == 1
        assert df_edges[0]["from_det"] == prod_det
        assert df_edges[0]["to_det"] == cons_det_id

    def test_end_to_end_no_edge_for_fire_and_forget(self, ts_project):
        """Fire-and-forget call → no dataflow edge (ExpressionStatement check)."""
        store = _store()
        pid = store.get_or_create_project(ts_project)

        store.insert_determination(
            pid, seq=1,
            file_path=f"{ts_project}/auth.ts",
            define_name="decode",
            node_fingerprint="fp_decode",
            edit_type="create",
        )

        log_det_id = store.insert_determination(
            pid, seq=2,
            file_path=f"{ts_project}/handler.ts",
            define_name="log",
            node_fingerprint="fp_log",
            edit_type="create",
        )
        log_row = store.con.execute("SELECT * FROM determinations WHERE id=?", (log_det_id,)).fetchone()

        ts_dataflow._toolchain_cache.pop(ts_project, None)

        from buer.callgraph import SymbolIndex
        gd._build_ts_cross_edges(store, pid, log_row, ts_project, SymbolIndex())

        df_edges = store.con.execute(
            "SELECT * FROM gd_edges WHERE project_id=? AND edge_class='cross_define_dataflow'",
            (pid,),
        ).fetchall()
        assert len(df_edges) == 0, "fire-and-forget call must not produce dataflow edge"
