"""
Clean singleflight-backed analysis helpers for WebWorker.
This keeps the brittle mixed-encoding source file small while moving the
maintainable logic into an ASCII-only module.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict

from config.settings import settings
from utils.logger import log_agent_action, log_debug_metrics, log_error
from utils.browser_toolkit import BrowserToolkit


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

    if len(html) > 15000:
        html = html[:15000] + "\n... (truncated)"

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
                max_tokens=4096,
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
