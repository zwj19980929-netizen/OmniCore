from pathlib import Path

from config.settings import settings
from core.constants import TaskType
from core.tool_registry import (
    build_dynamic_tool_prompt_lines,
    get_builtin_tool_registry,
    validate_plugin_manifest_dict,
)


def test_builtin_tool_registry_exposes_expected_task_types():
    registry = get_builtin_tool_registry()

    supported = set(registry.supported_task_types())

    assert str(TaskType.WEB_WORKER) in supported
    assert str(TaskType.BROWSER_AGENT) in supported
    assert str(TaskType.FILE_WORKER) in supported
    assert str(TaskType.SYSTEM_WORKER) in supported


def test_builtin_tool_registry_can_lookup_by_name_and_task_type():
    registry = get_builtin_tool_registry()

    web_tool = registry.get("web.fetch_and_extract")
    browser_tool = registry.get_by_task_type(str(TaskType.BROWSER_AGENT))

    assert web_tool is not None
    assert web_tool.adapter_name == "web_worker"
    assert web_tool.spec.task_type == str(TaskType.WEB_WORKER)
    assert browser_tool is not None
    assert browser_tool.spec.name == "browser.interact"


def test_web_fetch_and_extract_accepts_headless_flag():
    registry = get_builtin_tool_registry()

    web_tool = registry.get("web.fetch_and_extract")

    assert web_tool is not None
    assert "headless" in web_tool.spec.input_schema["properties"]


def test_builtin_tool_registry_can_resolve_task_by_tool_name_first():
    registry = get_builtin_tool_registry()

    resolved = registry.resolve_task(
        {
            "task_type": "legacy_unknown",
            "tool_name": "system.control",
        }
    )

    assert resolved is not None
    assert resolved.spec.task_type == str(TaskType.SYSTEM_WORKER)


def test_builtin_tool_registry_auto_registers_plugin_tools(monkeypatch):
    module_root = Path(__file__).parent / "plugin_fixtures"
    monkeypatch.syspath_prepend(str(module_root))
    monkeypatch.setattr(settings, "TOOL_ADAPTER_PLUGIN_MODULES", ("dynamic_tool_adapter_plugin_fixture",))
    monkeypatch.setattr(settings, "TOOL_ADAPTER_PLUGIN_DIRS", ())
    monkeypatch.setattr(settings, "ENABLED_TOOL_PLUGIN_IDS", ())
    monkeypatch.setattr(settings, "DISABLED_TOOL_PLUGIN_IDS", ())
    monkeypatch.setattr("core.tool_registry._builtin_registry", None)

    registry = get_builtin_tool_registry()
    plugin_tool = registry.get("plugin.dynamic_tool")

    assert plugin_tool is not None
    assert plugin_tool.adapter_name == "test.dynamic_plugin"
    assert plugin_tool.spec.task_type == "plugin_worker"


def test_dynamic_tool_prompt_lines_include_plugin_tools(monkeypatch):
    module_root = Path(__file__).parent / "plugin_fixtures"
    monkeypatch.syspath_prepend(str(module_root))
    monkeypatch.setattr(settings, "TOOL_ADAPTER_PLUGIN_MODULES", ("dynamic_tool_adapter_plugin_fixture",))
    monkeypatch.setattr(settings, "TOOL_ADAPTER_PLUGIN_DIRS", ())
    monkeypatch.setattr(settings, "ENABLED_TOOL_PLUGIN_IDS", ())
    monkeypatch.setattr(settings, "DISABLED_TOOL_PLUGIN_IDS", ())
    monkeypatch.setattr("core.tool_registry._builtin_registry", None)

    lines = build_dynamic_tool_prompt_lines()

    assert any("plugin.dynamic_tool" in line for line in lines)


def test_disabled_plugin_ids_are_excluded_from_tool_registry(monkeypatch):
    module_root = Path(__file__).parent / "plugin_fixtures"
    monkeypatch.syspath_prepend(str(module_root))
    monkeypatch.setattr(settings, "TOOL_ADAPTER_PLUGIN_MODULES", ("dynamic_tool_adapter_plugin_fixture",))
    monkeypatch.setattr(settings, "TOOL_ADAPTER_PLUGIN_DIRS", ())
    monkeypatch.setattr(settings, "ENABLED_TOOL_PLUGIN_IDS", ())
    monkeypatch.setattr(settings, "DISABLED_TOOL_PLUGIN_IDS", ("test.dynamic_fixture",))
    monkeypatch.setattr("core.tool_registry._builtin_registry", None)

    registry = get_builtin_tool_registry()

    assert registry.get("plugin.dynamic_tool") is None


def test_enabled_plugin_allowlist_filters_other_plugins(monkeypatch):
    module_root = Path(__file__).parent / "plugin_fixtures"
    monkeypatch.syspath_prepend(str(module_root))
    monkeypatch.setattr(
        settings,
        "TOOL_ADAPTER_PLUGIN_MODULES",
        ("dynamic_tool_adapter_plugin_fixture",),
    )
    monkeypatch.setattr(settings, "TOOL_ADAPTER_PLUGIN_DIRS", (str(module_root),))
    monkeypatch.setattr(settings, "ENABLED_TOOL_PLUGIN_IDS", ("test.directory_fixture",))
    monkeypatch.setattr(settings, "DISABLED_TOOL_PLUGIN_IDS", ())
    monkeypatch.setattr("core.tool_registry._builtin_registry", None)

    registry = get_builtin_tool_registry()

    assert registry.get("plugin.dynamic_tool") is None
    assert registry.get("plugin.directory_tool") is not None


def test_plugin_manifest_validation_rejects_invalid_identifier():
    try:
        validate_plugin_manifest_dict({"plugin_id": "bad id"})
    except ValueError as exc:
        assert "Invalid plugin_id" in str(exc)
    else:
        raise AssertionError("Expected invalid plugin_id to raise ValueError")
