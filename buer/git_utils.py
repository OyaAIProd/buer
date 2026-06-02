"""BUER git integration utilities — batch 1 (foundation).

Thin wrappers around git CLI commands.  All functions degrade gracefully:
- not a git repo  → returns None / False
- git not installed → returns None / False
- command timeout  → returns None / False
- non-zero exit    → returns None / False

Never raises.  Never blocks the main buer pipeline.
"""
from __future__ import annotations

import subprocess
from typing import Optional

GIT_TIMEOUT = 2.0  # seconds — uniform timeout for all git commands


def _run_git(args: list[str], cwd: str) -> Optional[str]:
    """Run a git command in cwd; return stripped stdout or None on any failure."""
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            check=False,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def is_git_repo(root: str) -> bool:
    """True iff root is inside a git working tree (and git is available)."""
    return _run_git(["rev-parse", "--is-inside-work-tree"], root) == "true"


def get_head_commit(root: str) -> Optional[str]:
    """Return HEAD commit hash (full 40-char), or None if unavailable."""
    return _run_git(["rev-parse", "HEAD"], root)


def get_current_branch(root: str) -> Optional[str]:
    """Return current branch name, or 'detached@<hash7>' if detached HEAD,
    or None if not in a git repo / git unavailable.
    """
    branch = _run_git(["symbolic-ref", "--short", "HEAD"], root)
    if branch:
        return branch
    # Detached HEAD: use short hash as a synthetic branch name
    head = _run_git(["rev-parse", "--short=7", "HEAD"], root)
    if head:
        return f"detached@{head}"
    return None


def get_parent_commit(root: str, commit: str = "HEAD") -> Optional[str]:
    """Return parent commit of `commit`, or None (e.g. initial commit)."""
    return _run_git(["rev-parse", f"{commit}^"], root)
