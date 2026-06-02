"""Tests for buer.git_utils — git command wrappers with graceful degradation."""
from __future__ import annotations

import subprocess
from unittest import mock

import pytest

from buer.git_utils import (
    GIT_TIMEOUT,
    _run_git,
    get_current_branch,
    get_head_commit,
    get_parent_commit,
    is_git_repo,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _init_git_repo(path) -> None:
    """Create a minimal git repo with one commit in path."""
    subprocess.run(["git", "init", "-b", "main"], cwd=str(path), check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(path),
                   check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path),
                   check=True, capture_output=True)
    (path / "f.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(path), check=True,
                   capture_output=True)


# ══════════════════════════════════════════════════════════════════════════════
# 1. Non-git directory
# ══════════════════════════════════════════════════════════════════════════════

def test_non_git_repo(tmp_path):
    """Plain directory with no .git → all functions return False/None."""
    assert is_git_repo(str(tmp_path)) is False
    assert get_head_commit(str(tmp_path)) is None
    assert get_current_branch(str(tmp_path)) is None


# ══════════════════════════════════════════════════════════════════════════════
# 2. Valid git repo
# ══════════════════════════════════════════════════════════════════════════════

def test_git_repo_basic(tmp_path):
    """Initialized git repo with one commit — all functions return valid values."""
    _init_git_repo(tmp_path)
    assert is_git_repo(str(tmp_path)) is True
    commit = get_head_commit(str(tmp_path))
    assert commit is not None and len(commit) == 40
    branch = get_current_branch(str(tmp_path))
    assert branch == "main"


def test_get_parent_commit(tmp_path):
    """get_parent_commit returns parent of HEAD after two commits."""
    _init_git_repo(tmp_path)
    first = get_head_commit(str(tmp_path))
    # Second commit
    (tmp_path / "g.txt").write_text("world")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "second"], cwd=str(tmp_path), check=True,
                   capture_output=True)
    parent = get_parent_commit(str(tmp_path))
    assert parent == first


def test_parent_commit_initial(tmp_path):
    """get_parent_commit on initial commit (no parent) returns None."""
    _init_git_repo(tmp_path)
    assert get_parent_commit(str(tmp_path)) is None


# ══════════════════════════════════════════════════════════════════════════════
# 3. Detached HEAD
# ══════════════════════════════════════════════════════════════════════════════

def test_detached_head(tmp_path):
    """Checkout to a specific commit → get_current_branch returns 'detached@<hash7>'."""
    _init_git_repo(tmp_path)
    commit = get_head_commit(str(tmp_path))
    subprocess.run(["git", "checkout", "--detach", commit], cwd=str(tmp_path),
                   check=True, capture_output=True)
    branch = get_current_branch(str(tmp_path))
    assert branch is not None
    assert branch.startswith("detached@")
    assert len(branch) == len("detached@") + 7


# ══════════════════════════════════════════════════════════════════════════════
# 4. Error conditions — graceful degradation
# ══════════════════════════════════════════════════════════════════════════════

def test_git_command_timeout(tmp_path):
    """Simulated timeout → function returns None without hanging."""
    with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("git", GIT_TIMEOUT)):
        assert get_head_commit(str(tmp_path)) is None
        assert get_current_branch(str(tmp_path)) is None
        assert is_git_repo(str(tmp_path)) is False


def test_git_not_installed(tmp_path):
    """FileNotFoundError (git not on PATH) → graceful None/False."""
    with mock.patch("subprocess.run", side_effect=FileNotFoundError):
        assert get_head_commit(str(tmp_path)) is None
        assert is_git_repo(str(tmp_path)) is False
