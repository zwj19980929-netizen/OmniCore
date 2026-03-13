"""
Browser Agent 行为诊断和修复
解决"打开网页啥也不做就闪退"的问题
"""
from typing import Dict, Any
from utils.logger import log_warning, log_error, console


def diagnose_early_exit(result: Dict[str, Any], task: str) -> str:
    """
    诊断 browser agent 为什么提前退出

    Args:
        result: browser agent 的返回结果
        task: 任务描述

    Returns:
        诊断报告
    """
    message = result.get("message", "")
    steps = result.get("steps", [])
    data = result.get("data", [])
    url = result.get("url", "")
    expected_url = result.get("expected_url", "")

    issues = []

    # 检查是否没有执行任何步骤就退出
    if len(steps) == 0:
        issues.append("🚨 **严重问题：没有执行任何操作就退出了！**")

        # 分析可能的原因
        if "blocked page" in message.lower():
            issues.append("   原因：误判为被封禁页面")
            issues.append("   建议：放宽封禁页面的判断条件")
        elif "unexpected page" in message.lower():
            issues.append(f"   原因：URL 不匹配（期望 {expected_url}，实际 {url}）")
            issues.append("   建议：放宽 URL 匹配条件，或者不要提前退出")
        elif "read-only task satisfied" in message.lower():
            issues.append("   原因：误判为只读任务已完成")
            issues.append("   建议：提高数据满足条件的阈值")
        else:
            issues.append("   原因：未知，可能是导航失败或其他问题")

    # 检查是否提取到数据
    if len(data) == 0:
        issues.append("⚠️ **问题：没有提取到任何数据**")
        issues.append("   建议：即使页面看起来不对，也应该尝试提取数据")

    # 检查是否执行了足够的步骤
    if 0 < len(steps) < 3:
        issues.append(f"⚠️ **问题：只执行了 {len(steps)} 步就退出了**")
        issues.append("   建议：增加最小步骤数要求，至少尝试 3-5 步")

    if not issues:
        return "✅ 没有发现明显的提前退出问题"

    report = "\n".join(issues)
    return f"""
╔════════════════════════════════════════════════════════════════════════════╗
║  🔍 Browser Agent 提前退出诊断
╚════════════════════════════════════════════════════════════════════════════╝

📋 **任务：** {task[:100]}
🌐 **URL：** {url}
📊 **执行步骤：** {len(steps)}
📦 **提取数据：** {len(data)} 条
💬 **退出消息：** {message}

{report}

╔════════════════════════════════════════════════════════════════════════════╗
║  💡 建议：修改 browser_agent.py 的提前退出逻辑
╚════════════════════════════════════════════════════════════════════════════╝
"""


def should_force_continue(result: Dict[str, Any], min_steps: int = 3) -> bool:
    """
    判断是否应该强制继续执行，而不是提前退出

    Args:
        result: browser agent 的返回结果
        min_steps: 最小步骤数

    Returns:
        是否应该强制继续
    """
    steps = result.get("steps", [])
    data = result.get("data", [])
    message = result.get("message", "")

    # 如果没有执行任何步骤，强制继续
    if len(steps) == 0:
        return True

    # 如果步骤数太少且没有数据，强制继续
    if len(steps) < min_steps and len(data) == 0:
        return True

    # 如果是 URL 不匹配但实际上可能是正确的页面，强制继续
    if "unexpected page" in message.lower():
        return True

    return False


def create_force_continue_action() -> Dict[str, Any]:
    """
    创建一个强制继续的动作

    Returns:
        继续执行的动作
    """
    return {
        "action_type": "wait",
        "description": "强制继续执行，不要提前退出",
        "confidence": 1.0
    }
