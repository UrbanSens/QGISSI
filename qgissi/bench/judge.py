"""LLM judge used by the benchmark adapters (no QGIS required).

``judge_main`` implements the ``--judge command`` contract of Google's
official runners: the prompt arrives on stdin, the reply goes to stdout, and
a non-zero exit status marks the call as failed.
"""
from __future__ import annotations

import sys
from typing import Optional, Tuple

from ..core.pricing import cost_usd
from ..providers import config_from_env, create_provider, parse_model_spec
from ..providers.base import Message, ModelRequest, text_block


def ask(model_spec: str, prompt: str, system: str = "", max_tokens: int = 16000) -> str:
    return ask_with_cost(model_spec, prompt, system, max_tokens)[0]


def ask_with_cost(model_spec: str, prompt: str, system: str = "",
                  max_tokens: int = 16000) -> Tuple[str, Optional[float]]:
    """``ask`` plus the dollar cost of the call (``None`` when the price is unknown)."""
    provider_name, model, effort = parse_model_spec(model_spec)
    config = config_from_env(provider_name)
    config.enable_fallbacks = False  # one fixed judge model for every call
    provider = create_provider(config)
    request = ModelRequest(model=model, system=system, messages=[Message("user", [text_block(prompt)])],
                           max_tokens=max_tokens, effort=effort, thinking_display="omitted", stream=True)
    response = provider.complete(request)
    if response.stop_reason == "refusal":
        raise RuntimeError(f"judge declined (category {response.refusal_category})")
    return response.message.text().strip(), cost_usd(model, response.usage, provider_name)


def judge_main(model_spec: str, prompt: str, max_tokens: int = 16000, out=None) -> int:
    out = out or sys.stdout
    try:
        reply = ask(model_spec, prompt, max_tokens=max_tokens)
    except Exception as exc:
        print(f"judge error: {exc}", file=sys.stderr)
        return 1
    out.write(reply + "\n")
    return 0


def maybe_cost(model_spec: str) -> Optional[str]:
    from ..providers.catalog import model_info
    _provider, model, _effort = parse_model_spec(model_spec)
    info = model_info(model)
    return None if info.price is None else f"${info.price[0]}/${info.price[1]} per MTok"
