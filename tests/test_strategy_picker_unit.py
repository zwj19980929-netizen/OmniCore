"""Unit tests for ``agents.browser_strategies`` (B6)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import patch

from agents.browser_strategies import (
    BatchExecuteStrategy,
    DecisionStrategy,
    LegacyPerStepStrategy,
    LoginReplayStrategy,
    StrategyContext,
    StrategyPicker,
    UnifiedActStrategy,
)
from agents.page_assessment_cache import PageAssessmentCache


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── Test helpers ─────────────────────────────────────────────


class _StubAgent:
    name = "StubAgent"

    def __init__(self, *, per_step=None, batch=None, execute_results=None):
        self._per_step_result = per_step
        self._batch_result = batch
        self._execute_results = list(execute_results or [])
        self.toolkit = SimpleNamespace(_page=None, get_current_url=self._get_url, get_title=self._get_title)
        self.per_step_calls = 0
        self.batch_calls = 0

    async def _run_per_step_loop(self, task, intent, expected_url, steps, max_steps):
        self.per_step_calls += 1
        return self._per_step_result or {
            "success": True, "message": "per-step done", "url": "", "steps": steps, "data": [],
        }

    async def _run_batch_mode(self, task, intent, expected_url, steps):
        self.batch_calls += 1
        return self._batch_result

    async def _execute_action(self, action):
        if not self._execute_results:
            return True
        return self._execute_results.pop(0)

    async def _get_url(self):
        return SimpleNamespace(data="https://acme.com/home", success=True)

    async def _get_title(self):
        return SimpleNamespace(data="Home", success=True)


def _ctx(**kw) -> StrategyContext:
    defaults = dict(
        task="do something",
        task_intent=SimpleNamespace(intent_type="read"),
        expected_url="https://acme.com/",
        steps=[],
        max_steps=8,
    )
    defaults.update(kw)
    return StrategyContext(**defaults)


def _enable(monkeypatch, **flags):
    from config.settings import settings as _settings

    defaults = dict(
        BROWSER_BATCH_EXECUTE_ENABLED=False,
        BROWSER_UNIFIED_ACT_ENABLED=False,
        BROWSER_PLAN_MEMORY_ENABLED=False,
        BROWSER_LOGIN_REPLAY_ENABLED=True,
        BROWSER_STRATEGY_REFACTOR_ENABLED=True,
    )
    defaults.update(flags)
    for name, value in defaults.items():
        monkeypatch.setattr(_settings, name, value)


# ── Base contract ────────────────────────────────────────────


class TestBase:
    def test_context_shares_steps_by_reference(self):
        steps: List[Dict[str, Any]] = []
        ctx = StrategyContext(task="t", task_intent=None, expected_url="", steps=steps)
        ctx.steps.append({"x": 1})
        assert steps == [{"x": 1}]

    def test_decision_strategy_is_abstract(self):
        import pytest

        with pytest.raises(TypeError):
            DecisionStrategy()  # type: ignore[abstract]


# ── Legacy / Unified / Batch wrappers ────────────────────────


class TestLegacyStrategy:
    def test_delegates_to_per_step_loop(self, monkeypatch):
        _enable(monkeypatch)
        agent = _StubAgent(per_step={"success": True, "message": "ok", "steps": [], "data": []})
        ctx = _ctx()
        result = _run(LegacyPerStepStrategy().execute(agent, ctx))
        assert result["message"] == "ok"
        assert agent.per_step_calls == 1
        assert ctx.attempted == ["legacy"]


class TestUnifiedStrategy:
    def test_also_delegates_to_per_step_loop(self, monkeypatch):
        _enable(monkeypatch, BROWSER_UNIFIED_ACT_ENABLED=True)
        agent = _StubAgent(per_step={"success": True, "steps": [], "data": []})
        ctx = _ctx()
        result = _run(UnifiedActStrategy().execute(agent, ctx))
        assert result["success"] is True
        assert agent.per_step_calls == 1
        assert ctx.attempted == ["unified"]


class TestBatchStrategy:
    def test_returns_none_when_batch_returns_none(self, monkeypatch):
        _enable(monkeypatch, BROWSER_BATCH_EXECUTE_ENABLED=True)
        agent = _StubAgent(batch=None)
        ctx = _ctx()
        result = _run(BatchExecuteStrategy().execute(agent, ctx))
        assert result is None
        assert ctx.attempted == ["batch"]

    def test_returns_batch_result(self, monkeypatch):
        _enable(monkeypatch, BROWSER_BATCH_EXECUTE_ENABLED=True)
        agent = _StubAgent(batch={"success": True, "message": "batch done"})
        result = _run(BatchExecuteStrategy().execute(agent, _ctx()))
        assert result["message"] == "batch done"


# ── Login replay strategy ────────────────────────────────────


class TestLoginReplayStrategy:
    def test_returns_none_when_replay_skipped(self, monkeypatch):
        _enable(monkeypatch, BROWSER_PLAN_MEMORY_ENABLED=False)
        strategy = LoginReplayStrategy(domain="acme.com")
        result = _run(strategy.execute(_StubAgent(), _ctx()))
        assert result is None

    def test_returns_terminal_dict_on_success(self, monkeypatch):
        _enable(monkeypatch, BROWSER_PLAN_MEMORY_ENABLED=True)

        async def _fake_replay(agent, domain, *, credentials=None, max_fail_count=3):
            from agents.browser_login_replay import LoginReplayResult

            return LoginReplayResult(
                success=True,
                executed_steps=[
                    {"action_type": "click", "target_selector": "#login", "success": True}
                ],
            )

        strategy = LoginReplayStrategy(domain="acme.com")
        with patch("agents.browser_login_replay.try_replay_login", _fake_replay):
            result = _run(strategy.execute(_StubAgent(), _ctx()))
        assert result is not None
        assert result["success"] is True
        assert result["login_replay"] is True
        assert result["url"] == "https://acme.com/home"
        assert len(result["steps"]) == 1


# ── StrategyPicker ordering ──────────────────────────────────


class TestStrategyPickerChain:
    def test_chain_includes_legacy_always(self, monkeypatch):
        _enable(monkeypatch)
        chain = StrategyPicker().build_chain(_StubAgent(), _ctx())
        assert [s.name for s in chain] == ["legacy"]

    def test_batch_preferred_over_unified(self, monkeypatch):
        _enable(
            monkeypatch,
            BROWSER_BATCH_EXECUTE_ENABLED=True,
            BROWSER_UNIFIED_ACT_ENABLED=True,
        )
        chain = StrategyPicker().build_chain(_StubAgent(), _ctx())
        assert [s.name for s in chain] == ["batch", "legacy"]

    def test_unified_when_batch_off(self, monkeypatch):
        _enable(
            monkeypatch,
            BROWSER_BATCH_EXECUTE_ENABLED=False,
            BROWSER_UNIFIED_ACT_ENABLED=True,
        )
        chain = StrategyPicker().build_chain(_StubAgent(), _ctx())
        assert [s.name for s in chain] == ["unified", "legacy"]

    def test_login_replay_prepended_when_stored_flow_matches(self, monkeypatch):
        _enable(monkeypatch, BROWSER_PLAN_MEMORY_ENABLED=True)
        ctx = _ctx(
            task="please log in to the dashboard",
            task_intent=SimpleNamespace(intent_type="auth"),
            expected_url="https://acme.com/login",
        )
        store = SimpleNamespace(
            get_login_flow=lambda domain: {"flow": [{"action_type": "click"}]}
        )
        with patch("utils.site_knowledge_store.get_site_knowledge_store", return_value=store):
            chain = StrategyPicker().build_chain(_StubAgent(), ctx)
        assert [s.name for s in chain] == ["login_replay", "legacy"]

    def test_login_replay_not_picked_for_non_auth_task(self, monkeypatch):
        _enable(monkeypatch, BROWSER_PLAN_MEMORY_ENABLED=True)
        ctx = _ctx(
            task="read latest news",
            task_intent=SimpleNamespace(intent_type="read"),
            expected_url="https://acme.com/",
        )
        store = SimpleNamespace(
            get_login_flow=lambda domain: {"flow": [{"action_type": "click"}]}
        )
        with patch("utils.site_knowledge_store.get_site_knowledge_store", return_value=store):
            chain = StrategyPicker().build_chain(_StubAgent(), ctx)
        assert [s.name for s in chain] == ["legacy"]

    def test_login_replay_skipped_when_no_stored_flow(self, monkeypatch):
        _enable(monkeypatch, BROWSER_PLAN_MEMORY_ENABLED=True)
        ctx = _ctx(
            task="login to dashboard",
            task_intent=SimpleNamespace(intent_type="auth"),
            expected_url="https://acme.com/login",
        )
        store = SimpleNamespace(get_login_flow=lambda domain: None)
        with patch("utils.site_knowledge_store.get_site_knowledge_store", return_value=store):
            chain = StrategyPicker().build_chain(_StubAgent(), ctx)
        assert [s.name for s in chain] == ["legacy"]

    def test_login_replay_disabled_when_memory_off(self, monkeypatch):
        _enable(monkeypatch, BROWSER_PLAN_MEMORY_ENABLED=False)
        ctx = _ctx(
            task="login to dashboard",
            task_intent=SimpleNamespace(intent_type="auth"),
            expected_url="https://acme.com/login",
        )
        chain = StrategyPicker().build_chain(_StubAgent(), ctx)
        assert all(s.name != "login_replay" for s in chain)

    def test_login_replay_disabled_when_replay_flag_off(self, monkeypatch):
        _enable(monkeypatch, BROWSER_PLAN_MEMORY_ENABLED=True, BROWSER_LOGIN_REPLAY_ENABLED=False)
        ctx = _ctx(
            task="login to dashboard",
            task_intent=SimpleNamespace(intent_type="auth"),
            expected_url="https://acme.com/login",
        )
        chain = StrategyPicker().build_chain(_StubAgent(), ctx)
        assert all(s.name != "login_replay" for s in chain)

    def test_chinese_login_keyword_detected(self, monkeypatch):
        _enable(monkeypatch, BROWSER_PLAN_MEMORY_ENABLED=True)
        ctx = _ctx(
            task="请帮我登录到后台",
            task_intent=SimpleNamespace(intent_type="read"),
            expected_url="https://acme.com/login",
        )
        store = SimpleNamespace(
            get_login_flow=lambda domain: {"flow": [{"action_type": "click"}]}
        )
        with patch("utils.site_knowledge_store.get_site_knowledge_store", return_value=store):
            chain = StrategyPicker().build_chain(_StubAgent(), ctx)
        assert chain[0].name == "login_replay"


# ── PageAssessmentCache ──────────────────────────────────────


class TestPageAssessmentCache:
    def test_put_and_get(self):
        cache = PageAssessmentCache(max_entries=4)
        cache.put("hash-a", {"x": 1})
        assert cache.get("hash-a") == {"x": 1}

    def test_empty_key_is_noop(self):
        cache = PageAssessmentCache()
        cache.put("", {"x": 1})
        assert cache.get("") is None
        assert len(cache) == 0

    def test_lru_eviction(self):
        cache = PageAssessmentCache(max_entries=2)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.put("c", 3)
        assert cache.get("a") is None
        assert cache.get("b") == 2
        assert cache.get("c") == 3

    def test_get_refreshes_recency(self):
        cache = PageAssessmentCache(max_entries=2)
        cache.put("a", 1)
        cache.put("b", 2)
        assert cache.get("a") == 1  # bumps a
        cache.put("c", 3)  # should evict b, not a
        assert cache.get("a") == 1
        assert cache.get("b") is None
        assert cache.get("c") == 3

    def test_get_or_compute_memoizes(self):
        cache = PageAssessmentCache()
        calls = {"n": 0}

        async def _compute():
            calls["n"] += 1
            return {"v": calls["n"]}

        first = _run(cache.get_or_compute("k", _compute))
        second = _run(cache.get_or_compute("k", _compute))
        assert first == {"v": 1}
        assert second == {"v": 1}
        assert calls["n"] == 1

    def test_get_or_compute_without_key_bypasses_cache(self):
        cache = PageAssessmentCache()
        calls = {"n": 0}

        async def _compute():
            calls["n"] += 1
            return calls["n"]

        _run(cache.get_or_compute("", _compute))
        _run(cache.get_or_compute("", _compute))
        assert calls["n"] == 2
        assert len(cache) == 0


# ── End-to-end orchestrator smoke ────────────────────────────


class TestOrchestrator:
    def test_chain_falls_through_when_first_returns_none(self, monkeypatch):
        _enable(monkeypatch, BROWSER_BATCH_EXECUTE_ENABLED=True)
        # batch returns None → legacy picks up
        agent = _StubAgent(batch=None, per_step={"success": True, "steps": [], "data": []})
        chain = StrategyPicker().build_chain(agent, _ctx())
        assert [s.name for s in chain] == ["batch", "legacy"]
        # Simulate orchestrator fall-through
        ctx = _ctx()
        last: Optional[Dict[str, Any]] = None
        for s in chain:
            last = _run(s.execute(agent, ctx))
            if last is not None:
                break
        assert last is not None
        assert last["success"] is True
        assert ctx.attempted == ["batch", "legacy"]
