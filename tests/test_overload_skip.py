"""Tests for @overload stub skipping (Python + TypeScript)."""
from __future__ import annotations

from pathlib import Path

import pytest

from buer.parse import extract_defines


# ── helpers ───────────────────────────────────────────────────────────────────

def _defines(src: str, suffix: str = ".py", tmp_path: Path | None = None) -> dict:
    """Extract defines from src string, return {qualified_name: define}."""
    if tmp_path is None:
        import tempfile, os
        d = tempfile.mkdtemp()
        p = os.path.join(d, f"mod{suffix}")
    else:
        p = str(tmp_path / f"mod{suffix}")
    Path(p).write_text(src, encoding="utf-8")
    defs = extract_defines(p)
    return {d.qualified_name: d for d in defs}


# ══════════════════════════════════════════════════════════════════════════════
# 1. Python — @overload __init__ x2 + real impl → only one define
# ══════════════════════════════════════════════════════════════════════════════

def test_overload_init_single_define(tmp_path):
    src = """\
from typing import overload

class HTTPBasicAuth:
    @overload
    def __init__(self, username: str, password: str) -> None: ...
    @overload
    def __init__(self, username: bytes, password: bytes) -> None: ...
    def __init__(self, username, password) -> None:
        self.username = username
        self.password = password
"""
    defs = _defines(src, tmp_path=tmp_path)
    keys = [k for k in defs if "__init__" in k]
    assert len(keys) == 1, f"Expected 1 __init__, got {len(keys)}: {keys}"
    assert "HTTPBasicAuth.__init__" in defs
    # Real implementation has a non-empty body (size_count > 0)
    impl = defs["HTTPBasicAuth.__init__"]
    assert impl.size_count > 0, "Expected real implementation body, got empty stub"


# ══════════════════════════════════════════════════════════════════════════════
# 2. Python — @typing.overload (prefixed form) also skipped
# ══════════════════════════════════════════════════════════════════════════════

def test_overload_typing_prefixed(tmp_path):
    src = """\
import typing

class Foo:
    @typing.overload
    def process(self, x: str) -> str: ...
    @typing.overload
    def process(self, x: int) -> int: ...
    def process(self, x):
        return x
"""
    defs = _defines(src, tmp_path=tmp_path)
    keys = [k for k in defs if "process" in k]
    assert len(keys) == 1
    assert "Foo.process" in defs


# ══════════════════════════════════════════════════════════════════════════════
# 3. Python — module-level @overload function
# ══════════════════════════════════════════════════════════════════════════════

def test_overload_function(tmp_path):
    src = """\
from typing import overload

@overload
def connect(host: str) -> None: ...
@overload
def connect(host: str, port: int) -> None: ...
def connect(host, port=80):
    pass
"""
    defs = _defines(src, tmp_path=tmp_path)
    keys = [k for k in defs if "connect" in k]
    assert len(keys) == 1
    assert "connect" in defs


# ══════════════════════════════════════════════════════════════════════════════
# 4. Python — normal decorators not affected (@property, @staticmethod, etc.)
# ══════════════════════════════════════════════════════════════════════════════

def test_normal_decorated_not_skipped(tmp_path):
    src = """\
class MyClass:
    @property
    def value(self):
        return self._value

    @staticmethod
    def helper():
        return 42

    @classmethod
    def create(cls):
        return cls()
"""
    defs = _defines(src, tmp_path=tmp_path)
    assert "MyClass.value" in defs
    assert "MyClass.helper" in defs
    assert "MyClass.create" in defs


# ══════════════════════════════════════════════════════════════════════════════
# 5. Python — no duplicate fingerprints for same qualified_name
# ══════════════════════════════════════════════════════════════════════════════

def test_no_duplicate_fingerprint(tmp_path):
    """Each qualified_name appears exactly once (no overload-induced duplicates)."""
    src = """\
from typing import overload

class Auth:
    @overload
    def __init__(self, username: str) -> None: ...
    @overload
    def __init__(self, username: bytes) -> None: ...
    def __init__(self, username) -> None:
        self.username = username
"""
    p = str(tmp_path / "mod.py")
    Path(p).write_text(src)
    defs = extract_defines(p)
    qnames = [d.qualified_name for d in defs]
    for qname in set(qnames):
        assert qnames.count(qname) == 1, f"{qname} appears {qnames.count(qname)} times"


# ══════════════════════════════════════════════════════════════════════════════
# 6. TypeScript — function overload signatures (no body) skipped
# ══════════════════════════════════════════════════════════════════════════════

def test_ts_overload_function(tmp_path):
    src = """\
function foo(x: string): void;
function foo(x: number): void;
function foo(x: any): void {
    console.log(x);
}
"""
    defs = _defines(src, suffix=".ts", tmp_path=tmp_path)
    keys = [k for k in defs if "foo" in k]
    assert len(keys) == 1, f"Expected 1 foo, got {len(keys)}: {keys}"


# ══════════════════════════════════════════════════════════════════════════════
# 7. TypeScript — method overload signatures skipped, impl kept
# ══════════════════════════════════════════════════════════════════════════════

def test_ts_overload_method(tmp_path):
    src = """\
class Parser {
    parse(input: string): string;
    parse(input: Buffer): string;
    parse(input: any): string {
        return String(input);
    }
}
"""
    defs = _defines(src, suffix=".ts", tmp_path=tmp_path)
    keys = [k for k in defs if "parse" in k]
    assert len(keys) == 1, f"Expected 1 parse, got {len(keys)}: {keys}"
    assert "Parser.parse" in defs


# ══════════════════════════════════════════════════════════════════════════════
# 8. TypeScript — arrow function not skipped
# ══════════════════════════════════════════════════════════════════════════════

def test_ts_arrow_not_skipped(tmp_path):
    src = """\
const add = (x: number, y: number): number => x + y;
const greet = (name: string) => {
    return `Hello ${name}`;
};
"""
    defs = _defines(src, suffix=".ts", tmp_path=tmp_path)
    assert "add" in defs
    assert "greet" in defs


# ══════════════════════════════════════════════════════════════════════════════
# 9. TypeScript — normal method with body not skipped
# ══════════════════════════════════════════════════════════════════════════════

def test_ts_normal_method_not_skipped(tmp_path):
    src = """\
class Service {
    fetch(url: string): Promise<Response> {
        return fetch(url);
    }

    async post(url: string, data: any) {
        return fetch(url, { method: 'POST', body: JSON.stringify(data) });
    }
}
"""
    defs = _defines(src, suffix=".ts", tmp_path=tmp_path)
    assert "Service.fetch" in defs
    assert "Service.post" in defs


# ══════════════════════════════════════════════════════════════════════════════
# 10. Regression — requests-like auth.py: no duplicate __init__ defines
# ══════════════════════════════════════════════════════════════════════════════

def test_requests_auth_no_dup(tmp_path):
    """Simulate requests/auth.py pattern: HTTPBasicAuth + HTTPDigestAuth with overloads."""
    src = """\
from typing import overload, Union

class HTTPBasicAuth:
    @overload
    def __init__(self, username: str, password: str) -> None: ...
    @overload
    def __init__(self, username: bytes, password: bytes) -> None: ...
    def __init__(self, username: Union[str, bytes], password: Union[str, bytes]) -> None:
        self.username = username
        self.password = password

    def __call__(self, r):
        r.headers["Authorization"] = "Basic"
        return r


class HTTPDigestAuth:
    @overload
    def __init__(self, username: str, password: str) -> None: ...
    @overload
    def __init__(self, username: bytes, password: bytes) -> None: ...
    def __init__(self, username: Union[str, bytes], password: Union[str, bytes]) -> None:
        self.username = username
        self.password = password

    def __call__(self, r):
        return r
"""
    p = str(tmp_path / "auth.py")
    Path(p).write_text(src)
    defs = extract_defines(p)

    qnames = [d.qualified_name for d in defs]
    # Each __init__ should appear exactly once
    assert qnames.count("HTTPBasicAuth.__init__") == 1
    assert qnames.count("HTTPDigestAuth.__init__") == 1
    # __call__ and other methods should still be there
    assert "HTTPBasicAuth.__call__" in qnames
    assert "HTTPDigestAuth.__call__" in qnames
