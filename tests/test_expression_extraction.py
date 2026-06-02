"""Tests for expression define extraction: class field arrows, object method shorthand,
abstract class methods, namespace functions, class field function expressions,
Python conditional/try/with defs, JS conditional functions, and object property
arrow/function (safe subset).

Two red lines:
  1. Zero false edges — dynamic/unknowable receivers leave edge empty.
  2. Correct direction — all new-define edges are producer→consumer (no reversal).
"""
from __future__ import annotations

import pytest

from buer.parse import extract_defines
from buer.reconcile import reconcile
from buer.store import Store


# ── helpers ───────────────────────────────────────────────────────────────────

def _ts(tmp_path, rel: str, body: str) -> str:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return str(p)


def _py(tmp_path, rel: str, body: str) -> str:
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
# A. abstract class — methods extracted, abstract signatures skipped
# ══════════════════════════════════════════════════════════════════════════════

def test_abstract_class_concrete_extracted(tmp_path):
    """abstract class Base { concrete() {} } → define Base.concrete is emitted."""
    f = _ts(tmp_path, "src/base.ts", """\
abstract class Base {
    concrete() { return 1; }
    abstract doThing(): void;
}
""")
    defines = extract_defines(f)
    names = [d.qualified_name for d in defines]
    assert "Base.concrete" in names, f"abstract class concrete method not extracted; names={names}"
    assert not any("doThing" in n for n in names), \
        f"abstract signature must not be extracted; names={names}"


def test_abstract_class_as_consumer(tmp_path):
    """abstract class method calling g() → call edge from Base.concrete to g."""
    _ts(tmp_path, "src/lib.ts", "export function g() {}")
    _ts(tmp_path, "src/base.ts", """\
import { g } from './lib';
export abstract class Base {
    concrete() { return g(); }
}
""")
    store, pid = _reconcile(
        str(tmp_path / "src" / "lib.ts"),
        str(tmp_path / "src" / "base.ts"),
        tmp_path=tmp_path,
    )
    # g() is the producer; Base.concrete is the consumer
    callers_g = store.callers_of(pid, "js_ts::src/lib.g")
    assert any("concrete" in c for c in callers_g), \
        f"Base.concrete must be a caller of g; callers={callers_g}"
    store.close()


def test_abstract_class_as_producer(tmp_path):
    """outer() calls Base.concrete() → call edge from outer to Base.concrete."""
    _ts(tmp_path, "src/base.ts", """\
export abstract class Base {
    concrete() { return 1; }
}
""")
    _ts(tmp_path, "src/consumer.ts", """\
import { Base } from './base';
function outer() {
    const b = new Base();
    return b.concrete();
}
""")
    store, pid = _reconcile(
        str(tmp_path / "src" / "base.ts"),
        str(tmp_path / "src" / "consumer.ts"),
        tmp_path=tmp_path,
    )
    # outer is consumer; Base.concrete is producer — verify Base.concrete is defined
    defines_base = [d for d in store.recorded_defines_for_file(pid, str(tmp_path / "src" / "base.ts"))]
    assert any("concrete" in d for d in defines_base), \
        f"Base.concrete must be recorded; defines={defines_base}"
    store.close()


def test_no_reversed_edge_abstract_class(tmp_path):
    """Direction guard: Base.concrete calling g() must produce g as callee of concrete, not vice versa."""
    _ts(tmp_path, "src/lib.ts", "export function g() {}")
    _ts(tmp_path, "src/base.ts", """\
import { g } from './lib';
export abstract class Base {
    concrete() { return g(); }
}
""")
    store, pid = _reconcile(
        str(tmp_path / "src" / "lib.ts"),
        str(tmp_path / "src" / "base.ts"),
        tmp_path=tmp_path,
    )
    # g must NOT have concrete as a callee (reversed)
    callees_g = store.callees_of(pid, "js_ts::src/lib.g")
    assert not any("concrete" in c for c in callees_g), \
        f"reversed edge: g must not call concrete; callees_of_g={callees_g}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# B. namespace function — N.f extracted
# ══════════════════════════════════════════════════════════════════════════════

def test_namespace_function_extracted(tmp_path):
    """namespace N { export function f() {} } → define N.f is emitted."""
    f = _ts(tmp_path, "src/ns.ts", """\
namespace N {
    export function f() { return 1; }
}
""")
    defines = extract_defines(f)
    names = [d.qualified_name for d in defines]
    assert "N.f" in names, f"namespace function not extracted; names={names}"


def test_nested_namespace_extracted(tmp_path):
    """namespace N { namespace M { function g() {} } } → N.M.g emitted."""
    f = _ts(tmp_path, "src/ns.ts", """\
namespace N {
    export namespace M {
        export function g() { return 1; }
    }
}
""")
    defines = extract_defines(f)
    names = [d.qualified_name for d in defines]
    assert "N.M.g" in names, f"nested namespace not extracted; names={names}"


def test_namespace_function_as_consumer(tmp_path):
    """N.f calling g() → Base.f is a caller of g (direction correct)."""
    _ts(tmp_path, "src/lib.ts", "export function g() {}")
    _ts(tmp_path, "src/ns.ts", """\
import { g } from './lib';
namespace N {
    export function f() { return g(); }
}
""")
    store, pid = _reconcile(
        str(tmp_path / "src" / "lib.ts"),
        str(tmp_path / "src" / "ns.ts"),
        tmp_path=tmp_path,
    )
    callers_g = store.callers_of(pid, "js_ts::src/lib.g")
    assert any("N.f" in c or "f" in c for c in callers_g), \
        f"N.f must be a caller of g; callers={callers_g}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# C. class field function expression — C.h extracted
# ══════════════════════════════════════════════════════════════════════════════

def test_class_field_function_expr_extracted(tmp_path):
    """class C { h = function(){ return 1; } } → define C.h is emitted."""
    f = _ts(tmp_path, "src/c.ts", """\
class C {
    h = function() { return 1; }
}
""")
    defines = extract_defines(f)
    names = [d.qualified_name for d in defines]
    assert "C.h" in names, f"class field function expression not extracted; names={names}"


def test_class_field_function_expr_as_consumer(tmp_path):
    """class C { h = function(){ g(); } } → C.h calls g (correct direction)."""
    _ts(tmp_path, "src/lib.ts", "export function g() {}")
    _ts(tmp_path, "src/c.ts", """\
import { g } from './lib';
export class C {
    h = function() { return g(); }
}
""")
    store, pid = _reconcile(
        str(tmp_path / "src" / "lib.ts"),
        str(tmp_path / "src" / "c.ts"),
        tmp_path=tmp_path,
    )
    callers_g = store.callers_of(pid, "js_ts::src/lib.g")
    assert any("h" in c for c in callers_g), \
        f"C.h must be caller of g; callers={callers_g}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# D. Python conditional/try/with defs
# ══════════════════════════════════════════════════════════════════════════════

def test_conditional_def_py(tmp_path):
    """if X: def f(): … → define f is emitted (Python)."""
    f = _py(tmp_path, "src/m.py", """\
import sys
if sys.version_info >= (3, 9):
    def f(): return 1
""")
    defines = extract_defines(f)
    names = [d.qualified_name for d in defines]
    assert "f" in names, f"conditional def not extracted; names={names}"


def test_try_def_py(tmp_path):
    """try: def f(): … → define f emitted (Python)."""
    f = _py(tmp_path, "src/m.py", """\
try:
    def f(): return 1
except ImportError:
    pass
""")
    defines = extract_defines(f)
    names = [d.qualified_name for d in defines]
    assert "f" in names, f"try def not extracted; names={names}"


def test_with_def_py(tmp_path):
    """with ctx: def f(): … → define f emitted (Python)."""
    f = _py(tmp_path, "src/m.py", """\
from contextlib import suppress
with suppress(Exception):
    def f(): return 1
""")
    defines = extract_defines(f)
    names = [d.qualified_name for d in defines]
    assert "f" in names, f"with def not extracted; names={names}"


def test_conditional_def_py_as_consumer(tmp_path):
    """if block def calling g() → f is a caller of g (direction correct, Python)."""
    _py(tmp_path, "lib.py", "def g(): pass")
    _py(tmp_path, "m.py", """\
from lib import g
if True:
    def f(): return g()
""")
    store, pid = _reconcile(
        str(tmp_path / "lib.py"),
        str(tmp_path / "m.py"),
        tmp_path=tmp_path,
    )
    callers_g = store.callers_of(pid, "py::lib.g")
    assert any("f" in c for c in callers_g), \
        f"conditional def f must call g; callers={callers_g}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# E. JS conditional function — function inside if/try block extracted
# ══════════════════════════════════════════════════════════════════════════════

def test_conditional_function_js(tmp_path):
    """if(x){ function f(){} } → define f is emitted (JS/TS)."""
    f = _ts(tmp_path, "src/m.ts", """\
declare const x: boolean;
if (x) {
    function f() { return 1; }
}
""")
    defines = extract_defines(f)
    names = [d.qualified_name for d in defines]
    assert "f" in names, f"conditional function not extracted; names={names}"


def test_conditional_function_js_as_consumer(tmp_path):
    """if(x){ function f(){ g(); } } → f is a caller of g (direction correct)."""
    _ts(tmp_path, "src/lib.ts", "export function g() {}")
    _ts(tmp_path, "src/m.ts", """\
import { g } from './lib';
declare const x: boolean;
if (x) {
    function f() { return g(); }
}
""")
    store, pid = _reconcile(
        str(tmp_path / "src" / "lib.ts"),
        str(tmp_path / "src" / "m.ts"),
        tmp_path=tmp_path,
    )
    callers_g = store.callers_of(pid, "js_ts::src/lib.g")
    assert any("f" in c for c in callers_g), \
        f"conditional function f must call g; callers={callers_g}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# F. Object property arrow/function — safe subset (const + direct object literal)
# ══════════════════════════════════════════════════════════════════════════════

def test_object_property_arrow_extracted(tmp_path):
    """const o = { h: () => 1 } → define o.h is emitted."""
    f = _ts(tmp_path, "src/obj.ts", "const o = { h: () => 1 };")
    defines = extract_defines(f)
    names = [d.qualified_name for d in defines]
    assert "o.h" in names, f"object property arrow not extracted; names={names}"


def test_object_property_function_extracted(tmp_path):
    """const o = { h: function(){} } → define o.h is emitted."""
    f = _ts(tmp_path, "src/obj.ts", "const o = { h: function() { return 1; } };")
    defines = extract_defines(f)
    names = [d.qualified_name for d in defines]
    assert "o.h" in names, f"object property function expression not extracted; names={names}"


def test_object_property_arrow_call_edge(tmp_path):
    """const o = { h: () => g() }; o.h() → edge from caller to o.h, o.h calls g."""
    _ts(tmp_path, "src/lib.ts", "export function g() {}")
    _ts(tmp_path, "src/obj.ts", """\
import { g } from './lib';
const o = { h: () => g() };
export function caller() { o.h(); }
""")
    store, pid = _reconcile(
        str(tmp_path / "src" / "lib.ts"),
        str(tmp_path / "src" / "obj.ts"),
        tmp_path=tmp_path,
    )
    callers_oh = store.callers_of(pid, "js_ts::src/obj.o.h")
    assert any("caller" in c for c in callers_oh), \
        f"caller must edge to o.h; callers={callers_oh}"
    callers_g = store.callers_of(pid, "js_ts::src/lib.g")
    assert any("o.h" in c or "h" in c for c in callers_g), \
        f"o.h must call g; callers={callers_g}"
    store.close()


def test_object_property_direction_no_reversal(tmp_path):
    """Direction guard: o.h calling g() must NOT make g a caller of o.h (reversed)."""
    _ts(tmp_path, "src/lib.ts", "export function g() {}")
    _ts(tmp_path, "src/obj.ts", """\
import { g } from './lib';
const o = { h: () => g() };
""")
    store, pid = _reconcile(
        str(tmp_path / "src" / "lib.ts"),
        str(tmp_path / "src" / "obj.ts"),
        tmp_path=tmp_path,
    )
    callees_g = store.callees_of(pid, "js_ts::src/lib.g")
    assert not any("h" in c for c in callees_g), \
        f"reversed edge: g must not call o.h; callees_of_g={callees_g}"
    store.close()


def test_property_conditional_receiver_no_edge(tmp_path):
    """False-edge guard: const x = flag ? o1 : o2; x.h() → no edge to any h."""
    _ts(tmp_path, "src/obj.ts", """\
declare const flag: boolean;
const x = flag ? { h: () => 1 } : { h: () => 2 };
export function caller() { x.h(); }
""")
    store, pid = _reconcile(
        str(tmp_path / "src" / "obj.ts"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "js_ts::src/obj.x.h")
    assert not any("caller" in c for c in callers), \
        f"conditional receiver must not produce call edge; callers={callers}"
    store.close()


def test_property_let_no_edge(tmp_path):
    """False-edge guard: let o = { h: () => 1 }; o.h() → no edge (mutable binding)."""
    _ts(tmp_path, "src/obj.ts", """\
let o = { h: () => 1 };
export function caller() { o.h(); }
""")
    store, pid = _reconcile(
        str(tmp_path / "src" / "obj.ts"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "js_ts::src/obj.o.h")
    assert not any("caller" in c for c in callers), \
        f"let binding must not produce call edge; callers={callers}"
    store.close()


def test_property_anon_no_edge(tmp_path):
    """False-edge guard: anonymous object literal must not produce property define."""
    f = _ts(tmp_path, "src/obj.ts", """\
declare function process(x: any): void;
export function caller() { process({ h: () => 1 }); }
""")
    defines = extract_defines(f)
    names = [d.qualified_name for d in defines]
    assert not any(".h" in n for n in names), \
        f"anonymous object property must not be extracted; names={names}"


# ══════════════════════════════════════════════════════════════════════════════
# G. export const object methods/properties — covered by safe subset
# ══════════════════════════════════════════════════════════════════════════════

def test_export_const_object_method_extracted(tmp_path):
    """export const api = { fetch() {} } → define api.fetch emitted."""
    f = _ts(tmp_path, "src/api.ts", """\
export const api = {
    fetch() { return 1; },
    get: () => 2,
};
""")
    defines = extract_defines(f)
    names = [d.qualified_name for d in defines]
    assert "api.fetch" in names, f"export const object method not extracted; names={names}"
    assert "api.get" in names, f"export const object property arrow not extracted; names={names}"


def test_export_const_object_call_edge(tmp_path):
    """export const api = { fetch() { g(); } }; api.fetch() → edge through safe subset."""
    _ts(tmp_path, "src/lib.ts", "export function g() {}")
    _ts(tmp_path, "src/api.ts", """\
import { g } from './lib';
export const api = { fetch() { return g(); } };
""")
    _ts(tmp_path, "src/consumer.ts", """\
import { api } from './api';
export function use() { api.fetch(); }
""")
    store, pid = _reconcile(
        str(tmp_path / "src" / "lib.ts"),
        str(tmp_path / "src" / "api.ts"),
        str(tmp_path / "src" / "consumer.ts"),
        tmp_path=tmp_path,
    )
    callers_fetch = store.callers_of(pid, "js_ts::src/api.api.fetch")
    assert any("use" in c for c in callers_fetch), \
        f"use must edge to api.fetch; callers={callers_fetch}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# Previously committed (9373b41) — regression guards kept
# ══════════════════════════════════════════════════════════════════════════════

def test_class_field_arrow_extracted(tmp_path):
    """class C { handler = () => 1; } → define C.handler is emitted."""
    f = _ts(tmp_path, "src/c.ts", """\
export class C {
    handler = () => 1;
}
""")
    defines = extract_defines(f)
    names = [d.qualified_name for d in defines]
    assert "C.handler" in names, f"class field arrow not extracted; names={names}"


def test_class_field_arrow_has_no_false_edge(tmp_path):
    """handler = () => target(); must create edge to target, not to unrelated."""
    _ts(tmp_path, "src/lib.ts", """\
export function target() {}
export function unrelated() {}
""")
    _ts(tmp_path, "src/c.ts", """\
import { target } from './lib';
export class C {
    handler = () => target();
}
""")
    store, pid = _reconcile(
        str(tmp_path / "src" / "lib.ts"),
        str(tmp_path / "src" / "c.ts"),
        tmp_path=tmp_path,
    )
    callers_target = store.callers_of(pid, "js_ts::src/lib.target")
    callers_unrelated = store.callers_of(pid, "js_ts::src/lib.unrelated")
    assert any("handler" in c for c in callers_target), \
        f"C.handler must call target; callers={callers_target}"
    assert not any("handler" in c for c in callers_unrelated), \
        f"C.handler must not call unrelated; callers={callers_unrelated}"
    store.close()


def test_object_method_shorthand_extracted(tmp_path):
    """const o = { m() { return 1; } } → define o.m is emitted."""
    f = _ts(tmp_path, "src/obj.ts", """\
const o = { m() { return 1; } };
""")
    defines = extract_defines(f)
    names = [d.qualified_name for d in defines]
    assert "o.m" in names, f"object method shorthand not extracted; names={names}"


def test_object_method_safe_call_edge(tmp_path):
    """const o = { m() { target(); } }; o.m() → edge to o.m which calls target."""
    _ts(tmp_path, "src/lib.ts", "export function target() {}")
    _ts(tmp_path, "src/obj.ts", """\
import { target } from './lib';
const o = { m() { target(); } };
export function caller() { o.m(); }
""")
    store, pid = _reconcile(
        str(tmp_path / "src" / "lib.ts"),
        str(tmp_path / "src" / "obj.ts"),
        tmp_path=tmp_path,
    )
    callers_om = store.callers_of(pid, "js_ts::src/obj.o.m")
    assert any("caller" in c for c in callers_om), \
        f"caller must edge to o.m; callers={callers_om}"
    store.close()


def test_let_object_no_call_edge(tmp_path):
    """let o = { m() {} }; o.m() must NOT edge to o.m (mutable binding, unsafe)."""
    _ts(tmp_path, "src/obj.ts", """\
let o = { m() { return 1; } };
export function caller() { o.m(); }
""")
    store, pid = _reconcile(
        str(tmp_path / "src" / "obj.ts"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "js_ts::src/obj.o.m")
    assert not any("caller" in c for c in callers), \
        f"let binding must not produce call edge; callers={callers}"
    store.close()


def test_conditional_object_no_call_edge(tmp_path):
    """const o = flag ? { m(){} } : {}; o.m() must NOT edge to any m."""
    _ts(tmp_path, "src/obj.ts", """\
declare const flag: boolean;
const o = flag ? { m() { return 1; } } : { m() { return 2; } };
export function caller() { o.m(); }
""")
    store, pid = _reconcile(
        str(tmp_path / "src" / "obj.ts"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "js_ts::src/obj.o.m")
    assert not any("caller" in c for c in callers), \
        f"conditional object must not produce call edge; callers={callers}"
    store.close()


def test_anon_object_no_false_edge(tmp_path):
    """process({ m() {} }) — anonymous object must not produce o.m define."""
    f = _ts(tmp_path, "src/obj.ts", """\
declare function process(x: any): void;
export function caller() { process({ m() { return 1; } }); }
""")
    defines = extract_defines(f)
    names = [d.qualified_name for d in defines]
    assert not any(".m" in n for n in names), \
        f"anonymous object literal must not produce a define; names={names}"
