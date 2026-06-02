"""Tests for TS namespace re-export: export * as ns from './x'.

Verifies that extract_reexports emits ("ns.*", target_module, "*") so that
_follow_reexport's existing star-wildcard mechanism resolves ns.foo() calls
through the namespace barrel to the real callee.
"""
from __future__ import annotations

from buer.parse import extract_reexports
from buer.reconcile import reconcile
from buer.store import Store


# ── helpers ───────────────────────────────────────────────────────────────────

def _ts(tmp_path, rel: str, body: str) -> str:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return str(p)


def _reconcile(*files, tmp_path) -> tuple[Store, int]:
    store = Store(":memory:")
    pid = store.get_or_create_project(str(tmp_path))
    reconcile(store, pid, list(files))
    return store, pid


# ══════════════════════════════════════════════════════════════════════════════
# 1. Extraction
# ══════════════════════════════════════════════════════════════════════════════

def test_namespace_reexport_extracted(tmp_path):
    """export * as ns from './x' → extract_reexports returns ('ns.*', target, '*')."""
    barrel = _ts(tmp_path, "src/barrel.ts", "export * as ns from './x';")
    _ts(tmp_path, "src/x.ts", "export function foo() {}")
    result = extract_reexports(barrel, root=str(tmp_path))
    assert ("ns.*", "src/x", "*") in result, \
        f"namespace record missing; result={result}"


def test_aliased_namespace(tmp_path):
    """export * as utils from './helpers' → ('utils.*', target, '*')."""
    barrel = _ts(tmp_path, "lib/barrel.ts", "export * as utils from './helpers';")
    _ts(tmp_path, "lib/helpers.ts", "export function helper() {}")
    result = extract_reexports(barrel, root=str(tmp_path))
    assert ("utils.*", "lib/helpers", "*") in result, \
        f"aliased namespace record missing; result={result}"


def test_namespace_does_not_emit_bare_star(tmp_path):
    """export * as ns must not also emit a bare ('*', ...) record."""
    barrel = _ts(tmp_path, "src/barrel.ts", "export * as ns from './x';")
    _ts(tmp_path, "src/x.ts", "export function foo() {}")
    result = extract_reexports(barrel, root=str(tmp_path))
    bare_stars = [r for r in result if r[0] == "*"]
    assert bare_stars == [], \
        f"namespace export must not produce bare star; result={result}"


# ══════════════════════════════════════════════════════════════════════════════
# 2. End-to-end barrel penetration
# ══════════════════════════════════════════════════════════════════════════════

def test_namespace_reexport_edge_e2e(tmp_path):
    """x.ts(foo) + barrel(export * as ns from './x') + consumer(ns.foo())
    → reconcile builds use→js_ts::src/x.foo edge through namespace barrel."""
    _ts(tmp_path, "src/x.ts", "export function foo() { return 1; }")
    _ts(tmp_path, "src/barrel.ts", "export * as ns from './x';")
    _ts(tmp_path, "src/consumer.ts",
        "import { ns } from './barrel';\n"
        "export function use() { ns.foo(); }")

    store, pid = _reconcile(
        str(tmp_path / "src" / "x.ts"),
        str(tmp_path / "src" / "barrel.ts"),
        str(tmp_path / "src" / "consumer.ts"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "js_ts::src/x.foo")
    assert any("use" in c for c in callers), \
        f"namespace barrel must penetrate to src/x.foo; callers={callers}"
    store.close()


def test_namespace_alongside_named(tmp_path):
    """Barrel mixes namespace re-export and named re-export: both resolve."""
    _ts(tmp_path, "src/x.ts", "export function foo() {}")
    _ts(tmp_path, "src/y.ts", "export function bar() {}")
    _ts(tmp_path, "src/barrel.ts",
        "export * as ns from './x';\n"
        "export { bar } from './y';")
    _ts(tmp_path, "src/consumer.ts",
        "import { ns, bar } from './barrel';\n"
        "export function use() { ns.foo(); bar(); }")

    store, pid = _reconcile(
        str(tmp_path / "src" / "x.ts"),
        str(tmp_path / "src" / "y.ts"),
        str(tmp_path / "src" / "barrel.ts"),
        str(tmp_path / "src" / "consumer.ts"),
        tmp_path=tmp_path,
    )
    callers_foo = store.callers_of(pid, "js_ts::src/x.foo")
    callers_bar = store.callers_of(pid, "js_ts::src/y.bar")
    assert any("use" in c for c in callers_foo), \
        f"namespace ns.foo must resolve; callers_foo={callers_foo}"
    assert any("use" in c for c in callers_bar), \
        f"named bar must resolve; callers_bar={callers_bar}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# 3. Existing re-export forms unaffected (regression guard)
# ══════════════════════════════════════════════════════════════════════════════

def test_named_reexport_unaffected(tmp_path):
    """export { decode } from './auth' → still ('decode', target, 'decode')."""
    barrel = _ts(tmp_path, "src/index.ts", "export { decode } from './auth';")
    _ts(tmp_path, "src/auth.ts", "export function decode() {}")
    result = extract_reexports(barrel, root=str(tmp_path))
    assert ("decode", "src/auth", "decode") in result, \
        f"named re-export broken; result={result}"


def test_star_reexport_unaffected(tmp_path):
    """export * from './x' → still ('*', target, '*')."""
    barrel = _ts(tmp_path, "src/index.ts", "export * from './x';")
    _ts(tmp_path, "src/x.ts", "export function fn() {}")
    result = extract_reexports(barrel, root=str(tmp_path))
    assert ("*", "src/x", "*") in result, \
        f"star re-export broken; result={result}"


def test_default_reexport_unaffected(tmp_path):
    """export default function Page() → still ('<default>', mod, 'Page')."""
    f = _ts(tmp_path, "src/page.ts", "export default function Page() { return 1; }")
    result = extract_reexports(f, root=str(tmp_path))
    assert any(r[0] == "<default>" and r[2] == "Page" for r in result), \
        f"default re-export broken; result={result}"
