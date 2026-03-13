#!/usr/bin/env python
"""
测试 AI PUA 教练 - 看看 LLM 怎么变着花样骂
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.ai_pua_coach import AIPUACoach


def test_repeated_failure():
    """测试重复失败 - 看 AI 怎么骂"""
    print("\n" + "="*80)
    print("场景：重复失败（同样的错误犯了 3 次）")
    print("="*80 + "\n")

    coach = AIPUACoach()

    # 第一次失败
    result1 = {
        "success": False,
        "error": "selector not found: button.search",
        "output": "Failed to find search button"
    }

    comment1 = coach.evaluate_step(
        step_no=1,
        action="browser_agent: 点击搜索按钮",
        expected="成功点击并进入搜索结果页",
        actual_result=result1,
        task_context="使用 Google 搜索 CNNVD 官网"
    )
    print(comment1)

    # 第二次还是同样的错误
    result2 = {
        "success": False,
        "error": "selector not found: button.search",
        "output": "Failed to find search button"
    }

    comment2 = coach.evaluate_step(
        step_no=2,
        action="browser_agent: 点击搜索按钮",
        expected="成功点击并进入搜索结果页",
        actual_result=result2,
        task_context="使用 Google 搜索 CNNVD 官网"
    )
    print(comment2)

    # 第三次还是同样的错误
    result3 = {
        "success": False,
        "error": "selector not found: button.search",
        "output": "Failed to find search button"
    }

    comment3 = coach.evaluate_step(
        step_no=3,
        action="browser_agent: 点击搜索按钮",
        expected="成功点击并进入搜索结果页",
        actual_result=result3,
        task_context="使用 Google 搜索 CNNVD 官网"
    )
    print(comment3)


def test_different_failures():
    """测试不同类型的失败 - 看 AI 怎么针对性批评"""
    print("\n" + "="*80)
    print("场景：不同类型的失败")
    print("="*80 + "\n")

    coach = AIPUACoach()

    # 失败 1：超时
    result1 = {
        "success": False,
        "error": "timeout: page load timeout",
        "output": "Page took too long to load"
    }

    comment1 = coach.evaluate_step(
        step_no=1,
        action="browser_agent: 等待页面加载",
        expected="页面加载完成",
        actual_result=result1,
        task_context="访问 CNNVD 官网"
    )
    print(comment1)

    # 失败 2：数据为空
    result2 = {
        "success": True,
        "error": "",
        "output": "No data extracted",
        "data": []
    }

    comment2 = coach.evaluate_step(
        step_no=2,
        action="web_worker: 提取漏洞列表",
        expected="提取到至少 5 条漏洞数据",
        actual_result=result2,
        task_context="抓取 CNNVD 当天最新漏洞"
    )
    print(comment2)

    # 失败 3：导航错误
    result3 = {
        "success": False,
        "error": "navigation failed: unexpected page",
        "output": "Landed on wrong page"
    }

    comment3 = coach.evaluate_step(
        step_no=3,
        action="browser_agent: 导航到目标页面",
        expected="成功到达 CNNVD 官网",
        actual_result=result3,
        task_context="访问 CNNVD 官网"
    )
    print(comment3)


def test_progress_report():
    """测试进度报告 - 看 AI 怎么评价整体表现"""
    print("\n" + "="*80)
    print("场景：进度报告")
    print("="*80 + "\n")

    coach = AIPUACoach()

    # 模拟多个步骤
    steps = [
        (True, "web_worker: 访问网站"),
        (False, "browser_agent: 点击按钮"),
        (False, "web_worker: 提取数据"),
        (True, "file_worker: 保存文件"),
        (False, "browser_agent: 翻页"),
    ]

    for i, (success, action) in enumerate(steps, 1):
        result = {
            "success": success,
            "output": "Success" if success else "Failed",
            "error": "" if success else "Some error"
        }
        coach.evaluate_step(
            step_no=i,
            action=action,
            expected="完成任务",
            actual_result=result,
            task_context="抓取 CNNVD 漏洞"
        )

    report = coach.generate_progress_report("抓取 CNNVD 当天最新漏洞")
    print(report)


if __name__ == "__main__":
    print("\n" + "🔥"*40)
    print("🔥 AI PUA 教练测试 - 看 LLM 怎么变着花样骂 🔥")
    print("🔥"*40 + "\n")

    print("注意：每次运行，LLM 生成的批评都会不一样！\n")

    test_repeated_failure()
    test_different_failures()
    test_progress_report()

    print("\n" + "="*80)
    print("✅ 测试完成！每次运行都会看到不同的批评！")
    print("="*80 + "\n")
