"""BUER TS/TSX dataflow bridge — §4.2a Unit B2.

Toolchain detection and Node-script invocation for TypeScript type-driven
cross-define dataflow analysis.

Public API
----------
detect_ts_toolchain(project_root) -> dict
run_ts_dataflow_analysis(project_root, file_path, define_name, timeout) -> dict

Design contract
---------------
- Borrows the PROJECT's typescript package (node_modules/typescript), never
  ships its own.  If the project doesn't have typescript, degrade gracefully.
- The Node script (ts_analysis/analyze.mjs) is the only subprocess; it writes
  JSON to stdout and is timeout-protected.
- Both TS and TSX files are handled; language_tsx grammar supports JSX but
  the define/data-flow semantics are identical to plain TS.
- Go/Rust/Java are still in STATICALLY_TYPED (reserved) but no dataflow
  implementation exists for them yet; callers handle that case separately.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

# ── toolchain detection ────────────────────────────────────────────────────────

# Module-level cache — toolchain presence doesn't change during a server process.
_toolchain_cache: dict[str, dict] = {}


def detect_ts_toolchain(project_root: str) -> dict:
    """Return {"available": bool, "reason": str, "tsconfig": str|None}.

    Requirements for "available":
      1. tsconfig.json exists at project_root
      2. node_modules/typescript exists at project_root
      3. node executable is on PATH
    """
    if project_root in _toolchain_cache:
        return _toolchain_cache[project_root]
    result = _detect_uncached(project_root)
    _toolchain_cache[project_root] = result
    return result


def _detect_uncached(project_root: str) -> dict:
    tsconfig = os.path.join(project_root, "tsconfig.json")
    if not os.path.isfile(tsconfig):
        return {"available": False, "reason": "no tsconfig.json", "tsconfig": None}

    ts_pkg = os.path.join(project_root, "node_modules", "typescript")
    if not os.path.isdir(ts_pkg):
        return {"available": False, "reason": "typescript not in node_modules", "tsconfig": tsconfig}

    try:
        r = subprocess.run(["node", "--version"], capture_output=True, timeout=5)
        if r.returncode != 0:
            return {"available": False, "reason": "node not executable", "tsconfig": tsconfig}
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {"available": False, "reason": "node not found", "tsconfig": tsconfig}

    return {"available": True, "reason": "ok", "tsconfig": tsconfig}


# ── Node script invocation ─────────────────────────────────────────────────────

def _script_path() -> str:
    return str(Path(__file__).parent / "ts_analysis" / "analyze.mjs")


def run_ts_dataflow_analysis(
    project_root: str,
    file_path: str,
    define_name: str,
    timeout: int = 30,
) -> dict:
    """Run the TS dataflow analysis script and return its parsed output.

    Returns {"edges": [...], "errors": [...], "degraded": bool}.
    "degraded" is True if the analysis could not complete (timeout, crash, etc.);
    callers should fall back to call-graph approximation.

    edges are dicts: {"producer_file": str, "producer_define": str}.
    """
    payload = json.dumps({
        "project_root": project_root,
        "file_path": os.path.abspath(file_path),
        "define_name": define_name,
    }).encode()

    try:
        result = subprocess.run(
            ["node", _script_path()],
            input=payload,
            capture_output=True,
            timeout=timeout,
            cwd=project_root,
        )
    except subprocess.TimeoutExpired:
        return {"edges": [], "errors": ["analysis timeout"], "unresolved_calls": [], "degraded": True}
    except FileNotFoundError:
        return {"edges": [], "errors": ["node executable not found"], "unresolved_calls": [], "degraded": True}
    except OSError as e:
        return {"edges": [], "errors": [str(e)], "unresolved_calls": [], "degraded": True}

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")[:500]
        return {"edges": [], "errors": [stderr], "unresolved_calls": [], "degraded": True}

    try:
        data = json.loads(result.stdout.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return {"edges": [], "errors": [f"invalid JSON output: {e}"], "unresolved_calls": [], "degraded": True}

    return {
        "edges": data.get("edges", []),
        "errors": data.get("errors", []),
        "unresolved_calls": data.get("unresolved_calls", []),
        "degraded": False,
    }
