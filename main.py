"""
OmniCore - 全栈智能体操作系统核心
主程序入口
"""
import sys
import time
import traceback
from typing import List, Dict

from core.statuses import WAITING_JOB_STATUSES
from core.runtime import (
    get_background_worker_status,
    run_background_worker_forever,
    run_task,
    start_background_worker,
    stop_background_worker,
)
from memory.scoped_chroma_store import ChromaMemory
from utils.cli_result_view import build_cli_result_view
from utils.logger import console, log_error
from utils.enhanced_input import EnhancedInput

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


def interactive_mode():
    """交互式命令行模式 - 支持历史记录和优雅退出"""
    print_banner()

    # 初始化记忆系统
    try:
        memory = ChromaMemory()
        stats = memory.get_stats()
        console.print(f"[dim]记忆系统已加载: {stats['total_memories']} 条历史记录[/dim]\n")
    except Exception as e:
        console.print(f"[yellow]记忆系统初始化失败: {e}[/yellow]\n")
        memory = None

    console.print("[green]输入你的指令，按 Ctrl+C 或 Ctrl+D 退出[/green]")
    console.print("[dim]提示：使用上下方向键浏览历史命令[/dim]\n")

    # 对话上下文：保留最近 5 轮的交互记录
    conversation_history: List[Dict] = []
    MAX_HISTORY = 5
    session_id = None

    # 初始化增强输入
    enhanced_input = EnhancedInput()

    if not enhanced_input.has_readline:
        console.print("[dim]提示：安装 gnureadline 可获得更好的命令行体验[/dim]")
        console.print("[dim]  macOS: pip install gnureadline[/dim]\n")

    while True:
        try:
            # prompt 不使用 emoji，避免 readline/libedit 光标错位
            console.print()
            user_input = enhanced_input.input("OmniCore > ")

            if user_input.lower() in ["quit", "exit", "q"]:
                console.print("\n[yellow]再见！👋[/yellow]")
                break

            # 内置命令：查看历史
            if user_input.lower() == "history":
                history = enhanced_input.get_history(20)
                if history:
                    console.print("\n[cyan]最近的命令：[/cyan]")
                    for i, cmd in enumerate(history, 1):
                        console.print(f"  [dim]{i}.[/dim] {cmd}")
                else:
                    console.print("[dim]暂无历史记录[/dim]")
                continue

            # 内置命令：清除历史
            if user_input.lower() == "clear history":
                enhanced_input.clear_history()
                console.print("[green]历史记录已清除[/green]")
                continue

            if not user_input.strip():
                continue

            # 执行任务，传入对话历史
            result = run_task(
                user_input,
                memory,
                conversation_history,
                session_id=session_id,
            )
            session_id = result.get("session_id") or session_id

            # 非内置命令才加入对话历史，避免污染 Router 上下文
            if not result.get("is_special_command"):
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
            view = build_cli_result_view(result)
            console.print(Panel(
                view["body"],
                title=view["title"],
                border_style=view["border_style"],
            ))

        except (KeyboardInterrupt, EOFError):
            console.print("\n[yellow]再见！[/yellow]")
            break
        except Exception as e:
            error_detail = traceback.format_exc()
            log_error(f"发生错误: {e}")
            console.print(f"[dim]{error_detail}[/dim]")
            continue

    # 保存历史记录
    enhanced_input.save_history()


def worker_mode():
    """Run the queue worker as a dedicated foreground process."""
    print_banner()
    started = start_background_worker()
    status = get_background_worker_status()
    console.print(f"[green]Queue worker {'started' if started else 'already running'}[/green]")
    if status.get("persisted"):
        console.print(f"[dim]{status['persisted']}[/dim]")
    console.print("[green]Press Ctrl+C to stop the worker.[/green]")

    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        stopped = stop_background_worker()
        if stopped:
            console.print("[yellow]Queue worker stopped[/yellow]")
        else:
            console.print("[yellow]Queue worker was not running[/yellow]")


def main():
    if len(sys.argv) >= 3 and sys.argv[1].lower() == "worker" and sys.argv[2] == "--process-loop":
        run_background_worker_forever()
        return
    if len(sys.argv) == 2 and sys.argv[1].lower() == "worker":
        worker_mode()
        return
    """主函数"""
    if len(sys.argv) > 1:
        # 命令行参数模式
        user_input = " ".join(sys.argv[1:])
        result = run_task(user_input)
        status = str(result.get("status", "") or "")
        view = build_cli_result_view(result)
        if result["success"] or status in WAITING_JOB_STATUSES:
            print(view["body"])
        else:
            print(f"Error: {view['body']}")
            sys.exit(1)
    else:
        # 交互式模式
        interactive_mode()


if __name__ == "__main__":
    main()
