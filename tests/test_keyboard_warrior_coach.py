#!/usr/bin/env python
"""
测试键盘侠教练系统
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.keyboard_warrior_coach import KeyboardWarriorCoach


def test_success_scenario():
    """测试成功场景"""
    print("\n" + "="*80)
    print("场景 1: 成功执行")
    print("="*80 + "\n")

    coach = KeyboardWarriorCoach()

    result = {
        "success": True,
        "output": "Successfully extracted 10 items",
        "data": [{"title": "漏洞1"}, {"title": "漏洞2"}]
    }

    comment = coach.evaluate_step(
        step_no=1,
        action="web_worker: 访问 CNNVD 官网",
        expected="成功访问网页并提取到数据",
        actual_result=result,
        task_context="到cnnvd找到当天最新的漏洞"
    )

    print(comment)


def test_repeated_mistake():
    """测试重复错误"""
    print("\n" + "="*80)
    print("场景 2: 重复错误")
    print("="*80 + "\n")

    coach = KeyboardWarriorCoach()

    # 第一次失败
    result1 = {
        "success": False,
        "error": "selector not found",
        "output": "Failed to find element"
    }

    comment1 = coach.evaluate_step(
        step_no=1,
        action="browser_agent: 点击搜索按钮",
        expected="成功点击并进入搜索结果页",
        actual_result=result1,
        task_context="使用 Google 搜索 CNNVD"
    )

    print(comment1)

    # 第二次还是同样的错误
    result2 = {
        "success": False,
        "error": "selector not found",
        "output": "Failed to find element"
    }

    comment2 = coach.evaluate_step(
        step_no=2,
        action="browser_agent: 点击搜索按钮",
        expected="成功点击并进入搜索结果页",
        actual_result=result2,
        task_context="使用 Google 搜索 CNNVD"
    )

    print(comment2)


def test_incomplete_result():
    """测试不完整结果"""
    print("\n" + "="*80)
    print("场景 3: 不完整结果")
    print("="*80 + "\n")

    coach = KeyboardWarriorCoach()

    result = {
        "success": True,
        "output": "No data extracted",
        "data": []
    }

    comment = coach.evaluate_step(
        step_no=3,
        action="web_worker: 提取漏洞列表",
        expected="提取到至少 5 条漏洞数据",
        actual_result=result,
        task_context="抓取 CNNVD 当天最新漏洞"
    )

    print(comment)


def test_progress_report():
    """测试进度报告"""
    print("\n" + "="*80)
    print("场景 4: 进度报告")
    print("="*80 + "\n")

    coach = KeyboardWarriorCoach()

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
    print("🔥 键盘侠教练系统测试 🔥")
    print("🔥"*40 + "\n")

    test_success_scenario()
    test_repeated_mistake()
    test_incomplete_result()
    test_progress_report()

    print("\n" + "="*80)
    print("✅ 测试完成！现在你知道教练会怎么评价 Agent 了！")
    print("="*80 + "\n")
