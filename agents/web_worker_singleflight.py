"""
Clean singleflight-backed analysis helpers for WebWorker.
This keeps the brittle mixed-encoding source file small while moving the
maintainable logic into an ASCII-only module.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, List, Tuple

from config.settings import settings
from utils.logger import log_agent_action, log_debug_metrics, log_error
from utils.browser_toolkit import BrowserToolkit
from utils.web_prompt_budget import (
    BudgetSection,
    extract_anchor_terms,
    extract_relevant_html_fragments,
    render_budgeted_sections,
)
import utils.web_debug_recorder as web_debug_recorder

_PAGE_ANALYSIS_PROMPT_BUDGET_TOKENS = 2200

_REGION_TASK_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "into", "page", "pages",
    "extract", "collect", "scrape", "read", "show", "return", "using", "current",
    "页面", "当前", "提取", "抓取", "获取", "读取", "返回", "展示", "内容", "数据",
}


def _task_terms(text: str) -> List[str]:
    seen: set[str] = set()
    terms: List[str] = []
    for match in re.findall(r"[\u4e00-\u9fff]{2,12}|[A-Za-z0-9][A-Za-z0-9._/+:-]{2,32}", str(text or "")):
        token = str(match or "").strip().lower()
        if not token or token in _REGION_TASK_STOPWORDS:
            continue
        if token in seen:
            continue
        seen.add(token)
        terms.append(token)
    return terms


def _score_candidate_region(task_description: str, kind: str, *texts: Any, item_count: int = 0, control_count: int = 0) -> int:
    task_terms = _task_terms(task_description)
    haystack = " ".join(str(text or "") for text in texts).lower()
    score = 0
    if task_terms:
        score += sum(3 for term in task_terms if term and term in haystack)

    lowered_task = str(task_description or "").lower()
    normalized_kind = str(kind or "").lower()
    if any(token in lowered_task for token in ("title", "link", "headline", "list", "结果", "标题", "链接", "列表", "表格")):
        if normalized_kind in {"list", "table", "search_result", "main"}:
            score += 4
    if any(token in lowered_task for token in ("summary", "content", "article", "正文", "文章", "摘要")):
        if normalized_kind in {"detail", "main", "section"}:
            score += 4
    if any(token in lowered_task for token in ("search", "serp", "搜索", "检索", "核实")):
        if normalized_kind in {"search_result", "list"}:
            score += 3
    if any(token in lowered_task for token in ("form", "login", "submit", "表单", "登录", "填写")):
        if normalized_kind in {"form", "modal"}:
            score += 4

    score += min(int(item_count or 0), 6)
    score += min(int(control_count or 0), 3)
    return score



def _format_candidate_regions_for_llm(
    snapshot: Dict[str, Any],
    page_structure_payload: Dict[str, Any],
    task_description: str = "",
) -> str:
    candidates: List[Tuple[int, str]] = []

    for region in (snapshot.get("regions", []) or [])[:8]:
        if not isinstance(region, dict):
            continue
        kind = str(region.get("kind", "") or "unknown")
        heading = str(region.get("heading", "") or "")[:120]
        sample = str(region.get("text_sample", "") or "")[:180]
        selector = str(region.get("selector", "") or "")[:72]
        ref = str(region.get("ref", "") or "")
        samples = " | ".join(
            str(item or "")[:100]
            for item in (region.get("sample_items", []) or [])[:3]
            if str(item or "").strip()
        )
        payload = (
            f"kind={kind} ref={ref} region={str(region.get('region', '') or '')} "
            f"selector={selector} heading={heading} sample={sample} "
            f"items={int(region.get('item_count', 0) or 0)} "
            f"links={int(region.get('link_count', 0) or 0)} "
            f"controls={int(region.get('control_count', 0) or 0)} "
            f"items_sample={samples or '(none)'}"
        )
        score = _score_candidate_region(
            task_description,
            kind,
            heading,
            sample,
            samples,
            selector,
            item_count=int(region.get("item_count", 0) or 0),
            control_count=int(region.get("control_count", 0) or 0),
        )
        candidates.append((score, payload))

    for card in (snapshot.get("cards", []) or [])[:6]:
        if not isinstance(card, dict):
            continue
        parts = [
            str(card.get("title", "") or "")[:100],
            str(card.get("source", "") or "")[:48],
            str(card.get("host", "") or "")[:48],
            str(card.get("snippet", "") or "")[:140],
        ]
        target_ref = str(card.get("target_ref", "") or "")
        target_selector = str(card.get("target_selector", "") or "")[:72]
        payload = " | ".join(part for part in parts if part)
        if not payload:
            continue
        tail = f" target_ref={target_ref}" if target_ref else (f" selector={target_selector}" if target_selector else "")
        score = _score_candidate_region(task_description, "search_result", payload, item_count=1)
        candidates.append((score, payload + tail))

    for collection in (snapshot.get("collections", []) or [])[:5]:
        if not isinstance(collection, dict):
            continue
        samples = " | ".join(
            str(sample or "")[:120]
            for sample in (collection.get("sample_items", []) or [])[:3]
            if str(sample or "").strip()
        )
        payload = (
            f"kind={str(collection.get('kind', '') or 'unknown')} "
            f"count={int(collection.get('item_count', 0) or 0)} "
            f"ref={str(collection.get('ref', '') or '')} "
            f"samples={samples or '(none)'}"
        )
        score = _score_candidate_region(
            task_description,
            str(collection.get("kind", "") or "unknown"),
            samples,
            item_count=int(collection.get("item_count", 0) or 0),
        )
        candidates.append((score, payload))

    for control in (snapshot.get("controls", []) or [])[:5]:
        if not isinstance(control, dict):
            continue
        payload = (
            f"kind={str(control.get('kind', '') or '')} "
            f"ref={str(control.get('ref', '') or '')} "
            f"text={str(control.get('text', '') or '')[:96]} "
            f"selector={str(control.get('selector', '') or '')[:72]}"
        )
        score = _score_candidate_region(
            task_description,
            str(control.get("kind", "") or "control"),
            str(control.get("text", "") or ""),
            control_count=1,
        )
        candidates.append((score, payload))

    for block in (page_structure_payload.get("main_content_blocks", []) or [])[:6]:
        if not isinstance(block, dict):
            continue
        payload = (
            f"type={str(block.get('block_type', '') or 'unknown')} "
            f"selector={str(block.get('selector', '') or '')[:72]} "
            f"content={str(block.get('content', '') or '')[:180]}"
        )
        score = _score_candidate_region(
            task_description,
            str(block.get("block_type", "") or "section"),
            str(block.get("content", "") or ""),
        )
        candidates.append((score, payload))

    if not candidates:
        return "(no candidate regions)"

    candidates.sort(key=lambda entry: entry[0], reverse=True)
    lines = ["Task-ranked candidate regions:"]
    for index, (_, payload) in enumerate(candidates[:10], 1):
        lines.append(f"{index}. {payload}")
    if len(candidates) > 10:
        lines.append(f"... {len(candidates) - 10} more candidate regions omitted")
    return "\n".join(lines)


def _build_page_analysis_context(
    task_description: str,
    html: str,
    semantic_snapshot: Dict[str, Any],
    semantic_snapshot_text: str,
    page_structure_text: str,
    page_structure_payload: Dict[str, Any],
    model: Any = "",
) -> tuple[Dict[str, str], Dict[str, Dict[str, Any]]]:
    candidate_regions = _format_candidate_regions_for_llm(semantic_snapshot, page_structure_payload, task_description=task_description)
    anchors = extract_anchor_terms(
        task=task_description,
        snapshot=semantic_snapshot,
        page_structure=page_structure_payload,
    )
    html_for_llm = extract_relevant_html_fragments(
        html,
        anchors,
        total_chars=3600,
        window_chars=1200,
        max_fragments=4,
    )
    rendered, budget = render_budgeted_sections(
        [
            BudgetSection(
                name="semantic_snapshot",
                text=semantic_snapshot_text,
                min_chars=900,
                max_chars=1800,
                weight=1.3,
                mode="lines",
                omission_label="snapshot lines",
            ),
            BudgetSection(
                name="page_structure",
                text=page_structure_text,
                min_chars=900,
                max_chars=1800,
                weight=1.2,
                mode="lines",
                omission_label="structure lines",
            ),
            BudgetSection(
                name="candidate_regions",
                text=candidate_regions,
                min_chars=800,
                max_chars=1600,
                weight=1.4,
                mode="lines",
                omission_label="candidate lines",
            ),
            BudgetSection(
                name="html_content",
                text=html_for_llm,
                min_chars=1800,
                max_chars=3600,
                weight=1.8,
                mode="text",
                omission_label="html fragments",
            ),
        ],
        total_tokens=_PAGE_ANALYSIS_PROMPT_BUDGET_TOKENS,
        model=model,
    )
    budget["anchors"] = {"values": anchors}
    return rendered, budget


async def determine_target_url_with_singleflight(self, task_description: str) -> Dict[str, Any]:
    from agents.web_worker import URL_ANALYSIS_PROMPT

    log_agent_action(self.name, "Analyze target URL", task_description[:50])

    task_signature = self.cache.build_task_signature(task_description)
    cache_key = self.cache.build_key(
        "url_analysis",
        task_signature=task_signature,
        prompt_version="url_analysis_prompt_v1",
        model_name=getattr(self.llm, "model", ""),
    )
    cached = self.cache.get(cache_key)
    if isinstance(cached, dict):
        web_debug_recorder.record_event(
            "url_analysis_cache_hit",
            task=task_description,
            cache_key=cache_key,
            result=cached,
        )
        log_agent_action(self.name, "Reuse cached URL analysis", task_description[:50])
        log_debug_metrics("llm_cache.url_analysis", self.cache.snapshot_stats())
        return cached

    async def _compute_url_analysis() -> Dict[str, Any]:
        try:
            prompt = URL_ANALYSIS_PROMPT.format(task_description=task_description)
            web_debug_recorder.write_text("url_analysis_prompt", prompt)
            response = await asyncio.to_thread(
                self.llm.chat_with_system,
                system_prompt=prompt,
                user_message="Please infer the best target URL for this task",
                temperature=0.2,
                json_mode=True,
            )
            web_debug_recorder.write_text("url_analysis_response", getattr(response, "content", ""))
            result = self.llm.parse_json_response(response)
            web_debug_recorder.write_json("url_analysis_result", result)
            if result.get("url") or result.get("need_search"):
                self.cache.set(
                    cache_key,
                    result,
                    settings.URL_ANALYSIS_CACHE_TTL_SECONDS,
                    metadata={
                        "namespace": "url_analysis",
                        "task_signature": task_signature,
                        "prompt_version": "url_analysis_prompt_v1",
                        "model_name": getattr(self.llm, "model", ""),
                    },
                )
            return result
        except Exception as exc:
            log_error(f"URL analysis failed: {exc}")
            return {"url": "", "need_search": True, "search_query": task_description}

    result = await self.cache.run_singleflight(cache_key, _compute_url_analysis)
    log_debug_metrics("llm_cache.url_analysis", self.cache.snapshot_stats())
    log_agent_action(self.name, "Resolved target URL", result.get("url", "unknown"))
    return result


async def analyze_page_structure_with_singleflight(
    self,
    tk: BrowserToolkit,
    task_description: str,
) -> Dict[str, Any]:
    from agents.web_worker import PAGE_ANALYSIS_PROMPT

    log_agent_action(self.name, "Analyze page structure")
    observation = await self._collect_page_observation_context(tk, task_description)
    url = str(observation.get("url", "") or "")
    html = str(observation.get("html", "") or "")
    semantic_snapshot = observation.get("semantic_snapshot", {}) or {}
    semantic_snapshot_text = str(observation.get("semantic_snapshot_text", "(无语义快照)") or "(无语义快照)")
    page_structure_text = str(
        observation.get("page_structure_text", "(页面结构提取失败，仅使用HTML分析)") or "(页面结构提取失败，仅使用HTML分析)"
    )
    page_structure_payload = observation.get("page_structure_payload", {}) or {}

    normalized_url = self.cache.normalize_url(url)
    task_signature = self.cache.build_task_signature(task_description)
    page_fingerprint = self.cache.build_page_fingerprint(html)
    cache_key = self.cache.build_key(
        "page_structure_analysis",
        normalized_url=normalized_url,
        task_signature=task_signature,
        page_fingerprint=page_fingerprint,
        prompt_version="page_analysis_prompt_v5",
        model_name=getattr(self.llm, "model", ""),
    )
    cached = self.cache.get(cache_key)
    if isinstance(cached, dict):
        cached = self._normalize_selector_config(cached, url, semantic_snapshot)
        web_debug_recorder.record_event(
            "page_analysis_cache_hit",
            normalized_url=normalized_url,
            cache_key=cache_key,
            item_selector=str(cached.get("item_selector", "") or ""),
            page_type=str(cached.get("page_type", "") or ""),
        )
        log_agent_action(self.name, "Reuse cached page analysis", normalized_url[:80])
        log_debug_metrics("llm_cache.page_analysis", self.cache.snapshot_stats())
        return cached

    html_cleaned = self._clean_html_for_llm(html[:120000])
    prompt_context, prompt_budget = _build_page_analysis_context(
        task_description=task_description,
        html=html_cleaned,
        semantic_snapshot=semantic_snapshot,
        semantic_snapshot_text=semantic_snapshot_text,
        page_structure_text=page_structure_text,
        page_structure_payload=page_structure_payload,
        model=getattr(self, "llm", ""),
    )
    html_for_llm = prompt_context.get("html_content", "")
    log_agent_action(
        self.name,
        "Page analysis context budgeted",
        (
            f"cleaned_html={len(html_cleaned)} chars, "
            f"html_for_llm={len(html_for_llm)} chars, "
            f"snapshot={len(prompt_context.get('semantic_snapshot', ''))} chars, "
            f"regions={len(prompt_context.get('candidate_regions', ''))} chars"
        ),
    )

    page_analysis_prompt = PAGE_ANALYSIS_PROMPT.format(
        task_description=task_description,
        semantic_snapshot=prompt_context.get("semantic_snapshot", "(无语义快照)"),
        html_content=html_for_llm,
        page_structure=prompt_context.get("page_structure", "(页面结构提取失败，仅使用HTML分析)"),
        candidate_regions=prompt_context.get("candidate_regions", "(no candidate regions)"),
        current_url=url,
    )
    web_debug_recorder.write_text("page_analysis_html_for_llm", html_for_llm, suffix=".html")
    web_debug_recorder.write_text(
        "page_analysis_candidate_regions",
        prompt_context.get("candidate_regions", "(no candidate regions)"),
    )
    web_debug_recorder.write_text("page_analysis_prompt", page_analysis_prompt)
    web_debug_recorder.write_json("page_analysis_budget", prompt_budget)

    async def _compute_page_analysis() -> Dict[str, Any]:
        try:
            response = await asyncio.to_thread(
                self.llm.chat_with_system,
                system_prompt=page_analysis_prompt,
                user_message="Please analyze the page structure and return selectors",
                temperature=0.2,
                json_mode=True,
            )
            web_debug_recorder.write_text("page_analysis_response", getattr(response, "content", ""))
            config = self.llm.parse_json_response(response)
            config = self._normalize_selector_config(config, url, semantic_snapshot)
            web_debug_recorder.write_json("page_analysis_config", config)
            if config.get("item_selector"):
                self.cache.set(
                    cache_key,
                    config,
                    settings.PAGE_ANALYSIS_CACHE_TTL_SECONDS,
                    metadata={
                        "namespace": "page_structure_analysis",
                        "normalized_url": normalized_url,
                        "task_signature": task_signature,
                        "page_fingerprint": page_fingerprint,
                        "prompt_version": "page_analysis_prompt_v5",
                        "model_name": getattr(self.llm, "model", ""),
                    },
                )
            return config
        except Exception as exc:
            log_error(f"Page analysis failed: {exc}")
            return {"success": False, "error": str(exc)}

    config = await self.cache.run_singleflight(cache_key, _compute_page_analysis)
    log_debug_metrics("llm_cache.page_analysis", self.cache.snapshot_stats())
    log_agent_action(
        self.name,
        "Page analysis complete",
        f"item_selector: {config.get('item_selector', 'N/A')}",
    )
    return config
