"""
OmniCore browser automation agent.
Agent 决策层 — 通过 BrowserToolkit 执行所有浏览器操作。
"""
import asyncio
import json
import os
import random
import re
from urllib.parse import parse_qs, quote_plus, urlparse
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from config.settings import settings
from core.llm import LLMClient
from utils.browser_toolkit import BrowserToolkit, ToolkitResult
from utils.logger import log_agent_action, log_error, log_success, log_warning
from utils.retry import async_retry, is_retryable


class ActionType(Enum):
    CLICK = "click"
    INPUT = "input"
    SELECT = "select"
    SCROLL = "scroll"
    WAIT = "wait"
    NAVIGATE = "navigate"
    EXTRACT = "extract"
    PRESS_KEY = "press_key"
    CONFIRM = "confirm"
    SWITCH_TAB = "switch_tab"
    CLOSE_TAB = "close_tab"
    UPLOAD_FILE = "upload_file"
    DOWNLOAD = "download"
    SWITCH_IFRAME = "switch_iframe"
    EXIT_IFRAME = "exit_iframe"
    FILL_FORM = "fill_form"
    DONE = "done"
    FAILED = "failed"


@dataclass
class PageElement:
    index: int
    tag: str
    text: str
    element_type: str
    selector: str
    attributes: Dict[str, str] = field(default_factory=dict)
    is_visible: bool = True
    is_clickable: bool = True


@dataclass
class BrowserAction:
    action_type: ActionType
    target_selector: str = ""
    value: str = ""
    description: str = ""
    confidence: float = 0.0
    requires_confirmation: bool = False
    fallback_selector: str = ""
    use_keyboard_fallback: bool = False
    keyboard_key: str = ""


@dataclass
class TaskIntent:
    intent_type: str = "read"
    query: str = ""
    confidence: float = 0.0
    fields: Dict[str, str] = field(default_factory=dict)
    requires_interaction: bool = False
    target_text: str = ""

ACTION_DECISION_PROMPT = """You are a browser control planner. Return JSON only.
Task: {task}
URL: {url}
Title: {title}
{data_progress}
Elements:
{elements}

Important decision rules:
- If data_collected < data_target and you see pagination controls (下一页/Next/page numbers/arrows), CLICK the next page button.
- If data_collected < data_target and no pagination is visible, use SCROLL (value="800") to trigger lazy loading, then EXTRACT.
- If data_collected < data_target and you see "加载更多"/"Load More"/"查看更多" buttons, CLICK them.
- If data_collected >= data_target or no more data can be loaded, use DONE.
- SCROLL value should be pixels to scroll down (e.g. "800" for one screen).

Return keys: thinking, action, confidence, requires_human_confirm. The action object must contain: type, element_index, value, description, fallback_selector, use_keyboard, keyboard_key."""


class BrowserAgent:
    def __init__(
        self,
        llm_client: Optional[LLMClient] = None,
        headless: Optional[bool] = None,
        user_data_dir: Optional[str] = None,
        toolkit: Optional[BrowserToolkit] = None,
    ):
        self.name = "BrowserAgent"
        self.llm = llm_client
        self._element_cache: List[PageElement] = []
        self._action_history: List[str] = []
        self._intent_cache: Dict[str, TaskIntent] = {}

        # 如果外部传入 toolkit 就用，否则自建
        if toolkit:
            self.toolkit = toolkit
        else:
            fast = settings.BROWSER_FAST_MODE
            self.toolkit = BrowserToolkit(
                headless=fast if headless is None else headless,
                fast_mode=fast,
                block_heavy_resources=settings.BLOCK_HEAVY_RESOURCES,
                user_data_dir=user_data_dir,
            )
        self._owns_toolkit = toolkit is None  # 自建的才负责关闭

    async def close(self) -> None:
        if self._owns_toolkit:
            await self.toolkit.close()

    def _get_llm(self) -> LLMClient:
        if self.llm is None:
            self.llm = LLMClient()
        return self.llm

    # ── pure logic helpers (no browser) ──────────────────────

    def _normalize_text(self, value: str) -> str:
        return re.sub(r"\s+", " ", (value or "").strip()).lower()

    def _is_read_only_task(self, task: str, intent: Optional[TaskIntent] = None) -> bool:
        normalized = self._normalize_text(task)
        if not normalized:
            return False
        if intent:
            if intent.requires_interaction:
                return False
            if intent.intent_type in {"search", "form", "auth", "navigate"}:
                return False
            if intent.target_text:
                return False
        if len(self._extract_structured_pairs(task)) >= 2:
            return False
        if self._extract_click_target_text(task):
            return False
        return True

    def _action_signature(self, action: BrowserAction) -> str:
        return "|".join([
            action.action_type.value,
            action.target_selector[:80],
            self._normalize_text(action.value)[:80],
            self._normalize_text(action.description)[:80],
        ])

    def _record_action(self, action: BrowserAction) -> None:
        self._action_history.append(self._action_signature(action))
        self._action_history = self._action_history[-6:]

    def _is_action_looping(self, action: BrowserAction, threshold: int = 2) -> bool:
        return self._action_history.count(self._action_signature(action)) >= threshold

    def _is_noise_element(self, element: PageElement) -> bool:
        attrs = element.attributes or {}
        text = self._normalize_text(element.text)
        href = self._normalize_text(attrs.get("href", ""))
        if href in {"#", "javascript:void(0)", "javascript:;", "javascript:"}:
            return True
        if not text and not any(attrs.get(k) for k in ["placeholder", "ariaLabel", "labelText", "title"]):
            return True
        return any(term in text for term in ["cookie", "privacy", "terms"])

    def _filter_noise_elements(self, elements: List[PageElement]) -> List[PageElement]:
        filtered = [e for e in elements if not self._is_noise_element(e)]
        return filtered or elements

    def _score_element_for_context(self, task: str, element: PageElement) -> float:
        attrs = element.attributes or {}
        haystack = " ".join([
            element.text, attrs.get("placeholder", ""), attrs.get("ariaLabel", ""),
            attrs.get("labelText", ""), attrs.get("title", ""), attrs.get("name", ""),
        ]).lower()
        score = 0.0
        for token in self._extract_task_tokens(task):
            if token in haystack:
                score += 2.0
        if element.element_type == "input":
            score += 1.0
        if not element.is_visible:
            score -= 2.0
        if not element.is_clickable:
            score -= 1.5
        if attrs.get("placeholder"):
            score += 0.8
        if attrs.get("labelText") or attrs.get("ariaLabel"):
            score += 0.8
        if element.element_type in {"button", "link"}:
            score += 0.4
        return score

    def _prioritize_elements(self, task: str, elements: List[PageElement], limit: int = 12) -> List[PageElement]:
        ranked = sorted(elements, key=lambda item: self._score_element_for_context(task, item), reverse=True)
        chosen: List[PageElement] = []
        seen_signatures = set()
        for item in ranked:
            attrs = item.attributes or {}
            signature = (
                item.selector[:80],
                self._normalize_text(item.text)[:48],
                self._normalize_text(attrs.get("name", ""))[:32],
                self._normalize_text(attrs.get("placeholder", ""))[:32],
            )
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)
            chosen.append(item)
            if len(chosen) >= limit:
                break
        return chosen

    def _choose_llm_element_limit(self, task: str) -> int:
        pair_count = len(self._extract_structured_pairs(task))
        if pair_count >= 2:
            return 12
        if len(self._derive_primary_query(task).split()) >= 8:
            return 10
        return 8

    def _extract_task_tokens(self, task: str) -> List[str]:
        return [
            token for token in re.split(r"[^a-zA-Z0-9_\u4e00-\u9fff]+", self._normalize_text(task))
            if len(token) >= 2
        ]

    def _format_elements_for_llm(self, task: str, elements: List[PageElement], max_items: Optional[int] = None) -> str:
        limit = max_items or self._choose_llm_element_limit(task)
        lines: List[str] = []
        for element in self._prioritize_elements(task, elements, limit=limit):
            attrs = element.attributes or {}
            descriptor = " | ".join(
                part for part in [
                    element.text[:48], attrs.get("labelText", "")[:36],
                    attrs.get("ariaLabel", "")[:36], attrs.get("placeholder", "")[:36],
                    attrs.get("title", "")[:36],
                ] if part
            )
            selector = element.selector[:72]
            lines.append(f"[{element.index}] type={element.element_type} selector={selector} info={descriptor}")
        return "\n".join(lines)

    # ── element extraction (Agent's "eyes", uses toolkit.evaluate_js) ──

    async def _extract_interactive_elements(self) -> List[PageElement]:
        r = await self.toolkit.evaluate_js(
            r"""
            () => {
              const nodes = Array.from(document.querySelectorAll('a, button, input, textarea, select, [role="button"], [role="link"], [contenteditable="true"]'));
              function textOf(el) {
                return (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
              }
              function isVisible(el) {
                const rects = el.getClientRects();
                return !!(el.offsetWidth || el.offsetHeight || rects.length);
              }
              function labelOf(el) {
                if (el.labels && el.labels.length) {
                  return Array.from(el.labels).map(x => textOf(x)).filter(Boolean).join(' ');
                }
                const id = el.getAttribute('id');
                if (id) {
                  const label = document.querySelector(`label[for="${id}"]`);
                  if (label) return textOf(label);
                }
                const parent = el.closest('label');
                return parent ? textOf(parent) : '';
              }
              function selectorOf(el) {
                if (el.id) return `#${CSS.escape(el.id)}`;
                const name = el.getAttribute('name');
                if (name) return `${el.tagName.toLowerCase()}[name="${CSS.escape(name)}"]`;
                const placeholder = el.getAttribute('placeholder');
                if (placeholder) return `${el.tagName.toLowerCase()}[placeholder="${CSS.escape(placeholder)}"]`;
                const parts = [];
                let cur = el;
                while (cur && cur.nodeType === 1 && parts.length < 4) {
                  let part = cur.tagName.toLowerCase();
                  const parent = cur.parentElement;
                  if (parent) {
                    const siblings = Array.from(parent.children).filter(x => x.tagName === cur.tagName);
                    if (siblings.length > 1) {
                      part += `:nth-of-type(${siblings.indexOf(cur) + 1})`;
                    }
                  }
                  parts.unshift(part);
                  cur = parent;
                }
                return parts.join(' > ');
              }
              function normalizedType(el) {
                const tag = el.tagName.toLowerCase();
                const inputType = (el.getAttribute('type') || '').toLowerCase();
                if (tag === 'a') return 'link';
                if (tag === 'button') return 'button';
                if (tag === 'input' && ['submit', 'button', 'reset'].includes(inputType)) return 'button';
                if (tag === 'input' && inputType) return inputType;
                return (inputType || tag);
              }
              return nodes
                .filter(el => isVisible(el))
                .slice(0, 60)
                .map((el, idx) => ({
                index: idx,
                tag: el.tagName.toLowerCase(),
                text: textOf(el).slice(0, 160),
                element_type: normalizedType(el),
                selector: selectorOf(el),
                attributes: {
                  id: el.getAttribute('id') || '',
                  name: el.getAttribute('name') || '',
                  type: el.getAttribute('type') || '',
                  role: el.getAttribute('role') || '',
                  href: el.getAttribute('href') || '',
                  placeholder: el.getAttribute('placeholder') || '',
                  ariaLabel: el.getAttribute('aria-label') || '',
                  title: el.getAttribute('title') || '',
                  labelText: labelOf(el).slice(0, 120),
                },
                is_visible: true,
                is_clickable: !el.disabled,
              }));
            }
            """
        )
        raw = r.data if r.success else []
        elements = [PageElement(**item) for item in (raw or [])]
        elements = self._filter_noise_elements(elements)
        self._element_cache = elements[:40]
        return self._element_cache

    def _get_cached_element_by_selector(self, selector: str) -> Optional[PageElement]:
        for element in self._element_cache:
            if element.selector == selector:
                return element
        return None

    # ── element finding helpers ────────────────────────────────

    def _find_ranked_elements(self, task: str, elements: List[PageElement],
                              kinds: Optional[List[str]] = None, keywords: Optional[List[str]] = None,
                              exclude_selectors: Optional[List[str]] = None) -> List[PageElement]:
        matches: List[Tuple[float, PageElement]] = []
        task_text = self._normalize_text(task)
        excluded = set(exclude_selectors or [])
        for element in elements:
            if not element.is_visible or not element.is_clickable:
                continue
            if element.selector in excluded:
                continue
            if kinds and element.element_type not in kinds and element.tag not in kinds:
                continue
            attrs = element.attributes or {}
            haystack = " ".join([
                element.text, attrs.get("placeholder", ""), attrs.get("ariaLabel", ""),
                attrs.get("labelText", ""), attrs.get("title", ""), attrs.get("name", ""),
            ]).lower()
            score = 0.0
            for token in [part for part in task_text.split() if len(part) >= 2]:
                if token in haystack:
                    score += 1.0
            if keywords:
                for keyword in keywords:
                    if keyword.lower() in haystack:
                        score += 3.0
            if attrs.get("placeholder"):
                score += 0.5
            if attrs.get("labelText"):
                score += 0.5
            if score > 0:
                matches.append((score, element))
        matches.sort(key=lambda item: item[0], reverse=True)
        return [item[1] for item in matches]

    def _find_best_element(self, task: str, elements: List[PageElement],
                           kinds: Optional[List[str]] = None, keywords: Optional[List[str]] = None,
                           exclude_selectors: Optional[List[str]] = None) -> Optional[PageElement]:
        ranked = self._find_ranked_elements(task, elements, kinds=kinds, keywords=keywords, exclude_selectors=exclude_selectors)
        return ranked[0] if ranked else None

    def _derive_primary_query(self, task: str) -> str:
        normalized = re.sub(r"https?://\S+", " ", task or "")
        normalized = re.sub(r"[^\w\u4e00-\u9fff]+", " ", normalized, flags=re.UNICODE)
        chunks = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9][A-Za-z0-9_+.-]{1,}", normalized)
        if chunks:
            return " ".join(chunks[:12]).strip()
        return ""

    def _build_form_fill_action(self, mapping: Dict[str, str]) -> BrowserAction:
        return BrowserAction(
            action_type=ActionType.FILL_FORM,
            value=json.dumps(mapping, ensure_ascii=False),
            description="fill form fields",
            confidence=0.9,
        )

    def _extract_click_target_text(self, task: str) -> str:
        for pattern in (
            r'"([^"\n]{2,64})"',
            r"'([^'\n]{2,64})'",
            r"“([^”\n]{2,64})”",
            r"‘([^’\n]{2,64})’",
            r"「([^」\n]{2,64})」",
            r"『([^』\n]{2,64})』",
            r"《([^》\n]{2,64})》",
        ):
            match = re.search(pattern, task or "")
            if not match:
                continue
            value = self._normalize_text(match.group(1))
            if len(value) >= 2:
                return value
        pairs = self._extract_structured_pairs(task)
        if len(pairs) == 1:
            value = self._normalize_text(next(iter(pairs.values())))
            if 2 <= len(value) <= 40:
                return value
        return ""

    def _extract_url_from_task(self, task: str) -> Optional[str]:
        match = re.search(r"https?://\S+", task or "")
        if not match:
            return None
        return match.group(0).rstrip('.,);]')

    def _extract_structured_pairs(self, task: str) -> Dict[str, str]:
        pairs: Dict[str, str] = {}
        for key, value in re.findall(
            r"([A-Za-z0-9_\u4e00-\u9fff]{1,24})\s*[:：=]\s*([^\n,，;；]{1,160})",
            task or "",
        ):
            normalized_key = self._normalize_text(key)
            cleaned_value = re.sub(r"\s+", " ", value).strip()
            if normalized_key and cleaned_value:
                pairs[normalized_key] = cleaned_value
        return pairs

    async def _infer_task_intent(self, task: str) -> TaskIntent:
        cache_key = self._normalize_text(task)
        cached = self._intent_cache.get(cache_key)
        if cached is not None:
            return cached

        query = self._derive_primary_query(task)
        fields = self._extract_structured_pairs(task)
        target_text = self._extract_click_target_text(task)

        if self._extract_url_from_task(task):
            fallback = TaskIntent(intent_type="navigate", query=query, confidence=0.55, target_text=target_text)
        elif len(fields) >= 2:
            fallback = TaskIntent(
                intent_type="form",
                query=query,
                confidence=0.6,
                fields=fields,
                requires_interaction=True,
                target_text=target_text,
            )
        else:
            fallback = TaskIntent(intent_type="search", query=query, confidence=0.35, target_text=target_text)

        try:
            llm = self._get_llm()
            response = await llm.achat(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Classify the browser task into exactly one intent: "
                            "search, read, form, auth, navigate, unknown. "
                            "Return JSON only with keys: intent, confidence, query, "
                            "requires_interaction, fields, target_text."
                        ),
                    },
                    {"role": "user", "content": task or ""},
                ],
                temperature=0.1,
                json_mode=True,
            )
            payload = llm.parse_json_response(response)
            intent_type = str(payload.get("intent", "") or "").strip().lower()
            if intent_type not in {"search", "read", "form", "auth", "navigate", "unknown"}:
                intent_type = fallback.intent_type

            confidence = float(payload.get("confidence", 0.0) or 0.0)
            llm_query = str(payload.get("query", "") or "").strip() or query
            llm_target = self._normalize_text(str(payload.get("target_text", "") or "").strip())
            llm_fields = payload.get("fields", {})
            normalized_fields: Dict[str, str] = {}
            if isinstance(llm_fields, dict):
                for raw_key, raw_value in llm_fields.items():
                    key = self._normalize_text(str(raw_key))
                    value = str(raw_value or "").strip()
                    if key and value:
                        normalized_fields[key] = value

            llm_intent = TaskIntent(
                intent_type=intent_type,
                query=llm_query,
                confidence=max(min(confidence, 1.0), 0.0),
                fields=normalized_fields,
                requires_interaction=bool(payload.get("requires_interaction", False)),
                target_text=llm_target,
            )

            if llm_intent.confidence >= max(fallback.confidence, 0.5):
                fallback = llm_intent
        except Exception as exc:
            log_warning(f"intent inference fallback: {str(exc)[:120]}")

        if not fallback.query:
            fallback.query = query
        if not fallback.fields and fallback.intent_type in {"form", "auth"}:
            fallback.fields = fields
        if not fallback.target_text:
            fallback.target_text = target_text
        if fallback.intent_type in {"form", "auth"}:
            fallback.requires_interaction = True
        if fallback.target_text and fallback.intent_type in {"read", "unknown"}:
            fallback.intent_type = "navigate"
            fallback.requires_interaction = True

        self._intent_cache[cache_key] = fallback
        return fallback

    def _iter_input_candidates(self, elements: List[PageElement]) -> List[PageElement]:
        candidates: List[PageElement] = []
        for element in elements:
            if not element.is_visible or not element.is_clickable:
                continue
            if element.element_type in {"input", "text", "search", "email", "password", "textarea"}:
                candidates.append(element)
                continue
            if element.tag in {"input", "textarea"}:
                candidates.append(element)
        return candidates

    def _field_match_score(self, field_name: str, element: PageElement) -> float:
        attrs = element.attributes or {}
        haystack = self._normalize_text(
            " ".join(
                [
                    element.text,
                    attrs.get("name", ""),
                    attrs.get("id", ""),
                    attrs.get("placeholder", ""),
                    attrs.get("labelText", ""),
                    attrs.get("ariaLabel", ""),
                    attrs.get("title", ""),
                    attrs.get("type", ""),
                ]
            )
        )
        score = 0.0
        for token in self._extract_task_tokens(field_name):
            if token and token in haystack:
                score += 2.0
        if element.element_type in {"text", "search", "email", "password", "textarea"}:
            score += 0.8
        if attrs.get("name"):
            score += 0.2
        return score

    def _build_form_mapping_from_pairs(
        self,
        fields: Dict[str, str],
        elements: List[PageElement],
    ) -> Dict[str, str]:
        if not fields:
            return {}

        available = self._iter_input_candidates(elements)
        if not available:
            return {}

        mapping: Dict[str, str] = {}
        remaining = list(available)

        for field_name, value in fields.items():
            if not remaining:
                break
            scored = sorted(
                ((self._field_match_score(field_name, item), item) for item in remaining),
                key=lambda pair: pair[0],
                reverse=True,
            )
            selected = scored[0][1] if scored else remaining[0]
            mapping[selected.selector] = value
            remaining = [item for item in remaining if item.selector != selected.selector]

        return mapping

    def _find_primary_text_input(self, elements: List[PageElement]) -> Optional[PageElement]:
        candidates = self._iter_input_candidates(elements)
        if not candidates:
            return None

        ranked = sorted(
            candidates,
            key=lambda item: (
                1 if (item.attributes or {}).get("placeholder") else 0,
                1 if (item.attributes or {}).get("labelText") else 0,
                1 if item.element_type in {"search", "text", "email"} else 0,
            ),
            reverse=True,
        )
        return ranked[0] if ranked else None

    def _find_primary_submit_control(self, elements: List[PageElement]) -> Optional[PageElement]:
        controls = [
            item
            for item in elements
            if item.is_visible and item.is_clickable and item.element_type in {"button", "submit", "link"}
        ]
        return controls[0] if controls else None

    async def _bootstrap_search_results(self, query: str) -> bool:
        if not query:
            return False
        search_url = f"https://www.bing.com/search?q={quote_plus(query)}"
        result = await self.toolkit.goto(search_url, timeout=30000)
        if not result.success:
            return False
        await self._wait_for_page_ready()
        log_agent_action(self.name, "bootstrap_search", query[:120])
        return True

    def _is_data_relevant(self, query: str, data: List[Dict[str, str]]) -> bool:
        if not data:
            return False
        tokens = [token for token in self._extract_task_tokens(query) if len(token) >= 2][:8]
        if not tokens:
            return True
        for item in data[:8]:
            haystack = self._normalize_text(" ".join(str(v) for v in item.values() if v))
            if any(token in haystack for token in tokens):
                return True
        return False

    # ── Agent decision: local heuristics ───────────────────────

    def _decide_action_locally(
        self,
        task: str,
        elements: List[PageElement],
        intent: Optional[TaskIntent] = None,
    ) -> Optional[BrowserAction]:
        active_intent = intent or TaskIntent(
            intent_type="read",
            query=self._derive_primary_query(task),
            confidence=0.0,
            fields=self._extract_structured_pairs(task),
            requires_interaction=False,
        )

        click_target = active_intent.target_text or self._extract_click_target_text(task)
        if click_target:
            explicit_target = self._find_best_element(
                task,
                elements,
                kinds=["button", "submit", "link"],
                keywords=[click_target],
            )
            if explicit_target:
                return BrowserAction(
                    action_type=ActionType.CLICK,
                    target_selector=explicit_target.selector,
                    description=f"click target {click_target}",
                    confidence=0.82,
                )

        if active_intent.intent_type in {"form", "auth"}:
            mapping = self._build_form_mapping_from_pairs(active_intent.fields, elements)
            if mapping:
                return self._build_form_fill_action(mapping)
            submit_control = self._find_primary_submit_control(elements)
            if submit_control:
                return BrowserAction(
                    action_type=ActionType.CLICK,
                    target_selector=submit_control.selector,
                    description="submit interactive form",
                    confidence=0.62,
                )

        if active_intent.intent_type == "search":
            query = active_intent.query or self._derive_primary_query(task)
            input_element = self._find_primary_text_input(elements)
            if input_element and query:
                return BrowserAction(
                    action_type=ActionType.INPUT,
                    target_selector=input_element.selector,
                    value=query,
                    description="fill search query",
                    confidence=0.9,
                    use_keyboard_fallback=True,
                    keyboard_key="Enter",
                )
            submit_control = self._find_primary_submit_control(elements)
            if submit_control:
                return BrowserAction(
                    action_type=ActionType.CLICK,
                    target_selector=submit_control.selector,
                    description="continue search flow",
                    confidence=0.55,
                    use_keyboard_fallback=True,
                    keyboard_key="Enter",
                )
            return BrowserAction(
                action_type=ActionType.EXTRACT,
                description="extract visible search results",
                confidence=0.4,
            )

        if active_intent.intent_type in {"read", "navigate", "unknown"} and self._is_read_only_task(task, active_intent):
            return BrowserAction(
                action_type=ActionType.EXTRACT,
                description="extract visible page content",
                confidence=0.45,
            )

        return None

    # ── Agent decision: LLM ────────────────────────────────────

    def _action_from_llm(self, payload: Dict[str, Any], elements: List[PageElement]) -> BrowserAction:
        action_payload = payload.get("action", {}) if isinstance(payload, dict) else {}
        action_type_raw = str(action_payload.get("type", "failed")).lower()
        try:
            action_type = ActionType(action_type_raw)
        except ValueError:
            action_type = ActionType.FAILED

        selector = str(action_payload.get("target_selector", "") or "")
        index = action_payload.get("element_index", -1)
        if not isinstance(index, int):
            try:
                index = int(index)
            except (TypeError, ValueError):
                index = -1
        if not selector and isinstance(index, int):
            for element in elements:
                if element.index == index:
                    selector = element.selector
                    break

        return BrowserAction(
            action_type=action_type, target_selector=selector,
            value=str(action_payload.get("value", "") or ""),
            description=str(action_payload.get("description", "") or ""),
            confidence=float(payload.get("confidence", 0.0) or 0.0),
            requires_confirmation=bool(payload.get("requires_human_confirm", False)),
            fallback_selector=str(action_payload.get("fallback_selector", "") or ""),
            use_keyboard_fallback=bool(action_payload.get("use_keyboard", False)),
            keyboard_key=str(action_payload.get("keyboard_key", "") or ""),
        )

    async def _decide_action_with_llm(self, task: str, elements: List[PageElement]) -> BrowserAction:
        try:
            if not elements:
                if self._is_read_only_task(task):
                    return BrowserAction(action_type=ActionType.EXTRACT, description="extract visible data")
                return BrowserAction(action_type=ActionType.WAIT, value="1", description="no actionable elements", confidence=0.05)

            title_r = await self.toolkit.get_title()
            page_title = title_r.data or ""
            url_r = await self.toolkit.get_current_url()

            surface = self.toolkit.active_surface
            current_data = await self._maybe_extract_data()
            data_collected = len(current_data) if current_data else 0
            target_match = re.search(r'(\d+)\s*(?:个|条|款|项|条数据|items?|results?)', task or "")
            data_target = int(target_match.group(1)) if target_match else 10
            data_progress = f"Data progress: collected {data_collected} / target {data_target}"
            if data_collected >= data_target:
                data_progress += " (ENOUGH - consider using DONE)"

            messages = [
                {"role": "system", "content": "Return JSON only."},
                {"role": "user", "content": ACTION_DECISION_PROMPT.format(
                    task=task, url=url_r.data or "", title=page_title,
                    data_progress=data_progress,
                    elements=self._format_elements_for_llm(task, elements),
                )},
            ]
            llm = self._get_llm()
            response = await llm.achat(messages, temperature=0.1, json_mode=True)
            return self._action_from_llm(llm.parse_json_response(response), elements)
        except Exception as exc:
            log_warning(f"LLM action fallback failed: {exc}")
            return BrowserAction(action_type=ActionType.WAIT, value="1", description="fallback wait", confidence=0.1)

    # ── recovery ─────────────────────────────────────────────

    def _recover_action(self, task: str, action: BrowserAction, elements: List[PageElement]) -> Optional[BrowserAction]:
        if action.action_type == ActionType.CLICK:
            cached = self._get_cached_element_by_selector(action.target_selector)
            attrs = cached.attributes if cached else {}
            keyword_candidates = [
                self._normalize_text(action.description),
                self._normalize_text(attrs.get("labelText", "")),
                self._normalize_text(attrs.get("ariaLabel", "")),
                self._normalize_text(attrs.get("title", "")),
            ]
            keywords = [item for item in keyword_candidates if len(item) >= 2] or self._extract_task_tokens(task)[:4]
            alternative = self._find_best_element(task, elements, kinds=["button", "submit", "link"],
                                                  keywords=keywords, exclude_selectors=[action.target_selector])
            if alternative:
                return BrowserAction(action_type=ActionType.CLICK, target_selector=alternative.selector,
                                     description=f"recovery click {alternative.text[:24]}".strip(),
                                     confidence=max(action.confidence - 0.2, 0.35),
                                     use_keyboard_fallback=action.use_keyboard_fallback, keyboard_key=action.keyboard_key)
        if action.action_type == ActionType.INPUT:
            alternative = self._find_best_element(task, elements, kinds=["input", "search", "textarea", "text"],
                                                  keywords=self._extract_task_tokens(task)[:4],
                                                  exclude_selectors=[action.target_selector])
            if alternative:
                return BrowserAction(action_type=ActionType.INPUT, target_selector=alternative.selector,
                                     value=action.value, description=f"recovery input {alternative.text[:24]}".strip(),
                                     confidence=max(action.confidence - 0.2, 0.35),
                                     use_keyboard_fallback=action.use_keyboard_fallback, keyboard_key=action.keyboard_key)
        return None

    # ── click/input fallback strategies (Agent-level, calls toolkit) ──

    async def _try_click_with_fallbacks(self, selector: str, action: Optional[BrowserAction] = None) -> bool:
        tk = self.toolkit
        strategies: List[Tuple[str, Any]] = []
        if selector:
            strategies.append(("direct_click", lambda: tk.click(selector)))
        # semantic strategies from cache
        element = self._get_cached_element_by_selector(selector)
        if element:
            attrs = element.attributes or {}
            labels = [element.text, attrs.get("labelText", ""), attrs.get("ariaLabel", ""), attrs.get("title", "")]
            labels = [l.strip()[:60] for l in labels if l and l.strip()]
            labels = list(dict.fromkeys(labels))
            role = "link" if element.element_type == "link" else "button"
            for label in labels:
                strategies.append((f"role:{role}:{label}", lambda r=role, l=label: tk.click_by_role(r, l)))
            for label in [attrs.get("labelText", ""), attrs.get("ariaLabel", "")]:
                if label and label.strip():
                    strategies.append((f"label:{label[:30]}", lambda l=label.strip()[:60]: tk.click_by_label(l)))
        if selector:
            strategies.append(("locator_click", lambda: tk.locator_click(selector)))
        if action and action.fallback_selector:
            fb = action.fallback_selector
            strategies.append((f"fallback:{fb}", lambda s=fb: tk.click(s)))
        if selector:
            strategies.append(("force_click", lambda: tk.force_click(selector)))
        if action and action.use_keyboard_fallback and action.keyboard_key:
            strategies.append((f"keyboard:{action.keyboard_key}", lambda k=action.keyboard_key: tk.press_key(k)))

        for name, handler in strategies:
            try:
                r = await handler()
                if isinstance(r, ToolkitResult) and r.success:
                    log_agent_action(self.name, "click", name)
                    return True
                elif not isinstance(r, ToolkitResult):
                    log_agent_action(self.name, "click", name)
                    return True
            except Exception:
                continue
        return False

    async def _try_input_with_fallbacks(self, selector: str, value: str) -> bool:
        if not selector:
            return False
        tk = self.toolkit
        strategies: List[Tuple[str, Any]] = [
            ("direct_fill", lambda: tk.input_text(selector, value)),
        ]
        element = self._get_cached_element_by_selector(selector)
        if element:
            attrs = element.attributes or {}
            if attrs.get("placeholder"):
                ph = attrs["placeholder"].strip()[:60]
                strategies.append((f"placeholder:{ph}", lambda p=ph: tk.fill_by_placeholder(p, value)))
            for label in [attrs.get("labelText", ""), attrs.get("ariaLabel", "")]:
                if label and label.strip():
                    strategies.append((f"label:{label[:30]}", lambda l=label.strip()[:60]: tk.fill_by_label(l, value)))
        strategies.append(("direct_type", lambda: tk.type_text(selector, value, delay=20)))

        for name, handler in strategies:
            try:
                r = await handler()
                if isinstance(r, ToolkitResult) and r.success:
                    log_agent_action(self.name, "input", name)
                    return True
            except Exception:
                continue
        return False

    async def _fill_form(self, form_data_json: str) -> bool:
        try:
            form_data = json.loads(form_data_json) if isinstance(form_data_json, str) else form_data_json
        except json.JSONDecodeError:
            log_error("invalid form payload")
            return False
        if not isinstance(form_data, dict) or not form_data:
            return False

        tk = self.toolkit
        success_count = 0
        for selector, value in form_data.items():
            try:
                exists = await tk.element_exists(selector)
                if exists.data:
                    # detect element type via JS
                    info = await tk.evaluate_js(
                        "(sel) => { const el = document.querySelector(sel); if (!el) return {}; return {tag: el.tagName.toLowerCase(), type: (el.type||'').toLowerCase()}; }",
                        selector,
                    )
                    tag = (info.data or {}).get("tag", "")
                    input_type = (info.data or {}).get("type", "")
                    if tag == "select":
                        await tk.select_option(selector, str(value))
                    elif input_type in {"checkbox", "radio"}:
                        should_check = str(value).lower() in {"true", "1", "yes", "on"}
                        checked_r = await tk.evaluate_js("(sel) => document.querySelector(sel)?.checked", selector)
                        if bool(checked_r.data) != should_check:
                            await tk.click(selector)
                    elif input_type == "file":
                        await tk.upload_file(selector, str(value))
                    else:
                        if not await self._try_input_with_fallbacks(selector, str(value)):
                            await tk.input_text(selector, str(value))
                else:
                    if not await self._try_input_with_fallbacks(selector, str(value)):
                        continue
                success_count += 1
                await tk.human_delay(50, 120)
            except Exception as exc:
                log_warning(f"fill field failed for {selector}: {exc}")
        return success_count > 0

    # ── thin _execute_action mapping ───────────────────────────

    async def _execute_action(self, action: BrowserAction) -> bool:
        tk = self.toolkit
        if action.action_type == ActionType.CLICK:
            return await self._try_click_with_fallbacks(action.target_selector, action)
        if action.action_type == ActionType.INPUT:
            success = await self._try_input_with_fallbacks(action.target_selector, action.value)
            if success and action.use_keyboard_fallback and action.keyboard_key:
                await tk.press_key(action.keyboard_key)
            return success
        if action.action_type == ActionType.FILL_FORM:
            return await self._fill_form(action.value)
        if action.action_type == ActionType.SELECT:
            r = await tk.select_option(action.target_selector, action.value)
            return r.success
        if action.action_type == ActionType.SCROLL:
            r = await tk.scroll_down(int(action.value or 800))
            return r.success
        if action.action_type == ActionType.WAIT:
            await asyncio.sleep(max(float(action.value or 1), 0.2))
            return True
        if action.action_type == ActionType.NAVIGATE:
            await tk.exit_iframe()
            r = await tk.goto(action.value, timeout=20000)
            return r.success
        if action.action_type == ActionType.PRESS_KEY:
            r = await tk.press_key(action.value or action.keyboard_key or "Enter")
            return r.success
        if action.action_type == ActionType.CONFIRM:
            return not settings.REQUIRE_HUMAN_CONFIRM
        if action.action_type == ActionType.SWITCH_TAB:
            idx = -1 if action.value == "last" else int(action.value or 0)
            r = await tk.switch_tab(idx)
            return r.success
        if action.action_type == ActionType.CLOSE_TAB:
            r = await tk.close_tab()
            return r.success
        if action.action_type == ActionType.DOWNLOAD:
            if not action.target_selector:
                return False
            r = await tk.expect_download(action.target_selector, save_path=action.value or "")
            if r.success:
                return True
            return await self._try_click_with_fallbacks(action.target_selector, action)
        if action.action_type == ActionType.SWITCH_IFRAME:
            r = await tk.switch_to_iframe(action.target_selector)
            return r.success
        if action.action_type == ActionType.EXIT_IFRAME:
            await tk.exit_iframe()
            return True
        if action.action_type == ActionType.UPLOAD_FILE:
            r = await tk.upload_file(action.target_selector, action.value)
            return r.success
        if action.action_type == ActionType.DONE:
            return True
        return False

    # ── verification (Agent-level, calls toolkit) ────────────

    async def _snapshot_page_state(self) -> Dict[str, Any]:
        tk = self.toolkit
        url_r = await tk.get_current_url()
        title_r = await tk.get_title()
        html_r = await tk.get_page_html()
        return {
            "url": url_r.data or "",
            "title": title_r.data or "",
            "content_len": len(html_r.data or ""),
        }

    def _action_must_change_state(self, action: BrowserAction) -> bool:
        return action.action_type in {
            ActionType.CLICK, ActionType.INPUT, ActionType.SELECT,
            ActionType.NAVIGATE, ActionType.PRESS_KEY, ActionType.FILL_FORM,
        }

    async def _verify_action_effect(self, before: Dict[str, Any], action: BrowserAction) -> bool:
        if not self._action_must_change_state(action):
            return True
        after = await self._snapshot_page_state()
        if after["url"] != before["url"]:
            return True
        if after["title"] != before["title"]:
            return True
        if abs(after["content_len"] - before["content_len"]) > 80:
            return True
        if action.action_type == ActionType.INPUT:
            r = await self.toolkit.get_input_value(action.target_selector)
            if r.success:
                return self._normalize_text(r.data) == self._normalize_text(action.value)
        if action.action_type == ActionType.FILL_FORM:
            return await self._verify_form_values(action.value)
        return False

    async def _verify_form_values(self, form_payload: str) -> bool:
        try:
            form_data = json.loads(form_payload) if isinstance(form_payload, str) else form_payload
        except Exception:
            return False
        if not isinstance(form_data, dict) or not form_data:
            return False
        matched = 0
        for selector, expected in form_data.items():
            r = await self.toolkit.get_input_value(str(selector))
            if r.success and self._normalize_text(r.data) == self._normalize_text(str(expected)):
                matched += 1
        return matched > 0

    # ── data extraction ────────────────────────────────────────

    async def _maybe_extract_data(self) -> List[Dict[str, str]]:
        r = await self.toolkit.evaluate_js(
            r"""
            () => {
              const links = Array.from(document.querySelectorAll('a[href]'))
                .slice(0, 10)
                .map(a => ({
                  title: (a.innerText || a.textContent || '').replace(/\s+/g, ' ').trim(),
                  link: a.href,
                }))
                .filter(item => item.title || item.link);
              if (links.length) return links;
              return Array.from(document.querySelectorAll('main p, article p, section p, p, li'))
                .map(node => (node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim())
                .filter(Boolean)
                .slice(0, 8)
                .map((text, idx) => ({ index: idx + 1, text }));
            }
            """
        )
        return r.data or [] if r.success else []

    def _task_looks_satisfied(
        self,
        task: str,
        current_url: str,
        intent: Optional[TaskIntent] = None,
    ) -> bool:
        active_intent = intent or TaskIntent(
            intent_type="search",
            query=self._derive_primary_query(task),
            confidence=0.0,
        )
        if active_intent.intent_type == "search":
            parsed = urlparse(current_url or "")
            query_string = " ".join(
                value
                for values in parse_qs(parsed.query).values()
                for value in values
            )
            haystack = self._normalize_text(f"{parsed.path} {query_string}")
            tokens = [token for token in self._extract_task_tokens(active_intent.query) if len(token) >= 2][:6]
            if not tokens:
                return bool(haystack)
            return any(token in haystack for token in tokens)
        if active_intent.intent_type in {"form", "auth"}:
            return bool(current_url)
        if active_intent.intent_type == "navigate":
            return bool(current_url)
        return False

    async def _wait_for_page_ready(self) -> None:
        tk = self.toolkit
        await tk.wait_for_load("domcontentloaded", timeout=10000)
        if not tk.fast_mode:
            await tk.wait_for_load("networkidle", timeout=3000)
        await tk.human_delay(40, 80)

    # ── main run loop ────────────────────────────────────────

    async def run(self, task: str, start_url: Optional[str] = None, max_steps: int = 8) -> Dict[str, Any]:
        tk = self.toolkit
        r = await tk.create_page()
        if not r.success:
            return {"success": False, "message": f"浏览器启动失败: {r.error}", "steps": []}

        url = start_url or self._extract_url_from_task(task) or "https://www.google.com"
        steps: List[Dict[str, Any]] = []
        self._action_history = []

        try:
            # 初始导航
            async def _initial_goto():
                page = tk.page
                if page and page.is_closed():
                    await tk.create_page()
                return await tk.goto(url, timeout=30000)

            try:
                await async_retry(
                    _initial_goto, max_attempts=3, base_delay=2.0, caller_name=self.name,
                )
            except Exception as nav_err:
                return {"success": False, "message": f"初始导航失败: {str(nav_err)[:200]}", "url": url, "steps": steps}

            await self._wait_for_page_ready()
            task_intent = await self._infer_task_intent(task)
            if (
                task_intent.intent_type == "search"
                and not start_url
                and not self._extract_url_from_task(task)
            ):
                await self._bootstrap_search_results(task_intent.query or self._derive_primary_query(task))

            if self._is_read_only_task(task, task_intent):
                initial_data = await self._maybe_extract_data()
                if initial_data and (
                    task_intent.intent_type != "search"
                    or self._is_data_relevant(task_intent.query, initial_data)
                ):
                    title_r = await tk.get_title()
                    url_r = await tk.get_current_url()
                    return {"success": True, "message": "read-only task satisfied from initial page",
                            "url": url_r.data or "", "title": title_r.data or "",
                            "steps": steps, "data": initial_data}

            # 累积数据容器
            _accumulated_data: List[Dict[str, str]] = []
            _seen_keys: set = set()

            def _merge_new_data(new_items: List[Dict[str, str]]):
                for item in (new_items or []):
                    vals = [str(v)[:80] for v in list(item.values())[:2] if v]
                    key = "|".join(vals)
                    if key and key not in _seen_keys:
                        _seen_keys.add(key)
                        _accumulated_data.append(item)

            for step_no in range(1, max_steps + 1):
                elements = await self._extract_interactive_elements()
                action = self._decide_action_locally(task, elements, task_intent)
                if action is None:
                    action = await self._decide_action_with_llm(task, elements)

                if action.action_type == ActionType.DONE:
                    data = await self._maybe_extract_data()
                    _merge_new_data(data)
                    log_success("browser task completed")
                    url_r = await tk.get_current_url()
                    title_r = await tk.get_title()
                    return {"success": True, "message": "task completed",
                            "url": url_r.data or "", "title": title_r.data or "",
                            "steps": steps, "data": _accumulated_data or data}

                if action.action_type == ActionType.EXTRACT:
                    data = await self._maybe_extract_data()
                    _merge_new_data(data)
                    url_r = await tk.get_current_url()
                    title_r = await tk.get_title()
                    return {"success": True, "message": "data extracted",
                            "url": url_r.data or "", "title": title_r.data or "",
                            "steps": steps, "data": _accumulated_data or data}

                if action.requires_confirmation and settings.REQUIRE_HUMAN_CONFIRM:
                    return {"success": False, "message": "action requires human confirmation",
                            "requires_confirmation": True, "steps": steps}

                if self._is_action_looping(action):
                    if self._is_read_only_task(task, task_intent):
                        _merge_new_data(await self._maybe_extract_data())
                        url_r = await tk.get_current_url()
                        title_r = await tk.get_title()
                        return {"success": True, "message": "repeated action avoided; extracted current page",
                                "url": url_r.data or "", "title": title_r.data or "",
                                "steps": steps, "data": _accumulated_data}
                    url_r = await tk.get_current_url()
                    title_r = await tk.get_title()
                    return {"success": False, "message": f"repeated action loop detected at step {step_no}",
                            "url": url_r.data or "", "title": title_r.data or "",
                            "steps": steps, "data": _accumulated_data}
                self._record_action(action)

                before = await self._snapshot_page_state()
                success = await self._execute_action(action)
                if success:
                    await self._wait_for_page_ready()
                    success = await self._verify_action_effect(before, action)
                if not success:
                    recovery = self._recover_action(task, action, elements)
                    if recovery:
                        recovery_before = await self._snapshot_page_state()
                        success = await self._execute_action(recovery)
                        if success:
                            await self._wait_for_page_ready()
                            success = await self._verify_action_effect(recovery_before, recovery)
                        if success:
                            action = recovery
                            self._record_action(action)

                url_r = await tk.get_current_url()
                steps.append({
                    "step": step_no,
                    "plan": action.description or action.action_type.value,
                    "action": action.target_selector or action.action_type.value,
                    "observation": "success" if success else "failed",
                    "decision": "continue" if success else "retry_or_fail",
                    "action_type": action.action_type.value,
                    "selector": action.target_selector,
                    "value": action.value,
                    "description": action.description,
                    "result": "success" if success else "failed",
                    "url": url_r.data or "",
                })

                if not success:
                    if action.action_type == ActionType.WAIT:
                        continue
                    _consecutive_fails = sum(1 for s in reversed(steps) if s.get("result") == "failed")
                    if _consecutive_fails < 2:
                        log_warning(f"step {step_no} 失败，跳过继续尝试下一步")
                        continue
                    title_r = await tk.get_title()
                    return {"success": False,
                            "message": f"连续 {_consecutive_fails} 步失败，放弃执行 (最后在 step {step_no})",
                            "url": url_r.data or "", "title": title_r.data or "",
                            "steps": steps, "data": _accumulated_data or await self._maybe_extract_data()}

                if action.action_type in {ActionType.CLICK, ActionType.INPUT, ActionType.FILL_FORM, ActionType.PRESS_KEY}:
                    step_data = await self._maybe_extract_data()
                    _merge_new_data(step_data)
                    if self._task_looks_satisfied(task, url_r.data or "", task_intent):
                        title_r = await tk.get_title()
                        return {"success": True, "message": "task reached target page",
                                "url": url_r.data or "", "title": title_r.data or "",
                                "steps": steps, "data": _accumulated_data or await self._maybe_extract_data()}

                if action.action_type == ActionType.SCROLL:
                    step_data = await self._maybe_extract_data()
                    _merge_new_data(step_data)

            # max steps reached
            _merge_new_data(await self._maybe_extract_data())
            url_r = await tk.get_current_url()
            title_r = await tk.get_title()
            return {
                "success": len(_accumulated_data) > 0,
                "message": "max steps reached" + (f", but collected {len(_accumulated_data)} items" if _accumulated_data else ""),
                "url": url_r.data or "", "title": title_r.data or "",
                "steps": steps, "data": _accumulated_data,
            }
        except Exception as exc:
            log_error(f"browser task failed: {exc}")
            url_r = await tk.get_current_url()
            return {"success": False, "message": str(exc), "url": url_r.data or "", "steps": steps}


async def _run_browser_task_async(task: str, start_url: Optional[str] = None, headless: bool = True) -> Dict[str, Any]:
    agent = BrowserAgent(headless=headless)
    try:
        return await agent.run(task, start_url=start_url)
    finally:
        await agent.close()


def run_browser_task(task: str, start_url: Optional[str] = None, headless: bool = True) -> Dict[str, Any]:
    return asyncio.run(_run_browser_task_async(task, start_url=start_url, headless=headless))


if __name__ == "__main__":
    print(json.dumps(run_browser_task("search latest ai news", headless=False), ensure_ascii=False, indent=2))
