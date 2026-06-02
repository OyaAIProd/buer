"""BUER project boundary helpers — unified should_ingest decision.

Provides the canonical exclude decision used by reconcile, build_symbol_index,
and walk-based helpers (assists, health).  Three exclusion dimensions:
  1. Blacklisted directory names (build output, VCS dirs, caches)
  2. Files inside nested independent git repos (sub-project pollution)
  3. Directory-level patterns from the project's own .gitignore

.gitignore support is intentionally simplified: bare directory names, trailing-
slash patterns, and leading-slash (root-anchored) patterns are supported.
Not implemented: negation (!...), file globs (*.x), deep wildcards (a/**/b).
This covers the common cases (coverage/, dist/, .next/) without a pathspec dep.
"""
from __future__ import annotations

import os
from functools import lru_cache

# Unified blacklist — superset of the three ad-hoc skip sets previously
# scattered across callgraph.py, assists.py, and health.py.
EXCLUDED_DIR_NAMES: frozenset = frozenset({
    "node_modules", "__pycache__", ".venv", "venv", ".git", ".hg", ".svn",
    "dist", "build", ".next", ".nuxt", "target", ".pytest_cache", ".mypy_cache",
})


def is_excluded_dir(path_parts) -> bool:
    """True if any component of the (relative) path is a blacklisted dir name."""
    return any(part in EXCLUDED_DIR_NAMES for part in path_parts)


@lru_cache(maxsize=4096)
def _nested_repo_root(dir_path: str, project_root_real: str) -> bool:
    """True if dir_path contains a .git that is NOT the project's own .git.

    Cached: the same directory is queried many times across a reconcile batch.
    project_root's own .git is legitimate; only nested ones mark sub-projects.
    """
    if dir_path == project_root_real:
        return False
    return os.path.exists(os.path.join(dir_path, ".git"))


def in_nested_independent_project(file_real: str, project_root_real: str) -> bool:
    """True if file_real lives inside a nested independent git repo.

    Walks from the file's directory up to (but not including) project_root.
    If any intermediate directory contains its own .git, the file belongs to a
    nested independent project and should be excluded from the host graph.
    """
    if not file_real.startswith(project_root_real + os.sep):
        return False
    d = os.path.dirname(file_real)
    while d and d.startswith(project_root_real) and d != project_root_real:
        if _nested_repo_root(d, project_root_real):
            return True
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return False


@lru_cache(maxsize=64)
def _gitignore_dir_patterns(project_root_real: str) -> frozenset:
    """Parse project_root/.gitignore and return directory-level exclude names.

    Supports:
      bare name     `coverage`  → exclude any dir component named 'coverage'
      trailing /    `build/`    → exclude dir 'build'
      leading /     `/dist`     → exclude top-level dir 'dist'
    Not supported (out of scope): negation (!...), file globs (*.x),
    deep wildcards (a/**/b).  Cached per root; process-lifetime cache is safe
    because .gitignore rarely changes mid-session; server restart re-reads.
    """
    gi = os.path.join(project_root_real, ".gitignore")
    names: set = set()
    try:
        with open(gi, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("!"):
                    continue  # negation not supported
                pat = line.strip("/")
                # Skip file globs and nested-path patterns (not dir-level)
                if any(ch in pat for ch in ("*", "?", "[")) or "/" in pat:
                    continue
                if pat:
                    names.add(pat)
    except FileNotFoundError:
        pass
    except Exception:
        pass  # malformed .gitignore → no extra excludes, don't crash
    return frozenset(names)


@lru_cache(maxsize=64)
def _buerignore_dir_patterns(project_root_real: str) -> frozenset:
    """Parse project_root/.buerignore — user-declared directory exclusions.

    Same simplified directory-level syntax as .gitignore parsing:
      bare name `sniff`, trailing `sniff/`, leading `/sniff`.
    Purpose: exclude independent code that has NO objective signal (no own .git,
    not in .gitignore, not a blacklisted dir name) — buer cannot auto-detect such
    intrusions, so the user declares them here.
    Not supported: negation (!), file globs, deep wildcards (dir-level only).
    Absent or malformed .buerignore is silently ignored.
    """
    bi = os.path.join(project_root_real, ".buerignore")
    names: set = set()
    try:
        with open(bi, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("!"):
                    continue
                pat = line.strip("/")
                if any(ch in pat for ch in ("*", "?", "[")) or "/" in pat:
                    continue
                if pat:
                    names.add(pat)
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return frozenset(names)


def should_ingest(file_real: str, project_root_real: str) -> bool:
    """Canonical decision: should buer ingest this file into the project graph.

    Four exclusion dimensions (checked in order):
    1. Blacklisted directory names (build output, VCS dirs, caches)
    2. Files inside nested independent git repos (sub-project pollution)
    3. Directory-level patterns from project_root/.gitignore
    4. Directory-level patterns from project_root/.buerignore (user-declared)

    .buerignore is the escape hatch for independent code that carries no
    objective signal (no own .git, not .gitignore'd, not blacklisted) that
    buer cannot auto-detect. The user lists those directories in .buerignore.

    Does not handle test-file exclusion (caller's exclude_tests flag).
    Assumes both args are already realpath-resolved.
    """
    rel = os.path.relpath(file_real, project_root_real)
    parts = rel.split(os.sep)
    if is_excluded_dir(parts):
        return False
    if in_nested_independent_project(file_real, project_root_real):
        return False
    gi_patterns = _gitignore_dir_patterns(project_root_real)
    if gi_patterns and any(part in gi_patterns for part in parts):
        return False
    bi_patterns = _buerignore_dir_patterns(project_root_real)
    if bi_patterns and any(part in bi_patterns for part in parts):
        return False
    return True
