"""
OmniCore - 全栈智能体操作系统核心
主程序入口
"""
import sys
import traceback
from typing import List, Dict

from core.runtime import run_task
from memory.chroma_store import ChromaMemory
from utils.logger import console, log_error

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
    session_id = None

    while True:
        try:
            user_input = input("\n🎯 OmniCore > ").strip()

            if user_input.lower() in ["quit", "exit", "q"]:
                console.print("\n[yellow]再见！👋[/yellow]")
                break

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
