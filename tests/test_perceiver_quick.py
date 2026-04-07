"""
快速验证 PagePerceiver 是否正常工作
"""
import pytest

pytestmark = pytest.mark.skip(reason="integration test requiring browser and LLM — run manually")

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.browser_toolkit import BrowserToolkit
from utils.page_perceiver import PagePerceiver
from utils.logger import console


async def test_perceiver():
    """测试 PagePerceiver 基础功能"""
    console.print("\n[bold cyan]测试 PagePerceiver 基础功能[/bold cyan]\n")

    perceiver = PagePerceiver()

    async with BrowserToolkit(headless=False) as tk:
        # 测试 Hacker News
        console.print("[yellow]访问 Hacker News...[/yellow]")
        await tk.goto("https://news.ycombinator.com")
        await tk.human_delay(2000, 3000)

        console.print("[yellow]提取页面结构...[/yellow]")
        try:
            page_structure = await perceiver.perceive_page(tk, "提取新闻列表")

            console.print("\n[bold green]页面结构提取成功！[/bold green]\n")
            console.print(f"URL: {page_structure.url}")
            console.print(f"标题: {page_structure.title}")
            console.print(f"主要内容块: {len(page_structure.main_content_blocks)} 个")
            console.print(f"导航块: {len(page_structure.navigation_blocks)} 个")
            console.print(f"交互元素: {len(page_structure.interactive_elements)} 个")

            # 显示 LLM 友好的文本
            console.print("\n[bold cyan]LLM 友好格式：[/bold cyan]")
            console.print("[dim]" + "=" * 80 + "[/dim]")
            llm_text = page_structure.to_llm_prompt()
            console.print(llm_text[:1000])  # 只显示前1000字符
            if len(llm_text) > 1000:
                console.print(f"\n... (还有 {len(llm_text) - 1000} 字符)")
            console.print("[dim]" + "=" * 80 + "[/dim]")

            console.print("\n[bold green]✅ PagePerceiver 工作正常！[/bold green]")
            return True

        except Exception as e:
            console.print(f"\n[bold red]❌ PagePerceiver 失败: {e}[/bold red]")
            import traceback
            traceback.print_exc()
            return False


if __name__ == "__main__":
    try:
        result = asyncio.run(test_perceiver())
        sys.exit(0 if result else 1)
    except KeyboardInterrupt:
        console.print("\n[yellow]测试被用户中断[/yellow]")
        sys.exit(1)
