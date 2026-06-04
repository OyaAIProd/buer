"""BUER delivery layer — §4.4: incident text rendering + pending_deliveries queue.

Translates incident details (already computed by signal detectors and stored as
JSON in incidents.details) into delivery-ready text for two channels:

  'agent'  — injected into the coding agent's context (notified_agent state)
  'user'   — escalation notification to the human operator (escalated_user state)

Design invariants:
  - Text generation reads from inc["details"] ONLY — no re-computation.
  - Tone: structural facts + direction.  No prescriptive "you should X" judgements.
  - Lateral context (§3.6): shared-ancestry ω-pairs are included when present so
    the agent gets "往哪看" direction, not just "你触发了信号".

Public API
----------
  agent_message(inc) -> str
  user_message(inc) -> str
  queue_agent_injection(store, project_id, inc)
  queue_user_notification(store, project_id, inc)
"""
from __future__ import annotations

import json

from buer.store import Store


# ── shared helpers ─────────────────────────────────────────────────────────────

def _lateral_lines(details: dict) -> list[str]:
    """Format lateral context lines for agent injection (§3.5 / §3.2 three dimensions)."""
    lateral = details.get("lateral") or {}
    lines = []

    # ω-based shared ancestry
    shared = lateral.get("shared_ancestry") or []
    if shared:
        pairs = ", ".join(f"{e['define']}(ω={e['omega']:.2f})" for e in shared[:3])
        lines.append(f"Shared structural ancestors: {pairs}")

    # Γ_R shared direct merger  [GD Def 7.1]
    gamma_r = lateral.get("gamma_r") or []
    if gamma_r:
        items = []
        for g in gamma_r[:3]:
            label = g["define"]
            if g.get("recently_modified"):
                label += " (recently changed)"
            items.append(label)
        lines.append(f"Shared merge point (Γ_R): {', '.join(items)}")

    return lines


# ── agent-channel text (injected into agent context) ──────────────────────────

def agent_message(inc) -> str:
    """Generate agent-facing reminder text from an incident row.

    Uses inc["details"] (JSON string) only; no additional DB lookups.
    All content fields (question, direction_note, loop_question, lateral,
    duplicate_peers, etc.) were computed by the respective signal detectors
    and stored in details — this function only formats them.
    """
    details = json.loads(inc["details"] or "{}")
    signal = inc["signal"]
    target = inc["target_node"] or ""

    if signal == "stuck_region":
        n = details.get("chain_length", "?")
        lines = [
            f"[BUER] stuck_region: {target}",
            f"this define has been revised {n} times without structural convergence.",
        ]
        lines += _lateral_lines(details)

    elif signal == "debug_loop":
        n = details.get("chain_length", "?")
        tier = details.get("test_tier", "")
        tcs = details.get("test_cases") or []
        note = details.get("direction_note", "")
        tc_str = ", ".join(tcs[:3]) + (" …" if len(tcs) > 3 else "")
        lines = [
            f"[BUER] debug_loop: {target}",
            f"this define has been revised {n} times; associated tests continue to fail ({tier}-tier association).",
        ]
        if tc_str:
            lines.append(f"failing tests: {tc_str}")
        if note:
            lines.append(f"direction hint: {note}")
        lines += _lateral_lines(details)

    elif signal == "regression":
        q = details.get("question", "")
        lines = [f"[BUER] regression: {target}", q]

    elif signal == "define_loop":
        q = details.get("loop_question", "")
        lines = [
            f"[BUER] define_loop: {target}",
            q,
        ]
        lines += _lateral_lines(details)

    elif signal == "boundary_breach":
        note = details.get("note", "")
        lines = [f"[BUER] boundary_breach: {target}", note]

    elif signal == "task_scope_breach":
        note = details.get("note", "")
        globs = details.get("allowed_globs") or []
        glob_str = ", ".join(globs)
        lines = [f"[BUER] task_scope_breach: {target}", note]
        if glob_str:
            lines.append(f"declared task scope: {glob_str}")

    elif signal == "test_tampering":
        # test_tampering normally escalates directly to user, but this path is
        # reached if it somehow lands in notified_agent (e.g., forced reminder).
        q = details.get("question", "")
        td = details.get("test_define", "")
        lines = [f"[BUER] test_tampering: {target}", q]
        if td:
            lines.append(f"modified test location: {td}")

    elif signal == "token_waste":
        rounds = details.get("rounds", "?")
        time_span = details.get("time_span_hours", 0)
        ic = details.get("incident_count", "?")
        define_display = target.split("::")[-1] if "::" in target else target
        time_part = f", over {time_span:.1f} hours" if time_span else ""
        lines = [
            f"[BUER] token_waste: {target}",
            f"{define_display} has accumulated {rounds} edit rounds{time_part} without convergence"
            f" ({ic} stuck/debug-loop signals fired).",
            "further effort here may yield little; consider pausing to inspect manually.",
            "(whether to stop is your call; BUER only reports effort vs. convergence)",
        ]

    else:
        lines = [
            f"[BUER] {signal}: {target}",
            json.dumps(details, ensure_ascii=False),
        ]

    return "\n".join(line for line in lines if line)


# ── user-channel text (escalation to human operator) ─────────────────────────

def user_message(inc) -> str:
    """Generate user-facing escalation text from an incident row.

    Escalation text is more prominent than agent reminders (⚠ prefix) and
    includes relevant structural direction so the user can orient quickly.
    """
    details = json.loads(inc["details"] or "{}")
    signal = inc["signal"]
    target = inc["target_node"] or ""
    header = f"[BUER] ⚠ escalated to user — {signal}: {target}"

    if signal in ("stuck_region", "debug_loop"):
        n = details.get("chain_length", "?")
        body = f"this define has been revised {n} times; BUER has reminded the agent multiple times but the signal continues to fire."
        if signal == "debug_loop":
            note = details.get("direction_note", "")
            if note:
                body += f"\ndirection hint: {note}"
        lines = [header, body] + _lateral_lines(details)

    elif signal == "regression":
        q = details.get("question", "")
        tc = details.get("testcase", target)
        lines = [header, f"test case: {tc}", q]

    elif signal == "test_tampering":
        q = details.get("question", "")
        td = details.get("test_define", "")
        lines = [header, q]
        if td:
            lines.append(f"modified test location: {td}")

    elif signal == "define_loop":
        q = details.get("loop_question", "")
        lines = [header, q]

    elif signal == "boundary_breach":
        note = details.get("note", "")
        lines = [header, note]

    elif signal == "task_scope_breach":
        note = details.get("note", "")
        globs = details.get("allowed_globs") or []
        glob_str = ", ".join(globs)
        lines = [header, note]
        if glob_str:
            lines.append(f"declared task scope: {glob_str}")

    elif signal == "token_waste":
        rounds = details.get("rounds", "?")
        time_span = details.get("time_span_hours", 0)
        ic = details.get("incident_count", "?")
        define_display = target.split("::")[-1] if "::" in target else target
        time_part = f", over {time_span:.1f} hours" if time_span else ""
        proxy_note = details.get("proxy_note", "")
        lines = [
            f"[BUER] ⚠ effort waste warning — {target}",
            f"{define_display} has accumulated {rounds} edit rounds{time_part} without convergence"
            f" ({ic} stuck/debug-loop signals fired).",
            "further effort here may yield little; consider pausing to intervene manually.",
        ]
        if proxy_note:
            lines.append(f"note: {proxy_note}")

    else:
        lines = [header, json.dumps(details, ensure_ascii=False)]

    return "\n".join(line for line in lines if line)


# ── queue helpers (called from advance_incidents §4.4 seams) ─────────────────

def queue_agent_injection(store: Store, project_id: int, inc) -> None:
    """Enqueue an agent-channel delivery for this incident (§4.4)."""
    msg = agent_message(inc)
    store.enqueue_delivery(project_id, inc["id"], "agent", msg, kind="alert")


def queue_user_notification(store: Store, project_id: int, inc) -> None:
    """Enqueue a user-channel delivery for this incident (§4.4)."""
    msg = user_message(inc)
    store.enqueue_delivery(project_id, inc["id"], "user", msg, kind="alert")
