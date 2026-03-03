"""
OmniCore 浠诲姟鎵规鎵ц鍣紙寮傛浼樺寲鐗堬級
- 缁熶竴浣跨敤寮傛鎵ц妯″紡
- Worker 瀹炰緥澶嶇敤
- 浣跨敤 asyncio.gather 杩涜骞惰璋冨害
"""
import asyncio
import copy
from typing import Dict, Any, List, Optional, Tuple

from agents.paod import classify_failure, make_trace_step
from config.settings import settings
from core.capability_detector import CapabilityDetector
from core.constants import (
    TaskType,
    TaskStatus,
    FailureType,
    SUPPORTED_TASK_TYPES,
    BROWSER_RETRIES,
)
from core.llm import LLMClient
from core.model_registry import get_registry, ModelCapability
from core.state import OmniCoreState
from core.tool_registry import get_builtin_tool_registry
from utils.logger import log_agent_action, log_error, log_warning
from utils.retry import is_retryable

# 鍏ㄥ眬鑳藉姏妫€娴嬪櫒
_capability_detector = CapabilityDetector()


class WorkerPool:
    """
    Worker 瀹炰緥姹?
    澶嶇敤 Worker 瀹炰緥锛岄伩鍏嶉噸澶嶅垱寤?
    """

    _instance: Optional["WorkerPool"] = None
    _lock = asyncio.Lock()

    def __init__(self):
        self._web_worker = None
        self._file_worker = None
        self._system_worker = None

    @classmethod
    async def get_instance(cls) -> "WorkerPool":
        """鑾峰彇鍗曚緥瀹炰緥锛堢嚎绋嬪畨鍏級"""
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @property
    def web_worker(self):
        """鑾峰彇 WebWorker 瀹炰緥"""
        if self._web_worker is None:
            from agents.web_worker import WebWorker
            self._web_worker = WebWorker()
        return self._web_worker

    @property
    def file_worker(self):
        """鑾峰彇 FileWorker 瀹炰緥"""
        if self._file_worker is None:
            from agents.file_worker import FileWorker
            self._file_worker = FileWorker()
        return self._file_worker

    @property
    def system_worker(self):
        """鑾峰彇 SystemWorker 瀹炰緥"""
        if self._system_worker is None:
            from agents.system_worker import SystemWorker
            self._system_worker = SystemWorker()
        return self._system_worker

    def create_browser_agent(self, llm_client=None, headless: bool = True, toolkit=None):
        """
        鍒涘缓 BrowserAgent 瀹炰緥
        BrowserAgent 涓嶅鐢紝鍥犱负姣忎釜浠诲姟闇€瑕佺嫭绔嬬殑娴忚鍣ㄤ笂涓嬫枃
        """
        from agents.browser_agent import BrowserAgent
        return BrowserAgent(llm_client=llm_client, headless=headless, toolkit=toolkit)


def _infer_task_dependencies(task: Dict[str, Any], task_queue: List[Dict[str, Any]]) -> List[str]:
    """
    浠庝换鍔″弬鏁颁腑鎺ㄦ柇闅愬紡渚濊禆銆?
    閲嶇偣瑕嗙洊 file_worker 鐨?data_source/data_sources锛?
    閬垮厤 Router 婕忓啓 depends_on 鏃惰骞惰璋冨害鎵撲贡銆?
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
    supported_types = set(registry.supported_task_types())
    if not supported_types:
        supported_types = {str(t) for t in SUPPORTED_TASK_TYPES}
    for idx, task in enumerate(state["task_queue"]):
        if task["status"] == str(TaskStatus.PENDING) and is_task_ready(task, state["task_queue"]):
            if registry.resolve_task(task) is not None or task["task_type"] in supported_types:
                ready_indexes.append(idx)
    return ready_indexes


def _resolve_registered_tool(task: Dict[str, Any]):
    return get_builtin_tool_registry().resolve_task(task)


def resolve_model_for_task(task: Dict[str, Any]) -> Optional[str]:
    """Select a model based on the task's required capabilities."""
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
            log_agent_action("ModelRouter", f"浠诲姟 [{task.get('task_id')}] 鑳藉姏 {primary.value} -> 妯″瀷 {model}")
        return model
    except Exception as e:
        log_warning(f"妯″瀷鑷姩閫夋嫨澶辫触: {e}锛屽皢浣跨敤榛樿妯″瀷")
        return None


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
            task_type = task["task_type"]
            if task_type == str(TaskType.BROWSER_AGENT):
                tool_key = "browser_agent"
                max_for_tool = max(settings.MAX_PARALLEL_BROWSER_TASKS, 1)
                serialized = False
            elif task_type == str(TaskType.SYSTEM_WORKER):
                tool_key = "system_worker"
                max_for_tool = max(settings.MAX_PARALLEL_SYSTEM_TASKS, 1)
                serialized = True
            else:
                tool_key = task_type
                max_for_tool = max_total
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


async def _run_browser_task_async(task: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a browser task asynchronously."""
    from utils.browser_toolkit import BrowserToolkit

    params = task["params"]
    task_desc = params.get("task", task["description"])
    start_url = params.get("start_url", "")
    headless = params.get("headless", settings.BROWSER_FAST_MODE)
    resolved_model = resolve_model_for_task(task)

    result = None
    last_error = None
    pool = await WorkerPool.get_instance()

    for attempt in range(BROWSER_RETRIES):
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
                log_warning(f"鍒濆鍖?BrowserAgent 浠诲姟妯″瀷澶辫触: {exc}锛屽洖閫€榛樿妯″瀷")

        agent = pool.create_browser_agent(llm_client=task_llm, headless=headless, toolkit=toolkit)

        try:
            result = await agent.run(task_desc, start_url)
            break
        except Exception as exc:
            last_error = exc
            if attempt < BROWSER_RETRIES - 1 and is_retryable(exc):
                log_warning(f"Browser Agent 寮傚父锛堝彲閲嶈瘯锛夛紝绗?{attempt + 2} 娆″皾璇? {str(exc)[:80]}")
                continue
            log_error(f"Browser Agent 鎵ц澶辫触: {exc}")
            break
        finally:
            await agent.close()

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
            "status": str(TaskStatus.COMPLETED) if result.get("success") else str(TaskStatus.FAILED),
            "task_type": task["task_type"],
            "params": task["params"],
            "result": result,
            "execution_trace": task.get("execution_trace", []),
            "failure_type": task.get("failure_type"),
            "shared_memory": result,
            "error_trace": "" if result.get("success") else result.get("message", "Browser task failed"),
        }

    error_message = str(last_error) if last_error else "鏈煡寮傚父"
    task["failure_type"] = classify_failure(error_message)
    task["execution_trace"] = [
        make_trace_step(1, "run browser_agent", task_desc[:80], error_message, "exception"),
    ]
    return {
        "status": str(TaskStatus.FAILED),
        "task_type": task["task_type"],
        "params": task["params"],
        "result": {"success": False, "error": error_message},
        "execution_trace": task.get("execution_trace", []),
        "failure_type": task.get("failure_type"),
        "shared_memory": {"success": False, "error": error_message},
        "error_trace": error_message,
    }


async def _execute_registered_tool_async(
    local_task: Dict[str, Any],
    shared_memory_snapshot: Dict[str, Any],
) -> Dict[str, Any]:
    registered_tool = _resolve_registered_tool(local_task)
    if registered_tool is None:
        task_type = local_task.get("task_type", "unknown")
        return {
            "status": str(TaskStatus.FAILED),
            "task_type": task_type,
            "tool_name": local_task.get("tool_name", ""),
            "params": local_task.get("params", {}),
            "result": {"success": False, "error": f"Unknown task type or tool: {task_type}"},
            "execution_trace": local_task.get("execution_trace", []),
            "failure_type": str(FailureType.INVALID_INPUT),
            "shared_memory": None,
            "error_trace": f"Unknown task type or tool: {task_type}",
            "risk_level": local_task.get("risk_level", "medium"),
        }

    adapter_name = registered_tool.adapter_name
    task_type = registered_tool.spec.task_type
    tool_name = registered_tool.spec.name
    local_task["task_type"] = task_type
    local_task["tool_name"] = tool_name
    local_task["risk_level"] = registered_tool.spec.risk_level

    pool = await WorkerPool.get_instance()

    if adapter_name == "web_worker":
        resolved_model = resolve_model_for_task(local_task)
        if resolved_model:
            local_task["params"]["_resolved_model"] = resolved_model

        worker = pool.web_worker
        result = await worker.execute_async(local_task, shared_memory_snapshot)

        clean_params = copy.deepcopy(local_task["params"])
        clean_params.pop("_resolved_model", None)

        if isinstance(result, dict) and result.get("_switch_worker"):
            target = result.get("_switch_worker")
            patch = result.get("_switch_params", {})
            params = copy.deepcopy(clean_params)
            params.update(patch)
            target_tool = get_builtin_tool_registry().get_by_task_type(target)
            return {
                "status": str(TaskStatus.PENDING),
                "task_type": target,
                "tool_name": target_tool.spec.name if target_tool else "",
                "params": params,
                "result": None,
                "execution_trace": local_task.get("execution_trace", []),
                "failure_type": None,
                "shared_memory": None,
                "error_trace": "",
                "risk_level": target_tool.spec.risk_level if target_tool else local_task.get("risk_level", "medium"),
            }

        return {
            "status": str(TaskStatus.COMPLETED) if result.get("success") else str(TaskStatus.FAILED),
            "task_type": task_type,
            "tool_name": tool_name,
            "params": clean_params,
            "result": result,
            "execution_trace": local_task.get("execution_trace", []),
            "failure_type": local_task.get("failure_type"),
            "shared_memory": result.get("data") if result.get("success") and result.get("data") else None,
            "error_trace": "" if result.get("success") else result.get("error", "Unknown error"),
            "risk_level": local_task.get("risk_level", "medium"),
        }

    if adapter_name == "file_worker":
        worker = pool.file_worker
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, worker.execute, local_task, shared_memory_snapshot
        )
        return {
            "status": str(TaskStatus.COMPLETED) if result.get("success") else str(TaskStatus.FAILED),
            "task_type": task_type,
            "tool_name": tool_name,
            "params": local_task["params"],
            "result": result,
            "execution_trace": local_task.get("execution_trace", []),
            "failure_type": local_task.get("failure_type"),
            "shared_memory": result,
            "error_trace": "" if result.get("success") else result.get("error", "Unknown error"),
            "risk_level": local_task.get("risk_level", "medium"),
        }

    if adapter_name == "system_worker":
        worker = pool.system_worker
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, worker.execute, local_task, shared_memory_snapshot
        )
        return {
            "status": str(TaskStatus.COMPLETED) if result.get("success") else str(TaskStatus.FAILED),
            "task_type": task_type,
            "tool_name": tool_name,
            "params": local_task["params"],
            "result": result,
            "execution_trace": local_task.get("execution_trace", []),
            "failure_type": local_task.get("failure_type"),
            "shared_memory": result,
            "error_trace": "" if result.get("success") else result.get("error", "Unknown error"),
            "risk_level": local_task.get("risk_level", "medium"),
        }

    if adapter_name == "browser_agent":
        outcome = await _run_browser_task_async(local_task)
        outcome["task_type"] = task_type
        outcome["tool_name"] = tool_name
        outcome["risk_level"] = local_task.get("risk_level", "medium")
        return outcome

    return {
        "status": str(TaskStatus.FAILED),
        "task_type": task_type,
        "tool_name": tool_name,
        "params": local_task.get("params", {}),
        "result": {"success": False, "error": f"Unknown tool adapter: {adapter_name}"},
        "execution_trace": local_task.get("execution_trace", []),
        "failure_type": str(FailureType.INVALID_INPUT),
        "shared_memory": None,
        "error_trace": f"Unknown tool adapter: {adapter_name}",
        "risk_level": local_task.get("risk_level", "medium"),
    }


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
    寮傛鎵ц褰撳墠鎵规 ready 浠诲姟銆?
    浣跨敤 asyncio.gather 杩涜骞惰璋冨害銆?
    """
    ready_indexes = collect_ready_task_indexes(state)
    batch_indexes = _select_batch_indexes(state, ready_indexes)
    if not batch_indexes:
        return state

    for idx in batch_indexes:
        state["task_queue"][idx]["status"] = str(TaskStatus.RUNNING)

    task_labels = [
        f"{state['task_queue'][idx]['task_id']}:{state['task_queue'][idx]['task_type']}"
        for idx in batch_indexes
    ]
    log_agent_action("TaskExecutor", f"鎵ц鎵规浠诲姟 ({len(batch_indexes)})", ", ".join(task_labels))

    shared_memory_snapshot = dict(state["shared_memory"])

    # 妫€鏌ユ槸鍚︽湁绯荤粺浠诲姟锛堢郴缁熶换鍔′覆琛屾墽琛岋級
    has_system_task = any(
        state["task_queue"][idx]["task_type"] == str(TaskType.SYSTEM_WORKER)
        for idx in batch_indexes
    )

    if len(batch_indexes) == 1 or has_system_task:
        # 涓茶鎵ц
        outcomes: List[Tuple[int, Dict[str, Any]]] = []
        for idx in batch_indexes:
            outcome = await _execute_single_task_async(
                state["task_queue"][idx], shared_memory_snapshot
            )
            outcomes.append((idx, outcome))
    else:
        # 骞惰鎵ц
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
                    "params": state["task_queue"][idx]["params"],
                    "result": {"success": False, "error": error_message},
                    "execution_trace": state["task_queue"][idx].get("execution_trace", []),
                    "failure_type": classify_failure(error_message),
                    "shared_memory": None,
                    "error_trace": error_message,
                }))
            else:
                outcomes.append((idx, result))

    for idx, outcome in sorted(outcomes, key=lambda item: item[0]):
        _apply_task_outcome(state, idx, outcome)

    return state


def run_ready_batch(state: OmniCoreState) -> OmniCoreState:
    """
    鎵ц褰撳墠鎵规 ready 浠诲姟锛堝悓姝ュ寘瑁呭櫒锛夈€?
    妫€娴嬫槸鍚﹀凡鍦ㄤ簨浠跺惊鐜腑杩愯锛岄伩鍏嶅祵濂楄皟鐢ㄩ棶棰樸€?
    """
    try:
        loop = asyncio.get_running_loop()
        # 宸插湪浜嬩欢寰幆涓紝涓嶈兘鐢?asyncio.run
        # 鍒涘缓涓€涓柊浠诲姟骞剁瓑寰呭畬鎴?
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(asyncio.run, run_ready_batch_async(state))
            return future.result()
    except RuntimeError:
        # 娌℃湁杩愯涓殑浜嬩欢寰幆锛屽彲浠ュ畨鍏ㄤ娇鐢?asyncio.run
        return asyncio.run(run_ready_batch_async(state))
