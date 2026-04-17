"""
Unit tests for Skill Store ``match_top_k`` + Router skill-hint injection (A3).
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from memory.skill_store import SkillDefinition, SkillMatch, SkillStore


def _fake_search_results(skill_specs):
    """Build fake ``search_memory`` results from concise specs."""
    out = []
    for spec in skill_specs:
        meta = {
            "skill_id": spec["id"],
            "name": spec["name"],
            "version": 1,
            "source_job_id": "",
            "source_intent": spec.get("intent", ""),
            "success_count": str(spec.get("success", 5)),
            "failure_count": str(spec.get("failure", 0)),
            "last_used_at": "",
            "deprecated": "true" if spec.get("deprecated") else "false",
            "tags": "",
            "task_template_json": json.dumps(
                [{"tool_name": t, "description_template": t} for t in spec.get("tools", [])]
            ),
            "parameters_json": "{}",
        }
        out.append(
            {
                "id": spec["id"],
                "content": spec.get("description", spec["name"]),
                "metadata": meta,
                "distance": spec.get("distance", 0.3),
            }
        )
    return out


def _patch_chroma(store: SkillStore, results):
    class _StubChroma:
        def search_memory(self, *a, **kw):
            return results

    store._chroma = _StubChroma()


class TestMatchTopK:
    def test_returns_empty_when_disabled(self, monkeypatch):
        from config.settings import settings as _settings
        monkeypatch.setattr(_settings, "SKILL_LIBRARY_ENABLED", False)
        store = SkillStore()
        assert store.match_top_k("hello") == []

    def test_returns_ordered_matches_with_scores(self, monkeypatch):
        from config.settings import settings as _settings
        monkeypatch.setattr(_settings, "SKILL_LIBRARY_ENABLED", True)
        store = SkillStore()
        _patch_chroma(
            store,
            _fake_search_results(
                [
                    {"id": "sk1", "name": "Close", "distance": 0.2, "tools": ["web_worker"]},
                    {"id": "sk2", "name": "Far", "distance": 0.7, "tools": ["file_worker"]},
                ]
            ),
        )
        matches = store.match_top_k("task", k=3)
        assert len(matches) == 2
        assert isinstance(matches[0], SkillMatch)
        assert matches[0].skill_id == "sk1"
        assert matches[0].score > matches[1].score
        assert matches[0].tool_sequence == ["web_worker"]

    def test_min_score_filter(self, monkeypatch):
        from config.settings import settings as _settings
        monkeypatch.setattr(_settings, "SKILL_LIBRARY_ENABLED", True)
        store = SkillStore()
        _patch_chroma(
            store,
            _fake_search_results(
                [
                    {"id": "sk1", "name": "Close", "distance": 0.2},
                    {"id": "sk2", "name": "Far", "distance": 0.9},  # score 0.1
                ]
            ),
        )
        matches = store.match_top_k("task", k=5, min_score=0.5)
        assert [m.skill_id for m in matches] == ["sk1"]

    def test_skips_deprecated_and_failure_heavy(self, monkeypatch):
        from config.settings import settings as _settings
        monkeypatch.setattr(_settings, "SKILL_LIBRARY_ENABLED", True)
        store = SkillStore()
        _patch_chroma(
            store,
            _fake_search_results(
                [
                    {"id": "sk_dep", "name": "Dep", "distance": 0.2, "deprecated": True},
                    {"id": "sk_fail", "name": "Fail", "distance": 0.2, "success": 1, "failure": 9},
                    {"id": "sk_ok", "name": "OK", "distance": 0.2, "success": 5, "failure": 0},
                ]
            ),
        )
        matches = store.match_top_k("task")
        assert [m.skill_id for m in matches] == ["sk_ok"]


class TestRouterSkillHintBlock:
    def _patch_settings(self, monkeypatch, *, enabled=True, library=True, top_k=3, min_score=0.0):
        from config.settings import settings as _settings
        monkeypatch.setattr(_settings, "SKILL_HINT_ENABLED", enabled)
        monkeypatch.setattr(_settings, "SKILL_LIBRARY_ENABLED", library)
        monkeypatch.setattr(_settings, "SKILL_HINT_TOP_K", top_k)
        monkeypatch.setattr(_settings, "SKILL_HINT_MIN_SCORE", min_score)

    def test_block_empty_when_disabled(self, monkeypatch):
        from core.router import RouterAgent
        self._patch_settings(monkeypatch, enabled=False)
        assert RouterAgent._build_skill_hint_block("do something") == ""

    def test_block_empty_when_library_disabled(self, monkeypatch):
        from core.router import RouterAgent
        self._patch_settings(monkeypatch, library=False)
        assert RouterAgent._build_skill_hint_block("do something") == ""

    def test_block_empty_when_no_matches(self, monkeypatch):
        from core.router import RouterAgent
        self._patch_settings(monkeypatch)

        class _StubStore:
            def match_top_k(self, *a, **kw):
                return []

        with patch("memory.skill_store.SkillStore", lambda: _StubStore()):
            assert RouterAgent._build_skill_hint_block("do something") == ""

    def test_block_renders_hints(self, monkeypatch):
        from core.router import RouterAgent
        self._patch_settings(monkeypatch)

        class _StubStore:
            def match_top_k(self, *a, **kw):
                return [
                    SkillMatch(
                        skill_id="sk1",
                        name="GitHub PR Review",
                        description="Open repo, read diffs, comment",
                        score=0.78,
                        success_rate=0.9,
                        total_uses=10,
                        tool_sequence=["browser_agent", "file_worker"],
                        source_intent="multi_step_task",
                    )
                ]

        with patch("memory.skill_store.SkillStore", lambda: _StubStore()):
            block = RouterAgent._build_skill_hint_block("review my PR")

        assert "GitHub PR Review" in block
        assert "score=0.78" in block
        assert "browser_agent → file_worker" in block
        assert "reference only" in block

    def test_block_swallows_errors(self, monkeypatch):
        from core.router import RouterAgent
        self._patch_settings(monkeypatch)

        class _Boom:
            def match_top_k(self, *a, **kw):
                raise RuntimeError("chroma down")

        with patch("memory.skill_store.SkillStore", lambda: _Boom()):
            assert RouterAgent._build_skill_hint_block("anything") == ""
