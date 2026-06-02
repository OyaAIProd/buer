"""Claude model list prices for cost estimation (layer-2 fallback — no OTel data).

Used ONLY when BUER has no real telemetry (cost_samples table empty for the project).
When OTel data is present, cost is taken directly from claude_code.cost.usage (measured);
this module is NEVER consulted in that path.

Prices: USD per 1M tokens.
Source: https://anthropic.com/pricing
"""
from __future__ import annotations

PRICING_AS_OF = "2025-06"
PRICING_VERIFY_URL = "https://anthropic.com/pricing"
PRICING_NOTE = (
    f"List prices as of {PRICING_AS_OF}; prices change — verify at {PRICING_VERIFY_URL}"
)

# USD per 1M tokens: input / output / cache_read / cache_write
MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-5":    {"input": 15.0,  "output": 75.0,  "cache_read": 1.50, "cache_write": 18.75},
    "claude-opus-4":      {"input": 15.0,  "output": 75.0,  "cache_read": 1.50, "cache_write": 18.75},
    "claude-sonnet-4-6":  {"input": 3.0,   "output": 15.0,  "cache_read": 0.30, "cache_write": 3.75},
    "claude-sonnet-4":    {"input": 3.0,   "output": 15.0,  "cache_read": 0.30, "cache_write": 3.75},
    "claude-haiku-4-5":   {"input": 0.80,  "output": 4.0,   "cache_read": 0.08, "cache_write": 1.00},
    "claude-haiku-4":     {"input": 0.80,  "output": 4.0,   "cache_read": 0.08, "cache_write": 1.00},
}

# Conservative per-round token estimate for layer-2 fallback.
# Actual token count depends on context size; this is a deliberate lower bound.
TOKENS_PER_ROUND_INPUT = 5_000
TOKENS_PER_ROUND_CACHE_READ = 1_000
TOKENS_PER_ROUND_OUTPUT = 2_000


def lookup(model: str) -> dict[str, float] | None:
    """Return pricing dict for model, or None if unknown. Prefix-matches versioned names."""
    if model in MODEL_PRICING:
        return MODEL_PRICING[model]
    for key in MODEL_PRICING:
        if model.startswith(key):
            return MODEL_PRICING[key]
    return None


def estimate_round_cost(model: str) -> float | None:
    """Estimate USD cost for one edit round. Returns None if model unknown."""
    p = lookup(model)
    if p is None:
        return None
    return (
        TOKENS_PER_ROUND_INPUT       / 1_000_000 * p["input"]
        + TOKENS_PER_ROUND_CACHE_READ / 1_000_000 * p["cache_read"]
        + TOKENS_PER_ROUND_OUTPUT     / 1_000_000 * p["output"]
    )
