"""
OmniCore shared runtime entrypoint.
Unifies task execution, built-in commands, and memory integration for CLI/UI.
"""
from datetime import datetime
import os
import signal
import subprocess
import sys
import time
import traceback
import threading
from typing import Optional, List, Dict, Any, TYPE_CHECKING

from config.settings import settings
from core.statuses import (
    BLOCKED,
    WAITING_FOR_APPROVAL,
    WAITING_FOR_EVENT,
    WAITING_JOB_STATUSES,
    is_recoverable_job_status,
    is_success_job_status,
)
from core.state import create_initial_state
from core.graph import get_graph  # noqa: F401 – also exposes build_graph_from_registry via graph module
from memory.manager import MemoryManager, build_memory_scope
from utils.logger import console, log_agent_action, log_debug_metrics, log_error, log_warning
from utils.text import sanitize_text, sanitize_value
from utils.structured_logger import get_structured_logger, LogContext

if TYPE_CHECKING:
    from memory.scoped_chroma_store import ChromaMemory


_queue_worker_lock = threading.Lock()
_queue_worker_thread: Optional[threading.Thread] = None
_queue_worker_process: Optional[subprocess.Popen] = None
_queue_worker_stop = threading.Event()
_runtime_memory_instance: Optional["ChromaMemory"] = None
_runtime_memory_init_failed = False


def _build_special_result(
    success: bool,
    output: str = "",
    error: str = "",
    status: str = "completed",
) -> Dict[str, Any]:
    return {
        "success": success,
        "output": output,
        "error": error,
        "status": status,
        "critic_feedback": "",
        "tasks_completed": 0,
        "tasks": [],
        "policy_decisions": [],
        "delivery_package": {},
        "intent": "system_command",
        "is_special_command": True,
        "runtime_metrics": _collect_runtime_metrics(),
    }


def _collect_runtime_metrics() -> Dict[str, Any]:
    metrics: Dict[str, Any] = {}

    try:
        from core.llm_cache import get_llm_cache

        metrics["llm_cache"] = sanitize_value(get_llm_cache().snapshot_stats())
    except Exception as e:
        metrics["llm_cache_error"] = sanitize_text(str(e))

    try:
        from utils.browser_runtime_pool import snapshot_browser_runtime_metrics

        metrics["browser_pool"] = sanitize_value(snapshot_browser_runtime_metrics())
    except Exception as e:
        metrics["browser_pool_error"] = sanitize_text(str(e))

    if "llm_cache" in metrics:
        log_debug_metrics("runtime.llm_cache", metrics["llm_cache"])
    if "browser_pool" in metrics:
        log_debug_metrics("runtime.browser_pool", metrics["browser_pool"])

    return metrics


def _resolve_runtime_memory(
    memory: Optional["ChromaMemory"] = None,
) -> Optional["ChromaMemory"]:
    global _runtime_memory_instance, _runtime_memory_init_failed

    if memory is not None:
        return memory
    if _runtime_memory_instance is not None:
        return _runtime_memory_instance
    if _runtime_memory_init_failed:
        return None

    try:
        from memory.scoped_chroma_store import ChromaMemory

        _runtime_memory_instance = ChromaMemory()
        return _runtime_memory_instance
    except Exception as e:
        _runtime_memory_init_failed = True
        log_warning(f"Memory initialization skipped: {e}")
        return None


def _is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _resolve_worker_mode(mode: Optional[str] = None) -> str:
    resolved = str(mode or settings.QUEUE_WORKER_MODE or "process").strip().lower()
    if resolved not in {"thread", "process"}:
        return "process"
    return resolved


def _release_due_schedules() -> List[Dict[str, Any]]:
    try:
        from utils.runtime_state_store import get_runtime_state_store

        released = sanitize_value(
            get_runtime_state_store().release_due_schedules(limit=settings.SCHEDULER_RELEASE_LIMIT)
        )
        if released:
            log_agent_action("Scheduler", "Released due schedules", f"{len(released)} job(s) queued")
        return released
    except Exception as e:
        log_warning(f"Schedule release failed: {e}")
        return []


def _release_directory_watch_events() -> List[Dict[str, Any]]:
    try:
        from utils.workflow_automation_store import get_workflow_automation_store

        events = sanitize_value(
            get_workflow_automation_store().poll_directory_watch_events(limit=5)
        )
        for event in events:
            if not isinstance(event, dict):
                continue
            template_id = sanitize_text(event.get("template_id") or "")
            if template_id:
                submission = submit_template(
                    template_id,
                    session_id=sanitize_text(event.get("session_id") or "") or None,
                    goal_id=sanitize_text(event.get("goal_id") or "") or None,
                    project_id=sanitize_text(event.get("project_id") or "") or None,
                    todo_id=sanitize_text(event.get("todo_id") or "") or None,
                    file_event_path=sanitize_text(event.get("file_path") or ""),
                    auto_start_worker=False,
                    trigger_source="file_event",
                )
                if submission:
                    continue
                session_id = sanitize_text(event.get("session_id") or "")
                log_warning(
                    f"Directory watch template {template_id} is unavailable; "
                    "falling back to direct file-event prompt."
                )
                _create_automation_notification(
                    session_id=session_id,
                    title="Directory watch template missing",
                    message=(
                        f"Template {template_id} could not be loaded. "
                        "The file event was queued with a direct fallback prompt instead."
                    ),
                )

            base_input = sanitize_text(event.get("user_input") or "")
            file_path = sanitize_text(event.get("file_path") or "")
            event_prompt = base_input or "Process the new file event."
            if file_path:
                event_prompt = f"{event_prompt}\n\nNew file detected: {file_path}"
            submit_task(
                event_prompt,
                session_id=sanitize_text(event.get("session_id") or "") or None,
                goal_id=sanitize_text(event.get("goal_id") or "") or None,
                project_id=sanitize_text(event.get("project_id") or "") or None,
                todo_id=sanitize_text(event.get("todo_id") or "") or None,
                auto_start_worker=False,
                trigger_source="file_event",
            )
        if events:
            log_agent_action("DirectoryWatch", "Released file events", f"{len(events)} job(s) queued")
        return events
    except Exception as e:
        log_warning(f"Directory watch polling failed: {e}")
        return []


def _load_effective_user_preferences(session_id: str) -> Dict[str, Any]:
    try:
        from utils.runtime_state_store import get_runtime_state_store

        return sanitize_value(get_runtime_state_store().get_preferences(session_id or None))
    except Exception as e:
        log_warning(f"Failed to load user preferences: {e}")
        return {
            "default_output_directory": settings.DEFAULT_OUTPUT_DIRECTORY,
            "user_location": settings.DEFAULT_USER_LOCATION,
            "preferred_tools": list(settings.DEFAULT_PREFERRED_TOOLS),
            "preferred_sites": list(settings.DEFAULT_PREFERRED_SITES),
            "auto_queue_confirmations": bool(settings.DEFAULT_AUTO_QUEUE_CONFIRMATIONS),
            "task_templates": {},
        }


def _build_current_os_context() -> Dict[str, str]:
    """构建当前操作系统上下文，供 Router LLM 使用正确命令。"""
    import platform
    import shutil
    import os as _os

    system = platform.system()      # Darwin / Linux / Windows
    release = platform.release()
    machine = platform.machine()    # arm64 / x86_64 / AMD64

    # 包管理器检测（只列出实际可用的）
    candidates = {
        "brew":    "macOS Homebrew",
        "apt":     "Debian/Ubuntu apt",
        "apt-get": "Debian/Ubuntu apt-get",
        "yum":     "RHEL/CentOS yum",
        "dnf":     "Fedora/RHEL dnf",
        "pacman":  "Arch Linux pacman",
        "pip":     "Python pip",
        "pip3":    "Python pip3",
        "npm":     "Node.js npm",
        "cargo":   "Rust cargo",
        "go":      "Go toolchain",
    }
    available_pkgs = [f"{cmd} ({label})" for cmd, label in candidates.items() if shutil.which(cmd)]

    # Shell 检测
    shell_path = _os.environ.get("SHELL", "")
    shell_name = _os.path.basename(shell_path) if shell_path else ("cmd" if system == "Windows" else "sh")

    # OS 友好名称 + 包管理器建议
    if system == "Darwin":
        mac_ver = platform.mac_ver()[0]
        os_display = f"macOS {mac_ver} ({machine})"
        pkg_hint = "Use 'brew' for system packages, 'pip'/'pip3' for Python, 'npm' for Node."
    elif system == "Linux":
        try:
            import distro
            os_display = f"{distro.name(pretty=True)} ({machine})"
        except ImportError:
            os_display = f"Linux {release} ({machine})"
        pkg_hint = "Use 'apt'/'apt-get' on Debian/Ubuntu, 'yum'/'dnf' on RHEL/Fedora, 'pacman' on Arch."
    elif system == "Windows":
        os_display = f"Windows {release} ({machine})"
        pkg_hint = "Use 'winget' or 'choco' for packages. Prefer PowerShell over cmd."
    else:
        os_display = f"{system} {release} ({machine})"
        pkg_hint = ""

    return {
        "system": system,
        "os_display": os_display,
        "machine": machine,
        "shell": shell_name,
        "shell_path": shell_path,
        "available_package_managers": ", ".join(available_pkgs) if available_pkgs else "unknown",
        "package_manager_hint": pkg_hint,
    }


def _build_current_time_context() -> Dict[str, str]:
    now = datetime.now().astimezone()
    timezone_name = now.tzname() or str(now.tzinfo or "")
    return {
        "iso_datetime": now.isoformat(timespec="seconds"),
        "local_date": now.strftime("%Y-%m-%d"),
        "local_time": now.strftime("%H:%M:%S"),
        "weekday": now.strftime("%A"),
        "timezone": timezone_name,
    }


def _build_current_location_context(
    user_preferences: Optional[Dict[str, Any]] = None,
    current_time_context: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    location = ""
    source = ""
    if isinstance(user_preferences, dict):
        location = sanitize_text(user_preferences.get("user_location") or "")
        if location:
            source = "user_preference"

    if not location:
        location = sanitize_text(settings.DEFAULT_USER_LOCATION)
        if location:
            source = "default_setting"

    if not location:
        return {}

    context = {
        "location": location,
        "source": source or "configured",
    }
    timezone_name = ""
    if isinstance(current_time_context, dict):
        timezone_name = sanitize_text(current_time_context.get("timezone") or "")
    if timezone_name:
        context["timezone"] = timezone_name
    return context


def _load_job_work_scope(job_id: str) -> Dict[str, str]:
    if not job_id:
        return {"goal_id": "", "project_id": "", "todo_id": ""}
    try:
        from utils.runtime_state_store import get_runtime_state_store

        job_record = get_runtime_state_store().get_job(job_id)
    except Exception:
        job_record = {}
    return {
        "goal_id": sanitize_text(job_record.get("goal_id") or ""),
        "project_id": sanitize_text(job_record.get("project_id") or ""),
        "todo_id": sanitize_text(job_record.get("todo_id") or ""),
    }


def _load_work_runtime_context(
    *,
    session_id: str,
    job_id: str,
    user_input: str,
) -> Dict[str, Any]:
    scope = _load_job_work_scope(job_id)
    context: Dict[str, Any] = {
        "scope": scope,
        "work_context": {},
        "resource_memory": [],
        "successful_paths": [],
        "failure_patterns": [],
    }

    try:
        from utils.work_context_store import get_work_context_store

        work_store = get_work_context_store()
        context["work_context"] = sanitize_value(
            work_store.get_context_snapshot(
                session_id=session_id,
                goal_id=scope["goal_id"],
                project_id=scope["project_id"],
                todo_id=scope["todo_id"],
            )
        )
        context["successful_paths"] = sanitize_value(
            work_store.suggest_success_paths(
                query=user_input,
                session_id=session_id,
                goal_id=scope["goal_id"],
                limit=3,
            )
        )
        context["failure_patterns"] = sanitize_value(
            work_store.suggest_failure_avoidance(
                query=user_input,
                session_id=session_id,
                goal_id=scope["goal_id"],
                limit=3,
            )
        )
    except Exception as e:
        log_warning(f"Failed to load work context: {e}")

    try:
        from utils.artifact_store import get_artifact_store

        context["resource_memory"] = sanitize_value(
            get_artifact_store().search_artifacts(
                session_id=session_id or None,
                goal_id=scope["goal_id"] or None,
                project_id=scope["project_id"] or None,
                todo_id=scope["todo_id"] or None,
                query=user_input,
                limit=10,
            )
        )
    except Exception as e:
        log_warning(f"Failed to load artifact context: {e}")

    return context


def _create_job_notification(finalized: Dict[str, Any]) -> None:
    session_id = sanitize_text(finalized.get("session_id") or "")
    if not session_id:
        return

    status = sanitize_text(finalized.get("status") or "")
    job_id = sanitize_text(finalized.get("job_id") or "")
    output = sanitize_text(finalized.get("output") or "")
    error = sanitize_text(finalized.get("error") or "")
    delivery = finalized.get("delivery_package") or {}
    headline = sanitize_text(delivery.get("headline") or "")
    issues = delivery.get("issues") or []
    pending_policy = [
        item for item in (finalized.get("policy_decisions") or [])
        if isinstance(item, dict) and str(item.get("decision", "") or "").strip().lower() == "pending"
    ]

    title = "Task completed"
    message = headline or _short_preview(output or error)
    level = "info"
    requires_action = False
    category = "job_result"

    if status == WAITING_FOR_APPROVAL:
        title = "Task waiting for approval"
        level = "warning"
        category = "approval"
        requires_action = True
        message = headline or "A prepared action is waiting for your approval."
    elif status == WAITING_FOR_EVENT:
        title = "Task waiting for event"
        level = "info"
        category = "automation"
        message = headline or "The task is waiting for an external event."
    elif status == BLOCKED:
        title = "Task blocked"
        level = "warning"
        category = "job_result"
        requires_action = True
        message = headline or "The task is blocked and needs manual attention."
    elif status in {"completed"} and not issues:
        level = "success"
    elif status in {"completed_with_issues"} or issues:
        title = "Task completed with issues"
        level = "warning"
        message = headline or _short_preview(error or output)
    elif status in {"error", "cancelled"} or not bool(finalized.get("success", False)):
        title = "Task failed"
        level = "error"
        message = _short_preview(error or output or status)
    if pending_policy:
        title = "Task requires review"
        level = "warning"
        requires_action = True
        category = "approval"
        message = _short_preview(
            "; ".join(
                f"{item.get('tool_name', '')}:{item.get('target_resource', '')}"
                for item in pending_policy[:3]
            )
            or "Pending approval items"
        )

    try:
        from utils.runtime_state_store import get_runtime_state_store

        get_runtime_state_store().create_notification(
            session_id=session_id,
            job_id=job_id,
            title=title,
            message=message or "No details available.",
            level=level,
            category=category,
            requires_action=requires_action,
        )
    except Exception as e:
        log_warning(f"Notification persistence failed: {e}")


def _create_automation_notification(
    *,
    session_id: str,
    title: str,
    message: str,
    level: str = "warning",
) -> None:
    if not session_id:
        return

    try:
        from utils.runtime_state_store import get_runtime_state_store

        get_runtime_state_store().create_notification(
            session_id=session_id,
            title=title,
            message=message,
            level=level,
            category="automation",
            requires_action=False,
        )
    except Exception as e:
        log_warning(f"Automation notification persistence failed: {e}")


def _short_preview(text: str, limit: int = 220) -> str:
    return sanitize_text(text or "")[:limit]


def _finalize_runtime_result(result: Dict[str, Any], user_input: str) -> Dict[str, Any]:
    finalized = dict(result)
    _sl = get_structured_logger()
    job_id = finalized.get("job_id", "")
    success = bool(finalized.get("success", False))
    with LogContext(job_id=job_id):
        _sl.log_event("job_end", detail=f"success={success}")
    runtime_metrics = sanitize_value(finalized.get("runtime_metrics") or _collect_runtime_metrics())
    finalized["runtime_metrics"] = runtime_metrics

    try:
        from utils.runtime_metrics_store import get_runtime_metrics_store

        record = get_runtime_metrics_store().append_record(
            user_input=user_input,
            success=bool(finalized.get("success", False)),
            status=sanitize_text(finalized.get("status") or ""),
            runtime_metrics=runtime_metrics,
            is_special_command=bool(finalized.get("is_special_command", False)),
        )
        finalized["runtime_delta"] = sanitize_value(record.get("runtime_delta", {}))
    except Exception as e:
        finalized.setdefault("runtime_delta", {})
        finalized["runtime_metrics_store_error"] = sanitize_text(str(e))
        log_warning(f"Runtime metrics persistence failed: {e}")

    tasks = sanitize_value(finalized.get("tasks") or [])
    policy_decisions = sanitize_value(finalized.get("policy_decisions") or [])
    delivery_package = sanitize_value(finalized.get("delivery_package") or {})
    session_id = sanitize_text(finalized.get("session_id") or "")
    job_id = sanitize_text(finalized.get("job_id") or "")
    finalized["policy_decisions"] = policy_decisions
    finalized["delivery_package"] = delivery_package

    # S3: emit job completion event + flush
    try:
        from core.event_log import emit_event, flush_events, EventType
        emit_event(
            EventType.JOB_STATUS_CHANGED,
            session_id=session_id,
            job_id=job_id,
            data={
                "new_status": sanitize_text(finalized.get("status") or ""),
                "intent": sanitize_text(finalized.get("intent") or ""),
                "error": sanitize_text(finalized.get("error") or "")[:500],
                "output": sanitize_text(finalized.get("output") or "")[:500],
            },
        )
        flush_events()
    except Exception:
        pass

    try:
        from utils.runtime_state_store import get_runtime_state_store

        if session_id and job_id:
            state_store = get_runtime_state_store()
            artifacts = sanitize_value(
                state_store.register_task_artifacts(
                    session_id=session_id,
                    job_id=job_id,
                    tasks=tasks,
                )
            )
            completion = state_store.complete_job(
                session_id=session_id,
                job_id=job_id,
                status=sanitize_text(finalized.get("status") or ""),
                success=bool(finalized.get("success", False)),
                output=sanitize_text(finalized.get("output") or ""),
                error=sanitize_text(finalized.get("error") or ""),
                intent=sanitize_text(finalized.get("intent") or ""),
                tasks=tasks,
                policy_decisions=policy_decisions,
                artifacts=artifacts,
                is_special_command=bool(finalized.get("is_special_command", False)),
            )
            finalized["artifacts"] = artifacts
            finalized["job_record"] = sanitize_value(completion.get("job_record", {}))
            finalized["session_record"] = sanitize_value(completion.get("session_record", {}))
            latest_checkpoint = sanitize_value(state_store.get_latest_checkpoint(job_id))
            if latest_checkpoint:
                finalized["checkpoint_summary"] = {
                    "checkpoint_id": sanitize_text(latest_checkpoint.get("checkpoint_id") or ""),
                    "stage": sanitize_text(latest_checkpoint.get("stage") or ""),
                    "created_at": sanitize_text(latest_checkpoint.get("created_at") or ""),
                }
        else:
            finalized.setdefault("artifacts", [])
    except Exception as e:
        finalized.setdefault("artifacts", [])
        finalized["runtime_state_store_error"] = sanitize_text(str(e))
        log_warning(f"Runtime state persistence failed: {e}")

    job_record = finalized.get("job_record") or {}
    goal_id = sanitize_text(job_record.get("goal_id") or "")
    project_id = sanitize_text(job_record.get("project_id") or "")
    todo_id = sanitize_text(job_record.get("todo_id") or "")
    memory_scope = build_memory_scope(
        session_id=session_id,
        goal_id=goal_id,
        project_id=project_id,
        todo_id=todo_id,
    )

    lifecycle_status = sanitize_text(finalized.get("status") or "")
    if lifecycle_status not in WAITING_JOB_STATUSES:
        try:
            from utils.artifact_store import get_artifact_store

            cataloged = get_artifact_store().record_artifacts(
                session_id=session_id,
                job_id=job_id,
                artifacts=sanitize_value(finalized.get("artifacts") or []),
                goal_id=goal_id,
                project_id=project_id,
                todo_id=todo_id,
            )
            finalized["artifact_catalog_entries"] = sanitize_value(cataloged)
        except Exception as e:
            finalized["artifact_store_error"] = sanitize_text(str(e))
            log_warning(f"Artifact catalog persistence failed: {e}")

        try:
            from utils.work_context_store import get_work_context_store

            tool_sequence = [
                str(task.get("tool_name", "") or task.get("task_type", "") or "").strip()
                for task in tasks
                if isinstance(task, dict) and str(task.get("status", "") or "") == "completed"
            ]
            work_store = get_work_context_store()
            work_store.record_experience(
                session_id=session_id,
                job_id=job_id,
                user_input=user_input,
                intent=sanitize_text(finalized.get("intent") or ""),
                tool_sequence=tool_sequence,
                success=bool(finalized.get("success", False)),
                goal_id=goal_id,
                project_id=project_id,
                todo_id=todo_id,
                summary=sanitize_text(finalized.get("output") or finalized.get("error") or ""),
                task_details=tasks,
                visited_urls=[
                    sanitize_text(
                        (
                            (task.get("result") or {}).get("url")
                            if isinstance(task.get("result"), dict)
                            else ""
                        )
                        or (
                            (task.get("result") or {}).get("current_url")
                            if isinstance(task.get("result"), dict)
                            else ""
                        )
                        or (
                            (task.get("params") or {}).get("url")
                            if isinstance(task.get("params"), dict)
                            else ""
                        )
                        or (
                            (task.get("params") or {}).get("start_url")
                            if isinstance(task.get("params"), dict)
                            else ""
                        )
                        or ""
                    )
                    for task in tasks
                    if isinstance(task, dict)
                ],
                artifact_refs=sanitize_value(finalized.get("artifacts") or []),
                failure_reason=sanitize_text(finalized.get("error") or ""),
            )
            if goal_id or project_id or todo_id:
                work_store.record_job_link(
                    job_id=job_id,
                    goal_id=goal_id,
                    project_id=project_id,
                    todo_id=todo_id,
                    success=bool(finalized.get("success", False)),
                )
        except Exception as e:
            finalized["work_context_store_error"] = sanitize_text(str(e))
            log_warning(f"Work context persistence failed: {e}")

        try:
            memory_store = _resolve_runtime_memory()
            memory_manager = MemoryManager(memory_store)
            finalized["memory_persistence"] = sanitize_value(
                memory_manager.persist_job_outcome(
                    user_input=user_input,
                    success=bool(finalized.get("success", False)),
                    final_output=sanitize_text(finalized.get("output") or ""),
                    final_error=sanitize_text(finalized.get("error") or ""),
                    intent=sanitize_text(finalized.get("intent") or ""),
                    scope=memory_scope,
                    tasks=tasks,
                    artifacts=sanitize_value(finalized.get("artifacts") or []),
                    is_special_command=bool(finalized.get("is_special_command", False)),
                )
            )
        except Exception as e:
            finalized["memory_store_error"] = sanitize_text(str(e))
            log_warning(f"Memory persistence failed: {e}")

    _create_job_notification(finalized)

    # Persistence Coordinator: async unified write (non-blocking best-effort)
    if lifecycle_status not in WAITING_JOB_STATUSES and session_id and job_id:
        try:
            from core.persistence_coordinator import PersistenceCoordinator
            coordinator = PersistenceCoordinator()
            import asyncio as _aio

            async def _coord_complete():
                return await coordinator.complete_job(
                    job_id=job_id,
                    result={
                        "session_id": session_id,
                        "user_input": user_input,
                        "status": lifecycle_status,
                        "success": bool(finalized.get("success", False)),
                        "output": sanitize_text(finalized.get("output") or ""),
                        "error": sanitize_text(finalized.get("error") or ""),
                        "intent": sanitize_text(finalized.get("intent") or ""),
                        "tasks": tasks,
                        "policy_decisions": policy_decisions,
                        "artifacts": sanitize_value(finalized.get("artifacts") or []),
                        "is_special_command": bool(finalized.get("is_special_command", False)),
                    },
                    scope={
                        "session_id": session_id,
                        "goal_id": goal_id,
                        "project_id": project_id,
                        "todo_id": todo_id,
                    },
                )

            try:
                loop = _aio.get_running_loop()
                # Already in async context — schedule as task
                loop.create_task(_coord_complete())
            except RuntimeError:
                # No running loop — run synchronously in a new loop
                _aio.run(_coord_complete())
        except Exception as e:
            log_warning(f"PersistenceCoordinator failed (non-critical): {e}")

    # 可选语音输出（VOICE_OUTPUT_ENABLED=true 时）
    if settings.VOICE_OUTPUT_ENABLED and finalized.get("success"):
        output_text = sanitize_text(finalized.get("output") or "")
        if output_text and len(output_text) < 500:
            try:
                from core.llm import LLMClient

                audio_path = os.path.join(
                    str(settings.DATA_DIR),
                    "speech",
                    f"tts_{job_id or 'tmp'}.mp3",
                )
                os.makedirs(os.path.dirname(audio_path), exist_ok=True)
                llm = LLMClient()
                llm.speak(
                    output_text,
                    output_path=audio_path,
                    voice=settings.VOICE_OUTPUT_VOICE,
                    model=settings.VOICE_OUTPUT_MODEL,
                )
                finalized["audio_output"] = audio_path
                log_agent_action("TTS", f"语音输出已生成: {audio_path}")
            except Exception as e:
                log_warning(f"TTS failed (non-blocking): {e}")

    return finalized


def _persist_runtime_checkpoint(
    *,
    session_id: str,
    job_id: str,
    stage: str,
    state: Dict[str, Any],
    note: str = "",
) -> None:
    if not session_id or not job_id:
        return

    try:
        from utils.runtime_state_store import get_runtime_state_store

        get_runtime_state_store().save_checkpoint(
            session_id=session_id,
            job_id=job_id,
            stage=stage,
            state=state,
            note=note,
        )
    except Exception as e:
        log_warning(f"Runtime checkpoint persistence failed: {e}")


def _handle_special_command(
    user_input: str,
    memory: Optional["ChromaMemory"] = None,
) -> Optional[Dict[str, Any]]:
    command = (user_input or "").strip().lower()

    if command == "memory stats":
        if not memory:
            return _build_special_result(
                success=False,
                error="记忆系统未初始化",
                status="error",
            )
        stats = memory.get_stats()
        return _build_special_result(
            success=True,
            output=f"记忆统计: {stats}",
        )

    if command == "clear memory":
        if not memory:
            return _build_special_result(
                success=False,
                error="记忆系统未初始化",
                status="error",
            )
        cleared = memory.clear_all()
        if cleared:
            return _build_special_result(
                success=True,
                output="Memory cleared",
            )
        return _build_special_result(
            success=False,
            error="娓呯┖璁板繂澶辫触",
            status="error",
        )

    return None


def _handle_scoped_memory_command(
    user_input: str,
    memory: Optional["ChromaMemory"] = None,
    *,
    scope: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    command = (user_input or "").strip().lower()
    if command not in {"memory stats", "clear memory"}:
        return None

    if not memory:
        return _build_special_result(
            success=False,
            error="Memory system is not available",
            status="error",
        )

    if command == "memory stats":
        scoped_stats = memory.get_stats(scope=scope)
        total_stats = memory.get_stats()
        return _build_special_result(
            success=True,
            output=f"Scoped memory stats: {scoped_stats}; total memory stats: {total_stats}",
        )

    cleared = memory.clear_scope(scope)
    if cleared > 0:
        return _build_special_result(
            success=True,
            output=f"Cleared {cleared} scoped memories",
        )
    return _build_special_result(
        success=False,
        error="No scoped memories matched the current session",
        status="error",
    )


def submit_task(
    user_input: str,
    *,
    session_id: Optional[str] = None,
    auto_start_worker: bool = True,
    goal_id: Optional[str] = None,
    project_id: Optional[str] = None,
    todo_id: Optional[str] = None,
    trigger_source: str = "manual",
) -> Dict[str, Any]:
    clean_user_input = sanitize_text(user_input or "")
    runtime_session_id = sanitize_text(session_id or "")

    try:
        from utils.runtime_state_store import get_runtime_state_store

        state_store = get_runtime_state_store()
        session_record = state_store.get_or_create_session(session_id=runtime_session_id)
        runtime_session_id = sanitize_text(session_record.get("session_id") or runtime_session_id)
        job_record = state_store.submit_job(
            session_id=runtime_session_id,
            user_input=clean_user_input,
            is_special_command=(clean_user_input or "").strip().lower() in {"memory stats", "clear memory"},
            goal_id=sanitize_text(goal_id or ""),
            project_id=sanitize_text(project_id or ""),
            todo_id=sanitize_text(todo_id or ""),
            trigger_source=sanitize_text(trigger_source or "manual") or "manual",
        )
        submission = {
            "session_id": runtime_session_id,
            "job_id": sanitize_text(job_record.get("job_id") or ""),
            "status": sanitize_text(job_record.get("status") or "queued"),
            "user_input": clean_user_input,
            "is_special_command": bool(job_record.get("is_special_command", False)),
            "goal_id": sanitize_text(job_record.get("goal_id") or ""),
            "project_id": sanitize_text(job_record.get("project_id") or ""),
            "todo_id": sanitize_text(job_record.get("todo_id") or ""),
        }
        # S3: emit job_submitted event
        try:
            from core.event_log import emit_event, EventType
            emit_event(
                EventType.JOB_SUBMITTED,
                session_id=runtime_session_id,
                job_id=submission["job_id"],
                data={"user_input": clean_user_input[:500]},
            )
        except Exception:
            pass
        if auto_start_worker and submission["job_id"]:
            start_background_worker()
        return submission
    except Exception as e:
        log_warning(f"Runtime state initialization failed: {e}")
        return {
            "session_id": runtime_session_id,
            "job_id": "",
            "status": "error",
            "user_input": clean_user_input,
            "is_special_command": False,
        }


def create_scheduled_task(
    *,
    user_input: str,
    session_id: Optional[str] = None,
    schedule_type: str = "once",
    run_at: str = "",
    interval_seconds: int = 0,
    time_of_day: str = "",
    note: str = "",
    auto_start_worker: bool = True,
    goal_id: Optional[str] = None,
    project_id: Optional[str] = None,
    todo_id: Optional[str] = None,
) -> Dict[str, Any]:
    clean_user_input = sanitize_text(user_input or "")
    runtime_session_id = sanitize_text(session_id or "")

    from utils.runtime_state_store import get_runtime_state_store

    state_store = get_runtime_state_store()
    session_record = state_store.get_or_create_session(session_id=runtime_session_id)
    runtime_session_id = sanitize_text(session_record.get("session_id") or runtime_session_id)
    record = state_store.create_schedule(
        session_id=runtime_session_id,
        user_input=clean_user_input,
        schedule_type=schedule_type,
        run_at=run_at,
        interval_seconds=interval_seconds,
        time_of_day=time_of_day,
        note=note,
        goal_id=sanitize_text(goal_id or ""),
        project_id=sanitize_text(project_id or ""),
        todo_id=sanitize_text(todo_id or ""),
    )
    if auto_start_worker:
        start_background_worker()
    return sanitize_value(record)


def get_scheduled_tasks(
    *,
    session_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: Optional[int] = 100,
) -> List[Dict[str, Any]]:
    from utils.runtime_state_store import get_runtime_state_store

    return sanitize_value(
        get_runtime_state_store().load_schedules(
            session_id=session_id,
            status=status,
            limit=limit,
        )
    )


def pause_scheduled_task(schedule_id: str) -> Dict[str, Any]:
    from utils.runtime_state_store import get_runtime_state_store

    return sanitize_value(get_runtime_state_store().pause_schedule(schedule_id))


def resume_scheduled_task(schedule_id: str) -> Dict[str, Any]:
    from utils.runtime_state_store import get_runtime_state_store

    return sanitize_value(get_runtime_state_store().resume_schedule(schedule_id))


def delete_scheduled_task(schedule_id: str) -> Dict[str, Any]:
    from utils.runtime_state_store import get_runtime_state_store

    return sanitize_value(get_runtime_state_store().delete_schedule(schedule_id))


def get_user_preferences(session_id: Optional[str] = None) -> Dict[str, Any]:
    return sanitize_value(_load_effective_user_preferences(sanitize_text(session_id or "")))


def update_user_preferences(
    preferences: Dict[str, Any],
    *,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    from utils.runtime_state_store import get_runtime_state_store

    updated = sanitize_value(
        get_runtime_state_store().update_preferences(
            preferences=sanitize_value(preferences or {}),
            session_id=sanitize_text(session_id or "") or None,
        )
    )
    try:
        memory_store = _resolve_runtime_memory()
        MemoryManager(memory_store).persist_preferences(
            updated,
            session_id=sanitize_text(session_id or ""),
        )
    except Exception as e:
        log_warning(f"Preference memory persistence failed: {e}")
    return updated


def purge_session_working_memory(session_id: str) -> int:
    """A4: drop working-tier memories scoped to ``session_id``.

    Called at CLI/UI session teardown so short-lived scratch entries
    don't outlive the session. No-ops when tiered memory is disabled or
    the session_id is empty.
    """
    sid = sanitize_text(session_id or "")
    if not sid:
        return 0
    try:
        memory_store = _resolve_runtime_memory()
        if memory_store is None:
            return 0
        manager = MemoryManager(memory_store)
        tiered = manager.tiered_store
        if tiered is None:
            return 0
        return int(tiered.purge_working(sid))
    except Exception as e:
        log_warning(f"purge_session_working_memory failed: {e}")
        return 0


def get_notification_feed(
    *,
    session_id: Optional[str] = None,
    unread_only: bool = False,
    limit: Optional[int] = 50,
) -> List[Dict[str, Any]]:
    from utils.runtime_state_store import get_runtime_state_store

    return sanitize_value(
        get_runtime_state_store().load_notifications(
            session_id=sanitize_text(session_id or "") or None,
            unread_only=bool(unread_only),
            limit=limit,
        )
    )


def mark_notification_read(notification_id: str) -> Dict[str, Any]:
    from utils.runtime_state_store import get_runtime_state_store

    return sanitize_value(get_runtime_state_store().mark_notification_read(notification_id))


def mark_all_notifications_read(session_id: Optional[str] = None) -> int:
    from utils.runtime_state_store import get_runtime_state_store

    return int(
        get_runtime_state_store().mark_notifications_read(
            sanitize_text(session_id or "") or None
        )
    )


def create_goal(
    *,
    session_id: str,
    title: str,
    description: str = "",
) -> Dict[str, Any]:
    from utils.work_context_store import get_work_context_store

    return sanitize_value(
        get_work_context_store().create_goal(
            session_id=sanitize_text(session_id),
            title=sanitize_text(title),
            description=sanitize_text(description),
        )
    )


def create_project(
    *,
    session_id: str,
    title: str,
    goal_id: Optional[str] = None,
    description: str = "",
) -> Dict[str, Any]:
    from utils.work_context_store import get_work_context_store

    return sanitize_value(
        get_work_context_store().create_project(
            session_id=sanitize_text(session_id),
            title=sanitize_text(title),
            goal_id=sanitize_text(goal_id or ""),
            description=sanitize_text(description),
        )
    )


def create_todo(
    *,
    session_id: str,
    title: str,
    goal_id: Optional[str] = None,
    project_id: Optional[str] = None,
    details: str = "",
) -> Dict[str, Any]:
    from utils.work_context_store import get_work_context_store

    return sanitize_value(
        get_work_context_store().create_todo(
            session_id=sanitize_text(session_id),
            title=sanitize_text(title),
            goal_id=sanitize_text(goal_id or ""),
            project_id=sanitize_text(project_id or ""),
            details=sanitize_text(details),
        )
    )


def update_todo_status(todo_id: str, status: str) -> Dict[str, Any]:
    from utils.work_context_store import get_work_context_store

    return sanitize_value(
        get_work_context_store().update_todo_status(
            sanitize_text(todo_id),
            sanitize_text(status),
        )
    )


def get_work_dashboard(session_id: str) -> Dict[str, Any]:
    from utils.work_context_store import get_work_context_store
    from utils.artifact_store import get_artifact_store

    safe_session_id = sanitize_text(session_id)
    work_store = get_work_context_store()
    goals = sanitize_value(work_store.list_goals(session_id=safe_session_id, limit=100))
    projects = sanitize_value(work_store.list_projects(session_id=safe_session_id, limit=100))
    todos = sanitize_value(work_store.list_todos(session_id=safe_session_id, limit=200))
    artifacts = sanitize_value(get_artifact_store().search_artifacts(session_id=safe_session_id, limit=50))

    todo_summary = {
        "pending": sum(1 for item in todos if str(item.get("status", "")) == "pending"),
        "in_progress": sum(1 for item in todos if str(item.get("status", "")) == "in_progress"),
        "done": sum(1 for item in todos if str(item.get("status", "")) == "done"),
    }
    return {
        "goals": goals,
        "projects": projects,
        "todos": todos,
        "artifacts": artifacts,
        "todo_summary": todo_summary,
    }


def create_work_template(
    *,
    session_id: str,
    name: str,
    user_input: str,
    goal_id: Optional[str] = None,
    project_id: Optional[str] = None,
    todo_id: Optional[str] = None,
    source_job_id: str = "",
    notes: str = "",
) -> Dict[str, Any]:
    from utils.workflow_automation_store import get_workflow_automation_store

    return sanitize_value(
        get_workflow_automation_store().create_template(
            session_id=sanitize_text(session_id),
            name=sanitize_text(name),
            user_input=sanitize_text(user_input),
            goal_id=sanitize_text(goal_id or ""),
            project_id=sanitize_text(project_id or ""),
            todo_id=sanitize_text(todo_id or ""),
            source_job_id=sanitize_text(source_job_id),
            notes=sanitize_text(notes),
        )
    )


def create_template_from_job(job_id: str, template_name: str) -> Dict[str, Any]:
    from utils.runtime_state_store import get_runtime_state_store

    job_record = sanitize_value(get_runtime_state_store().get_job(job_id))
    if not job_record:
        return {}
    return create_work_template(
        session_id=sanitize_text(job_record.get("session_id") or ""),
        name=template_name,
        user_input=sanitize_text(job_record.get("user_input") or ""),
        goal_id=sanitize_text(job_record.get("goal_id") or ""),
        project_id=sanitize_text(job_record.get("project_id") or ""),
        todo_id=sanitize_text(job_record.get("todo_id") or ""),
        source_job_id=sanitize_text(job_record.get("job_id") or ""),
        notes="Saved from successful job",
    )


def list_work_templates(
    *,
    session_id: Optional[str] = None,
    limit: Optional[int] = 100,
) -> List[Dict[str, Any]]:
    from utils.workflow_automation_store import get_workflow_automation_store

    return sanitize_value(
        get_workflow_automation_store().list_templates(
            session_id=sanitize_text(session_id or "") or None,
            limit=limit,
        )
    )


def delete_work_template(template_id: str) -> Dict[str, Any]:
    from utils.workflow_automation_store import get_workflow_automation_store

    return sanitize_value(get_workflow_automation_store().delete_template(sanitize_text(template_id)))


def submit_template(
    template_id: str,
    *,
    session_id: Optional[str] = None,
    goal_id: Optional[str] = None,
    project_id: Optional[str] = None,
    todo_id: Optional[str] = None,
    file_event_path: str = "",
    auto_start_worker: bool = True,
    trigger_source: str = "template",
) -> Dict[str, Any]:
    from utils.workflow_automation_store import get_workflow_automation_store

    template = sanitize_value(get_workflow_automation_store().get_template(sanitize_text(template_id)))
    if not template:
        return {}
    user_input = sanitize_text(template.get("user_input") or "")
    if file_event_path:
        user_input = f"{user_input}\n\nNew file detected: {sanitize_text(file_event_path)}"
    return submit_task(
        user_input,
        session_id=session_id or sanitize_text(template.get("session_id") or "") or None,
        goal_id=goal_id or sanitize_text(template.get("goal_id") or "") or None,
        project_id=project_id or sanitize_text(template.get("project_id") or "") or None,
        todo_id=todo_id or sanitize_text(template.get("todo_id") or "") or None,
        auto_start_worker=auto_start_worker,
        trigger_source=trigger_source,
    )


def create_directory_watch(
    *,
    session_id: str,
    directory_path: str,
    template_id: str = "",
    user_input: str = "",
    goal_id: Optional[str] = None,
    project_id: Optional[str] = None,
    todo_id: Optional[str] = None,
    note: str = "",
) -> Dict[str, Any]:
    from utils.workflow_automation_store import get_workflow_automation_store

    watch = get_workflow_automation_store().create_directory_watch(
        session_id=sanitize_text(session_id),
        directory_path=sanitize_text(directory_path),
        template_id=sanitize_text(template_id),
        user_input=sanitize_text(user_input),
        goal_id=sanitize_text(goal_id or ""),
        project_id=sanitize_text(project_id or ""),
        todo_id=sanitize_text(todo_id or ""),
        note=sanitize_text(note),
    )
    start_background_worker()
    return sanitize_value(watch)


def list_directory_watches(
    *,
    session_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: Optional[int] = 100,
) -> List[Dict[str, Any]]:
    from utils.workflow_automation_store import get_workflow_automation_store

    return sanitize_value(
        get_workflow_automation_store().list_directory_watches(
            session_id=sanitize_text(session_id or "") or None,
            status=sanitize_text(status or "") or None,
            limit=limit,
        )
    )


def pause_directory_watch(watch_id: str) -> Dict[str, Any]:
    from utils.workflow_automation_store import get_workflow_automation_store

    return sanitize_value(
        get_workflow_automation_store().update_directory_watch_status(
            sanitize_text(watch_id),
            "paused",
        )
    )


def resume_directory_watch(watch_id: str) -> Dict[str, Any]:
    from utils.workflow_automation_store import get_workflow_automation_store

    return sanitize_value(
        get_workflow_automation_store().update_directory_watch_status(
            sanitize_text(watch_id),
            "waiting_for_event",
        )
    )


def delete_directory_watch(watch_id: str) -> Dict[str, Any]:
    from utils.workflow_automation_store import get_workflow_automation_store

    return sanitize_value(
        get_workflow_automation_store().delete_directory_watch(
            sanitize_text(watch_id)
        )
    )


def list_directory_watch_events(
    *,
    session_id: Optional[str] = None,
    limit: Optional[int] = 100,
) -> List[Dict[str, Any]]:
    from utils.workflow_automation_store import get_workflow_automation_store

    return sanitize_value(
        get_workflow_automation_store().list_directory_watch_events(
            session_id=sanitize_text(session_id or "") or None,
            limit=limit,
        )
    )


def _background_queue_worker_loop() -> None:
    last_job_id = ""
    while not _queue_worker_stop.is_set():
        try:
            from utils.runtime_state_store import get_runtime_state_store

            state_store = get_runtime_state_store()
            _release_directory_watch_events()
            state_store.update_worker_state(
                status="running",
                worker_id="omnicore-queue-worker",
                last_job_id=last_job_id,
                mode="thread",
            )
            result = run_next_queued_task()
            if result is None:
                _queue_worker_stop.wait(settings.QUEUE_WORKER_POLL_INTERVAL_SECONDS)
                continue
            last_job_id = str(result.get("job_id", "") or last_job_id)
            state_store.update_worker_state(
                status="running",
                worker_id="omnicore-queue-worker",
                last_job_id=last_job_id,
                note=f"Processed {last_job_id}",
                mode="thread",
            )
            log_agent_action(
                "QueueWorker",
                "Processed queued job",
                f"{result.get('job_id', '')}: {result.get('status', '')}",
            )
        except Exception as e:
            log_warning(f"Background queue worker failed: {e}")
            _queue_worker_stop.wait(settings.QUEUE_WORKER_POLL_INTERVAL_SECONDS)


def run_background_worker_forever() -> None:
    last_job_id = ""
    from utils.runtime_state_store import get_runtime_state_store

    state_store = get_runtime_state_store()
    state_store.update_worker_state(
        status="running",
        worker_id="omnicore-queue-worker",
        last_job_id=last_job_id,
        note="Foreground worker loop active",
        mode="process",
        pid=os.getpid(),
    )
    try:
        while True:
            _release_directory_watch_events()
            result = run_next_queued_task()
            if result is None:
                time.sleep(settings.QUEUE_WORKER_POLL_INTERVAL_SECONDS)
                state_store.update_worker_state(
                    status="running",
                    worker_id="omnicore-queue-worker",
                    last_job_id=last_job_id,
                    note="Idle",
                    mode="process",
                    pid=os.getpid(),
                )
                continue
            last_job_id = sanitize_text(result.get("job_id") or last_job_id)
            state_store.update_worker_state(
                status="running",
                worker_id="omnicore-queue-worker",
                last_job_id=last_job_id,
                note=f"Processed {last_job_id}",
                mode="process",
                pid=os.getpid(),
            )
    except KeyboardInterrupt:
        state_store.update_worker_state(
            status="stopped",
            worker_id="omnicore-queue-worker",
            last_job_id=last_job_id,
            note="Worker process stopped",
            mode="process",
            pid=0,
        )
    except Exception as e:
        state_store.update_worker_state(
            status="error",
            worker_id="omnicore-queue-worker",
            last_job_id=last_job_id,
            note=f"Worker process crashed: {sanitize_text(str(e))}",
            mode="process",
            pid=os.getpid(),
        )
        raise


def start_background_worker(mode: Optional[str] = None) -> bool:
    global _queue_worker_thread, _queue_worker_process
    resolved_mode = _resolve_worker_mode(mode)
    with _queue_worker_lock:
        if _queue_worker_thread and _queue_worker_thread.is_alive():
            return False
        if _queue_worker_process and _queue_worker_process.poll() is None:
            return False
        persisted = {}
        try:
            from utils.runtime_state_store import get_runtime_state_store

            persisted = get_runtime_state_store().get_worker_state()
        except Exception:
            persisted = {}
        existing_pid = int(persisted.get("pid", 0) or 0)
        if existing_pid and _is_pid_running(existing_pid):
            return False
        try:
            from utils.runtime_state_store import get_runtime_state_store

            recovery = get_runtime_state_store().recover_stale_running_jobs(
                settings.QUEUE_STALE_AFTER_SECONDS
            )
            get_runtime_state_store().update_worker_state(
                status="starting",
                worker_id="omnicore-queue-worker",
                note=(
                    f"Recovered {recovery.get('jobs_requeued', 0)} stale job(s)"
                    if recovery.get("jobs_requeued", 0)
                    else "Worker starting"
                ),
                mode=resolved_mode,
            )
        except Exception as e:
            log_warning(f"Failed to initialize background worker state: {e}")
        if resolved_mode == "process":
            script_path = str((settings.PROJECT_ROOT / "main.py").resolve())
            creationflags = 0
            if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
                creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP
            if hasattr(subprocess, "DETACHED_PROCESS"):
                creationflags |= subprocess.DETACHED_PROCESS
            _queue_worker_process = subprocess.Popen(
                [sys.executable, script_path, "worker", "--process-loop"],
                cwd=str(settings.PROJECT_ROOT),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            try:
                from utils.runtime_state_store import get_runtime_state_store

                get_runtime_state_store().update_worker_state(
                    status="running",
                    worker_id="omnicore-queue-worker",
                    note="Worker process started",
                    mode="process",
                    pid=int(_queue_worker_process.pid or 0),
                )
            except Exception as e:
                log_warning(f"Failed to persist process worker state: {e}")
            return True
        _queue_worker_stop.clear()
        _queue_worker_thread = threading.Thread(
            target=_background_queue_worker_loop,
            name="omnicore-queue-worker",
            daemon=True,
        )
        _queue_worker_thread.start()
        return True


def stop_background_worker() -> bool:
    global _queue_worker_thread, _queue_worker_process
    with _queue_worker_lock:
        if _queue_worker_process and _queue_worker_process.poll() is None:
            pid = int(_queue_worker_process.pid or 0)
            if pid > 0:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
            time.sleep(0.2)
            alive = _is_pid_running(pid)
            if not alive:
                try:
                    from utils.runtime_state_store import get_runtime_state_store

                    get_runtime_state_store().update_worker_state(
                        status="stopped",
                        worker_id="omnicore-queue-worker",
                        note="Worker process stopped",
                        mode="process",
                        pid=0,
                    )
                except Exception as e:
                    log_warning(f"Failed to persist stopped process worker state: {e}")
                _queue_worker_process = None
                return True
        if not _queue_worker_thread or not _queue_worker_thread.is_alive():
            try:
                from utils.runtime_state_store import get_runtime_state_store

                persisted = get_runtime_state_store().get_worker_state()
                pid = int(persisted.get("pid", 0) or 0)
                if persisted.get("mode") == "process" and pid and _is_pid_running(pid):
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except OSError:
                        return False
                    time.sleep(0.2)
                    stopped = not _is_pid_running(pid)
                    if stopped:
                        get_runtime_state_store().update_worker_state(
                            status="stopped",
                            worker_id="omnicore-queue-worker",
                            note="Worker process stopped",
                            mode="process",
                            pid=0,
                        )
                    return stopped
            except Exception:
                pass
            return False
        _queue_worker_stop.set()
        _queue_worker_thread.join(timeout=2.0)
        stopped = not _queue_worker_thread.is_alive()
        if stopped:
            try:
                from utils.runtime_state_store import get_runtime_state_store

                get_runtime_state_store().update_worker_state(
                    status="stopped",
                    worker_id="omnicore-queue-worker",
                    note="Worker stopped",
                    mode="thread",
                )
            except Exception as e:
                log_warning(f"Failed to persist stopped worker state: {e}")
            _queue_worker_thread = None
        return stopped


def get_background_worker_status() -> Dict[str, Any]:
    with _queue_worker_lock:
        alive = bool(_queue_worker_thread and _queue_worker_thread.is_alive())
        thread_name = _queue_worker_thread.name if alive and _queue_worker_thread else ""
        process_alive = bool(_queue_worker_process and _queue_worker_process.poll() is None)
        process_pid = int(_queue_worker_process.pid or 0) if process_alive and _queue_worker_process else 0
    persisted: Dict[str, Any] = {}
    try:
        from utils.runtime_state_store import get_runtime_state_store

        persisted = sanitize_value(get_runtime_state_store().get_worker_state())
    except Exception as e:
        persisted = {"error": sanitize_text(str(e))}
    status = {
        "running": alive or process_alive,
        "thread_name": thread_name,
        "mode": "thread" if alive else ("process" if process_alive else sanitize_text(persisted.get("mode") or "")),
        "pid": process_pid,
    }
    if persisted:
        persisted_pid = int(persisted.get("pid", 0) or 0)
        if not process_alive and persisted_pid and _is_pid_running(persisted_pid):
            status["running"] = True
            status["mode"] = "process"
            status["pid"] = persisted_pid
        status["persisted"] = persisted
        if not alive and persisted.get("status"):
            status["last_status"] = persisted.get("status")
    return status


def rerun_job(job_id: str) -> Optional[Dict[str, Any]]:
    try:
        from utils.runtime_state_store import get_runtime_state_store

        job_record = get_runtime_state_store().get_job(job_id)
    except Exception as e:
        log_warning(f"Failed to load job for rerun: {e}")
        return None

    if not job_record:
        return None

    return submit_task(
        str(job_record.get("user_input", "") or ""),
        session_id=str(job_record.get("session_id", "") or "") or None,
        goal_id=str(job_record.get("goal_id", "") or "") or None,
        project_id=str(job_record.get("project_id", "") or "") or None,
        todo_id=str(job_record.get("todo_id", "") or "") or None,
    )


def resume_failed_job(job_id: str) -> Optional[Dict[str, Any]]:
    try:
        from utils.runtime_state_store import get_runtime_state_store

        job_record = get_runtime_state_store().get_job(job_id)
    except Exception as e:
        log_warning(f"Failed to load job for resume: {e}")
        return None

    if not job_record:
        return None

    if not is_recoverable_job_status(job_record.get("status", "")):
        return None

    return submit_task(
        str(job_record.get("user_input", "") or ""),
        session_id=str(job_record.get("session_id", "") or "") or None,
        goal_id=str(job_record.get("goal_id", "") or "") or None,
        project_id=str(job_record.get("project_id", "") or "") or None,
        todo_id=str(job_record.get("todo_id", "") or "") or None,
    )


def resume_job_from_checkpoint(
    job_id: str,
    memory: Optional["ChromaMemory"] = None,
    conversation_history: Optional[List[Dict[str, Any]]] = None,
    checkpoint_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    # S3: try event-log-based resume first (if enabled)
    try:
        from core.event_log import get_event_reader, emit_event, EventType
        from config.settings import settings as _s3_settings
        if getattr(_s3_settings, "SESSION_EVENT_LOG_ENABLED", False):
            reader = get_event_reader()
            # We need session_id to locate events; get it from state_store
            from utils.runtime_state_store import get_runtime_state_store
            _job = get_runtime_state_store().get_job(job_id)
            if _job:
                _sid = _job.get("session_id", "")
                if _sid and reader.has_events(_sid):
                    events = reader.load_job_events(_sid, job_id)
                    if events:
                        last = reader.last_event(_sid)
                        interrupted = last and last.event_type != EventType.SESSION_END
                        rebuilt = reader.rebuild_job_state(events, job_id)
                        log_agent_action("EventLog", f"Rebuilt job state from {len(events)} events")
                        # S3: emit session resume event
                        emit_event(
                            EventType.JOB_STATUS_CHANGED,
                            session_id=_sid, job_id=job_id,
                            data={"new_status": "resuming", "source": "event_log"},
                        )
                        # Fall through to checkpoint-based resume below
                        # (event log provides audit, checkpoint provides actual state)
    except Exception as e:
        log_warning(f"Event-log resume attempt failed, falling back to checkpoint: {e}")

    try:
        from utils.runtime_state_store import get_runtime_state_store

        state_store = get_runtime_state_store()
        job_record = state_store.get_job(job_id)
        checkpoints = state_store.load_checkpoints(job_id=job_id, limit=None)
    except Exception as e:
        log_warning(f"Failed to load checkpoint for resume: {e}")
        return None

    if not job_record or not checkpoints:
        return None

    if str(job_record.get("is_special_command", False)).lower() == "true" or bool(job_record.get("is_special_command", False)):
        return None

    checkpoint = {}
    if checkpoint_id:
        for item in checkpoints:
            if str(item.get("checkpoint_id", "") or "") == str(checkpoint_id):
                checkpoint = dict(item)
                break
    if not checkpoint:
        non_finalize = [
            item for item in checkpoints
            if isinstance(item, dict) and str(item.get("stage", "") or "") != "finalize"
        ]
        if non_finalize:
            checkpoint = dict(non_finalize[-1])
        else:
            checkpoint = dict(checkpoints[-1])

    checkpoint_state = checkpoint.get("state")
    if not isinstance(checkpoint_state, dict):
        return None

    resumed_state = sanitize_value(checkpoint_state)
    resumed_state.setdefault("messages", [])
    from core.message_bus import (
        MessageBus, MSG_RESUME_STAGE, MSG_RESUME_CHECKPOINT_ID, MSG_RESUME_REQUESTED_AT,
    )
    _resume_bus = MessageBus.from_dict(resumed_state.get("message_bus", []))
    _resume_bus.publish("runtime", "*", MSG_RESUME_STAGE, {"value": sanitize_text(checkpoint.get("stage") or "")})
    _resume_bus.publish("runtime", "*", MSG_RESUME_CHECKPOINT_ID, {"value": sanitize_text(checkpoint.get("checkpoint_id") or "")})
    _resume_bus.publish("runtime", "*", MSG_RESUME_REQUESTED_AT, {"value": str(int(time.time()))})
    resumed_state["message_bus"] = _resume_bus.to_dict()

    # R5: inject persisted plan context into resumed state
    from core.plan_manager import load_plan
    plan_content = load_plan(job_id)
    if plan_content:
        from langchain_core.messages import SystemMessage
        resumed_state.setdefault("messages", []).insert(
            0, SystemMessage(content=f"[恢复的执行计划]\n{plan_content}")
        )

    result = _execute_submitted_job(
        sanitize_text(job_record.get("user_input") or ""),
        runtime_session_id=sanitize_text(job_record.get("session_id") or ""),
        runtime_job_id=sanitize_text(job_record.get("job_id") or ""),
        memory=memory,
        clean_history=sanitize_value(conversation_history or []),
        initial_state_override=resumed_state,
    )
    if isinstance(result, dict):
        result["resumed_from_checkpoint"] = True
        result["resume_checkpoint_id"] = sanitize_text(checkpoint.get("checkpoint_id") or "")
        result["resume_checkpoint_stage"] = sanitize_text(checkpoint.get("stage") or "")
        result["resume_strategy"] = "checkpoint_replay"
    return result


def approve_waiting_job(job_id: str) -> Optional[Dict[str, Any]]:
    try:
        from utils.runtime_state_store import get_runtime_state_store

        state_store = get_runtime_state_store()
        job_record = state_store.get_job(job_id)
        checkpoints = state_store.load_checkpoints(job_id=job_id, limit=None)
    except Exception as e:
        log_warning(f"Failed to load waiting job for approval: {e}")
        return None

    if not job_record:
        return None
    if str(job_record.get("status", "") or "") != WAITING_FOR_APPROVAL:
        return None
    if not checkpoints:
        return None

    selected_checkpoint: Dict[str, Any] = {}
    for item in reversed(checkpoints):
        snapshot = item.get("state", {}) if isinstance(item, dict) else {}
        tasks = snapshot.get("task_queue", []) if isinstance(snapshot, dict) else []
        if any(
            isinstance(task, dict) and str(task.get("status", "") or "") == WAITING_FOR_APPROVAL
            for task in tasks
        ):
            selected_checkpoint = dict(item)
            break
    if not selected_checkpoint:
        return None

    resumed_state = sanitize_value(selected_checkpoint.get("state") or {})
    approved_actions = []
    for task in resumed_state.get("task_queue", []) or []:
        if not isinstance(task, dict):
            continue
        if str(task.get("status", "") or "") != WAITING_FOR_APPROVAL:
            continue
        task["status"] = "pending"
        approved_actions.append(str(task.get("task_id", "") or ""))
    if not approved_actions:
        return None

    from core.message_bus import (
        MessageBus, MSG_APPROVED_ACTIONS, MSG_RESUME_STAGE,
        MSG_RESUME_CHECKPOINT_ID, MSG_APPROVAL_RESUMED_AT,
    )
    _approval_bus = MessageBus.from_dict(resumed_state.get("message_bus", []))
    _approval_bus.publish("policy", "executor", MSG_APPROVED_ACTIONS, {"value": approved_actions})
    _approval_bus.publish("runtime", "*", MSG_RESUME_STAGE, {"value": "human_confirm"})
    _approval_bus.publish("runtime", "*", MSG_RESUME_CHECKPOINT_ID, {"value": sanitize_text(selected_checkpoint.get("checkpoint_id") or "")})
    _approval_bus.publish("runtime", "*", MSG_APPROVAL_RESUMED_AT, {"value": str(int(time.time()))})
    resumed_state["message_bus"] = _approval_bus.to_dict()

    result = _execute_submitted_job(
        sanitize_text(job_record.get("user_input") or ""),
        runtime_session_id=sanitize_text(job_record.get("session_id") or ""),
        runtime_job_id=sanitize_text(job_record.get("job_id") or ""),
        initial_state_override=resumed_state,
    )
    if isinstance(result, dict):
        result["approved_waiting_job"] = True
        result["resume_strategy"] = "approval_resume"
    return result


def reject_waiting_job(job_id: str) -> Optional[Dict[str, Any]]:
    try:
        from utils.runtime_state_store import get_runtime_state_store

        state_store = get_runtime_state_store()
        job_record = state_store.get_job(job_id)
        if not job_record:
            return None
        if str(job_record.get("status", "") or "") != WAITING_FOR_APPROVAL:
            return None
        updated = state_store.set_job_status(
            job_id=job_id,
            status=BLOCKED,
            error="Approval request rejected by user",
        )
        state_store.create_notification(
            session_id=sanitize_text(updated.get("session_id") or job_record.get("session_id") or ""),
            job_id=sanitize_text(job_id),
            title="Approval rejected",
            message="The waiting action was rejected and the job is now blocked.",
            level="warning",
            category="approval",
            requires_action=False,
        )
        return sanitize_value(updated)
    except Exception as e:
        log_warning(f"Failed to reject waiting job: {e}")
        return None


def _execute_submitted_job(
    clean_user_input: str,
    *,
    runtime_session_id: str,
    runtime_job_id: str,
    memory: Optional["ChromaMemory"] = None,
    clean_history: Optional[List[Dict[str, Any]]] = None,
    initial_state_override: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    _job_start_time = time.time()
    _sl = get_structured_logger()
    with LogContext(job_id=runtime_job_id):
        _sl.log_event("job_start", detail=clean_user_input[:200])
    user_preferences = _load_effective_user_preferences(runtime_session_id)
    current_time_context = _build_current_time_context()
    current_os_context = _build_current_os_context()
    current_location_context = _build_current_location_context(
        user_preferences,
        current_time_context,
    )
    work_runtime_context = _load_work_runtime_context(
        session_id=runtime_session_id,
        job_id=runtime_job_id,
        user_input=clean_user_input,
    )
    memory = _resolve_runtime_memory(memory)
    memory_scope = build_memory_scope(
        session_id=runtime_session_id,
        goal_id=str((work_runtime_context.get("scope") or {}).get("goal_id", "") or ""),
        project_id=str((work_runtime_context.get("scope") or {}).get("project_id", "") or ""),
        todo_id=str((work_runtime_context.get("scope") or {}).get("todo_id", "") or ""),
    )
    memory_manager = MemoryManager(memory)
    try:
        from utils.runtime_state_store import get_runtime_state_store

        if runtime_session_id and runtime_job_id:
            get_runtime_state_store().start_job(
                job_id=runtime_job_id,
                session_id=runtime_session_id,
                user_input=clean_user_input,
                is_special_command=(clean_user_input or "").strip().lower() in {"memory stats", "clear memory"},
            )
    except Exception as e:
        log_warning(f"Runtime state start_job failed: {e}")

    # S3: emit job running event
    try:
        from core.event_log import emit_event, EventType
        emit_event(
            EventType.JOB_STATUS_CHANGED,
            session_id=runtime_session_id,
            job_id=runtime_job_id,
            data={"new_status": "running", "user_input": clean_user_input[:500]},
        )
    except Exception:
        pass

    special = None if initial_state_override else _handle_scoped_memory_command(
        clean_user_input,
        memory,
        scope=memory_scope,
    )
    if special is not None:
        special["session_id"] = runtime_session_id
        special["job_id"] = runtime_job_id
        return _finalize_runtime_result(special, clean_user_input)

    if initial_state_override:
        initial_state = sanitize_value(initial_state_override)
        initial_state["user_input"] = clean_user_input
        initial_state["session_id"] = runtime_session_id
        initial_state["job_id"] = runtime_job_id
    else:
        initial_state = create_initial_state(
            clean_user_input,
            session_id=runtime_session_id,
            job_id=runtime_job_id,
        )

    # Publish all context data to MessageBus (R2: replaces shared_memory)
    from core.message_bus import (
        MessageBus,
        MSG_TIME_CONTEXT, MSG_OS_CONTEXT, MSG_LOCATION_CONTEXT,
        MSG_USER_PREFERENCES, MSG_WORK_CONTEXT, MSG_RESOURCE_MEMORY,
        MSG_SUCCESSFUL_PATHS, MSG_FAILURE_PATTERNS, MSG_WORK_SCOPE,
        MSG_MEMORY_SCOPE, MSG_CONVERSATION_HISTORY, MSG_SESSION_ARTIFACTS,
        MSG_RELATED_HISTORY,
    )
    bus = MessageBus.from_dict(initial_state.get("message_bus", []))
    job_id = runtime_job_id

    def _pub(msg_type: str, value: Any) -> None:
        if value is not None:
            bus.publish("system", "*", msg_type, {"value": value}, job_id=job_id)

    _pub(MSG_TIME_CONTEXT, current_time_context)
    _pub(MSG_OS_CONTEXT, current_os_context)
    _pub(MSG_LOCATION_CONTEXT, current_location_context)
    _pub(MSG_USER_PREFERENCES, user_preferences)
    _pub(MSG_WORK_CONTEXT, work_runtime_context.get("work_context", {}))
    _pub(MSG_RESOURCE_MEMORY, work_runtime_context.get("resource_memory", []))
    _pub(MSG_SUCCESSFUL_PATHS, work_runtime_context.get("successful_paths", []))
    _pub(MSG_FAILURE_PATTERNS, work_runtime_context.get("failure_patterns", []))
    _pub(MSG_WORK_SCOPE, work_runtime_context.get("scope", {}))
    _pub(MSG_MEMORY_SCOPE, memory_scope)

    if clean_history:
        _pub(MSG_CONVERSATION_HISTORY, clean_history)

    # Load session artifacts
    try:
        from utils.runtime_state_store import get_runtime_state_store

        if runtime_session_id:
            session_artifacts = get_runtime_state_store().load_artifacts(
                session_id=runtime_session_id,
                limit=10,
            )
            if session_artifacts:
                _pub(MSG_SESSION_ARTIFACTS, sanitize_value(session_artifacts))
    except Exception as e:
        log_warning(f"Failed to load session artifacts: {e}")

    # Load related memories
    if memory:
        try:
            related_memories = memory_manager.search_related_history(
                clean_user_input,
                scope=memory_scope,
                n_results=3,
            )
            if related_memories:
                related_memories = sanitize_value(related_memories)
                log_agent_action("Memory", "Found related memories", f"{len(related_memories)} item(s)")
                _pub(MSG_RELATED_HISTORY, related_memories)
        except Exception as e:
            log_warning(f"Failed to query related memories: {e}")

    initial_state["message_bus"] = bus.to_dict()

    _persist_runtime_checkpoint(
        session_id=runtime_session_id,
        job_id=runtime_job_id,
        stage="initial_state",
        state=initial_state,
        note="Execution state prepared",
    )

    graph = get_graph()
    result_holder: Dict[str, Any] = {}

    def _run_graph():
        try:
            result_holder["state"] = graph.invoke(initial_state)
        except Exception as e:
            result_holder["error"] = e
            result_holder["traceback"] = traceback.format_exc()

    worker = threading.Thread(target=_run_graph, daemon=True)
    worker.start()

    try:
        while worker.is_alive():
            worker.join(timeout=0.5)
    except KeyboardInterrupt:
        console.print("\n[yellow]Task cancelled[/yellow]")
        return _finalize_runtime_result({
            "success": False,
            "error": "User cancelled operation",
            "status": "cancelled",
            "tasks": [],
            "intent": "",
            "is_special_command": False,
            "session_id": runtime_session_id,
            "job_id": runtime_job_id,
            "runtime_metrics": _collect_runtime_metrics(),
        }, clean_user_input)

    if "error" in result_holder:
        error = result_holder["error"]
        error_detail = result_holder.get("traceback") or traceback.format_exc()
        log_error(f"Execution failed: {error}")
        console.print(f"[dim]{error_detail}[/dim]")
        return _finalize_runtime_result({
            "success": False,
            "error": str(error),
            "status": "error",
            "tasks": [],
            "intent": "",
            "is_special_command": False,
            "session_id": runtime_session_id,
            "job_id": runtime_job_id,
            "runtime_metrics": _collect_runtime_metrics(),
        }, clean_user_input)

    final_state = result_holder.get("state")
    if not final_state:
        return _finalize_runtime_result({
            "success": False,
            "error": "Execution failed: no result returned",
            "status": "error",
            "tasks": [],
            "intent": "",
            "is_special_command": False,
            "session_id": runtime_session_id,
            "job_id": runtime_job_id,
            "runtime_metrics": _collect_runtime_metrics(),
        }, clean_user_input)

    final_output = sanitize_text(final_state.get("final_output") or "")
    final_error = sanitize_text(final_state.get("error_trace") or "")
    final_tasks = sanitize_value(final_state.get("task_queue", []))
    final_policy_decisions = sanitize_value(final_state.get("policy_decisions", []))
    final_intent = sanitize_text(final_state.get("current_intent") or "")
    final_feedback = sanitize_text(final_state.get("critic_feedback") or "")
    final_delivery_package = sanitize_value(final_state.get("delivery_package", {}))

    return _finalize_runtime_result({
        "success": is_success_job_status(final_state.get("execution_status")),
        "output": final_output,
        "error": final_error,
        "status": final_state.get("execution_status"),
        "critic_feedback": final_feedback,
        "tasks_completed": len([
            task for task in final_tasks
            if task["status"] == "completed"
        ]),
        "tasks": final_tasks,
        "policy_decisions": final_policy_decisions,
        "delivery_package": final_delivery_package,
        "intent": final_intent,
        "is_special_command": False,
        "session_id": sanitize_text(final_state.get("session_id") or runtime_session_id),
        "job_id": sanitize_text(final_state.get("job_id") or runtime_job_id),
        "runtime_metrics": _collect_runtime_metrics(),
    }, clean_user_input)


def run_task(
    user_input: str,
    memory: Optional["ChromaMemory"] = None,
    conversation_history: Optional[List[Dict[str, Any]]] = None,
    session_id: Optional[str] = None,
    goal_id: Optional[str] = None,
    project_id: Optional[str] = None,
    todo_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute a task immediately through the compatibility entrypoint."""
    clean_user_input = sanitize_text(user_input or "")
    clean_history = sanitize_value(conversation_history or [])

    if clean_user_input != (user_input or ""):
        log_warning("Detected unsafe input characters and sanitized them automatically.")

    log_agent_action("OmniCore", "Receive task", clean_user_input[:50])
    memory = _resolve_runtime_memory(memory)

    submission = submit_task(
        clean_user_input,
        session_id=session_id,
        auto_start_worker=False,
        goal_id=goal_id,
        project_id=project_id,
        todo_id=todo_id,
    )
    return _execute_submitted_job(
        clean_user_input,
        runtime_session_id=sanitize_text(submission.get("session_id") or ""),
        runtime_job_id=sanitize_text(submission.get("job_id") or ""),
        memory=memory,
        clean_history=clean_history,
    )


def run_next_queued_task(
    memory: Optional["ChromaMemory"] = None,
    conversation_history: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """Claim and execute the next queued job, if any."""
    memory = _resolve_runtime_memory(memory)
    _release_due_schedules()
    _release_directory_watch_events()
    try:
        from utils.event_dispatcher import get_event_dispatcher
        get_event_dispatcher().dispatch_pending_events()
    except Exception as e:
        log_warning(f"Event dispatcher failed: {e}")
    try:
        from utils.runtime_state_store import get_runtime_state_store

        claimed = get_runtime_state_store().claim_next_queued_job()
    except Exception as e:
        log_warning(f"Runtime queue claim failed: {e}")
        return None

    if not claimed:
        return None

    return _execute_submitted_job(
        sanitize_text(claimed.get("user_input") or ""),
        runtime_session_id=sanitize_text(claimed.get("session_id") or ""),
        runtime_job_id=sanitize_text(claimed.get("job_id") or ""),
        memory=memory,
        clean_history=sanitize_value(conversation_history or []),
    )
