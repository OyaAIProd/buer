"""BUER threshold calibration telemetry — read-only analysis tool.

Usage:
    python -m buer.calibration [--db PATH] [--project ROOT]

Produces a descriptive calibration report over all incidents in the store.
Purely read-only: no INSERT / UPDATE / DELETE is performed; the incidents
table is identical byte-for-byte before and after this command.

Honest scope:
  - Descriptive evidence to assist manual threshold tuning; does NOT
    automatically classify true/false positives.
  - resolve_by paths carry partial causal signal (test_green = test passed
    again; back_in_scope = scope restored) but 'no_more_equiv' is ambiguous
    (fixed correctly OR agent gave up).
  - Distributions with n < 20 are marked "sample too small, indicative only".
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from pathlib import Path
from typing import Any

from buer.store import Store
import buer.signals as _sig


# ── All 9 monitored signals ───────────────────────────────────────────────────

_ALL_SIGNALS = [
    "stuck_region",
    "define_loop",
    "debug_loop",
    "regression",
    "test_tampering",
    "boundary_breach",
    "task_scope_breach",
    "token_waste",
    "dangling_reference",
]

_SMALL_SAMPLE = 20


# ── Threshold look-up ─────────────────────────────────────────────────────────

def _theta2(signal: str) -> int:
    return _sig.THETA_2.get(signal, _sig.THETA_2_DEFAULT)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _percentiles(values: list[float]) -> dict[str, float | str]:
    if not values:
        return {"min": "n/a", "p25": "n/a", "median": "n/a", "p75": "n/a", "max": "n/a"}
    s = sorted(values)
    n = len(s)
    def _p(pct: float) -> float:
        idx = pct / 100 * (n - 1)
        lo, hi = int(idx), min(int(idx) + 1, n - 1)
        return s[lo] + (idx - lo) * (s[hi] - s[lo])
    return {
        "min": round(s[0], 1),
        "p25": round(_p(25), 1),
        "median": round(_p(50), 1),
        "p75": round(_p(75), 1),
        "max": round(s[-1], 1),
    }


def _parse_details(row: Any) -> dict:
    try:
        raw = row["details"]
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return {}


def _duration_seconds(row: Any) -> float | None:
    """Return (updated_at - created_at) in seconds for resolved rows."""
    try:
        from datetime import datetime
        fmt = "%Y-%m-%d %H:%M:%S"
        ca = row["created_at"]
        ua = row["updated_at"]
        if ca and ua:
            t0 = datetime.strptime(ca[:19], fmt)
            t1 = datetime.strptime(ua[:19], fmt)
            return (t1 - t0).total_seconds()
    except Exception:
        pass
    return None


def _fmt_pct(n: int, total: int) -> str:
    if total == 0:
        return "n/a"
    return f"{100 * n / total:.0f}%"


def _quantile_str(values: list[float], threshold: float | None = None,
                  label: str = "") -> str:
    p = _percentiles(values)
    s = (f"  min={p['min']}  p25={p['p25']}  median={p['median']}"
         f"  p75={p['p75']}  max={p['max']}  (n={len(values)})")
    if threshold is not None:
        s += f"  [threshold {label}={threshold}]"
    if values:
        med = p["median"]
        if isinstance(med, (int, float)) and threshold is not None:
            ratio = med / threshold if threshold != 0 else float("inf")
            if ratio < 1.2:
                s += "\n  ↳ median near threshold → many edge-triggers → threshold may be loose"
            elif ratio > 3:
                s += "\n  ↳ median far above threshold → threshold may be tight / real issues"
    return s


# ── Per-signal analysis ───────────────────────────────────────────────────────

def _analyse_signal(rows: list[Any], signal: str) -> str:
    n = len(rows)
    lines: list[str] = []

    if n == 0:
        return "  (no incidents recorded)\n"

    sample_note = "  ⚠ sample too small (n < 20), indicative only\n" if n < _SMALL_SAMPLE else ""
    lines.append(sample_note)

    # State distribution
    state_counts: dict[str, int] = {}
    for r in rows:
        state_counts[r["state"]] = state_counts.get(r["state"], 0) + 1
    lines.append("  state distribution:")
    for st in ("open", "notified_agent", "escalated_user", "resolved"):
        c = state_counts.get(st, 0)
        lines.append(f"    {st:<20} {c:>4}  ({_fmt_pct(c, n)})")

    # Escalate rate
    esc = state_counts.get("escalated_user", 0)
    lines.append(f"  escalate rate: {esc}/{n} = {_fmt_pct(esc, n)}")

    # Resolve paths
    resolved_rows = [r for r in rows if r["state"] == "resolved"]
    if resolved_rows:
        by_path: dict[str, int] = {}
        for r in resolved_rows:
            rb = r["resolved_by"] or "unknown"
            by_path[rb] = by_path.get(rb, 0) + 1
        lines.append(f"  resolve paths (n={len(resolved_rows)}):")
        for path, cnt in sorted(by_path.items(), key=lambda x: -x[1]):
            lines.append(f"    {path:<30} {cnt}")

    # post_notify_count distribution
    pnc_values = [r["post_notify_count"] for r in rows if r["post_notify_count"] is not None]
    theta2 = _theta2(signal)
    lines.append(f"  post_notify_count (Θ₂={theta2}):")
    lines.append(_quantile_str(pnc_values, float(theta2), "Θ₂"))

    # Time-to-resolve for resolved incidents
    durations = [_duration_seconds(r) for r in resolved_rows]
    durations = [d for d in durations if d is not None]
    if durations:
        med_s = statistics.median(durations)
        if med_s < 120:
            dur_str = f"{med_s:.0f}s"
        elif med_s < 3600:
            dur_str = f"{med_s/60:.1f} min"
        else:
            dur_str = f"{med_s/3600:.1f} h"
        lines.append(f"  median time-to-resolve (n={len(durations)}): {dur_str}")

    # Signal-specific metric distributions
    detail_failures = 0
    metric_lines: list[str] = []

    if signal == "token_waste":
        rounds_vals: list[float] = []
        for r in rows:
            d = _parse_details(r)
            if not d:
                detail_failures += 1
                continue
            v = d.get("rounds")
            if v is not None:
                rounds_vals.append(float(v))
        metric_lines.append(f"  details.rounds (Θ_WASTE={_sig.THETA_WASTE}):")
        metric_lines.append(_quantile_str(rounds_vals, float(_sig.THETA_WASTE), "Θ_WASTE"))

    elif signal == "define_loop":
        consec_vals: list[float] = []
        for r in rows:
            d = _parse_details(r)
            if not d:
                detail_failures += 1
                continue
            v = d.get("consec") or d.get("consecutive_edits")
            if v is not None:
                consec_vals.append(float(v))
        metric_lines.append(
            f"  details.consec (agent={_sig.N_DEFINE_LOOP_CONSEC_AGENT},"
            f" user={_sig.N_DEFINE_LOOP_CONSEC_USER}):"
        )
        metric_lines.append(_quantile_str(
            consec_vals, float(_sig.N_DEFINE_LOOP_CONSEC_AGENT), "N_AGENT"
        ))

    elif signal == "stuck_region":
        chain_vals: list[float] = []
        for r in rows:
            d = _parse_details(r)
            if not d:
                detail_failures += 1
                continue
            v = d.get("chain_length")
            if v is not None:
                chain_vals.append(float(v))
        metric_lines.append(f"  details.chain_length (Θ₁={_sig.THETA_1}):")
        metric_lines.append(_quantile_str(chain_vals, float(_sig.THETA_1), "Θ₁"))

    elif signal == "debug_loop":
        chain_vals2: list[float] = []
        for r in rows:
            d = _parse_details(r)
            if not d:
                detail_failures += 1
                continue
            v = d.get("chain_length")
            if v is not None:
                chain_vals2.append(float(v))
        if chain_vals2:
            metric_lines.append("  details.chain_length (no independent numeric threshold):")
            metric_lines.append(_quantile_str(chain_vals2))

    elif signal == "dangling_reference":
        metric_lines.append(
            f"  note: obs-count vs Θ₁_DANGLING={_sig.THETA_1_DANGLING} lives in"
            "  dangling_ref_observations table, not in details;"
            "  obs-count threshold calibration not included in this version."
        )

    if detail_failures:
        metric_lines.append(f"  details parse failures (skipped): {detail_failures}")

    lines.extend(metric_lines)
    return "\n".join(lines) + "\n"


# ── Report ────────────────────────────────────────────────────────────────────

_HEADER = """\
╔══════════════════════════════════════════════════════════════════════════════╗
║            BUER Threshold Calibration Report (read-only)                   ║
╚══════════════════════════════════════════════════════════════════════════════╝

Honest scope
  · Descriptive evidence — assists manual threshold tuning only.
  · Does NOT automatically classify true/false positives.
  · resolve_by paths carry partial causal signal:
      test_green        = associated test turned green again
      back_in_scope     = agent returned to declared scope
      no_more_equiv     = loop broken — but ambiguous (fixed OR gave up)
  · n < 20 per signal: marked "sample too small, indicative only".
  · Read-only: zero writes to the store during this run.
"""


def _build_report(store: Store, project_id: int | None) -> str:
    parts = [_HEADER]

    # project filter clause
    if project_id is not None:
        where = f"WHERE project_id = {project_id}"
        scope_label = f"project_id={project_id}"
    else:
        where = ""
        scope_label = "all projects"

    total_incidents = store.con.execute(
        f"SELECT COUNT(*) AS n FROM incidents {where}"
    ).fetchone()["n"]
    parts.append(f"Scope: {scope_label}  |  total incidents: {total_incidents}\n")

    all_rows = store.con.execute(
        f"SELECT * FROM incidents {where} ORDER BY id"
    ).fetchall()

    by_signal: dict[str, list[Any]] = {s: [] for s in _ALL_SIGNALS}
    for row in all_rows:
        sig = row["signal"]
        if sig in by_signal:
            by_signal[sig].append(row)

    for signal in _ALL_SIGNALS:
        rows = by_signal[signal]
        parts.append(f"{'─' * 78}")
        parts.append(f"Signal: {signal}  (n={len(rows)})")
        parts.append(_analyse_signal(rows, signal))

    return "\n".join(parts)


# ── CLI entry point ───────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m buer.calibration",
        description="Read-only threshold calibration report over BUER incident history.",
    )
    parser.add_argument(
        "--db",
        default=str(Path.home() / ".buer" / "store.sqlite"),
        help="Path to BUER SQLite store (default: ~/.buer/store.sqlite)",
    )
    parser.add_argument(
        "--project",
        metavar="ROOT",
        default=None,
        help="Limit to one project by root path; omit for whole-db aggregate.",
    )
    args = parser.parse_args(argv)

    store = Store(args.db)

    project_id: int | None = None
    if args.project:
        project_id = store.find_project_for_file(args.project)
        if project_id is None:
            print(
                f"[calibration] project not found for root: {args.project!r}",
                file=sys.stderr,
            )
            sys.exit(1)

    report = _build_report(store, project_id)
    print(report)


if __name__ == "__main__":
    main()
