"""
工具适配器钩子 - 在每个工具执行后调用 AI PUA 教练评估
"""
from typing import Dict, Any
from utils.ai_pua_coach import get_ai_coach
from utils.logger import console


def evaluate_tool_result(
    tool_name: str,
    task: Dict[str, Any],
    result: Dict[str, Any],
    step_no: int
) -> None:
    """
    评估工具执行结果并给出 AI PUA 教练的评论

    Args:
        tool_name: 工具名称
        task: 任务信息
        result: 执行结果
        step_no: 步骤编号
    """
    coach = get_ai_coach()

    # 构建动作描述
    action = f"{tool_name}: {task.get('description', '')[:50]}"

    # 构建期望结果
    expected = _build_expected_result(tool_name, task)

    # 任务上下文
    context = task.get("description", "")

    # 评估结果
    comment = coach.evaluate_step(
        step_no=step_no,
        action=action,
        expected=expected,
        actual_result=result,
        task_context=context
    )

    # 显示评论
    console.print(f"\n[cyan]{comment}[/cyan]\n")


def _build_expected_result(tool_name: str, task: Dict[str, Any]) -> str:
    """根据工具类型构建期望结果描述"""
    if "web" in tool_name.lower() or "browser" in tool_name.lower():
        return "成功访问网页并提取到数据"
    elif "file" in tool_name.lower():
        action = task.get("params", {}).get("action", "")
        if action == "write":
            return "成功保存文件"
        else:
            return "成功读取文件内容"
    elif "system" in tool_name.lower():
        return "成功执行系统命令"
    else:
        return "成功完成任务"


def show_progress_report(task_description: str) -> None:
    """显示任务进度报告"""
    coach = get_ai_coach()
    report = coach.generate_progress_report(task_description)
    console.print(f"\n[yellow]{report}[/yellow]\n")
