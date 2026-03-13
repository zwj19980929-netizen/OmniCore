"""
OmniCore 智能 Web Worker Agent
自适应网页爬取：自主搜索、页面理解、反爬应对、自动导航、验证码处理
所有浏览器操作通过 BrowserToolkit 完成。
"""
import asyncio
import base64
import json
import random
import re
from typing import Dict, Any, List, Optional, Set, Tuple
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse
import requests

from core.state import OmniCoreState, TaskItem
from core.llm import LLMClient
from core.llm_cache import get_llm_cache
from utils.logger import (
    log_agent_action,
    log_debug_metrics,
    logger,
    log_success,
    log_error,
    log_warning,
)
from utils.browser_toolkit import BrowserToolkit, ToolkitResult
from utils.retry import async_retry, is_retryable
from config.settings import settings

# ==================== 预编译正则表达式 ====================
# HTML 清理相关
RE_SCRIPT_TAG = re.compile(r"<script[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE)
RE_STYLE_TAG = re.compile(r"<style[^>]*>.*?</style>", re.DOTALL | re.IGNORECASE)
RE_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
RE_HTML_TAG = re.compile(r"<[^>]+>")
RE_WHITESPACE = re.compile(r"\s+")

# 链接提取相关
RE_HEADING_WITH_LINK = re.compile(
    r"<(h1|h2|h3)[^>]*>.*?<a[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)
RE_ANCHOR_TAG = re.compile(
    r"<a[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
    re.DOTALL | re.IGNORECASE,
)

# 文本块提取相关
RE_PARAGRAPH_BLOCK = re.compile(r"<(p|li)[^>]*>(.*?)</\1>", re.DOTALL | re.IGNORECASE)
RE_CONTENT_BLOCK = re.compile(
    r"<(main|article|section|div)[^>]*>(.*?)</\1>", re.DOTALL | re.IGNORECASE
)

# 分词相关
RE_TOKEN_SPLIT = re.compile(r"[\s,.;:|/\\]+")
RE_DIRECT_URL = re.compile(r"https?://[^\s)>\]\"']+", re.IGNORECASE)
RE_DOMAIN_HINT = re.compile(r"\b(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}\b")
RE_WORD_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*|[\u4e00-\u9fff]{1,}")

SEARCH_QUERY_PLANNER_PROMPT = """You plan concise search-engine queries for a web research agent.

Return JSON with this schema:
{
  "queries": ["short query 1", "short query 2"],
  "preferred_domains": ["example.com"],
  "reasoning": "brief reason"
}

Rules:
- Output 1 to 4 short search queries only.
- Queries must be concise search phrases, not full task instructions.
- Preserve important entities, dates, locations, and source/domain hints.
- If a domain is important, use site:domain in one of the queries.
- Prefer article/report/statement discovery queries over homepage navigation queries.
"""

SEARCH_RESULT_RANKING_PROMPT = """You are ranking search-result cards for a web research task.

Return JSON with this schema:
{
  "selected_indexes": [1, 3],
  "serp_sufficient": false,
  "reasoning": "brief reason"
}

Rules:
- Select the results most likely to contain evidence needed for the task.
- Prefer direct articles/statements/report pages over homepages, section pages, category pages, docs, forums, and ecommerce pages.
- If the visible snippets alone are already enough to answer the task safely, set serp_sufficient to true.
- Use the 1-based indexes from the provided result list.
"""

GENERIC_SEARCH_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "current", "detail", "details",
    "extract", "find", "for", "from", "get", "give", "how", "in", "into", "latest",
    "most", "news", "of", "on", "or", "recent", "report", "reports", "search",
    "source", "sources", "statement", "statements", "that", "the", "their", "this",
    "to", "using", "verify", "with",
    "一下", "一些", "使用", "信息", "内容", "分析", "声明", "报道", "搜索", "提取", "搜集",
    "最新", "最近", "材料", "核实", "来源", "请", "请你", "请帮我", "资料", "通过",
}

# ==================== 延迟导入 ====================
def _import_paod():
    from agents.paod import (
        classify_failure, make_trace_step, evaluate_success_criteria,
        execute_fallback, MAX_FALLBACK_ATTEMPTS,
    )
    return classify_failure, make_trace_step, evaluate_success_criteria, execute_fallback, MAX_FALLBACK_ATTEMPTS


# URL 和导航分析提示词
URL_ANALYSIS_PROMPT = """你是一个智能网站导航专家。根据用户任务，推理出最佳的目标 URL。

## 用户任务
{task_description}

## 你的工作方式
1. 先理解用户到底想访问什么网站、获取什么数据
2. 根据你的知识推理出最可能的 URL（注意区分名称相似但完全不同的网站）
3. 如果你不确定具体 URL，设置 need_search 为 true，让系统通过搜索引擎查找
4. 优先给出具体的数据列表页 URL，而不是网站首页

## 返回 JSON
```json
{{
    "url": "最可能包含目标数据的完整 URL",
    "backup_urls": ["备选 URL 列表"],
    "need_search": false,
    "search_query": "如果 need_search 为 true，这里填搜索词"
}}
```

重要：如果你对 URL 没有把握，宁可设置 need_search=true 让搜索引擎帮忙，也不要瞎猜。
"""

# 页面分析提示词
PAGE_ANALYSIS_PROMPT = """你是一个网页结构分析专家。请分析以下 HTML，找出目标数据的 CSS 选择器。

## 任务目标
{task_description}

## 页面 HTML (已截取关键部分)
```html
{html_content}
```

## 页面当前 URL
{current_url}

## 你的工作方式
1. 仔细阅读 HTML 结构，理解页面布局
2. 找出包含目标数据的重复元素（列表项、表格行等）
3. 为每个需要提取的字段确定精确的 CSS 选择器
4. 如果页面结构不明确，给出你最有把握的选择器

返回 JSON 格式：
```json
{{
    "success": true,
    "item_selector": "每一条数据项的选择器（如 tr, li, div.item 等）",
    "fields": {{
        "title": "标题文本的选择器（相对于 item）",
        "link": "链接的选择器（相对于 item，a 标签会自动提取 href）",
        "date": "日期的选择器（可选）",
        "severity": "严重程度/等级的选择器（可选）",
        "id": "编号/ID 的选择器（可选）"
    }},
    "need_click_first": false,
    "click_selector": "如果需要先点击某元素才能看到数据，填写选择器",
    "notes": "其他注意事项"
}}
```

注意：
- item_selector 应该能选中多个重复的数据项
- fields 中的选择器是相对于每个 item 的
- 只填你在 HTML 中确实看到的选择器，看不到的字段留空字符串
"""


class WebWorker:
    """
    智能 Web Worker Agent
    具备自主搜索、页面理解、反爬应对能力
    所有浏览器操作通过 BrowserToolkit 完成。
    """

    def __init__(self, llm_client: LLMClient = None):
        self.name = "WebWorker"
        self.llm = llm_client or LLMClient()
        self.cache = get_llm_cache()
        self.fast_mode = settings.BROWSER_FAST_MODE
        self.block_heavy_resources = settings.BLOCK_HEAVY_RESOURCES
        self.static_fetch_enabled = settings.STATIC_FETCH_ENABLED

    def _create_toolkit(self, headless: bool = True) -> BrowserToolkit:
        return BrowserToolkit(
            headless=headless,
            fast_mode=self.fast_mode,
            block_heavy_resources=self.block_heavy_resources,
        )

    def _extract_direct_urls(self, text: str) -> List[str]:
        urls: List[str] = []
        seen: Set[str] = set()
        for match in RE_DIRECT_URL.findall(text or ""):
            url = str(match or "").strip().rstrip(".,)")
            if not url or url in seen:
                continue
            seen.add(url)
            urls.append(url)
        return urls

    def _extract_domain_hints(self, text: str) -> List[str]:
        domains: List[str] = []
        seen: Set[str] = set()
        for match in RE_DOMAIN_HINT.findall(text or ""):
            value = str(match or "").strip().lower().strip(".,)")
            if not value or "@" in value or value in seen:
                continue
            seen.add(value)
            domains.append(value)
        return domains

    def _tokenize_query_terms(self, text: str) -> List[str]:
        terms: List[str] = []
        seen: Set[str] = set()
        for token in RE_WORD_TOKEN.findall(text or ""):
            value = str(token or "").strip().lower()
            if not value:
                continue
            if value in GENERIC_SEARCH_STOPWORDS:
                continue
            if len(value) == 1 and not re.search(r"[\u4e00-\u9fff]", value):
                continue
            if value in seen:
                continue
            seen.add(value)
            terms.append(value)
        return terms

    def _compact_query_text(self, text: str, max_terms: int = 8) -> str:
        terms = self._tokenize_query_terms(text)
        if not terms:
            return str(text or "").strip()[:80]
        return " ".join(terms[:max_terms]).strip()

    def _build_site_search_query(self, task_description: str, domain: str) -> str:
        compact = self._compact_query_text(task_description, max_terms=6)
        if not compact:
            compact = domain
        return f"site:{domain} {compact}".strip()

    def _fallback_search_queries(
        self,
        task_description: str,
        *,
        base_query: str = "",
        domain_hints: Optional[List[str]] = None,
        max_queries: int = 3,
    ) -> List[str]:
        domain_hints = [item for item in (domain_hints or []) if item]
        queries: List[str] = []
        candidates = [str(base_query or "").strip(), self._compact_query_text(task_description, max_terms=8)]

        if domain_hints:
            candidates.insert(0, self._build_site_search_query(task_description, domain_hints[0]))
            if base_query:
                candidates.append(f"site:{domain_hints[0]} {self._compact_query_text(base_query, max_terms=8)}".strip())

        seen: Set[str] = set()
        for candidate in candidates:
            value = re.sub(r"\s+", " ", str(candidate or "").strip())
            if not value or value in seen:
                continue
            seen.add(value)
            queries.append(value)
            if len(queries) >= max_queries:
                break
        return queries

    def _is_probably_detail_url(self, url: str) -> bool:
        try:
            parsed = urlparse(url or "")
        except Exception:
            return False
        path = (parsed.path or "").strip("/")
        if not path:
            return False
        if re.search(r"\.(?:html?|shtml|php|aspx?)$", path, re.IGNORECASE):
            return True
        segments = [segment for segment in path.split("/") if segment]
        if len(segments) >= 2 and any(any(ch.isdigit() for ch in segment) for segment in segments):
            return True
        if any("-" in segment and len(segment) >= 12 for segment in segments):
            return True
        return len(segments) >= 3

    # ── URL determination (pure LLM, no browser) ────────────

    async def determine_target_url(self, task_description: str) -> Dict[str, Any]:
        log_agent_action(self.name, "分析目标 URL", task_description[:50])
        direct_urls = self._extract_direct_urls(task_description)
        if direct_urls:
            return {
                "url": direct_urls[0],
                "backup_urls": direct_urls[1:3],
                "need_search": False,
                "search_query": "",
            }

        domain_hints = self._extract_domain_hints(task_description)
        if self._task_mentions_weather(task_description) and domain_hints:
            return {
                "url": "",
                "backup_urls": [],
                "need_search": True,
                "search_query": self._build_site_search_query(task_description, domain_hints[0]),
            }

        task_signature = self.cache.build_task_signature(task_description)
        cache_key = self.cache.build_key(
            "url_analysis",
            task_signature=task_signature,
            prompt_version="url_analysis_prompt_v1",
            model_name=getattr(self.llm, "model", ""),
        )
        cached = self.cache.get(cache_key)
        if isinstance(cached, dict):
            log_agent_action(self.name, "命中 URL 分析缓存", task_description[:50])
            log_debug_metrics("llm_cache.url_analysis", self.cache.snapshot_stats())
            return cached
        response = self.llm.chat_with_system(
            system_prompt=URL_ANALYSIS_PROMPT.format(task_description=task_description),
            user_message="请分析应该访问哪个 URL",
            temperature=0.2, json_mode=True,
        )
        try:
            result = self.llm.parse_json_response(response)
            result.setdefault("backup_urls", [])
            result.setdefault("need_search", False)
            result.setdefault("search_query", "")
            url_value = str(result.get("url", "") or "").strip()
            if url_value and not self._is_probably_detail_url(url_value):
                domain = str(urlparse(url_value).netloc or "").strip()
                result["backup_urls"] = [url_value] + [
                    str(item).strip()
                    for item in (result.get("backup_urls") or [])
                    if str(item).strip() and str(item).strip() != url_value
                ]
                result["url"] = ""
                result["need_search"] = True
                if domain and not str(result.get("search_query", "") or "").strip():
                    result["search_query"] = self._build_site_search_query(task_description, domain)
            if result.get("url") or result.get("need_search"):
                self.cache.set(
                    cache_key,
                    result,
                    settings.URL_ANALYSIS_CACHE_TTL_SECONDS,
                )
                log_debug_metrics("llm_cache.url_analysis", self.cache.snapshot_stats())
            log_agent_action(self.name, "目标 URL", result.get("url", "未知"))
            return result
        except Exception as e:
            log_error(f"URL 分析失败: {e}")
            return {
                "url": "",
                "backup_urls": [],
                "need_search": True,
                "search_query": self._compact_query_text(task_description, max_terms=8) or task_description,
            }

    # ── static fetch (no browser) ──────────────────────────────

    def _can_use_static_fetch(self, task_description: str, url: Optional[str]) -> bool:
        if not self.static_fetch_enabled or not url:
            return False
        desc = (task_description or "").lower()
        interactive_keywords = [
            "登录", "注册", "填写", "点击", "提交", "支付", "购买",
            "login", "sign in", "register", "click", "submit", "checkout", "buy",
        ]
        return not any(k in desc for k in interactive_keywords)

    def _clean_html_text(self, raw_html: str) -> str:
        html = RE_SCRIPT_TAG.sub("", raw_html)
        html = RE_STYLE_TAG.sub("", html)
        html = RE_HTML_COMMENT.sub("", html)
        return html

    def _strip_tags(self, text: str) -> str:
        text = RE_HTML_TAG.sub(" ", text)
        text = RE_WHITESPACE.sub(" ", text)
        return text.strip()

    def _is_noise_link(self, text: str, href: str) -> bool:
        t = (text or "").strip().lower()
        h = (href or "").strip().lower()
        if not t or len(t) < 4:
            return True
        if h.startswith("javascript:") or h.startswith("mailto:") or h == "#" or not h:
            return True
        noise_keywords = [
            "login", "register", "privacy", "terms", "cookie", "help", "about",
            "登录", "注册", "隐私", "条款", "帮助", "关于", "更多",
        ]
        return any(k in t for k in noise_keywords)

    def _score_static_link(self, text: str, href: str, task_description: str) -> int:
        score = 0
        t = text.lower()
        task = (task_description or "").lower()
        if len(text) >= 12:
            score += 2
        if href.startswith("http"):
            score += 1
        for token in RE_TOKEN_SPLIT.split(task):
            token = token.strip()
            if len(token) >= 3 and token in t:
                score += 2
        return score

    def _prefers_static_text(self, task_description: str) -> bool:
        desc = (task_description or "").lower()
        text_keywords = [
            "read", "summary", "summarize", "extract text", "article", "content",
            "weather", "forecast", "temperature", "humidity", "air quality", "wind",
            "读取", "总结", "概述", "正文", "文章", "内容",
        ]
        return any(k in desc for k in text_keywords) or self._task_mentions_weather(task_description)

    def _task_mentions_weather(self, task_description: str) -> bool:
        desc = (task_description or "").lower()
        weather_keywords = [
            "weather", "forecast", "temperature", "humidity", "air quality", "aqi", "wind",
            "天气", "预报", "气温", "湿度", "空气质量", "风力",
        ]
        return any(keyword in desc for keyword in weather_keywords)

    def _looks_like_weather_text(self, text: str) -> bool:
        value = (text or "").lower()
        weather_signals = [
            "°c", "℃", "temperature", "humidity", "air quality", "aqi", "wind",
            "weather", "forecast", "today", "tomorrow",
            "气温", "湿度", "空气质量", "风力", "天气", "预报", "今天", "明天",
        ]
        return any(signal in value for signal in weather_signals) or bool(re.search(r"\b\d{1,2}\s*(?:°c|℃)\b", value))

    def _static_data_looks_useful(self, task_description: str, data: List[Dict[str, Any]]) -> bool:
        if not data:
            return False
        detail_task = self._prefers_static_text(task_description) or self._task_mentions_weather(task_description)
        if not detail_task:
            return True
        text_items = [
            str(item.get("text", "") or "").strip()
            for item in data
            if isinstance(item, dict) and str(item.get("text", "") or "").strip()
        ]
        if not text_items:
            return False
        if self._task_mentions_weather(task_description):
            return any(self._looks_like_weather_text(item) for item in text_items[:8])
        return True

    def _extract_static_links(self, html: str, base_url: str, task_description: str, limit: int) -> List[Dict[str, Any]]:
        cleaned = self._clean_html_text(html)
        candidates: List[Dict[str, Any]] = []
        seen = set()

        def _append(href: str, raw_text: str):
            text = self._strip_tags(raw_text)
            full_href = urljoin(base_url, href.strip())
            if self._is_noise_link(text, full_href):
                return
            key = (text[:80], full_href)
            if key in seen:
                return
            seen.add(key)
            candidates.append({
                "title": text[:160], "link": full_href,
                "_score": self._score_static_link(text, full_href, task_description),
                "_order": len(candidates),
            })

        for match in RE_HEADING_WITH_LINK.finditer(cleaned):
            _append(match.group(2), match.group(3))
        if len(candidates) < limit:
            for match in RE_ANCHOR_TAG.finditer(cleaned):
                _append(match.group(1), match.group(2))
                if len(candidates) >= max(limit * 4, 20):
                    break
        candidates.sort(key=lambda x: (-x["_score"], x["_order"]))
        results = candidates[:limit]
        for item in results:
            item.pop("_score", None)
            item.pop("_order", None)
        return results

    def _extract_static_text_blocks(self, html: str, limit: int, task_description: str = "") -> List[Dict[str, Any]]:
        cleaned = self._clean_html_text(html)
        blocks: List[Dict[str, Any]] = []
        seen = set()
        patterns = [RE_PARAGRAPH_BLOCK, RE_CONTENT_BLOCK]
        weather_task = self._task_mentions_weather(task_description)
        for pattern in patterns:
            for match in pattern.finditer(cleaned):
                text = self._strip_tags(match.group(2))
                min_length = 12 if weather_task else 40
                if len(text) < min_length:
                    continue
                if weather_task and not self._looks_like_weather_text(text) and len(text) < 40:
                    continue
                key = RE_WHITESPACE.sub(" ", text).strip().lower()[:120]
                if key in seen:
                    continue
                seen.add(key)
                blocks.append({"text": text[:400]})
                if len(blocks) >= limit:
                    return blocks
        return blocks

    def _static_fetch(self, url: str, task_description: str, limit: int) -> Dict[str, Any]:
        try:
            resp = requests.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                },
                timeout=15,
            )
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "").lower()
            if "html" not in content_type:
                return {"success": False, "error": f"非HTML响应: {content_type}", "data": [], "url": url}
            current_encoding = str(getattr(resp, "encoding", "") or "").lower()
            if current_encoding in {"", "iso-8859-1", "latin-1", "ascii"}:
                apparent_encoding = str(getattr(resp, "apparent_encoding", "") or "").strip()
                if apparent_encoding:
                    try:
                        resp.encoding = apparent_encoding
                    except Exception:
                        pass
            html = str(getattr(resp, "text", "") or "")
            text_data = self._extract_static_text_blocks(html, max(3, min(limit, 6)), task_description)
            link_data = self._extract_static_links(html, url, task_description, limit)
            if self._prefers_static_text(task_description):
                data = text_data or link_data
            else:
                data = link_data or text_data
            if not data:
                return {"success": False, "error": "静态抓取未提取到有效内容", "data": [], "url": url}
            if not self._static_data_looks_useful(task_description, data):
                return {"success": False, "error": "static fetch returned navigation links instead of usable detail data", "data": [], "url": url}
            return {"success": True, "data": data, "count": len(data), "source": url,
                    "mode": "static_fetch_text" if "text" in data[0] else "static_fetch"}
        except Exception as e:
            return {"success": False, "error": str(e), "data": [], "url": url}

    # ── data quality validation (pure LLM) ───────────────────

    def validate_data_quality(self, data: List[Dict], task_description: str, limit: int) -> Dict[str, Any]:
        if not data:
            return {"valid": False, "reason": "数据为空", "suggestion": "换页面或换选择器"}
        sample = data[:3]
        sample_str = json.dumps(sample, ensure_ascii=False, default=str)[:1500]
        from utils.prompt_manager import get_prompt
        validation_prompt = get_prompt("web_worker_data_validation")
        response = self.llm.chat_with_system(
            system_prompt=validation_prompt,
            user_message=f"任务：{task_description}\n\n抓到的数据样本（前3条）：\n{sample_str}\n\n共抓到 {len(data)} 条，要求 {limit} 条",
            temperature=0.2, json_mode=True,
        )
        try:
            return self.llm.parse_json_response(response)
        except Exception:
            return {"valid": True, "reason": "审查失败，默认通过", "suggestion": ""}

    # ── search (uses toolkit) ──────────────────────────────────

    def plan_search_queries(
        self,
        task_description: str,
        *,
        base_query: str = "",
        domain_hints: Optional[List[str]] = None,
        max_queries: int = 3,
    ) -> List[str]:
        domain_hints = [item for item in (domain_hints or self._extract_domain_hints(task_description)) if item]
        fallback = self._fallback_search_queries(
            task_description,
            base_query=base_query,
            domain_hints=domain_hints,
            max_queries=max_queries,
        )
        payload = {
            "task": task_description,
            "base_query": base_query,
            "domain_hints": domain_hints,
            "fallback_queries": fallback,
        }
        try:
            response = self.llm.chat_with_system(
                system_prompt=SEARCH_QUERY_PLANNER_PROMPT,
                user_message=json.dumps(payload, ensure_ascii=False),
                temperature=0.2,
                json_mode=True,
            )
            parsed = self.llm.parse_json_response(response)
            queries: List[str] = []
            seen: Set[str] = set()
            for item in (parsed.get("queries") or []):
                value = re.sub(r"\s+", " ", str(item or "").strip())
                if not value or value in seen:
                    continue
                seen.add(value)
                queries.append(value)
                if len(queries) >= max_queries:
                    break
            return queries or fallback
        except Exception:
            return fallback

    def _score_search_result_candidate(self, card: Dict[str, Any], task_description: str, query: str) -> int:
        haystack = " ".join(
            [
                str(card.get("title", "") or ""),
                str(card.get("snippet", "") or ""),
                str(card.get("source", "") or ""),
                str(card.get("link", "") or ""),
            ]
        ).lower()
        score = 0
        for token in self._tokenize_query_terms(f"{task_description} {query}"):
            if token in haystack:
                score += 3
        if self._is_probably_detail_url(str(card.get("link", "") or "")):
            score += 2
        if len(str(card.get("snippet", "") or "").strip()) >= 30:
            score += 1
        return score

    def _rerank_search_results(
        self,
        task_description: str,
        query: str,
        cards: List[Dict[str, Any]],
        max_results: int = 5,
    ) -> Tuple[List[Dict[str, Any]], bool]:
        if not cards:
            return [], False
        ranked_fallback = sorted(
            cards,
            key=lambda card: self._score_search_result_candidate(card, task_description, query),
            reverse=True,
        )
        if len(cards) == 1:
            return ranked_fallback[:1], False

        payload_cards = []
        for idx, card in enumerate(cards[:12], 1):
            payload_cards.append(
                {
                    "index": idx,
                    "title": str(card.get("title", "") or "")[:200],
                    "source": str(card.get("source", "") or "")[:80],
                    "snippet": str(card.get("snippet", "") or "")[:320],
                    "link": str(card.get("link", "") or "")[:240],
                }
            )
        try:
            response = self.llm.chat_with_system(
                system_prompt=SEARCH_RESULT_RANKING_PROMPT,
                user_message=json.dumps(
                    {
                        "task": task_description,
                        "query": query,
                        "results": payload_cards,
                    },
                    ensure_ascii=False,
                ),
                temperature=0.1,
                json_mode=True,
            )
            parsed = self.llm.parse_json_response(response)
            selected_indexes = []
            for item in (parsed.get("selected_indexes") or []):
                try:
                    selected_indexes.append(int(item))
                except Exception:
                    continue
            chosen: List[Dict[str, Any]] = []
            seen_links: Set[str] = set()
            for index in selected_indexes:
                if index < 1 or index > len(cards):
                    continue
                card = cards[index - 1]
                link = str(card.get("link", "") or "")
                if not link or link in seen_links:
                    continue
                seen_links.add(link)
                chosen.append(card)
                if len(chosen) >= max_results:
                    break
            if chosen:
                return chosen, bool(parsed.get("serp_sufficient"))
        except Exception:
            pass
        return ranked_fallback[:max_results], False

    async def _extract_search_result_cards(
        self,
        tk: BrowserToolkit,
        query: str,
        max_results: int,
    ) -> List[Dict[str, Any]]:
        result = await tk.evaluate_js(
            """(limit) => {
                const root = document.querySelector('#b_results, #search, main, [role="main"]') || document.body;
                const candidates = Array.from(root.querySelectorAll('li, article, div')).slice(0, 500);
                const compact = (text) => (text || '').replace(/\\s+/g, ' ').trim();
                const seen = new Set();
                const cards = [];

                const getSnippet = (node, title) => {
                    const parts = [];
                    const snippetNodes = node.querySelectorAll('p, .b_caption p, .snippet, .c-abstract, [class*="snippet"], [class*="caption"], [data-testid*="snippet"]');
                    for (const child of snippetNodes) {
                        const text = compact(child.innerText || child.textContent || '');
                        if (text && text !== title) parts.push(text);
                        if (parts.join(' ').length > 260) break;
                    }
                    if (!parts.length) {
                        const raw = compact(node.innerText || node.textContent || '');
                        const trimmed = raw.startsWith(title) ? raw.slice(title.length).trim() : raw;
                        if (trimmed) parts.push(trimmed);
                    }
                    return compact(parts.join(' ')).slice(0, 320);
                };

                for (const node of candidates) {
                    const anchor = node.querySelector('h1 a[href], h2 a[href], h3 a[href], h4 a[href], a[href]');
                    if (!anchor) continue;
                    const href = anchor.href || anchor.getAttribute('href') || '';
                    if (!href || !/^https?:/i.test(href)) continue;
                    if (seen.has(href)) continue;
                    const titleNode = node.querySelector('h1, h2, h3, h4') || anchor;
                    const title = compact(titleNode.innerText || titleNode.textContent || '').slice(0, 220);
                    if (!title || title.length < 8) continue;
                    seen.add(href);
                    const sourceNode = node.querySelector('cite, .source, .b_attribution, [class*="source"], [data-testid*="source"]');
                    const dateNode = node.querySelector('time, [datetime], .news_dt, [class*="date"]');
                    cards.push({
                        title,
                        link: href,
                        source: compact(sourceNode ? (sourceNode.innerText || sourceNode.textContent || '') : '').slice(0, 120),
                        date: compact(dateNode ? (dateNode.innerText || dateNode.textContent || '') : '').slice(0, 80),
                        snippet: getSnippet(node, title),
                    });
                    if (cards.length >= Math.max(limit * 4, 12)) break;
                }
                return cards;
            }""",
            max_results,
        )
        cards = result.data if result.success and isinstance(result.data, list) else []
        filtered: List[Dict[str, Any]] = []
        seen_links: Set[str] = set()
        for item in cards:
            if not isinstance(item, dict):
                continue
            link = self._decode_redirect_url(str(item.get("link", "") or "").strip())
            if not link or not link.startswith("http"):
                continue
            if self._is_search_engine_domain(link):
                continue
            if link in seen_links:
                continue
            seen_links.add(link)
            title = str(item.get("title", "") or "").strip()
            if not title:
                continue
            filtered.append(
                {
                    "title": title,
                    "link": link,
                    "source": str(item.get("source", "") or "").strip(),
                    "date": str(item.get("date", "") or "").strip(),
                    "snippet": str(item.get("snippet", "") or "").strip(),
                }
            )
        ranked, _ = self._rerank_search_results(query, query, filtered, max_results=max_results)
        return ranked

    async def _wait_for_search_results_ready(self, tk: BrowserToolkit, search_url: str) -> bool:
        selectors_by_host = {
            "bing.com": "#b_results, li.b_algo, .b_ans, .b_algo",
            "google.com": "#search, div.g, .tF2Cxc, [data-sokoban-container]",
            "duckduckgo.com": ".results, .result, .result__body, .result__a",
        }
        host = str(urlparse(search_url).netloc or "").lower()
        selector = "#b_results, #search, .results, [role='main']"
        for domain, value in selectors_by_host.items():
            if domain in host:
                selector = value
                break

        for _ in range(4):
            await tk.wait_for_load("domcontentloaded", timeout=8000)
            await tk.wait_for_load("networkidle", timeout=3000)
            wait_result = await tk.wait_for_selector(selector, timeout=3000)
            if wait_result.success:
                summary = await tk.evaluate_js(
                    """(sel) => {
                        const nodes = Array.from(document.querySelectorAll(sel));
                        const visible = nodes.filter((node) => {
                            const style = window.getComputedStyle(node);
                            return style && style.visibility !== 'hidden' && style.display !== 'none';
                        });
                        return {
                            matches: visible.length,
                            textLength: (document.body && document.body.innerText ? document.body.innerText.length : 0),
                        };
                    }""",
                    selector,
                )
                if summary.success and isinstance(summary.data, dict):
                    if int(summary.data.get("matches", 0) or 0) > 0:
                        return True
                    if int(summary.data.get("textLength", 0) or 0) >= 300:
                        return True
            await tk.human_delay(400, 1200)
        return False

    async def _perform_native_search(
        self,
        tk: BrowserToolkit,
        homepage: str,
        search_input_selectors: List[str],
        query: str,
    ) -> bool:
        """
        模拟真实用户在搜索引擎首页输入关键词并搜索
        返回是否成功执行搜索
        """
        # 1. 访问搜索引擎首页
        goto_r = await tk.goto(homepage)
        if not goto_r.success:
            log_warning(f"无法访问 {homepage}")
            return False

        await tk.human_delay(500, 1200)

        # 2. 尝试找到搜索框并输入
        search_selector = None
        for selector in search_input_selectors:
            # 检查元素是否存在
            exists_r = await tk.element_exists(selector)
            if exists_r.success and exists_r.data:
                search_selector = selector
                break

        if not search_selector:
            log_warning(f"未找到搜索框: {homepage}")
            return False

        # 3. 输入搜索关键词
        type_r = await tk.type_text(search_selector, query, delay=80)
        if not type_r.success:
            log_warning(f"无法输入搜索词: {query}")
            return False

        await tk.human_delay(300, 800)

        # 4. 按回车提交搜索（在整个页面上按键，因为输入框已经聚焦）
        press_r = await tk.press_key("Enter")
        if not press_r.success:
            log_warning("按回车失败")
            return False

        # 5. 等待搜索结果加载
        await tk.human_delay(1000, 2000)
        return True

    async def search_for_result_cards(
        self,
        query: str,
        *,
        task_description: str = "",
        max_results: int = 5,
        headless: Optional[bool] = None,
        tk: BrowserToolkit = None,
    ) -> List[Dict[str, Any]]:
        log_agent_action(self.name, "搜索候选网站", query[:60])
        own_tk = tk is None
        if own_tk:
            tk = self._create_toolkit(headless=True if headless is None else headless)
            await tk.create_page()
        cards: List[Dict[str, Any]] = []
        try:
            # 定义搜索引擎配置：首页 + 搜索框选择器
            search_engines = [
                {
                    "name": "Google",
                    "homepage": "https://www.google.com",
                    "selectors": ["input[name='q']", "textarea[name='q']", "#APjFqb"],
                },
                {
                    "name": "Bing",
                    "homepage": "https://www.bing.com",
                    "selectors": ["input[name='q']", "#sb_form_q"],
                },
                {
                    "name": "Baidu",
                    "homepage": "https://www.baidu.com",
                    "selectors": ["input[name='wd']", "#kw"],
                },
            ]

            for engine in search_engines:
                log_agent_action(self.name, f"尝试 {engine['name']} 原生搜索", query[:40])

                # 使用原生搜索框输入
                success = await self._perform_native_search(
                    tk,
                    engine["homepage"],
                    engine["selectors"],
                    query,
                )

                if not success:
                    continue

                # 等待结果加载
                current_url = (await tk.get_url()).data or ""
                ready = await self._wait_for_search_results_ready(tk, current_url)
                if not ready:
                    await tk.human_delay(800, 1800)

                # 提取搜索结果
                raw_cards = await self._extract_search_result_cards(tk, query, max_results=max_results * 2)
                if raw_cards:
                    cards, _ = self._rerank_search_results(
                        task_description or query,
                        query,
                        raw_cards,
                        max_results=max_results,
                    )
                    if cards:
                        log_success(f"{engine['name']} 搜索成功，找到 {len(cards)} 个结果")
                        break

            if not cards:
                log_warning("所有搜索引擎均未找到结果")
        except Exception as e:
            log_error(f"搜索失败: {e}")
        finally:
            if own_tk:
                await tk.close()
        return cards

    async def search_for_url(self, query: str) -> Optional[str]:
        cards = await self.search_for_result_cards(query, task_description=query, max_results=1)
        if not cards:
            return None
        return str(cards[0].get("link", "") or "").strip() or None

    @staticmethod
    def _decode_redirect_url(href: str) -> str:
        if not href:
            return ""
        try:
            parsed = urlparse(href)
        except Exception:
            return href
        host = (parsed.netloc or "").lower()
        if "bing.com" not in host and "duckduckgo.com" not in host:
            return href

        qs = parse_qs(parsed.query or "")
        candidate = (
            (qs.get("uddg") or [None])[0]
            or (qs.get("u") or [None])[0]
            or (qs.get("url") or [None])[0]
        )
        if not candidate:
            return href
        if candidate.startswith("http"):
            return candidate

        # Bing sometimes uses u=a1<base64url(url)>
        if candidate.startswith("a1"):
            raw = candidate[2:]
            padding = "=" * ((4 - len(raw) % 4) % 4)
            try:
                decoded = base64.urlsafe_b64decode((raw + padding).encode("ascii")).decode("utf-8", errors="ignore")
                if decoded.startswith("http"):
                    return decoded
            except Exception:
                pass
        return href

    @staticmethod
    def _is_search_engine_domain(url: str) -> bool:
        try:
            host = (urlparse(url).netloc or "").lower()
        except Exception:
            return True
        if not host:
            return True
        search_hosts = (
            "bing.com",
            "baidu.com",
            "google.com",
            "duckduckgo.com",
            "sogou.com",
            "so.com",
            "yahoo.com",
            "yandex.com",
        )
        return any(host == domain or host.endswith(f".{domain}") for domain in search_hosts)

    async def _legacy_collect_search_links(self, tk: BrowserToolkit, query: str, max_results: int) -> List[str]:
        selectors = [
            "li.b_algo h2 a",
            ".b_algo h2 a",
            ".result__a",
            "main a[href]",
            "a[href]",
            "天气", "预报", "气温", "湿度", "空气质量", "风力",
        ]
        urls: List[str] = []
        seen = set()
        query_tokens = {
            token.strip().lower()
            for token in RE_TOKEN_SPLIT.split(query or "")
            if len(token.strip()) >= 2
        }

        for selector in selectors:
            links_r = await tk.query_all(selector)
            if not links_r.success:
                continue
            for elem in (links_r.data or [])[: max_results * 12]:
                try:
                    href = await elem.get_attribute("href")
                except Exception:
                    href = None
                if not href:
                    continue
                href = self._decode_redirect_url(href.strip())
                if not href.startswith("http"):
                    continue
                if self._is_search_engine_domain(href):
                    continue
                if href in seen:
                    continue
                seen.add(href)

                # Prefer results that mention query tokens in anchor text,
                # but still keep URL as fallback to avoid empty result sets.
                include = True
                try:
                    text = str((await elem.inner_text()) or "").strip().lower()
                except Exception:
                    text = ""
                if query_tokens and text:
                    token_hits = sum(1 for token in query_tokens if token in text)
                    include = token_hits > 0 or len(urls) < max_results // 2
                if include:
                    urls.append(href)
                if len(urls) >= max_results:
                    return urls
            if urls:
                break
        return urls

    async def _collect_search_links(self, tk: BrowserToolkit, query: str, max_results: int) -> List[str]:
        selectors = [
            "li.b_algo h2 a",
            ".b_algo h2 a",
            ".result__a",
            "main a[href]",
            "a[href]",
        ]
        urls: List[str] = []
        seen = set()
        query_tokens = {
            token.strip().lower()
            for token in RE_TOKEN_SPLIT.split(query or "")
            if len(token.strip()) >= 2
        }

        for selector in selectors:
            links_r = await tk.query_all(selector)
            if not links_r.success:
                continue
            for elem in (links_r.data or [])[: max_results * 12]:
                try:
                    href = await elem.get_attribute("href")
                except Exception:
                    href = None
                if not href:
                    continue
                href = self._decode_redirect_url(href.strip())
                if not href.startswith("http"):
                    continue
                if self._is_search_engine_domain(href):
                    continue
                if href in seen:
                    continue
                seen.add(href)

                include = True
                try:
                    text = str((await elem.inner_text()) or "").strip().lower()
                except Exception:
                    text = ""
                if query_tokens and text:
                    token_hits = sum(1 for token in query_tokens if token in text)
                    include = token_hits > 0 or len(urls) < max_results // 2
                if include:
                    urls.append(href)
                if len(urls) >= max_results:
                    return urls
            if urls:
                break
        return urls

    async def search_for_urls(self, query: str, max_results: int = 5, tk: BrowserToolkit = None) -> List[str]:
        cards = await self.search_for_result_cards(
            query,
            task_description=query,
            max_results=max_results,
            tk=tk,
        )
        if cards:
            return [
                str(card.get("link", "") or "").strip()
                for card in cards
                if str(card.get("link", "") or "").strip()
            ]
        return []

    async def gather_search_candidates(
        self,
        task_description: str,
        *,
        base_query: str = "",
        domain_hints: Optional[List[str]] = None,
        max_results: int = 5,
        headless: Optional[bool] = None,
        tk: BrowserToolkit = None,
    ) -> Dict[str, Any]:
        queries = self.plan_search_queries(
            task_description,
            base_query=base_query,
            domain_hints=domain_hints,
            max_queries=min(4, max(1, max_results)),
        )
        aggregate_cards: List[Dict[str, Any]] = []
        seen_links: Set[str] = set()
        serp_sufficient = False
        for query in queries:
            cards = await self.search_for_result_cards(
                query,
                task_description=task_description,
                max_results=max_results,
                headless=headless,
                tk=tk,
            )
            ranked_cards, ranked_serp_sufficient = self._rerank_search_results(
                task_description,
                query,
                cards,
                max_results=max_results,
            )
            serp_sufficient = serp_sufficient or ranked_serp_sufficient
            for card in ranked_cards:
                link = str(card.get("link", "") or "").strip()
                if not link or link in seen_links:
                    continue
                seen_links.add(link)
                aggregate_cards.append(card)

        ranked_cards, _ = self._rerank_search_results(
            task_description,
            " ".join(queries),
            aggregate_cards,
            max_results=max_results,
        )
        if ranked_cards and not serp_sufficient:
            try:
                quality = self.validate_data_quality(
                    ranked_cards[: min(3, len(ranked_cards))],
                    task_description,
                    min(max_results, max(1, len(ranked_cards))),
                )
                serp_sufficient = bool(
                    quality.get("valid")
                    and any(str(card.get("snippet", "") or "").strip() for card in ranked_cards[:2])
                )
            except Exception:
                serp_sufficient = False
        return {
            "queries": queries,
            "cards": ranked_cards,
            "urls": [
                str(card.get("link", "") or "").strip()
                for card in ranked_cards
                if str(card.get("link", "") or "").strip()
            ],
            "serp_sufficient": serp_sufficient,
        }

    async def explore_for_data_page(self, tk: BrowserToolkit, task_description: str) -> Optional[str]:
        log_agent_action(self.name, "探索页面导航，寻找数据页面")
        r = await tk.evaluate_js("""() => {
            const anchors = document.querySelectorAll('a[href]');
            const results = [];
            for (const a of anchors) {
                const href = a.href;
                const text = a.innerText.trim();
                if (href && text && text.length < 50 && !href.startsWith('javascript:')) {
                    results.push({text, href});
                }
            }
            return results.slice(0, 50);
        }""")
        links = r.data if r.success else []
        if not links:
            return None

        url_r = await tk.get_current_url()
        links_text = "\n".join([f"- [{l['text']}]({l['href']})" for l in links])
        response = self.llm.chat_with_system(
            system_prompt=f"""你是一个网页导航专家。用户想要获取特定数据，但当前页面是网站首页或非数据页。
请从下面的链接列表中，找出最可能包含目标数据的链接。

## 用户任务
{task_description}

## 当前页面 URL
{url_r.data or ""}

## 页面上的链接
{links_text}

返回 JSON：
```json
{{"target_url": "最可能包含数据的链接URL", "reasoning": "为什么选这个链接"}}
```

如果没有合适的链接，target_url 设为空字符串。""",
            user_message="请分析哪个链接最可能包含目标数据",
            temperature=0.2, json_mode=True,
        )
        try:
            result = self.llm.parse_json_response(response)
            target = result.get("target_url", "")
            if target:
                log_agent_action(self.name, "找到数据页面", target[:80])
            return target or None
        except Exception:
            return None

    async def extract_news_links_fallback(
        self,
        tk: BrowserToolkit,
        task_description: str,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Fallback extractor for news/article pages when selector analysis returns empty."""
        r = await tk.evaluate_js(
            """(limit) => {
                const nodes = Array.from(document.querySelectorAll("a[href]"));
                const seen = new Set();
                const out = [];
                const abs = (href) => {
                    try { return new URL(href, location.href).toString(); } catch { return ""; }
                };
                const scoreText = (txt) => {
                    let s = 0;
                    if (!txt) return s;
                    const n = txt.trim().length;
                    if (n >= 18 && n <= 180) s += 2;
                    if (/[0-9]{4}|\\d{1,2}:\\d{2}/.test(txt)) s += 1;
                    return s;
                };
                for (const a of nodes) {
                    const href = abs(a.getAttribute("href") || "");
                    if (!href || !href.startsWith("http")) continue;
                    const text = (a.innerText || a.textContent || "").trim().replace(/\\s+/g, " ");
                    if (!text || text.length < 12) continue;
                    const parentSkip = a.closest("nav, header, footer, aside");
                    if (parentSkip) continue;
                    if (seen.has(href)) continue;
                    seen.add(href);
                    out.push({ title: text.slice(0, 220), link: href, _score: scoreText(text) });
                }
                out.sort((a, b) => b._score - a._score);
                return out.slice(0, Math.max(limit * 3, 30));
            }""",
            max(3, min(limit, 20)),
        )
        if not r.success or not isinstance(r.data, list):
            return []

        task_tokens = {
            token.strip().lower()
            for token in RE_TOKEN_SPLIT.split(task_description or "")
            if len(token.strip()) >= 2
        }
        filtered: List[Dict[str, Any]] = []
        for item in r.data:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "") or "").strip()
            link = str(item.get("link", "") or "").strip()
            if not title or not link:
                continue
            title_lower = title.lower()
            token_hits = sum(1 for token in task_tokens if token in title_lower)
            if token_hits == 0 and len(filtered) >= max(2, limit // 2):
                continue
            filtered.append({"title": title, "link": link})
            if len(filtered) >= limit:
                break
        return filtered

    # ── page analysis & extraction (uses toolkit) ─────────────

    async def analyze_page_structure(self, tk: BrowserToolkit, task_description: str) -> Dict[str, Any]:
        log_agent_action(self.name, "分析页面结构")
        url_r = await tk.get_current_url()
        html_r = await tk.get_page_html()
        html = html_r.data or ""

        html = RE_SCRIPT_TAG.sub('', html)
        html = RE_STYLE_TAG.sub('', html)
        html = RE_HTML_COMMENT.sub('', html)
        html = RE_WHITESPACE.sub(' ', html)
        normalized_url = self.cache.normalize_url(url_r.data or "")
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
            log_agent_action(self.name, "命中页面结构分析缓存", normalized_url[:80])
            log_debug_metrics("llm_cache.page_analysis", self.cache.snapshot_stats())
            return cached
        if len(html) > 15000:
            html = html[:15000] + "\n... (truncated)"

        response = self.llm.chat_with_system(
            system_prompt=PAGE_ANALYSIS_PROMPT.format(
                task_description=task_description,
                html_content=html,
                current_url=url_r.data or "",
            ),
            user_message="请分析页面结构并返回选择器配置",
            temperature=0.2, max_tokens=4096, json_mode=True,
        )
        try:
            config = self.llm.parse_json_response(response)
            if config.get("item_selector"):
                self.cache.set(
                    cache_key,
                    config,
                    settings.PAGE_ANALYSIS_CACHE_TTL_SECONDS,
                )
                log_debug_metrics("llm_cache.page_analysis", self.cache.snapshot_stats())
            log_agent_action(self.name, "页面分析完成", f"item_selector: {config.get('item_selector', 'N/A')}")
            return config
        except Exception as e:
            log_error(f"页面分析失败: {e}")
            return {"success": False, "error": str(e)}

    async def extract_data_with_selectors(self, tk: BrowserToolkit, config: Dict[str, Any], limit: int = 10) -> List[Dict[str, Any]]:
        results = []
        item_selector = config.get("item_selector", "")
        fields = config.get("fields", {})
        if not item_selector:
            log_warning("未找到有效的项目选择器")
            return results

        log_agent_action(self.name, "提取数据", f"选择器: {item_selector}")

        if config.get("need_click_first") and config.get("click_selector"):
            r = await tk.click(config["click_selector"])
            if r.success:
                await tk.human_delay(1000, 2000)

        try:
            items_r = await tk.query_all(item_selector)
            items = items_r.data if items_r.success else []
            log_agent_action(self.name, f"找到 {len(items)} 个元素")

            for i, item in enumerate(items[:limit]):
                data = {"index": i + 1}
                for field_name, selector in fields.items():
                    if not selector:
                        continue
                    try:
                        elem = await item.query_selector(selector)
                        if elem:
                            tag = await elem.evaluate("el => el.tagName.toLowerCase()")
                            text = (await elem.inner_text()).strip()
                            if tag == "a":
                                data[field_name] = text
                                href = await elem.get_attribute("href")
                                if href:
                                    if field_name == "title":
                                        data["link"] = href
                                    else:
                                        data[f"{field_name}_link"] = href
                            else:
                                data[field_name] = text
                    except Exception as e:
                        logger.debug(f"提取字段 {field_name} 失败: {e}")

                if len([v for k, v in data.items() if k != "index" and v]) > 0:
                    results.append(data)
        except Exception as e:
            log_error(f"数据提取失败: {e}")
        return results

    # ── smart_scrape (main entry, uses toolkit) ────────────────

    async def smart_scrape(
        self,
        url: Optional[str],
        task_description: str,
        limit: int = 10,
        *,
        headless: Optional[bool] = None,
        query: str = "",
        shared_memory: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        log_agent_action(self.name, "开始智能爬取", task_description[:50])

        # 🔥 读取之前的失败历史，避免重复错误
        shared_memory = shared_memory or {}
        replan_history = shared_memory.get("_replan_history", [])
        tried_urls_set: Set[str] = set()

        if replan_history:
            log_agent_action(self.name, f"检测到 {len(replan_history)} 轮重规划历史", "")
            for h in replan_history:
                for tried_url in h.get("urls", []):
                    tried_urls_set.add(tried_url.strip())
            if tried_urls_set:
                log_warning(f"已尝试过的URL（将避免重复访问）: {', '.join(list(tried_urls_set)[:3])}")

        # Step 1: LLM 分析确定最佳目标 URL
        url_info = await self.determine_target_url(task_description)
        best_url = url_info.get("url", "")
        backup_urls = [
            str(item).strip()
            for item in (url_info.get("backup_urls") or [])
            if str(item).strip().startswith("http")
        ]
        search_base_query = str(query or url_info.get("search_query", "") or "").strip()
        domain_hints = self._extract_domain_hints(task_description)
        candidate_urls: List[str] = []
        seen_candidate_urls: Set[str] = set()

        def _append_candidate(candidate: str) -> None:
            value = str(candidate or "").strip()
            if not value or not value.startswith("http") or value in seen_candidate_urls:
                return
            # 🔥 跳过已经尝试过且失败的 URL
            if value in tried_urls_set:
                log_warning(f"跳过已尝试过的URL: {value}")
                return
            seen_candidate_urls.add(value)
            candidate_urls.append(value)
        if url:
            _append_candidate(url)
        if best_url:
            _append_candidate(best_url)
        for backup in backup_urls:
            _append_candidate(backup)

        should_search_first = bool(
            url_info.get("need_search")
            or not candidate_urls
            or any(not self._is_probably_detail_url(item) for item in candidate_urls[:1])
        )
        if should_search_first:
            search_bundle = await self.gather_search_candidates(
                task_description,
                base_query=search_base_query or task_description,
                domain_hints=domain_hints,
                max_results=max(3, min(limit, 6)),
                headless=headless if headless is not None else False,
            )
            for found_url in search_bundle.get("urls", []):
                _append_candidate(found_url)
            if search_bundle.get("serp_sufficient") and search_bundle.get("cards"):
                cards = search_bundle["cards"][:limit]
                return {
                    "success": True,
                    "data": cards,
                    "count": len(cards),
                    "source": "search_results",
                    "mode": "search_results",
                    "queries": search_bundle.get("queries", []),
                }
            search_urls = list(search_bundle.get("urls", []))
            if search_urls:
                url = str(search_urls[0]).strip()
            elif candidate_urls and (not url or should_search_first):
                url = candidate_urls[0]
        if not url and best_url:
            url = best_url
        elif not url and backup_urls:
            url = backup_urls[0]
        elif not url and url_info.get("need_search"):
            query = url_info.get("search_query", task_description)
            url = await self.search_for_url(query)
        if not url and backup_urls:
            url = backup_urls[0]
        if not url:
            return {"success": False, "error": "无法确定目标网站 URL", "data": []}

        # Step 1.5: Prefer static fetch for readable pages before browser extraction.
        if self._can_use_static_fetch(task_description, url):
            static_try_urls = [url] + [
                item
                for item in candidate_urls
                if item != url
            ]
            if not static_try_urls:
                static_try_urls = [url] + [item for item in backup_urls if item != url]
            static_try_urls = static_try_urls[:5]
            static_error = ""
            for static_url in static_try_urls:
                static_result = self._static_fetch(static_url, task_description, limit)
                if static_result.get("success"):
                    # 🔥 静态抓取也需要进行数据质量验证
                    static_data = static_result.get("data", [])
                    if static_data:
                        quality = self.validate_data_quality(static_data, task_description, limit)
                        if quality.get("valid"):
                            log_success(f"静态抓取成功，获取 {static_result.get('count', 0)} 条数据，质量验证通过")
                            return static_result
                        else:
                            log_warning(f"静态抓取数据质量不符合要求: {quality.get('reason', '')[:80]}")
                            log_warning(f"建议: {quality.get('suggestion', '')[:80]}")
                            # 继续尝试下一个 URL 或回退到浏览器模式
                    else:
                        log_warning("静态抓取返回空数据")
                static_error = str(static_result.get("error", "") or "")
                if static_error:
                    log_warning(f"静态抓取失败: {static_error[:80]}")
            # static fetch failed for all candidates; continue with browser mode

        effective_headless = headless if headless is not None else (False if should_search_first else True)
        tk = self._create_toolkit(headless=effective_headless)
        await tk.create_page()

        # 用于捕获 SPA 页面的 API 响应数据
        api_responses = []

        async def _capture_api_response(response):
            try:
                content_type = response.headers.get("content-type", "")
                if "json" in content_type and response.status == 200:
                    body = await response.json()
                    if isinstance(body, list) and len(body) > 0:
                        api_responses.append({"url": response.url, "data": body})
                    elif isinstance(body, dict):
                        for key, val in body.items():
                            if isinstance(val, list) and len(val) >= 3 and isinstance(val[0], dict):
                                api_responses.append({"url": response.url, "data": val, "key": key})
            except Exception:
                pass

        if tk.page:
            tk.page.on("response", _capture_api_response)

        config = {}
        try:
            # Step 2: 访问页面（带重试 + 反爬自适应）
            log_agent_action(self.name, "访问页面", url)
            _goto_attempt = 0

            async def _rebuild_page_and_goto():
                nonlocal _goto_attempt
                _goto_attempt += 1
                if tk.page and tk.page.is_closed():
                    await tk.create_page()
                    if tk.page:
                        tk.page.on("response", _capture_api_response)
                wait_strategy = "domcontentloaded" if _goto_attempt <= 2 else "commit"
                return await tk.goto(url, wait_until=wait_strategy, timeout=45000)

            try:
                await async_retry(
                    _rebuild_page_and_goto, max_attempts=4,
                    base_delay=2.0, max_delay=15.0, caller_name=self.name,
                )
            except Exception as goto_err:
                return {"success": False, "error": f"页面加载失败: {str(goto_err)[:200]}", "data": [], "url": url}

            await tk.human_delay(180, 3000)

            # 等待页面稳定
            if self.fast_mode:
                await tk.wait_for_load("domcontentloaded", timeout=3000)
            else:
                await tk.wait_for_load("networkidle", timeout=15000)

            # 等待动态内容渲染
            await tk.wait_for_selector(
                "table, .list, ul li, [class*='list'], [class*='item'], .el-table",
                timeout=4000 if self.fast_mode else 10000,
            )

            # Step 2.5: 检测并处理验证码
            captcha_r = await tk.detect_captcha()
            if captcha_r.success and captcha_r.data and captcha_r.data.get("has_captcha"):
                log_agent_action(self.name, "检测到验证码，尝试自动处理")
                solve_r = await tk.solve_captcha(max_retries=5)
                if solve_r.success:
                    await tk.human_delay(250, 3000)
                    await tk.wait_for_load("domcontentloaded", timeout=10000)
                    await tk.wait_for_load("networkidle", timeout=10000)
                else:
                    return {"success": False, "error": "验证码处理失败", "data": [], "url": url}

            # 模拟人类滚动
            for _ in range(random.randint(1, 2) if self.fast_mode else random.randint(2, 4)):
                await tk.scroll_down(random.randint(200, 500))
                await tk.human_delay(120, 800)

            # Step 3-5: 提取数据（带自主探索重试）
            max_attempts = 3
            data = []

            for attempt in range(max_attempts):
                if attempt > 0:
                    url_r = await tk.get_current_url()
                    log_agent_action(self.name, f"第 {attempt + 1} 次尝试提取数据", (url_r.data or "")[:60])

                # Step 3: 分析页面结构
                config = await self.analyze_page_structure(tk, task_description)

                if config.get("item_selector"):
                    # Step 4: 用选择器提取数据
                    data = await self.extract_data_with_selectors(tk, config, limit)

                    # Step 4.5: 数据不够时，滚动加载更多
                    if data and len(data) < limit:
                        log_agent_action(self.name, f"数据不足 ({len(data)}/{limit})，尝试滚动加载更多")
                        last_count = len(data)
                        no_change = 0
                        for _ in range(8):
                            await tk.scroll_down(random.randint(600, 1000))
                            await tk.human_delay(300, 2000)
                            data = await self.extract_data_with_selectors(tk, config, limit)
                            if len(data) >= limit:
                                break
                            if len(data) <= last_count:
                                no_change += 1
                                if no_change >= 2:
                                    break
                            else:
                                no_change = 0
                                last_count = len(data)

                    # Step 4.6: 滚动后仍不够，尝试翻页
                    if data and len(data) < limit:
                        for _page_num in range(3):
                            if len(data) >= limit:
                                break
                            clicked = await self._try_next_page_via_toolkit(tk)
                            if not clicked:
                                break
                            for _ in range(random.randint(1, 2)):
                                await tk.scroll_down(random.randint(200, 500))
                                await tk.human_delay(120, 800)
                            page_data = await self.extract_data_with_selectors(tk, config, limit - len(data))
                            if not page_data:
                                break
                            data.extend(page_data)
                            log_agent_action(self.name, f"翻页后累计 {len(data)}/{limit} 条数据")

                if not data:
                    # 尝试通用选择器
                    common_selectors = [
                        "table tbody tr", "table tr:not(:first-child)",
                        ".list-item", ".item", "ul.list li",
                        ".el-table__row", "[class*='vuln'] tr", "[class*='list'] li",
                    ]
                    for sel in common_selectors:
                        items_r = await tk.query_all(sel)
                        if items_r.success and len(items_r.data or []) >= 3:
                            config["item_selector"] = sel
                            config["fields"] = {"title": "td:nth-child(2)", "link": "a"}
                            data = await self.extract_data_with_selectors(tk, config, limit)
                            if data:
                                break

                if not data and api_responses:
                    best_api = max(api_responses, key=lambda x: len(x["data"]))
                    data = best_api["data"][:limit]
                    log_success(f"从 API 响应中提取到 {len(data)} 条数据")

                if not data:
                    fallback_links = await self.extract_news_links_fallback(tk, task_description, limit=limit)
                    if fallback_links:
                        data = fallback_links
                        log_success(f"通用新闻链接兜底提取成功，获取 {len(data)} 条数据")

                if data:
                    quality = self.validate_data_quality(data, task_description, limit)
                    if quality.get("valid"):
                        log_success(f"数据质量验证通过: {quality.get('reason', '')[:50]}")
                        break
                    else:
                        log_warning(f"数据不符合要求: {quality.get('reason', '')[:80]}")
                        data = []

                # 探索页面导航找到正确的数据页
                if attempt < max_attempts - 1:
                    log_agent_action(self.name, "当前页面无数据，尝试探索导航链接")
                    next_url = await self.explore_for_data_page(tk, task_description)
                    url_r = await tk.get_current_url()
                    if next_url and next_url != (url_r.data or ""):
                        api_responses.clear()
                        await tk.goto(next_url, timeout=30000)
                        await tk.human_delay(200, 4000)
                        await tk.wait_for_load("networkidle", timeout=15000)
                        await tk.wait_for_selector(
                            "table, .list, ul li, [class*='list'], [class*='item'], .el-table",
                            timeout=10000,
                        )
                        for _ in range(random.randint(1, 2)):
                            await tk.scroll_down(random.randint(200, 500))
                            await tk.human_delay(120, 800)
                    else:
                        break

            # 处理相对链接
            base_url = "/".join(url.split("/")[:3])
            for item in data:
                for key in ["link", "title_link", "id_link"]:
                    if item.get(key) and not item[key].startswith("http"):
                        if item[key].startswith("/"):
                            item[key] = base_url + item[key]
                        else:
                            item[key] = url.rsplit("/", 1)[0] + "/" + item[key]

            if data:
                log_success(f"最终成功提取 {len(data)} 条数据")
            else:
                # 换源搜索（最多尝试 2 个替代来源，防止无限循环）
                log_agent_action(self.name, "当前来源失败，尝试通过搜索引擎寻找替代来源")
                alt_bundle = await self.gather_search_candidates(
                    task_description,
                    base_query=search_base_query or task_description,
                    domain_hints=domain_hints,
                    max_results=3,
                    tk=tk,
                )
                alt_urls = list(alt_bundle.get("urls", []))
                original_domain = "/".join(url.split("/")[:3]) if url else ""
                alt_urls = [u for u in alt_urls if original_domain not in u]

                for idx, alt_url in enumerate(alt_urls[:2], 1):
                    log_agent_action(self.name, f"尝试替代来源 ({idx}/2)", alt_url[:80])
                    try:
                        await tk.goto(alt_url, timeout=30000)
                        await tk.human_delay(500, 1500)
                        for _ in range(random.randint(1, 2)):
                            await tk.scroll_down(random.randint(200, 500))
                            await tk.human_delay(120, 800)
                        alt_config = await self.analyze_page_structure(tk, task_description)
                        if alt_config.get("item_selector"):
                            data = await self.extract_data_with_selectors(tk, alt_config, limit)
                        if data:
                            quality = self.validate_data_quality(data, task_description, limit)
                            if quality.get("valid"):
                                url = alt_url
                                config = alt_config
                                log_success(f"替代来源成功，从 {alt_url[:60]} 提取到 {len(data)} 条数据")
                                break
                            else:
                                log_warning(f"替代来源 {idx} 数据质量不符合要求")
                                data = []
                        else:
                            log_warning(f"替代来源 {idx} 未能提取到数据")
                    except Exception as alt_err:
                        log_warning(f"替代来源 {idx} 访问失败: {str(alt_err)[:80]}")
                        continue
                if not data:
                    log_warning("所有来源（包括 2 个替代来源）均未能提取到数据")

            return {
                "success": len(data) > 0, "data": data, "count": len(data),
                "source": url, "selectors_used": config,
            }
        except Exception as e:
            log_error(f"爬取失败: {e}")
            return {"success": False, "error": str(e), "data": [], "url": url}
        finally:
            await tk.close()

    async def scrape_hackernews(self, limit: int = 5) -> Dict[str, Any]:
        """兼容旧测试/旧调用方的 Hacker News 抓取入口。"""
        result = await self.smart_scrape(
            url="https://news.ycombinator.com",
            task_description=f"抓取 Hacker News 首页前 {limit} 条新闻的标题和链接",
            limit=limit,
        )
        if result.get("success"):
            for idx, item in enumerate(result.get("data", []), 1):
                if isinstance(item, dict):
                    item.setdefault("rank", idx)
        return result

    async def _try_next_page_via_toolkit(self, tk: BrowserToolkit) -> bool:
        """尝试点击分页控件翻到下一页"""
        next_page_selectors = [
            "a:has-text('下一页')", "button:has-text('下一页')",
            "a:has-text('Next')", "button:has-text('Next')",
            "a:has-text('下页')", "a:has-text('>')",
            "[class*='next']", "[class*='pager-next']",
            "a[aria-label='Next']", "button[aria-label='Next']",
            "a[aria-label='下一页']", "button[aria-label='下一页']",
            ".pagination .next", ".pager .next",
            "li.next > a", "li.next > button",
            ".ant-pagination-next:not(.ant-pagination-disabled) a",
            ".el-pagination .btn-next:not(:disabled)",
        ]
        for sel in next_page_selectors:
            vis_r = await tk.is_visible(sel)
            if not (vis_r.success and vis_r.data):
                continue
            # 检查是否禁用
            disabled_r = await tk.evaluate_js(
                "(sel) => { const el = document.querySelector(sel); return el && (el.disabled || el.classList.contains('disabled') || el.getAttribute('aria-disabled') === 'true'); }",
                sel,
            )
            if disabled_r.success and disabled_r.data:
                continue
            r = await tk.click(sel)
            if r.success:
                log_agent_action(self.name, "翻到下一页", sel)
                await tk.human_delay(500, 3000)
                await tk.wait_for_load("domcontentloaded", timeout=8000)
                return True
        return False

    # ── execute / process (LangGraph integration) ────────────

    async def execute_async(self, task: TaskItem, shared_memory: Dict[str, Any]) -> Dict[str, Any]:
        classify_failure, make_trace_step, evaluate_success_criteria, execute_fallback, MAX_FALLBACK_ATTEMPTS = _import_paod()

        params = task["params"]
        url = params.get("url", "")
        query = params.get("query", "")
        limit = params.get("limit", 10)
        headless = params.get("headless")
        task_description = task["description"]

        # 🔥 修复：如果 description 是 task_id 或太短，尝试从 params 中获取实际的 task 描述
        if task_description and (task_description.startswith("task_") or len(task_description) < 10):
            # description 可能被错误地设置为 task_id，尝试从 params["task"] 获取
            actual_task = params.get("task", "")
            if actual_task and len(actual_task) > len(task_description):
                log_warning(f"检测到 description 可能是 task_id ('{task_description}')，使用 params['task'] 代替: {actual_task[:80]}")
                task_description = actual_task
            else:
                # 如果 params["task"] 也没有，记录警告但继续使用原 description
                log_warning(f"检测到可疑的 task description: '{task_description}'，但 params 中没有更好的替代")

        # 额外的调试日志
        log_agent_action(self.name, "开始智能爬取", task_description[:50])

        trace: List[Dict[str, Any]] = task.get("execution_trace", [])
        step_no = len(trace) + 1
        resolved_model = params.get("_resolved_model", "")

        trace.append(make_trace_step(step_no, "执行 smart_scrape", f"url={url}, limit={limit}", "", ""))
        runner = self
        if resolved_model:
            try:
                runner = WebWorker(llm_client=LLMClient(model=resolved_model))
                runner.fast_mode = self.fast_mode
                runner.block_heavy_resources = self.block_heavy_resources
                runner.static_fetch_enabled = self.static_fetch_enabled
            except Exception as e:
                log_warning(f"初始化任务专用模型失败: {e}，回退默认模型")
                runner = self

        result = await runner.smart_scrape(
            url,
            task_description,
            limit,
            headless=headless,
            query=query,
            shared_memory=shared_memory,
        )
        trace[-1]["observation"] = f"success={result.get('success')}, count={result.get('count', 0)}"

        criteria = task.get("success_criteria", [])
        if result.get("success") and evaluate_success_criteria(criteria, result):
            trace[-1]["decision"] = "criteria_met → done"
            task["execution_trace"] = trace
            return result

        trace[-1]["decision"] = "criteria_not_met → try fallback"
        fb_index = 0
        while fb_index < MAX_FALLBACK_ATTEMPTS:
            fb = execute_fallback(task, fb_index, shared_memory)
            if fb is None:
                break
            fb_index += 1
            step_no += 1

            if fb["action"] == "switch_worker":
                trace.append(make_trace_step(step_no, f"switch_worker → {fb['target']}", "signal", "", "escalate"))
                task["execution_trace"] = trace
                result["_switch_worker"] = fb["target"]
                result["_switch_params"] = fb.get("param_patch", {})
                return result

            patch = fb.get("param_patch", {})
            patched_params = {**params, **patch}
            retry_url = patched_params.get("url", url)
            retry_query = patched_params.get("query", query)
            retry_limit = patched_params.get("limit", limit)
            retry_headless = patched_params.get("headless", headless)
            trace.append(make_trace_step(step_no, f"retry #{fb_index}", f"url={retry_url}, query={retry_query}, patch={patch}", "", ""))

            result = await runner.smart_scrape(
                retry_url,
                task_description,
                retry_limit,
                headless=retry_headless,
                query=retry_query,
            )
            trace[-1]["observation"] = f"success={result.get('success')}, count={result.get('count', 0)}"

            if result.get("success") and evaluate_success_criteria(criteria, result):
                trace[-1]["decision"] = "criteria_met → done"
                task["execution_trace"] = trace
                return result
            trace[-1]["decision"] = "still_failing → next fallback"

        if not result.get("success"):
            task["failure_type"] = classify_failure(result.get("error", ""))
        task["execution_trace"] = trace
        return result

    def execute(self, task: TaskItem, shared_memory: Dict[str, Any]) -> Dict[str, Any]:
        return asyncio.run(self.execute_async(task, shared_memory))

    def process(self, state: OmniCoreState) -> OmniCoreState:
        """LangGraph 节点函数（PAOD 增强）"""
        classify_failure = _import_paod()[0]

        async def _process_all():
            for idx, task in enumerate(state["task_queue"]):
                if task["task_type"] == "web_worker" and task["status"] == "pending":
                    state["task_queue"][idx]["status"] = "running"

                    result = await self.execute_async(task, state["shared_memory"])

                    # 检测 switch_worker 信号
                    if isinstance(result, dict) and result.get("_switch_worker"):
                        target = result.pop("_switch_worker")
                        patch = result.pop("_switch_params", {})
                        log_warning(f"WebWorker 触发 switch_worker → {target}")
                        state["task_queue"][idx]["task_type"] = target
                        state["task_queue"][idx]["params"].update(patch)
                        state["task_queue"][idx]["status"] = "pending"
                        continue

                    state["task_queue"][idx]["status"] = (
                        "completed" if result.get("success") else "failed"
                    )
                    state["task_queue"][idx]["result"] = result

                    if result.get("success") and result.get("data"):
                        state["shared_memory"][task["task_id"]] = result["data"]

                    if not result.get("success"):
                        state["task_queue"][idx]["failure_type"] = classify_failure(
                            result.get("error", "")
                        )
                        state["error_trace"] = result.get("error", "未知错误")

        asyncio.run(_process_all())
        return state


from agents.web_worker_singleflight import (
    analyze_page_structure_with_singleflight,
    determine_target_url_with_singleflight,
)


WebWorker.determine_target_url = determine_target_url_with_singleflight
WebWorker.analyze_page_structure = analyze_page_structure_with_singleflight
