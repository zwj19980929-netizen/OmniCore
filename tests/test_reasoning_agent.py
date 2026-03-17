"""
测试脚本：推理式Browser Agent

测试新的推理式Agent在实际任务上的表现
"""

import asyncio
from playwright.async_api import async_playwright

from core.llm import LLMClient
from utils.browser_toolkit import BrowserToolkit
from agents.enhanced_reasoning_browser_agent import EnhancedReasoningBrowserAgent
from utils.logger import log_agent_action, log_success, log_error


async def test_cnnvd_task():
    """测试CNNVD漏洞查询任务"""
    print("\n" + "="*80)
    print("🧪 测试任务1: CNNVD漏洞查询")
    print("="*80 + "\n")

    # 创建LLM客户端
    llm_client = LLMClient()

    # 使用BrowserToolkit的上下文管理器
    async with BrowserToolkit(headless=False) as toolkit:
        agent = EnhancedReasoningBrowserAgent(llm_client, toolkit)

        # 执行任务
        task = "访问CNNVD网站，获取最新的5个漏洞信息（包括编号、名称、危害等级）"
        start_url = "https://www.cnnvd.org.cn/"

        result = await agent.run(task, start_url, max_steps=8)

        # 输出结果
        print("\n" + "="*80)
        print("📊 任务结果")
        print("="*80)
        print(f"成功: {result.get('success')}")
        print(f"步数: {result.get('steps_taken', 0)}")

        if result.get('success'):
            data = result.get('data', {})
            print(f"\n提取的数据:")
            print(f"类型: {data.get('data_type')}")
            print(f"摘要: {data.get('summary')}")
            print(f"数据项数量: {len(data.get('extracted_data', []))}")
        else:
            print(f"\n错误: {result.get('error')}")

        print("\n操作历史:")
        for action in result.get('action_history', []):
            print(f"  Step {action['step']}: {action['action']} - {action['reasoning'][:60]}")

        return result


async def test_weather_task():
    """测试天气查询任务"""
    print("\n" + "="*80)
    print("🧪 测试任务2: 天气查询")
    print("="*80 + "\n")

    llm_client = LLMClient()

    async with BrowserToolkit(headless=False) as toolkit:
        agent = EnhancedReasoningBrowserAgent(llm_client, toolkit)

        task = "查询广州今天的天气情况（温度、天气状况、湿度）"
        start_url = "https://tianqi.2345.com/guangzhou/57816.htm"

        result = await agent.run(task, start_url, max_steps=6)

        print("\n" + "="*80)
        print("📊 任务结果")
        print("="*80)
        print(f"成功: {result.get('success')}")
        print(f"步数: {result.get('steps_taken', 0)}")

        if result.get('success'):
            data = result.get('data', {})
            print(f"\n提取的数据:")
            print(f"摘要: {data.get('summary')}")
        else:
            print(f"\n错误: {result.get('error')}")

        return result


async def test_hackernews_task():
    """测试Hacker News抓取任务"""
    print("\n" + "="*80)
    print("🧪 测试任务3: Hacker News")
    print("="*80 + "\n")

    llm_client = LLMClient()

    async with BrowserToolkit(headless=False) as toolkit:
        agent = EnhancedReasoningBrowserAgent(llm_client, toolkit)

        task = "获取Hacker News首页前5条新闻的标题和链接"
        start_url = "https://news.ycombinator.com/"

        result = await agent.run(task, start_url, max_steps=5)

        print("\n" + "="*80)
        print("📊 任务结果")
        print("="*80)
        print(f"成功: {result.get('success')}")
        print(f"步数: {result.get('steps_taken', 0)}")

        if result.get('success'):
            data = result.get('data', {})
            print(f"\n提取的数据:")
            extracted = data.get('extracted_data', [])
            for i, item in enumerate(extracted[:5], 1):
                print(f"{i}. {item}")
        else:
            print(f"\n错误: {result.get('error')}")

        return result


async def test_simple_navigation():
    """测试简单的导航推理"""
    print("\n" + "="*80)
    print("🧪 测试任务4: 简单导航推理")
    print("="*80 + "\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()

        toolkit = BrowserToolkit(page)
        llm_client = LLMClient()
        agent = EnhancedReasoningBrowserAgent(llm_client, toolkit)

        # 测试：从首页导航到特定页面
        task = "找到'关于我们'或'About'页面"
        start_url = "https://www.example.com/"

        result = await agent.run(task, start_url, max_steps=5)

        print("\n" + "="*80)
        print("📊 任务结果")
        print("="*80)
        print(f"成功: {result.get('success')}")
        print(f"推理过程:")
        for action in result.get('action_history', []):
            print(f"  {action['reasoning']}")

        await browser.close()

        return result


async def run_all_tests():
    """运行所有测试"""
    print("\n" + "🚀"*40)
    print("开始测试推理式Browser Agent")
    print("🚀"*40 + "\n")

    results = {}

    # 测试1: CNNVD
    try:
        results['cnnvd'] = await test_cnnvd_task()
    except Exception as e:
        log_error(f"CNNVD测试失败: {e}")
        results['cnnvd'] = {"success": False, "error": str(e)}

    await asyncio.sleep(2)

    # 测试2: 天气
    try:
        results['weather'] = await test_weather_task()
    except Exception as e:
        log_error(f"天气测试失败: {e}")
        results['weather'] = {"success": False, "error": str(e)}

    await asyncio.sleep(2)

    # 测试3: Hacker News
    try:
        results['hackernews'] = await test_hackernews_task()
    except Exception as e:
        log_error(f"Hacker News测试失败: {e}")
        results['hackernews'] = {"success": False, "error": str(e)}

    # 总结
    print("\n" + "="*80)
    print("📊 测试总结")
    print("="*80)

    for task_name, result in results.items():
        status = "✅ 成功" if result.get('success') else "❌ 失败"
        steps = result.get('steps_taken', 0)
        print(f"{task_name:15} {status:10} 步数: {steps}")

    success_count = sum(1 for r in results.values() if r.get('success'))
    total_count = len(results)
    success_rate = success_count / total_count if total_count > 0 else 0

    print(f"\n总体成功率: {success_rate:.0%} ({success_count}/{total_count})")

    return results


async def test_single_task(task_name: str):
    """测试单个任务"""
    if task_name == "cnnvd":
        return await test_cnnvd_task()
    elif task_name == "weather":
        return await test_weather_task()
    elif task_name == "hackernews":
        return await test_hackernews_task()
    elif task_name == "navigation":
        return await test_simple_navigation()
    else:
        print(f"未知的任务: {task_name}")
        print("可用任务: cnnvd, weather, hackernews, navigation")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        # 测试单个任务
        task_name = sys.argv[1]
        asyncio.run(test_single_task(task_name))
    else:
        # 运行所有测试
        asyncio.run(run_all_tests())
