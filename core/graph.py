"""
OmniCore LangGraph DAG 编排
将所有 Agent 串联成完整的执行图
支持 Worker 失败后反思重规划
"""
import json
import re
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse
from typing import Any, Dict, List, Literal
from langgraph.graph import StateGraph, END

from core.statuses import BLOCKED, WAITING_FOR_APPROVAL, WAITING_FOR_EVENT
from core.state import OmniCoreState
from core.router import RouterAgent
from core.task_planner import build_policy_decision_from_task, build_task_item_from_plan
from agents.critic import CriticAgent
from agents.validator import Validator
from core.llm import LLMClient
from core.task_executor import collect_ready_task_indexes, run_ready_batch
from core.stage_registry import register_stage, StageRegistry
from utils.logger import log_agent_action, log_success, log_error, log_warning
from utils.human_confirm import HumanConfirm
from utils.prompt_manager import get_prompt
from utils.url_utils import sanitize_extracted_url
from utils.web_result_normalizer import canonicalize_item, infer_requested_fields


# 初始化所有 Agent
router_agent = RouterAgent()
critic_agent = CriticAgent()
validator_agent = Validator()

MAX_REPLAN = 3  # 最多重规划 3 次（给 Replanner 足够空间做策略转换）

_CHECKPOINT_STAGE_ORDER = {
    "route": 1,
    "human_confirm": 2,
    "parallel_executor": 3,
    "validator": 4,
    "critic": 5,
    "replanner": 6,
    "finalize": 7,
}

_MOJIBAKE_MARKERS = (
    "\u00c3",
    "\u00c2",
    "\u00e6",
    "\u00e5",
    "\u00e4",
    "\u00e7",
    "\u00e8",
    "\u00e9",
    "\u00ea",
    "\u00ef",
    "\u00f0",
)

def _build_finalize_time_hint(current_time_context) -> str:
    if not isinstance(current_time_context, dict):
        return ""

    lines = []
    iso_datetime = str(current_time_context.get("iso_datetime", "") or "").strip()
    local_date = str(current_time_context.get("local_date", "") or "").strip()
    local_time = str(current_time_context.get("local_time", "") or "").strip()
    weekday = str(current_time_context.get("weekday", "") or "").strip()
    timezone_name = str(current_time_context.get("timezone", "") or "").strip()

    if iso_datetime:
        lines.append(f"- Current datetime: {iso_datetime}")
    if local_date:
        lines.append(f"- Current date: {local_date}")
    if local_time:
        lines.append(f"- Current local time: {local_time}")
    if weekday:
        lines.append(f"- Weekday: {weekday}")
    if timezone_name:
        lines.append(f"- Timezone: {timezone_name}")

    if not lines:
        return ""
    return "\n\nCurrent local time (authoritative):\n" + "\n".join(lines)


def _build_finalize_location_hint(current_location_context) -> str:
    if not isinstance(current_location_context, dict):
        return ""

    lines = []
    location_name = str(current_location_context.get("location", "") or "").strip()
    timezone_name = str(current_location_context.get("timezone", "") or "").strip()
    source_name = str(current_location_context.get("source", "") or "").strip()

    if location_name:
        lines.append(f"- User location: {location_name}")
    if timezone_name:
        lines.append(f"- Location timezone: {timezone_name}")
    if source_name:
        lines.append(f"- Source: {source_name}")

    if not lines:
        return ""
    return "\n\nCurrent user location (authoritative):\n" + "\n".join(lines)


def _looks_like_mojibake(text: str) -> bool:
    if not isinstance(text, str) or len(text) < 6:
        return False
    marker_hits = sum(text.count(marker) for marker in _MOJIBAKE_MARKERS)
    if marker_hits < 2:
        return False
    cjk_count = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk_count == 0


def _repair_mojibake_text(text: str) -> str:
    if not isinstance(text, str) or not text:
        return text
    if not _looks_like_mojibake(text):
        return text
    try:
        repaired = text.encode("latin-1", errors="ignore").decode("utf-8", errors="ignore")
    except Exception:
        return text
    if not repaired or repaired == text:
        return text
    repaired_cjk = sum(1 for ch in repaired if "\u4e00" <= ch <= "\u9fff")
    if repaired_cjk == 0:
        return text
    return repaired


def _normalize_text_value(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return _repair_mojibake_text(text)


def _normalize_payload(value: Any) -> Any:
    if isinstance(value, str):
        return _repair_mojibake_text(value)
    if isinstance(value, list):
        return [_normalize_payload(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_normalize_payload(item) for item in value)
    if isinstance(value, dict):
        normalized: Dict[Any, Any] = {}
        for key, item in value.items():
            normalized[key] = _normalize_payload(item)
        return normalized
    return value


def _payload_preview(payload: Any, limit: int = 220) -> str:
    normalized = _normalize_payload(payload)
    try:
        if isinstance(normalized, (dict, list, tuple)):
            text = json.dumps(normalized, ensure_ascii=False, default=str)
        else:
            text = str(normalized)
    except Exception:
        text = str(normalized)
    return text.replace("\n", " ")[:limit]


def _extract_structured_findings(state: OmniCoreState, max_items: int = 5) -> str:
    title_keys = ("title", "headline", "name", "subject", "text")
    date_keys = ("date", "time", "datetime", "published_at", "published")
    link_keys = ("link", "url", "source_url", "article_url")
    detail_keys = ("summary", "description", "desc", "snippet", "content", "text")

    findings: List[Dict[str, str]] = []
    seen = set()
    for task in state.get("task_queue", []) or []:
        if task.get("status") != "completed":
            continue
        result = task.get("result")
        if not isinstance(result, dict):
            continue
        for source_key in ("data", "items", "content"):
            payload = result.get(source_key)
            if payload in (None, "", [], {}):
                continue
            records = payload if isinstance(payload, list) else [payload]
            for record in records:
                if not isinstance(record, dict):
                    text = _normalize_text_value(record)
                    if not text:
                        continue
                    key = (text[:120], "")
                    if key in seen:
                        continue
                    seen.add(key)
                    findings.append({"title": text, "date": "", "link": "", "detail": ""})
                    continue

                title = next((_normalize_text_value(record.get(key)) for key in title_keys if record.get(key)), "")
                date = next((_normalize_text_value(record.get(key)) for key in date_keys if record.get(key)), "")
                link = next((_normalize_text_value(record.get(key)) for key in link_keys if record.get(key)), "")
                detail = next((_normalize_text_value(record.get(key)) for key in detail_keys if record.get(key)), "")
                if not title and detail:
                    title = detail[:120]
                if not title:
                    continue
                key = (title[:120], link[:160])
                if key in seen:
                    continue
                seen.add(key)
                findings.append(
                    {
                        "title": title,
                        "date": date,
                        "link": link,
                        "detail": detail if detail and detail != title else "",
                    }
                )

    if not findings:
        return ""

    lines = ["Findings:"]
    for idx, item in enumerate(findings[:max_items], 1):
        title = item.get("title", "")
        date = item.get("date", "")
        link = item.get("link", "")
        detail = item.get("detail", "")
        line = f"{idx}. {title}"
        if date:
            line += f" ({date})"
        if link:
            line += f" - {link}"
        lines.append(line)
        if detail:
            lines.append(f"   {detail[:180]}")
    if len(findings) > max_items:
        lines.append(f"... {len(findings) - max_items} more item(s) extracted.")
    return "\n".join(lines)


def _extract_requested_item_count(text: str) -> int:
    source = str(text or "")
    patterns = (
        r"前\s*(\d+)\s*(?:条|个|项|篇)?",
        r"top\s*(\d+)",
        r"(\d+)\s*(?:items?|results?|links?|headlines?|stories|repositories|repos|models?)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, source, flags=re.IGNORECASE)
        if not match:
            continue
        try:
            value = int(match.group(1))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0


def _looks_like_explicit_list_output_request(text: str) -> bool:
    lowered = str(text or "").lower()
    list_tokens = (
        "抓取前",
        "列出",
        "列表",
        "清单",
        "名称和链接",
        "标题和链接",
        "前 ",
        "前",
        "top",
        "list",
        "links",
        "titles",
        "headlines",
        "models",
        "repositories",
        "repos",
    )
    return any(token in lowered for token in list_tokens) or _extract_requested_item_count(text) > 1


def _escape_markdown_cell(value: Any) -> str:
    text = _normalize_text_value(value)
    if not text:
        return ""
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _collect_best_completed_record_list(state: OmniCoreState) -> List[Dict[str, Any]]:
    best_records: List[Dict[str, Any]] = []
    for task in state.get("task_queue", []) or []:
        if not isinstance(task, dict) or str(task.get("status", "") or "") != "completed":
            continue
        result = task.get("result")
        if not isinstance(result, dict):
            continue
        for source_key in ("data", "items", "content"):
            payload = result.get(source_key)
            if not isinstance(payload, list):
                continue
            dict_records = [item for item in payload if isinstance(item, dict)]
            if len(dict_records) > len(best_records):
                best_records = dict_records
    return best_records


def _build_deterministic_list_answer(state: OmniCoreState, package: Dict[str, Any]) -> str:
    if not bool(state.get("critic_approved", False)):
        return ""
    if str(package.get("review_status", "") or "") != "approved":
        return ""
    if package.get("issues"):
        return ""

    task_descriptions = [
        str(task.get("description", "") or "")
        for task in state.get("task_queue", []) or []
        if isinstance(task, dict)
    ]
    request_context = "\n".join(
        part for part in [str(state.get("user_input", "") or "").strip(), *task_descriptions] if part
    )
    if not _looks_like_explicit_list_output_request(request_context):
        return ""

    raw_records = _collect_best_completed_record_list(state)
    if len(raw_records) < 2:
        return ""

    requested_fields = infer_requested_fields(request_context, {"page_type": "list"})
    normalized_records: List[Dict[str, Any]] = []
    for raw in raw_records:
        item = canonicalize_item(raw, requested_fields)
        if not item:
            continue
        if not item.get("title") and not item.get("url") and not item.get("link"):
            continue
        normalized_records.append(item)

    if len(normalized_records) < 2:
        return ""

    requested_count = _extract_requested_item_count(request_context)
    render_count = len(normalized_records)
    if requested_count > 0:
        if len(normalized_records) < requested_count:
            return ""
        render_count = min(len(normalized_records), requested_count)
    render_count = min(render_count, 100)
    rows = normalized_records[:render_count]
    if not rows:
        return ""

    preferred_columns = ["title", "url", "date", "summary", "author", "source", "location"]
    column_labels = {
        "title": "标题",
        "url": "链接",
        "date": "日期",
        "summary": "摘要",
        "author": "作者",
        "source": "来源",
        "location": "地点",
    }
    requested_set = {field for field in requested_fields if field in column_labels}
    if "title" not in requested_set:
        requested_set.add("title")
    if any(item.get("url") or item.get("link") for item in rows):
        requested_set.add("url")

    columns = [
        field for field in preferred_columns
        if field in requested_set and any(_escape_markdown_cell(item.get(field)) for item in rows)
    ]
    if not columns:
        return ""

    lines = [f"根据当前抓取到的信息，已提取 {render_count} 条结果：", ""]
    header = "| 序号 | " + " | ".join(column_labels[field] for field in columns) + " |"
    divider = "| --- | " + " | ".join("---" for _ in columns) + " |"
    lines.append(header)
    lines.append(divider)

    for idx, item in enumerate(rows, 1):
        cells: List[str] = []
        for field in columns:
            value = item.get(field, "")
            if field == "url" and not value:
                value = item.get("link", "")
            cells.append(_escape_markdown_cell(value))
        lines.append(f"| {idx} | " + " | ".join(cells) + " |")

    return "\n".join(lines)


def _save_runtime_checkpoint(state: OmniCoreState, stage: str, note: str = "") -> None:
    session_id = str(state.get("session_id", "") or "").strip()
    job_id = str(state.get("job_id", "") or "").strip()
    if not session_id or not job_id:
        return

    try:
        from utils.runtime_state_store import get_runtime_state_store

        get_runtime_state_store().save_checkpoint(
            session_id=session_id,
            job_id=job_id,
            stage=stage,
            state=state,
            note=note,
        )
    except Exception as exc:
        log_warning(f"Runtime checkpoint persistence failed: {exc}")


def _derive_authoritative_target_url(state: OmniCoreState) -> str:
    def _is_generic_entry_url(value: str) -> bool:
        candidate = str(value or "").strip()
        if not candidate:
            return False
        parsed = urlparse(candidate)
        host = parsed.netloc.lower()
        path = (parsed.path or "").rstrip("/")
        normalized = candidate.lower()
        if host.startswith("www."):
            host = host[4:]
        if any(token in normalized for token in ("/ok.html", "/captcha", "/verify", "/challenge", "/forbidden", "/blocked")):
            return True
        if host in {"google.com", "bing.com", "baidu.com", "duckduckgo.com", "sogou.com"}:
            return True
        if host in {"weather.com.cn", "moji.com", "tianqi.com"} and path in {"", "/index", "/index.html"}:
            return True
        return False

    direct_url = RouterAgent._extract_first_url(str(state.get("user_input", "") or ""))
    if direct_url:
        return direct_url

    for task in state.get("task_queue", []) or []:
        if not isinstance(task, dict):
            continue
        params = task.get("params", {}) if isinstance(task.get("params"), dict) else {}
        result = task.get("result", {}) if isinstance(task.get("result"), dict) else {}
        for candidate in (
            result.get("expected_url"),
            params.get("start_url"),
            params.get("url"),
            result.get("url"),
        ):
            value = sanitize_extracted_url(candidate)
            if value and not _is_generic_entry_url(value):
                return value
    return ""


def _repair_replan_task_params(
    tasks: List[Dict[str, Any]],
    target_url: str,
) -> List[Dict[str, Any]]:
    target_url = sanitize_extracted_url(target_url)
    if not target_url:
        return tasks

    repaired = []
    for raw_task in tasks or []:
        task_data = dict(raw_task)
        params = task_data.get("params")
        if not isinstance(params, dict):
            params = {}

        tool_name = str(task_data.get("tool_name", "") or "").strip()
        task_type = str(task_data.get("task_type", "") or "").strip()
        if (
            (tool_name == "browser.interact" or task_type == "browser_agent")
            and not str(params.get("start_url", "") or "").strip()
        ):
            params = dict(params)
            params["start_url"] = target_url
            task_data["params"] = params
        elif (
            tool_name in {"web.fetch_and_extract", "web.smart_extract"}
            and not str(params.get("url", "") or "").strip()
        ):
            params = dict(params)
            params["url"] = target_url
            task_data["params"] = params

        repaired.append(task_data)

    return repaired


_SYSTEM_EXECUTION_PARAM_KEYS = (
    "command",
    "application",
    "args",
    "working_directory",
)


def _has_actionable_system_params(params: Any) -> bool:
    if not isinstance(params, dict):
        return False

    for key in _SYSTEM_EXECUTION_PARAM_KEYS:
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, (list, tuple)) and any(str(item).strip() for item in value):
            return True
    return False


def _extract_finalize_instructions_from_replan_tasks(
    tasks: List[Dict[str, Any]],
) -> tuple[List[Dict[str, Any]], List[str]]:
    executable_tasks: List[Dict[str, Any]] = []
    finalize_instructions: List[str] = []

    for raw_task in tasks or []:
        if not isinstance(raw_task, dict):
            continue

        task_data = dict(raw_task)
        tool_name = str(task_data.get("tool_name", "") or "").strip()
        task_type = str(task_data.get("task_type", "") or "").strip()
        params = task_data.get("params")
        if params is None:
            params = task_data.get("tool_args", {})

        is_system_task = tool_name == "system.control" or task_type == "system_worker"
        if is_system_task and not _has_actionable_system_params(params):
            instruction = _normalize_text_value(task_data.get("description", ""))
            if instruction:
                finalize_instructions.append(instruction)
            continue

        executable_tasks.append(task_data)

    return executable_tasks, finalize_instructions


def _is_task_preservable_for_replan(task: Dict[str, Any]) -> bool:
    if not isinstance(task, dict):
        return False
    if str(task.get("status", "") or "") != "completed":
        return False
    return bool(task.get("critic_approved", False))


def _build_replan_failure_record(task: Dict[str, Any]) -> Dict[str, Any]:
    result = task.get("result", {}) if isinstance(task.get("result"), dict) else {}
    review = task.get("critic_review", {}) if isinstance(task.get("critic_review"), dict) else {}
    params = task.get("params", {}) if isinstance(task.get("params"), dict) else {}

    expected_url = (
        sanitize_extracted_url(result.get("expected_url"))
        or sanitize_extracted_url(params.get("start_url"))
        or sanitize_extracted_url(params.get("url"))
        or ""
    )
    visited_url = result.get("url") or ""
    error = result.get("error") or result.get("message") or ""
    failure_type = str(task.get("failure_type", "") or "unknown")

    if str(task.get("status", "") or "") == "completed" and not bool(task.get("critic_approved", False)):
        failure_type = "critic_rejected"
        review_issues = review.get("issues", []) if isinstance(review.get("issues"), list) else []
        error = error or "; ".join(str(item) for item in review_issues if str(item).strip())
        error = error or str(review.get("summary", "") or "critic rejected the task result")

    if not error:
        error = "unknown error"

    url = expected_url or visited_url or ""
    if expected_url and visited_url and visited_url != expected_url:
        url = f"{visited_url} (expected {expected_url})"

    return {
        "url": url,
        "expected_url": str(expected_url or "").strip(),
        "visited_url": str(visited_url or "").strip(),
        "error": str(error),
        "failure_type": failure_type,
    }


def _should_skip_for_resume(state: OmniCoreState, stage: str) -> bool:
    shared_memory = state.get("shared_memory", {})
    if not isinstance(shared_memory, dict):
        return False

    resume_after = str(shared_memory.get("_resume_after_stage", "") or "").strip()
    if not resume_after:
        return False

    target_index = _CHECKPOINT_STAGE_ORDER.get(resume_after)
    current_index = _CHECKPOINT_STAGE_ORDER.get(stage)
    if target_index is None or current_index is None:
        shared_memory.pop("_resume_after_stage", None)
        return False

    if current_index <= target_index:
        return True

    shared_memory.pop("_resume_after_stage", None)
    return False


def _task_statuses(state: OmniCoreState) -> set[str]:
    return {
        str(task.get("status", "") or "")
        for task in state.get("task_queue", []) or []
        if isinstance(task, dict)
    }


def _has_waiting_tasks(state: OmniCoreState) -> bool:
    statuses = _task_statuses(state)
    return any(status in statuses for status in (WAITING_FOR_APPROVAL, WAITING_FOR_EVENT, BLOCKED))


def _mark_confirmation_required_tasks_waiting(state: OmniCoreState) -> None:
    shared_memory = state.get("shared_memory", {})
    if not isinstance(shared_memory, dict):
        shared_memory = {}
        state["shared_memory"] = shared_memory

    approved_actions = {
        str(item).strip()
        for item in (shared_memory.get("_approved_actions", []) or [])
        if str(item).strip()
    }

    has_waiting = False
    for task in state.get("task_queue", []) or []:
        if not isinstance(task, dict):
            continue

        task_id = str(task.get("task_id", "") or "")
        status = str(task.get("status", "") or "")
        requires_confirmation = bool(task.get("requires_confirmation", False))
        already_approved = task_id in approved_actions or bool(state.get("human_approved", False))

        if requires_confirmation and not already_approved:
            if status in {"", "pending", "running"}:
                task["status"] = WAITING_FOR_APPROVAL
                status = WAITING_FOR_APPROVAL
            if status == WAITING_FOR_APPROVAL:
                has_waiting = True

    state["needs_human_confirm"] = has_waiting
    if has_waiting and not collect_ready_task_indexes(state):
        state["execution_status"] = WAITING_FOR_APPROVAL


@register_stage(name="router", order=10, required=True)
def route_node(state: OmniCoreState) -> OmniCoreState:
    """Route user request: analyze intent and decompose into sub-tasks."""
    if _should_skip_for_resume(state, "route"):
        return state
    state = router_agent.route(state)
    _save_runtime_checkpoint(state, "route", "Router completed")
    return state


@register_stage(name="parallel_executor", order=30, required=True, depends_on=("router",))
def parallel_executor_node(state: OmniCoreState) -> OmniCoreState:
    """Batch executor: run all ready tasks in the current batch."""
    if _should_skip_for_resume(state, "parallel_executor"):
        return state
    if collect_ready_task_indexes(state):
        state["execution_status"] = "executing"
    state = run_ready_batch(state)
    _save_runtime_checkpoint(state, "parallel_executor", "Executed ready task batch")
    return state


def _legacy_replanner_node_v2(state: OmniCoreState) -> OmniCoreState:
    """反思重规划节点：分析失败原因，制定新策略"""
    if _should_skip_for_resume(state, "replanner"):
        return state
    state["replan_count"] = state.get("replan_count", 0) + 1
    log_agent_action("Replanner", f"开始反思重规划（第 {state['replan_count']} 次）")

    # 🔥 加入鞭策机制：分析失败并给予严厉反馈
    from utils.agent_critic import AgentCritic
    from utils.logger import console

    critic = AgentCritic()

    # 收集失败信息用于批评
    failure_result = {
        "success": False,
        "completed_tasks": len([t for t in state["task_queue"] if t.get("status") == "completed"]),
        "total_tasks": len(state["task_queue"]),
        "task": state.get("user_input", ""),
        "output": "",
        "error": ""
    }

    # 从任务队列中提取错误信息
    for task in state["task_queue"]:
        result = task.get("result", {})
        if task.get("status") == "failed":
            failure_result["error"] += str(result.get("error", "")) + " "
            failure_result["output"] += str(result.get("output", "")) + " "
        # 也检查执行轨迹中的错误
        for step in task.get("execution_trace", []):
            if step.get("observation"):
                failure_result["output"] += str(step.get("observation", "")) + " "

    # 🔥 无论如何都要显示批评！
    issues = critic.analyze_failure(failure_result)

    # 强制显示批评报告
    console.print("\n" + "="*80)
    console.print("[bold red]🚨 AGENT 性能审查报告 🚨[/bold red]")
    console.print("="*80 + "\n")

    if issues:
        pua_report = critic.generate_pua_report(issues, state["replan_count"])
        console.print(f"[red]{pua_report}[/red]\n")
    else:
        # 即使没有检测到具体问题，也要批评
        console.print("[red]💩 **又失败了！** 虽然我不知道你具体哪里搞砸了，但失败就是失败！[/red]")
        console.print(f"[red]📊 完成进度: {failure_result['completed_tasks']}/{failure_result['total_tasks']} 任务[/red]")
        console.print(f"[red]🤦 这是第 {state['replan_count']} 次重试了，能不能争点气？[/red]\n")

    # 如果失败 3 次，给出替代策略建议
    if state["replan_count"] >= 3:
        # 传入 LLM 客户端，让 critic 动态生成建议
        from core.llm import LLMClient
        llm_client = LLMClient()
        alternative = critic.suggest_alternative_strategy(
            state.get("user_input", ""),
            state["replan_count"],
            llm_client=llm_client
        )
        console.print(f"[yellow]{alternative}[/yellow]\n")

    console.print("="*80 + "\n")

    # 如果已经是最后一次重规划，标记为"必须给出答案"模式
    is_final_attempt = state["replan_count"] >= MAX_REPLAN
    authoritative_target_url = _derive_authoritative_target_url(state)

    # 记录本轮失败策略到历史（防止 Replanner 兜圈子）
    replan_history = state.get("shared_memory", {}).get("_replan_history", [])

    # 收集失败信息（包含已尝试的路径，帮助 Replanner 避免重蹈覆辙）
    failures = []
    tried_urls = []
    current_strategies = []
    preserved_tasks = []
    for task in state["task_queue"]:
        if _is_task_preservable_for_replan(task):
            preserved_tasks.append(task)
            continue

        if task["status"] == "failed" or (
            task["status"] == "completed" and not bool(task.get("critic_approved", False))
        ):
            failure_record = _build_replan_failure_record(task)
            url = failure_record["url"]
            error = failure_record["error"]
            failure_type = failure_record["failure_type"]
            error = result.get("error", "未知错误")
            url = result.get("url") or task.get("params", {}).get("url") or task.get("params", {}).get("start_url") or ""
            error = result.get("error") or result.get("message") or error
            url = expected_url or visited_url or url
            if expected_url and visited_url and visited_url != expected_url:
                url = f"{visited_url} (expected {expected_url})"
            failure_type = task.get("failure_type", "unknown")
            trace_summary = ""
            for step in task.get("execution_trace", [])[-3:]:
                trace_summary += f"\n    step {step.get('step_no')}: {step.get('plan')} → {step.get('observation', '')[:80]}"
            failures.append(
                f"- 任务: {task['description']}\n  访问的URL: {url}\n  失败类型: {failure_type}\n  失败原因: {error}{trace_summary}"
            )
            if url:
                tried_urls.append(expected_url or visited_url or url)
            current_strategies.append(f"{task['task_type']}: {task['description'][:80]}")

    # 把本轮策略加入历史
    replan_history.append({
        "round": state["replan_count"],
        "strategies": current_strategies,
        "urls": tried_urls,
    })
    state["shared_memory"]["_replan_history"] = replan_history

    failure_summary = "\n".join(failures) if failures else "无明确失败信息，但任务结果不符合预期"
    if tried_urls:
        failure_summary += f"\n\n已尝试过的URL（不要再访问）：{', '.join(tried_urls)}"

    # 构建历史策略摘要，让 Replanner 知道之前都试过什么
    if authoritative_target_url:
        failure_summary += (
            f"\n\nAuthoritative target URL: {authoritative_target_url}"
            "\nIf this is still the user's target page, preserve this URL and change the extraction or waiting strategy before changing sites."
        )

    history_summary = ""
    if len(replan_history) > 1:
        history_summary = "\n\n## 之前已经尝试过的策略（绝对不要重复）：\n"
        for h in replan_history[:-1]:
            history_summary += f"第 {h['round']} 轮：\n"
            for s in h["strategies"]:
                history_summary += f"  - {s}\n"
            if h["urls"]:
                history_summary += f"  访问过的URL: {', '.join(h['urls'])}\n"

    # 🔥 先让 AI PUA Coach 喷一顿，指出问题
    from utils.ai_pua_coach import get_ai_coach
    from utils.logger import console

    coach = get_ai_coach()

    # 构建失败任务的描述
    failed_task_description = f"重规划第 {state['replan_count']} 次"
    failed_action = "执行任务并尝试访问网页"
    expected_result = "成功获取数据并完成用户需求"

    # 构建实际结果
    actual_result = {
        "success": False,
        "error": failure_summary,
        "output": f"失败次数: {len(failures)}, 尝试过的URL: {', '.join(tried_urls) if tried_urls else '无'}"
    }

    # 让 PUA Coach 评估并生成批评
    pua_comment = coach.evaluate_step(
        step_no=state['replan_count'],
        action=failed_action,
        expected=expected_result,
        actual_result=actual_result,
        task_context=state.get('user_input', '')
    )

    # 显示 PUA 批评
    console.print(f"\n{pua_comment}\n")

    # 让 LLM 分析失败原因并重新规划
    from utils.prompt_manager import get_prompt
    llm = LLMClient()
    replanner_prompt = get_prompt("replanner_system")

    # 🔥 将 PUA Coach 的批评传递给 Replanner，让它知道问题所在
    user_message_with_pua = f"""用户原始需求：{state['user_input']}

失败的任务：
{failure_summary}{history_summary}

## AI PUA Coach 的失败分析：
{pua_comment}

这是第 {state['replan_count']} 次重规划（{'最后一次，必须给出明确答案' if is_final_attempt else '请提出和之前所有尝试都不同的新策略'}）。
请根据 PUA Coach 指出的问题，制定新的策略。"""

    response = llm.chat_with_system(
        system_prompt=replanner_prompt,
        user_message=user_message_with_pua,
        temperature=0.3,
        json_mode=True,
    )

    try:
        result = llm.parse_json_response(response)
        repaired_tasks = _repair_replan_task_params(
            result.get("tasks", []),
            authoritative_target_url,
        )
        result["tasks"], finalize_instructions = _extract_finalize_instructions_from_replan_tasks(
            repaired_tasks
        )
        if finalize_instructions:
            shared_memory["_final_answer_instructions"] = finalize_instructions
        else:
            shared_memory.pop("_final_answer_instructions", None)
        log_agent_action("Replanner", f"分析: {result.get('analysis', '')[:80]}")

        # 检查是否应该放弃并直接回答用户
        if result.get("should_give_up", False):
            log_warning(f"Replanner 决定放弃: {result.get('give_up_reason', '')}")
            state["final_output"] = result.get("direct_answer", "抱歉，无法完成您的请求。")
            state["execution_status"] = "completed"
            state["critic_approved"] = True
            state["task_queue"] = []  # 清空任务队列，直接结束
            return state

        log_agent_action("Replanner", f"新策略: {result.get('new_strategy', '')[:80]}")

        # 用新任务替换失败的任务
        new_tasks = []
        for task_data in result.get("tasks", []):
            new_tasks.append(
                build_task_item_from_plan(
                    task_data,
                    task_id_prefix="replan",
                    default_priority=10,
                )
            )

        if new_tasks:
            # 不保留之前的任务，因为既然触发了 Replanner，说明之前的任务都不满足要求
            # 保留它们只会导致 Critic 重复审查并持续失败
            state["task_queue"] = new_tasks
            state["policy_decisions"] = [
                build_policy_decision_from_task(task)
                for task in new_tasks
            ]
            state["needs_human_confirm"] = any(
                task.get("requires_confirmation", False) for task in new_tasks
            )
            state["human_approved"] = not state["needs_human_confirm"]
            _mark_confirmation_required_tasks_waiting(state)
            state["error_trace"] = ""
            log_success(f"重规划完成，新增 {len(new_tasks)} 个任务（已清空旧任务）")
        else:
            log_warning("重规划未产生新任务")

    except Exception as e:
        log_error(f"重规划失败: {e}")

    from langchain_core.messages import SystemMessage
    state["messages"].append(
        SystemMessage(content=f"Replanner 重规划完成（第 {state['replan_count']} 次）")
    )
    _save_runtime_checkpoint(state, "replanner", "Replanner completed")

    return state


# encoding-health: ignore-start
def _legacy_replanner_node(state: OmniCoreState) -> OmniCoreState:
    """Reflect on failed execution and produce a better next plan."""
    if _should_skip_for_resume(state, "replanner"):
        return state

    state["replan_count"] = state.get("replan_count", 0) + 1
    log_agent_action("Replanner", f"寮€濮嬪弽鎬濋噸瑙勫垝锛堢 {state['replan_count']} 娆★級")

    is_final_attempt = state["replan_count"] >= MAX_REPLAN
    authoritative_target_url = _derive_authoritative_target_url(state)
    replan_history = state.get("shared_memory", {}).get("_replan_history", [])

    failures: List[str] = []
    tried_urls: List[str] = []
    current_strategies: List[str] = []
    preserved_tasks: List[Dict[str, Any]] = []

    for task in state.get("task_queue", []) or []:
        if _is_task_preservable_for_replan(task):
            preserved_tasks.append(task)
            continue

        status = str(task.get("status", "") or "")
        critic_rejected = status == "completed" and not bool(task.get("critic_approved", False))
        if status not in {"failed", "completed"} or (status == "completed" and not critic_rejected):
            continue

        failure_record = _build_replan_failure_record(task)
        trace_summary = ""
        for step in task.get("execution_trace", [])[-3:]:
            trace_summary += (
                f"\n    step {step.get('step_no')}: {step.get('plan')} "
                f"鈫?{step.get('observation', '')[:80]}"
            )

        failures.append(
            f"- 浠诲姟: {task['description']}\n"
            f"  璁块棶鐨刄RL: {failure_record['url']}\n"
            f"  澶辫触绫诲瀷: {failure_record['failure_type']}\n"
            f"  澶辫触鍘熷洜: {failure_record['error']}{trace_summary}"
        )

        tried_url = (
            failure_record["expected_url"]
            or failure_record["visited_url"]
            or failure_record["url"]
        )
        if tried_url:
            tried_urls.append(tried_url)
        current_strategies.append(
            f"{task.get('task_type', '')}: {str(task.get('description', '') or '')[:80]}"
        )

    replan_history.append(
        {
            "round": state["replan_count"],
            "strategies": current_strategies,
            "urls": tried_urls,
        }
    )
    state["shared_memory"]["_replan_history"] = replan_history

    failure_summary = (
        "\n".join(failures)
        if failures
        else "鏃犳槑纭け璐ヤ俊鎭紝浣嗙幇鏈夌粨鏋滀笉绗﹀悎棰勬湡"
    )
    if tried_urls:
        failure_summary += f"\n\n宸插皾璇曡繃鐨刄RL锛堜笉瑕佸啀璁块棶锛夛細{', '.join(tried_urls)}"
    if authoritative_target_url:
        failure_summary += (
            f"\n\nAuthoritative target URL: {authoritative_target_url}"
            "\nIf the user still wants this page, keep the URL and change extraction, waiting, or navigation strategy before switching sites."
        )

    history_summary = ""
    if len(replan_history) > 1:
        history_summary = "\n\n## Previous failed strategies (do not repeat)\n"
        for item in replan_history[:-1]:
            history_summary += f"Round {item['round']}:\n"
            for strategy in item.get("strategies", []):
                history_summary += f"- {strategy}\n"
            if item.get("urls"):
                history_summary += f"Visited URLs: {', '.join(item['urls'])}\n"

    llm = LLMClient()
    replanner_en_prompt = get_prompt("replanner_system_en")
    response = llm.chat_with_system(
        system_prompt=replanner_en_prompt,
        user_message=(
            f"User request: {state['user_input']}\n\n"
            f"Failure summary:\n{failure_summary}{history_summary}\n\n"
            f"Replan round: {state['replan_count']} "
            f"({'final attempt' if is_final_attempt else 'more retries allowed'})"
        ),
        temperature=0.3,
        json_mode=True,
    )

    try:
        result = llm.parse_json_response(response)
        result["tasks"] = _repair_replan_task_params(
            result.get("tasks", []),
            authoritative_target_url,
        )
        log_agent_action("Replanner", f"鍒嗘瀽: {str(result.get('analysis', '') or '')[:80]}")

        if result.get("should_give_up", False):
            log_warning(f"Replanner 鍐冲畾鏀惧純: {result.get('give_up_reason', '')}")
            state["final_output"] = result.get("direct_answer", "鎶辨瓑锛屾棤娉曞畬鎴愭偍鐨勮姹傘€?")
            state["execution_status"] = "completed_with_issues"
            state["critic_approved"] = False
            state["task_queue"] = preserved_tasks
            state["policy_decisions"] = [
                build_policy_decision_from_task(task)
                for task in state["task_queue"]
            ]
            return state

        log_agent_action("Replanner", f"鏂扮瓥鐣? {str(result.get('new_strategy', '') or '')[:80]}")

        new_tasks = [
            build_task_item_from_plan(
                task_data,
                task_id_prefix="replan",
                default_priority=10,
            )
            for task_data in result.get("tasks", [])
        ]

        if new_tasks or preserved_tasks:
            state["task_queue"] = preserved_tasks + new_tasks
            state["policy_decisions"] = [
                build_policy_decision_from_task(task)
                for task in state["task_queue"]
            ]
            state["needs_human_confirm"] = any(
                task.get("requires_confirmation", False)
                for task in state["task_queue"]
            )
            state["human_approved"] = not state["needs_human_confirm"]
            _mark_confirmation_required_tasks_waiting(state)
            state["error_trace"] = ""
            log_success(
                f"閲嶈鍒掑畬鎴愶紝淇濈暀 {len(preserved_tasks)} 涓粨鏋滐紝鏂板 {len(new_tasks)} 涓换鍔?"
            )
        else:
            log_warning("閲嶈鍒掓湭浜х敓鏂颁换鍔?")

    except Exception as exc:
        log_error(f"閲嶈鍒掑け璐? {exc}")

    from langchain_core.messages import SystemMessage

    state["messages"].append(
        SystemMessage(content=f"Replanner 閲嶈鍒掑畬鎴愶紙绗?{state['replan_count']} 娆★級")
    )
    _save_runtime_checkpoint(state, "replanner", "Replanner completed")
    return state


# encoding-health: ignore-end
@register_stage(
    name="critic", order=50, required=False,
    depends_on=("validator",),
    skip_condition="state.get('validator_passed') == False",
)
def critic_node(state: OmniCoreState) -> OmniCoreState:
    """Critic review node: evaluate task output quality."""
    if _should_skip_for_resume(state, "critic"):
        return state
    state = critic_agent.review(state)
    _save_runtime_checkpoint(state, "critic", "Critic review completed")
    return state


@register_stage(name="validator", order=40, required=False, depends_on=("parallel_executor",))
def validator_node(state: OmniCoreState) -> OmniCoreState:
    """Hard-rule validation node."""
    if _should_skip_for_resume(state, "validator"):
        return state
    state = validator_agent.validate(state)
    _save_runtime_checkpoint(state, "validator", "Validator completed")
    return state


def human_confirm_node(state: OmniCoreState) -> OmniCoreState:
    """人类确认节点"""
    if state["needs_human_confirm"] and not state["human_approved"]:
        confirmed = HumanConfirm.request_confirmation(
            operation="执行任务队列",
            details=f"即将执行 {len(state['task_queue'])} 个任务",
            affected_items=[t["description"] for t in state["task_queue"]],
        )
        state["human_approved"] = confirmed
        if not confirmed:
            state["execution_status"] = "cancelled"
            state["error_trace"] = "用户取消执行"
            state["final_output"] = "操作已取消，任务队列未执行。"
    else:
        state["human_approved"] = True
    return state


def _sync_policy_decisions_after_confirmation(
    state: OmniCoreState,
    *,
    approved: bool,
) -> None:
    timestamp = datetime.now().isoformat(timespec="seconds")
    decisions = []
    existing = {
        str(item.get("task_id", "") or ""): dict(item)
        for item in state.get("policy_decisions", []) or []
        if isinstance(item, dict) and item.get("task_id")
    }

    for task in state.get("task_queue", []) or []:
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("task_id", "") or "")
        current = existing.get(task_id) or dict(build_policy_decision_from_task(task))
        if bool(current.get("requires_human_confirm", False)):
            current["decision"] = "approved" if approved else "rejected"
            current["approved_by"] = "user"
            current["approved_at"] = timestamp
        decisions.append(current)

    state["policy_decisions"] = decisions


@register_stage(
    name="human_confirm", order=20, required=False,
    depends_on=("router",),
    skip_condition="not state.get('needs_human_confirm')",
)
def human_confirm_node_v2(state: OmniCoreState) -> OmniCoreState:
    """Deterministic-policy aware human confirmation node."""
    if _should_skip_for_resume(state, "human_confirm"):
        return state
    user_preferences = state.get("shared_memory", {}).get("user_preferences", {})
    auto_queue_confirmations = bool(
        isinstance(user_preferences, dict) and user_preferences.get("auto_queue_confirmations", False)
    )
    if auto_queue_confirmations and state["needs_human_confirm"] and not state["human_approved"]:
        state["human_approved"] = True
        _sync_policy_decisions_after_confirmation(state, approved=True)
        _save_runtime_checkpoint(state, "human_confirm", "Auto-approved by user preference")
        return state
    if state["needs_human_confirm"] and not state["human_approved"]:
        flagged_tasks = [
            task for task in state["task_queue"] if task.get("requires_confirmation", False)
        ]
        tasks_for_review = flagged_tasks or state["task_queue"]
        affected_items = []
        for task in tasks_for_review:
            reason = str(task.get("policy_reason", "") or "").strip()
            if reason:
                affected_items.append(f"{task['description']} [{reason}]")
            else:
                affected_items.append(task["description"])

        details = f"About to execute {len(state['task_queue'])} task(s)."
        if flagged_tasks:
            details += f" {len(flagged_tasks)} task(s) were flagged by deterministic policy."

        router_risk_reason = str(
            state.get("shared_memory", {}).get("router_high_risk_reason", "") or ""
        ).strip()
        if router_risk_reason:
            details += f" Router risk signal: {router_risk_reason}"

        confirmed = HumanConfirm.request_confirmation(
            operation="Execute planned task queue",
            details=details,
            affected_items=affected_items,
        )
        state["human_approved"] = confirmed
        _sync_policy_decisions_after_confirmation(state, approved=confirmed)
        if not confirmed:
            state["execution_status"] = "cancelled"
            state["error_trace"] = "User cancelled execution"
            state["final_output"] = "Execution cancelled before running the queued tasks."
    else:
        state["human_approved"] = True
    _save_runtime_checkpoint(state, "human_confirm", "Human confirmation handled")
    return state


def _collect_delivery_artifacts(state: OmniCoreState):
    artifacts = []
    seen = set()
    path_keys = ("file_path", "path", "output_path", "download_path", "screenshot_path")

    for artifact in state.get("artifacts", []) or []:
        if not isinstance(artifact, dict):
            continue
        fingerprint = (
            str(artifact.get("path", "") or ""),
            str(artifact.get("name", "") or ""),
            str(artifact.get("artifact_type", "") or ""),
        )
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        artifacts.append(dict(artifact))

    for task in state.get("task_queue", []) or []:
        result = task.get("result")
        if not isinstance(result, dict):
            continue

        for source_key in path_keys:
            raw_path = str(result.get(source_key, "") or "").strip()
            if not raw_path:
                continue
            artifact_type = "file"
            if source_key == "screenshot_path":
                artifact_type = "image"
            elif source_key == "download_path":
                artifact_type = "download"
            fingerprint = (raw_path, source_key, artifact_type)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            artifacts.append(
                {
                    "task_id": task.get("task_id", ""),
                    "task_type": task.get("task_type", ""),
                    "tool_name": task.get("tool_name", ""),
                    "artifact_type": artifact_type,
                    "source_key": source_key,
                    "path": raw_path,
                    "name": Path(raw_path).name or raw_path,
                }
            )

        for source_key in ("data", "items", "content"):
            payload = result.get(source_key)
            if payload in (None, "", [], {}):
                continue
            preview = _payload_preview(payload)
            fingerprint = ("inline", source_key, preview)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            artifacts.append(
                {
                    "task_id": task.get("task_id", ""),
                    "task_type": task.get("task_type", ""),
                    "tool_name": task.get("tool_name", ""),
                    "artifact_type": "structured_data",
                    "source_key": source_key,
                    "path": "",
                    "name": f"{task.get('task_id', 'task')}_{source_key}",
                    "preview": preview,
                }
            )

    return artifacts


def _build_delivery_package(state: OmniCoreState) -> dict:
    tasks = state.get("task_queue", []) or []
    completed = [task for task in tasks if task.get("status") == "completed"]
    failed = [task for task in tasks if task.get("status") == "failed"]
    waiting_approval = [task for task in tasks if task.get("status") == WAITING_FOR_APPROVAL]
    waiting_event = [task for task in tasks if task.get("status") == WAITING_FOR_EVENT]
    blocked = [task for task in tasks if task.get("status") == BLOCKED]
    pending = [
        task for task in tasks
        if task.get("status") not in {"completed", "failed", WAITING_FOR_APPROVAL, WAITING_FOR_EVENT, BLOCKED}
    ]
    artifacts = _collect_delivery_artifacts(state)

    deliverables = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        deliverables.append(
            {
                "artifact_type": str(artifact.get("artifact_type", "") or "artifact"),
                "name": str(artifact.get("name", "") or "artifact"),
                "location": str(artifact.get("path", "") or artifact.get("preview", "") or "").strip(),
                "task_id": str(artifact.get("task_id", "") or ""),
                "tool_name": str(artifact.get("tool_name", "") or ""),
            }
        )

    issues = []
    for task in failed:
        result = task.get("result") if isinstance(task.get("result"), dict) else {}
        error_message = result.get("error") or result.get("message") or "Unknown error"
        issues.append(
            {
                "task_id": str(task.get("task_id", "") or ""),
                "description": str(task.get("description", "") or task.get("task_id", "") or "Failed task"),
                "error": str(error_message),
            }
        )
    for task in waiting_approval:
        issues.append(
            {
                "task_id": str(task.get("task_id", "") or ""),
                "description": str(task.get("description", "") or task.get("task_id", "") or "Approval needed"),
                "error": "Waiting for approval",
            }
        )
    for task in waiting_event:
        issues.append(
            {
                "task_id": str(task.get("task_id", "") or ""),
                "description": str(task.get("description", "") or task.get("task_id", "") or "Waiting for event"),
                "error": "Waiting for external event",
            }
        )
    for task in blocked:
        issues.append(
            {
                "task_id": str(task.get("task_id", "") or ""),
                "description": str(task.get("description", "") or task.get("task_id", "") or "Blocked task"),
                "error": "Task is blocked",
            }
        )
    for task in pending:
        issues.append(
            {
                "task_id": str(task.get("task_id", "") or ""),
                "description": str(task.get("description", "") or task.get("task_id", "") or "Pending task"),
                "error": f"Task status is {task.get('status', 'pending')}",
            }
        )

    review_status = "approved" if state.get("critic_approved") else "needs_attention"
    work_context = state.get("shared_memory", {}).get("work_context", {})
    goal = work_context.get("goal", {}) if isinstance(work_context, dict) else {}
    project = work_context.get("project", {}) if isinstance(work_context, dict) else {}
    todo = work_context.get("todo", {}) if isinstance(work_context, dict) else {}
    open_todos = work_context.get("open_todos", []) if isinstance(work_context, dict) else []
    if not tasks:
        headline = "Answered directly without executing worker tasks."
    elif waiting_approval:
        headline = f"{len(waiting_approval)} task(s) are prepared and waiting for approval."
    elif waiting_event:
        headline = f"{len(waiting_event)} task(s) are waiting for an external event."
    elif blocked:
        headline = f"{len(blocked)} task(s) are blocked and require manual intervention."
    elif not issues:
        headline = f"Completed all {len(completed)} planned task(s)."
    else:
        headline = f"Completed {len(completed)} of {len(tasks)} task(s); follow-up review is recommended."

    recommended_next_step = ""
    if waiting_approval:
        recommended_next_step = "Review and approve the waiting action to continue execution."
    elif waiting_event:
        recommended_next_step = "Wait for the watched event or adjust the event source."
    elif blocked:
        recommended_next_step = "Unblock or rerun the blocked task after resolving the issue."
    elif failed:
        recommended_next_step = "Review the failed task(s) or retry from the latest checkpoint."
    elif pending:
        recommended_next_step = "Resume the unfinished task(s) to complete the workflow."
    elif review_status != "approved":
        recommended_next_step = "Review the critic feedback before reusing this result."

    return {
        "headline": headline,
        "intent": str(state.get("current_intent", "") or ""),
        "review_status": review_status,
        "goal": {
            "goal_id": str(goal.get("goal_id", "") or ""),
            "title": str(goal.get("title", "") or ""),
        },
        "project": {
            "project_id": str(project.get("project_id", "") or ""),
            "title": str(project.get("title", "") or ""),
        },
        "todo": {
            "todo_id": str(todo.get("todo_id", "") or ""),
            "title": str(todo.get("title", "") or ""),
            "status": str(todo.get("status", "") or ""),
        },
        "completed_task_count": len(completed),
        "total_task_count": len(tasks),
        "completed_tasks": [
            str(task.get("description", "") or task.get("task_id", "") or "Completed task")
            for task in completed
        ],
        "deliverables": deliverables,
        "issues": issues,
        "critic_feedback": str(state.get("critic_feedback", "") or "").strip(),
        "recommended_next_step": recommended_next_step,
        "open_todos": [
            {
                "todo_id": str(item.get("todo_id", "") or ""),
                "title": str(item.get("title", "") or ""),
                "status": str(item.get("status", "") or ""),
            }
            for item in open_todos[:8]
            if isinstance(item, dict)
        ],
    }


def _build_delivery_summary(state: OmniCoreState) -> str:
    package = _build_delivery_package(state)
    state["artifacts"] = _collect_delivery_artifacts(state)
    state["delivery_package"] = package

    lines = [
        package["headline"],
        f"Review status: {package['review_status']}",
        f"Progress: {package['completed_task_count']}/{package['total_task_count']} task(s) completed.",
    ]

    findings_summary = _extract_structured_findings(state)
    if findings_summary:
        lines.append("")
        lines.append(findings_summary)

    completed_tasks = package.get("completed_tasks", [])
    if completed_tasks:
        lines.append("")
        lines.append("Completed work:")
        for item in completed_tasks:
            lines.append(f"- {item}")

    deliverables = package.get("deliverables", [])
    if deliverables:
        lines.append("")
        lines.append("Deliverables:")
        for item in deliverables[:8]:
            if item.get("location"):
                lines.append(f"- [{item.get('artifact_type', 'artifact')}] {item.get('name', 'artifact')}: {item.get('location')}")
            else:
                lines.append(f"- [{item.get('artifact_type', 'artifact')}] {item.get('name', 'artifact')}")

    issues = package.get("issues", [])
    if issues:
        lines.append("")
        lines.append("Open issues:")
        for item in issues[:8]:
            lines.append(f"- {item.get('description', 'Issue')}: {item.get('error', 'Unknown error')}")

    critic_feedback = package.get("critic_feedback", "")
    if critic_feedback:
        lines.append("")
        lines.append(f"Review note: {critic_feedback}")

    next_step = package.get("recommended_next_step", "")
    if next_step:
        lines.append("")
        lines.append(f"Recommended next step: {next_step}")

    open_todos = package.get("open_todos", [])
    if open_todos:
        lines.append("")
        lines.append("Pending work:")
        for item in open_todos:
            lines.append(f"- {item.get('title', 'Todo')} [{item.get('status', 'pending')}]")

    return "\n".join(lines)


def _should_keep_delivery_summary_as_final_output(
    state: OmniCoreState,
    package: Dict[str, Any],
) -> bool:
    statuses = {
        str(task.get("status", "") or "")
        for task in state.get("task_queue", []) or []
        if isinstance(task, dict)
    }
    if WAITING_FOR_APPROVAL in statuses or WAITING_FOR_EVENT in statuses or BLOCKED in statuses:
        return True
    return int(package.get("completed_task_count", 0) or 0) <= 0


def _build_execution_evidence_for_answer(state: OmniCoreState) -> str:
    sections: List[str] = []

    findings_summary = _extract_structured_findings(state, max_items=8)
    if findings_summary:
        sections.append(findings_summary)

    task_result_lines = []
    for task in state.get("task_queue", []) or []:
        if not isinstance(task, dict):
            continue
        if str(task.get("status", "") or "") != "completed":
            continue
        result = task.get("result")
        if result in (None, "", [], {}):
            continue
        task_result_lines.append(
            f"- {str(task.get('description', '') or task.get('task_id', '') or 'Completed task')}: "
            f"{_payload_preview(result, limit=500)}"
        )
        if len(task_result_lines) >= 4:
            break
    if task_result_lines:
        sections.append("Completed task results:\n" + "\n".join(task_result_lines))

    return "\n\n".join(section for section in sections if section).strip()


def _legacy_synthesize_user_facing_answer(
    state: OmniCoreState,
    delivery_summary: str,
) -> str:
    package = state.get("delivery_package", {}) or {}
    if not isinstance(package, dict):
        package = {}
    if _should_keep_delivery_summary_as_final_output(state, package):
        return delivery_summary

    deterministic_list_answer = _build_deterministic_list_answer(state, package)
    if deterministic_list_answer:
        return deterministic_list_answer

    evidence = _build_execution_evidence_for_answer(state)
    if not evidence:
        return delivery_summary

    shared_memory = state.get("shared_memory", {})
    if not isinstance(shared_memory, dict):
        shared_memory = {}

    current_time_context = shared_memory.get("current_time_context")
    current_location_context = shared_memory.get("current_location_context")
    final_answer_instructions = shared_memory.get("_final_answer_instructions", [])
    if not isinstance(final_answer_instructions, list):
        final_answer_instructions = [final_answer_instructions]
    instruction_lines = [
        f"- {str(item).strip()}"
        for item in final_answer_instructions
        if str(item).strip()
    ]
    deliverables = package.get("deliverables", []) or []
    issues = package.get("issues", []) or []
    completed_tasks = package.get("completed_tasks", []) or []

    deliverable_lines = []
    for item in deliverables[:5]:
        if not isinstance(item, dict):
            continue
        location = str(item.get("location", "") or "").strip()
        label = str(item.get("name", "") or item.get("artifact_type", "") or "deliverable")
        if location:
            deliverable_lines.append(f"- {label}: {location}")
        else:
            deliverable_lines.append(f"- {label}")

    issue_lines = []
    for item in issues[:5]:
        if not isinstance(item, dict):
            continue
        issue_lines.append(
            f"- {str(item.get('description', '') or item.get('task_id', '') or 'Issue')}: "
            f"{str(item.get('error', '') or 'Unknown error')}"
        )

    completed_lines = [
        f"- {str(item or '').strip()}"
        for item in completed_tasks[:5]
        if str(item or "").strip()
    ]

    try:
        llm = LLMClient()
        response = llm.chat_with_system(
            system_prompt=get_prompt("finalize_answer_detailed"),
            user_message=(
                f"用户原始问题：{state.get('user_input', '')}\n\n"
                f"执行结果概览：{package.get('headline', '')}\n\n"
                f"提炼出的证据：\n{evidence}\n\n"
                f"已完成的工作：\n{chr(10).join(completed_lines) if completed_lines else '- 无'}\n\n"
                f"可交付产物：\n{chr(10).join(deliverable_lines) if deliverable_lines else '- 无'}\n\n"
                f"未解决问题：\n{chr(10).join(issue_lines) if issue_lines else '- 无'}"
                f"{_build_finalize_time_hint(current_time_context)}"
                f"{_build_finalize_location_hint(current_location_context)}"
            ),
            temperature=0.4,
        )
        synthesized = str(getattr(response, "content", "") or "").strip()
        if synthesized:
            return synthesized
    except Exception:
        pass

    return delivery_summary


def _legacy_finalize_node(state: OmniCoreState) -> OmniCoreState:
    """最终输出节点"""
    if _should_skip_for_resume(state, "finalize"):
        return state
    if not state["task_queue"]:
        shared_memory = state.get("shared_memory", {})
        if not isinstance(shared_memory, dict):
            shared_memory = {}
        current_time_context = shared_memory.get("current_time_context")
        current_location_context = shared_memory.get("current_location_context")
        direct_answer = str(shared_memory.get("router_direct_answer", "") or "").strip()

        if direct_answer:
            state["final_output"] = direct_answer
            state["execution_status"] = "completed"
            state["critic_approved"] = True
            state["delivery_package"] = {
                "headline": "Answered directly without worker execution.",
                "intent": str(state.get("current_intent", "") or ""),
                "review_status": "approved",
                "completed_task_count": 0,
                "total_task_count": 0,
                "completed_tasks": [],
                "deliverables": [],
                "issues": [],
                "critic_feedback": "",
                "recommended_next_step": "",
            }
            _save_runtime_checkpoint(state, "finalize", "Finalize completed without task queue")
            return state

        router_reasoning = ""
        for msg in reversed(state.get("messages", [])):
            content = getattr(msg, "content", "")
            if "Router 分析完成" in content:
                router_reasoning = content.replace("Router 分析完成: ", "")
                break
        if False:
            llm = LLMClient()
            response = llm.chat_with_system(
                system_prompt=get_prompt("finalize_answer"),
                user_message=(
                    f"用户问题：{state['user_input']}\n\n"
                    f"分析结果：{router_reasoning}"
                    f"{_build_finalize_time_hint(current_time_context)}"
                    f"{_build_finalize_location_hint(current_location_context)}"
                ),
                temperature=0.7,
            )
            state["final_output"] = response.content
        if True:
            state["final_output"] = (
                router_reasoning
                or "这次没有形成可执行计划，也没有拿到可验证的结果，所以我不能直接给你事实性答案。请重试，或明确要查询的目标。"
            )
            """
            state["final_output"] = router_reasoning or "抱歉，我没有理解你的意思，请再说一次。"
            """
        state["execution_status"] = "completed"
        state["critic_approved"] = True
        state["delivery_package"] = {
            "headline": "Answered directly without worker execution.",
            "intent": str(state.get("current_intent", "") or ""),
            "review_status": "approved",
            "completed_task_count": 0,
            "total_task_count": 0,
            "completed_tasks": [],
            "deliverables": [],
            "issues": [],
            "critic_feedback": "",
            "recommended_next_step": "",
        }
        _save_runtime_checkpoint(state, "finalize", "Finalize completed without task queue")
        return state

    results = []
    for task in state["task_queue"]:
        if task["status"] == "completed":
            results.append(f"✅ {task['description']}")
        elif task["status"] == "failed":
            results.append(f"❌ {task['description']}: {task.get('result', {}).get('error', '未知错误')}")

    delivery_summary = _build_delivery_summary(state)
    state["final_output"] = _synthesize_user_facing_answer(state, delivery_summary)

    statuses = {str(task.get("status", "") or "") for task in state["task_queue"]}
    if WAITING_FOR_APPROVAL in statuses:
        state["execution_status"] = WAITING_FOR_APPROVAL
    elif WAITING_FOR_EVENT in statuses:
        state["execution_status"] = WAITING_FOR_EVENT
    elif BLOCKED in statuses:
        state["execution_status"] = BLOCKED
    elif state["critic_approved"]:
        state["execution_status"] = "completed"
        log_success("所有任务执行完成")
    else:
        state["execution_status"] = "completed_with_issues"
        log_error(f"任务未通过审查: {state['critic_feedback']}")

    _save_runtime_checkpoint(state, "finalize", "Finalize completed")
    return state


# === 条件路由函数 ===

# encoding-health: ignore-start
def _legacy_finalize_node_v2(state: OmniCoreState) -> OmniCoreState:
    """Build the final user-facing output from direct answers or executed tasks."""
    if _should_skip_for_resume(state, "finalize"):
        return state

    if not state["task_queue"]:
        shared_memory = state.get("shared_memory", {})
        if not isinstance(shared_memory, dict):
            shared_memory = {}

        direct_answer = str(shared_memory.get("router_direct_answer", "") or "").strip()
        if direct_answer:
            state["final_output"] = direct_answer
            state["execution_status"] = "completed"
            state["critic_approved"] = True
            state["delivery_package"] = {
                "headline": "Answered directly without worker execution.",
                "intent": str(state.get("current_intent", "") or ""),
                "review_status": "approved",
                "completed_task_count": 0,
                "total_task_count": 0,
                "completed_tasks": [],
                "deliverables": [],
                "issues": [],
                "critic_feedback": "",
                "recommended_next_step": "",
            }
            _save_runtime_checkpoint(state, "finalize", "Finalize completed without task queue")
            return state

        router_reasoning = ""
        for msg in reversed(state.get("messages", [])):
            content = getattr(msg, "content", "")
            if "Router 鍒嗘瀽瀹屾垚" in content:
                router_reasoning = content.replace("Router 鍒嗘瀽瀹屾垚: ", "")
                break

        final_output = (
            "杩欐娌℃湁褰㈡垚鍙墽琛岃鍒掞紝涔熸病鏈夋嬁鍒板彲楠岃瘉鐨勭粨鏋滐紝鎵€浠ユ垜涓嶈兘鐩存帴缁欎綘浜嬪疄鎬х瓟妗堛€傝閲嶈瘯锛屾垨鏄庣‘瑕佹煡璇㈢殑鐩爣銆?"
        )
        if router_reasoning:
            final_output += f"\n\nSystem note: {router_reasoning}"

        state["final_output"] = final_output
        state["execution_status"] = "completed_with_issues"
        state["critic_approved"] = False
        state["delivery_package"] = {
            "headline": "No verified result was produced.",
            "intent": str(state.get("current_intent", "") or ""),
            "review_status": "needs_attention",
            "completed_task_count": 0,
            "total_task_count": 0,
            "completed_tasks": [],
            "deliverables": [],
            "issues": [
                {
                    "task_id": "",
                    "description": "No executable plan or verifiable result",
                    "error": router_reasoning or "Router did not produce a valid executable answer.",
                }
            ],
            "critic_feedback": router_reasoning or "No verifiable result was produced.",
            "recommended_next_step": "Retry the request, or specify the exact target/source to query.",
        }
        _save_runtime_checkpoint(state, "finalize", "Finalize completed without task queue")
        return state

    delivery_summary = _build_delivery_summary(state)
    state["final_output"] = _synthesize_user_facing_answer(state, delivery_summary)

    statuses = {str(task.get("status", "") or "") for task in state["task_queue"]}
    if WAITING_FOR_APPROVAL in statuses:
        state["execution_status"] = WAITING_FOR_APPROVAL
    elif WAITING_FOR_EVENT in statuses:
        state["execution_status"] = WAITING_FOR_EVENT
    elif BLOCKED in statuses:
        state["execution_status"] = BLOCKED
    elif state["critic_approved"]:
        state["execution_status"] = "completed"
        log_success("鎵€鏈変换鍔℃墽琛屽畬鎴?")
    else:
        state["execution_status"] = "completed_with_issues"
        log_error(f"浠诲姟鏈€氳繃瀹℃煡: {state['critic_feedback']}")

    _save_runtime_checkpoint(state, "finalize", "Finalize completed")
    return state


# encoding-health: ignore-end
def should_continue_after_route(state: OmniCoreState) -> Literal["human_confirm", "finalize"]:
    if not state["task_queue"]:
        return "finalize"
    return "human_confirm"


def get_first_executor(state: OmniCoreState) -> Literal["parallel_executor", "validator", "end"]:
    if str(state.get("execution_status", "") or "") == "cancelled":
        return "end"
    if collect_ready_task_indexes(state):
        return "parallel_executor"
    if _has_waiting_tasks(state):
        return "validator"
    if not state["human_approved"]:
        return "end"
    return "validator"


def after_validator(state: OmniCoreState) -> Literal["critic", "replanner", "finalize"]:
    """Validator 之后：passed → critic，failed + replan_count < MAX → replanner，否则 finalize"""
    if _has_waiting_tasks(state):
        return "finalize"
    if state.get("validator_passed", True):
        return "critic"
    if any(str(task.get("status", "") or "") == "completed" for task in state.get("task_queue", [])):
        return "critic"
    if state.get("replan_count", 0) < MAX_REPLAN:
        return "replanner"
    return "finalize"


def should_retry_or_finish(state: OmniCoreState) -> Literal["finalize", "replanner"]:
    """Critic 审查后决定是否重试"""
    if _has_waiting_tasks(state):
        return "finalize"
    if state["critic_approved"]:
        return "finalize"
    if state.get("replan_count", 0) < MAX_REPLAN:
        return "replanner"
    return "finalize"


def after_parallel_executor(state: OmniCoreState) -> Literal["parallel_executor", "validator"]:
    if collect_ready_task_indexes(state):
        return "parallel_executor"
    return "validator"


def _synthesize_user_facing_answer(
    state: OmniCoreState,
    delivery_summary: str,
) -> str:
    package = state.get("delivery_package", {}) or {}
    if not isinstance(package, dict):
        package = {}
    if _should_keep_delivery_summary_as_final_output(state, package):
        return delivery_summary

    deterministic_list_answer = _build_deterministic_list_answer(state, package)
    if deterministic_list_answer:
        return deterministic_list_answer

    evidence = _build_execution_evidence_for_answer(state)
    if not evidence:
        return delivery_summary

    shared_memory = state.get("shared_memory", {})
    if not isinstance(shared_memory, dict):
        shared_memory = {}

    current_time_context = shared_memory.get("current_time_context")
    current_location_context = shared_memory.get("current_location_context")
    final_answer_instructions = shared_memory.get("_final_answer_instructions", [])
    if not isinstance(final_answer_instructions, list):
        final_answer_instructions = [final_answer_instructions]
    instruction_lines = [
        f"- {str(item).strip()}"
        for item in final_answer_instructions
        if str(item).strip()
    ]
    deliverables = package.get("deliverables", []) or []
    issues = package.get("issues", []) or []
    completed_tasks = package.get("completed_tasks", []) or []

    deliverable_lines = []
    for item in deliverables[:5]:
        if not isinstance(item, dict):
            continue
        location = str(item.get("location", "") or "").strip()
        label = str(item.get("name", "") or item.get("artifact_type", "") or "deliverable")
        if location:
            deliverable_lines.append(f"- {label}: {location}")
        else:
            deliverable_lines.append(f"- {label}")

    issue_lines = []
    for item in issues[:5]:
        if not isinstance(item, dict):
            continue
        issue_lines.append(
            f"- {str(item.get('description', '') or item.get('task_id', '') or 'Issue')}: "
            f"{str(item.get('error', '') or 'Unknown error')}"
        )

    completed_lines = [
        f"- {str(item or '').strip()}"
        for item in completed_tasks[:5]
        if str(item or "").strip()
    ]

    try:
        llm = LLMClient()
        response = llm.chat_with_system(
            system_prompt=(
                "You are OmniCore's user-facing answer synthesizer.\n"
                "Write a direct answer for the user based only on executed evidence.\n"
                "Do not mention internal runtime components such as Router, Worker, Critic, "
                "Validator, task queue, or delivery package.\n"
                "If the evidence is partial, say so explicitly.\n"
                "If files or artifacts were produced, briefly mention where they are.\n"
                "If answer guidance is provided, follow it only when the executed evidence supports it."
            ),
            user_message=(
                f"Original user request:\n{state.get('user_input', '')}\n\n"
                f"Execution headline:\n{package.get('headline', '')}\n\n"
                f"Evidence:\n{evidence}\n\n"
                f"Answer guidance:\n{chr(10).join(instruction_lines) if instruction_lines else '- None'}\n\n"
                f"Completed work:\n{chr(10).join(completed_lines) if completed_lines else '- None'}\n\n"
                f"Deliverables:\n{chr(10).join(deliverable_lines) if deliverable_lines else '- None'}\n\n"
                f"Open issues:\n{chr(10).join(issue_lines) if issue_lines else '- None'}"
                f"{_build_finalize_time_hint(current_time_context)}"
                f"{_build_finalize_location_hint(current_location_context)}"
            ),
            temperature=0.4,
        )
        synthesized = _normalize_text_value(getattr(response, "content", ""))
        if synthesized:
            return synthesized
    except Exception:
        pass

    return delivery_summary


@register_stage(
    name="replanner", order=35, required=False,
    depends_on=("parallel_executor",),
    skip_condition="state.get('replan_count', 0) >= 3",
)
def replanner_node(state: OmniCoreState) -> OmniCoreState:
    """Reflect on failed execution and produce a better next plan."""
    if _should_skip_for_resume(state, "replanner"):
        return state

    shared_memory = state.get("shared_memory", {})
    if not isinstance(shared_memory, dict):
        shared_memory = {}
        state["shared_memory"] = shared_memory

    state["replan_count"] = state.get("replan_count", 0) + 1
    log_agent_action("Replanner", f"开始反思重规划（第 {state['replan_count']} 次）")

    is_final_attempt = state["replan_count"] >= MAX_REPLAN
    authoritative_target_url = _derive_authoritative_target_url(state)
    replan_history = shared_memory.get("_replan_history", [])

    failures: List[str] = []
    tried_urls: List[str] = []
    current_strategies: List[str] = []
    preserved_tasks: List[Dict[str, Any]] = []

    for task in state.get("task_queue", []) or []:
        if _is_task_preservable_for_replan(task):
            preserved_tasks.append(task)
            continue

        status = str(task.get("status", "") or "")
        critic_rejected = status == "completed" and not bool(task.get("critic_approved", False))
        if status not in {"failed", "completed"} or (status == "completed" and not critic_rejected):
            continue

        failure_record = _build_replan_failure_record(task)
        trace_summary = ""
        for step in task.get("execution_trace", [])[-3:]:
            trace_summary += (
                f"\n    step {step.get('step_no')}: {step.get('plan')} "
                f"-> {step.get('observation', '')[:80]}"
            )

        failures.append(
            f"- 任务: {task['description']}\n"
            f"  访问 URL: {failure_record['url']}\n"
            f"  失败类型: {failure_record['failure_type']}\n"
            f"  失败原因: {failure_record['error']}{trace_summary}"
        )

        tried_url = (
            failure_record["expected_url"]
            or failure_record["visited_url"]
            or failure_record["url"]
        )
        if tried_url:
            tried_urls.append(tried_url)
        current_strategies.append(
            f"{task.get('task_type', '')}: {str(task.get('description', '') or '')[:80]}"
        )

    replan_history.append(
        {
            "round": state["replan_count"],
            "strategies": current_strategies,
            "urls": tried_urls,
        }
    )
    shared_memory["_replan_history"] = replan_history

    failure_summary = (
        "\n".join(failures)
        if failures
        else "没有拿到明确的失败证据，但当前结果仍然不满足用户需求。"
    )
    if tried_urls:
        failure_summary += f"\n\n已尝试过的 URL（避免重复走相同路径）: {', '.join(tried_urls)}"
    if authoritative_target_url:
        failure_summary += (
            f"\n\nAuthoritative target URL: {authoritative_target_url}"
            "\nIf the user still wants this page, keep the URL and change extraction, waiting, "
            "or navigation strategy before switching sites."
        )

    history_summary = ""
    if len(replan_history) > 1:
        history_summary = "\n\n## Previous failed strategies (do not repeat)\n"
        for item in replan_history[:-1]:
            history_summary += f"Round {item['round']}:\n"
            for strategy in item.get("strategies", []):
                history_summary += f"- {strategy}\n"
            if item.get("urls"):
                history_summary += f"Visited URLs: {', '.join(item['urls'])}\n"

    llm = LLMClient()
    replanner_en_prompt = get_prompt("replanner_system_en")
    response = llm.chat_with_system(
        system_prompt=replanner_en_prompt,
        user_message=(
            f"User request: {state['user_input']}\n\n"
            f"Failure summary:\n{failure_summary}{history_summary}\n\n"
            f"Replan round: {state['replan_count']} "
            f"({'final attempt' if is_final_attempt else 'more retries allowed'})"
        ),
        temperature=0.3,
        json_mode=True,
    )

    try:
        result = _normalize_payload(llm.parse_json_response(response))
        repaired_tasks = _repair_replan_task_params(
            result.get("tasks", []),
            authoritative_target_url,
        )
        result["tasks"], finalize_instructions = _extract_finalize_instructions_from_replan_tasks(
            repaired_tasks
        )
        if finalize_instructions:
            shared_memory["_final_answer_instructions"] = finalize_instructions
        else:
            shared_memory.pop("_final_answer_instructions", None)
        log_agent_action("Replanner", f"分析: {str(result.get('analysis', '') or '')[:80]}")

        if result.get("should_give_up", False):
            log_warning(f"Replanner 决定放弃: {result.get('give_up_reason', '')}")
            state["final_output"] = result.get(
                "direct_answer",
                "抱歉，当前没有足够的可验证证据来继续完成这个请求。",
            )
            state["execution_status"] = "completed_with_issues"
            state["critic_approved"] = False
            state["task_queue"] = preserved_tasks
            state["policy_decisions"] = [
                build_policy_decision_from_task(task)
                for task in state["task_queue"]
            ]
            return state

        log_agent_action("Replanner", f"新策略: {str(result.get('new_strategy', '') or '')[:80]}")

        new_tasks = [
            build_task_item_from_plan(
                task_data,
                task_id_prefix="replan",
                default_priority=10,
            )
            for task_data in result.get("tasks", [])
        ]

        if new_tasks or preserved_tasks:
            state["task_queue"] = preserved_tasks + new_tasks
            state["policy_decisions"] = [
                build_policy_decision_from_task(task)
                for task in state["task_queue"]
            ]
            state["needs_human_confirm"] = any(
                task.get("requires_confirmation", False)
                for task in state["task_queue"]
            )
            state["human_approved"] = not state["needs_human_confirm"]
            _mark_confirmation_required_tasks_waiting(state)
            state["error_trace"] = ""
            log_success(
                f"重规划完成，保留 {len(preserved_tasks)} 个结果，新增 {len(new_tasks)} 个任务"
            )
        else:
            log_warning("重规划未生成新任务")

    except Exception as exc:
        log_error(f"重规划失败: {exc}")

    from langchain_core.messages import SystemMessage

    state["messages"].append(
        SystemMessage(content=f"Replanner 重规划完成（第 {state['replan_count']} 次）")
    )
    _save_runtime_checkpoint(state, "replanner", "Replanner completed")
    return state


@register_stage(name="finalize", order=90, required=True, depends_on=("router",))
def finalize_node(state: OmniCoreState) -> OmniCoreState:
    """Build the final user-facing output from direct answers or executed tasks."""
    if _should_skip_for_resume(state, "finalize"):
        return state

    if not state["task_queue"]:
        shared_memory = state.get("shared_memory", {})
        if not isinstance(shared_memory, dict):
            shared_memory = {}

        direct_answer = str(shared_memory.get("router_direct_answer", "") or "").strip()
        if direct_answer:
            state["final_output"] = direct_answer
            state["execution_status"] = "completed"
            state["critic_approved"] = True
            state["delivery_package"] = {
                "headline": "Answered directly without worker execution.",
                "intent": str(state.get("current_intent", "") or ""),
                "review_status": "approved",
                "completed_task_count": 0,
                "total_task_count": 0,
                "completed_tasks": [],
                "deliverables": [],
                "issues": [],
                "critic_feedback": "",
                "recommended_next_step": "",
            }
            _save_runtime_checkpoint(state, "finalize", "Finalize completed without task queue")
            return state

        router_reasoning = ""
        for msg in reversed(state.get("messages", [])):
            raw_content = str(getattr(msg, "content", "") or "")
            content = _normalize_text_value(raw_content)
            if "Router 分析完成" in content:
                router_reasoning = content.replace("Router 分析完成: ", "")
                break
            if "Router 鍒嗘瀽瀹屾垚" in raw_content:
                router_reasoning = raw_content.replace("Router 鍒嗘瀽瀹屾垚: ", "")
                break

        final_output = "这次没有形成可执行计划，也没有拿到可验证的结果，所以我不能直接给你事实性答案。请重试，或明确要查询的目标。"
        if router_reasoning:
            final_output += f"\n\nSystem note: {router_reasoning}"

        state["final_output"] = final_output
        state["execution_status"] = "completed_with_issues"
        state["critic_approved"] = False
        state["delivery_package"] = {
            "headline": "No verified result was produced.",
            "intent": str(state.get("current_intent", "") or ""),
            "review_status": "needs_attention",
            "completed_task_count": 0,
            "total_task_count": 0,
            "completed_tasks": [],
            "deliverables": [],
            "issues": [
                {
                    "task_id": "",
                    "description": "No executable plan or verifiable result",
                    "error": router_reasoning or "Router did not produce a valid executable answer.",
                }
            ],
            "critic_feedback": router_reasoning or "No verifiable result was produced.",
            "recommended_next_step": "Retry the request, or specify the exact target/source to query.",
        }
        _save_runtime_checkpoint(state, "finalize", "Finalize completed without task queue")
        return state

    delivery_summary = _build_delivery_summary(state)
    state["final_output"] = _synthesize_user_facing_answer(state, delivery_summary)

    statuses = {str(task.get("status", "") or "") for task in state["task_queue"]}
    if WAITING_FOR_APPROVAL in statuses:
        state["execution_status"] = WAITING_FOR_APPROVAL
    elif WAITING_FOR_EVENT in statuses:
        state["execution_status"] = WAITING_FOR_EVENT
    elif BLOCKED in statuses:
        state["execution_status"] = BLOCKED
    elif state["critic_approved"]:
        state["execution_status"] = "completed"
        log_success("所有任务执行完成")
    else:
        state["execution_status"] = "completed_with_issues"
        log_error(f"任务未通过审查: {state['critic_feedback']}")

    _save_runtime_checkpoint(state, "finalize", "Finalize completed")
    return state


def build_graph() -> StateGraph:
    """
    构建 OmniCore 执行图 v0.2
    新流程: Router → human_confirm → parallel_executor(batch) → Validator → Critic → finalize
                                                    ↓ fail      ↓ fail
                                                 replanner    replanner
    """

    graph = StateGraph(OmniCoreState)

    # 添加节点（7 个）
    graph.add_node("router", route_node)
    graph.add_node("human_confirm", human_confirm_node_v2)
    graph.add_node("parallel_executor", parallel_executor_node)
    graph.add_node("validator", validator_node)
    graph.add_node("replanner", replanner_node)
    graph.add_node("critic", critic_node)
    graph.add_node("finalize", finalize_node)

    # 入口
    graph.set_entry_point("router")

    # Router → human_confirm 或 finalize
    graph.add_conditional_edges("router", should_continue_after_route, {
        "human_confirm": "human_confirm",
        "finalize": "finalize",
    })

    # human_confirm → 执行批次 / validator / end
    graph.add_conditional_edges("human_confirm", get_first_executor, {
        "parallel_executor": "parallel_executor",
        "validator": "validator",
        "end": END,
    })

    # 批次执行后：还有 ready 任务就继续下一批，否则进入 validator
    graph.add_conditional_edges("parallel_executor", after_parallel_executor, {
        "parallel_executor": "parallel_executor",
        "validator": "validator",
    })

    # Validator → critic / replanner / finalize
    graph.add_conditional_edges("validator", after_validator, {
        "critic": "critic",
        "replanner": "replanner",
        "finalize": "finalize",
    })

    # Replanner → 执行批次 / validator / end
    graph.add_conditional_edges("replanner", get_first_executor, {
        "parallel_executor": "parallel_executor",
        "validator": "validator",
        "end": END,
    })

    # Critic → finalize 或 replanner
    graph.add_conditional_edges("critic", should_retry_or_finish, {
        "finalize": "finalize",
        "replanner": "replanner",
    })

    # 最终节点
    graph.add_edge("finalize", END)

    return graph


def compile_graph():
    """Compile and return the legacy hardcoded graph (backward compat)."""
    graph = build_graph()
    return graph.compile()


# ---------------------------------------------------------------------------
# Adaptive Re-routing (Direction 7)
# ---------------------------------------------------------------------------

def should_skip_remaining_tasks(state: OmniCoreState) -> bool:
    """Lightweight post-batch check: should remaining tasks be skipped?

    Rules (no LLM call needed):
    1. Single-answer task + answer already found -> skip remaining
    2. Accumulated data meets or exceeds the requested item count -> skip remaining
    3. All remaining tasks depend on a failed prerequisite -> stop
    """
    task_queue = state.get("task_queue") or []
    if not task_queue:
        return False

    completed = [t for t in task_queue if str(t.get("status", "")) == "completed"]
    pending = [t for t in task_queue if str(t.get("status", "")) == "pending"]

    if not pending:
        return False

    # Rule 1: single-answer intent already answered
    intent = str(state.get("current_intent", "") or "").lower()
    single_answer_intents = ("direct_answer", "simple_query", "factual_question")
    if any(tok in intent for tok in single_answer_intents):
        if completed:
            return True

    # Rule 2: requested item count already met
    user_input = str(state.get("user_input", "") or "")
    requested_count = _extract_requested_item_count(user_input)
    if requested_count > 0 and completed:
        total_items = 0
        for task in completed:
            result = task.get("result")
            if isinstance(result, dict):
                for key in ("data", "items", "content"):
                    payload = result.get(key)
                    if isinstance(payload, list):
                        total_items += len(payload)
        if total_items >= requested_count:
            return True

    # Rule 3: all remaining tasks depend on a failed task
    failed_ids = {
        str(t.get("task_id", ""))
        for t in task_queue
        if str(t.get("status", "")) == "failed"
    }
    if failed_ids and pending:
        all_blocked = True
        for task in pending:
            deps = list(task.get("depends_on") or [])
            if not deps or not any(dep in failed_ids for dep in deps):
                all_blocked = False
                break
        if all_blocked:
            return True

    return False


def _apply_adaptive_skip(state: OmniCoreState) -> OmniCoreState:
    """Mark remaining pending tasks as skipped when adaptive re-routing triggers."""
    task_queue = state.get("task_queue") or []
    skipped_count = 0
    for task in task_queue:
        if str(task.get("status", "")) == "pending":
            task["status"] = "completed"
            task.setdefault("result", {})
            if isinstance(task["result"], dict):
                task["result"]["skipped_by_adaptive_reroute"] = True
            skipped_count += 1
    if skipped_count:
        log_agent_action(
            "AdaptiveReroute",
            f"Skipped {skipped_count} remaining task(s) — goal already satisfied",
        )
    return state


# ---------------------------------------------------------------------------
# Dynamic graph builder (Direction 1)
# ---------------------------------------------------------------------------

def build_graph_from_registry(registry: StageRegistry = None):
    """Build the LangGraph DAG dynamically from the StageRegistry.

    This replaces hardcoded graph construction with a data-driven approach.
    Stages register themselves via the ``@register_stage`` decorator, and this
    function wires them together based on order and dependency declarations.

    The complex routing logic (executor self-loop, critic->replanner loop,
    router->finalize shortcut) is preserved by reusing the same conditional
    edge functions that the legacy ``build_graph()`` uses.
    """
    if registry is None:
        registry = StageRegistry.get_instance()

    graph = StateGraph(OmniCoreState)
    stages = registry.get_ordered_stages()
    stage_names = {s.name for s in stages}

    if not stages:
        raise RuntimeError("No stages registered in the StageRegistry")

    # Add all registered nodes
    for stage in stages:
        graph.add_node(stage.name, stage.node_fn)

    # Entry point is always the first stage by order (router)
    graph.set_entry_point(stages[0].name)

    # ---------------------------------------------------------------
    # Wire conditional edges.
    #
    # We reuse the existing hand-written routing functions because they
    # encode important domain logic (direct_answer shortcut, executor
    # self-loop, critic->replanner retry, etc.).  The registry tells us
    # which stages exist so we can gracefully degrade when a stage is
    # removed.
    # ---------------------------------------------------------------

    def _safe_targets(*names):
        """Filter target names to only those actually registered."""
        return {n: n for n in names if n in stage_names or n == END}

    # Router -> human_confirm | finalize
    if "router" in stage_names:
        targets = {}
        if "human_confirm" in stage_names:
            targets["human_confirm"] = "human_confirm"
        if "finalize" in stage_names:
            targets["finalize"] = "finalize"
        if targets:
            graph.add_conditional_edges("router", should_continue_after_route, targets)

    # human_confirm -> parallel_executor | validator | END
    if "human_confirm" in stage_names:
        targets = {}
        if "parallel_executor" in stage_names:
            targets["parallel_executor"] = "parallel_executor"
        if "validator" in stage_names:
            targets["validator"] = "validator"
        targets["end"] = END
        graph.add_conditional_edges("human_confirm", get_first_executor, targets)

    # parallel_executor -> parallel_executor (self-loop) | validator
    if "parallel_executor" in stage_names:
        targets = {}
        targets["parallel_executor"] = "parallel_executor"
        if "validator" in stage_names:
            targets["validator"] = "validator"
        graph.add_conditional_edges(
            "parallel_executor",
            _after_parallel_executor_adaptive,
            targets,
        )

    # validator -> critic | replanner | finalize
    if "validator" in stage_names:
        targets = {}
        if "critic" in stage_names:
            targets["critic"] = "critic"
        if "replanner" in stage_names:
            targets["replanner"] = "replanner"
        if "finalize" in stage_names:
            targets["finalize"] = "finalize"
        if targets:
            graph.add_conditional_edges("validator", after_validator, targets)

    # replanner -> parallel_executor | validator | END
    if "replanner" in stage_names:
        targets = {}
        if "parallel_executor" in stage_names:
            targets["parallel_executor"] = "parallel_executor"
        if "validator" in stage_names:
            targets["validator"] = "validator"
        targets["end"] = END
        graph.add_conditional_edges("replanner", get_first_executor, targets)

    # critic -> finalize | replanner
    if "critic" in stage_names:
        targets = {}
        if "finalize" in stage_names:
            targets["finalize"] = "finalize"
        if "replanner" in stage_names:
            targets["replanner"] = "replanner"
        if targets:
            graph.add_conditional_edges("critic", should_retry_or_finish, targets)

    # finalize -> END
    if "finalize" in stage_names:
        graph.add_edge("finalize", END)

    return graph.compile()


def _after_parallel_executor_adaptive(
    state: OmniCoreState,
) -> Literal["parallel_executor", "validator"]:
    """Post-executor routing with adaptive re-routing check (Direction 7).

    Before checking for more ready tasks, evaluate whether the execution
    goal has already been met.  If so, skip remaining tasks and proceed
    to validation.
    """
    if should_skip_remaining_tasks(state):
        _apply_adaptive_skip(state)
        return "validator"
    return after_parallel_executor(state)


# ---------------------------------------------------------------------------
# Global compiled graph singleton
# ---------------------------------------------------------------------------

_USE_REGISTRY_GRAPH = True  # flip to False to fall back to legacy build_graph()

omnicore_graph = None


def get_graph(use_registry: bool = None):
    """Return the compiled graph singleton.

    By default uses ``build_graph_from_registry()`` (Direction 1).
    Pass ``use_registry=False`` or set module-level ``_USE_REGISTRY_GRAPH = False``
    to fall back to the legacy ``build_graph()``.
    """
    global omnicore_graph
    if omnicore_graph is not None:
        return omnicore_graph

    should_use_registry = use_registry if use_registry is not None else _USE_REGISTRY_GRAPH

    if should_use_registry:
        try:
            omnicore_graph = build_graph_from_registry()
            log_agent_action(
                "GraphBuilder",
                "Built graph from StageRegistry",
                f"{len(StageRegistry.get_instance().list_names())} stages",
            )
        except Exception as exc:
            log_warning(f"Registry graph build failed, falling back to legacy: {exc}")
            omnicore_graph = compile_graph()
    else:
        omnicore_graph = compile_graph()

    return omnicore_graph
