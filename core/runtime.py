"""
OmniCore 共享运行时入口
统一 CLI / UI 的任务执行、内置命令和记忆接入逻辑
"""
import traceback
import threading
from typing import Optional, List, Dict, Any, TYPE_CHECKING

from core.state import create_initial_state
from core.graph import get_graph
from utils.logger import console, log_agent_action, log_error, log_warning
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
    }


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
                output="记忆已清空",
            )
        return _build_special_result(
            success=False,
            error="清空记忆失败",
            status="error",
        )

    return None


def run_task(
    user_input: str,
    memory: Optional["ChromaMemory"] = None,
    conversation_history: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    执行用户任务（共享入口）

    Args:
        user_input: 用户输入的自然语言指令
        memory: 可选的记忆存储实例
        conversation_history: 最近的对话历史

    Returns:
        执行结果
    """
    clean_user_input = sanitize_text(user_input or "")
    clean_history = sanitize_value(conversation_history or [])

    if clean_user_input != (user_input or ""):
        log_warning("检测到非法输入字符，已自动清洗")

    log_agent_action("OmniCore", "接收任务", clean_user_input[:50])

    special = _handle_special_command(clean_user_input, memory)
    if special is not None:
        return special

    initial_state = create_initial_state(clean_user_input)

    if clean_history:
        initial_state["shared_memory"]["conversation_history"] = clean_history

    if memory:
        try:
            related_memories = memory.search_memory(clean_user_input, n_results=3)
            if related_memories:
                related_memories = sanitize_value(related_memories)
                log_agent_action("Memory", "找到相关记忆", f"{len(related_memories)} 条")
                initial_state["shared_memory"]["related_history"] = related_memories
        except Exception as e:
            log_warning(f"查询相关记忆失败: {e}")

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
        console.print("\n[yellow]任务已取消[/yellow]")
        return {
            "success": False,
            "error": "用户取消操作",
            "status": "cancelled",
            "tasks": [],
            "intent": "",
            "is_special_command": False,
        }

    if "error" in result_holder:
        error = result_holder["error"]
        error_detail = result_holder.get("traceback") or traceback.format_exc()
        log_error(f"执行失败: {error}")
        console.print(f"[dim]{error_detail}[/dim]")
        return {
            "success": False,
            "error": str(error),
            "status": "error",
            "tasks": [],
            "intent": "",
            "is_special_command": False,
        }

    final_state = result_holder.get("state")
    if not final_state:
        return {
            "success": False,
            "error": "执行异常：无返回结果",
            "status": "error",
            "tasks": [],
            "intent": "",
            "is_special_command": False,
        }

    if memory and final_state.get("execution_status") == "completed":
        output = sanitize_text(final_state.get("final_output") or "")
        has_completed_tasks = any(
            task["status"] == "completed" for task in final_state.get("task_queue", [])
        )
        if output and has_completed_tasks and "返回空内容" not in output and "解析失败" not in output:
            try:
                memory.save_task_result(
                    task_description=clean_user_input,
                    result=output,
                    success=True,
                )
            except Exception as e:
                log_warning(f"保存任务结果到记忆失败: {e}")

    final_output = sanitize_text(final_state.get("final_output") or "")
    final_error = sanitize_text(final_state.get("error_trace") or "")
    final_tasks = sanitize_value(final_state.get("task_queue", []))
    final_intent = sanitize_text(final_state.get("current_intent") or "")
    final_feedback = sanitize_text(final_state.get("critic_feedback") or "")

    return {
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
    }
