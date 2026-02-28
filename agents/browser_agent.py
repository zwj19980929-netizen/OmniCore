"""
OmniCore browser automation agent.
"""
import asyncio
import json
import os
import random
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from playwright.async_api import Browser, Page, Playwright, async_playwright

from config.settings import settings
from core.llm import LLMClient
from utils.captcha_solver import CaptchaSolver
from utils.logger import log_agent_action, log_error, log_success, log_warning


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


ACTION_DECISION_PROMPT = """You are a browser control planner. Return JSON only.\nTask: {task}\nURL: {url}\nTitle: {title}\nElements:\n{elements}\nReturn keys: thinking, action, confidence, requires_human_confirm. The action object must contain: type, element_index, value, description, fallback_selector, use_keyboard, keyboard_key."""


class BrowserAgent:
    def __init__(self, llm_client: Optional[LLMClient] = None, headless: Optional[bool] = None, user_data_dir: Optional[str] = None):
        self.name = "BrowserAgent"
        self.llm = llm_client
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context = None
        self._page: Optional[Page] = None
        self._element_cache: List[PageElement] = []
        self.captcha_solver = CaptchaSolver()
        self.fast_mode = settings.BROWSER_FAST_MODE
        self.block_heavy_resources = settings.BLOCK_HEAVY_RESOURCES
        self.headless = self.fast_mode if headless is None else headless
        self.user_data_dir = user_data_dir
        self._current_frame = None
        self._in_iframe = False
        self._action_history: List[str] = []

    def _get_storage_state_path(self) -> Optional[str]:
        if not self.user_data_dir:
            return None
        return os.path.join(self.user_data_dir, "storage_state.json")

    def _get_active_surface(self, page: Page):
        if self._in_iframe and self._current_frame is not None:
            return self._current_frame
        return page

    def _get_active_page(self, page: Page) -> Page:
        return self._page or page

    def _get_llm(self) -> LLMClient:
        if self.llm is None:
            self.llm = LLMClient()
        return self.llm

    def _get_keyboard_target(self, page: Any):
        active_page = self._page or page
        return active_page.keyboard

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

    async def _ensure_browser(self) -> Browser:
        if self._browser:
            return self._browser

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--disable-infobars",
                "--window-size=1366,768",
            ],
            ignore_default_args=["--enable-automation"],
        )
        return self._browser

    async def _create_page(self) -> Page:
        browser = await self._ensure_browser()
        context_kwargs = {
            "viewport": {"width": 1366, "height": 768},
            "locale": "zh-CN",
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
        }

        storage_state_path = self._get_storage_state_path()
        if storage_state_path and os.path.exists(storage_state_path):
            context_kwargs["storage_state"] = storage_state_path
            log_agent_action(self.name, "load_storage_state", storage_state_path)

        self._context = await browser.new_context(
            **context_kwargs,
        )

        if self.block_heavy_resources:
            async def _route(route):
                req = route.request
                if req.resource_type in {"image", "font", "media"}:
                    await route.abort()
                    return
                await route.continue_()

            await self._context.route("**/*", _route)

        self._page = await self._context.new_page()
        return self._page

    async def close(self) -> None:
        if self._context:
            storage_state_path = self._get_storage_state_path()
            if storage_state_path:
                try:
                    os.makedirs(os.path.dirname(storage_state_path), exist_ok=True)
                    await self._context.storage_state(path=storage_state_path)
                    log_agent_action(self.name, "save_storage_state", storage_state_path)
                except Exception as exc:
                    log_warning(f"save storage state failed: {exc}")
        if self._page:
            await self._page.close()
            self._page = None
        if self._context:
            await self._context.close()
            self._context = None
        self._current_frame = None
        self._in_iframe = False
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    async def _human_delay(self, slow_ms: int = 120, fast_ms: int = 320) -> None:
        if self.fast_mode:
            await asyncio.sleep(random.uniform(0.02, 0.08))
            return
        await asyncio.sleep(random.uniform(slow_ms / 1000, fast_ms / 1000))

    async def _wait_for_page_ready(self, surface: Any) -> None:
        try:
            await surface.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass
        if not self.fast_mode:
            try:
                await surface.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                pass
        await self._human_delay(40, 80)

    async def _snapshot_page_state(self, page: Page) -> Dict[str, Any]:
        surface = self._get_active_surface(self._get_active_page(page))
        try:
            content = await page.content()
            title = await page.title()
        except Exception:
            content = ""
            title = ""
        try:
            surface_content = await surface.content()
        except Exception:
            surface_content = ""
        surface_url = getattr(surface, "url", page.url)
        return {
            "url": page.url,
            "title": title,
            "content_len": len(content),
            "surface_url": surface_url,
            "surface_content_len": len(surface_content),
        }

    def _action_must_change_state(self, action: BrowserAction) -> bool:
        return action.action_type in {
            ActionType.CLICK,
            ActionType.INPUT,
            ActionType.SELECT,
            ActionType.NAVIGATE,
            ActionType.PRESS_KEY,
            ActionType.FILL_FORM,
        }

    async def _verify_action_effect(self, page: Page, before: Dict[str, Any], action: BrowserAction) -> bool:
        if not self._action_must_change_state(action):
            return True
        after = await self._snapshot_page_state(page)
        if after["url"] != before["url"]:
            return True
        if after.get("surface_url") != before.get("surface_url"):
            return True
        if after["title"] != before["title"]:
            return True
        if abs(after["content_len"] - before["content_len"]) > 80:
            return True
        if abs(after.get("surface_content_len", 0) - before.get("surface_content_len", 0)) > 40:
            return True
        surface = self._get_active_surface(self._get_active_page(page))
        if action.action_type == ActionType.INPUT:
            return await self._verify_input_value(surface, action.target_selector, action.value)
        if action.action_type == ActionType.FILL_FORM:
            return await self._verify_form_values(surface, action.value)
        if action.action_type == ActionType.SELECT:
            return await self._verify_input_value(surface, action.target_selector, action.value)
        return False

    def _normalize_text(self, value: str) -> str:
        return re.sub(r"\s+", " ", (value or "").strip()).lower()

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
        filtered = [element for element in elements if not self._is_noise_element(element)]
        return filtered or elements

    def _score_element_for_context(self, task: str, element: PageElement) -> float:
        attrs = element.attributes or {}
        haystack = " ".join([
            element.text,
            attrs.get("placeholder", ""),
            attrs.get("ariaLabel", ""),
            attrs.get("labelText", ""),
            attrs.get("title", ""),
            attrs.get("name", ""),
        ]).lower()
        score = 0.0
        tokens = self._extract_task_tokens(task)
        for token in tokens:
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
                    element.text[:48],
                    attrs.get("labelText", "")[:36],
                    attrs.get("ariaLabel", "")[:36],
                    attrs.get("placeholder", "")[:36],
                    attrs.get("title", "")[:36],
                ] if part
            )
            selector = element.selector[:72]
            lines.append(f"[{element.index}] type={element.element_type} selector={selector} info={descriptor}")
        return "\n".join(lines)

    async def _extract_interactive_elements(self, surface: Any) -> List[PageElement]:
        raw = await surface.evaluate(
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
        elements = [PageElement(**item) for item in (raw or [])]
        elements = self._filter_noise_elements(elements)
        self._element_cache = elements[:40]
        return self._element_cache

    def _get_cached_element_by_selector(self, selector: str) -> Optional[PageElement]:
        for element in self._element_cache:
            if element.selector == selector:
                return element
        return None

    def _derive_primary_query(self, task: str) -> str:
        text = re.sub(r"https?://\S+", " ", task or "")
        text = re.sub(r"[,.!?:;()\[\]{}]", " ", text)
        text = re.sub(
            r"(please|search|find|open|go to|visit|look up|login|register|form|submit|帮我|搜索|查找|打开|访问|登录|注册|填写|表单|提交)",
            " ",
            text,
            flags=re.IGNORECASE,
        )
        parts = [part.strip() for part in text.split() if len(part.strip()) >= 2]
        return " ".join(parts[:8]).strip()

    def _extract_task_credentials(self, task: str) -> Dict[str, str]:
        credentials: Dict[str, str] = {}
        email = re.search(r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})", task or "")
        if email:
            credentials["email"] = email.group(1)
        patterns = {
            "password": [
                r"(?:password|pwd|密码)[:：=\s]+([^\s,，;；]+)",
            ],
            "username": [
                r"(?:username|user|account|用户名|账号)[:：=\s]+([^\s,，;；]+)",
            ],
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
            "name": [
                r"(?:name|姓名)[:：=\s]+([^,，;\n]+)",
            ],
            "phone": [
                r"(?:phone|mobile|电话|手机号)[:：=\s]+([0-9+\- ]{6,})",
            ],
            "email": [
                r"(?:email|邮箱)[:：=\s]+([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})",
            ],
            "company": [
                r"(?:company|公司)[:：=\s]+([^,，;\n]+)",
            ],
            "title": [
                r"(?:title|job title|职位)[:：=\s]+([^,，;\n]+)",
            ],
            "address": [
                r"(?:address|地址)[:：=\s]+([^\n]+)",
            ],
            "verification_code": [
                r"(?:code|otp|验证码)[:：=\s]+([^,，;\n]+)",
            ],
        }
        for key, pattern_list in patterns.items():
            for pattern in pattern_list:
                match = re.search(pattern, task or "", flags=re.IGNORECASE)
                if match:
                    fields[key] = match.group(1).strip()
                    break
        return fields

    def _find_ranked_elements(
        self,
        task: str,
        elements: List[PageElement],
        kinds: Optional[List[str]] = None,
        keywords: Optional[List[str]] = None,
        exclude_selectors: Optional[List[str]] = None,
    ) -> List[PageElement]:
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
                element.text,
                attrs.get("placeholder", ""),
                attrs.get("ariaLabel", ""),
                attrs.get("labelText", ""),
                attrs.get("title", ""),
                attrs.get("name", ""),
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

    def _find_best_element(
        self,
        task: str,
        elements: List[PageElement],
        kinds: Optional[List[str]] = None,
        keywords: Optional[List[str]] = None,
        exclude_selectors: Optional[List[str]] = None,
    ) -> Optional[PageElement]:
        ranked = self._find_ranked_elements(
            task,
            elements,
            kinds=kinds,
            keywords=keywords,
            exclude_selectors=exclude_selectors,
        )
        return ranked[0] if ranked else None

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
            alternative = self._find_best_element(
                task,
                elements,
                kinds=["button", "submit", "link"],
                keywords=keywords,
                exclude_selectors=[action.target_selector],
            )
            if alternative:
                return BrowserAction(
                    action_type=ActionType.CLICK,
                    target_selector=alternative.selector,
                    description=f"recovery click {alternative.text[:24]}".strip(),
                    confidence=max(action.confidence - 0.2, 0.35),
                    use_keyboard_fallback=action.use_keyboard_fallback,
                    keyboard_key=action.keyboard_key,
                )
        if action.action_type == ActionType.INPUT:
            alternative = self._find_best_element(
                task,
                elements,
                kinds=["input", "search", "textarea", "text"],
                keywords=self._extract_task_tokens(task)[:4],
                exclude_selectors=[action.target_selector],
            )
            if alternative:
                return BrowserAction(
                    action_type=ActionType.INPUT,
                    target_selector=alternative.selector,
                    value=action.value,
                    description=f"recovery input {alternative.text[:24]}".strip(),
                    confidence=max(action.confidence - 0.2, 0.35),
                    use_keyboard_fallback=action.use_keyboard_fallback,
                    keyboard_key=action.keyboard_key,
                )
        return None

    def _decide_action_locally(self, task: str, page: Page, elements: List[PageElement]) -> Optional[BrowserAction]:
        lowered = self._normalize_text(task)
        click_target = self._extract_click_target_text(task)

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

        if any(token in lowered for token in ["search", "find", "lookup", "搜索", "查找"]):
            query = self._derive_primary_query(task)
            input_element = self._find_best_element(task, elements, kinds=["input", "search", "textarea"], keywords=["search", "query", "keyword", "搜索"])
            if input_element and query:
                return BrowserAction(
                    action_type=ActionType.INPUT,
                    target_selector=input_element.selector,
                    value=query,
                    description="fill search query",
                    confidence=0.92,
                    use_keyboard_fallback=True,
                    keyboard_key="Enter",
                )
            click_element = self._find_best_element(task, elements, kinds=["button", "submit", "link"], keywords=["search", "submit", "go", "搜索", "提交"])
            if click_element:
                return BrowserAction(
                    action_type=ActionType.CLICK,
                    target_selector=click_element.selector,
                    description="click search action",
                    confidence=0.75,
                    use_keyboard_fallback=True,
                    keyboard_key="Enter",
                )

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
                return BrowserAction(
                    action_type=ActionType.CLICK,
                    target_selector=auth_element.selector,
                    description="open auth flow",
                    confidence=0.7,
                )

        if any(token in lowered for token in ["form", "fill", "submit", "apply", "book", "表单", "填写", "提交", "申请", "预约"]):
            fields = self._extract_task_form_fields(task)
            if len(fields) >= 2:
                mapping: Dict[str, str] = {}
                key_map = {
                    "name": ["name", "姓名"],
                    "phone": ["phone", "mobile", "电话", "手机号"],
                    "email": ["email", "邮箱"],
                    "company": ["company", "公司"],
                    "title": ["title", "职位"],
                    "address": ["address", "地址"],
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
                return BrowserAction(
                    action_type=ActionType.CLICK,
                    target_selector=submit_element.selector,
                    description="submit form",
                    confidence=0.68,
                )

        return None

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
            action_type=action_type,
            target_selector=selector,
            value=str(action_payload.get("value", "") or ""),
            description=str(action_payload.get("description", "") or ""),
            confidence=float(payload.get("confidence", 0.0) or 0.0),
            requires_confirmation=bool(payload.get("requires_human_confirm", False)),
            fallback_selector=str(action_payload.get("fallback_selector", "") or ""),
            use_keyboard_fallback=bool(action_payload.get("use_keyboard", False)),
            keyboard_key=str(action_payload.get("keyboard_key", "") or ""),
        )

    async def _decide_action_with_llm(self, task: str, page: Page, elements: List[PageElement]) -> BrowserAction:
        try:
            if not elements:
                if any(token in self._normalize_text(task) for token in ["extract", "read", "scrape", "summary", "提取", "读取", "总结"]):
                    return BrowserAction(action_type=ActionType.EXTRACT, description="extract visible data")
                return BrowserAction(action_type=ActionType.WAIT, value="1", description="no actionable elements", confidence=0.05)
            try:
                page_title = await page.title()
            except Exception:
                page_title = ""
            messages = [
                {"role": "system", "content": "Return JSON only."},
                {
                    "role": "user",
                    "content": ACTION_DECISION_PROMPT.format(
                        task=task,
                        url=page.url,
                        title=page_title,
                        elements=self._format_elements_for_llm(task, elements),
                    ),
                },
            ]
            llm = self._get_llm()
            response = await llm.achat(
                messages,
                temperature=0.1,
                json_mode=True,
            )
            return self._action_from_llm(llm.parse_json_response(response), elements)
        except Exception as exc:
            log_warning(f"LLM action fallback failed: {exc}")
            return BrowserAction(action_type=ActionType.WAIT, value="1", description="fallback wait", confidence=0.1)

    def _build_semantic_click_strategies(self, surface: Any, selector: str) -> List[Tuple[str, Any]]:
        strategies: List[Tuple[str, Any]] = []
        element = self._get_cached_element_by_selector(selector)
        if not element:
            return strategies
        if not all(hasattr(surface, attr) for attr in ["get_by_role", "get_by_label", "get_by_title"]):
            return strategies

        def _make_role_click(target_role: str, target_label: str):
            async def _handler():
                await surface.get_by_role(target_role, name=target_label, exact=False).first.click(timeout=5000)
            return _handler

        def _make_label_click(target_label: str):
            async def _handler():
                await surface.get_by_label(target_label, exact=False).first.click(timeout=5000)
            return _handler

        def _make_title_click(target_title: str):
            async def _handler():
                await surface.get_by_title(target_title, exact=False).first.click(timeout=5000)
            return _handler

        attrs = element.attributes or {}
        labels = [element.text, attrs.get("labelText", ""), attrs.get("ariaLabel", ""), attrs.get("title", "")]
        labels = [label.strip()[:60] for label in labels if label and label.strip()]
        labels = list(dict.fromkeys(labels))
        role = "link" if element.element_type == "link" else "button"

        for label in labels:
            strategies.append((f"get_by_role:{role}:{label}", _make_role_click(role, label)))
        for label in [attrs.get("labelText", ""), attrs.get("ariaLabel", "")]:
            if label and label.strip():
                short = label.strip()[:60]
                strategies.append((f"get_by_label:{short}", _make_label_click(short)))
        if attrs.get("title"):
            title = attrs["title"].strip()[:60]
            strategies.append((f"get_by_title:{title}", _make_title_click(title)))
        return strategies

    def _build_semantic_input_strategies(self, surface: Any, selector: str, value: str) -> List[Tuple[str, Any]]:
        strategies: List[Tuple[str, Any]] = []
        element = self._get_cached_element_by_selector(selector)
        if not element:
            return strategies
        if not all(hasattr(surface, attr) for attr in ["get_by_placeholder", "get_by_label", "locator"]):
            return strategies

        def _make_placeholder_fill(target_placeholder: str):
            async def _handler():
                await surface.get_by_placeholder(target_placeholder, exact=False).first.fill(value)
            return _handler

        def _make_label_fill(target_label: str):
            async def _handler():
                await surface.get_by_label(target_label, exact=False).first.fill(value)
            return _handler

        def _make_name_fill(target_name: str):
            async def _handler():
                await surface.locator(f'[name="{target_name}"]').first.fill(value)
            return _handler

        attrs = element.attributes or {}
        if attrs.get("placeholder"):
            placeholder = attrs["placeholder"].strip()[:60]
            strategies.append((f"get_by_placeholder:{placeholder}", _make_placeholder_fill(placeholder)))
        for label in [attrs.get("labelText", ""), attrs.get("ariaLabel", "")]:
            if label and label.strip():
                short = label.strip()[:60]
                strategies.append((f"get_by_label:{short}", _make_label_fill(short)))
        if attrs.get("name"):
            name = attrs["name"].strip()
            strategies.append((f"name:{name}", _make_name_fill(name)))
        return strategies

    async def _read_field_value(self, surface: Any, selector: str) -> str:
        if not selector:
            return ""
        locator = surface.locator(selector).first
        try:
            if not await locator.count():
                return ""
        except Exception:
            return ""
        try:
            return await locator.input_value()
        except Exception:
            pass
        try:
            value = await locator.evaluate(
                """
                el => {
                    if (typeof el.value === 'string') return el.value;
                    return (el.textContent || '').trim();
                }
                """
            )
            return str(value or "")
        except Exception:
            return ""

    async def _verify_input_value(self, surface: Any, selector: str, expected: str) -> bool:
        actual = self._normalize_text(await self._read_field_value(surface, selector))
        expected_text = self._normalize_text(expected)
        return bool(actual) and actual == expected_text

    async def _verify_form_values(self, surface: Any, form_payload: str) -> bool:
        try:
            form_data = json.loads(form_payload) if isinstance(form_payload, str) else form_payload
        except Exception:
            return False
        if not isinstance(form_data, dict) or not form_data:
            return False
        matched = 0
        for selector, expected in form_data.items():
            if await self._verify_input_value(surface, str(selector), str(expected)):
                matched += 1
        return matched > 0

    async def _try_click_with_fallbacks(self, page: Page, selector: str, action: Optional[BrowserAction] = None) -> bool:
        strategies: List[Tuple[str, Any]] = []
        if selector:
            strategies.append(("direct_click", lambda: page.click(selector, timeout=5000)))
        strategies.extend(self._build_semantic_click_strategies(page, selector))
        if selector:
            strategies.append(("locator_click", lambda: page.locator(selector).first.click(timeout=5000)))
        if action and action.fallback_selector:
            fallback = action.fallback_selector
            strategies.append((f"fallback:{fallback}", lambda fallback=fallback: page.click(fallback, timeout=4000)))
        if selector:
            strategies.append(("force_click", lambda: page.locator(selector).first.click(timeout=5000, force=True)))
        if action and action.use_keyboard_fallback and action.keyboard_key:
            strategies.append((f"keyboard:{action.keyboard_key}", lambda key=action.keyboard_key: self._get_keyboard_target(page).press(key)))

        for name, handler in strategies:
            try:
                await handler()
                log_agent_action(self.name, "click", name)
                return True
            except Exception:
                continue
        return False

    async def _try_input_with_fallbacks(self, page: Page, selector: str, value: str) -> bool:
        if not selector:
            return False
        strategies: List[Tuple[str, Any]] = [("direct_fill", lambda: page.locator(selector).first.fill(value, timeout=5000))]
        strategies.extend(self._build_semantic_input_strategies(page, selector, value))
        strategies.append(("direct_type", lambda: page.locator(selector).first.type(value, delay=20, timeout=5000)))

        for name, handler in strategies:
            try:
                await handler()
                log_agent_action(self.name, "input", name)
                return True
            except Exception:
                continue
        return False

    async def _fill_form(self, page: Page, form_data_json: str) -> bool:
        try:
            form_data = json.loads(form_data_json) if isinstance(form_data_json, str) else form_data_json
        except json.JSONDecodeError:
            log_error("invalid form payload")
            return False
        if not isinstance(form_data, dict) or not form_data:
            log_error("form payload must be a non-empty object")
            return False

        success_count = 0
        surface = self._get_active_surface(page)
        for selector, value in form_data.items():
            try:
                locator = surface.locator(selector).first
                if await locator.count():
                    tag = await locator.evaluate("el => el.tagName.toLowerCase()")
                    input_type = await locator.evaluate("el => (el.type || '').toLowerCase()")
                    if tag == "select":
                        await locator.select_option(value=str(value))
                    elif input_type in {"checkbox", "radio"}:
                        checked = await locator.is_checked()
                        should_check = str(value).lower() in {"true", "1", "yes", "on"}
                        if checked != should_check:
                            await locator.click()
                    elif input_type == "file":
                        await locator.set_input_files(str(value))
                    else:
                        if not await self._try_input_with_fallbacks(surface, selector, str(value)):
                            await locator.fill(str(value))
                else:
                    if not await self._try_input_with_fallbacks(surface, selector, str(value)):
                        continue
                success_count += 1
                await self._human_delay(50, 120)
            except Exception as exc:
                log_warning(f"fill field failed for {selector}: {exc}")
        return success_count > 0

    async def _execute_action(self, page: Page, action: BrowserAction) -> bool:
        surface = self._get_active_surface(page)
        if action.action_type == ActionType.CLICK:
            return await self._try_click_with_fallbacks(surface, action.target_selector, action)
        if action.action_type == ActionType.INPUT:
            success = await self._try_input_with_fallbacks(surface, action.target_selector, action.value)
            if success and action.use_keyboard_fallback and action.keyboard_key:
                try:
                    await self._get_keyboard_target(page).press(action.keyboard_key)
                except Exception:
                    pass
            return success
        if action.action_type == ActionType.FILL_FORM:
            return await self._fill_form(page, action.value)
        if action.action_type == ActionType.SELECT:
            await surface.locator(action.target_selector).first.select_option(value=action.value)
            return True
        if action.action_type == ActionType.SCROLL:
            await page.mouse.wheel(0, int(action.value or 800))
            return True
        if action.action_type == ActionType.WAIT:
            await asyncio.sleep(max(float(action.value or 1), 0.2))
            return True
        if action.action_type == ActionType.NAVIGATE:
            self._current_frame = None
            self._in_iframe = False
            await page.goto(action.value, wait_until="domcontentloaded", timeout=20000)
            return True
        if action.action_type == ActionType.PRESS_KEY:
            await self._get_keyboard_target(page).press(action.value or action.keyboard_key or "Enter")
            return True
        if action.action_type == ActionType.CONFIRM:
            return not settings.REQUIRE_HUMAN_CONFIRM
        if action.action_type == ActionType.SWITCH_TAB:
            if not self._context or not self._context.pages:
                return False
            if action.value == "last":
                candidates = [item for item in self._context.pages if not item.is_closed()]
                if not candidates:
                    return False
                target_page = candidates[-1]
            else:
                try:
                    index = int(action.value or 0)
                except ValueError:
                    return False
                candidates = [item for item in self._context.pages if not item.is_closed()]
                if index < 0 or index >= len(candidates):
                    return False
                target_page = candidates[index]
            self._page = target_page
            self._current_frame = None
            self._in_iframe = False
            await self._page.bring_to_front()
            return True
        if action.action_type == ActionType.CLOSE_TAB:
            await page.close()
            if self._context:
                candidates = [item for item in self._context.pages if not item.is_closed()]
            else:
                candidates = []
            if candidates:
                self._page = candidates[-1]
                self._current_frame = None
                self._in_iframe = False
                await self._page.bring_to_front()
                return True
            self._page = None
            return False
        if action.action_type == ActionType.DOWNLOAD:
            if not action.target_selector:
                return False
            try:
                async with page.expect_download(timeout=10000) as download_info:
                    if not await self._try_click_with_fallbacks(surface, action.target_selector, action):
                        return False
                download = await download_info.value
                if action.value:
                    await download.save_as(action.value)
                return True
            except Exception:
                return await self._try_click_with_fallbacks(surface, action.target_selector, action)
        if action.action_type == ActionType.SWITCH_IFRAME:
            frame = None
            if action.target_selector:
                try:
                    frame_handle = await page.locator(action.target_selector).first.element_handle()
                    if frame_handle:
                        frame = await frame_handle.content_frame()
                except Exception:
                    frame = None
            if frame is None:
                child_frames = [item for item in page.frames if item != page.main_frame]
                if child_frames:
                    frame = child_frames[0]
            if frame is None:
                return False
            self._current_frame = frame
            self._in_iframe = True
            try:
                await frame.wait_for_load_state("domcontentloaded", timeout=3000)
            except Exception:
                pass
            await self._human_delay(30, 80)
            return True
        if action.action_type == ActionType.EXIT_IFRAME:
            self._current_frame = None
            self._in_iframe = False
            return True
        if action.action_type == ActionType.UPLOAD_FILE:
            await surface.locator(action.target_selector).first.set_input_files(action.value)
            return True
        if action.action_type == ActionType.DONE:
            return True
        return False

    async def _maybe_extract_data(self, surface: Any) -> List[Dict[str, str]]:
        try:
            data = await surface.evaluate(
                r"""
                () => {
                  const links = Array.from(document.querySelectorAll('a[href]'))
                    .slice(0, 10)
                    .map(a => ({
                      title: (a.innerText || a.textContent || '').replace(/\s+/g, ' ').trim(),
                      link: a.href,
                    }))
                    .filter(item => item.title || item.link);
                  if (links.length) {
                    return links;
                  }
                  return Array.from(document.querySelectorAll('main p, article p, section p, p, li'))
                    .map(node => (node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim())
                    .filter(Boolean)
                    .slice(0, 8)
                    .map((text, idx) => ({ index: idx + 1, text }));
                }
                """
            )
            return data or []
        except Exception:
            return []

    def _task_looks_satisfied(self, task: str, current_url: str) -> bool:
        lowered = self._normalize_text(task)
        url = (current_url or "").lower()
        if any(token in lowered for token in ["search", "find", "lookup", "搜索", "查找"]):
            return any(token in url for token in ["search", "query", "result"])
        if any(token in lowered for token in ["login", "sign in", "登录"]):
            return all(token not in url for token in ["login", "signin"])
        return False

    def _extract_url_from_task(self, task: str) -> Optional[str]:
        match = re.search(r"https?://\S+", task or "")
        if not match:
            return None
        return match.group(0).rstrip('.,);]')

    async def run(self, task: str, start_url: Optional[str] = None, max_steps: int = 8) -> Dict[str, Any]:
        page = await self._create_page()
        url = start_url or self._extract_url_from_task(task) or "https://www.google.com"
        steps: List[Dict[str, Any]] = []
        self._action_history = []

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page = self._get_active_page(page)
            await self._wait_for_page_ready(page)
            if self._is_read_only_task(task):
                initial_data = await self._maybe_extract_data(self._get_active_surface(page))
                if initial_data:
                    return {
                        "success": True,
                        "message": "read-only task satisfied from initial page",
                        "url": page.url,
                        "title": await page.title(),
                        "steps": steps,
                        "data": initial_data,
                    }

            for step_no in range(1, max_steps + 1):
                page = self._get_active_page(page)
                surface = self._get_active_surface(page)
                elements = await self._extract_interactive_elements(surface)
                action = self._decide_action_locally(task, page, elements)
                if action is None:
                    action = await self._decide_action_with_llm(task, page, elements)

                if action.action_type == ActionType.DONE:
                    data = await self._maybe_extract_data(surface)
                    log_success("browser task completed")
                    return {
                        "success": True,
                        "message": "task completed",
                        "url": page.url,
                        "title": await page.title(),
                        "steps": steps,
                        "data": data,
                    }

                if action.action_type == ActionType.EXTRACT:
                    data = await self._maybe_extract_data(surface)
                    return {
                        "success": True,
                        "message": "data extracted",
                        "url": page.url,
                        "title": await page.title(),
                        "steps": steps,
                        "data": data,
                    }

                if action.requires_confirmation and settings.REQUIRE_HUMAN_CONFIRM:
                    return {
                        "success": False,
                        "message": "action requires human confirmation",
                        "requires_confirmation": True,
                        "steps": steps,
                    }

                if self._is_action_looping(action):
                    if self._is_read_only_task(task):
                        return {
                            "success": True,
                            "message": "repeated action avoided; extracted current page",
                            "url": page.url,
                            "title": await page.title(),
                            "steps": steps,
                            "data": await self._maybe_extract_data(surface),
                        }
                    return {
                        "success": False,
                        "message": f"repeated action loop detected at step {step_no}",
                        "url": page.url,
                        "title": await page.title(),
                        "steps": steps,
                    }
                self._record_action(action)

                before = await self._snapshot_page_state(page)
                success = await self._execute_action(page, action)
                if success:
                    page = self._get_active_page(page)
                    await self._wait_for_page_ready(self._get_active_surface(page))
                    success = await self._verify_action_effect(page, before, action)
                if not success:
                    recovery = self._recover_action(task, action, elements)
                    if recovery:
                        recovery_before = await self._snapshot_page_state(page)
                        success = await self._execute_action(page, recovery)
                        if success:
                            page = self._get_active_page(page)
                            await self._wait_for_page_ready(self._get_active_surface(page))
                            success = await self._verify_action_effect(page, recovery_before, recovery)
                        if success:
                            action = recovery
                            self._record_action(action)

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
                    "url": page.url,
                })

                if not success:
                    if action.action_type == ActionType.WAIT:
                        continue
                    return {
                        "success": False,
                        "message": f"action failed at step {step_no}",
                        "url": page.url,
                        "title": await page.title(),
                        "steps": steps,
                    }

                if action.action_type in {ActionType.CLICK, ActionType.INPUT, ActionType.FILL_FORM, ActionType.PRESS_KEY}:
                    if self._task_looks_satisfied(task, page.url):
                        return {
                            "success": True,
                            "message": "task reached target page",
                            "url": page.url,
                            "title": await page.title(),
                            "steps": steps,
                            "data": await self._maybe_extract_data(self._get_active_surface(page)),
                        }

            return {
                "success": False,
                "message": "max steps reached before task completion",
                "url": page.url,
                "title": await page.title(),
                "steps": steps,
                "data": await self._maybe_extract_data(self._get_active_surface(page)),
            }
        except Exception as exc:
            log_error(f"browser task failed: {exc}")
            return {
                "success": False,
                "message": str(exc),
                "url": getattr(page, "url", ""),
                "steps": steps,
            }


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
