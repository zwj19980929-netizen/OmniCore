"""
OmniCore 鍏变韩杩愯鏃跺叆鍙?
缁熶竴 CLI / UI 鐨勪换鍔℃墽琛屻€佸唴缃懡浠ゅ拰璁板繂鎺ュ叆閫昏緫
"""
import traceback
import threading
from typing import Optional, List, Dict, Any, TYPE_CHECKING

from core.state import create_initial_state
from core.graph import get_graph
from utils.logger import console, log_agent_action, log_debug_metrics, log_error, log_warning
from utils.text import sanitize_text, sanitize_value

if TYPE_CHECKING:
    from memory.chroma_store import ChromaMemory


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


def _finalize_runtime_result(result: Dict[str, Any], user_input: str) -> Dict[str, Any]:
    finalized = dict(result)
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
        log_warning(f"娣囨繂鐡ㄦ潻鎰攽閹稿洦鐖ｆ径杈Е: {e}")

    tasks = sanitize_value(finalized.get("tasks") or [])
    session_id = sanitize_text(finalized.get("session_id") or "")
    job_id = sanitize_text(finalized.get("job_id") or "")

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
                artifacts=artifacts,
                is_special_command=bool(finalized.get("is_special_command", False)),
            )
            finalized["artifacts"] = artifacts
            finalized["job_record"] = sanitize_value(completion.get("job_record", {}))
            finalized["session_record"] = sanitize_value(completion.get("session_record", {}))
        else:
            finalized.setdefault("artifacts", [])
    except Exception as e:
        finalized.setdefault("artifacts", [])
        finalized["runtime_state_store_error"] = sanitize_text(str(e))
        log_warning(f"Runtime state persistence failed: {e}")

    return finalized


def _handle_special_command(
    user_input: str,
    memory: Optional["ChromaMemory"] = None,
) -> Optional[Dict[str, Any]]:
    command = (user_input or "").strip().lower()

    if command == "memory stats":
        if not memory:
            return _build_special_result(
                success=False,
                error="璁板繂绯荤粺鏈垵濮嬪寲",
                status="error",
            )
        stats = memory.get_stats()
        return _build_special_result(
            success=True,
            output=f"璁板繂缁熻: {stats}",
        )

    if command == "clear memory":
        if not memory:
            return _build_special_result(
                success=False,
                error="璁板繂绯荤粺鏈垵濮嬪寲",
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


def submit_task(
    user_input: str,
    *,
    session_id: Optional[str] = None,
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
        )
        return {
            "session_id": runtime_session_id,
            "job_id": sanitize_text(job_record.get("job_id") or ""),
            "status": sanitize_text(job_record.get("status") or "queued"),
            "user_input": clean_user_input,
            "is_special_command": bool(job_record.get("is_special_command", False)),
        }
    except Exception as e:
        log_warning(f"Runtime state initialization failed: {e}")
        return {
            "session_id": runtime_session_id,
            "job_id": "",
            "status": "error",
            "user_input": clean_user_input,
            "is_special_command": False,
        }


def _execute_submitted_job(
    clean_user_input: str,
    *,
    runtime_session_id: str,
    runtime_job_id: str,
    memory: Optional["ChromaMemory"] = None,
    clean_history: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
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

    special = _handle_special_command(clean_user_input, memory)
    if special is not None:
        special["session_id"] = runtime_session_id
        special["job_id"] = runtime_job_id
        return _finalize_runtime_result(special, clean_user_input)

    initial_state = create_initial_state(
        clean_user_input,
        session_id=runtime_session_id,
        job_id=runtime_job_id,
    )

    if clean_history:
        initial_state["shared_memory"]["conversation_history"] = clean_history

    if memory:
        try:
            related_memories = memory.search_memory(clean_user_input, n_results=3)
            if related_memories:
                related_memories = sanitize_value(related_memories)
                log_agent_action("Memory", "Found related memories", f"{len(related_memories)} item(s)")
                initial_state["shared_memory"]["related_history"] = related_memories
        except Exception as e:
            log_warning(f"Failed to query related memories: {e}")

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

    if memory and final_state.get("execution_status") == "completed":
        output = sanitize_text(final_state.get("final_output") or "")
        has_completed_tasks = any(
            task["status"] == "completed" for task in final_state.get("task_queue", [])
        )
        if output and has_completed_tasks and "empty content" not in output and "analysis failed" not in output:
            try:
                memory.save_task_result(
                    task_description=clean_user_input,
                    result=output,
                    success=True,
                )
            except Exception as e:
                log_warning(f"Failed to save task result to memory: {e}")

    final_output = sanitize_text(final_state.get("final_output") or "")
    final_error = sanitize_text(final_state.get("error_trace") or "")
    final_tasks = sanitize_value(final_state.get("task_queue", []))
    final_intent = sanitize_text(final_state.get("current_intent") or "")
    final_feedback = sanitize_text(final_state.get("critic_feedback") or "")

    return _finalize_runtime_result({
        "success": final_state.get("execution_status") in ["completed", "completed_with_issues"],
        "output": final_output,
        "error": final_error,
        "status": final_state.get("execution_status"),
        "critic_feedback": final_feedback,
        "tasks_completed": len([
            task for task in final_tasks
            if task["status"] == "completed"
        ]),
        "tasks": final_tasks,
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
) -> Dict[str, Any]:
    """Execute a task immediately through the compatibility entrypoint."""
    clean_user_input = sanitize_text(user_input or "")
    clean_history = sanitize_value(conversation_history or [])

    if clean_user_input != (user_input or ""):
        log_warning("Detected unsafe input characters and sanitized them automatically.")

    log_agent_action("OmniCore", "Receive task", clean_user_input[:50])

    submission = submit_task(clean_user_input, session_id=session_id)
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
