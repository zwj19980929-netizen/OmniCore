"""
Unit tests for B4: BrowserToolkit.list_frames / list_tabs surface + the
BrowserExecutionLayer iframe auto-scan fallback.

Playwright Page/Frame/Context are mocked. We only exercise the glue:
- list_frames returns correct structure (incl. main vs child, domain)
- list_tabs returns correct structure (incl. is_active marker)
- _iframe_auto_scan_click / _input iterate child frames until success
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


# --------- Fakes ---------


class FakeFrame:
    def __init__(self, url: str = "", name: str = "", detached: bool = False):
        self.url = url
        self.name = name
        self._detached = detached

    def is_detached(self) -> bool:
        return self._detached


class FakePage:
    def __init__(
        self,
        url: str = "about:blank",
        frames: Optional[List[FakeFrame]] = None,
        title: str = "",
    ):
        self.url = url
        self._closed = False
        self._title = title
        self.main_frame = FakeFrame(url=url, name="__main__")
        # Playwright's page.frames always includes main_frame first
        self.frames: List[FakeFrame] = [self.main_frame] + (frames or [])

    def is_closed(self) -> bool:
        return self._closed

    async def close(self) -> None:
        self._closed = True

    async def title(self) -> str:
        return self._title


class FakeContext:
    def __init__(self, pages: Optional[List[FakePage]] = None):
        self.pages: List[FakePage] = pages or []


def _tk_with(page: Optional[FakePage] = None, ctx: Optional[FakeContext] = None) -> BrowserToolkit:
    tk = BrowserToolkit(headless=True)
    tk._page = page
    tk._context = ctx
    return tk


# --------- list_frames ---------


class TestListFrames:
    def test_includes_main_and_children_with_domain(self):
        child1 = FakeFrame(url="https://pay.stripe.com/frame", name="stripe")
        child2 = FakeFrame(url="https://captcha.example.com/", name="cap")
        page = FakePage(url="https://shop.example.com/cart", frames=[child1, child2])
        tk = _tk_with(page=page)

        r = _run(tk.list_frames(include_main=True))
        assert r.success
        data = r.data
        # 1 main + 2 children
        assert len(data) == 3
        main_row = next(f for f in data if f["is_main"])
        assert main_row["domain"] == "shop.example.com"
        stripe = next(f for f in data if f["name"] == "stripe")
        assert stripe["is_main"] is False
        assert stripe["domain"] == "pay.stripe.com"

    def test_exclude_main(self):
        child = FakeFrame(url="https://x.com", name="x")
        page = FakePage(frames=[child])
        tk = _tk_with(page=page)
        r = _run(tk.list_frames(include_main=False))
        assert r.success
        assert len(r.data) == 1
        assert r.data[0]["name"] == "x"

    def test_no_page_returns_empty(self):
        tk = _tk_with(page=None)
        r = _run(tk.list_frames())
        assert r.success
        assert r.data == []


# --------- list_tabs ---------


class TestListTabs:
    def test_marks_active_and_collects_title(self):
        p1 = FakePage(url="https://a.com", title="A")
        p2 = FakePage(url="https://b.com", title="B")
        p3 = FakePage(url="https://c.com", title="C")
        ctx = FakeContext(pages=[p1, p2, p3])
        tk = _tk_with(page=p2, ctx=ctx)

        r = _run(tk.list_tabs())
        assert r.success
        tabs = r.data
        assert len(tabs) == 3
        active = [t for t in tabs if t["is_active"]]
        assert len(active) == 1
        assert active[0]["url"] == "https://b.com"
        titles = {t["title"] for t in tabs}
        assert titles == {"A", "B", "C"}

    def test_skips_closed_tabs(self):
        open_tab = FakePage(url="https://live.com")
        closed = FakePage(url="https://dead.com")
        closed._closed = True
        tk = _tk_with(page=open_tab, ctx=FakeContext(pages=[open_tab, closed]))
        r = _run(tk.list_tabs())
        assert r.success
        assert len(r.data) == 1
        assert r.data[0]["url"] == "https://live.com"

    def test_no_context_returns_empty(self):
        tk = _tk_with()
        r = _run(tk.list_tabs())
        assert r.success
        assert r.data == []


# --------- iframe auto-scan fallback in BrowserExecutionLayer ---------


class ScanToolkit:
    """Minimal stand-in exposing just what _iframe_auto_scan_* touches."""

    def __init__(self, frames_data: List[Dict[str, Any]], successes: List[str]):
        self._frames_data = frames_data
        self._in_iframe = False
        self.switched: List[str] = []
        self.clicked_in_frame: List[str] = []
        self.inputs_in_frame: List[str] = []
        self.exited = 0
        self._success_frames = set(successes)
        self._active_frame: Optional[str] = None

    async def list_frames(self, include_main: bool = True):
        data = self._frames_data
        if not include_main:
            data = [f for f in data if not f.get("is_main")]
        return ToolkitResult(success=True, data=data)

    async def get_current_url(self):
        return ToolkitResult(success=True, data="https://host.example/")

    async def switch_to_iframe(self, selector: str):
        # Mark which frame we switched into by the selector value (name-based)
        # e.g. iframe[name="alpha"]
        name = ""
        if 'name="' in selector:
            name = selector.split('name="', 1)[1].rstrip('"]')
        self.switched.append(name)
        self._active_frame = name
        self._in_iframe = True
        return ToolkitResult(success=True)

    async def click(self, selector: str):
        self.clicked_in_frame.append(f"{self._active_frame}:{selector}")
        if self._active_frame in self._success_frames:
            return ToolkitResult(success=True)
        return ToolkitResult(success=False, error="miss")

    async def input_text(self, selector: str, value: str):
        self.inputs_in_frame.append(f"{self._active_frame}:{selector}:{value}")
        if self._active_frame in self._success_frames:
            return ToolkitResult(success=True)
        return ToolkitResult(success=False, error="miss")

    async def exit_iframe(self):
        self.exited += 1
        self._in_iframe = False
        self._active_frame = None
        return ToolkitResult(success=True)


@pytest.fixture
def auto_scan_on(monkeypatch):
    from config.settings import settings as _settings
    monkeypatch.setattr(_settings, "BROWSER_IFRAME_AUTO_SCAN_ON_STUCK", True)


class TestIframeAutoScan:
    def test_scans_frames_until_click_succeeds(self, auto_scan_on):
        from agents.browser_execution import BrowserExecutionLayer

        frames = [
            {"index": 0, "name": "", "url": "", "is_main": True, "is_detached": False, "domain": ""},
            {"index": 1, "name": "alpha", "url": "https://a.test", "is_main": False, "is_detached": False, "domain": "a.test"},
            {"index": 2, "name": "beta",  "url": "https://b.test", "is_main": False, "is_detached": False, "domain": "b.test"},
        ]
        tk = ScanToolkit(frames_data=frames, successes={"beta"})
        layer = BrowserExecutionLayer(toolkit=tk, agent_name="t")
        ok = _run(layer._iframe_auto_scan_click("#x", action=None))
        assert ok is True
        # Tried alpha first (missed + exit), then beta (hit)
        assert tk.switched == ["alpha", "beta"]
        assert tk.exited == 1  # only alpha miss triggered exit; beta stays
        assert tk.clicked_in_frame == ["alpha:#x", "beta:#x"]

    def test_no_frames_returns_false(self, auto_scan_on):
        from agents.browser_execution import BrowserExecutionLayer
        frames = [
            {"index": 0, "name": "", "url": "", "is_main": True, "is_detached": False, "domain": ""},
        ]
        tk = ScanToolkit(frames_data=frames, successes=set())
        layer = BrowserExecutionLayer(toolkit=tk, agent_name="t")
        ok = _run(layer._iframe_auto_scan_click("#x", action=None))
        assert ok is False

    def test_feature_off_noop(self, monkeypatch):
        from config.settings import settings as _settings
        monkeypatch.setattr(_settings, "BROWSER_IFRAME_AUTO_SCAN_ON_STUCK", False)
        from agents.browser_execution import BrowserExecutionLayer
        frames = [{"index": 1, "name": "x", "url": "https://y", "is_main": False, "is_detached": False, "domain": "y"}]
        tk = ScanToolkit(frames_data=frames, successes={"x"})
        layer = BrowserExecutionLayer(toolkit=tk, agent_name="t")
        ok = _run(layer._iframe_auto_scan_click("#x", action=None))
        assert ok is False
        assert tk.switched == []

    def test_input_auto_scan_succeeds_in_last_frame(self, auto_scan_on):
        from agents.browser_execution import BrowserExecutionLayer
        frames = [
            {"index": 0, "name": "", "url": "", "is_main": True, "is_detached": False, "domain": ""},
            {"index": 1, "name": "one", "url": "https://one.com", "is_main": False, "is_detached": False, "domain": "one.com"},
            {"index": 2, "name": "two", "url": "https://two.com", "is_main": False, "is_detached": False, "domain": "two.com"},
        ]
        tk = ScanToolkit(frames_data=frames, successes={"two"})
        layer = BrowserExecutionLayer(toolkit=tk, agent_name="t")
        ok = _run(layer._iframe_auto_scan_input("input#q", "hello", action=None))
        assert ok is True
        assert tk.switched == ["one", "two"]
        assert tk.inputs_in_frame == ["one:input#q:hello", "two:input#q:hello"]
