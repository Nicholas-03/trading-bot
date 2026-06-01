import config as config_module


def _base_env(monkeypatch):
    monkeypatch.setattr(config_module, "load_dotenv", lambda: None)
    for key in (
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "LLM_PROVIDER",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "TELEGRAM_ENABLED",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ALPACA_API_KEY", "alpaca-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "alpaca-secret")


def test_load_config_accepts_chatgpt_provider(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "chatgpt")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-openai-model")

    cfg = config_module.load_config()

    assert cfg.llm_provider == "chatgpt"
    assert cfg.openai_api_key == "openai-key"
    assert cfg.openai_model == "test-openai-model"


def test_load_config_defaults_to_chatgpt(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

    cfg = config_module.load_config()

    assert cfg.llm_provider == "chatgpt"


def test_load_config_requires_openai_key_for_chatgpt(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "chatgpt")

    try:
        config_module.load_config()
    except ValueError as exc:
        assert "OPENAI_API_KEY" in str(exc)
    else:
        raise AssertionError("load_config should require OPENAI_API_KEY for chatgpt")
