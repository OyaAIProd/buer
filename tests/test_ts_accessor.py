"""Tests for TS getter/setter accessor qualified_name disambiguation."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from buer.parse import extract_defines
from buer.store import Store
from buer.reconcile import reconcile


# ── helpers ───────────────────────────────────────────────────────────────────

def _parse_ts(src: str, tmp_path: Path, suffix: str = ".ts") -> list:
    p = tmp_path / f"mod{suffix}"
    p.write_text(textwrap.dedent(src))
    return extract_defines(str(p))


def _qnames(defs) -> list[str]:
    return [d.qualified_name for d in defs]


# ══════════════════════════════════════════════════════════════════════════════
# 1. get/set produce distinct qualified_names
# ══════════════════════════════════════════════════════════════════════════════

def test_getter_setter_distinct(tmp_path):
    src = """\
        class C {
          get x(): number { return this._x; }
          set x(v: number) { this._x = v; }
        }
    """
    defs = _parse_ts(src, tmp_path)
    qnames = _qnames(defs)
    assert "C.get x" in qnames
    assert "C.set x" in qnames
    assert qnames.count("C.get x") == 1
    assert qnames.count("C.set x") == 1


# ══════════════════════════════════════════════════════════════════════════════
# 2. repeated extraction produces stable, non-jumping fingerprints
# ══════════════════════════════════════════════════════════════════════════════

def test_getter_setter_stable_fp(tmp_path):
    """Extract same code twice; get/set fingerprints must each be identical across runs."""
    src = """\
        class Options {
          get request(): string { return this._r; }
          set request(v: string) { assertAny(v); this._r = v; }
        }
    """
    p = tmp_path / "mod.ts"
    p.write_text(textwrap.dedent(src))

    from buer.parse import compute_fingerprint
    defs1 = extract_defines(str(p))
    fps1 = {d.qualified_name: compute_fingerprint(d) for d in defs1}

    defs2 = extract_defines(str(p))
    fps2 = {d.qualified_name: compute_fingerprint(d) for d in defs2}

    for qname in fps1:
        assert fps1[qname] == fps2[qname], f"{qname} fingerprint differs between runs"

    # getter and setter should have different fingerprints from each other
    assert fps1.get("Options.get request") != fps1.get("Options.set request"), \
        "getter and setter should have distinct fingerprints"


# ══════════════════════════════════════════════════════════════════════════════
# 3. normal method unchanged (no get/set prefix)
# ══════════════════════════════════════════════════════════════════════════════

def test_normal_method_unchanged(tmp_path):
    src = """\
        class C {
          foo(): number { return 42; }
          bar(x: string): string { return x; }
        }
    """
    defs = _parse_ts(src, tmp_path)
    qnames = _qnames(defs)
    assert "C.foo" in qnames
    assert "C.bar" in qnames
    assert not any("get " in q or "set " in q for q in qnames)


# ══════════════════════════════════════════════════════════════════════════════
# 4. only getter (no setter) — single define with get prefix
# ══════════════════════════════════════════════════════════════════════════════

def test_only_getter(tmp_path):
    src = """\
        class C {
          get name(): string { return this._name; }
          doSomething() { return this.name; }
        }
    """
    defs = _parse_ts(src, tmp_path)
    qnames = _qnames(defs)
    assert "C.get name" in qnames
    assert "C.set name" not in qnames
    assert "C.doSomething" in qnames


# ══════════════════════════════════════════════════════════════════════════════
# 5. regression — got-like class with many get/set: zero multi-fingerprint defines
# ══════════════════════════════════════════════════════════════════════════════

def test_real_got_options_no_multi_fp(tmp_path):
    """Class with 50 get/set pairs: no qualified_name has more than one distinct Define."""
    lines = ["class Options {"]
    for i in range(25):
        lines.append(f"  get opt{i}(): string {{ return this._opt{i}; }}")
        lines.append(f"  set opt{i}(v: string) {{ this._opt{i} = v; }}")
    lines.append("}")
    src = "\n".join(lines)

    p = tmp_path / "options.ts"
    p.write_text(src)
    defs = extract_defines(str(p))

    by_qname: dict[str, int] = {}
    for d in defs:
        by_qname[d.qualified_name] = by_qname.get(d.qualified_name, 0) + 1

    multi = {k: v for k, v in by_qname.items() if v > 1}
    assert len(multi) == 0, f"Multi-define qualified_names (should be 0): {multi}"


# ══════════════════════════════════════════════════════════════════════════════
# 6. repeated reconcile — accessor fingerprints stable (no drift)
# ══════════════════════════════════════════════════════════════════════════════

def test_reconcile_no_drift(tmp_path):
    """Reconcile same file 3 times; get/set fingerprints must not jump."""
    src = """\
class Options {
  get request(): string { return this._r; }
  set request(v: string) { assertAny(v); this._r = v; }
  normal(): number { return 42; }
}
"""
    p = tmp_path / "options.ts"
    p.write_text(src)

    store = Store(":memory:")
    pid = store.get_or_create_project(str(tmp_path))

    for _ in range(3):
        reconcile(store, pid, [str(p)])

    # Latest fingerprint for each accessor should be consistent
    rows = store.con.execute(
        """SELECT define_name, node_fingerprint
           FROM determinations
           WHERE project_id=? AND file_path=? AND define_name IS NOT NULL
           ORDER BY define_name, seq""",
        (pid, str(p)),
    ).fetchall()

    by_name: dict[str, set] = {}
    for r in rows:
        by_name.setdefault(r["define_name"], set()).add(r["node_fingerprint"])

    # define_name stores qualified_name; get/set each must have exactly 1 fingerprint
    get_key = f"{str(tmp_path)}/options.ts::Options.get request"
    # actually define_name = qualified_name, no file prefix
    assert len(by_name.get("Options.get request", set())) == 1, \
        f"get request fingerprint not stable: {by_name.get('Options.get request')}"
    assert len(by_name.get("Options.set request", set())) == 1, \
        f"set request fingerprint not stable: {by_name.get('Options.set request')}"

    store.close()
