"""
Initial tool registry bridging the current fixed worker model to a future
tool-centric runtime.
"""
from __future__ import annotations

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
                        "selectors": {"type": "array"},
                        "output_file": {"type": "string"},
                    },
                },
            ),
            adapter_name="web_worker",
            max_parallelism=4,
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
                        "action": {"type": "string"},
                        "file_path": {"type": "string"},
                        "data_source": {"type": "string"},
                        "data_sources": {"type": "array"},
                        "content": {"type": "string"},
                        "format": {"type": "string"},
                        "title": {"type": "string"},
                        "filename": {"type": "string"},
                    },
                },
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
            ),
            adapter_name="system_worker",
            max_parallelism=1,
            serialized=True,
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


def build_builtin_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    _register_builtin_tools(registry)
    return _sync_plugin_tools(registry)


_builtin_registry: Optional[ToolRegistry] = None
_builtin_registry_lock = threading.Lock()


def get_builtin_tool_registry() -> ToolRegistry:
    global _builtin_registry
    with _builtin_registry_lock:
        _builtin_registry = build_builtin_tool_registry()
        return _builtin_registry
