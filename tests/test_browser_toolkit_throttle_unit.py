"""
Unit tests for B2 BrowserToolkit接入: throttle hint application, UA override,
response listener (429/503 -> record_block), successful goto positive-feedback
(record_request), and MAX_TAB_COUNT enforcement.

Playwright Page/Context are mocked — we only exercise the toolkit glue.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, List, Optional

import pytest

from utils.browser_toolkit import BrowserToolkit, ToolkitResult


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class FakeRequest:
    def __init__(self, resource_type: str = "document"):
        self.resource_type = resource_type


class FakeResponse:
    def __init__(self, url: str, status: int, resource_type: str = "document"):
        self.url = url
        self.status = status
        self.request = FakeRequest(resource_type)


class FakePage:
    """Minimal stand-in for playwright.async_api.Page used by BrowserToolkit."""

    def __init__(self, url: str = "about:blank"):
        self.url = url
        self._closed = False
        self._listeners: Dict[str, List[Callable]] = {}
        self.goto_calls: List[Dict[str, Any]] = []

    def is_closed(self) -> bool:
        return self._closed

    def on(self, event: str, handler: Callable) -> None:
        self._listeners.setdefault(event, []).append(handler)

    async def close(self) -> None:
        self._closed = True

    async def goto(self, url: str, wait_until: str = "domcontentloaded", timeout: int = 30000):
        self.goto_calls.append({"url": url, "wait_until": wait_until, "timeout": timeout})
        self.url = url
        return None

    async def bring_to_front(self) -> None:
        return None

    async def title(self) -> str:
        return "T"

    def fire(self, event: str, *args) -> None:
        for h in self._listeners.get(event, []):
            h(*args)


class FakeContext:
    def __init__(self, pages: Optional[List[FakePage]] = None):
        self.pages: List[FakePage] = pages or []


# --------- helpers ---------


def _make_toolkit(
    *,
    page: Optional[FakePage] = None,
    context: Optional[FakeContext] = None,
) -> BrowserToolkit:
    tk = BrowserToolkit(headless=True)
    tk._page = page
    tk._context = context
    return tk


@pytest.fixture
def enable_antibot(monkeypatch, tmp_path):
    from config.settings import settings as _settings
    monkeypatch.setattr(_settings, "ANTI_BOT_PROFILE_ENABLED", True)
    monkeypatch.setattr(_settings, "ANTI_BOT_PROFILE_DB", str(tmp_path / "ab.db"))
    monkeypatch.setattr(_settings, "ANTI_BOT_INITIAL_DELAY_SEC", 0.5)
    monkeypatch.setattr(_settings, "ANTI_BOT_MAX_DELAY_SEC", 5)
    monkeypatch.setattr(_settings, "ANTI_BOT_BLOCK_DECAY_DAYS", 14)
    monkeypatch.setattr(_settings, "ANTI_BOT_SUCCESS_TO_COOLDOWN", 3)
    # Reset singleton so tmp_path db is picked up.
    import utils.anti_bot_profile as abm
    monkeypatch.setattr(abm, "_ANTIBOT_SINGLETON", None)
    yield
    monkeypatch.setattr(abm, "_ANTIBOT_SINGLETON", None)


# --------- apply_throttle_hint ---------


class TestApplyHint:
    def test_ua_and_delay_copied(self):
        from utils.anti_bot_profile import ThrottleHint
        tk = _make_toolkit()
        tk.apply_throttle_hint(ThrottleHint(delay_sec=2.5, ua="UA/1.0", headed=True))
        assert tk._ua_override == "UA/1.0"
        assert tk._pending_delay_sec == 2.5

    def test_stacked_hints_take_larger_delay(self):
        from utils.anti_bot_profile import ThrottleHint
        tk = _make_toolkit()
        tk.apply_throttle_hint(ThrottleHint(delay_sec=1.0, ua="A"))
        tk.apply_throttle_hint(ThrottleHint(delay_sec=3.0, ua="B"))
        # UA overwrites; delay keeps larger value
        assert tk._ua_override == "B"
        assert tk._pending_delay_sec == 3.0

    def test_none_hint_is_noop(self):
        tk = _make_toolkit()
        tk.apply_throttle_hint(None)
        assert tk._ua_override == ""
        assert tk._pending_delay_sec == 0.0


# --------- goto: delay + positive feedback ---------


class TestGotoIntegration:
    def test_delay_applied_then_cleared(self, enable_antibot, monkeypatch):
        page = FakePage(url="https://example.com")
        tk = _make_toolkit(page=page)
        tk._pending_delay_sec = 1.2

        slept: List[float] = []

        async def fake_sleep(sec):
            slept.append(sec)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        r = _run(tk.goto("https://example.com/path"))
        assert r.success
        assert slept == [1.2]
        # Cleared after application
        assert tk._pending_delay_sec == 0.0

    def test_success_records_positive_request(self, enable_antibot):
        page = FakePage(url="https://foo.com")
        tk = _make_toolkit(page=page)
        r = _run(tk.goto("https://foo.com/a"))
        assert r.success

        from utils.anti_bot_profile import get_anti_bot_profile_store
        store = get_anti_bot_profile_store()
        profile = store.get_profile("https://foo.com")
        assert profile is not None
        assert profile.success_count >= 1
        assert profile.consecutive_success >= 1

    def test_failure_does_not_record_success(self, enable_antibot):
        class FailingPage(FakePage):
            async def goto(self, url, wait_until="domcontentloaded", timeout=30000):
                raise RuntimeError("nav failed")

        tk = _make_toolkit(page=FailingPage(url="https://x.com"))
        r = _run(tk.goto("https://x.com/b"))
        assert not r.success

        from utils.anti_bot_profile import get_anti_bot_profile_store
        store = get_anti_bot_profile_store()
        profile = store.get_profile("https://x.com")
        # Nothing should have been recorded since goto failed before our hook
        assert profile is None

    def test_positive_feedback_noop_when_feature_off(self, monkeypatch):
        from config.settings import settings as _settings
        monkeypatch.setattr(_settings, "ANTI_BOT_PROFILE_ENABLED", False)
        page = FakePage(url="https://q.com")
        tk = _make_toolkit(page=page)
        # no exception and no store touched
        r = _run(tk.goto("https://q.com/"))
        assert r.success


# --------- response listener ---------


class TestResponseListener:
    def test_429_records_rate_limit(self, enable_antibot):
        page = FakePage(url="https://rate.test")
        tk = _make_toolkit(page=page)
        tk._install_response_listener(page)

        page.fire("response", FakeResponse("https://rate.test/api", 429))

        from utils.anti_bot_profile import get_anti_bot_profile_store
        store = get_anti_bot_profile_store()
        profile = store.get_profile("https://rate.test")
        assert profile is not None
        assert profile.block_count == 1
        assert profile.last_block_kind == "rate_limit"

    def test_503_records_service_unavailable(self, enable_antibot):
        page = FakePage(url="https://down.test")
        tk = _make_toolkit(page=page)
        tk._install_response_listener(page)

        page.fire("response", FakeResponse("https://down.test/", 503))

        from utils.anti_bot_profile import get_anti_bot_profile_store
        store = get_anti_bot_profile_store()
        profile = store.get_profile("https://down.test")
        assert profile is not None
        assert profile.last_block_kind == "service_unavailable"

    def test_non_document_response_ignored(self, enable_antibot):
        page = FakePage(url="https://ok.test")
        tk = _make_toolkit(page=page)
        tk._install_response_listener(page)

        # XHR 429 should not count
        page.fire("response", FakeResponse("https://ok.test/xhr", 429, resource_type="xhr"))

        from utils.anti_bot_profile import get_anti_bot_profile_store
        store = get_anti_bot_profile_store()
        assert store.get_profile("https://ok.test") is None

    def test_listener_noop_when_feature_off(self, monkeypatch):
        from config.settings import settings as _settings
        monkeypatch.setattr(_settings, "ANTI_BOT_PROFILE_ENABLED", False)
        page = FakePage(url="https://x.y")
        tk = _make_toolkit(page=page)
        tk._install_response_listener(page)
        # Should not register a listener
        assert "response" not in page._listeners

    def test_200_status_no_block(self, enable_antibot):
        page = FakePage(url="https://ok2.test")
        tk = _make_toolkit(page=page)
        tk._install_response_listener(page)
        page.fire("response", FakeResponse("https://ok2.test/", 200))

        from utils.anti_bot_profile import get_anti_bot_profile_store
        store = get_anti_bot_profile_store()
        assert store.get_profile("https://ok2.test") is None


# --------- MAX_TAB_COUNT enforcement ---------


class TestTabCap:
    def test_closes_oldest_non_active_when_over_cap(self, monkeypatch):
        from config.settings import settings as _settings
        monkeypatch.setattr(_settings, "BROWSER_MAX_TAB_COUNT", 2)
        active = FakePage(url="https://active.example")
        old = FakePage(url="https://old.example")
        newer = FakePage(url="https://newer.example")
        ctx = FakeContext(pages=[old, newer, active])
        tk = _make_toolkit(page=active, context=ctx)

        _run(tk._enforce_tab_cap())
        # At least one non-active page should now be closed.
        assert old._closed is True
        assert active._closed is False

    def test_does_not_exceed_cap_means_noop(self, monkeypatch):
        from config.settings import settings as _settings
        monkeypatch.setattr(_settings, "BROWSER_MAX_TAB_COUNT", 10)
        a = FakePage(url="https://a.com")
        b = FakePage(url="https://b.com")
        tk = _make_toolkit(page=a, context=FakeContext(pages=[a, b]))
        _run(tk._enforce_tab_cap())
        assert a._closed is False
        assert b._closed is False
