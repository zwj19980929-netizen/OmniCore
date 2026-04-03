"""
Foundational tool abstractions for the staged migration away from fixed task types.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol


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
    # 能力标签（R4）— S6: 默认值改为 fail-closed（新工具未显式声明时取最保守值）
    concurrent_safe: bool = False  # 是否可与其他工具并发执行（fail-closed: 默认不可并发）
    output_type: str = ""          # 输出类型，取值参考 TaskOutputType
    needs_network: bool = False    # 是否需要网络访问
    destructive: bool = True       # 是否会修改文件/系统状态（fail-closed: 默认视为有破坏性）
    # S6: 信任分层
    trust_level: str = "builtin"   # builtin > local > mcp_local > mcp_remote
    # S4: Pipeline 扩展字段
    validate_input: Optional[Callable[[Dict[str, Any]], Optional[str]]] = field(default=None, repr=False)  # 语义校验函数，返回 None 表示通过，否则返回错误信息
    required_context: List[str] = field(default_factory=list)         # 需要自动注入的上下文字段列表
    # 注意：param_schema 复用已有的 input_schema 字段（JSON Schema 格式）


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
