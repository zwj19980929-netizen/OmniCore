"""
增强版 Web Worker - 集成三层感知架构
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

from core.llm import LLMClient
from utils.browser_toolkit import BrowserToolkit
from utils.page_perceiver import get_page_understanding
from utils.logger import log_agent_action, log_success, log_warning, log_error
from utils.web_result_normalizer import normalize_search_cards, normalize_web_results


_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "page_perception.txt"
try:
    _PROMPTS = _PROMPT_PATH.read_text(encoding="utf-8-sig")
except Exception:
    _PROMPTS = ""


def _extract_prompt(name: str) -> str:
    pattern = f"# {name}\n(.*?)(?=\n# |$)"
    match = re.search(pattern, _PROMPTS, re.DOTALL)
    return match.group(1).strip() if match else ""


PAGE_UNDERSTANDING_PROMPT = _extract_prompt("第一阶段：页面理解")
SELECTOR_GENERATION_PROMPT = _extract_prompt("第二阶段：选择器生成")
ACTION_PLANNING_PROMPT = _extract_prompt("第三阶段：操作规划")


FIELD_ALIASES: Dict[str, Set[str]] = {
    "title": {"title", "headline", "name", "subject", "topic", "question", "story", "article", "repo", "repository", "project", "标题", "名称", "主题", "仓库", "项目"},
    "url": {"url", "link", "href", "website", "address", "canonical_url", "target_url", "detail_url", "链接", "网址", "地址"},
    "summary": {"summary", "description", "snippet", "excerpt", "abstract", "content", "body", "details", "desc", "overview", "摘要", "描述", "简介", "正文", "内容", "概述"},
    "date": {"date", "time", "published", "publish_date", "created", "updated", "timestamp", "datetime", "日期", "时间", "发布时间", "更新时间"},
    "author": {"author", "owner", "creator", "publisher", "by", "作者", "发布者", "所有者"},
    "source": {"source", "site", "host", "domain", "来源", "站点", "网站", "域名"},
    "location": {"location", "city", "country", "place", "region", "地点", "城市", "国家", "地区"},
    "temperature": {"temperature", "temp", "degree", "温度", "气温"},
    "weather": {"weather", "forecast", "condition", "天气", "天气状况", "预报"},
    "humidity": {"humidity", "湿度"},
    "wind": {"wind", "wind_speed", "wind_force", "风力", "风速"},
    "aqi": {"aqi", "air_quality", "空气质量"},
    "price": {"price", "cost", "amount", "价格", "金额", "售价"},
    "rating": {"rating", "score", "stars", "评分", "分数", "星级"},
    "comments": {"comments", "comment_count", "评论", "评论数"},
    "points": {"points", "likes", "stars", "积分", "点赞", "喜欢"},
}

STRICT_NOISE_TEXTS = {
    "home",
    "homepage",
    "首页",
    "login",
    "log in",
    "sign in",
    "register",
    "sign up",
    "menu",
    "导航",
    "next",
    "previous",
    "prev",
    "下一页",
    "上一页",
    "privacy",
    "terms",
    "contact",
    "about",
    "search",
    "搜索",
    "more",
    "更多",
}

NOISE_URL_HINTS = (
    "javascript:",
    "/login",
    "/signin",
    "/sign-in",
    "/signup",
    "/sign-up",
    "/register",
    "/privacy",
    "/terms",
    "/contact",
    "/about",
    "/settings",
    "/preferences",
)

CONTENT_FIELDS = {
    "title",
    "summary",
    "date",
    "author",
    "source",
    "location",
    "temperature",
    "weather",
    "humidity",
    "wind",
    "aqi",
    "price",
    "rating",
    "comments",
    "points",
}


class EnhancedWebWorker:
    """
    增强版 Web Worker - 三层感知架构

    Layer 1: 页面理解 - 理解页面是什么、有什么功能
    Layer 2: 选择器生成 - 基于理解生成精确的提取策略
    Layer 3: 执行提取 - 结合语义后处理输出可用结构化结果
    """

    def __init__(self, llm_client: Optional[LLMClient] = None):
        self.name = "EnhancedWebWorker"
        self.llm = llm_client or LLMClient()
        self._understanding_cache: Dict[str, Dict[str, Any]] = {}

    async def smart_extract(
        self,
        toolkit: BrowserToolkit,
        task_description: str,
        limit: int = 10,
    ) -> Dict[str, Any]:
        log_agent_action(self.name, "开始三层感知分析", task_description[:80])

        understanding = await self._understand_page(toolkit, task_description)
        if not understanding.get("success"):
            return {"success": False, "error": "页面理解失败", "data": []}

        log_success(
            f"页面理解完成: {understanding.get('page_type', 'unknown')} - "
            f"{understanding.get('main_function', '')}"
        )

        semantic_result = self._extract_from_semantic_snapshot(
            understanding.get("semantic_snapshot", {}) or {},
            task_description,
            understanding,
            limit,
        )
        if semantic_result is not None:
            log_success(f"语义快照直接提取 {semantic_result.get('count', 0)} 条数据")
            return semantic_result

        serp_dom_result = await self._extract_from_serp_dom(toolkit, task_description, understanding, limit)
        if serp_dom_result is not None:
            log_success(f"SERP DOM 直接提取 {serp_dom_result.get('count', 0)} 条数据")
            return serp_dom_result

        selector_config = await self._generate_selectors(toolkit, task_description, understanding)
        if not selector_config.get("success"):
            return {"success": False, "error": "选择器生成失败", "data": []}

        log_success(f"选择器生成完成: {selector_config.get('item_selector')}")

        result = await self._execute_extraction(toolkit, selector_config, limit)
        if not result.get("success"):
            return result

        raw_data = list(result.get("data", []) or [])
        cleaned = self._post_process_results(raw_data, task_description, understanding, limit)
        if cleaned:
            result["data"] = cleaned
            result["count"] = len(cleaned)
            result["raw_count"] = len(raw_data)
        elif raw_data:
            result["raw_count"] = len(raw_data)
        return result

    def _normalize_text(self, value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    def _tokenize(self, value: str) -> List[str]:
        return [
            token
            for token in re.findall(r"[\u4e00-\u9fff]{1,}|[A-Za-z0-9][A-Za-z0-9_+.-]{1,}", self._normalize_text(value).lower())
            if token
        ]

    def _canonical_field_name(self, field_name: str) -> str:
        normalized = self._normalize_text(field_name).lower().replace(" ", "_")
        if not normalized:
            return ""
        for canonical, aliases in FIELD_ALIASES.items():
            if normalized == canonical or normalized in aliases:
                return canonical
            if normalized.endswith("_url") and canonical == "url":
                return canonical
        return normalized

    def _serialize_understanding(self, understanding: Dict[str, Any]) -> str:
        payload = {
            key: value
            for key, value in (understanding or {}).items()
            if key not in {"success", "page_structure", "semantic_snapshot", "semantic_snapshot_text"}
        }
        try:
            return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        except Exception:
            return str(payload)

    def _format_semantic_snapshot_for_llm(self, snapshot: Dict[str, Any]) -> str:
        if not isinstance(snapshot, dict) or not snapshot:
            return "(无语义快照)"

        lines = [
            f"页面类型: {self._normalize_text(snapshot.get('page_type', 'unknown')) or 'unknown'}",
            f"URL: {self._normalize_text(snapshot.get('url', ''))}",
            f"标题: {self._normalize_text(snapshot.get('title', ''))}",
        ]

        affordances = snapshot.get("affordances", {}) or {}
        if affordances:
            affordance_text = ", ".join(f"{key}={value}" for key, value in affordances.items())
            lines.append(f"页面特征: {affordance_text}")

        collections = snapshot.get("collections", []) or []
        if collections:
            lines.append("页面集合结构:")
            for idx, collection in enumerate(collections[:4], 1):
                if not isinstance(collection, dict):
                    continue
                samples = " | ".join(
                    self._normalize_text(item)[:100]
                    for item in (collection.get("sample_items", []) or [])[:3]
                    if self._normalize_text(item)
                )
                lines.append(
                    f"{idx}. kind={self._normalize_text(collection.get('kind', 'unknown'))} "
                    f"count={int(collection.get('item_count', 0) or 0)} "
                    f"ref={self._normalize_text(collection.get('ref', ''))} "
                    f"samples={samples or '(none)'}"
                )

        controls = snapshot.get("controls", []) or []
        if controls:
            lines.append("关键控件:")
            for idx, control in enumerate(controls[:6], 1):
                if not isinstance(control, dict):
                    continue
                lines.append(
                    f"{idx}. kind={self._normalize_text(control.get('kind', ''))} "
                    f"ref={self._normalize_text(control.get('ref', ''))} "
                    f"text={self._normalize_text(control.get('text', ''))[:80]}"
                )

        cards = snapshot.get("cards", []) or []
        if cards:
            lines.append("搜索结果卡片:")
            for idx, card in enumerate(cards[:8], 1):
                if not isinstance(card, dict):
                    continue
                payload = " | ".join(
                    part for part in [
                        self._normalize_text(card.get("title", ""))[:100],
                        self._normalize_text(card.get("source", ""))[:48],
                        self._normalize_text(card.get("date", ""))[:40],
                        self._normalize_text(card.get("snippet", ""))[:140],
                        self._normalize_text(card.get("link", ""))[:120],
                    ] if part
                )
                lines.append(f"{idx}. {payload}")

        elements = snapshot.get("elements", []) or []
        if elements:
            lines.append("可见关键元素:")
            for idx, element in enumerate(elements[:12], 1):
                if not isinstance(element, dict):
                    continue
                parts = [
                    self._normalize_text(element.get("type", element.get("role", "")))[:24],
                    self._normalize_text(element.get("text", ""))[:80],
                    self._normalize_text(element.get("label", ""))[:60],
                    self._normalize_text(element.get("ref", "")),
                ]
                lines.append(f"{idx}. " + " | ".join(part for part in parts if part))
        return "\n".join(lines)

    async def _understand_page(
        self,
        toolkit: BrowserToolkit,
        task_description: str,
    ) -> Dict[str, Any]:
        log_agent_action(self.name, "Layer 1: 页面理解")

        url_r = await toolkit.get_current_url()
        current_url = self._normalize_text(url_r.data)
        cache_key = f"{current_url}:{task_description}"
        if cache_key in self._understanding_cache:
            log_agent_action(self.name, "命中页面理解缓存")
            return self._understanding_cache[cache_key]

        page_structure = await get_page_understanding(toolkit, task_description)
        semantic_snapshot: Dict[str, Any] = {}
        semantic_snapshot_text = "(无语义快照)"
        try:
            snapshot_r = await toolkit.semantic_snapshot(max_elements=80, include_cards=True)
            if snapshot_r.success and isinstance(snapshot_r.data, dict):
                semantic_snapshot = snapshot_r.data
                semantic_snapshot_text = self._format_semantic_snapshot_for_llm(semantic_snapshot)
        except Exception as exc:
            log_warning(f"获取语义快照失败: {exc}")

        prompt = PAGE_UNDERSTANDING_PROMPT.format(
            task_description=task_description,
            page_structure=page_structure,
            semantic_snapshot=semantic_snapshot_text,
        )

        response = self.llm.chat_with_system(
            system_prompt=prompt,
            user_message="请分析这个页面并返回 JSON 格式的理解结果",
            temperature=0.2,
            json_mode=True,
        )

        try:
            understanding = self.llm.parse_json_response(response)
            understanding["success"] = True
            understanding["page_structure"] = page_structure
            understanding["semantic_snapshot"] = semantic_snapshot
            understanding["semantic_snapshot_text"] = semantic_snapshot_text
            self._understanding_cache[cache_key] = understanding
            return understanding
        except Exception as exc:
            log_error(f"页面理解解析失败: {exc}")
            return {"success": False, "error": str(exc)}

    async def _generate_selectors(
        self,
        toolkit: BrowserToolkit,
        task_description: str,
        understanding: Dict[str, Any],
    ) -> Dict[str, Any]:
        del toolkit
        log_agent_action(self.name, "Layer 2: 选择器生成")

        prompt = SELECTOR_GENERATION_PROMPT.format(
            task_description=task_description,
            page_understanding=self._serialize_understanding(understanding),
            page_structure=understanding.get("page_structure", ""),
            semantic_snapshot=understanding.get("semantic_snapshot_text", "(无语义快照)"),
        )

        response = self.llm.chat_with_system(
            system_prompt=prompt,
            user_message="请生成数据提取选择器配置",
            temperature=0.1,
            json_mode=True,
        )

        try:
            config = self.llm.parse_json_response(response)
            return config
        except Exception as exc:
            log_error(f"选择器生成解析失败: {exc}")
            return {"success": False, "error": str(exc)}

    async def _resolve_field_target(self, item: Any, selector: str) -> Tuple[Any, str]:
        normalized = self._normalize_text(selector)
        if not normalized:
            return None, ""
        if normalized in {".", ":self", "self", "text()", "::text"}:
            return item, "text"
        if normalized.startswith("@"):
            return item, f"attr:{normalized[1:].strip()}"
        attr_pseudo = re.match(r"^(.*)::attr\((['\"]?)([A-Za-z0-9:_-]+)\2\)$", normalized)
        if attr_pseudo:
            base_selector = (attr_pseudo.group(1) or "").strip()
            attr_name = (attr_pseudo.group(3) or "").strip()
            target = item if base_selector in {"", ".", ":self", "self"} else await item.query_selector(base_selector)
            return target, f"attr:{attr_name}"
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
                            text = self._normalize_text(await candidate.inner_text())
                        except Exception:
                            text = ""
                        if text and needle in text:
                            return candidate, "element"
        return await item.query_selector(normalized), "element"

    async def _extract_field_value(
        self,
        item: Any,
        field_name: str,
        selector: str,
        current_url: str,
    ) -> Dict[str, Any]:
        target, mode = await self._resolve_field_target(item, selector)
        if not target or not mode:
            return {}

        if mode == "text":
            text = await self._extract_target_text(target)
            return {field_name: text} if text else {}

        if mode.startswith("attr:"):
            attr_name = mode.split(":", 1)[1]
            if not attr_name:
                return {}
            value = self._normalize_text(await target.get_attribute(attr_name))
            if not value:
                return {}
            if attr_name.lower() in {"href", "src"}:
                value = urljoin(current_url, value)
            return {field_name: value}

        text = await self._extract_target_text(target)
        result: Dict[str, Any] = {field_name: text} if text else {}
        try:
            tag = self._normalize_text(await target.evaluate("el => el.tagName.toLowerCase()"))
        except Exception:
            tag = ""
        if tag == "a":
            href = self._normalize_text(await target.get_attribute("href"))
            if href:
                result[f"{field_name}_url"] = urljoin(current_url, href)
        return result

    async def _extract_target_text(self, target: Any) -> str:
        text = ""
        try:
            text = self._normalize_text(await target.inner_text())
        except Exception:
            text = ""
        if text:
            return text
        text_content = getattr(target, "text_content", None)
        if callable(text_content):
            try:
                text = self._normalize_text(await text_content())
            except Exception:
                text = ""
        if text:
            return text
        for attr_name in ("title", "aria-label", "ariaLabel", "alt", "value"):
            try:
                value = self._normalize_text(await target.get_attribute(attr_name))
            except Exception:
                value = ""
            if value:
                return value
        return ""

    def _compute_scan_limit(self, item_count: int, limit: int) -> int:
        if item_count <= 0:
            return 0
        desired = max(limit * 4, limit + 6, 10)
        return min(item_count, desired)

    async def _execute_extraction(
        self,
        toolkit: BrowserToolkit,
        config: Dict[str, Any],
        limit: int,
    ) -> Dict[str, Any]:
        log_agent_action(self.name, "Layer 3: 执行数据提取")

        pre_actions = config.get("pre_actions", []) or []
        for action in pre_actions:
            if action.get("action") == "click":
                selector = action.get("selector")
                log_agent_action(self.name, f"执行预操作: 点击 {selector}")
                await toolkit.click(selector)
                await toolkit.human_delay(1000, 2000)

        item_selector = self._normalize_text(config.get("item_selector", ""))
        fields = dict(config.get("fields", {}) or {})
        if not item_selector:
            return {"success": False, "error": "未找到有效的 item_selector", "data": []}

        try:
            items_r = await toolkit.query_all(item_selector)
            if not items_r.success:
                return {"success": False, "error": f"查询失败: {items_r.error}", "data": []}

            items = items_r.data or []
            log_agent_action(self.name, f"找到 {len(items)} 个数据项")

            current_url = ""
            try:
                current_url = self._normalize_text((await toolkit.get_current_url()).data)
            except Exception:
                current_url = ""

            results: List[Dict[str, Any]] = []
            scan_limit = self._compute_scan_limit(len(items), limit)
            for i, item in enumerate(items[:scan_limit]):
                data: Dict[str, Any] = {"index": i + 1}
                for field_name, selector in fields.items():
                    selector_value = self._normalize_text(selector)
                    if not selector_value:
                        continue
                    try:
                        payload = await self._extract_field_value(item, field_name, selector_value, current_url)
                        if payload:
                            data.update(payload)
                    except Exception as exc:
                        log_warning(f"提取字段 {field_name} 失败: {exc}")
                if any(value for key, value in data.items() if key != "index"):
                    results.append(data)

            log_success(f"成功提取 {len(results)} 条候选数据")
            return {"success": True, "data": results, "count": len(results), "config": config}
        except Exception as exc:
            log_error(f"数据提取失败: {exc}")
            return {"success": False, "error": str(exc), "data": []}

    def _looks_like_url(self, value: str) -> bool:
        normalized = self._normalize_text(value).lower()
        return normalized.startswith(("http://", "https://")) or normalized.startswith("mailto:")

    def _best_url_from_item(self, item: Dict[str, Any]) -> str:
        candidates: List[str] = []
        for key, value in item.items():
            key_name = self._canonical_field_name(key)
            normalized = self._normalize_text(value)
            if not normalized:
                continue
            if key_name == "url" or key.endswith("_url") or self._looks_like_url(normalized):
                candidates.append(normalized)
        for candidate in candidates:
            if candidate.startswith(("http://", "https://")):
                return candidate
        return candidates[0] if candidates else ""

    def _best_title_from_item(self, item: Dict[str, Any]) -> str:
        preferred_keys = ("title", "headline", "name")
        for key in preferred_keys:
            value = self._normalize_text(item.get(key))
            if value and not self._looks_like_url(value):
                return value
        link_value = self._normalize_text(item.get("link"))
        if link_value and not self._looks_like_url(link_value):
            return link_value
        for key, value in item.items():
            if key == "index":
                continue
            normalized = self._normalize_text(value)
            if normalized and not self._looks_like_url(normalized):
                return normalized
        return ""

    def _best_semantic_value(self, item: Dict[str, Any], canonical_field: str) -> str:
        candidates: List[str] = []
        for key, value in item.items():
            if key == "index":
                continue
            normalized = self._normalize_text(value)
            if not normalized:
                continue
            field_name = self._canonical_field_name(key)
            if field_name == canonical_field:
                if canonical_field == "url" and not self._looks_like_url(normalized):
                    continue
                candidates.append(normalized)
        if not candidates:
            return ""
        if canonical_field == "summary":
            candidates.sort(key=len, reverse=True)
        return candidates[0]

    def _infer_requested_fields(
        self,
        task_description: str,
        understanding: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        requested: List[str] = []
        seen: Set[str] = set()

        def _append(field_name: str) -> None:
            canonical = self._canonical_field_name(field_name)
            if not canonical or canonical in seen:
                return
            seen.add(canonical)
            requested.append(canonical)

        for field_name in understanding.get("key_fields", []) if isinstance(understanding, dict) else []:
            _append(str(field_name))

        task_text = self._normalize_text(task_description).lower()
        for canonical, aliases in FIELD_ALIASES.items():
            if canonical == "url":
                tokens = aliases | {"url", "链接", "网址"}
            else:
                tokens = aliases | {canonical}
            if any(token.lower() in task_text for token in tokens):
                _append(canonical)

        page_type = self._normalize_text((understanding or {}).get("page_type", "")).lower()
        if not requested and page_type in {"serp", "list", "table", "列表页", "搜索结果页"}:
            _append("title")
            _append("url")
        if not requested and page_type in {"detail", "详情页"}:
            _append("title")
            _append("summary")
            _append("url")
        if not requested:
            _append("title")
            _append("url")
        return requested

    def _is_noise_item(self, item: Dict[str, Any], requested_fields: List[str]) -> bool:
        title = self._normalize_text(item.get("title"))
        url = self._normalize_text(item.get("url", item.get("link", "")))
        summary = self._normalize_text(item.get("summary"))
        if not any(self._normalize_text(item.get(field)) for field in requested_fields if field != "url") and not url:
            return True

        title_lower = title.lower()
        if title_lower in STRICT_NOISE_TEXTS and not summary:
            return True

        if url:
            parsed = urlparse(url)
            url_lower = f"{parsed.path} {parsed.query}".lower()
            if any(token in url_lower for token in NOISE_URL_HINTS) and not summary:
                return True
            if parsed.path in {"", "/"} and title_lower in STRICT_NOISE_TEXTS:
                return True
        return False

    def _score_item(
        self,
        item: Dict[str, Any],
        requested_fields: List[str],
        task_description: str,
    ) -> int:
        score = 0
        task_tokens = set(self._tokenize(task_description))
        title = self._normalize_text(item.get("title"))
        summary = self._normalize_text(item.get("summary"))
        haystack = f"{title} {summary}".lower()

        if title:
            score += 3
        if self._normalize_text(item.get("url")):
            score += 2
        for field_name in requested_fields:
            if self._normalize_text(item.get(field_name)):
                score += 2
        if task_tokens:
            score += sum(1 for token in task_tokens if token in haystack)
        if self._is_noise_item(item, requested_fields):
            score -= 5
        return score

    def _canonicalize_item(
        self,
        item: Dict[str, Any],
        requested_fields: List[str],
    ) -> Dict[str, Any]:
        normalized: Dict[str, Any] = {}
        if item.get("index") is not None:
            normalized["index"] = item.get("index")

        title = self._best_title_from_item(item)
        url = self._best_url_from_item(item)
        if title:
            normalized["title"] = title
        if url:
            normalized["url"] = url
            normalized["link"] = url

        for field_name in CONTENT_FIELDS:
            value = self._best_semantic_value(item, field_name)
            if value and value not in {title, url}:
                normalized[field_name] = value

        if not title:
            name_value = self._best_semantic_value(item, "title")
            if name_value:
                normalized["title"] = name_value

        if not normalized.get("summary"):
            for fallback_key in ("description", "snippet", "content", "details"):
                value = self._normalize_text(item.get(fallback_key))
                if value and value != normalized.get("title", ""):
                    normalized["summary"] = value
                    break

        for key, value in item.items():
            if key in normalized or key == "index":
                continue
            normalized_value = self._normalize_text(value)
            if not normalized_value:
                continue
            canonical_key = self._canonical_field_name(key)
            if canonical_key in {"title", "url"}:
                continue
            if normalized_value in {normalized.get("title", ""), normalized.get("url", ""), normalized.get("summary", "")}:
                continue
            if key.endswith("_url") and normalized.get("url"):
                continue
            normalized[key] = normalized_value

        if not normalized.get("link") and normalized.get("url"):
            normalized["link"] = normalized["url"]

        if "url" in requested_fields and normalized.get("url") and "link" not in normalized:
            normalized["link"] = normalized["url"]
        return normalized

    def _post_process_results(
        self,
        results: List[Dict[str, Any]],
        task_description: str,
        understanding: Optional[Dict[str, Any]],
        limit: int,
    ) -> List[Dict[str, Any]]:
        return normalize_web_results(
            results,
            task_description,
            limit=limit,
            understanding=understanding,
        )

    def _extract_from_semantic_snapshot(
        self,
        snapshot: Dict[str, Any],
        task_description: str,
        understanding: Optional[Dict[str, Any]],
        limit: int,
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(snapshot, dict) or not snapshot:
            return None

        cards = snapshot.get("cards", []) or []
        page_type = self._normalize_text(snapshot.get("page_type", "")).lower()
        if not cards or page_type != "serp":
            return None

        cleaned = normalize_search_cards(
            cards,
            task_description,
            limit=limit,
            understanding=understanding,
        )
        if not cleaned:
            return None

        return {
            "success": True,
            "data": cleaned,
            "count": len(cleaned),
            "config": {
                "success": True,
                "mode": "semantic_snapshot_cards",
                "page_type": page_type,
                "validation": {"expected_min_items": min(limit, len(cleaned)), "required_fields": self._infer_requested_fields(task_description, understanding or {})},
            },
        }

    def _looks_like_serp(self, understanding: Optional[Dict[str, Any]]) -> bool:
        page_type = self._normalize_text((understanding or {}).get("page_type", "")).lower()
        main_function = self._normalize_text((understanding or {}).get("main_function", "")).lower()
        snapshot_type = self._normalize_text(((understanding or {}).get("semantic_snapshot", {}) or {}).get("page_type", "")).lower()
        candidates = (page_type, main_function, snapshot_type)
        return any(value and ("serp" in value or "搜索结果" in value) for value in candidates)

    async def _extract_from_serp_dom(
        self,
        toolkit: BrowserToolkit,
        task_description: str,
        understanding: Optional[Dict[str, Any]],
        limit: int,
    ) -> Optional[Dict[str, Any]]:
        if not self._looks_like_serp(understanding):
            return None
        try:
            result = await toolkit.evaluate_js(
                r"""(scanLimit) => {
                    const root = document.querySelector('#b_results, #search, #content_left, main, [role="main"]') || document.body;
                    const candidates = Array.from(root.querySelectorAll('li, article, div')).slice(0, scanLimit);
                    const compact = (text) => (text || '').replace(/\s+/g, ' ').trim();
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
                        if (node.closest('header, nav, footer, form, aside')) continue;
                        const anchor = node.querySelector('h1 a[href], h2 a[href], h3 a[href], h4 a[href], a[href]');
                        if (!anchor) continue;
                        const href = anchor.href || anchor.getAttribute('href') || '';
                        if (!href || !/^https?:/i.test(href)) continue;
                        if (seen.has(href)) continue;
                        const titleNode = node.querySelector('h1, h2, h3, h4') || anchor;
                        const title = compact(titleNode.innerText || titleNode.textContent || '').slice(0, 220);
                        if (!title || title.length < 6) continue;
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
                        if (cards.length >= Math.max(20, scanLimit / 10)) break;
                    }
                    return cards;
                }""",
                max(limit * 20, 240),
            )
        except Exception as exc:
            log_warning(f"SERP DOM 直接提取失败: {exc}")
            return None

        cards = result.data if result.success and isinstance(result.data, list) else []
        cleaned = normalize_search_cards(
            cards,
            task_description,
            limit=limit,
            understanding=understanding,
        )
        if not cleaned:
            return None
        return {
            "success": True,
            "data": cleaned,
            "count": len(cleaned),
            "config": {
                "success": True,
                "mode": "serp_dom_cards",
                "page_type": "serp",
                "validation": {"expected_min_items": min(limit, len(cleaned)), "required_fields": self._infer_requested_fields(task_description, understanding or {})},
            },
        }

    async def plan_next_action(
        self,
        toolkit: BrowserToolkit,
        task_description: str,
        understanding: Dict[str, Any],
        collected_count: int,
        target_count: int,
    ) -> Dict[str, Any]:
        url_r = await toolkit.get_current_url()
        title_r = await toolkit.get_title()

        prompt = ACTION_PLANNING_PROMPT.format(
            task_description=task_description,
            page_understanding=self._serialize_understanding(understanding),
            current_url=url_r.data or "",
            page_title=title_r.data or "",
            collected_count=collected_count,
            target_count=target_count,
        )

        response = self.llm.chat_with_system(
            system_prompt=prompt,
            user_message="请规划下一步操作",
            temperature=0.3,
            json_mode=True,
        )

        try:
            return self.llm.parse_json_response(response)
        except Exception as exc:
            log_error(f"操作规划解析失败: {exc}")
            return {"next_action": "done", "reasoning": f"规划失败: {exc}"}
