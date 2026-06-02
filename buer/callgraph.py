"""BUER call-graph layer — §4.2a step 1: build_call_edges.

Builds caller→callee edges from static analysis of a source file.
This is entity-theoretic engineering approximation: edges come from
structural/syntactic analysis, not from GD/SDT theory. Known limitations
are documented in Design v2.0 §3.7 (dynamic dispatch, reflection, etc.).

Public API
----------
build_symbol_index(root) -> SymbolIndex
compute_call_edges(file_path, root, idx) -> list[tuple]   # pure, no DB
build_call_edges(project_id, file_path, root, idx, conn)  # writes to store
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from buer import boundary, parse
from buer.store import Store


# ── module name ───────────────────────────────────────────────────────────────

def module_name_of(file_path: str, root: str) -> str:
    """Relative path of file under root, extension stripped.

    Python files use dot-separators (mirrors import syntax):
      /proj/pkg/auth.py  →  "pkg.auth"

    JS/TS files use forward-slash separators (mirrors import paths):
      /proj/src/auth.ts  →  "src/auth"
      /proj/src/auth.js  →  "src/auth"
    """
    rel = os.path.relpath(file_path, root)
    for ext in (".py", ".ts", ".tsx", ".js", ".jsx", ".mjs"):
        if rel.endswith(ext):
            rel = rel[:-len(ext)]
            break
    if file_path.endswith(".py"):
        mod = rel.replace(os.sep, ".")
        if mod.endswith(".__init__"):
            mod = mod[:-len(".__init__")]
        elif mod == "__init__":
            mod = ""
        return mod
    return rel.replace(os.sep, "/")


# ── symbol index ──────────────────────────────────────────────────────────────

@dataclass
class SymbolIndex:
    qualified: set = field(default_factory=set)   # "module.qualified_name"
    simple: dict = field(default_factory=dict)     # name → [fqn, ...]
    modules: set = field(default_factory=set)      # all project module names
    loc: dict = field(default_factory=dict)        # fqn → (file_path, qualified_name_without_module)
    reexport: dict = field(default_factory=dict)   # "barrel_mod.exported" → "target_mod.target_name"
    return_types: dict = field(default_factory=dict)  # fqn → return_type_name (L3 inference)


_SRC_PATTERNS: tuple[str, ...] = ("*.py", "*.ts", "*.tsx", "*.js", "*.jsx")


def build_symbol_index(root: str, exclude_tests: bool = True) -> SymbolIndex:
    """Walk all Python and JS/TS source files under root and build a SymbolIndex.

    exclude_tests=True (default) skips test/spec files so test fixture names
    (createChain, makeRequest, …) don't pollute the simple-name index.
    """
    idx = SymbolIndex()
    root_real = os.path.realpath(root)
    for pattern in _SRC_PATTERNS:
        for src_file in Path(root).rglob(pattern):
            if not boundary.should_ingest(str(src_file.resolve()), root_real):
                continue
            if exclude_tests and parse.is_test_file(str(src_file)):
                continue
            mod = module_name_of(str(src_file), root)
            idx.modules.add(mod)
            try:
                defines = parse.extract_defines(str(src_file))
            except Exception:
                continue
            for d in defines:
                fqn = _lang_fqn(str(src_file), mod, d.qualified_name)
                idx.qualified.add(fqn)
                idx.simple.setdefault(d.name, []).append(fqn)
                idx.loc[fqn] = (str(src_file), d.qualified_name)
                if d.return_type:
                    idx.return_types[fqn] = d.return_type
    return idx


def build_symbol_index_from_store(store: Store, project_id: int, root: str) -> SymbolIndex:
    """Project SymbolIndex from the 𝒢_D frontier (determinations table).

    Reads alive defines from node_equivalence_classes (deleted defines have their
    member_node removed via delete_equivalence_member) and projects into SymbolIndex.
    ~3ms for 3000-define projects vs ~78s rglob scan.
    """
    idx = SymbolIndex()
    alive: set[str] = {
        r["member_node"]
        for r in store.con.execute(
            "SELECT member_node FROM node_equivalence_classes WHERE project_id = ?",
            (project_id,),
        ).fetchall()
    }
    if not alive:
        return idx
    rows = store.con.execute(
        """SELECT DISTINCT file_path, define_name
           FROM determinations
           WHERE project_id = ? AND define_name IS NOT NULL""",
        (project_id,),
    ).fetchall()
    for r in rows:
        fp, qual = r["file_path"], r["define_name"]
        mod = module_name_of(fp, root)
        bare_fqn = f"{mod}.{qual}"
        if bare_fqn not in alive:
            continue
        lang = _lang_of_file(fp)
        fqn = _with_lang(lang, bare_fqn)
        idx.modules.add(mod)
        idx.qualified.add(fqn)
        idx.simple.setdefault(qual.split(".")[-1], []).append(fqn)
        idx.loc[fqn] = (fp, qual)
    # Build module→lang mapping from idx.loc so reexport entries are lang-aware.
    # (reexport_edges stores bare module names; language is inferred from files
    # already indexed above.) Pure barrel modules (no own defines) may not appear
    # in idx.loc, so propagate lang through reexport chains iteratively.
    mod_lang: dict[str, str] = {}
    for fqn_key in idx.loc:
        if "::" in fqn_key:
            _lang, _rest = fqn_key.split("::", 1)
            _mod = _rest.rsplit(".", 1)[0] if "." in _rest else _rest
            mod_lang.setdefault(_mod, _lang)
    reexport_rows = store.all_reexports(project_id)
    # Propagate lang from target modules to barrel modules (handles pure-barrel
    # __init__.py / index.ts that have no own defines).
    changed = True
    while changed:
        changed = False
        for r in reexport_rows:
            bmod, tmod = r['barrel_module'], r['target_module']
            if bmod not in mod_lang and tmod in mod_lang:
                mod_lang[bmod] = mod_lang[tmod]
                changed = True
            elif tmod not in mod_lang and bmod in mod_lang:
                mod_lang[tmod] = mod_lang[bmod]
                changed = True
    for r in reexport_rows:
        bmod = r['barrel_module']
        tmod = r['target_module']
        exported = r['exported_name']
        target = r['target_name']
        lang = mod_lang.get(bmod) or mod_lang.get(tmod) or "js_ts"
        if exported == '<default>':
            key = _with_lang(lang, bmod)
        else:
            key = _with_lang(lang, f"{bmod}.{exported}")
        idx.reexport[key] = _with_lang(lang, f"{tmod}.{target}")
    # return_types: latest return_type per (file_path, define_name) for L3 inference
    rt_rows = store.con.execute(
        """SELECT d.file_path, d.define_name, d.return_type
           FROM determinations d
           WHERE d.project_id = ?
             AND d.define_name IS NOT NULL
             AND d.return_type != ''
             AND d.seq = (
               SELECT MAX(seq) FROM determinations
               WHERE project_id = d.project_id
                 AND file_path = d.file_path
                 AND define_name = d.define_name
             )""",
        (project_id,),
    ).fetchall()
    for r in rt_rows:
        fp, qual, rt = r["file_path"], r["define_name"], r["return_type"]
        fqn = _lang_fqn(fp, module_name_of(fp, root), qual)
        if fqn in idx.qualified:
            idx.return_types[fqn] = rt
    return idx


# ── language helpers ──────────────────────────────────────────────────────────

def _lang_of_file(file_path: str) -> str:
    """Coarse language bucket from file extension."""
    if file_path.endswith(".py"):
        return "py"
    if file_path.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs")):
        return "js_ts"
    return "other"


def _with_lang(lang: str, bare: str) -> str:
    """Prefix a bare fqn/module key with its language bucket: 'py::mod.helper'.

    Single construction point for the lang:: prefix so the format is changed
    in exactly one place. Empty lang → bare unchanged (no-lang fallback).
    """
    return f"{lang}::{bare}" if lang else bare


def _lang_fqn(file_path: str, mod: str, qual: str) -> str:
    """Construct a language-prefixed FQN: 'lang::mod.qual'."""
    return _with_lang(_lang_of_file(file_path), f"{mod}.{qual}")


def _candidate_lang(fqn: str, idx: SymbolIndex) -> Optional[str]:
    """Return language bucket of fqn.

    Fast path: extract from '::' prefix ('py::mod.fn' → 'py').
    Fallback: look up idx.loc for backward compat with manually-built test indexes.
    """
    if "::" in fqn:
        return fqn.split("::", 1)[0]
    loc = idx.loc.get(fqn)
    return _lang_of_file(loc[0]) if loc else None


# ── re-export following ───────────────────────────────────────────────────────

def _follow_reexport(target: str, idx: SymbolIndex) -> str:
    """Follow re-export redirects until a real define or dead end. Chain-safe.

    Handles named/aliased (exact key in reexport) and wildcard star (B.*→X.*):
    if B.foo has no exact key, check B.* star record → try X.foo.
    """
    seen: set = set()
    cur = target
    while cur not in idx.qualified and cur not in seen:
        seen.add(cur)
        if cur in idx.reexport:
            cur = idx.reexport[cur]
            continue
        # star wildcard fallback: "B.foo" → check "B.*" → "X.*" → try "X.foo"
        if "." in cur:
            base, _, name = cur.rpartition(".")
            star_key = f"{base}.*"
            if star_key in idx.reexport:
                star_target = idx.reexport[star_key]  # "X.*"
                if star_target.endswith(".*"):
                    x_mod = star_target[:-2]           # "X"
                    cur = f"{x_mod}.{name}"            # "X.foo"
                    continue
        break
    return cur


# ── callee resolution ─────────────────────────────────────────────────────────

def resolve_callee(
    raw_call: str,
    import_map: dict[str, str],
    idx: SymbolIndex,
    caller_module: str,
    caller_class: Optional[str],
    caller_lang: Optional[str] = None,
    receiver_types: Optional[dict] = None,
    self_attr_types: Optional[dict] = None,
    name_bindings: Optional[dict] = None,
) -> Optional[str]:
    """Resolve a raw callee string to a project-qualified name, or None.

    Returns None for external libraries, dynamic dispatch, or ambiguous names
    (§3.7 honest approximation — unresolved edges are silently dropped).

    caller_lang: language bucket of the caller file (_lang_of_file result).
    When provided, guessed matches (import_map, simple-name) are rejected if
    the candidate belongs to a different language — Python cannot call TS and
    vice versa, so such matches are 100% false edges.

    name_bindings: unified {receiver_name: type_name} dict (replaces separate
    receiver_types/self_attr_types lookup).  receiver_name may be a local var
    ("store"), a self/this attribute path ("self.repo", "this.cache"), or any
    other receiver expression whose type is statically known.

    receiver_types, self_attr_types: kept for backward compatibility but are
    no longer used directly by this function; callers should pass name_bindings.
    """
    receiver_types = receiver_types or {}
    self_attr_types = self_attr_types or {}
    name_bindings = name_bindings or {}

    if "." not in raw_call:
        # a. Same-module → same file → same language; no lang check needed.
        candidate = _with_lang(caller_lang, f"{caller_module}.{raw_call}")
        if candidate in idx.qualified:
            return candidate
        # b. import_map: authoritative source, but still reject cross-language.
        if raw_call in import_map:
            bare_target = import_map[raw_call]
            target = _with_lang(caller_lang, bare_target)
            if target not in idx.qualified:
                target = _follow_reexport(target, idx)
                if target not in idx.qualified:
                    return None
            if caller_lang and _candidate_lang(target, idx) != caller_lang:
                return None
            return target
        # c. Unique simple-name match: guessed, highest false-edge risk.
        matches = idx.simple.get(raw_call, [])
        if len(matches) == 1:
            if caller_lang and _candidate_lang(matches[0], idx) != caller_lang:
                return None
            return matches[0]
        return None
    else:
        head, _, rest = raw_call.partition(".")
        last = raw_call.rsplit(".", 1)[-1]
        # a. self/cls/this → same-class method; same module → same language.
        #    Guard: only direct self.method() / this.method() calls (no dot in rest).
        #    self.attr.method() / this.attr.method() fall through to binding step.
        if head in ("self", "cls", "this") and caller_class and "." not in rest:
            candidate = _with_lang(caller_lang, f"{caller_module}.{caller_class}.{last}")
            return candidate if candidate in idx.qualified else None
        # binding step: unified type-based resolution (replaces L1/L2; handles all
        # receiver_name → type_name bindings regardless of source or language).
        # receiver = all of raw_call except final method segment.
        receiver = raw_call.rsplit(".", 1)[0]
        method = raw_call.rsplit(".", 1)[-1]
        if receiver in name_bindings:
            type_name = name_bindings[receiver]
            type_head, _, type_rest = type_name.partition(".")
            if type_head in import_map:
                base = import_map[type_head]
                bare_full = f"{base}.{type_rest}.{method}" if type_rest else f"{base}.{method}"
                full = _with_lang(caller_lang, bare_full)
                if full in idx.qualified and (not caller_lang or _candidate_lang(full, idx) == caller_lang):
                    return full
            bare_cand = f"{caller_module}.{type_name}.{method}"
            cand = _with_lang(caller_lang, bare_cand)
            if cand in idx.qualified and (not caller_lang or _candidate_lang(cand, idx) == caller_lang):
                return cand
            # type known but no idx hit → don't guess; fall through to b-step
        # b. head via import_map.
        if head in import_map:
            bare_full = f"{import_map[head]}.{rest}"
            full = _with_lang(caller_lang, bare_full)
            if full not in idx.qualified:
                full = _follow_reexport(full, idx)
                if full not in idx.qualified:
                    return None
            if caller_lang and _candidate_lang(full, idx) != caller_lang:
                return None
            return full
        # c. Last resort: same-module dotted-name exact match.
        #    Enables object method shorthand: const o={m(){}}; o.m() → module.o.m.
        #    Only activates when: not self/this, not in name_bindings, not in import_map.
        candidate = _with_lang(caller_lang, f"{caller_module}.{raw_call}")
        if candidate in idx.qualified and (not caller_lang or _candidate_lang(candidate, idx) == caller_lang):
            return candidate
        return None


# ── edge computation (pure) ───────────────────────────────────────────────────

def compute_call_edges(
    file_path: str,
    root: str,
    idx: SymbolIndex,
) -> list[tuple[str, str, str]]:
    """Return deduplicated (caller, callee, kind) tuples. Pure function, no DB."""
    defines = parse.extract_defines(file_path)
    import_map = parse.extract_module_imports(file_path, root=root)
    mod = module_name_of(file_path, root)
    caller_lang = _lang_of_file(file_path)

    edges: set[tuple[str, str, str]] = set()

    for d in defines:
        caller = _lang_fqn(file_path, mod, d.qualified_name)
        caller_class = d.qualified_name.rsplit(".", 1)[0] if "." in d.qualified_name else None

        # Unified name_bindings: start from define's file-scope bindings
        nb = dict(d.name_bindings)

        # Cross-file return-type augmentation (file-scope is in name_bindings already;
        # x = imported_func() still needs idx.return_types lookup from L3)
        if idx.return_types:
            for var, func_name in d.call_assigns:
                if var not in nb:
                    fqn_cand: Optional[str] = None
                    same_mod_cand = _with_lang(caller_lang, f"{mod}.{func_name}")
                    if same_mod_cand in idx.qualified:
                        fqn_cand = same_mod_cand
                    elif func_name in import_map:
                        t = _with_lang(caller_lang, import_map[func_name])
                        if t in idx.qualified:
                            fqn_cand = t
                    else:
                        matches = idx.simple.get(func_name, [])
                        if len(matches) == 1:
                            fqn_cand = matches[0]
                    if fqn_cand and fqn_cand in idx.return_types:
                        nb[var] = idx.return_types[fqn_cand]

        # Call edges
        for raw in d.calls:
            callee = resolve_callee(raw, import_map, idx, mod, caller_class, caller_lang,
                                    name_bindings=nb)
            if callee is not None:
                edges.add((caller, callee, "call"))

        # Import edges (auxiliary — body-local imports only, per §3.3)
        for t in d.imports:
            top = t.split(".")[0]
            if t in idx.modules or top in idx.modules:
                edges.add((caller, t, "import"))

    return list(edges)


# ── DB write ──────────────────────────────────────────────────────────────────

def build_call_edges(
    project_id: int,
    file_path: str,
    root: str,
    idx: SymbolIndex,
    conn: Store,
) -> None:
    """Compute and persist call edges for file_path.

    Delete-then-insert: removes stale edges from deleted/renamed defines
    before upserting the freshly computed set (§4.2a step 1).
    conn: a buer.store.Store instance.
    """
    conn.delete_call_edges_for_file(project_id, file_path)
    for caller, callee, edge_kind in compute_call_edges(file_path, root, idx):
        conn.upsert_call_edge(project_id, caller, callee, edge_kind, source_file=file_path)
