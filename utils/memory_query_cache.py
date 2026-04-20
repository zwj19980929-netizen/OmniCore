"""Lightweight TTL cache for ChromaMemory.search_memory calls.

Caches (collection, query, n_results, memory_type) → results for TTL_SEC seconds.
Prevents duplicate embedding + Chroma queries for the same search within a single
request/session cycle (typical LangGraph turn ≤ 30 s).
"""
from __future__ import annotations

import time
from typing import Any

from config.settings import settings

_cache: dict[tuple, tuple[float, Any]] = {}  # key → (ts, results)


def _make_key(collection: str, query: str, n_results: int, memory_type: str | None) -> tuple:
    return (collection, query.strip()[:200], n_results, memory_type or "")


def get_cached(
    collection: str,
    query: str,
    n_results: int,
    memory_type: str | None = None,
) -> Any | None:
    """Return cached results if available and fresh, else None."""
    if not getattr(settings, "MEMORY_QUERY_CACHE_ENABLED", True):
        return None
    key = _make_key(collection, query, n_results, memory_type)
    entry = _cache.get(key)
    if entry is None:
        return None
    ts, results = entry
    ttl = getattr(settings, "MEMORY_QUERY_CACHE_TTL_SEC", 60)
    if time.monotonic() - ts > ttl:
        del _cache[key]
        return None
    return results


def set_cached(
    collection: str,
    query: str,
    n_results: int,
    memory_type: str | None,
    results: Any,
) -> None:
    """Store results in cache."""
    if not getattr(settings, "MEMORY_QUERY_CACHE_ENABLED", True):
        return
    key = _make_key(collection, query, n_results, memory_type)
    _cache[key] = (time.monotonic(), results)
    if len(_cache) > 256:
        oldest = min(_cache, key=lambda k: _cache[k][0])
        del _cache[oldest]


def clear() -> None:
    """Clear cache (used in tests)."""
    _cache.clear()
