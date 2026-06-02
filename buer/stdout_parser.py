"""BUER stdout test-capture parser — §4.4 real-time test ingestion.

Parses terminal output from test runner commands to extract run-level
pass/fail counts.  Supplements §4.5 JUnit XML ingestion for real-time
feedback when the XML report hasn't landed on disk yet.

Honest boundary (§4.4):
  - Run-level totals only.  Per-testcase parsing is intentionally NOT
    attempted — stdout format varies across frameworks and versions;
    fragile parsing introduces false data (宁漏不误报).
  - No coverage_map data from stdout (test_case → define attribution
    requires XML/lcov with line information).
  - Parse failures are silent: None return means caller must NOT record
    a run.  Unknown formats are dropped, never guessed.
  - source='stdout' distinguishes these rows from XML-sourced rows so
    consumers can apply appropriate trust levels.

SDT 地位: 测试接入是数据源，无 SDT 对位，工程（同 §4.5）.
"""
from __future__ import annotations

import hashlib
import re
from typing import Optional


# ── test-command detection ─────────────────────────────────────────────────────

_TEST_RUNNER_RE = re.compile(
    r"\b("
    r"pytest|"
    r"jest|vitest|mocha|"
    r"npm\s+(?:run\s+)?test|"
    r"npx\s+(?:jest|vitest|mocha)|"
    r"go\s+test|"
    r"cargo\s+test"
    r")\b",
    re.IGNORECASE,
)


def is_test_command(command: str) -> bool:
    """True if command contains a known test runner invocation.

    Fast path: called for every Bash hook event; must be cheap.
    Non-test commands (ls, git, curl, …) return False immediately.
    """
    return bool(_TEST_RUNNER_RE.search(command))


# ── per-framework summary parsers ─────────────────────────────────────────────
# Each parser returns {passed, failed, skipped, runner} or None.
# Parsers only extract the LAST summary line — intermediate output is ignored.

_PYTEST_PASSED_RE  = re.compile(r"(\d+)\s+passed",                   re.IGNORECASE)
_PYTEST_FAILED_RE  = re.compile(r"(\d+)\s+(?:failed|error)",         re.IGNORECASE)
_PYTEST_SKIPPED_RE = re.compile(r"(\d+)\s+skipped",                  re.IGNORECASE)


def _parse_pytest(output: str) -> Optional[dict]:
    """pytest summary line: '=== N passed, M failed … in Xs ==='"""
    if "===" not in output:
        return None
    summary = None
    for line in reversed(output.splitlines()):
        if "===" in line and ("passed" in line.lower() or "failed" in line.lower()
                              or "error" in line.lower()):
            summary = line
            break
    if not summary:
        return None
    passed  = int(m.group(1)) if (m := _PYTEST_PASSED_RE.search(summary))  else 0
    failed  = int(m.group(1)) if (m := _PYTEST_FAILED_RE.search(summary))  else 0
    skipped = int(m.group(1)) if (m := _PYTEST_SKIPPED_RE.search(summary)) else 0
    if passed == 0 and failed == 0:
        return None
    return {"passed": passed, "failed": failed, "skipped": skipped, "runner": "pytest"}


_JEST_PASSED_RE  = re.compile(r"(\d+)\s+passed",          re.IGNORECASE)
_JEST_FAILED_RE  = re.compile(r"(\d+)\s+failed",          re.IGNORECASE)
_JEST_SKIPPED_RE = re.compile(r"(\d+)\s+(?:skipped|pending)", re.IGNORECASE)


def _parse_jest(output: str) -> Optional[dict]:
    """jest/vitest summary line: 'Tests: N failed, M passed, T total'"""
    if "Tests:" not in output:
        return None
    for line in reversed(output.splitlines()):
        if "Tests:" in line:
            passed  = int(m.group(1)) if (m := _JEST_PASSED_RE.search(line))  else 0
            failed  = int(m.group(1)) if (m := _JEST_FAILED_RE.search(line))  else 0
            skipped = int(m.group(1)) if (m := _JEST_SKIPPED_RE.search(line)) else 0
            if passed > 0 or failed > 0:
                return {"passed": passed, "failed": failed, "skipped": skipped,
                        "runner": "jest"}
    return None


_GO_OK_RE   = re.compile(r"^ok\s+\S",             re.MULTILINE)
_GO_FAIL_RE = re.compile(r"^(?:FAIL|---\s*FAIL)",  re.MULTILINE)


def _parse_go_test(output: str) -> Optional[dict]:
    """go test: count ok/FAIL package lines as proxy for passed/failed.

    go test emits no single-line total; package-level ok/FAIL is the best
    available summary.  Documented as proxy in honest-boundary comment.
    """
    ok_n   = len(_GO_OK_RE.findall(output))
    fail_n = len(_GO_FAIL_RE.findall(output))
    if ok_n == 0 and fail_n == 0:
        return None
    return {"passed": ok_n, "failed": fail_n, "skipped": 0, "runner": "go_test"}


_CARGO_RE = re.compile(
    r"test result:\s+\w+\.\s+(\d+)\s+passed;\s+(\d+)\s+failed;\s+(\d+)\s+ignored",
    re.IGNORECASE,
)


def _parse_cargo(output: str) -> Optional[dict]:
    """cargo test: 'test result: ok. N passed; M failed; K ignored'"""
    passed = failed = skipped = 0
    found = False
    for m in _CARGO_RE.finditer(output):
        passed  += int(m.group(1))
        failed  += int(m.group(2))
        skipped += int(m.group(3))
        found = True
    if not found:
        return None
    return {"passed": passed, "failed": failed, "skipped": skipped, "runner": "cargo"}


_MOCHA_PASSING_RE = re.compile(r"(\d+)\s+passing", re.IGNORECASE)
_MOCHA_FAILING_RE = re.compile(r"(\d+)\s+failing",  re.IGNORECASE)
_MOCHA_PENDING_RE = re.compile(r"(\d+)\s+pending",  re.IGNORECASE)


def _parse_mocha(output: str) -> Optional[dict]:
    """mocha: 'N passing (Xms)' / 'M failing'"""
    pm = _MOCHA_PASSING_RE.search(output)
    fm = _MOCHA_FAILING_RE.search(output)
    if not pm and not fm:
        return None
    passed  = int(pm.group(1)) if pm else 0
    failed  = int(fm.group(1)) if fm else 0
    skipped = int(m.group(1)) if (m := _MOCHA_PENDING_RE.search(output)) else 0
    if passed == 0 and failed == 0:
        return None
    return {"passed": passed, "failed": failed, "skipped": skipped, "runner": "mocha"}


# ── dispatcher ────────────────────────────────────────────────────────────────

def parse_test_output(command: str, output: str) -> Optional[dict]:
    """Parse terminal output to extract run-level test counts.

    Tries the most-likely framework parser first (based on command),
    then falls back to trying all parsers.

    Returns {passed, failed, skipped, runner} or None.
    None means the format was unrecognized — caller MUST NOT record a run.
    宁漏不误报: silence is better than wrong counts.
    """
    if not output:
        return None

    cmd_lower = command.lower()

    # Ordered by command hint — avoid cross-framework false positives
    if "pytest" in cmd_lower:
        r = _parse_pytest(output)
        if r:
            return r
    if "vitest" in cmd_lower or "jest" in cmd_lower:
        r = _parse_jest(output)
        if r:
            return r
    if "go test" in cmd_lower or "go\ttest" in cmd_lower:
        r = _parse_go_test(output)
        if r:
            return r
    if "cargo" in cmd_lower:
        r = _parse_cargo(output)
        if r:
            return r
    if "mocha" in cmd_lower:
        r = _parse_mocha(output)
        if r:
            return r
    # npm/npx: try jest first (most common), then mocha
    if "npm" in cmd_lower or "npx" in cmd_lower:
        r = _parse_jest(output) or _parse_mocha(output)
        if r:
            return r

    # Generic fallback: try all parsers
    for parser in (_parse_pytest, _parse_jest, _parse_cargo, _parse_mocha, _parse_go_test):
        r = parser(output)
        if r:
            return r

    return None  # unknown format — drop silently


def cmd_fingerprint(command: str) -> str:
    """12-char SHA-1 hex of normalized command. Used as source_path for stdout runs."""
    normalized = re.sub(r"\s+", " ", command.strip().lower())
    return hashlib.sha1(normalized.encode()).hexdigest()[:12]
