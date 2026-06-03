"""BUER §4.5 — test-artifact ingestion layer.

Parses JUnit XML and coverage files into test_runs / test_cases / coverage_map.
Pure data ingestion — no signal detection here.  Signals (debug_loop, regression,
tampering) consume this data in a later phase.

R2 compliance: everything is derived from standard test-artifact files on disk.
The agent declares nothing — file path, mtime, pass/fail counts, and define
coverage are all read from the artifacts themselves.

Public API
----------
scan_test_results(store, project_id, root) — called by reconcile after each edit
"""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Optional

from buer.store import Store

# ---------------------------------------------------------------------------
# Heuristic artifact locations (§4.5 "常见位置启发，可配置，待校准")
# ---------------------------------------------------------------------------

_JUNIT_HEURISTICS = [
    "junit.xml",
    "test-results/junit.xml",
    "test-results.xml",
    "pytest-results.xml",
    ".pytest_cache/junit.xml",
    "reports/junit.xml",
    "build/test-results/junit.xml",   # Gradle
    "target/surefire-reports/*.xml",  # Maven (glob pattern, handled separately)
]

_COVERAGE_HEURISTICS = [
    "coverage.xml",          # pytest --cov (Cobertura format)
    "coverage/coverage.xml",
    "lcov.info",
    "coverage/lcov.info",
    ".coverage.xml",
]


def _mtime_iso(path: str) -> str:
    """Return file mtime as ISO-8601 UTC string (seconds precision)."""
    ts = os.path.getmtime(path)
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _glob_paths(pattern: str) -> list[str]:
    """Expand a simple glob pattern (only '*' in basename supported)."""
    import glob
    return glob.glob(pattern)


# ---------------------------------------------------------------------------
# Artifact location
# ---------------------------------------------------------------------------

def locate_junit_xml(root: str, configured_path: Optional[str]) -> list[str]:
    """Return existing JUnit XML paths to ingest.

    Priority: project-configured path first, then heuristic locations under root.
    """
    if configured_path and os.path.isfile(configured_path):
        return [configured_path]

    found = []
    for hint in _JUNIT_HEURISTICS:
        if "*" in hint:
            found.extend(p for p in _glob_paths(os.path.join(root, hint)) if os.path.isfile(p))
        else:
            p = os.path.join(root, hint)
            if os.path.isfile(p):
                found.append(p)
    return found


def locate_coverage(root: str, configured_path: Optional[str] = None) -> list[str]:
    """Return existing coverage-file paths to ingest."""
    if configured_path and os.path.isfile(configured_path):
        return [configured_path]

    found = []
    for hint in _COVERAGE_HEURISTICS:
        p = os.path.join(root, hint)
        if os.path.isfile(p):
            found.append(p)
    return found


# ---------------------------------------------------------------------------
# JUnit XML parsing
# ---------------------------------------------------------------------------

def _status_from_testcase(tc_elem) -> str:
    """Derive status string from a <testcase> element."""
    if tc_elem.find("failure") is not None:
        return "failed"
    if tc_elem.find("error") is not None:
        return "error"
    if tc_elem.find("skipped") is not None:
        return "skipped"
    return "passed"


def _file_from_classname(classname: str, root: str) -> Optional[str]:
    """Heuristic: map classname (dotted module path) to a source file path.

    e.g. "tests.test_auth" → "<root>/tests/test_auth.py"
    Returns None when the file doesn't exist (no false positives).
    """
    if not classname:
        return None
    rel = classname.replace(".", os.sep) + ".py"
    candidate = os.path.join(root, rel)
    return candidate if os.path.isfile(candidate) else None


def parse_junit_xml(path: str, root: str) -> dict:
    """Parse a JUnit XML report into a plain dict.

    Returns:
        {
            "passed": int, "failed": int, "skipped": int,
            "cases": [{"classname", "name", "file_path", "status"}, ...],
        }
    Handles both <testsuites><testsuite> and bare <testsuite> roots (cross-framework).
    """
    tree = ET.parse(path)
    xml_root = tree.getroot()

    # Collect all <testsuite> elements (handles nested and flat structures)
    if xml_root.tag == "testsuites":
        suites = list(xml_root.iter("testsuite"))
    elif xml_root.tag == "testsuite":
        suites = [xml_root] + [ts for ts in xml_root.iter("testsuite") if ts is not xml_root]
    else:
        suites = list(xml_root.iter("testsuite"))

    cases = []
    passed = failed = skipped = 0

    for suite in suites:
        for tc in suite.findall("testcase"):
            classname = tc.get("classname", "")
            name = tc.get("name", "")
            status = _status_from_testcase(tc)
            file_path = tc.get("file") or _file_from_classname(classname, root)

            cases.append({
                "classname": classname,
                "name": name,
                "file_path": file_path,
                "status": status,
            })

            if status == "passed":
                passed += 1
            elif status in ("failed", "error"):
                failed += 1
            else:
                skipped += 1

    return {"passed": passed, "failed": failed, "skipped": skipped, "cases": cases}


# ---------------------------------------------------------------------------
# Coverage parsing — Cobertura XML (coverage.xml)
# ---------------------------------------------------------------------------

def _parse_cobertura(path: str) -> list[tuple[str, str]]:
    """Parse Cobertura XML → [(test_case_id, define_name), ...].

    Cobertura doesn't embed per-testcase line attribution directly; instead we
    map covered <class name="…"> → define candidates via class/method names.
    When a <testcase> element appears inside a Cobertura report (some tools embed
    them), use the precise mapping.  Otherwise fall back to class-level attribution
    tagged to the suite name.

    Returns list of (test_case, define_name) pairs for coverage_map insertion.
    """
    tree = ET.parse(path)
    xml_root = tree.getroot()

    entries: list[tuple[str, str]] = []

    # Check for embedded testcase elements (some tools like pytest-cov emit these)
    testcases = list(xml_root.iter("testcase"))
    if testcases:
        for tc in testcases:
            tc_id = f"{tc.get('classname', '')}::{tc.get('name', '')}"
            # Each testcase may have <covered_lines> referencing class methods
            for covered in tc.findall("covered_class"):
                cls = covered.get("name", "")
                for method in covered.findall("method"):
                    define = method.get("name", "")
                    if cls and define:
                        entries.append((tc_id, f"{cls}.{define}"))
                if cls and not covered.findall("method"):
                    entries.append((tc_id, cls))
        return entries

    # Standard Cobertura: class-level only.  Map each covered class/method to a
    # synthetic test_case id built from the package + class name ("suite::class").
    for pkg in xml_root.iter("package"):
        pkg_name = pkg.get("name", "")
        for cls in pkg.iter("class"):
            cls_name = cls.get("name", "")
            full_cls = f"{pkg_name}.{cls_name}".strip(".")
            # Treat the class itself as a define and attribute it to a
            # synthetic "coverage::ClassName" test_case (heuristic tier).
            tc_id = f"coverage::{full_cls}"
            entries.append((tc_id, full_cls))
            for method in cls.iter("method"):
                m_name = method.get("name", "")
                if m_name:
                    entries.append((tc_id, f"{full_cls}.{m_name}"))

    return entries


# ---------------------------------------------------------------------------
# Coverage parsing — lcov
# ---------------------------------------------------------------------------

def _heuristic_define_from_line(line_no: int, source_path: str) -> Optional[str]:
    """Best-effort: read the source file and find which define contains line_no.

    Returns None if file unreadable or line not inside a function/class.
    This is a lightweight regex scan — not a full AST parse.
    """
    try:
        with open(source_path) as f:
            lines = f.readlines()
    except OSError:
        return None

    # Walk backward from line_no to find the nearest def/class header
    for i in range(min(line_no - 1, len(lines) - 1), -1, -1):
        m = re.match(r"^(?:def|class|async def)\s+(\w+)", lines[i].lstrip())
        if m:
            return m.group(1)
    return None


def _parse_lcov(path: str) -> list[tuple[str, str]]:
    """Parse lcov.info → [(test_case_id, define_name), ...].

    lcov format: TN:<test name>, SF:<source file>, DA:<line>,<hits>, end_of_record.
    Map covered lines back to define names via heuristic reverse lookup.
    """
    entries: list[tuple[str, str]] = []
    current_test = ""
    current_source = ""
    covered_lines: list[int] = []

    with open(path) as f:
        for raw in f:
            line = raw.strip()
            if line.startswith("TN:"):
                current_test = line[3:].strip() or "lcov"
            elif line.startswith("SF:"):
                current_source = line[3:].strip()
                covered_lines = []
            elif line.startswith("DA:"):
                parts = line[3:].split(",")
                if len(parts) >= 2 and parts[1] != "0":
                    try:
                        covered_lines.append(int(parts[0]))
                    except ValueError:
                        pass
            elif line == "end_of_record":
                seen_defines: set[str] = set()
                for ln in covered_lines:
                    define = _heuristic_define_from_line(ln, current_source)
                    if define and define not in seen_defines:
                        seen_defines.add(define)
                        tc_id = f"{current_test}::{os.path.basename(current_source)}"
                        entries.append((tc_id, define))
                current_source = ""
                covered_lines = []

    return entries


def parse_coverage(path: str) -> list[tuple[str, str]]:
    """Dispatch to the right coverage parser based on file extension/content."""
    if path.endswith(".info") or "lcov" in os.path.basename(path).lower():
        return _parse_lcov(path)
    return _parse_cobertura(path)


# ---------------------------------------------------------------------------
# Heuristic testcase ↔ define association (§4.5 启发档, §5.3 标降级)
# ---------------------------------------------------------------------------

_TEST_PREFIX_RE = re.compile(
    r"^(?:test_?|Test|spec_?|Spec|check_?|Check|it_?|should_?)",
    re.IGNORECASE,
)


def _is_file_path_classname(classname: str) -> bool:
    """True if classname looks like a file path rather than a dotted class name.

    vitest JUnit XML emits the test file path as classname
    (e.g. "tests/capability/normalizers.test.ts"), not a Python-style
    "pkg.Module.Class" name.  Detecting this avoids rsplit(".",1) returning
    the file extension ("ts") instead of a useful symbol name.
    """
    return "/" in classname or bool(re.search(r"\.(ts|tsx|js|jsx|test|spec)\b", classname))


def _stem_from_file_classname(classname: str) -> str:
    """Extract the module stem from a file-path classname.

    "tests/capability/normalizers.test.ts" → "normalizers"
    "src/utils.spec.js"                   → "utils"

    Returns "" when nothing useful can be extracted.
    """
    basename = os.path.basename(classname)
    # Strip known test/spec suffixes and the file extension repeatedly.
    # Order matters: strip ".test.ts" before ".ts".
    for suffix in (".test.ts", ".spec.ts", ".test.tsx", ".spec.tsx",
                   ".test.js", ".spec.js", ".test.jsx", ".spec.jsx",
                   ".ts", ".tsx", ".js", ".jsx"):
        if basename.endswith(suffix):
            basename = basename[: -len(suffix)]
            break
    return basename


def heuristic_defines_for_testcase(
    classname: str, name: str
) -> list[str]:
    """§5.3 降级档: infer likely define names from testcase naming conventions.

    Handles two naming styles (honest boundary: heuristic only, not reliable):

    pytest / unittest style:
        test_validate_token  → ["validate_token"]
        TestTokenExpiry      → ["TokenExpiry", "token_expiry"]   (class)
        test_MyClass_method  → ["MyClass.method", "MyClass_method"]

    vitest / Jest style (JUnit XML: classname=file path, name="describe > test"):
        classname="tests/capability/normalizers.test.ts"
        name="clamp > passes values within range"
        → describe segment "clamp" added as highest-priority candidate
        → file stem "normalizers" added as lower-priority candidate

    The vitest describe-block name is NOT guaranteed to be a define name —
    developers may write describe('clamp helper', ...) or describe('utils').
    This extraction improves hit-rate but remains heuristic.  Precise
    test↔define association still requires coverage_map (precise tier).

    Returns candidates ordered by confidence (most specific first).
    Returns [] when no convention match found.
    """
    candidates: list[str] = []

    # ── vitest / Jest: split name on " > " (describe > test hierarchy) ────────
    if " > " in name:
        segments = [s.strip() for s in name.split(" > ")]
        for i, seg in enumerate(segments[:-1]):  # all but the leaf test description
            seg_stripped = _TEST_PREFIX_RE.sub("", seg).strip("_")
            if not seg_stripped:
                continue
            # First (outermost) segment is highest-confidence candidate
            if seg_stripped not in candidates:
                candidates.append(seg_stripped)
            snake = re.sub(r"(?<=[a-z])(?=[A-Z])", "_", seg_stripped).lower()
            if snake != seg_stripped and snake not in candidates:
                candidates.append(snake)
        # Fall through: also run the standard prefix-strip on the full name
        # (handles edge cases like "test_clamp > edge case")

    # ── Standard prefix-strip on the full test name (pytest / Jest test_ style) ─
    stripped = _TEST_PREFIX_RE.sub("", name).strip("_")
    # For vitest names the stripped result is the whole "clamp > passes…" string;
    # skip adding that as a candidate (it's too long to be a useful define name).
    if stripped and " > " not in stripped and stripped not in candidates:
        candidates.append(stripped)
        snake = re.sub(r"(?<=[a-z])(?=[A-Z])", "_", stripped).lower()
        if snake != stripped and snake not in candidates:
            candidates.append(snake)

    # ── classname: dotted Python class OR file path ───────────────────────────
    if classname:
        if _is_file_path_classname(classname):
            # vitest: extract module stem from file path
            stem = _stem_from_file_classname(classname)
            stem_stripped = _TEST_PREFIX_RE.sub("", stem).strip("_")
            if stem_stripped and stem_stripped not in candidates:
                candidates.append(stem_stripped)
        else:
            # pytest: last dotted component → strip test prefix
            cls_simple = classname.rsplit(".", 1)[-1]
            cls_stripped = _TEST_PREFIX_RE.sub("", cls_simple).strip("_")
            if cls_stripped and cls_stripped not in candidates:
                candidates.append(cls_stripped)
                # "TestMyClass" → "MyClass.method" if name gave "method"
                if stripped and " > " not in stripped:
                    qualified = f"{cls_stripped}.{stripped}"
                    if qualified not in candidates:
                        candidates.append(qualified)

    return candidates


# ---------------------------------------------------------------------------
# File-level naming convention matcher (§改动1)
# ---------------------------------------------------------------------------

_TC_FILE_SUFFIXES = (
    ".test.ts", ".spec.ts", ".test.tsx", ".spec.tsx",
    ".test.js", ".spec.js", ".test.jsx", ".spec.jsx",
    ".py", ".ts", ".tsx", ".js", ".jsx",
)


def test_stem_matches_source(test_identifier: str, source_file_path: str) -> bool:
    """True when test_identifier names the same module as source_file_path by convention.

    Accepts a test file path OR a dotted classname as test_identifier.
    Strips test_ prefix / _test/_spec suffix, then compares stems with strict equality.
    """
    source_stem = os.path.splitext(os.path.basename(source_file_path))[0]
    if not source_stem:
        return False

    if "/" in test_identifier or os.sep in test_identifier or _is_file_path_classname(test_identifier):
        # File path — extract and normalise the basename
        basename = os.path.basename(test_identifier)
        for suffix in _TC_FILE_SUFFIXES:
            if basename.endswith(suffix):
                basename = basename[: -len(suffix)]
                break
        else:
            basename = os.path.splitext(basename)[0]
        test_stem = re.sub(r"^test_", "", basename, flags=re.IGNORECASE)
        test_stem = re.sub(r"(?:_test|_spec)$", "", test_stem, flags=re.IGNORECASE)
    else:
        # Dotted classname — find the test_ or _test segment
        segments = test_identifier.split(".")
        test_seg = next(
            (s for s in segments
             if re.match(r"^test_", s, re.IGNORECASE)
             or re.search(r"_test$", s, re.IGNORECASE)),
            None,
        )
        if test_seg is None:
            return False
        test_stem = re.sub(r"^test_", "", test_seg, flags=re.IGNORECASE)
        test_stem = re.sub(r"(?:_test|_spec)$", "", test_stem, flags=re.IGNORECASE)

    return bool(test_stem) and source_stem == test_stem


# ---------------------------------------------------------------------------
# Main ingestion entry point
# ---------------------------------------------------------------------------

def scan_test_results(store: Store, project_id: int, root: str) -> None:
    """§4.5 entry point called by reconcile after each edit cycle.

    1. Locate and ingest JUnit XML files (dedup by path + mtime).
    2. Locate and ingest coverage files into coverage_map (dedup same way).
    """
    project = store.get_project(project_id)
    configured_junit = project["test_report_path"] if project else None

    # ── JUnit XML ─────────────────────────────────────────────────────────────
    for xml_path in locate_junit_xml(root, configured_junit):
        mtime = _mtime_iso(xml_path)
        if store.test_run_already_ingested(project_id, xml_path, mtime):
            continue

        try:
            parsed = parse_junit_xml(xml_path, root)
        except Exception:
            continue  # malformed XML — skip silently, leave state unchanged

        nearest_seq = store.nearest_seq_for_mtime(project_id, mtime)
        run_id = store.insert_test_run(
            project_id,
            seq=nearest_seq,
            source_path=xml_path,
            source_mtime=mtime,
            passed=parsed["passed"],
            failed=parsed["failed"],
            skipped=parsed["skipped"],
        )
        for case in parsed["cases"]:
            store.insert_test_case(
                run_id=run_id,
                classname=case["classname"],
                name=case["name"],
                file_path=case["file_path"],
                status=case["status"],
            )

    # ── Coverage ──────────────────────────────────────────────────────────────
    for cov_path in locate_coverage(root):
        mtime = _mtime_iso(cov_path)
        # Reuse test_run_already_ingested for coverage files (same dedup key).
        if store.test_run_already_ingested(project_id, cov_path, mtime):
            continue

        try:
            entries = parse_coverage(cov_path)
        except Exception:
            continue

        for test_case, define_name in entries:
            store.insert_coverage_entry(project_id, test_case, define_name)

        # Record the coverage file as ingested so we don't re-process it.
        store.insert_test_run(
            project_id,
            seq=None,
            source_path=cov_path,
            source_mtime=mtime,
            passed=0,
            failed=0,
            skipped=0,
        )
