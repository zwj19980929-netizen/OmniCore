"""
OmniCore 独立 Playwright 测试脚本
直接测试 Web 自动化功能，无需 LLM 调用
"""
import asyncio
import os
import pytest
from playwright.async_api import async_playwright


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_EXTERNAL_INTEGRATION_TESTS", "").lower() != "true",
    reason="external browser tests require Playwright and network access",
)


async def scrape_hackernews_standalone():
    """
    独立的 Hacker News 抓取脚本
    用于验证 Playwright 环境配置正确
    """
    print("=" * 60)
    print("Playwright 独立测试 - Hacker News 抓取")
    print("=" * 60)

    async with async_playwright() as p:
        # 启动浏览器（无头模式）
        print("\n[1/4] 启动浏览器...")
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        # 访问页面
        print("[2/4] 访问 Hacker News...")
        await page.goto("https://news.ycombinator.com", wait_until="domcontentloaded")
        print(f"      页面标题: {await page.title()}")

        # 抓取数据
        print("[3/4] 提取新闻数据...")
        news_items = []

        # 获取所有新闻条目
        items = await page.query_selector_all(".athing")

        for i, item in enumerate(items[:5]):
            try:
                title_elem = await item.query_selector(".titleline > a")
                if title_elem:
                    title = await title_elem.inner_text()
                    link = await title_elem.get_attribute("href")

                    # 处理相对链接
                    if link and not link.startswith("http"):
                        link = f"https://news.ycombinator.com/{link}"

                    news_items.append({
                        "rank": i + 1,
                        "title": title,
                        "link": link,
                    })
                    print(f"      [{i+1}] {title[:50]}...")
            except Exception as e:
                print(f"      [!] 提取第 {i+1} 条失败: {e}")

        # 关闭浏览器
        print("[4/4] 关闭浏览器...")
        await browser.close()

        # 输出结果
        print("\n" + "=" * 60)
        print("抓取结果:")
        print("=" * 60)

        for item in news_items:
            print(f"\n{item['rank']}. {item['title']}")
            print(f"   {item['link']}")

        print("\n" + "=" * 60)
        print(f"✅ 成功抓取 {len(news_items)} 条新闻")
        print("=" * 60)

        return news_items


async def _test_page_screenshot():
    """测试截图功能"""
    print("\n测试截图功能...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        await page.goto("https://news.ycombinator.com")

        # 截图保存
        screenshot_path = "hackernews_screenshot.png"
        await page.screenshot(path=screenshot_path)
        print(f"截图已保存: {screenshot_path}")

        await browser.close()


async def _test_form_interaction():
    """测试表单交互（以 Hacker News 搜索为例）"""
    print("\n测试页面交互...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        # 访问 Hacker News 搜索页面 (Algolia)
        await page.goto("https://hn.algolia.com/")

        # 等待搜索框加载
        search_input = await page.wait_for_selector("input[type='search']")

        # 输入搜索词
        await search_input.fill("Python")
        print("已输入搜索词: Python")

        # 等待结果加载
        await page.wait_for_timeout(2000)

        # 获取搜索结果数量
        results = await page.query_selector_all(".Story")
        print(f"搜索结果数量: {len(results)}")

        await browser.close()


def main():
    """主函数"""
    print("""
    ╔═══════════════════════════════════════════════════════╗
    ║     OmniCore Playwright 自动化测试脚本                ║
    ╚═══════════════════════════════════════════════════════╝
    """)

    # 运行测试
    asyncio.run(scrape_hackernews_standalone())


def test_page_screenshot_external():
    """Optional pytest entrypoint for the screenshot flow."""
    asyncio.run(_test_page_screenshot())


def test_form_interaction_external():
    """Optional pytest entrypoint for the form interaction flow."""
    asyncio.run(_test_form_interaction())


if __name__ == "__main__":
    main()
