"""
测试 web_worker 的页面感知能力（阶段1修复验证）
"""
import asyncio
import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.web_worker import WebWorker
from utils.browser_toolkit import BrowserToolkit
from utils.logger import console


async def test_hacker_news():
    """测试 Hacker News 页面分析（简单列表页）"""
    console.print("\n" + "=" * 80)
    console.print("[bold cyan]测试用例1: Hacker News (简单列表页)[/bold cyan]")
    console.print("=" * 80 + "\n")

    worker = WebWorker()

    async with BrowserToolkit(headless=False) as tk:
        console.print("[yellow]正在访问 Hacker News...[/yellow]")
        await tk.goto("https://news.ycombinator.com")
        await tk.human_delay(2000, 3000)

        console.print("[yellow]正在分析页面结构...[/yellow]")
        config = await worker.analyze_page_structure(
            tk,
            "提取首页前 10 条新闻的标题和链接"
        )

        console.print("\n[bold green]页面分析结果：[/bold green]")
        console.print(f"  item_selector: [cyan]{config.get('item_selector', 'N/A')}[/cyan]")
        console.print(f"  fields: [cyan]{config.get('fields', {})}[/cyan]")
        console.print(f"  success: [cyan]{config.get('success', False)}[/cyan]")
        if config.get('notes'):
            console.print(f"  notes: [dim]{config.get('notes')}[/dim]")

        if config.get('success') and config.get('item_selector'):
            console.print("\n[yellow]正在提取数据...[/yellow]")
            data = await worker.extract_data_with_selectors(tk, config, limit=10)

            console.print(f"\n[bold green]提取到 {len(data)} 条数据：[/bold green]")
            for i, item in enumerate(data[:5], 1):
                title = item.get('title', 'N/A')
                link = item.get('link', 'N/A')
                console.print(f"  {i}. [white]{title[:80]}[/white]")
                console.print(f"     [dim]{link[:100]}[/dim]")

            if len(data) > 5:
                console.print(f"  ... 还有 {len(data) - 5} 条数据")

            # 验证结果
            if len(data) >= 5:
                console.print("\n[bold green]✅ 测试通过：成功提取到数据[/bold green]")
                return True
            else:
                console.print("\n[bold red]❌ 测试失败：提取数据不足[/bold red]")
                return False
        else:
            console.print("\n[bold red]❌ 测试失败：页面分析失败[/bold red]")
            return False


async def test_github_trending():
    """测试 GitHub Trending 页面分析（中等复杂度）"""
    console.print("\n" + "=" * 80)
    console.print("[bold cyan]测试用例2: GitHub Trending (中等复杂度)[/bold cyan]")
    console.print("=" * 80 + "\n")

    worker = WebWorker()

    async with BrowserToolkit(headless=False) as tk:
        console.print("[yellow]正在访问 GitHub Trending...[/yellow]")
        await tk.goto("https://github.com/trending")
        await tk.human_delay(2000, 3000)

        console.print("[yellow]正在分析页面结构...[/yellow]")
        config = await worker.analyze_page_structure(
            tk,
            "提取今日热门仓库的名称、描述和星标数"
        )

        console.print("\n[bold green]页面分析结果：[/bold green]")
        console.print(f"  item_selector: [cyan]{config.get('item_selector', 'N/A')}[/cyan]")
        console.print(f"  fields: [cyan]{config.get('fields', {})}[/cyan]")
        console.print(f"  success: [cyan]{config.get('success', False)}[/cyan]")

        if config.get('success') and config.get('item_selector'):
            console.print("\n[yellow]正在提取数据...[/yellow]")
            data = await worker.extract_data_with_selectors(tk, config, limit=10)

            console.print(f"\n[bold green]提取到 {len(data)} 条数据：[/bold green]")
            for i, item in enumerate(data[:3], 1):
                title = item.get('title', 'N/A')
                desc = item.get('description', 'N/A')
                console.print(f"  {i}. [white]{title}[/white]")
                console.print(f"     [dim]{desc[:100]}[/dim]")

            if len(data) >= 3:
                console.print("\n[bold green]✅ 测试通过：成功提取到数据[/bold green]")
                return True
            else:
                console.print("\n[bold red]❌ 测试失败：提取数据不足[/bold red]")
                return False
        else:
            console.print("\n[bold red]❌ 测试失败：页面分析失败[/bold red]")
            return False


async def main():
    """运行所有测试"""
    console.print("\n[bold magenta]" + "=" * 80 + "[/bold magenta]")
    console.print("[bold magenta]WebWorker 页面感知能力测试（阶段1修复验证）[/bold magenta]")
    console.print("[bold magenta]" + "=" * 80 + "[/bold magenta]\n")

    results = []

    # 测试1：Hacker News
    try:
        result1 = await test_hacker_news()
        results.append(("Hacker News", result1))
    except Exception as e:
        console.print(f"\n[bold red]测试1异常: {e}[/bold red]")
        results.append(("Hacker News", False))

    # 测试2：GitHub Trending
    try:
        result2 = await test_github_trending()
        results.append(("GitHub Trending", result2))
    except Exception as e:
        console.print(f"\n[bold red]测试2异常: {e}[/bold red]")
        results.append(("GitHub Trending", False))

    # 总结
    console.print("\n" + "=" * 80)
    console.print("[bold magenta]测试总结[/bold magenta]")
    console.print("=" * 80 + "\n")

    passed = sum(1 for _, result in results if result)
    total = len(results)

    for name, result in results:
        status = "[green]✅ 通过[/green]" if result else "[red]❌ 失败[/red]"
        console.print(f"  {name}: {status}")

    console.print(f"\n[bold]总计: {passed}/{total} 通过[/bold]")

    if passed == total:
        console.print("\n[bold green]🎉 所有测试通过！阶段1修复成功！[/bold green]")
    else:
        console.print("\n[bold yellow]⚠️  部分测试失败，需要进一步调试[/bold yellow]")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]测试被用户中断[/yellow]")
    except Exception as e:
        console.print(f"\n[bold red]测试执行失败: {e}[/bold red]")
        import traceback
        traceback.print_exc()
