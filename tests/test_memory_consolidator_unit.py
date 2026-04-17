"""
Unit tests for memory/consolidator.py (A1).

Uses a FakeStore double instead of real Chroma so tests are hermetic.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List
from unittest.mock import patch

from memory.consolidator import (
    _group_by_scope,
    _scope_from_metadata,
    consolidate_expired,
)


_NOW = datetime(2026, 4, 16, 12, 0, 0)


def _iso(days_ago: float) -> str:
    return (_NOW - timedelta(days=days_ago)).isoformat(timespec="seconds")


class _FakeCollection:
    def __init__(self, records: List[Dict[str, Any]]):
        self._records = {r["id"]: r for r in records}
        self.added: List[Dict[str, Any]] = []
        self.deleted: List[str] = []

    def get(self, *, where=None):
        ids, docs, metas = [], [], []
        for rec in self._records.values():
            ids.append(rec["id"])
            docs.append(rec["content"])
            metas.append(rec["metadata"])
        return {"ids": ids, "documents": docs, "metadatas": metas}

    def delete(self, ids):
        for i in ids:
            self._records.pop(i, None)
            self.deleted.append(i)


class _FakeStore:
    def __init__(self, records: List[Dict[str, Any]]):
        self._collection = _FakeCollection(records)
        self.writes: List[Dict[str, Any]] = []

    def add_memory(self, *, content, metadata, memory_type, scope=None,
                   fingerprint="", allow_update=False, skip_dedup=False):
        self.writes.append(
            {
                "content": content,
                "metadata": metadata,
                "memory_type": memory_type,
                "scope": scope,
                "fingerprint": fingerprint,
            }
        )
        return fingerprint or f"mem_{len(self.writes)}"


def _make_record(
    mem_id: str,
    *,
    created_days_ago: float,
    hit_count: int = 0,
    scope_key: str = "session_id:s1",
    session_id: str = "s1",
    mem_type: str = "task_result",
    content: str = "some content",
) -> Dict[str, Any]:
    return {
        "id": mem_id,
        "content": content,
        "metadata": {
            "created_at": _iso(created_days_ago),
            "updated_at": _iso(created_days_ago),
            "hit_count": hit_count,
            "type": mem_type,
            "scope_key": scope_key,
            "scope_session_id": session_id,
        },
    }


class TestScopeFromMetadata:
    def test_extracts_session_scope(self):
        scope = _scope_from_metadata(
            {"scope_session_id": "s1", "scope_project_id": "p1"}
        )
        assert scope == {"session_id": "s1", "project_id": "p1"}

    def test_ignores_unknown_keys(self):
        assert _scope_from_metadata({"foo": "bar"}) == {}


class TestGroupByScope:
    def test_same_scope_groups_together(self):
        records = [
            _make_record("a", created_days_ago=5, scope_key="session_id:s1"),
            _make_record("b", created_days_ago=6, scope_key="session_id:s1"),
            _make_record("c", created_days_ago=7, scope_key="session_id:s2"),
        ]
        batches = _group_by_scope(records, batch_size=10)
        assert len(batches) == 2
        all_scopes = {r["metadata"]["scope_key"] for batch in batches for r in batch}
        assert all_scopes == {"session_id:s1", "session_id:s2"}

    def test_chunks_respect_batch_size(self):
        records = [
            _make_record(f"r{i}", created_days_ago=i + 1, scope_key="same")
            for i in range(7)
        ]
        batches = _group_by_scope(records, batch_size=3)
        assert [len(b) for b in batches] == [3, 3, 1]


class TestConsolidateExpired:
    def _setup_settings(self, monkeypatch, *, llm_enabled=False, ttl_days=10,
                        min_hits=1, batch_size=5):
        from config.settings import settings as _settings
        monkeypatch.setattr(_settings, "MEMORY_CONSOLIDATION_ENABLED", llm_enabled)
        monkeypatch.setattr(_settings, "MEMORY_TTL_DAYS", ttl_days)
        monkeypatch.setattr(_settings, "MEMORY_CONSOLIDATION_MIN_HITS", min_hits)
        monkeypatch.setattr(_settings, "MEMORY_CONSOLIDATION_BATCH_SIZE", batch_size)
        monkeypatch.setattr(_settings, "MEMORY_CONSOLIDATION_MODEL", "")

    def test_deletes_never_used_stale_records(self, monkeypatch):
        self._setup_settings(monkeypatch)
        records = [
            _make_record("cold_old", created_days_ago=30, hit_count=0),
            _make_record("hot_old", created_days_ago=30, hit_count=5),
            _make_record("fresh", created_days_ago=2, hit_count=0),
        ]
        store = _FakeStore(records)
        report = consolidate_expired(store, now=_NOW)
        # cold_old is deleted; hot_old is kept (llm disabled); fresh untouched
        assert "cold_old" in store._collection.deleted
        assert "hot_old" not in store._collection.deleted
        assert "fresh" not in store._collection.deleted
        assert report.deleted_never_used == 1
        assert report.consolidated_memories == 0

    def test_dry_run_does_not_write_or_delete(self, monkeypatch):
        self._setup_settings(monkeypatch)
        records = [_make_record("cold_old", created_days_ago=30, hit_count=0)]
        store = _FakeStore(records)
        report = consolidate_expired(store, dry_run=True, now=_NOW)
        assert store._collection.deleted == []
        assert store.writes == []
        assert report.deleted_never_used == 1  # count is reported

    def test_skips_preferences_and_summaries(self, monkeypatch):
        self._setup_settings(monkeypatch)
        records = [
            _make_record("pref", created_days_ago=30, hit_count=0, mem_type="preference"),
            _make_record("summary", created_days_ago=30, hit_count=0, mem_type="consolidated_summary"),
            _make_record("skill", created_days_ago=30, hit_count=0, mem_type="skill_definition"),
        ]
        store = _FakeStore(records)
        report = consolidate_expired(store, now=_NOW)
        assert store._collection.deleted == []
        assert report.scanned == 0

    def test_llm_consolidation_writes_summary_and_deletes(self, monkeypatch):
        self._setup_settings(monkeypatch, llm_enabled=True, min_hits=1)
        records = [
            _make_record("a", created_days_ago=30, hit_count=2, scope_key="session_id:s1"),
            _make_record("b", created_days_ago=30, hit_count=3, scope_key="session_id:s1"),
        ]
        store = _FakeStore(records)

        class _FakeResp:
            content = (
                '{"summary": "用户在 s1 做了两件事", '
                '"key_points": ["点1"], "time_range": "2026-03-15 ~ 2026-03-17", '
                '"entities": ["Acme"]}'
            )

        class _FakeLLM:
            def __init__(self, *a, **kw):
                pass

            def chat_with_system(self, **kw):
                return _FakeResp()

        with patch("core.llm.LLMClient", _FakeLLM):
            report = consolidate_expired(store, now=_NOW)

        assert report.consolidated_groups == 1
        assert report.consolidated_memories == 2
        assert len(store.writes) == 1
        written = store.writes[0]
        assert written["memory_type"] == "consolidated_summary"
        assert "用户在 s1 做了两件事" in written["content"]
        assert set(store._collection.deleted) == {"a", "b"}

    def test_llm_topics_too_diverse_is_skipped(self, monkeypatch):
        self._setup_settings(monkeypatch, llm_enabled=True, min_hits=1)
        records = [
            _make_record("a", created_days_ago=30, hit_count=2, scope_key="session_id:s1"),
            _make_record("b", created_days_ago=30, hit_count=3, scope_key="session_id:s1"),
        ]
        store = _FakeStore(records)

        class _Resp:
            content = '{"summary": "", "key_points": ["topics_too_diverse"]}'

        class _FakeLLM:
            def __init__(self, *a, **kw):
                pass

            def chat_with_system(self, **kw):
                return _Resp()

        with patch("core.llm.LLMClient", _FakeLLM):
            report = consolidate_expired(store, now=_NOW)

        assert report.skipped_diverse == 1
        assert report.consolidated_groups == 0
        assert store.writes == []
        # originals should not be deleted when summary rejected
        assert "a" not in store._collection.deleted
        assert "b" not in store._collection.deleted
