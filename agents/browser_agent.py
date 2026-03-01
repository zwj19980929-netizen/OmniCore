"""
OmniCore browser automation agent.
Agent 决策层 — 通过 BrowserToolkit 执行所有浏览器操作。
"""
import asyncio
import json
import os
import random
import re
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

    def _is_read_only_task(self, task: str) -> bool:
        lowered = self._normalize_text(task)
        if not lowered:
            return False
        interactive_tokens = [
            "click", "tap", "press", "login", "sign in", "register", "fill", "submit", "upload",
            "点击", "打开", "登录", "注册", "填写", "提交", "上传",
        ]
        read_tokens = [
            "read", "extract", "scrape", "collect", "summarize", "summary", "list",
            "读取", "提取", "抓取", "收集", "总结", "汇总", "列出",
        ]
        if any(token in lowered for token in interactive_tokens):
            return False
        return any(token in lowered for token in read_tokens)

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
        lowered = self._normalize_text(task)
        if any(token in lowered for token in ["search", "find", "lookup", "搜索", "查找"]):
            return 8
        if any(token in lowered for token in ["login", "sign in", "register", "signup", "登录", "注册"]):
            return 10
        if any(token in lowered for token in ["form", "fill", "submit", "apply", "book", "表单", "填写", "提交"]):
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
        text = re.sub(r"https?://\S+", " ", task or "")
        text = re.sub(r"[,.!?:;()\[\]{}]", " ", text)
        text = re.sub(
            r"(please|search|find|open|go to|visit|look up|login|register|form|submit|帮我|搜索|查找|打开|访问|登录|注册|填写|表单|提交)",
            " ", text, flags=re.IGNORECASE,
        )
        parts = [part.strip() for part in text.split() if len(part.strip()) >= 2]
        return " ".join(parts[:8]).strip()

    def _extract_task_credentials(self, task: str) -> Dict[str, str]:
        credentials: Dict[str, str] = {}
        email = re.search(r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})", task or "")
        if email:
            credentials["email"] = email.group(1)
        patterns = {
            "password": [r"(?:password|pwd|密码)[:：=\s]+([^\s,，;；]+)"],
            "username": [r"(?:username|user|account|用户名|账号)[:：=\s]+([^\s,，;；]+)"],
        }
        for key, pattern_list in patterns.items():
            for pattern in pattern_list:
                match = re.search(pattern, task or "", flags=re.IGNORECASE)
                if match:
                    credentials[key] = match.group(1)
                    break
        return credentials

    def _extract_task_form_fields(self, task: str) -> Dict[str, str]:
        fields: Dict[str, str] = {}
        patterns = {
            "name": [r"(?:name|姓名)[:：=\s]+([^,，;\n]+)"],
            "phone": [r"(?:phone|mobile|电话|手机号)[:：=\s]+([0-9+\- ]{6,})"],
            "email": [r"(?:email|邮箱)[:：=\s]+([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})"],
            "company": [r"(?:company|公司)[:：=\s]+([^,，;\n]+)"],
            "title": [r"(?:title|job title|职位)[:：=\s]+([^,，;\n]+)"],
            "address": [r"(?:address|地址)[:：=\s]+([^\n]+)"],
            "verification_code": [r"(?:code|otp|验证码)[:：=\s]+([^,，;\n]+)"],
        }
        for key, pattern_list in patterns.items():
            for pattern in pattern_list:
                match = re.search(pattern, task or "", flags=re.IGNORECASE)
                if match:
                    fields[key] = match.group(1).strip()
                    break
        return fields

    def _build_form_fill_action(self, mapping: Dict[str, str]) -> BrowserAction:
        return BrowserAction(
            action_type=ActionType.FILL_FORM,
            value=json.dumps(mapping, ensure_ascii=False),
            description="fill form fields",
            confidence=0.9,
        )

    def _extract_click_target_text(self, task: str) -> str:
        patterns = [
            r"(?:click|open|tap)\s+([A-Za-z0-9_\-\u4e00-\u9fff ]{2,40})",
            r"(?:点击|打开)\s*([A-Za-z0-9_\-\u4e00-\u9fff ]{2,40})",
        ]
        for pattern in patterns:
            match = re.search(pattern, task or "", flags=re.IGNORECASE)
            if match:
                value = self._normalize_text(match.group(1))
                value = re.split(r"\b(?:button|link|page|页面|按钮)\b", value, maxsplit=1)[0].strip()
                if len(value) >= 2:
                    return value
        return ""

    def _extract_url_from_task(self, task: str) -> Optional[str]:
        match = re.search(r"https?://\S+", task or "")
        if not match:
            return None
        return match.group(0).rstrip('.,);]')

    # ── Agent decision: local heuristics ───────────────────────

    def _decide_action_locally(self, task: str, elements: List[PageElement]) -> Optional[BrowserAction]:
        lowered = self._normalize_text(task)
        click_target = self._extract_click_target_text(task)

        if click_target:
            explicit_target = self._find_best_element(task, elements, kinds=["button", "submit", "link"], keywords=[click_target])
            if explicit_target:
                return BrowserAction(action_type=ActionType.CLICK, target_selector=explicit_target.selector,
                                     description=f"click target {click_target}", confidence=0.82)

        if any(token in lowered for token in ["search", "find", "lookup", "搜索", "查找"]):
            query = self._derive_primary_query(task)
            input_element = self._find_best_element(task, elements, kinds=["input", "search", "textarea"], keywords=["search", "query", "keyword", "搜索"])
            if input_element and query:
                return BrowserAction(action_type=ActionType.INPUT, target_selector=input_element.selector,
                                     value=query, description="fill search query", confidence=0.92,
                                     use_keyboard_fallback=True, keyboard_key="Enter")
            click_element = self._find_best_element(task, elements, kinds=["button", "submit", "link"], keywords=["search", "submit", "go", "搜索", "提交"])
            if click_element:
                return BrowserAction(action_type=ActionType.CLICK, target_selector=click_element.selector,
                                     description="click search action", confidence=0.75,
                                     use_keyboard_fallback=True, keyboard_key="Enter")

        if any(token in lowered for token in ["login", "sign in", "register", "signup", "登录", "注册"]):
            credentials = self._extract_task_credentials(task)
            if credentials:
                mapping: Dict[str, str] = {}
                for field_name, keywords in {
                    "email": ["email", "mail", "邮箱"],
                    "username": ["username", "user", "account", "用户名", "账号"],
                    "password": ["password", "pass", "密码"],
                }.items():
                    if field_name not in credentials:
                        continue
                    target = self._find_best_element(task, elements, kinds=["input", "email", "password", "text"], keywords=keywords)
                    if target:
                        mapping[target.selector] = credentials[field_name]
                if mapping:
                    return self._build_form_fill_action(mapping)
            auth_element = self._find_best_element(task, elements, kinds=["button", "link", "submit"], keywords=["login", "sign in", "register", "signup", "登录", "注册"])
            if auth_element:
                return BrowserAction(action_type=ActionType.CLICK, target_selector=auth_element.selector,
                                     description="open auth flow", confidence=0.7)

        if any(token in lowered for token in ["form", "fill", "submit", "apply", "book", "表单", "填写", "提交", "申请", "预约"]):
            fields = self._extract_task_form_fields(task)
            if len(fields) >= 2:
                mapping: Dict[str, str] = {}
                key_map = {
                    "name": ["name", "姓名"], "phone": ["phone", "mobile", "电话", "手机号"],
                    "email": ["email", "邮箱"], "company": ["company", "公司"],
                    "title": ["title", "职位"], "address": ["address", "地址"],
                    "verification_code": ["code", "otp", "验证码"],
                }
                for field_name, value in fields.items():
                    target = self._find_best_element(task, elements, kinds=["input", "textarea", "text"], keywords=key_map.get(field_name, [field_name]))
                    if target:
                        mapping[target.selector] = value
                if mapping:
                    return self._build_form_fill_action(mapping)
            submit_element = self._find_best_element(task, elements, kinds=["button", "submit", "link"], keywords=["submit", "confirm", "apply", "提交", "确认"])
            if submit_element:
                return BrowserAction(action_type=ActionType.CLICK, target_selector=submit_element.selector,
                                     description="submit form", confidence=0.68)
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
                if any(token in self._normalize_text(task) for token in ["extract", "read", "scrape", "summary", "提取", "读取", "总结"]):
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

    def _task_looks_satisfied(self, task: str, current_url: str) -> bool:
        lowered = self._normalize_text(task)
        url = (current_url or "").lower()
        if any(token in lowered for token in ["search", "find", "lookup", "搜索", "查找"]):
            return any(token in url for token in ["search", "query", "result"])
        if any(token in lowered for token in ["login", "sign in", "登录"]):
            return all(token not in url for token in ["login", "signin"])
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

            if self._is_read_only_task(task):
                initial_data = await self._maybe_extract_data()
                if initial_data:
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
                action = self._decide_action_locally(task, elements)
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
                    if self._is_read_only_task(task):
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
                    if self._task_looks_satisfied(task, url_r.data or ""):
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
