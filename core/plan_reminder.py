"""
Plan execution reminder (R5).

Inspects current task progress and loop state to generate reminder messages
that keep the LLM on track during long-running jobs.
"""

from typing import Optional

from config.settings import Settings
from core.loop_state import LoopState


def generate_reminder(state: dict) -> Optional[str]:
    """Return a plan reminder string, or ``None`` if no reminder is needed.

    Checks:
    1. Stale turns — no task status change for too long.
    2. Low completion rate — too many turns with few tasks done.
    3. All tasks done — nudge toward finalize.
    """
    task_queue = state.get("task_queue", [])
    if not task_queue:
        return None

    loop = LoopState.from_dict(state.get("loop_state", {}))
    turn_count = loop.turn_count
    last_change = loop.last_status_change_turn

    total = len(task_queue)
    completed = sum(1 for t in task_queue if t.get("status") == "completed")
    failed = sum(1 for t in task_queue if t.get("status") == "failed")
    pending = sum(1 for t in task_queue if t.get("status") in ("pending", "queued"))

    reminders = []

    # 1. Stale turns
    stale_turns = turn_count - last_change
    if stale_turns >= Settings.PLAN_REMINDER_INTERVAL and pending > 0:
        reminders.append(
            f"[计划提醒] 已过 {stale_turns} 轮未有任务状态变化。"
            f"当前进度：{completed}/{total} 完成，{pending} 待执行。"
            f"请检查是否有阻塞或需要重新规划。"
        )

    # 2. Low completion rate
    if turn_count > total * 2 and total > 0:
        completion_rate = completed / total
        if completion_rate < 0.3 and failed > 0:
            reminders.append(
                f"[计划提醒] 任务完成率较低（{completed}/{total} = {completion_rate:.0%}），"
                f"已有 {failed} 个任务失败。建议触发重规划。"
            )

    # 3. All tasks done
    if completed == total and total > 0:
        reminders.append(
            "[计划提醒] 所有计划任务已完成，请进行最终输出合成。"
        )

    return "\n".join(reminders) if reminders else None


def update_status_change_turn(state: dict) -> None:
    """Record current turn as the last turn a task status changed."""
    loop = LoopState.from_dict(state.get("loop_state", {}))
    loop.last_status_change_turn = loop.turn_count
    state["loop_state"] = loop.to_dict()
