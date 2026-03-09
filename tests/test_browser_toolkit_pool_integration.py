import asyncio

from config.settings import settings
from utils.browser_runtime_pool import BrowserLease, BrowserPoolCircuitOpenError
from utils.browser_toolkit import BrowserToolkit


class FakePage:
    def __init__(self):
        self.closed = False
        self.init_scripts = []

    async def add_init_script(self, script):
        self.init_scripts.append(script)

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self):
        self.closed = False
        self.page = FakePage()

    async def new_page(self):
        return self.page

    async def close(self):
        self.closed = True

    async def route(self, *_args, **_kwargs):
        return None

    async def storage_state(self, **_kwargs):
        return {}


class FakeBrowser:
    def __init__(self):
        self.closed = False
        self.contexts = []

    def is_connected(self):
        return not self.closed

    async def new_context(self, **_kwargs):
        context = FakeContext()
        self.contexts.append(context)
        return context

    async def close(self):
        self.closed = True


class FakePool:
    def __init__(self, browser_factory=FakeBrowser):
        self.browser_factory = browser_factory
        self.browser = None
        self.acquires = 0
        self.reuse_hits = 0
        self.launches = 0
        self.releases = 0
        self.active_leases = 0

    async def acquire_browser(self, headless=True):
        self.acquires += 1
        if self.browser is None or not self.browser.is_connected():
            self.browser = self.browser_factory()
            self.launches += 1
        else:
            self.reuse_hits += 1
        self.active_leases += 1
        return BrowserLease(
            pool=self,
            key=("chromium", bool(headless)),
            browser_id=1,
            browser=self.browser,
        )

    async def release_browser(self, _lease):
        self.releases += 1
        self.active_leases = max(self.active_leases - 1, 0)

    def snapshot_stats(self):
        return {
            "acquires": self.acquires,
            "reuse_hits": self.reuse_hits,
            "launches": self.launches,
            "releases": self.releases,
            "cleanup_closes": 0,
            "waits": 0,
            "wait_timeouts": 0,
            "pooled_browsers": 1 if self.browser and self.browser.is_connected() else 0,
            "active_leases": self.active_leases,
        }


class FakeChromium:
    def __init__(self, browser):
        self._browser = browser

    async def launch(self, **_kwargs):
        return self._browser


class FakeDirectPlaywright:
    def __init__(self, browser):
        self.chromium = FakeChromium(browser)
        self.stopped = False

    async def stop(self):
        self.stopped = True


class FakePlaywrightFactory:
    def __init__(self, browser):
        self._browser = browser

    async def start(self):
        return FakeDirectPlaywright(self._browser)


class FakeCircuitOpenPool:
    async def acquire_browser(self, headless=True):
        raise BrowserPoolCircuitOpenError(f"circuit open for headless={headless}")

    def snapshot_stats(self):
        return {"circuit_open": True}


class FailingContextBrowser(FakeBrowser):
    async def new_context(self, **_kwargs):
        raise RuntimeError("context creation boom")


def test_browser_toolkit_uses_pool_and_releases_leases(monkeypatch):
    async def _run():
        fake_pool = FakePool()
        monkeypatch.setattr(settings, "BROWSER_POOL_ENABLED", True)
        monkeypatch.setattr("utils.browser_toolkit.get_browser_runtime_pool", lambda: fake_pool)

        first = BrowserToolkit(headless=True, block_heavy_resources=False)
        second = BrowserToolkit(headless=True, block_heavy_resources=False)

        first_result = await first.create_page()
        second_result = await second.create_page()

        assert first_result.success is True
        assert second_result.success is True
        assert fake_pool.acquires == 2
        assert fake_pool.launches == 1
        assert fake_pool.reuse_hits == 1
        assert fake_pool.active_leases == 2
        assert first._browser is second._browser
        assert len(fake_pool.browser.contexts) == 2

        first_context = fake_pool.browser.contexts[0]
        second_context = fake_pool.browser.contexts[1]
        first_page = first_context.page
        second_page = second_context.page

        await first.close()
        assert fake_pool.releases == 1
        assert fake_pool.active_leases == 1
        assert first_context.closed is True
        assert first_page.closed is True
        assert first.page is None
        assert first.context is None

        await second.close()
        assert fake_pool.releases == 2
        assert fake_pool.active_leases == 0
        assert second_context.closed is True
        assert second_page.closed is True
        assert second.page is None
        assert second.context is None

    asyncio.run(_run())


def test_browser_toolkit_falls_back_to_direct_launch_when_pool_is_open(monkeypatch):
    async def _run():
        fake_browser = FakeBrowser()
        monkeypatch.setattr(settings, "BROWSER_POOL_ENABLED", True)
        monkeypatch.setattr(
            "utils.browser_toolkit.get_browser_runtime_pool",
            lambda: FakeCircuitOpenPool(),
        )
        monkeypatch.setattr(
            "utils.browser_toolkit.async_playwright",
            lambda: FakePlaywrightFactory(fake_browser),
        )

        toolkit = BrowserToolkit(headless=True, block_heavy_resources=False)
        result = await toolkit.create_page()

        assert result.success is True
        assert toolkit._browser_lease is None
        assert toolkit._browser is fake_browser
        assert toolkit.context is not None

        await toolkit.close()
        assert fake_browser.closed is True

    asyncio.run(_run())


def test_browser_toolkit_releases_lease_when_create_page_fails(monkeypatch):
    async def _run():
        fake_pool = FakePool(browser_factory=FailingContextBrowser)
        monkeypatch.setattr(settings, "BROWSER_POOL_ENABLED", True)
        monkeypatch.setattr("utils.browser_toolkit.get_browser_runtime_pool", lambda: fake_pool)

        toolkit = BrowserToolkit(headless=True, block_heavy_resources=False)
        result = await toolkit.create_page()

        assert result.success is False
        assert "context creation boom" in (result.error or "")
        assert fake_pool.acquires == 1
        assert fake_pool.releases == 1
        assert fake_pool.active_leases == 0
        assert toolkit._browser_lease is None
        assert toolkit._browser is None
        assert toolkit.context is None
        assert toolkit.page is None

    asyncio.run(_run())
