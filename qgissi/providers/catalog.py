"""Known models, their API capabilities and list prices.

Prices are USD per million tokens (input, output, 5-minute cache write, cache
read) as published by the vendor. Anthropic figures were verified against the
Claude API reference on 2026-10-21. Models that are not listed still work; the
plugin then reports tokens without a dollar figure unless the user adds a
price override in the settings.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class ModelInfo:
    provider: str
    id: str
    label: str
    context: int = 200_000
    max_output: int = 32_000
    default_max_tokens: int = 16_000
    vision: bool = True
    # Thinking behaviour: "adaptive" (adaptive on request), "always" (cannot be
    # disabled; effort is the control), "budget" (legacy budget_tokens),
    # "none" (no thinking API).
    thinking: str = "adaptive"
    effort: bool = False
    default_effort: Optional[str] = None
    preserved_thinking: bool = False   # thinking blocks bound to model + prefix
    fallbacks: Optional[str] = None     # "default" when server-side fallback exists
    forced_tool_choice: bool = True
    price: Optional[Tuple[float, float, float, float]] = None
    long_context_price: Optional[Tuple[int, Tuple[float, float, float, float]]] = None
    notes: str = ""
    tags: Tuple[str, ...] = field(default_factory=tuple)


def _claude(model_id: str, label: str, price, **kw) -> ModelInfo:
    base = dict(provider="anthropic", id=model_id, label=label, context=1_000_000,
                max_output=128_000, default_max_tokens=64_000, vision=True, price=price)
    base.update(kw)
    return ModelInfo(**base)


MODELS: Dict[str, ModelInfo] = {m.id: m for m in [
    # --- Anthropic: current generation -------------------------------------
    _claude("claude-opus-5-5", "Claude Opus 5.5", (4.0, 20.0, 5.0, 0.20), thinking="always",
            effort=True, default_effort="medium", preserved_thinking=True, fallbacks="default",
            forced_tool_choice=False, tags=("frontier", "default")),
    _claude("claude-fable-5-1", "Claude Fable 5.1", (10.0, 50.0, 12.5, 0.25), thinking="always",
            effort=True, default_effort="high", preserved_thinking=True, fallbacks="default",
            forced_tool_choice=False, tags=("frontier",)),
    _claude("claude-sonnet-5-5", "Claude Sonnet 5.5", (2.0, 10.0, 2.5, 0.20), thinking="always",
            effort=True, default_effort="high", preserved_thinking=True, fallbacks="default",
            forced_tool_choice=False),
    _claude("claude-haiku-5-5", "Claude Haiku 5.5", (0.10, 0.50, 0.125, 0.01), thinking="adaptive",
            effort=True, default_effort="medium", preserved_thinking=True,
            long_context_price=(100_000, (0.50, 2.50, 0.625, 0.05))),
    # --- Anthropic: previous generations (still served) --------------------
    _claude("claude-opus-5", "Claude Opus 5", (5.0, 25.0, 6.25, 0.50), effort=True,
            default_effort="high", fallbacks="default"),
    _claude("claude-sonnet-5", "Claude Sonnet 5", (2.0, 10.0, 2.5, 0.20), effort=True, default_effort="high"),
    _claude("claude-opus-4-8", "Claude Opus 4.8", (5.0, 25.0, 6.25, 0.50), effort=True, default_effort="high"),
    _claude("claude-opus-4-7", "Claude Opus 4.7", (5.0, 25.0, 6.25, 0.50), effort=True, default_effort="high"),
    _claude("claude-opus-4-6", "Claude Opus 4.6", (5.0, 25.0, 6.25, 0.50), effort=True, default_effort="high"),
    _claude("claude-sonnet-4-6", "Claude Sonnet 4.6", (3.0, 15.0, 3.75, 0.30), effort=True, default_effort="high"),
    _claude("claude-haiku-4-5", "Claude Haiku 4.5", (1.0, 5.0, 1.25, 0.10), context=200_000,
            max_output=64_000, default_max_tokens=16_000, thinking="budget"),
]}

PROVIDER_DEFAULT_MODEL = {
    "anthropic": "claude-opus-5-5",
    "openai": "",
    "gemini": "",
    "ollama": "",
    "openai_compatible": "",
}

PROVIDER_LABELS = {
    "anthropic": "Anthropic (Claude)",
    "openai": "OpenAI",
    "gemini": "Google Gemini",
    "ollama": "Ollama (local)",
    "openai_compatible": "OpenAI-compatible endpoint",
}


def model_info(model_id: str, provider: str = "") -> ModelInfo:
    """Return catalog info, or a conservative default for unknown models."""
    info = MODELS.get(model_id)
    if info is not None:
        return info
    if provider == "anthropic" or model_id.startswith("claude-"):
        return _claude(model_id, model_id, None, effort=True)
    if provider == "ollama":
        return ModelInfo(provider="ollama", id=model_id, label=model_id, context=32_768,
                         max_output=8_192, default_max_tokens=8_192, thinking="none", price=(0, 0, 0, 0))
    return ModelInfo(provider=provider or "unknown", id=model_id, label=model_id, context=128_000,
                     max_output=32_000, default_max_tokens=16_000, thinking="none")


def anthropic_models():
    return [m for m in MODELS.values() if m.provider == "anthropic"]
