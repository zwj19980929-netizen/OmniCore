"""
Helpers for rendering runtime results in the CLI.
"""
from __future__ import annotations

from typing import Any, Dict

from core.statuses import BLOCKED, WAITING_FOR_APPROVAL, WAITING_FOR_EVENT, WAITING_JOB_STATUSES


def build_cli_result_view(result: Dict[str, Any]) -> Dict[str, Any]:
    status = str(result.get("status", "") or "").strip()
    success = bool(result.get("success", False))

    if success:
        body = str(result.get("output") or "Task completed").strip() or "Task completed"
        return {
            "title": "SUCCESS",
            "border_style": "green",
            "body": body,
            "is_failure": False,
        }

    detail = str(
        result.get("output")
        or result.get("error")
        or f"Status: {status or 'unknown'}"
    ).strip() or f"Status: {status or 'unknown'}"

    if status == WAITING_FOR_APPROVAL:
        return {
            "title": "WAITING FOR APPROVAL",
            "border_style": "yellow",
            "body": detail,
            "is_failure": False,
        }
    if status == WAITING_FOR_EVENT:
        return {
            "title": "WAITING FOR EVENT",
            "border_style": "yellow",
            "body": detail,
            "is_failure": False,
        }
    if status == BLOCKED:
        return {
            "title": "BLOCKED",
            "border_style": "yellow",
            "body": detail,
            "is_failure": False,
        }
    if status in WAITING_JOB_STATUSES:
        return {
            "title": status.upper(),
            "border_style": "yellow",
            "body": detail,
            "is_failure": False,
        }

    return {
        "title": "FAILED",
        "border_style": "red",
        "body": detail,
        "is_failure": True,
    }
