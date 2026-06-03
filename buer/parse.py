"""BUER input derivation layer — step 1: parse source files into Define objects.

Public API
----------
extract_defines(file_path) -> list[Define]
compute_fingerprint(define) -> (coarse: str, fine: str)
extract_module_imports(file_path, root="") -> dict[str, str]

Language dispatch (§4.2 / §4.2a)
-----------------
detect_language(file_path) -> str
get_parser(lang) -> Parser
EXTRACTORS                        # {"python", "javascript", "typescript"}

JS/TS callgraph tier: dynamic/static-type languages use call-graph archive
(near-approximate, has spurious edges). Data-flow archive (strict G3) for TS
is留 §4.2a / 单元 3b.

No imports from store/sqlite3. No reconcile / build_call_edges / build_gd_edges.
All inputs auto-derived from source; nothing declared by the agent.
Design ref: BUER_Design_v2.0.md §4.2 / §4.2a / §4.6.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# ── test-file detection ───────────────────────────────────────────────────────

# Directory names whose presence anywhere in the path marks a file as test code.
_TEST_DIR_NAMES: frozenset[str] = frozenset({"tests", "__tests__", "e2e", "cypress"})

# JS/TS filename suffix patterns (checked against the basename).
_JS_TEST_SUFFIXES: tuple[str, ...] = (
    ".test.ts", ".test.tsx", ".test.js", ".test.jsx",
    ".spec.ts", ".spec.tsx", ".spec.js", ".spec.jsx",
    ".e2e.ts",  ".e2e.tsx",
)

# Python filename patterns.
_PY_TEST_PREFIXES: tuple[str, ...] = ("test_",)
_PY_TEST_SUFFIXES: tuple[str, ...] = ("_test.py",)
_PY_TEST_EXACT:    frozenset[str]  = frozenset({"conftest.py"})


def is_test_file(path: str) -> bool:
    """Return True if path is a test/spec file that should be excluded from graph building.

    Convention-based — does not read test-framework config.  Covers JS/TS and Python;
    extend _TEST_DIR_NAMES / _JS_TEST_SUFFIXES / _PY_TEST_* for other languages.

    Precision invariant: production files that happen to contain 'test' in their
    name (vitest.config.ts, scripts/test-smtp.ts) must NOT be excluded.  Only the
    precise patterns below trigger exclusion.
    """
    p = Path(path)
    # 1. Any path component is an exact test-directory name
    if any(part in _TEST_DIR_NAMES for part in p.parts):
        return True
    # 2. JS/TS filename suffix
    name = p.name
    if any(name.endswith(s) for s in _JS_TEST_SUFFIXES):
        return True
    # 3. Python conventions
    if name in _PY_TEST_EXACT:
        return True
    if name.endswith(".py") and (
        any(name.startswith(pfx) for pfx in _PY_TEST_PREFIXES)
        or any(name.endswith(sfx) for sfx in _PY_TEST_SUFFIXES)
    ):
        return True
    return False


# ── language detection ────────────────────────────────────────────────────────

_EXT_TO_LANG: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "tsx",        # TSX grammar includes JSX; separate parser from pure .ts
    ".js": "javascript",
    ".jsx": "javascript",
}


def detect_language(file_path: str) -> str:
    ext = Path(file_path).suffix.lower()
    return _EXT_TO_LANG.get(ext, "unknown")


# ── parser factory ────────────────────────────────────────────────────────────

def get_parser(lang: str):
    """Return a tree-sitter Parser for lang (Python, JavaScript, TypeScript, TSX)."""
    from tree_sitter import Language, Parser
    if lang == "python":
        import tree_sitter_python as tsp
        return Parser(Language(tsp.language()))
    if lang == "javascript":
        import tree_sitter_javascript as tsj
        return Parser(Language(tsj.language()))
    if lang == "typescript":
        import tree_sitter_typescript as tst
        return Parser(Language(tst.language_typescript()))
    if lang == "tsx":
        import tree_sitter_typescript as tst
        return Parser(Language(tst.language_tsx()))
    raise NotImplementedError(f"parser for {lang} not wired (interface reserved)")


# ── Define dataclass ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Define:
    name: str
    qualified_name: str
    file_path: str
    params_shape: tuple       # (req_positional, with_default, has_args, has_kwargs, kw_only)
    returns_kind: str         # "none" | "bare" | "value"
    calls: tuple              # raw callee strings from body (original text)
    imports: tuple            # import targets lexically in body
    side_effects: frozenset   # structural markers
    size_count: int           # statement count for size-bucket (raw)
    numeric_literals: tuple = ()  # numeric literal texts in body, in order (§4.2 feature 6)
    operators: tuple = ()        # operator type names in body, sorted (§4.2 feature 7)
    receiver_types: tuple = ()   # ((var_name, type_name), ...) function-scope type bindings for L1
    self_attr_types: tuple = ()  # ((attr_path, type_name), ...) class-level self-attr bindings for L2
    return_type: str = ""        # explicit -> Type annotation (L3 return-type inference)
    call_assigns: tuple = ()     # ((var_name, func_name), ...) plain x=func() assigns for L3
    name_bindings: tuple = ()    # ((receiver_name, type_name), ...) unified; lang-agnostic


# ── shared tree-sitter helpers ────────────────────────────────────────────────

def _child_of_type(node, *types):
    """First direct child whose .type is in types, or None."""
    for c in node.children:
        if c.type in types:
            return c
    return None


def _identifier_text(node) -> str:
    c = _child_of_type(node, "identifier")
    return c.text.decode() if c else ""


# Transparent wrappers: the variable's runtime behaviour matches the inner type,
# so unwrapping Outer[T] → T is correct (Optional[Store] → Store, etc.).
# Containers (list, Set, Array, …) are NOT transparent: a list[Store] variable
# behaves as a list, not as a Store — unwrapping would bind the wrong type and
# create false call edges (e.g. items.append() misresolved to Store.append).
# Unknown generics default to no-unwrap (conservative, zero false edges).
_TRANSPARENT_WRAPPERS: frozenset[str] = frozenset({
    'Optional', 'Promise', 'Awaitable', 'Final', 'ClassVar', 'Coroutine',
})


def _normalize_type_name(raw: str) -> str:
    """Extract core type name from raw type annotation text (both Python and TS/JS).

    Handles: bare names, Optional[T]/Promise<T>, Union X|Y.
    Conservative: returns '' for containers (list[T]/Array<T>), multi-arg generics
    (Dict[K,V]), or unknown generics — only transparent wrappers are unwrapped.
    Strips leading ':' (TS type_annotation text includes it).
    """
    s = raw.strip().lstrip(':').strip()
    # Union X|Y → take first concrete type
    s = s.split('|')[0].strip().rstrip('?').strip()
    # Unwrap single-arg generics only for transparent wrappers (Optional, Promise, …).
    # Containers (list, Set, Array, …) and unknown generics → return '' (no binding).
    for left, right in (('[', ']'), ('<', '>')):
        if left in s:
            outer_s = s[:s.index(left)].strip()
            if outer_s not in _TRANSPARENT_WRAPPERS:
                return ''
            rest_s = s[s.index(left) + 1:]
            inner = rest_s[:rest_s.index(right)].strip() if right in rest_s else rest_s.strip()
            # Only unwrap single-type-arg (no commas, no nested brackets)
            if inner and ',' not in inner and inner[0].isalpha() and '[' not in inner and '<' not in inner:
                s = inner
            else:
                return ''
            break
    return s if s and s[0].isalpha() and '[' not in s and '<' not in s else ''


def _has_staticmethod(decorated_node) -> bool:
    for c in decorated_node.children:
        if c.type == "decorator":
            ident = _child_of_type(c, "identifier")
            if ident and ident.text == b"staticmethod":
                return True
    return False


def _has_overload(decorated_node) -> bool:
    """True if a decorated_definition carries @overload or @typing.overload.

    @overload stubs are type-only annotations; the real implementation follows
    as the last same-name definition. Stubs must not enter 𝒢_D.
    """
    for c in decorated_node.children:
        if c.type == "decorator":
            name = c.text.decode().lstrip("@").strip()
            if name == "overload" or name.endswith(".overload"):
                return True
    return False


# ── Python: params_shape (§4.2 feature 1) ────────────────────────────────────

def _params_shape(params_node, strip_first: bool) -> tuple:
    """(req_positional, with_default, has_args:bool, has_kwargs:bool, kw_only).

    strip_first=True for non-static methods: skip first positional (self/cls).
    Param names are excluded; only shape counts.
    """
    req = 0
    defaults = 0
    has_args = False
    has_kwargs = False
    kw_only = 0
    in_kw_only = False
    _skip = strip_first  # drops the first positional (self/cls)

    for c in params_node.children:
        t = c.type
        if t in (",", "(", ")"):
            continue

        if t in ("identifier", "typed_parameter"):
            if _skip:
                _skip = False
                continue
            if in_kw_only:
                kw_only += 1
            else:
                req += 1

        elif t in ("default_parameter", "typed_default_parameter"):
            # default_parameter never the first positional (Python syntax forbids it)
            if in_kw_only:
                kw_only += 1
            else:
                defaults += 1

        elif t == "list_splat_pattern":  # *args
            has_args = True
            in_kw_only = True

        elif t == "keyword_separator":  # bare * with no name
            in_kw_only = True

        elif t == "dictionary_splat_pattern":  # **kwargs
            has_kwargs = True

    return (req, defaults, has_args, has_kwargs, kw_only)


# ── Python: returns_kind (§4.2 feature 2) ────────────────────────────────────

def _returns_kind(body_node) -> str:
    """
    "value" ≥1 return <expr>;  "bare" only bare return;  "none" no return.
    Does not descend into nested function/class defs.
    """
    has_value = False
    has_bare = False

    def walk(node):
        nonlocal has_value, has_bare
        for c in node.children:
            if c.type in ("function_definition", "class_definition"):
                continue
            if c.type == "return_statement":
                non_kw = [x for x in c.children if x.type != "return"]
                if non_kw:
                    has_value = True
                else:
                    has_bare = True
            else:
                walk(c)

    walk(body_node)
    if has_value:
        return "value"
    if has_bare:
        return "bare"
    return "none"


# ── Python: calls extraction (§4.2 feature 3 input) ──────────────────────────

def _collect_calls(body_node) -> tuple:
    """Raw callee strings from call nodes in body.

    identifier callees: plain name ("decode").
    attribute callees: full dot-text ("self._check", "json.dumps").
    Nested calls are included. Does not descend into nested defs.
    """
    calls: list[str] = []

    def walk(node):
        for c in node.children:
            if c.type in ("function_definition", "class_definition"):
                continue
            if c.type == "call":
                fn = c.children[0] if c.children else None
                if fn is not None and fn.type in ("identifier", "attribute"):
                    calls.append(fn.text.decode())
            walk(c)

    walk(body_node)
    return tuple(calls)


# ── Python: numeric literals (§4.2 feature 6) ────────────────────────────────

def _collect_numeric_literals(body_node) -> tuple:
    """Numeric literals (integer/float) in body, in source order.

    Does not descend into nested defs/classes (consistent with _collect_calls).
    Used as fingerprint feature 6 so pure-numeric edits (e.g. timeout=30→5000)
    are visible to reconcile.
    """
    if body_node is None:
        return ()
    nums: list[str] = []

    def walk(node):
        for c in node.children:
            if c.type in ("function_definition", "class_definition"):
                continue
            if c.type in ("integer", "float"):
                nums.append(c.text.decode().strip())
            walk(c)

    walk(body_node)
    return tuple(nums)


# ── Python: operators (§4.2 feature 7) ───────────────────────────────────────

_PY_BINOP_MAP: dict[str, str] = {
    "+": "Add", "-": "Sub", "*": "Mult", "/": "Div",
    "%": "Mod", "**": "Pow", "//": "FloorDiv",
    "&": "BitAnd", "|": "BitOr", "^": "BitXor",
    "<<": "LShift", ">>": "RShift", "@": "MatMult",
}
_PY_CMPOP_MAP: dict[str, str] = {
    "<": "Lt", ">": "Gt", "<=": "LtE", ">=": "GtE",
    "==": "Eq", "!=": "NotEq", "<>": "NotEq",
    "in": "In", "is": "Is",
}
_PY_BOOLOP_MAP: dict[str, str] = {"and": "And", "or": "Or"}
_PY_UNARYOP_MAP: dict[str, str] = {"not": "Not", "-": "USub", "+": "UAdd", "~": "Invert"}
_PY_AUGOP_MAP: dict[str, str] = {
    "+=": "AugAdd", "-=": "AugSub", "*=": "AugMult", "/=": "AugDiv",
    "%=": "AugMod", "**=": "AugPow", "//=": "AugFloorDiv",
    "&=": "AugBitAnd", "|=": "AugBitOr", "^=": "AugBitXor",
    "<<=": "AugLShift", ">>=": "AugRShift", "@=": "AugMatMult",
}


def _collect_operators(body_node) -> tuple:
    """Operator type names (Python AST class names) in body, sorted (multiset).

    Covers BinOp/Compare/BoolOp/UnaryOp/AugAssign.
    Does not descend into nested function/class definitions.
    """
    if body_node is None:
        return ()
    ops: list[str] = []

    def walk(node) -> None:
        for c in node.children:
            if c.type in ("function_definition", "class_definition"):
                continue
            if c.type == "binary_operator":
                for ch in c.children:
                    name = _PY_BINOP_MAP.get(ch.type)
                    if name:
                        ops.append(name)
                        break
            elif c.type == "comparison_operator":
                unnamed = [ch for ch in c.children if not ch.is_named]
                i = 0
                while i < len(unnamed):
                    t = unnamed[i].type
                    if t == "not" and i + 1 < len(unnamed) and unnamed[i + 1].type == "in":
                        ops.append("NotIn")
                        i += 2
                    elif t == "is" and i + 1 < len(unnamed) and unnamed[i + 1].type == "not":
                        ops.append("IsNot")
                        i += 2
                    else:
                        name = _PY_CMPOP_MAP.get(t)
                        if name:
                            ops.append(name)
                        i += 1
            elif c.type == "boolean_operator":
                for ch in c.children:
                    name = _PY_BOOLOP_MAP.get(ch.type)
                    if name:
                        ops.append(name)
                        break
            elif c.type == "not_operator":
                ops.append("Not")
            elif c.type == "unary_operator":
                for ch in c.children:
                    name = _PY_UNARYOP_MAP.get(ch.type)
                    if name:
                        ops.append(name)
                        break
            elif c.type == "augmented_assignment":
                for ch in c.children:
                    name = _PY_AUGOP_MAP.get(ch.type)
                    if name:
                        ops.append(name)
                        break
            walk(c)

    walk(body_node)
    return tuple(sorted(ops))


# ── Python: body imports ───────────────────────────────────────────────────────

def _collect_body_imports(body_node) -> tuple:
    """Import targets lexically inside this define's body (local imports)."""
    targets: list[str] = []

    def walk(node):
        for c in node.children:
            if c.type in ("function_definition", "class_definition"):
                continue
            if c.type == "import_statement":
                for x in c.children:
                    if x.type == "dotted_name":
                        targets.append(x.text.decode())
            elif c.type == "import_from_statement":
                for x in c.children:
                    if x.type == "dotted_name":
                        targets.append(x.text.decode())
            else:
                walk(c)

    walk(body_node)
    return tuple(targets)


# ── Python: side_effects (§4.2 feature 4) ────────────────────────────────────

def _side_effects(body_node, is_async_def: bool) -> frozenset:
    """Structural markers:
    attr_write, subscript_write, global_write, nonlocal_write,
    async, generator, raises.
    Does not descend into nested defs.
    """
    effects: set[str] = set()
    if is_async_def:
        effects.add("async")

    def walk(node):
        for c in node.children:
            if c.type in ("function_definition", "class_definition"):
                continue
            if c.type == "global_statement":
                effects.add("global_write")
            elif c.type == "nonlocal_statement":
                effects.add("nonlocal_write")
            elif c.type == "raise_statement":
                effects.add("raises")
            elif c.type == "await":
                effects.add("async")
            elif c.type == "yield":
                effects.add("generator")
            elif c.type in ("assignment", "augmented_assignment"):
                target = c.children[0] if c.children else None
                if target is not None:
                    if target.type == "attribute":
                        effects.add("attr_write")
                    elif target.type == "subscript":
                        effects.add("subscript_write")
            walk(c)

    walk(body_node)
    return frozenset(effects)


# ── Python: size_count (§4.2 feature 5 input) ────────────────────────────────

_SKIP_STMT_TYPES = frozenset({
    "newline", "indent", "dedent", "comment",
    ",", "(", ")", ":", ";", "{", "}", "[", "]",
})


def _count_statements(body_node) -> int:
    """Count direct statement-level children of the block."""
    return sum(1 for c in body_node.children if c.type not in _SKIP_STMT_TYPES)


# ── Python: L1 receiver-type extraction (§4.2 L1 type inference) ─────────────

def _norm_type_name(node) -> Optional[str]:
    """Normalize a type annotation AST node to a clean class name, or None.

    Only returns names whose first character is uppercase (class convention).
    Handles: type wrapper, bare identifier, dotted attribute, Optional[T].
    Conservative: anything unclear returns None.
    """
    t = node.type
    # 'type' is a named wrapper node in tree-sitter-python — unwrap it.
    if t == "type":
        named_ch = [c for c in node.children if c.is_named]
        return _norm_type_name(named_ch[0]) if named_ch else None
    if t == "identifier":
        n = node.text.decode()
        return n if n and n[0].isupper() else None
    if t == "attribute":
        text = node.text.decode()
        last = text.rsplit(".", 1)[-1]
        return text if last and last[0].isupper() else None
    if t == "generic_type":
        # Optional[T] pattern: generic_type → identifier("Optional") + type_parameter
        named_ch = [c for c in node.children if c.is_named]
        if not named_ch or named_ch[0].type != "identifier":
            return None
        if named_ch[0].text.decode() != "Optional":
            return None
        for ch in named_ch[1:]:
            if ch.type == "type_parameter":
                inner = [c for c in ch.children if c.is_named]
                if inner:
                    return _norm_type_name(inner[0])
        return None
    if t == "subscript":
        # Fallback for older tree-sitter-python using subscript instead of generic_type
        ch = [c for c in node.children if c.type not in ("[", "]", ",")]
        if not ch:
            return None
        if ch[0].type == "identifier" and ch[0].text.decode() == "Optional" and len(ch) > 1:
            return _norm_type_name(ch[1])
        return None
    return None


def _collect_receiver_types(params_node, body_node, skip_first: bool) -> tuple:
    """Extract (var_name, type_name) pairs for L1 function-scope type inference.

    Sources:
      1. Typed parameters: def f(store: Store) → ("store", "Store")
      2. Annotated assignment in body: store: Store [= ...] → ("store", "Store")
      3. Constructor assignment: store = Store(...) → ("store", "Store")

    In tree-sitter-python, both annotated and plain assignments appear as
    'assignment' nodes; annotated ones carry a 'type' child node.

    skip_first=True for non-static instance methods (drops self/cls).
    Last binding wins (linear scan). Returns tuple of (var, type) pairs.
    Conservative: skips anything that can't be cleanly expressed as an
    uppercase-initial name or dotted attribute path.
    """
    bindings: dict[str, str] = {}

    # ── 1. Typed parameters ───────────────────────────────────────────────────
    if params_node:
        _skip = skip_first
        for c in params_node.children:
            if c.type in (",", "(", ")"):
                continue
            if c.type in ("typed_parameter", "typed_default_parameter"):
                # children: identifier ":" type_node [= default]
                # tree-sitter wraps the annotation in a named 'type' node.
                if _skip:
                    _skip = False
                    continue
                idents = [x for x in c.children if x.type == "identifier"]
                type_nodes = [x for x in c.children if x.type == "type"]
                if not idents:
                    continue
                param_name = idents[0].text.decode()
                type_node = type_nodes[0] if type_nodes else None
                if type_node:
                    tn = _norm_type_name(type_node)
                    if tn:
                        bindings[param_name] = tn
            elif c.type in ("identifier", "default_parameter",
                            "list_splat_pattern", "dictionary_splat_pattern",
                            "keyword_separator"):
                if _skip:
                    _skip = False

    # ── 2 + 3. Body scan ──────────────────────────────────────────────────────
    if body_node:
        def walk(node):
            for c in node.children:
                if c.type in ("function_definition", "class_definition"):
                    continue
                if c.type == "assignment":
                    ch = c.children
                    if not ch or ch[0].type != "identifier":
                        walk(c)
                        continue
                    left = ch[0]
                    type_child = next((x for x in ch if x.type == "type"), None)
                    if type_child:
                        # Annotated assignment: identifier ":" type [= value]
                        tn = _norm_type_name(type_child)
                        if tn:
                            bindings[left.text.decode()] = tn
                    else:
                        # Plain assignment: check for constructor on RHS
                        right = ch[-1]
                        if right.type == "call":
                            fn = right.children[0] if right.children else None
                            if fn is not None:
                                if fn.type == "identifier":
                                    cn = fn.text.decode()
                                    if cn and cn[0].isupper():
                                        bindings[left.text.decode()] = cn
                                elif fn.type == "attribute":
                                    text = fn.text.decode()
                                    last = text.rsplit(".", 1)[-1]
                                    if last and last[0].isupper():
                                        bindings[left.text.decode()] = text
                else:
                    walk(c)
        walk(body_node)

    return tuple(bindings.items())


# ── Python: L2 self-attribute type extraction (§4.2 L2 type inference) ───────

def _collect_self_attr_types(class_block_node) -> dict[str, str]:
    """Scan all method bodies in a class block for self-attribute type bindings.

    Returns {"self.attr": "TypeName"} for bindings found via:
      - Constructor:  self.x = ClassName()
      - Annotation:   self.x: ClassName [= ...]

    Conflicts (same attr bound to two different types) are excluded.
    Conservative: only exact self.X patterns (no nested paths).
    """
    seen: dict[str, str] = {}
    conflicts: set = set()

    def record(key: str, val: str) -> None:
        if key in conflicts:
            return
        if key in seen:
            if seen[key] != val:
                conflicts.add(key)
                del seen[key]
        else:
            seen[key] = val

    def walk_body(node) -> None:
        for ch in node.children:
            if ch.type in ("function_definition", "class_definition"):
                continue
            if ch.type == "assignment":
                children = ch.children
                if not children or children[0].type != "attribute":
                    walk_body(ch)
                    continue
                left = children[0]
                left_text = left.text.decode()
                parts = left_text.split(".")
                if len(parts) != 2 or parts[0] != "self":
                    walk_body(ch)
                    continue
                attr_path = left_text  # "self.store"
                type_child = next((x for x in children if x.type == "type"), None)
                if type_child:
                    tn = _norm_type_name(type_child)
                    if tn:
                        record(attr_path, tn)
                else:
                    right = children[-1]
                    if right.type == "call":
                        fn_ch = right.children[0] if right.children else None
                        if fn_ch is not None:
                            if fn_ch.type == "identifier":
                                cn = fn_ch.text.decode()
                                if cn and cn[0].isupper():
                                    record(attr_path, cn)
                            elif fn_ch.type == "attribute":
                                text = fn_ch.text.decode()
                                last = text.rsplit(".", 1)[-1]
                                if last and last[0].isupper():
                                    record(attr_path, text)
            else:
                walk_body(ch)

    for c in class_block_node.children:
        fn_node = None
        if c.type == "function_definition":
            fn_node = c
        elif c.type == "decorated_definition":
            fn_node = _child_of_type(c, "function_definition")
        if fn_node is None:
            continue
        body = _child_of_type(fn_node, "block")
        if body:
            walk_body(body)

    return seen


# ── Python: L3 call-assign extraction (§4.2 L3 return-type inference) ────────

def _collect_call_assigns(body_node) -> tuple:
    """Extract (var_name, func_name) for x = func() plain-call assignments (L3).

    Captures simple-name lowercase function calls assigned to plain variables:
      store = get_store()  → ("store", "get_store")

    Excludes: uppercase calls (constructors → L1), annotated assignments (→ L1),
    dotted-call RHS, attribute targets (self.x → L2).
    Last binding wins (linear scan). Conservative.
    """
    assigns: dict[str, str] = {}

    def walk(node) -> None:
        for c in node.children:
            if c.type in ("function_definition", "class_definition"):
                continue
            if c.type == "assignment":
                ch = c.children
                if not ch or ch[0].type != "identifier":
                    walk(c)
                    continue
                left = ch[0]
                type_child = next((x for x in ch if x.type == "type"), None)
                if type_child:
                    # Annotated: handled by L1, skip
                    walk(c)
                    continue
                right = ch[-1]
                if right.type == "call":
                    fn = right.children[0] if right.children else None
                    if fn is not None and fn.type == "identifier":
                        cn = fn.text.decode()
                        if cn and cn[0].islower():
                            assigns[left.text.decode()] = cn
            else:
                walk(c)

    walk(body_node)
    return tuple(assigns.items())


# ── Python: unified name-binding extraction (§4.2 name_bindings) ─────────────

def _collect_py_file_return_types(tree_root) -> dict[str, str]:
    """Scan whole file AST for def f() -> T; return {func_name: type_name}.

    Used to resolve x = f() bindings within the same file (L3 file-scope).
    Conflicts (same name, different return types) are excluded conservatively.
    """
    result: dict[str, str] = {}
    conflicts: set[str] = set()

    def record(name: str, tn: str) -> None:
        if name in conflicts:
            return
        if name in result and result[name] != tn:
            conflicts.add(name)
            del result[name]
        else:
            result[name] = tn

    def walk(node) -> None:
        for c in node.children:
            fn_node = None
            if c.type == "function_definition":
                fn_node = c
            elif c.type == "decorated_definition":
                fn_node = _child_of_type(c, "function_definition")
            if fn_node is not None:
                name_node = next((x for x in fn_node.children if x.type == "identifier"), None)
                type_node = _child_of_type(fn_node, "type")
                if name_node and type_node:
                    tn = _normalize_type_name(type_node.text.decode())
                    if tn:
                        record(name_node.text.decode(), tn)
                walk(fn_node)
            else:
                walk(c)

    walk(tree_root)
    return result


def _collect_py_name_bindings(
    fn_node,
    class_field_types: Optional[dict] = None,
    file_return_types: Optional[dict] = None,
    skip_first: bool = False,
) -> tuple:
    """Unified name→type binding extraction for Python functions (L1+L2+L3 file-scope).

    Sources (all yield the same (receiver_name, type_name) pairs):
      1. Typed parameters:  def f(store: Store) → ("store", "Store")
      2. Body annotated assignment:  store: Store [= ...]  → ("store", "Store")
      3. Constructor assignment:  store = Store()  → ("store", "Store")
      4. File-scope return call:  store = get_store() (get_store→Store) → ("store", "Store")
      5. Class field types (self.x):  merged from class_field_types {"self.x": "T"}

    skip_first=True strips self/cls from typed param scan (instance methods).
    Last binding wins for local vars. class_field_types pre-merged (lower priority).
    """
    params_node = _child_of_type(fn_node, "parameters")
    body_node = _child_of_type(fn_node, "block")

    bindings: dict[str, str] = {}

    # 5. Class field types (self.x → T) — lowest priority, function body overrides
    if class_field_types:
        bindings.update(class_field_types)

    # 1. Typed parameters
    if params_node:
        _skip = skip_first
        for c in params_node.children:
            if c.type in (",", "(", ")"):
                continue
            if c.type in ("typed_parameter", "typed_default_parameter"):
                if _skip:
                    _skip = False
                    continue
                idents = [x for x in c.children if x.type == "identifier"]
                type_nodes = [x for x in c.children if x.type == "type"]
                if idents and type_nodes:
                    tn = _normalize_type_name(type_nodes[0].text.decode())
                    if tn:
                        bindings[idents[0].text.decode()] = tn
            elif c.type in ("identifier", "default_parameter",
                            "list_splat_pattern", "dictionary_splat_pattern",
                            "keyword_separator"):
                if _skip:
                    _skip = False

    # 2+3+4. Body scan
    if body_node:
        frt = file_return_types or {}

        def walk(node) -> None:
            for c in node.children:
                if c.type in ("function_definition", "class_definition"):
                    continue
                if c.type == "assignment":
                    ch = c.children
                    if not ch or ch[0].type != "identifier":
                        walk(c)
                        continue
                    left = ch[0]
                    var_name = left.text.decode()
                    type_child = next((x for x in ch if x.type == "type"), None)
                    if type_child:
                        tn = _normalize_type_name(type_child.text.decode())
                        if tn:
                            bindings[var_name] = tn
                    else:
                        right = ch[-1]
                        if right.type == "call":
                            fn_ch = right.children[0] if right.children else None
                            if fn_ch is not None and fn_ch.type == "identifier":
                                cn = fn_ch.text.decode()
                                if cn and cn[0].isupper():
                                    bindings[var_name] = cn      # constructor
                                elif cn and cn[0].islower() and cn in frt:
                                    bindings[var_name] = frt[cn] # file-scope return type
                else:
                    walk(c)

        walk(body_node)

    return tuple(bindings.items())


# ── Python: function extractor ────────────────────────────────────────────────

def _extract_function(
    fn_node,
    file_path: str,
    class_prefix: Optional[str],
    is_method: bool,
    is_static: bool,
    self_attr_types: Optional[dict] = None,
    class_field_types: Optional[dict] = None,
    file_return_types: Optional[dict] = None,
) -> Define:
    name = _identifier_text(fn_node)
    qualified_name = f"{class_prefix}.{name}" if class_prefix else name

    params_node = _child_of_type(fn_node, "parameters")
    body_node = _child_of_type(fn_node, "block")
    is_async_def = bool(fn_node.children) and fn_node.children[0].type == "async"

    skip_first = is_method and not is_static
    params = _params_shape(params_node, skip_first) if params_node else (0, 0, False, False, 0)
    ret = _returns_kind(body_node) if body_node else "none"
    calls = _collect_calls(body_node) if body_node else ()
    body_imports = _collect_body_imports(body_node) if body_node else ()
    effects = _side_effects(body_node, is_async_def) if body_node else frozenset()
    size = _count_statements(body_node) if body_node else 0
    numerics = _collect_numeric_literals(body_node) if body_node else ()
    operators = _collect_operators(body_node) if body_node else ()
    receiver_types = _collect_receiver_types(params_node, body_node, skip_first)
    call_assigns = _collect_call_assigns(body_node) if body_node else ()
    # Return type annotation: the 'type' child of function_definition (after ->)
    rt_node = _child_of_type(fn_node, "type")
    return_type = _norm_type_name(rt_node) or "" if rt_node else ""

    # Unified name_bindings (replaces separate L1/L2/L3 reasoning in resolve_callee)
    name_bindings = _collect_py_name_bindings(
        fn_node,
        class_field_types=class_field_types or self_attr_types,
        file_return_types=file_return_types,
        skip_first=skip_first,
    )

    return Define(
        name=name,
        qualified_name=qualified_name,
        file_path=file_path,
        params_shape=params,
        returns_kind=ret,
        calls=calls,
        imports=body_imports,
        side_effects=effects,
        size_count=size,
        numeric_literals=numerics,
        operators=operators,
        receiver_types=receiver_types,
        self_attr_types=tuple((self_attr_types or {}).items()),
        return_type=return_type,
        call_assigns=call_assigns,
        name_bindings=name_bindings,
    )


# ── Python: recursive define collector ───────────────────────────────────────

def _collect_defines(
    node,
    file_path: str,
    class_prefix: Optional[str],
    is_class_body: bool,
    out: list,
    class_self_attr_types: Optional[dict] = None,
    file_return_types: Optional[dict] = None,
) -> None:
    for c in node.children:
        if c.type == "function_definition":
            out.append(_extract_function(
                c, file_path, class_prefix,
                is_method=is_class_body, is_static=False,
                self_attr_types=class_self_attr_types,
                class_field_types=class_self_attr_types,
                file_return_types=file_return_types,
            ))

        elif c.type == "decorated_definition":
            if _has_overload(c):
                continue  # @overload is a type-only stub; real impl follows — skip
            is_static = _has_staticmethod(c)
            fn = _child_of_type(c, "function_definition")
            cls = _child_of_type(c, "class_definition")
            if fn:
                out.append(_extract_function(
                    fn, file_path, class_prefix,
                    is_method=is_class_body, is_static=is_static,
                    self_attr_types=class_self_attr_types,
                    class_field_types=class_self_attr_types,
                    file_return_types=file_return_types,
                ))
            elif cls:
                # Decorated class (@dataclass, @attrs, etc.): same handling as bare
                # class_definition — class is a namespace, recurse into its body.
                cname = _identifier_text(cls)
                prefix = f"{class_prefix}.{cname}" if class_prefix else cname
                block = _child_of_type(cls, "block")
                if block:
                    sat = _collect_self_attr_types(block)
                    _collect_defines(block, file_path, prefix, is_class_body=True, out=out,
                                     class_self_attr_types=sat, file_return_types=file_return_types)

        elif c.type == "class_definition":
            # Class is a namespace in 𝒢_D, not an independent node (§3.3):
            # classes don't produce/consume — no real 𝒢_D edges at class level.
            # qualified_name carries the class prefix ("Cls.method") so the class
            # acts as a naming scope; qualified_name tracks the class prefix only.
            cname = _identifier_text(c)
            prefix = f"{class_prefix}.{cname}" if class_prefix else cname
            block = _child_of_type(c, "block")
            if block:
                sat = _collect_self_attr_types(block)
                _collect_defines(block, file_path, prefix, is_class_body=True, out=out,
                                 class_self_attr_types=sat, file_return_types=file_return_types)

        elif c.type in ("if_statement", "try_statement", "with_statement",
                        "for_statement", "while_statement"):
            # Descend into control-flow blocks to pick up module-level defs.
            # Same-name defs in multiple branches approximate platform-specific
            # fallbacks (accepted elsewhere): multi-edge, not false-edge.
            for block in c.children:
                if block.type == "block":
                    _collect_defines(block, file_path, class_prefix, is_class_body, out,
                                     class_self_attr_types=class_self_attr_types,
                                     file_return_types=file_return_types)


def _extract_python(file_path: str, src: bytes, parser) -> list[Define]:
    tree = parser.parse(src)
    out: list[Define] = []
    file_return_types = _collect_py_file_return_types(tree.root_node)
    _collect_defines(tree.root_node, file_path, class_prefix=None, is_class_body=False, out=out,
                     file_return_types=file_return_types)
    return out


# ── JS/TS shared constants ─────────────────────────────────────────────────────

# Node types that form a nested function boundary (do not descend into these
# when walking a function body for calls/side-effects/returns).
_JS_FUNC_SKIP: frozenset[str] = frozenset({
    "function_declaration",
    "generator_function_declaration",
    "function_expression",
    "arrow_function",
    "method_definition",
    "class_declaration",
})

# Punctuation and non-structural children of statement_block to skip in size_count.
# "comment" covers //, /* */, and /** */ — all share the same node type in
# tree-sitter-javascript/typescript.  Comments are documentation, not logic;
# including them would make size_count (and therefore the fingerprint) sensitive
# to whether a line has a comment, causing false define_loop misses.
_JS_SKIP_STMT: frozenset[str] = frozenset({"{", "}", "comment"})


# ── JS/TS params_shape ─────────────────────────────────────────────────────────

def _js_params_shape(params_node) -> tuple:
    """(req, defaults, has_args, False, 0) — JS/TS has no **kwargs or kw_only.

    Handles JS bare patterns (identifier, assignment_pattern, rest_pattern,
    object_pattern, array_pattern) and TS wrappers (required_parameter,
    optional_parameter).  TS rest (`...x: T[]`) appears as required_parameter
    wrapping a rest_pattern child.
    """
    req = 0
    defaults = 0
    has_args = False

    for c in params_node.children:
        t = c.type
        if t in (",", "(", ")"):
            continue
        if t == "required_parameter":
            # TS: may wrap a rest_pattern (...x) or have a default (= expr)
            if any(x.type == "rest_pattern" for x in c.children):
                has_args = True
            elif any(x.type == "=" for x in c.children):
                defaults += 1
            else:
                req += 1
        elif t == "optional_parameter":
            # TS: x?: T  — optional at call site
            defaults += 1
        elif t == "rest_parameter":
            # Standalone rest_parameter (some grammars)
            has_args = True
        elif t in ("identifier", "object_pattern", "array_pattern"):
            req += 1
        elif t == "assignment_pattern":
            defaults += 1
        elif t == "rest_pattern":
            has_args = True

    return (req, defaults, has_args, False, 0)


# ── JS/TS returns_kind ─────────────────────────────────────────────────────────

def _js_returns_kind(body_node) -> str:
    """returns_kind for a JS/TS body.

    Expression body (arrow without braces): always "value".
    statement_block: walk for return_statement, same logic as Python.
    """
    if body_node is None:
        return "none"
    if body_node.type != "statement_block":
        return "value"  # arrow function expression body

    has_value = False
    has_bare = False

    def walk(node):
        nonlocal has_value, has_bare
        for c in node.children:
            if c.type in _JS_FUNC_SKIP:
                continue
            if c.type == "return_statement":
                non_kw = [x for x in c.children if x.type not in ("return", ";")]
                if non_kw:
                    has_value = True
                else:
                    has_bare = True
            else:
                walk(c)

    walk(body_node)
    if has_value:
        return "value"
    if has_bare:
        return "bare"
    return "none"


# ── JS/TS calls ────────────────────────────────────────────────────────────────

def _js_collect_calls(body_node) -> tuple:
    """call_expression targets in body; does not descend into nested callables.

    Checks the root body_node itself (handles arrow expression bodies where the
    entire body is a single call_expression) then recurses into children.
    """
    if body_node is None:
        return ()
    calls: list[str] = []

    def _add_if_call(node) -> None:
        if node.type == "call_expression":
            fn = node.children[0] if node.children else None
            if fn is not None and fn.type in ("identifier", "member_expression"):
                calls.append(fn.text.decode())

    def walk(node) -> None:
        for c in node.children:
            if c.type in _JS_FUNC_SKIP:
                continue
            _add_if_call(c)
            walk(c)

    _add_if_call(body_node)  # for arrow expression bodies
    walk(body_node)
    return tuple(calls)


# ── JS/TS numeric literals (§4.2 feature 6) ───────────────────────────────────

def _js_collect_numeric_literals(body_node) -> tuple:
    """Numeric literals (number nodes) in JS/TS body, in source order.

    Does not descend into nested callables (consistent with _js_collect_calls).
    """
    if body_node is None:
        return ()
    nums: list[str] = []

    def _add_if_num(node) -> None:
        if node.type == "number":
            nums.append(node.text.decode().strip())

    def walk(node) -> None:
        for c in node.children:
            if c.type in _JS_FUNC_SKIP:
                continue
            _add_if_num(c)
            walk(c)

    _add_if_num(body_node)  # arrow expression body
    walk(body_node)
    return tuple(nums)


# ── JS/TS operators (§4.2 feature 7) ─────────────────────────────────────────

_JS_OP_NODES: frozenset[str] = frozenset({
    "binary_expression",
    "unary_expression",
    "augmented_assignment_expression",
})


def _js_collect_operators(body_node) -> tuple:
    """Operator symbols in JS/TS body, sorted (multiset).

    Covers binary_expression / unary_expression / augmented_assignment_expression.
    Symbols used directly ('+', '-', '===', '&&', '!', etc.).
    Does not descend into nested callables.
    """
    if body_node is None:
        return ()
    ops: list[str] = []

    def walk(node) -> None:
        for c in node.children:
            if c.type in _JS_FUNC_SKIP:
                continue
            if c.type in _JS_OP_NODES:
                for ch in c.children:
                    if not ch.is_named:
                        op_text = ch.text.decode().strip()
                        if op_text:
                            ops.append(op_text)
                            break
            walk(c)

    walk(body_node)
    return tuple(sorted(ops))


# ── JS/TS side_effects ─────────────────────────────────────────────────────────

def _js_side_effects(body_node, is_async: bool) -> frozenset:
    """Structural side-effect markers for JS/TS.

    async, generator, raises, attr_write, subscript_write.
    JS has no explicit global/nonlocal statements; those markers are omitted.
    Checks the root body_node itself (arrow expression body) then recurses.
    """
    if body_node is None:
        return frozenset({"async"}) if is_async else frozenset()

    effects: set[str] = set()
    if is_async:
        effects.add("async")

    def _check(node) -> None:
        if node.type == "await_expression":
            effects.add("async")
        elif node.type == "yield_expression":
            effects.add("generator")
        elif node.type == "throw_statement":
            effects.add("raises")
        elif node.type in ("assignment_expression", "augmented_assignment_expression"):
            lhs = node.children[0] if node.children else None
            if lhs is not None:
                if lhs.type == "member_expression":
                    effects.add("attr_write")
                elif lhs.type == "subscript_expression":
                    effects.add("subscript_write")

    def walk(node) -> None:
        for c in node.children:
            if c.type in _JS_FUNC_SKIP:
                continue
            _check(c)
            walk(c)

    _check(body_node)  # for arrow expression bodies
    walk(body_node)
    return frozenset(effects)


# ── JS/TS size_count ───────────────────────────────────────────────────────────

def _js_count_statements(body_node) -> int:
    """Statement count in a JS/TS body.

    statement_block: direct children minus { and }.
    Expression body (arrow): 1.
    """
    if body_node is None:
        return 0
    if body_node.type != "statement_block":
        return 1
    return sum(1 for c in body_node.children if c.type not in _JS_SKIP_STMT)


# ── JS/TS type-binding helpers (§4.2 unified name bindings) ──────────────────

def _collect_js_file_return_types(tree_root) -> dict[str, str]:
    """Scan TS/JS file for function f(): T annotations; return {func_name: type_name}."""
    result: dict[str, str] = {}
    conflicts: set[str] = set()

    def record(name: str, tn: str) -> None:
        if name in conflicts:
            return
        if name in result and result[name] != tn:
            conflicts.add(name)
            del result[name]
        else:
            result[name] = tn

    def walk(node) -> None:
        for c in node.children:
            if c.type in ("function_declaration", "generator_function_declaration"):
                name_node = next((x for x in c.children if x.type == "identifier"), None)
                type_ann = next((x for x in c.children if x.type == "type_annotation"), None)
                if name_node and type_ann:
                    tn = _normalize_type_name(type_ann.text.decode())
                    if tn:
                        record(name_node.text.decode(), tn)
                walk(c)
            elif c.type in ("lexical_declaration", "variable_declaration"):
                for vd in c.children:
                    if vd.type != "variable_declarator":
                        continue
                    vname = vd.children[0] if vd.children else None
                    if vname and vname.type == "identifier":
                        fn_node = _find_callable_deep(vd)
                        if fn_node:
                            type_ann = next((x for x in fn_node.children if x.type == "type_annotation"), None)
                            if type_ann:
                                tn = _normalize_type_name(type_ann.text.decode())
                                if tn:
                                    record(vname.text.decode(), tn)
                walk(c)
            elif c.type == "export_statement":
                walk(c)
            else:
                walk(c)

    walk(tree_root)
    return result


def _collect_js_class_field_types(class_body_node) -> dict[str, str]:
    """Collect {this.field: TypeName} from TS/JS class body.

    Sources:
      - public_field_definition with type_annotation: private store: Store
      - public_field_definition with new_expression init: store = new Store()
      - method_definition bodies: this.store = new Store()
    Conflict detection: same field name, different types → excluded.
    """
    result: dict[str, str] = {}
    conflicts: set[str] = set()

    def record(key: str, val: str) -> None:
        if key in conflicts:
            return
        if key in result and result[key] != val:
            conflicts.add(key)
            del result[key]
        else:
            result[key] = val

    for c in class_body_node.children:
        if c.type in ("public_field_definition", "field_definition"):
            prop = next((x for x in c.children
                         if x.type in ("property_identifier", "private_identifier")), None)
            if prop is None:
                continue
            field_name = prop.text.decode().lstrip('#')
            key = f"this.{field_name}"
            type_ann = next((x for x in c.children if x.type == "type_annotation"), None)
            if type_ann:
                tn = _normalize_type_name(type_ann.text.decode())
                if tn:
                    record(key, tn)
                    continue
            for ch in c.children:
                if ch.type == "new_expression":
                    for gch in ch.children:
                        if gch.type in ("identifier", "type_identifier"):
                            cn = gch.text.decode()
                            if cn and cn[0].isupper():
                                record(key, cn)
                            break
                    break

        elif c.type == "method_definition":
            body = _child_of_type(c, "statement_block")
            if body is None:
                continue

            def walk_method(node) -> None:
                for ch in node.children:
                    if ch.type in _JS_FUNC_SKIP:
                        continue
                    if ch.type == "expression_statement":
                        for inner in ch.children:
                            if inner.type == "assignment_expression" and inner.children:
                                lhs = inner.children[0]
                                if lhs.type == "member_expression":
                                    lhs_text = lhs.text.decode()
                                    parts = lhs_text.split(".")
                                    if len(parts) == 2 and parts[0] == "this":
                                        rhs = inner.children[-1]
                                        if rhs.type == "new_expression":
                                            for gch in rhs.children:
                                                if gch.type in ("identifier", "type_identifier"):
                                                    cn = gch.text.decode()
                                                    if cn and cn[0].isupper():
                                                        record(lhs_text, cn)
                                                    break
                    else:
                        walk_method(ch)

            walk_method(body)

    return result


def _collect_js_name_bindings(
    fn_node,
    class_field_types: Optional[dict] = None,
    file_return_types: Optional[dict] = None,
) -> tuple:
    """Unified name→type binding extraction for TS/JS callables.

    Sources:
      1. Typed parameters:  (store: Store) → ("store", "Store")
      2. const x = new Type() → ("x", "Type")
      3. let x: Type [= ...] → ("x", "Type")
      4. const x = func() where func has return type → ("x", "ReturnType")
      5. Class field types (this.x → T): merged from class_field_types
    """
    params_node = _child_of_type(fn_node, "formal_parameters")
    if fn_node.type == "arrow_function":
        body_node = _js_arrow_body(fn_node)
    else:
        body_node = _child_of_type(fn_node, "statement_block")

    bindings: dict[str, str] = {}

    # 5. Class field types (this.x → T) — lower priority
    if class_field_types:
        bindings.update(class_field_types)

    # 1. Typed parameters
    if params_node:
        for c in params_node.children:
            if c.type not in ("required_parameter", "optional_parameter"):
                continue
            name_node = next((x for x in c.children if x.type == "identifier"), None)
            type_ann = next((x for x in c.children if x.type == "type_annotation"), None)
            if name_node and type_ann:
                tn = _normalize_type_name(type_ann.text.decode())
                if tn:
                    bindings[name_node.text.decode()] = tn

    # 2+3+4. Body scan
    if body_node and body_node.type == "statement_block":
        frt = file_return_types or {}

        def walk(node) -> None:
            for c in node.children:
                if c.type in _JS_FUNC_SKIP:
                    continue
                if c.type in ("lexical_declaration", "variable_declaration"):
                    for vd in c.children:
                        if vd.type != "variable_declarator":
                            continue
                        name_node = vd.children[0] if vd.children else None
                        if name_node is None or name_node.type != "identifier":
                            continue
                        var_name = name_node.text.decode()
                        # 3. Annotated: let x: Type
                        type_ann = next((x for x in vd.children if x.type == "type_annotation"), None)
                        if type_ann:
                            tn = _normalize_type_name(type_ann.text.decode())
                            if tn:
                                bindings[var_name] = tn
                            continue
                        # 2. Constructor: const x = new Type()
                        new_expr = next((x for x in vd.children if x.type == "new_expression"), None)
                        if new_expr:
                            past_new = False
                            for ch in new_expr.children:
                                if ch.type == "new":
                                    past_new = True
                                    continue
                                if past_new and ch.type in ("identifier", "type_identifier"):
                                    cn = ch.text.decode()
                                    if cn and cn[0].isupper():
                                        bindings[var_name] = cn
                                    break
                            continue
                        # 4. Return-typed call: const x = func()
                        call_expr = next((x for x in vd.children if x.type == "call_expression"), None)
                        if call_expr and call_expr.children:
                            fn_ch = call_expr.children[0]
                            if fn_ch.type == "identifier":
                                cn = fn_ch.text.decode()
                                if cn and cn[0].islower() and cn in frt:
                                    bindings[var_name] = frt[cn]
                else:
                    walk(c)

        walk(body_node)

    return tuple(bindings.items())


# ── JS/TS helpers ──────────────────────────────────────────────────────────────

def _js_is_async(fn_node) -> bool:
    return any(c.type == "async" for c in fn_node.children)


def _js_arrow_body(fn_node):
    """Body node of an arrow_function — the child that follows '=>'."""
    past_arrow = False
    for c in fn_node.children:
        if past_arrow:
            return c
        if c.type == "=>":
            past_arrow = True
    return None


def _js_decl_name(node) -> str:
    """Name identifier from a JS/TS declaration node (handles type_identifier for TS)."""
    for c in node.children:
        if c.type in ("identifier", "type_identifier"):
            return c.text.decode()
    return ""


def _js_method_name(method_node) -> str:
    """Property name from a method_definition node.

    Getter/setter accessors get a 'get '/'set ' prefix so that `get x` and
    `set x` are distinct determinations (SDT §1.2.6: different layout content =
    different structure). Without this, get/set share one qualified_name and
    their differing fingerprints cause spurious drift on every reconcile.
    """
    kind: str | None = None
    for c in method_node.children:
        if c.type == "get":
            kind = "get"
        elif c.type == "set":
            kind = "set"
        elif c.type in ("property_identifier", "private_property_identifier"):
            name = c.text.decode()
            return f"{kind} {name}" if kind else name
    return ""


# ── JS/TS callable extractor ───────────────────────────────────────────────────

def _extract_js_callable(
    fn_node, name: str, qualified_name: str, file_path: str,
    class_field_types: Optional[dict] = None,
    file_return_types: Optional[dict] = None,
) -> Define:
    """Build a Define from any JS/TS callable node (function, method, arrow, etc.)."""
    params_node = _child_of_type(fn_node, "formal_parameters")

    if fn_node.type == "arrow_function":
        body_node = _js_arrow_body(fn_node)
        if params_node is None:
            # Single-param shorthand: x => expr  → 1 required param
            params = (1, 0, False, False, 0)
        else:
            params = _js_params_shape(params_node)
    else:
        body_node = _child_of_type(fn_node, "statement_block")
        params = _js_params_shape(params_node) if params_node else (0, 0, False, False, 0)

    is_async = _js_is_async(fn_node)
    name_bindings = _collect_js_name_bindings(fn_node, class_field_types, file_return_types)

    return Define(
        name=name,
        qualified_name=qualified_name,
        file_path=file_path,
        params_shape=params,
        returns_kind=_js_returns_kind(body_node),
        calls=_js_collect_calls(body_node),
        imports=(),  # JS/TS body imports not extracted (dynamic import() is rare and complex)
        side_effects=_js_side_effects(body_node, is_async),
        size_count=_js_count_statements(body_node),
        numeric_literals=_js_collect_numeric_literals(body_node),
        operators=_js_collect_operators(body_node),
        name_bindings=name_bindings,
    )


_CALLABLE_TYPES = ("arrow_function", "function_expression")


def _find_callable_deep(vd):
    """Return first arrow_function/function_expression reachable from vd.

    Pass 1: direct children (original logic — handles plain arrow assignments).
    Pass 2: bounded recursion into call_expression / arguments /
            parenthesized_expression / await_expression only.
            Does NOT recurse into object / array / template literals, so
            `export const CONFIG = { handler: () => {} }` is NOT extracted.
    """
    for c in vd.children:
        if c.type in _CALLABLE_TYPES:
            return c

    _DIVE = frozenset({"call_expression", "arguments",
                       "parenthesized_expression", "await_expression"})

    def _rec(n):
        for c in n.children:
            if c.type in _CALLABLE_TYPES:
                return c
            if c.type in _DIVE:
                r = _rec(c)
                if r:
                    return r
        return None

    return _rec(vd)


def _is_module_exports(node) -> bool:
    """True if node is the ``module.exports`` member_expression."""
    if node.type != "member_expression":
        return False
    nc = [c for c in node.children if c.is_named]
    return (
        len(nc) == 2
        and nc[0].type == "identifier"
        and nc[0].text.decode() == "module"
        and nc[1].text.decode() == "exports"
    )


def _extract_object_methods(
    obj_node,
    obj_name: Optional[str],
    class_prefix: Optional[str],
    file_path: str,
    file_return_types: Optional[dict],
    out: list,
) -> None:
    """Extract defines from an object literal node.

    obj_name=None yields bare method names (module.exports = { fn: … } pattern).
    obj_name set yields ``obj_name.method`` qualified names (const obj = { … }).
    """
    for prop in obj_node.children:
        if prop.type == "method_definition":
            mname = _js_method_name(prop)
            if not mname or _child_of_type(prop, "statement_block") is None:
                continue
            pq = f"{obj_name}.{mname}" if obj_name else mname
            full_qname = f"{class_prefix}.{pq}" if class_prefix else pq
            out.append(_extract_js_callable(prop, mname, full_qname, file_path,
                                             file_return_types=file_return_types))
        elif prop.type == "pair":
            key_node = next(
                (ch for ch in prop.children
                 if ch.type in ("property_identifier", "string", "identifier")),
                None,
            )
            val_node = next(
                (ch for ch in prop.children if ch.type in _CALLABLE_TYPES),
                None,
            )
            if key_node is None or val_node is None:
                continue
            pname = key_node.text.decode().strip("'\"")
            pq = f"{obj_name}.{pname}" if obj_name else pname
            full_pq = f"{class_prefix}.{pq}" if class_prefix else pq
            out.append(_extract_js_callable(val_node, pname, full_pq, file_path,
                                             file_return_types=file_return_types))


def _handle_cjs_assignment(
    assign_node,
    class_prefix: Optional[str],
    file_path: str,
    file_return_types: Optional[dict],
    out: list,
) -> None:
    """Handle CommonJS assignment expression patterns.

    (A) module.exports = { fn: … }          → bare method names
    (B) X.prototype.method = function() {}  → qualified_name = X.method
    (C) exports.foo = fn                    → bare name foo
        module.exports.foo = fn             → bare name foo
    """
    nc = [c for c in assign_node.children if c.is_named]
    if len(nc) < 2:
        return
    left, right = nc[0], nc[-1]

    if left.type != "member_expression":
        return
    left_nc = [c for c in left.children if c.is_named]
    if len(left_nc) < 2:
        return
    left_obj = left_nc[0]
    left_prop = left_nc[-1]
    prop_name = left_prop.text.decode()

    # Pattern A: module.exports = { … }
    if _is_module_exports(left) and right.type == "object":
        _extract_object_methods(right, None, class_prefix, file_path, file_return_types, out)
        return

    # Pattern B: X.prototype.method = function() { … }
    if left_obj.type == "member_expression" and right.type in _CALLABLE_TYPES:
        obj_nc = [c for c in left_obj.children if c.is_named]
        if (len(obj_nc) == 2
                and obj_nc[0].type == "identifier"
                and obj_nc[1].text.decode() == "prototype"):
            class_name = obj_nc[0].text.decode()
            qname_local = f"{class_name}.{prop_name}"
            full_qname = f"{class_prefix}.{qname_local}" if class_prefix else qname_local
            out.append(_extract_js_callable(right, prop_name, full_qname, file_path,
                                             file_return_types=file_return_types))
            return

    # Pattern C: exports.foo = fn  or  module.exports.foo = fn
    if right.type in _CALLABLE_TYPES:
        is_bare_exports = (left_obj.type == "identifier"
                           and left_obj.text.decode() == "exports")
        is_mod_exports = _is_module_exports(left_obj)
        if is_bare_exports or is_mod_exports:
            full_qname = f"{class_prefix}.{prop_name}" if class_prefix else prop_name
            out.append(_extract_js_callable(right, prop_name, full_qname, file_path,
                                             file_return_types=file_return_types))
            return

    # Pattern D: localVar.method = fn  (module-level object method, e.g. res.send = fn)
    if right.type in _CALLABLE_TYPES and left_obj.type == "identifier":
        obj_name = left_obj.text.decode()
        if obj_name not in ("module", "exports"):
            qname_local = f"{obj_name}.{prop_name}"
            full_qname = f"{class_prefix}.{qname_local}" if class_prefix else qname_local
            out.append(_extract_js_callable(right, prop_name, full_qname, file_path,
                                             file_return_types=file_return_types))


def _find_iife_fn_node(expr_stmt_node):
    """Return the function_expression/arrow_function node of an IIFE, or None.

    Handles:
      (function(){...})()          — basic, parens around function
      (() => {...})()              — arrow variant
      (function(){...}.call(this)) — lodash .call/.apply form (outer parens)
    UMD factory-as-argument is not handled (recorded as known residual).
    """
    named = [c for c in expr_stmt_node.children if c.is_named]
    if not named:
        return None
    inner = named[0]
    # Unwrap optional outer parenthesized_expression (lodash: ;(fn.call(this));)
    if inner.type == "parenthesized_expression":
        pnc = [c for c in inner.children if c.is_named]
        if not pnc:
            return None
        inner = pnc[0]
    if inner.type != "call_expression":
        return None
    call_nc = [c for c in inner.children if c.is_named]
    if not call_nc:
        return None
    callee = call_nc[0]
    # Case A: (function(){})() or (() => {})() — parens wrap the function
    if callee.type == "parenthesized_expression":
        fn_cands = [c for c in callee.children if c.is_named]
        if fn_cands and fn_cands[0].type in _CALLABLE_TYPES:
            return fn_cands[0]
    # Case B: function(){}() — function directly as callee (rare)
    if callee.type in _CALLABLE_TYPES:
        return callee
    # Case C: (function(){}.call(this)) — member_expression with .call/.apply
    if callee.type == "member_expression":
        me_nc = [c for c in callee.children if c.is_named]
        if (len(me_nc) >= 2
                and me_nc[0].type in _CALLABLE_TYPES
                and me_nc[-1].text.decode() in ("call", "apply")):
            return me_nc[0]
    return None


# ── JS/TS define collector ─────────────────────────────────────────────────────

def _collect_js_defines(
    node,
    file_path: str,
    class_prefix: Optional[str],
    is_class_body: bool,
    out: list,
    class_field_types: Optional[dict] = None,
    file_return_types: Optional[dict] = None,
    export_default: bool = False,
) -> None:
    for c in node.children:
        t = c.type

        if t in ("function_declaration", "generator_function_declaration"):
            name = _identifier_text(c)
            if name:
                if _child_of_type(c, "statement_block") is None:
                    continue  # overload signature / abstract declaration — no body
                qname = f"{class_prefix}.{name}" if class_prefix else name
                out.append(_extract_js_callable(c, name, qname, file_path,
                                                 file_return_types=file_return_types))

        elif t in ("lexical_declaration", "variable_declaration"):
            is_const = (t == "lexical_declaration" and bool(c.children)
                        and c.children[0].type == "const")
            for vd in c.children:
                if vd.type != "variable_declarator":
                    continue
                name_node = vd.children[0] if vd.children else None
                if name_node is None or name_node.type != "identifier":
                    continue
                name = name_node.text.decode()
                val = _find_callable_deep(vd)
                if val is not None:
                    qname = f"{class_prefix}.{name}" if class_prefix else name
                    out.append(_extract_js_callable(val, name, qname, file_path,
                                                     file_return_types=file_return_types))
                elif is_const:
                    # Object method shorthand: const obj = { fn: … } / const obj = { fn() {} }
                    obj_node = next((ch for ch in vd.children if ch.type == "object"), None)
                    if obj_node is not None:
                        _extract_object_methods(obj_node, name, class_prefix, file_path,
                                                 file_return_types, out)
                    else:
                        # const proto = module.exports = { … } (chained assignment)
                        asgn = next(
                            (ch for ch in vd.children if ch.type == "assignment_expression"), None,
                        )
                        if asgn is not None:
                            _handle_cjs_assignment(asgn, class_prefix, file_path,
                                                   file_return_types, out)
                else:
                    # var/let: handle chained assignment var proto = module.exports = { … }
                    asgn = next(
                        (ch for ch in vd.children if ch.type == "assignment_expression"), None,
                    )
                    if asgn is not None:
                        _handle_cjs_assignment(asgn, class_prefix, file_path,
                                               file_return_types, out)

        elif t in ("class_declaration", "abstract_class_declaration"):
            # Class is a namespace in 𝒢_D (§3.3), not an independent node.
            # Exception: export default class Foo — also record Foo as callable
            # so default-import patterns (import Bar from './comp'; Bar()) can resolve.
            # abstract_class_declaration: same body structure as class_declaration;
            # abstract methods have no statement_block so they're skipped by method_definition branch.
            cname = _js_decl_name(c)
            if not cname:
                continue
            prefix = f"{class_prefix}.{cname}" if class_prefix else cname
            if not is_class_body and export_default:
                out.append(Define(
                    name=cname, qualified_name=prefix, file_path=file_path,
                    params_shape=(0, 0, False, False, 0), returns_kind="unknown",
                    calls=(), imports=(), side_effects=frozenset(), size_count=0,
                ))
            body = _child_of_type(c, "class_body")
            if body:
                js_cft = _collect_js_class_field_types(body)
                _collect_js_defines(body, file_path, prefix, is_class_body=True, out=out,
                                     class_field_types=js_cft, file_return_types=file_return_types)

        elif t in ("public_field_definition", "field_definition") and is_class_body:
            prop = next((x for x in c.children
                         if x.type in ("property_identifier", "private_property_identifier")), None)
            if prop is None:
                continue
            field_name = prop.text.decode().lstrip('#')
            init = next((x for x in c.children if x.type in _CALLABLE_TYPES), None)
            if init is None:
                continue
            qname = f"{class_prefix}.{field_name}" if class_prefix else field_name
            out.append(_extract_js_callable(init, field_name, qname, file_path,
                                             class_field_types=class_field_types,
                                             file_return_types=file_return_types))

        elif t == "method_definition" and is_class_body:
            mname = _js_method_name(c)
            if mname:
                if _child_of_type(c, "statement_block") is None:
                    continue  # overload/abstract method signature — no body
                qname = f"{class_prefix}.{mname}" if class_prefix else mname
                out.append(_extract_js_callable(c, mname, qname, file_path,
                                                 class_field_types=class_field_types,
                                                 file_return_types=file_return_types))

        elif t == "export_statement":
            # Recurse: export function foo, export class Bar, export default fn, etc.
            has_default = any(ch.type == "default" for ch in c.children)
            _collect_js_defines(c, file_path, class_prefix, is_class_body, out,
                                 class_field_types=class_field_types,
                                 file_return_types=file_return_types,
                                 export_default=has_default)

        elif t in ("expression_statement", "internal_module"):
            # TS wraps namespace declarations as expression_statement → internal_module.
            # Ambient module declarations may also produce internal_module directly.
            # Extract ns_name from internal_module and descend into its statement_block.
            mod_node = c if t == "internal_module" else next(
                (ch for ch in c.children if ch.type == "internal_module"), None,
            )
            if mod_node is None:
                if t == "expression_statement":
                    # CommonJS assignment: module.exports / X.prototype / exports.foo
                    asgn = next(
                        (ch for ch in c.children if ch.type == "assignment_expression"), None,
                    )
                    if asgn is not None:
                        _handle_cjs_assignment(asgn, class_prefix, file_path,
                                               file_return_types, out)
                    # IIFE: (function(){...})() or (function(){...}.call(this))
                    iife_fn = _find_iife_fn_node(c)
                    if iife_fn is not None:
                        body = _child_of_type(iife_fn, "statement_block")
                        if body is not None:
                            _collect_js_defines(
                                body, file_path, class_prefix, is_class_body=False,
                                out=out, class_field_types=None,
                                file_return_types=file_return_types,
                            )
                continue
            ns_name = next(
                (ch.text.decode() for ch in mod_node.children if ch.type == "identifier"),
                None,
            )
            if not ns_name:
                continue
            ns_prefix = f"{class_prefix}.{ns_name}" if class_prefix else ns_name
            body = _child_of_type(mod_node, "statement_block")
            if body:
                _collect_js_defines(body, file_path, ns_prefix, is_class_body=False, out=out,
                                     class_field_types=None, file_return_types=file_return_types)

        elif t in ("if_statement", "try_statement", "for_statement",
                   "while_statement", "catch_clause", "finally_clause"):
            # Control-flow descent: function_declaration inside control blocks.
            # Only enter statement_block children (not nested function bodies).
            for sb in c.children:
                if sb.type == "statement_block":
                    _collect_js_defines(sb, file_path, class_prefix, is_class_body, out,
                                         class_field_types=class_field_types,
                                         file_return_types=file_return_types)

        # field_definition, static_block, etc. → skip


def _extract_javascript(file_path: str, src: bytes, parser) -> list[Define]:
    # 动态语言，调用图档（近似有损）；数据流档留 §4.2a / 单元 3b
    tree = parser.parse(src)
    out: list[Define] = []
    file_return_types = _collect_js_file_return_types(tree.root_node)
    _collect_js_defines(tree.root_node, file_path, class_prefix=None, is_class_body=False, out=out,
                        file_return_types=file_return_types)
    return out


def _extract_typescript(file_path: str, src: bytes, parser) -> list[Define]:
    # 静态类型语言，当前走调用图档（近似有损）；数据流档（严格 G3）留 §4.2a / 单元 3b
    tree = parser.parse(src)
    out: list[Define] = []
    file_return_types = _collect_js_file_return_types(tree.root_node)
    _collect_js_defines(tree.root_node, file_path, class_prefix=None, is_class_body=False, out=out,
                        file_return_types=file_return_types)
    return out


# ── extractor dispatch ────────────────────────────────────────────────────────

EXTRACTORS: dict = {
    "python": _extract_python,
    "javascript": _extract_javascript,
    "typescript": _extract_typescript,
    "tsx": _extract_typescript,   # same define logic; different parser (language_tsx)
}


# ── public API ────────────────────────────────────────────────────────────────

def extract_defines(file_path: str) -> list[Define]:
    """Extract Define objects from file_path. Language auto-detected via extension."""
    lang = detect_language(file_path)
    extractor = EXTRACTORS.get(lang)
    if extractor is None:
        raise NotImplementedError(f"extractor for {lang} not implemented (interface reserved)")
    parser = get_parser(lang)
    src = Path(file_path).read_bytes()
    return extractor(file_path, src, parser)


def _fine_call(c: str) -> str:
    """Strip self/cls/this prefix from a dotted call string, keep rest.

    self.parser.foo → parser.foo
    self.foo        → foo
    cls.method      → method
    this.svc.bar    → svc.bar
    validator.foo   → validator.foo  (non-keyword receiver: unchanged)
    foo             → foo
    """
    parts = c.split(".")
    if parts[0] in ("self", "cls", "this") and len(parts) > 1:
        parts = parts[1:]
    return ".".join(parts)


def compute_fingerprint(define: Define) -> tuple[str, str]:
    """Paired (coarse, fine) SHA-256 fingerprints of seven structural features (§4.2).

    Both hashes use features 1,2,4,5,6 unchanged.  Feature 3 (calls) differs:

    coarse — last dot-segment only, sorted dedup (legacy; backward compat).
              Stable across receiver renames; used for define_loop baseline.
              Does NOT include operators (feature 7) — protecting 59% historical chains.
    fine   — strip self/cls/this prefix, keep remaining receiver + method (発見3);
              PLUS operators (feature 7, 発見4): sorted multiset of operator type names.
              Distinguishes handler_a(self.parser.foo) from handler_b(self.validator.foo),
              AND distinguishes a+b from a-b, a>b from a<b, a and b from a or b, etc.
              Used for find_duplicates equivalence class and change detection.

    coarse is stored as node_fingerprint (backward compat); fine as fine_fingerprint.
    Change detection fires when EITHER coarse OR fine changes.
    Loop detection requires BOTH coarse AND fine to match (prevents false-equiv loops).
    """
    # Feature 4
    side_feat = tuple(sorted(define.side_effects))

    # Feature 5
    n = define.size_count
    if n <= 3:
        bucket = "xs"
    elif n <= 10:
        bucket = "s"
    elif n <= 30:
        bucket = "m"
    elif n <= 100:
        bucket = "l"
    else:
        bucket = "xl"

    shared_tail = (define.params_shape, define.returns_kind, side_feat, bucket,
                   define.numeric_literals)

    # coarse — Feature 3: last segment, sorted dedup (legacy; backward compat)
    coarse_calls = tuple(sorted({c.split(".")[-1] for c in define.calls}))
    coarse_payload = repr((shared_tail[0], shared_tail[1], coarse_calls) + shared_tail[2:])
    coarse = hashlib.sha256(coarse_payload.encode()).hexdigest()[:16]

    # fine — Feature 3: strip self/cls/this, keep receiver, sorted dedup
    #      + Feature 7: operator multiset (only in fine, not in coarse)
    fine_calls = tuple(sorted({_fine_call(c) for c in define.calls}))
    fine_payload = repr(
        (shared_tail[0], shared_tail[1], fine_calls) + shared_tail[2:] + (define.operators,)
    )
    fine = hashlib.sha256(fine_payload.encode()).hexdigest()[:16]

    return coarse, fine


# ── JS/TS module import extraction ────────────────────────────────────────────

def _js_string_content(string_node) -> str:
    """Unquoted content of a JS/TS string literal node."""
    for c in string_node.children:
        if c.type == "string_fragment":
            return c.text.decode()
    raw = string_node.text.decode()
    return raw[1:-1] if len(raw) >= 2 else raw


# ── tsconfig/jsconfig alias resolution ───────────────────────────────────────

# Cache: root_dir → {alias_prefix: target_base_dir}
# e.g. "@/*": ["./*"] with baseUrl "." → {"@/": ""}
_ALIAS_MAP_CACHE: dict[str, dict[str, str]] = {}

# Strips only // line comments (not block comments — block-comment regex mismatches
# glob patterns like "**/*.ts" inside JSON strings).
_LINE_COMMENTS = re.compile(r'//[^\n]*')


def _parse_tsconfig_json(raw: str) -> dict:
    """Parse a tsconfig.json/jsconfig.json that may contain JSON5 // comments.

    Strategy: try plain json.loads first (covers most real-world tsconfigs).
    Fall back to stripping // line comments only — NOT block comments, because
    the /*...*/ regex would incorrectly eat glob patterns like "**/*.ts".
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(_LINE_COMMENTS.sub("", raw))
    except json.JSONDecodeError:
        return {}


def _load_alias_map(root: str) -> dict[str, str]:
    """Read tsconfig.json/jsconfig.json paths → {alias_prefix: resolved_base}.

    Returns {} when no config exists or no paths are configured.
    Result is cached by root so each project pays the I/O cost only once.
    """
    if root in _ALIAS_MAP_CACHE:
        return _ALIAS_MAP_CACHE[root]

    result: dict[str, str] = {}
    for config_name in ("tsconfig.json", "jsconfig.json"):
        config_path = os.path.join(root, config_name)
        if not os.path.exists(config_path):
            continue
        try:
            raw = Path(config_path).read_text(encoding="utf-8", errors="ignore")
            cfg = _parse_tsconfig_json(raw)
        except Exception:
            continue
        if not cfg:
            continue
        opts = cfg.get("compilerOptions") or {}
        paths = opts.get("paths") or {}
        base_url = (opts.get("baseUrl") or ".").rstrip("/")
        if base_url == ".":
            base_url = ""

        for pattern, targets in paths.items():
            if not targets:
                continue
            if pattern.endswith("/*"):
                prefix = pattern[:-1]            # "@/*" → "@/"
                target = targets[0]
                if target.endswith("/*"):
                    target = target[:-2]         # "./*" → "."
                target = target.lstrip("./") or ""   # "." → ""
                if base_url and target:
                    target = f"{base_url}/{target}"
                elif base_url:
                    target = base_url
                result[prefix] = target          # "@/" → ""
        break  # tsconfig.json takes precedence; don't also read jsconfig.json

    _ALIAS_MAP_CACHE[root] = result
    return result


def _js_resolve_import_path(raw_path: str, from_file: str, root: str) -> Optional[str]:
    """Resolve a JS/TS import path to a project-relative module name.

    Handles:
      ./foo, ../bar     — relative paths (original behaviour)
      @/lib/foo, ~/x    — alias paths read from tsconfig/jsconfig compilerOptions.paths
    Returns None for unresolvable external package imports.
    """
    # Strip known extensions before any path math.
    path = raw_path
    for ext in (".ts", ".tsx", ".js", ".jsx", ".mjs"):
        if path.endswith(ext):
            path = path[:-len(ext)]
            break

    if path.startswith("./") or path.startswith("../"):
        abs_path = os.path.normpath(os.path.join(os.path.dirname(from_file), path))
        if not root:
            return os.path.basename(abs_path)
        try:
            rel = os.path.relpath(abs_path, root)
        except ValueError:
            return None
        return rel.replace(os.sep, "/")

    # Alias paths — requires a project root to look up tsconfig.
    if root:
        alias_map = _load_alias_map(root)
        for prefix, base_dir in alias_map.items():
            if path.startswith(prefix):
                rest = path[len(prefix):]
                resolved = f"{base_dir}/{rest}" if base_dir else rest
                return resolved.replace(os.sep, "/")

    return None


def _js_extract_module_imports(
    file_path: str, src: bytes, parser, root: str = ""
) -> dict[str, str]:
    """Module-level import map for JS/TS (ES6 imports + CommonJS require).

    import { decode } from './auth'      → {"decode": "src/auth.decode"}
    import Auth from './auth'             → {"Auth": "src/auth"}
    import * as auth from './auth'        → {"auth": "src/auth"}
    const x = require('./util')           → {"x": "src/util"}
    import { cn } from '@/lib/utils'      → {"cn": "lib/utils.cn"}  (alias resolved)

    Relative imports (./…, ../…) and configured path aliases (@/…, etc.) are
    resolved; external package imports are skipped.
    root is used for canonical path derivation and alias-map lookup.
    """
    tree = parser.parse(src)
    result: dict[str, str] = {}

    for node in tree.root_node.children:
        if node.type == "import_statement":
            src_node = next((x for x in node.children if x.type == "string"), None)
            if src_node is None:
                continue
            mod = _js_resolve_import_path(_js_string_content(src_node), file_path, root)
            if mod is None:
                continue
            clause = next((x for x in node.children if x.type == "import_clause"), None)
            if clause is None:
                continue
            for ic in clause.children:
                if ic.type == "identifier":
                    # default import: import Auth from './auth'
                    result[ic.text.decode()] = mod
                elif ic.type == "namespace_import":
                    # * as auth
                    ident = next((x for x in ic.children if x.type == "identifier"), None)
                    if ident:
                        result[ident.text.decode()] = mod
                elif ic.type == "named_imports":
                    for spec in ic.children:
                        if spec.type != "import_specifier":
                            continue
                        names = [x for x in spec.children if x.type == "identifier"]
                        if len(names) == 1:
                            result[names[0].text.decode()] = f"{mod}.{names[0].text.decode()}"
                        elif len(names) >= 2:
                            # import { original as alias }  → last ident is alias
                            result[names[-1].text.decode()] = f"{mod}.{names[0].text.decode()}"

        elif node.type in ("lexical_declaration", "variable_declaration"):
            # const x = require('./path')
            for vd in node.children:
                if vd.type != "variable_declarator":
                    continue
                name_node = vd.children[0] if vd.children else None
                if name_node is None or name_node.type != "identifier":
                    continue
                call = next((x for x in vd.children if x.type == "call_expression"), None)
                if call is None:
                    continue
                fn = call.children[0] if call.children else None
                if fn is None or fn.type != "identifier" or fn.text != b"require":
                    continue
                args = _child_of_type(call, "arguments")
                if args is None:
                    continue
                arg = next((x for x in args.children if x.type == "string"), None)
                if arg is None:
                    continue
                mod = _js_resolve_import_path(_js_string_content(arg), file_path, root)
                if mod:
                    result[name_node.text.decode()] = mod

    return result


def extract_reexports(file_path: str, root: str = "") -> list[tuple[str, str, str]]:
    """Return [(exported_name, target_module, target_name), ...] for re-exports.

    Python: relative from-imports are re-exports.
      from .real import fn         → ("fn", "pkg.real", "fn")
      from .real import fn as g    → ("g",  "pkg.real", "fn")
      from .real import *          → ("*",  "pkg.real", "*")
    Excludes: from . import mod (submodule, no dotted_name in relative_import).

    JS/TS: named/aliased/star re-exports.
      export { decode } from './auth'         → ("decode", "src/auth", "decode")
      export { decode as dec } from './auth'  → ("dec",    "src/auth", "decode")
      export * from './x'                     → ("*",      "src/x",    "*")
      export * as ns from './x'               → ("ns.*",   "src/x",    "*")
    Excludes:
      export type { T } from './t'   — type-only, no runtime call
      export function foo(){}        — local export (no 'from'), not a re-export
    """
    lang = detect_language(file_path)
    if lang not in ("javascript", "typescript", "tsx", "python"):
        return []

    if lang == "python":
        parser = get_parser("python")
        src = Path(file_path).read_bytes()
        tree = parser.parse(src)
        out: list[tuple[str, str, str]] = []
        for node in tree.root_node.children:
            if node.type != "import_from_statement":
                continue
            rel_node = next((c for c in node.children if c.type == "relative_import"), None)
            if rel_node is None:
                continue  # absolute import, not a relative re-export
            # from . import mod → submodule (no dotted_name in relative_import) → skip
            if not any(c.type == "dotted_name" for c in rel_node.children):
                continue
            target_module = _resolve_relative_module(rel_node, file_path, root)
            if not target_module:
                continue
            past_kw = False
            for child in node.children:
                t = child.type
                if t == "import":
                    past_kw = True
                elif past_kw:
                    if t == "wildcard_import":
                        out.append(("*", target_module, "*"))
                        break
                    elif t == "dotted_name":
                        nm = child.text.decode()
                        out.append((nm, target_module, nm))
                    elif t == "aliased_import":
                        dn = _child_of_type(child, "dotted_name")
                        alias_node = child.children[-1]
                        if dn and alias_node.type == "identifier":
                            out.append((alias_node.text.decode(), target_module, dn.text.decode()))
        return out
    parser = get_parser(lang)
    src = Path(file_path).read_bytes()
    tree = parser.parse(src)
    out: list[tuple[str, str, str]] = []
    # own module name — used for default export records (target lives in this file)
    if root:
        _rel = file_path
        for _ext in (".ts", ".tsx", ".js", ".jsx", ".mjs"):
            if _rel.endswith(_ext):
                _rel = _rel[:-len(_ext)]
                break
        try:
            own_module: Optional[str] = os.path.relpath(_rel, root).replace(os.sep, "/")
        except ValueError:
            own_module = None
    else:
        own_module = None
    for node in tree.root_node.children:
        if node.type != "export_statement":
            continue
        if any(c.type == "type" for c in node.children):
            continue  # type-only, excluded
        # Default export: export default function Page() / class / identifier reference
        if any(c.type == "default" for c in node.children):
            if not any(c.type == "from" for c in node.children) and own_module:
                name = _js_default_export_name(node)
                if name:
                    out.append(("<default>", own_module, name))
            continue  # default handled; don't fall through to named/star logic
        if not any(c.type == "from" for c in node.children):
            continue  # local named export, not a re-export
        ns_node = next((c for c in node.children if c.type == "namespace_export"), None)
        if ns_node is not None:
            # export * as ns from './x' → ("ns.*", target_module, "*")
            # Reuses existing star mechanism: _follow_reexport resolves ns.foo via ns.* → x.*
            ns_id = next((c for c in ns_node.children if c.type == "identifier"), None)
            str_node = next((c for c in node.children if c.type == "string"), None)
            if ns_id is not None and str_node is not None:
                ns_name = ns_id.text.decode()
                raw_path = _js_string_content(str_node)
                target_module = _js_resolve_import_path(raw_path, file_path, root)
                if target_module:
                    out.append((f"{ns_name}.*", target_module, "*"))
            continue
        has_star = any(c.type == "*" for c in node.children)
        str_node = next((c for c in node.children if c.type == "string"), None)
        if str_node is None:
            continue
        raw_path = _js_string_content(str_node)
        target_module = _js_resolve_import_path(raw_path, file_path, root)
        if target_module is None:
            continue
        if has_star:
            out.append(("*", target_module, "*"))
            continue
        clause = next((c for c in node.children if c.type == "export_clause"), None)
        if clause is None:
            continue
        for spec in clause.children:
            if spec.type != "export_specifier":
                continue
            idents = [x for x in spec.children if x.type == "identifier"]
            if len(idents) == 1:
                nm = idents[0].text.decode()
                out.append((nm, target_module, nm))
            elif len(idents) >= 2:
                orig = idents[0].text.decode()
                alias = idents[-1].text.decode()
                out.append((alias, target_module, orig))
    return out


def _js_default_export_name(export_node) -> "str | None":
    """Extract the real name from 'export default <decl-or-ref>', or None if anonymous.

    export default function Page(){}  → "Page"
    export default class Foo {}       → "Foo"
    export default Page               → "Page"  (identifier reference)
    export default function(){}       → None    (anonymous)
    export default () => {}           → None    (arrow, anonymous)
    export default {a: 1}             → None    (object literal)
    """
    for child in export_node.children:
        t = child.type
        if t in ("function_declaration", "class_declaration",
                 "generator_function_declaration"):
            # TS class names use type_identifier; function names use identifier
            ident = next(
                (c for c in child.children
                 if c.type in ("identifier", "type_identifier")),
                None,
            )
            if ident:
                return ident.text.decode()
        elif t in ("identifier", "type_identifier"):
            return child.text.decode()
    return None


def _resolve_relative_module(rel_node, file_path: str, root: str) -> "str | None":
    """Resolve a Python relative_import AST node to an absolute dotted module name.

    from .real import fn   (in pkg/svc.py)  →  "pkg.real"
    from . import mod      (in pkg/svc.py)  →  "pkg"
    from ..util import h   (in pkg/sub/x.py)→  "pkg.util"
    """
    if not root or not file_path.endswith(".py"):
        return None
    prefix = next((c for c in rel_node.children if c.type == "import_prefix"), None)
    dotted = next((c for c in rel_node.children if c.type == "dotted_name"), None)
    if prefix is None:
        return None
    dots = prefix.text.decode().count(".")
    rel = os.path.relpath(os.path.dirname(file_path), root)
    pkg_parts: list[str] = [] if rel in (".", "") else rel.split(os.sep)
    if dots - 1 > 0:
        if len(pkg_parts) < dots - 1:
            return None
        pkg_parts = pkg_parts[:len(pkg_parts) - (dots - 1)]
    base = ".".join(pkg_parts)
    tail = dotted.text.decode() if dotted is not None else ""
    if base and tail:
        return f"{base}.{tail}"
    return base or tail or None


def extract_module_imports(file_path: str, root: str = "") -> dict[str, str]:
    """Module-level (top-level) alias → target import map.

    Python:
      import json              → {"json": "json"}
      import os.path as p      → {"p": "os.path"}
      from auth import decode  → {"decode": "auth.decode"}

    JS/TS (relative imports only; root improves path resolution):
      import { decode } from './auth'  → {"decode": "src/auth.decode"}
      import * as u from '../utils'    → {"u": "utils"}
      const x = require('./util')      → {"x": "src/util"}

    Only top-level imports; body-local imports are in Define.imports (Python only).
    """
    lang = detect_language(file_path)
    parser = get_parser(lang)
    src = Path(file_path).read_bytes()

    if lang in ("javascript", "typescript", "tsx"):
        return _js_extract_module_imports(file_path, src, parser, root=root)

    if lang != "python":
        raise NotImplementedError(
            f"extract_module_imports for {lang} not wired (interface reserved)"
        )

    tree = parser.parse(src)
    result: dict[str, str] = {}

    for node in tree.root_node.children:
        if node.type == "import_statement":
            for child in node.children:
                if child.type == "dotted_name":
                    # import json  or  import os.path → bind first segment as alias
                    target = child.text.decode()
                    result[target.split(".")[0]] = target
                elif child.type == "aliased_import":
                    # import os.path as p
                    dotted = _child_of_type(child, "dotted_name")
                    alias_node = child.children[-1]
                    if dotted and alias_node.type == "identifier":
                        result[alias_node.text.decode()] = dotted.text.decode()

        elif node.type == "import_from_statement":
            # Scan once: track module (first dotted_name before 'import' keyword),
            # then collect imported names/aliases after the keyword.
            module: str | None = None
            past_kw = False
            for child in node.children:
                t = child.type
                if t == "dotted_name" and not past_kw:
                    module = child.text.decode()
                elif t == "relative_import" and not past_kw:
                    module = _resolve_relative_module(child, file_path, root)
                elif t == "import":
                    past_kw = True
                elif past_kw and module:
                    if t == "dotted_name":
                        name = child.text.decode()
                        result[name] = f"{module}.{name}"
                    elif t == "aliased_import":
                        dotted = _child_of_type(child, "dotted_name")
                        alias_node = child.children[-1]
                        if dotted and alias_node.type == "identifier":
                            result[alias_node.text.decode()] = (
                                f"{module}.{dotted.text.decode()}"
                            )

    return result
