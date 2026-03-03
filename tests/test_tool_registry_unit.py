from core.constants import TaskType
from core.tool_registry import get_builtin_tool_registry


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
