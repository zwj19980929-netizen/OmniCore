"""
测试增强版 Web Worker 的三层感知能力
"""
import asyncio
from agents.enhanced_web_worker import EnhancedWebWorker
from utils.browser_toolkit import BrowserToolkit
from utils.logger import log_success, log_error


async def test_enhanced_perception():
    """测试三层感知架构"""

    # 测试用例：抓取 Hacker News 首页新闻
    test_cases = [
        {
            "name": "Hacker News 新闻列表",
            "url": "https://news.ycombinator.com",
            "task": "提取首页前 10 条新闻的标题和链接",
            "limit": 10
        },
        {
            "name": "GitHub Trending",
            "url": "https://github.com/trending",
            "task": "提取今日热门仓库的名称、描述和星标数",
            "limit": 10
        }
    ]

    worker = EnhancedWebWorker()

    for test_case in test_cases:
        print(f"\n{'='*60}")
        print(f"测试用例: {test_case['name']}")
        print(f"{'='*60}\n")

        async with BrowserToolkit(headless=False) as toolkit:
            # 导航到目标页面
            goto_r = await toolkit.goto(test_case['url'])
            if not goto_r.success:
                log_error(f"导航失败: {goto_r.error}")
                continue

            # 等待页面加载
            await toolkit.human_delay(2000, 3000)

            # 执行智能提取
            result = await worker.smart_extract(
                toolkit=toolkit,
                task_description=test_case['task'],
                limit=test_case['limit']
            )

            # 输出结果
            if result.get("success"):
                log_success(f"提取成功，获取 {result.get('count', 0)} 条数据")
                print("\n提取的数据:")
                for i, item in enumerate(result.get("data", [])[:5], 1):
                    print(f"\n{i}. {item}")
            else:
                log_error(f"提取失败: {result.get('error')}")


async def test_page_understanding_only():
    """只测试页面理解层"""
    from utils.page_perceiver import get_page_understanding

    async with BrowserToolkit(headless=False) as toolkit:
        await toolkit.goto("https://news.ycombinator.com")
        await toolkit.human_delay(2000, 3000)

        # 获取页面理解
        understanding = await get_page_understanding(
            toolkit,
            task_description="提取新闻列表"
        )

        print("\n" + "="*60)
        print("页面理解结果:")
        print("="*60)
        print(understanding)


if __name__ == "__main__":
    print("选择测试模式:")
    print("1. 完整三层感知测试")
    print("2. 仅测试页面理解层")

    choice = input("\n请输入选项 (1/2): ").strip()

    if choice == "1":
        asyncio.run(test_enhanced_perception())
    elif choice == "2":
        asyncio.run(test_page_understanding_only())
    else:
        print("无效选项")
