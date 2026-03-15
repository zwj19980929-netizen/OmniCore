"""
Tool execution diagnostics.

Keep failure reporting concise and factual. This hook must never emit abusive
or role-playing output into the runtime logs.
"""
from typing import Dict, Any

from utils.logger import console


def evaluate_tool_result(
    tool_name: str,
    task: Dict[str, Any],
    result: Dict[str, Any],
    step_no: int
) -> None:
    """
    Print a short diagnostic line for failed tool executions.

    Args:
        tool_name: 工具名称
        task: 任务信息
        result: 执行结果
        step_no: 步骤编号
    """
    if bool(result.get("success")):
        return

    expected = _build_expected_result(tool_name, task)
    description = str(task.get("description", "") or "").strip()
    error = (
        str(result.get("error", "") or "").strip()
        or str(result.get("message", "") or "").strip()
        or "unknown error"
    )
    count = result.get("count")
    count_suffix = f", count={count}" if count is not None else ""
    console.print(
        "\n"
        f"[yellow][ToolFailure][/yellow] step={step_no} tool={tool_name} "
        f"expected={expected} error={error[:240]}{count_suffix}\n"
        f"[dim]{description[:240]}[/dim]\n"
    )


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
    """Emit a concise task progress header."""
    console.print(f"\n[yellow][Progress][/yellow] {str(task_description or '').strip()[:240]}\n")
