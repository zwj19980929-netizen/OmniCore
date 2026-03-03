"""
OmniCore 测试用例 - Hacker News 抓取
测试完整的任务执行流程：网页抓取 + 文件写入
"""
import asyncio
import os
import sys
from pathlib import Path
import pytest

# 添加项目根目录到 path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.web_worker import WebWorker
from agents.file_worker import FileWorker
from agents.critic import CriticAgent
from config.settings import settings
from utils.logger import console, log_success, log_error

from rich.panel import Panel
from rich.table import Table


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_EXTERNAL_INTEGRATION_TESTS", "").lower() != "true",
    reason="external integration tests require network access and local side effects",
)


async def _test_hackernews_scrape():
    """测试 Hacker News 抓取功能"""
    console.print(Panel("测试 1: Hacker News 网页抓取", style="cyan"))

    web_worker = WebWorker()

    # 执行抓取
    result = await web_worker.scrape_hackernews(limit=5)

    if result["success"]:
        log_success(f"抓取成功，获取 {result['count']} 条新闻")

        # 显示结果表格
        table = Table(title="Hacker News Top 5", show_header=True)
        table.add_column("排名", style="cyan", width=6)
        table.add_column("标题", style="white")
        table.add_column("链接", style="dim", max_width=40)

        for item in result["data"]:
            table.add_row(
                str(item.get("rank", "")),
                item.get("title", "")[:60],
                item.get("link", "")[:40] + "..."
            )

        console.print(table)
        return result["data"]
    else:
        log_error(f"抓取失败: {result.get('error')}")
        return None


def _test_file_write(news_data: list):
    """测试文件写入功能"""
    console.print(Panel("测试 2: 文件写入", style="cyan"))

    file_worker = FileWorker()

    # 格式化数据
    content = file_worker.format_news_data(news_data)

    # 写入文件（测试时不需要确认）
    output_path = settings.USER_DESKTOP_PATH / "news_summary.txt"

    result = file_worker.write_file(
        file_path=str(output_path),
        content=content,
        require_confirm=False,  # 测试模式跳过确认
    )

    if result["success"]:
        log_success(f"文件写入成功: {result['file_path']}")
        console.print(f"[dim]文件大小: {result['size']} 字节[/dim]")
        return result["file_path"]
    else:
        log_error(f"文件写入失败: {result.get('error')}")
        return None


def _test_critic_verify(file_path: str):
    """测试 Critic 验证功能"""
    console.print(Panel("测试 3: Critic 文件验证", style="cyan"))

    critic = CriticAgent()

    result = critic.verify_file_created(file_path)

    if result["approved"]:
        log_success(f"验证通过: {result['summary']}")
        if result.get("content_preview"):
            console.print(Panel(
                result["content_preview"],
                title="文件内容预览",
                border_style="green",
            ))
    else:
        log_error(f"验证失败: {result['summary']}")
        for issue in result.get("issues", []):
            console.print(f"  [red]• {issue}[/red]")

    return result["approved"]


async def run_full_test():
    """运行完整测试流程"""
    console.print(Panel(
        "OmniCore 集成测试\n"
        "测试场景: 抓取 Hacker News 前 5 条新闻并保存到桌面",
        title="🧪 测试开始",
        style="bold cyan",
    ))

    console.print()

    # Step 1: 网页抓取
    news_data = await _test_hackernews_scrape()
    if not news_data:
        log_error("测试中止: 网页抓取失败")
        return False

    console.print()

    # Step 2: 文件写入
    file_path = _test_file_write(news_data)
    if not file_path:
        log_error("测试中止: 文件写入失败")
        return False

    console.print()

    # Step 3: Critic 验证
    verified = _test_critic_verify(file_path)

    console.print()

    # 测试结果汇总
    if verified:
        console.print(Panel(
            "✅ 所有测试通过！\n\n"
            f"• 成功抓取 {len(news_data)} 条新闻\n"
            f"• 文件已保存至: {file_path}\n"
            f"• Critic 验证通过",
            title="🎉 测试成功",
            border_style="green",
        ))
        return True
    else:
        console.print(Panel(
            "❌ 测试未完全通过",
            title="测试结果",
            border_style="red",
        ))
        return False


def main():
    """测试入口"""
    try:
        success = asyncio.run(run_full_test())
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        console.print("\n[yellow]测试已中断[/yellow]")
        sys.exit(1)
    except Exception as e:
        log_error(f"测试异常: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


def test_hackernews_pipeline_external():
    """Optional pytest entrypoint for the external integration flow."""
    assert asyncio.run(run_full_test()) is True


if __name__ == "__main__":
    main()
