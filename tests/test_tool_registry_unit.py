from pathlib import Path

import pytest

from config.settings import settings
from core.constants import TaskType, TaskOutputType
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


# ── R4: 工具能力标签化 ─────────────────────────────────────────────────────────

def test_all_builtin_tools_have_capability_fields():
    """所有内置工具的 ToolSpec 都有结构化能力字段。"""
    registry = get_builtin_tool_registry()
    for tool in registry.list_tools():
        spec = tool.spec
        assert hasattr(spec, "concurrent_safe"), f"{spec.name} 缺少 concurrent_safe"
        assert hasattr(spec, "output_type"), f"{spec.name} 缺少 output_type"
        assert hasattr(spec, "needs_network"), f"{spec.name} 缺少 needs_network"
        assert hasattr(spec, "destructive"), f"{spec.name} 缺少 destructive"
        assert isinstance(spec.concurrent_safe, bool), f"{spec.name}.concurrent_safe 非 bool"
        assert isinstance(spec.output_type, str), f"{spec.name}.output_type 非 str"
        assert isinstance(spec.needs_network, bool), f"{spec.name}.needs_network 非 bool"
        assert isinstance(spec.destructive, bool), f"{spec.name}.destructive 非 bool"


def test_web_tools_are_concurrent_safe_and_network():
    registry = get_builtin_tool_registry()
    for tool_name in ("web.fetch_and_extract", "web.smart_extract"):
        tool = registry.get(tool_name)
        assert tool is not None
        assert tool.spec.concurrent_safe is True, f"{tool_name} 应为 concurrent_safe"
        assert tool.spec.needs_network is True, f"{tool_name} 应声明 needs_network"
        assert tool.spec.output_type == str(TaskOutputType.TEXT_EXTRACTION)


def test_browser_tool_is_not_concurrent_safe():
    registry = get_builtin_tool_registry()
    tool = registry.get("browser.interact")
    assert tool is not None
    assert tool.spec.concurrent_safe is False
    assert tool.spec.needs_network is True
    assert tool.spec.output_type == str(TaskOutputType.TEXT_EXTRACTION)


def test_file_tool_is_destructive():
    registry = get_builtin_tool_registry()
    tool = registry.get("file.read_write")
    assert tool is not None
    assert tool.spec.destructive is True
    assert tool.spec.output_type == str(TaskOutputType.FILE_DOWNLOAD)


def test_system_and_terminal_execute_are_not_concurrent_safe_and_destructive():
    registry = get_builtin_tool_registry()
    for tool_name in ("system.control", "terminal.execute", "terminal.edit_file"):
        tool = registry.get(tool_name)
        assert tool is not None, f"{tool_name} 未注册"
        assert tool.spec.concurrent_safe is False, f"{tool_name} 应为非 concurrent_safe"
        assert tool.spec.destructive is True, f"{tool_name} 应声明 destructive"
        assert tool.spec.output_type == str(TaskOutputType.COMMAND_OUTPUT)


def test_terminal_read_and_search_are_concurrent_safe():
    registry = get_builtin_tool_registry()
    for tool_name in ("terminal.read_file", "terminal.search"):
        tool = registry.get(tool_name)
        assert tool is not None, f"{tool_name} 未注册"
        assert tool.spec.concurrent_safe is True, f"{tool_name} 应为 concurrent_safe"
        assert tool.spec.destructive is False, f"{tool_name} 不应声明 destructive"


def test_extract_typed_output_uses_spec_output_type_not_string_heuristic():
    """_extract_typed_output 应通过 ToolSpec.output_type 而非工具名字符串判断类型。"""
    from core.task_executor import _extract_typed_output

    # 使用真实注册的工具，output_type="text_extraction"
    task = {"tool_name": "web.fetch_and_extract", "task_type": "web_worker"}
    outcome = {"result": {"extracted_text": "hello world", "url": "https://example.com"}}
    result = _extract_typed_output(task, outcome)
    assert result is not None
    assert result["type"] == str(TaskOutputType.TEXT_EXTRACTION)
    assert result["content"] == "hello world"

    # file 工具有 file_path → FILE_DOWNLOAD
    task_file = {"tool_name": "file.read_write", "task_type": "file_worker"}
    outcome_file = {"result": {"file_path": "/tmp/output.csv", "file_size": 1024}}
    result_file = _extract_typed_output(task_file, outcome_file)
    assert result_file is not None
    assert result_file["type"] == str(TaskOutputType.FILE_DOWNLOAD)
    assert result_file["file_path"] == "/tmp/output.csv"

    # file 工具无 file_path → 降级为 TEXT_EXTRACTION
    outcome_file_text = {"result": {"content": "file contents"}}
    result_file_text = _extract_typed_output(task_file, outcome_file_text)
    assert result_file_text is not None
    assert result_file_text["type"] == str(TaskOutputType.TEXT_EXTRACTION)

    # terminal 工具 → COMMAND_OUTPUT
    task_term = {"tool_name": "terminal.execute", "task_type": "terminal_worker"}
    outcome_term = {"result": {"output": "ls output", "returncode": 0}}
    result_term = _extract_typed_output(task_term, outcome_term)
    assert result_term is not None
    assert result_term["type"] == str(TaskOutputType.COMMAND_OUTPUT)
    assert result_term["stdout"] == "ls output"


def test_select_batch_indexes_respects_concurrent_safe():
    """concurrent_safe=False 的工具不应与其他任务混入同一批次。"""
    from core.task_executor import _select_batch_indexes
    from core.constants import TaskStatus

    # 构造一个最简 state：第一个任务是 concurrent_safe=False（browser），第二个是 web
    state = {
        "task_queue": [
            {
                "task_id": "t1",
                "tool_name": "browser.interact",
                "task_type": "browser_agent",
                "status": str(TaskStatus.PENDING),
                "params": {},
                "estimated_cost": "medium",
            },
            {
                "task_id": "t2",
                "tool_name": "web.fetch_and_extract",
                "task_type": "web_worker",
                "status": str(TaskStatus.PENDING),
                "params": {},
                "estimated_cost": "low",
            },
        ],
        "message_bus": [],
        "task_outputs": {},
    }
    monkeypatch_settings = type("S", (), {"ENABLE_PARALLEL_EXECUTION": True, "MAX_PARALLEL_TASKS": 4})()

    import core.task_executor as te
    original_settings = te.settings
    te.settings = monkeypatch_settings
    try:
        batch = _select_batch_indexes(state, [0, 1])
    finally:
        te.settings = original_settings

    # browser 是 concurrent_safe=False，所以批次里只有它自己
    assert batch == [0], f"期望只有 browser 任务，实际: {batch}"
