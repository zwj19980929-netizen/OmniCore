"""
Foundational tool abstractions for the staged migration away from fixed task types.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol


@dataclass(frozen=True)
class ToolSpec:
    """Static metadata that describes a tool capability."""

    name: str
    task_type: str
    description: str
    risk_level: str = "medium"
    tags: List[str] = field(default_factory=list)
    input_schema: Dict[str, Any] = field(default_factory=dict)
    output_schema: Dict[str, Any] = field(default_factory=dict)
    # 能力标签（R4）
    concurrent_safe: bool = True   # 是否可与其他工具并发执行
    output_type: str = ""          # 输出类型，取值参考 TaskOutputType
    needs_network: bool = False    # 是否需要网络访问
    destructive: bool = False      # 是否会修改文件/系统状态


@dataclass
class ToolContext:
    """Runtime context passed to a tool invocation."""

    task_id: str = ""
    shared_memory: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResultEnvelope:
    """Normalized wrapper for tool execution results."""

    success: bool
    output: Any = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


class ToolExecutor(Protocol):
    """Execution contract for a registered tool implementation."""

    async def execute(self, args: Dict[str, Any], context: ToolContext) -> ToolResultEnvelope:
        ...
