"""
OmniCore 限流和熔断机制
- 域名级别的请求限流
- 指数退避重试
- 熔断器模式
"""
import asyncio
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional, Callable, Any
from urllib.parse import urlparse

from utils.logger import log_warning, log_error, log_agent_action


class CircuitState(Enum):
    """熔断器状态"""
    CLOSED = "closed"  # 正常状态
    OPEN = "open"  # 熔断打开，拒绝请求
    HALF_OPEN = "half_open"  # 半开状态，尝试恢复


@dataclass
class RateLimitConfig:
    """限流配置"""
    requests_per_second: float = 2.0  # 每秒请求数
    burst_size: int = 5  # 突发容量
    min_interval_ms: int = 500  # 最小请求间隔（毫秒）


@dataclass
class CircuitBreakerConfig:
    """熔断器配置"""
    failure_threshold: int = 5  # 失败阈值
    success_threshold: int = 2  # 恢复阈值（半开状态下）
    timeout_seconds: int = 60  # 熔断超时（秒）
    half_open_max_calls: int = 3  # 半开状态最大尝试次数


@dataclass
class DomainStats:
    """域名统计信息"""
    total_requests: int = 0
    failed_requests: int = 0
    last_request_time: float = 0.0
    request_times: deque = field(default_factory=lambda: deque(maxlen=100))

    # 熔断器状态
    circuit_state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    circuit_opened_at: float = 0.0


class RateLimiter:
    """
    域名级别的限流器
    使用令牌桶算法
    """

    def __init__(self, config: RateLimitConfig = None):
        self.config = config or RateLimitConfig()
        self._domain_tokens: Dict[str, float] = defaultdict(lambda: self.config.burst_size)
        self._domain_last_update: Dict[str, float] = defaultdict(time.time)
        self._locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def _get_domain(self, url: str) -> str:
        """从 URL 提取域名"""
        try:
            parsed = urlparse(url)
            return parsed.netloc or parsed.path.split('/')[0]
        except Exception:
            return "unknown"

    def _refill_tokens(self, domain: str, now: float):
        """补充令牌"""
        last_update = self._domain_last_update[domain]
        elapsed = now - last_update

        # 根据时间补充令牌
        tokens_to_add = elapsed * self.config.requests_per_second
        self._domain_tokens[domain] = min(
            self._domain_tokens[domain] + tokens_to_add,
            self.config.burst_size
        )
        self._domain_last_update[domain] = now

    async def acquire(self, url: str) -> bool:
        """
        获取访问许可

        Returns:
            True 如果允许访问，False 如果需要等待
        """
        domain = self._get_domain(url)

        async with self._locks[domain]:
            now = time.time()
            self._refill_tokens(domain, now)

            if self._domain_tokens[domain] >= 1.0:
                self._domain_tokens[domain] -= 1.0
                return True

            # 计算需要等待的时间
            wait_time = (1.0 - self._domain_tokens[domain]) / self.config.requests_per_second
            log_warning(f"域名 {domain} 限流，等待 {wait_time:.2f}秒")
            await asyncio.sleep(wait_time)

            # 重新补充令牌并消费
            self._refill_tokens(domain, time.time())
            self._domain_tokens[domain] -= 1.0
            return True

    async def wait_if_needed(self, url: str):
        """等待直到可以发送请求"""
        domain = self._get_domain(url)

        async with self._locks[domain]:
            now = time.time()
            last_request = self._domain_last_update.get(domain, 0)
            min_interval = self.config.min_interval_ms / 1000.0

            elapsed = now - last_request
            if elapsed < min_interval:
                wait_time = min_interval - elapsed
                await asyncio.sleep(wait_time)

            self._domain_last_update[domain] = time.time()


class CircuitBreaker:
    """
    熔断器
    防止对持续失败的服务发送请求
    """

    def __init__(self, config: CircuitBreakerConfig = None):
        self.config = config or CircuitBreakerConfig()
        self._domain_stats: Dict[str, DomainStats] = defaultdict(DomainStats)
        self._locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def _get_domain(self, url: str) -> str:
        """从 URL 提取域名"""
        try:
            parsed = urlparse(url)
            return parsed.netloc or parsed.path.split('/')[0]
        except Exception:
            return "unknown"

    async def call(self, url: str, func: Callable, *args, **kwargs) -> Any:
        """
        通过熔断器调用函数

        Args:
            url: 目标 URL
            func: 要调用的函数（可以是协程）
            *args, **kwargs: 函数参数

        Returns:
            函数执行结果

        Raises:
            Exception: 如果熔断器打开或函数执行失败
        """
        domain = self._get_domain(url)

        async with self._locks[domain]:
            stats = self._domain_stats[domain]
            now = time.time()

            # 检查熔断器状态
            if stats.circuit_state == CircuitState.OPEN:
                # 检查是否可以进入半开状态
                if now - stats.circuit_opened_at >= self.config.timeout_seconds:
                    log_agent_action("CircuitBreaker", f"域名 {domain} 进入半开状态")
                    stats.circuit_state = CircuitState.HALF_OPEN
                    stats.success_count = 0
                    stats.failure_count = 0
                else:
                    raise Exception(f"熔断器打开：域名 {domain} 暂时不可用")

            # 半开状态下限制请求数
            if stats.circuit_state == CircuitState.HALF_OPEN:
                if stats.total_requests >= self.config.half_open_max_calls:
                    raise Exception(f"熔断器半开：域名 {domain} 达到最大尝试次数")

        # 执行函数
        stats.total_requests += 1
        stats.last_request_time = time.time()

        try:
            # 判断是否是协程
            if asyncio.iscoroutinefunction(func):
                result = await func(*args, **kwargs)
            else:
                result = func(*args, **kwargs)

            # 成功
            await self._on_success(domain)
            return result

        except Exception as e:
            # 失败
            await self._on_failure(domain, e)
            raise

    async def _on_success(self, domain: str):
        """处理成功请求"""
        async with self._locks[domain]:
            stats = self._domain_stats[domain]

            if stats.circuit_state == CircuitState.HALF_OPEN:
                stats.success_count += 1
                if stats.success_count >= self.config.success_threshold:
                    log_agent_action("CircuitBreaker", f"域名 {domain} 熔断器关闭")
                    stats.circuit_state = CircuitState.CLOSED
                    stats.failure_count = 0
                    stats.success_count = 0
            elif stats.circuit_state == CircuitState.CLOSED:
                # 重置失败计数
                stats.failure_count = max(0, stats.failure_count - 1)

    async def _on_failure(self, domain: str, error: Exception):
        """处理失败请求"""
        async with self._locks[domain]:
            stats = self._domain_stats[domain]
            stats.failed_requests += 1
            stats.failure_count += 1

            if stats.circuit_state == CircuitState.HALF_OPEN:
                # 半开状态下失败，立即打开熔断器
                log_error(f"域名 {domain} 半开状态失败，重新打开熔断器")
                stats.circuit_state = CircuitState.OPEN
                stats.circuit_opened_at = time.time()
                stats.success_count = 0

            elif stats.circuit_state == CircuitState.CLOSED:
                # 检查是否达到失败阈值
                if stats.failure_count >= self.config.failure_threshold:
                    log_error(f"域名 {domain} 连续失败 {stats.failure_count} 次，打开熔断器")
                    stats.circuit_state = CircuitState.OPEN
                    stats.circuit_opened_at = time.time()

    def get_stats(self, url: str) -> Dict[str, Any]:
        """获取域名统计信息"""
        domain = self._get_domain(url)
        stats = self._domain_stats[domain]

        return {
            "domain": domain,
            "total_requests": stats.total_requests,
            "failed_requests": stats.failed_requests,
            "failure_rate": stats.failed_requests / max(stats.total_requests, 1),
            "circuit_state": stats.circuit_state.value,
            "failure_count": stats.failure_count,
            "last_request_time": stats.last_request_time,
        }


class ExponentialBackoff:
    """
    指数退避重试策略
    """

    def __init__(
        self,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        multiplier: float = 2.0,
        jitter: bool = True,
    ):
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.multiplier = multiplier
        self.jitter = jitter

    def get_delay(self, attempt: int) -> float:
        """
        计算第 N 次重试的延迟时间

        Args:
            attempt: 重试次数（从 0 开始）

        Returns:
            延迟秒数
        """
        delay = min(self.base_delay * (self.multiplier ** attempt), self.max_delay)

        if self.jitter:
            import random
            delay = delay * (0.5 + random.random() * 0.5)

        return delay

    async def sleep(self, attempt: int):
        """等待指定的退避时间"""
        delay = self.get_delay(attempt)
        log_warning(f"指数退避：第 {attempt + 1} 次重试，等待 {delay:.2f}秒")
        await asyncio.sleep(delay)


# 全局单例
_rate_limiter: Optional[RateLimiter] = None
_circuit_breaker: Optional[CircuitBreaker] = None


def get_rate_limiter() -> RateLimiter:
    """获取全局限流器单例"""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter()
    return _rate_limiter


def get_circuit_breaker() -> CircuitBreaker:
    """获取全局熔断器单例"""
    global _circuit_breaker
    if _circuit_breaker is None:
        _circuit_breaker = CircuitBreaker()
    return _circuit_breaker
