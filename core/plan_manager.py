"""
Plan persistence and recovery (R5).

Saves / updates / loads Markdown plan files under ``data/plans/``.
"""

import os
from datetime import datetime
from pathlib import Path

from config.settings import Settings

PLANS_DIR = os.path.join("data", "plans")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def save_plan(
    job_id: str,
    task_queue: list,
    user_input: str = "",
    replan_count: int = 0,
    replan_reason: str = "",
) -> str:
    """Persist *task_queue* as a Markdown plan file.

    On first call for a *job_id* the file is created; subsequent calls with
    a non-empty *replan_reason* append a replan record instead of overwriting.

    Returns the plan file path, or ``""`` if persistence is disabled.
    """
    if not Settings.PLAN_PERSISTENCE_ENABLED:
        return ""

    os.makedirs(PLANS_DIR, exist_ok=True)
    plan_path = os.path.join(PLANS_DIR, f"{job_id}.md")

    if os.path.exists(plan_path) and replan_reason:
        _append_replan_record(plan_path, replan_count, replan_reason, task_queue)
        return plan_path

    # --- first creation ---------------------------------------------------
    title = (user_input[:50] + "...") if len(user_input) > 50 else user_input
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines = [
        f"# 计划: {title}",
        "",
        f"> Job ID: {job_id}",
        f"> 创建时间: {now}",
        "> 状态: executing",
        f"> 重规划次数: {replan_count}",
        "",
        "## 任务列表",
        "",
        "| # | 任务 | 工具 | 状态 | 依赖 |",
        "|---|------|------|------|------|",
    ]

    for i, task in enumerate(task_queue, 1):
        lines.append(_task_row(task, i, with_depends=True))

    lines.append("")

    with open(plan_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return plan_path


def complete_plan(job_id: str) -> None:
    """Mark the plan file as *completed*."""
    plan_path = os.path.join(PLANS_DIR, f"{job_id}.md")
    if not os.path.exists(plan_path):
        return

    with open(plan_path, "r", encoding="utf-8") as f:
        content = f.read()

    content = content.replace("> 状态: executing", "> 状态: completed")

    with open(plan_path, "w", encoding="utf-8") as f:
        f.write(content)


def load_plan(job_id: str) -> str:
    """Return the full Markdown content of a plan file (empty string if absent)."""
    plan_path = os.path.join(PLANS_DIR, f"{job_id}.md")
    if not os.path.exists(plan_path):
        return ""

    with open(plan_path, "r", encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _task_row(task: dict, index: int, *, with_depends: bool = False) -> str:
    desc = task.get("description", "") or task.get("params", {}).get("description", task.get("task_type", ""))
    if isinstance(desc, dict):
        desc = desc.get("description", str(desc))
    desc = str(desc)[:60]
    tool = task.get("tool_name", "") or task.get("task_type", "unknown")
    status = task.get("status", "pending")
    if with_depends:
        depends = ",".join(task.get("depends_on", [])) or "-"
        return f"| {index} | {desc} | {tool} | {status} | {depends} |"
    return f"| {index} | {desc} | {tool} | {status} |"


def _append_replan_record(
    plan_path: str,
    replan_count: int,
    reason: str,
    new_task_queue: list,
) -> None:
    """Append a replan record section to an existing plan file."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Update replan count in header
    with open(plan_path, "r", encoding="utf-8") as f:
        content = f.read()

    old_count_line = f"> 重规划次数: {replan_count - 1}"
    new_count_line = f"> 重规划次数: {replan_count}"
    if old_count_line in content:
        content = content.replace(old_count_line, new_count_line)

    # Build replan section
    section_lines = [""]
    if replan_count == 1:
        section_lines.append("## 重规划记录")
    section_lines += [
        "",
        f"### 重规划 #{replan_count} ({now})",
        f"原因: {reason}",
        "调整后的任务列表:",
        "",
        "| # | 任务 | 工具 | 状态 |",
        "|---|------|------|------|",
    ]

    for i, task in enumerate(new_task_queue, 1):
        section_lines.append(_task_row(task, i))

    section_lines.append("")

    with open(plan_path, "w", encoding="utf-8") as f:
        f.write(content + "\n".join(section_lines))
