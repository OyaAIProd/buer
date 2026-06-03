"""Tests for JS/TS extractors in buer/parse.py and callgraph.py.

Covers:
  - JS: function_declaration, arrow_function, function_expression, method_definition
  - JS: five features (params_shape, returns_kind, calls, side_effects, size_count)
  - JS: compute_fingerprint consistency
  - TS: same structure — required_parameter, optional_parameter, rest in required_parameter
  - TS: class with type_identifier, export_statement wrapping
  - module_name_of: JS/TS extension handling and slash vs dot separators
  - extract_module_imports: ES6 named/default/namespace + require
  - callgraph: build_symbol_index walks JS/TS; compute_call_edges resolves cross-file calls
"""
import os
import tempfile
import textwrap
from pathlib import Path

import pytest

from buer.parse import (
    Define,
    compute_fingerprint,
    detect_language,
    extract_defines,
    extract_module_imports,
)
from buer.callgraph import (
    SymbolIndex,
    build_symbol_index,
    compute_call_edges,
    module_name_of,
)


# ── language detection ─────────────────────────────────────────────────────────

def test_detect_js():
    assert detect_language("src/app.js") == "javascript"

def test_detect_jsx():
    assert detect_language("src/App.jsx") == "javascript"

def test_detect_ts():
    assert detect_language("src/auth.ts") == "typescript"

def test_detect_tsx():
    assert detect_language("src/App.tsx") == "tsx"


# ── module_name_of ─────────────────────────────────────────────────────────────

def test_module_name_py_dots():
    assert module_name_of("/proj/pkg/auth.py", "/proj") == "pkg.auth"

def test_module_name_ts_slashes():
    assert module_name_of("/proj/src/auth.ts", "/proj") == "src/auth"

def test_module_name_js_slashes():
    assert module_name_of("/proj/src/utils/helper.js", "/proj") == "src/utils/helper"

def test_module_name_tsx_slashes():
    assert module_name_of("/proj/src/App.tsx", "/proj") == "src/App"


# ── TSX: React component extraction ───────────────────────────────────────────

class TestTsx:
    def _parse(self, src: str) -> list[Define]:
        with tempfile.NamedTemporaryFile(suffix=".tsx", delete=False) as f:
            f.write(textwrap.dedent(src).encode())
            path = f.name
        try:
            return extract_defines(path)
        finally:
            os.unlink(path)

    def test_function_component_extracted(self):
        src = """\
            export default function App() {
              return <div>Hello</div>;
            }
        """
        defs = self._parse(src)
        assert any(d.name == "App" for d in defs)

    def test_arrow_component_extracted(self):
        src = """\
            const Greeting = ({ name }: { name: string }) => {
              return <span>{name}</span>;
            };
        """
        defs = self._parse(src)
        assert any(d.name == "Greeting" for d in defs)

    def test_jsx_element_not_a_define(self):
        src = """\
            function App() {
              return <div><span>hello</span></div>;
            }
        """
        defs = self._parse(src)
        # Only App should be extracted; JSX elements are not defines
        assert len(defs) == 1
        assert defs[0].name == "App"

    def test_returns_kind_with_jsx_return(self):
        src = """\
            function App() {
              return <div>Hello</div>;
            }
        """
        defs = self._parse(src)
        assert defs[0].returns_kind == "value"

    def test_params_shape_with_destructured_props(self):
        src = """\
            function Btn({ label, onClick }: { label: string; onClick: () => void }) {
              return <button onClick={onClick}>{label}</button>;
            }
        """
        defs = self._parse(src)
        d = defs[0]
        # destructured object pattern = 1 required param (the props object)
        assert d.params_shape[0] == 1  # req
        assert d.params_shape[1] == 0  # defaults

    def test_calls_collected_inside_component(self):
        src = """\
            function App() {
              const data = fetchData();
              return <div>{data}</div>;
            }
        """
        defs = self._parse(src)
        assert "fetchData" in defs[0].calls

    def test_tsx_class_component(self):
        src = """\
            import React from 'react';
            class Counter extends React.Component {
              render() { return <div>{this.state.count}</div>; }
              increment() { this.setState({ count: this.state.count + 1 }); }
            }
        """
        defs = self._parse(src)
        names = {d.name for d in defs}
        assert "render" in names
        assert "increment" in names
        assert "Counter" not in names  # class is a namespace, not a define

    def test_tsx_uses_tsx_grammar_not_ts(self):
        # A JSX expression that would fail to parse with pure TS grammar
        # If this returns defines without error, the tsx parser is being used
        src = """\
            const el = <div className="test">content</div>;
            function wrap() { return <span>{el}</span>; }
        """
        defs = self._parse(src)
        assert any(d.name == "wrap" for d in defs)

    def test_ts_file_unaffected(self):
        # Pure .ts file should still work (uses language_typescript, not tsx)
        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as f:
            f.write(b"function decode(token: string): string { return token; }")
            path = f.name
        try:
            defs = extract_defines(path)
            assert len(defs) == 1
            assert defs[0].name == "decode"
        finally:
            os.unlink(path)


# ── JS function_declaration ───────────────────────────────────────────────────

class TestJsFunction:
    def _parse(self, src: str) -> list[Define]:
        with tempfile.NamedTemporaryFile(suffix=".js", delete=False) as f:
            f.write(textwrap.dedent(src).encode())
            path = f.name
        try:
            return extract_defines(path)
        finally:
            os.unlink(path)

    def test_basic_function(self):
        defs = self._parse("function greet(a, b) { return a + b; }")
        assert len(defs) == 1
        d = defs[0]
        assert d.name == "greet"
        assert d.qualified_name == "greet"
        assert d.params_shape == (2, 0, False, False, 0)
        assert d.returns_kind == "value"

    def test_default_param(self):
        defs = self._parse("function f(x, y = 0) { return x; }")
        assert defs[0].params_shape == (1, 1, False, False, 0)

    def test_rest_param(self):
        defs = self._parse("function f(a, ...rest) { return rest; }")
        assert defs[0].params_shape == (1, 0, True, False, 0)

    def test_destructured_param(self):
        defs = self._parse("function f({ x, y }, [a, b]) { return x; }")
        assert defs[0].params_shape == (2, 0, False, False, 0)

    def test_async_function(self):
        defs = self._parse("async function f(x) { await doSomething(x); }")
        assert "async" in defs[0].side_effects

    def test_generator_function(self):
        defs = self._parse("function* gen() { yield 1; }")
        assert "generator" in defs[0].side_effects

    def test_throw_marks_raises(self):
        defs = self._parse("function f() { throw new Error('bad'); }")
        assert "raises" in defs[0].side_effects

    def test_attr_write(self):
        defs = self._parse("function f(obj) { obj.x = 1; }")
        assert "attr_write" in defs[0].side_effects

    def test_subscript_write(self):
        defs = self._parse("function f(arr) { arr[0] = 1; }")
        assert "subscript_write" in defs[0].side_effects

    def test_calls_collected(self):
        defs = self._parse("function f() { foo(); bar.baz(); }")
        assert "foo" in defs[0].calls
        assert "bar.baz" in defs[0].calls

    def test_size_count(self):
        src = """\
            function f() {
              const x = 1;
              const y = 2;
              return x + y;
            }
        """
        defs = self._parse(src)
        assert defs[0].size_count == 3

    def test_size_count_ignores_comments(self):
        """Comments must not be counted as statements (bug B regression test)."""
        src_no_comment = """\
            function f() {
              const x = 1;
              return x;
            }
        """
        src_with_comments = """\
            function f() {
              // line comment
              const x = 1;
              /* block comment */
              return x;
            }
        """
        d1 = self._parse(src_no_comment)[0]
        d2 = self._parse(src_with_comments)[0]
        assert d1.size_count == 2
        assert d2.size_count == 2  # comments don't add to count

    def test_no_return(self):
        defs = self._parse("function f() { console.log(1); }")
        assert defs[0].returns_kind == "none"

    def test_bare_return(self):
        defs = self._parse("function f() { if (x) return; }")
        assert defs[0].returns_kind == "bare"

    def test_nested_function_not_extracted_as_sibling(self):
        src = """\
            function outer() {
              function inner() { return 1; }
              return inner();
            }
        """
        defs = self._parse(src)
        names = [d.name for d in defs]
        assert "outer" in names
        # inner is nested — it's extracted separately when the file has two top-level
        # definitions; but here inner is inside outer's block so it IS nested.
        # The collector only recurses top-level; inner should NOT appear.
        assert "inner" not in names

    def test_calls_do_not_descend_into_nested_function(self):
        src = """\
            function outer() {
              function inner() { innerHelper(); }
              outerHelper();
            }
        """
        defs = self._parse(src)
        outer = next(d for d in defs if d.name == "outer")
        assert "outerHelper" in outer.calls
        assert "innerHelper" not in outer.calls


# ── JS arrow functions ─────────────────────────────────────────────────────────

class TestJsArrow:
    def _parse(self, src: str) -> list[Define]:
        with tempfile.NamedTemporaryFile(suffix=".js", delete=False) as f:
            f.write(textwrap.dedent(src).encode())
            path = f.name
        try:
            return extract_defines(path)
        finally:
            os.unlink(path)

    def test_arrow_with_braces(self):
        defs = self._parse("const add = (a, b) => { return a + b; };")
        assert len(defs) == 1
        d = defs[0]
        assert d.name == "add"
        assert d.params_shape == (2, 0, False, False, 0)
        assert d.returns_kind == "value"

    def test_arrow_expression_body(self):
        defs = self._parse("const double = x => x * 2;")
        d = defs[0]
        assert d.name == "double"
        assert d.returns_kind == "value"
        assert d.size_count == 1

    def test_arrow_expression_body_with_parens(self):
        defs = self._parse("const double = (x) => x * 2;")
        assert defs[0].name == "double"
        assert defs[0].params_shape == (1, 0, False, False, 0)

    def test_arrow_calls_in_expression_body(self):
        defs = self._parse("const f = (x) => helper(x);")
        assert "helper" in defs[0].calls

    def test_arrow_with_default_param(self):
        defs = self._parse("const f = (x, y = 0) => x + y;")
        assert defs[0].params_shape == (1, 1, False, False, 0)

    def test_function_expression(self):
        defs = self._parse("const fn = function(a, b) { return a; };")
        assert len(defs) == 1
        assert defs[0].name == "fn"
        assert defs[0].params_shape == (2, 0, False, False, 0)

    def test_let_arrow(self):
        defs = self._parse("let f = (x) => x;")
        assert defs[0].name == "f"

    def test_var_arrow(self):
        defs = self._parse("var f = (x) => x;")
        assert defs[0].name == "f"


# ── JS class methods ───────────────────────────────────────────────────────────

class TestJsClass:
    def _parse(self, src: str) -> list[Define]:
        with tempfile.NamedTemporaryFile(suffix=".js", delete=False) as f:
            f.write(textwrap.dedent(src).encode())
            path = f.name
        try:
            return extract_defines(path)
        finally:
            os.unlink(path)

    def test_class_methods_extracted(self):
        src = """\
            class Foo {
              constructor(x) { this.x = x; }
              bar(a, b) { return this.baz(a); }
            }
        """
        defs = self._parse(src)
        names = {d.name for d in defs}
        assert "constructor" in names
        assert "bar" in names

    def test_method_qualified_name(self):
        src = """\
            class Foo {
              bar(a) { return a; }
            }
        """
        defs = self._parse(src)
        assert any(d.qualified_name == "Foo.bar" for d in defs)

    def test_class_not_a_define(self):
        # The class itself should not appear as a Define — only its methods.
        src = "class Foo { bar() { return 1; } }"
        defs = self._parse(src)
        assert not any(d.name == "Foo" for d in defs)

    def test_async_method(self):
        src = "class A { async fetch(url) { return await request(url); } }"
        defs = self._parse(src)
        m = next(d for d in defs if d.name == "fetch")
        assert "async" in m.side_effects

    def test_static_method(self):
        src = "class A { static create(x) { return new A(x); } }"
        defs = self._parse(src)
        assert any(d.name == "create" for d in defs)

    def test_method_params(self):
        src = "class A { run(a, b = 0, ...rest) { } }"
        defs = self._parse(src)
        m = next(d for d in defs if d.name == "run")
        assert m.params_shape == (1, 1, True, False, 0)

    def test_method_calls_this(self):
        src = "class A { bar(a) { return this.baz(a); } }"
        defs = self._parse(src)
        m = next(d for d in defs if d.name == "bar")
        assert "this.baz" in m.calls


# ── JS export_statement ───────────────────────────────────────────────────────

class TestJsExport:
    def _parse(self, src: str) -> list[Define]:
        with tempfile.NamedTemporaryFile(suffix=".js", delete=False) as f:
            f.write(textwrap.dedent(src).encode())
            path = f.name
        try:
            return extract_defines(path)
        finally:
            os.unlink(path)

    def test_export_function(self):
        defs = self._parse("export function foo(x) { return x; }")
        assert any(d.name == "foo" for d in defs)

    def test_export_default_function(self):
        defs = self._parse("export default function main(a) { return a; }")
        assert any(d.name == "main" for d in defs)

    def test_export_class(self):
        src = "export class Bar { run() { } }"
        defs = self._parse(src)
        assert any(d.name == "run" for d in defs)
        assert not any(d.name == "Bar" for d in defs)

    def test_export_const_arrow(self):
        defs = self._parse("export const f = (x) => x + 1;")
        assert any(d.name == "f" for d in defs)


# ── JS fingerprint ─────────────────────────────────────────────────────────────

class TestJsFingerprint:
    def _parse_one(self, src: str) -> Define:
        with tempfile.NamedTemporaryFile(suffix=".js", delete=False) as f:
            f.write(src.encode())
            path = f.name
        try:
            return extract_defines(path)[0]
        finally:
            os.unlink(path)

    def test_fingerprint_deterministic(self):
        src = "function f(a, b) { return a + b; }"
        d1 = self._parse_one(src)
        d2 = self._parse_one(src)
        assert compute_fingerprint(d1) == compute_fingerprint(d2)

    def test_fingerprint_changes_on_new_call(self):
        d1 = self._parse_one("function f(a) { return foo(a); }")
        d2 = self._parse_one("function f(a) { return bar(a); }")
        assert compute_fingerprint(d1) != compute_fingerprint(d2)

    def test_fingerprint_stable_across_rename(self):
        # Receiver-stripped: this.foo and that.foo both map to last segment "foo"
        d1 = self._parse_one("function f(a) { this.foo(a); }")
        d2 = self._parse_one("function f(a) { self.foo(a); }")
        assert compute_fingerprint(d1) == compute_fingerprint(d2)

    def test_fingerprint_stable_across_line_comment(self):
        """Adding a // comment must not change the fingerprint (bug B fix)."""
        d1 = self._parse_one("function f(x) { return x + 1; }")
        d2 = self._parse_one("function f(x) { // explanation\n  return x + 1; }")
        assert compute_fingerprint(d1) == compute_fingerprint(d2)

    def test_fingerprint_stable_across_block_comment(self):
        """Adding a /* */ block comment must not change the fingerprint."""
        d1 = self._parse_one("function f(x) { return Math.max(0, x); }")
        d2 = self._parse_one("function f(x) { /* note */ return Math.max(0, x); }")
        assert compute_fingerprint(d1) == compute_fingerprint(d2)

    def test_fingerprint_stable_across_jsdoc_in_body(self):
        """A /** */ comment inside the body must not change the fingerprint."""
        d1 = self._parse_one("function f(x) { return x; }")
        d2 = self._parse_one("function f(x) { /** doc */ return x; }")
        assert compute_fingerprint(d1) == compute_fingerprint(d2)


# ── TS function features ───────────────────────────────────────────────────────

class TestTsFunction:
    def _parse(self, src: str) -> list[Define]:
        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as f:
            f.write(textwrap.dedent(src).encode())
            path = f.name
        try:
            return extract_defines(path)
        finally:
            os.unlink(path)

    def test_basic_function(self):
        defs = self._parse("function greet(name: string): string { return name; }")
        assert len(defs) == 1
        assert defs[0].name == "greet"
        assert defs[0].params_shape == (1, 0, False, False, 0)
        assert defs[0].returns_kind == "value"

    def test_optional_param(self):
        defs = self._parse("function f(a: string, b?: number): void { }")
        assert defs[0].params_shape == (1, 1, False, False, 0)

    def test_default_param(self):
        defs = self._parse("function f(a: string, b: number = 0): void { }")
        assert defs[0].params_shape == (1, 1, False, False, 0)

    def test_rest_param(self):
        defs = self._parse("function f(...args: string[]): void { }")
        assert defs[0].params_shape == (0, 0, True, False, 0)

    def test_async_function(self):
        defs = self._parse("async function f(x: string): Promise<void> { await fetch(x); }")
        assert "async" in defs[0].side_effects

    def test_throws(self):
        defs = self._parse("function f(): never { throw new Error(); }")
        assert "raises" in defs[0].side_effects

    def test_interface_not_extracted(self):
        src = """\
            interface Greeter { greet(name: string): void; }
            function hello(name: string): string { return name; }
        """
        defs = self._parse(src)
        assert all(d.name != "Greeter" for d in defs)
        assert any(d.name == "hello" for d in defs)

    def test_type_alias_not_extracted(self):
        src = "type ID = string | number;\nfunction f(id: ID): void { }"
        defs = self._parse(src)
        assert all(d.name not in ("ID", "type") for d in defs)
        assert any(d.name == "f" for d in defs)

    def test_arrow_with_type_annotation(self):
        defs = self._parse("const double = (x: number): number => x * 2;")
        assert len(defs) == 1
        assert defs[0].name == "double"
        assert defs[0].params_shape == (1, 0, False, False, 0)
        assert defs[0].returns_kind == "value"


# ── TS class methods ───────────────────────────────────────────────────────────

class TestTsClass:
    def _parse(self, src: str) -> list[Define]:
        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as f:
            f.write(textwrap.dedent(src).encode())
            path = f.name
        try:
            return extract_defines(path)
        finally:
            os.unlink(path)

    def test_class_methods_with_type_identifier(self):
        src = """\
            class Foo {
              constructor(private x: number) {}
              bar(a: string): string { return a; }
            }
        """
        defs = self._parse(src)
        names = {d.name for d in defs}
        assert "constructor" in names
        assert "bar" in names
        assert "Foo" not in names

    def test_qualified_name_with_ts_class(self):
        src = "class App { run(x: number): void { this.stop(x); } }"
        defs = self._parse(src)
        assert any(d.qualified_name == "App.run" for d in defs)

    def test_constructor_access_modifier_params(self):
        src = "class A { constructor(private x: number, public y?: string) {} }"
        defs = self._parse(src)
        ctor = next(d for d in defs if d.name == "constructor")
        # private x: number → required_parameter → req
        # public y?: string → optional_parameter → defaults
        assert ctor.params_shape == (1, 1, False, False, 0)

    def test_rest_in_method(self):
        src = "class A { handle(...args: string[]): void { } }"
        defs = self._parse(src)
        m = next(d for d in defs if d.name == "handle")
        assert m.params_shape == (0, 0, True, False, 0)

    def test_export_default_class(self):
        src = """\
            export default class App {
              run(x: number): void { this.stop(); }
            }
        """
        defs = self._parse(src)
        assert any(d.qualified_name == "App.run" for d in defs)


# ── extract_module_imports ─────────────────────────────────────────────────────

class TestJsModuleImports:
    def _imports(self, src: str, suffix: str = ".js", root: str = "") -> dict:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False, dir="/tmp") as f:
            f.write(textwrap.dedent(src).encode())
            path = f.name
        try:
            return extract_module_imports(path, root=root)
        finally:
            os.unlink(path)

    def test_named_import(self):
        result = self._imports("import { decode } from './auth';", root="/tmp")
        assert "decode" in result
        assert result["decode"].endswith(".decode")

    def test_named_import_alias(self):
        result = self._imports("import { decode as dec } from './auth';", root="/tmp")
        assert "dec" in result
        assert result["dec"].endswith(".decode")

    def test_default_import(self):
        result = self._imports("import React from 'react';", root="/tmp")
        # external package — should not appear
        assert "React" not in result

    def test_namespace_import(self):
        result = self._imports("import * as auth from './auth';", root="/tmp")
        assert "auth" in result
        # value is the module path (no .name suffix for namespace import)
        assert not result["auth"].endswith(".auth")

    def test_relative_path_with_root(self):
        # File at /tmp/src/handler.js imports from ./utils
        with tempfile.TemporaryDirectory() as tmpdir:
            src_dir = os.path.join(tmpdir, "src")
            os.makedirs(src_dir)
            src_path = os.path.join(src_dir, "handler.js")
            Path(src_path).write_bytes(b"import { helper } from './utils';")
            result = extract_module_imports(src_path, root=tmpdir)
            assert "helper" in result
            assert "src/utils" in result["helper"]

    def test_require(self):
        result = self._imports("const auth = require('./auth');", root="/tmp")
        assert "auth" in result

    def test_external_import_skipped(self):
        result = self._imports("import express from 'express';", root="/tmp")
        assert "express" not in result

    def test_ts_named_import(self):
        result = self._imports(
            "import { decode } from './auth';", suffix=".ts", root="/tmp"
        )
        assert "decode" in result

    def test_multiple_named_imports(self):
        result = self._imports(
            "import { encode, decode } from './auth';", root="/tmp"
        )
        assert "encode" in result
        assert "decode" in result


# ── callgraph: build_symbol_index ─────────────────────────────────────────────

class TestBuildSymbolIndex:
    def test_indexes_js_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(os.path.join(tmpdir, "auth.js")).write_text(
                "export function decode(token) { return token; }"
            )
            idx = build_symbol_index(tmpdir)
            assert any("decode" in fqn for fqn in idx.qualified)

    def test_indexes_ts_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(os.path.join(tmpdir, "auth.ts")).write_text(
                "export function decode(token: string): string { return token; }"
            )
            idx = build_symbol_index(tmpdir)
            assert any("decode" in fqn for fqn in idx.qualified)

    def test_skips_node_modules(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            nm = os.path.join(tmpdir, "node_modules", "lib")
            os.makedirs(nm)
            Path(os.path.join(nm, "index.js")).write_text(
                "function internal() { return 1; }"
            )
            idx = build_symbol_index(tmpdir)
            assert not any("internal" in fqn for fqn in idx.qualified)

    def test_module_names_use_slashes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            src_dir = os.path.join(tmpdir, "src")
            os.makedirs(src_dir)
            Path(os.path.join(src_dir, "auth.ts")).write_text(
                "function decode() { return 1; }"
            )
            idx = build_symbol_index(tmpdir)
            assert any("src/auth.decode" in fqn for fqn in idx.qualified)


# ── callgraph: compute_call_edges ─────────────────────────────────────────────

class TestComputeCallEdges:
    def test_resolves_same_module_call(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            src = textwrap.dedent("""\
                function helper(x) { return x + 1; }
                function main(x) { return helper(x); }
            """)
            fpath = os.path.join(tmpdir, "util.js")
            Path(fpath).write_text(src)
            idx = build_symbol_index(tmpdir)
            edges = compute_call_edges(fpath, tmpdir, idx)
            callers = {e[0].split(".")[-1] for e in edges}
            callees = {e[1].split(".")[-1] for e in edges}
            assert "main" in callers
            assert "helper" in callees

    def test_resolves_cross_file_import(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(os.path.join(tmpdir, "auth.js")).write_text(
                "export function decode(token) { return token; }"
            )
            handler = os.path.join(tmpdir, "handler.js")
            Path(handler).write_text(textwrap.dedent("""\
                import { decode } from './auth';
                function handle(req) { return decode(req.token); }
            """))
            idx = build_symbol_index(tmpdir)
            edges = compute_call_edges(handler, tmpdir, idx)
            callee_names = {e[1].split(".")[-1] for e in edges}
            assert "decode" in callee_names

    def test_external_call_not_resolved(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = os.path.join(tmpdir, "app.js")
            Path(fpath).write_text("function main() { fetch('/api'); }")
            idx = build_symbol_index(tmpdir)
            edges = compute_call_edges(fpath, tmpdir, idx)
            # fetch is not in the project → no edge
            assert all("fetch" not in e[1] for e in edges)


# ── CommonJS define extraction (Blind Spots A+B fix) ──────────────────────────

class TestCjsDefines:
    def _parse(self, src: str) -> list[Define]:
        with tempfile.NamedTemporaryFile(suffix=".js", delete=False) as f:
            f.write(textwrap.dedent(src).encode())
            path = f.name
        try:
            return extract_defines(path)
        finally:
            os.unlink(path)

    # Pattern A: module.exports = { method: function() {} }
    def test_module_exports_object_literal(self):
        src = """\
            'use strict';
            module.exports = {
              use: function use(fn) { this.stack.push(fn); return this; },
              handle: function handle(req, res, done) { done(); },
            };
        """
        defs = self._parse(src)
        names = {d.name for d in defs}
        assert "use" in names
        assert "handle" in names

    def test_module_exports_bare_qualified_names(self):
        src = """\
            module.exports = {
              foo: function(x) { return x; },
              bar: function(y) { return y + 1; },
            };
        """
        defs = self._parse(src)
        qnames = {d.qualified_name for d in defs}
        # bare names — no obj_name prefix
        assert "foo" in qnames
        assert "bar" in qnames
        assert not any("." in q for q in qnames)

    # Pattern B: X.prototype.method = function() {}
    def test_prototype_method(self):
        src = """\
            'use strict';
            function Layer(path, fn) { this.path = path; this.fn = fn; }
            Layer.prototype.handle_request = function handle_request(req, res, next) {
              this.fn(req, res, next);
            };
            Layer.prototype.match = function match(path) {
              return path === this.path;
            };
        """
        defs = self._parse(src)
        names = {d.name for d in defs}
        assert "handle_request" in names
        assert "match" in names

    def test_prototype_method_qualified_name(self):
        src = """\
            function Layer(path, fn) { this.path = path; this.fn = fn; }
            Layer.prototype.handle_request = function(req, res, next) {
              this.fn(req, res, next);
            };
        """
        defs = self._parse(src)
        qnames = {d.qualified_name for d in defs}
        # qualified_name must be Layer.handle_request for this-resolution to work
        assert "Layer.handle_request" in qnames

    # Pattern C: exports.foo = function() {}
    def test_exports_property(self):
        src = """\
            'use strict';
            exports.decode = function decode(token) { return token.split('.')[1]; };
            exports.verify = function verify(token, secret) { return true; };
        """
        defs = self._parse(src)
        names = {d.name for d in defs}
        assert "decode" in names
        assert "verify" in names

    # Pattern A via var chain: var proto = module.exports = { … }
    def test_var_chain_assignment(self):
        src = """\
            'use strict';
            var proto = module.exports = {
              use: function use(fn) { this.stack.push(fn); return this; },
              handle: function handle(req, res, done) { done(); },
              route: function route(path) { return this; },
            };
        """
        defs = self._parse(src)
        names = {d.name for d in defs}
        assert "use" in names
        assert "handle" in names
        assert "route" in names

    # No regression: existing const obj = { … } pattern still works
    def test_no_regression_const_object(self):
        src = """\
            const router = {
              use: function(fn) { return this; },
              handle: (req, res) => res.end(),
            };
        """
        defs = self._parse(src)
        qnames = {d.qualified_name for d in defs}
        assert "router.use" in qnames
        assert "router.handle" in qnames

    # Pattern D: localVar.method = fn (module-level object, e.g. res.send = fn)
    def test_local_object_method_assignment(self):
        src = """\
            var res = Object.create(require('http').ServerResponse.prototype);
            module.exports = res;
            res.status = function status(code) { this.statusCode = code; return this; };
            res.send = function send(body) { this.end(body); return this; };
        """
        defs = self._parse(src)
        names = {d.name for d in defs}
        assert "status" in names
        assert "send" in names

    def test_local_object_method_qualified_name(self):
        src = """\
            var res = {};
            res.send = function(body) { this.end(body); };
        """
        defs = self._parse(src)
        qnames = {d.qualified_name for d in defs}
        assert "res.send" in qnames

    # const X = module.exports = { … } (const + chained assignment)
    def test_const_chain_module_exports(self):
        src = """\
            const proto = module.exports = {
              inspect() { return this.toJSON(); },
              toJSON() { return { request: this.request }; },
            };
        """
        defs = self._parse(src)
        names = {d.name for d in defs}
        assert "inspect" in names
        assert "toJSON" in names

    # Callgraph: module.exports = { use: fn } resolves cross-file via require
    def test_cjs_require_resolution(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(os.path.join(tmpdir, "router.js")).write_text(textwrap.dedent("""\
                'use strict';
                module.exports = {
                  use: function use(fn) { this.stack.push(fn); return this; },
                };
            """))
            caller = os.path.join(tmpdir, "app.js")
            Path(caller).write_text(textwrap.dedent("""\
                'use strict';
                var router = require('./router');
                function main(fn) { router.use(fn); }
            """))
            idx = build_symbol_index(tmpdir)
            edges = compute_call_edges(caller, tmpdir, idx)
            callee_names = {e[1].split(".")[-1] for e in edges}
            assert "use" in callee_names


class TestIifeDefines:
    """Blind spot C: functions inside IIFEs must be extracted as top-level defines."""

    def _parse(self, src: str) -> list[Define]:
        with tempfile.NamedTemporaryFile(suffix=".js", delete=False) as f:
            f.write(textwrap.dedent(src).encode())
            path = f.name
        try:
            return extract_defines(path)
        finally:
            os.unlink(path)

    def test_basic_iife_extracts_function_declarations(self):
        src = """\
            (function() {
              function helper(x) { return x + 1; }
              function chunk(arr, size) { return arr.slice(0, size); }
            })();
        """
        defs = self._parse(src)
        names = {d.name for d in defs}
        assert "helper" in names
        assert "chunk" in names

    def test_lodash_call_form_extracts_functions(self):
        # ;(function() { ... }.call(this)); — lodash pattern
        src = """\
            ;(function() {
              function baseSlice(array, start, end) { return array.slice(start, end); }
              function chunk(array, size) { return array; }
            }.call(this));
        """
        defs = self._parse(src)
        names = {d.name for d in defs}
        assert "baseSlice" in names
        assert "chunk" in names

    def test_arrow_iife_extracts_function_declarations(self):
        src = """\
            (() => {
              function helper() { return 42; }
            })();
        """
        defs = self._parse(src)
        names = {d.name for d in defs}
        assert "helper" in names

    def test_nested_iife_bounded_recursion(self):
        src = """\
            (function() {
              function outer() { return 1; }
              (function() {
                function inner() { return 2; }
              })();
            })();
        """
        defs = self._parse(src)
        names = {d.name for d in defs}
        assert "outer" in names
        assert "inner" in names

    def test_iife_bare_names_no_namespace_prefix(self):
        # IIFE has no namespace → inner functions use bare names
        src = """\
            (function() {
              function doThing() {}
            })();
        """
        defs = self._parse(src)
        qnames = {d.qualified_name for d in defs}
        assert "doThing" in qnames
        assert not any("." in q for q in qnames)

    def test_non_iife_expression_statement_unaffected(self):
        # Regular assignment expression should not be treated as IIFE
        src = """\
            var x = 1;
            function normal() { return x; }
        """
        defs = self._parse(src)
        names = {d.name for d in defs}
        assert "normal" in names
        assert "x" not in names

    def test_no_regression_cjs_still_works(self):
        # Existing CJS patterns must still extract correctly alongside IIFE fix
        src = """\
            module.exports = {
              use: function use(fn) { return fn; },
            };
        """
        defs = self._parse(src)
        names = {d.name for d in defs}
        assert "use" in names


class TestLineRanges:
    """Define.start_line / end_line: 1-based, tree-sitter node bounds."""

    def _parse_py(self, src: str) -> list:
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
            f.write(textwrap.dedent(src).encode())
            path = f.name
        try:
            return extract_defines(path)
        finally:
            os.unlink(path)

    def _parse_js(self, src: str) -> list:
        with tempfile.NamedTemporaryFile(suffix=".js", delete=False) as f:
            f.write(textwrap.dedent(src).encode())
            path = f.name
        try:
            return extract_defines(path)
        finally:
            os.unlink(path)

    def test_py_single_line_function(self):
        src = "def foo(): pass\n"
        defs = self._parse_py(src)
        d = next(d for d in defs if d.name == "foo")
        assert d.start_line == 1
        assert d.end_line == 1

    def test_py_multiline_function(self):
        src = """\
            def foo(x, y):
                a = x + 1
                b = y + 2
                c = a + b
                return c
        """
        defs = self._parse_py(src)
        d = next(d for d in defs if d.name == "foo")
        assert d.start_line == 1
        assert d.end_line == 5

    def test_py_second_function_correct_start(self):
        src = """\
            def foo():
                return 1


            def bar():
                return 2
        """
        defs = self._parse_py(src)
        bar = next(d for d in defs if d.name == "bar")
        assert bar.start_line == 5

    def test_py_method_in_class(self):
        src = """\
            class MyClass:
                def method(self):
                    return 42
        """
        defs = self._parse_py(src)
        m = next(d for d in defs if d.name == "method")
        assert m.start_line == 2
        assert m.end_line == 3

    def test_js_function_declaration(self):
        src = """\
            function hello(name) {
              return 'hi ' + name;
            }
        """
        defs = self._parse_js(src)
        d = next(d for d in defs if d.name == "hello")
        assert d.start_line == 1
        assert d.end_line == 3

    def test_js_arrow_function(self):
        src = """\
            const greet = (x) => {
              return x;
            };
        """
        defs = self._parse_js(src)
        d = next(d for d in defs if d.name == "greet")
        assert d.start_line == 1
        assert d.end_line == 3

    def test_js_class_method(self):
        src = """\
            class Foo {
              bar(x) {
                return x + 1;
              }
            }
        """
        defs = self._parse_js(src)
        d = next(d for d in defs if d.name == "bar")
        assert d.start_line == 2
        assert d.end_line == 4

    def test_cjs_define_has_line_range(self):
        src = """\
            module.exports = {
              use: function use(fn) { return fn; },
            };
        """
        defs = self._parse_js(src)
        d = next(d for d in defs if d.name == "use")
        assert d.start_line >= 1
        assert d.end_line >= d.start_line

    def test_iife_inner_function_has_line_range(self):
        src = """\
            (function() {
              function helper(x) {
                return x + 1;
              }
            })();
        """
        defs = self._parse_js(src)
        d = next(d for d in defs if d.name == "helper")
        assert d.start_line == 2
        assert d.end_line == 4

    def test_start_line_always_lte_end_line(self):
        src = """\
            def short(): return 1
            def multi():
                x = 1
                return x
        """
        defs = self._parse_py(src)
        for d in defs:
            assert d.start_line <= d.end_line, \
                f"{d.name}: start_line={d.start_line} > end_line={d.end_line}"
            assert d.start_line > 0
