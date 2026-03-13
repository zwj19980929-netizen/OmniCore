#!/usr/bin/env python
"""
测试鞭策机制 - 看看 Agent 被骂得有多惨
"""
import sys
import os

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.agent_critic import AgentCritic


def test_repeated_action_loop():
    """测试重复操作循环的批评"""
    print("\n" + "="*80)
    print("测试场景 1: 重复点击循环")
    print("="*80 + "\n")

    critic = AgentCritic()
    result = {
        "success": False,
        "error": "repeated action loop detected at step 3",
        "completed_tasks": 0,
        "total_tasks": 3,
        "task": "使用 Google 搜索 CNNVD",
        "output": "Clicked Google 搜索 button 3 times"
    }

    issues = critic.analyze_failure(result)
    report = critic.generate_pua_report(issues, attempt_count=3)
    print(report)

    alternative = critic.suggest_alternative_strategy("搜索 CNNVD 漏洞", 3)
    print(alternative)


def test_zero_progress():
    """测试零进展的批评"""
    print("\n" + "="*80)
    print("测试场景 2: 零进展")
    print("="*80 + "\n")

    critic = AgentCritic()
    result = {
        "success": False,
        "error": "Task failed",
        "completed_tasks": 0,
        "total_tasks": 5,
        "task": "抓取 CNNVD 漏洞",
        "output": "Failed to complete any task"
    }

    issues = critic.analyze_failure(result)
    report = critic.generate_pua_report(issues, attempt_count=3)
    print(report)


def test_selector_failure():
    """测试选择器失败的批评"""
    print("\n" + "="*80)
    print("测试场景 3: 选择器错误")
    print("="*80 + "\n")

    critic = AgentCritic()
    result = {
        "success": False,
        "error": "selector_not_found: element not found",
        "completed_tasks": 1,
        "total_tasks": 3,
        "task": "提取页面数据",
        "output": "Failed to find element with selector"
    }

    issues = critic.analyze_failure(result)
    report = critic.generate_pua_report(issues, attempt_count=2)
    print(report)


def test_all_scenarios():
    """测试所有场景"""
    test_repeated_action_loop()
    test_zero_progress()
    test_selector_failure()


if __name__ == "__main__":
    print("\n" + "🔥"*40)
    print("🔥 Agent 鞭策机制测试 - 准备好被骂了吗？ 🔥")
    print("🔥"*40 + "\n")

    test_all_scenarios()

    print("\n" + "="*80)
    print("✅ 测试完成！现在你知道 Agent 会被骂得多惨了吧！")
    print("="*80 + "\n")
