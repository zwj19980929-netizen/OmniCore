import importlib
import os

settings_module = importlib.import_module("config.settings")


def test_runtime_metrics_override_file_only_applies_tuning_keys(tmp_path, monkeypatch):
    """The override loader should propagate keys in ``RUNTIME_METRICS_TUNING_KEYS``
    to ``os.environ`` while leaving unrelated keys (e.g. ``DEFAULT_MODEL``) alone.

    We test the loader function directly instead of reloading the settings
    module: ``importlib.reload`` would create a fresh ``Settings`` instance,
    but other modules that previously did ``from config.settings import settings``
    still hold a reference to the old instance, which silently breaks
    downstream tests that monkeypatch ``config.settings.settings``.
    """
    override_path = tmp_path / "override.env"
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

    monkeypatch.setattr(settings_module, "RUNTIME_METRICS_OVERRIDE_PATH", override_path)
    monkeypatch.delenv("URL_ANALYSIS_CACHE_TTL_SECONDS", raising=False)
    monkeypatch.delenv("BROWSER_POOL_MAX_BROWSERS_PER_KEY", raising=False)
    monkeypatch.delenv("LLM_CACHE_URL_ANALYSIS_MAX_ENTRIES", raising=False)
    monkeypatch.setenv("DEFAULT_MODEL", "base-model")

    settings_module._load_runtime_metrics_overrides()

    # Keys listed in RUNTIME_METRICS_TUNING_KEYS were propagated to the env
    assert os.environ.get("URL_ANALYSIS_CACHE_TTL_SECONDS") == "321"
    assert os.environ.get("BROWSER_POOL_MAX_BROWSERS_PER_KEY") == "2"
    assert os.environ.get("LLM_CACHE_URL_ANALYSIS_MAX_ENTRIES") == "64"
    # DEFAULT_MODEL is NOT in the tuning allowlist, so the loader must leave
    # the existing env value alone.
    assert os.environ.get("DEFAULT_MODEL") == "base-model"


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
