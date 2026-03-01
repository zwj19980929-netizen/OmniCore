"""
OmniCore 任务批次执行器
负责从状态中选取当前批次可执行任务，并按配置进行并行调度
"""
import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Any, List, Optional, Tuple

from agents.browser_agent import BrowserAgent
from agents.file_worker import FileWorker
from agents.system_worker import SystemWorker
from agents.web_worker import WebWorker
from agents.paod import classify_failure, make_trace_step
from config.settings import settings
from core.capability_detector import CapabilityDetector
from core.llm import LLMClient
from core.model_registry import get_registry, ModelCapability
from core.state import OmniCoreState
from utils.browser_toolkit import BrowserToolkit
from utils.logger import log_agent_action, log_error, log_warning
from utils.retry import is_retryable

_capability_detector = CapabilityDetector()
_SUPPORTED_TASK_TYPES = {"web_worker", "file_worker", "system_worker", "browser_agent"}
_BROWSER_RETRIES = 2


def _infer_task_dependencies(task: Dict[str, Any], task_queue: List[Dict[str, Any]]) -> List[str]:
    """
    从任务参数中推断隐式依赖。
    重点覆盖 file_worker 的 data_source/data_sources，避免 Router 漏写 depends_on 时被并行调度打乱。
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
    """检查任务依赖是否都已经完成。"""
    depends = list(task.get("depends_on") or [])
    depends.extend(_infer_task_dependencies(task, task_queue))
    if not depends:
        return True
    completed_ids = {
        queued_task["task_id"]
        for queued_task in task_queue
        if queued_task["status"] == "completed"
    }
    return all(dep in completed_ids for dep in depends)


def collect_ready_task_indexes(state: OmniCoreState) -> List[int]:
    """收集当前状态下所有 ready 的 pending 任务索引。"""
    ready_indexes: List[int] = []
    for idx, task in enumerate(state["task_queue"]):
        if task["status"] == "pending" and is_task_ready(task, state["task_queue"]):
            if task["task_type"] in _SUPPORTED_TASK_TYPES:
                ready_indexes.append(idx)
    return ready_indexes


def resolve_model_for_task(task: Dict[str, Any]) -> Optional[str]:
    """
    根据任务的 required_capabilities 选择最合适的模型。
    """
    try:
        registry = get_registry()
        required_caps = task.get("required_capabilities", [])

        if not required_caps:
            detected = _capability_detector.detect(
                task.get("description", ""),
                task.get("params"),
            )
            required_caps = [cap.value for cap in detected]

        capability_set = set()
        for capability in required_caps:
            try:
                capability_set.add(ModelCapability(capability))
            except ValueError:
                continue

        if not capability_set:
            return None

        primary = _capability_detector.get_primary_capability(capability_set)
        model = registry.get_model_for_capability(primary)
        if model:
            log_agent_action("ModelRouter", f"任务 [{task.get('task_id')}] 能力 {primary.value} → 模型 {model}")
        return model
    except Exception as e:
        log_warning(f"模型自动选择失败: {e}，将使用默认模型")
        return None


def _select_batch_indexes(state: OmniCoreState, ready_indexes: List[int]) -> List[int]:
    if not ready_indexes:
        return []

    if not settings.ENABLE_PARALLEL_EXECUTION:
        return [ready_indexes[0]]

    max_total = max(settings.MAX_PARALLEL_TASKS, 1)
    max_browser = max(settings.MAX_PARALLEL_BROWSER_TASKS, 1)
    max_system = max(settings.MAX_PARALLEL_SYSTEM_TASKS, 1)

    selected: List[int] = []
    browser_count = 0
    system_count = 0

    for idx in ready_indexes:
        if len(selected) >= max_total:
            break

        task_type = state["task_queue"][idx]["task_type"]
        if task_type == "browser_agent" and browser_count >= max_browser:
            continue
        if task_type == "system_worker" and system_count >= max_system:
            continue

        selected.append(idx)
        if task_type == "browser_agent":
            browser_count += 1
        elif task_type == "system_worker":
            system_count += 1

    return selected or [ready_indexes[0]]


def _run_browser_task_item(task: Dict[str, Any]) -> Dict[str, Any]:
    params = task["params"]
    task_desc = params.get("task", task["description"])
    start_url = params.get("start_url", "")
    headless = params.get("headless", settings.BROWSER_FAST_MODE)
    resolved_model = resolve_model_for_task(task)

    result = None
    last_error = None

    for attempt in range(_BROWSER_RETRIES):
        async def _run_browser():
            toolkit = BrowserToolkit(
                headless=headless,
                fast_mode=settings.BROWSER_FAST_MODE,
                block_heavy_resources=settings.BLOCK_HEAVY_RESOURCES,
            )
            task_llm = None
            if resolved_model:
                try:
                    task_llm = LLMClient(model=resolved_model)
                except Exception as exc:
                    log_warning(f"初始化 BrowserAgent 任务模型失败: {exc}，回退默认模型")
            agent = BrowserAgent(llm_client=task_llm, headless=headless, toolkit=toolkit)
            try:
                return await agent.run(task_desc, start_url)
            finally:
                await agent.close()

        try:
            result = asyncio.run(_run_browser())
            break
        except Exception as exc:
            last_error = exc
            if attempt < _BROWSER_RETRIES - 1 and is_retryable(exc):
                log_warning(f"Browser Agent 异常（可重试），第 {attempt + 2} 次尝试: {str(exc)[:80]}")
                continue
            log_error(f"Browser Agent 执行失败: {exc}")
            break

    if result is not None:
        trace = []
        for step_no, step in enumerate(result.get("steps", []), 1):
            trace.append(make_trace_step(
                step_no=step_no,
                plan=step.get("plan", step.get("action_type", "")),
                action=step.get("action", step.get("selector", "")),
                observation=step.get("observation", step.get("result", "")),
                decision=step.get("decision", "continue"),
            ))
        task["execution_trace"] = trace
        task["result"] = result
        if not result.get("success"):
            task["failure_type"] = classify_failure(
                result.get("message", result.get("error", ""))
            )
        return {
            "status": "completed" if result.get("success") else "failed",
            "task_type": task["task_type"],
            "params": task["params"],
            "result": result,
            "execution_trace": task.get("execution_trace", []),
            "failure_type": task.get("failure_type"),
            "shared_memory": result,
            "error_trace": "" if result.get("success") else result.get("message", "浏览器任务失败"),
        }

    error_message = str(last_error) if last_error else "未知异常"
    task["failure_type"] = classify_failure(error_message)
    task["execution_trace"] = [
        make_trace_step(1, "run browser_agent", task_desc[:80], error_message, "exception"),
    ]
    return {
        "status": "failed",
        "task_type": task["task_type"],
        "params": task["params"],
        "result": {"success": False, "error": error_message},
        "execution_trace": task.get("execution_trace", []),
        "failure_type": task.get("failure_type"),
        "shared_memory": {"success": False, "error": error_message},
        "error_trace": error_message,
    }


def _execute_single_task(task: Dict[str, Any], shared_memory_snapshot: Dict[str, Any]) -> Dict[str, Any]:
    local_task = copy.deepcopy(task)
    local_task["status"] = "running"

    try:
        task_type = local_task["task_type"]

        if task_type == "web_worker":
            resolved_model = resolve_model_for_task(local_task)
            if resolved_model:
                local_task["params"]["_resolved_model"] = resolved_model
            result = WebWorker().execute(local_task, shared_memory_snapshot)
            clean_params = copy.deepcopy(local_task["params"])
            clean_params.pop("_resolved_model", None)

            if isinstance(result, dict) and result.get("_switch_worker"):
                target = result.get("_switch_worker")
                patch = result.get("_switch_params", {})
                params = copy.deepcopy(clean_params)
                params.update(patch)
                return {
                    "status": "pending",
                    "task_type": target,
                    "params": params,
                    "result": None,
                    "execution_trace": local_task.get("execution_trace", []),
                    "failure_type": None,
                    "shared_memory": None,
                    "error_trace": "",
                }

            return {
                "status": "completed" if result.get("success") else "failed",
                "task_type": task_type,
                "params": clean_params,
                "result": result,
                "execution_trace": local_task.get("execution_trace", []),
                "failure_type": local_task.get("failure_type"),
                "shared_memory": result.get("data") if result.get("success") and result.get("data") else None,
                "error_trace": "" if result.get("success") else result.get("error", "未知错误"),
            }

        if task_type == "file_worker":
            result = FileWorker().execute(local_task, shared_memory_snapshot)
            return {
                "status": "completed" if result.get("success") else "failed",
                "task_type": task_type,
                "params": local_task["params"],
                "result": result,
                "execution_trace": local_task.get("execution_trace", []),
                "failure_type": local_task.get("failure_type"),
                "shared_memory": result,
                "error_trace": "" if result.get("success") else result.get("error", "未知错误"),
            }

        if task_type == "system_worker":
            result = SystemWorker().execute(local_task, shared_memory_snapshot)
            return {
                "status": "completed" if result.get("success") else "failed",
                "task_type": task_type,
                "params": local_task["params"],
                "result": result,
                "execution_trace": local_task.get("execution_trace", []),
                "failure_type": local_task.get("failure_type"),
                "shared_memory": result,
                "error_trace": "" if result.get("success") else result.get("error", "未知错误"),
            }

        if task_type == "browser_agent":
            return _run_browser_task_item(local_task)

        return {
            "status": "failed",
            "task_type": task_type,
            "params": local_task.get("params", {}),
            "result": {"success": False, "error": f"未知任务类型: {task_type}"},
            "execution_trace": local_task.get("execution_trace", []),
            "failure_type": "invalid_input",
            "shared_memory": None,
            "error_trace": f"未知任务类型: {task_type}",
        }

    except Exception as e:
        error_message = str(e)
        return {
            "status": "failed",
            "task_type": local_task.get("task_type", "unknown"),
            "params": local_task.get("params", {}),
            "result": {"success": False, "error": error_message},
            "execution_trace": local_task.get("execution_trace", []),
            "failure_type": classify_failure(error_message),
            "shared_memory": None,
            "error_trace": error_message,
        }


def _apply_task_outcome(state: OmniCoreState, idx: int, outcome: Dict[str, Any]) -> None:
    state["task_queue"][idx]["task_type"] = outcome.get("task_type", state["task_queue"][idx]["task_type"])
    state["task_queue"][idx]["params"] = outcome.get("params", state["task_queue"][idx]["params"])
    state["task_queue"][idx]["status"] = outcome["status"]
    state["task_queue"][idx]["result"] = outcome.get("result")
    state["task_queue"][idx]["execution_trace"] = outcome.get("execution_trace", state["task_queue"][idx].get("execution_trace", []))
    state["task_queue"][idx]["failure_type"] = outcome.get("failure_type")

    if outcome.get("shared_memory") is not None:
        task_id = state["task_queue"][idx]["task_id"]
        state["shared_memory"][task_id] = outcome["shared_memory"]

    if outcome.get("error_trace"):
        state["error_trace"] = outcome["error_trace"]


def run_ready_batch(state: OmniCoreState) -> OmniCoreState:
    """
    执行当前批次 ready 任务。
    同一批次内的任务基于 shared_memory 快照运行，避免并发写共享状态。
    """
    ready_indexes = collect_ready_task_indexes(state)
    batch_indexes = _select_batch_indexes(state, ready_indexes)
    if not batch_indexes:
        return state

    for idx in batch_indexes:
        state["task_queue"][idx]["status"] = "running"

    task_labels = [
        f"{state['task_queue'][idx]['task_id']}:{state['task_queue'][idx]['task_type']}"
        for idx in batch_indexes
    ]
    log_agent_action("TaskExecutor", f"执行批次任务 ({len(batch_indexes)})", ", ".join(task_labels))

    shared_memory_snapshot = dict(state["shared_memory"])
    outcomes: List[Tuple[int, Dict[str, Any]]] = []
    has_system_task = any(
        state["task_queue"][idx]["task_type"] == "system_worker"
        for idx in batch_indexes
    )

    if len(batch_indexes) == 1 or has_system_task:
        for idx in batch_indexes:
            outcomes.append((idx, _execute_single_task(state["task_queue"][idx], shared_memory_snapshot)))
    else:
        with ThreadPoolExecutor(max_workers=len(batch_indexes)) as executor:
            future_map = {
                executor.submit(_execute_single_task, state["task_queue"][idx], shared_memory_snapshot): idx
                for idx in batch_indexes
            }
            for future in as_completed(future_map):
                idx = future_map[future]
                try:
                    outcomes.append((idx, future.result()))
                except Exception as e:
                    error_message = str(e)
                    outcomes.append((idx, {
                        "status": "failed",
                        "task_type": state["task_queue"][idx]["task_type"],
                        "params": state["task_queue"][idx]["params"],
                        "result": {"success": False, "error": error_message},
                        "execution_trace": state["task_queue"][idx].get("execution_trace", []),
                        "failure_type": classify_failure(error_message),
                        "shared_memory": None,
                        "error_trace": error_message,
                    }))

    for idx, outcome in sorted(outcomes, key=lambda item: item[0]):
        _apply_task_outcome(state, idx, outcome)

    return state
