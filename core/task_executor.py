"""
OmniCore task batch executor (async-optimized)
- Unified async execution mode
- Worker instance reuse
- Parallel scheduling via asyncio.gather
"""
import atexit
import asyncio
import copy
import re
import threading
from typing import Dict, Any, List, Optional, Tuple

from agents.paod import classify_failure
from config.settings import settings
from utils.context_budget import truncate_tool_result, truncate_result_dict
from core.constants import (
    TaskStatus,
    TaskOutputType,
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


def is_task_ready(
    task: Dict[str, Any],
    task_queue: List[Dict[str, Any]],
    task_outputs: Optional[Dict[str, Any]] = None,
) -> bool:
    """Check whether a task's dependencies are satisfied and conditions are met."""
    depends = list(task.get("depends_on") or [])
    depends.extend(_infer_task_dependencies(task, task_queue))
    if depends:
        completed_ids = {
            queued_task["task_id"]
            for queued_task in task_queue
            if queued_task["status"] == str(TaskStatus.COMPLETED)
        }
        if not all(dep in completed_ids for dep in depends):
            return False

    # 条件检查：支持 conditional.when 表达式
    conditional = task.get("conditional")
    if conditional and task_outputs is not None:
        when_expr = conditional.get("when", "")
        if when_expr and not _evaluate_condition(when_expr, task_outputs):
            if conditional.get("else_skip", False):
                task["status"] = str(TaskStatus.COMPLETED)
                task["result"] = {"skipped": True, "reason": f"Condition not met: {when_expr}"}
            return False

    return True


_COST_ORDER = {"low": 0, "medium": 1, "high": 2}


def collect_ready_task_indexes(state: OmniCoreState) -> List[int]:
    """Collect indexes of pending tasks that are ready to run, sorted by estimated_cost (low first)."""
    ready_indexes: List[int] = []
    registry = get_builtin_tool_registry()
    task_outputs = state.get("task_outputs") or {}
    for idx, task in enumerate(state["task_queue"]):
        if task["status"] == str(TaskStatus.PENDING) and is_task_ready(task, state["task_queue"], task_outputs):
            if registry.resolve_task(task) is not None:
                ready_indexes.append(idx)
    # 按成本排序：低成本任务优先执行
    ready_indexes.sort(
        key=lambda i: _COST_ORDER.get(
            state["task_queue"][i].get("estimated_cost", "medium"), 1
        )
    )
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
    # 批次中已出现的非 concurrent_safe 工具类型（同类可并行，异类互斥）
    non_concurrent_safe_type: Optional[str] = None

    for idx in ready_indexes:
        if len(selected) >= max_total:
            break

        task = state["task_queue"][idx]
        registered_tool = registry.resolve_task(task)
        if registered_tool is None:
            tool_key = str(task.get("tool_name") or task.get("task_type") or f"task_{idx}")
            max_for_tool = 1
            serialized = False
            concurrent_safe = True
        else:
            tool_key = registered_tool.spec.name
            max_for_tool = max(registered_tool.max_parallelism, 1)
            serialized = registered_tool.serialized
            concurrent_safe = registered_tool.spec.concurrent_safe

        if serialized_selected:
            continue
        if serialized and selected:
            continue
        # concurrent_safe=False 的工具：同类可并行，但不与其他工具混批
        if not concurrent_safe:
            if selected and non_concurrent_safe_type != tool_key:
                continue
        else:
            # concurrent_safe=True 的工具：不能加入已有非 concurrent_safe 异类的批次
            if non_concurrent_safe_type is not None:
                continue
        if per_tool_counts.get(tool_key, 0) >= max_for_tool:
            continue

        selected.append(idx)
        per_tool_counts[tool_key] = per_tool_counts.get(tool_key, 0) + 1
        if serialized:
            serialized_selected = True
        if not concurrent_safe and non_concurrent_safe_type is None:
            non_concurrent_safe_type = tool_key

    return selected or [ready_indexes[0]]


async def _execute_registered_tool_async(
    local_task: Dict[str, Any],
    shared_memory_snapshot: Dict[str, Any],
    state: Optional[Dict[str, Any]] = None,
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

    # S4: 通过 Pipeline 执行（可通过 TOOL_PIPELINE_ENABLED=false 回退旧路径）
    if settings.TOOL_PIPELINE_ENABLED:
        from core.tool_pipeline import ToolPipeline
        pipeline = ToolPipeline()
        pipeline_state = state or {}
        ctx = await pipeline.execute(
            tool_name=registered_tool.spec.name,
            params=normalized_params,
            state=pipeline_state,
            registered_tool=registered_tool,
            shared_memory_snapshot=shared_memory_snapshot,
            description=str(local_task.get("description", "") or ""),
        )
        # Pipeline 返回的 raw_result 就是 adapter 的 outcome dict，直接复用
        if ctx.raw_result is not None:
            return ctx.raw_result
        # 校验阶段 fatal 失败（未到执行阶段）
        if ctx.normalized_result is not None:
            return ctx.normalized_result.to_outcome_dict(
                task_type=registered_tool.spec.task_type,
                tool_name=registered_tool.spec.name,
                params=normalized_params,
                execution_trace=local_task.get("execution_trace", []),
                risk_level=registered_tool.spec.risk_level,
            )

    return await execute_tool_via_adapter(
        local_task,
        shared_memory_snapshot,
        registered_tool,
    )


async def _execute_single_task_async(
    task: Dict[str, Any],
    shared_memory_snapshot: Dict[str, Any],
    state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Dispatch a task through the registered tool path."""
    local_task = copy.deepcopy(task)
    local_task["status"] = str(TaskStatus.RUNNING)
    try:
        return await _execute_registered_tool_async(local_task, shared_memory_snapshot, state=state)
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
            "error_trace": error_message,
            "risk_level": local_task.get("risk_level", "medium"),
        }


def _extract_typed_output(task: Dict[str, Any], outcome: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从任务执行结果中提取类型化输出，用于下游任务引用。"""
    result = outcome.get("result") or {}
    if isinstance(result, str):
        return {
            "type": str(TaskOutputType.TEXT_EXTRACTION),
            "content": result,
            "source_url": "",
        }
    if not isinstance(result, dict):
        return None

    # 优先从注册工具的 ToolSpec.output_type 获取输出类型，避免字符串 heuristic
    registered = _resolve_registered_tool(task)
    output_type = registered.spec.output_type if registered else ""

    if output_type == str(TaskOutputType.TEXT_EXTRACTION):
        return {
            "type": str(TaskOutputType.TEXT_EXTRACTION),
            "content": (
                result.get("extracted_text")
                or result.get("output")
                or result.get("text")
                or result.get("content", "")
            ),
            "source_url": result.get("url", ""),
        }
    if output_type == str(TaskOutputType.FILE_DOWNLOAD):
        file_path = result.get("file_path") or result.get("path", "")
        if file_path:
            return {
                "type": str(TaskOutputType.FILE_DOWNLOAD),
                "file_path": file_path,
                "file_size": result.get("file_size", 0),
            }
        # 文件工具读操作返回文本而非文件路径时降级为 text_extraction
        return {
            "type": str(TaskOutputType.TEXT_EXTRACTION),
            "content": result.get("content") or result.get("output", ""),
            "source_url": "",
        }
    if output_type == str(TaskOutputType.COMMAND_OUTPUT):
        return {
            "type": str(TaskOutputType.COMMAND_OUTPUT),
            "stdout": result.get("output") or result.get("stdout", ""),
            "returncode": result.get("returncode", 0),
        }

    # 通用兜底：output_type 未设置时按结果字段推断
    content = result.get("content") or result.get("output") or result.get("text")
    if content:
        return {
            "type": str(TaskOutputType.TEXT_EXTRACTION),
            "content": str(content),
            "source_url": result.get("url", ""),
        }
    return None


def _resolve_task_params(
    params: Dict[str, Any],
    task_outputs: Dict[str, Any],
) -> Dict[str, Any]:
    """
    解析任务参数中的 $ref 引用，支持跨任务数据传递。

    引用格式：
    - "$task_1.file_path"  → task_outputs["task_1"]["file_path"]
    - "$task_1.content"    → task_outputs["task_1"]["content"]
    - "$task_1"            → task_outputs["task_1"] (整个输出 dict)
    """
    if not task_outputs:
        return params

    ref_pattern = re.compile(r"\$([A-Za-z0-9_-]+)(?:\.([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*))?")

    def _lookup_ref(task_id: str, field: Optional[str], original: str) -> Any:
        task_out = task_outputs.get(task_id)
        if task_out is None:
            return original
        if not field:
            return task_out
        if isinstance(task_out, dict):
            current: Any = task_out
            for part in field.split("."):
                if isinstance(current, dict) and part in current:
                    current = current[part]
                else:
                    return original
            return current
        return original

    def _resolve_value(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: _resolve_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [_resolve_value(item) for item in value]
        if not isinstance(value, str):
            return value

        full_match = ref_pattern.fullmatch(value.strip())
        if full_match:
            return _lookup_ref(full_match.group(1), full_match.group(2), value)

        def _replace(match: re.Match) -> str:
            original = match.group(0)
            resolved_value = _lookup_ref(match.group(1), match.group(2), original)
            if resolved_value is original:
                return original
            return str(resolved_value)

        return ref_pattern.sub(_replace, value)

    return {k: _resolve_value(v) for k, v in params.items()}


def _evaluate_condition(when_expr: str, task_outputs: Dict[str, Any]) -> bool:
    """
    求值简单条件表达式（不使用 eval）。

    支持的操作符：
    - "$task_id.field ends_with .ext"
    - "$task_id.field starts_with prefix"
    - "$task_id.field contains substr"
    - "$task_id.field == value"
    - "$task_id.field != value"
    - "$task_id.field exists"
    """
    if not when_expr or not when_expr.strip():
        return True

    parts = when_expr.strip().split(None, 2)
    if len(parts) < 2:
        return True

    # 解析左值
    lhs_raw = parts[0]
    if lhs_raw.startswith("$"):
        ref = lhs_raw[1:]
        ref_parts = ref.split(".", 1)
        ref_task_id = ref_parts[0]
        task_out = task_outputs.get(ref_task_id, {})
        if len(ref_parts) > 1:
            lhs = str(task_out.get(ref_parts[1], ""))
        else:
            lhs = str(task_out)
    else:
        lhs = lhs_raw

    op = parts[1].lower()
    rhs = parts[2] if len(parts) > 2 else ""

    if op == "ends_with":
        return lhs.endswith(rhs)
    elif op == "starts_with":
        return lhs.startswith(rhs)
    elif op == "contains":
        return rhs in lhs
    elif op == "==":
        return lhs == rhs
    elif op == "!=":
        return lhs != rhs
    elif op == "exists":
        return bool(lhs)
    else:
        return True  # 不认识的操作符视为满足


def _apply_task_outcome(state: OmniCoreState, idx: int, outcome: Dict[str, Any]) -> None:
    """Apply a task execution outcome back into runtime state."""
    old_status = state["task_queue"][idx].get("status")
    state["task_queue"][idx]["task_type"] = outcome.get("task_type", state["task_queue"][idx]["task_type"])
    state["task_queue"][idx]["tool_name"] = outcome.get(
        "tool_name", state["task_queue"][idx].get("tool_name", "")
    )
    state["task_queue"][idx]["params"] = outcome.get("params", state["task_queue"][idx]["params"])
    state["task_queue"][idx]["status"] = outcome["status"]
    raw_result = outcome.get("result")
    state["task_queue"][idx]["result"] = truncate_result_dict(raw_result) if isinstance(raw_result, dict) else raw_result
    state["task_queue"][idx]["risk_level"] = outcome.get(
        "risk_level", state["task_queue"][idx].get("risk_level", "medium")
    )
    state["task_queue"][idx]["execution_trace"] = outcome.get(
        "execution_trace", state["task_queue"][idx].get("execution_trace", [])
    )
    state["task_queue"][idx]["failure_type"] = outcome.get("failure_type")

    if outcome.get("error_trace"):
        state["error_trace"] = outcome["error_trace"]

    # R5: track when task status last changed (for plan reminder)
    new_status = state["task_queue"][idx].get("status")
    if old_status != new_status:
        from core.plan_reminder import update_status_change_turn
        update_status_change_turn(state)

    # 多 Agent 协作：写入类型化输出到 task_outputs
    if outcome.get("status") == str(TaskStatus.COMPLETED):
        task = state["task_queue"][idx]
        typed_output = _extract_typed_output(task, outcome)
        if typed_output:
            if "task_outputs" not in state:
                state["task_outputs"] = {}
            task_id = task.get("task_id", "")
            if task_id:
                for field in ("content", "stdout", "text"):
                    if field in typed_output and isinstance(typed_output[field], str):
                        typed_output[field] = truncate_tool_result(typed_output[field])
                state["task_outputs"][task_id] = typed_output


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

    from core.message_bus import MessageBus
    bus = MessageBus.from_dict(state.get("message_bus", []))
    shared_memory_snapshot = bus.to_snapshot()

    # 多 Agent 协作：解析 $ref 参数引用
    task_outputs = state.get("task_outputs") or {}
    if task_outputs:
        for idx in batch_indexes:
            original_params = state["task_queue"][idx].get("params") or {}
            resolved = _resolve_task_params(original_params, task_outputs)
            if resolved != original_params:
                state["task_queue"][idx]["params"] = resolved
                log_agent_action(
                    "TaskExecutor",
                    f"解析 $ref 参数 ({state['task_queue'][idx]['task_id']})",
                    str({k: v for k, v in resolved.items() if v != original_params.get(k)}),
                )

    if len(batch_indexes) == 1:
        # 串行执行
        outcomes: List[Tuple[int, Dict[str, Any]]] = []
        for idx in batch_indexes:
            outcome = await _execute_single_task_async(
                state["task_queue"][idx], shared_memory_snapshot, state=state
            )
            outcomes.append((idx, outcome))
    else:
        # 并行执行
        tasks = [
            _execute_single_task_async(state["task_queue"][idx], shared_memory_snapshot, state=state)
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
