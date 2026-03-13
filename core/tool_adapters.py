"""
Tool adapter implementations for the staged move away from hard-coded executor
branches inside ``task_executor``.
"""
from __future__ import annotations

import asyncio
import copy
import json
import importlib
import importlib.util
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

from agents.paod import classify_failure, make_trace_step
from config.settings import settings
from core.capability_detector import CapabilityDetector
from core.constants import BROWSER_RETRIES, FailureType, TaskStatus
from core.llm import LLMClient
from core.model_registry import ModelCapability, get_registry
from core.tool_registry import RegisteredTool, get_builtin_tool_registry
from utils.logger import log_agent_action, log_error, log_warning
from utils.retry import is_retryable

_capability_detector = CapabilityDetector()


class WorkerPool:
    """Lazy singleton that owns long-lived worker instances."""

    _instance: Optional["WorkerPool"] = None
    _lock = asyncio.Lock()

    def __init__(self):
        self._web_worker = None
        self._file_worker = None
        self._system_worker = None

    @classmethod
    async def get_instance(cls) -> "WorkerPool":
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @property
    def web_worker(self):
        if self._web_worker is None:
            from agents.web_worker import WebWorker

            self._web_worker = WebWorker()
        return self._web_worker

    @property
    def file_worker(self):
        if self._file_worker is None:
            from agents.file_worker import FileWorker

            self._file_worker = FileWorker()
        return self._file_worker

    @property
    def system_worker(self):
        if self._system_worker is None:
            from agents.system_worker import SystemWorker

            self._system_worker = SystemWorker()
        return self._system_worker

    def create_browser_agent(self, llm_client=None, headless: bool = True, toolkit=None):
        from agents.browser_agent import BrowserAgent

        return BrowserAgent(llm_client=llm_client, headless=headless, toolkit=toolkit)


def resolve_model_for_task(task: Dict[str, Any]) -> Optional[str]:
    """Select the best model for a task based on declared capabilities."""
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
            log_agent_action(
                "ModelRouter",
                f"Task [{task.get('task_id')}] capability {primary.value} -> model {model}",
            )
        return model
    except Exception as exc:
        log_warning(f"Model auto-selection failed: {exc}; falling back to default model")
        return None


def _base_outcome(
    task: Dict[str, Any],
    registered_tool: RegisteredTool,
    *,
    status: str,
    result: Any,
    shared_memory: Any,
    error_trace: str,
    failure_type: Optional[str],
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "status": status,
        "task_type": registered_tool.spec.task_type,
        "tool_name": registered_tool.spec.name,
        "params": params if params is not None else task.get("params", {}),
        "result": result,
        "execution_trace": task.get("execution_trace", []),
        "failure_type": failure_type,
        "shared_memory": shared_memory,
        "error_trace": error_trace,
        "risk_level": task.get("risk_level", registered_tool.spec.risk_level),
    }


class BaseToolAdapter:
    """Base class for tool-specific execution adapters."""

    async def execute(
        self,
        task: Dict[str, Any],
        shared_memory_snapshot: Dict[str, Any],
        registered_tool: RegisteredTool,
    ) -> Dict[str, Any]:
        raise NotImplementedError


_registered_adapter_types: Dict[str, type[BaseToolAdapter]] = {}
_loaded_plugin_modules: set[str] = set()
_loaded_plugin_files: set[str] = set()
_plugin_load_errors: Dict[str, str] = {}
_plugin_load_lock = threading.Lock()


def register_tool_adapter_class(adapter_name: str, adapter_cls: type[BaseToolAdapter]) -> None:
    """Register an adapter class for lazy runtime instantiation."""
    _registered_adapter_types[str(adapter_name)] = adapter_cls


def tool_adapter(adapter_name: str):
    """Decorator used by adapters to self-register with the runtime registry."""

    def decorator(adapter_cls: type[BaseToolAdapter]) -> type[BaseToolAdapter]:
        register_tool_adapter_class(adapter_name, adapter_cls)
        return adapter_cls

    return decorator


def load_tool_adapter_plugins(
    module_names: Optional[list[str]] = None,
    *,
    retry_failed: bool = False,
) -> Dict[str, str]:
    """Import configured adapter plugin modules so they can self-register."""
    targets = module_names if module_names is not None else list(settings.TOOL_ADAPTER_PLUGIN_MODULES)
    configured_store = {
        "module_sources": [],
        "directory_sources": [],
        "disabled_plugin_ids": [],
        "blocked_modules": [],
        "blocked_files": [],
    }
    try:
        from utils.tool_plugin_store import get_tool_plugin_store

        configured_store = get_tool_plugin_store().get_config()
    except Exception:
        configured_store = dict(configured_store)

    normalized = [
        str(item).strip()
        for item in [*targets, *configured_store.get("module_sources", [])]
        if str(item or "").strip()
    ]
    normalized = [item for item in dict.fromkeys(normalized) if item not in set(configured_store.get("blocked_modules", []))]
    configured_dirs = [
        str(item).strip()
        for item in [*settings.TOOL_ADAPTER_PLUGIN_DIRS, *configured_store.get("directory_sources", [])]
        if str(item or "").strip()
    ]
    configured_dirs = list(dict.fromkeys(configured_dirs))
    blocked_files = {
        str(Path(item).expanduser())
        for item in configured_store.get("blocked_files", [])
        if str(item or "").strip()
    }
    if not normalized and not configured_dirs:
        return dict(_plugin_load_errors)

    with _plugin_load_lock:
        for module_name in normalized:
            if module_name in _loaded_plugin_modules:
                continue
            if not retry_failed and module_name in _plugin_load_errors:
                continue
            try:
                module = importlib.import_module(module_name)
            except Exception as exc:
                _plugin_load_errors[module_name] = str(exc)
                log_warning(f"Tool adapter plugin import failed for {module_name}: {exc}")
                continue

            try:
                from core.tool_registry import register_plugin_manifest_dict

                register_plugin_manifest_dict(
                    getattr(module, "PLUGIN_MANIFEST", {}),
                    source=module_name,
                )
            except Exception as exc:
                _plugin_load_errors[module_name] = str(exc)
                log_warning(f"Tool adapter plugin manifest registration failed for {module_name}: {exc}")
                continue

            _loaded_plugin_modules.add(module_name)
            _plugin_load_errors.pop(module_name, None)

        for raw_dir in configured_dirs:
            plugin_dir = Path(str(raw_dir)).expanduser()
            dir_key = str(plugin_dir)
            if not plugin_dir.is_dir():
                _plugin_load_errors[dir_key] = "Plugin directory not found"
                continue

            for file_path in sorted(plugin_dir.rglob("*.py")):
                if file_path.name == "__init__.py" or file_path.name.startswith("_"):
                    continue
                file_key = str(file_path)
                if file_key in blocked_files:
                    continue
                if file_key in _loaded_plugin_files:
                    continue
                if not retry_failed and file_key in _plugin_load_errors:
                    continue

                synthetic_name = f"omnicore_tool_plugin_{abs(hash(file_key))}"
                try:
                    spec = importlib.util.spec_from_file_location(synthetic_name, file_path)
                    if spec is None or spec.loader is None:
                        raise ImportError("Unable to build module spec")
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                except Exception as exc:
                    _plugin_load_errors[file_key] = str(exc)
                    log_warning(f"Tool adapter plugin import failed for {file_path}: {exc}")
                    continue

                try:
                    from core.tool_registry import register_plugin_manifest_dict

                    register_plugin_manifest_dict(
                        getattr(module, "PLUGIN_MANIFEST", {}),
                        source=file_key,
                    )
                except Exception as exc:
                    _plugin_load_errors[file_key] = str(exc)
                    log_warning(f"Tool adapter plugin manifest registration failed for {file_path}: {exc}")
                    continue

                _loaded_plugin_files.add(file_key)
                _plugin_load_errors.pop(file_key, None)

    return dict(_plugin_load_errors)


def get_tool_adapter_plugin_status() -> Dict[str, Any]:
    """Expose the current plugin loading state for debugging and UI surfaces."""
    registered_tools = []
    plugin_manifests = []
    plugin_store = {
        "module_sources": [],
        "directory_sources": [],
        "disabled_plugin_ids": [],
        "blocked_modules": [],
        "blocked_files": [],
    }
    try:
        from utils.tool_plugin_store import get_tool_plugin_store

        plugin_store = get_tool_plugin_store().get_config()
    except Exception:
        plugin_store = dict(plugin_store)
    try:
        from core.tool_registry import (
            is_plugin_enabled,
            list_registered_plugin_manifests,
            list_registered_plugin_tools,
        )

        plugin_tools = list_registered_plugin_tools()
        registered_tools = [
            item.spec.name
            for item in plugin_tools
        ]
        tool_names_by_plugin: Dict[str, list[str]] = {}
        for item in plugin_tools:
            plugin_id = str(item.plugin_id or "").strip()
            if not plugin_id:
                continue
            tool_names_by_plugin.setdefault(plugin_id, []).append(item.spec.name)

        for manifest in list_registered_plugin_manifests():
            plugin_manifests.append(
                {
                    "plugin_id": manifest.plugin_id,
                    "version": manifest.version,
                    "description": manifest.description,
                    "dependencies": list(manifest.dependencies),
                    "source": manifest.source,
                    "enabled": is_plugin_enabled(manifest.plugin_id),
                    "tools": sorted(tool_names_by_plugin.get(manifest.plugin_id, [])),
                }
            )
    except Exception:
        registered_tools = []
        plugin_manifests = []

    return {
        "configured_modules": list(settings.TOOL_ADAPTER_PLUGIN_MODULES),
        "configured_directories": list(settings.TOOL_ADAPTER_PLUGIN_DIRS),
        "installed_modules": list(plugin_store.get("module_sources", [])),
        "installed_directories": list(plugin_store.get("directory_sources", [])),
        "disabled_plugin_ids": list(plugin_store.get("disabled_plugin_ids", [])),
        "blocked_modules": list(plugin_store.get("blocked_modules", [])),
        "blocked_files": list(plugin_store.get("blocked_files", [])),
        "loaded_modules": sorted(_loaded_plugin_modules),
        "loaded_files": sorted(_loaded_plugin_files),
        "load_errors": dict(_plugin_load_errors),
        "registered_adapters": sorted(_registered_adapter_types.keys()),
        "registered_tools": sorted(registered_tools),
        "plugin_manifests": sorted(plugin_manifests, key=lambda item: item.get("plugin_id", "")),
    }


def _refresh_plugin_runtime_state() -> Dict[str, Any]:
    from core.tool_registry import reset_builtin_tool_registry

    reset_builtin_tool_registry()
    load_tool_adapter_plugins(retry_failed=True)
    return get_tool_adapter_plugin_status()


def install_tool_plugin_module(module_name: str) -> Dict[str, Any]:
    from utils.tool_plugin_store import get_tool_plugin_store

    get_tool_plugin_store().install_module(module_name)
    return _refresh_plugin_runtime_state()


def install_tool_plugin_directory(directory: str) -> Dict[str, Any]:
    from utils.tool_plugin_store import get_tool_plugin_store

    get_tool_plugin_store().install_directory(directory)
    return _refresh_plugin_runtime_state()


def enable_tool_plugin(plugin_id: str) -> Dict[str, Any]:
    from utils.tool_plugin_store import get_tool_plugin_store

    get_tool_plugin_store().enable_plugin(plugin_id)
    return _refresh_plugin_runtime_state()


def disable_tool_plugin(plugin_id: str) -> Dict[str, Any]:
    from utils.tool_plugin_store import get_tool_plugin_store

    get_tool_plugin_store().disable_plugin(plugin_id)
    return _refresh_plugin_runtime_state()


def uninstall_tool_plugin(plugin_id: str) -> Dict[str, Any]:
    from core.tool_registry import list_registered_plugin_manifests
    from utils.tool_plugin_store import get_tool_plugin_store

    source = ""
    for manifest in list_registered_plugin_manifests():
        if manifest.plugin_id == str(plugin_id or "").strip():
            source = manifest.source
            break

    get_tool_plugin_store().uninstall_plugin(plugin_id, source=source)
    return _refresh_plugin_runtime_state()


@tool_adapter("web_worker")
class WebWorkerAdapter(BaseToolAdapter):
    async def execute(
        self,
        task: Dict[str, Any],
        shared_memory_snapshot: Dict[str, Any],
        registered_tool: RegisteredTool,
    ) -> Dict[str, Any]:
        resolved_model = resolve_model_for_task(task)
        if resolved_model:
            task.setdefault("params", {})["_resolved_model"] = resolved_model

        pool = await WorkerPool.get_instance()
        result = await pool.web_worker.execute_async(task, shared_memory_snapshot)

        # 🔥 键盘侠教练评估
        from utils.tool_evaluation_hook import evaluate_tool_result
        step_no = len(task.get("execution_trace", [])) + 1
        evaluate_tool_result("web_worker", task, result, step_no)

        clean_params = copy.deepcopy(task.get("params", {}))
        clean_params.pop("_resolved_model", None)

        if isinstance(result, dict) and result.get("_switch_worker"):
            target_identifier = str(result.get("_switch_worker", "") or "").strip()
            patch = result.get("_switch_params", {})
            next_params = copy.deepcopy(clean_params)
            if isinstance(patch, dict):
                next_params.update(patch)

            registry = get_builtin_tool_registry()
            target_tool = registry.get(target_identifier) or registry.get_by_task_type(target_identifier)

            return {
                "status": str(TaskStatus.PENDING),
                "task_type": target_tool.spec.task_type if target_tool else target_identifier,
                "tool_name": target_tool.spec.name if target_tool else "",
                "params": next_params,
                "tool_args": dict(next_params),
                "result": None,
                "execution_trace": task.get("execution_trace", []),
                "failure_type": None,
                "shared_memory": None,
                "error_trace": "",
                "risk_level": target_tool.spec.risk_level if target_tool else task.get("risk_level", "medium"),
            }

        shared_memory_value = None
        if isinstance(result, dict) and result.get("success") and result.get("data"):
            shared_memory_value = result.get("data")
        status = str(TaskStatus.COMPLETED) if result.get("success") else str(TaskStatus.FAILED)
        return _base_outcome(
            task,
            registered_tool,
            status=status,
            result=result,
            shared_memory=shared_memory_value,
            error_trace="" if result.get("success") else result.get("error", "Unknown error"),
            failure_type=task.get("failure_type"),
            params=clean_params,
        )


class ExecutorBackedAdapter(BaseToolAdapter):
    """Base adapter for sync workers executed in a thread executor."""

    worker_attr = ""

    async def execute(
        self,
        task: Dict[str, Any],
        shared_memory_snapshot: Dict[str, Any],
        registered_tool: RegisteredTool,
    ) -> Dict[str, Any]:
        pool = await WorkerPool.get_instance()
        worker = getattr(pool, self.worker_attr)
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            worker.execute,
            task,
            shared_memory_snapshot,
        )

        # 🔥 键盘侠教练评估
        from utils.tool_evaluation_hook import evaluate_tool_result
        step_no = len(task.get("execution_trace", [])) + 1
        evaluate_tool_result(self.worker_attr, task, result, step_no)

        status = str(TaskStatus.COMPLETED) if result.get("success") else str(TaskStatus.FAILED)
        return _base_outcome(
            task,
            registered_tool,
            status=status,
            result=result,
            shared_memory=result,
            error_trace="" if result.get("success") else result.get("error", "Unknown error"),
            failure_type=task.get("failure_type"),
        )


@tool_adapter("file_worker")
class FileWorkerAdapter(ExecutorBackedAdapter):
    worker_attr = "file_worker"


@tool_adapter("api_worker")
class ApiWorkerAdapter(BaseToolAdapter):
    async def execute(
        self,
        task: Dict[str, Any],
        shared_memory_snapshot: Dict[str, Any],
        registered_tool: RegisteredTool,
    ) -> Dict[str, Any]:
        params = copy.deepcopy(task.get("params", {}) or {})
        method = str(params.get("method", "GET") or "GET").strip().upper()
        url = str(params.get("url", "") or "").strip()
        timeout_seconds = max(int(params.get("timeout_seconds", 20) or 20), 1)
        if not url:
            error_message = "API call requires a target url"
            return _base_outcome(
                task,
                registered_tool,
                status=str(TaskStatus.FAILED),
                result={"success": False, "error": error_message},
                shared_memory=None,
                error_trace=error_message,
                failure_type=str(FailureType.INVALID_INPUT),
                params=params,
            )

        approved_actions = {
            str(item).strip()
            for item in (shared_memory_snapshot.get("_approved_actions", []) or [])
            if str(item).strip()
        }
        is_mutating = method not in {"GET", "HEAD", "OPTIONS"}
        if is_mutating and str(task.get("task_id", "") or "") not in approved_actions:
            approval_result = {
                "success": False,
                "approval_required": True,
                "approval_key": str(task.get("task_id", "") or ""),
                "method": method,
                "url": url,
                "headers": params.get("headers", {}) or {},
                "body_preview": str(
                    params.get("json_body")
                    if params.get("json_body") is not None
                    else params.get("body", "")
                )[:300],
                "message": "Mutating API call prepared and waiting for approval.",
            }
            return _base_outcome(
                task,
                registered_tool,
                status="waiting_for_approval",
                result=approval_result,
                shared_memory=approval_result,
                error_trace="",
                failure_type=None,
                params=params,
            )

        headers = params.get("headers", {}) or {}
        if not isinstance(headers, dict):
            headers = {}
        payload = None
        if params.get("json_body") is not None:
            payload = json.dumps(params.get("json_body"), ensure_ascii=False).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        elif params.get("body") is not None:
            body = params.get("body")
            payload = str(body).encode("utf-8")

        request = urllib.request.Request(
            url=url,
            data=payload,
            method=method,
            headers={str(k): str(v) for k, v in headers.items()},
        )

        def _call_api() -> Dict[str, Any]:
            try:
                with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                    raw_bytes = response.read()
                    raw_text = raw_bytes.decode("utf-8", errors="replace")
                    content_type = str(response.headers.get("Content-Type", "") or "").lower()
                    parsed: Any = raw_text
                    if "application/json" in content_type:
                        try:
                            parsed = json.loads(raw_text)
                        except json.JSONDecodeError:
                            parsed = raw_text
                    return {
                        "success": True,
                        "status_code": getattr(response, "status", 200),
                        "headers": dict(response.headers.items()),
                        "data": parsed,
                        "content": raw_text[:20000],
                        "url": url,
                        "method": method,
                    }
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else ""
                return {
                    "success": False,
                    "error": f"HTTP {exc.code}: {exc.reason}",
                    "status_code": int(exc.code),
                    "content": body[:20000],
                    "url": url,
                    "method": method,
                }
            except Exception as exc:
                return {
                    "success": False,
                    "error": str(exc),
                    "url": url,
                    "method": method,
                }

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _call_api)
        status = str(TaskStatus.COMPLETED) if result.get("success") else str(TaskStatus.FAILED)
        return _base_outcome(
            task,
            registered_tool,
            status=status,
            result=result,
            shared_memory=result if result.get("success") else None,
            error_trace="" if result.get("success") else str(result.get("error", "Unknown error")),
            failure_type=None if result.get("success") else classify_failure(str(result.get("error", ""))),
            params=params,
        )


@tool_adapter("system_worker")
class SystemWorkerAdapter(ExecutorBackedAdapter):
    worker_attr = "system_worker"


@tool_adapter("browser_agent")
class BrowserAgentAdapter(BaseToolAdapter):
    async def execute(
        self,
        task: Dict[str, Any],
        shared_memory_snapshot: Dict[str, Any],
        registered_tool: RegisteredTool,
    ) -> Dict[str, Any]:
        from utils.browser_toolkit import BrowserToolkit

        del shared_memory_snapshot

        params = task.get("params", {})
        task_desc = params.get("task", task.get("description", ""))
        start_url = params.get("start_url", "")
        headless = params.get("headless", settings.BROWSER_FAST_MODE)
        max_steps = params.get("max_steps", 8)
        if not isinstance(max_steps, int) or max_steps <= 0:
            max_steps = 8
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
                    log_warning(f"BrowserAgent model init failed: {exc}; falling back to default model")

            agent = pool.create_browser_agent(
                llm_client=task_llm,
                headless=headless,
                toolkit=toolkit,
            )

            try:
                result = await agent.run(task_desc, start_url, max_steps=max_steps)
                break
            except Exception as exc:
                last_error = exc
                if attempt < BROWSER_RETRIES - 1 and is_retryable(exc):
                    log_warning(
                        f"BrowserAgent transient failure, retrying attempt {attempt + 2}: {str(exc)[:80]}"
                    )
                    continue
                log_error(f"BrowserAgent execution failed: {exc}")
                break
            finally:
                try:
                    await agent.close()
                except Exception as exc:
                    log_warning(f"BrowserAgent cleanup failed after browser task: {exc}")
                try:
                    toolkit_close_result = await toolkit.close()
                except Exception as exc:
                    log_warning(f"BrowserToolkit cleanup raised after browser task: {exc}")
                else:
                    if not toolkit_close_result.success:
                        log_warning(
                            f"BrowserToolkit cleanup failed after browser task: {toolkit_close_result.error}"
                        )

        if result is not None:
            # 🔥 诊断提前退出问题
            from utils.browser_diagnostics import diagnose_early_exit
            steps = result.get("steps", [])
            if len(steps) == 0 and not result.get("success"):
                diagnosis = diagnose_early_exit(result, task.get("description", ""))
                from utils.logger import console
                console.print(f"\n[red]{diagnosis}[/red]\n")

            # 🔥 键盘侠教练评估
            from utils.tool_evaluation_hook import evaluate_tool_result
            step_no = len(result.get("steps", [])) + 1
            evaluate_tool_result("browser_agent", task, result, step_no)

            trace = []
            for step_no, step in enumerate(result.get("steps", []), 1):
                trace.append(
                    make_trace_step(
                        step_no=step_no,
                        plan=step.get("plan", step.get("action_type", "")),
                        action=step.get("action", step.get("selector", "")),
                        observation=step.get("observation", step.get("result", "")),
                        decision=step.get("decision", "continue"),
                    )
                )
            task["execution_trace"] = trace
            task["result"] = result
            if not result.get("success"):
                task["failure_type"] = classify_failure(
                    result.get("message", result.get("error", ""))
                )

            return _base_outcome(
                task,
                registered_tool,
                status=str(TaskStatus.COMPLETED) if result.get("success") else str(TaskStatus.FAILED),
                result=result,
                shared_memory=result,
                error_trace="" if result.get("success") else result.get("message", "Browser task failed"),
                failure_type=task.get("failure_type"),
            )

        error_message = str(last_error) if last_error else "Unknown browser execution failure"
        task["failure_type"] = classify_failure(error_message)
        task["execution_trace"] = [
            make_trace_step(1, "run browser_agent", task_desc[:80], error_message, "exception"),
        ]
        return _base_outcome(
            task,
            registered_tool,
            status=str(TaskStatus.FAILED),
            result={"success": False, "error": error_message},
            shared_memory={"success": False, "error": error_message},
            error_trace=error_message,
            failure_type=task.get("failure_type"),
        )


class ToolAdapterRegistry:
    """Runtime registry for adapter implementations."""

    def __init__(self):
        self._adapters: Dict[str, BaseToolAdapter] = {}

    def register(self, adapter_name: str, adapter: BaseToolAdapter) -> None:
        self._adapters[adapter_name] = adapter

    def register_class(self, adapter_name: str, adapter_cls: type[BaseToolAdapter]) -> None:
        register_tool_adapter_class(adapter_name, adapter_cls)

    def get(self, adapter_name: str) -> Optional[BaseToolAdapter]:
        load_tool_adapter_plugins()
        adapter = self._adapters.get(adapter_name)
        if adapter is not None:
            return adapter

        adapter_cls = _registered_adapter_types.get(str(adapter_name))
        if adapter_cls is None:
            return None

        instance = adapter_cls()
        self._adapters[str(adapter_name)] = instance
        return instance


def build_tool_adapter_registry() -> ToolAdapterRegistry:
    load_tool_adapter_plugins()
    return ToolAdapterRegistry()


_tool_adapter_registry: Optional[ToolAdapterRegistry] = None


def get_tool_adapter_registry() -> ToolAdapterRegistry:
    global _tool_adapter_registry
    if _tool_adapter_registry is None:
        _tool_adapter_registry = build_tool_adapter_registry()
    return _tool_adapter_registry


async def execute_tool_via_adapter(
    task: Dict[str, Any],
    shared_memory_snapshot: Dict[str, Any],
    registered_tool: RegisteredTool,
) -> Dict[str, Any]:
    adapter = get_tool_adapter_registry().get(registered_tool.adapter_name)
    if adapter is None:
        return _base_outcome(
            task,
            registered_tool,
            status=str(TaskStatus.FAILED),
            result={
                "success": False,
                "error": f"Unknown tool adapter: {registered_tool.adapter_name}",
            },
            shared_memory=None,
            error_trace=f"Unknown tool adapter: {registered_tool.adapter_name}",
            failure_type=str(FailureType.INVALID_INPUT),
        )
    return await adapter.execute(task, shared_memory_snapshot, registered_tool)
