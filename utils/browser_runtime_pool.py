"""
Browser runtime pool for reusing Playwright browser processes per event loop.
"""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from playwright.async_api import Browser, Playwright, async_playwright

from config.settings import settings


BrowserKey = Tuple[str, bool]


class BrowserPoolCircuitOpenError(RuntimeError):
    """Raised when the pool is temporarily bypassed due to repeated launch failures."""


class BrowserPoolLaunchError(RuntimeError):
    """Raised when the pool cannot launch a browser process."""


@dataclass
class _PooledBrowser:
    browser_id: int
    browser: Browser
    ref_count: int = 0
    last_released_at: float = 0.0


@dataclass
class BrowserLease:
    pool: "BrowserRuntimePool"
    key: BrowserKey
    browser_id: int
    browser: Browser
    _released: bool = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self.pool.release_browser(self)


class BrowserRuntimePool:
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self._lock = asyncio.Lock()
        self._condition = asyncio.Condition(self._lock)
        self._playwright: Optional[Playwright] = None
        self._browsers: Dict[BrowserKey, Dict[int, _PooledBrowser]] = {}
        self._next_browser_id = 1
        self._acquire_count = 0
        self._reuse_hits = 0
        self._launch_count = 0
        self._release_count = 0
        self._cleanup_close_count = 0
        self._wait_count = 0
        self._wait_timeout_count = 0
        self._launch_failure_count = 0
        self._circuit_open_rejections = 0
        self._consecutive_launch_failures = 0
        self._circuit_open_until = 0.0

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self._loop

    async def _ensure_playwright_locked(self) -> Playwright:
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        return self._playwright

    async def _launch_browser_locked(self, headless: bool) -> Browser:
        playwright = await self._ensure_playwright_locked()
        return await playwright.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--disable-infobars",
                "--no-sandbox",
                "--window-size=1366,768",
            ],
            ignore_default_args=["--enable-automation"],
        )

    def _is_circuit_open(self, now: Optional[float] = None) -> bool:
        current = time.monotonic() if now is None else now
        return current < self._circuit_open_until

    def _record_launch_success_locked(self) -> None:
        self._consecutive_launch_failures = 0
        self._circuit_open_until = 0.0

    def _record_launch_failure_locked(self) -> None:
        self._launch_failure_count += 1
        self._consecutive_launch_failures += 1
        if self._consecutive_launch_failures >= max(settings.BROWSER_POOL_CIRCUIT_BREAK_THRESHOLD, 1):
            self._circuit_open_until = (
                time.monotonic() + max(settings.BROWSER_POOL_CIRCUIT_BREAK_SECONDS, 1)
            )

    async def _close_pooled_browser_locked(self, key: BrowserKey, browser_id: int) -> None:
        key_pool = self._browsers.get(key)
        if not key_pool:
            return
        pooled = key_pool.pop(browser_id, None)
        if pooled is None:
            return
        self._cleanup_close_count += 1
        try:
            if pooled.browser.is_connected():
                await pooled.browser.close()
        except Exception:
            pass
        if not key_pool:
            self._browsers.pop(key, None)

    def _iter_pooled_items(self):
        for key, key_pool in self._browsers.items():
            for browser_id, pooled in key_pool.items():
                yield key, browser_id, pooled

    def _pick_reusable_browser_locked(self, key: BrowserKey) -> Optional[_PooledBrowser]:
        key_pool = self._browsers.get(key) or {}
        max_contexts = max(settings.BROWSER_POOL_MAX_CONTEXTS_PER_BROWSER, 1)
        candidates = [
            pooled
            for pooled in key_pool.values()
            if pooled.browser.is_connected() and pooled.ref_count < max_contexts
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item.ref_count, item.last_released_at, item.browser_id))
        return candidates[0]

    async def _cleanup_idle_locked(self, now: Optional[float] = None) -> None:
        current = now if now is not None else time.time()
        ttl = max(settings.BROWSER_POOL_IDLE_TTL_SECONDS, 1)
        stale_items = []

        for key, browser_id, pooled in self._iter_pooled_items():
            if not pooled.browser.is_connected():
                stale_items.append((key, browser_id))
                continue
            if pooled.ref_count > 0:
                continue
            if pooled.last_released_at and (current - pooled.last_released_at) >= ttl:
                stale_items.append((key, browser_id))

        for key, browser_id in stale_items:
            await self._close_pooled_browser_locked(key, browser_id)

        if not self._browsers and self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    async def acquire_browser(self, headless: bool = True) -> BrowserLease:
        key: BrowserKey = ("chromium", bool(headless))
        start_monotonic = time.monotonic()
        deadline = start_monotonic + max(settings.BROWSER_POOL_ACQUIRE_TIMEOUT_SECONDS, 1)
        max_browsers = max(settings.BROWSER_POOL_MAX_BROWSERS_PER_KEY, 1)
        started_waiting = False

        async with self._condition:
            self._acquire_count += 1
            if self._is_circuit_open(start_monotonic):
                self._circuit_open_rejections += 1
                raise BrowserPoolCircuitOpenError("browser pool circuit is open")
            await self._cleanup_idle_locked(time.time())

            while True:
                pooled = self._pick_reusable_browser_locked(key)
                if pooled is not None:
                    self._reuse_hits += 1
                    pooled.ref_count += 1
                    return BrowserLease(
                        pool=self,
                        key=key,
                        browser_id=pooled.browser_id,
                        browser=pooled.browser,
                    )

                key_pool = self._browsers.get(key) or {}
                connected_browsers = [
                    pooled for pooled in key_pool.values() if pooled.browser.is_connected()
                ]
                if len(connected_browsers) < max_browsers:
                    try:
                        browser = await self._launch_browser_locked(headless=headless)
                    except Exception as exc:
                        self._record_launch_failure_locked()
                        raise BrowserPoolLaunchError(
                            f"failed to launch pooled browser for {key!r}"
                        ) from exc
                    self._record_launch_success_locked()
                    browser_id = self._next_browser_id
                    self._next_browser_id += 1
                    pooled = _PooledBrowser(
                        browser_id=browser_id,
                        browser=browser,
                        ref_count=1,
                    )
                    if key not in self._browsers:
                        self._browsers[key] = {}
                    self._browsers[key][browser_id] = pooled
                    self._launch_count += 1
                    return BrowserLease(
                        pool=self,
                        key=key,
                        browser_id=browser_id,
                        browser=browser,
                    )

                if not started_waiting:
                    self._wait_count += 1
                    started_waiting = True

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._wait_timeout_count += 1
                    raise TimeoutError(
                        f"Timed out waiting for browser capacity for {key!r}"
                    )

                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=remaining)
                except asyncio.TimeoutError as exc:
                    self._wait_timeout_count += 1
                    raise TimeoutError(
                        f"Timed out waiting for browser capacity for {key!r}"
                    ) from exc
                await self._cleanup_idle_locked(time.time())

    async def release_browser(self, lease: BrowserLease) -> None:
        async with self._condition:
            key_pool = self._browsers.get(lease.key)
            pooled = key_pool.get(lease.browser_id) if key_pool else None
            if pooled is None:
                self._condition.notify_all()
                return
            pooled.ref_count = max(pooled.ref_count - 1, 0)
            self._release_count += 1
            pooled.last_released_at = time.time()
            await self._cleanup_idle_locked(time.time())
            self._condition.notify_all()

    async def close_all(self) -> None:
        async with self._condition:
            for key, key_pool in list(self._browsers.items()):
                for browser_id in list(key_pool.keys()):
                    await self._close_pooled_browser_locked(key, browser_id)
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None
            self._condition.notify_all()

    def snapshot_stats(self) -> dict:
        pooled_browsers = 0
        active_leases = 0
        per_key_browser_counts: Dict[str, int] = {}
        for key, key_pool in self._browsers.items():
            key_label = f"{key[0]}:{'headless' if key[1] else 'headed'}"
            per_key_browser_counts[key_label] = len(key_pool)
            pooled_browsers += len(key_pool)
            active_leases += sum(pooled.ref_count for pooled in key_pool.values())

        return {
            "acquires": self._acquire_count,
            "reuse_hits": self._reuse_hits,
            "launches": self._launch_count,
            "releases": self._release_count,
            "cleanup_closes": self._cleanup_close_count,
            "waits": self._wait_count,
            "wait_timeouts": self._wait_timeout_count,
            "launch_failures": self._launch_failure_count,
            "circuit_open_rejections": self._circuit_open_rejections,
            "circuit_open": self._is_circuit_open(),
            "pooled_browsers": pooled_browsers,
            "active_leases": active_leases,
            "max_browsers_per_key": max(settings.BROWSER_POOL_MAX_BROWSERS_PER_KEY, 1),
            "max_contexts_per_browser": max(settings.BROWSER_POOL_MAX_CONTEXTS_PER_BROWSER, 1),
            "per_key_browser_counts": per_key_browser_counts,
        }


_pool_registry: Dict[int, BrowserRuntimePool] = {}
_pool_registry_lock = threading.Lock()


def get_browser_runtime_pool() -> BrowserRuntimePool:
    loop = asyncio.get_running_loop()
    loop_id = id(loop)

    with _pool_registry_lock:
        stale_loop_ids = [
            pool_id
            for pool_id, pool in _pool_registry.items()
            if pool.loop.is_closed()
        ]
        for stale_loop_id in stale_loop_ids:
            _pool_registry.pop(stale_loop_id, None)

        pool = _pool_registry.get(loop_id)
        if pool is None:
            pool = BrowserRuntimePool(loop)
            _pool_registry[loop_id] = pool
        return pool


async def close_all_browser_runtime_pools(timeout_seconds: float = 8.0) -> None:
    timeout = max(float(timeout_seconds or 0), 1.0)
    current_loop = asyncio.get_running_loop()
    pending = []

    with _pool_registry_lock:
        pools = list(_pool_registry.values())

    for pool in pools:
        if pool.loop.is_closed():
            continue
        if pool.loop is current_loop:
            try:
                await pool.close_all()
            except Exception:
                pass
            continue
        try:
            pending.append(asyncio.run_coroutine_threadsafe(pool.close_all(), pool.loop))
        except Exception:
            continue

    for future in pending:
        try:
            future.result(timeout=timeout)
        except Exception:
            pass

    with _pool_registry_lock:
        stale_loop_ids = [
            pool_id
            for pool_id, pool in _pool_registry.items()
            if pool.loop.is_closed()
        ]
        for stale_loop_id in stale_loop_ids:
            _pool_registry.pop(stale_loop_id, None)


def snapshot_browser_runtime_metrics() -> dict:
    with _pool_registry_lock:
        pool_snapshots = [pool.snapshot_stats() for pool in _pool_registry.values()]

    aggregate = {
        "pool_count": len(pool_snapshots),
        "acquires": 0,
        "reuse_hits": 0,
        "launches": 0,
        "releases": 0,
        "cleanup_closes": 0,
        "waits": 0,
        "wait_timeouts": 0,
        "launch_failures": 0,
        "circuit_open_rejections": 0,
        "pooled_browsers": 0,
        "active_leases": 0,
    }
    for snapshot in pool_snapshots:
        for key in aggregate:
            if key == "pool_count":
                continue
            aggregate[key] += int(snapshot.get(key, 0))
    return aggregate
