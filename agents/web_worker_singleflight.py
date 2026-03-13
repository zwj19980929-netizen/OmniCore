"""
Clean singleflight-backed analysis helpers for WebWorker.
This keeps the brittle mixed-encoding source file small while moving the
maintainable logic into an ASCII-only module.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Dict

from config.settings import settings
from utils.logger import log_agent_action, log_debug_metrics, log_error
from utils.browser_toolkit import BrowserToolkit

_WEATHER_TOKENS = (
    "weather",
    "forecast",
    "temperature",
    "humidity",
    "aqi",
    "air quality",
    "wind",
    "天气",
    "天气预报",
    "气温",
    "空气质量",
    "风力",
    "湿度",
)

_WEATHER_DOMAINS = (
    "weather.com.cn",
    "moji.com",
    "tianqi.com",
)


def _extract_first_url(text: str) -> str:
    match = re.search(r"https?://\S+", str(text or ""))
    if not match:
        return ""
    return match.group(0).rstrip(".,);]")


def _looks_like_weather_task(task_description: str) -> bool:
    lowered = str(task_description or "").lower()
    if not lowered:
        return False
    return any(token in lowered for token in _WEATHER_TOKENS) or any(
        domain in lowered for domain in _WEATHER_DOMAINS
    )


def _extract_preferred_weather_domain(task_description: str) -> str:
    lowered = str(task_description or "").lower()
    direct_url = _extract_first_url(task_description)
    if direct_url:
        for domain in _WEATHER_DOMAINS:
            if domain in direct_url.lower():
                return domain

    for domain in _WEATHER_DOMAINS:
        if domain in lowered:
            return domain
    return _WEATHER_DOMAINS[0]


def _build_weather_site_query(task_description: str, domain: str) -> str:
    description = str(task_description or "").strip()
    patterns = (
        r"(?P<location>[\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z .'\-]{1,31})的(?P<timeframe>今天|明天|后天|当前|本周末|本周)(?:（\d{4}-\d{2}-\d{2}）)?天气",
        r"(?P<timeframe>今天|明天|后天|当前|本周末|本周)(?:（\d{4}-\d{2}-\d{2}）)?天气",
    )
    for pattern in patterns:
        match = re.search(pattern, description)
        if not match:
            continue
        location = str(match.groupdict().get("location", "") or "").strip()
        location = re.sub(
            r"^(?:directly obtain|use a visible browser to retrieve|retrieve|obtain|use)\s+",
            "",
            location,
            flags=re.IGNORECASE,
        ).strip()
        timeframe = str(match.groupdict().get("timeframe", "") or "").strip()
        parts = [f"site:{domain}"]
        if location:
            parts.append(location)
        if timeframe:
            parts.append(timeframe)
        parts.append("天气")
        return " ".join(parts)

    compact = re.sub(r"\s+", " ", description)
    compact = compact[:120]
    return f"site:{domain} {compact}".strip()


async def determine_target_url_with_singleflight(self, task_description: str) -> Dict[str, Any]:
    from agents.web_worker import URL_ANALYSIS_PROMPT

    log_agent_action(self.name, "Analyze target URL", task_description[:50])

    if _looks_like_weather_task(task_description):
        direct_url = _extract_first_url(task_description)
        if direct_url and any(domain in direct_url.lower() for domain in _WEATHER_DOMAINS):
            result = {
                "url": direct_url,
                "backup_urls": [],
                "need_search": False,
                "search_query": "",
            }
            log_agent_action(self.name, "Resolved target URL", direct_url)
            return result

        preferred_domain = _extract_preferred_weather_domain(task_description)
        query = _build_weather_site_query(task_description, preferred_domain)
        result = {
            "url": "",
            "backup_urls": [],
            "need_search": True,
            "search_query": query,
        }
        log_agent_action(self.name, "Resolved target URL", query)
        return result

    task_signature = self.cache.build_task_signature(task_description)
    cache_key = self.cache.build_key(
        "url_analysis",
        task_signature=task_signature,
        prompt_version="url_analysis_prompt_v1",
        model_name=getattr(self.llm, "model", ""),
    )
    cached = self.cache.get(cache_key)
    if isinstance(cached, dict):
        log_agent_action(self.name, "Reuse cached URL analysis", task_description[:50])
        log_debug_metrics("llm_cache.url_analysis", self.cache.snapshot_stats())
        return cached

    async def _compute_url_analysis() -> Dict[str, Any]:
        try:
            response = await asyncio.to_thread(
                self.llm.chat_with_system,
                system_prompt=URL_ANALYSIS_PROMPT.format(task_description=task_description),
                user_message="Please infer the best target URL for this task",
                temperature=0.2,
                json_mode=True,
            )
            result = self.llm.parse_json_response(response)
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
    from agents.web_worker import (
        PAGE_ANALYSIS_PROMPT,
        RE_HTML_COMMENT,
        RE_SCRIPT_TAG,
        RE_STYLE_TAG,
        RE_WHITESPACE,
    )

    log_agent_action(self.name, "Analyze page structure")
    url_result = await tk.get_current_url()
    html_result = await tk.get_page_html()
    html = html_result.data or ""

    html = RE_SCRIPT_TAG.sub("", html)
    html = RE_STYLE_TAG.sub("", html)
    html = RE_HTML_COMMENT.sub("", html)
    html = RE_WHITESPACE.sub(" ", html)
    normalized_url = self.cache.normalize_url(url_result.data or "")
    task_signature = self.cache.build_task_signature(task_description)
    page_fingerprint = self.cache.build_page_fingerprint(html)
    cache_key = self.cache.build_key(
        "page_structure_analysis",
        normalized_url=normalized_url,
        task_signature=task_signature,
        page_fingerprint=page_fingerprint,
        prompt_version="page_analysis_prompt_v1",
        model_name=getattr(self.llm, "model", ""),
    )
    cached = self.cache.get(cache_key)
    if isinstance(cached, dict):
        log_agent_action(self.name, "Reuse cached page analysis", normalized_url[:80])
        log_debug_metrics("llm_cache.page_analysis", self.cache.snapshot_stats())
        return cached

    if len(html) > 100000:
        html = html[:100000] + "\n... (truncated)"

    async def _compute_page_analysis() -> Dict[str, Any]:
        try:
            response = await asyncio.to_thread(
                self.llm.chat_with_system,
                system_prompt=PAGE_ANALYSIS_PROMPT.format(
                    task_description=task_description,
                    html_content=html,
                    current_url=url_result.data or "",
                ),
                user_message="Please analyze the page structure and return selectors",
                temperature=0.2,
                json_mode=True,
            )
            config = self.llm.parse_json_response(response)
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
                        "prompt_version": "page_analysis_prompt_v1",
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
