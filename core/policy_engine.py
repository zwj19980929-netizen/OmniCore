"""
Deterministic policy evaluation for task execution.

This keeps high-risk confirmation decisions in code instead of relying only on
LLM output.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from core.constants import TaskType
from core.tool_registry import get_builtin_tool_registry


_BROWSER_CONFIRM_TOKENS = (
    "login",
    "sign in",
    "signin",
    "register",
    "submit",
    "upload",
    "delete",
    "buy",
    "purchase",
    "pay",
    "publish",
    "post",
    "send",
    "book",
    "apply",
)
_BROWSER_HIGH_RISK_TOKENS = (
    "delete",
    "buy",
    "purchase",
    "pay",
    "publish",
    "post",
    "send",
)


@dataclass(frozen=True)
class PolicyDecision:
    """Deterministic decision for whether a task needs confirmation."""

    requires_confirmation: bool
    reason: str = ""
    risk_level: str = "medium"
    affected_resources: List[str] = field(default_factory=list)


def _normalize_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _infer_file_action(task: Dict[str, Any]) -> str:
    params = task.get("params", {}) or {}
    action = _normalize_text(params.get("action"))
    if action:
        return action

    description = _normalize_text(task.get("description"))
    if params.get("data_source") or params.get("data_sources"):
        return "write"
    if any(token in description for token in ("save", "write", "export", "generate")):
        return "write"
    return "read"


def evaluate_task_policy(task: Dict[str, Any]) -> PolicyDecision:
    """
    Evaluate whether a task should require human confirmation.

    Rules are deterministic and only depend on the task payload and registered
    tool metadata.
    """
    registry = get_builtin_tool_registry()
    registered_tool = registry.resolve_task(task)

    task_type = str(task.get("task_type", ""))
    tool_name = str(task.get("tool_name", ""))
    if registered_tool is not None:
        task_type = registered_tool.spec.task_type
        tool_name = registered_tool.spec.name
        risk_level = registered_tool.spec.risk_level
    else:
        risk_level = str(task.get("risk_level", "medium") or "medium")

    params = task.get("params", {}) or {}
    description = str(task.get("description", "") or "")
    lowered_description = description.lower()

    if task_type == str(TaskType.SYSTEM_WORKER) or tool_name == "system.control":
        command = str(params.get("command", "")).strip()
        working_dir = str(params.get("working_dir", "")).strip()
        affected = [item for item in (command, working_dir) if item]
        return PolicyDecision(
            requires_confirmation=True,
            reason="system operations always require explicit approval",
            risk_level="high",
            affected_resources=affected,
        )

    if task_type == str(TaskType.FILE_WORKER) or tool_name == "file.read_write":
        action = _infer_file_action(task)
        if action == "write":
            file_path = str(params.get("file_path", "") or "~/Desktop/output.txt")
            return PolicyDecision(
                requires_confirmation=True,
                reason="file write operations require explicit approval",
                risk_level="medium",
                affected_resources=[file_path],
            )
        return PolicyDecision(
            requires_confirmation=False,
            reason="read-only file access",
            risk_level="low",
            affected_resources=[str(params.get("file_path", "")).strip()] if params.get("file_path") else [],
        )

    if task_type == str(TaskType.BROWSER_AGENT) or tool_name == "browser.interact":
        browser_task = " ".join(
            item for item in (
                str(params.get("task", "") or ""),
                description,
            ) if item
        ).lower()
        if any(token in browser_task for token in _BROWSER_CONFIRM_TOKENS):
            risk_level = "high" if any(
                token in browser_task for token in _BROWSER_HIGH_RISK_TOKENS
            ) else "medium"
            affected = [str(params.get("start_url", "")).strip()] if params.get("start_url") else []
            return PolicyDecision(
                requires_confirmation=True,
                reason="interactive browser actions require explicit approval",
                risk_level=risk_level,
                affected_resources=affected,
            )
        return PolicyDecision(
            requires_confirmation=False,
            reason="read-only browser automation",
            risk_level="low",
            affected_resources=[str(params.get("start_url", "")).strip()] if params.get("start_url") else [],
        )

    if task_type == str(TaskType.WEB_WORKER) or tool_name == "web.fetch_and_extract":
        target = str(params.get("url", "")).strip()
        return PolicyDecision(
            requires_confirmation=False,
            reason="read-only web fetch",
            risk_level="low",
            affected_resources=[target] if target else [],
        )

    if "delete" in lowered_description or "remove" in lowered_description:
        return PolicyDecision(
            requires_confirmation=True,
            reason="destructive intent detected in task description",
            risk_level="high",
        )

    return PolicyDecision(
        requires_confirmation=False,
        reason="no deterministic approval rule matched",
        risk_level=risk_level,
    )
