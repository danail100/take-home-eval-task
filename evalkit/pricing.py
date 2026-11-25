"""A versioned price table. Cost is derived from token counts when a run is analysed.

Spans store tokens, not dollars. That way a price change never forces new inference, and
two runs are always priced with the same table when they are compared.
"""

from __future__ import annotations

PRICE_TABLE_VERSION = "2026-11-24"

# USD per 1M tokens: (input, output)
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    # Fictional prices for the offline scripted models, chosen to mirror a small/large tier gap.
    "scripted-small": (1.00, 5.00),
    "scripted-large": (3.00, 15.00),
    # Anthropic first-party list prices (standard tier, no caching/batch discounts;
    # Haiku 5.5 price applies to prompts up to 100K tokens).
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}


class UnknownModelPrice(KeyError):
    pass


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    if model not in PRICES_PER_MTOK:
        raise UnknownModelPrice(f"no price for model {model!r}; add it to evalkit/pricing.py")
    p_in, p_out = PRICES_PER_MTOK[model]
    return (input_tokens * p_in + output_tokens * p_out) / 1_000_000
