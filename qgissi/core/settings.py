"""Plugin settings (QgsSettings) and API-key storage.

Keys are stored in the QGIS authentication database (encrypted with the QGIS
master password) when it is available. If the user declines a master
password, the key falls back to QgsSettings and the UI says so. Environment
variables (ANTHROPIC_API_KEY, ...) are used when nothing is stored.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, fields
from typing import Any, Dict, Optional

from qgis.core import QgsApplication, QgsAuthMethodConfig, QgsSettings

from ..providers import ProviderConfig, config_from_env

PREFIX = "qgissi/"
_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY",
        "openai_compatible": "QGISSI_COMPAT_API_KEY"}


@dataclass
class PluginSettings:
    provider: str = "anthropic"
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    approval_mode: str = "ask_risky"
    tool_tier: str = "full"
    max_steps: int = 40
    max_cost_usd: float = 5.0
    thinking_display: str = "summarized"
    allow_network: bool = True
    enable_fallbacks: bool = True
    anthropic_base_url: str = ""
    openai_base_url: str = ""
    compat_base_url: str = "http://localhost:1234/v1"
    ollama_base_url: str = "http://localhost:11434"
    ollama_num_ctx: int = 32768
    output_dir: str = ""

    @classmethod
    def load(cls) -> "PluginSettings":
        store = QgsSettings()
        values: Dict[str, Any] = {}
        for f in fields(cls):
            default = f.default
            raw = store.value(PREFIX + f.name, default)
            try:
                if isinstance(default, bool):
                    values[f.name] = raw in (True, "true", "True", 1, "1")
                elif isinstance(default, int):
                    values[f.name] = int(raw)
                elif isinstance(default, float):
                    values[f.name] = float(raw)
                else:
                    values[f.name] = str(raw) if raw is not None else default
            except (TypeError, ValueError):
                values[f.name] = default
        return cls(**values)

    def save(self) -> None:
        store = QgsSettings()
        for key, value in asdict(self).items():
            store.setValue(PREFIX + key, value)

    def effective_output_dir(self) -> str:
        if self.output_dir:
            return self.output_dir
        return os.path.join(QgsApplication.qgisSettingsDirPath(), "qgissi", "outputs")

    def backup_dir(self) -> str:
        return os.path.join(QgsApplication.qgisSettingsDirPath(), "qgissi", "backups")

    def base_url(self, provider: str) -> str:
        return {"anthropic": self.anthropic_base_url, "openai": self.openai_base_url,
                "openai_compatible": self.compat_base_url, "ollama": self.ollama_base_url}.get(provider, "")


# ------------------------------------------------------------------ keys
def _auth_ready() -> bool:
    manager = QgsApplication.authManager()
    return manager is not None and not manager.isDisabled()


def store_api_key(provider: str, key: str) -> str:
    """Persist a key; returns 'auth-db' or 'settings' (unencrypted fallback)."""
    store = QgsSettings()
    if not key:
        store.remove(PREFIX + f"keys/{provider}")
        store.remove(PREFIX + f"authcfg/{provider}")
        return "removed"
    if _auth_ready():
        manager = QgsApplication.authManager()
        if manager.masterPasswordIsSet() or manager.setMasterPassword(True):
            config = QgsAuthMethodConfig("Basic")
            existing = store.value(PREFIX + f"authcfg/{provider}", "")
            if existing:
                config.setId(existing)
            config.setName(f"QGISSI {provider} API key")
            config.setConfig("username", provider)
            config.setConfig("password", key)
            ok = manager.updateAuthenticationConfig(config) if existing else manager.storeAuthenticationConfig(config)
            if ok:
                store.setValue(PREFIX + f"authcfg/{provider}", config.id())
                store.remove(PREFIX + f"keys/{provider}")
                return "auth-db"
    store.setValue(PREFIX + f"keys/{provider}", key)
    return "settings"


def load_api_key(provider: str) -> str:
    store = QgsSettings()
    authcfg = store.value(PREFIX + f"authcfg/{provider}", "")
    if authcfg and _auth_ready():
        config = QgsAuthMethodConfig()
        if QgsApplication.authManager().loadAuthenticationConfig(authcfg, config, True):
            value = config.config("password")
            if value:
                return value
    value = store.value(PREFIX + f"keys/{provider}", "")
    if value:
        return str(value)
    env = _ENV.get(provider)
    return os.environ.get(env, "") if env else ""


def key_source(provider: str) -> str:
    store = QgsSettings()
    if store.value(PREFIX + f"authcfg/{provider}", ""):
        return "QGIS auth database (encrypted)"
    if store.value(PREFIX + f"keys/{provider}", ""):
        return "QGIS settings (not encrypted)"
    env = _ENV.get(provider)
    if env and os.environ.get(env):
        return f"environment variable {env}"
    return "not set"


def provider_config(settings: PluginSettings, provider: Optional[str] = None) -> ProviderConfig:
    name = provider or settings.provider
    config = config_from_env(name)
    key = load_api_key(name)
    if key:
        config.api_key = key
    base = settings.base_url(name)
    if base:
        config.base_url = base
    config.enable_fallbacks = settings.enable_fallbacks
    config.num_ctx = settings.ollama_num_ctx
    return config
