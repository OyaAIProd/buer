"""Tests for default export resolution (JS/TS).

export default function Page() {} + import Foo from './page' + Foo()
→ edge to src/page.Page via <default> slot in reexport_edges.
"""
from __future__ import annotations

import pytest

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
# 1-2. extract_reexports — named function/class default
# ══════════════════════════════════════════════════════════════════════════════

def test_default_extract(tmp_path):
    """export default function Page → extract_reexports contains ('<default>', 'src/page', 'Page')."""
    page = _ts(tmp_path, "src/page.ts", "export default function Page() { return 1; }")
    result = extract_reexports(str(page), root=str(tmp_path))
    assert ("<default>", "src/page", "Page") in result, \
        f"default record missing; result={result}"


def test_default_class_extract(tmp_path):
    """export default class Foo → ('<default>', 'src/comp', 'Foo')."""
    comp = _ts(tmp_path, "src/comp.tsx", "export default class Foo {}")
    result = extract_reexports(str(comp), root=str(tmp_path))
    assert ("<default>", "src/comp", "Foo") in result, \
        f"class default record missing; result={result}"


def test_default_anonymous_no_record(tmp_path):
    """export default function(){} → no record (anonymous, no name)."""
    f = _ts(tmp_path, "src/anon.ts", "export default function() { return 1; }")
    result = extract_reexports(str(f), root=str(tmp_path))
    assert not any(r[0] == "<default>" for r in result), \
        f"anonymous default must not produce record; result={result}"


def test_default_object_no_record(tmp_path):
    """export default {a: 1} → no record (object literal)."""
    f = _ts(tmp_path, "src/obj.ts", "export default { a: 1 };")
    result = extract_reexports(str(f), root=str(tmp_path))
    assert not any(r[0] == "<default>" for r in result), \
        f"object default must not produce record; result={result}"


# ══════════════════════════════════════════════════════════════════════════════
# 3. End-to-end: named function default
# ══════════════════════════════════════════════════════════════════════════════

def test_default_func_named(tmp_path):
    """page.ts export default function Page; app.ts import Foo from './page'; Foo()
    → edge to src/page.Page (Foo≠Page but connected via default slot)."""
    _ts(tmp_path, "src/page.ts", "export default function Page() { return 1; }")
    _ts(tmp_path, "src/app.ts",
        "import Foo from './page';\nexport function run() { Foo(); }")

    store, pid = _reconcile(
        str(tmp_path / "src" / "page.ts"),
        str(tmp_path / "src" / "app.ts"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "js_ts::src/page.Page")
    assert any("run" in c for c in callers), \
        f"default must map Foo → src/page.Page; callers={callers}"
    store.close()


def test_default_class_named(tmp_path):
    """export default class Foo; import Bar from './comp'; Bar() → edge to src/comp.Foo."""
    _ts(tmp_path, "src/comp.tsx", "export default class Foo {}")
    _ts(tmp_path, "src/app.ts",
        "import Bar from './comp';\nexport function run() { Bar(); }")

    store, pid = _reconcile(
        str(tmp_path / "src" / "comp.tsx"),
        str(tmp_path / "src" / "app.ts"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "js_ts::src/comp.Foo")
    assert any("run" in c for c in callers), \
        f"default class must map Bar → src/comp.Foo; callers={callers}"
    store.close()


def test_default_reference(tmp_path):
    """const Page=()=>1; export default Page; import X; X() → edge to src/page.Page."""
    _ts(tmp_path, "src/page.ts",
        "function Page() { return 1; }\nexport default Page;")
    _ts(tmp_path, "src/app.ts",
        "import X from './page';\nexport function run() { X(); }")

    store, pid = _reconcile(
        str(tmp_path / "src" / "page.ts"),
        str(tmp_path / "src" / "app.ts"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "js_ts::src/page.Page")
    assert any("run" in c for c in callers), \
        f"default ref must map X → src/page.Page; callers={callers}"
    store.close()


def test_default_anonymous_no_edge(tmp_path):
    """export default function(){}; import X; X() → no edge (anonymous)."""
    _ts(tmp_path, "src/anon.ts", "export default function() { return 1; }")
    _ts(tmp_path, "src/app.ts",
        "import X from './anon';\nexport function run() { X(); }")

    store, pid = _reconcile(
        str(tmp_path / "src" / "anon.ts"),
        str(tmp_path / "src" / "app.ts"),
        tmp_path=tmp_path,
    )
    edges = store.con.execute(
        "SELECT callee FROM call_edges WHERE project_id=? AND caller LIKE '%run%'",
        (pid,),
    ).fetchall()
    assert not any("anon" in (r["callee"] or "") for r in edges), \
        f"anonymous default must not create edge; edges={[dict(r) for r in edges]}"
    store.close()


def test_default_object_no_edge(tmp_path):
    """export default {a:1}; import X; X() → no edge (object literal)."""
    _ts(tmp_path, "src/obj.ts", "export default { a: 1 };")
    _ts(tmp_path, "src/app.ts",
        "import X from './obj';\nexport function run() { X(); }")

    store, pid = _reconcile(
        str(tmp_path / "src" / "obj.ts"),
        str(tmp_path / "src" / "app.ts"),
        tmp_path=tmp_path,
    )
    edges = store.con.execute(
        "SELECT callee FROM call_edges WHERE project_id=? AND caller LIKE '%run%'",
        (pid,),
    ).fetchall()
    assert not any("obj" in (r["callee"] or "") for r in edges), \
        f"object default must not create edge; edges={[dict(r) for r in edges]}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# 4. default + named co-exist
# ══════════════════════════════════════════════════════════════════════════════

def test_default_plus_named(tmp_path):
    """Same file: export default function Page + export function helper.
    import Foo, {helper} → Foo connects to Page, helper connects to helper."""
    _ts(tmp_path, "src/page.ts",
        "export default function Page() { return 1; }\n"
        "export function helper() { return 2; }")
    _ts(tmp_path, "src/app.ts",
        "import Foo, { helper } from './page';\n"
        "export function run() { Foo(); helper(); }")

    store, pid = _reconcile(
        str(tmp_path / "src" / "page.ts"),
        str(tmp_path / "src" / "app.ts"),
        tmp_path=tmp_path,
    )
    callers_page = store.callers_of(pid, "js_ts::src/page.Page")
    callers_helper = store.callers_of(pid, "js_ts::src/page.helper")
    assert any("run" in c for c in callers_page), \
        f"default Foo must connect to Page; callers={callers_page}"
    assert any("run" in c for c in callers_helper), \
        f"named helper must connect; callers={callers_helper}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# 5. No false edge when file has no named default
# ══════════════════════════════════════════════════════════════════════════════

def test_default_no_false_edge(tmp_path):
    """import Foo from './plain', plain.ts has no default export → no edge created."""
    _ts(tmp_path, "src/plain.ts", "export function realFn() { return 1; }")
    _ts(tmp_path, "src/app.ts",
        "import Foo from './plain';\nexport function run() { Foo(); }")

    store, pid = _reconcile(
        str(tmp_path / "src" / "plain.ts"),
        str(tmp_path / "src" / "app.ts"),
        tmp_path=tmp_path,
    )
    callers_real = store.callers_of(pid, "js_ts::src/plain.realFn")
    assert not any("run" in c for c in callers_real), \
        f"no default record → must not connect to realFn; callers={callers_real}"
    store.close()
