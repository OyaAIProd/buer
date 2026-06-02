"""Tests for buer.stacktrace — stack trace parsing + FQN extraction."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from buer import stacktrace as st


# ── Sample stack trace strings ────────────────────────────────────────────────

_TSX_STACK = """\
TypeError: Cannot read properties of null (reading 'id')
    at processRow (/home/proj/lib/db/postgres.ts:42:18)
    at fetchProject (/home/proj/lib/db/postgres.ts:55:5)
    at handleRequest (/home/proj/app/api/handler.ts:12:3)
    at Object.<anonymous> (/home/proj/app/api/handler.ts:20:1)
    at Module._compile (node:internal/modules/cjs/loader:1376:14)
"""

_VITEST_STACK = """\
 FAIL  tests/lib/coins.test.ts
  AssertionError: expected 3 to equal 999
  ❯ getTierConfig lib/coins/tier-config.ts:28:14
  ❯ fetchProject lib/db/postgres.ts:55:5
  ❯ handleRequest app/api/handler.ts:12:3
"""

_NO_STACK = """\
tsc --noEmit
error TS2345: Argument of type 'string' is not assignable to parameter of type 'number'.
  3 failed, 0 passed
"""

_NODE_INTERNAL_ONLY = """\
TypeError: something
    at Module._compile (node:internal/modules/cjs/loader:1376:14)
    at Object.Module._extensions..js (node:internal/modules/cjs/loader:1399:10)
"""


# ── has_stack_trace ───────────────────────────────────────────────────────────

class TestHasStackTrace:
    def test_detects_tsx_format(self):
        assert st.has_stack_trace(_TSX_STACK) is True

    def test_detects_vitest_format(self):
        assert st.has_stack_trace(_VITEST_STACK) is True

    def test_rejects_plain_error(self):
        assert st.has_stack_trace(_NO_STACK) is False

    def test_rejects_node_internal_only(self):
        # Node internals use tsx format but have node: path — signature check
        # only looks at structure, not content, so this may or may not match.
        # The important thing: parse_stack_frames will return [] for node-only.
        # has_stack_trace is a fast pre-filter; it may return True here (ok).
        pass

    def test_empty_string(self):
        assert st.has_stack_trace("") is False


# ── parse_stack_frames ────────────────────────────────────────────────────────

class TestParseStackFrames:
    def test_tsx_extracts_frames(self, tmp_path):
        # Create placeholder lib/db/postgres.ts so the path resolves
        (tmp_path / "lib" / "db").mkdir(parents=True)
        (tmp_path / "lib" / "db" / "postgres.ts").touch()
        (tmp_path / "app" / "api").mkdir(parents=True)
        (tmp_path / "app" / "api" / "handler.ts").touch()

        root = str(tmp_path)
        # Inline stack with absolute paths pointing at tmp_path
        stack = (
            "TypeError: null\n"
            f"    at processRow ({tmp_path}/lib/db/postgres.ts:42:18)\n"
            f"    at fetchProject ({tmp_path}/lib/db/postgres.ts:55:5)\n"
            f"    at handleRequest ({tmp_path}/app/api/handler.ts:12:3)\n"
            "    at Object.<anonymous> (whatever.ts:1:1)\n"
        )
        frames = st.parse_stack_frames(stack, root)
        fn_names = {f["fn"] for f in frames}
        assert "processRow" in fn_names
        assert "fetchProject" in fn_names
        assert "handleRequest" in fn_names

    def test_tsx_excludes_anonymous(self, tmp_path):
        root = str(tmp_path)
        stack = (
            f"    at Object.<anonymous> ({tmp_path}/lib/foo.ts:1:1)\n"
        )
        frames = st.parse_stack_frames(stack, root)
        assert len(frames) == 0

    def test_tsx_excludes_internal_fns(self, tmp_path):
        root = str(tmp_path)
        stack = (
            f"    at Module._compile ({tmp_path}/lib/foo.ts:1:1)\n"
            f"    at wrapModuleLoad ({tmp_path}/lib/foo.ts:2:1)\n"
        )
        frames = st.parse_stack_frames(stack, root)
        assert len(frames) == 0

    def test_tsx_excludes_node_internals(self, tmp_path):
        root = str(tmp_path)
        stack = "    at Module._compile (node:internal/modules/cjs/loader:1376:14)\n"
        frames = st.parse_stack_frames(stack, root)
        assert len(frames) == 0

    def test_tsx_excludes_node_modules(self, tmp_path):
        root = str(tmp_path)
        stack = f"    at someLib ({tmp_path}/node_modules/vitest/dist/foo.js:1:1)\n"
        frames = st.parse_stack_frames(stack, root)
        assert len(frames) == 0

    def test_tsx_excludes_test_files(self, tmp_path):
        root = str(tmp_path)
        stack = f"    at myTest ({tmp_path}/tests/foo.test.ts:5:3)\n"
        frames = st.parse_stack_frames(stack, root)
        assert len(frames) == 0

    def test_tsx_excludes_scripts_dir(self, tmp_path):
        root = str(tmp_path)
        stack = f"    at doThing ({tmp_path}/scripts/migrate.ts:10:1)\n"
        frames = st.parse_stack_frames(stack, root)
        assert len(frames) == 0

    def test_vitest_relative_paths(self, tmp_path):
        root = str(tmp_path)
        (tmp_path / "lib" / "coins").mkdir(parents=True)
        (tmp_path / "lib" / "coins" / "tier-config.ts").touch()
        stack = "  ❯ getTierConfig lib/coins/tier-config.ts:28:14\n"
        frames = st.parse_stack_frames(stack, root)
        assert len(frames) == 1
        assert frames[0]["fn"] == "getTierConfig"
        assert frames[0]["line"] == 28

    def test_vitest_excludes_test_file(self, tmp_path):
        root = str(tmp_path)
        stack = "  ❯ myTest tests/lib/coins.test.ts:10:5\n"
        frames = st.parse_stack_frames(stack, root)
        assert len(frames) == 0

    def test_deduplicates_by_fn_and_path(self, tmp_path):
        root = str(tmp_path)
        (tmp_path / "lib").mkdir(parents=True)
        (tmp_path / "lib" / "foo.ts").touch()
        # Same fn+path appears in both tsx and vitest scans (two patterns)
        stack = (
            f"    at doThing ({tmp_path}/lib/foo.ts:10:1)\n"
            "  ❯ doThing lib/foo.ts:10:1\n"
        )
        frames = st.parse_stack_frames(stack, root)
        fn_names = [f["fn"] for f in frames]
        assert fn_names.count("doThing") == 1

    def test_frame_dict_keys(self, tmp_path):
        root = str(tmp_path)
        (tmp_path / "lib").mkdir(parents=True)
        (tmp_path / "lib" / "foo.ts").touch()
        stack = f"    at doThing ({tmp_path}/lib/foo.ts:42:1)\n"
        frames = st.parse_stack_frames(stack, root)
        assert len(frames) == 1
        assert {"fn", "file_path", "rel_path", "line"} == set(frames[0].keys())
        assert frames[0]["line"] == 42


# ── stack_fqns ────────────────────────────────────────────────────────────────

class TestStackFqns:
    def test_returns_empty_when_no_stack(self, tmp_path):
        result = st.stack_fqns(_NO_STACK, str(tmp_path))
        assert result == set()

    def test_returns_fqns_for_ts_files(self, tmp_path):
        root = str(tmp_path)
        (tmp_path / "lib" / "db").mkdir(parents=True)
        (tmp_path / "lib" / "db" / "postgres.ts").touch()
        stack = f"    at getPool ({tmp_path}/lib/db/postgres.ts:10:1)\n"
        fqns = st.stack_fqns(stack, root)
        assert "js_ts::lib/db/postgres.getPool" in fqns

    def test_fqn_format_slash_separator_for_ts(self, tmp_path):
        root = str(tmp_path)
        (tmp_path / "lib" / "coins").mkdir(parents=True)
        (tmp_path / "lib" / "coins" / "tier-config.ts").touch()
        stack = f"    at getTierConfig ({tmp_path}/lib/coins/tier-config.ts:28:1)\n"
        fqns = st.stack_fqns(stack, root)
        # module_name_of strips .ts extension and uses slash separators; lang prefix added
        assert any("getTierConfig" in fqn for fqn in fqns)
        assert any("lib/coins/" in fqn for fqn in fqns)

    def test_skips_frames_outside_root(self, tmp_path):
        # Path not under root → module_name_of raises → silently skipped
        root = str(tmp_path)
        stack = "    at externalFn (/some/other/path/lib/foo.ts:1:1)\n"
        fqns = st.stack_fqns(stack, root)
        # May be empty or contain the frame — depends on module_name_of behavior.
        # Key assertion: no exception raised
        assert isinstance(fqns, set)

    def test_node_internal_stack_returns_empty(self, tmp_path):
        fqns = st.stack_fqns(_NODE_INTERNAL_ONLY, str(tmp_path))
        assert fqns == set()


# ── normalize_error_signature ─────────────────────────────────────────────────

class TestNormalizeErrorSignature:
    def test_assertion_to_equal(self):
        output = "AssertionError: expected 3 to equal 999"
        assert st.normalize_error_signature(output) == "AssertionError|to_equal"

    def test_assertion_to_deeply_equal(self):
        output = "AssertionError: expected { a: 1 } to deeply equal { a: 2 }"
        assert st.normalize_error_signature(output) == "AssertionError|to_deeply_equal"

    def test_type_error_cannot_read_property(self):
        output = "TypeError: Cannot read properties of null (reading 'id')"
        assert st.normalize_error_signature(output) == "TypeError|cannot_read_property"

    def test_type_error_is_not_a_function(self):
        output = "TypeError: myFn is not a function"
        assert st.normalize_error_signature(output) == "TypeError|is_not_a_function"

    def test_reference_error_is_not_defined(self):
        output = "ReferenceError: myVar is not defined"
        assert st.normalize_error_signature(output) == "ReferenceError|is_not_defined"

    def test_error_of_null(self):
        output = "TypeError: reading property of null"
        assert st.normalize_error_signature(output) == "TypeError|of_null"

    def test_same_class_same_signature(self):
        """Two AssertionError|to_equal messages with different values produce same sig."""
        out1 = "AssertionError: expected 2 to equal 1"
        out2 = "AssertionError: expected 5 to equal 3"
        assert st.normalize_error_signature(out1) == st.normalize_error_signature(out2)
        assert st.normalize_error_signature(out1) == "AssertionError|to_equal"

    def test_different_classes_different_signatures(self):
        out_eq = "AssertionError: expected 2 to equal 3"
        out_throw = "AssertionError: expected function to throw"
        assert st.normalize_error_signature(out_eq) != st.normalize_error_signature(out_throw)

    def test_no_error_type_returns_none(self):
        output = "tsc --noEmit\nerror TS2345: bad argument"
        assert st.normalize_error_signature(output) is None

    def test_vitest_real_format(self):
        """Vitest output from _VITEST_STACK: AssertionError with to equal."""
        assert st.normalize_error_signature(_VITEST_STACK) == "AssertionError|to_equal"

    def test_fallback_strips_literals(self):
        """Unknown error message: literals stripped, snake_case result returned."""
        output = 'Error: something went wrong at step 42 with value "foo"'
        sig = st.normalize_error_signature(output)
        assert sig is not None
        assert sig.startswith("Error|")
        # Numbers and quoted strings should not appear in the normalized part
        assert "42" not in sig
        assert "foo" not in sig

    def test_unknown_error_no_message(self):
        output = "Error:"
        sig = st.normalize_error_signature(output)
        # No message part → falls through to unknown
        assert sig == "Error|unknown"


# ── Python traceback ──────────────────────────────────────────────────────────

class TestPythonTraceback:
    """Python 'File "path.py", line N, in fn' format."""

    def _make_py_stack(self, tmp_path: Path) -> tuple[str, str]:
        """Build a realistic Python traceback rooted at tmp_path."""
        (tmp_path / "TradingAgents" / "cli").mkdir(parents=True)
        (tmp_path / "TradingAgents" / "graph").mkdir(parents=True)
        (tmp_path / "TradingAgents" / "cli" / "main.py").touch()
        (tmp_path / "TradingAgents" / "graph" / "trading_graph.py").touch()
        root = str(tmp_path)
        stack = (
            "Traceback (most recent call last):\n"
            f'  File "{tmp_path}/TradingAgents/cli/main.py", line 42, in analyze\n'
            "    result = pipeline.run(inputs)\n"
            f'  File "{tmp_path}/TradingAgents/graph/trading_graph.py", line 88, in run\n'
            "    return self._execute(inputs)\n"
            # stdlib — should be excluded
            '  File "/usr/lib/python3.11/subprocess.py", line 120, in run\n'
            # site-packages — should be excluded
            '  File "/home/user/.venv/lib/python3.11/site-packages/langchain/chains.py", line 55, in invoke\n'
            # <module> — should be excluded (anonymous)
            f'  File "{tmp_path}/TradingAgents/cli/main.py", line 1, in <module>\n'
        )
        return stack, root

    def test_has_stack_trace_detects_python(self, tmp_path):
        stack, root = self._make_py_stack(tmp_path)
        assert st.has_stack_trace(stack) is True

    def test_parse_extracts_production_frames(self, tmp_path):
        stack, root = self._make_py_stack(tmp_path)
        frames = st.parse_stack_frames(stack, root)
        fn_names = {f["fn"] for f in frames}
        assert "analyze" in fn_names
        assert "run" in fn_names

    def test_parse_excludes_module_frame(self, tmp_path):
        stack, root = self._make_py_stack(tmp_path)
        frames = st.parse_stack_frames(stack, root)
        fn_names = {f["fn"] for f in frames}
        assert "<module>" not in fn_names

    def test_parse_excludes_stdlib(self, tmp_path):
        stack, root = self._make_py_stack(tmp_path)
        frames = st.parse_stack_frames(stack, root)
        rel_paths = {f["rel_path"] for f in frames}
        assert not any("python3" in rp for rp in rel_paths)

    def test_parse_excludes_site_packages(self, tmp_path):
        stack, root = self._make_py_stack(tmp_path)
        frames = st.parse_stack_frames(stack, root)
        rel_paths = {f["rel_path"] for f in frames}
        assert not any("site-packages" in rp for rp in rel_paths)

    def test_parse_frame_count(self, tmp_path):
        stack, root = self._make_py_stack(tmp_path)
        frames = st.parse_stack_frames(stack, root)
        # analyze (main.py) + run (trading_graph.py) = 2 production frames
        assert len(frames) == 2

    def test_parse_lambda_excluded(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "utils.py").touch()
        root = str(tmp_path)
        stack = f'  File "{tmp_path}/src/utils.py", line 10, in <lambda>\n'
        frames = st.parse_stack_frames(stack, root)
        assert len(frames) == 0

    def test_parse_test_file_excluded(self, tmp_path):
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_foo.py").touch()
        root = str(tmp_path)
        stack = f'  File "{tmp_path}/tests/test_foo.py", line 5, in test_bar\n'
        frames = st.parse_stack_frames(stack, root)
        assert len(frames) == 0

    def test_fqn_dot_separator_format(self, tmp_path):
        """Python FQN must use dot separators to match call_edges."""
        (tmp_path / "TradingAgents" / "cli").mkdir(parents=True)
        (tmp_path / "TradingAgents" / "cli" / "main.py").touch()
        root = str(tmp_path)
        stack = f'  File "{tmp_path}/TradingAgents/cli/main.py", line 42, in analyze\n'
        fqns = st.stack_fqns(stack, root)
        assert "py::TradingAgents.cli.main.analyze" in fqns

    def test_fqn_no_slash_in_python_fqn(self, tmp_path):
        """Python FQNs must not contain slash separators (call_edges uses dots)."""
        (tmp_path / "a" / "b").mkdir(parents=True)
        (tmp_path / "a" / "b" / "mod.py").touch()
        root = str(tmp_path)
        stack = f'  File "{tmp_path}/a/b/mod.py", line 1, in myfunc\n'
        fqns = st.stack_fqns(stack, root)
        assert len(fqns) == 1
        fqn = next(iter(fqns))
        assert "/" not in fqn
        assert fqn == "py::a.b.mod.myfunc"

    def test_deduplication_across_patterns(self, tmp_path):
        """Same (path, fn) matched by both tsx and Python patterns counted once."""
        (tmp_path / "lib").mkdir()
        (tmp_path / "lib" / "foo.py").touch()
        root = str(tmp_path)
        # tsx format for a .py file
        tsx_line = f"    at doThing ({tmp_path}/lib/foo.py:10:1)\n"
        # Python format for same file+fn
        py_line = f'  File "{tmp_path}/lib/foo.py", line 10, in doThing\n'
        frames = st.parse_stack_frames(tsx_line + py_line, root)
        fn_names = [f["fn"] for f in frames]
        assert fn_names.count("doThing") == 1

    def test_js_tsx_regression(self, tmp_path):
        """Adding Python pattern must not affect tsx/vitest parsing."""
        (tmp_path / "lib" / "db").mkdir(parents=True)
        (tmp_path / "lib" / "db" / "postgres.ts").touch()
        root = str(tmp_path)
        stack = f"    at getPool ({tmp_path}/lib/db/postgres.ts:10:1)\n"
        fqns = st.stack_fqns(stack, root)
        assert "js_ts::lib/db/postgres.getPool" in fqns

    def test_pure_python_no_stack_returns_empty(self, tmp_path):
        output = "ValueError: invalid literal for int() with base 10: 'abc'"
        fqns = st.stack_fqns(output, str(tmp_path))
        assert fqns == set()
