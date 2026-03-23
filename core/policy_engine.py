"""
Deterministic policy evaluation for task execution.

This keeps high-risk confirmation decisions in code instead of relying only on
LLM output.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from config.settings import settings
from core.constants import TaskType, TerminalPermissionLevel
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

    # Terminal worker：使用三级权限模型，而非始终确认
    if task_type == str(TaskType.TERMINAL_WORKER) or tool_name in (
        "terminal.execute", "terminal.read_file", "terminal.edit_file", "terminal.search"
    ):
        from agents.terminal_worker import _classify_command_permission
        command = str(params.get("command", "")).strip()
        action = str(params.get("action", "shell")).strip()
        perm = _classify_command_permission(
            command or action,
            mode=settings.TERMINAL_PERMISSION_MODE,
            user_auto_allow=settings.TERMINAL_AUTO_ALLOW_PATTERNS,
            user_always_confirm=settings.TERMINAL_ALWAYS_CONFIRM_PATTERNS,
        )
        if perm == TerminalPermissionLevel.REQUIRE_CONFIRM:
            return PolicyDecision(
                requires_confirmation=True,
                reason="terminal command classified as high-risk",
                risk_level="high",
                affected_resources=[command] if command else [],
            )
        elif perm == TerminalPermissionLevel.NOTIFY:
            return PolicyDecision(
                requires_confirmation=False,
                reason="terminal command notify-level: auto-execute with logging",
                risk_level="medium",
                affected_resources=[command] if command else [],
            )
        else:
            return PolicyDecision(
                requires_confirmation=False,
                reason="terminal command read-only: auto-allow",
                risk_level="low",
                affected_resources=[],
            )

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
        # 只检查纯文本描述，排除 URL（URL 中的 "buy"/"book" 等不代表用户意图）
        import re as _re
        task_text = str(params.get("task", "") or "")
        desc_text = description
        # 去掉 URL 避免误匹配（如 /shop/buy-iphone 中的 buy）
        _url_re = _re.compile(r'https?://\S+', _re.IGNORECASE)
        task_text_clean = _url_re.sub("", task_text).lower()
        desc_text_clean = _url_re.sub("", desc_text).lower()
        browser_task = f"{task_text_clean} {desc_text_clean}"
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

    if tool_name == "api.call" or task_type == "api_worker":
        method = _normalize_text(params.get("method") or "get") or "get"
        target = str(params.get("url", "")).strip()
        if method == "get":
            return PolicyDecision(
                requires_confirmation=False,
                reason="read-only API request",
                risk_level="low",
                affected_resources=[target] if target else [],
            )
        return PolicyDecision(
            requires_confirmation=False,
            reason="mutating API calls use deferred approval before dispatch",
            risk_level="high",
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
