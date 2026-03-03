"""
Lightweight cache for deterministic or semi-stable LLM analysis results.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

from config.settings import settings


@dataclass
class _CacheEntry:
    value: Any
    expires_at: float
    created_at: float
    metadata: Dict[str, str] = field(default_factory=dict)


@dataclass
class _InflightCall:
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future


class LLMCache:
    def __init__(
        self,
        max_entries: Optional[int] = None,
        namespace_limits: Optional[Dict[str, int]] = None,
    ):
        self.max_entries = max_entries or settings.LLM_CACHE_MAX_ENTRIES
        self._namespace_limits = self._build_namespace_limits(namespace_limits)
        self._entries: "OrderedDict[str, _CacheEntry]" = OrderedDict()
        self._inflight: Dict[str, _InflightCall] = {}
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._sets = 0
        self._evictions = 0
        self._expirations = 0
        self._singleflight_waits = 0
        self._singleflight_timeouts = 0

    def _build_namespace_limits(
        self,
        custom_limits: Optional[Dict[str, int]],
    ) -> Dict[str, int]:
        defaults = {
            "url_analysis": min(
                max(settings.LLM_CACHE_URL_ANALYSIS_MAX_ENTRIES, 1),
                self.max_entries,
            ),
            "page_structure_analysis": min(
                max(settings.LLM_CACHE_PAGE_ANALYSIS_MAX_ENTRIES, 1),
                self.max_entries,
            ),
        }
        if not custom_limits:
            return defaults
        merged = dict(defaults)
        for namespace, limit in custom_limits.items():
            merged[str(namespace)] = min(max(int(limit), 1), self.max_entries)
        return merged

    def _now(self) -> float:
        return time.time()

    def _prune_expired_locked(self, now: Optional[float] = None) -> None:
        current = now if now is not None else self._now()
        expired_keys = [
            key for key, entry in self._entries.items()
            if entry.expires_at <= current
        ]
        self._expirations += len(expired_keys)
        for key in expired_keys:
            self._entries.pop(key, None)

    def _apply_namespace_limit_locked(self, namespace: str) -> None:
        limit = self._namespace_limits.get(namespace)
        if not limit:
            return
        namespace_keys = [
            key
            for key, entry in self._entries.items()
            if entry.metadata.get("namespace") == namespace
        ]
        while len(namespace_keys) > limit:
            evict_key = namespace_keys.pop(0)
            if self._entries.pop(evict_key, None) is not None:
                self._evictions += 1

    def _apply_global_limit_locked(self) -> None:
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
            self._evictions += 1

    def get(self, key: str) -> Any:
        if not settings.LLM_CACHE_ENABLED:
            return None

        now = self._now()
        with self._lock:
            self._prune_expired_locked(now)
            entry = self._entries.get(key)
            if entry is None:
                self._misses += 1
                return None
            self._hits += 1
            self._entries.move_to_end(key)
            return entry.value

    def set(
        self,
        key: str,
        value: Any,
        ttl_seconds: int,
        *,
        metadata: Optional[Dict[str, str]] = None,
    ) -> None:
        if not settings.LLM_CACHE_ENABLED or ttl_seconds <= 0:
            return

        now = self._now()
        resolved_metadata = dict(metadata or {})
        if "namespace" not in resolved_metadata:
            resolved_metadata["namespace"] = key.split(":", 1)[0]

        entry = _CacheEntry(
            value=value,
            expires_at=now + ttl_seconds,
            created_at=now,
            metadata=resolved_metadata,
        )

        with self._lock:
            self._prune_expired_locked(now)
            self._sets += 1
            self._entries[key] = entry
            self._entries.move_to_end(key)
            self._apply_namespace_limit_locked(resolved_metadata["namespace"])
            self._apply_global_limit_locked()

    def clear(self, prefix: str = "") -> int:
        with self._lock:
            if not prefix:
                count = len(self._entries)
                self._entries.clear()
                return count

            removed = 0
            keys = [key for key in self._entries if key.startswith(prefix)]
            for key in keys:
                self._entries.pop(key, None)
                removed += 1
            return removed

    def clear_namespace(self, namespace: str) -> int:
        namespace = (namespace or "").strip()
        if not namespace:
            return 0
        return self.clear(prefix=f"{namespace}:")

    def clear_by_url_prefix(self, namespace: str, url_prefix: str) -> int:
        normalized_prefix = self.normalize_url(url_prefix)
        if not namespace or not normalized_prefix:
            return 0
        with self._lock:
            keys = [
                key
                for key, entry in self._entries.items()
                if entry.metadata.get("namespace") == namespace
                and entry.metadata.get("normalized_url", "").startswith(normalized_prefix)
            ]
            for key in keys:
                self._entries.pop(key, None)
            return len(keys)

    def clear_by_prompt_version(self, namespace: str, prompt_version: str) -> int:
        namespace = (namespace or "").strip()
        prompt_version = (prompt_version or "").strip()
        if not namespace or not prompt_version:
            return 0
        with self._lock:
            keys = [
                key
                for key, entry in self._entries.items()
                if entry.metadata.get("namespace") == namespace
                and entry.metadata.get("prompt_version") == prompt_version
            ]
            for key in keys:
                self._entries.pop(key, None)
            return len(keys)

    def snapshot_stats(self) -> dict:
        with self._lock:
            namespace_sizes: Dict[str, int] = {}
            for entry in self._entries.values():
                namespace = entry.metadata.get("namespace", "")
                namespace_sizes[namespace] = namespace_sizes.get(namespace, 0) + 1

            return {
                "size": len(self._entries),
                "max_entries": self.max_entries,
                "hits": self._hits,
                "misses": self._misses,
                "sets": self._sets,
                "evictions": self._evictions,
                "expirations": self._expirations,
                "singleflight_waits": self._singleflight_waits,
                "singleflight_timeouts": self._singleflight_timeouts,
                "inflight_keys": len(self._inflight),
                "namespace_sizes": namespace_sizes,
                "namespace_limits": dict(self._namespace_limits),
            }

    async def run_singleflight(
        self,
        key: str,
        compute_fn: Callable[[], Any],
        *,
        wait_timeout_seconds: Optional[float] = None,
    ) -> Any:
        if not settings.LLM_CACHE_ENABLED:
            return await self._resolve_compute_result(compute_fn)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return await self._resolve_compute_result(compute_fn)

        inflight_future: Optional[asyncio.Future] = None
        created_future: Optional[asyncio.Future] = None
        cross_loop_bypass = False

        with self._lock:
            inflight = self._inflight.get(key)
            if inflight is not None and not inflight.future.done():
                if inflight.loop is loop:
                    inflight_future = inflight.future
                    self._singleflight_waits += 1
                else:
                    cross_loop_bypass = True
            if inflight_future is None and not cross_loop_bypass:
                created_future = loop.create_future()
                created_future.add_done_callback(self._consume_future_exception)
                self._inflight[key] = _InflightCall(loop=loop, future=created_future)

        if cross_loop_bypass:
            return await self._resolve_compute_result(compute_fn)

        if inflight_future is not None:
            timeout = wait_timeout_seconds
            if timeout is None:
                timeout = max(settings.LLM_CACHE_INFLIGHT_WAIT_SECONDS, 1)
            try:
                return await asyncio.wait_for(
                    asyncio.shield(inflight_future),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                with self._lock:
                    self._singleflight_timeouts += 1
                return await self._resolve_compute_result(compute_fn)

        try:
            result = await self._resolve_compute_result(compute_fn)
        except Exception as exc:
            if created_future is not None and not created_future.done():
                created_future.set_exception(exc)
            with self._lock:
                current = self._inflight.get(key)
                if current is not None and current.future is created_future:
                    self._inflight.pop(key, None)
            raise

        if created_future is not None and not created_future.done():
            created_future.set_result(result)
        with self._lock:
            current = self._inflight.get(key)
            if current is not None and current.future is created_future:
                self._inflight.pop(key, None)
        return result

    @staticmethod
    async def _resolve_compute_result(compute_fn: Callable[[], Any]) -> Any:
        result = compute_fn()
        if inspect.isawaitable(result):
            return await result
        return result

    @staticmethod
    def _consume_future_exception(future: asyncio.Future) -> None:
        if future.cancelled():
            return
        try:
            future.exception()
        except Exception:
            return

    @staticmethod
    def normalize_url(url: str) -> str:
        if not url:
            return ""
        try:
            parts = urlsplit(url.strip())
            path = parts.path or "/"
            return urlunsplit((
                parts.scheme.lower(),
                parts.netloc.lower(),
                path,
                parts.query,
                "",
            ))
        except Exception:
            return (url or "").strip().lower()

    @staticmethod
    def build_task_signature(task_description: str) -> str:
        normalized = " ".join((task_description or "").strip().lower().split())
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        return digest[:24]

    @staticmethod
    def build_page_fingerprint(content: str) -> str:
        normalized = " ".join((content or "").strip().split())
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        return digest[:24]

    def build_key(
        self,
        namespace: str,
        *,
        normalized_url: str = "",
        task_signature: str = "",
        page_fingerprint: str = "",
        prompt_version: str = "",
        model_name: str = "",
    ) -> str:
        raw = "|".join([
            namespace,
            normalized_url,
            task_signature,
            page_fingerprint,
            prompt_version,
            model_name,
        ])
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return f"{namespace}:{digest}"


_llm_cache: Optional[LLMCache] = None
_llm_cache_lock = threading.Lock()


def get_llm_cache() -> LLMCache:
    global _llm_cache
    if _llm_cache is None:
        with _llm_cache_lock:
            if _llm_cache is None:
                _llm_cache = LLMCache()
    return _llm_cache
