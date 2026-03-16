"""
OmniCore 智能 Web Worker Agent
自适应网页爬取：自主搜索、页面理解、反爬应对、自动导航、验证码处理
所有浏览器操作通过 BrowserToolkit 完成。
"""
import asyncio
import json
import random
import re
from typing import Dict, Any, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse
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
from utils.page_perceiver import PagePerceiver
from utils.search_engine_profiles import (
    decode_search_redirect_url,
    get_search_input_selectors,
    get_search_result_selectors,
    is_search_engine_domain,
    iter_search_engine_profiles,
    looks_like_search_results_url,
)
from utils.search_engine import SearchEngineManager, SearchStrategy
from utils.url_utils import extract_all_urls
from utils.web_result_normalizer import (
    looks_like_detail_list_item,
    normalize_search_cards,
    normalize_web_results,
    score_detail_like_url,
)
import utils.web_debug_recorder as web_debug_recorder
from config.settings import settings
from config.domain_keywords import SEARCH_STOPWORDS_SET

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
RE_ANCHOR_WITH_ATTRS = re.compile(
    r"<a([^>]*)href=[\"']([^\"']+)[\"']([^>]*)>(.*?)</a>",
    re.DOTALL | re.IGNORECASE,
)
RE_NEXT_PAGE_HINT = re.compile(r"(next|more|older|下一页|更多|后页)", re.IGNORECASE)

# 文本块提取相关
RE_PARAGRAPH_BLOCK = re.compile(r"<(p|li)[^>]*>(.*?)</\1>", re.DOTALL | re.IGNORECASE)
RE_CONTENT_BLOCK = re.compile(
    r"<(main|article|section|div)[^>]*>(.*?)</\1>", re.DOTALL | re.IGNORECASE
)

# 分词相关
RE_TOKEN_SPLIT = re.compile(r"[\s,.;:|/\\]+")

# ==================== HTML 处理常量 ====================
# HTML 截断长度配置
HTML_MAX_LENGTH_WITH_STRUCTURE = 5000  # 有页面结构时，HTML 最大长度
HTML_MAX_LENGTH_WITHOUT_STRUCTURE = 100000  # 无页面结构时，HTML 最大长度
HTML_PRE_CLEAN_LENGTH = 20000  # 清洗前预截断长度

# 页面结构提取失败的标记
PAGE_STRUCTURE_FAILED_MARKER = "(页面结构提取失败，仅使用HTML分析)"
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
- Keep source/domain hints available for ranking, but do not add site:domain filters unless the user explicitly requested a domain-constrained search.
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

GENERIC_SEARCH_STOPWORDS = SEARCH_STOPWORDS_SET

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

## 页面语义快照（优先参考）
{semantic_snapshot}

## 页面结构概览（由页面感知器提取）
{page_structure}

## 候选区域（按任务相关性排序）
{candidate_regions}

## 页面 HTML 证据片段（已清洗，按任务相关性抽样，不是整页）
```html
{html_content}
```

## 页面当前 URL
{current_url}

## 你的工作方式
1. **先看页面结构概览**：理解页面的整体布局和内容组织方式（标题、列表、表格、段落等）
2. **再看候选区域**：优先判断哪些区域最像目标数据区、详情区、表格区、列表区
3. **最后看 HTML 证据片段**：只把它当作局部证据，不要假设这就是整页全部内容
4. **为每个需要提取的字段确定精确的 CSS 选择器**
5. **优先使用语义快照、候选区域和页面结构概览中提到的选择器/区域**，这些区域经过可见性验证更可靠
6. 如果页面是搜索结果页、跳转页、聚合页，而不是最终数据页，要明确说明当前页不适合直接抽取
7. 如果页面结构不明确，给出你最有把握的选择器

返回 JSON 格式：
```json
{{
    "success": true,
    "page_type": "list/detail/serp/form/unknown",
    "requires_navigation": false,
    "navigation_reason": "",
    "item_selector": "每一条数据项的选择器（如 tr, li, div.item 等）",
    "fields": {{
        "title": "标题文本的选择器（相对于 item）",
        "link": "链接的选择器（相对于 item，a 标签会自动提取 href）",
        "date": "日期的选择器（可选）",
        "severity": "严重程度/等级的选择器（可选）",
        "id": "编号/ID 的选择器（可选，若字段就在当前 item 属性上可用 @id 这种属性写法）"
    }},
    "need_click_first": false,
    "click_selector": "如果需要先点击某元素才能看到数据，填写选择器",
    "notes": "其他注意事项"
}}
```

注意：
- item_selector 应该能选中多个重复的数据项
- fields 中的选择器是相对于每个 item 的
- 如果字段直接来自当前 item 的属性，可用 `@attr`；如果来自子元素属性，可用 `selector/@attr`
- 如果字段就是当前 item 或子元素的文本，可用 `text()` 或 `selector/text()`
- 只填你在 HTML 或页面结构中确实看到的选择器，看不到的字段留空字符串
- 上下文可能是按预算裁剪过的局部证据；不能因为某字段没展示出来就假设页面里绝对没有
- 优先使用语义快照、候选区域和页面结构概览中提到的选择器，它们经过可见性验证
- 如果当前页明显是搜索结果页且不应直接抽取，把 requires_navigation 设为 true
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
        self.page_perceiver = PagePerceiver()  # 页面感知器实例
        self.search_engine_manager = SearchEngineManager()  # 搜索引擎管理器
        self.static_fetch_enabled = settings.STATIC_FETCH_ENABLED

    def _create_toolkit(self, headless: bool = True) -> BrowserToolkit:
        return BrowserToolkit(
            headless=headless,
            fast_mode=self.fast_mode,
            block_heavy_resources=self.block_heavy_resources,
        )

    def _clean_html_for_llm(self, html: str) -> str:
        """
        清洗HTML，移除噪音，只保留有效信息

        目标：
        - 移除 <script>, <style> 标签
        - 移除冗余属性（class, style, data-*）
        - 保留关键属性（id, name, href, type, role, placeholder, value, aria-label）
        - 压缩空白字符

        预期效果：降低 90% 的 Token 噪音
        """
        if not html:
            return ""

        # 1. 移除 script 和 style 标签
        html = RE_SCRIPT_TAG.sub('', html)
        html = RE_STYLE_TAG.sub('', html)
        html = RE_HTML_COMMENT.sub('', html)

        # 2. 移除冗余属性（保留关键属性）
        # 匹配模式：属性名="属性值"
        # 保留：id, name, href, type, role, placeholder, value, aria-label, aria-*, for, action, method
        # 移除：class, style, data-*, onclick, onload, 等
        def clean_attributes(match):
            tag = match.group(0)
            # 保留的属性列表
            keep_attrs = ['id', 'name', 'href', 'type', 'role', 'placeholder', 'value',
                         'aria-label', 'aria-describedby', 'for', 'action', 'method', 'src', 'alt']

            # 移除 class 属性
            tag = re.sub(r'\sclass="[^"]*"', '', tag)
            tag = re.sub(r"\sclass='[^']*'", '', tag)

            # 移除 style 属性
            tag = re.sub(r'\sstyle="[^"]*"', '', tag)
            tag = re.sub(r"\sstyle='[^']*'", '', tag)

            # 移除 data-* 属性
            tag = re.sub(r'\sdata-[a-z0-9-]+="[^"]*"', '', tag, flags=re.IGNORECASE)
            tag = re.sub(r"\sdata-[a-z0-9-]+='[^']*'", '', tag, flags=re.IGNORECASE)

            # 移除事件处理器（on*）
            tag = re.sub(r'\son[a-z]+="[^"]*"', '', tag, flags=re.IGNORECASE)
            tag = re.sub(r"\son[a-z]+='[^']*'", '', tag, flags=re.IGNORECASE)

            return tag

        html = re.sub(r'<[^>]+>', clean_attributes, html)

        # 3. 压缩连续空白字符
        html = RE_WHITESPACE.sub(' ', html)

        # 4. 移除标签之间的多余空白
        html = re.sub(r'>\s+<', '><', html)

        return html.strip()

    def _extract_direct_urls(self, text: str) -> List[str]:
        return extract_all_urls(text)

    def _page_structure_to_debug_payload(self, page_structure: Any) -> Dict[str, Any]:
        if not page_structure:
            return {}
        return {
            "url": str(getattr(page_structure, "url", "") or ""),
            "title": str(getattr(page_structure, "title", "") or ""),
            "main_content_blocks": [
                {
                    "block_type": str(getattr(block, "block_type", "") or ""),
                    "content": str(getattr(block, "content", "") or ""),
                    "selector": str(getattr(block, "selector", "") or ""),
                    "depth": int(getattr(block, "depth", 0) or 0),
                    "attributes": dict(getattr(block, "attributes", {}) or {}),
                }
                for block in (getattr(page_structure, "main_content_blocks", []) or [])
            ],
            "navigation_blocks": [
                {
                    "block_type": str(getattr(block, "block_type", "") or ""),
                    "content": str(getattr(block, "content", "") or ""),
                    "selector": str(getattr(block, "selector", "") or ""),
                    "depth": int(getattr(block, "depth", 0) or 0),
                    "attributes": dict(getattr(block, "attributes", {}) or {}),
                }
                for block in (getattr(page_structure, "navigation_blocks", []) or [])
            ],
            "interactive_elements": list(getattr(page_structure, "interactive_elements", []) or []),
            "metadata": dict(getattr(page_structure, "metadata", {}) or {}),
        }

    @staticmethod
    def _snapshot_cards_to_search_cards(snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
        cards: List[Dict[str, Any]] = []
        for item in (snapshot.get("cards", []) or []):
            if not isinstance(item, dict):
                continue
            raw_link = str(item.get("raw_link", "") or item.get("link", "") or "").strip()
            target_url = str(item.get("target_url", "") or "").strip()
            link = target_url or raw_link
            title = str(item.get("title", "") or "").strip()
            if not link or not title:
                continue
            payload = {
                "title": title,
                "link": link,
                "source": str(item.get("source", "") or "").strip(),
                "date": str(item.get("date", "") or "").strip(),
                "snippet": str(item.get("snippet", "") or "").strip(),
                "target_ref": str(item.get("target_ref", "") or "").strip(),
                "target_selector": str(item.get("target_selector", "") or "").strip(),
            }
            if raw_link and raw_link != link:
                payload["raw_link"] = raw_link
            if target_url:
                payload["target_url"] = target_url
            cards.append(payload)
        return cards

    def _rank_snapshot_search_cards(
        self,
        snapshot: Dict[str, Any],
        task_description: str,
        query: str,
        *,
        max_results: int,
    ) -> Tuple[List[Dict[str, Any]], bool]:
        cards = self._snapshot_cards_to_search_cards(snapshot)
        if not cards:
            return [], False

        normalized_cards = normalize_search_cards(
            cards,
            task_description,
            limit=max_results,
            understanding={"page_type": str(snapshot.get("page_type", "") or "serp")},
        )
        candidate_cards = normalized_cards or cards
        ranked_cards, serp_sufficient = self._rerank_search_results(
            task_description,
            query,
            candidate_cards,
            max_results=max_results,
        )
        return ranked_cards or candidate_cards[:max_results], serp_sufficient

    async def _extract_search_cards_from_semantic_snapshot(
        self,
        tk: BrowserToolkit,
        task_description: str,
        query: str,
        *,
        max_results: int,
    ) -> List[Dict[str, Any]]:
        if not hasattr(tk, "semantic_snapshot"):
            return []

        try:
            snapshot_r = await tk.semantic_snapshot(max_elements=80, include_cards=True)
        except Exception as exc:
            log_warning(f"语义快照搜索结果提取失败: {exc}")
            return []

        snapshot = snapshot_r.data if snapshot_r.success and isinstance(snapshot_r.data, dict) else {}
        if not snapshot:
            return []

        web_debug_recorder.write_json(
            "search_results_semantic_snapshot",
            {
                "query": query,
                "task_description": task_description,
                "snapshot": snapshot,
            },
        )
        cards, _ = self._rank_snapshot_search_cards(
            snapshot,
            task_description,
            query,
            max_results=max_results,
        )
        return cards

    def _format_semantic_snapshot_for_llm(self, snapshot: Dict[str, Any]) -> str:
        if not isinstance(snapshot, dict) or not snapshot:
            return "(无语义快照)"

        lines = [
            f"页面类型: {str(snapshot.get('page_type', 'unknown') or 'unknown')}",
            f"页面阶段: {str(snapshot.get('page_stage', 'unknown') or 'unknown')}",
            f"URL: {str(snapshot.get('url', '') or '')}",
            f"标题: {str(snapshot.get('title', '') or '')}",
        ]
        blocked_signals = [
            str(item or "").strip()
            for item in (snapshot.get("blocked_signals", []) or [])[:6]
            if str(item or "").strip()
        ]
        if blocked_signals:
            lines.append("阻塞信号: " + " | ".join(blocked_signals))
        main_text = str(snapshot.get("main_text", "") or "").strip()
        if main_text:
            lines.append(f"主体文本: {main_text[:500]}")
        visible_text_blocks = snapshot.get("visible_text_blocks", []) or []
        if visible_text_blocks:
            lines.append("可见文本块:")
            for idx, block in enumerate(visible_text_blocks[:8], 1):
                if not isinstance(block, dict):
                    continue
                lines.append(
                    f"{idx}. kind={str(block.get('kind', '') or '')} "
                    f"text={str(block.get('text', '') or '')[:140]} "
                    f"selector={str(block.get('selector', '') or '')[:80]}"
                )
        affordances = snapshot.get("affordances", {}) or {}
        if affordances:
            affordance_text = ", ".join(
                f"{key}={value}"
                for key, value in affordances.items()
            )
            lines.append(f"页面特征: {affordance_text}")

        regions = snapshot.get("regions", []) or []
        if regions:
            lines.append("页面区域:")
            for idx, region in enumerate(regions[:6], 1):
                if not isinstance(region, dict):
                    continue
                sample_items = " | ".join(
                    str(item or "")[:80]
                    for item in (region.get("sample_items", []) or [])[:2]
                    if str(item or "").strip()
                )
                lines.append(
                    f"{idx}. kind={str(region.get('kind', '') or 'unknown')} "
                    f"ref={str(region.get('ref', '') or '')} "
                    f"region={str(region.get('region', '') or '')} "
                    f"items={int(region.get('item_count', 0) or 0)} "
                    f"links={int(region.get('link_count', 0) or 0)} "
                    f"controls={int(region.get('control_count', 0) or 0)} "
                    f"selector={str(region.get('selector', '') or '')[:80]} "
                    f"heading={str(region.get('heading', '') or '')[:80]} "
                    f"sample={str(region.get('text_sample', '') or '')[:120]} "
                    f"items_sample={sample_items or '(none)'}"
                )
            if len(regions) > 6:
                lines.append(f"... 还有 {len(regions) - 6} 个页面区域未展示")

        collections = snapshot.get("collections", []) or []
        if collections:
            lines.append("页面集合结构:")
            for idx, collection in enumerate(collections[:4], 1):
                if not isinstance(collection, dict):
                    continue
                sample_items = " | ".join(
                    str(item or "")[:100]
                    for item in (collection.get("sample_items", []) or [])[:3]
                    if str(item or "").strip()
                )
                lines.append(
                    f"{idx}. kind={str(collection.get('kind', '') or 'unknown')} "
                    f"count={int(collection.get('item_count', 0) or 0)} "
                    f"ref={str(collection.get('ref', '') or '')} "
                    f"samples={sample_items or '(none)'}"
                )
            if len(collections) > 4:
                lines.append(f"... 还有 {len(collections) - 4} 个页面集合未展示")

        controls = snapshot.get("controls", []) or []
        if controls:
            lines.append("关键控件:")
            for idx, control in enumerate(controls[:6], 1):
                if not isinstance(control, dict):
                    continue
                lines.append(
                    f"{idx}. kind={str(control.get('kind', '') or '')} "
                    f"ref={str(control.get('ref', '') or '')} "
                    f"text={str(control.get('text', '') or '')[:80]} "
                    f"selector={str(control.get('selector', '') or '')[:80]}"
                )
            if len(controls) > 6:
                lines.append(f"... 还有 {len(controls) - 6} 个控件未展示")

        cards = self._snapshot_cards_to_search_cards(snapshot)
        if cards:
            lines.append("搜索结果卡片:")
            for idx, card in enumerate(cards[:8], 1):
                payload = " | ".join(
                    part for part in [
                        card["title"][:100],
                        card.get("source", "")[:48],
                        card.get("date", "")[:40],
                        card.get("snippet", "")[:160],
                        card.get("link", "")[:140],
                        f"target_ref={card.get('target_ref', '')}" if card.get("target_ref") else "",
                    ] if part
                )
                lines.append(f"{idx}. {payload}")
            if len(cards) > 8:
                lines.append(f"... 还有 {len(cards) - 8} 个搜索结果卡未展示")

        elements = snapshot.get("elements", []) or []
        if elements:
            lines.append("可见关键元素:")
            for idx, element in enumerate(elements[:12], 1):
                if not isinstance(element, dict):
                    continue
                parts = [
                    str(element.get("type", element.get("role", "")) or "")[:24],
                    str(element.get("text", "") or "")[:80],
                    str(element.get("label", "") or "")[:60],
                    str(element.get("selector", "") or "")[:80],
                    f"ref={str(element.get('ref', '') or '')}" if element.get("ref") else "",
                ]
                lines.append(f"{idx}. " + " | ".join(part for part in parts if part))
            if len(elements) > 12:
                lines.append(f"... 还有 {len(elements) - 12} 个可见元素未展示")

        return "\n".join(lines)

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

    def _strip_search_query_noise(self, text: str) -> str:
        cleaned = str(text or "")
        cleaned = re.sub(r"https?://\S+", " ", cleaned)
        cleaned = re.sub(r"\bsite:\s*[^\s]+", " ", cleaned, flags=re.IGNORECASE)
        cleaned = RE_DOMAIN_HINT.sub(" ", cleaned)
        cleaned = re.sub(
            r"\b(?:from|use|using|via|prefer|preferred|primary|secondary)\b\s+[^\n,.;，。；]{0,120}\b(?:source|site|domain|url)\b",
            " ",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            r"(?:作为|用作)?(?:主要|首选|次要|备用)?(?:来源|站点|域名)[^\n,.;，。；]{0,80}",
            " ",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            r"\b(?:extract|retrieve|obtain|collect|scrape|read|get|fetch|open|visit|navigate|click|input|submit|report|show|display|return)\b",
            " ",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            r"\b(?:relevant|authoritative|official|best|appropriate)\b[^\n,.;，。；]{0,80}\b(?:page|site|source|website)\b",
            " ",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            r"\b(?:city|weather|forecast|detail)\b\s+\b(?:page|site|source)\b",
            " ",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            r"(?:抓取|提取|获取|读取|打开|访问|进入|点击|输入|填写|提交|展示|显示|返回|汇总|总结|报告|使用|通过)",
            " ",
            cleaned,
        )
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned.strip()

    def _compact_query_text(self, text: str, max_terms: int = 8) -> str:
        terms = self._tokenize_query_terms(self._strip_search_query_noise(text))
        if not terms:
            return str(self._strip_search_query_noise(text) or "").strip()[:80]
        return " ".join(terms[:max_terms]).strip()

    def _normalize_search_query_for_dedup(self, text: str) -> str:
        normalized = self._strip_search_query_noise(text).lower()
        replacements = {
            "weather forecast": "weather",
            "forecast": "weather",
            "headlines": "headline",
            "links": "link",
            "sources": "source",
            "results": "result",
            "天气预报": "天气",
            "气象预报": "天气",
            "新闻头条": "头条",
            "链接列表": "链接",
            "来源列表": "来源",
        }
        for source, target in replacements.items():
            normalized = normalized.replace(source, target)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return normalized

    def _query_signature(self, text: str) -> Tuple[str, ...]:
        normalized = self._normalize_search_query_for_dedup(text)
        if not normalized:
            return tuple()
        return tuple(sorted(self._tokenize_query_terms(normalized)))

    def _dedupe_search_queries(self, queries: List[str], *, max_queries: int) -> List[str]:
        deduped: List[str] = []
        seen_signatures: Set[Tuple[str, ...]] = set()
        seen_normalized: Set[str] = set()
        for query in queries:
            value = re.sub(r"\s+", " ", str(query or "").strip())
            if not value:
                continue
            normalized = self._normalize_search_query_for_dedup(value)
            signature = self._query_signature(value)
            if normalized in seen_normalized or (signature and signature in seen_signatures):
                continue
            seen_normalized.add(normalized)
            if signature:
                seen_signatures.add(signature)
            deduped.append(value)
            if len(deduped) >= max_queries:
                break
        return deduped[:max_queries]

    def _search_query_budget(self, task_description: str, max_queries: int) -> int:
        if max_queries <= 1:
            return 1
        if self._prefers_static_text(task_description):
            return 1
        if self._looks_like_list_page_task(task_description):
            return min(max_queries, 3)
        return min(max_queries, 2)

    def _build_natural_search_query(self, task_description: str, max_terms: int = 8) -> str:
        cleaned = self._strip_search_query_noise(task_description)
        if self._task_mentions_weather(task_description):
            weather_patterns = (
                r"([\u4e00-\u9fff]{2,12})的(今天|明天|后天|当前|本周末|本周)(?:（\d{4}-\d{2}-\d{2}）)?天气",
                r"(今天|明天|后天|当前|本周末|本周)(?:（\d{4}-\d{2}-\d{2}）)?的([\u4e00-\u9fff]{2,12})天气",
                r"(?:查询|查|搜索|搜|获取|看看)?\s*([\u4e00-\u9fff]{2,12}?)(?:(今天|明天|后天|当前|本周末|本周))?(?:的)?(?:天气|天气预报|气温|空气质量)",
            )
            for pattern in weather_patterns:
                weather_match = re.search(pattern, cleaned)
                if not weather_match:
                    continue
                group_1 = str(weather_match.group(1) or "").strip()
                group_2 = str(weather_match.group(2) or "").strip()
                if pattern.startswith(r"(今天"):
                    timeframe, location = group_1, group_2
                else:
                    location, timeframe = group_1, group_2
                query = " ".join(part for part in [location, timeframe, "天气"] if part).strip()
                if query:
                    return query
        compact = self._compact_query_text(cleaned, max_terms=max_terms)
        if compact:
            return compact
        return cleaned[:80].strip()

    def _task_explicitly_requests_domain_constraint(self, task_description: str) -> bool:
        text = str(task_description or "")
        normalized = text.lower()
        if "site:" in normalized:
            return True
        explicit_patterns = (
            r"(?:只看|限定|仅限|仅在|只搜索|只搜|仅搜索)\s*(?:这个|该)?(?:网站|站点|域名)?",
            r"(?:only|restrict(?:ed)?\s+to|limit(?:ed)?\s+to)\s+(?:this\s+)?(?:site|domain|website)",
        )
        return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in explicit_patterns)

    def _build_domain_constrained_query(self, task_description: str, domain: str) -> str:
        compact = self._build_natural_search_query(task_description, max_terms=6)
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
        candidates = []
        if str(base_query or "").strip():
            candidates.append(str(base_query or "").strip())
        else:
            candidates.append(self._compact_query_text(task_description, max_terms=8))

        if domain_hints and self._task_explicitly_requests_domain_constraint(task_description):
            if base_query:
                candidates.append(self._build_domain_constrained_query(base_query, domain_hints[0]))
            candidates.append(self._build_domain_constrained_query(task_description, domain_hints[0]))

        seen: Set[str] = set()
        for candidate in candidates:
            value = re.sub(r"\s+", " ", str(candidate or "").strip())
            if not value or value in seen:
                continue
            seen.add(value)
            queries.append(value)
            if len(queries) >= max_queries:
                break
        return self._dedupe_search_queries(queries, max_queries=max_queries)

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
        if self._task_mentions_weather(task_description):
            return {
                "url": "",
                "backup_urls": [],
                "need_search": True,
                "search_query": self._build_natural_search_query(task_description),
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
                    result["search_query"] = self._build_natural_search_query(task_description)
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
                "search_query": self._build_natural_search_query(task_description),
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

    def _score_static_link(self, text: str, href: str, task_description: str, base_url: str = "", attrs: str = "") -> int:
        score = 0
        t = text.lower()
        task = (task_description or "").lower()
        if len(text) >= 12:
            score += 2
        if len(text) >= 32:
            score += 2
        if href.startswith("http"):
            score += 1
        score += score_detail_like_url(href, reference_url=base_url, title=text)
        attr_text = str(attrs or "").lower()
        if any(token in attr_text for token in ("nav", "navbar", "menu", "footer", "breadcrumb")):
            score -= 3
        if any(token in attr_text for token in ("result", "item", "entry", "row", "card")):
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

    def _should_try_detail_text_fallback(self, task_description: str, page_type: str = "") -> bool:
        normalized_page_type = str(page_type or "").strip().lower()
        if self._looks_like_list_page_task(task_description):
            return False
        if self._prefers_static_text(task_description):
            return True
        return normalized_page_type in {"detail", "unknown", "modal"}

    def _looks_like_list_page_task(self, task_description: str) -> bool:
        desc = (task_description or "").lower()
        list_keywords = [
            "top", "title", "titles", "link", "links", "headline", "headlines", "list",
            "repository", "repositories", "repos", "stories", "news", "results",
            "前", "标题", "链接", "列表", "仓库", "新闻", "结果",
        ]
        if any(keyword in desc for keyword in list_keywords):
            return True
        return bool(re.search(r"\b\d+\s*(?:items?|results?|links?|headlines?|stories|repos)\b", desc))

    def _task_mentions_weather(self, task_description: str) -> bool:
        desc = (task_description or "").lower()
        weather_keywords = [
            "weather", "forecast", "temperature", "humidity", "air quality", "aqi", "wind",
            "天气", "预报", "气温", "湿度", "空气质量", "风力",
        ]
        return any(keyword in desc for keyword in weather_keywords)

    def _task_allows_serp_answer(self, task_description: str, limit: int = 0) -> bool:
        if self._prefers_static_text(task_description):
            return False

        desc = str(task_description or "").lower()
        if any(
            keyword in desc
            for keyword in [
                "detail", "details", "full text", "full article", "official page",
                "verify", "verified", "spec", "specs", "pricing", "parameter",
                "详情", "正文", "原文", "参数", "价格", "核实", "官网",
            ]
        ):
            return False

        if self._looks_like_list_page_task(task_description):
            return True

        if limit and limit > 5:
            return True

        return False

    def _looks_like_weather_text(self, text: str) -> bool:
        value = (text or "").lower()
        weather_signals = [
            "°c", "℃", "temperature", "humidity", "air quality", "aqi", "wind",
            "weather", "forecast", "today", "tomorrow",
            "气温", "湿度", "空气质量", "风力", "天气", "预报", "今天", "明天",
        ]
        return any(signal in value for signal in weather_signals) or bool(re.search(r"\b\d{1,2}\s*(?:°c|℃)\b", value))

    def _weather_signal_categories(self, data: List[Dict[str, Any]]) -> Set[str]:
        categories: Set[str] = set()
        for item in data[:8]:
            if not isinstance(item, dict):
                continue

            parts: List[str] = []
            for key, value in item.items():
                if key.startswith("_"):
                    continue
                key_text = str(key or "").strip().lower()
                if key_text in {"temperature", "temp", "humidity", "aqi", "air_quality", "wind", "weather"}:
                    mapped = {
                        "temperature": "temperature",
                        "temp": "temperature",
                        "humidity": "humidity",
                        "aqi": "aqi",
                        "air_quality": "aqi",
                        "wind": "wind",
                        "weather": "condition",
                    }
                    categories.add(mapped[key_text])
                if isinstance(value, (str, int, float)):
                    text = str(value).strip()
                    if text:
                        parts.append(text)

            haystack = " ".join(parts).lower()
            if not haystack:
                continue
            if (
                re.search(r"\d{1,2}\s*(?:°c|℃|度)\b", haystack)
                or any(token in haystack for token in ("temperature", "气温", "温度", "最高", "最低"))
            ):
                categories.add("temperature")
            if any(token in haystack for token in ("humidity", "湿度")):
                categories.add("humidity")
            if any(token in haystack for token in ("aqi", "air quality", "空气质量")):
                categories.add("aqi")
            if any(token in haystack for token in ("wind", "风力", "风向")) or re.search(r"\d+\s*级", haystack):
                categories.add("wind")
            if any(token in haystack for token in ("weather", "forecast", "天气", "晴", "阴", "多云", "雨", "雪", "雷")):
                categories.add("condition")
        return categories

    def _weather_data_has_required_signals(self, data: List[Dict[str, Any]]) -> bool:
        categories = self._weather_signal_categories(data)
        if len(categories) >= 3:
            return True
        if len(categories) >= 2 and len(data) >= 4:
            return True
        return False

    def _primary_task_url(self, task_description: str) -> str:
        direct_urls = self._extract_direct_urls(task_description)
        if direct_urls:
            return direct_urls[0]
        urls = extract_all_urls(task_description)
        if urls:
            return str(urls[0] or "").strip()
        return ""

    def _static_data_looks_useful(self, task_description: str, data: List[Dict[str, Any]], limit: int = 0) -> bool:
        if not data:
            return False
        if self._looks_like_list_page_task(task_description):
            reference_url = self._primary_task_url(task_description)
            required_count = max(1, min(limit or len(data), 3))
            usable_items = 0
            for item in data[:max(required_count, min(len(data), 8))]:
                if looks_like_detail_list_item(item, reference_url=reference_url):
                    usable_items += 1
            return usable_items >= required_count
        detail_task = self._prefers_static_text(task_description) or self._task_mentions_weather(task_description)
        if not detail_task:
            return True
        text_items = [
            str(item.get("text", "") or item.get("summary", "") or "").strip()
            for item in data
            if isinstance(item, dict) and str(item.get("text", "") or item.get("summary", "") or "").strip()
        ]
        if not text_items:
            return False
        if self._task_mentions_weather(task_description):
            return self._weather_data_has_required_signals(data) or any(
                self._looks_like_weather_text(item) for item in text_items[:8]
            )
        return True

    def _extract_static_links(self, html: str, base_url: str, task_description: str, limit: int) -> List[Dict[str, Any]]:
        cleaned = self._clean_html_text(html)
        candidates: List[Dict[str, Any]] = []
        seen = set()
        list_task = self._looks_like_list_page_task(task_description)
        scan_limit = max(limit * 12, 120) if list_task else max(limit * 4, 20)
        detail_like_hits = 0

        def _append(href: str, raw_text: str, attrs: str = ""):
            nonlocal detail_like_hits
            text = self._strip_tags(raw_text)
            full_href = urljoin(base_url, href.strip())
            if self._is_noise_link(text, full_href):
                return
            key = (text[:80], full_href)
            if key in seen:
                return
            seen.add(key)
            if score_detail_like_url(full_href, reference_url=base_url, title=text) >= 1:
                detail_like_hits += 1
            candidates.append({
                "title": text[:160], "link": full_href,
                "_score": self._score_static_link(text, full_href, task_description, base_url=base_url, attrs=attrs),
                "_order": len(candidates),
            })

        for match in RE_HEADING_WITH_LINK.finditer(cleaned):
            _append(match.group(2), match.group(3))
        if len(candidates) < limit:
            for match in RE_ANCHOR_WITH_ATTRS.finditer(cleaned):
                attrs = f"{match.group(1) or ''} {match.group(3) or ''}"
                _append(match.group(2), match.group(4), attrs=attrs)
                if len(candidates) >= scan_limit and detail_like_hits >= max(3, limit):
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

    def _backup_urls_for_explicit_url(self, url: str, task_description: str = "") -> List[str]:
        normalized = str(url or "").strip()
        if not normalized:
            return []
        if self._task_mentions_weather(task_description):
            match = re.search(r"https?://(?:www\.)?weather\.com\.cn/weather/(\d{9})\.shtml", normalized, re.IGNORECASE)
            if match:
                city_code = match.group(1)
                candidates = [
                    f"https://m.weather.com.cn/weather/{city_code}.shtml",
                    f"https://www.weather.com.cn/weather1d/{city_code}.shtml",
                    f"https://www.weather.com.cn/weather15d/{city_code}.shtml",
                ]
                return [item for item in candidates if item != normalized]
        return []

    def _extract_static_next_page_url(self, html: str, base_url: str) -> str:
        best_url = ""
        best_score = 0
        for match in RE_ANCHOR_WITH_ATTRS.finditer(str(html or "")):
            attrs = f"{match.group(1) or ''} {match.group(3) or ''}"
            href = str(match.group(2) or "").strip()
            label = self._strip_tags(match.group(4) or "")
            if not href:
                continue
            full_href = urljoin(base_url, href)
            if not full_href.startswith(("http://", "https://")):
                continue
            if full_href.rstrip("/") == str(base_url or "").rstrip("/"):
                continue

            attr_text = attrs.lower()
            label_text = label.lower()
            score = 0
            if "rel=\"next\"" in attr_text or "rel='next'" in attr_text or "rel=next" in attr_text:
                score += 5
            if any(token in attr_text for token in ("morelink", "pagination", "pager", "next", "older")):
                score += 4
            if RE_NEXT_PAGE_HINT.search(label_text):
                score += 3
            if any(token in href.lower() for token in ("page=", "p=", "start=", "offset=", "after=", "next=")):
                score += 1
            if score > best_score:
                best_score = score
                best_url = full_href
        return best_url if best_score >= 3 else ""

    @staticmethod
    def _merge_unique_items(existing: List[Dict[str, Any]], incoming: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        merged: List[Dict[str, Any]] = list(existing)
        seen = {
            (
                str(item.get("title", "") or "").strip()[:200],
                str(item.get("link", item.get("url", "")) or "").strip()[:300],
            )
            for item in merged
            if isinstance(item, dict)
        }
        for item in incoming or []:
            if not isinstance(item, dict):
                continue
            key = (
                str(item.get("title", "") or "").strip()[:200],
                str(item.get("link", item.get("url", "")) or "").strip()[:300],
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
        return merged

    def _static_fetch(self, url: str, task_description: str, limit: int) -> Dict[str, Any]:
        try:
            session = requests.Session()
            session.headers.update(
                {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                }
            )
            current_url = str(url or "").strip()
            visited_urls: Set[str] = set()
            pages_fetched = 0
            collected_data: List[Dict[str, Any]] = []
            mode = "static_fetch"
            allow_pagination = self._looks_like_list_page_task(task_description) and limit > 0
            max_pages = max(1, min(5, ((limit - 1) // 30) + 2)) if allow_pagination else 1

            while current_url and current_url not in visited_urls and pages_fetched < max_pages and len(collected_data) < limit:
                visited_urls.add(current_url)
                pages_fetched += 1

                resp = session.get(current_url, timeout=15)
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "").lower()
                if "html" not in content_type:
                    return {"success": False, "error": f"非HTML响应: {content_type}", "data": [], "url": current_url}
                current_encoding = str(getattr(resp, "encoding", "") or "").lower()
                if current_encoding in {"", "iso-8859-1", "latin-1", "ascii"}:
                    apparent_encoding = str(getattr(resp, "apparent_encoding", "") or "").strip()
                    if apparent_encoding:
                        try:
                            resp.encoding = apparent_encoding
                        except Exception:
                            pass

                html = str(getattr(resp, "text", "") or "")
                remaining = max(limit - len(collected_data), 0) or limit
                page_data: List[Dict[str, Any]] = []

                # 使用通用提取方法，不再针对特定网站硬编码
                text_data = self._extract_static_text_blocks(html, max(3, min(remaining, 6)), task_description)
                link_data = self._extract_static_links(html, current_url, task_description, remaining)
                if self._prefers_static_text(task_description):
                    page_data = text_data or link_data
                else:
                    page_data = link_data or text_data
                if page_data and "text" in page_data[0]:
                    mode = "static_fetch_text"

                collected_data = self._merge_unique_items(collected_data, page_data)[:limit]
                if len(collected_data) >= limit or not allow_pagination:
                    break

                next_page_url = self._extract_static_next_page_url(html, current_url)
                if not next_page_url:
                    break
                current_url = next_page_url

            if not collected_data:
                return {"success": False, "error": "静态抓取未提取到有效内容", "data": [], "url": url}
            if self._looks_like_list_page_task(task_description):
                collected_data = normalize_web_results(
                    collected_data,
                    task_description,
                    limit=limit,
                    understanding={"page_type": "list"},
                )
            if not self._static_data_looks_useful(task_description, collected_data, limit=limit):
                return {"success": False, "error": "static fetch returned navigation links instead of usable detail data", "data": [], "url": url}
            return {
                "success": True,
                "data": collected_data[:limit],
                "count": min(len(collected_data), limit),
                "source": url,
                "mode": mode,
            }
        except Exception as e:
            return {"success": False, "error": str(e), "data": [], "url": url}

    # ── data quality validation (pure LLM) ───────────────────

    def validate_data_quality(self, data: List[Dict], task_description: str, limit: int) -> Dict[str, Any]:
        if not data:
            return {"valid": False, "reason": "数据为空", "suggestion": "换页面或换选择器"}
        normalized_task = str(task_description or "").lower()
        if self._task_mentions_weather(task_description):
            if self._weather_data_has_required_signals(data):
                return {
                    "valid": True,
                    "reason": "天气详情已包含足够的关键字段信号",
                    "suggestion": "",
                }
            return {
                "valid": False,
                "reason": "天气数据缺少足够的温度、天气状况、湿度、风力或空气质量信号",
                "suggestion": "优先保留天气详情文本块或切换到更完整的天气详情页",
            }
        requires_rich_metadata = any(
            token in normalized_task
            for token in (
                "points", "score", "评分", "分数",
                "comments", "评论",
                "author", "作者", "user", "用户",
                "time", "date", "时间", "日期", "发布时间",
            )
        )
        looks_like_news_list = any(
            token in normalized_task
            for token in ("news", "story", "stories", "headline", "headlines", "新闻", "头条", "列表", "top", "前")
        )
        looks_like_generic_list = looks_like_news_list or self._looks_like_list_page_task(task_description)
        if looks_like_generic_list and not requires_rich_metadata:
            reference_url = self._primary_task_url(task_description)
            required_count = max(1, min(limit, 3))
            usable_items = 0
            for item in data[:max(required_count, min(len(data), 8))]:
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title") or item.get("name") or item.get("text") or "").strip()
                link = str(item.get("link") or item.get("title_link") or item.get("url") or "").strip()
                if (
                    title
                    and len(title) >= 4
                    and (link.startswith("http") or link.startswith("/"))
                    and looks_like_detail_list_item({"title": title, "url": link}, reference_url=reference_url)
                ):
                    usable_items += 1
            if usable_items >= required_count:
                return {"valid": True, "reason": "标题和链接已满足列表抓取任务", "suggestion": ""}
            return {
                "valid": False,
                "reason": "抓取结果更像导航、筛选或翻页链接，而不是目标列表实体",
                "suggestion": "优先定位重复列表区域，并排除同页筛选、分页和站点导航链接",
            }
        detail_like_items = 0
        for item in data[: max(1, min(len(data), 5))]:
            if not isinstance(item, dict):
                continue
            summary = str(item.get("summary") or item.get("text") or item.get("content") or "").strip()
            title = str(item.get("title") or item.get("name") or "").strip()
            if len(summary) >= 40 or (title and len(title) >= 6 and len(summary) >= 20):
                detail_like_items += 1
        if detail_like_items >= 1 and not looks_like_generic_list:
            return {
                "valid": True,
                "reason": "详情页正文或摘要信号已满足内容提取任务",
                "suggestion": "",
            }
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

    async def _collect_page_observation_context(
        self,
        tk: BrowserToolkit,
        task_description: str,
    ) -> Dict[str, Any]:
        url_r = await tk.get_current_url()
        html_r = await tk.get_page_html()
        raw_html = html_r.data or ""
        html = raw_html
        html = RE_SCRIPT_TAG.sub('', html)
        html = RE_STYLE_TAG.sub('', html)
        html = RE_HTML_COMMENT.sub('', html)
        html = RE_WHITESPACE.sub(' ', html)

        web_debug_recorder.write_text("page_raw_html", raw_html, suffix=".html")
        web_debug_recorder.write_text("page_cleaned_html", html, suffix=".html")
        web_debug_recorder.record_event(
            "observation_context",
            url=url_r.data or "",
            raw_html_length=len(raw_html),
            cleaned_html_length=len(html),
            task=task_description,
        )

        semantic_snapshot: Dict[str, Any] = {}
        semantic_snapshot_text = "(无语义快照)"
        try:
            snapshot_r = await tk.semantic_snapshot(max_elements=80, include_cards=True)
            if snapshot_r.success and isinstance(snapshot_r.data, dict):
                semantic_snapshot = snapshot_r.data
                semantic_snapshot_text = self._format_semantic_snapshot_for_llm(semantic_snapshot)
                web_debug_recorder.write_json("semantic_snapshot", semantic_snapshot)
                web_debug_recorder.write_text("semantic_snapshot_llm", semantic_snapshot_text)
                log_agent_action(
                    self.name,
                    "语义快照提取完成",
                    f"page_type={semantic_snapshot.get('page_type', 'unknown')} cards={len(semantic_snapshot.get('cards', []) or [])}"
                )
        except Exception as exc:
            log_warning(f"语义快照提取失败，继续使用旧页面感知: {exc}")

        page_structure_text = ""
        page_structure_obj = None
        page_structure_payload: Dict[str, Any] = {}
        try:
            page_structure_obj = await self.page_perceiver.perceive_page(tk, task_description)
            page_structure_text = page_structure_obj.to_llm_prompt()
            page_structure_payload = self._page_structure_to_debug_payload(page_structure_obj)
            web_debug_recorder.write_json(
                "page_structure",
                page_structure_payload,
            )
            web_debug_recorder.write_text("page_structure_llm", page_structure_text)
            log_agent_action(
                self.name,
                "页面结构提取完成",
                f"{len(page_structure_obj.main_content_blocks)} 个内容块, {len(page_structure_obj.interactive_elements)} 个交互元素"
            )
        except Exception as e:
            logger.exception(f"页面结构提取失败，降级为纯HTML分析: {e}")
            page_structure_text = PAGE_STRUCTURE_FAILED_MARKER
            web_debug_recorder.record_event(
                "page_structure_failed",
                error=str(e),
            )

        return {
            "url": url_r.data or "",
            "raw_html": raw_html,
            "html": html,
            "semantic_snapshot": semantic_snapshot,
            "semantic_snapshot_text": semantic_snapshot_text,
            "page_structure_text": page_structure_text,
            "page_structure_payload": page_structure_payload,
        }

    async def _maybe_handle_semantic_search_results(
        self,
        tk: BrowserToolkit,
        task_description: str,
        limit: int,
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        active_snapshot = snapshot or {}
        if not active_snapshot:
            try:
                snapshot_r = await tk.semantic_snapshot(max_elements=80, include_cards=True)
                if snapshot_r.success and isinstance(snapshot_r.data, dict):
                    active_snapshot = snapshot_r.data
            except Exception:
                active_snapshot = {}

        if str(active_snapshot.get("page_type", "") or "") != "serp":
            return {"handled": False}

        allow_serp_answer = self._task_allows_serp_answer(task_description, limit)
        ranked_cards, serp_sufficient = self._rank_snapshot_search_cards(
            active_snapshot,
            task_description,
            task_description,
            max_results=max(1, min(limit, 5)),
        )
        if not ranked_cards:
            return {"handled": False}

        if serp_sufficient and allow_serp_answer:
            return {
                "handled": True,
                "return_data": True,
                "data": ranked_cards[:limit],
                "mode": "semantic_search_results",
            }

        try:
            quality = self.validate_data_quality(
                ranked_cards[: min(3, len(ranked_cards))],
                task_description,
                min(limit, max(1, len(ranked_cards))),
            )
            if allow_serp_answer and quality.get("valid") and any(
                str(card.get("snippet", card.get("summary", "")) or "").strip()
                for card in ranked_cards[:2]
            ):
                return {
                    "handled": True,
                    "return_data": True,
                    "data": ranked_cards[:limit],
                    "mode": "semantic_search_results",
                }
        except Exception:
            pass

        best_card = ranked_cards[0]
        target_ref = str(best_card.get("target_ref", "") or "").strip()
        target_url = str(best_card.get("link", "") or "").strip()
        if target_ref:
            click_r = await tk.click_ref(target_ref)
            if click_r.success:
                log_agent_action(self.name, "语义搜索结果预导航", target_ref)
                await tk.human_delay(300, 1200)
                await tk.wait_for_load("domcontentloaded", timeout=10000)
                await tk.wait_for_load("networkidle", timeout=5000)
                await tk.wait_for_page_type_change("serp", timeout=4000)
                return {
                    "handled": True,
                    "navigated": True,
                    "target_ref": target_ref,
                    "target_url": target_url,
                }

        if target_url:
            goto_r = await tk.goto(target_url, timeout=30000)
            if goto_r.success:
                log_agent_action(self.name, "语义搜索结果预导航", target_url[:120])
                await tk.human_delay(300, 1200)
                await tk.wait_for_load("domcontentloaded", timeout=10000)
                await tk.wait_for_load("networkidle", timeout=5000)
                return {
                    "handled": True,
                    "navigated": True,
                    "target_ref": "",
                    "target_url": target_url,
                }

        return {"handled": False}

    # ── search (uses toolkit) ──────────────────────────────────

    def plan_search_queries(
        self,
        task_description: str,
        *,
        base_query: str = "",
        domain_hints: Optional[List[str]] = None,
        max_queries: int = 3,
    ) -> List[str]:
        query_budget = self._search_query_budget(task_description or base_query, max_queries)
        domain_hints = [item for item in (domain_hints or self._extract_domain_hints(task_description)) if item]
        fallback = self._fallback_search_queries(
            task_description,
            base_query=base_query,
            domain_hints=domain_hints,
            max_queries=query_budget,
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
                if len(queries) >= query_budget:
                    break
            return self._dedupe_search_queries(queries or fallback, max_queries=query_budget)
        except Exception:
            return self._dedupe_search_queries(fallback, max_queries=query_budget)

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
        snippet = str(card.get("snippet", card.get("summary", "")) or "").strip()
        if len(snippet) >= 30:
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
                    "snippet": str(card.get("snippet", card.get("summary", "")) or "")[:320],
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
        current_url_result = await tk.get_current_url()
        current_url = str(current_url_result.data or "") if current_url_result.success else ""
        selectors = get_search_result_selectors(current_url)
        result = await tk.evaluate_js(
            r"""(payload) => {
                const compact = (text) => (text || '').replace(/\s+/g, ' ').trim();
                const toAbsoluteUrl = (value) => {
                    const text = compact(value);
                    if (!text || /^javascript:/i.test(text)) return '';
                    try {
                        return new URL(text, location.href).toString();
                    } catch (_error) {
                        return '';
                    }
                };
                const cleanHost = (value) => String(value || '').replace(/^www\./i, '').toLowerCase();
                const hostOf = (value) => {
                    try {
                        return cleanHost(new URL(value, location.href).hostname);
                    } catch (_error) {
                        return '';
                    }
                };
                const parseDataLog = (value) => {
                    const text = compact(value);
                    if (!text) return '';
                    try {
                        const parsed = JSON.parse(text);
                        return compact(
                            parsed.mu ||
                            parsed.url ||
                            parsed.target ||
                            parsed.lmu ||
                            parsed.land_url ||
                            (parsed.data && (parsed.data.mu || parsed.data.url || parsed.data.target)) ||
                            ''
                        );
                    } catch (_error) {
                        return '';
                    }
                };
                const decodeParamValue = (value) => {
                    let text = compact(value);
                    if (!text) return '';
                    for (let i = 0; i < 2; i += 1) {
                        try {
                            const decoded = decodeURIComponent(text);
                            if (decoded === text) break;
                            text = decoded;
                        } catch (_error) {
                            break;
                        }
                    }
                    return /^https?:/i.test(text) ? text : '';
                };
                const extractRedirectTarget = (value) => {
                    const href = toAbsoluteUrl(value);
                    if (!href) return '';
                    try {
                        const parsed = new URL(href, location.href);
                        const candidates = ['uddg', 'u', 'url', 'q', 'target', 'redirect', 'imgurl']
                            .flatMap((key) => parsed.searchParams.getAll(key))
                            .map((candidate) => decodeParamValue(candidate))
                            .filter(Boolean);
                        return candidates[0] || '';
                    } catch (_error) {
                        return '';
                    }
                };
                const resolveResultLink = (node, anchor) => {
                    const rawHref = toAbsoluteUrl(anchor.href || anchor.getAttribute('href') || '');
                    const currentHost = cleanHost(location.hostname || '');
                    const candidates = [
                        extractRedirectTarget(rawHref),
                        anchor.getAttribute('mu'),
                        anchor.getAttribute('data-landurl'),
                        anchor.getAttribute('data-url'),
                        anchor.getAttribute('data-target'),
                        node.getAttribute('mu'),
                        node.getAttribute('data-landurl'),
                        node.getAttribute('data-url'),
                        node.getAttribute('data-target'),
                        parseDataLog(anchor.getAttribute('data-log') || ''),
                        parseDataLog(node.getAttribute('data-log') || ''),
                    ]
                        .map((value) => toAbsoluteUrl(value))
                        .filter(Boolean);
                    const external = candidates.find((value) => {
                        const candidateHost = hostOf(value);
                        return candidateHost && candidateHost !== currentHost;
                    }) || '';
                    return {
                        rawHref,
                        targetUrl: external || candidates[0] || '',
                        link: external || candidates[0] || rawHref,
                    };
                };
                const selectors = Array.isArray(payload.selectors) ? payload.selectors : [];
                const limit = Number(payload.limit || 10);
                const seenNodes = new Set();
                const candidates = [];

                for (const selector of selectors) {
                    for (const node of Array.from(document.querySelectorAll(selector))) {
                        if (!node || seenNodes.has(node)) continue;
                        seenNodes.add(node);
                        candidates.push(node);
                        if (candidates.length >= Math.max(limit * 8, 24)) break;
                    }
                    if (candidates.length >= Math.max(limit * 8, 24)) break;
                }

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

                const seen = new Set();
                const cards = [];
                for (const node of candidates) {
                    if (node.closest('header, nav, footer, form, aside')) continue;
                    const anchor = node.querySelector('h1 a[href], h2 a[href], h3 a[href], h4 a[href], a[href]');
                    if (!anchor) continue;
                    const resolvedLink = resolveResultLink(node, anchor);
                    const href = resolvedLink.link;
                    if (!href || !/^https?:/i.test(href)) continue;
                    if (seen.has(href)) continue;
                    const titleNode = node.querySelector('h1, h2, h3, h4') || anchor;
                    const title = compact(titleNode.innerText || titleNode.textContent || '').slice(0, 220);
                    if (!title || title.length < 8) continue;
                    const snippet = getSnippet(node, title);
                    if (!snippet && compact(node.innerText || node.textContent || '').length < 40) continue;
                    seen.add(href);
                    const sourceNode = node.querySelector('cite, .source, .b_attribution, [class*="source"], [data-testid*="source"]');
                    const dateNode = node.querySelector('time, [datetime], .news_dt, [class*="date"]');
                    cards.push({
                        title,
                        link: href,
                        raw_link: resolvedLink.rawHref,
                        target_url: resolvedLink.targetUrl,
                        source: compact(sourceNode ? (sourceNode.innerText || sourceNode.textContent || '') : '').slice(0, 120),
                        date: compact(dateNode ? (dateNode.innerText || dateNode.textContent || '') : '').slice(0, 80),
                        snippet,
                    });
                    if (cards.length >= Math.max(limit * 4, 12)) break;
                }
                return cards;
            }""",
            {
                "limit": max_results,
                "selectors": selectors,
            },
        )
        cards = result.data if result.success and isinstance(result.data, list) else []
        web_debug_recorder.write_json(
            "search_result_cards_raw",
            {
                "query": query,
                "count": len(cards),
                "items": cards[: max_results * 4],
            },
        )
        filtered: List[Dict[str, Any]] = []
        seen_links: Set[str] = set()
        for item in cards:
            if not isinstance(item, dict):
                continue
            candidate_links: List[str] = []
            for candidate in (
                item.get("target_url", ""),
                item.get("link", ""),
                item.get("raw_link", ""),
            ):
                resolved = self._decode_redirect_url(str(candidate or "").strip())
                if resolved and resolved.startswith("http") and resolved not in candidate_links:
                    candidate_links.append(resolved)
            link = next(
                (candidate for candidate in candidate_links if not self._is_search_engine_domain(candidate)),
                "",
            )
            if not link or not link.startswith("http"):
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
                    "url": link,
                    "source": str(item.get("source", "") or "").strip(),
                    "date": str(item.get("date", "") or "").strip(),
                    "snippet": str(item.get("snippet", "") or "").strip(),
                    "target_ref": str(item.get("target_ref", "") or "").strip(),
                    "target_selector": str(item.get("target_selector", "") or "").strip(),
                }
            )
        web_debug_recorder.write_json(
            "search_result_cards_filtered",
            {
                "query": query,
                "count": len(filtered),
                "items": filtered[: max_results * 2],
            },
        )
        ranked, _ = self._rerank_search_results(query, query, filtered, max_results=max_results)
        return ranked

    def _looks_like_search_blocked_page(self, url: str, title: str = "", body_text: str = "") -> bool:
        normalized_url = str(url or "").lower()
        normalized_title = str(title or "").lower()
        normalized_body = str(body_text or "").lower()
        url_tokens = ("/sorry", "/captcha", "/verify", "/challenge", "/blocked")
        text_tokens = ("unusual traffic", "robot check", "captcha", "异常流量", "人机身份验证", "验证码", "安全验证")
        return any(token in normalized_url for token in url_tokens) or any(
            token in normalized_title or token in normalized_body for token in text_tokens
        )

    def _looks_like_search_results_url(self, url: str) -> bool:
        return looks_like_search_results_url(url or "")

    async def _wait_for_search_results_ready(self, tk: BrowserToolkit, search_url: str) -> bool:
        selector = ", ".join(get_search_result_selectors(search_url))

        last_probe: Dict[str, Any] = {}
        last_snapshot: Dict[str, Any] = {}
        for _ in range(4):
            dom_ready = await tk.wait_for_load("domcontentloaded", timeout=8000)
            network_idle = await tk.wait_for_load("networkidle", timeout=2500)
            current_url_result = await tk.get_current_url()
            current_url = str(current_url_result.data or search_url) if current_url_result.success else search_url
            title = ""
            if hasattr(tk, "get_title"):
                title_result = await tk.get_title()
                title = str(title_result.data or "") if title_result.success else ""
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
                        bodyText: (document.body && document.body.innerText ? document.body.innerText.slice(0, 4000) : ''),
                    };
                }""",
                selector,
            )
            snapshot: Dict[str, Any] = {}
            if hasattr(tk, "semantic_snapshot"):
                try:
                    snapshot_r = await tk.semantic_snapshot(max_elements=60, include_cards=True)
                    if snapshot_r.success and isinstance(snapshot_r.data, dict):
                        snapshot = snapshot_r.data
                except Exception:
                    snapshot = {}
            if summary.success and isinstance(summary.data, dict):
                body_text = str(summary.data.get("bodyText", "") or "")
                affordances = snapshot.get("affordances", {}) if isinstance(snapshot, dict) else {}
                card_count = len(snapshot.get("cards", []) or []) if isinstance(snapshot, dict) else 0
                collection_item_count = (
                    int(affordances.get("collection_item_count", 0) or 0)
                    if isinstance(affordances, dict)
                    else 0
                )
                has_snapshot_results = bool(
                    card_count > 0
                    or collection_item_count >= 3
                    or (isinstance(affordances, dict) and affordances.get("has_results"))
                )
                last_probe = {
                    "search_url": search_url,
                    "current_url": current_url,
                    "title": title,
                    "selector": selector,
                    "dom_ready": bool(dom_ready.success),
                    "network_idle": bool(network_idle.success),
                    "matches": int(summary.data.get("matches", 0) or 0),
                    "text_length": int(summary.data.get("textLength", 0) or 0),
                    "looks_like_results_url": self._looks_like_search_results_url(current_url),
                    "snapshot_page_type": str(snapshot.get("page_type", "") or ""),
                    "snapshot_card_count": card_count,
                    "snapshot_collection_item_count": collection_item_count,
                    "snapshot_has_results": has_snapshot_results,
                }
                web_debug_recorder.write_json("search_ready_probe", last_probe)
                if snapshot:
                    last_snapshot = snapshot
                if self._looks_like_search_blocked_page(current_url, title, body_text):
                    return False
                matches = int(summary.data.get("matches", 0) or 0)
                if has_snapshot_results and (
                    self._looks_like_search_results_url(current_url)
                    or int(summary.data.get("textLength", 0) or 0) >= 300
                ):
                    return True
                if matches > 0 and self._looks_like_search_results_url(current_url):
                    return True
                if matches >= 3:
                    return True
            await tk.human_delay(400, 1200)
        if last_probe:
            web_debug_recorder.record_event("search_ready_failed", **last_probe)
        if last_snapshot:
            web_debug_recorder.write_json("search_ready_last_snapshot", last_snapshot)
        if hasattr(tk, "get_page_html"):
            try:
                html_r = await tk.get_page_html()
                if html_r.success and html_r.data:
                    web_debug_recorder.write_text("search_ready_page_html", html_r.data, suffix=".html")
            except Exception:
                pass
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

        # 🔥 新增：优先尝试 API 搜索
        log_agent_action(self.name, "尝试 API 搜索（优先策略）")
        api_response = await self.search_engine_manager.search(
            query=query,
            max_results=max_results,
            strategies=[SearchStrategy.API]
        )

        if api_response.success and api_response.results:
            log_success(f"API 搜索成功，找到 {len(api_response.results)} 个结果")
            # 转换为原有格式
            cards = []
            for result in api_response.results:
                cards.append({
                    "title": result.title,
                    "link": result.url,
                    "url": result.url,
                    "snippet": result.snippet,
                    "source": result.source,
                })
            return cards

        log_warning(f"API 搜索失败: {api_response.error}，降级到原生搜索")

        # 降级到原生搜索
        own_tk = tk is None
        if own_tk:
            tk = self._create_toolkit(headless=True if headless is None else headless)
            await tk.create_page()
        cards: List[Dict[str, Any]] = []
        try:
            # 定义搜索引擎配置：首页 + 搜索框选择器
            search_engines = list(iter_search_engine_profiles())

            for engine in search_engines:
                direct_search_url = engine.build_search_url(query)

                if direct_search_url:
                    web_debug_recorder.record_event(
                        "search_engine_attempt",
                        engine=engine.name,
                        phase="direct",
                        query=query,
                        url=direct_search_url,
                    )
                    log_agent_action(self.name, f"尝试 {engine.name} 直达搜索结果页", query[:40])
                    goto_r = await tk.goto(direct_search_url)
                    if goto_r.success:
                        ready = await self._wait_for_search_results_ready(tk, direct_search_url)
                        if ready:
                            semantic_cards = await self._extract_search_cards_from_semantic_snapshot(
                                tk,
                                task_description or query,
                                query,
                                max_results=max_results,
                            )
                            if semantic_cards:
                                cards = semantic_cards
                                log_success(f"{engine.name} 语义快照搜索成功，找到 {len(cards)} 个结果")
                                break
                            raw_cards = await self._extract_search_result_cards(tk, query, max_results=max_results * 2)
                            if raw_cards:
                                cards, _ = self._rerank_search_results(
                                    task_description or query,
                                    query,
                                    raw_cards,
                                    max_results=max_results,
                                )
                                if cards:
                                    log_success(f"{engine.name} 直达搜索成功，找到 {len(cards)} 个结果")
                                    break
                    else:
                        log_warning(f"无法访问 {direct_search_url}")

                web_debug_recorder.record_event(
                    "search_engine_attempt",
                    engine=engine.name,
                    phase="native",
                    query=query,
                    url=engine.homepage,
                )
                log_agent_action(self.name, f"尝试 {engine.name} 原生搜索", query[:40])

                # 使用原生搜索框输入
                success = await self._perform_native_search(
                    tk,
                    engine.homepage,
                    get_search_input_selectors(engine.homepage),
                    query,
                )

                if not success:
                    continue

                # 等待结果加载
                url_result = await tk.get_current_url()
                if not url_result.success:
                    log_warning(f"获取当前URL失败: {getattr(url_result, 'error', '')}")
                    continue
                current_url = url_result.data or ""
                ready = await self._wait_for_search_results_ready(tk, current_url)
                if not ready:
                    log_warning(f"{engine.name} 未进入稳定结果页，跳过该搜索源")
                    await tk.human_delay(800, 1800)
                    continue

                semantic_cards = await self._extract_search_cards_from_semantic_snapshot(
                    tk,
                    task_description or query,
                    query,
                    max_results=max_results,
                )
                if semantic_cards:
                    cards = semantic_cards
                    log_success(f"{engine.name} 语义快照搜索成功，找到 {len(cards)} 个结果")
                    break

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
                        log_success(f"{engine.name} 搜索成功，找到 {len(cards)} 个结果")
                        break

            if not cards:
                log_warning("所有原生搜索引擎均未找到结果")

                # 🔥 新增：最终降级到直接 URL 策略
                log_agent_action(self.name, "尝试直接 URL 策略（最终降级）")
                direct_response = await self.search_engine_manager.search(
                    query=query,
                    max_results=max_results,
                    strategies=[SearchStrategy.DIRECT]
                )

                if direct_response.success and direct_response.results:
                    log_success(f"直接 URL 策略成功，返回 {len(direct_response.results)} 个结果")
                    for result in direct_response.results:
                        cards.append({
                            "title": result.title,
                            "link": result.url,
                            "url": result.url,
                            "snippet": result.snippet,
                            "source": result.source,
                        })
                else:
                    log_error("所有搜索策略（API + 原生 + 直接URL）均失败")

        except Exception as e:
            logger.exception(f"搜索失败: {e}")
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
        return decode_search_redirect_url(href or "")

    @staticmethod
    def _is_search_engine_domain(url: str) -> bool:
        return is_search_engine_domain(url or "")

    @staticmethod
    def _urls_match_for_navigation(current_url: str, target_url: str) -> bool:
        current_value = str(current_url or "").strip()
        target_value = str(target_url or "").strip()
        if not current_value or not target_value:
            return False
        if current_value.rstrip("/") == target_value.rstrip("/"):
            return True
        try:
            current_parsed = urlparse(current_value)
            target_parsed = urlparse(target_value)
        except Exception:
            return False
        same_origin = (
            current_parsed.scheme == target_parsed.scheme
            and current_parsed.netloc == target_parsed.netloc
        )
        if not same_origin:
            return False
        current_path = (current_parsed.path or "").rstrip("/")
        target_path = (target_parsed.path or "").rstrip("/")
        if current_path != target_path:
            return False
        if not target_parsed.query or target_parsed.query == current_parsed.query:
            return True
        return target_parsed.query in (current_parsed.query or "")

    async def _open_search_candidate_from_search_page(
        self,
        tk: BrowserToolkit,
        candidate: Dict[str, Any],
        *,
        prefer_click: bool = True,
    ) -> Dict[str, Any]:
        current_url = ""
        if hasattr(tk, "get_current_url"):
            try:
                current_url_r = await tk.get_current_url()
                if current_url_r.success:
                    current_url = str(current_url_r.data or "").strip()
            except Exception:
                current_url = ""

        if not self._is_search_engine_domain(current_url):
            return {"opened": False, "method": "", "target_url": ""}

        target_ref = str(candidate.get("target_ref", "") or "").strip()
        target_url = str(candidate.get("link", "") or candidate.get("url", "") or "").strip()
        if prefer_click and target_ref and hasattr(tk, "click_ref"):
            click_r = await tk.click_ref(target_ref)
            if click_r.success:
                await tk.human_delay(180, 600)
                await tk.wait_for_load("domcontentloaded", timeout=10000)
                if hasattr(tk, "wait_for_page_type_change"):
                    await tk.wait_for_page_type_change("serp", timeout=4000)
                if not self.fast_mode:
                    await tk.wait_for_load("networkidle", timeout=5000)
                current_after = ""
                if hasattr(tk, "get_current_url"):
                    try:
                        current_after_r = await tk.get_current_url()
                        if current_after_r.success:
                            current_after = str(current_after_r.data or "").strip()
                    except Exception:
                        current_after = ""
                if current_after and not self._is_search_engine_domain(current_after):
                    web_debug_recorder.record_event(
                        "search_target_opened_via_click_ref",
                        search_url=current_url,
                        target_ref=target_ref,
                        target_url=current_after or target_url,
                    )
                    return {
                        "opened": True,
                        "method": "click_ref",
                        "target_url": current_after or target_url,
                    }

        if target_url and hasattr(tk, "new_tab"):
            new_tab_r = await tk.new_tab(target_url)
            if not new_tab_r.success:
                web_debug_recorder.record_event(
                    "search_target_open_in_new_tab_failed",
                    search_url=current_url,
                    target_url=target_url,
                    error=str(getattr(new_tab_r, "error", "") or ""),
                )
                return {"opened": False, "method": "", "target_url": target_url}

            web_debug_recorder.record_event(
                "search_target_opened_in_new_tab",
                search_url=current_url,
                target_url=target_url,
            )
            await tk.human_delay(180, 600)
            await tk.wait_for_load("domcontentloaded", timeout=10000)
            if not self.fast_mode:
                await tk.wait_for_load("networkidle", timeout=5000)
            return {"opened": True, "method": "new_tab", "target_url": target_url}

        return {"opened": False, "method": "", "target_url": target_url}

    async def _open_target_in_new_tab_from_search_page(
        self,
        tk: BrowserToolkit,
        target_url: str,
    ) -> bool:
        result = await self._open_search_candidate_from_search_page(
            tk,
            {"link": str(target_url or "").strip()},
            prefer_click=False,
        )
        return bool(result.get("opened"))

    async def _return_to_search_results_tab(self, tk: BrowserToolkit, *, open_method: str = "") -> bool:
        current_url = ""
        if hasattr(tk, "get_current_url"):
            try:
                current_url_r = await tk.get_current_url()
                if current_url_r.success:
                    current_url = str(current_url_r.data or "").strip()
            except Exception:
                current_url = ""

        if self._is_search_engine_domain(current_url):
            return True

        if open_method == "click_ref" and hasattr(tk, "go_back"):
            back_r = await tk.go_back()
            if back_r.success:
                try:
                    current_url_r = await tk.get_current_url()
                    current_url = str(current_url_r.data or "").strip() if current_url_r.success else ""
                except Exception:
                    current_url = ""
                if self._is_search_engine_domain(current_url):
                    web_debug_recorder.record_event(
                        "search_results_tab_restored",
                        method="go_back",
                        current_url=current_url,
                    )
                    return True

        if hasattr(tk, "close_tab"):
            close_r = await tk.close_tab()
            if close_r.success:
                try:
                    current_url_r = await tk.get_current_url()
                    current_url = str(current_url_r.data or "").strip() if current_url_r.success else ""
                except Exception:
                    current_url = ""
                if self._is_search_engine_domain(current_url):
                    web_debug_recorder.record_event(
                        "search_results_tab_restored",
                        method="close_tab",
                        current_url=current_url,
                    )
                    return True

        if hasattr(tk, "switch_tab"):
            switch_r = await tk.switch_tab(0)
            if switch_r.success:
                try:
                    current_url_r = await tk.get_current_url()
                    current_url = str(current_url_r.data or "").strip() if current_url_r.success else ""
                except Exception:
                    current_url = ""
                if self._is_search_engine_domain(current_url):
                    web_debug_recorder.record_event(
                        "search_results_tab_restored",
                        method="switch_tab",
                        current_url=current_url,
                    )
                    return True

        web_debug_recorder.record_event(
            "search_results_tab_restore_failed",
            current_url=current_url,
        )
        return False

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
        search_context = str(base_query or "").strip() or task_description
        allow_serp_answer = self._task_allows_serp_answer(search_context, max_results)
        queries = self.plan_search_queries(
            search_context,
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
                task_description=search_context,
                max_results=max_results,
                headless=headless,
                tk=tk,
            )
            ranked_cards, ranked_serp_sufficient = self._rerank_search_results(
                search_context,
                query,
                cards,
                max_results=max_results,
            )
            serp_sufficient = serp_sufficient or (allow_serp_answer and ranked_serp_sufficient)
            for card in ranked_cards:
                link = str(card.get("link", "") or "").strip()
                if not link or link in seen_links:
                    continue
                seen_links.add(link)
                aggregate_cards.append(card)

        ranked_cards, _ = self._rerank_search_results(
            search_context,
            " ".join(queries),
            aggregate_cards,
            max_results=max_results,
        )
        if ranked_cards and allow_serp_answer and not serp_sufficient:
            try:
                quality = self.validate_data_quality(
                    ranked_cards[: min(3, len(ranked_cards))],
                    search_context,
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

        url_r = await tk.get_current_url()
        reference_url = str(url_r.data or "").strip()
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
            detail_score = score_detail_like_url(link, reference_url=reference_url, title=title)
            if detail_score < 1 and token_hits == 0 and len(filtered) >= max(2, limit // 2):
                continue
            filtered.append(
                {
                    "title": title,
                    "link": link,
                    "_rank_score": detail_score * 2 + token_hits,
                }
            )

        filtered.sort(key=lambda item: item.get("_rank_score", 0), reverse=True)
        normalized = normalize_web_results(
            filtered,
            task_description,
            limit=max(limit * 2, 12),
            understanding={"page_type": "list"},
        )
        detail_like = [
            item for item in normalized
            if looks_like_detail_list_item(item, reference_url=reference_url)
        ]
        final_items = detail_like if len(detail_like) >= max(1, min(limit, 3)) else normalized
        return final_items[:limit]

    async def extract_table_links_fallback(
        self,
        tk: BrowserToolkit,
        task_description: str,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Fallback extractor for table/list pages where row anchors are more reliable than field selectors."""
        r = await tk.evaluate_js(
            """(limit) => {
                const rows = Array.from(document.querySelectorAll(
                    "table tbody tr, .list_list tbody tr, tr[onclick], table tr"
                ));
                const abs = (href) => {
                    try { return new URL(href, location.href).toString(); } catch { return ""; }
                };
                const out = [];
                const seen = new Set();
                for (const row of rows) {
                    const rowText = (row.innerText || row.textContent || "").replace(/\\s+/g, " ").trim();
                    if (!rowText || rowText.length < 4) continue;
                    const anchors = Array.from(row.querySelectorAll("a[href]"));
                    let best = null;
                    for (const anchor of anchors) {
                        const title = (anchor.innerText || anchor.textContent || "").replace(/\\s+/g, " ").trim();
                        const href = abs(anchor.getAttribute("href") || "");
                        if (!title || !href || !href.startsWith("http")) continue;
                        if (!best || title.length > best.title.length) {
                            best = { title, link: href };
                        }
                    }
                    if (!best) continue;
                    const dedupeKey = `${best.title}|${best.link}`;
                    if (seen.has(dedupeKey)) continue;
                    seen.add(dedupeKey);
                    out.push({
                        title: best.title.slice(0, 220),
                        link: best.link,
                        row_text: rowText.slice(0, 400),
                    });
                    if (out.length >= Math.max(limit * 3, 30)) break;
                }
                return out;
            }""",
            max(3, min(limit, 20)),
        )
        if not r.success or not isinstance(r.data, list):
            return []

        header_tokens = {
            "仅标题", "标题", "漏洞编号", "编号", "cve", "cnnvd", "cnvd", "bid",
            "发布日期", "提交时间", "等级", "危害级别", "操作", "序号",
        }
        task_mentions_vulnerability = "漏洞" in str(task_description or "")
        filtered: List[Dict[str, Any]] = []
        for item in r.data:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "") or "").strip()
            link = str(item.get("link", "") or "").strip()
            row_text = str(item.get("row_text", "") or "").strip()
            if not title or not link:
                continue
            normalized_title = title.lower()
            if normalized_title in header_tokens:
                continue
            if task_mentions_vulnerability:
                vulnerability_signals = (
                    "漏洞" in title
                    or "CNVD-" in row_text
                    or "CNNVD-" in row_text
                    or "CVE-" in row_text
                    or "/flaw/show" in link
                    or "/show/" in link
                )
                if not vulnerability_signals and len(filtered) >= max(2, limit // 2):
                    continue
            filtered.append({"title": title, "link": link})
            if len(filtered) >= limit:
                break
        return filtered

    # ── page analysis & extraction (uses toolkit) ─────────────

    async def analyze_page_structure(self, tk: BrowserToolkit, task_description: str) -> Dict[str, Any]:
        log_agent_action(self.name, "分析页面结构")
        observation = await self._collect_page_observation_context(tk, task_description)
        url = observation.get("url", "")
        html = observation.get("html", "") or ""
        semantic_snapshot = observation.get("semantic_snapshot", {}) or {}
        semantic_snapshot_text = observation.get("semantic_snapshot_text", "(无语义快照)")
        page_structure_text = observation.get("page_structure_text", PAGE_STRUCTURE_FAILED_MARKER)

        normalized_url = self.cache.normalize_url(url)
        task_signature = self.cache.build_task_signature(task_description)
        page_fingerprint = self.cache.build_page_fingerprint(html)
        cache_key = self.cache.build_key(
            "page_structure_analysis",
            normalized_url=normalized_url,
            task_signature=task_signature,
            page_fingerprint=page_fingerprint,
            prompt_version="page_analysis_prompt_v4",  # 加入语义快照
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
            log_agent_action(self.name, "命中页面结构分析缓存", normalized_url[:80])
            log_debug_metrics("llm_cache.page_analysis", self.cache.snapshot_stats())
            return cached

        # 🔥 优化：如果有页面结构，大幅缩减HTML（只保留关键片段）
        html_for_llm = ""
        has_valid_structure = (
            page_structure_text
            and page_structure_text != PAGE_STRUCTURE_FAILED_MARKER
        )

        if has_valid_structure:
            # 有页面结构时，只传精简的HTML片段
            html_cleaned = self._clean_html_for_llm(html[:HTML_PRE_CLEAN_LENGTH])
            html_for_llm = html_cleaned[:HTML_MAX_LENGTH_WITH_STRUCTURE]
            if len(html_cleaned) > HTML_MAX_LENGTH_WITH_STRUCTURE:
                html_for_llm += "\n... (已省略，请优先使用页面结构概览)"
            log_agent_action(
                self.name,
                "HTML精简完成（有页面结构）",
                f"原始: {len(html)} 字符 → 传给LLM: {len(html_for_llm)} 字符"
            )
        else:
            # 没有页面结构时，传完整的清洗后HTML（降级模式）
            if len(html) > HTML_MAX_LENGTH_WITHOUT_STRUCTURE:
                html = html[:HTML_MAX_LENGTH_WITHOUT_STRUCTURE] + "\n... (truncated)"
            html_cleaned = self._clean_html_for_llm(html)
            html_for_llm = html_cleaned
            original_len = len(html)
            cleaned_len = len(html_cleaned)
            reduction_pct = (1 - cleaned_len / original_len) * 100 if original_len > 0 else 0
            log_agent_action(
                self.name,
                "HTML清洗完成（降级模式）",
                f"原始: {original_len} 字符, 清洗后: {cleaned_len} 字符, 减少: {reduction_pct:.1f}%"
            )

        page_analysis_prompt = PAGE_ANALYSIS_PROMPT.format(
            task_description=task_description,
            semantic_snapshot=semantic_snapshot_text,
            html_content=html_for_llm,
            page_structure=page_structure_text,
            candidate_regions="(候选区域未单独构建，请结合语义快照和页面结构概览)",
            current_url=url,
        )
        web_debug_recorder.write_text("page_analysis_html_for_llm", html_for_llm, suffix=".html")
        web_debug_recorder.write_text("page_analysis_prompt", page_analysis_prompt)

        # 🔥 修改：传入页面结构 + 精简HTML
        response = self.llm.chat_with_system(
            system_prompt=page_analysis_prompt,
            user_message="请分析页面结构并返回选择器配置",
            temperature=0.2, json_mode=True,
        )
        web_debug_recorder.write_text("page_analysis_response", response.content)
        try:
            config = self.llm.parse_json_response(response)
            config = self._normalize_selector_config(config, url, semantic_snapshot)
            web_debug_recorder.write_json("page_analysis_config", config)
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

    def _normalize_selector_config(
        self,
        config: Dict[str, Any],
        url: str,
        semantic_snapshot: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not isinstance(config, dict):
            return {"success": False, "error": "invalid selector config"}

        snapshot = semantic_snapshot or {}
        config.setdefault("page_type", str(snapshot.get("page_type", "") or "unknown"))
        config.setdefault("observed_page_type", str(snapshot.get("page_type", "") or "unknown"))
        config.setdefault("page_stage", str(snapshot.get("page_stage", "") or "unknown"))
        config.setdefault("observed_page_stage", str(snapshot.get("page_stage", "") or "unknown"))

        host = str(urlparse(url).netloc or "").lower()
        item_selector = str(config.get("item_selector", "") or "").strip()
        if host.endswith("news.ycombinator.com") and item_selector in {"tr", "table tr"}:
            config["item_selector"] = "tr.athing"
            fields = dict(config.get("fields", {}) or {})
            fields["title"] = fields.get("title") or ".titleline > a"
            fields["id"] = fields.get("id") or "@id"
            config["fields"] = fields
        return config

    async def _resolve_field_target(self, item, selector: str):
        normalized = str(selector or "").strip()
        if not normalized:
            return None, ""
        if normalized in {".", ":self", "self", "text()", "::text"}:
            return item, "text"
        if normalized.startswith("@"):
            return item, f"attr:{normalized[1:].strip()}"
        if normalized.endswith("/text()"):
            base_selector = normalized[:-7].strip()
            target = item if base_selector in {"", ".", ":self", "self"} else await item.query_selector(base_selector)
            return target, "text"
        if "/@" in normalized:
            base_selector, attr_name = normalized.rsplit("/@", 1)
            base_selector = base_selector.strip()
            target = item if base_selector in {"", ".", ":self", "self"} else await item.query_selector(base_selector)
            return target, f"attr:{attr_name.strip()}"
        if ":contains(" in normalized:
            match = re.match(r"^(.*?):contains\((['\"]?)(.*?)\2\)$", normalized)
            if match:
                base_selector = (match.group(1) or "").strip() or "*"
                needle = (match.group(3) or "").strip()
                if needle:
                    try:
                        candidates = await item.query_selector_all(base_selector)
                    except Exception:
                        candidates = []
                    for candidate in candidates:
                        try:
                            text = (await candidate.inner_text()).strip()
                        except Exception:
                            text = ""
                        if text and needle in text:
                            return candidate, "element"
        return await item.query_selector(normalized), "element"

    async def _extract_field_value(self, item, field_name: str, selector: str, current_url: str = "") -> Dict[str, Any]:
        target, mode = await self._resolve_field_target(item, selector)
        if not target or not mode:
            return {}

        if mode == "text":
            text = (await target.inner_text()).strip()
            return {field_name: text} if text else {}

        if mode.startswith("attr:"):
            attr_name = mode.split(":", 1)[1]
            if not attr_name:
                return {}
            value = await target.get_attribute(attr_name)
            value = str(value or "").strip()
            if not value:
                return {}
            if current_url and attr_name.lower() in {"href", "src"}:
                value = urljoin(current_url, value)
            return {field_name: value}

        tag = await target.evaluate("el => el.tagName.toLowerCase()")
        text = (await target.inner_text()).strip()
        if tag == "a":
            data = {field_name: text} if text else {}
            href = await target.get_attribute("href")
            if href:
                if current_url:
                    href = urljoin(current_url, href)
                if field_name == "title":
                    data["link"] = href
                else:
                    data[f"{field_name}_link"] = href
            return data
        return {field_name: text} if text else {}

    async def extract_data_with_selectors(self, tk: BrowserToolkit, config: Dict[str, Any], limit: int = 10) -> List[Dict[str, Any]]:
        results = []
        item_selector = config.get("item_selector", "")
        fields = config.get("fields", {})
        if not item_selector:
            log_warning("未找到有效的项目选择器")
            web_debug_recorder.record_event(
                "selector_extraction_skipped",
                reason="missing_item_selector",
                config=config,
            )
            return results

        log_agent_action(self.name, "提取数据", f"选择器: {item_selector}")

        if config.get("need_click_first") and config.get("click_selector"):
            r = await tk.click(config["click_selector"])
            if r.success:
                await tk.human_delay(1000, 2000)

        try:
            items_r = await tk.query_all(item_selector)
            items = items_r.data if items_r.success else []
            current_url = ""
            try:
                current_url = str((await tk.get_current_url()).data or "").strip()
            except Exception:
                current_url = ""
            log_agent_action(self.name, f"找到 {len(items)} 个元素")
            web_debug_recorder.record_event(
                "selector_extraction_start",
                item_selector=item_selector,
                field_selectors=fields,
                matched_items=len(items),
                limit=limit,
            )

            for i, item in enumerate(items[:limit]):
                data = {"index": i + 1}
                for field_name, selector in fields.items():
                    if not selector:
                        continue
                    try:
                        data.update(await self._extract_field_value(item, field_name, selector, current_url=current_url))
                    except Exception as e:
                        logger.debug(f"提取字段 {field_name} 失败: {e}")

                if len([v for k, v in data.items() if k != "index" and v]) > 0:
                    results.append(data)
        except Exception as e:
            log_error(f"数据提取失败: {e}")
            web_debug_recorder.record_event(
                "selector_extraction_failed",
                item_selector=item_selector,
                error=str(e),
            )
        web_debug_recorder.write_json(
            "selector_extraction_results",
            {
                "item_selector": item_selector,
                "fields": fields,
                "count": len(results),
                "results": results,
            },
        )
        return results

    async def extract_detail_text_blocks(self, tk: BrowserToolkit, task_description: str, limit: int = 10) -> List[Dict[str, Any]]:
        html_r = await tk.get_page_html()
        html = str(html_r.data or "") if html_r.success else ""
        if not html:
            return []

        page_title = ""
        try:
            title_r = await tk.get_title()
            page_title = str(title_r.data or "") if getattr(title_r, "success", False) else ""
        except Exception:
            page_title = ""

        candidates: List[Dict[str, Any]] = []
        page_text = self._strip_tags(self._clean_html_text(html))
        if len(page_text) >= 80:
            entry = {"summary": page_text[:4000], "text": page_text[:4000]}
            if page_title:
                entry["title"] = page_title[:200]
            candidates.append(entry)

        for block in self._extract_static_text_blocks(
            html,
            limit=max(limit * 6, 24),
            task_description=task_description,
        ):
            text_value = str(block.get("text", "") or "").strip()
            if len(text_value) < 40:
                continue
            entry = {"summary": text_value[:1600], "text": text_value[:1600]}
            if page_title:
                entry["title"] = page_title[:200]
            candidates.append(entry)

        if not candidates:
            return []

        return normalize_web_results(
            candidates,
            task_description,
            limit=limit,
            understanding={"page_type": "detail"},
        )

    async def extract_weather_text_blocks(self, tk: BrowserToolkit, task_description: str, limit: int = 10) -> List[Dict[str, Any]]:
        html_r = await tk.get_page_html()
        html = str(html_r.data or "") if html_r.success else ""
        if not html:
            return []
        blocks = self._extract_static_text_blocks(
            html,
            limit=max(limit * 6, 24),
            task_description=task_description,
        )
        page_text = self._strip_tags(self._clean_html_text(html))
        if page_text and self._looks_like_weather_text(page_text):
            blocks = [{"text": page_text[:4000]}] + blocks
        if not blocks:
            return []
        return normalize_web_results(
            blocks,
            task_description,
            limit=limit,
            understanding={"page_type": "detail"},
        )

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
        trace = web_debug_recorder.start_trace(
            "web_worker",
            {
                "task": task_description,
                "input_url": url or "",
                "limit": limit,
                "query": query or "",
                "headless": headless,
            },
        )
        token = web_debug_recorder.activate_trace(trace)
        tk: Optional[BrowserToolkit] = None
        close_tk = False
        opened_from_search_page = False
        opened_search_candidate_method = ""
        active_search_candidate_link = ""
        if trace:
            log_agent_action(self.name, "网页调试记录已开启", str(trace.root_dir))

        try:
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

            explicit_input_url = bool(str(url or "").strip())
            explicit_task_urls = self._extract_direct_urls(task_description)
            if explicit_input_url:
                url_info = {
                    "url": str(url or "").strip(),
                    "backup_urls": self._backup_urls_for_explicit_url(str(url or "").strip(), task_description),
                    "need_search": False,
                    "search_query": "",
                }
            else:
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
            search_candidate_urls: List[str] = []
            search_candidate_entries: List[Dict[str, Any]] = []

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

            should_search_first = bool(url_info.get("need_search") or not candidate_urls)
            if (explicit_input_url or explicit_task_urls) and candidate_urls:
                should_search_first = False
            if (
                not should_search_first
                and not explicit_input_url
                and not explicit_task_urls
                and any(not self._is_probably_detail_url(item) for item in candidate_urls[:1])
            ):
                should_search_first = True
            web_debug_recorder.write_json(
                "url_resolution",
                {
                    "task": task_description,
                    "input_url": url or "",
                    "explicit_input_url": explicit_input_url,
                    "explicit_task_urls": explicit_task_urls,
                    "url_info": url_info,
                    "candidate_urls": candidate_urls,
                    "should_search_first": should_search_first,
                    "domain_hints": domain_hints,
                    "replan_tried_urls": sorted(tried_urls_set),
                },
            )
            if should_search_first:
                if tk is None:
                    search_headless = headless if headless is not None else False
                    tk = self._create_toolkit(headless=search_headless)
                    await tk.create_page()
                    close_tk = True
                search_bundle = await self.gather_search_candidates(
                    task_description,
                    base_query=search_base_query or task_description,
                    domain_hints=domain_hints,
                    max_results=max(3, min(limit, 6)),
                    headless=headless if headless is not None else False,
                    tk=tk,
                )
                web_debug_recorder.write_json("search_bundle", search_bundle)
                for card in search_bundle.get("cards", []):
                    if not isinstance(card, dict):
                        continue
                    link_value = str(card.get("link", "") or card.get("url", "") or "").strip()
                    if not link_value:
                        continue
                    normalized_card = dict(card)
                    normalized_card["link"] = link_value
                    normalized_card["url"] = link_value
                    search_candidate_entries.append(normalized_card)
                for found_url in search_bundle.get("urls", []):
                    found_value = str(found_url or "").strip()
                    if found_value and found_value not in search_candidate_urls:
                        search_candidate_urls.append(found_value)
                    _append_candidate(found_url)
                if not search_candidate_entries:
                    for found_value in search_candidate_urls:
                        search_candidate_entries.append({"link": found_value, "url": found_value})
                if (
                    search_bundle.get("serp_sufficient")
                    and search_bundle.get("cards")
                    and self._task_allows_serp_answer(task_description, limit)
                ):
                    cards = search_bundle["cards"][:limit]
                    return {
                        "success": True,
                        "data": cards,
                        "count": len(cards),
                        "source": "search_results",
                        "mode": "search_results",
                        "queries": search_bundle.get("queries", []),
                    }
                search_urls = list(search_candidate_urls)
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
                    web_debug_recorder.write_json(
                        "static_fetch_result",
                        {
                            "url": static_url,
                            "result": static_result,
                        },
                    )
                    if static_result.get("success"):
                        # 🔥 静态抓取也需要进行数据质量验证
                        static_data = static_result.get("data", [])
                        if static_data:
                            quality = self.validate_data_quality(static_data, task_description, limit)
                            web_debug_recorder.write_json(
                                "static_fetch_quality",
                                {
                                    "url": static_url,
                                    "quality": quality,
                                    "sample": static_data[: min(3, len(static_data))],
                                },
                            )
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
            if tk is None:
                tk = self._create_toolkit(headless=effective_headless)
                await tk.create_page()
                close_tk = True
            if should_search_first and url:
                initial_candidate = next(
                    (
                        candidate
                        for candidate in search_candidate_entries
                        if self._urls_match_for_navigation(
                            str(candidate.get("link", "") or candidate.get("url", "") or ""),
                            url,
                        )
                    ),
                    {"link": url, "url": url},
                )
                open_result = await self._open_search_candidate_from_search_page(tk, initial_candidate)
                opened_from_search_page = bool(open_result.get("opened"))
                opened_search_candidate_method = str(open_result.get("method", "") or "")
                active_search_candidate_link = str(
                    initial_candidate.get("link", "") or initial_candidate.get("url", "") or ""
                ).strip()
                if open_result.get("target_url"):
                    url = str(open_result.get("target_url", "") or url).strip() or url

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

            async def _try_remaining_search_candidates(
                remaining_candidates: List[Dict[str, Any]],
            ) -> Tuple[List[Dict[str, Any]], Dict[str, Any], str]:
                nonlocal opened_search_candidate_method, active_search_candidate_link
                total_candidates = len(remaining_candidates)
                for idx, candidate in enumerate(remaining_candidates, 1):
                    restored = await self._return_to_search_results_tab(
                        tk,
                        open_method=opened_search_candidate_method,
                    )
                    if not restored:
                        break

                    open_result = await self._open_search_candidate_from_search_page(tk, candidate)
                    if not open_result.get("opened"):
                        continue
                    opened_search_candidate_method = str(open_result.get("method", "") or "")
                    active_search_candidate_link = str(
                        candidate.get("link", "") or candidate.get("url", "") or ""
                    ).strip()
                    next_url = str(
                        open_result.get("target_url", "")
                        or candidate.get("link", "")
                        or candidate.get("url", "")
                        or ""
                    ).strip()

                    if tk.page:
                        try:
                            tk.page.on("response", _capture_api_response)
                        except Exception:
                            pass

                    api_responses.clear()
                    log_agent_action(self.name, f"搜索候选重试 ({idx}/{total_candidates})", next_url[:80])

                    try:
                        await tk.human_delay(180, 3000)
                        if self.fast_mode:
                            await tk.wait_for_load("domcontentloaded", timeout=3000)
                        else:
                            await tk.wait_for_load("networkidle", timeout=15000)

                        await tk.wait_for_selector(
                            "table, .list, ul li, [class*='list'], [class*='item'], .el-table",
                            timeout=4000 if self.fast_mode else 10000,
                        )

                        captcha_r = await tk.detect_captcha()
                        if captcha_r.success and captcha_r.data and captcha_r.data.get("has_captcha"):
                            solve_r = await tk.solve_captcha(max_retries=5)
                            if not solve_r.success:
                                log_warning(f"搜索候选重试验证码处理失败: {next_url[:80]}")
                                continue
                            await tk.human_delay(250, 3000)
                            await tk.wait_for_load("domcontentloaded", timeout=10000)
                            await tk.wait_for_load("networkidle", timeout=10000)

                        for _ in range(random.randint(1, 2) if self.fast_mode else random.randint(2, 3)):
                            await tk.scroll_down(random.randint(200, 500))
                            await tk.human_delay(120, 800)

                        retry_snapshot_r = await tk.semantic_snapshot(max_elements=80, include_cards=True)
                        retry_snapshot = (
                            retry_snapshot_r.data
                            if retry_snapshot_r.success and isinstance(retry_snapshot_r.data, dict)
                            else {}
                        )
                        semantic_serp = await self._maybe_handle_semantic_search_results(
                            tk,
                            task_description,
                            limit,
                            snapshot=retry_snapshot,
                        )
                        if semantic_serp.get("return_data"):
                            cards = list(semantic_serp.get("data", []) or [])[:limit]
                            web_debug_recorder.record_event(
                                "search_candidate_retry_success",
                                url=next_url,
                                index=idx,
                                mode=str(semantic_serp.get("mode", "semantic_search_results")),
                            )
                            return cards, {"page_type": "serp"}, next_url
                        if semantic_serp.get("navigated"):
                            retry_snapshot_r = await tk.semantic_snapshot(max_elements=80, include_cards=True)
                            retry_snapshot = (
                                retry_snapshot_r.data
                                if retry_snapshot_r.success and isinstance(retry_snapshot_r.data, dict)
                                else {}
                            )

                        retry_config = await self.analyze_page_structure(tk, task_description)
                        retry_data: List[Dict[str, Any]] = []
                        if retry_config.get("item_selector"):
                            retry_data = await self.extract_data_with_selectors(tk, retry_config, limit)

                        observed_page_type = str(
                            retry_config.get("page_type", "")
                            or retry_config.get("observed_page_type", "")
                            or retry_snapshot.get("page_type", "")
                            or ""
                        ).strip().lower()

                        if not retry_data and self._should_try_detail_text_fallback(task_description, observed_page_type):
                            retry_data = await self.extract_detail_text_blocks(tk, task_description, limit=limit)

                        if not retry_data and self._task_mentions_weather(task_description):
                            retry_data = await self.extract_weather_text_blocks(tk, task_description, limit=limit)

                        if not retry_data and api_responses:
                            best_api = max(api_responses, key=lambda item: len(item["data"]))
                            retry_data = best_api["data"][:limit]

                        if not retry_data:
                            retry_data = await self.extract_news_links_fallback(tk, task_description, limit=limit)

                        if not retry_data:
                            retry_data = await self.extract_table_links_fallback(tk, task_description, limit=limit)

                        if retry_data:
                            retry_data = normalize_web_results(
                                retry_data,
                                task_description,
                                limit=limit,
                                understanding={
                                    "page_type": str(
                                        retry_config.get("page_type", "")
                                        or retry_config.get("observed_page_type", "")
                                        or observed_page_type
                                        or ""
                                    )
                                },
                            )
                            retry_quality = self.validate_data_quality(retry_data, task_description, limit)
                            if retry_quality.get("valid"):
                                web_debug_recorder.record_event(
                                    "search_candidate_retry_success",
                                    url=next_url,
                                    index=idx,
                                    count=len(retry_data),
                                )
                                return retry_data, retry_config, next_url

                        web_debug_recorder.record_event(
                            "search_candidate_retry_failed",
                            url=next_url,
                            index=idx,
                        )
                    except Exception as retry_err:
                        log_warning(f"搜索候选重试失败: {str(retry_err)[:80]}")
                        web_debug_recorder.record_event(
                            "search_candidate_retry_exception",
                            url=next_url,
                            index=idx,
                            error=str(retry_err),
                        )
                        continue

                return [], {}, ""

            if tk.page:
                tk.page.on("response", _capture_api_response)

            config = {}
            try:
                # Step 2: 访问页面（带重试 + 反爬自适应）
                log_agent_action(self.name, "访问页面", url)
                web_debug_recorder.record_event("browser_navigation_start", url=url, headless=effective_headless)
                _goto_attempt = 0

                async def _rebuild_page_and_goto():
                    nonlocal _goto_attempt
                    _goto_attempt += 1
                    if tk.page and tk.page.is_closed():
                        await tk.create_page()
                        if tk.page:
                            tk.page.on("response", _capture_api_response)
                    if opened_from_search_page and _goto_attempt == 1:
                        current_url_r = await tk.get_current_url()
                        current_open_url = str(current_url_r.data or "") if current_url_r.success else ""
                        if self._urls_match_for_navigation(current_open_url, url):
                            web_debug_recorder.record_event(
                                "browser_navigation_reused_search_tab",
                                current_url=current_open_url,
                                target_url=url,
                            )
                            return ToolkitResult(success=True, data=current_open_url)
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
                web_debug_recorder.write_json(
                    "captcha_detection",
                    {
                        "success": captcha_r.success,
                        "data": captcha_r.data,
                        "error": getattr(captcha_r, "error", ""),
                    },
                )
                if captcha_r.success and captcha_r.data and captcha_r.data.get("has_captcha"):
                    log_agent_action(self.name, "检测到验证码，尝试自动处理")
                    solve_r = await tk.solve_captcha(max_retries=5)
                    web_debug_recorder.write_json(
                        "captcha_solution",
                        {
                            "success": solve_r.success,
                            "data": solve_r.data,
                            "error": getattr(solve_r, "error", ""),
                        },
                    )
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

                    snapshot_r = await tk.semantic_snapshot(max_elements=80, include_cards=True)
                    snapshot = snapshot_r.data if snapshot_r.success and isinstance(snapshot_r.data, dict) else {}
                    semantic_serp = await self._maybe_handle_semantic_search_results(
                        tk,
                        task_description,
                        limit,
                        snapshot=snapshot,
                    )
                    if semantic_serp.get("return_data"):
                        cards = list(semantic_serp.get("data", []) or [])[:limit]
                        return {
                            "success": True,
                            "data": cards,
                            "count": len(cards),
                            "source": url,
                            "mode": str(semantic_serp.get("mode", "semantic_search_results")),
                        }
                    if semantic_serp.get("navigated"):
                        snapshot_r = await tk.semantic_snapshot(max_elements=80, include_cards=True)
                        snapshot = snapshot_r.data if snapshot_r.success and isinstance(snapshot_r.data, dict) else {}

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

                    observed_page_type = str(
                        config.get("page_type", "")
                        or config.get("observed_page_type", "")
                        or snapshot.get("page_type", "")
                        or ""
                    ).strip().lower()

                    if not data and self._should_try_detail_text_fallback(task_description, observed_page_type):
                        detail_blocks = await self.extract_detail_text_blocks(tk, task_description, limit=limit)
                        if detail_blocks:
                            data = detail_blocks
                            log_success(f"详情文本块提取成功，获取 {len(data)} 条数据")

                    if not data and self._task_mentions_weather(task_description):
                        weather_blocks = await self.extract_weather_text_blocks(tk, task_description, limit=limit)
                        if weather_blocks:
                            data = weather_blocks
                            log_success(f"天气文本块提取成功，获取 {len(data)} 条数据")

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

                    if not data:
                        table_links = await self.extract_table_links_fallback(tk, task_description, limit=limit)
                        if table_links:
                            data = table_links
                            log_success(f"表格链接兜底提取成功，获取 {len(data)} 条数据")

                    if data:
                        data = normalize_web_results(
                            data,
                            task_description,
                            limit=limit,
                            understanding={"page_type": str(config.get("page_type", "") or config.get("observed_page_type", "") or "")},
                        )
                        quality = self.validate_data_quality(data, task_description, limit)
                        if quality.get("valid"):
                            log_success(f"数据质量验证通过: {quality.get('reason', '')[:50]}")
                            break
                        log_warning(f"数据不符合要求: {quality.get('reason', '')[:80]}")
                        if self._should_try_detail_text_fallback(task_description, observed_page_type):
                            detail_blocks = await self.extract_detail_text_blocks(tk, task_description, limit=limit)
                            if detail_blocks and detail_blocks != data:
                                detail_quality = self.validate_data_quality(detail_blocks, task_description, limit)
                                if detail_quality.get("valid"):
                                    data = detail_blocks
                                    log_success(f"详情文本块兜底修正成功，获取 {len(data)} 条数据")
                                    break
                        if self._task_mentions_weather(task_description):
                            weather_blocks = await self.extract_weather_text_blocks(tk, task_description, limit=limit)
                            if weather_blocks and weather_blocks != data:
                                weather_quality = self.validate_data_quality(weather_blocks, task_description, limit)
                                if weather_quality.get("valid"):
                                    data = weather_blocks
                                    log_success(f"天气文本块兜底修正成功，获取 {len(data)} 条数据")
                                    break
                        table_links = await self.extract_table_links_fallback(tk, task_description, limit=limit)
                        if table_links and table_links != data:
                            fallback_quality = self.validate_data_quality(table_links, task_description, limit)
                            if fallback_quality.get("valid"):
                                data = normalize_web_results(
                                    table_links,
                                    task_description,
                                    limit=limit,
                                    understanding={"page_type": "list"},
                                )
                                log_success(f"表格链接兜底修正成功，获取 {len(data)} 条数据")
                                break
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

                data = normalize_web_results(
                    data,
                    task_description,
                    limit=limit,
                    understanding={"page_type": str(config.get("page_type", "") or config.get("observed_page_type", "") or "")},
                )

                if data:
                    log_success(f"最终成功提取 {len(data)} 条数据")
                else:
                    if opened_from_search_page and search_candidate_entries:
                        remaining_search_candidates = [
                            candidate
                            for candidate in search_candidate_entries
                            if str(candidate.get("link", "") or candidate.get("url", "") or "").strip()
                            and str(candidate.get("link", "") or candidate.get("url", "") or "").strip() != active_search_candidate_link
                        ]
                        retry_data, retry_config, retry_url = await _try_remaining_search_candidates(
                            remaining_search_candidates[:2]
                        )
                        if retry_data:
                            data = retry_data
                            config = retry_config or config
                            url = retry_url
                            result = {
                                "success": True,
                                "data": data,
                                "count": len(data),
                                "source": url,
                                "selectors_used": config,
                            }
                            web_debug_recorder.write_json("smart_scrape_result", result)
                            return result

                    if not data and (explicit_input_url or explicit_task_urls):
                        log_warning("显式 URL 任务保持当前来源，不切换到替代来源")
                        return {
                            "success": False,
                            "error": "在指定页面上未能提取到满足要求的数据",
                            "data": [],
                            "count": 0,
                            "source": url,
                            "selectors_used": config,
                        }
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
                                log_warning(f"替代来源 {idx} 数据质量不符合要求")
                                data = []
                            else:
                                log_warning(f"替代来源 {idx} 未能提取到数据")
                        except Exception as alt_err:
                            log_warning(f"替代来源 {idx} 访问失败: {str(alt_err)[:80]}")
                            continue
                    if not data:
                        log_warning("所有来源（包括 2 个替代来源）均未能提取到数据")

                result = {
                    "success": len(data) > 0, "data": data, "count": len(data),
                    "source": url, "selectors_used": config,
                }
                web_debug_recorder.write_json("smart_scrape_result", result)
                return result
            except Exception as e:
                log_error(f"爬取失败: {e}")
                web_debug_recorder.record_event("smart_scrape_exception", error=str(e), url=url)
                return {"success": False, "error": str(e), "data": [], "url": url}
            finally:
                if close_tk and tk is not None:
                    await tk.close()
                    close_tk = False
        finally:
            if close_tk and tk is not None:
                await tk.close()
            web_debug_recorder.deactivate_trace(token)

    async def _try_next_page_via_toolkit(self, tk: BrowserToolkit) -> bool:
        """尝试点击分页控件翻到下一页"""
        try:
            snapshot_r = await tk.semantic_snapshot(max_elements=80, include_cards=True)
            snapshot = snapshot_r.data if snapshot_r.success and isinstance(snapshot_r.data, dict) else {}
            affordances = snapshot.get("affordances", {}) or {}
            load_more_ref = str(affordances.get("load_more_ref", "") or "")
            next_page_ref = str(affordances.get("next_page_ref", "") or "")

            for ref, label in [(load_more_ref, "加载更多"), (next_page_ref, "翻到下一页")]:
                if not ref:
                    continue
                clicked = await tk.click_ref(ref)
                if clicked.success:
                    log_agent_action(self.name, label, ref)
                    await tk.human_delay(500, 3000)
                    await tk.wait_for_load("domcontentloaded", timeout=8000)
                    return True
        except Exception:
            pass

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
