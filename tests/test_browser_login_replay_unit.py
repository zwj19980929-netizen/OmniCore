"""Unit tests for ``agents.browser_login_replay.try_replay_login`` (B1)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import patch

from agents.browser_login_replay import (
    _substitute_placeholders,
    try_replay_login,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeStore:
    def __init__(self, record=None, raise_on_get: bool = False):
        self._record = record
        self._raise = raise_on_get
        self.success_calls: List[Dict[str, Any]] = []
        self.failure_calls: List[Dict[str, Any]] = []

    def get_login_flow(self, domain: str):
        if self._raise:
            raise RuntimeError("store boom")
        return self._record

    def record_login_flow(self, domain: str, *, flow, success: bool):
        entry = {"domain": domain, "flow": flow, "success": success}
        if success:
            self.success_calls.append(entry)
        else:
            self.failure_calls.append(entry)
        return True


class _FakePage:
    """Minimal page stub — checkpoint module reads attributes opportunistically."""

    def __init__(self, url: str = "https://acme.com/home", body_text: str = ""):
        self.url = url
        self._body = body_text

    async def query_selector(self, selector: str):
        return None

    async def text_content(self, selector: str):
        return self._body


class _FakeAgent:
    name = "FakeBrowser"

    def __init__(self, *, execute_results: List[bool], page: _FakePage | None = None):
        self._results = list(execute_results)
        self.executed: List[Any] = []
        self.toolkit = SimpleNamespace(_page=page)

    async def _execute_action(self, action):
        self.executed.append(action)
        if not self._results:
            return True
        return self._results.pop(0)


def _enable(monkeypatch, *, memory=True, replay=True):
    from config.settings import settings as _settings

    monkeypatch.setattr(_settings, "BROWSER_PLAN_MEMORY_ENABLED", memory)
    monkeypatch.setattr(_settings, "BROWSER_LOGIN_REPLAY_ENABLED", replay)


class TestSkips:
    def test_skipped_when_memory_disabled(self, monkeypatch):
        _enable(monkeypatch, memory=False)
        agent = _FakeAgent(execute_results=[])
        result = _run(try_replay_login(agent, "acme.com"))
        assert result.skipped is True
        assert agent.executed == []

    def test_skipped_when_replay_disabled(self, monkeypatch):
        _enable(monkeypatch, replay=False)
        agent = _FakeAgent(execute_results=[])
        result = _run(try_replay_login(agent, "acme.com"))
        assert result.skipped is True

    def test_skipped_when_store_none(self, monkeypatch):
        _enable(monkeypatch)
        with patch("utils.site_knowledge_store.get_site_knowledge_store", return_value=None):
            result = _run(try_replay_login(_FakeAgent(execute_results=[]), "acme.com"))
        assert result.skipped is True
        assert "disabled" in result.reason

    def test_skipped_when_no_flow(self, monkeypatch):
        _enable(monkeypatch)
        store = _FakeStore(record=None)
        with patch("utils.site_knowledge_store.get_site_knowledge_store", return_value=store):
            result = _run(try_replay_login(_FakeAgent(execute_results=[]), "acme.com"))
        assert result.skipped is True
        assert "no stored flow" in result.reason

    def test_skipped_when_fail_count_too_high(self, monkeypatch):
        _enable(monkeypatch)
        store = _FakeStore(
            record={
                "flow": [{"action_type": "click", "selector": "#login"}],
                "fail_count": 5,
            }
        )
        with patch("utils.site_knowledge_store.get_site_knowledge_store", return_value=store):
            result = _run(try_replay_login(
                _FakeAgent(execute_results=[]), "acme.com", max_fail_count=3
            ))
        assert result.skipped is True
        assert "unstable" in result.reason

    def test_skipped_when_flow_not_list(self, monkeypatch):
        _enable(monkeypatch)
        store = _FakeStore(record={"flow": "nonsense"})
        with patch("utils.site_knowledge_store.get_site_knowledge_store", return_value=store):
            result = _run(try_replay_login(_FakeAgent(execute_results=[]), "acme.com"))
        assert result.skipped is True

    def test_skipped_when_get_flow_raises(self, monkeypatch):
        _enable(monkeypatch)
        store = _FakeStore(raise_on_get=True)
        with patch("utils.site_knowledge_store.get_site_knowledge_store", return_value=store):
            result = _run(try_replay_login(_FakeAgent(execute_results=[]), "acme.com"))
        assert result.skipped is True

    def test_empty_domain_skipped(self, monkeypatch):
        _enable(monkeypatch)
        result = _run(try_replay_login(_FakeAgent(execute_results=[]), ""))
        assert result.skipped is True
        assert "empty domain" in result.reason


class TestExecution:
    def test_success_path_calls_record_success(self, monkeypatch):
        _enable(monkeypatch)
        flow = [
            {"action_type": "input", "selector": "#user", "value": "{{username}}"},
            {"action_type": "input", "selector": "#pass", "value": "{{password}}"},
            {"action_type": "click", "selector": "button#submit"},
        ]
        store = _FakeStore(record={"flow": flow, "fail_count": 0})
        agent = _FakeAgent(execute_results=[True, True, True], page=_FakePage())
        with patch("utils.site_knowledge_store.get_site_knowledge_store", return_value=store):
            result = _run(try_replay_login(
                agent, "acme.com",
                credentials={"username": "alice", "password": "s3cret"},
            ))
        assert result.success is True
        assert len(result.executed_steps) == 3
        assert agent.executed[0].value == "alice"
        assert agent.executed[1].value == "s3cret"
        assert len(store.success_calls) == 1
        assert store.failure_calls == []

    def test_failure_when_execute_returns_false(self, monkeypatch):
        _enable(monkeypatch)
        flow = [
            {"action_type": "click", "selector": "#a"},
            {"action_type": "click", "selector": "#b"},
        ]
        store = _FakeStore(record={"flow": flow})
        agent = _FakeAgent(execute_results=[True, False], page=_FakePage())
        with patch("utils.site_knowledge_store.get_site_knowledge_store", return_value=store):
            result = _run(try_replay_login(agent, "acme.com"))
        assert result.success is False
        assert len(result.executed_steps) == 2
        assert result.executed_steps[-1]["success"] is False
        assert len(store.failure_calls) == 1
        assert store.success_calls == []

    def test_failure_when_action_type_unknown(self, monkeypatch):
        _enable(monkeypatch)
        flow = [{"action_type": "teleport", "selector": "#x"}]
        store = _FakeStore(record={"flow": flow})
        agent = _FakeAgent(execute_results=[True], page=_FakePage())
        with patch("utils.site_knowledge_store.get_site_knowledge_store", return_value=store):
            result = _run(try_replay_login(agent, "acme.com"))
        assert result.success is False
        assert "unknown action_type" in result.reason
        assert len(store.failure_calls) == 1


class TestCheckpoint:
    def test_checkpoint_failure_aborts_replay(self, monkeypatch):
        _enable(monkeypatch)
        flow = [
            {
                "action_type": "click",
                "selector": "#login",
                "expected_checkpoint": {
                    "check_type": "url_change",
                    "expected_value": "home",
                },
            },
        ]
        store = _FakeStore(record={"flow": flow})
        page = _FakePage(url="https://acme.com/login")
        agent = _FakeAgent(execute_results=[True], page=page)
        with patch("utils.site_knowledge_store.get_site_knowledge_store", return_value=store):
            result = _run(try_replay_login(agent, "acme.com"))
        assert result.success is False
        assert "checkpoint" in result.reason
        assert len(store.failure_calls) == 1
        step = result.executed_steps[0]
        assert step["checkpoint_passed"] is False

    def test_checkpoint_success_is_recorded(self, monkeypatch):
        _enable(monkeypatch)
        flow = [
            {
                "action_type": "navigate",
                "value": "https://acme.com/home",
                "expected_checkpoint": {
                    "check_type": "url_change",
                    "expected_value": "home",
                },
            },
        ]
        store = _FakeStore(record={"flow": flow})
        agent = _FakeAgent(
            execute_results=[True],
            page=_FakePage(url="https://acme.com/home"),
        )
        with patch("utils.site_knowledge_store.get_site_knowledge_store", return_value=store):
            result = _run(try_replay_login(agent, "acme.com"))
        assert result.success is True
        step = result.executed_steps[0]
        assert step["checkpoint_passed"] is True


class TestSubstitute:
    def test_no_creds_is_noop(self):
        step = {"value": "{{username}}"}
        assert _substitute_placeholders(step, {}) is step

    def test_replaces(self):
        step = {"value": "hi {{username}}"}
        out = _substitute_placeholders(step, {"username": "bob"})
        assert out["value"] == "hi bob"
        assert step["value"] == "hi {{username}}"

    def test_without_marker_is_noop(self):
        step = {"value": "plain"}
        out = _substitute_placeholders(step, {"username": "bob"})
        assert out is step
