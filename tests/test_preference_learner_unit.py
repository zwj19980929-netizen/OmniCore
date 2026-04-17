"""
Unit tests for memory/preference_learner.py + manager.persist_inferred_preferences (A5).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List

import pytest

from memory.preference_learner import (
    PreferenceCandidate,
    _analyze_hours,
    _analyze_intents,
    _analyze_tool_usage,
    infer_preferences,
    maybe_run_learner,
    should_run_now,
)


_NOW = datetime(2026, 4, 16, 14, 0, 0)


def _iso(days_ago: float, hour: int = 14) -> str:
    dt = _NOW - timedelta(days=days_ago)
    return dt.replace(hour=hour).isoformat(timespec="seconds")


def _task_record(
    mem_id: str,
    *,
    tools: List[str],
    success: bool = True,
    intent: str = "information_query",
    days_ago: float = 1,
    hour: int = 14,
) -> Dict[str, Any]:
    return {
        "id": mem_id,
        "content": "Task: foo\nOutcome: bar",
        "metadata": {
            "type": "task_result",
            "success": success,
            "tool_sequence": ",".join(tools),
            "intent": intent,
            "created_at": _iso(days_ago, hour=hour),
            "updated_at": _iso(days_ago, hour=hour),
        },
    }


class _FakeCollection:
    def __init__(self, records: List[Dict[str, Any]]):
        self.records = records

    def get(self, *, where=None):
        return {
            "ids": [r["id"] for r in self.records],
            "documents": [r["content"] for r in self.records],
            "metadatas": [r["metadata"] for r in self.records],
        }


class _FakeStore:
    def __init__(self, records: List[Dict[str, Any]]):
        self._collection = _FakeCollection(records)


class TestAnalyzeTool:
    def test_ranks_by_success_weighted_score(self):
        records = [
            _task_record("a", tools=["web_worker"], success=True),
            _task_record("b", tools=["web_worker"], success=True),
            _task_record("c", tools=["file_worker"], success=False),
        ]
        stats = _analyze_tool_usage(records)
        top = stats["ranking"][0]
        assert top["tool"] == "web_worker"
        assert top["success"] == 2
        assert top["uses"] == 2


class TestAnalyzeIntents:
    def test_counts_intents(self):
        records = [
            _task_record("a", tools=["x"], intent="information_query"),
            _task_record("b", tools=["x"], intent="information_query"),
            _task_record("c", tools=["x"], intent="web_scraping"),
        ]
        stats = _analyze_intents(records)
        assert stats["ranking"][0]["intent"] == "information_query"
        assert stats["ranking"][0]["uses"] == 2


class TestAnalyzeHours:
    def test_groups_by_bucket(self):
        records = [
            _task_record("a", tools=["x"], hour=14),
            _task_record("b", tools=["x"], hour=15),
            _task_record("c", tools=["x"], hour=3),
        ]
        stats = _analyze_hours(records)
        assert stats["ranking"][0]["bucket"] == "afternoon"


class TestInferPreferences:
    def _setup(self, monkeypatch, *, min_samples=2, min_conf=0.3):
        from config.settings import settings as _settings
        monkeypatch.setattr(_settings, "PREFERENCE_LEARNING_MIN_SAMPLES", min_samples)
        monkeypatch.setattr(_settings, "PREFERENCE_LEARNING_MIN_CONFIDENCE", min_conf)
        monkeypatch.setattr(_settings, "PREFERENCE_LEARNING_WINDOW_DAYS", 7)

    def test_empty_below_min_samples(self, monkeypatch):
        self._setup(monkeypatch, min_samples=5)
        store = _FakeStore([_task_record("a", tools=["web_worker"])])
        assert infer_preferences(store, now=_NOW) == []

    def test_detects_preferred_tool(self, monkeypatch):
        self._setup(monkeypatch, min_samples=2, min_conf=0.3)
        records = [
            _task_record("a", tools=["web_worker"], success=True),
            _task_record("b", tools=["web_worker"], success=True),
            _task_record("c", tools=["web_worker"], success=True),
        ]
        store = _FakeStore(records)
        candidates = infer_preferences(store, now=_NOW)
        keys = {c.key for c in candidates}
        assert "preferred_tool" in keys
        tool_cand = next(c for c in candidates if c.key == "preferred_tool")
        assert tool_cand.value == "web_worker"
        assert tool_cand.confidence >= 0.99

    def test_filters_preferred_tool_below_confidence(self, monkeypatch):
        self._setup(monkeypatch, min_samples=2, min_conf=0.9)
        # 2 records each with a different tool → share=0.5 each, below 0.9
        records = [
            _task_record("a", tools=["web_worker"]),
            _task_record("b", tools=["file_worker"]),
        ]
        store = _FakeStore(records)
        candidates = infer_preferences(store, now=_NOW)
        assert not any(c.key == "preferred_tool" for c in candidates)

    def test_skips_non_task_records(self, monkeypatch):
        self._setup(monkeypatch, min_samples=2, min_conf=0.3)
        records = [
            _task_record("a", tools=["web_worker"]),
            _task_record("b", tools=["web_worker"]),
            {
                "id": "pref",
                "content": "x",
                "metadata": {"type": "preference", "created_at": _iso(1)},
            },
        ]
        store = _FakeStore(records)
        candidates = infer_preferences(store, now=_NOW)
        # only task_result records counted → still infers preferred_tool
        assert any(c.key == "preferred_tool" for c in candidates)


class TestShouldRunNow:
    def test_disabled_returns_false(self, monkeypatch, tmp_path):
        from config.settings import settings as _settings
        monkeypatch.setattr(_settings, "PREFERENCE_LEARNING_ENABLED", False)
        assert should_run_now(state_path=tmp_path / "state.json") is False

    def test_no_prior_run_returns_true(self, monkeypatch, tmp_path):
        from config.settings import settings as _settings
        monkeypatch.setattr(_settings, "PREFERENCE_LEARNING_ENABLED", True)
        assert should_run_now(state_path=tmp_path / "state.json", now=_NOW) is True

    def test_within_interval_returns_false(self, monkeypatch, tmp_path):
        from config.settings import settings as _settings
        monkeypatch.setattr(_settings, "PREFERENCE_LEARNING_ENABLED", True)
        state = tmp_path / "state.json"
        # Write a "last run = 1 hour ago" marker
        state.write_text(
            f'{{"last_run_at": "{(_NOW - timedelta(hours=1)).isoformat()}"}}',
            encoding="utf-8",
        )
        assert should_run_now(min_interval_hours=24, now=_NOW, state_path=state) is False

    def test_past_interval_returns_true(self, monkeypatch, tmp_path):
        from config.settings import settings as _settings
        monkeypatch.setattr(_settings, "PREFERENCE_LEARNING_ENABLED", True)
        state = tmp_path / "state.json"
        state.write_text(
            f'{{"last_run_at": "{(_NOW - timedelta(hours=48)).isoformat()}"}}',
            encoding="utf-8",
        )
        assert should_run_now(min_interval_hours=24, now=_NOW, state_path=state) is True


class TestMaybeRunLearner:
    def test_skips_when_gate_closed(self, monkeypatch, tmp_path):
        from config.settings import settings as _settings
        monkeypatch.setattr(_settings, "PREFERENCE_LEARNING_ENABLED", False)

        class _Mgr:
            chroma_memory = object()

            def persist_inferred_preferences(self, cands):
                raise AssertionError("should not run")

        assert maybe_run_learner(_Mgr(), state_path=tmp_path / "state.json") == 0

    def test_runs_and_persists(self, monkeypatch, tmp_path):
        from config.settings import settings as _settings
        monkeypatch.setattr(_settings, "PREFERENCE_LEARNING_ENABLED", True)
        monkeypatch.setattr(_settings, "PREFERENCE_LEARNING_MIN_SAMPLES", 2)
        monkeypatch.setattr(_settings, "PREFERENCE_LEARNING_MIN_CONFIDENCE", 0.3)
        monkeypatch.setattr(_settings, "PREFERENCE_LEARNING_WINDOW_DAYS", 7)
        monkeypatch.setattr(_settings, "PREFERENCE_LEARNING_MIN_INTERVAL_HOURS", 24)

        records = [
            _task_record("a", tools=["web_worker"], success=True),
            _task_record("b", tools=["web_worker"], success=True),
            _task_record("c", tools=["web_worker"], success=True),
        ]

        class _Mgr:
            chroma_memory = _FakeStore(records)
            persisted = []

            def persist_inferred_preferences(self, cands, **kw):
                self.persisted = list(cands)
                return [f"mid_{i}" for i in range(len(cands))]

        mgr = _Mgr()
        state = tmp_path / "state.json"
        written = maybe_run_learner(mgr, now=_NOW, state_path=state)
        assert written >= 1
        assert any(c.key == "preferred_tool" for c in mgr.persisted)
        # State file should now exist (gate written)
        assert state.exists()

    def test_gate_recorded_even_when_no_candidates(self, monkeypatch, tmp_path):
        from config.settings import settings as _settings
        monkeypatch.setattr(_settings, "PREFERENCE_LEARNING_ENABLED", True)
        monkeypatch.setattr(_settings, "PREFERENCE_LEARNING_MIN_SAMPLES", 100)

        class _Mgr:
            chroma_memory = _FakeStore([_task_record("a", tools=["x"])])

            def persist_inferred_preferences(self, cands, **kw):
                return []

        state = tmp_path / "state.json"
        assert maybe_run_learner(_Mgr(), now=_NOW, state_path=state) == 0
        # Second call within window is a no-op
        assert should_run_now(min_interval_hours=24, now=_NOW, state_path=state) is False


class TestLLMDistill:
    """A5 final: LLM layer adds non-obvious preferences on top of the rule layer."""

    def _setup(self, monkeypatch, *, min_conf=0.3):
        from config.settings import settings as _settings
        monkeypatch.setattr(_settings, "PREFERENCE_LEARNING_MIN_SAMPLES", 2)
        monkeypatch.setattr(_settings, "PREFERENCE_LEARNING_MIN_CONFIDENCE", min_conf)
        monkeypatch.setattr(_settings, "PREFERENCE_LEARNING_WINDOW_DAYS", 7)
        monkeypatch.setattr(_settings, "PREFERENCE_LEARNING_MODEL", "fake-cheap-model")

    def test_llm_layer_adds_extras(self, monkeypatch):
        self._setup(monkeypatch)
        records = [
            _task_record("a", tools=["web_worker"]),
            _task_record("b", tools=["web_worker"]),
            _task_record("c", tools=["web_worker"]),
        ]
        store = _FakeStore(records)

        captured = {}

        class _FakeResponse:
            content = (
                '{"preferences": [{"key": "output_format", "value": "markdown", '
                '"confidence": 0.75, "notes": "4/5 outputs md"}]}'
            )

        class _FakeLLM:
            def __init__(self, model=None):
                captured["model"] = model

            def chat_with_system(self, **kw):
                captured["kw"] = kw
                return _FakeResponse()

        monkeypatch.setattr("core.llm.LLMClient", _FakeLLM)

        candidates = infer_preferences(store, now=_NOW)
        keys = {c.key for c in candidates}
        assert "preferred_tool" in keys  # rule layer still fires
        assert "output_format" in keys  # LLM layer adds new key
        # LLM candidate is tagged as llm_inferred
        llm_cand = next(c for c in candidates if c.key == "output_format")
        assert llm_cand.source == "llm_inferred"
        assert llm_cand.value == "markdown"
        assert captured["model"] == "fake-cheap-model"

    def test_llm_layer_dedups_existing_keys(self, monkeypatch):
        self._setup(monkeypatch)
        records = [
            _task_record("a", tools=["web_worker"]),
            _task_record("b", tools=["web_worker"]),
            _task_record("c", tools=["web_worker"]),
        ]
        store = _FakeStore(records)

        class _FakeResponse:
            # Returns same key as rule layer → should be ignored
            content = '{"preferences": [{"key": "preferred_tool", "value": "X", "confidence": 0.9}]}'

        class _FakeLLM:
            def __init__(self, model=None):
                pass

            def chat_with_system(self, **kw):
                return _FakeResponse()

        monkeypatch.setattr("core.llm.LLMClient", _FakeLLM)
        candidates = infer_preferences(store, now=_NOW)
        tool_cands = [c for c in candidates if c.key == "preferred_tool"]
        # Only the rule-layer candidate remains (no duplicate from LLM)
        assert len(tool_cands) == 1
        assert tool_cands[0].source == "inferred"

    def test_llm_layer_disabled_when_no_model(self, monkeypatch):
        from config.settings import settings as _settings
        monkeypatch.setattr(_settings, "PREFERENCE_LEARNING_MIN_SAMPLES", 2)
        monkeypatch.setattr(_settings, "PREFERENCE_LEARNING_MIN_CONFIDENCE", 0.3)
        monkeypatch.setattr(_settings, "PREFERENCE_LEARNING_WINDOW_DAYS", 7)
        monkeypatch.setattr(_settings, "PREFERENCE_LEARNING_MODEL", "")  # empty

        called = {"n": 0}

        class _FakeLLM:
            def __init__(self, model=None):
                called["n"] += 1

            def chat_with_system(self, **kw):
                return None

        monkeypatch.setattr("core.llm.LLMClient", _FakeLLM)

        records = [
            _task_record("a", tools=["web_worker"]),
            _task_record("b", tools=["web_worker"]),
        ]
        store = _FakeStore(records)
        infer_preferences(store, now=_NOW)
        assert called["n"] == 0  # LLM not invoked when model is empty

    def test_llm_layer_swallows_errors(self, monkeypatch):
        self._setup(monkeypatch)

        class _FakeLLM:
            def __init__(self, model=None):
                pass

            def chat_with_system(self, **kw):
                raise RuntimeError("llm down")

        monkeypatch.setattr("core.llm.LLMClient", _FakeLLM)

        records = [
            _task_record("a", tools=["web_worker"]),
            _task_record("b", tools=["web_worker"]),
        ]
        store = _FakeStore(records)
        # Should not raise; rule-layer candidates still returned
        candidates = infer_preferences(store, now=_NOW)
        assert any(c.key == "preferred_tool" for c in candidates)


class TestPersistInferredPreferences:
    def test_persists_only_above_threshold(self, monkeypatch):
        from memory.manager import MemoryManager
        from config.settings import settings as _settings
        monkeypatch.setattr(_settings, "PREFERENCE_LEARNING_MIN_CONFIDENCE", 0.5)
        # Disable tiered routing so writes go to the stub chroma_memory
        # rather than through a freshly-instantiated TieredMemoryStore that
        # would talk to real ChromaDB.
        monkeypatch.setattr(_settings, "MEMORY_TIERED_ENABLED", False)

        class _Stub:
            def __init__(self):
                self.saved: List[Dict[str, Any]] = []

            def save_user_preference(self, key, value, *, scope=None, metadata=None):
                self.saved.append(
                    {"key": key, "value": value, "scope": scope, "metadata": metadata}
                )
                return f"mid_{len(self.saved)}"

        stub = _Stub()
        mgr = MemoryManager(chroma_memory=stub)
        candidates = [
            PreferenceCandidate(key="preferred_tool", value="web_worker", confidence=0.8, evidence_ids=["m1"]),
            PreferenceCandidate(key="common_intent", value="x", confidence=0.2),
        ]
        written = mgr.persist_inferred_preferences(candidates, session_id="s1")
        assert len(written) == 1
        assert stub.saved[0]["key"] == "preferred_tool"
        assert stub.saved[0]["metadata"]["source"] == "inferred"
        assert stub.saved[0]["metadata"]["confidence"] == 0.8
