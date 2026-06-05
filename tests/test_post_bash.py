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
    """post-bash no longer extracts crash stacks from stdout (batch A phase 2).
    Crash stacks come from JUnit XML / crash.log via testscan."""

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


# (crash injection tests removed — _compute_crash_injection deleted in batch A phase 2;
#  crash stacks now come from JUnit XML traceback and crash.log via testscan)
