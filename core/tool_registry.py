"""
Initial tool registry bridging the current fixed worker model to a future
tool-centric runtime.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from core.constants import TaskType
from core.tool_protocol import ToolSpec


@dataclass(frozen=True)
class RegisteredTool:
    """Registry entry for a tool capability."""

    spec: ToolSpec
    adapter_name: str
    max_parallelism: int = 1
    serialized: bool = False


class ToolRegistry:
    """Simple in-memory registry for available tool capabilities."""

    def __init__(self):
        self._by_name: Dict[str, RegisteredTool] = {}
        self._by_task_type: Dict[str, RegisteredTool] = {}

    def register(self, tool: RegisteredTool) -> None:
        self._by_name[tool.spec.name] = tool
        self._by_task_type[tool.spec.task_type] = tool

    def get(self, name: str) -> Optional[RegisteredTool]:
        return self._by_name.get(name)

    def get_by_task_type(self, task_type: str) -> Optional[RegisteredTool]:
        return self._by_task_type.get(str(task_type))

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


def build_builtin_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            spec=ToolSpec(
                name="web.fetch_and_extract",
                task_type=str(TaskType.WEB_WORKER),
                description="Fetch and extract structured data from websites.",
                risk_level="low",
                tags=["web", "scraping", "research"],
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
            ),
            adapter_name="file_worker",
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
            ),
            adapter_name="system_worker",
            max_parallelism=1,
            serialized=True,
        )
    )
    return registry


_builtin_registry: Optional[ToolRegistry] = None
_builtin_registry_lock = threading.Lock()


def get_builtin_tool_registry() -> ToolRegistry:
    global _builtin_registry
    if _builtin_registry is None:
        with _builtin_registry_lock:
            if _builtin_registry is None:
                _builtin_registry = build_builtin_tool_registry()
    return _builtin_registry
