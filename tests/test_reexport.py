"""Tests for re-export penetration (barrel named + aliased + chained, batch 1)."""
from __future__ import annotations

import os

import pytest

from buer.callgraph import SymbolIndex, _follow_reexport, build_symbol_index_from_store
from buer.parse import extract_reexports
from buer.reconcile import rebuild_call_edges_full, reconcile
from buer.store import Store


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_project(tmp_path, store: Store) -> int:
    return store.get_or_create_project(str(tmp_path))


def _ts(tmp_path, rel: str, body: str) -> str:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return str(p)


# ══════════════════════════════════════════════════════════════════════════════
# 1. extract_reexports — named
# ══════════════════════════════════════════════════════════════════════════════

def test_extract_reexports_named(tmp_path):
    barrel = _ts(tmp_path, "barrel.ts", "export { foo } from './real';")
    result = extract_reexports(str(barrel), root=str(tmp_path))
    assert len(result) == 1
    exported, tmod, tname = result[0]
    assert exported == "foo"
    assert tmod == "real"
    assert tname == "foo"


# ── 2. extract_reexports — aliased ───────────────────────────────────────────

def test_extract_reexports_aliased(tmp_path):
    barrel = _ts(tmp_path, "barrel.ts", "export { original as alias } from './real';")
    result = extract_reexports(str(barrel), root=str(tmp_path))
    assert len(result) == 1
    exported, tmod, tname = result[0]
    assert exported == "alias"
    assert tmod == "real"
    assert tname == "original"


# ── 3. extract_reexports — type-only excluded ─────────────────────────────────

def test_extract_reexports_excludes_type_only(tmp_path):
    barrel = _ts(tmp_path, "barrel.ts", "export type { Foo } from './types';")
    result = extract_reexports(str(barrel), root=str(tmp_path))
    assert result == []


# ── 4. extract_reexports — star produces wildcard record ──────────────────────

def test_extract_reexports_star(tmp_path):
    barrel = _ts(tmp_path, "barrel.ts", "export * from './real';")
    result = extract_reexports(str(barrel), root=str(tmp_path))
    assert ("*", "real", "*") in result, f"star must produce wildcard record; result={result}"


# ── 5. extract_reexports — local export (no 'from') excluded ──────────────────

def test_extract_reexports_excludes_local(tmp_path):
    barrel = _ts(tmp_path, "barrel.ts", "export function foo() { return 1; }")
    result = extract_reexports(str(barrel), root=str(tmp_path))
    assert result == []


# ══════════════════════════════════════════════════════════════════════════════
# 6. _follow_reexport — single hop
# ══════════════════════════════════════════════════════════════════════════════

def test_follow_reexport_direct():
    idx = SymbolIndex()
    idx.reexport["barrel.foo"] = "real.foo"
    idx.qualified.add("real.foo")
    assert _follow_reexport("barrel.foo", idx) == "real.foo"


# ── 7. _follow_reexport — chained (multi-hop) ────────────────────────────────

def test_follow_reexport_chained():
    idx = SymbolIndex()
    idx.reexport["b2.foo"] = "b1.foo"
    idx.reexport["b1.foo"] = "real.foo"
    idx.qualified.add("real.foo")
    assert _follow_reexport("b2.foo", idx) == "real.foo"


# ── 8. _follow_reexport — cycle safe ─────────────────────────────────────────

def test_follow_reexport_cycle():
    idx = SymbolIndex()
    idx.reexport["a.x"] = "b.x"
    idx.reexport["b.x"] = "a.x"
    # Neither in qualified; should exit without infinite loop
    result = _follow_reexport("a.x", idx)
    assert result not in idx.qualified  # dead end, not an exception


# ══════════════════════════════════════════════════════════════════════════════
# 9. End-to-end: named barrel
# ══════════════════════════════════════════════════════════════════════════════

def test_e2e_named_barrel(tmp_path):
    _ts(tmp_path, "real.ts",    "export function fn() { return 1; }")
    _ts(tmp_path, "barrel.ts",  "export { fn } from './real';")
    _ts(tmp_path, "caller.ts",  "import { fn } from './barrel';\nexport function caller() { fn(); }")

    store = Store(":memory:")
    pid = _make_project(tmp_path, store)
    reconcile(store, pid, [
        str(tmp_path / "real.ts"),
        str(tmp_path / "barrel.ts"),
        str(tmp_path / "caller.ts"),
    ])

    callers = store.callers_of(pid, "js_ts::real.fn")
    assert any("caller" in c for c in callers), f"Expected penetrated edge to real.fn; got callers={callers}"
    store.close()


# ── 10. End-to-end: aliased barrel ───────────────────────────────────────────

def test_e2e_aliased_barrel(tmp_path):
    _ts(tmp_path, "real.ts",   "export function original() { return 1; }")
    _ts(tmp_path, "barrel.ts", "export { original as alias } from './real';")
    _ts(tmp_path, "caller.ts", "import { alias } from './barrel';\nexport function caller() { alias(); }")

    store = Store(":memory:")
    pid = _make_project(tmp_path, store)
    reconcile(store, pid, [
        str(tmp_path / "real.ts"),
        str(tmp_path / "barrel.ts"),
        str(tmp_path / "caller.ts"),
    ])

    callers = store.callers_of(pid, "js_ts::real.original")
    assert any("caller" in c for c in callers), f"Expected aliased edge to real.original; got {callers}"
    store.close()


# ── 11. End-to-end: chained barrel ───────────────────────────────────────────

def test_e2e_chained_barrel(tmp_path):
    _ts(tmp_path, "real.ts",    "export function fn() { return 1; }")
    _ts(tmp_path, "barrel.ts",  "export { fn } from './real';")
    _ts(tmp_path, "barrel2.ts", "export { fn } from './barrel';")
    _ts(tmp_path, "caller.ts",  "import { fn } from './barrel2';\nexport function caller() { fn(); }")

    store = Store(":memory:")
    pid = _make_project(tmp_path, store)
    reconcile(store, pid, [
        str(tmp_path / "real.ts"),
        str(tmp_path / "barrel.ts"),
        str(tmp_path / "barrel2.ts"),
        str(tmp_path / "caller.ts"),
    ])

    callers = store.callers_of(pid, "js_ts::real.fn")
    assert any("caller" in c for c in callers), f"Expected chained edge to real.fn; got {callers}"
    store.close()


# ── 12. Reconcile stores reexports; re-reconcile doesn't accumulate ───────────

def test_reconcile_stores_reexports(tmp_path):
    _ts(tmp_path, "real.ts",   "export function fn() { return 1; }")
    _ts(tmp_path, "barrel.ts", "export { fn } from './real';")

    store = Store(":memory:")
    pid = _make_project(tmp_path, store)

    reconcile(store, pid, [str(tmp_path / "real.ts"), str(tmp_path / "barrel.ts")])
    rows1 = store.all_reexports(pid)
    assert any(r["barrel_module"] == "barrel" and r["exported_name"] == "fn" for r in rows1)

    # Second reconcile must not accumulate duplicates
    reconcile(store, pid, [str(tmp_path / "barrel.ts")])
    rows2 = store.all_reexports(pid)
    barrel_rows = [r for r in rows2 if r["barrel_module"] == "barrel"]
    assert len(barrel_rows) == len([r for r in rows1 if r["barrel_module"] == "barrel"])
    store.close()


# ── 13. Delete barrel file → reexport_edges cleaned up ────────────────────────

def test_reconcile_deletes_reexports_on_file_deletion(tmp_path):
    _ts(tmp_path, "real.ts",   "export function fn() { return 1; }")
    barrel_path = str(tmp_path / "barrel.ts")
    _ts(tmp_path, "barrel.ts", "export { fn } from './real';")

    store = Store(":memory:")
    pid = _make_project(tmp_path, store)

    reconcile(store, pid, [str(tmp_path / "real.ts"), barrel_path])
    rows = store.all_reexports(pid)
    assert any(r["barrel_module"] == "barrel" for r in rows)

    # Delete the barrel file and reconcile it
    os.remove(barrel_path)
    reconcile(store, pid, [barrel_path])

    rows_after = store.all_reexports(pid)
    assert not any(r["barrel_module"] == "barrel" for r in rows_after)
    store.close()


# ── 14. Pure barrel (no local defines) → reexport stored ──────────────────────

def test_pure_barrel_in_files_parsed(tmp_path):
    # barrel.ts has NO local defines — only re-exports
    _ts(tmp_path, "real.ts",   "export function fn() { return 1; }")
    _ts(tmp_path, "barrel.ts", "export { fn } from './real';")

    store = Store(":memory:")
    pid = _make_project(tmp_path, store)
    reconcile(store, pid, [str(tmp_path / "real.ts"), str(tmp_path / "barrel.ts")])

    rows = store.all_reexports(pid)
    barrel_rows = [r for r in rows if r["barrel_module"] == "barrel"]
    assert len(barrel_rows) == 1
    assert barrel_rows[0]["exported_name"] == "fn"
    assert barrel_rows[0]["target_name"] == "fn"
    store.close()


# ── 15. rebuild_call_edges_full repairs batch-ordering miss ───────────────────

def test_rebuild_call_edges_full_repairs_barrel_miss(tmp_path):
    """Simulates full-ingest batch ordering: caller reconciled before barrel.

    Batch 1: caller.ts (barrel reexport not yet in DB → edge misses)
    Batch 2: real.ts + barrel.ts (reexports written, call_edges rebuilt for these)
    After both batches: caller.ts still has the wrong call_edges from batch 1.
    rebuild_call_edges_full() must repair the miss.
    """
    _ts(tmp_path, "real.ts",   "export function fn() { return 1; }")
    _ts(tmp_path, "barrel.ts", "export { fn } from './real';")
    _ts(tmp_path, "caller.ts", "import { fn } from './barrel';\nexport function caller() { fn(); }")

    store = Store(":memory:")
    pid = _make_project(tmp_path, store)

    # Batch 1: caller only — barrel reexport not yet in DB
    reconcile(store, pid, [str(tmp_path / "caller.ts")])
    callers_before = store.callers_of(pid, "js_ts::real.fn")
    assert not any("caller" in c for c in callers_before), \
        "Edge should be absent before barrel is reconciled"

    # Batch 2: real + barrel — reexports written, call_edges for these two rebuilt
    reconcile(store, pid, [str(tmp_path / "real.ts"), str(tmp_path / "barrel.ts")])
    # caller.ts still has stale call_edges from batch 1 (barrel wasn't in idx then)
    # rebuild_call_edges_full repairs the miss
    n = rebuild_call_edges_full(store, pid)
    assert n >= 1

    callers_after = store.callers_of(pid, "js_ts::real.fn")
    assert any("caller" in c for c in callers_after), \
        f"rebuild_call_edges_full must repair barrel-miss; got callers={callers_after}"
    store.close()
