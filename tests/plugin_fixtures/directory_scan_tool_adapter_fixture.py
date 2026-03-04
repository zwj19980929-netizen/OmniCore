from core.tool_adapters import BaseToolAdapter, tool_adapter
from core.tool_protocol import ToolSpec
from core.tool_registry import tool_plugin

PLUGIN_MANIFEST = {
    "plugin_id": "test.directory_fixture",
    "version": "2.1.0",
    "description": "Directory-scanned fixture plugin for adapter loading tests.",
    "dependencies": ["pytest"],
    "enabled": True,
}


@tool_plugin(
    spec=ToolSpec(
        name="plugin.directory_tool",
        task_type="plugin_directory_worker",
        description="A directory-scanned plugin tool used in tests.",
        risk_level="low",
        tags=["plugin", "directory", "test"],
    ),
    adapter_name="test.directory_scanned_plugin",
    max_parallelism=1,
    plugin_id="test.directory_fixture",
)
@tool_adapter("test.directory_scanned_plugin")
class DirectoryScannedPluginAdapter(BaseToolAdapter):
    async def execute(self, task, shared_memory_snapshot, registered_tool):
        return {
            "status": "completed",
            "result": {"success": True},
        }
