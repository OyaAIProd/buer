"""Tests for per-testcase stdout parsing (§4.4 regression signal via stdout).

Covers _pytest_cases classname/name/status/file_path extraction and
parse_test_output integration so test_case_history keys are consistent
between XML and stdout sources.
"""
import pytest

from buer.stdout_parser import _pytest_cases, _parse_pytest, parse_test_output


# ---------------------------------------------------------------------------
# 1. Full -v block with mixed statuses and parametrize / class method
# ---------------------------------------------------------------------------

_VERBOSE_BLOCK = """\
tests/test_auth.py::test_login PASSED                              [ 10%]
tests/test_auth.py::test_signup[case0] FAILED                      [ 20%]
tests/test_auth.py::TestAuth::test_logout PASSED                   [ 30%]
tests/test_auth.py::test_reset SKIPPED (not ready)                 [ 40%]
=== 2 passed, 1 failed, 1 skipped in 0.42s ===
"""


def test_verbose_plain_function():
    cases = _pytest_cases(_VERBOSE_BLOCK)
    c = next(c for c in cases if c["name"] == "test_login")
    assert c["classname"] == "tests.test_auth"
    assert c["name"] == "test_login"
    assert c["status"] == "passed"
    assert c["file_path"] == "tests/test_auth.py"


def test_verbose_parametrized():
    cases = _pytest_cases(_VERBOSE_BLOCK)
    c = next(c for c in cases if c["name"] == "test_signup[case0]")
    assert c["classname"] == "tests.test_auth"
    assert c["status"] == "failed"


def test_verbose_class_method():
    cases = _pytest_cases(_VERBOSE_BLOCK)
    c = next(c for c in cases if c["name"] == "test_logout")
    assert c["classname"] == "tests.test_auth.TestAuth"
    assert c["status"] == "passed"


def test_verbose_skipped():
    cases = _pytest_cases(_VERBOSE_BLOCK)
    c = next(c for c in cases if c["name"] == "test_reset")
    assert c["status"] == "skipped"


# ---------------------------------------------------------------------------
# 2. Module conversion: strip .py, '/' -> '.'
# ---------------------------------------------------------------------------

def test_module_conversion_simple():
    output = "tests/test_utils.py::test_x PASSED\n=== 1 passed in 0.1s ===\n"
    cases = _pytest_cases(output)
    assert len(cases) == 1
    assert cases[0]["classname"] == "tests.test_utils"
    assert cases[0]["name"] == "test_x"


# ---------------------------------------------------------------------------
# 3. Class chain conversion: pkg/test_a.py::TestB::test_c
# ---------------------------------------------------------------------------

def test_class_chain_classname():
    output = "pkg/test_a.py::TestB::test_c FAILED\n=== 1 failed in 0.1s ===\n"
    cases = _pytest_cases(output)
    assert len(cases) == 1
    assert cases[0]["classname"] == "pkg.test_a.TestB"
    assert cases[0]["name"] == "test_c"
    assert cases[0]["status"] == "failed"


# ---------------------------------------------------------------------------
# 4. Non -v output (summary only) -> empty cases list
# ---------------------------------------------------------------------------

def test_non_verbose_output_empty_cases():
    output = "=== 3 passed, 1 failed in 1.23s ===\n"
    result = _parse_pytest(output)
    assert result is not None
    assert result["cases"] == []


# ---------------------------------------------------------------------------
# 5. parse_test_output returns non-empty cases for -v output
# ---------------------------------------------------------------------------

def test_parse_test_output_returns_cases():
    result = parse_test_output("pytest -v", _VERBOSE_BLOCK)
    assert result is not None
    assert "cases" in result
    assert len(result["cases"]) > 0


# ---------------------------------------------------------------------------
# 6. Status mapping: XFAIL -> skipped, XPASS -> passed
# ---------------------------------------------------------------------------

def test_xfail_maps_to_skipped():
    output = "tests/test_x.py::test_foo XFAIL\n=== 1 xfailed in 0.1s ===\n"
    # _pytest_cases parses even without summary (summary line has no per-case format)
    cases = _pytest_cases(output)
    assert len(cases) == 1
    assert cases[0]["status"] == "skipped"


def test_xpass_maps_to_passed():
    output = "tests/test_x.py::test_bar XPASS\n=== 1 xpassed in 0.1s ===\n"
    cases = _pytest_cases(output)
    assert len(cases) == 1
    assert cases[0]["status"] == "passed"
