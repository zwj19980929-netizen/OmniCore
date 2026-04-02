import asyncio

from core import task_executor


def test_run_ready_batch_reuses_background_loop(monkeypatch):
    task_executor.shutdown_executor_runtime()
    observed_loop_ids = []

    async def _fake_run_ready_batch_async(state):
        observed_loop_ids.append(id(asyncio.get_running_loop()))
        return state

    monkeypatch.setattr(task_executor, "run_ready_batch_async", _fake_run_ready_batch_async)

    state = {"task_queue": [], "message_bus": []}
    task_executor.run_ready_batch(state)
    task_executor.run_ready_batch(state)

    assert len(observed_loop_ids) == 2
    assert len(set(observed_loop_ids)) == 1
    task_executor.shutdown_executor_runtime()


def test_shutdown_executor_runtime_closes_browser_pools(monkeypatch):
    task_executor.shutdown_executor_runtime()
    close_calls = {"count": 0}

    async def _fake_close_all_browser_runtime_pools(timeout_seconds: float = 8.0):
        _ = timeout_seconds
        close_calls["count"] += 1

    async def _fake_run_ready_batch_async(state):
        return state

    monkeypatch.setattr(
        "utils.browser_runtime_pool.close_all_browser_runtime_pools",
        _fake_close_all_browser_runtime_pools,
    )
    monkeypatch.setattr(task_executor, "run_ready_batch_async", _fake_run_ready_batch_async)

    state = {"task_queue": [], "message_bus": []}
    task_executor.run_ready_batch(state)
    task_executor.shutdown_executor_runtime(timeout_seconds=2.0)

    assert close_calls["count"] == 1
