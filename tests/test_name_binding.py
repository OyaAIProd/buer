"""Tests for unified name_bindings field and TS/JS type inference.

Covers Python+TS × 5 binding forms × c-step ambiguity verification,
plus self/this a-step, name_bindings extraction, and _normalize_type_name.
"""
from __future__ import annotations

import pytest

from buer.callgraph import SymbolIndex, resolve_callee
from buer.parse import extract_defines, _normalize_type_name
from buer.reconcile import reconcile
from buer.store import Store


# ── helpers ───────────────────────────────────────────────────────────────────

def _py(tmp_path, rel: str, body: str) -> str:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return str(p)


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
# 1. Python — typed parameter → resolves via import_map
#    c-step ambiguity: OtherClass also has process()
# ══════════════════════════════════════════════════════════════════════════════

def test_py_typed_param(tmp_path):
    """Python: def f(repo: Store) → name_bindings resolves repo.process() to Store.process."""
    _py(tmp_path, "db.py", "class Store:\n    def process(self): pass\n")
    _py(tmp_path, "other.py", "class OtherClass:\n    def process(self): pass\n")
    _py(tmp_path, "runner.py",
        "from db import Store\n"
        "def run(repo: Store):\n"
        "    repo.process()\n")

    store, pid = _reconcile(
        str(tmp_path / "db.py"),
        str(tmp_path / "other.py"),
        str(tmp_path / "runner.py"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "py::db.Store.process")
    assert any("runner" in c for c in callers), \
        f"Typed param must resolve to Store.process; callers={callers}"
    callers_other = store.callers_of(pid, "py::other.OtherClass.process")
    assert not any("runner" in c for c in callers_other), \
        f"Must not produce false edge to OtherClass.process; callers={callers_other}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# 2. Python — annotated variable → resolves via name_bindings
# ══════════════════════════════════════════════════════════════════════════════

def test_py_annotated_var(tmp_path):
    """Python: repo: Store = ... → name_bindings resolves repo.process()."""
    _py(tmp_path, "db.py", "class Store:\n    def process(self): pass\n")
    _py(tmp_path, "other.py", "class OtherClass:\n    def process(self): pass\n")
    _py(tmp_path, "runner.py",
        "from db import Store\n"
        "def run():\n"
        "    repo: Store = None\n"
        "    repo.process()\n")

    store, pid = _reconcile(
        str(tmp_path / "db.py"),
        str(tmp_path / "other.py"),
        str(tmp_path / "runner.py"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "py::db.Store.process")
    assert any("runner" in c for c in callers), \
        f"Annotated var must resolve to Store.process; callers={callers}"
    callers_other = store.callers_of(pid, "py::other.OtherClass.process")
    assert not any("runner" in c for c in callers_other), \
        f"Must not produce false edge to OtherClass.process; callers={callers_other}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# 3. Python — constructor assignment → name_bindings
# ══════════════════════════════════════════════════════════════════════════════

def test_py_constructor_assign(tmp_path):
    """Python: repo = Store() → name_bindings resolves repo.process() to Store.process."""
    _py(tmp_path, "db.py", "class Store:\n    def process(self): pass\n")
    _py(tmp_path, "other.py", "class OtherClass:\n    def process(self): pass\n")
    _py(tmp_path, "runner.py",
        "from db import Store\n"
        "def run():\n"
        "    repo = Store()\n"
        "    repo.process()\n")

    store, pid = _reconcile(
        str(tmp_path / "db.py"),
        str(tmp_path / "other.py"),
        str(tmp_path / "runner.py"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "py::db.Store.process")
    assert any("runner" in c for c in callers), \
        f"Constructor assign must resolve to Store.process; callers={callers}"
    callers_other = store.callers_of(pid, "py::other.OtherClass.process")
    assert not any("runner" in c for c in callers_other), \
        f"Must not produce false edge to OtherClass.process; callers={callers_other}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# 4. Python — self.attr class field → name_bindings (self.repo.process())
# ══════════════════════════════════════════════════════════════════════════════

def test_py_self_attr_field(tmp_path):
    """Python: self.repo = Store() in __init__ → name_bindings resolves self.repo.process()."""
    _py(tmp_path, "db.py", "class Store:\n    def process(self): pass\n")
    _py(tmp_path, "other.py", "class OtherClass:\n    def process(self): pass\n")
    _py(tmp_path, "app.py",
        "from db import Store\n"
        "class App:\n"
        "    def __init__(self):\n"
        "        self.repo = Store()\n"
        "    def run(self):\n"
        "        self.repo.process()\n")

    store, pid = _reconcile(
        str(tmp_path / "db.py"),
        str(tmp_path / "other.py"),
        str(tmp_path / "app.py"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "py::db.Store.process")
    assert any("run" in c for c in callers), \
        f"self.repo = Store() must resolve self.repo.process(); callers={callers}"
    callers_other = store.callers_of(pid, "py::other.OtherClass.process")
    assert not any("run" in c for c in callers_other), \
        f"Must not produce false edge to OtherClass.process; callers={callers_other}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# 5. Python — file-scope return type → name_bindings (x = get_store())
# ══════════════════════════════════════════════════════════════════════════════

def test_py_file_return_type(tmp_path):
    """Python: def get_store() -> Store; repo = get_store() → name_bindings resolves repo.process()."""
    _py(tmp_path, "db.py", "class Store:\n    def process(self): pass\n")
    _py(tmp_path, "other.py", "class OtherClass:\n    def process(self): pass\n")
    _py(tmp_path, "runner.py",
        "from db import Store\n"
        "def get_store() -> Store: pass\n"
        "def run():\n"
        "    repo = get_store()\n"
        "    repo.process()\n")

    store, pid = _reconcile(
        str(tmp_path / "db.py"),
        str(tmp_path / "other.py"),
        str(tmp_path / "runner.py"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "py::db.Store.process")
    assert any("runner" in c for c in callers), \
        f"File-scope return type must resolve repo.process(); callers={callers}"
    callers_other = store.callers_of(pid, "py::other.OtherClass.process")
    assert not any("runner" in c for c in callers_other), \
        f"Must not produce false edge to OtherClass.process; callers={callers_other}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# 6. Python — untyped receiver → no false edge (fallback test)
# ══════════════════════════════════════════════════════════════════════════════

def test_py_untyped_fallback(tmp_path):
    """Python: unannotated receiver must NOT produce a false edge."""
    _py(tmp_path, "db.py", "class Store:\n    def process(self): pass\n")
    _py(tmp_path, "other.py", "class OtherClass:\n    def process(self): pass\n")
    _py(tmp_path, "runner.py",
        "def run(repo):  # no type annotation\n"
        "    repo.process()\n")

    store, pid = _reconcile(
        str(tmp_path / "db.py"),
        str(tmp_path / "other.py"),
        str(tmp_path / "runner.py"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "py::db.Store.process")
    assert not any("runner" in c for c in callers), \
        f"Untyped receiver must not produce edge to Store.process; callers={callers}"
    callers_other = store.callers_of(pid, "py::other.OtherClass.process")
    assert not any("runner" in c for c in callers_other), \
        f"Untyped receiver must not produce edge to OtherClass.process; callers={callers_other}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# 7. TypeScript — typed parameter → name_bindings
# ══════════════════════════════════════════════════════════════════════════════

def test_ts_typed_param(tmp_path):
    """TS: function f(repo: Store) → name_bindings resolves repo.process() to Store.process."""
    _ts(tmp_path, "db.ts",
        "export class Store {\n    process(): void {}\n}\n")
    _ts(tmp_path, "other.ts",
        "export class OtherClass {\n    process(): void {}\n}\n")
    _ts(tmp_path, "runner.ts",
        "import { Store } from './db';\n"
        "function run(repo: Store): void {\n"
        "    repo.process();\n"
        "}\n")

    store, pid = _reconcile(
        str(tmp_path / "db.ts"),
        str(tmp_path / "other.ts"),
        str(tmp_path / "runner.ts"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "js_ts::db.Store.process")
    assert any("runner" in c for c in callers), \
        f"TS typed param must resolve to Store.process; callers={callers}"
    callers_other = store.callers_of(pid, "js_ts::other.OtherClass.process")
    assert not any("runner" in c for c in callers_other), \
        f"Must not produce false edge to OtherClass.process; callers={callers_other}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# 8. TypeScript — const x = new Type() → name_bindings constructor binding
# ══════════════════════════════════════════════════════════════════════════════

def test_ts_new_constructor(tmp_path):
    """TS: const repo = new Store() → name_bindings resolves repo.process() to Store.process."""
    _ts(tmp_path, "db.ts",
        "export class Store {\n    process(): void {}\n}\n")
    _ts(tmp_path, "other.ts",
        "export class OtherClass {\n    process(): void {}\n}\n")
    _ts(tmp_path, "runner.ts",
        "import { Store } from './db';\n"
        "function run(): void {\n"
        "    const repo = new Store();\n"
        "    repo.process();\n"
        "}\n")

    store, pid = _reconcile(
        str(tmp_path / "db.ts"),
        str(tmp_path / "other.ts"),
        str(tmp_path / "runner.ts"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "js_ts::db.Store.process")
    assert any("runner" in c for c in callers), \
        f"TS new Store() must resolve to Store.process; callers={callers}"
    callers_other = store.callers_of(pid, "js_ts::other.OtherClass.process")
    assert not any("runner" in c for c in callers_other), \
        f"Must not produce false edge to OtherClass.process; callers={callers_other}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# 9. TypeScript — let x: Type → name_bindings annotated var
# ══════════════════════════════════════════════════════════════════════════════

def test_ts_annotated_var(tmp_path):
    """TS: let repo: Store → name_bindings resolves repo.process() to Store.process."""
    _ts(tmp_path, "db.ts",
        "export class Store {\n    process(): void {}\n}\n")
    _ts(tmp_path, "other.ts",
        "export class OtherClass {\n    process(): void {}\n}\n")
    _ts(tmp_path, "runner.ts",
        "import { Store } from './db';\n"
        "function run(repo: Store): void {\n"
        "    let r: Store = repo;\n"
        "    r.process();\n"
        "}\n")

    store, pid = _reconcile(
        str(tmp_path / "db.ts"),
        str(tmp_path / "other.ts"),
        str(tmp_path / "runner.ts"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "js_ts::db.Store.process")
    assert any("runner" in c for c in callers), \
        f"TS annotated var must resolve to Store.process; callers={callers}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# 10. TypeScript — this.field class field binding → this.repo.process()
# ══════════════════════════════════════════════════════════════════════════════

def test_ts_this_field(tmp_path):
    """TS: this.repo = new Store() in constructor → resolves this.repo.process()."""
    _ts(tmp_path, "db.ts",
        "export class Store {\n    process(): void {}\n}\n")
    _ts(tmp_path, "other.ts",
        "export class OtherClass {\n    process(): void {}\n}\n")
    _ts(tmp_path, "app.ts",
        "import { Store } from './db';\n"
        "class App {\n"
        "    constructor() {\n"
        "        this.repo = new Store();\n"
        "    }\n"
        "    run(): void {\n"
        "        this.repo.process();\n"
        "    }\n"
        "}\n")

    store, pid = _reconcile(
        str(tmp_path / "db.ts"),
        str(tmp_path / "other.ts"),
        str(tmp_path / "app.ts"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "js_ts::db.Store.process")
    assert any("run" in c for c in callers), \
        f"TS this.repo = new Store() must resolve this.repo.process(); callers={callers}"
    callers_other = store.callers_of(pid, "js_ts::other.OtherClass.process")
    assert not any("run" in c for c in callers_other), \
        f"Must not produce false edge to OtherClass.process; callers={callers_other}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# 11. TypeScript — file-scope return type → const x = func(); x.process()
# ══════════════════════════════════════════════════════════════════════════════

def test_ts_file_return_type(tmp_path):
    """TS: function getStore(): Store; const repo = getStore() → resolves repo.process()."""
    _ts(tmp_path, "db.ts",
        "export class Store {\n    process(): void {}\n}\n")
    _ts(tmp_path, "other.ts",
        "export class OtherClass {\n    process(): void {}\n}\n")
    _ts(tmp_path, "runner.ts",
        "import { Store } from './db';\n"
        "function getStore(): Store { return new Store(); }\n"
        "function run(): void {\n"
        "    const repo = getStore();\n"
        "    repo.process();\n"
        "}\n")

    store, pid = _reconcile(
        str(tmp_path / "db.ts"),
        str(tmp_path / "other.ts"),
        str(tmp_path / "runner.ts"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "js_ts::db.Store.process")
    assert any("runner" in c for c in callers), \
        f"TS file-scope return type must resolve repo.process(); callers={callers}"
    callers_other = store.callers_of(pid, "js_ts::other.OtherClass.process")
    assert not any("runner" in c for c in callers_other), \
        f"Must not produce false edge to OtherClass.process; callers={callers_other}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# 12. TypeScript — untyped receiver → no false edge (fallback test)
# ══════════════════════════════════════════════════════════════════════════════

def test_ts_untyped_fallback(tmp_path):
    """TS: untyped receiver must NOT produce a false edge to any class."""
    _ts(tmp_path, "db.ts",
        "export class Store {\n    process(): void {}\n}\n")
    _ts(tmp_path, "other.ts",
        "export class OtherClass {\n    process(): void {}\n}\n")
    _ts(tmp_path, "runner.ts",
        "function run(repo: any): void {\n"
        "    repo.process();\n"
        "}\n")

    store, pid = _reconcile(
        str(tmp_path / "db.ts"),
        str(tmp_path / "other.ts"),
        str(tmp_path / "runner.ts"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "js_ts::db.Store.process")
    assert not any("runner" in c for c in callers), \
        f"Untyped TS receiver must not produce edge to Store.process; callers={callers}"
    callers_other = store.callers_of(pid, "js_ts::other.OtherClass.process")
    assert not any("runner" in c for c in callers_other), \
        f"Untyped TS receiver must not produce edge to OtherClass.process; callers={callers_other}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# 13. TypeScript — TS field type annotation: private store: Store
# ══════════════════════════════════════════════════════════════════════════════

def test_ts_field_type_annotation(tmp_path):
    """TS: class field with type annotation: private store: Store → resolves this.store.process()."""
    _ts(tmp_path, "db.ts",
        "export class Store {\n    process(): void {}\n}\n")
    _ts(tmp_path, "other.ts",
        "export class OtherClass {\n    process(): void {}\n}\n")
    _ts(tmp_path, "app.ts",
        "import { Store } from './db';\n"
        "class App {\n"
        "    private store: Store;\n"
        "    constructor(s: Store) {\n"
        "        this.store = s;\n"
        "    }\n"
        "    run(): void {\n"
        "        this.store.process();\n"
        "    }\n"
        "}\n")

    store, pid = _reconcile(
        str(tmp_path / "db.ts"),
        str(tmp_path / "other.ts"),
        str(tmp_path / "app.ts"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "js_ts::db.Store.process")
    assert any("run" in c for c in callers), \
        f"TS field annotation must resolve this.store.process(); callers={callers}"
    callers_other = store.callers_of(pid, "js_ts::other.OtherClass.process")
    assert not any("run" in c for c in callers_other), \
        f"Must not produce false edge to OtherClass.process; callers={callers_other}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# 14. Python — Promise<Store> / Optional[Store] unwrapping
# ══════════════════════════════════════════════════════════════════════════════

def test_py_optional_unwrap(tmp_path):
    """Python: Optional[Store] typed param → name_bindings unwraps to Store."""
    _py(tmp_path, "db.py", "class Store:\n    def process(self): pass\n")
    _py(tmp_path, "other.py", "class OtherClass:\n    def process(self): pass\n")
    _py(tmp_path, "runner.py",
        "from typing import Optional\n"
        "from db import Store\n"
        "def run(repo: Optional[Store]):\n"
        "    repo.process()\n")

    store, pid = _reconcile(
        str(tmp_path / "db.py"),
        str(tmp_path / "other.py"),
        str(tmp_path / "runner.py"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "py::db.Store.process")
    assert any("runner" in c for c in callers), \
        f"Optional[Store] must unwrap and resolve to Store.process; callers={callers}"
    callers_other = store.callers_of(pid, "py::other.OtherClass.process")
    assert not any("runner" in c for c in callers_other), \
        f"Must not produce false edge to OtherClass.process; callers={callers_other}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# 15. self.compute() and this.compute() → still resolved via a-step
# ══════════════════════════════════════════════════════════════════════════════

def test_self_method_still_a_step(tmp_path):
    """Python self.compute() and TS this.compute() must still resolve via a-step."""
    # Python
    _py(tmp_path, "pyapp.py",
        "class App:\n"
        "    def compute(self): pass\n"
        "    def run(self):\n"
        "        self.compute()\n")

    # TypeScript
    _ts(tmp_path, "tsapp.ts",
        "class TsApp {\n"
        "    compute(): void {}\n"
        "    run(): void {\n"
        "        this.compute();\n"
        "    }\n"
        "}\n")

    store, pid = _reconcile(
        str(tmp_path / "pyapp.py"),
        str(tmp_path / "tsapp.ts"),
        tmp_path=tmp_path,
    )
    py_callers = store.callers_of(pid, "py::pyapp.App.compute")
    assert any("run" in c for c in py_callers), \
        f"Python self.compute() must resolve via a-step; callers={py_callers}"

    ts_callers = store.callers_of(pid, "js_ts::tsapp.TsApp.compute")
    assert any("run" in c for c in ts_callers), \
        f"TS this.compute() must resolve via a-step; callers={ts_callers}"
    store.close()


# ══════════════════════════════════════════════════════════════════════════════
# 16. Direct name_bindings extraction from extract_defines
# ══════════════════════════════════════════════════════════════════════════════

def test_bindings_extraction(tmp_path):
    """extract_defines correctly populates name_bindings with all five sources."""
    # Python: typed param, annotated var, constructor assign, file-scope return, self.x
    py_src = (
        "from db import Store, Other\n"
        "def get_conn() -> Other: pass\n"
        "class App:\n"
        "    def __init__(self):\n"
        "        self.cache = Store()\n"
        "    def run(self, store: Store):\n"
        "        conn: Other = get_conn()\n"
        "        cache2 = Store()\n"
        "        svc = get_conn()\n"
        "        store.process()\n"
        "        conn.query()\n"
        "        cache2.flush()\n"
        "        svc.query()\n"
    )
    fp = _py(tmp_path, "pyapp.py", py_src)
    defines = extract_defines(fp)
    run_def = next(d for d in defines if d.name == "run")
    nb = dict(run_def.name_bindings)

    assert nb.get("store") == "Store", f"typed param missed; got {nb}"
    assert nb.get("conn") == "Other", f"annotated var missed; got {nb}"
    assert nb.get("cache2") == "Store", f"constructor assign missed; got {nb}"
    assert nb.get("svc") == "Other", f"file-scope return type missed; got {nb}"
    assert nb.get("self.cache") == "Store", f"self.x class field missed; got {nb}"

    # TypeScript: typed param + constructor assign + field annotation
    ts_src = (
        "import { Store } from './db';\n"
        "class App {\n"
        "    private cache: Store;\n"
        "    run(store: Store): void {\n"
        "        const repo = new Store();\n"
        "        repo.process();\n"
        "        store.process();\n"
        "    }\n"
        "}\n"
    )
    fp_ts = _ts(tmp_path, "tsapp.ts", ts_src)
    ts_defines = extract_defines(fp_ts)
    ts_run = next(d for d in ts_defines if d.name == "run")
    ts_nb = dict(ts_run.name_bindings)

    assert ts_nb.get("store") == "Store", f"TS typed param missed; got {ts_nb}"
    assert ts_nb.get("repo") == "Store", f"TS constructor assign missed; got {ts_nb}"
    assert ts_nb.get("this.cache") == "Store", f"TS class field annotation missed; got {ts_nb}"


# ══════════════════════════════════════════════════════════════════════════════
# 17. _normalize_type_name unit tests
# ══════════════════════════════════════════════════════════════════════════════

def test_normalize_type():
    """_normalize_type_name handles all expected forms correctly."""
    assert _normalize_type_name("Store") == "Store"
    assert _normalize_type_name("Optional[Store]") == "Store"
    assert _normalize_type_name("Promise<Store>") == "Store"
    assert _normalize_type_name("Store|null") == "Store"
    assert _normalize_type_name(": Store") == "Store"
    assert _normalize_type_name("Dict[str, Store]") == ""
