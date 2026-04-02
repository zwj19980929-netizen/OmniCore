"""
Initial tool registry bridging the current fixed worker model to a future
tool-centric runtime.
"""
from __future__ import annotations

import asyncio
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from config.settings import settings
from core.constants import TaskType
from core.tool_protocol import ToolSpec


@dataclass(frozen=True)
class RegisteredTool:
    """Registry entry for a tool capability."""

    spec: ToolSpec
    adapter_name: str
    max_parallelism: int = 1
    serialized: bool = False
    plugin_id: str = ""


@dataclass(frozen=True)
class PluginManifest:
    """Metadata describing a plugin package and its governance state."""

    plugin_id: str
    version: str = ""
    description: str = ""
    dependencies: List[str] = field(default_factory=list)
    source: str = ""
    enabled: bool = True


class ToolRegistry:
    """Simple in-memory registry for available tool capabilities."""

    def __init__(self):
        self._by_name: Dict[str, RegisteredTool] = {}
        self._by_task_type: Dict[str, RegisteredTool] = {}

    def register(self, tool: RegisteredTool) -> None:
        self._by_name[tool.spec.name] = tool
        if tool.spec.task_type:
            self._by_task_type[tool.spec.task_type] = tool

    def get(self, name: str) -> Optional[RegisteredTool]:
        return self._by_name.get(name)

    def get_by_task_type(self, task_type: str) -> Optional[RegisteredTool]:
        if not task_type:
            return None
        return self._by_task_type.get(str(task_type))

    def resolve_identifier(self, identifier: str) -> Optional[RegisteredTool]:
        token = str(identifier or "").strip()
        if not token:
            return None
        return self.get(token) or self.get_by_task_type(token)

    def resolve_task(self, task: Dict[str, Any]) -> Optional[RegisteredTool]:
        tool_name = str(task.get("tool_name", "") or "").strip()
        if tool_name:
            resolved = self.get(tool_name)
            if resolved is not None:
                return resolved
        return self.get_by_task_type(str(task.get("task_type", "") or ""))

    def list_tools(self) -> List[RegisteredTool]:
        return list(self._by_name.values())

    def supported_task_types(self) -> List[str]:
        return list(self._by_task_type.keys())


_plugin_tools: Dict[str, RegisteredTool] = {}
_plugin_tools_lock = threading.Lock()
_plugin_manifests: Dict[str, PluginManifest] = {}
_plugin_manifests_lock = threading.Lock()
_PLUGIN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_PLUGIN_VERSION_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]+){0,2}(?:[-.][A-Za-z0-9_.-]+)?$")


def register_plugin_tool(tool: RegisteredTool) -> None:
    """Register a plugin-provided tool definition for future registry builds."""
    with _plugin_tools_lock:
        _plugin_tools[tool.spec.name] = tool


def tool_plugin(
    *,
    spec: ToolSpec,
    adapter_name: str,
    max_parallelism: int = 1,
    serialized: bool = False,
    plugin_id: str = "",
):
    """Decorator for plugin modules to self-register a tool definition."""
    registered = RegisteredTool(
        spec=spec,
        adapter_name=adapter_name,
        max_parallelism=max_parallelism,
        serialized=serialized,
        plugin_id=str(plugin_id or "").strip(),
    )

    def decorator(obj):
        register_plugin_tool(registered)
        return obj

    return decorator


def register_plugin_manifest(manifest: PluginManifest) -> None:
    """Register plugin package metadata for governance and UI introspection."""
    plugin_id = str(manifest.plugin_id or "").strip()
    if not plugin_id:
        return
    with _plugin_manifests_lock:
        _plugin_manifests[plugin_id] = manifest


def register_plugin_manifest_dict(manifest: Dict[str, Any], *, source: str = "") -> None:
    """Register a plugin manifest from a plain dict exported by a plugin module."""
    validated = validate_plugin_manifest_dict(manifest, source=source)
    if validated is None:
        return

    register_plugin_manifest(validated)


def validate_plugin_manifest_dict(
    manifest: Dict[str, Any],
    *,
    source: str = "",
) -> Optional[PluginManifest]:
    """Validate and normalize a plugin manifest dict."""
    if not isinstance(manifest, dict):
        return None

    plugin_id = str(manifest.get("plugin_id", "") or "").strip()
    if not plugin_id:
        return None
    if not _PLUGIN_ID_PATTERN.match(plugin_id):
        raise ValueError(f"Invalid plugin_id: {plugin_id}")

    dependencies = manifest.get("dependencies", [])
    if not isinstance(dependencies, list):
        dependencies = [str(dependencies)]
    normalized_dependencies = [str(item).strip() for item in dependencies if str(item).strip()]
    if any(not _PLUGIN_ID_PATTERN.match(item) for item in normalized_dependencies):
        raise ValueError(f"Invalid dependency identifier in plugin {plugin_id}")

    version = str(manifest.get("version", "") or "").strip()
    if version and not _PLUGIN_VERSION_PATTERN.match(version):
        raise ValueError(f"Invalid plugin version for {plugin_id}: {version}")

    enabled_value = manifest.get("enabled", True)
    if isinstance(enabled_value, bool):
        enabled = enabled_value
    else:
        enabled = str(enabled_value).strip().lower() not in {"0", "false", "no", "off"}

    return PluginManifest(
        plugin_id=plugin_id,
        version=version,
        description=str(manifest.get("description", "") or "").strip(),
        dependencies=normalized_dependencies,
        source=str(source or manifest.get("source", "") or "").strip(),
        enabled=enabled,
    )


def reset_builtin_tool_registry() -> None:
    """Drop the cached registry so plugin state changes take effect immediately."""
    global _builtin_registry
    with _builtin_registry_lock:
        _builtin_registry = None


def list_registered_plugin_tools() -> List[RegisteredTool]:
    with _plugin_tools_lock:
        return list(_plugin_tools.values())


def list_registered_plugin_manifests() -> List[PluginManifest]:
    with _plugin_manifests_lock:
        return list(_plugin_manifests.values())


def is_plugin_enabled(plugin_id: str) -> bool:
    token = str(plugin_id or "").strip()
    if not token:
        return True

    allowlist = {item for item in settings.ENABLED_TOOL_PLUGIN_IDS if item}
    denylist = {item for item in settings.DISABLED_TOOL_PLUGIN_IDS if item}
    try:
        from utils.tool_plugin_store import get_tool_plugin_store

        denylist.update(
            item
            for item in get_tool_plugin_store().get_config().get("disabled_plugin_ids", [])
            if item
        )
    except Exception:
        pass
    if token in denylist:
        return False
    if allowlist and token not in allowlist:
        return False

    with _plugin_manifests_lock:
        manifest = _plugin_manifests.get(token)
    if manifest is not None and not bool(manifest.enabled):
        return False
    return True


def _register_builtin_tools(registry: ToolRegistry) -> None:
    registry.register(
        RegisteredTool(
            spec=ToolSpec(
                name="web.fetch_and_extract",
                task_type=str(TaskType.WEB_WORKER),
                description="Fetch and extract structured data from websites.",
                risk_level="low",
                tags=["web", "scraping", "research"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "task": {"type": "string"},
                        "url": {"type": "string"},
                        "query": {"type": "string"},
                        "limit": {"type": "integer"},
                        "headless": {"type": "boolean"},
                        "selectors": {"type": "array"},
                        "output_file": {"type": "string"},
                    },
                },
                concurrent_safe=True,
                output_type="text_extraction",
                needs_network=True,
            ),
            adapter_name="web_worker",
            max_parallelism=4,
        )
    )
    # 🔥 新增：增强版 Web Worker（三层感知架构）
    registry.register(
        RegisteredTool(
            spec=ToolSpec(
                name="web.smart_extract",
                task_type="enhanced_web_worker",
                description="Enhanced web extraction with three-layer perception (understand → generate selectors → extract). Better for complex pages.",
                risk_level="low",
                tags=["web", "scraping", "enhanced", "perception"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "task": {"type": "string"},
                        "url": {"type": "string"},
                        "limit": {"type": "integer"},
                        "headless": {"type": "boolean"},
                    },
                },
                concurrent_safe=True,
                output_type="text_extraction",
                needs_network=True,
            ),
            adapter_name="enhanced_web_worker",
            max_parallelism=2,
        )
    )
    registry.register(
        RegisteredTool(
            spec=ToolSpec(
                name="browser.interact",
                task_type=str(TaskType.BROWSER_AGENT),
                description="Interact with dynamic websites through a browser context.",
                risk_level="medium",
                tags=["browser", "automation", "ui"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "task": {"type": "string"},
                        "start_url": {"type": "string"},
                        "headless": {"type": "boolean"},
                        "max_steps": {"type": "integer"},
                    },
                },
                concurrent_safe=False,
                output_type="text_extraction",
                needs_network=True,
            ),
            adapter_name="browser_agent",
            max_parallelism=2,
        )
    )
    registry.register(
        RegisteredTool(
            spec=ToolSpec(
                name="file.read_write",
                task_type=str(TaskType.FILE_WORKER),
                description="Read from and write to local files.",
                risk_level="medium",
                tags=["file", "io", "artifacts"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["write", "read", "append", "generate", "convert", "archive"],
                        },
                        "file_path": {"type": "string"},
                        "data_source": {"type": "string"},
                        "data_sources": {"type": "array", "items": {"type": "string"}},
                        "content": {"type": "string"},
                        "format": {"type": "string"},
                        "title": {"type": "string"},
                        "filename": {"type": "string"},
                        "template": {"type": "string", "description": "templates/ 目录下的 Jinja2 模板文件名"},
                        "topic": {"type": "string", "description": "generate 模式的文档主题"},
                        "outline": {"type": "array", "items": {"type": "string"}, "description": "generate 模式的章节大纲"},
                        "style": {"type": "string", "description": "generate 模式的文档风格：技术文档/邮件/周报/README/方案"},
                        "columns": {"type": "array", "items": {"type": "string"}, "description": "输出字段白名单"},
                        "exclude_columns": {"type": "array", "items": {"type": "string"}, "description": "输出字段黑名单"},
                        "source_path": {"type": "string", "description": "convert 模式的源文件路径"},
                        "target_path": {"type": "string", "description": "convert/archive 模式的目标路径"},
                        "sources": {"type": "array", "items": {"type": "string"}, "description": "archive 模式的源文件路径列表"},
                    },
                },
                concurrent_safe=True,
                output_type="file_download",
                destructive=True,
            ),
            adapter_name="file_worker",
            max_parallelism=4,
        )
    )
    registry.register(
        RegisteredTool(
            spec=ToolSpec(
                name="api.call",
                task_type="api_worker",
                description="Call external HTTP APIs with structured request parameters.",
                risk_level="medium",
                tags=["api", "http", "integration"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "method": {"type": "string"},
                        "headers": {"type": "object"},
                        "body": {"type": ["string", "object", "array"]},
                        "json_body": {"type": ["object", "array"]},
                        "timeout_seconds": {"type": "integer"},
                    },
                },
                concurrent_safe=True,
                output_type="text_extraction",
                needs_network=True,
            ),
            adapter_name="api_worker",
            max_parallelism=4,
        )
    )
    registry.register(
        RegisteredTool(
            spec=ToolSpec(
                name="system.control",
                task_type=str(TaskType.SYSTEM_WORKER),
                description="Execute controlled system-level operations.",
                risk_level="high",
                tags=["system", "desktop", "commands"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "application": {"type": "string"},
                        "args": {"type": ["array", "string"]},
                        "timeout": {"type": ["integer", "number"]},
                        "working_directory": {"type": "string"},
                    },
                },
                concurrent_safe=False,
                output_type="command_output",
                destructive=True,
            ),
            adapter_name="system_worker",
            max_parallelism=1,
            serialized=True,
        )
    )
    # 终端执行工具（Claude Code 级别的 shell 能力）
    registry.register(
        RegisteredTool(
            spec=ToolSpec(
                name="terminal.execute",
                task_type=str(TaskType.TERMINAL_WORKER),
                description="Execute shell commands with full syntax support (pipes, chaining, redirection). Use for running scripts, build commands, git operations, file management, etc.",
                risk_level="medium",
                tags=["terminal", "shell", "commands", "cli"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["shell", "cd", "ls"]},
                        "command": {"type": "string", "description": "Shell command to execute (full syntax supported)"},
                        "working_dir": {"type": "string"},
                        "timeout": {"type": "integer", "description": "Timeout in seconds (max 600)"},
                        "env": {"type": "object", "description": "Extra environment variables"},
                    },
                    "required": ["command"],
                },
                concurrent_safe=False,
                output_type="command_output",
                destructive=True,
            ),
            adapter_name="terminal_worker",
            max_parallelism=1,
            serialized=True,
        )
    )
    registry.register(
        RegisteredTool(
            spec=ToolSpec(
                name="terminal.read_file",
                task_type=str(TaskType.TERMINAL_WORKER),
                description="Read file contents with line numbers and pagination support.",
                risk_level="low",
                tags=["terminal", "file", "read"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["read_file"]},
                        "file_path": {"type": "string"},
                        "offset": {"type": "integer", "description": "Start line number (1-based)"},
                        "limit": {"type": "integer", "description": "Max lines to read"},
                        "encoding": {"type": "string"},
                    },
                    "required": ["file_path"],
                },
                concurrent_safe=True,
                output_type="text_extraction",
            ),
            adapter_name="terminal_worker",
            max_parallelism=4,
        )
    )
    registry.register(
        RegisteredTool(
            spec=ToolSpec(
                name="terminal.edit_file",
                task_type=str(TaskType.TERMINAL_WORKER),
                description="Write or edit files. Use write_file to create/overwrite, edit_file for precise string replacements.",
                risk_level="medium",
                tags=["terminal", "file", "write", "edit"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["write_file", "edit_file"]},
                        "file_path": {"type": "string"},
                        "content": {"type": "string", "description": "File content (for write_file)"},
                        "old_string": {"type": "string", "description": "Exact string to replace (for edit_file)"},
                        "new_string": {"type": "string", "description": "Replacement string (for edit_file)"},
                        "replace_all": {"type": "boolean", "default": False},
                        "encoding": {"type": "string"},
                    },
                    "required": ["file_path"],
                },
                concurrent_safe=False,
                output_type="command_output",
                destructive=True,
            ),
            adapter_name="terminal_worker",
            max_parallelism=1,
        )
    )
    registry.register(
        RegisteredTool(
            spec=ToolSpec(
                name="terminal.search",
                task_type=str(TaskType.TERMINAL_WORKER),
                description="Search files by pattern (glob) or content (grep/regex). Use for finding files or searching code.",
                risk_level="low",
                tags=["terminal", "search", "glob", "grep"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["glob", "grep"]},
                        "pattern": {"type": "string", "description": "Glob pattern (e.g. **/*.py) or regex pattern"},
                        "path": {"type": "string", "description": "Base directory or file to search"},
                        "include": {"type": "string", "description": "File filter glob for grep (e.g. *.py)"},
                        "case_insensitive": {"type": "boolean", "default": False},
                        "max_results": {"type": "integer", "default": 50},
                        "context_lines": {"type": "integer", "default": 0},
                    },
                    "required": ["pattern"],
                },
                concurrent_safe=True,
                output_type="text_extraction",
            ),
            adapter_name="terminal_worker",
            max_parallelism=4,
        )
    )


def _sync_plugin_tools(registry: ToolRegistry) -> ToolRegistry:
    from core.tool_adapters import load_tool_adapter_plugins

    load_tool_adapter_plugins()
    for tool in list_registered_plugin_tools():
        if tool.plugin_id and not is_plugin_enabled(tool.plugin_id):
            continue
        registry.register(tool)
    return registry


def build_dynamic_tool_prompt_lines() -> List[str]:
    """Generate a tool catalog snippet for the Router prompt from the registry."""
    registry = get_builtin_tool_registry()
    lines = [
        "## Registered Tool Catalog (generated from ToolRegistry)",
        "Use these exact tool_name values when planning tasks:",
    ]
    for tool in sorted(registry.list_tools(), key=lambda item: item.spec.name):
        lines.append(
            "- "
            f"{tool.spec.name} "
            f"(adapter: {tool.adapter_name}, task_type: {tool.spec.task_type}, risk: {tool.spec.risk_level}) - "
            f"{tool.spec.description}"
        )
    return lines


def _register_mcp_tools(registry: ToolRegistry) -> None:
    """从 MCPClientManager 动态注册所有已发现的 MCP 工具。

    在同步上下文中调用：如果有可用的 async 事件循环则复用，否则创建临时循环。
    MCP Server 连接失败不影响启动——跳过并记录警告。
    """
    from config.settings import settings

    if not getattr(settings, "MCP_ENABLED", True):
        return

    try:
        from core.mcp_client import MCPClientManager
    except ImportError:
        return

    async def _init_and_collect():
        manager = await MCPClientManager.get_instance()
        return manager

    # 获取或创建事件循环
    try:
        loop = asyncio.get_running_loop()
        # 已有运行中的循环（如 Streamlit），跳过同步初始化
        # MCP 工具将在首次 adapter.execute 调用时懒加载
        return
    except RuntimeError:
        pass

    try:
        loop = asyncio.new_event_loop()
        try:
            manager = loop.run_until_complete(_init_and_collect())
        finally:
            # 不关闭循环中的 MCP 子进程连接，只停止循环
            loop.close()
    except Exception as exc:
        from utils.logger import log_warning
        log_warning(f"MCP tool registration skipped: {exc}")
        return

    for tool in manager.get_all_tools():
        full_name = f"mcp.{tool.server_name}.{tool.name}"
        client = manager.get_client(tool.server_name)
        if not client:
            continue

        risk = client.config.risk_level
        registry.register(
            RegisteredTool(
                spec=ToolSpec(
                    name=full_name,
                    task_type="mcp_handler",
                    description=tool.description or f"MCP tool: {tool.name}",
                    risk_level=risk,
                    tags=["mcp", tool.server_name],
                    input_schema=tool.input_schema,
                    concurrent_safe=risk != "high",
                    output_type="text_extraction",
                    needs_network=True,
                    destructive=risk in ("medium", "high"),
                ),
                adapter_name="mcp_adapter",
                max_parallelism=client.config.max_parallelism,
                serialized=risk == "high",
            )
        )


def build_builtin_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    _register_builtin_tools(registry)
    _register_mcp_tools(registry)
    return _sync_plugin_tools(registry)


_builtin_registry: Optional[ToolRegistry] = None
_builtin_registry_lock = threading.Lock()


def get_builtin_tool_registry() -> ToolRegistry:
    global _builtin_registry
    with _builtin_registry_lock:
        _builtin_registry = build_builtin_tool_registry()
        return _builtin_registry
