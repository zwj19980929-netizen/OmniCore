"""
Utilities for building budgeted webpage prompts.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import litellm
except Exception:  # pragma: no cover - fallback when litellm is unavailable
    litellm = None


APPROX_CHARS_PER_TOKEN = 4
_DEFAULT_MIN_SECTION_CHARS = 96
_DEFAULT_MIN_SECTION_TOKENS = 24
_WORD_STOP_TOKENS = {
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
    "from",
    "into",
    "about",
    "page",
    "pages",
    "site",
    "website",
    "result",
    "results",
    "item",
    "items",
    "search",
    "query",
    "extract",
    "read",
    "find",
    "show",
    "open",
    "click",
    "input",
    "title",
    "link",
    "text",
    "visible",
    "content",
    "section",
    "current",
    "latest",
    "recent",
    "today",
    "网页",
    "页面",
    "网站",
    "结果",
    "内容",
    "提取",
    "打开",
    "点击",
    "输入",
    "显示",
    "搜索",
    "查询",
    "当前",
    "最新",
    "最近",
    "今天",
}


@dataclass
class BudgetSection:
    name: str
    text: str
    min_chars: int = 0
    max_chars: int = 0
    min_tokens: int = 0
    max_tokens: int = 0
    weight: float = 1.0
    mode: str = "lines"
    omission_label: str = "lines"


def token_budget_to_chars(token_budget: int) -> int:
    return max(int(token_budget or 0), 0) * APPROX_CHARS_PER_TOKEN


def char_budget_to_tokens(char_budget: int) -> int:
    return max(int(math.ceil(max(int(char_budget or 0), 0) / APPROX_CHARS_PER_TOKEN)), 0)


def approximate_tokens(text: Any) -> int:
    raw = str(text or "")
    if not raw:
        return 0
    return int(math.ceil(len(raw) / APPROX_CHARS_PER_TOKEN))


def count_tokens(text: Any, model: str = "") -> int:
    raw = str(text or "")
    if not raw:
        return 0
    if litellm is None:
        return approximate_tokens(raw)
    try:
        resolved_model = str(model or "").strip()
        if resolved_model:
            return int(litellm.token_counter(model=resolved_model, text=raw))
        return int(litellm.token_counter(text=raw))
    except Exception:
        return approximate_tokens(raw)


@lru_cache(maxsize=32)
def supports_exact_token_count(model: str = "") -> bool:
    if litellm is None:
        return False
    try:
        resolved_model = str(model or "").strip()
        if resolved_model:
            value = int(litellm.token_counter(model=resolved_model, text="token budget probe"))
        else:
            value = int(litellm.token_counter(text="token budget probe"))
        return value > 0
    except Exception:
        return False


def normalize_whitespace(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def clip_text(text: Any, max_chars: int, omission_label: str = "content") -> str:
    raw = str(text or "")
    if max_chars <= 0:
        return ""
    if len(raw) <= max_chars:
        return raw
    suffix = f"... ({omission_label} truncated)"
    if max_chars <= len(suffix):
        return suffix[:max_chars]
    return raw[: max_chars - len(suffix)].rstrip() + suffix


def clip_text_to_tokens(text: Any, max_tokens: int, model: str = "", omission_label: str = "content") -> str:
    raw = str(text or "")
    if max_tokens <= 0:
        return ""
    if count_tokens(raw, model=model) <= max_tokens:
        return raw

    suffix = f"... ({omission_label} truncated)"
    suffix_tokens = max(count_tokens(suffix, model=model), 1)
    if max_tokens <= suffix_tokens:
        return clip_text(suffix, token_budget_to_chars(max_tokens), omission_label=omission_label)

    low = 0
    high = len(raw)
    best = ""
    budget = max_tokens - suffix_tokens
    while low <= high:
        mid = (low + high) // 2
        candidate = raw[:mid].rstrip()
        token_count = count_tokens(candidate, model=model)
        if token_count <= budget:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    if not best:
        return clip_text(suffix, token_budget_to_chars(max_tokens), omission_label=omission_label)
    return best + suffix


def clip_lines(text: Any, max_chars: int, omission_label: str = "lines") -> str:
    raw = str(text or "").strip()
    if not raw or max_chars <= 0:
        return ""
    if len(raw) <= max_chars:
        return raw

    lines = [line.rstrip() for line in raw.splitlines()]
    if len(lines) <= 1:
        return clip_text(raw, max_chars, omission_label=omission_label)

    kept: List[str] = []
    used_chars = 0
    reserve = min(max(40, len(omission_label) + 28), max_chars // 3 or 1)
    kept_count = 0
    for line in lines:
        line_length = len(line) + (1 if kept else 0)
        if kept and used_chars + line_length + reserve > max_chars:
            break
        if not kept and len(line) + reserve > max_chars:
            trimmed = clip_text(line, max_chars - reserve, omission_label=omission_label)
            kept.append(trimmed)
            kept_count = 1
            break
        kept.append(line)
        kept_count += 1
        used_chars += line_length

    omitted = max(len(lines) - kept_count, 0)
    output = "\n".join(kept).strip()
    if omitted <= 0:
        return output

    suffix = f"\n... ({omitted} {omission_label} omitted)"
    if len(output) + len(suffix) <= max_chars:
        return output + suffix
    return clip_text(output, max_chars - len(suffix), omission_label=omission_label) + suffix


def clip_lines_to_tokens(text: Any, max_tokens: int, model: str = "", omission_label: str = "lines") -> str:
    raw = str(text or "").strip()
    if not raw or max_tokens <= 0:
        return ""
    if count_tokens(raw, model=model) <= max_tokens:
        return raw

    lines = [line.rstrip() for line in raw.splitlines()]
    if len(lines) <= 1:
        return clip_text_to_tokens(raw, max_tokens, model=model, omission_label=omission_label)

    suffix_template = "\n... ({count} " + omission_label + " omitted)"
    kept: List[str] = []
    kept_count = 0
    for line in lines:
        next_kept = kept + [line]
        omitted = max(len(lines) - (kept_count + 1), 0)
        suffix = suffix_template.format(count=omitted) if omitted else ""
        candidate = "\n".join(next_kept).strip() + suffix
        if count_tokens(candidate, model=model) > max_tokens:
            break
        kept = next_kept
        kept_count += 1

    if not kept:
        return clip_text_to_tokens(raw, max_tokens, model=model, omission_label=omission_label)

    omitted = max(len(lines) - kept_count, 0)
    output = "\n".join(kept).strip()
    if omitted <= 0:
        return output

    suffix = suffix_template.format(count=omitted)
    combined = output + suffix
    if count_tokens(combined, model=model) <= max_tokens:
        return combined
    clipped = clip_text_to_tokens(output, max_tokens - count_tokens(suffix, model=model), model=model, omission_label=omission_label)
    return clipped + suffix


def resolve_budget_model(model_hint: Any = "") -> str:
    if model_hint is None:
        return ""
    if isinstance(model_hint, str):
        return model_hint.strip()
    for attr in ("_get_litellm_model", "model"):
        value = getattr(model_hint, attr, None)
        if callable(value):
            try:
                resolved = value()
            except Exception:
                continue
            if resolved:
                return str(resolved).strip()
        elif value:
            return str(value).strip()
    return ""


def render_budgeted_sections(
    sections: Sequence[BudgetSection],
    total_chars: Optional[int] = None,
    *,
    total_tokens: Optional[int] = None,
    model: Any = "",
) -> Tuple[Dict[str, str], Dict[str, Dict[str, Any]]]:
    resolved_model = resolve_budget_model(model)
    active = [section for section in sections if str(section.text or "").strip()]
    use_tokens = bool(total_tokens and total_tokens > 0)
    metric = "tokens" if use_tokens else "chars"
    tokenizer_mode = "litellm" if supports_exact_token_count(resolved_model) else "approx"
    total_budget = int(total_tokens or 0) if use_tokens else int(total_chars or 0)
    if total_budget <= 0 or not active:
        return (
            {section.name: "" for section in sections},
            {
                section.name: {
                    "requested_chars": len(str(section.text or "")),
                    "requested_tokens": count_tokens(str(section.text or ""), model=resolved_model),
                    "allocated_chars": 0,
                    "allocated_tokens": 0,
                    "used_chars": 0,
                    "used_tokens": 0,
                    "truncated": bool(str(section.text or "").strip()),
                    "mode": section.mode,
                    "budget_metric": metric,
                    "tokenizer_model": resolved_model,
                    "tokenizer_mode": tokenizer_mode,
                }
                for section in sections
            },
        )

    budgets: Dict[str, int] = {}
    max_caps: Dict[str, int] = {}
    total_min = 0
    for section in active:
        raw = str(section.text or "")
        requested_units = count_tokens(raw, model=resolved_model) if use_tokens else len(raw)
        if use_tokens:
            max_cap = section.max_tokens or (char_budget_to_tokens(section.max_chars) if section.max_chars else 0)
            cap = requested_units if max_cap <= 0 else min(requested_units, max_cap)
            minimum = section.min_tokens or (
                char_budget_to_tokens(section.min_chars) if section.min_chars else min(cap, _DEFAULT_MIN_SECTION_TOKENS)
            )
        else:
            cap = requested_units if section.max_chars <= 0 else min(requested_units, section.max_chars)
            minimum = section.min_chars or min(cap, _DEFAULT_MIN_SECTION_CHARS)
        max_caps[section.name] = max(cap, 0)
        if cap <= 0:
            budgets[section.name] = 0
            continue
        minimum = min(max(minimum, 0), cap)
        budgets[section.name] = minimum
        total_min += minimum

    if total_min > total_budget and total_min > 0:
        scale = total_budget / total_min
        remaining = total_budget
        for index, section in enumerate(active):
            original = budgets.get(section.name, 0)
            scaled = int(original * scale)
            if original > 0:
                scaled = max(min(scaled, original), min(original, 12 if use_tokens else 48))
            if index == len(active) - 1:
                scaled = max(0, min(max_caps.get(section.name, 0), remaining))
            budgets[section.name] = scaled
            remaining -= scaled
    else:
        remaining = max(total_budget - total_min, 0)
        expandable = [section for section in active if max_caps.get(section.name, 0) > budgets.get(section.name, 0)]
        while remaining > 0 and expandable:
            total_weight = sum(max(section.weight, 0.1) for section in expandable)
            granted_any = False
            for index, section in enumerate(expandable):
                room = max_caps[section.name] - budgets[section.name]
                if room <= 0:
                    continue
                if index == len(expandable) - 1:
                    grant = room if total_weight <= 0 else min(room, remaining)
                else:
                    grant = int(round(remaining * (max(section.weight, 0.1) / total_weight)))
                    grant = max(grant, 8 if use_tokens else 32)
                    grant = min(grant, room, remaining)
                if grant <= 0:
                    continue
                budgets[section.name] += grant
                remaining -= grant
                granted_any = True
                if remaining <= 0:
                    break
            if not granted_any:
                break
            expandable = [section for section in expandable if max_caps.get(section.name, 0) > budgets.get(section.name, 0)]

    rendered: Dict[str, str] = {}
    report: Dict[str, Dict[str, Any]] = {}
    for section in sections:
        raw = str(section.text or "")
        allocated = budgets.get(section.name, 0)
        if use_tokens and section.mode == "text":
            final = clip_text_to_tokens(raw, allocated, model=resolved_model, omission_label=section.omission_label)
        elif use_tokens:
            final = clip_lines_to_tokens(raw, allocated, model=resolved_model, omission_label=section.omission_label)
        elif section.mode == "text":
            final = clip_text(raw, allocated, omission_label=section.omission_label)
        else:
            final = clip_lines(raw, allocated, omission_label=section.omission_label)
        rendered[section.name] = final
        report[section.name] = {
            "requested_chars": len(raw),
            "requested_tokens": count_tokens(raw, model=resolved_model),
            "allocated_chars": token_budget_to_chars(allocated) if use_tokens else allocated,
            "allocated_tokens": allocated if use_tokens else char_budget_to_tokens(allocated),
            "used_chars": len(final),
            "used_tokens": count_tokens(final, model=resolved_model),
            "truncated": len(final) < len(raw),
            "mode": section.mode,
            "budget_metric": metric,
            "tokenizer_model": resolved_model,
            "tokenizer_mode": tokenizer_mode,
        }
    return rendered, report


def extract_anchor_terms(
    task: str = "",
    snapshot: Dict[str, Any] | None = None,
    page_structure: Dict[str, Any] | None = None,
    extra_texts: Iterable[str] | None = None,
    limit: int = 14,
) -> List[str]:
    terms: List[str] = []
    seen = set()

    def push(value: Any) -> None:
        text = normalize_whitespace(value)
        if len(text) < 2:
            return
        key = text.lower()
        if key in seen:
            return
        seen.add(key)
        terms.append(text[:80])

    def push_text_tokens(value: Any) -> None:
        text = normalize_whitespace(value)
        if not text:
            return
        for match in re.findall(r"[\u4e00-\u9fff]{2,12}|[A-Za-z0-9][A-Za-z0-9._/+:-]{2,32}", text):
            token = normalize_whitespace(match)
            lowered = token.lower()
            if lowered in _WORD_STOP_TOKENS:
                continue
            if len(token) < 2:
                continue
            push(token)
            if len(terms) >= limit:
                return

    push_text_tokens(task)
    if snapshot:
        push(snapshot.get("title", ""))
        for region in (snapshot.get("regions", []) or [])[:6]:
            if not isinstance(region, dict):
                continue
            push(region.get("heading", ""))
            push(region.get("text_sample", ""))
            for sample in (region.get("sample_items", []) or [])[:2]:
                push(sample)
                push_text_tokens(sample)
                if len(terms) >= limit:
                    break
            if len(terms) >= limit:
                break
        for card in (snapshot.get("cards", []) or [])[:8]:
            if not isinstance(card, dict):
                continue
            push(card.get("title", ""))
            push(card.get("source", ""))
            push_text_tokens(card.get("title", ""))
            push_text_tokens(card.get("snippet", ""))
            if len(terms) >= limit:
                break
        for collection in (snapshot.get("collections", []) or [])[:6]:
            if not isinstance(collection, dict):
                continue
            for sample in (collection.get("sample_items", []) or [])[:3]:
                push(sample)
                push_text_tokens(sample)
                if len(terms) >= limit:
                    break
            if len(terms) >= limit:
                break
        for control in (snapshot.get("controls", []) or [])[:4]:
            if not isinstance(control, dict):
                continue
            push(control.get("text", ""))
            if len(terms) >= limit:
                break

    if page_structure:
        for block in (page_structure.get("main_content_blocks", []) or [])[:6]:
            if not isinstance(block, dict):
                continue
            push(block.get("content", ""))
            push_text_tokens(block.get("content", ""))
            if len(terms) >= limit:
                break

    for value in extra_texts or []:
        push(value)
        push_text_tokens(value)
        if len(terms) >= limit:
            break

    return terms[:limit]


def extract_relevant_html_fragments(
    html: str,
    anchors: Sequence[str],
    *,
    total_chars: int,
    window_chars: int = 900,
    max_fragments: int = 4,
) -> str:
    source = str(html or "")
    if not source or total_chars <= 0:
        return ""
    if len(source) <= total_chars:
        return source

    lowered = source.lower()
    normalized_anchors: List[str] = []
    for anchor in anchors:
        value = normalize_whitespace(anchor)
        if len(value) < 2:
            continue
        normalized_anchors.append(value[:80])

    windows: List[Tuple[int, int, str]] = []
    for anchor in normalized_anchors:
        idx = lowered.find(anchor.lower())
        if idx < 0 and " " in anchor:
            for part in sorted(anchor.split(), key=len, reverse=True):
                if len(part) < 3:
                    continue
                idx = lowered.find(part.lower())
                if idx >= 0:
                    anchor = part
                    break
        if idx < 0:
            continue
        start = max(0, idx - window_chars // 2)
        end = min(len(source), idx + len(anchor) + window_chars // 2)
        merged = False
        for index, (win_start, win_end, label) in enumerate(windows):
            if not (end < win_start or start > win_end):
                windows[index] = (min(start, win_start), max(end, win_end), label)
                merged = True
                break
        if not merged:
            windows.append((start, end, anchor))
        if len(windows) >= max_fragments:
            break

    if not windows:
        return clip_text(source, total_chars, omission_label="html")

    windows.sort(key=lambda item: item[0])
    fragments: List[str] = []
    for index, (start, end, anchor) in enumerate(windows[:max_fragments], 1):
        snippet = source[start:end].strip()
        if not snippet:
            continue
        header = f"<!-- fragment {index}: anchor={anchor} -->\n"
        fragments.append(header + snippet)

    if not fragments:
        return clip_text(source, total_chars, omission_label="html")

    joined = "\n\n".join(fragments)
    if len(joined) <= total_chars:
        return joined
    return clip_text(joined, total_chars, omission_label="html fragments")
