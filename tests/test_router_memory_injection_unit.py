"""
Unit tests for router memory-injection hooks:

- ``_build_top_entities_block`` (A2): ``MEMORY_INJECT_TOP_ENTITIES`` gates
  a block built from ``EntityIndex.top_entities``.
- ``_build_inferred_preferences_block`` (A5): ``PREFERENCE_INJECT_TO_ROUTER``
  gates a block built from semantic-tier preferences whose metadata carries
  ``source=inferred``.
"""
from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from memory.entity_index import EntityRecord


# ---------------------------------------------------------------------------
# A2: top-entities block
# ---------------------------------------------------------------------------


class TestTopEntitiesBlock:
    def _settings(self, monkeypatch, *, inject=True, indexed=True, top_k=5):
        from config.settings import settings as _settings
        monkeypatch.setattr(_settings, "MEMORY_INJECT_TOP_ENTITIES", inject)
        monkeypatch.setattr(_settings, "MEMORY_ENTITY_INDEX_ENABLED", indexed)
        monkeypatch.setattr(_settings, "MEMORY_ENTITY_TOP_K", top_k)

    def test_empty_when_injection_disabled(self, monkeypatch):
        from core.router import RouterAgent
        self._settings(monkeypatch, inject=False)
        assert RouterAgent._build_top_entities_block() == ""

    def test_empty_when_entity_index_disabled(self, monkeypatch):
        from core.router import RouterAgent
        self._settings(monkeypatch, indexed=False)
        assert RouterAgent._build_top_entities_block() == ""

    def test_renders_grouped_by_type(self, monkeypatch):
        from core.router import RouterAgent
        self._settings(monkeypatch)

        class _StubIndex:
            def top_entities(self, *, limit=10):
                return [
                    EntityRecord("ORG", "Acme", occurrence_count=5),
                    EntityRecord("ORG", "OnceCo", occurrence_count=2),
                    EntityRecord("PERSON", "Alice", occurrence_count=3),
                ]

        with patch("memory.entity_index.EntityIndex", lambda: _StubIndex()):
            block = RouterAgent._build_top_entities_block()
        assert "Recently-active entities" in block
        assert "Acme" in block and "OnceCo" in block
        assert "Alice" in block
        # grouped: ORG line mentions both items
        assert "ORG: " in block
        assert "PERSON: " in block

    def test_skips_zero_count_entries(self, monkeypatch):
        from core.router import RouterAgent
        self._settings(monkeypatch)

        class _StubIndex:
            def top_entities(self, *, limit=10):
                return [EntityRecord("ORG", "Ghost", occurrence_count=0)]

        with patch("memory.entity_index.EntityIndex", lambda: _StubIndex()):
            assert RouterAgent._build_top_entities_block() == ""

    def test_swallows_errors(self, monkeypatch):
        from core.router import RouterAgent
        self._settings(monkeypatch)

        class _Boom:
            def top_entities(self, **kw):
                raise RuntimeError("chroma down")

        with patch("memory.entity_index.EntityIndex", lambda: _Boom()):
            assert RouterAgent._build_top_entities_block() == ""


# ---------------------------------------------------------------------------
# A5: inferred-preferences block
# ---------------------------------------------------------------------------


class _StubCollection:
    def __init__(self, rows: List[Dict[str, Any]]):
        self.rows = rows

    def get(self, *, where=None):
        return {
            "ids": [r["id"] for r in self.rows],
            "documents": [r.get("content", "") for r in self.rows],
            "metadatas": [r["metadata"] for r in self.rows],
        }


class _StubChroma:
    def __init__(self, rows: List[Dict[str, Any]]):
        self._collection = _StubCollection(rows)


class TestInferredPreferencesBlock:
    def _settings(self, monkeypatch, *, inject=True, learning=True, min_conf=0.5):
        from config.settings import settings as _settings
        monkeypatch.setattr(_settings, "PREFERENCE_INJECT_TO_ROUTER", inject)
        monkeypatch.setattr(_settings, "PREFERENCE_LEARNING_ENABLED", learning)
        monkeypatch.setattr(_settings, "PREFERENCE_LEARNING_MIN_CONFIDENCE", min_conf)
        monkeypatch.setattr(_settings, "MEMORY_TIERED_ENABLED", True)

    def test_empty_when_disabled(self, monkeypatch):
        from core.router import RouterAgent
        self._settings(monkeypatch, inject=False)
        assert RouterAgent._build_inferred_preferences_block() == ""

    def test_renders_inferred_rows(self, monkeypatch):
        from core.router import RouterAgent
        self._settings(monkeypatch)
        rows = [
            {
                "id": "p1",
                "content": "User preference - preferred_tool: web_worker",
                "metadata": {
                    "type": "preference",
                    "source": "inferred",
                    "preference_key": "preferred_tool",
                    "preference_value": "web_worker",
                    "confidence": 0.8,
                    "notes": "4/5 jobs",
                },
            },
            {
                "id": "p2",
                "content": "User preference - something_low: x",
                "metadata": {
                    "type": "preference",
                    "source": "inferred",
                    "preference_key": "something_low",
                    "preference_value": "x",
                    "confidence": 0.1,
                },
            },
            {
                "id": "p3",
                "content": "User preference - manual_pref: y",
                "metadata": {
                    "type": "preference",
                    "source": "runtime_preferences",  # not inferred → skip
                    "preference_key": "manual_pref",
                    "preference_value": "y",
                    "confidence": 0.9,
                },
            },
        ]
        stub = _StubChroma(rows)
        with patch("memory.scoped_chroma_store.ChromaMemory", lambda *a, **kw: stub):
            block = RouterAgent._build_inferred_preferences_block()
        assert "preferred_tool" in block
        assert "web_worker" in block
        assert "confidence=80%" in block
        # below threshold row suppressed
        assert "something_low" not in block
        # non-inferred row suppressed
        assert "manual_pref" not in block

    def test_swallows_errors(self, monkeypatch):
        from core.router import RouterAgent
        self._settings(monkeypatch)

        def _boom(*a, **kw):
            raise RuntimeError("chroma init failed")

        with patch("memory.scoped_chroma_store.ChromaMemory", _boom):
            assert RouterAgent._build_inferred_preferences_block() == ""
