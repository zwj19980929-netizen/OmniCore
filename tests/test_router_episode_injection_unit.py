"""
Verify the router's _build_episode_brief_block hook (C1).

We call the staticmethod directly — no LLM involved. The store is real but
backed by an isolated tmp SQLite file, so this also covers the
record → search → format roundtrip end-to-end at the prompt-injection seam.
"""
from __future__ import annotations

import pytest

from core.router import RouterAgent
from memory import episode_store as es


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    from config.settings import settings as _s
    db = tmp_path / "episodes.db"
    monkeypatch.setattr(_s, "EPISODE_REPLAY_ENABLED", True)
    monkeypatch.setattr(_s, "EPISODE_REPLAY_DB", str(db))
    monkeypatch.setattr(_s, "EPISODE_REPLAY_MAX_AGE_DAYS", 60)
    monkeypatch.setattr(_s, "EPISODE_REPLAY_MIN_SIMILARITY", 0.45)
    monkeypatch.setattr(_s, "EPISODE_REPLAY_MAX_DAG_STEPS", 8)
    es.reset_episode_store_singleton_for_tests()
    yield
    es.reset_episode_store_singleton_for_tests()


def _record(user_input: str, *, success: bool):
    state = {
        "user_input": user_input,
        "current_intent": "web_scraping",
        "execution_status": "completed" if success else "completed_with_issues",
        "critic_approved": success,
        "task_queue": [
            {
                "task_id": "t1",
                "task_type": "web_worker",
                "tool_name": "web_worker",
                "description": "search arxiv",
                "params": {"query": user_input},
                "status": "completed" if success else "failed",
                "result": {"summary": "ok" if success else "blocked by captcha"},
            }
        ],
        "total_cost_usd": 0.01,
        "elapsed_ms": 3500,
        "llm_call_count": 4,
    }
    es.get_episode_store().record_episode(state)


def test_brief_block_empty_when_disabled(monkeypatch):
    from config.settings import settings as _s
    monkeypatch.setattr(_s, "EPISODE_REPLAY_ENABLED", False)
    out = RouterAgent._build_episode_brief_block("download arxiv paper foo bar")
    assert out == ""


def test_brief_block_empty_when_no_episodes():
    out = RouterAgent._build_episode_brief_block("anything goes here token")
    assert out == ""


def test_brief_block_includes_success_and_failure_examples():
    _record("download arxiv paper transformer summary", success=True)
    _record("download arxiv paper transformer summary alt", success=False)
    out = RouterAgent._build_episode_brief_block("download arxiv paper transformer")
    assert "Past similar task traces" in out
    assert "[success]" in out
    assert "[failure]" in out
    assert out.endswith("---\n")


def test_brief_block_swallows_store_errors(monkeypatch):
    """Any unexpected store error must NOT propagate into the router."""
    def boom(*_a, **_kw):
        raise RuntimeError("simulated store crash")

    monkeypatch.setattr(es.EpisodeStore, "fetch_brief_pair", boom)
    out = RouterAgent._build_episode_brief_block("anything")
    assert out == ""


def test_brief_block_skipped_when_similarity_below_threshold(monkeypatch):
    from config.settings import settings as _s
    _record("totally unrelated alpha beta gamma", success=True)
    monkeypatch.setattr(_s, "EPISODE_REPLAY_MIN_SIMILARITY", 0.99)
    out = RouterAgent._build_episode_brief_block("download arxiv paper xyz")
    assert out == ""
