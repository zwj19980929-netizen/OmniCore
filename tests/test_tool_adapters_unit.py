import shutil
import uuid
import asyncio
from pathlib import Path
from types import SimpleNamespace

import core.tool_adapters as tool_adapters_module
import utils.browser_toolkit as browser_toolkit_module
from config.settings import settings
from core.tool_adapters import (
    BaseToolAdapter,
    ToolAdapterRegistry,
    build_tool_adapter_registry,
    disable_tool_plugin,
    enable_tool_plugin,
    get_tool_adapter_plugin_status,
    install_tool_plugin_module,
    register_tool_adapter_class,
    uninstall_tool_plugin,
)
from core.tool_registry import get_builtin_tool_registry
from utils.tool_plugin_store import ToolPluginStore


class _CustomAdapter(BaseToolAdapter):
    async def execute(self, task, shared_memory_snapshot, registered_tool):
        return {
            "status": "completed",
            "result": {"success": True},
        }


def _make_plugin_store():
    root = Path.cwd() / "data" / f"test_tool_plugin_store_{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True, exist_ok=True)
    return root, ToolPluginStore(config_path=root / "tool_plugins.json")


def test_builtin_tool_adapters_are_available_without_manual_builder_wiring():
    registry = build_tool_adapter_registry()

    web_adapter = registry.get("web_worker")
    browser_adapter = registry.get("browser_agent")

    assert web_adapter is not None
    assert web_adapter.__class__.__name__ == "WebWorkerAdapter"
    assert browser_adapter is not None
    assert browser_adapter.__class__.__name__ == "BrowserAgentAdapter"


def test_custom_tool_adapter_classes_can_self_register_and_are_cached_per_registry():
    register_tool_adapter_class("test.custom_adapter", _CustomAdapter)
    registry = ToolAdapterRegistry()

    first = registry.get("test.custom_adapter")
    second = registry.get("test.custom_adapter")

    assert isinstance(first, _CustomAdapter)
    assert first is second


def test_configured_tool_adapter_plugin_modules_are_loaded_automatically(monkeypatch):
    module_name = "dynamic_tool_adapter_plugin_fixture"
    module_root = Path(__file__).parent / "plugin_fixtures"
    monkeypatch.syspath_prepend(str(module_root))
    monkeypatch.setattr(settings, "TOOL_ADAPTER_PLUGIN_MODULES", (module_name,))
    monkeypatch.setattr(settings, "TOOL_ADAPTER_PLUGIN_DIRS", ())
    monkeypatch.setattr(settings, "ENABLED_TOOL_PLUGIN_IDS", ())
    monkeypatch.setattr(settings, "DISABLED_TOOL_PLUGIN_IDS", ())

    registry = build_tool_adapter_registry()
    adapter = registry.get("test.dynamic_plugin")
    status = get_tool_adapter_plugin_status()

    assert adapter is not None
    assert adapter.__class__.__name__ == "DynamicPluginAdapter"
    assert module_name in status["loaded_modules"]
    assert "plugin.dynamic_tool" in status["registered_tools"]
    manifest = next(item for item in status["plugin_manifests"] if item["plugin_id"] == "test.dynamic_fixture")
    assert manifest["version"] == "1.0.0"
    assert manifest["enabled"] is True


def test_tool_adapter_plugin_directories_are_scanned(monkeypatch):
    module_root = Path(__file__).parent / "plugin_fixtures"
    monkeypatch.setattr(settings, "TOOL_ADAPTER_PLUGIN_MODULES", ())
    monkeypatch.setattr(settings, "TOOL_ADAPTER_PLUGIN_DIRS", (str(module_root),))
    monkeypatch.setattr(settings, "ENABLED_TOOL_PLUGIN_IDS", ())
    monkeypatch.setattr(settings, "DISABLED_TOOL_PLUGIN_IDS", ())

    registry = build_tool_adapter_registry()
    adapter = registry.get("test.directory_scanned_plugin")
    status = get_tool_adapter_plugin_status()

    assert adapter is not None
    assert adapter.__class__.__name__ == "DirectoryScannedPluginAdapter"
    assert any(str(module_root) in item for item in status["configured_directories"])
    assert any("directory_scan_tool_adapter_fixture.py" in item for item in status["loaded_files"])
    assert "plugin.directory_tool" in status["registered_tools"]
    manifest = next(item for item in status["plugin_manifests"] if item["plugin_id"] == "test.directory_fixture")
    assert manifest["enabled"] is True
    assert "plugin.directory_tool" in manifest["tools"]


def test_plugin_status_reflects_disabled_manifest_state(monkeypatch):
    module_root = Path(__file__).parent / "plugin_fixtures"
    monkeypatch.syspath_prepend(str(module_root))
    monkeypatch.setattr(settings, "TOOL_ADAPTER_PLUGIN_MODULES", ("dynamic_tool_adapter_plugin_fixture",))
    monkeypatch.setattr(settings, "TOOL_ADAPTER_PLUGIN_DIRS", ())
    monkeypatch.setattr(settings, "ENABLED_TOOL_PLUGIN_IDS", ())
    monkeypatch.setattr(settings, "DISABLED_TOOL_PLUGIN_IDS", ("test.dynamic_fixture",))

    build_tool_adapter_registry()
    status = get_tool_adapter_plugin_status()

    manifest = next(item for item in status["plugin_manifests"] if item["plugin_id"] == "test.dynamic_fixture")
    assert manifest["enabled"] is False


def test_plugin_management_flow_updates_store_and_registry(monkeypatch):
    module_root = Path(__file__).parent / "plugin_fixtures"
    store_root, store = _make_plugin_store()

    try:
        monkeypatch.syspath_prepend(str(module_root))
        monkeypatch.setattr(settings, "TOOL_ADAPTER_PLUGIN_MODULES", ())
        monkeypatch.setattr(settings, "TOOL_ADAPTER_PLUGIN_DIRS", ())
        monkeypatch.setattr(settings, "ENABLED_TOOL_PLUGIN_IDS", ())
        monkeypatch.setattr(settings, "DISABLED_TOOL_PLUGIN_IDS", ())
        monkeypatch.setattr("utils.tool_plugin_store._tool_plugin_store", store, raising=False)

        install_status = install_tool_plugin_module("dynamic_tool_adapter_plugin_fixture")
        enabled_registry = get_builtin_tool_registry()
        disable_status = disable_tool_plugin("test.dynamic_fixture")
        disabled_registry = get_builtin_tool_registry()
        reenabled_status = enable_tool_plugin("test.dynamic_fixture")
        reenabled_registry = get_builtin_tool_registry()
        uninstall_status = uninstall_tool_plugin("test.dynamic_fixture")
        uninstalled_registry = get_builtin_tool_registry()

        assert "dynamic_tool_adapter_plugin_fixture" in install_status["installed_modules"]
        assert enabled_registry.get("plugin.dynamic_tool") is not None
        disabled_manifest = next(
            item for item in disable_status["plugin_manifests"]
            if item["plugin_id"] == "test.dynamic_fixture"
        )
        assert disabled_manifest["enabled"] is False
        assert disabled_registry.get("plugin.dynamic_tool") is None
        assert reenabled_registry.get("plugin.dynamic_tool") is not None
        assert "test.dynamic_fixture" in uninstall_status["disabled_plugin_ids"]
        assert uninstalled_registry.get("plugin.dynamic_tool") is None
    finally:
        shutil.rmtree(store_root, ignore_errors=True)


def test_api_worker_adapter_waits_for_approval_before_mutating_calls():
    registry = build_tool_adapter_registry()
    adapter = registry.get("api_worker")
    tool = get_builtin_tool_registry().get("api.call")

    outcome = asyncio.run(
        adapter.execute(
            {
                "task_id": "task_api",
                "task_type": "api_worker",
                "tool_name": "api.call",
                "description": "send a webhook",
                "params": {"method": "POST", "url": "https://example.com/webhook"},
                "execution_trace": [],
            },
            {},
            tool,
        )
    )

    assert outcome["status"] == "waiting_for_approval"
    assert outcome["result"]["approval_required"] is True


def test_browser_agent_adapter_closes_toolkit_it_created(monkeypatch):
    created_toolkits = []

    class FakeToolkit:
        def __init__(self, **_kwargs):
            self.closed = False
            created_toolkits.append(self)

        async def close(self):
            self.closed = True
            return SimpleNamespace(success=True, error=None)

    class FakeAgent:
        def __init__(self, toolkit):
            self.toolkit = toolkit

        async def run(self, _task_desc, _start_url, max_steps=8):
            assert max_steps == 6
            return {"success": True, "message": "ok", "steps": []}

        async def close(self):
            return None

    class FakeWorkerPool:
        def create_browser_agent(self, llm_client=None, headless=True, toolkit=None):
            assert llm_client is None
            assert headless is True
            return FakeAgent(toolkit)

    async def _fake_get_instance(_cls):
        return FakeWorkerPool()

    monkeypatch.setattr(browser_toolkit_module, "BrowserToolkit", FakeToolkit)
    monkeypatch.setattr(tool_adapters_module, "resolve_model_for_task", lambda _task: None)
    monkeypatch.setattr(tool_adapters_module.WorkerPool, "get_instance", classmethod(_fake_get_instance))

    registry = build_tool_adapter_registry()
    adapter = registry.get("browser_agent")
    tool = get_builtin_tool_registry().get("browser.interact")

    outcome = asyncio.run(
        adapter.execute(
            {
                "task_id": "task_browser",
                "task_type": "browser_agent",
                "tool_name": "browser.interact",
                "description": "search latest news",
                "params": {"task": "search latest news", "headless": True, "max_steps": 6},
                "execution_trace": [],
            },
            {},
            tool,
        )
    )

    assert outcome["status"] == "completed"
    assert outcome["result"]["success"] is True
    assert len(created_toolkits) == 1
    assert created_toolkits[0].closed is True
