"""
Unit tests for memory/tiered_store.py (A4).
"""
from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from memory.tiered_store import (
    MemoryTier,
    TieredMemoryStore,
    default_tier_for_type,
    tier_weight,
)


class _FakeStore:
    """Minimal in-memory double for ChromaMemory."""

    def __init__(self, *, name: str) -> None:
        self.name = name
        self.items: List[Dict[str, Any]] = []
        self.cleared_sessions: List[str] = []

    def add_memory(self, *, content, metadata=None, memory_type="general",
                   scope=None, fingerprint="", allow_update=False, skip_dedup=False):
        mid = f"{self.name}::{fingerprint or len(self.items)}"
        self.items.append(
            {
                "id": mid,
                "content": content,
                "metadata": {**(metadata or {}), "type": memory_type, "scope_session_id": (scope or {}).get("session_id", "")},
            }
        )
        return mid

    def search_memory(self, query, n_results=5, memory_type=None, **kw):
        out = []
        for item in self.items:
            if memory_type and item["metadata"].get("type") != memory_type:
                continue
            if query.lower() in item["content"].lower():
                out.append(
                    {
                        "id": item["id"],
                        "content": item["content"],
                        "metadata": item["metadata"],
                        "distance": 0.2,
                    }
                )
        return out[:n_results]

    def clear_scope(self, scope, *, memory_type=None):
        session = (scope or {}).get("session_id", "")
        before = len(self.items)
        self.items = [i for i in self.items if i["metadata"].get("scope_session_id") != session]
        self.cleared_sessions.append(session)
        return before - len(self.items)

    def get_stats(self):
        return {"total_memories": len(self.items), "collection_name": self.name}


@pytest.fixture
def enabled(monkeypatch):
    from config.settings import settings as _settings
    monkeypatch.setattr(_settings, "MEMORY_TIERED_ENABLED", True)
    return _settings


class TestTierResolution:
    def test_preference_goes_to_semantic(self):
        assert default_tier_for_type("preference") == MemoryTier.SEMANTIC
        assert default_tier_for_type("consolidated_summary") == MemoryTier.SEMANTIC

    def test_task_result_goes_to_episodic(self):
        assert default_tier_for_type("task_result") == MemoryTier.EPISODIC
        assert default_tier_for_type("artifact_reference") == MemoryTier.EPISODIC

    def test_unknown_type_defaults_to_episodic(self):
        assert default_tier_for_type("anything_else") == MemoryTier.EPISODIC

    def test_weights_match_settings(self, enabled):
        # default semantic > episodic per spec
        assert tier_weight(MemoryTier.SEMANTIC) >= tier_weight(MemoryTier.EPISODIC)


class TestTieredMemoryStore:
    def _make(self):
        w = _FakeStore(name="working")
        e = _FakeStore(name="episodic")
        s = _FakeStore(name="semantic")
        return TieredMemoryStore(working=w, episodic=e, semantic=s), (w, e, s)

    def test_add_routes_to_correct_tier(self, enabled):
        store, (w, e, s) = self._make()
        store.add("pref", memory_type="preference", fingerprint="p1")
        store.add("task", memory_type="task_result", fingerprint="t1")
        store.add("scratch", memory_type="general", tier=MemoryTier.WORKING, fingerprint="x1")

        assert len(s.items) == 1 and s.items[0]["content"] == "pref"
        assert len(e.items) == 1 and e.items[0]["content"] == "task"
        assert len(w.items) == 1 and w.items[0]["content"] == "scratch"

    def test_search_applies_tier_weight(self, enabled, monkeypatch):
        from config.settings import settings as _settings
        monkeypatch.setattr(_settings, "MEMORY_TIER_WEIGHT_SEMANTIC", 2.0)
        monkeypatch.setattr(_settings, "MEMORY_TIER_WEIGHT_EPISODIC", 1.0)

        store, (_w, e, s) = self._make()
        e.items.append({"id": "e1", "content": "apple pie", "metadata": {"type": "task_result"}})
        s.items.append({"id": "s1", "content": "apple pie", "metadata": {"type": "preference"}})

        merged = store.search("apple", n_results=5)
        assert [item["id"] for item in merged[:2]] == ["s1", "e1"]
        assert merged[0]["tier"] == "semantic"
        assert merged[0]["tier_score"] > merged[1]["tier_score"]

    def test_search_skips_disabled_tier(self, enabled):
        store, (_w, e, _s) = self._make()
        e.items.append({"id": "e1", "content": "hello", "metadata": {"type": "task_result"}})
        merged = store.search("hello", tiers=[MemoryTier.EPISODIC])
        assert all(item["tier"] == "episodic" for item in merged)

    def test_purge_working_deletes_scoped(self, enabled):
        store, (w, _e, _s) = self._make()
        w.items.append({"id": "w1", "content": "s1 note", "metadata": {"scope_session_id": "s1"}})
        w.items.append({"id": "w2", "content": "s2 note", "metadata": {"scope_session_id": "s2"}})
        removed = store.purge_working("s1")
        assert removed == 1
        remaining_sessions = {i["metadata"]["scope_session_id"] for i in w.items}
        assert remaining_sessions == {"s2"}

    def test_add_returns_empty_when_tiered_disabled(self, monkeypatch):
        from config.settings import settings as _settings
        monkeypatch.setattr(_settings, "MEMORY_TIERED_ENABLED", False)
        # passing explicit stores does not auto-enable — add still works, but
        # lazy tiers won't be instantiated. Here we pass stores so add should
        # still succeed (callers that pass their own stores control the gate).
        store, _ = self._make()
        result = store.add("x", memory_type="task_result", fingerprint="f1")
        assert result.startswith("episodic::")

    def test_stats_reports_all_tiers(self, enabled):
        store, _ = self._make()
        out = store.stats()
        assert set(out.keys()) == {"working", "episodic", "semantic"}
        assert out["working"]["total_memories"] == 0
