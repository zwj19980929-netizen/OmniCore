"""
Structured data extraction and deterministic list-answer formatting.

Extracted from core/graph.py (R3 refactor).  Used by the Finalizer to
build user-facing structured outputs from completed task results.
"""

import json
import re
from typing import Any, Dict, List

from utils.text_repair import normalize_text_value, normalize_payload, payload_preview
from utils.web_result_normalizer import canonicalize_item, infer_requested_fields, normalize_web_results


# ---------------------------------------------------------------------------
# Structured findings
# ---------------------------------------------------------------------------

def extract_structured_findings(task_queue: list, max_items: int = 5) -> str:
    """Extract a human-readable findings summary from completed tasks.

    *task_queue* is ``state["task_queue"]``.
    """
    title_keys = ("title", "headline", "name", "subject", "text")
    date_keys = ("date", "time", "datetime", "published_at", "published")
    link_keys = ("link", "url", "source_url", "article_url")
    detail_keys = ("summary", "description", "desc", "snippet", "content", "text")

    findings: List[Dict[str, str]] = []
    seen = set()
    for task in task_queue or []:
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
                    text = normalize_text_value(record)
                    if not text:
                        continue
                    key = (text[:120], "")
                    if key in seen:
                        continue
                    seen.add(key)
                    findings.append({"title": text, "date": "", "link": "", "detail": ""})
                    continue

                title = next((normalize_text_value(record.get(key)) for key in title_keys if record.get(key)), "")
                date = next((normalize_text_value(record.get(key)) for key in date_keys if record.get(key)), "")
                link = next((normalize_text_value(record.get(key)) for key in link_keys if record.get(key)), "")
                detail = next((normalize_text_value(record.get(key)) for key in detail_keys if record.get(key)), "")
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


# ---------------------------------------------------------------------------
# Requested-item-count heuristics
# ---------------------------------------------------------------------------

def extract_requested_item_count(text: str) -> int:
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


def looks_like_explicit_list_output_request(text: str) -> bool:
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
    return any(token in lowered for token in list_tokens) or extract_requested_item_count(text) > 1


# ---------------------------------------------------------------------------
# Markdown helpers
# ---------------------------------------------------------------------------

def escape_markdown_cell(value: Any) -> str:
    text = normalize_text_value(value)
    if not text:
        return ""
    return text.replace("|", "\\|").replace("\n", " ").strip()


# ---------------------------------------------------------------------------
# Best-record collection
# ---------------------------------------------------------------------------

def collect_best_completed_record_list(task_queue: list) -> List[Dict[str, Any]]:
    best_records: List[Dict[str, Any]] = []
    for task in task_queue or []:
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


# ---------------------------------------------------------------------------
# LLM noise filter
# ---------------------------------------------------------------------------

_LLM_FILTER_PROMPT = """You are filtering extracted web data for a user task.

Given a task description and a list of extracted items, return ONLY the indexes of items
that are genuine content results (articles, papers, products, entries, etc.).
Remove items that are clearly website UI elements, navigation links, page chrome,
pagination controls, or unrelated boilerplate.

Return JSON: {"keep": [1, 3, 5]}
Use the 1-based indexes from the provided list. If all items look legitimate, return all indexes.
"""


def llm_filter_noisy_results(
    records: List[Dict[str, Any]],
    task_description: str,
) -> List[Dict[str, Any]]:
    """When results look noisy, use a lightweight LLM call to filter out UI junk."""
    if len(records) < 4:
        return records

    short_no_summary = sum(
        1 for r in records
        if len(str(r.get("title", "") or "")) <= 10
        and not str(r.get("summary", "") or "").strip()
    )
    if short_no_summary <= len(records) * 0.4:
        return records

    payload_items = []
    for idx, record in enumerate(records[:30], 1):
        payload_items.append({
            "index": idx,
            "title": str(record.get("title", "") or "")[:200],
            "url": str(record.get("url", record.get("link", "")) or "")[:200],
        })

    try:
        from core.llm import LLMClient
        llm = LLMClient()
        response = llm.chat_with_system(
            system_prompt=_LLM_FILTER_PROMPT,
            user_message=json.dumps(
                {"task": task_description[:500], "items": payload_items},
                ensure_ascii=False,
            ),
            temperature=0.0,
            json_mode=True,
        )
        parsed = llm.parse_json_response(response)
        keep_indexes = parsed.get("keep", [])
        if not keep_indexes:
            return records
        filtered = []
        for idx_val in keep_indexes:
            try:
                i = int(idx_val) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= i < len(records):
                filtered.append(records[i])
        return filtered if len(filtered) >= 2 else records
    except Exception:
        return records


# ---------------------------------------------------------------------------
# Deterministic list answer
# ---------------------------------------------------------------------------

def build_deterministic_list_answer(
    task_queue: list,
    critic_approved: bool,
    user_input: str,
    package: Dict[str, Any],
) -> str:
    """Build a Markdown table answer when the user explicitly requested a list.

    Returns an empty string if the request is not a list-type output or
    the result quality is insufficient.
    """
    if not critic_approved:
        return ""
    if str(package.get("review_status", "") or "") != "approved":
        return ""
    if package.get("issues"):
        return ""

    task_descriptions = [
        str(task.get("description", "") or "")
        for task in task_queue or []
        if isinstance(task, dict)
    ]
    request_context = "\n".join(
        part for part in [str(user_input or "").strip(), *task_descriptions] if part
    )
    if not looks_like_explicit_list_output_request(request_context):
        return ""

    raw_records = collect_best_completed_record_list(task_queue)
    if len(raw_records) < 2:
        return ""

    requested_count = extract_requested_item_count(request_context)
    normalize_limit = max(requested_count, 100) if requested_count > 0 else 100
    normalized_records = normalize_web_results(raw_records, request_context, limit=normalize_limit)

    normalized_records = llm_filter_noisy_results(normalized_records, request_context)

    if len(normalized_records) < 2:
        return ""

    render_count = len(normalized_records)
    if requested_count > 0:
        if len(normalized_records) < requested_count:
            return ""
        render_count = min(len(normalized_records), requested_count)
    render_count = min(render_count, 100)
    rows = normalized_records[:render_count]
    if not rows:
        return ""

    requested_fields = infer_requested_fields(request_context, {"page_type": "list"})
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
        if field in requested_set and any(escape_markdown_cell(item.get(field)) for item in rows)
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
            cells.append(escape_markdown_cell(value))
        lines.append(f"| {idx} | " + " | ".join(cells) + " |")

    return "\n".join(lines)
