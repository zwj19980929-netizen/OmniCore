"""
Task plan normalization helpers.

These helpers bridge legacy `task_type` plans and the staged `tool_name`
runtime shape.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict

from core.policy_engine import evaluate_task_policy
from core.state import PolicyDecisionItem, TaskItem, ensure_task_defaults
from core.tool_registry import get_builtin_tool_registry


_COST_BY_TASK_TYPE = {
    "web_worker": "low",
    "system_worker": "low",
    "terminal_worker": "low",
    "file_worker": "medium",
    "browser_agent": "high",
}


def _infer_estimated_cost(task_type: str) -> str:
    """根据 task_type 自动推断执行成本。"""
    return _COST_BY_TASK_TYPE.get(task_type, "medium")


def build_task_item_from_plan(
    task_data: Dict[str, Any],
    *,
    task_id_prefix: str = "task",
    default_priority: int = 5,
) -> TaskItem:
    """Normalize a raw plan entry into a runtime task item."""
    registry = get_builtin_tool_registry()
    registered_tool = registry.resolve_task(task_data)

    params = task_data.get("params")
    if params is None:
        params = task_data.get("tool_args", {})
    if not isinstance(params, dict):
        params = {}

    task_type = task_data.get("task_type", "unknown")
    tool_name = str(task_data.get("tool_name", "") or "")
    risk_level = str(task_data.get("risk_level", "medium") or "medium")

    if registered_tool is not None:
        task_type = registered_tool.spec.task_type
        tool_name = registered_tool.spec.name
        risk_level = registered_tool.spec.risk_level

    task_item = TaskItem(
        task_id=task_data.get("task_id", f"{task_id_prefix}_{uuid.uuid4().hex[:8]}"),
        task_type=task_type,
        description=task_data.get("description", ""),
        params=params,
        status="pending",
        result=None,
        priority=task_data.get("priority", default_priority),
        success_criteria=task_data.get("success_criteria", []),
        fallbacks=task_data.get("fallbacks", []),
        abort_conditions=task_data.get("abort_conditions", []),
        depends_on=task_data.get("depends_on", []),
        required_capabilities=task_data.get("required_capabilities", ["text_chat"]),
        tool_name=tool_name,
        risk_level=risk_level,
        estimated_cost=task_data.get("estimated_cost") or _infer_estimated_cost(task_type),
    )
    ensure_task_defaults(task_item)

    decision = evaluate_task_policy(task_item)
    task_item["requires_confirmation"] = decision.requires_confirmation
    task_item["policy_reason"] = decision.reason
    task_item["affected_resources"] = decision.affected_resources
    task_item["risk_level"] = decision.risk_level
    return task_item


def build_policy_decision_from_task(
    task: TaskItem,
    *,
    decision_override: str | None = None,
    approved_by: str = "",
    approved_at: str = "",
) -> PolicyDecisionItem:
    """Project task-level policy metadata into an auditable decision record."""
    affected_resources = task.get("affected_resources") or []
    primary_target = str(affected_resources[0]).strip() if affected_resources else ""
    action = str(task.get("tool_name") or task.get("task_type") or "").strip()
    requires_confirmation = bool(task.get("requires_confirmation", False))

    decision = decision_override
    if not decision:
        decision = "pending_confirmation" if requires_confirmation else "auto_allow"

    if approved_by and not approved_at:
        approved_at = datetime.now().isoformat(timespec="seconds")

    return PolicyDecisionItem(
        task_id=str(task.get("task_id", "") or ""),
        tool_name=str(task.get("tool_name", "") or ""),
        action=action,
        target_resource=primary_target,
        risk_level=str(task.get("risk_level", "medium") or "medium"),
        decision=decision,
        reason=str(task.get("policy_reason", "") or ""),
        requires_human_confirm=requires_confirmation,
        approved_by=approved_by,
        approved_at=approved_at,
    )
