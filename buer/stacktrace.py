"""BUER stack trace parser — extracts production-code FQNs from command output.

Parses three verified formats:
  - tsx/Node.js:  "    at funcName (/abs/path/file.ts:line:col)"
  - vitest:       " ❯ funcName rel/path/file.ts:line:col"
  - Python:       '  File "/abs/path/file.py", line N, in func_name'

Filters out anonymous frames, test files, scripts, node internals, Python
stdlib, and site-packages.
Converts file paths to FQNs via module_name_of — same format as call_edges.

Public API
----------
has_stack_trace(output) -> bool                      # cheap signature check
parse_stack_frames(output, root) -> list[dict]        # [{fn, file_path, rel_path, line}]
stack_fqns(output, root) -> set[str]                  # call_edges-compatible FQN set
normalize_error_signature(output) -> str | None       # normalized "ErrorType|pattern"
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from buer.callgraph import module_name_of, _lang_fqn
from buer.parse import _JS_TEST_SUFFIXES, _PY_TEST_PREFIXES, _TEST_DIR_NAMES

# ── Error signature patterns ─────────────────────────────────────────────────

# Recognised JS/Python error type prefixes (first-word match on error line).
_ERROR_TYPE_RE = re.compile(
    r"^\s*(?P<etype>AssertionError|TypeError|ReferenceError|RangeError|SyntaxError|Error)"
    r"(?:\s*:\s*(?P<msg>.*))?$",
    re.MULTILINE,
)

# Ordered keyword → snake_case mapping.  First match wins.
_KEYWORD_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"to deeply equal",        re.IGNORECASE), "to_deeply_equal"),
    (re.compile(r"to equal",               re.IGNORECASE), "to_equal"),
    (re.compile(r"to be\b",               re.IGNORECASE), "to_be"),
    (re.compile(r"to throw",               re.IGNORECASE), "to_throw"),
    (re.compile(r"to match",               re.IGNORECASE), "to_match"),
    (re.compile(r"to contain",             re.IGNORECASE), "to_contain"),
    (re.compile(r"cannot read propert(?:y|ies)", re.IGNORECASE), "cannot_read_property"),
    (re.compile(r"is not a function",      re.IGNORECASE), "is_not_a_function"),
    (re.compile(r"is not defined",         re.IGNORECASE), "is_not_defined"),
    (re.compile(r"of undefined",           re.IGNORECASE), "of_undefined"),
    (re.compile(r"of null",               re.IGNORECASE), "of_null"),
]

# Fallback stripping: remove object literals, arrays, quoted strings, numbers.
_STRIP_LITERALS_RE = re.compile(
    r'\{[^}]*\}|\[[^\]]*\]|"[^"]*"|\'[^\']*\'|`[^`]*`|\b\d+(?:\.\d+)?\b'
)

# ── Compiled patterns ────────────────────────────────────────────────────────

# Fast signature check — before paying for full parse.
# Matches the first line of any supported stack format.
_STACK_SIGNATURE = re.compile(
    r'(?:'
    r'^\s{2,}at\s+\S+\s+\('                          # tsx/Node.js
    r'|^\s+❯\s+\S+\s+\S+\.(?:ts|tsx|js|jsx|py):\d'  # vitest
    r'|^\s+File "[^"]+\.py", line \d+'               # Python
    r')',
    re.MULTILINE,
)

# tsx/Node.js:  "    at funcName (/abs/path/to/file.ts:347:11)"
# Deliberately does NOT match "at <anonymous>" (fn group requires non-< start).
_RE_TSX = re.compile(
    r"^\s{2,}at\s+(?P<fn>[^\s(<][^\s(]*)\s+"
    r"\((?P<path>[^:)]+\.(?:ts|tsx|js|jsx|py)):(?P<line>\d+):\d+\)",
    re.MULTILINE,
)

# vitest:  " ❯ funcName lib/path/file.ts:347:11"
_RE_VITEST = re.compile(
    r"^\s+❯\s+(?P<fn>\S+)\s+(?P<path>[^:\s]+\.(?:ts|tsx|js|jsx|py)):(?P<line>\d+)",
    re.MULTILINE,
)

# Python:  '  File "/abs/path/to/file.py", line 42, in func_name'
# fn can be <module> or <lambda> — filtered by _is_anonymous.
_RE_PY = re.compile(
    r'^\s+File "(?P<path>[^"]+\.py)", line (?P<line>\d+), in (?P<fn>\S+)',
    re.MULTILINE,
)

# ── Filter constants ─────────────────────────────────────────────────────────

# fn values that are JS engine internals, never user code.
_INTERNAL_FNS: frozenset[str] = frozenset({
    "Object.<anonymous>",
    "Array.map", "Array.forEach", "Array.reduce", "Array.filter",
    "Module._compile", "Module.load", "Function._load",
    "Object.transformer", "wrapModuleLoad", "TracingChannel.traceSync",
    "resolveForCJSWithHooks", "defaultResolveImpl", "nextResolveSimple",
})

# Path component names that mark non-production code.
# _TEST_DIR_NAMES already covers tests/__tests__/e2e/cypress; add scripts.
_EXCLUDE_DIR_NAMES: frozenset[str] = _TEST_DIR_NAMES | frozenset({"scripts"})


# ── Internal helpers ─────────────────────────────────────────────────────────

def _is_anonymous(fn: str) -> bool:
    # fn.startswith("<") catches Python's <module>, <lambda>, plus JS <anonymous>
    return fn.startswith("<") or "<anonymous>" in fn or fn in _INTERNAL_FNS


def _is_excluded_path(raw_path: str, rel_path: str) -> bool:
    """Return True if this path should NOT be included in production frames."""
    # node: built-ins (node:internal/modules/...)
    if raw_path.startswith("node:"):
        return True
    # node_modules anywhere in the path
    if "node_modules/" in rel_path or "node_modules\\" in rel_path:
        return True
    # Python: third-party packages installed in site-packages / dist-packages
    if "site-packages/" in raw_path or "dist-packages/" in raw_path:
        return True
    # Python: stdlib paths like /usr/lib/python3.11/ or ~/.venv/lib/python3.11/
    if "/lib/python3." in raw_path or "/lib/python2." in raw_path:
        return True
    # directory component exclusions (tests/, scripts/, ...)
    parts = Path(rel_path).parts
    if any(part in _EXCLUDE_DIR_NAMES for part in parts):
        return True
    # JS/TS test file suffixes (.test.ts, .spec.ts, ...)
    name = Path(rel_path).name
    if any(name.endswith(s) for s in _JS_TEST_SUFFIXES):
        return True
    # Python test prefixes (test_foo.py)
    if any(name.startswith(p) for p in _PY_TEST_PREFIXES):
        return True
    return False


def _normalize(raw_path: str, root: str) -> tuple[str, str]:
    """Return (abs_path, rel_path) for a raw stack frame path.

    raw_path can be absolute (tsx) or relative (vitest).
    rel_path is relative to root, using the OS path separator.
    """
    if os.path.isabs(raw_path):
        abs_path = raw_path
        rel_path = os.path.relpath(raw_path, root)
    else:
        rel_path = raw_path
        abs_path = os.path.normpath(os.path.join(root, raw_path))
    return abs_path, rel_path


# ── Public API ───────────────────────────────────────────────────────────────

def has_stack_trace(output: str) -> bool:
    """Quick check: does output look like it contains a stack trace?

    Runs a single-pass regex on the raw string.  Cheap — safe to call on
    every post-bash invocation as a pre-filter before full parsing.
    """
    return bool(_STACK_SIGNATURE.search(output))


def parse_stack_frames(output: str, root: str) -> list[dict]:
    """Extract production-code stack frames from command output.

    Scans for both tsx/Node and vitest formats.  Deduplicates by
    (rel_path, fn).  Returns filtered frames only — no test, scripts,
    node_modules, or anonymous entries.

    Each frame dict: {fn, file_path (abs), rel_path, line (int)}.
    """
    seen: set[str] = set()
    frames: list[dict] = []

    for pattern in (_RE_TSX, _RE_VITEST, _RE_PY):
        for m in pattern.finditer(output):
            fn = m.group("fn")
            raw_path = m.group("path")
            line = m.group("line")

            if _is_anonymous(fn):
                continue

            abs_path, rel_path = _normalize(raw_path, root)

            if _is_excluded_path(raw_path, rel_path):
                continue

            key = f"{rel_path}\x00{fn}"
            if key in seen:
                continue
            seen.add(key)

            frames.append({
                "fn": fn,
                "file_path": abs_path,
                "rel_path": rel_path,
                "line": int(line),
            })

    return frames


def stack_fqns(output: str, root: str) -> set[str]:
    """Parse output, filter to production frames, and return FQN set.

    FQN format matches call_edges:
      "lib/db/postgres.getPool"   (TS — slash separators)
      "buer.callgraph.build_call_edges"  (Python — dot separators)

    Returns empty set when the output contains no parseable source frames
    (pure assertion failures, tsc errors, or no stack at all).
    """
    if not has_stack_trace(output):
        return set()

    fqns: set[str] = set()
    for frame in parse_stack_frames(output, root):
        try:
            mod = module_name_of(frame["file_path"], root)
            fqns.add(_lang_fqn(frame["file_path"], mod, frame["fn"]))
        except Exception:
            pass
    return fqns


def normalize_error_signature(output: str) -> str | None:
    """Extract a normalized error signature from command output.

    Returns "ErrorType|keyword_pattern" when a recognizable error is found, e.g.:
      "AssertionError|to_equal"
      "TypeError|cannot_read_property"
      "Error|is_not_a_function"

    Fallback: when no keyword matches, strips literals/numbers from the message
    and takes the first 40 chars of the remaining structure:
      "AssertionError|expected_str_to_be_value"

    Returns None when no error type header is found.

    Design goal: two runs of the same class of error (e.g. "expected 2 to equal 1"
    and "expected 5 to equal 3") must return the same signature.  Different error
    classes ("to_equal" vs "to_throw") must return different signatures.
    """
    m = _ERROR_TYPE_RE.search(output)
    if m is None:
        return None

    etype = m.group("etype")
    msg = (m.group("msg") or "").strip()

    # Try keyword patterns in order
    for pattern, label in _KEYWORD_PATTERNS:
        if pattern.search(msg):
            return f"{etype}|{label}"

    # Fallback: strip literals and normalise
    stripped = _STRIP_LITERALS_RE.sub("", msg)
    # Collapse whitespace and convert to snake_case-ish
    stripped = re.sub(r"\s+", "_", stripped.strip())
    stripped = re.sub(r"[^\w]", "", stripped)
    stripped = stripped[:40].lower()
    if stripped:
        return f"{etype}|{stripped}"
    return f"{etype}|unknown"
