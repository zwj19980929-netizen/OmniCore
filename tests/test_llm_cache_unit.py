import asyncio

from config.settings import settings
from core.llm_cache import LLMCache


def test_normalize_url_strips_fragment_and_normalizes_host():
    cache = LLMCache(max_entries=4)

    assert cache.normalize_url("HTTPS://Example.com#frag") == "https://example.com/"
    assert (
        cache.normalize_url(" https://Example.com/path?a=1#section ")
        == "https://example.com/path?a=1"
    )


def test_cache_hit_and_expiry(monkeypatch):
    cache = LLMCache(max_entries=4)
    now = {"value": 100.0}
    monkeypatch.setattr(cache, "_now", lambda: now["value"])

    cache.set("url:key", {"url": "https://example.com"}, ttl_seconds=10)

    assert cache.get("url:key") == {"url": "https://example.com"}

    now["value"] = 111.0

    assert cache.get("url:key") is None

    stats = cache.snapshot_stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert stats["expirations"] == 1


def test_cache_uses_lru_eviction_when_capacity_is_exceeded(monkeypatch):
    cache = LLMCache(max_entries=2)
    monkeypatch.setattr(cache, "_now", lambda: 100.0)

    cache.set("k1", 1, ttl_seconds=60)
    cache.set("k2", 2, ttl_seconds=60)
    assert cache.get("k1") == 1

    cache.set("k3", 3, ttl_seconds=60)

    assert cache.get("k2") is None
    assert cache.get("k1") == 1
    assert cache.get("k3") == 3
    assert cache.snapshot_stats()["evictions"] == 1


def test_singleflight_reuses_same_inflight_compute(monkeypatch):
    async def _run():
        monkeypatch.setattr(settings, "LLM_CACHE_ENABLED", True)
        monkeypatch.setattr(settings, "LLM_CACHE_INFLIGHT_WAIT_SECONDS", 1)
        cache = LLMCache(max_entries=4)
        calls = {"count": 0}

        async def _compute():
            calls["count"] += 1
            await asyncio.sleep(0.05)
            return {"ok": True, "count": calls["count"]}

        first, second = await asyncio.gather(
            cache.run_singleflight("shared:key", _compute),
            cache.run_singleflight("shared:key", _compute),
        )

        assert first == second
        assert calls["count"] == 1
        stats = cache.snapshot_stats()
        assert stats["singleflight_waits"] == 1
        assert stats["singleflight_timeouts"] == 0

    asyncio.run(_run())


def test_namespace_limit_evicts_oldest_entry_in_same_namespace(monkeypatch):
    monkeypatch.setattr(settings, "LLM_CACHE_ENABLED", True)
    cache = LLMCache(
        max_entries=4,
        namespace_limits={"page_structure_analysis": 2},
    )
    monkeypatch.setattr(cache, "_now", lambda: 100.0)

    cache.set(
        "page_structure_analysis:k1",
        {"value": 1},
        ttl_seconds=60,
        metadata={"namespace": "page_structure_analysis", "normalized_url": "https://example.com/a"},
    )
    cache.set(
        "page_structure_analysis:k2",
        {"value": 2},
        ttl_seconds=60,
        metadata={"namespace": "page_structure_analysis", "normalized_url": "https://example.com/b"},
    )
    cache.set(
        "page_structure_analysis:k3",
        {"value": 3},
        ttl_seconds=60,
        metadata={"namespace": "page_structure_analysis", "normalized_url": "https://example.com/c"},
    )

    assert cache.get("page_structure_analysis:k1") is None
    assert cache.get("page_structure_analysis:k2") == {"value": 2}
    assert cache.get("page_structure_analysis:k3") == {"value": 3}
    assert cache.snapshot_stats()["namespace_sizes"]["page_structure_analysis"] == 2


def test_cache_can_clear_by_namespace_url_prefix_and_prompt_version(monkeypatch):
    monkeypatch.setattr(settings, "LLM_CACHE_ENABLED", True)
    cache = LLMCache(max_entries=6)
    monkeypatch.setattr(cache, "_now", lambda: 100.0)

    cache.set(
        "page_structure_analysis:a",
        {"value": "a"},
        ttl_seconds=60,
        metadata={
            "namespace": "page_structure_analysis",
            "normalized_url": "https://example.com/tasks",
            "prompt_version": "v1",
        },
    )
    cache.set(
        "page_structure_analysis:b",
        {"value": "b"},
        ttl_seconds=60,
        metadata={
            "namespace": "page_structure_analysis",
            "normalized_url": "https://example.com/tasks/archive",
            "prompt_version": "v2",
        },
    )
    cache.set(
        "url_analysis:c",
        {"value": "c"},
        ttl_seconds=60,
        metadata={
            "namespace": "url_analysis",
            "prompt_version": "v1",
        },
    )

    removed_by_prefix = cache.clear_by_url_prefix(
        "page_structure_analysis",
        "https://example.com/tasks",
    )
    assert removed_by_prefix == 2

    cache.set(
        "page_structure_analysis:d",
        {"value": "d"},
        ttl_seconds=60,
        metadata={
            "namespace": "page_structure_analysis",
            "normalized_url": "https://example.com/other",
            "prompt_version": "v1",
        },
    )
    removed_by_prompt = cache.clear_by_prompt_version("page_structure_analysis", "v1")
    removed_by_namespace = cache.clear_namespace("url_analysis")

    assert removed_by_prompt == 1
    assert removed_by_namespace == 1
    assert cache.snapshot_stats()["size"] == 0
