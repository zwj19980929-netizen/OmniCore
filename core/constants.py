"""
OmniCore core constants.

Centralized enum types and constants, replacing scattered magic strings.
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import Dict, FrozenSet, List

logger = logging.getLogger(__name__)


class TaskType(str, Enum):
    """Task type enum.

    .. deprecated::
        Prefer :class:`core.agent_registry.AgentRegistry` for checking valid
        agent/task types.  This enum is kept for backward compatibility with
        existing code that imports ``TaskType`` members directly.
    """
    WEB_WORKER = "web_worker"
    BROWSER_AGENT = "browser_agent"
    FILE_WORKER = "file_worker"
    SYSTEM_WORKER = "system_worker"

    def __str__(self) -> str:
        return self.value


class TaskStatus(str, Enum):
    """任务状态枚举"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

    def __str__(self) -> str:
        return self.value


class ExecutionStatus(str, Enum):
    """执行状态枚举"""
    IDLE = "idle"
    ROUTING = "routing"
    EXECUTING = "executing"
    REVIEWING = "reviewing"
    COMPLETED = "completed"
    COMPLETED_WITH_ISSUES = "completed_with_issues"
    CANCELLED = "cancelled"
    ERROR = "error"

    def __str__(self) -> str:
        return self.value


class FailureType(str, Enum):
    """失败类型枚举"""
    TIMEOUT = "timeout"
    SELECTOR_NOT_FOUND = "selector_not_found"
    BLOCKED_OR_CAPTCHA = "blocked_or_captcha"
    PERMISSION_DENIED = "permission_denied"
    INVALID_INPUT = "invalid_input"
    UNKNOWN = "unknown"

    def __str__(self) -> str:
        return self.value


class IntentType(str, Enum):
    """意图类型枚举"""
    WEB_SCRAPING = "web_scraping"
    FILE_OPERATION = "file_operation"
    SYSTEM_CONTROL = "system_control"
    DATA_ANALYSIS = "data_analysis"
    INFORMATION_QUERY = "information_query"
    MULTI_STEP_TASK = "multi_step_task"

    def __str__(self) -> str:
        return self.value


class FileFormat(str, Enum):
    """文件格式枚举"""
    TXT = "txt"
    XLSX = "xlsx"
    CSV = "csv"
    MARKDOWN = "markdown"
    HTML = "html"

    def __str__(self) -> str:
        return self.value


class FileAction(str, Enum):
    """文件操作类型枚举"""
    READ = "read"
    WRITE = "write"

    def __str__(self) -> str:
        return self.value


class SystemAction(str, Enum):
    """系统操作类型枚举"""
    EXECUTE_COMMAND = "execute_command"
    KEYBOARD = "keyboard"
    MOUSE_CLICK = "mouse_click"
    SCREENSHOT = "screenshot"

    def __str__(self) -> str:
        return self.value


# 失败类型关键词映射（用于从错误消息中分类）
FAILURE_KEYWORDS: Dict[FailureType, List[str]] = {
    FailureType.TIMEOUT: ["timeout", "timed out", "超时", "TimeoutError"],
    FailureType.SELECTOR_NOT_FOUND: [
        "selector", "not found", "找不到元素", "no element", "query_selector"
    ],
    FailureType.BLOCKED_OR_CAPTCHA: [
        "captcha", "验证码", "blocked", "forbidden", "403", "anti-bot", "反爬"
    ],
    FailureType.PERMISSION_DENIED: [
        "permission", "denied", "权限", "PermissionError", "access denied"
    ],
    FailureType.INVALID_INPUT: [
        "invalid", "参数错误", "missing param", "ValueError", "KeyError"
    ],
}

# 高危操作列表
HIGH_RISK_OPERATIONS = frozenset([
    "delete_file",
    "send_email",
    "execute_script",
    "modify_system",
    "transfer_money",
    "post_to_social",
])

# Supported task types - static fallback set.
# Prefer ``get_supported_task_types()`` which merges this with AgentRegistry.
_STATIC_TASK_TYPES: FrozenSet[str] = frozenset([
    TaskType.WEB_WORKER,
    TaskType.BROWSER_AGENT,
    TaskType.FILE_WORKER,
    TaskType.SYSTEM_WORKER,
])

# Keep the original name as a module-level constant for backward compatibility.
# Code that only reads this constant will still work; new code should call
# ``get_supported_task_types()`` instead to include dynamically registered types.
SUPPORTED_TASK_TYPES: FrozenSet[str] = _STATIC_TASK_TYPES

# PAOD 常量
MAX_STEPS_PER_TASK = 6
MAX_FALLBACK_ATTEMPTS = 3
MAX_REPLAN_ATTEMPTS = 3

# 浏览器重试次数
BROWSER_RETRIES = 2


def get_supported_task_types() -> FrozenSet[str]:
    """Return the full set of supported task types.

    Merges the static ``_STATIC_TASK_TYPES`` with any types registered in the
    :class:`~core.agent_registry.AgentRegistry`.  Falls back gracefully to the
    static set if the registry is not yet initialised.
    """
    try:
        from core.agent_registry import AgentRegistry
        registry = AgentRegistry.get_instance()
        return _STATIC_TASK_TYPES | registry.get_supported_types()
    except Exception:
        return _STATIC_TASK_TYPES


def is_valid_task_type(task_type: str) -> bool:
    """Check whether *task_type* is a recognised task/agent type.

    Checks the :class:`~core.agent_registry.AgentRegistry` first (which
    includes dynamically registered plugin types), then falls back to the
    static :class:`TaskType` enum for backward compatibility.
    """
    try:
        from core.agent_registry import AgentRegistry
        registry = AgentRegistry.get_instance()
        if registry.is_valid_type(task_type):
            return True
    except Exception:
        pass

    # Fallback: check the legacy enum
    try:
        TaskType(task_type)
        return True
    except ValueError:
        return False


def classify_failure_type(error_msg: str) -> FailureType:
    """根据错误信息关键词匹配分类 failure_type"""
    if not error_msg:
        return FailureType.UNKNOWN
    lower = error_msg.lower()
    for ftype, keywords in FAILURE_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in lower:
                return ftype
    return FailureType.UNKNOWN
