"""Cost accounting for model usage."""
from __future__ import annotations

from typing import Dict, Optional, Sequence

from ..providers.base import Usage
from ..providers.catalog import model_info


def cost_usd(model_id: str, usage: Usage, provider: str = "",
             overrides: Optional[Dict[str, Sequence[float]]] = None) -> Optional[float]:
    """Dollar cost of one request, or ``None`` when the price is unknown.

    ``overrides`` maps model id -> (input, output, cache_write, cache_read)
    in USD per million tokens and wins over the built-in catalog.
    """
    info = model_info(model_id, provider)
    price = None
    if overrides and model_id in overrides:
        price = tuple(float(x) for x in overrides[model_id])
        if len(price) == 2:
            price = (price[0], price[1], price[0] * 1.25, price[0] * 0.1)
    if price is None:
        price = info.price
        if info.long_context_price and usage.total_input > info.long_context_price[0]:
            price = info.long_context_price[1]
    if price is None:
        return None
    inp, out, cache_write, cache_read = price
    total = (usage.input_tokens * inp + usage.output_tokens * out
             + usage.cache_write_tokens * cache_write + usage.cache_read_tokens * cache_read)
    return total / 1_000_000.0


def format_cost(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    if value == 0:
        return "$0.00"
    if value < 0.01:
        return f"${value:.4f}"
    return f"${value:.2f}"
