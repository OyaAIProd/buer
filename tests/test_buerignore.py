"""Tests for .buerignore user-declared directory exclusion (boundary.py)."""
from __future__ import annotations

import os

import pytest

from buer.boundary import _buerignore_dir_patterns, should_ingest


# ── helpers ───────────────────────────────────────────────────────────────────

def _write(tmp_path, rel: str, body: str = "") -> str:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return str(p)


def _buerignore(tmp_path, content: str) -> None:
    (tmp_path / ".buerignore").write_text(content, encoding="utf-8")
    # lru_cache must be cleared between tests — each test uses a fresh tmp_path
    # so the cache key (tmp_path str) differs; no explicit clear needed.


# ── 1. bare name ─────────────────────────────────────────────────────────────

def test_buerignore_bare(tmp_path):
    _buerignore(tmp_path, "sniff\n")
    fp = _write(tmp_path, "sniff/foo.py")
    assert should_ingest(os.path.realpath(fp), str(tmp_path)) is False


# ── 2. trailing slash ─────────────────────────────────────────────────────────

def test_buerignore_trailing(tmp_path):
    _buerignore(tmp_path, "sniff/\n")
    fp = _write(tmp_path, "sniff/foo.py")
    assert should_ingest(os.path.realpath(fp), str(tmp_path)) is False


# ── 3. leading slash (root-anchored) ─────────────────────────────────────────

def test_buerignore_leading(tmp_path):
    _buerignore(tmp_path, "/sniff\n")
    fp = _write(tmp_path, "sniff/foo.py")
    assert should_ingest(os.path.realpath(fp), str(tmp_path)) is False


# ── 4. absent .buerignore — no crash, normal files pass ──────────────────────

def test_buerignore_absent(tmp_path):
    # No .buerignore file at all
    fp = _write(tmp_path, "src/app.py")
    assert should_ingest(os.path.realpath(fp), str(tmp_path)) is True


# ── 5. .buerignore with sniff doesn't affect unrelated files ─────────────────

def test_buerignore_does_not_override_normal(tmp_path):
    _buerignore(tmp_path, "sniff\n")
    fp = _write(tmp_path, "src/app.py")
    assert should_ingest(os.path.realpath(fp), str(tmp_path)) is True


# ── 6. .buerignore and .gitignore combine independently ──────────────────────

def test_buerignore_and_gitignore_combine(tmp_path):
    (tmp_path / ".gitignore").write_text("build\n", encoding="utf-8")
    _buerignore(tmp_path, "sniff\n")
    fp_sniff = _write(tmp_path, "sniff/x.py")
    fp_build = _write(tmp_path, "build/out.py")
    fp_src   = _write(tmp_path, "src/ok.py")
    root = str(tmp_path)
    assert should_ingest(os.path.realpath(fp_sniff), root) is False
    assert should_ingest(os.path.realpath(fp_build), root) is False
    assert should_ingest(os.path.realpath(fp_src),   root) is True


# ── 7. malformed .buerignore doesn't crash ────────────────────────────────────

def test_buerignore_malformed(tmp_path):
    # Binary garbage — errors='ignore' in the parser must swallow it
    (tmp_path / ".buerignore").write_bytes(b"\xff\xfe\x00\x01garbage\nsniff\n")
    fp = _write(tmp_path, "sniff/foo.py")
    # Should not raise; result doesn't matter as long as no exception
    try:
        should_ingest(os.path.realpath(fp), str(tmp_path))
    except Exception as exc:
        pytest.fail(f"should_ingest raised on malformed .buerignore: {exc}")


# ── 8. no-signal intrusion: without vs with .buerignore ──────────────────────

def test_no_signal_intrusion_excluded(tmp_path):
    """sniff/ has no .git, not blacklisted, not in .gitignore.

    Without .buerignore: should_ingest returns True (buer cannot auto-detect).
    With .buerignore listing sniff: should_ingest returns False.
    """
    fp = _write(tmp_path, "sniff/karaoke.py")
    root = str(tmp_path)
    root_real = os.path.realpath(root)
    fp_real = os.path.realpath(fp)

    # No .buerignore → buer cannot distinguish sniff from a normal subdirectory
    assert should_ingest(fp_real, root_real) is True, \
        "Without .buerignore a no-signal directory must pass (confirms the gap)"

    # With .buerignore listing sniff → excluded.
    # Must clear lru_cache so the new file is re-read (cache is process-lifetime
    # by design; tests that mutate the filesystem must clear it explicitly).
    _buerignore(tmp_path, "sniff\n")
    _buerignore_dir_patterns.cache_clear()
    assert should_ingest(fp_real, root_real) is False, \
        "With .buerignore listing sniff the file must be excluded"
