"""Plugin settings and API-key storage (isolated test profile)."""


def test_settings_round_trip(qgis_app):
    from qgissi.core.settings import PluginSettings
    settings = PluginSettings.load()
    settings.model, settings.effort, settings.max_cost_usd, settings.allow_network = "claude-sonnet-5-5", "high", 1.5, False
    settings.save()
    loaded = PluginSettings.load()
    assert (loaded.model, loaded.effort, loaded.max_cost_usd, loaded.allow_network) == ("claude-sonnet-5-5", "high", 1.5, False)


def test_api_key_goes_to_encrypted_auth_db(qgis_app, monkeypatch):
    from qgis.core import QgsApplication

    from qgissi.core.settings import key_source, load_api_key, provider_config, store_api_key, PluginSettings
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    manager = QgsApplication.authManager()
    if not manager.masterPasswordIsSet():
        assert manager.setMasterPassword("qgissi-test-master", True)
    assert store_api_key("anthropic", "sk-ant-test-123") == "auth-db"
    assert load_api_key("anthropic") == "sk-ant-test-123"
    assert key_source("anthropic").startswith("QGIS auth database")
    assert provider_config(PluginSettings(), "anthropic").api_key == "sk-ant-test-123"
    assert store_api_key("anthropic", "sk-ant-test-456") == "auth-db"  # update in place
    assert load_api_key("anthropic") == "sk-ant-test-456"
    assert store_api_key("anthropic", "") == "removed" and load_api_key("anthropic") == ""


def test_environment_key_is_the_fallback(qgis_app, monkeypatch):
    from qgissi.core.settings import key_source, load_api_key
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-key")
    assert load_api_key("openai") == "sk-env-key" and key_source("openai") == "environment variable OPENAI_API_KEY"
