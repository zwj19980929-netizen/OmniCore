"""
Shared current-time helpers for planning, workers, and review.

The runtime publishes ``current_time_context`` on the MessageBus. These helpers
keep every downstream stage using the same shape and wording instead of asking
LLMs to infer what "latest" or "today" means.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, Optional


TIME_SENSITIVE_TOKENS = (
    "latest", "recent", "currently", "current", "today", "tonight", "this week",
    "this month", "this year", "now", "as of", "up-to-date", "newest",
    "最新", "最近", "近期", "当前", "目前", "今天", "今日", "今晚",
    "本周", "这个月", "本月", "今年", "截至", "截至目前", "当下",
)


def build_current_time_context(now: Optional[datetime] = None) -> Dict[str, str]:
    current = (now or datetime.now().astimezone()).astimezone()
    timezone_name = current.tzname() or str(current.tzinfo or "")
    return {
        "iso_datetime": current.isoformat(timespec="seconds"),
        "local_date": current.strftime("%Y-%m-%d"),
        "local_time": current.strftime("%H:%M:%S"),
        "weekday": current.strftime("%A"),
        "timezone": timezone_name,
        "current_year": current.strftime("%Y"),
    }


def normalize_current_time_context(
    current_time_context: Optional[Dict[str, Any]] = None,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, str]:
    base = build_current_time_context(now=now)
    if not isinstance(current_time_context, dict):
        return base

    normalized = dict(base)
    for key in ("iso_datetime", "local_date", "local_time", "weekday", "timezone", "current_year"):
        value = str(current_time_context.get(key, "") or "").strip()
        if value:
            normalized[key] = value

    local_date = normalized.get("local_date", "")
    try:
        normalized["current_year"] = str(date.fromisoformat(local_date).year)
    except Exception:
        normalized["current_year"] = str(normalized.get("current_year") or base["current_year"])
    return normalized


def current_year(current_time_context: Optional[Dict[str, Any]] = None) -> int:
    context = normalize_current_time_context(current_time_context)
    try:
        return int(context.get("current_year") or context.get("local_date", "")[:4])
    except Exception:
        return datetime.now().year


def is_time_sensitive_text(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(token in lowered for token in TIME_SENSITIVE_TOKENS)


def build_time_context_prompt(current_time_context: Optional[Dict[str, Any]] = None) -> str:
    context = normalize_current_time_context(current_time_context)
    lines = [
        "Current local time context (authoritative):",
        f"- Current datetime: {context.get('iso_datetime', '')}",
        f"- Current date: {context.get('local_date', '')}",
        f"- Current year: {context.get('current_year', '')}",
    ]
    if context.get("local_time"):
        lines.append(f"- Current local time: {context['local_time']}")
    if context.get("weekday"):
        lines.append(f"- Weekday: {context['weekday']}")
    if context.get("timezone"):
        lines.append(f"- Timezone: {context['timezone']}")
    lines.append(
        "For latest/today/recent/current tasks, resolve relative dates against this context. "
        "Do not present older information as current unless the cited sources prove it is still current."
    )
    return "\n".join(line for line in lines if line.strip())


def build_temporal_task_instruction(current_time_context: Optional[Dict[str, Any]] = None) -> str:
    context = normalize_current_time_context(current_time_context)
    return (
        f"时间约束：当前本地日期是 {context.get('local_date')}，当前年份是 {context.get('current_year')}，"
        f"时区是 {context.get('timezone') or '本地时区'}。"
        "将“最新/最近/当前/今天/latest/recent/current/today”等相对时间词按该日期解释；"
        "检索和报告必须优先使用当前或最近来源，并明确来源日期/链接。"
    )


def append_temporal_instruction_if_needed(
    text: str,
    current_time_context: Optional[Dict[str, Any]] = None,
    *,
    force: bool = False,
) -> str:
    value = str(text or "")
    if not force and not is_time_sensitive_text(value):
        return value
    instruction = build_temporal_task_instruction(current_time_context)
    if instruction in value or "时间约束：当前本地日期是" in value:
        return value
    separator = "\n\n" if value else ""
    return f"{value}{separator}{instruction}"
