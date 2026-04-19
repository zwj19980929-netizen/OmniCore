"""Per-run page-assessment cache shared across strategies (B6).

Strategies can compute the same observation multiple times when they
coexist in a single run (e.g. a login-replay fallback followed by the
legacy loop). This cache reuses those results keyed by ``page_hash``.

It is deliberately tiny — an LRU-ish dict with a cap. No concurrency
primitives: callers are single-run, single-agent.
"""
from __future__ import annotations

from collections import OrderedDict
from typing import Any, Awaitable, Callable


class PageAssessmentCache:
    """Small LRU cache for page-assessment-like values.

    Identity is the opaque ``page_hash`` provided by the caller (usually
    a DOM-structure fingerprint). Values are whatever the producer
    returned.
    """

    def __init__(self, max_entries: int = 16):
        self._max = max(1, int(max_entries))
        self._store: "OrderedDict[str, Any]" = OrderedDict()

    def __len__(self) -> int:
        return len(self._store)

    def clear(self) -> None:
        self._store.clear()

    def get(self, page_hash: str):
        if not page_hash:
            return None
        value = self._store.get(page_hash)
        if value is not None:
            self._store.move_to_end(page_hash)
        return value

    def put(self, page_hash: str, value: Any) -> None:
        if not page_hash:
            return
        self._store[page_hash] = value
        self._store.move_to_end(page_hash)
        while len(self._store) > self._max:
            self._store.popitem(last=False)

    async def get_or_compute(
        self,
        page_hash: str,
        compute_fn: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Return cached value or run ``compute_fn`` and memoize."""
        if not page_hash:
            return await compute_fn()
        cached = self.get(page_hash)
        if cached is not None:
            return cached
        value = await compute_fn()
        self.put(page_hash, value)
        return value
