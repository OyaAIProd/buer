"""Tests for container type annotation fix: list[T]/Set[T]/Array<T> must not bind
variable to element type T (false-edge source).

Only transparent wrappers (Optional/Promise/Awaitable/Final/ClassVar/Coroutine)
should unwrap to the inner type. Containers and unknown generics must return ''.

Tests:
  1. Unit: 11 container types → '' (no binding)
  2. Unit: 6 transparent wrappers → inner type (no regression)
  3. Unit: simple bare type → unchanged
  4. E2E: list[Store]-typed param → no false call edge to Store methods
"""
from __future__ import annotations

import pytest

from buer.parse import _normalize_type_name, extract_defines
from buer.reconcile import reconcile
from buer.store import Store


# ── helpers ───────────────────────────────────────────────────────────────────

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
# 1. Unit: container types must NOT unwrap (return '')
# ══════════════════════════════════════════════════════════════════════════════

CONTAINER_ANNOTATIONS = [
    # Python built-in containers
    "list[Store]",
    "List[Store]",
    "Set[Store]",
    "set[Store]",
    "frozenset[Store]",
    "Sequence[Store]",
    "Iterable[Store]",
    "Iterator[Store]",
    "Collection[Store]",
    # TypeScript containers
    "Array<Store>",
    "ReadonlyArray<Store>",
]


@pytest.mark.parametrize("annotation", CONTAINER_ANNOTATIONS)
def test_container_not_bound(annotation):
    """Container type annotations must not unwrap to element type (no false-edge binding)."""
    result = _normalize_type_name(annotation)
    assert result == "", \
        f"Container '{annotation}' must not bind to element type; got '{result}'"


# ══════════════════════════════════════════════════════════════════════════════
# 2. Unit: transparent wrappers must still unwrap (regression guard)
# ══════════════════════════════════════════════════════════════════════════════

TRANSPARENT_ANNOTATIONS = [
    ("Optional[Store]", "Store"),
    ("Promise<Store>", "Store"),
    ("Awaitable[Store]", "Store"),
    ("Final[Store]", "Store"),
    ("ClassVar[Store]", "Store"),
    ("Coroutine[Store]", "Store"),
]


@pytest.mark.parametrize("annotation,expected", TRANSPARENT_ANNOTATIONS)
def test_transparent_wrapper_still_bound(annotation, expected):
    """Transparent wrappers (Optional/Promise/…) must still unwrap to inner type."""
    result = _normalize_type_name(annotation)
    assert result == expected, \
        f"Transparent wrapper '{annotation}' must unwrap to '{expected}'; got '{result}'"


# ══════════════════════════════════════════════════════════════════════════════
# 3. Unit: simple bare type unchanged
# ══════════════════════════════════════════════════════════════════════════════

def test_simple_type_unchanged():
    """Bare type annotations are returned as-is (simple types unaffected by fix)."""
    assert _normalize_type_name("Store") == "Store"
    assert _normalize_type_name(": Store") == "Store"
    assert _normalize_type_name("Store|None") == "Store"
    assert _normalize_type_name("Store?") == "Store"


# ══════════════════════════════════════════════════════════════════════════════
# 4. False-edge guard: container-typed param must not create edge to element method
# ══════════════════════════════════════════════════════════════════════════════

def test_container_method_no_false_edge(tmp_path):
    """items: list[Store]; items.append(1) must NOT create an edge to Store.append.

    Before the fix, items was bound to Store via list[Store] unwrapping → any
    method call on items could be misresolved to Store, producing false GD edges.
    """
    _py(tmp_path, "db.py", """\
class Store:
    def query(self): pass
    def append(self, x): pass  # same name as list.append — the trap
""")
    _py(tmp_path, "runner.py", """\
from db import Store
def process(items: list[Store]):
    items.append(1)
    items.query()
""")

    store, pid = _reconcile(
        str(tmp_path / "db.py"),
        str(tmp_path / "runner.py"),
        tmp_path=tmp_path,
    )
    # items is list[Store]; items must NOT be bound to Store.
    # So items.append() and items.query() must not produce edges to Store methods.
    callers_append = store.callers_of(pid, "py::db.Store.append")
    callers_query = store.callers_of(pid, "py::db.Store.query")

    assert not any("process" in c for c in callers_append), \
        f"list[Store].append must not edge to Store.append (false edge); callers={callers_append}"
    assert not any("process" in c for c in callers_query), \
        f"list[Store].query must not edge to Store.query (false edge); callers={callers_query}"
    store.close()


def test_optional_typed_param_still_resolves(tmp_path):
    """Regression guard: Optional[Store] still binds variable to Store (transparent wrapper)."""
    _py(tmp_path, "db.py", "class Store:\n    def query(self): pass\n")
    _py(tmp_path, "runner.py", """\
from db import Store
from typing import Optional
def process(repo: Optional[Store]):
    repo.query()
""")

    store, pid = _reconcile(
        str(tmp_path / "db.py"),
        str(tmp_path / "runner.py"),
        tmp_path=tmp_path,
    )
    callers = store.callers_of(pid, "py::db.Store.query")
    assert any("process" in c for c in callers), \
        f"Optional[Store] must still bind to Store; callers={callers}"
    store.close()
