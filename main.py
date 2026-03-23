"""
OmniCore - 全栈智能体操作系统核心
主程序入口
"""
import sys
import time
import traceback
from typing import List, Dict, Optional

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

# 全局 TerminalWorker 实例（在交互模式下跨会话持久化工作目录）
_terminal_worker: Optional[object] = None


def _get_terminal_worker():
    """懒加载 TerminalWorker（仅在 terminal 功能启用时）"""
    global _terminal_worker
    from config.settings import settings
    if not settings.TERMINAL_ENABLED:
        return None
    if _terminal_worker is None:
        from agents.terminal_worker import TerminalWorker
        _terminal_worker = TerminalWorker()
    return _terminal_worker


def _handle_builtin_command(user_input: str) -> Optional[Dict]:
    """
    处理终端内置快捷命令，返回 result dict 或 None（不是内置命令）。

    快捷命令：
      !<cmd>         直接执行 shell 命令（跳过 LLM 路由）
      /cd <path>     切换工作目录
      /ls [path]     列出目录
      /cwd           显示当前工作目录
      /allow <prefix> 会话内批准某类命令前缀
      /shell         显示当前 shell 和工作目录信息
    """
    stripped = user_input.strip()

    # !cmd 快捷方式：直接执行 shell 命令
    if stripped.startswith("!"):
        cmd = stripped[1:].strip()
        if not cmd:
            return {"success": False, "output": "用法: !<命令>", "is_special_command": True}
        worker = _get_terminal_worker()
        if worker is None:
            return {"success": False, "output": "终端功能未启用（TERMINAL_ENABLED=false）", "is_special_command": True}

        console.print(f"[dim]$ {cmd}[/dim]")

        def _stream_cb(line: str, stream_type: str):
            if stream_type == "stdout":
                console.print(line, end="", highlight=False)
            else:
                console.print(f"[dim red]{line}[/dim red]", end="")

        result = worker.execute_shell(
            command=cmd,
            stream_callback=_stream_cb,
        )
        output = result.get("stdout", "") or result.get("error", "")
        return {
            "success": result.get("success", False),
            "output": output.strip(),
            "error": result.get("error", ""),
            "is_special_command": True,
            "status": "completed" if result.get("success") else "failed",
        }

    # /cd <path>
    if stripped.lower().startswith("/cd ") or stripped.lower() == "/cd":
        path = stripped[3:].strip() or "~"
        worker = _get_terminal_worker()
        if worker is None:
            return {"success": False, "output": "终端功能未启用", "is_special_command": True}
        result = worker.change_directory(path)
        if result.get("success"):
            return {"success": True, "output": f"工作目录: {result['working_dir']}", "is_special_command": True}
        return {"success": False, "output": result.get("error", "切换失败"), "is_special_command": True}

    # /ls [path]
    if stripped.lower().startswith("/ls"):
        path = stripped[3:].strip() or None
        worker = _get_terminal_worker()
        if worker is None:
            return {"success": False, "output": "终端功能未启用", "is_special_command": True}
        result = worker.list_dir(path=path)
        if result.get("success"):
            lines = []
            for entry in result["entries"]:
                icon = "📁" if entry["type"] == "dir" else "📄"
                size = f" ({entry['size']} B)" if entry.get("size") is not None else ""
                lines.append(f"  {icon} {entry['name']}{size}")
            return {
                "success": True,
                "output": f"{result['path']}\n" + "\n".join(lines),
                "is_special_command": True,
            }
        return {"success": False, "output": result.get("error", "列出失败"), "is_special_command": True}

    # /cwd
    if stripped.lower() == "/cwd":
        worker = _get_terminal_worker()
        cwd = worker.working_dir if worker else "终端功能未启用"
        return {"success": True, "output": f"当前工作目录: {cwd}", "is_special_command": True}

    # /allow <prefix>
    if stripped.lower().startswith("/allow "):
        prefix = stripped[7:].strip()
        worker = _get_terminal_worker()
        if worker is None:
            return {"success": False, "output": "终端功能未启用", "is_special_command": True}
        worker.approve_command_prefix(prefix)
        return {"success": True, "output": f"已批准命令前缀: '{prefix}'（本会话内有效）", "is_special_command": True}

    # /shell
    if stripped.lower() == "/shell":
        from config.settings import settings
        import platform
        worker = _get_terminal_worker()
        info = [
            f"OS:          {platform.system()} {platform.release()} ({platform.machine()})",
            f"Shell:       {worker.shell if worker else settings.TERMINAL_SHELL}",
            f"工作目录:    {worker.working_dir if worker else 'N/A'}",
            f"权限模式:    {settings.TERMINAL_PERMISSION_MODE}",
            f"默认超时:    {settings.TERMINAL_DEFAULT_TIMEOUT}s",
            f"沙箱模式:    {'启用 → ' + settings.TERMINAL_SANDBOX_ROOT if settings.TERMINAL_SANDBOX_ENABLED else '禁用'}",
            f"流式输出:    {'启用' if settings.TERMINAL_STREAM_OUTPUT else '禁用'}",
        ]
        return {"success": True, "output": "\n".join(info), "is_special_command": True}

    return None  # 不是内置命令


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

    # 初始化记忆系统（后台预热，不阻塞启动）
    try:
        memory = ChromaMemory()
        import threading, os, contextlib

        def _warmup():
            import warnings
            with open(os.devnull, "w") as devnull, \
                 contextlib.redirect_stderr(devnull), \
                 contextlib.redirect_stdout(devnull), \
                 warnings.catch_warnings():
                warnings.simplefilter("ignore")
                os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
                memory._collection

        threading.Thread(target=_warmup, daemon=True).start()
        console.print("[dim]记忆系统就绪[/dim]\n")
    except Exception as e:
        console.print(f"[yellow]记忆系统初始化失败: {e}[/yellow]\n")
        memory = None

    console.print("[green]输入你的指令，按 Ctrl+C 或 Ctrl+D 退出[/green]")
    console.print("[dim]提示：使用上下方向键浏览历史命令 | !<cmd> 直接执行 shell | /cd /ls /cwd /allow /shell[/dim]\n")

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

            # 终端内置快捷命令（!cmd, /cd, /ls, /cwd, /allow, /shell）
            builtin_result = _handle_builtin_command(user_input)
            if builtin_result is not None:
                if not builtin_result.get("is_special_command") or user_input.strip().startswith("!"):
                    # 对于 !cmd，流式输出已打印，只显示状态
                    status_style = "green" if builtin_result.get("success") else "red"
                    if not builtin_result.get("success") and builtin_result.get("output"):
                        console.print(f"[{status_style}]{builtin_result['output']}[/{status_style}]")
                else:
                    output = builtin_result.get("output", "")
                    style = "green" if builtin_result.get("success") else "red"
                    if output:
                        console.print(f"[{style}]{output}[/{style}]")
                continue

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
