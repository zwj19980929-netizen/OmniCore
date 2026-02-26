"""
OmniCore - 全栈智能体操作系统核心
主程序入口
"""
import sys
import traceback
import threading
from typing import Optional, List, Dict

from core.state import create_initial_state
from core.graph import get_graph
from memory.chroma_store import ChromaMemory
from utils.logger import console, log_success, log_error, log_agent_action
from config.settings import settings

from rich.panel import Panel


def print_banner():
    """打印启动横幅"""
    banner = r"""
   ____                  _ ______
  / __ \____ ___  ____  (_) ____/___  ________
 / / / / __ `__ \/ __ \/ / /   / __ \/ ___/ _ \
/ /_/ / / / / / / / / / / /___/ /_/ / /  /  __/
\____/_/ /_/ /_/_/ /_/_/\____/\____/_/   \___/

    Full-Stack Agentic OS Core v0.1.0
    """
    console.print(Panel(banner, style="cyan", title="OmniCore"))


def run_task(user_input: str, memory: Optional[ChromaMemory] = None, conversation_history: Optional[List[Dict]] = None) -> dict:
    """
    执行用户任务

    Args:
        user_input: 用户输入的自然语言指令
        memory: 可选的记忆存储实例
        conversation_history: 最近的对话历史

    Returns:
        执行结果
    """
    log_agent_action("OmniCore", "接收任务", user_input[:50])

    # 创建初始状态
    initial_state = create_initial_state(user_input)

    # 注入对话历史到 shared_memory，供 Router 使用
    if conversation_history:
        initial_state["shared_memory"]["conversation_history"] = conversation_history

    # 如果有记忆系统，先查询相关历史
    if memory:
        related_memories = memory.search_memory(user_input, n_results=3)
        if related_memories:
            log_agent_action("Memory", "找到相关记忆", f"{len(related_memories)} 条")
            initial_state["shared_memory"]["related_history"] = related_memories

    # 获取编译好的图
    graph = get_graph()

    # 在守护线程中执行图，主线程保持对 Ctrl+C 的响应
    result_holder = {}

    def _run_graph():
        try:
            final_state = graph.invoke(initial_state)
            result_holder["state"] = final_state
        except Exception as e:
            result_holder["error"] = e

    worker = threading.Thread(target=_run_graph, daemon=True)
    worker.start()

    try:
        # 主线程等待，每 0.5 秒检查一次，保持 Ctrl+C 响应
        while worker.is_alive():
            worker.join(timeout=0.5)
    except KeyboardInterrupt:
        console.print("\n[yellow]任务已取消[/yellow]")
        return {
            "success": False,
            "error": "用户取消操作",
            "status": "cancelled",
        }

    if "error" in result_holder:
        e = result_holder["error"]
        error_detail = traceback.format_exc()
        log_error(f"执行失败: {e}")
        console.print(f"[dim]{error_detail}[/dim]")
        return {
            "success": False,
            "error": str(e),
            "status": "error",
        }

    final_state = result_holder.get("state")
    if not final_state:
        return {
            "success": False,
            "error": "执行异常：无返回结果",
            "status": "error",
        }

    # 保存执行结果到记忆（只保存真正有意义的结果）
    if memory and final_state.get("execution_status") == "completed":
        output = final_state.get("final_output", "")
        has_completed_tasks = any(
            t["status"] == "completed" for t in final_state.get("task_queue", [])
        )
        if output and has_completed_tasks and "返回空内容" not in output and "解析失败" not in output:
            memory.save_task_result(
                task_description=user_input,
                result=output,
                success=True,
            )

    return {
        "success": final_state.get("execution_status") in ["completed", "completed_with_issues"],
        "output": final_state.get("final_output", ""),
        "status": final_state.get("execution_status"),
        "critic_feedback": final_state.get("critic_feedback", ""),
        "tasks_completed": len([
            t for t in final_state.get("task_queue", [])
            if t["status"] == "completed"
        ]),
    }


def interactive_mode():
    """交互式命令行模式"""
    print_banner()

    # 初始化记忆系统
    try:
        memory = ChromaMemory()
        stats = memory.get_stats()
        console.print(f"[dim]记忆系统已加载: {stats['total_memories']} 条历史记录[/dim]\n")
    except Exception as e:
        console.print(f"[yellow]记忆系统初始化失败: {e}[/yellow]\n")
        memory = None

    console.print("[green]输入你的指令，输入 'quit' 或 'exit' 退出[/green]\n")

    # 对话上下文：保留最近 5 轮的交互记录
    conversation_history: List[Dict] = []
    MAX_HISTORY = 5

    while True:
        try:
            user_input = input("\n🎯 OmniCore > ").strip()

            if user_input.lower() in ["quit", "exit", "q"]:
                console.print("\n[yellow]再见！👋[/yellow]")
                break

            if not user_input.strip():
                continue

            # 特殊命令
            if user_input.lower() == "memory stats":
                if memory:
                    stats = memory.get_stats()
                    console.print(f"记忆统计: {stats}")
                continue

            if user_input.lower() == "clear memory":
                if memory:
                    memory.clear_all()
                continue

            # 执行任务，传入对话历史
            result = run_task(user_input, memory, conversation_history)

            # 记录本轮对话到历史
            turn_record = {
                "user_input": user_input,
                "success": result.get("success", False),
                "output": (result.get("output") or result.get("error") or "")[:300],
            }
            conversation_history.append(turn_record)
            if len(conversation_history) > MAX_HISTORY:
                conversation_history.pop(0)

            # 显示结果
            console.print()
            if result["success"]:
                console.print(Panel(
                    result.get("output", "任务完成"),
                    title="✅ 执行结果",
                    border_style="green",
                ))
            else:
                console.print(Panel(
                    result.get("error", result.get("output", "执行失败")),
                    title="❌ 执行失败",
                    border_style="red",
                ))

        except KeyboardInterrupt:
            console.print("\n[yellow]操作已中断[/yellow]")
            continue
        except Exception as e:
            error_detail = traceback.format_exc()
            log_error(f"发生错误: {e}")
            console.print(f"[dim]{error_detail}[/dim]")
            continue


def main():
    """主函数"""
    if len(sys.argv) > 1:
        # 命令行参数模式
        user_input = " ".join(sys.argv[1:])
        result = run_task(user_input)
        if result["success"]:
            print(result.get("output", "完成"))
        else:
            print(f"错误: {result.get('error', '未知错误')}")
            sys.exit(1)
    else:
        # 交互式模式
        interactive_mode()


if __name__ == "__main__":
    main()
