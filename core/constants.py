"""
OmniCore 核心常量定义
统一管理枚举类型和常量，替代散落各处的魔法字符串
"""
from enum import Enum
from typing import Dict, List


class TaskType(str, Enum):
    """任务类型枚举"""
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

# 支持的任务类型集合
SUPPORTED_TASK_TYPES = frozenset([
    TaskType.WEB_WORKER,
    TaskType.BROWSER_AGENT,
    TaskType.FILE_WORKER,
    TaskType.SYSTEM_WORKER,
])

# PAOD 常量
MAX_STEPS_PER_TASK = 6
MAX_FALLBACK_ATTEMPTS = 3
MAX_REPLAN_ATTEMPTS = 3

# 浏览器重试次数
BROWSER_RETRIES = 2


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
