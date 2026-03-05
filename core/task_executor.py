"""
OmniCore task batch executor (async-optimized)
- Unified async execution mode
- Worker instance reuse
- Parallel scheduling via asyncio.gather
"""
import atexit
import asyncio
import copy
import threading
from typing import Dict, Any, List, Optional, Tuple

from agents.paod import classify_failure
from config.settings import settings
from core.constants import (
    TaskStatus,
    FailureType,
)
from core.state import OmniCoreState
from core.tool_adapters import WorkerPool, execute_tool_via_adapter
from core.tool_registry import get_builtin_tool_registry
from utils.logger import log_agent_action


_EXECUTOR_LOOP_LOCK = threading.Lock()
_EXECUTOR_LOOP: Optional[asyncio.AbstractEventLoop] = None
_EXECUTOR_THREAD: Optional[threading.Thread] = None


def _executor_loop_main(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    try:
        loop.run_forever()
    finally:
        pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            try:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        try:
            loop.run_until_complete(loop.shutdown_default_executor())
        except Exception:
            pass
        loop.close()


def _ensure_executor_loop() -> asyncio.AbstractEventLoop:
    global _EXECUTOR_LOOP, _EXECUTOR_THREAD
    with _EXECUTOR_LOOP_LOCK:
        if (
            _EXECUTOR_LOOP is not None
            and _EXECUTOR_THREAD is not None
            and _EXECUTOR_THREAD.is_alive()
            and not _EXECUTOR_LOOP.is_closed()
        ):
            return _EXECUTOR_LOOP

        loop = asyncio.new_event_loop()
        thread = threading.Thread(
            target=_executor_loop_main,
            args=(loop,),
            name="omnicore-task-executor-loop",
            daemon=True,
        )
        thread.start()
        _EXECUTOR_LOOP = loop
        _EXECUTOR_THREAD = thread
        return loop


def shutdown_executor_runtime(timeout_seconds: float = 8.0) -> None:
    global _EXECUTOR_LOOP, _EXECUTOR_THREAD
    with _EXECUTOR_LOOP_LOCK:
        loop = _EXECUTOR_LOOP
        thread = _EXECUTOR_THREAD
        _EXECUTOR_LOOP = None
        _EXECUTOR_THREAD = None

    if loop is None or loop.is_closed():
        return

    timeout = max(float(timeout_seconds or 0), 1.0)

    try:
        from utils.browser_runtime_pool import close_all_browser_runtime_pools

        future = asyncio.run_coroutine_threadsafe(close_all_browser_runtime_pools(), loop)
        future.result(timeout=timeout)
    except Exception:
        pass

    try:
        loop.call_soon_threadsafe(loop.stop)
    except Exception:
        return

    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)


atexit.register(shutdown_executor_runtime)


def _infer_task_dependencies(task: Dict[str, Any], task_queue: List[Dict[str, Any]]) -> List[str]:
    """
    Infer implicit dependencies from task parameters.
    Prioritizes file_worker data_source/data_sources references.
    Prevents dependency races when Router omits depends_on.
    """
    params = task.get("params", {})
    references: List[str] = []

    data_source = params.get("data_source")
    if isinstance(data_source, str) and data_source.strip():
        references.append(data_source.strip())

    data_sources = params.get("data_sources")
    if isinstance(data_sources, list):
        references.extend(
            str(source).strip()
            for source in data_sources
            if str(source).strip()
        )

    if not references:
        return []

    inferred: List[str] = []
    for reference in references:
        for queued_task in task_queue:
            task_id = queued_task.get("task_id", "")
            if not task_id:
                continue
            if reference == task_id or reference in task_id or task_id in reference:
                inferred.append(task_id)

    return list(dict.fromkeys(inferred))


def is_task_ready(task: Dict[str, Any], task_queue: List[Dict[str, Any]]) -> bool:
    """Check whether a task's dependencies are satisfied."""
    depends = list(task.get("depends_on") or [])
    depends.extend(_infer_task_dependencies(task, task_queue))
    if not depends:
        return True
    completed_ids = {
        queued_task["task_id"]
        for queued_task in task_queue
        if queued_task["status"] == str(TaskStatus.COMPLETED)
    }
    return all(dep in completed_ids for dep in depends)


def collect_ready_task_indexes(state: OmniCoreState) -> List[int]:
    """Collect indexes of pending tasks that are ready to run."""
    ready_indexes: List[int] = []
    registry = get_builtin_tool_registry()
    for idx, task in enumerate(state["task_queue"]):
        if task["status"] == str(TaskStatus.PENDING) and is_task_ready(task, state["task_queue"]):
            if registry.resolve_task(task) is not None:
                ready_indexes.append(idx)
    return ready_indexes


def _resolve_registered_tool(task: Dict[str, Any]):
    return get_builtin_tool_registry().resolve_task(task)


def _select_batch_indexes(state: OmniCoreState, ready_indexes: List[int]) -> List[int]:
    """Select task indexes for the next execution batch."""
    if not ready_indexes:
        return []

    if not settings.ENABLE_PARALLEL_EXECUTION:
        return [ready_indexes[0]]

    registry = get_builtin_tool_registry()
    max_total = max(settings.MAX_PARALLEL_TASKS, 1)

    selected: List[int] = []
    per_tool_counts: Dict[str, int] = {}
    serialized_selected = False

    for idx in ready_indexes:
        if len(selected) >= max_total:
            break

        task = state["task_queue"][idx]
        registered_tool = registry.resolve_task(task)
        if registered_tool is None:
            tool_key = str(task.get("tool_name") or task.get("task_type") or f"task_{idx}")
            max_for_tool = 1
            serialized = False
        else:
            tool_key = registered_tool.spec.name
            max_for_tool = max(registered_tool.max_parallelism, 1)
            serialized = registered_tool.serialized

        if serialized_selected:
            continue
        if serialized and selected:
            continue
        if per_tool_counts.get(tool_key, 0) >= max_for_tool:
            continue

        selected.append(idx)
        per_tool_counts[tool_key] = per_tool_counts.get(tool_key, 0) + 1
        if serialized:
            serialized_selected = True

    return selected or [ready_indexes[0]]


async def _execute_registered_tool_async(
    local_task: Dict[str, Any],
    shared_memory_snapshot: Dict[str, Any],
) -> Dict[str, Any]:
    registered_tool = _resolve_registered_tool(local_task)
    if registered_tool is None:
        unknown_identifier = str(
            local_task.get("tool_name")
            or local_task.get("task_type")
            or "unknown"
        )
        return {
            "status": str(TaskStatus.FAILED),
            "task_type": str(local_task.get("task_type", "unknown") or "unknown"),
            "tool_name": local_task.get("tool_name", ""),
            "params": local_task.get("params", {}),
            "result": {"success": False, "error": f"Unknown tool or compatibility task type: {unknown_identifier}"},
            "execution_trace": local_task.get("execution_trace", []),
            "failure_type": str(FailureType.INVALID_INPUT),
            "shared_memory": None,
            "error_trace": f"Unknown tool or compatibility task type: {unknown_identifier}",
            "risk_level": local_task.get("risk_level", "medium"),
        }

    local_task["task_type"] = registered_tool.spec.task_type
    local_task["tool_name"] = registered_tool.spec.name
    local_task["risk_level"] = registered_tool.spec.risk_level
    normalized_params = copy.deepcopy(
        local_task.get("tool_args") or local_task.get("params") or {}
    )
    local_task["params"] = normalized_params
    local_task["tool_args"] = copy.deepcopy(normalized_params)
    return await execute_tool_via_adapter(
        local_task,
        shared_memory_snapshot,
        registered_tool,
    )


async def _execute_single_task_async(
    task: Dict[str, Any],
    shared_memory_snapshot: Dict[str, Any],
) -> Dict[str, Any]:
    """Dispatch a task through the registered tool path."""
    local_task = copy.deepcopy(task)
    local_task["status"] = str(TaskStatus.RUNNING)
    try:
        return await _execute_registered_tool_async(local_task, shared_memory_snapshot)
    except Exception as e:
        error_message = str(e)
        return {
            "status": str(TaskStatus.FAILED),
            "task_type": local_task.get("task_type", "unknown"),
            "tool_name": local_task.get("tool_name", ""),
            "params": local_task.get("params", {}),
            "result": {"success": False, "error": error_message},
            "execution_trace": local_task.get("execution_trace", []),
            "failure_type": classify_failure(error_message),
            "shared_memory": None,
            "error_trace": error_message,
            "risk_level": local_task.get("risk_level", "medium"),
        }


def _apply_task_outcome(state: OmniCoreState, idx: int, outcome: Dict[str, Any]) -> None:
    """Apply a task execution outcome back into runtime state."""
    state["task_queue"][idx]["task_type"] = outcome.get("task_type", state["task_queue"][idx]["task_type"])
    state["task_queue"][idx]["tool_name"] = outcome.get(
        "tool_name", state["task_queue"][idx].get("tool_name", "")
    )
    state["task_queue"][idx]["params"] = outcome.get("params", state["task_queue"][idx]["params"])
    state["task_queue"][idx]["status"] = outcome["status"]
    state["task_queue"][idx]["result"] = outcome.get("result")
    state["task_queue"][idx]["risk_level"] = outcome.get(
        "risk_level", state["task_queue"][idx].get("risk_level", "medium")
    )
    state["task_queue"][idx]["execution_trace"] = outcome.get(
        "execution_trace", state["task_queue"][idx].get("execution_trace", [])
    )
    state["task_queue"][idx]["failure_type"] = outcome.get("failure_type")

    if outcome.get("shared_memory") is not None:
        task_id = state["task_queue"][idx]["task_id"]
        state["shared_memory"][task_id] = outcome["shared_memory"]

    if outcome.get("error_trace"):
        state["error_trace"] = outcome["error_trace"]


async def run_ready_batch_async(state: OmniCoreState) -> OmniCoreState:
    """
    Execute the current ready batch asynchronously.
    Uses asyncio.gather for parallel dispatch.
    """
    ready_indexes = collect_ready_task_indexes(state)
    batch_indexes = _select_batch_indexes(state, ready_indexes)
    if not batch_indexes:
        return state

    for idx in batch_indexes:
        state["task_queue"][idx]["status"] = str(TaskStatus.RUNNING)

    task_labels = [
        f"{state['task_queue'][idx]['task_id']}:{state['task_queue'][idx].get('tool_name') or state['task_queue'][idx]['task_type']}"
        for idx in batch_indexes
    ]
    log_agent_action("TaskExecutor", f"执行批次任务 ({len(batch_indexes)})", ", ".join(task_labels))

    shared_memory_snapshot = dict(state["shared_memory"])

    if len(batch_indexes) == 1:
        # 串行执行
        outcomes: List[Tuple[int, Dict[str, Any]]] = []
        for idx in batch_indexes:
            outcome = await _execute_single_task_async(
                state["task_queue"][idx], shared_memory_snapshot
            )
            outcomes.append((idx, outcome))
    else:
        # 并行执行
        tasks = [
            _execute_single_task_async(state["task_queue"][idx], shared_memory_snapshot)
            for idx in batch_indexes
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        outcomes = []
        for idx, result in zip(batch_indexes, results):
            if isinstance(result, Exception):
                error_message = str(result)
                outcomes.append((idx, {
                    "status": str(TaskStatus.FAILED),
                    "task_type": state["task_queue"][idx]["task_type"],
                    "tool_name": state["task_queue"][idx].get("tool_name", ""),
                    "params": state["task_queue"][idx]["params"],
                    "result": {"success": False, "error": error_message},
                    "execution_trace": state["task_queue"][idx].get("execution_trace", []),
                    "failure_type": classify_failure(error_message),
                    "shared_memory": None,
                    "error_trace": error_message,
                    "risk_level": state["task_queue"][idx].get("risk_level", "medium"),
                }))
            else:
                outcomes.append((idx, result))

    for idx, outcome in sorted(outcomes, key=lambda item: item[0]):
        _apply_task_outcome(state, idx, outcome)

    return state


def run_ready_batch(state: OmniCoreState) -> OmniCoreState:
    """
    Execute the current ready batch (sync wrapper).
    Reuses a dedicated background event loop to avoid loop churn on Windows.
    """
    loop = _ensure_executor_loop()
    future = asyncio.run_coroutine_threadsafe(run_ready_batch_async(state), loop)
    return future.result()
