"""Tests for OTel cost tracking and savings_report (11 tests).

Coverage:
  1. test_otlp_metrics_parsed         — real OTLP/json payload parsed; cost_samples inserted
  2. test_session_bridge              — session_id from sessions table → project_id
  3. test_unknown_session_null_project — unknown session → project_id NULL, data not lost
  4. test_savings_report_layer1_labels — layer 1: measured label + estimate label present
  5. test_no_telemetry_graceful       — no samples → layer 3 proxy output
  6. test_subscription_note           — layer 1 report contains subscription note
  7. test_no_transcript_reads         — privacy: source code never reads .jsonl / transcript
  8. test_fallback_layer2_estimate    — no samples + known model → layer 2 estimate
  9. test_fallback_layer3_proxy_only  — no samples + no model → layer 3, no USD amount
  10. test_pricing_table_dated        — pricing table has date annotation + verify hint
  11. test_layer1_ignores_pricing     — OTel data → pricing table not consulted
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from buer import pricing
from buer.mcp.server import (
    _otel_attrs,
    _parse_otlp_metrics,
    _savings_layer1,
    _savings_layer2,
    _savings_layer3,
    _set_store_for_testing,
    savings_report,
)
from buer.store import Store


# ── helpers ────────────────────────────────────────────────────────────────────

def _store() -> Store:
    return Store(":memory:")


def _assert_layer3_checklist(report: str) -> None:
    """Assert Layer-3 diagnostic checklist covers all four failure modes."""
    assert "systemctl status buer" in report
    assert "CLAUDE_CODE_ENABLE_TELEMETRY" in report
    assert "grpc" in report
    assert "session" in report.lower() and "after" in report.lower()


def _project(store: Store, root: str = "/testproj") -> int:
    return store.get_or_create_project(root)


def _session(store: Store, pid: int, session_id: str = "sess-abc-123") -> None:
    store.open_session(pid, session_id)


def _det(store: Store, pid: int, seq: int, define: str = "fn_a") -> None:
    store.insert_determination(
        pid, seq=seq, file_path="/testproj/main.py",
        define_name=define, node_fingerprint=f"fp-{seq}", edit_type="modify",
    )


# Minimal real OTLP/http/json payload (matches Claude Code telemetry format).
# session.id attribute key uses dot notation (OTel semantic convention).
# cost.usage is a sum metric; value is asDouble.
SAMPLE_OTLP_PAYLOAD = {
    "resourceMetrics": [
        {
            "resource": {
                "attributes": [
                    {"key": "user.email", "value": {"stringValue": "test@example.com"}},
                ]
            },
            "scopeMetrics": [
                {
                    "metrics": [
                        {
                            "name": "claude_code.cost.usage",
                            "unit": "USD",
                            "sum": {
                                "dataPoints": [
                                    {
                                        "attributes": [
                                            {"key": "session.id", "value": {"stringValue": "sess-abc-123"}},
                                            {"key": "model", "value": {"stringValue": "claude-sonnet-4-6"}},
                                            {"key": "query_source", "value": {"stringValue": "main"}},
                                        ],
                                        "asDouble": 0.0425,
                                    }
                                ],
                                "isMonotonic": True,
                            },
                        },
                        {
                            "name": "claude_code.token.usage",
                            "unit": "tokens",
                            "sum": {
                                "dataPoints": [
                                    {
                                        "attributes": [
                                            {"key": "session.id", "value": {"stringValue": "sess-abc-123"}},
                                            {"key": "model", "value": {"stringValue": "claude-sonnet-4-6"}},
                                            {"key": "type", "value": {"stringValue": "input"}},
                                        ],
                                        "asDouble": 3000.0,
                                    },
                                    {
                                        "attributes": [
                                            {"key": "session.id", "value": {"stringValue": "sess-abc-123"}},
                                            {"key": "model", "value": {"stringValue": "claude-sonnet-4-6"}},
                                            {"key": "type", "value": {"stringValue": "cacheRead"}},
                                        ],
                                        "asDouble": 12964.0,
                                    },
                                    {
                                        "attributes": [
                                            {"key": "session.id", "value": {"stringValue": "sess-abc-123"}},
                                            {"key": "model", "value": {"stringValue": "claude-sonnet-4-6"}},
                                            {"key": "type", "value": {"stringValue": "output"}},
                                        ],
                                        "asDouble": 500.0,
                                    },
                                ],
                                "isMonotonic": True,
                            },
                        },
                    ]
                }
            ],
        }
    ]
}


# ── 1. OTLP payload parsed and stored ─────────────────────────────────────────

def test_otlp_metrics_parsed():
    store = _store()
    _parse_otlp_metrics(store, SAMPLE_OTLP_PAYLOAD)

    rows = store.con.execute("SELECT * FROM cost_samples").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == "sess-abc-123"
    assert row["model"] == "claude-sonnet-4-6"
    assert abs(row["cost_usd"] - 0.0425) < 1e-9
    assert row["input_tokens"] == 3000
    assert row["cache_read_tokens"] == 12964
    assert row["output_tokens"] == 500


# ── 2. session_id bridges to project_id ───────────────────────────────────────

def test_session_bridge():
    store = _store()
    pid = _project(store)
    _session(store, pid, "sess-abc-123")  # registers session_id → project_id

    _parse_otlp_metrics(store, SAMPLE_OTLP_PAYLOAD)

    row = store.con.execute("SELECT project_id FROM cost_samples").fetchone()
    assert row["project_id"] == pid


# ── 3. unknown session → project_id NULL, data not lost ───────────────────────

def test_unknown_session_null_project():
    store = _store()
    # No session registered — session_id unknown to BUER
    _parse_otlp_metrics(store, SAMPLE_OTLP_PAYLOAD)

    rows = store.con.execute("SELECT * FROM cost_samples").fetchall()
    assert len(rows) == 1
    assert rows[0]["project_id"] is None          # project unknown, row still kept
    assert abs(rows[0]["cost_usd"] - 0.0425) < 1e-9  # data preserved


# ── 4. Layer 1 report: measured + estimate labels ─────────────────────────────

def test_savings_report_layer1_labels():
    store = _store()
    pid = _project(store)
    _session(store, pid, "sess-abc-123")
    _parse_otlp_metrics(store, SAMPLE_OTLP_PAYLOAD)

    _set_store_for_testing(store)
    try:
        report = savings_report("/testproj")
    finally:
        _set_store_for_testing(None)

    assert "measured via telemetry" in report
    assert "$" in report
    assert "0.0425" in report


# ── 5. No telemetry → graceful layer-3 output ─────────────────────────────────

def test_no_telemetry_graceful():
    store = _store()
    pid = _project(store)
    for i in range(1, 4):
        _det(store, pid, i)

    _set_store_for_testing(store)
    try:
        report = savings_report("/testproj")
    finally:
        _set_store_for_testing(None)

    assert "no telemetry" in report.lower() or "proxy metrics" in report.lower()
    # Layer 3 must not fabricate a USD amount without data
    assert "$" not in report
    _assert_layer3_checklist(report)


# ── 6. Subscription note present in layer-1 report ───────────────────────────

def test_subscription_note():
    store = _store()
    pid = _project(store)
    _session(store, pid, "sess-abc-123")
    _parse_otlp_metrics(store, SAMPLE_OTLP_PAYLOAD)

    _set_store_for_testing(store)
    try:
        report = savings_report("/testproj")
    finally:
        _set_store_for_testing(None)

    assert "subscription" in report.lower() or "Max/Pro" in report


# ── 7. Privacy: no transcript/.jsonl reads in source ─────────────────────────

def test_no_transcript_reads():
    src_root = Path(__file__).parent.parent / "buer"
    patterns = [".jsonl", "transcript_path", "~/.claude/projects"]
    for py_file in src_root.rglob("*.py"):
        text = py_file.read_text(errors="replace")
        for pat in patterns:
            # Allow the pattern in docstrings/comments that explain what is NOT read
            # but flag any actual file read calls
            for line in text.splitlines():
                stripped = line.strip()
                if pat in stripped and not stripped.startswith("#") and not stripped.startswith('"""') and not stripped.startswith("'"):
                    # Allow string literals used only as documentation (in tool docstrings)
                    # Fail only on actual os.open / open() / Path.read calls
                    if any(op in stripped for op in ("open(", "read_text", "read_bytes", "Path(")):
                        pytest.fail(
                            f"Possible transcript read in {py_file}: {stripped!r}"
                        )


# ── 8. Layer-2 estimate with known model ─────────────────────────────────────

def test_fallback_layer2_estimate():
    store = _store()
    pid = _project(store)
    for i in range(1, 11):
        _det(store, pid, i)

    _set_store_for_testing(store)
    try:
        report = savings_report("/testproj", model="claude-sonnet-4-6")
    finally:
        _set_store_for_testing(None)

    assert "ESTIMATED" in report
    assert "no telemetry" in report.lower()
    assert "claude-sonnet-4-6" in report
    assert "$" in report  # USD estimate present
    assert "estimate" in report.lower()


# ── 9. Layer-3 proxy only when no model ──────────────────────────────────────

def test_fallback_layer3_proxy_only():
    store = _store()
    pid = _project(store)
    for i in range(1, 6):
        _det(store, pid, i)

    _set_store_for_testing(store)
    try:
        report = savings_report("/testproj")  # no model
    finally:
        _set_store_for_testing(None)

    # Should report proxy metrics
    assert "edit round" in report.lower() or "edit activity" in report.lower()
    # Must not claim a USD amount
    assert "$" not in report
    _assert_layer3_checklist(report)


# ── 10. Pricing table has date annotation and verify hint ─────────────────────

def test_pricing_table_dated():
    assert pricing.PRICING_AS_OF  # non-empty date string
    assert re.match(r"\d{4}-\d{2}", pricing.PRICING_AS_OF), "must be YYYY-MM format"
    assert "verify" in pricing.PRICING_NOTE.lower() or "check" in pricing.PRICING_NOTE.lower()
    assert "anthropic.com" in pricing.PRICING_NOTE


# ── 11. Layer-1 ignores pricing table ────────────────────────────────────────

def test_layer1_ignores_pricing():
    store = _store()
    pid = _project(store)
    _session(store, pid, "sess-abc-123")
    _parse_otlp_metrics(store, SAMPLE_OTLP_PAYLOAD)

    call_log = []
    original_lookup = pricing.lookup

    def spy_lookup(model):
        call_log.append(model)
        return original_lookup(model)

    _set_store_for_testing(store)
    try:
        with patch.object(pricing, "lookup", side_effect=spy_lookup):
            savings_report("/testproj")
    finally:
        _set_store_for_testing(None)

    assert call_log == [], (
        "pricing.lookup() must not be called when OTel cost_samples are present"
    )
