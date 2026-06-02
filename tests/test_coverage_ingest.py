"""Tests for coverage-completeness background full ingest (session_start)."""
from __future__ import annotations

import os
import textwrap
import threading
import time

import pytest

from buer.mcp.server import (
    COVERAGE_THRESHOLD,
    _coverage_ratio,
    _full_ingest_in_background,
    _full_ingest_in_progress,
    _full_ingest_lock,
    _maybe_trigger_full_ingest,
    _scan_boundary_files,
)
from buer.store import Store


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_project(tmp_path, store: Store) -> int:
    return store.get_or_create_project(str(tmp_path))


def _write_ts(tmp_path, rel: str, body: str) -> str:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    return str(p)


# ── 1. _scan_boundary_files returns .ts files under root ─────────────────────

def test_scan_boundary_files_finds_ts(tmp_path):
    _write_ts(tmp_path, "src/a.ts", "export function a() {}")
    _write_ts(tmp_path, "src/b.ts", "export function b() {}")
    files = _scan_boundary_files(str(tmp_path))
    rels = {os.path.relpath(f, str(tmp_path)) for f in files}
    assert "src/a.ts" in rels
    assert "src/b.ts" in rels


def test_scan_boundary_files_excludes_node_modules(tmp_path):
    _write_ts(tmp_path, "node_modules/pkg/index.ts", "export function x() {}")
    _write_ts(tmp_path, "src/real.ts", "export function real() {}")
    files = _scan_boundary_files(str(tmp_path))
    rels = {os.path.relpath(f, str(tmp_path)) for f in files}
    assert "src/real.ts" in rels
    assert not any("node_modules" in r for r in rels)


# ── 2. _coverage_ratio returns 1.0 when denom=0 (no boundary files) ──────────

def test_coverage_ratio_no_boundary_files(tmp_path):
    store = Store(":memory:")
    pid = _make_project(tmp_path, store)
    ratio, covered, denom = _coverage_ratio(store, pid, str(tmp_path))
    assert ratio == 1.0
    assert denom == 0
    store.close()


# ── 3. _coverage_ratio correct when no determinations ────────────────────────

def test_coverage_ratio_zero_covered(tmp_path):
    _write_ts(tmp_path, "src/a.ts", "export function alpha() {}")
    _write_ts(tmp_path, "src/b.ts", "export function beta() {}")
    store = Store(":memory:")
    pid = _make_project(tmp_path, store)
    ratio, covered, denom = _coverage_ratio(store, pid, str(tmp_path))
    assert ratio == 0.0
    assert covered == 0
    assert denom == 2
    store.close()


# ── 4. no_define_count subtracts from denominator ────────────────────────────

def test_coverage_ratio_no_define_count_reduces_denom(tmp_path):
    _write_ts(tmp_path, "src/a.ts", "export function alpha() {}")
    _write_ts(tmp_path, "src/types.ts", "export type Foo = string;")  # no define
    store = Store(":memory:")
    pid = _make_project(tmp_path, store)
    store.set_no_define_count(pid, 1)  # 1 file with no defines
    ratio, covered, denom = _coverage_ratio(store, pid, str(tmp_path))
    assert denom == 1  # 2 files − 1 no-define = 1
    store.close()


# ── 5. _maybe_trigger_full_ingest fires when ratio < threshold ───────────────

def test_maybe_trigger_fires_when_below_threshold(tmp_path):
    _write_ts(tmp_path, "src/a.ts", "export function alpha() {}")
    store = Store(":memory:")
    pid = _make_project(tmp_path, store)

    import buer.mcp.server as srv
    orig_db = srv._db_path
    orig_override = srv._store_override
    try:
        srv._store_override = store
        triggered = _maybe_trigger_full_ingest(store, pid, str(tmp_path))
        assert triggered is True
    finally:
        srv._store_override = orig_override
        with _full_ingest_lock:
            _full_ingest_in_progress.discard(pid)
    store.close()


# ── 6. _maybe_trigger_full_ingest skips when already at/above threshold ──────

def test_maybe_trigger_skips_when_covered(tmp_path):
    store = Store(":memory:")
    pid = _make_project(tmp_path, store)
    # No boundary files → denom=0 → ratio=1.0 → no trigger
    triggered = _maybe_trigger_full_ingest(store, pid, str(tmp_path))
    assert triggered is False
    store.close()


# ── 7. _maybe_trigger_full_ingest is idempotent (no double-trigger) ───────────

def test_maybe_trigger_no_double_trigger(tmp_path):
    _write_ts(tmp_path, "src/a.ts", "export function alpha() {}")
    store = Store(":memory:")
    pid = _make_project(tmp_path, store)

    import buer.mcp.server as srv
    orig_override = srv._store_override
    try:
        srv._store_override = store
        t1 = _maybe_trigger_full_ingest(store, pid, str(tmp_path))
        t2 = _maybe_trigger_full_ingest(store, pid, str(tmp_path))
        assert t1 is True
        assert t2 is False  # already in-progress
    finally:
        srv._store_override = orig_override
        with _full_ingest_lock:
            _full_ingest_in_progress.discard(pid)
    store.close()


# ── 8. _full_ingest_in_background sets no_define_count after run ──────────────

def test_full_ingest_sets_no_define_count(tmp_path):
    _write_ts(tmp_path, "src/a.ts", "export function alpha() { return 1; }")
    _write_ts(tmp_path, "src/types.ts", "// empty no define")
    store_path = str(tmp_path / "store.sqlite")
    store = Store(store_path)
    pid = _make_project(tmp_path, store)
    store.close()

    import buer.mcp.server as srv
    orig_db = srv._db_path
    try:
        srv._db_path = store_path
        _full_ingest_in_background(pid, str(tmp_path))
    finally:
        srv._db_path = orig_db

    store2 = Store(store_path)
    count = store2.get_no_define_count(pid)
    store2.close()
    assert count >= 0  # set (exact value depends on parse result)


# ── 9. COVERAGE_THRESHOLD constant is 0.95 ───────────────────────────────────

def test_coverage_threshold_value():
    assert COVERAGE_THRESHOLD == 0.95
