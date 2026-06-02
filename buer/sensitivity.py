"""BUER notification level — the single user-facing tuning knob.

Controls only how often BUER proactively escalates to the user.
Detection is unaffected: BUER keeps monitoring at full sensitivity regardless.

Four levels (default: medium = current behaviour, backward-compatible):
  high   — more alerts; escalation threshold tightened by 1
  medium — balanced, default
  low    — fewer alerts; escalation threshold raised by 2
  silent — no proactive user escalation for efficiency signals;
            integrity signals (cheating/scope-violation) still surface

Silent semantics — "don't interrupt" not "turn off":
  • Agent channel works as normal (post-edit reminders to agent unaffected)
  • Pull tools (check_drift, get_user_alerts) return full data
  • Integrity signals escalate at medium threshold regardless of level

UX entry point: user tells Claude Code in plain language ("notify me less",
"silence BUER", "more alerts") → agent calls set_notification_level().
"""
from __future__ import annotations

NOTIFICATION_LEVELS: frozenset[str] = frozenset({"high", "medium", "low", "silent"})

# Efficiency signals: "you're spinning / wasting time" — safe to silence
EFFICIENCY_SIGNALS: frozenset[str] = frozenset({
    "stuck_region",
    "debug_loop",
    "define_loop",
    "token_waste",
    "regression",
    "dangling_reference",
})

# Integrity signals: agent cheating / scope violation — must not be silenced
# (user set silent to avoid noise; silencing cheating alerts defeats the purpose)
INTEGRITY_SIGNALS: frozenset[str] = frozenset({
    "test_tampering",
    "boundary_breach",
    "task_scope_breach",
})


def effective_escalation_theta2(
    level: str,
    signal: str,
    base_theta2: int,
) -> int | None:
    """Return effective THETA_2 for (notification_level, signal, base_theta2).

    Integrity signals are never weakened by level — silent/low preserve base_theta2.
    Efficiency signals obey level fully; silent returns None (skip user escalation).

    Returns:
        int  — escalate to user when post_notify_count >= this value
        None — never escalate to user proactively (silent mode, efficiency signal)
    """
    if signal in INTEGRITY_SIGNALS:
        # Safety rail: integrity alerts ignore silent/low — at most high tightens by 1
        if level == "high":
            return max(1, base_theta2 - 1)
        return base_theta2  # medium / low / silent: no weakening

    # Efficiency signals: fully obey level
    if level == "high":
        return max(1, base_theta2 - 1)
    if level == "medium":
        return base_theta2
    if level == "low":
        return base_theta2 + 2
    if level == "silent":
        return None  # never escalate user; agent channel still works
    return base_theta2  # unknown level → safe fallback


_LEVEL_DESCRIPTIONS: dict[str, str] = {
    "high":   "More alerts, including minor issues. Escalation threshold tightened.",
    "medium": "Balanced (default). Matches current BUER behaviour.",
    "low":    "Only important alerts. Minor issues reported to agent only.",
    "silent": (
        "No proactive user alerts for efficiency issues "
        "(BUER still monitors; check_drift still works; "
        "integrity issues like test-cheating and scope-violation still surface)."
    ),
}


def level_description(level: str) -> str:
    return _LEVEL_DESCRIPTIONS.get(level, level)
