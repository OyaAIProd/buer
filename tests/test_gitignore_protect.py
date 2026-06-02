"""Tests for _buer_in_gitignore, _ensure_buer_gitignored, and mtime-gated helpers (§ auto-protect)."""
import os
import stat
import pytest
from buer.mcp.server import (
    _buer_in_gitignore,
    _ensure_buer_gitignored,
    _gitignore_changed,
    _gitignore_seen_mtime_path,
)


def _make_git_root(tmp_path):
    """Create a minimal git root (just .git dir)."""
    (tmp_path / ".git").mkdir()
    return str(tmp_path)


# ── _ensure_buer_gitignored: gate tests ──────────────────────────────────────

def test_noop_for_non_git(tmp_path):
    """Non-git project: _ensure must not create .gitignore."""
    root = str(tmp_path)
    _ensure_buer_gitignored(root)
    assert not (tmp_path / ".gitignore").exists()


def test_creates_gitignore_when_absent(tmp_path):
    """Git project with no .gitignore: creates one containing .buer/."""
    root = _make_git_root(tmp_path)
    _ensure_buer_gitignored(root)
    gi = tmp_path / ".gitignore"
    assert gi.exists()
    assert ".buer/" in gi.read_text()


def test_appends_when_gitignore_exists_no_buer(tmp_path):
    """Existing .gitignore without .buer: appends, preserves original content."""
    root = _make_git_root(tmp_path)
    gi = tmp_path / ".gitignore"
    gi.write_text("*.pyc\n__pycache__/\n")
    _ensure_buer_gitignored(root)
    content = gi.read_text()
    assert "*.pyc" in content
    assert "__pycache__/" in content
    assert ".buer/" in content


def test_no_line_joining_when_no_trailing_newline(tmp_path):
    """If existing .gitignore lacks trailing newline, appended .buer/ must be on its own line."""
    root = _make_git_root(tmp_path)
    gi = tmp_path / ".gitignore"
    gi.write_text("*.pyc")  # no trailing newline
    _ensure_buer_gitignored(root)
    lines = gi.read_text().splitlines()
    assert "*.pyc" in lines
    assert ".buer/" in lines


def test_idempotent_already_has_buer(tmp_path):
    """Already has .buer/: calling _ensure again must not append duplicates."""
    root = _make_git_root(tmp_path)
    gi = tmp_path / ".gitignore"
    gi.write_text("*.pyc\n.buer/\n")
    original = gi.read_text()
    _ensure_buer_gitignored(root)
    _ensure_buer_gitignored(root)
    assert gi.read_text() == original


def test_recreates_after_gitignore_deleted(tmp_path):
    """If .gitignore is deleted after first ensure, re-calling recreates it (tracking)."""
    root = _make_git_root(tmp_path)
    _ensure_buer_gitignored(root)
    (tmp_path / ".gitignore").unlink()
    _ensure_buer_gitignored(root)
    assert (tmp_path / ".gitignore").exists()
    assert ".buer/" in (tmp_path / ".gitignore").read_text()


def test_silent_on_readonly_gitignore(tmp_path):
    """Read-only .gitignore: _ensure must not raise (degrade silently)."""
    root = _make_git_root(tmp_path)
    gi = tmp_path / ".gitignore"
    gi.write_text("*.pyc\n")
    gi.chmod(stat.S_IRUSR | stat.S_IRGRP)
    try:
        _ensure_buer_gitignored(root)  # must not raise
    finally:
        gi.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)


# ── _buer_in_gitignore: variant recognition ──────────────────────────────────

@pytest.mark.parametrize("line", [
    ".buer/",
    ".buer",
    "/.buer/",
    "/.buer",
    ".buer/*",
    ".buer/**",
    "**/.buer",
    "**/.buer/",
])
def test_recognises_nonstandard_variants(tmp_path, line):
    """_buer_in_gitignore recognises common non-standard .buer ignore patterns."""
    root = _make_git_root(tmp_path)
    (tmp_path / ".gitignore").write_text(f"*.pyc\n{line}\n")
    assert _buer_in_gitignore(root) is True


def test_respects_explicit_opt_out(tmp_path):
    """!.buer/ (explicit negation) is treated as 'handled' — _ensure leaves it alone."""
    root = _make_git_root(tmp_path)
    (tmp_path / ".gitignore").write_text("!.buer/\n")
    assert _buer_in_gitignore(root) is True
    original = (tmp_path / ".gitignore").read_text()
    _ensure_buer_gitignored(root)
    assert (tmp_path / ".gitignore").read_text() == original


# ── _ensure_buer_gitignored: bool return value ───────────────────────────────

def test_ensure_returns_true_when_wrote(tmp_path):
    """_ensure_buer_gitignored returns True when it writes .gitignore."""
    root = _make_git_root(tmp_path)
    assert _ensure_buer_gitignored(root) is True


def test_ensure_returns_false_when_already_present(tmp_path):
    """_ensure_buer_gitignored returns False when .buer/ is already ignored."""
    root = _make_git_root(tmp_path)
    (tmp_path / ".gitignore").write_text(".buer/\n")
    assert _ensure_buer_gitignored(root) is False


def test_ensure_returns_false_for_non_git(tmp_path):
    """_ensure_buer_gitignored returns False for non-git directories."""
    assert _ensure_buer_gitignored(str(tmp_path)) is False


# ── _gitignore_changed: mtime gate ───────────────────────────────────────────

def test_gitignore_changed_absent_first_call(tmp_path):
    """No .gitignore, no marker: first call returns True and writes ABSENT marker."""
    root = _make_git_root(tmp_path)
    assert _gitignore_changed(root) is True
    seen = open(_gitignore_seen_mtime_path(root)).read().strip()
    assert seen == "ABSENT"


def test_gitignore_changed_absent_second_call(tmp_path):
    """Second call with still-absent .gitignore returns False."""
    root = _make_git_root(tmp_path)
    _gitignore_changed(root)  # prime
    assert _gitignore_changed(root) is False


def test_gitignore_changed_after_create(tmp_path):
    """After priming with ABSENT, creating .gitignore triggers True."""
    root = _make_git_root(tmp_path)
    _gitignore_changed(root)  # prime: ABSENT
    (tmp_path / ".gitignore").write_text("*.pyc\n")
    assert _gitignore_changed(root) is True


def test_gitignore_changed_stable_after_create(tmp_path):
    """After .gitignore created and re-primed, unchanged file returns False."""
    root = _make_git_root(tmp_path)
    (tmp_path / ".gitignore").write_text("*.pyc\n")
    _gitignore_changed(root)  # prime with real mtime
    assert _gitignore_changed(root) is False


def test_gitignore_changed_after_modification(tmp_path):
    """Modifying .gitignore after priming returns True again."""
    root = _make_git_root(tmp_path)
    gi = tmp_path / ".gitignore"
    gi.write_text("*.pyc\n")
    _gitignore_changed(root)  # prime
    gi.write_text("*.pyc\n__pycache__/\n")
    assert _gitignore_changed(root) is True


def test_gitignore_seen_mtime_path_location(tmp_path):
    """Marker file lives inside .buer/ subdirectory."""
    root = str(tmp_path)
    path = _gitignore_seen_mtime_path(root)
    assert path == os.path.join(root, ".buer", ".gitignore_seen_mtime")
