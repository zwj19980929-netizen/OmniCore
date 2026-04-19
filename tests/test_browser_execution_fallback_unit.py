"""
Unit tests for B1 (site_hint prepend) + B5 (strategy_stats reorder/record)
integration inside ``BrowserExecutionLayer.try_click_with_fallbacks`` /
``try_input_with_fallbacks``.

The real BrowserToolkit is far too heavy for a unit test; we stub the
minimum surface the fallback chain calls and assert side effects on the
two stores.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import pytest

from utils.browser_toolkit import ToolkitResult


def _run(coro):
    """Run a coroutine in an isolated event loop without leaking loop state
    to subsequent tests (mirrors ``asyncio.run`` semantics but works across
    Python 3.10+ where ``get_event_loop`` behavior changed)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class FakeToolkit:
    """Minimal async-surface stand-in for BrowserToolkit.

    - ``script`` is a dict ``{method_name: [ToolkitResult, ...]}`` that pops
      the next result on each call (exhausted list → default fail).
    - Records all calls so tests can assert on the order fallback strategies
      were tried.
    """

    def __init__(self, *, current_url: str = "https://acme.com/", script: Optional[Dict[str, List[Any]]] = None):
        self._current_url = current_url
        self.script = script or {}
        self.calls: List[tuple] = []

    def _next(self, method: str) -> ToolkitResult:
        q = self.script.get(method)
        if q:
            r = q.pop(0)
            if isinstance(r, ToolkitResult):
                return r
            return ToolkitResult(success=bool(r))
        return ToolkitResult(success=False, error="unscripted")

    async def get_current_url(self) -> ToolkitResult:
        return ToolkitResult(success=True, data=self._current_url)

    async def click(self, selector, timeout=None):
        self.calls.append(("click", selector))
        return self._next("click")

    async def click_ref(self, ref, timeout=None):
        self.calls.append(("click_ref", ref))
        return self._next("click_ref")

    async def click_by_role(self, role, name, timeout=None):
        self.calls.append(("click_by_role", role, name))
        return self._next("click_by_role")

    async def click_by_label(self, label, timeout=None):
        self.calls.append(("click_by_label", label))
        return self._next("click_by_label")

    async def locator_click(self, selector, timeout=None):
        self.calls.append(("locator_click", selector))
        return self._next("locator_click")

    async def force_click(self, selector, timeout=None):
        self.calls.append(("force_click", selector))
        return self._next("force_click")

    async def press_key(self, key):
        self.calls.append(("press_key", key))
        return self._next("press_key")

    async def input_text(self, selector, text, timeout=None):
        self.calls.append(("input_text", selector, text))
        return self._next("input_text")

    async def input_ref(self, ref, text, timeout=None):
        self.calls.append(("input_ref", ref, text))
        return self._next("input_ref")

    async def fill_by_placeholder(self, ph, text):
        self.calls.append(("fill_by_placeholder", ph, text))
        return self._next("fill_by_placeholder")

    async def fill_by_label(self, label, text):
        self.calls.append(("fill_by_label", label, text))
        return self._next("fill_by_label")

    async def clear_input(self, selector):
        self.calls.append(("clear_input", selector))
        return self._next("clear_input")

    async def type_text(self, selector, text, delay=0):
        self.calls.append(("type_text", selector, text))
        return self._next("type_text")

    async def evaluate_js(self, script, *args):
        self.calls.append(("evaluate_js",))
        return self._next("evaluate_js")


@pytest.fixture
def enable_all(monkeypatch, tmp_path):
    from config.settings import settings as _settings
    monkeypatch.setattr(_settings, "BROWSER_PLAN_MEMORY_ENABLED", True)
    monkeypatch.setattr(_settings, "BROWSER_SITE_HINTS_EXEC_INJECT", True)
    monkeypatch.setattr(_settings, "BROWSER_SELECTOR_HINT_TOP_K", 5)
    monkeypatch.setattr(_settings, "BROWSER_SELECTOR_MIN_SUCCESS_RATE", 0.5)
    monkeypatch.setattr(_settings, "BROWSER_SELECTOR_DECAY_DAYS", 30)
    monkeypatch.setattr(_settings, "BROWSER_STRATEGY_LEARNING_ENABLED", True)
    monkeypatch.setattr(_settings, "BROWSER_STRATEGY_MIN_SAMPLES", 3)
    monkeypatch.setattr(_settings, "BROWSER_STRATEGY_SKIP_THRESHOLD", 0.2)

    # Point singletons at tmp_path dbs and reset module-level singletons
    import utils.site_knowledge_store as sks
    import utils.strategy_stats as ssm
    monkeypatch.setattr(_settings, "BROWSER_SITE_KNOWLEDGE_DB", str(tmp_path / "sk.db"))
    monkeypatch.setattr(_settings, "BROWSER_STRATEGY_DB", str(tmp_path / "ss.db"))
    monkeypatch.setattr(sks, "_SINGLETON", None)
    monkeypatch.setattr(ssm, "_SINGLETON", None)
    yield
    monkeypatch.setattr(sks, "_SINGLETON", None)
    monkeypatch.setattr(ssm, "_SINGLETON", None)


@pytest.fixture
def exec_layer():
    from agents.browser_execution import BrowserExecutionLayer
    tk = FakeToolkit()
    layer = BrowserExecutionLayer(toolkit=tk, agent_name="test")
    return layer, tk


# ---------- Canonical strategy name ----------


class TestCanonical:
    def test_basic(self):
        from agents.browser_execution import BrowserExecutionLayer as BE
        assert BE._canonical_strategy_name("role:button:Submit") == "role"
        assert BE._canonical_strategy_name("site_hint:#x") == "site_hint"
        assert BE._canonical_strategy_name("direct_click") == "direct_click"
        assert BE._canonical_strategy_name("") == ""


# ---------- Click path ----------


class TestClickFallback:
    def test_feature_off_preserves_legacy_order(self, monkeypatch, exec_layer):
        from config.settings import settings as _settings
        monkeypatch.setattr(_settings, "BROWSER_PLAN_MEMORY_ENABLED", False)
        monkeypatch.setattr(_settings, "BROWSER_STRATEGY_LEARNING_ENABLED", False)
        layer, tk = exec_layer
        tk.script = {"click": [ToolkitResult(success=True)]}
        ok = _run(layer.try_click_with_fallbacks("#submit"))
        assert ok is True
        # Only direct_click should have run (first strategy, success).
        assert tk.calls == [("click", "#submit")]

    def test_site_hint_prepended_and_tried_first(self, enable_all, exec_layer):
        """A stored site_hint selector runs before direct_click."""
        from utils.site_knowledge_store import get_site_knowledge_store
        store = get_site_knowledge_store()
        store.record_selector_success("acme.com", "click", "#hinted")

        layer, tk = exec_layer
        # First click attempt is against the hint selector → success.
        tk.script = {"click": [ToolkitResult(success=True)]}
        ok = _run(layer.try_click_with_fallbacks("#submit"))
        assert ok is True
        # hint ran first, loop terminated before direct_click.
        assert tk.calls == [("click", "#hinted")]

    def test_site_hint_failure_falls_through_and_records_both_stores(
        self, enable_all, exec_layer
    ):
        from utils.site_knowledge_store import get_site_knowledge_store
        from utils.strategy_stats import get_strategy_stats_store
        sk = get_site_knowledge_store()
        ss = get_strategy_stats_store()
        sk.record_selector_success("acme.com", "click", "#hinted")

        layer, tk = exec_layer
        # Hint fails, direct_click succeeds on "#submit"
        tk.script = {
            "click": [ToolkitResult(success=False), ToolkitResult(success=True)],
        }
        ok = _run(layer.try_click_with_fallbacks("#submit"))
        assert ok is True
        assert tk.calls == [("click", "#hinted"), ("click", "#submit")]

        # Stats: site_hint got 1 failure, direct_click got 1 success.
        stats = ss.get_stats("acme.com", "click")
        assert stats["site_hint"]["fail_count"] == 1
        assert stats["site_hint"]["success_count"] == 0
        assert stats["direct_click"]["success_count"] == 1

        # site_knowledge_store: hint selector has 1 success (from setup)
        # + 1 failure (from this run).
        hints = sk.get_selector_hints("acme.com", role="click", limit=10)
        by_sel = {h["selector"]: h for h in hints}
        # success_rate = 1/(1+1) = 0.5, still passes min_success_rate=0.5.
        assert by_sel["#hinted"]["fail_count"] == 1
        assert by_sel["#hinted"]["hit_count"] == 1

    def test_strategy_stats_records_on_success(self, enable_all, exec_layer):
        from utils.strategy_stats import get_strategy_stats_store
        ss = get_strategy_stats_store()
        layer, tk = exec_layer
        tk.script = {"click": [ToolkitResult(success=True)]}
        _run(layer.try_click_with_fallbacks("#submit"))
        stats = ss.get_stats("acme.com", "click")
        assert stats["direct_click"]["success_count"] == 1
        assert stats["direct_click"]["fail_count"] == 0

    def test_skip_strategy_is_filtered(self, enable_all, exec_layer):
        """A strategy whose historic success rate is below skip threshold
        is dropped from the fallback chain entirely."""
        from utils.strategy_stats import get_strategy_stats_store
        ss = get_strategy_stats_store()
        # Mark direct_click as dead on acme.com (0/4)
        for _ in range(4):
            ss.record("acme.com", "click", "direct_click", success=False)
        # Prime locator_click as healthy so it's ranked (3/3)
        for _ in range(3):
            ss.record("acme.com", "click", "locator_click", success=True)

        layer, tk = exec_layer
        # direct_click is skipped; locator_click runs first after ranking.
        tk.script = {"locator_click": [ToolkitResult(success=True)]}
        ok = _run(layer.try_click_with_fallbacks("#submit"))
        assert ok is True
        # direct_click must NOT appear in calls.
        methods_called = [c[0] for c in tk.calls]
        assert "click" not in methods_called
        assert "locator_click" in methods_called

    def test_all_strategies_fail_returns_false(self, enable_all, exec_layer):
        layer, tk = exec_layer
        # everything scripted to fail (or unscripted, default fail)
        ok = _run(layer.try_click_with_fallbacks("#submit"))
        assert ok is False


# ---------- Input path ----------


class TestInputFallback:
    def test_site_hint_input_uses_value(self, enable_all, exec_layer):
        from utils.site_knowledge_store import get_site_knowledge_store
        sk = get_site_knowledge_store()
        sk.record_selector_success("acme.com", "input", "#email")

        layer, tk = exec_layer
        tk.script = {"input_text": [ToolkitResult(success=True)]}
        ok = _run(layer.try_input_with_fallbacks("#user", "hello@x.com"))
        assert ok is True
        # hint uses input_text with the hinted selector + user's value.
        assert tk.calls[0] == ("input_text", "#email", "hello@x.com")

    def test_input_feature_off_behaves_legacy(self, monkeypatch, exec_layer):
        from config.settings import settings as _settings
        monkeypatch.setattr(_settings, "BROWSER_PLAN_MEMORY_ENABLED", False)
        monkeypatch.setattr(_settings, "BROWSER_STRATEGY_LEARNING_ENABLED", False)
        layer, tk = exec_layer
        tk.script = {"input_text": [ToolkitResult(success=True)]}
        ok = _run(layer.try_input_with_fallbacks("#user", "hi"))
        assert ok is True
        assert tk.calls == [("input_text", "#user", "hi")]

    def test_input_records_success_to_strategy_stats(self, enable_all, exec_layer):
        from utils.strategy_stats import get_strategy_stats_store
        ss = get_strategy_stats_store()
        layer, tk = exec_layer
        tk.script = {"input_text": [ToolkitResult(success=True)]}
        _run(layer.try_input_with_fallbacks("#user", "hi"))
        stats = ss.get_stats("acme.com", "input")
        assert stats["direct_fill"]["success_count"] == 1
