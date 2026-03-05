from core.llm import LLMClient
from core.model_discovery import ModelDiscovery
from core.model_registry import ModelRegistry
from config.settings import settings


def test_llm_detects_minimax_provider_from_prefixed_model():
    client = LLMClient(model="minimax/MiniMax-M2.5")

    assert client._get_provider_from_model() == "minimax"
    assert client._get_litellm_model() == "openai/MiniMax-M2.5"


def test_llm_detects_minimax_provider_from_abab_model_id():
    client = LLMClient(model="abab6.5s-chat")

    assert client._get_provider_from_model() == "minimax"
    assert client._get_litellm_model() == "openai/abab6.5s-chat"


def test_llm_minimax_extra_kwargs_respect_provider_config(monkeypatch):
    monkeypatch.setattr(settings, "MINIMAX_API_KEY", "")
    monkeypatch.setenv("UNITTEST_MINIMAX_KEY", "minimax-test-key")

    client = LLMClient(model="minimax/MiniMax-M2.5")
    client.provider_config = {
        "minimax": {
            "api_key_env": "UNITTEST_MINIMAX_KEY",
            "api_base": "https://api.minimax.example/v1",
        }
    }

    kwargs = client._get_extra_kwargs()

    assert kwargs["api_base"] == "https://api.minimax.example/v1"
    assert kwargs["api_key"] == "minimax-test-key"


def test_model_discovery_supports_minimax_openai_compatible_payload(monkeypatch):
    discovery = ModelDiscovery(
        api_keys={"minimax": "dummy"},
        provider_config={"minimax": {"disable_model_discovery": False}},
    )

    monkeypatch.setattr(
        discovery,
        "_request_models",
        lambda provider, default_endpoint: {"data": [{"id": "MiniMax-M2.5"}]},
    )

    models = discovery.list_models("minimax")

    assert len(models) == 1
    assert models[0].provider == "minimax"
    assert models[0].id == "MiniMax-M2.5"


def test_model_discovery_minimax_falls_back_when_models_endpoint_unavailable(monkeypatch):
    discovery = ModelDiscovery(
        api_keys={"minimax": "dummy"},
        provider_config={
            "minimax": {
                "disable_model_discovery": False,
                "static_models": ["MiniMax-M2.5", "MiniMax-M2.1"],
            }
        },
    )

    monkeypatch.setattr(discovery, "_request_models", lambda provider, default_endpoint: None)

    models = discovery.list_models("minimax")

    assert len(models) == 2
    assert any(item.id == "MiniMax-M2.5" for item in models)


def test_model_discovery_minimax_static_mode_skips_models_endpoint(monkeypatch):
    discovery = ModelDiscovery(
        api_keys={"minimax": "dummy"},
        provider_config={
            "minimax": {
                "disable_model_discovery": True,
                "static_models": ["MiniMax-M2.5", "MiniMax-M2.5-highspeed"],
            }
        },
    )

    monkeypatch.setattr(
        discovery,
        "_request_models",
        lambda provider, default_endpoint: (_ for _ in ()).throw(AssertionError("should not call /models")),
    )

    models = discovery.list_models("minimax")

    assert [item.id for item in models] == ["MiniMax-M2.5", "MiniMax-M2.5-highspeed"]


def test_model_registry_supports_minimax_provider():
    assert "minimax" in ModelRegistry.SUPPORTED_PROVIDERS
