"""
OmniCore 通用重试工具
区分可重试错误和致命错误，支持指数退避
"""
import asyncio
import random
from typing import Callable, Any, Optional, Set
from utils.logger import log_warning, log_agent_action


# 可重试的错误关键词（网络抖动、超时、临时性故障）
RETRYABLE_KEYWORDS = {
    "timeout", "timed out", "超时",
    "connection reset", "connection refused", "connection closed",
    "network", "dns", "ssl", "eof",
    "target page, currentcontext, or browser has been closed",
    "page crashed", "context destroyed",
    "navigation failed", "net::",
    "econnreset", "econnrefused", "epipe", "ehostunreach",
    "temporary", "502", "503", "504", "429",
}

# 不可重试的错误关键词（逻辑错误、参数错误）
FATAL_KEYWORDS = {
    "invalid url", "malformed", "valueerror",
    "permission denied", "not found", "404",
    "authentication", "unauthorized", "401", "403",
}


def is_retryable(error: Exception) -> bool:
    """判断错误是否值得重试"""
    msg = str(error).lower()
    # 先检查是否明确不可重试
    if any(k in msg for k in FATAL_KEYWORDS):
        return False
    # 再检查是否匹配可重试模式
    if any(k in msg for k in RETRYABLE_KEYWORDS):
        return True
    # TimeoutError 类型直接可重试
    if isinstance(error, (TimeoutError, asyncio.TimeoutError, ConnectionError, OSError)):
        return True
    return False


async def async_retry(
    fn: Callable,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 10.0,
    caller_name: str = "",
    on_retry: Optional[Callable] = None,
) -> Any:
    """
    异步重试包装器，带指数退避 + 抖动

    Args:
        fn: 要执行的异步函数（无参数，用 lambda 包装）
        max_attempts: 最大尝试次数
        base_delay: 基础延迟秒数
        max_delay: 最大延迟秒数
        caller_name: 调用者名称（用于日志）
        on_retry: 每次重试前的回调（可选，接收 attempt 和 error）
    """
    last_error = None
    for attempt in range(max_attempts):
        try:
            return await fn()
        except Exception as e:
            last_error = e
            if attempt == max_attempts - 1:
                break
            if not is_retryable(e):
                log_warning(f"[{caller_name}] 不可重试的错误，直接失败: {str(e)[:100]}")
                break
            delay = min(base_delay * (2 ** attempt) + random.uniform(0, 1), max_delay)
            log_agent_action(
                caller_name or "Retry",
                f"第 {attempt + 1} 次失败，{delay:.1f}s 后重试",
                str(e)[:80],
            )
            if on_retry:
                try:
                    result = on_retry(attempt, e)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    pass
            await asyncio.sleep(delay)
    raise last_error


def sync_retry(
    fn: Callable,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    caller_name: str = "",
) -> Any:
    """同步版重试"""
    import time
    last_error = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as e:
            last_error = e
            if attempt == max_attempts - 1:
                break
            if not is_retryable(e):
                break
            delay = min(base_delay * (2 ** attempt) + random.uniform(0, 1), 10.0)
            log_agent_action(
                caller_name or "Retry",
                f"第 {attempt + 1} 次失败，{delay:.1f}s 后重试",
                str(e)[:80],
            )
            time.sleep(delay)
    raise last_error
