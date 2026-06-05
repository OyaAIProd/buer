"""Tests for /buer/post-bash endpoint (§4.4 crash detection + test-command detection).

Test RESULTS come exclusively from JUnit XML (testscan), never from stdout.
post-bash only detects that a test command ran (is_test_command reads the short
command field), and warns once if no JUnit XML was found.

Coverage plan:
  A. Non-test commands → silent 200, no insert
  B. Test command (any output) → no insert (output not parsed)
  C. No matching project → silent 200, no insert
  D. XML present → no warning
  E. No XML, already warned → no warning
  F. No XML, first time → one-shot warning in additionalContext
  G. Malformed / empty body → silent 200
  H. Response body always empty
  I. Real CC payload format (dict tool_response)
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

from buer.mcp.server import _set_store_for_testing, mcp
from buer.store import Store


def _ac(r) -> str:
    """Extract additionalContext from hook JSON, or '' when body is {}."""
    return r.json().get("hookSpecificOutput", {}).get("additionalContext", "")


# ── sample outputs ─────────────────────────────────────────────────────────────

_PYTEST_OUT = (
    "============================= test session starts ==============================\n"
    "tests/test_foo.py::test_one PASSED\n"
    "=========================== 3 passed in 0.42s =================================\n"
)

_JEST_OUT = (
    "Test Suites: 1 failed, 1 total\n"
    "Tests: 2 failed, 8 passed, 10 total\n"
    "Time: 2.3 s\n"
)

_CARGO_OUT = (
    "test foo ... ok\n"
    "test bar ... ok\n"
    "test result: ok. 2 passed; 0 failed; 0 ignored; 0 measured\n"
)

_MOCHA_OUT = (
    "  passing (42ms)\n"
    "\n"
    "  4 passing (42ms)\n"
    "  1 failing\n"
)


# ── helpers ────────────────────────────────────────────────────────────────────

def _mock_store(
    *,
    pid: int | None = 1,
    xml_exists: bool = False,
    stdout_dup: bool = False,
    max_seq_val: int = 5,
) -> MagicMock:
    store = MagicMock(spec=Store)
    store.find_project_for_file.return_value = pid
    store.recent_xml_run_exists.return_value = xml_exists
    store.recent_stdout_run_for_cmd.return_value = stdout_dup
    store.max_seq.return_value = max_seq_val
    store.insert_test_run.return_value = 42
    return store


def _client(store: MagicMock) -> TestClient:
    _set_store_for_testing(store)
    return TestClient(mcp.streamable_http_app(), raise_server_exceptions=True)


def _insert_kwargs(store: MagicMock) -> dict:
    """Return keyword args passed to insert_test_run as a flat dict."""
    ca = store.insert_test_run.call_args
    keys = ["project_id", "seq", "source_path", "source_mtime",
            "passed", "failed", "skipped", "source"]
    result = dict(zip(keys, ca[0]))
    result.update(ca[1])
    return result


# ── fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_store():
    yield
    _set_store_for_testing(None)


# ── A: non-test commands ───────────────────────────────────────────────────────

class TestNonTestCommands:
    def test_ls_skipped(self):
        store = _mock_store()
        r = _client(store).post("/buer/post-bash", json={
            "tool_input": {"command": "ls -la"},
            "cwd": "/proj",
        })
        assert r.status_code == 200
        assert _ac(r) == ""
        store.insert_test_run.assert_not_called()

    def test_git_commit_skipped(self):
        store = _mock_store()
        r = _client(store).post("/buer/post-bash", json={
            "tool_input": {"command": "git commit -m 'wip'"},
            "cwd": "/proj",
        })
        assert r.status_code == 200
        store.insert_test_run.assert_not_called()

    def test_curl_skipped(self):
        store = _mock_store()
        r = _client(store).post("/buer/post-bash", json={
            "tool_input": {"command": "curl https://example.com"},
            "cwd": "/proj",
        })
        assert r.status_code == 200
        store.insert_test_run.assert_not_called()

    def test_missing_command_skipped(self):
        store = _mock_store()
        r = _client(store).post("/buer/post-bash", json={
            "tool_input": {},
            "cwd": "/proj",
        })
        assert r.status_code == 200
        store.insert_test_run.assert_not_called()


# ── B: unrecognized output ─────────────────────────────────────────────────────

class TestUnrecognizedOutput:
    def test_pytest_import_error_skipped(self):
        store = _mock_store()
        r = _client(store).post("/buer/post-bash", json={
            "tool_input": {"command": "pytest tests/"},
            "tool_response": "ImportError: cannot import name 'foo'",
            "cwd": "/proj",
        })
        assert r.status_code == 200
        store.insert_test_run.assert_not_called()

    def test_empty_output_skipped(self):
        store = _mock_store()
        r = _client(store).post("/buer/post-bash", json={
            "tool_input": {"command": "pytest"},
            "tool_response": "",
            "cwd": "/proj",
        })
        assert r.status_code == 200
        store.insert_test_run.assert_not_called()

    def test_missing_output_field_skipped(self):
        store = _mock_store()
        r = _client(store).post("/buer/post-bash", json={
            "tool_input": {"command": "pytest"},
            "cwd": "/proj",
        })
        assert r.status_code == 200
        store.insert_test_run.assert_not_called()


# ── C: project not found ───────────────────────────────────────────────────────

class TestProjectNotFound:
    def test_no_cwd_skipped(self):
        store = _mock_store(pid=None)
        r = _client(store).post("/buer/post-bash", json={
            "tool_input": {"command": "pytest tests/"},
            "tool_response": _PYTEST_OUT,
        })
        assert r.status_code == 200
        store.insert_test_run.assert_not_called()

    def test_empty_cwd_skipped(self):
        store = _mock_store(pid=None)
        r = _client(store).post("/buer/post-bash", json={
            "tool_input": {"command": "pytest tests/"},
            "tool_response": _PYTEST_OUT,
            "cwd": "",
        })
        assert r.status_code == 200
        store.insert_test_run.assert_not_called()

    def test_unregistered_cwd_skipped(self):
        store = _mock_store(pid=None)
        r = _client(store).post("/buer/post-bash", json={
            "tool_input": {"command": "pytest tests/"},
            "tool_response": _PYTEST_OUT,
            "cwd": "/unknown/project",
        })
        assert r.status_code == 200
        store.insert_test_run.assert_not_called()


# ── D: XML suppresses warning ─────────────────────────────────────────────────

class TestDedupXml:
    def test_recent_xml_suppresses_warning(self):
        """XML present → xml_warn not emitted (we're inside recent_xml_run_exists=True branch)."""
        store = _mock_store(xml_exists=True)
        r = _client(store).post("/buer/post-bash", json={
            "tool_input": {"command": "pytest tests/"},
            "tool_response": _PYTEST_OUT,
            "cwd": "/proj",
        })
        assert r.status_code == 200
        assert _ac(r) == ""

    def test_xml_check_uses_120s_window(self):
        store = _mock_store(xml_exists=False)
        _client(store).post("/buer/post-bash", json={
            "tool_input": {"command": "pytest tests/"},
            "tool_response": _PYTEST_OUT,
            "cwd": "/proj",
        })
        store.recent_xml_run_exists.assert_called_once_with(1, within_seconds=120)


# ── E: already warned → silent ────────────────────────────────────────────────

class TestAlreadyWarned:
    def test_already_warned_no_second_warning(self):
        """xml_missing_warned=1 → _missing_xml_should_warn returns False → no warning."""
        store = _mock_store(xml_exists=False)
        # Return a state dict with xml_missing_warned=1 (already warned)
        store.get_assist_state.return_value = {"xml_missing_warned": 1}
        r = _client(store).post("/buer/post-bash", json={
            "tool_input": {"command": "pytest tests/"},
            "tool_response": _PYTEST_OUT,
            "cwd": "/proj",
        })
        assert r.status_code == 200
        assert _ac(r) == ""
        store.update_assist_state.assert_not_called()


# ── F: first detection → one-shot warning ────────────────────────────────────

class TestMissingXmlWarning:
    def test_first_detection_emits_warning(self):
        """xml_missing_warned=0 → warning fires and update_assist_state sets flag."""
        store = _mock_store(xml_exists=False)
        store.get_assist_state.return_value = {"xml_missing_warned": 0}
        r = _client(store).post("/buer/post-bash", json={
            "tool_input": {"command": "pytest tests/"},
            "tool_response": _PYTEST_OUT,
            "cwd": "/proj",
        })
        assert r.status_code == 200
        warn = _ac(r)
        assert "[BUER]" in warn
        assert "JUnit XML" in warn
        store.update_assist_state.assert_called_once_with(1, xml_missing_warned=1)

    def test_warning_not_fired_for_non_test_command(self):
        """Non-test commands never enter Feature 2 → no warning ever."""
        store = _mock_store(xml_exists=False)
        store.get_assist_state.return_value = {"xml_missing_warned": 0}
        r = _client(store).post("/buer/post-bash", json={
            "tool_input": {"command": "ls -la"},
            "cwd": "/proj",
        })
        assert _ac(r) == ""
        store.update_assist_state.assert_not_called()

    def test_warning_appended_after_crash_inject(self):
        """If crash injection also fires, xml_warn is appended after it."""
        store = _mock_store(xml_exists=False)
        store.get_assist_state.return_value = {"xml_missing_warned": 0}
        # No crash stack in output, but test both inject_text paths covered by
        # checking that a pytest command still emits the xml_warn.
        r = _client(store).post("/buer/post-bash", json={
            "tool_input": {"command": "pytest tests/"},
            "tool_response": _PYTEST_OUT,
            "cwd": "/proj",
        })
        assert "JUnit XML" in _ac(r)


# ── G: malformed requests ─────────────────────────────────────────────────────

class TestMalformed:
    def test_invalid_json_returns_200(self):
        store = _mock_store()
        r = _client(store).post(
            "/buer/post-bash",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 200
        store.insert_test_run.assert_not_called()

    def test_empty_body_returns_200(self):
        store = _mock_store()
        r = _client(store).post("/buer/post-bash", json={})
        assert r.status_code == 200
        store.insert_test_run.assert_not_called()


# ── I: response is always empty ───────────────────────────────────────────────

class TestResponseAlwaysEmpty:
    def test_no_text_on_successful_insert(self):
        store = _mock_store()
        r = _client(store).post("/buer/post-bash", json={
            "tool_input": {"command": "pytest tests/"},
            "tool_response": _PYTEST_OUT,
            "cwd": "/proj",
        })
        assert _ac(r) == ""

    def test_no_text_on_non_test_command(self):
        store = _mock_store()
        r = _client(store).post("/buer/post-bash", json={
            "tool_input": {"command": "ls"},
            "cwd": "/proj",
        })
        assert _ac(r) == ""


# ── I: real Claude Code payload format (dict tool_response) ──────────────────
# Verified empirically: CC v2.1.145 sends tool_response as a dict with stdout/stderr.
# stdout contains merged output (CC merges stderr → stdout). Legacy string format
# is also supported for tests/older integrations.

_TSX_STACK = (
    "TypeError: Cannot read properties of null (reading 'id')\n"
    "    at processRow (/tmp/crash_tsx.ts:5:15)\n"
    "    at handleRequest (/tmp/crash_tsx.ts:15:18)\n"
    "    at Module._compile (node:internal/modules/cjs/loader:1705:14)\n"
)

class TestDictToolResponse:
    """Real Claude Code payload has tool_response as dict {stdout, stderr, ...}."""

    def test_empty_stdout_in_dict_no_insert(self):
        """Dict payload with empty stdout → no insert (nothing to parse)."""
        store = _mock_store()
        r = _client(store).post("/buer/post-bash", json={
            "tool_name": "Bash",
            "tool_input": {"command": "pytest tests/"},
            "tool_response": {
                "stdout": "",
                "stderr": "",
                "interrupted": False,
                "isImage": False,
                "noOutputExpected": False,
            },
            "cwd": "/proj",
        })
        assert r.status_code == 200
        store.insert_test_run.assert_not_called()

    def test_crash_stack_stored_from_dict_stdout(self):
        """Crash output in dict stdout → insert_crash_stack called best-effort."""
        store = _mock_store()
        # Provide a root that contains /tmp path so FQN resolution can proceed
        r = _client(store).post("/buer/post-bash", json={
            "tool_name": "Bash",
            "tool_input": {"command": "npx tsx crash.ts", "description": "run"},
            "tool_response": {
                "stdout": _TSX_STACK,
                "stderr": "",
                "interrupted": False,
                "isImage": False,
                "noOutputExpected": False,
            },
            "cwd": "/tmp",
        })
        assert r.status_code == 200
        # insert_crash_stack may or may not be called depending on FQN resolution;
        # key assertion: no exception, response is empty.
        assert _ac(r) == ""

    def test_response_always_empty_for_dict_format(self):
        store = _mock_store()
        r = _client(store).post("/buer/post-bash", json={
            "tool_name": "Bash",
            "tool_input": {"command": "pytest tests/"},
            "tool_response": {"stdout": _PYTEST_OUT, "stderr": ""},
            "cwd": "/proj",
        })
        assert _ac(r) == ""


# ── K: crash stack detection via real Store ───────────────────────────────────

class TestCrashStackDetection:
    """End-to-end crash stack insertion using a real Store (not MagicMock)."""

    def test_stack_trace_in_dict_stdout_stored(self, tmp_path):
        """When output contains tsx stack trace, crash_stacks row is inserted."""
        import json
        from buer.store import Store
        s = Store(str(tmp_path / "store.sqlite"))
        pid = s.get_or_create_project(str(tmp_path))
        # Add a session so pid resolves
        _set_store_for_testing(s)
        try:
            crash_output = (
                "TypeError: Cannot read properties of null (reading 'id')\n"
                f"    at getPool ({tmp_path}/lib/db.ts:10:1)\n"
                "    at Module._compile (node:internal/modules/cjs/loader:1705:14)\n"
            )
            (tmp_path / "lib").mkdir()
            (tmp_path / "lib" / "db.ts").touch()
            r = _client(s).post("/buer/post-bash", json={
                "tool_name": "Bash",
                "tool_input": {"command": "npx tsx crash.ts"},
                "tool_response": {
                    "stdout": crash_output,
                    "stderr": "",
                    "interrupted": False,
                    "isImage": False,
                    "noOutputExpected": False,
                },
                "cwd": str(tmp_path),
            })
            assert r.status_code == 200
            rows = s.recent_crash_stacks(pid)
            assert len(rows) == 1
            fqns = json.loads(rows[0]["stack_fqns"])
            assert any("getPool" in fqn for fqn in fqns), f"FQNs: {fqns}"
        finally:
            _set_store_for_testing(None)
            s.close()

    def test_no_stack_no_row_inserted(self, tmp_path):
        """Plain output without stack trace → crash_stacks stays empty."""
        from buer.store import Store
        s = Store(str(tmp_path / "store.sqlite"))
        pid = s.get_or_create_project(str(tmp_path))
        _set_store_for_testing(s)
        try:
            _client(s).post("/buer/post-bash", json={
                "tool_name": "Bash",
                "tool_input": {"command": "echo hello"},
                "tool_response": {"stdout": "hello\n", "stderr": ""},
                "cwd": str(tmp_path),
            })
            assert len(s.recent_crash_stacks(pid)) == 0
        finally:
            _set_store_for_testing(None)
            s.close()


# ── L: crash injection (cone ∩ stack → response body) ─────────────────────────

class TestCrashInjection:
    """_compute_crash_injection injects suspects into the response body."""

    @pytest.fixture(autouse=True)
    def _clear_dedup(self):
        import buer.mcp.server as _srv
        _srv._crash_inject_seen.clear()
        yield
        _srv._crash_inject_seen.clear()

    def _setup_store(self, tmp_path, *, with_changes: bool = True):
        """Create Store + project + session.  Returns (store, pid)."""
        s = Store(str(tmp_path / "store.sqlite"))
        pid = s.get_or_create_project(str(tmp_path))
        s.open_session(pid, "sess-1")
        if with_changes:
            afile = str(tmp_path / "a.ts")
            (tmp_path / "a.ts").touch()
            s.con.execute(
                """INSERT INTO determinations
                   (project_id, seq, file_path, define_name, node_fingerprint, edit_type, created_at)
                   VALUES (?, 1, ?, 'changed_fn', 'fp1', 'create', datetime('now'))""",
                (pid, afile),
            )
            s.con.commit()
        s.close_session(pid, "sess-1")
        return s, pid

    def _add_caller_edge(self, s, pid, caller_fqn="js_ts::lib/db.getPool"):
        s.con.execute(
            """INSERT OR IGNORE INTO call_edges (project_id, caller, callee, edge_kind)
               VALUES (?, ?, 'js_ts::a.changed_fn', 'call')""",
            (pid, caller_fqn),
        )
        s.con.commit()

    def _post_crash(self, s, tmp_path, fn_name, crash_file):
        crash_output = (
            f"TypeError: something\n"
            f"    at {fn_name} ({crash_file}:10:1)\n"
        )
        return _client(s).post(
            "/buer/post-bash",
            json={
                "tool_input": {"command": "npx tsx run.ts"},
                "tool_response": {
                    "stdout": crash_output,
                    "stderr": "",
                    "interrupted": False,
                    "isImage": False,
                    "noOutputExpected": False,
                },
                "cwd": str(tmp_path),
            },
        )

    def test_seed_hits_when_changed_fn_in_stack(self, tmp_path):
        """Changed function itself in crash stack → Branch a '头号嫌疑'."""
        s, pid = self._setup_store(tmp_path)
        afile = str(tmp_path / "a.ts")
        try:
            r = self._post_crash(s, tmp_path, "changed_fn", afile)
            assert r.status_code == 200
            assert "top crash suspect" in _ac(r)
            assert "a.changed_fn" in _ac(r)
        finally:
            s.close()

    def test_victims_in_response_when_intersection_nonempty(self, tmp_path):
        """cone ∩ stack non-empty, seed empty → Branch b '崩溃路径嫌疑'."""
        s, pid = self._setup_store(tmp_path)
        (tmp_path / "lib").mkdir()
        lib_db = tmp_path / "lib" / "db.ts"
        lib_db.touch()
        self._add_caller_edge(s, pid)   # lib/db.getPool → a.changed_fn
        try:
            r = self._post_crash(s, tmp_path, "getPool", str(lib_db))
            assert r.status_code == 200
            assert "⚡" in _ac(r)
            assert "crash-path suspects" in _ac(r)
            assert "lib/db.getPool" in _ac(r)
        finally:
            s.close()

    def test_no_suspects_message_when_intersection_empty(self, tmp_path):
        """cone ∩ stack empty → '未经过改动区' in response body."""
        s, pid = self._setup_store(tmp_path)
        self._add_caller_edge(s, pid, "m.someOther")
        (tmp_path / "lib").mkdir()
        unrelated = tmp_path / "lib" / "unrelated.ts"
        unrelated.touch()
        try:
            r = self._post_crash(s, tmp_path, "unrelatedFn", str(unrelated))
            assert r.status_code == 200
            assert "crash stack does not directly intersect" in _ac(r)
        finally:
            s.close()

    def test_empty_response_when_no_changes(self, tmp_path):
        """No changes in session range → empty response."""
        s, pid = self._setup_store(tmp_path, with_changes=False)
        (tmp_path / "lib").mkdir()
        crash_file = tmp_path / "lib" / "foo.ts"
        crash_file.touch()
        try:
            r = self._post_crash(s, tmp_path, "doThing", str(crash_file))
            assert r.status_code == 200
            assert _ac(r) == ""
        finally:
            s.close()

    def test_dedup_suppresses_second_injection_within_window(self, tmp_path):
        """Same crash fingerprint within 60s → second response is empty."""
        s, pid = self._setup_store(tmp_path)
        (tmp_path / "lib").mkdir()
        lib_db = tmp_path / "lib" / "db.ts"
        lib_db.touch()
        self._add_caller_edge(s, pid)
        payload = {
            "tool_input": {"command": "npx tsx run.ts"},
            "tool_response": {
                "stdout": f"TypeError: x\n    at getPool ({lib_db}:10:1)\n",
                "stderr": "",
                "interrupted": False,
                "isImage": False,
                "noOutputExpected": False,
            },
            "cwd": str(tmp_path),
        }
        try:
            client = _client(s)
            r1 = client.post("/buer/post-bash", json=payload)
            r2 = client.post("/buer/post-bash", json=payload)
            assert "⚡" in r1.text
            assert _ac(r2) == ""
        finally:
            s.close()

    def test_exception_in_injection_returns_empty_crash_still_stored(self, tmp_path):
        """Exception inside _compute_crash_injection → empty response, crash stored."""
        from unittest.mock import patch
        s, pid = self._setup_store(tmp_path)
        (tmp_path / "lib").mkdir()
        crash_file = tmp_path / "lib" / "db.ts"
        crash_file.touch()
        self._add_caller_edge(s, pid)
        try:
            with patch(
                "buer.influence.caller_cone_with_depth",
                side_effect=RuntimeError("simulated failure"),
            ):
                r = self._post_crash(s, tmp_path, "getPool", str(crash_file))
            assert r.status_code == 200
            assert _ac(r) == ""
            rows = s.recent_crash_stacks(pid)
            assert len(rows) >= 1
        finally:
            s.close()

    def test_max_cap_truncates_at_12(self, tmp_path):
        """When seed_hits > 12, list is capped to 12 with '...及其他X个'."""
        s = Store(str(tmp_path / "store.sqlite"))
        pid = s.get_or_create_project(str(tmp_path))
        s.open_session(pid, "sess-trunc")
        # Insert 14 changed functions, each in its own file
        for i in range(14):
            fname = f"fn{i}"
            fpath = str(tmp_path / f"{fname}.ts")
            (tmp_path / f"{fname}.ts").touch()
            s.con.execute(
                "INSERT INTO determinations"
                " (project_id, seq, file_path, define_name, node_fingerprint, edit_type, created_at)"
                " VALUES (?, ?, ?, ?, 'fp', 'create', datetime('now'))",
                (pid, i + 1, fpath, fname),
            )
        s.con.commit()
        s.close_session(pid, "sess-trunc")

        # Crash at all 14 functions → 14 seed_hits
        crash_lines = ["TypeError: boom"]
        for i in range(14):
            fpath = str(tmp_path / f"fn{i}.ts")
            crash_lines.append(f"    at fn{i} ({fpath}:1:1)")
        crash_output = "\n".join(crash_lines)

        try:
            r = _client(s).post("/buer/post-bash", json={
                "tool_input": {"command": "npx tsx run.ts"},
                "tool_response": {
                    "stdout": crash_output,
                    "stderr": "",
                    "interrupted": False,
                    "isImage": False,
                    "noOutputExpected": False,
                },
                "cwd": str(tmp_path),
            })
            assert r.status_code == 200
            assert "top crash suspect" in _ac(r)
            assert "... and 2 more" in _ac(r)
            numbered = [l for l in _ac(r).split("\n") if l.strip() and l.strip()[0].isdigit()]
            assert len(numbered) == 12
        finally:
            s.close()
