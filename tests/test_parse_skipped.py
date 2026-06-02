"""Tests for parse_skipped incident (発見5 — parse failure visibility).

Guards:
  - deep expression (RecursionError) → parse_skipped incident recorded
  - non-RecursionError exception (monkeypatched) → parse_skipped incident recorded
  - same file reconciled twice → dedup: only 1 open incident
  - batch: normal+bomb+normal → normal files ingest, bomb records incident
  - file fixed after failure → incident resolves automatically
  - normal file never produces parse_skipped (no false positives)
  - mutation: removing _warn_parse_skipped → test_deep_expression_recorded red

Note: tree-sitter is error-tolerant (syntax errors return [] without raising), so
tests that need a non-RecursionError failure use monkeypatching to inject the exception.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from buer import parse as buer_parse
from buer.reconcile import reconcile
from buer.store import Store


# ── helpers ───────────────────────────────────────────────────────────────────

def _mem_store() -> Store:
    return Store(":memory:")


def _deep_expr_src(depth: int = 5000) -> str:
    """Python source whose binary expression chain is ~depth levels deep."""
    chain = " + ".join(["1"] * depth)
    return f"x = {chain}\n"


def _open_parse_skipped(store: Store, pid: int) -> list:
    return [
        i for i in store.open_incidents(pid)
        if i["signal"] == "parse_skipped"
    ]


# ══════════════════════════════════════════════════════════════════════════════
# 1. Deep expression (RecursionError) → incident recorded
# ══════════════════════════════════════════════════════════════════════════════

def test_deep_expression_recorded(tmp_path):
    """depth-5000 binary chain triggers RecursionError; reconcile records parse_skipped.

    Mutation: removing _warn_parse_skipped call → no incident → this test red.
    """
    bomb = tmp_path / "bomb.py"
    bomb.write_text(_deep_expr_src(5000))

    s = _mem_store()
    pid = s.get_or_create_project(str(tmp_path))
    reconcile(s, pid, [str(bomb)])

    incidents = _open_parse_skipped(s, pid)
    assert len(incidents) == 1, f"expected 1 parse_skipped, got {len(incidents)}"
    assert incidents[0]["target_node"] == str(bomb)
    assert incidents[0]["state"] == "escalated_user"
    import json
    details = json.loads(incidents[0]["details"])
    assert "RecursionError" in details["error_type"]
    s.close()


# ══════════════════════════════════════════════════════════════════════════════
# 2. Non-RecursionError exception → incident recorded (覆盖非RecursionError的失败)
# ══════════════════════════════════════════════════════════════════════════════

def test_non_recursion_error_recorded(tmp_path):
    """Any exception from extract_defines (not just RecursionError) records parse_skipped.

    tree-sitter is error-tolerant so syntax errors return [] without raising.
    We monkeypatch to inject a ValueError and verify the except clause catches it.
    """
    f = tmp_path / "f.py"
    f.write_text("def fn(): pass\n")

    s = _mem_store()
    pid = s.get_or_create_project(str(tmp_path))

    with patch.object(buer_parse, "extract_defines", side_effect=ValueError("injected")):
        reconcile(s, pid, [str(f)])

    incidents = _open_parse_skipped(s, pid)
    assert len(incidents) == 1, f"expected 1 parse_skipped, got {len(incidents)}"
    assert incidents[0]["target_node"] == str(f)
    assert incidents[0]["state"] == "escalated_user"
    import json
    details = json.loads(incidents[0]["details"])
    assert details["error_type"] == "ValueError"
    s.close()


# ══════════════════════════════════════════════════════════════════════════════
# 3. Dedup: same failing file reconciled twice → only 1 open incident
# ══════════════════════════════════════════════════════════════════════════════

def test_dedup_same_file(tmp_path):
    """Reconciling the same broken file twice does not create duplicate incidents."""
    bomb = tmp_path / "bomb.py"
    bomb.write_text(_deep_expr_src(5000))

    s = _mem_store()
    pid = s.get_or_create_project(str(tmp_path))
    reconcile(s, pid, [str(bomb)])
    reconcile(s, pid, [str(bomb)])

    incidents = _open_parse_skipped(s, pid)
    assert len(incidents) == 1, (
        f"dedup must keep exactly 1 open incident; got {len(incidents)}"
    )
    s.close()


# ══════════════════════════════════════════════════════════════════════════════
# 4. Per-file isolation preserved: normal + bomb + normal
# ══════════════════════════════════════════════════════════════════════════════

def test_per_file_isolation_preserved(tmp_path):
    """Batch [good_a, bomb, good_b]: a and b ingest normally; bomb records parse_skipped."""
    good_a = tmp_path / "good_a.py"
    good_a.write_text("def alpha():\n    return 1\n")

    bomb = tmp_path / "bomb.py"
    bomb.write_text(_deep_expr_src(5000))

    good_b = tmp_path / "good_b.py"
    good_b.write_text("def beta():\n    return 2\n")

    s = _mem_store()
    pid = s.get_or_create_project(str(tmp_path))
    result = reconcile(s, pid, [str(good_a), str(bomb), str(good_b)])

    # good files got determinations
    affected_names = {name for _, name, _ in result.affected}
    assert "alpha" in affected_names, "alpha must be ingested"
    assert "beta" in affected_names, "beta must be ingested"

    # bomb recorded as blind spot
    incidents = _open_parse_skipped(s, pid)
    assert len(incidents) == 1
    assert incidents[0]["target_node"] == str(bomb)
    s.close()


# ══════════════════════════════════════════════════════════════════════════════
# 5. Resolve after fix: incident cleared when file later parses successfully
# ══════════════════════════════════════════════════════════════════════════════

def test_resolve_after_fix(tmp_path):
    """File fails parse → parse_skipped incident. File is fixed → incident resolves."""
    f = tmp_path / "calc.py"
    f.write_text(_deep_expr_src(5000))  # raises RecursionError

    s = _mem_store()
    pid = s.get_or_create_project(str(tmp_path))
    reconcile(s, pid, [str(f)])

    assert len(_open_parse_skipped(s, pid)) == 1, "must have 1 open incident after failure"

    # Fix the file
    f.write_text("def fn(a, b):\n    return a + b\n")
    reconcile(s, pid, [str(f)])

    # Incident must be resolved now
    open_now = _open_parse_skipped(s, pid)
    assert len(open_now) == 0, (
        f"parse_skipped incident must resolve when file parses again; open: {open_now}"
    )
    # The incident row exists but is resolved
    all_resolved = s.con.execute(
        "SELECT * FROM incidents WHERE signal='parse_skipped' AND target_node=? AND state='resolved'",
        (str(f),),
    ).fetchall()
    assert len(all_resolved) == 1, "resolved incident row must exist"
    s.close()


# ══════════════════════════════════════════════════════════════════════════════
# 6. Normal file: no parse_skipped incident (no false positives)
# ══════════════════════════════════════════════════════════════════════════════

def test_normal_file_no_incident(tmp_path):
    """Normal Python file produces no parse_skipped incident."""
    good = tmp_path / "good.py"
    good.write_text("def fn(a, b):\n    return a + b\n")

    s = _mem_store()
    pid = s.get_or_create_project(str(tmp_path))
    reconcile(s, pid, [str(good)])

    assert _open_parse_skipped(s, pid) == [], "normal file must never produce parse_skipped"
    s.close()
