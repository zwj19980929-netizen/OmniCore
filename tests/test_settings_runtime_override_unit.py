import importlib
import os

settings_module = importlib.import_module("config.settings")


def test_runtime_metrics_override_file_only_applies_tuning_keys():
    override_path = settings_module.DATA_DIR / "test_runtime_metrics_override_loader.env"
    override_path.parent.mkdir(parents=True, exist_ok=True)
    override_path.write_text(
        "\n".join(
            [
                "# runtime tuning override",
                "URL_ANALYSIS_CACHE_TTL_SECONDS=321",
                "BROWSER_POOL_MAX_BROWSERS_PER_KEY=2",
                "LLM_CACHE_URL_ANALYSIS_MAX_ENTRIES=64",
                "DEFAULT_MODEL=should-not-apply",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    original_override_path = os.environ.get("RUNTIME_METRICS_OVERRIDE_PATH")
    original_url_ttl = os.environ.get("URL_ANALYSIS_CACHE_TTL_SECONDS")
    original_default_model = os.environ.get("DEFAULT_MODEL")

    try:
        os.environ["RUNTIME_METRICS_OVERRIDE_PATH"] = str(override_path)
        os.environ.pop("URL_ANALYSIS_CACHE_TTL_SECONDS", None)
        os.environ["DEFAULT_MODEL"] = "base-model"

        importlib.reload(settings_module)

        assert settings_module.settings.URL_ANALYSIS_CACHE_TTL_SECONDS == 321
        assert settings_module.settings.BROWSER_POOL_MAX_BROWSERS_PER_KEY == 2
        assert settings_module.settings.LLM_CACHE_URL_ANALYSIS_MAX_ENTRIES == 64
        assert settings_module.settings.DEFAULT_MODEL == "base-model"
    finally:
        if original_override_path is None:
            os.environ.pop("RUNTIME_METRICS_OVERRIDE_PATH", None)
        else:
            os.environ["RUNTIME_METRICS_OVERRIDE_PATH"] = original_override_path

        if original_url_ttl is None:
            os.environ.pop("URL_ANALYSIS_CACHE_TTL_SECONDS", None)
        else:
            os.environ["URL_ANALYSIS_CACHE_TTL_SECONDS"] = original_url_ttl

        if original_default_model is None:
            os.environ.pop("DEFAULT_MODEL", None)
        else:
            os.environ["DEFAULT_MODEL"] = original_default_model

        importlib.reload(settings_module)


def test_managed_proxy_env_ignores_system_proxy_by_default_and_applies_project_proxy():
    tracked_keys = [
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    ]
    original = {key: os.environ.get(key) for key in tracked_keys}

    try:
        os.environ["HTTP_PROXY"] = "http://127.0.0.1:9"
        os.environ["HTTPS_PROXY"] = "http://127.0.0.1:9"
        os.environ["ALL_PROXY"] = "http://127.0.0.1:9"

        settings_module._apply_managed_proxy_env(
            allow_system_proxy=False,
            https_proxy="http://127.0.0.1:7890",
            no_proxy="localhost,127.0.0.1",
        )

        assert os.environ.get("HTTP_PROXY", "") == ""
        assert os.environ.get("http_proxy", "") == ""
        assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:7890"
        assert os.environ["https_proxy"] == "http://127.0.0.1:7890"
        assert os.environ.get("ALL_PROXY", "") == ""
        assert os.environ["NO_PROXY"] == "localhost,127.0.0.1"
        assert os.environ["no_proxy"] == "localhost,127.0.0.1"
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
