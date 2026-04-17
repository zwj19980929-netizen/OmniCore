"""
Unit tests for memory/entity_index.py (A2).

Backed by a fake store so tests don't hit ChromaDB.
"""
from __future__ import annotations

from typing import Any, Dict, List

from memory.entity_index import EntityIndex, EntityRecord


class _FakeCollection:
    def __init__(self) -> None:
        self.records: Dict[str, Dict[str, Any]] = {}

    def get(self, *, ids=None, where=None):
        if ids is not None:
            result_ids, docs, metas = [], [], []
            for i in ids:
                rec = self.records.get(i)
                if rec is not None:
                    result_ids.append(i)
                    docs.append(rec["content"])
                    metas.append(rec["metadata"])
            return {"ids": result_ids, "documents": docs, "metadatas": metas}
        return {
            "ids": list(self.records.keys()),
            "documents": [r["content"] for r in self.records.values()],
            "metadatas": [r["metadata"] for r in self.records.values()],
        }

    def query(self, *, query_texts, n_results, where=None):
        # naive substring match for tests
        q = (query_texts[0] or "").lower()
        ids, docs, metas, dists = [], [], [], []
        for rec_id, rec in self.records.items():
            if q in rec["content"].lower() or q in rec["metadata"].get("entity_text", "").lower():
                ids.append(rec_id)
                docs.append(rec["content"])
                metas.append(rec["metadata"])
                dists.append(0.1)
        return {
            "ids": [ids[:n_results]],
            "documents": [docs[:n_results]],
            "metadatas": [metas[:n_results]],
            "distances": [dists[:n_results]],
        }

    def upsert(self, *, ids, documents, metadatas):
        for i, d, m in zip(ids, documents, metadatas):
            self.records[i] = {"content": d, "metadata": dict(m)}

    def add(self, *, ids, documents, metadatas):
        self.upsert(ids=ids, documents=documents, metadatas=metadatas)

    def delete(self, ids):
        for i in ids:
            self.records.pop(i, None)


class _FakeStore:
    """Minimal stand-in for ChromaMemory with the methods EntityIndex needs."""

    DEDUP_DISTANCE_THRESHOLD = 0.15

    def __init__(self) -> None:
        self._collection = _FakeCollection()

    # ---- methods used by EntityIndex ----
    def _memory_id_from_fingerprint(self, fingerprint: str) -> str:
        import hashlib
        return "mem_" + hashlib.sha1(fingerprint.encode()).hexdigest()[:12]

    def _get_single_record(self, memory_id: str) -> Dict[str, Any]:
        rec = self._collection.records.get(memory_id)
        if rec is None:
            return {}
        return {
            "id": memory_id,
            "content": rec["content"],
            "metadata": dict(rec["metadata"]),
        }

    def add_memory(self, *, content, metadata, memory_type, scope=None,
                   fingerprint="", allow_update=False, skip_dedup=False):
        memory_id = self._memory_id_from_fingerprint(fingerprint) if fingerprint else f"mem_{len(self._collection.records)}"
        meta = dict(metadata or {})
        meta["type"] = memory_type
        meta["fingerprint"] = fingerprint
        self._collection.upsert(
            ids=[memory_id], documents=[content], metadatas=[meta],
        )
        return memory_id

    def search_memory(self, query, n_results=5, memory_type=None, **kw):
        q_result = self._collection.query(query_texts=[query], n_results=n_results)
        out: List[Dict[str, Any]] = []
        for idx, mid in enumerate(q_result["ids"][0]):
            meta = q_result["metadatas"][0][idx]
            if memory_type and meta.get("type") != memory_type:
                continue
            out.append(
                {
                    "id": mid,
                    "content": q_result["documents"][0][idx],
                    "metadata": meta,
                    "distance": q_result["distances"][0][idx],
                }
            )
        return out[:n_results]


class TestEntityIndex:
    def _make(self, monkeypatch):
        from config.settings import settings as _settings
        monkeypatch.setattr(_settings, "MEMORY_ENTITY_INDEX_ENABLED", True)
        store = _FakeStore()
        index = EntityIndex(store=store)
        return index, store

    def test_record_increments_occurrence(self, monkeypatch):
        index, store = self._make(monkeypatch)
        mid1 = index.record("ORG", "Acme", memory_id="mem_a")
        mid2 = index.record("ORG", "Acme", memory_id="mem_b")
        assert mid1 and mid1 == mid2  # same fingerprint → same id
        meta = store._collection.records[mid1]["metadata"]
        assert meta["occurrence_count"] == 2
        assert "mem_a" in meta["related_memory_ids"].split(",")
        assert "mem_b" in meta["related_memory_ids"].split(",")

    def test_record_rejects_blank_text(self, monkeypatch):
        index, _ = self._make(monkeypatch)
        assert index.record("ORG", "") == ""

    def test_record_many_returns_count(self, monkeypatch):
        index, store = self._make(monkeypatch)
        written = index.record_many(
            [
                {"type": "ORG", "text": "Acme"},
                {"type": "PERSON", "text": "Alice"},
                {"type": "ORG", "text": ""},  # should be skipped
            ],
            memory_id="mem_1",
        )
        assert written == 2
        assert len(store._collection.records) == 2

    def test_search_returns_matching_entities(self, monkeypatch):
        index, _ = self._make(monkeypatch)
        index.record("ORG", "Acme", memory_id="mem_1")
        index.record("PERSON", "Bob", memory_id="mem_2")
        results = index.search("Acme")
        assert len(results) >= 1
        assert any(r.entity_text == "Acme" for r in results)

    def test_search_filters_by_type(self, monkeypatch):
        index, _ = self._make(monkeypatch)
        index.record("ORG", "Acme", memory_id="mem_1")
        index.record("PERSON", "Acme-like", memory_id="mem_2")
        results = index.search("Acme", entity_type="ORG")
        assert len(results) == 1
        assert results[0].entity_type == "ORG"

    def test_top_entities_sorted_by_count(self, monkeypatch):
        index, _ = self._make(monkeypatch)
        # Acme seen 3 times
        for _ in range(3):
            index.record("ORG", "Acme", memory_id="mem_x")
        # OnceCo seen 1 time
        index.record("ORG", "OnceCo", memory_id="mem_y")
        top = index.top_entities(entity_type="ORG", limit=5)
        assert [r.entity_text for r in top] == ["Acme", "OnceCo"]
        assert top[0].occurrence_count == 3
        assert top[1].occurrence_count == 1

    def test_disabled_returns_empty(self, monkeypatch):
        from config.settings import settings as _settings
        monkeypatch.setattr(_settings, "MEMORY_ENTITY_INDEX_ENABLED", False)
        index = EntityIndex(store=_FakeStore())
        assert index.record("ORG", "Acme") == ""
        assert index.search("Acme") == []
        assert index.top_entities() == []

    def test_delete_by_entity_drops_matching_rows(self, monkeypatch):
        index, store = self._make(monkeypatch)
        index.record("ORG", "Acme", memory_id="m1")
        index.record("ORG", "Acme", memory_id="m2")
        index.record("ORG", "OnceCo", memory_id="m3")
        index.record("PERSON", "Acme", memory_id="m4")  # same text, different type
        deleted = index.delete_by_entity("Acme", entity_type="ORG")
        assert deleted == 1  # one fingerprint row accumulates both m1 and m2
        # OnceCo and PERSON:Acme should still be present
        remaining = index.top_entities(limit=10)
        texts = {(r.entity_type, r.entity_text) for r in remaining}
        assert ("ORG", "OnceCo") in texts
        assert ("PERSON", "Acme") in texts
        assert ("ORG", "Acme") not in texts

    def test_delete_by_entity_case_insensitive(self, monkeypatch):
        index, _ = self._make(monkeypatch)
        index.record("ORG", "Acme", memory_id="m1")
        assert index.delete_by_entity("ACME") == 1

    def test_delete_by_entity_rejects_blank(self, monkeypatch):
        index, _ = self._make(monkeypatch)
        assert index.delete_by_entity("") == 0

    def test_delete_by_entity_disabled_returns_zero(self, monkeypatch):
        from config.settings import settings as _settings
        monkeypatch.setattr(_settings, "MEMORY_ENTITY_INDEX_ENABLED", False)
        index = EntityIndex(store=_FakeStore())
        assert index.delete_by_entity("anything") == 0
