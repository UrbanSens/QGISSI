"""Model providers and a factory that builds them from settings or a spec string."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional, Tuple

from .anthropic import AnthropicProvider
from .base import EFFORT_LEVELS, Provider, ProviderError
from .gemini import GeminiProvider
from .http import HttpClient
from .ollama import OllamaProvider
from .openai_chat import OpenAIChatProvider

PROVIDERS = ("anthropic", "openai", "gemini", "ollama", "openai_compatible")


@dataclass
class ProviderConfig:
    provider: str
    api_key: str = ""
    base_url: str = ""
    auth_token: str = ""
    enable_fallbacks: bool = True
    prompt_caching: bool = True
    timeout: float = 600.0
    proxy: str = ""
    num_ctx: int = 32768


def parse_model_spec(spec: str) -> Tuple[str, str, Optional[str]]:
    """``"anthropic/claude-opus-5-5@high"`` -> ("anthropic", "claude-opus-5-5", "high").

    The provider is the text before the first ``/``; an ``@effort`` suffix is
    optional. Model ids may contain ``/`` and ``:`` (``ollama/mistral:latest``).
    """
    provider, _, model = spec.partition("/")
    if not model:
        provider, model = ("anthropic", provider) if provider.startswith("claude-") else (provider, "")
    effort = None
    if "@" in model:
        head, _, tail = model.rpartition("@")
        if tail in EFFORT_LEVELS:
            model, effort = head, tail
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown provider '{provider}' in '{spec}'. Use one of: {', '.join(PROVIDERS)}")
    return provider, model, effort


def config_from_env(provider: str) -> ProviderConfig:
    env = os.environ.get
    if provider == "anthropic":
        return ProviderConfig(provider, api_key=env("ANTHROPIC_API_KEY", ""),
                              auth_token=env("ANTHROPIC_AUTH_TOKEN", ""), base_url=env("ANTHROPIC_BASE_URL", ""))
    if provider == "openai":
        return ProviderConfig(provider, api_key=env("OPENAI_API_KEY", ""), base_url=env("OPENAI_BASE_URL", ""))
    if provider == "gemini":
        return ProviderConfig(provider, api_key=env("GEMINI_API_KEY", "") or env("GOOGLE_API_KEY", ""))
    if provider == "ollama":
        host = env("OLLAMA_HOST", "")
        if host and not host.startswith("http"):
            host = "http://" + host
        return ProviderConfig(provider, base_url=host)
    return ProviderConfig(provider, api_key=env("QGISSI_COMPAT_API_KEY", ""), base_url=env("QGISSI_COMPAT_BASE_URL", ""))


def create_provider(config: ProviderConfig) -> Provider:
    http = HttpClient(timeout=config.timeout, proxy=config.proxy or None)
    name = config.provider
    if name == "anthropic":
        return AnthropicProvider(api_key=config.api_key, auth_token=config.auth_token,
                                 base_url=config.base_url or "https://api.anthropic.com", http=http,
                                 enable_fallbacks=config.enable_fallbacks, prompt_caching=config.prompt_caching)
    if name == "openai":
        return OpenAIChatProvider(api_key=config.api_key, base_url=config.base_url or "https://api.openai.com/v1",
                                  http=http, name="openai")
    if name == "gemini":
        return GeminiProvider(api_key=config.api_key, base_url=config.base_url or "", http=http)
    if name == "ollama":
        return OllamaProvider(base_url=config.base_url or "http://localhost:11434",
                              http=HttpClient(timeout=max(config.timeout, 1800), proxy=config.proxy or None),
                              num_ctx=config.num_ctx)
    if name == "openai_compatible":
        if not config.base_url:
            raise ProviderError("The OpenAI-compatible provider needs a base URL (e.g. http://localhost:1234/v1).")
        return OpenAIChatProvider(api_key=config.api_key, base_url=config.base_url, http=http,
                                  name="openai_compatible")
    raise ProviderError(f"Unknown provider: {name}")


__all__ = ["PROVIDERS", "Provider", "ProviderConfig", "ProviderError", "config_from_env", "create_provider",
           "parse_model_spec"]
