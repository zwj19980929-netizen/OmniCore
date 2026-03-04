from core.tool_adapters import BaseToolAdapter, tool_adapter
from core.tool_protocol import ToolSpec
from core.tool_registry import tool_plugin

PLUGIN_MANIFEST = {
    "plugin_id": "test.dynamic_fixture",
    "version": "1.0.0",
    "description": "Dynamic fixture plugin for adapter loading tests.",
    "dependencies": ["pytest", "streamlit"],
    "enabled": True,
}


@tool_plugin(
    spec=ToolSpec(
        name="plugin.dynamic_tool",
        task_type="plugin_worker",
        description="A dynamically loaded plugin tool used in tests.",
        risk_level="low",
        tags=["plugin", "test"],
    ),
    adapter_name="test.dynamic_plugin",
    max_parallelism=2,
    plugin_id="test.dynamic_fixture",
)
@tool_adapter("test.dynamic_plugin")
class DynamicPluginAdapter(BaseToolAdapter):
    async def execute(self, task, shared_memory_snapshot, registered_tool):
        return {
            "status": "completed",
            "result": {"success": True},
        }
