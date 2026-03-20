"""
OmniCore 日志工具
使用 rich 库实现美化输出
"""
import sys
import logging
from rich.console import Console
from rich.logging import RichHandler
from rich.theme import Theme

from config.settings import settings

# 修复 Windows 终端编码问题
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# 自定义主题
custom_theme = Theme({
    "info": "cyan",
    "warning": "yellow",
    "error": "red bold",
    "success": "green",
    "agent": "magenta",
})

console = Console(theme=custom_theme, force_terminal=True)


def setup_logger(name: str = "omnicore") -> logging.Logger:
    """配置并返回 logger"""
    logger = logging.getLogger(name)

    if not logger.handlers:
        handler = RichHandler(
            console=console,
            show_time=True,
            show_path=settings.DEBUG_MODE,
            rich_tracebacks=True,
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)

    logger.setLevel(getattr(logging, settings.LOG_LEVEL))
    return logger


# 全局 logger 实例
logger = setup_logger()


def log_agent_action(agent_name: str, action: str, details: str = ""):
    """记录 Agent 动作的专用方法"""
    console.print(f"[agent][Agent: {agent_name}][/agent] {action}", highlight=False)
    if details:
        console.print(f"   -> {details}", style="dim")


def log_task_status(task_id: str, status: str, message: str = ""):
    """记录任务状态"""
    status_icons = {
        "pending": "[WAIT]",
        "running": "[RUN]",
        "completed": "[OK]",
        "failed": "[FAIL]",
    }
    icon = status_icons.get(status, "[*]")
    console.print(f"{icon} Task [{task_id}]: {status} {message}")


def log_success(message: str):
    """成功日志"""
    console.print(f"[success][OK] {message}[/success]")


def log_error(message: str):
    """错误日志"""
    console.print(f"[error][ERROR] {message}[/error]")


def log_warning(message: str):
    """警告日志"""
    console.print(f"[warning][WARN] {message}[/warning]")


def log_debug_metrics(scope: str, metrics: dict):
    """Only emit lightweight metrics when debug mode is enabled."""
    if not settings.DEBUG_MODE:
        return
    if not metrics:
        console.print(f"[dim][debug] {scope}: no metrics[/dim]")
        return
    parts = [f"{key}={metrics[key]}" for key in sorted(metrics)]
    console.print(f"[dim][debug] {scope}: {', '.join(parts)}[/dim]", highlight=False)


# ---------------------------------------------------------------------------
# Structured logger integration
# ---------------------------------------------------------------------------
from utils.structured_logger import StructuredLogger, LogContext, get_structured_logger  # noqa: E402

# Initialize the singleton so the JSONL handler is ready
_structured_logger = StructuredLogger()

__all__ = [
    "logger",
    "console",
    "setup_logger",
    "log_agent_action",
    "log_task_status",
    "log_success",
    "log_error",
    "log_warning",
    "log_debug_metrics",
    "get_structured_logger",
    "LogContext",
]
