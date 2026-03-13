#!/usr/bin/env python
"""
测试增强的命令行输入功能
"""
import sys
import os

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.enhanced_input import EnhancedInput
from utils.logger import console


def test_enhanced_input():
    """测试增强输入功能"""
    console.print("[cyan]═══════════════════════════════════════════[/cyan]")
    console.print("[cyan]  增强命令行输入测试[/cyan]")
    console.print("[cyan]═══════════════════════════════════════════[/cyan]\n")

    enhanced_input = EnhancedInput()

    if enhanced_input.has_readline:
        console.print("[green]✓ readline 支持已启用[/green]")
        console.print("[dim]  - 上下方向键浏览历史[/dim]")
        console.print("[dim]  - Tab 键自动补全[/dim]")
        console.print("[dim]  - Ctrl+A/E 移动光标[/dim]")
    else:
        console.print("[yellow]⚠ readline 未启用[/yellow]")
        console.print("[dim]  安装方法: pip install gnureadline[/dim]")

    console.print("\n[green]可用命令：[/green]")
    console.print("  [cyan]history[/cyan]       - 查看历史记录")
    console.print("  [cyan]clear history[/cyan] - 清除历史记录")
    console.print("  [cyan]test[/cyan]          - 测试命令")
    console.print("  [cyan]quit/exit[/cyan]     - 退出")
    console.print("\n[dim]提示：按 Ctrl+C 或 Ctrl+D 也可以退出[/dim]\n")

    while True:
        try:
            user_input = enhanced_input.input("测试 > ")

            if user_input.lower() in ["quit", "exit", "q"]:
                console.print("\n[yellow]再见！👋[/yellow]")
                break

            if user_input.lower() == "history":
                history = enhanced_input.get_history(20)
                if history:
                    console.print("\n[cyan]历史记录：[/cyan]")
                    for i, cmd in enumerate(history, 1):
                        console.print(f"  [dim]{i:2d}.[/dim] {cmd}")
                else:
                    console.print("[dim]暂无历史记录[/dim]")
                continue

            if user_input.lower() == "clear history":
                enhanced_input.clear_history()
                console.print("[green]✓ 历史记录已清除[/green]")
                continue

            if user_input.lower() == "test":
                console.print("[green]✓ 测试成功！[/green]")
                console.print(f"[dim]你输入了: {user_input}[/dim]")
                continue

            if not user_input.strip():
                continue

            console.print(f"[dim]你输入了:[/dim] {user_input}")

        except KeyboardInterrupt:
            console.print("\n[yellow]再见！👋[/yellow]")
            break
        except EOFError:
            console.print("\n[yellow]再见！👋[/yellow]")
            break

    enhanced_input.save_history()
    console.print("[dim]历史记录已保存到 ~/.omnicore_history[/dim]")


if __name__ == "__main__":
    test_enhanced_input()
