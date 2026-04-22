"""
BrowserExecutionLayer - Executes browser actions via toolkit. Pure side effects.

This is Layer 3 of the three-layer BrowserAgent architecture.
Responsibilities:
- Click, input, navigate, scroll execution with fallback strategies
- Form filling
- Search engine interaction (performing searches, waiting for results)
- Data extraction (DOM-based, using toolkit)
- Anti-robot handling during execution
- Action verification (did the action actually change the page?)
"""
import asyncio
import hashlib
import json
import time
from typing import Any, Dict, List, Optional, Tuple

from config.settings import settings
from utils.browser_toolkit import BrowserToolkit, ToolkitResult
from utils.logger import log_agent_action, log_error, log_warning
from utils.site_knowledge_store import normalize_domain
from utils.search_engine_profiles import (
    build_direct_search_urls,
    decode_search_redirect_url,
    find_search_engine_profile,
    get_search_result_selectors,
    is_search_engine_domain,
    validate_selectors,
)
import utils.web_debug_recorder as web_debug_recorder

from agents.browser_agent import (
    ActionType,
    BrowserAction,
    PageElement,
    TaskIntent,
    _NON_TEXT_INPUT_TYPES,
)


class BrowserExecutionLayer:
    """Executes browser actions via toolkit.

    This layer encapsulates all side-effect-producing browser operations:
    clicking, typing, navigating, scrolling, form filling, data extraction,
    and search execution. It delegates to BrowserToolkit for all low-level
    browser operations.
    """

    def __init__(
        self,
        toolkit: BrowserToolkit,
        agent_name: str = "BrowserAgent",
        perception=None,
    ):
        self.toolkit = toolkit
        self.name = agent_name
        self.perception = perception  # Optional[BrowserPerceptionLayer]

        # Element cache for fallback strategies
        self._element_cache: List[PageElement] = []

    @property
    def element_cache(self) -> List[PageElement]:
        return self._element_cache

    @element_cache.setter
    def element_cache(self, value: List[PageElement]):
        self._element_cache = value

    # ── toolkit call helper ──────────────────────────────────

    async def _call_toolkit(self, method_name: str, *args: Any, **kwargs: Any) -> ToolkitResult:
        method = getattr(self.toolkit, method_name, None)
        if not callable(method):
            return ToolkitResult(success=False, error=f"{method_name} unavailable")
        try:
            result = await method(*args, **kwargs)
        except Exception as exc:
            return ToolkitResult(success=False, error=str(exc))
        if isinstance(result, ToolkitResult):
            return result
        return ToolkitResult(success=True, data=result)

    # ── element cache helpers ────────────────────────────────

    def _get_cached_element_by_selector(self, selector: str) -> Optional[PageElement]:
        for element in self._element_cache:
            if element.selector == selector:
                return element
        return None

    def _get_cached_element_by_ref(self, ref: str) -> Optional[PageElement]:
        for element in self._element_cache:
            if element.ref == ref:
                return element
        return None

    # ── B1/B5 strategy-stats + site-hints helpers ────────────

    @staticmethod
    def _canonical_strategy_name(name: str) -> str:
        """Collapse dynamic strategy labels (e.g. "role:button:Submit") to a
        stable key ("role") used as the bucket for B5 success-rate stats."""
        base = (name or "").split(":", 1)[0]
        return base or name or ""

    async def _current_domain(self) -> str:
        """Resolve the registered domain of the current page, or empty."""
        try:
            r = await self._call_toolkit("get_current_url")
        except Exception:
            return ""
        if not r.success:
            return ""
        return normalize_domain(str(r.data or ""))

    def _load_site_hint_selectors(self, role: str, domain: str) -> List[str]:
        """Return selectors proven to work on this (domain, role) — empty
        when the feature is off or the store has nothing."""
        if (
            not domain
            or not settings.BROWSER_PLAN_MEMORY_ENABLED
            or not settings.BROWSER_SITE_HINTS_EXEC_INJECT
        ):
            return []
        try:
            from utils.site_knowledge_store import get_site_knowledge_store
            store = get_site_knowledge_store()
            if store is None:
                return []
            hints = store.get_selector_hints(domain, role=role)
        except Exception:
            return []
        out: List[str] = []
        for h in hints:
            sel = str(h.get("selector") or "").strip()
            if sel:
                out.append(sel)
        return out

    def _reorder_strategies(
        self,
        strategies: List[Tuple[str, Any]],
        domain: str,
        role: str,
    ) -> List[Tuple[str, Any]]:
        """B5: apply ranked/skip hints from strategy_stats.

        - ``site_hint`` entries are always pinned to the front (site-specific
          evidence outranks generic ranking).
        - Entries whose canonical name is in ``skip_strategies`` are dropped.
        - Remaining entries whose canonical name is in ``ranked_strategies``
          are promoted (in ranked order); others keep their original order.
        """
        if not settings.BROWSER_STRATEGY_LEARNING_ENABLED or not domain:
            return strategies
        try:
            from utils.strategy_stats import get_strategy_stats_store
            store = get_strategy_stats_store()
            if store is None:
                return strategies
            ranked = store.ranked_strategies(domain, role)
            skip = store.skip_strategies(domain, role)
        except Exception:
            return strategies
        if not ranked and not skip:
            return strategies
        ranked_pos = {name: i for i, name in enumerate(ranked)}

        pinned: List[Tuple[str, Any]] = []
        ranked_bucket: List[Tuple[int, Tuple[str, Any]]] = []
        rest: List[Tuple[str, Any]] = []
        for s in strategies:
            canonical = self._canonical_strategy_name(s[0])
            if canonical == "site_hint":
                pinned.append(s)
                continue
            if canonical in skip:
                continue
            if canonical in ranked_pos:
                ranked_bucket.append((ranked_pos[canonical], s))
            else:
                rest.append(s)
        ranked_bucket.sort(key=lambda t: t[0])
        return pinned + [s for _, s in ranked_bucket] + rest

    def _record_strategy_outcome(
        self, domain: str, role: str, canonical: str, success: bool, latency_ms: int
    ) -> None:
        if not domain or not settings.BROWSER_STRATEGY_LEARNING_ENABLED:
            return
        try:
            from utils.strategy_stats import get_strategy_stats_store
            store = get_strategy_stats_store()
            if store is None:
                return
            store.record(domain, role, canonical, success=success, latency_ms=latency_ms)
        except Exception:
            pass

    def _record_site_hint_outcome(
        self, domain: str, role: str, selector: str, success: bool
    ) -> None:
        if not domain or not selector or not settings.BROWSER_PLAN_MEMORY_ENABLED:
            return
        try:
            from utils.site_knowledge_store import get_site_knowledge_store
            store = get_site_knowledge_store()
            if store is None:
                return
            if success:
                store.record_selector_success(domain, role, selector)
            else:
                store.record_selector_failure(domain, role, selector)
        except Exception:
            pass

    # ── B4 iframe auto-scan ──────────────────────────────────

    async def _iframe_auto_scan_click(
        self, selector: str, action: Optional[BrowserAction]
    ) -> bool:
        """Try the click selector inside each non-main iframe, in order.

        Leaves the toolkit switched into the frame on success; restores main
        frame when none of them match. No-op unless
        ``BROWSER_IFRAME_AUTO_SCAN_ON_STUCK`` is enabled and a selector is
        present (text/role-only clicks would need richer heuristics).
        """
        if not settings.BROWSER_IFRAME_AUTO_SCAN_ON_STUCK or not selector:
            return False
        tk = self.toolkit
        if getattr(tk, "_in_iframe", False):
            # already inside an iframe; auto-scan only kicks in from main frame
            return False
        try:
            frames_r = await self._call_toolkit("list_frames", include_main=False)
        except Exception:
            return False
        if not frames_r.success or not isinstance(frames_r.data, list):
            return False
        children = [
            f for f in frames_r.data
            if isinstance(f, dict) and not f.get("is_main") and not f.get("is_detached")
        ]
        if not children:
            return False
        for frame in children:
            name = str(frame.get("name") or "").strip()
            url = str(frame.get("url") or "").strip()
            # Prefer a name-based selector; fall back to an url-matching one.
            frame_selector = ""
            if name:
                frame_selector = f'iframe[name="{name}"]'
            elif url:
                frame_selector = f'iframe[src*="{url[-60:]}"]' if len(url) >= 8 else ""
            if not frame_selector:
                continue
            try:
                switch_r = await tk.switch_to_iframe(frame_selector)
            except Exception:
                switch_r = ToolkitResult(success=False, error="switch exception")
            if not switch_r.success:
                continue
            try:
                click_r = await tk.click(selector)
            except Exception:
                click_r = ToolkitResult(success=False, error="click exception")
            if click_r.success:
                return True
            # did not match inside this frame — restore main frame before next iter
            try:
                await tk.exit_iframe()
            except Exception:
                pass
        return False

    async def _iframe_auto_scan_input(
        self, selector: str, value: str, action: Optional[BrowserAction]
    ) -> bool:
        if not settings.BROWSER_IFRAME_AUTO_SCAN_ON_STUCK or not selector:
            return False
        tk = self.toolkit
        if getattr(tk, "_in_iframe", False):
            return False
        try:
            frames_r = await self._call_toolkit("list_frames", include_main=False)
        except Exception:
            return False
        if not frames_r.success or not isinstance(frames_r.data, list):
            return False
        children = [
            f for f in frames_r.data
            if isinstance(f, dict) and not f.get("is_main") and not f.get("is_detached")
        ]
        if not children:
            return False
        for frame in children:
            name = str(frame.get("name") or "").strip()
            url = str(frame.get("url") or "").strip()
            if name:
                frame_selector = f'iframe[name="{name}"]'
            elif url and len(url) >= 8:
                frame_selector = f'iframe[src*="{url[-60:]}"]'
            else:
                continue
            try:
                switch_r = await tk.switch_to_iframe(frame_selector)
            except Exception:
                switch_r = ToolkitResult(success=False, error="switch exception")
            if not switch_r.success:
                continue
            try:
                input_r = await tk.input_text(selector, value)
            except Exception:
                input_r = ToolkitResult(success=False, error="input exception")
            if input_r.success:
                return True
            try:
                await tk.exit_iframe()
            except Exception:
                pass
        return False

    # ── Click with fallbacks ─────────────────────────────────

    async def try_click_with_fallbacks(self, selector: str, action: Optional[BrowserAction] = None) -> bool:
        tk = self.toolkit
        strategies: List[Tuple[str, Any]] = []
        if action and action.target_ref:
            strategies.append((f"ref:{action.target_ref}", lambda r=action.target_ref: tk.click_ref(r)))
        if selector:
            strategies.append(("direct_click", lambda: tk.click(selector)))
        element = self._get_cached_element_by_ref(action.target_ref) if action and action.target_ref else None
        if element is None:
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

        # B1: prepend site_hint selectors harvested from prior successes.
        # B5: reorder & skip according to per-(domain, role) success stats.
        domain = await self._current_domain()
        hint_name_to_selector: Dict[str, str] = {}
        hint_strategies: List[Tuple[str, Any]] = []
        for sel in self._load_site_hint_selectors("click", domain):
            hint_name = f"site_hint:{sel[:40]}"
            hint_strategies.append((hint_name, lambda s=sel: tk.click(s)))
            hint_name_to_selector[hint_name] = sel
        strategies = hint_strategies + strategies
        strategies = self._reorder_strategies(strategies, domain, "click")

        for name, handler in strategies:
            canonical = self._canonical_strategy_name(name)
            t0 = time.monotonic()
            success = False
            try:
                r = await handler()
                if isinstance(r, ToolkitResult):
                    success = bool(r.success)
                else:
                    success = True
            except Exception:
                success = False
            latency_ms = int((time.monotonic() - t0) * 1000)
            self._record_strategy_outcome(domain, "click", canonical, success, latency_ms)
            if canonical == "site_hint":
                self._record_site_hint_outcome(
                    domain, "click", hint_name_to_selector.get(name, ""), success
                )
            if success:
                log_agent_action(self.name, "click", name)
                return True

        # B4 heuristic fallback: if all strategies failed on the main frame
        # and iframe auto-scan is enabled, try the same selector inside each
        # child iframe. Leaves the toolkit in whichever frame succeeded (so
        # the follow-up perception cycle sees it); restores main frame on miss.
        if await self._iframe_auto_scan_click(selector, action):
            log_agent_action(self.name, "click", "iframe_auto_scan")
            return True
        desc = (action.description[:60] if action else selector[:60]) if (action or selector) else "?"
        log_warning(f"try_click: all {len(strategies)} strategies failed — {desc}")
        return False

    # ── Input with fallbacks ─────────────────────────────────

    async def try_input_with_fallbacks(
        self,
        selector: str,
        value: str,
        action: Optional[BrowserAction] = None,
    ) -> bool:
        tk = self.toolkit
        strategies: List[Tuple[str, Any]] = []
        if action and action.target_ref:
            strategies.append((f"ref:{action.target_ref}", lambda r=action.target_ref: tk.input_ref(r, value)))
        if selector:
            strategies.append(("direct_fill", lambda: tk.input_text(selector, value)))
        element = self._get_cached_element_by_ref(action.target_ref) if action and action.target_ref else None
        if element is None and selector:
            element = self._get_cached_element_by_selector(selector)
        if element:
            attrs = element.attributes or {}
            if attrs.get("placeholder"):
                ph = attrs["placeholder"].strip()[:60]
                strategies.append((f"placeholder:{ph}", lambda p=ph: tk.fill_by_placeholder(p, value)))
            for label in [attrs.get("labelText", ""), attrs.get("ariaLabel", "")]:
                if label and label.strip():
                    strategies.append((f"label:{label[:30]}", lambda l=label.strip()[:60]: tk.fill_by_label(l, value)))
        if selector:
            async def _clear_then_type(s=selector):
                await tk.clear_input(s)
                return await tk.type_text(s, value, delay=20)
            strategies.append(("direct_type", _clear_then_type))

        # Focused element strategy
        async def _try_focused_input():
            focused_r = await tk.evaluate_js(
                r"""() => {
                    const el = document.activeElement;
                    if (!el || el === document.body) return null;
                    const tag = el.tagName.toLowerCase();
                    if (!['input', 'textarea'].includes(tag) && el.contentEditable !== 'true') return null;
                    if (el.id) return '#' + el.id;
                    const name = el.getAttribute('name');
                    if (name) return tag + '[name="' + name + '"]';
                    const ph = el.getAttribute('placeholder');
                    if (ph) return tag + '[placeholder="' + ph + '"]';
                    return ':focus';
                }"""
            )
            if focused_r.success and focused_r.data:
                focused_selector = str(focused_r.data)
                fill_r = await tk.input_text(focused_selector, value)
                if isinstance(fill_r, ToolkitResult) and fill_r.success:
                    return fill_r
                await tk.clear_input(focused_selector)
                type_r = await tk.type_text(focused_selector, value, delay=20)
                return type_r
            return ToolkitResult(success=False, error="no focused input")
        strategies.append(("focused_input", _try_focused_input))

        # Keyboard type fallback
        async def _try_keyboard_type():
            return await tk.evaluate_js(
                r"""(text) => {
                    const el = document.activeElement;
                    if (el && el !== document.body && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.contentEditable === 'true')) {
                        el.value = text;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        return true;
                    }
                    return false;
                }""",
                value,
            )
        strategies.append(("keyboard_type_js", _try_keyboard_type))

        # B1: prepend site_hint selectors; B5: reorder/skip by success stats.
        domain = await self._current_domain()
        hint_name_to_selector: Dict[str, str] = {}
        hint_strategies: List[Tuple[str, Any]] = []
        for sel in self._load_site_hint_selectors("input", domain):
            hint_name = f"site_hint:{sel[:40]}"
            hint_strategies.append((hint_name, lambda s=sel: tk.input_text(s, value)))
            hint_name_to_selector[hint_name] = sel
        strategies = hint_strategies + strategies
        strategies = self._reorder_strategies(strategies, domain, "input")

        for name, handler in strategies:
            canonical = self._canonical_strategy_name(name)
            t0 = time.monotonic()
            success = False
            try:
                r = await handler()
                if isinstance(r, ToolkitResult) and r.success:
                    if canonical == "keyboard_type_js" and r.data is False:
                        success = False
                    else:
                        success = True
            except Exception:
                success = False
            latency_ms = int((time.monotonic() - t0) * 1000)
            self._record_strategy_outcome(domain, "input", canonical, success, latency_ms)
            if canonical == "site_hint":
                self._record_site_hint_outcome(
                    domain, "input", hint_name_to_selector.get(name, ""), success
                )
            if success:
                log_agent_action(self.name, "input", name)
                return True

        # B4 heuristic fallback: try the input selector inside each child iframe.
        if await self._iframe_auto_scan_input(selector, value, action):
            log_agent_action(self.name, "input", "iframe_auto_scan")
            return True
        return False

    # ── Form filling ─────────────────────────────────────────

    async def fill_form(self, form_data_json: str, fallback_selector: str = "") -> bool:
        try:
            form_data = json.loads(form_data_json) if isinstance(form_data_json, str) else form_data_json
        except json.JSONDecodeError:
            form_data = None
        # 兼容模型返回纯字符串（非 JSON）的情况：
        # 如果有 target_selector，降级为单字段输入，而不是直接报错。
        # 这样无论模型聪明与否，fill_form 都能正确执行。
        if not isinstance(form_data, dict) or not form_data:
            raw_value = str(form_data_json or "").strip() if not isinstance(form_data, dict) else ""
            if raw_value and fallback_selector:
                return await self.try_input_with_fallbacks(fallback_selector, raw_value)
            if raw_value:
                log_warning(f"fill_form: 非结构化 value 且无 selector，无法执行: {raw_value[:80]}")
            return False

        tk = self.toolkit
        success_count = 0
        for field_key, value in form_data.items():
            selector = field_key
            if field_key.startswith("el_") or field_key.startswith("ctl_") or ":" in field_key:
                bare_ref = field_key.split(":", 1)[-1] if ":" in field_key else field_key
                ref_info = tk.resolve_ref(field_key) or tk.resolve_ref(bare_ref)
                if ref_info and ref_info.get("selector"):
                    selector = ref_info["selector"]

            try:
                exists = await tk.element_exists(selector)
                if exists.data:
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
                        if not await self.try_input_with_fallbacks(selector, str(value)):
                            await tk.input_text(selector, str(value))
                else:
                    if not await self.try_input_with_fallbacks(selector, str(value)):
                        continue
                success_count += 1
                await tk.human_delay(50, 120)
            except Exception as exc:
                log_warning(f"fill field failed for {selector}: {exc}")
        return success_count > 0

    # ── Execute action ───────────────────────────────────────

    async def execute(self, action: BrowserAction, invalidate_cache_fn=None) -> bool:
        """Execute a single browser action.

        Args:
            action: The action to execute.
            invalidate_cache_fn: Optional callback to invalidate perception cache
                                 on state-changing actions.
        """
        # Invalidate cache on state-changing actions
        if action.action_type in {ActionType.CLICK, ActionType.INPUT, ActionType.NAVIGATE,
                                   ActionType.PRESS_KEY, ActionType.FILL_FORM, ActionType.SELECT}:
            if invalidate_cache_fn:
                invalidate_cache_fn()

        tk = self.toolkit
        if action.action_type == ActionType.CLICK:
            return await self.try_click_with_fallbacks(action.target_selector, action)
        if action.action_type == ActionType.INPUT:
            success = await self.try_input_with_fallbacks(action.target_selector, action.value, action)
            if success and action.use_keyboard_fallback and action.keyboard_key:
                await tk.press_key(action.keyboard_key)
            return success
        if action.action_type == ActionType.FILL_FORM:
            return await self.fill_form(action.value, fallback_selector=action.target_selector or "")
        if action.action_type == ActionType.SELECT:
            r = await tk.select_option(action.target_selector, action.value)
            return r.success
        if action.action_type == ActionType.SCROLL:
            r = await tk.scroll_down(int(action.value or 800))
            log_agent_action(self.name, "scroll", f"px={action.value or 800}")
            return r.success
        if action.action_type == ActionType.WAIT:
            raw_value = float(action.value or 1)
            # LLM 常误把 value 按毫秒给（如 5000 表示 5 秒），统一折算
            if raw_value > settings.BROWSER_MAX_WAIT_SEC * 10:
                raw_value = raw_value / 1000.0
            wait_sec = min(max(raw_value, 0.2), float(settings.BROWSER_MAX_WAIT_SEC))
            log_agent_action(self.name, "wait", f"{wait_sec:.1f}s")
            await asyncio.sleep(wait_sec)
            return True
        if action.action_type == ActionType.NAVIGATE:
            log_agent_action(self.name, "navigate", (action.value or "")[:80])
            await tk.exit_iframe()
            r = await tk.goto(action.value, timeout=20000)
            return r.success
        if action.action_type == ActionType.PRESS_KEY:
            key = action.value or action.keyboard_key or "Enter"
            log_agent_action(self.name, "press_key", key)
            r = await tk.press_key(key)
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
            return await self.try_click_with_fallbacks(action.target_selector, action)
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

    # ── Data extraction ──────────────────────────────────────

    async def extract_data(
        self,
        *,
        prefer_content: bool = False,
        prefer_links: bool = False,
    ) -> List[Dict[str, str]]:
        """Extract structured data from the current page via DOM evaluation."""
        mode = "auto"
        if prefer_content:
            mode = "content"
        elif prefer_links:
            mode = "links"

        current_url_r = await self.toolkit.get_current_url()
        current_url = current_url_r.data or ""
        on_search_engine = is_search_engine_domain(current_url)

        r = await self.toolkit.evaluate_js(
            r"""
            (payload) => {
              const mode = payload.mode;
              const onSearchEngine = payload.onSearchEngine;
              const normalize = (value) => (value || '').replace(/\s+/g, ' ').trim();
              const isVisible = (element) => {
                if (!element) return false;
                const style = window.getComputedStyle(element);
                if (!style || style.visibility === 'hidden' || style.display === 'none') return false;
                const rect = element.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
              };
              const isExcluded = (element) => Boolean(
                element.closest('nav, header, footer, aside, form, [role="navigation"], [role="banner"], [role="contentinfo"], script, style, noscript')
              );
              const dedupe = (items, keyFactory) => {
                const seen = new Set();
                const result = [];
                for (const item of items) {
                  const key = keyFactory(item);
                  if (!key || seen.has(key)) continue;
                  seen.add(key);
                  result.push(item);
                }
                return result;
              };

              // 通用主内容区检测：优先用语义标签，降级到 body
              const mainArea = document.querySelector(
                'main, [role="main"], article, #content, .content, #main'
              ) || document.body;

              const contentSelectors = [
                'li', 'p', 'h1', 'h2', 'h3', 'tr', 'div',
              ].join(', ');

              const denseContent = dedupe(
                Array.from(mainArea.querySelectorAll(contentSelectors))
                  .filter(element => !isExcluded(element) && isVisible(element))
                  .map((element, index) => ({
                    index: index + 1,
                    text: normalize(element.innerText || element.textContent || ''),
                  }))
                  .filter(item => item.text.length >= 4 && item.text.length <= 300),
                (item) => item.text
              ).slice(0, 20);

              const rawBodyText = normalize(mainArea.innerText || '');
              const bodyLines = dedupe(
                rawBodyText
                  .split(/\n+/)
                  .map((text, index) => ({
                    index: index + 1,
                    text: normalize(text),
                  }))
                  .filter(item => item.text.length >= 4 && item.text.length <= 300),
                (item) => item.text
              ).slice(0, 20);

              const merged = dedupe(
                [...denseContent, ...bodyLines],
                (item) => item.text
              ).slice(0, 30);

              // 链接也从主内容区提取，而非整个 document
              const links = dedupe(
                Array.from(mainArea.querySelectorAll('a[href]'))
                  .filter(element => !isExcluded(element) && isVisible(element))
                  .map((element) => ({
                    title: normalize(
                      element.innerText ||
                      element.textContent ||
                      element.getAttribute('aria-label') ||
                      element.getAttribute('title') ||
                      ''
                    ),
                    link: element.href || element.getAttribute('href') || '',
                  }))
                  .filter(item => {
                    if (!item.link || !item.title) return false;
                    if (/^javascript:/i.test(item.link)) return false;
                    if (item.title.length < 4) return false;
                    if (onSearchEngine) {
                      try {
                        const linkHost = new URL(item.link, location.href).hostname.replace(/^www\./, '').toLowerCase();
                        const currentHost = location.hostname.replace(/^www\./, '').toLowerCase();
                        if (linkHost === currentHost) return false;
                      } catch (_e) { /* ignore */ }
                    }
                    return true;
                  }),
                (item) => `${item.title}|${item.link}`
              ).slice(0, 15);

              if (mode === 'content') return merged.length ? merged : links;
              if (mode === 'links') return links.length ? links : merged;
              if (merged.length >= 3) return merged;
              if (links.length) return links;
              return merged;
            }
            """,
            {"mode": mode, "onSearchEngine": on_search_engine},
        )
        return r.data or [] if r.success else []

    async def extract_data_for_intent(self, intent: Optional[TaskIntent] = None) -> List[Dict[str, str]]:
        """Extract data based on task intent (search results or page content)."""
        active_intent = intent or TaskIntent(intent_type="read", query="", confidence=0.0)
        current_url_r = await self.toolkit.get_current_url()
        current_url = current_url_r.data or ""
        if active_intent.intent_type == "search" or is_search_engine_domain(current_url):
            serp_data = await self.extract_search_results_data()
            if serp_data:
                return serp_data
        prefer_links = active_intent.intent_type == "search" or is_search_engine_domain(current_url)
        return await self.extract_data(
            prefer_content=not prefer_links,
            prefer_links=prefer_links,
        )

    async def extract_search_results_data(self) -> List[Dict[str, str]]:
        """Extract search results from a search engine results page."""
        current_url_r = await self.toolkit.get_current_url()
        current_url = current_url_r.data or ""
        if not is_search_engine_domain(current_url):
            return []

        # Check selector health — skip CSS extraction entirely when score is 0
        profile = find_search_engine_profile(current_url)
        _skip_css = False
        if profile:
            try:
                health = await validate_selectors(self.toolkit, profile)
                if health["health_score"] == 0.0:
                    log_warning(
                        f"search selectors all failed [{profile.name}], skipping CSS extraction"
                        f" | failed={health['primary_failed']}"
                        f" | fallback_matched={health['fallback_matched']}"
                    )
                    _skip_css = not health["fallback_matched"]
                elif health["health_score"] < 0.5:
                    log_warning(
                        f"search selector health low [{profile.name}]"
                        f" | score={health['health_score']:.2f}"
                        f" | failed={health['primary_failed']}"
                    )
            except Exception as _exc:
                pass  # health check is advisory only

        # Try cards from semantic snapshot first (handled by orchestrator via perception layer)
        # This method focuses on the JS-based extraction fallback
        cards: list = []
        selectors = get_search_result_selectors(current_url)
        if _skip_css:
            # Health check determined all selectors miss — jump straight to vision fallback
            pass
        else:
            r = await self.toolkit.evaluate_js(
                r"""
            (payload) => {
              const normalize = (value) => (value || '').replace(/\s+/g, ' ').trim();
              const isVisible = (element) => {
                if (!element) return false;
                const style = window.getComputedStyle(element);
                if (!style || style.visibility === 'hidden' || style.display === 'none') return false;
                const rect = element.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
              };
              const cleanHost = (value) => String(value || '').replace(/^www\./, '').toLowerCase();
              const currentHost = cleanHost(location.hostname || '');
              const selectors = Array.isArray(payload?.selectors) && payload.selectors.length
                ? payload.selectors
                : ['main a[href]', 'article a[href]'];
              const decodeParamValue = (value) => {
                let text = normalize(value);
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
              const decodeRedirectHref = (value) => {
                const href = normalize(value);
                if (!href) return '';
                try {
                  const parsed = new URL(href, location.href);
                  const candidates = ['uddg', 'u', 'url', 'q', 'target', 'redirect', 'imgurl']
                    .flatMap((key) => parsed.searchParams.getAll(key))
                    .map((candidate) => decodeParamValue(candidate))
                    .filter(Boolean);
                  return candidates[0] || parsed.toString();
                } catch (_error) {
                  return '';
                }
              };

              const seen = new Set();
              const items = [];
              for (const container of Array.from(document.querySelectorAll(selectors.join(', ')))) {
                if (!isVisible(container)) continue;
                const anchor = container.matches('a[href]')
                  ? container
                  : container.querySelector('h2 a, h3 a, a[href]');
                if (!anchor || !isVisible(anchor)) continue;
                const href = decodeRedirectHref(anchor.href || anchor.getAttribute('href') || '');
                if (!href || /^javascript:/i.test(href)) continue;

                let linkHost = '';
                try {
                  linkHost = cleanHost(new URL(href, location.href).hostname);
                } catch (_error) {
                  continue;
                }
                if (!linkHost || linkHost === currentHost) continue;

                const titleNode = container.querySelector('h2, h3') || anchor;
                const title = normalize(
                  titleNode?.innerText ||
                  titleNode?.textContent ||
                  anchor.getAttribute('aria-label') ||
                  anchor.getAttribute('title') ||
                  ''
                );
                if (title.length < 4) continue;

                const snippetNode = container.querySelector(
                  '.b_caption p, .snippet, .st, .c-abstract, .compText, p, [data-testid="result-snippet"]'
                );
                let text = normalize(snippetNode?.innerText || snippetNode?.textContent || '');
                if (!text) {
                  const raw = normalize(container.innerText || container.textContent || '');
                  text = normalize(raw.replace(title, ''));
                }
                if (text.length > 280) {
                  text = text.slice(0, 280);
                }

                const sourceNode = container.querySelector(
                  'cite, .cite, .b_attribution, .source, .news-source, [data-testid="result-source"]'
                );
                const dateNode = container.querySelector('time, .news-date, .timestamp, .date');
                let dateHint = normalize(dateNode?.innerText || dateNode?.textContent || '');
                if (!dateHint && text) {
                  const match = text.match(
                    /\\b(?:\\d{4}[-/]\\d{1,2}[-/]\\d{1,2}|\\d+\\s+(?:hours?|days?|weeks?|months?)\\s+ago)\\b/i
                  );
                  dateHint = match ? match[0] : '';
                }

                const item = {
                  title,
                  text,
                  link: href,
                  source: normalize(sourceNode?.innerText || sourceNode?.textContent || ''),
                  date: dateHint,
                };
                const key = `${item.title}|${item.link}`;
                if (seen.has(key)) continue;
                seen.add(key);
                items.push(item);
                if (items.length >= 10) break;
              }
              return items;
            }
            """,
            {"selectors": selectors},
            )
            cards = r.data if r.success and isinstance(r.data, list) else []

        # Vision fallback: when all CSS selectors fail, ask the vision model to extract results
        if not cards and self.perception is not None:
            try:
                vision_cards = await self.perception.extract_data_with_vision(
                    task="extract search results",
                    task_intent=TaskIntent(intent_type="search", query="", confidence=0.5),
                )
                if vision_cards:
                    log_warning("CSS selectors failed, fell back to vision extraction")
                    cards = vision_cards
            except Exception as _exc:
                pass  # vision fallback is best-effort

        return cards

    # ── Search bootstrap ─────────────────────────────────────

    async def bootstrap_search_results(self, query: str, wait_ready_fn=None) -> bool:
        """Navigate to a search engine and perform a search.

        Args:
            query: Search query string.
            wait_ready_fn: async callable(search_url) -> bool to wait for results.
        """
        if not query:
            return False
        for profile, search_url in build_direct_search_urls(query):
            result = await self.toolkit.goto(search_url, timeout=30000)
            if not result.success:
                continue
            await self.wait_for_page_ready()
            if wait_ready_fn:
                ready = await wait_ready_fn(search_url)
            else:
                ready = True
            if ready:
                log_agent_action(self.name, f"bootstrap_search:{profile.name}", query[:120])
                return True
        return False

    # ── Page ready / wait helpers ────────────────────────────

    async def wait_for_page_ready(self) -> None:
        tk = self.toolkit
        await tk.wait_for_load("domcontentloaded", timeout=10000)
        # 始终等待 networkidle 以确保 AJAX 数据加载完成
        # fast_mode 下用更短的超时，避免慢站点阻塞
        idle_timeout = 2000 if tk.fast_mode else 3000
        await tk.wait_for_load("networkidle", timeout=idle_timeout)
        # SPA 点击后需要额外等待：路由切换 + API 请求 + DOM 渲染
        # 不用 human_delay 因为 fast_mode 会缩短到不够用
        import asyncio as _asyncio
        await _asyncio.sleep(3.0)

    # ── Action verification ──────────────────────────────────

    async def snapshot_page_state(self, get_snapshot_fn) -> Dict[str, Any]:
        """Capture a lightweight page state for before/after comparison.

        Args:
            get_snapshot_fn: async callable returning the semantic snapshot.
        """
        # ⚡ 优化: 并行获取 url/title/html/snapshot，节省 ~0.3-0.8s
        import asyncio as _asyncio
        url_r, title_r, html_r, semantic_snapshot = await _asyncio.gather(
            self._call_toolkit("get_current_url"),
            self._call_toolkit("get_title"),
            self._call_toolkit("get_page_html"),
            get_snapshot_fn(),
        )
        url = str(url_r.data or "") if url_r.success else ""
        title = str(title_r.data or "") if title_r.success else ""
        html = str(html_r.data or "") if html_r.success else ""
        from agents.browser_perception import BrowserPerceptionLayer
        return {
            "url": url,
            "title": title,
            "content_len": len(html),
            "content_hash": hashlib.sha1(html[:12000].encode("utf-8", errors="ignore")).hexdigest() if html else "",
            "page_type": str(semantic_snapshot.get("page_type", "") or ""),
            "page_stage": str(semantic_snapshot.get("page_stage", "") or ""),
            "card_count": len(semantic_snapshot.get("cards", []) or []),
            "item_count": max(
                len(semantic_snapshot.get("cards", []) or []),
                max(
                    (int(c.get("item_count", 0) or 0) for c in BrowserPerceptionLayer.collections_from_snapshot(semantic_snapshot)),
                    default=0,
                ),
                int((BrowserPerceptionLayer.get_snapshot_affordances(semantic_snapshot) or {}).get("collection_item_count", 0) or 0),
            ),
            "has_modal": bool(BrowserPerceptionLayer.get_snapshot_affordances(semantic_snapshot).get("has_modal")),
            "has_pagination": bool(BrowserPerceptionLayer.get_snapshot_affordances(semantic_snapshot).get("has_pagination")),
            "has_load_more": bool(BrowserPerceptionLayer.get_snapshot_affordances(semantic_snapshot).get("has_load_more")),
            "blocked_signals": BrowserPerceptionLayer.get_snapshot_blocked_signals(semantic_snapshot),
            "main_text_len": len(BrowserPerceptionLayer.get_snapshot_main_text(semantic_snapshot)),
            "visible_text_block_count": len(BrowserPerceptionLayer.get_snapshot_visible_text_blocks(semantic_snapshot)),
        }

    async def verify_action_effect(
        self, before: Dict[str, Any], action: BrowserAction, get_snapshot_fn=None
    ) -> bool:
        """Verify that an action actually changed the page state."""
        if action.action_type not in {
            ActionType.CLICK, ActionType.INPUT, ActionType.SELECT,
            ActionType.NAVIGATE, ActionType.PRESS_KEY, ActionType.FILL_FORM,
            ActionType.SCROLL,
        }:
            return True

        if action.action_type == ActionType.PRESS_KEY and (action.value or action.keyboard_key) in ("Enter", "Return"):
            try:
                await asyncio.sleep(1.5)
                await self.wait_for_page_ready()
            except Exception:
                pass

        after = await self.snapshot_page_state(get_snapshot_fn) if get_snapshot_fn else {}
        result = False

        if action.expected_page_type and after.get("page_type") == action.expected_page_type:
            result = True
        elif action.expected_text:
            text_wait = await self.toolkit.wait_for_text_appear(action.expected_text, timeout=3000)
            if text_wait.success:
                result = True

        if not result:
            if after["url"] != before["url"]:
                result = True
            elif after["title"] != before["title"]:
                result = True
            elif before.get("page_type") and after.get("page_type") and before["page_type"] != after["page_type"]:
                result = True
            elif int(after.get("card_count", 0) or 0) > int(before.get("card_count", 0) or 0):
                result = True
            elif int(after.get("item_count", 0) or 0) > int(before.get("item_count", 0) or 0):
                result = True
            elif bool(before.get("has_modal")) and not bool(after.get("has_modal")):
                result = True
            elif abs(after["content_len"] - before["content_len"]) > 80:
                result = True
            elif after.get("content_hash") and after["content_hash"] != before.get("content_hash"):
                result = True

        if not result:
            if action.action_type == ActionType.INPUT:
                if action.target_ref:
                    ref_info = self.toolkit.resolve_ref(action.target_ref)
                    selector = str(ref_info.get("selector", "") or "")
                else:
                    selector = action.target_selector
                if selector:
                    r = await self.toolkit.get_input_value(selector)
                    if r.success:
                        norm = lambda v: re.sub(r"\s+", " ", (v or "").strip()).lower()
                        result = norm(r.data) == norm(action.value)
            elif action.action_type == ActionType.FILL_FORM:
                result = await self._verify_form_values(action.value)

        web_debug_recorder.write_json(
            "browser_action_verification",
            {
                "before": before,
                "after": after,
                "action": {
                    "action_type": action.action_type.value,
                    "target_selector": action.target_selector,
                    "value": action.value,
                    "description": action.description,
                },
                "result": result,
            },
        )
        return result

    async def _verify_form_values(self, form_payload: str) -> bool:
        try:
            form_data = json.loads(form_payload) if isinstance(form_payload, str) else form_payload
        except Exception:
            return False
        if not isinstance(form_data, dict) or not form_data:
            return False
        matched = 0
        norm = lambda v: re.sub(r"\s+", " ", (v or "").strip()).lower()
        for selector, expected in form_data.items():
            r = await self.toolkit.get_input_value(str(selector))
            if r.success and norm(r.data) == norm(str(expected)):
                matched += 1
        return matched > 0
