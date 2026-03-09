import asyncio

from config.settings import settings
from utils.browser_runtime_pool import (
    BrowserPoolCircuitOpenError,
    BrowserPoolLaunchError,
    BrowserRuntimePool,
)


class FakeBrowser:
    def __init__(self):
        self.closed = False

    def is_connected(self):
        return not self.closed

    async def close(self):
        self.closed = True


def test_pool_reuses_browser_for_same_runtime_key():
    async def _run():
        pool = BrowserRuntimePool(asyncio.get_running_loop())
        launched = []

        async def _fake_launch(headless: bool):
            browser = FakeBrowser()
            launched.append((headless, browser))
            return browser

        pool._launch_browser_locked = _fake_launch

        lease_one = await pool.acquire_browser(headless=True)
        lease_two = await pool.acquire_browser(headless=True)

        assert lease_one.browser is lease_two.browser
        stats = pool.snapshot_stats()
        assert stats["launches"] == 1
        assert stats["reuse_hits"] == 1
        assert stats["active_leases"] == 2

        await lease_one.release()
        await lease_two.release()

        assert pool.snapshot_stats()["releases"] == 2
        await pool.close_all()
        assert launched[0][1].closed is True

    asyncio.run(_run())


def test_browser_lease_release_is_idempotent():
    async def _run():
        pool = BrowserRuntimePool(asyncio.get_running_loop())

        async def _fake_launch(headless: bool):
            return FakeBrowser()

        pool._launch_browser_locked = _fake_launch

        lease = await pool.acquire_browser(headless=True)
        await lease.release()
        await lease.release()

        stats = pool.snapshot_stats()
        assert stats["releases"] == 1
        assert stats["active_leases"] == 0

    asyncio.run(_run())


def test_browser_lease_release_can_retry_after_pool_release_failure():
    async def _run():
        pool = BrowserRuntimePool(asyncio.get_running_loop())

        async def _fake_launch(headless: bool):
            return FakeBrowser()

        pool._launch_browser_locked = _fake_launch
        lease = await pool.acquire_browser(headless=True)
        original_release_browser = pool.release_browser
        attempts = 0

        async def _flaky_release(current_lease):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary release failure")
            await original_release_browser(current_lease)

        pool.release_browser = _flaky_release

        try:
            await lease.release()
            assert False, "expected the first release to fail"
        except RuntimeError:
            pass

        assert lease._released is False

        await lease.release()

        assert lease._released is True
        assert pool.snapshot_stats()["releases"] == 1

    asyncio.run(_run())


def test_pool_waits_for_capacity_then_reuses_browser(monkeypatch):
    async def _run():
        monkeypatch.setattr(settings, "BROWSER_POOL_MAX_BROWSERS_PER_KEY", 1)
        monkeypatch.setattr(settings, "BROWSER_POOL_MAX_CONTEXTS_PER_BROWSER", 1)
        monkeypatch.setattr(settings, "BROWSER_POOL_ACQUIRE_TIMEOUT_SECONDS", 1)
        pool = BrowserRuntimePool(asyncio.get_running_loop())
        launched = []

        async def _fake_launch(headless: bool):
            browser = FakeBrowser()
            launched.append((headless, browser))
            return browser

        pool._launch_browser_locked = _fake_launch

        lease_one = await pool.acquire_browser(headless=True)
        acquire_task = asyncio.create_task(pool.acquire_browser(headless=True))

        await asyncio.sleep(0.05)
        assert acquire_task.done() is False

        await lease_one.release()
        lease_two = await acquire_task

        assert lease_two.browser is launched[0][1]
        stats = pool.snapshot_stats()
        assert stats["launches"] == 1
        assert stats["waits"] == 1
        assert stats["reuse_hits"] == 1

        await lease_two.release()
        await pool.close_all()

    asyncio.run(_run())


def test_pool_launches_second_browser_before_waiting(monkeypatch):
    async def _run():
        monkeypatch.setattr(settings, "BROWSER_POOL_MAX_BROWSERS_PER_KEY", 2)
        monkeypatch.setattr(settings, "BROWSER_POOL_MAX_CONTEXTS_PER_BROWSER", 1)
        monkeypatch.setattr(settings, "BROWSER_POOL_ACQUIRE_TIMEOUT_SECONDS", 1)
        pool = BrowserRuntimePool(asyncio.get_running_loop())
        launched = []

        async def _fake_launch(headless: bool):
            browser = FakeBrowser()
            launched.append((headless, browser))
            return browser

        pool._launch_browser_locked = _fake_launch

        lease_one = await pool.acquire_browser(headless=True)
        lease_two = await pool.acquire_browser(headless=True)

        assert lease_one.browser is not lease_two.browser
        stats = pool.snapshot_stats()
        assert stats["launches"] == 2
        assert stats["pooled_browsers"] == 2
        assert stats["active_leases"] == 2
        assert stats["waits"] == 0

        await lease_one.release()
        await lease_two.release()
        await pool.close_all()

    asyncio.run(_run())


def test_pool_times_out_when_capacity_does_not_free(monkeypatch):
    async def _run():
        monkeypatch.setattr(settings, "BROWSER_POOL_MAX_BROWSERS_PER_KEY", 1)
        monkeypatch.setattr(settings, "BROWSER_POOL_MAX_CONTEXTS_PER_BROWSER", 1)
        monkeypatch.setattr(settings, "BROWSER_POOL_ACQUIRE_TIMEOUT_SECONDS", 1)
        pool = BrowserRuntimePool(asyncio.get_running_loop())

        async def _fake_launch(headless: bool):
            return FakeBrowser()

        pool._launch_browser_locked = _fake_launch

        lease = await pool.acquire_browser(headless=True)

        try:
            await asyncio.wait_for(pool.acquire_browser(headless=True), timeout=2)
            assert False, "expected a timeout while waiting for browser capacity"
        except TimeoutError:
            pass

        await lease.release()
        assert pool.snapshot_stats()["wait_timeouts"] == 1

    asyncio.run(_run())


def test_pool_opens_circuit_after_repeated_launch_failures(monkeypatch):
    async def _run():
        monkeypatch.setattr(settings, "BROWSER_POOL_CIRCUIT_BREAK_THRESHOLD", 2)
        monkeypatch.setattr(settings, "BROWSER_POOL_CIRCUIT_BREAK_SECONDS", 30)
        pool = BrowserRuntimePool(asyncio.get_running_loop())

        async def _failing_launch(headless: bool):
            raise RuntimeError(f"launch failed for headless={headless}")

        pool._launch_browser_locked = _failing_launch

        for _ in range(2):
            try:
                await pool.acquire_browser(headless=True)
                assert False, "expected pooled launch failure"
            except BrowserPoolLaunchError:
                pass

        try:
            await pool.acquire_browser(headless=True)
            assert False, "expected circuit breaker to reject pooled launch"
        except BrowserPoolCircuitOpenError:
            pass

        stats = pool.snapshot_stats()
        assert stats["launch_failures"] == 2
        assert stats["circuit_open_rejections"] == 1
        assert stats["circuit_open"] is True

    asyncio.run(_run())
