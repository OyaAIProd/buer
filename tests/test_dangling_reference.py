"""Tests for dangling_reference signal (§2.8) — 9th signal.

Verification coverage (A–H from design spec):
  (A) TS define calls readExcell (not in project, not library) → across 2 edits
      → report with similar "readExcel" suggestion
  (B) Single-edit occurrence → no report (persistence gate θ₁=2)
  (C) node_modules library call → not in unresolved_calls (TS resolves symbol)
  (D) Python/JS project → no report (dynamic language boundary)
  (E) TS without toolchain → no report (no unresolved_calls from gd layer)
  (F) Nested function unresolved call → detected (full traversal)
  (G) callee gets defined → resolved (_signal_recurred returns False)
  (H) Wording is a question; standard two-step (not direct-escalate)
"""
from __future__ import annotations

import json
import os
import subprocess
from unittest.mock import patch

import pytest

from buer import gd, signals, ts_dataflow
from buer.signals import (
    THETA_1_DANGLING,
    _levenshtein,
    _similar_defines,
    detect_dangling_reference,
)
from buer.store import Store


# ── helpers ────────────────────────────────────────────────────────────────────

def _store() -> Store:
    return Store(":memory:")


def _project(store: Store, root: str = "/test") -> int:
    return store.get_or_create_project(root)


def _det(store, pid, file_path, define_name, seq, fp=None):
    fp = fp or f"fp_{define_name}_{seq}"
    did = store.insert_determination(
        pid, seq=seq, file_path=file_path, define_name=define_name,
        node_fingerprint=fp, edit_type="modify",
    )
    return store.con.execute("SELECT * FROM determinations WHERE id=?", (did,)).fetchone()


def _open_dangling(store, pid):
    return [
        i for i in store.open_incidents(pid)
        if i["signal"] == "dangling_reference"
    ]


TS_FILE = "/test/src/handler.ts"
ROOT = "/test"


# ── Levenshtein + similarity ───────────────────────────────────────────────────

class TestLevenshtein:
    def test_equal(self):
        assert _levenshtein("foo", "foo") == 0

    def test_empty(self):
        assert _levenshtein("", "abc") == 3
        assert _levenshtein("abc", "") == 3

    def test_insertion(self):
        assert _levenshtein("cat", "cats") == 1

    def test_substitution(self):
        assert _levenshtein("readExcell", "readExcel") == 1

    def test_two_edits(self):
        assert _levenshtein("readExcell", "readExcel") == 1
        assert _levenshtein("getUsre", "getUser") == 2

    def test_beyond_threshold(self):
        assert _levenshtein("completely", "different") > 2


class TestSimilarDefines:
    def _rows(self, names):
        return [{"define_name": n} for n in names]

    def test_exact_not_returned(self):
        result = _similar_defines("readExcel", self._rows(["readExcel"]))
        assert result == []

    def test_dist1_returned(self):
        result = _similar_defines("readExcell", self._rows(["readExcel", "writeFile"]))
        assert "readExcel" in result

    def test_dist2_returned(self):
        result = _similar_defines("getUsre", self._rows(["getUser", "setUser"]))
        assert "getUser" in result

    def test_beyond_dist2_not_returned_unless_substring(self):
        result = _similar_defines("xyz", self._rows(["longFunctionName"]))
        assert result == []

    def test_substring_returned(self):
        result = _similar_defines("read", self._rows(["readExcelFile"]))
        assert "readExcelFile" in result

    def test_class_method_bare_name(self):
        result = _similar_defines("readExcell", self._rows(["Parser.readExcel"]))
        assert "Parser.readExcel" in result

    def test_returns_at_most_3(self):
        names = [f"readExcel{i}" for i in range(5)]
        result = _similar_defines("readExcel", self._rows(names))
        assert len(result) <= 3

    def test_sorted_closest_first(self):
        rows = self._rows(["readExcel", "readExcelll"])
        result = _similar_defines("readExcell", rows)
        assert result[0] == "readExcel"  # dist=1 < dist=2


# ── store.record_dangling_observation ─────────────────────────────────────────

class TestDanglingObservations:
    def test_first_observation_returns_1(self):
        store = _store()
        pid = _project(store)
        n = store.record_dangling_observation(pid, TS_FILE, "handle", "readExcell", det_id=1)
        assert n == 1

    def test_second_det_returns_2(self):
        store = _store()
        pid = _project(store)
        store.record_dangling_observation(pid, TS_FILE, "handle", "readExcell", det_id=1)
        n = store.record_dangling_observation(pid, TS_FILE, "handle", "readExcell", det_id=2)
        assert n == 2

    def test_same_det_idempotent(self):
        store = _store()
        pid = _project(store)
        store.record_dangling_observation(pid, TS_FILE, "handle", "readExcell", det_id=1)
        n = store.record_dangling_observation(pid, TS_FILE, "handle", "readExcell", det_id=1)
        assert n == 1  # UNIQUE constraint prevents double-count

    def test_different_callee_tracked_separately(self):
        store = _store()
        pid = _project(store)
        store.record_dangling_observation(pid, TS_FILE, "handle", "readExcell", det_id=1)
        n = store.record_dangling_observation(pid, TS_FILE, "handle", "writeExcell", det_id=1)
        assert n == 1  # separate callee_text

    def test_different_caller_define_tracked_separately(self):
        store = _store()
        pid = _project(store)
        store.record_dangling_observation(pid, TS_FILE, "handle", "foo", det_id=1)
        n = store.record_dangling_observation(pid, TS_FILE, "process", "foo", det_id=1)
        assert n == 1  # separate caller_define


# ── detect_dangling_reference (unit, mocked unresolved_calls) ─────────────────

class TestDetectDanglingReference:
    def _call(self, file=None, caller="handle", callee="readExcell"):
        return {
            "caller_define": caller,
            "callee_text": callee,
            "file": os.path.abspath(file or TS_FILE),
            "line": 5,
        }

    def test_no_report_on_first_occurrence(self):
        """Single occurrence (obs_count=1 < θ₁=2) → no incident. (B)"""
        store = _store()
        pid = _project(store)
        det1 = _det(store, pid, TS_FILE, "handle", seq=1)

        detect_dangling_reference(
            store, pid,
            [(TS_FILE, "handle", det1["id"])],
            ROOT,
            [self._call()],
        )
        assert _open_dangling(store, pid) == []

    def test_report_on_second_occurrence(self):
        """Two observations (θ₁=2 met) → incident created. (A)"""
        store = _store()
        pid = _project(store)
        det1 = _det(store, pid, TS_FILE, "handle", seq=1)
        det2 = _det(store, pid, TS_FILE, "handle", seq=2)

        # First pass
        detect_dangling_reference(
            store, pid, [(TS_FILE, "handle", det1["id"])], ROOT, [self._call()]
        )
        # Second pass — new det_id
        detect_dangling_reference(
            store, pid, [(TS_FILE, "handle", det2["id"])], ROOT, [self._call()]
        )
        incs = _open_dangling(store, pid)
        assert len(incs) == 1

    def test_target_node_format(self):
        store = _store()
        pid = _project(store)
        det1 = _det(store, pid, TS_FILE, "handle", seq=1)
        det2 = _det(store, pid, TS_FILE, "handle", seq=2)
        for d in (det1, det2):
            detect_dangling_reference(
                store, pid, [(TS_FILE, "handle", d["id"])], ROOT, [self._call()]
            )
        inc = _open_dangling(store, pid)[0]
        assert inc["target_node"] == f"{TS_FILE}::handle::readExcell"

    def test_details_contain_callee_text(self):
        store = _store()
        pid = _project(store)
        det1 = _det(store, pid, TS_FILE, "handle", seq=1)
        det2 = _det(store, pid, TS_FILE, "handle", seq=2)
        for d in (det1, det2):
            detect_dangling_reference(
                store, pid, [(TS_FILE, "handle", d["id"])], ROOT, [self._call()]
            )
        details = json.loads(_open_dangling(store, pid)[0]["details"])
        assert details["callee_text"] == "readExcell"

    def test_details_question_is_interrogative(self):
        """Details message must be phrased as a question (§2.8). (H)"""
        store = _store()
        pid = _project(store)
        det1 = _det(store, pid, TS_FILE, "handle", seq=1)
        det2 = _det(store, pid, TS_FILE, "handle", seq=2)
        for d in (det1, det2):
            detect_dangling_reference(
                store, pid, [(TS_FILE, "handle", d["id"])], ROOT, [self._call()]
            )
        details = json.loads(_open_dangling(store, pid)[0]["details"])
        assert "?" in details["question"]

    def test_similar_define_suggestion_in_details(self):
        """When readExcel exists in project, readExcell typo gets suggestion. (A)"""
        store = _store()
        pid = _project(store)
        # Insert a known define "readExcel" in the project
        _det(store, pid, "/test/src/excel.ts", "readExcel", seq=1)

        det1 = _det(store, pid, TS_FILE, "handle", seq=2)
        det2 = _det(store, pid, TS_FILE, "handle", seq=3)
        for d in (det1, det2):
            detect_dangling_reference(
                store, pid, [(TS_FILE, "handle", d["id"])], ROOT, [self._call()]
            )
        details = json.loads(_open_dangling(store, pid)[0]["details"])
        assert "readExcel" in details["question"]
        assert "readExcel" in details.get("similar_defines", [])

    def test_no_report_if_callee_already_defined(self):
        """When callee is defined, TS doesn't report it as unresolved → unresolved_calls=[] → no incident."""
        store = _store()
        pid = _project(store)
        _det(store, pid, "/test/src/utils.ts", "readExcell", seq=1)  # defined!
        det1 = _det(store, pid, TS_FILE, "handle", seq=2)
        det2 = _det(store, pid, TS_FILE, "handle", seq=3)
        for d in (det1, det2):
            # TS already resolved the call → no unresolved_calls from gd layer
            detect_dangling_reference(
                store, pid, [(TS_FILE, "handle", d["id"])], ROOT, []
            )
        assert _open_dangling(store, pid) == []

    def test_no_report_for_empty_unresolved_calls(self):
        """Empty unresolved_calls → no incident (Python/JS, toolchain-missing TS). (D)(E)"""
        store = _store()
        pid = _project(store)
        det = _det(store, pid, TS_FILE, "handle", seq=1)
        detect_dangling_reference(store, pid, [(TS_FILE, "handle", det["id"])], ROOT, [])
        assert _open_dangling(store, pid) == []

    def test_no_report_if_caller_not_in_affected(self):
        """unresolved_calls for a define NOT in affected → no observation. (caller not edited)"""
        store = _store()
        pid = _project(store)
        det = _det(store, pid, TS_FILE, "otherFunc", seq=1)
        # call from "handle" but affected only has "otherFunc"
        detect_dangling_reference(
            store, pid,
            [(TS_FILE, "otherFunc", det["id"])],
            ROOT,
            [self._call(caller="handle")],   # "handle" not in affected
        )
        assert _open_dangling(store, pid) == []

    def test_idempotent_no_duplicate_incidents(self):
        """Third pass with open incident → no second incident created."""
        store = _store()
        pid = _project(store)
        dets = [_det(store, pid, TS_FILE, "handle", seq=i) for i in range(1, 4)]
        for d in dets:
            detect_dangling_reference(
                store, pid, [(TS_FILE, "handle", d["id"])], ROOT, [self._call()]
            )
        assert len(_open_dangling(store, pid)) == 1

    def test_standard_two_step_not_direct_escalate(self):
        """Incident starts as 'open', NOT 'escalated_user'. (H)"""
        store = _store()
        pid = _project(store)
        det1 = _det(store, pid, TS_FILE, "handle", seq=1)
        det2 = _det(store, pid, TS_FILE, "handle", seq=2)
        for d in (det1, det2):
            detect_dangling_reference(
                store, pid, [(TS_FILE, "handle", d["id"])], ROOT, [self._call()]
            )
        inc = _open_dangling(store, pid)[0]
        assert inc["state"] == "open"
        assert not json.loads(inc["details"] or "{}").get("escalate_user_directly")

    def test_multiple_unresolved_in_same_define(self):
        """Two different unresolved callees from same define are tracked separately."""
        store = _store()
        pid = _project(store)
        det1 = _det(store, pid, TS_FILE, "handle", seq=1)
        det2 = _det(store, pid, TS_FILE, "handle", seq=2)
        calls = [self._call(callee="readExcell"), self._call(callee="writeXlss")]
        for d in (det1, det2):
            detect_dangling_reference(
                store, pid, [(TS_FILE, "handle", d["id"])], ROOT, calls
            )
        incs = _open_dangling(store, pid)
        callee_texts = {json.loads(i["details"])["callee_text"] for i in incs}
        assert callee_texts == {"readExcell", "writeXlss"}


# ── _signal_recurred for dangling_reference ────────────────────────────────────

class TestDanglingRecurrence:
    def _make_incident(self, store, pid, file_path, caller, callee, question="?"):
        inc_id = store.write_incident(
            pid,
            signal="dangling_reference",
            target_node=f"{file_path}::{caller}::{callee}",
            details=json.dumps({
                "file_path": file_path,
                "caller_define": caller,
                "callee_text": callee,
                "similar_defines": [],
                "question": question,
                "consecutive_stable": 0,
            }),
        )
        return store.con.execute("SELECT * FROM incidents WHERE id=?", (inc_id,)).fetchone()

    def _toolchain_ok(self):
        return {"available": True, "reason": "ok", "tsconfig": "/test/tsconfig.json"}

    def test_recurred_when_callee_not_defined(self):
        store = _store()
        pid = _project(store)
        _det(store, pid, TS_FILE, "handle", seq=1)
        inc = self._make_incident(store, pid, TS_FILE, "handle", "readExcell")
        mock_result = {
            "unresolved_calls": [{"callee_text": "readExcell", "caller_define": "handle",
                                  "file": TS_FILE, "line": 5}],
            "degraded": False,
        }
        with patch.object(ts_dataflow, "detect_ts_toolchain", return_value=self._toolchain_ok()), \
             patch.object(ts_dataflow, "run_ts_dataflow_analysis", return_value=mock_result):
            assert signals._signal_recurred(store, pid, inc, [(TS_FILE, "handle", 1)]) is True

    def test_not_recurred_when_callee_defined(self):
        """(G) TS no longer reports callee as unresolved (callee got defined) → resolve."""
        store = _store()
        pid = _project(store)
        _det(store, pid, TS_FILE, "handle", seq=1)
        _det(store, pid, "/test/utils.ts", "readExcell", seq=2)  # now defined!
        inc = self._make_incident(store, pid, TS_FILE, "handle", "readExcell")
        mock_result = {"unresolved_calls": [], "degraded": False}
        with patch.object(ts_dataflow, "detect_ts_toolchain", return_value=self._toolchain_ok()), \
             patch.object(ts_dataflow, "run_ts_dataflow_analysis", return_value=mock_result):
            assert signals._signal_recurred(store, pid, inc, []) is False

    def test_not_recurred_when_callee_defined_as_method(self):
        """Callee defined as MyClass.readExcell → TS resolves it → unresolved_calls empty → not recurred."""
        store = _store()
        pid = _project(store)
        _det(store, pid, TS_FILE, "handle", seq=1)
        _det(store, pid, "/test/utils.ts", "MyClass.readExcell", seq=2)
        inc = self._make_incident(store, pid, TS_FILE, "handle", "readExcell")
        mock_result = {"unresolved_calls": [], "degraded": False}
        with patch.object(ts_dataflow, "detect_ts_toolchain", return_value=self._toolchain_ok()), \
             patch.object(ts_dataflow, "run_ts_dataflow_analysis", return_value=mock_result):
            assert signals._signal_recurred(store, pid, inc, []) is False

    def test_same_name_different_module_does_not_false_resolve(self):
        """A function with same bare name in different module must not resolve if TS still reports unresolved."""
        store = _store()
        pid = _project(store)
        _det(store, pid, TS_FILE, "handle", seq=1)
        # Different module has "readExcell" — but the TS call is to a different symbol
        _det(store, pid, "/other/module.ts", "readExcell", seq=2)
        inc = self._make_incident(store, pid, TS_FILE, "handle", "readExcell")
        # TS still reports the call as unresolved (module import path doesn't resolve)
        mock_result = {
            "unresolved_calls": [{"callee_text": "readExcell", "caller_define": "handle",
                                  "file": TS_FILE, "line": 5}],
            "degraded": False,
        }
        with patch.object(ts_dataflow, "detect_ts_toolchain", return_value=self._toolchain_ok()), \
             patch.object(ts_dataflow, "run_ts_dataflow_analysis", return_value=mock_result):
            assert signals._signal_recurred(store, pid, inc, []) is True

    def test_not_recurred_when_caller_deleted(self):
        """Caller define deleted → resolve immediately."""
        store = _store()
        pid = _project(store)
        # Don't insert caller define — it doesn't exist
        inc = self._make_incident(store, pid, TS_FILE, "handle", "readExcell")
        assert signals._signal_recurred(store, pid, inc, []) is False

    def test_resolve_reason(self):
        from buer.signals import _resolve_reason
        assert _resolve_reason("dangling_reference") == "defined_or_removed"

    def test_theta_2_entry_exists(self):
        from buer.signals import THETA_2
        assert "dangling_reference" in THETA_2

    def test_advance_incidents_moves_to_notified(self):
        """Standard first advance: open → notified_agent (two-step). (H)"""
        store = _store()
        pid = _project(store)
        _det(store, pid, TS_FILE, "handle", seq=1)  # caller still exists
        inc_id = store.write_incident(
            pid,
            signal="dangling_reference",
            target_node=f"{TS_FILE}::handle::readExcell",
            details=json.dumps({
                "file_path": TS_FILE,
                "caller_define": "handle",
                "callee_text": "readExcell",
                "similar_defines": [],
                "question": "readExcell has no definition in the project — possible typo or missing definition?",
                "consecutive_stable": 0,
            }),
        )
        signals.advance_incidents(store, pid, [(TS_FILE, "handle", 1)])
        inc = store.con.execute("SELECT * FROM incidents WHERE id=?", (inc_id,)).fetchone()
        assert inc["state"] == "notified_agent"

    def test_advance_incidents_resolves_when_callee_defined(self):
        """After callee is defined, notified → resolve via N_STABLE countdown."""
        store = _store()
        pid = _project(store)
        _det(store, pid, TS_FILE, "handle", seq=1)
        _det(store, pid, "/test/utils.ts", "readExcell", seq=2)  # now defined!
        inc_id = store.write_incident(
            pid,
            signal="dangling_reference",
            target_node=f"{TS_FILE}::handle::readExcell",
            details=json.dumps({
                "file_path": TS_FILE,
                "caller_define": "handle",
                "callee_text": "readExcell",
                "similar_defines": [],
                "question": "?",
                "consecutive_stable": 0,
            }),
        )
        mock_result = {"unresolved_calls": [], "degraded": False}
        with patch.object(ts_dataflow, "detect_ts_toolchain",
                          return_value=self._toolchain_ok()), \
             patch.object(ts_dataflow, "run_ts_dataflow_analysis", return_value=mock_result):
            # Advance to notified_agent (open → notified; _signal_recurred not called yet)
            signals.advance_incidents(store, pid, [])
            inc = store.con.execute("SELECT * FROM incidents WHERE id=?", (inc_id,)).fetchone()
            assert inc["state"] == "notified_agent"
            # N_STABLE rounds without recurrence → resolved
            for _ in range(signals.N_STABLE):
                signals.advance_incidents(store, pid, [])
        inc = store.con.execute("SELECT * FROM incidents WHERE id=?", (inc_id,)).fetchone()
        assert inc["state"] == "resolved"
        assert inc["resolved_by"] == "defined_or_removed"


# ── gd.py return type ─────────────────────────────────────────────────────────

class TestBuildTsCrossEdgesReturnType:
    """_build_ts_cross_edges (on-demand tsc path) returns unresolved_calls list."""

    def test_returns_list_for_ts(self):
        from buer.callgraph import SymbolIndex
        store = _store()
        pid = _project(store)
        det = _det(store, pid, TS_FILE, "handle", seq=1)
        mock_result = {"edges": [], "errors": [], "unresolved_calls": [], "degraded": False}
        with patch.object(ts_dataflow, "detect_ts_toolchain",
                          return_value={"available": True, "reason": "ok", "tsconfig": "/test/tsconfig.json"}), \
             patch.object(ts_dataflow, "run_ts_dataflow_analysis", return_value=mock_result):
            result = gd._build_ts_cross_edges(store, pid, det, ROOT, SymbolIndex())
        assert isinstance(result, list)

    def test_returns_unresolved_calls_from_analysis(self):
        """unresolved_calls from analyze.mjs propagate through _build_ts_cross_edges."""
        from buer.callgraph import SymbolIndex
        store = _store()
        pid = _project(store)
        det = _det(store, pid, TS_FILE, "handle", seq=1)
        mock_unresolved = [{"caller_define": "handle", "callee_text": "readExcell",
                            "file": TS_FILE, "line": 3}]
        mock_result = {"edges": [], "errors": [], "unresolved_calls": mock_unresolved, "degraded": False}
        with patch.object(ts_dataflow, "detect_ts_toolchain",
                          return_value={"available": True, "reason": "ok", "tsconfig": "/test/tsconfig.json"}), \
             patch.object(ts_dataflow, "run_ts_dataflow_analysis", return_value=mock_result):
            result = gd._build_ts_cross_edges(store, pid, det, ROOT, SymbolIndex())
        assert result == mock_unresolved

    def test_returns_empty_list_when_toolchain_missing(self):
        """No toolchain → callgraph fallback → empty list. (E)"""
        from buer.callgraph import SymbolIndex
        store = _store()
        pid = _project(store)
        det = _det(store, pid, TS_FILE, "handle", seq=1)
        with patch.object(ts_dataflow, "detect_ts_toolchain",
                          return_value={"available": False, "reason": "no tsconfig.json", "tsconfig": None}):
            result = gd._build_ts_cross_edges(store, pid, det, ROOT, SymbolIndex())
        assert result == []

    def test_returns_empty_list_when_degraded(self):
        """Script degraded → callgraph fallback → empty list."""
        from buer.callgraph import SymbolIndex
        store = _store()
        pid = _project(store)
        det = _det(store, pid, TS_FILE, "handle", seq=1)
        mock_result = {"edges": [], "errors": ["timeout"], "unresolved_calls": [], "degraded": True}
        with patch.object(ts_dataflow, "detect_ts_toolchain",
                          return_value={"available": True, "reason": "ok", "tsconfig": "/test/tsconfig.json"}), \
             patch.object(ts_dataflow, "run_ts_dataflow_analysis", return_value=mock_result):
            result = gd._build_ts_cross_edges(store, pid, det, ROOT, SymbolIndex())
        assert result == []


# ── integration tests (require node + typescript) ─────────────────────────────

def _has_node():
    try:
        r = subprocess.run(["node", "--version"], capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


_NEED_NODE = pytest.mark.skipif(not _has_node(), reason="node not available")


@pytest.fixture(scope="module")
def ts_dangling_project(tmp_path_factory):
    """TS project with a define that calls an undefined function."""
    root = tmp_path_factory.mktemp("ts_dangling")

    (root / "tsconfig.json").write_text(json.dumps({
        "compilerOptions": {"target": "ES2020", "module": "commonjs", "strict": False},
        "include": [str(root) + "/**/*"],
    }))

    nm = root / "node_modules"
    nm.mkdir()
    global_ts = subprocess.run(
        ["node", "-e",
         "console.log(require.resolve('typescript').replace('/typescript.js','').replace('/lib',''))"],
        capture_output=True, cwd=str(root), timeout=10,
    ).stdout.decode().strip()
    if not global_ts or not os.path.isdir(global_ts):
        npm_root = subprocess.run(
            ["npm", "root", "-g"], capture_output=True, timeout=10
        ).stdout.decode().strip()
        global_ts = os.path.join(npm_root, "typescript")
    if os.path.isdir(global_ts):
        os.symlink(global_ts, str(nm / "typescript"))

    # handler.ts — has a call to undefined readExcell (typo, should be readExcel)
    (root / "handler.ts").write_text("""\
export function handle(token: string): string {
    const data = readExcell(token);   // readExcell is not defined anywhere
    return data;
}
export function withNested(x: string): string {
    function inner(y: string): string {
        const r = readExcell(y);      // nested function — should still be detected
        return r;
    }
    return inner(x);
}
export function useConsole(s: string): void {
    console.log(s);   // console.log → stdlib, should NOT be flagged
}
""")

    # excel.ts — defines the correctly-spelled readExcel
    (root / "excel.ts").write_text("""\
export function readExcel(path: string): string[] {
    return [path];
}
""")

    return str(root)


@_NEED_NODE
class TestIntegrationUnresolvedCalls:
    def test_unresolved_call_detected(self, ts_dangling_project):
        result = ts_dataflow.run_ts_dataflow_analysis(
            ts_dangling_project, f"{ts_dangling_project}/handler.ts", "handle"
        )
        assert result["degraded"] is False
        callees = [u["callee_text"] for u in result.get("unresolved_calls", [])]
        assert "readExcell" in callees

    def test_stdlib_call_not_flagged(self, ts_dangling_project):
        """console.log is typed in lib.d.ts → should NOT appear in unresolved_calls. (C)"""
        result = ts_dataflow.run_ts_dataflow_analysis(
            ts_dangling_project, f"{ts_dangling_project}/handler.ts", "useConsole"
        )
        assert result["degraded"] is False
        callees = [u["callee_text"] for u in result.get("unresolved_calls", [])]
        assert "log" not in callees

    def test_nested_function_unresolved_detected(self, ts_dangling_project):
        """Unresolved call inside nested function body is detected. (F)"""
        result = ts_dataflow.run_ts_dataflow_analysis(
            ts_dangling_project, f"{ts_dangling_project}/handler.ts", "withNested"
        )
        assert result["degraded"] is False
        callees = [u["callee_text"] for u in result.get("unresolved_calls", [])]
        assert "readExcell" in callees

    def test_caller_define_in_result(self, ts_dangling_project):
        result = ts_dataflow.run_ts_dataflow_analysis(
            ts_dangling_project, f"{ts_dangling_project}/handler.ts", "handle"
        )
        for u in result.get("unresolved_calls", []):
            assert u["caller_define"] == "handle"

    def test_file_path_is_absolute(self, ts_dangling_project):
        result = ts_dataflow.run_ts_dataflow_analysis(
            ts_dangling_project, f"{ts_dangling_project}/handler.ts", "handle"
        )
        for u in result.get("unresolved_calls", []):
            assert os.path.isabs(u["file"])

    def test_line_number_positive(self, ts_dangling_project):
        result = ts_dataflow.run_ts_dataflow_analysis(
            ts_dangling_project, f"{ts_dangling_project}/handler.ts", "handle"
        )
        for u in result.get("unresolved_calls", []):
            assert u["line"] >= 1


@_NEED_NODE
class TestIntegrationEndToEnd:
    def test_full_dangling_reference_pipeline(self, ts_dangling_project):
        """End-to-end via _build_ts_cross_edges: two edits → dangling_reference incident. (A)"""
        from buer.callgraph import SymbolIndex
        store = Store(":memory:")
        pid = store.get_or_create_project(ts_dangling_project)
        ts_dataflow._toolchain_cache.pop(ts_dangling_project, None)

        file_path = f"{ts_dangling_project}/handler.ts"

        # First edit
        det1_id = store.insert_determination(
            pid, seq=1, file_path=file_path, define_name="handle",
            node_fingerprint="fp1", edit_type="create",
        )
        det1 = store.con.execute("SELECT * FROM determinations WHERE id=?", (det1_id,)).fetchone()
        unresolved1 = gd._build_ts_cross_edges(store, pid, det1, ts_dangling_project, SymbolIndex())
        detect_dangling_reference(store, pid, [(file_path, "handle", det1_id)],
                                  ts_dangling_project, unresolved1)
        assert _open_dangling(store, pid) == []  # (B) single edit → no report

        # Second edit (same unresolved call persists)
        det2_id = store.insert_determination(
            pid, seq=2, file_path=file_path, define_name="handle",
            node_fingerprint="fp2", edit_type="modify",
        )
        det2 = store.con.execute("SELECT * FROM determinations WHERE id=?", (det2_id,)).fetchone()
        unresolved2 = gd._build_ts_cross_edges(store, pid, det2, ts_dangling_project, SymbolIndex())
        detect_dangling_reference(store, pid, [(file_path, "handle", det2_id)],
                                  ts_dangling_project, unresolved2)

        incs = _open_dangling(store, pid)
        assert len(incs) == 1  # (A) two edits → report
        details = json.loads(incs[0]["details"])
        assert details["callee_text"] == "readExcell"
        # Suggestion: readExcel is in the project (dist=1)
        assert "readExcel" in details["question"]

    def test_no_report_for_correctly_spelled_call(self, ts_dangling_project):
        """A call to a defined function must not trigger dangling_reference."""
        from buer.callgraph import SymbolIndex
        store = Store(":memory:")
        pid = store.get_or_create_project(ts_dangling_project)
        ts_dataflow._toolchain_cache.pop(ts_dangling_project, None)

        # First register readExcel as a known define
        store.insert_determination(
            pid, seq=1,
            file_path=f"{ts_dangling_project}/excel.ts",
            define_name="readExcel",
            node_fingerprint="fp_excel",
            edit_type="create",
        )

        file_path = f"{ts_dangling_project}/handler.ts"
        for seq in (2, 3):
            det_id = store.insert_determination(
                pid, seq=seq, file_path=file_path, define_name="useConsole",
                node_fingerprint=f"fp{seq}", edit_type="modify",
            )
            det = store.con.execute("SELECT * FROM determinations WHERE id=?", (det_id,)).fetchone()
            unresolved = gd._build_ts_cross_edges(store, pid, det, ts_dangling_project, SymbolIndex())
            detect_dangling_reference(
                store, pid, [(file_path, "useConsole", det_id)],
                ts_dangling_project, unresolved,
            )
        assert _open_dangling(store, pid) == []
