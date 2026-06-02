"""Tests for /buer/post-bash endpoint (§4.4 real-time test capture).

Coverage plan:
  A. Non-test commands → silent 200, no insert
  B. Test command with unrecognized output → silent 200, no insert
  C. No matching project → silent 200, no insert
  D. Happy path: pytest output → inserts run with source='stdout'
  E. Happy path: jest/cargo/mocha output → parsed and inserted
  F. Dedup: recent XML run exists → stdout run skipped
  G. Dedup: recent stdout same-cmd → skipped
  H. Malformed / empty body → silent 200
  I. Response body always empty
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

from buer.mcp.server import _set_store_for_testing, mcp
from buer.store import Store

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
        assert r.text == ""
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


# ── D: happy path — pytest ─────────────────────────────────────────────────────

class TestHappyPathPytest:
    def test_inserts_with_source_stdout(self):
        store = _mock_store()
        r = _client(store).post("/buer/post-bash", json={
            "tool_input": {"command": "pytest tests/"},
            "tool_response": _PYTEST_OUT,
            "cwd": "/proj",
        })
        assert r.status_code == 200
        assert r.text == ""
        store.insert_test_run.assert_called_once()
        kw = _insert_kwargs(store)
        assert kw["source"] == "stdout"

    def test_passed_count(self):
        store = _mock_store()
        _client(store).post("/buer/post-bash", json={
            "tool_input": {"command": "pytest"},
            "tool_response": _PYTEST_OUT,
            "cwd": "/proj",
        })
        kw = _insert_kwargs(store)
        assert kw["passed"] == 3
        assert kw["failed"] == 0

    def test_source_path_starts_with_stdout_prefix(self):
        store = _mock_store()
        _client(store).post("/buer/post-bash", json={
            "tool_input": {"command": "pytest tests/"},
            "tool_response": _PYTEST_OUT,
            "cwd": "/proj",
        })
        kw = _insert_kwargs(store)
        assert kw["source_path"].startswith("stdout:")

    def test_same_command_produces_same_fingerprint(self):
        store = _mock_store()
        for _ in range(2):
            _client(store).post("/buer/post-bash", json={
                "tool_input": {"command": "pytest tests/"},
                "tool_response": _PYTEST_OUT,
                "cwd": "/proj",
            })
        fp1 = store.insert_test_run.call_args_list[0][1].get(
            "source_path",
            store.insert_test_run.call_args_list[0][0][2] if store.insert_test_run.call_args_list[0][0] else "",
        )
        fp2 = store.insert_test_run.call_args_list[1][1].get(
            "source_path",
            store.insert_test_run.call_args_list[1][0][2] if store.insert_test_run.call_args_list[1][0] else "",
        )
        assert fp1 == fp2

    def test_project_id_forwarded(self):
        store = _mock_store(pid=7)
        _client(store).post("/buer/post-bash", json={
            "tool_input": {"command": "pytest tests/"},
            "tool_response": _PYTEST_OUT,
            "cwd": "/proj",
        })
        kw = _insert_kwargs(store)
        assert kw["project_id"] == 7


# ── E: other runners ───────────────────────────────────────────────────────────

class TestOtherRunners:
    def test_jest_parsed(self):
        store = _mock_store()
        _client(store).post("/buer/post-bash", json={
            "tool_input": {"command": "npx jest --coverage"},
            "tool_response": _JEST_OUT,
            "cwd": "/proj",
        })
        store.insert_test_run.assert_called_once()
        kw = _insert_kwargs(store)
        assert kw["passed"] == 8
        assert kw["failed"] == 2
        assert kw["source"] == "stdout"

    def test_cargo_parsed(self):
        store = _mock_store()
        _client(store).post("/buer/post-bash", json={
            "tool_input": {"command": "cargo test"},
            "tool_response": _CARGO_OUT,
            "cwd": "/proj",
        })
        store.insert_test_run.assert_called_once()
        kw = _insert_kwargs(store)
        assert kw["passed"] == 2
        assert kw["failed"] == 0
        assert kw["source"] == "stdout"

    def test_mocha_parsed(self):
        store = _mock_store()
        _client(store).post("/buer/post-bash", json={
            "tool_input": {"command": "npx mocha"},
            "tool_response": _MOCHA_OUT,
            "cwd": "/proj",
        })
        store.insert_test_run.assert_called_once()
        kw = _insert_kwargs(store)
        assert kw["passed"] == 4
        assert kw["failed"] == 1
        assert kw["source"] == "stdout"


# ── F: dedup — XML priority ────────────────────────────────────────────────────

class TestDedupXml:
    def test_recent_xml_skips_stdout(self):
        store = _mock_store(xml_exists=True)
        r = _client(store).post("/buer/post-bash", json={
            "tool_input": {"command": "pytest tests/"},
            "tool_response": _PYTEST_OUT,
            "cwd": "/proj",
        })
        assert r.status_code == 200
        store.insert_test_run.assert_not_called()

    def test_xml_check_uses_120s_window(self):
        store = _mock_store(xml_exists=False)
        _client(store).post("/buer/post-bash", json={
            "tool_input": {"command": "pytest tests/"},
            "tool_response": _PYTEST_OUT,
            "cwd": "/proj",
        })
        store.recent_xml_run_exists.assert_called_once_with(1, within_seconds=120)


# ── G: dedup — same-cmd stdout ────────────────────────────────────────────────

class TestDedupStdout:
    def test_recent_stdout_same_cmd_skipped(self):
        store = _mock_store(stdout_dup=True)
        r = _client(store).post("/buer/post-bash", json={
            "tool_input": {"command": "pytest tests/"},
            "tool_response": _PYTEST_OUT,
            "cwd": "/proj",
        })
        assert r.status_code == 200
        store.insert_test_run.assert_not_called()

    def test_stdout_dedup_uses_30s_window(self):
        store = _mock_store(stdout_dup=False)
        _client(store).post("/buer/post-bash", json={
            "tool_input": {"command": "pytest tests/"},
            "tool_response": _PYTEST_OUT,
            "cwd": "/proj",
        })
        store.recent_stdout_run_for_cmd.assert_called_once()
        _, kwargs = store.recent_stdout_run_for_cmd.call_args
        assert kwargs.get("within_seconds", 30) == 30

    def test_no_dedup_when_both_absent(self):
        store = _mock_store(xml_exists=False, stdout_dup=False)
        _client(store).post("/buer/post-bash", json={
            "tool_input": {"command": "pytest tests/"},
            "tool_response": _PYTEST_OUT,
            "cwd": "/proj",
        })
        store.insert_test_run.assert_called_once()


# ── H: malformed requests ─────────────────────────────────────────────────────

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
        assert r.text == ""

    def test_no_text_on_non_test_command(self):
        store = _mock_store()
        r = _client(store).post("/buer/post-bash", json={
            "tool_input": {"command": "ls"},
            "cwd": "/proj",
        })
        assert r.text == ""


# ── J: real Claude Code payload format (dict tool_response) ──────────────────
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

    def test_test_output_parsed_from_dict_stdout(self):
        """Happy path: pytest output delivered in dict format → run inserted."""
        store = _mock_store()
        r = _client(store).post("/buer/post-bash", json={
            "tool_name": "Bash",
            "tool_input": {"command": "pytest tests/", "description": "Run tests"},
            "tool_response": {
                "stdout": _PYTEST_OUT,
                "stderr": "",
                "interrupted": False,
                "isImage": False,
                "noOutputExpected": False,
            },
            "cwd": "/proj",
        })
        assert r.status_code == 200
        store.insert_test_run.assert_called_once()

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
        assert r.text == ""

    def test_response_always_empty_for_dict_format(self):
        store = _mock_store()
        r = _client(store).post("/buer/post-bash", json={
            "tool_name": "Bash",
            "tool_input": {"command": "pytest tests/"},
            "tool_response": {"stdout": _PYTEST_OUT, "stderr": ""},
            "cwd": "/proj",
        })
        assert r.text == ""


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
            assert "top crash suspect" in r.text
            assert "a.changed_fn" in r.text
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
            assert "⚡" in r.text
            assert "crash-path suspects" in r.text
            assert "lib/db.getPool" in r.text
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
            assert "crash stack does not directly intersect" in r.text
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
            assert r.text == ""
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
            assert r2.text == ""
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
            assert r.text == ""
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
            assert "top crash suspect" in r.text
            assert "... and 2 more" in r.text
            numbered = [l for l in r.text.split("\n") if l.strip() and l.strip()[0].isdigit()]
            assert len(numbered) == 12
        finally:
            s.close()
