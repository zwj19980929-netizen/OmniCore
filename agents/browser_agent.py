"""
OmniCore browser automation agent.
Agent 决策层 — 通过 BrowserToolkit 执行所有浏览器操作。
"""
import asyncio
import hashlib
import json
import os
import random
import re
import time
from urllib.parse import parse_qs, urlparse
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from config.settings import settings
from core.llm import LLMClient
from utils.accessibility_tree_extractor import AccessibilityTreeExtractor, AccessibleElement
from utils.browser_toolkit import BrowserToolkit, ToolkitResult
from utils.enhanced_page_perceiver import EnhancedPagePerceiver, PageContent
from utils.logger import log_agent_action, log_error, log_success, log_warning
from utils.retry import async_retry, is_retryable
from utils.search_engine_profiles import (
    build_direct_search_urls,
    decode_search_redirect_url,
    get_search_result_selectors,
    is_search_engine_domain,
    looks_like_search_results_url,
)
from utils.url_utils import extract_first_url
from utils.web_prompt_budget import BudgetSection, render_budgeted_sections
import utils.web_debug_recorder as web_debug_recorder
from utils.text_relevance import extract_relevant_text_safe_async
from utils.perception_scripts import (
    SCRIPT_FALLBACK_SEMANTIC_SNAPSHOT,
    SCRIPT_EXTRACT_INTERACTIVE_ELEMENTS,
)


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
    ref: str = ""
    role: str = ""
    attributes: Dict[str, str] = field(default_factory=dict)
    is_visible: bool = True
    is_clickable: bool = True
    context_before: str = ""  # 🔥 新增：元素前面的上下文文本
    context_after: str = ""   # 🔥 新增：元素后面的上下文文本
    parent_ref: str = ""
    region: str = ""


@dataclass
class SearchResultCard:
    ref: str
    title: str
    target_ref: str = ""
    target_selector: str = ""
    link: str = ""
    raw_link: str = ""
    target_url: str = ""
    snippet: str = ""
    source: str = ""
    host: str = ""
    date: str = ""
    rank: int = 0


@dataclass
class BrowserAction:
    action_type: ActionType
    target_selector: str = ""
    target_ref: str = ""
    value: str = ""
    description: str = ""
    confidence: float = 0.0
    requires_confirmation: bool = False
    fallback_selector: str = ""
    use_keyboard_fallback: bool = False
    keyboard_key: str = ""
    expected_page_type: str = ""
    expected_text: str = ""


@dataclass
class TaskIntent:
    intent_type: str = "read"
    query: str = ""
    confidence: float = 0.0
    fields: Dict[str, str] = field(default_factory=dict)
    requires_interaction: bool = False
    target_text: str = ""


@dataclass
class PageState:
    page_type: str = "unknown"
    stage: str = "unknown"
    confidence: float = 0.0
    item_count: int = 0
    target_count: int = 0
    has_pagination: bool = False
    has_load_more: bool = False
    has_modal: bool = False
    goal_satisfied: bool = False


@dataclass
class PageObservation:
    """Unified page observation combining all perception sources."""
    snapshot: Dict[str, Any] = field(default_factory=dict)
    a11y_elements: List[Any] = field(default_factory=list)  # AccessibleElement list
    page_content: Optional[Any] = None  # PageContent from perceiver
    vision_description: str = ""
    snapshot_version: int = 0
    timestamp: float = 0.0
    headings: List[Dict[str, str]] = field(default_factory=list)


_QUERY_STOP_TOKENS = frozenset(
    {
        "the", "and", "for", "with", "from", "that", "this", "into", "about",
        "what", "when", "where", "which", "who", "whom", "whose", "how",
        "is", "are", "was", "were", "be", "been", "being", "do", "does", "did",
        "has", "have", "had", "there", "their", "them", "they", "then", "than",
        "will", "would", "could", "should", "can", "may", "might", "must",
        "if", "whether", "after", "before", "during", "within", "last", "past",
        "days", "day", "weeks", "week", "months", "month", "official", "announcement",
        "confirm", "confirmed", "confirmation", "public", "statement",
        "latest", "recent", "today", "news", "article", "articles", "report", "reports",
        "search", "query", "result", "results", "page", "site", "website", "source", "sources",
        "open", "click", "input", "show", "extract", "read", "find", "look", "lookup", "retrieve",
        "browser", "browsers", "task", "tasks", "wait", "waiting", "render", "rendering",
        "load", "loading", "loaded", "fully", "complete", "completed", "display", "summary",
        "summarize", "summarise", "report", "collect", "scrape",
        "current", "recently", "recentest", "verify", "verification", "rumor", "rumors",
        "最近", "最新", "当前", "今天", "新闻", "报道", "文章", "分析", "来源", "网页", "页面", "网站",
        "搜索", "查询", "结果", "打开", "点击", "输入", "提取", "读取", "显示", "获取", "核实", "传闻",
        "浏览器", "任务", "等待", "渲染", "加载", "完成", "完整", "操作", "过程", "步骤", "总结", "收集",
    }
)

_FACT_QUERY_HINTS = (
    "who is",
    "is dead",
    "died",
    "killed",
    "death",
    "alive",
    "whether",
    "是否",
    "是谁",
    "死了",
    "死亡",
    "被炸死",
    "还活着",
    "公开露面",
    "讲话",
    "声明",
)

# Load prompts from files
from utils.prompt_manager import get_prompt
ACTION_DECISION_PROMPT = get_prompt("browser_action_decision")
PAGE_ASSESSMENT_PROMPT = get_prompt("browser_page_assessment")
VISION_ACTION_PROMPT = get_prompt("browser_vision_decision")
UNIFIED_PLAN_PROMPT = get_prompt("browser_unified_plan")
_PAGE_ASSESSMENT_CONTEXT_TOKENS = None  # use settings.PAGE_ASSESSMENT_CONTEXT_TOKENS
_AUTH_USERNAME_ALIASES = (
    "username",
    "user name",
    "login name",
    "login account",
    "account",
    "account name",
    "user id",
    "userid",
    "\u7528\u6237\u540d",
    "\u767b\u5f55\u540d",
    "\u767b\u5f55\u8d26\u53f7",
    "\u8d26\u53f7",
    "\u8d26\u6237",
    "\u5e10\u53f7",
)
_AUTH_EMAIL_ALIASES = (
    "email",
    "e-mail",
    "mail",
    "\u90ae\u7bb1",
    "\u7535\u5b50\u90ae\u7bb1",
    "\u90ae\u4ef6",
)
_AUTH_PASSWORD_ALIASES = (
    "password",
    "passcode",
    "passwd",
    "pwd",
    "\u5bc6\u7801",
    "\u767b\u5f55\u5bc6\u7801",
)
_AUTH_SUBMIT_POSITIVE_TOKENS = (
    "login",
    "log in",
    "sign in",
    "signin",
    "submit",
    "continue",
    "next",
    "enter",
    "\u767b\u5f55",
    "\u767b\u5165",
    "\u63d0\u4ea4",
    "\u7ee7\u7eed",
    "\u786e\u8ba4",
    "\u8fdb\u5165",
)
_AUTH_SUBMIT_NEGATIVE_TOKENS = (
    "register",
    "sign up",
    "signup",
    "forgot",
    "reset",
    "help",
    "cancel",
    "close",
    "back",
    "guest",
    "\u6ce8\u518c",
    "\u5fd8\u8bb0",
    "\u91cd\u7f6e",
    "\u5e2e\u52a9",
    "\u53d6\u6d88",
    "\u5173\u95ed",
    "\u8fd4\u56de",
    "\u6e38\u5ba2",
)
_AUTH_SECONDARY_PROVIDER_TOKENS = (
    "sso",
    "oauth",
    "openid",
    "single sign-on",
    "single sign on",
    "continue with",
    "use another",
    "third-party",
    "third party",
    "unified",
    "enterprise",
    "google",
    "github",
    "microsoft",
    "wechat",
    "\u7b2c\u4e09\u65b9",
    "\u7edf\u4e00\u8ba4\u8bc1",
    "\u7edf\u4e00\u767b\u5f55",
    "\u4f01\u4e1a\u767b\u5f55",
)
_AUTH_VALUE_NOISE_TOKENS = frozenset(
    {
        "login",
        "log",
        "sign",
        "signin",
        "sign in",
        "username",
        "user",
        "password",
        "passcode",
        "passwd",
        "account",
        "email",
        "mail",
        "button",
        "page",
        "open",
        "click",
        "test",
        "report",
        "with",
        "then",
        "primary",
        "\u767b\u5f55",
        "\u767b\u5165",
        "\u7528\u6237\u540d",
        "\u8d26\u53f7",
        "\u8d26\u6237",
        "\u5bc6\u7801",
        "\u6309\u94ae",
        "\u9875\u9762",
        "\u70b9\u51fb",
        "\u6d4b\u8bd5",
        "\u62a5\u544a",
    }
)
_NON_TEXT_INPUT_TYPES = {
    "button",
    "checkbox",
    "color",
    "date",
    "datetime-local",
    "file",
    "hidden",
    "image",
    "month",
    "radio",
    "range",
    "reset",
    "submit",
    "time",
    "week",
}
_STRUCTURED_PAIR_SKIP_KEYS = {"http", "https", "ftp", "www", "localhost"}


@dataclass
class VisionBudget:
    """Per-run token/call budget for vision LLM fallbacks."""
    max_calls_per_run: int = 5
    max_total_tokens: int = 20000
    cooldown_seconds: float = 3.0
    calls_made: int = 0
    tokens_used: int = 0
    last_call_time: float = 0.0

    def can_call(self) -> bool:
        if self.calls_made >= self.max_calls_per_run:
            return False
        if self.tokens_used >= self.max_total_tokens:
            return False
        if time.time() - self.last_call_time < self.cooldown_seconds:
            return False
        return True

    def record_call(self, tokens: int = 0) -> None:
        self.calls_made += 1
        self.tokens_used += tokens
        self.last_call_time = time.time()

    def remaining(self) -> int:
        return max(0, self.max_calls_per_run - self.calls_made)


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
        self._page_assessment_cache: Dict[str, Optional[BrowserAction]] = {}
        self._last_semantic_snapshot: Dict[str, Any] = {}
        self._vision_llm: Optional[LLMClient] = None
        self._vision_llm_attempted = False
        self._vision_llm_unavailable_logged = False
        self._before_action_screenshot: Optional[bytes] = None
        self._vision_budget = VisionBudget(
            max_calls_per_run=settings.VISION_MAX_CALLS_PER_RUN,
            max_total_tokens=settings.VISION_MAX_TOKENS_PER_RUN,
            cooldown_seconds=settings.VISION_COOLDOWN_SECONDS,
        )

        # Perception subsystems
        self.a11y_extractor = AccessibilityTreeExtractor()
        self.page_perceiver = EnhancedPagePerceiver()

        # Snapshot caching (Phase 3)
        self._snapshot_version: int = 0
        self._last_snapshot_hash: str = ""
        self._last_observation: Optional[PageObservation] = None
        # B6: per-run shared assessment cache, keyed by page_hash. Current
        # observation pipeline reuses ``_last_observation`` within a single
        # strategy; this cache is available to strategies that need to
        # share results across the fall-through chain (e.g. LoginReplay
        # → Legacy hand-off on the same page).
        from agents.page_assessment_cache import PageAssessmentCache
        self._assessment_cache = PageAssessmentCache(max_entries=16)

        # Build or accept toolkit
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
        self._owns_toolkit = toolkit is None

        # ── Three-layer architecture ─────────────────────────
        # Layer 1: Perception - transforms raw browser state into structured observations
        from agents.browser_perception import BrowserPerceptionLayer
        self.perception = BrowserPerceptionLayer(
            toolkit=self.toolkit,
            llm_client_getter=self._get_llm,
            a11y_extractor=self.a11y_extractor,
            page_perceiver=self.page_perceiver,
            agent_name=self.name,
        )

        # Layer 2: Decision - decides next action (as pure as possible, no side effects)
        from agents.browser_decision import BrowserDecisionLayer
        self.decision = BrowserDecisionLayer(
            llm_client_getter=self._get_llm,
            perception=self.perception,
            orchestrator=self,
            agent_name=self.name,
        )

        # Layer 3: Execution - executes browser actions via toolkit (pure side effects)
        from agents.browser_execution import BrowserExecutionLayer
        self.execution = BrowserExecutionLayer(
            toolkit=self.toolkit,
            agent_name=self.name,
            perception=self.perception,
        )

    async def close(self) -> None:
        if self._owns_toolkit:
            await self.toolkit.close()

    # ── Layer sync helpers ───────────────────────────────────
    # Keep the orchestrator's state in sync with the layer objects.
    # The layers own the canonical state; these properties bridge
    # the legacy code that still reads from self._ attributes.

    def _sync_perception_state(self) -> None:
        """Push perception layer state back into orchestrator attributes."""
        self._last_semantic_snapshot = self.perception.last_semantic_snapshot
        self._last_observation = self.perception.last_observation
        self._snapshot_version = self.perception.snapshot_version
        self._vision_llm = self.perception._vision_llm
        self._vision_llm_attempted = self.perception._vision_llm_attempted

    def _sync_decision_state(self) -> None:
        """Push decision layer state back into orchestrator attributes."""
        self._action_history = self.decision._action_history
        self._page_assessment_cache = self.decision._page_assessment_cache
        self._last_semantic_snapshot = self.decision.last_semantic_snapshot
        self._last_observation = self.decision.last_observation

    def _sync_state_to_layers(self) -> None:
        """Push orchestrator state into the layer objects."""
        self.perception._last_semantic_snapshot = self._last_semantic_snapshot
        self.perception._last_observation = self._last_observation
        self.perception._snapshot_version = self._snapshot_version
        self.perception._vision_llm = self._vision_llm
        self.perception._vision_llm_attempted = self._vision_llm_attempted
        self.decision._action_history = self._action_history
        self.decision._page_assessment_cache = self._page_assessment_cache
        self.decision.last_semantic_snapshot = self._last_semantic_snapshot
        self.decision.last_observation = self._last_observation
        self.execution.element_cache = self._element_cache

    def _get_llm(self) -> LLMClient:
        if self.llm is None:
            self.llm = LLMClient()
        return self.llm

    def _elements_to_debug_payload(self, elements: List[PageElement]) -> List[Dict[str, Any]]:
        return self.decision._elements_to_debug_payload(elements)

    def _action_to_debug_payload(self, action: Optional[BrowserAction]) -> Dict[str, Any]:
        return self.decision._action_to_debug_payload(action)

    # ── pure logic helpers (no browser) ──────────────────────

    def _normalize_text(self, value: str) -> str:
        return re.sub(r"\s+", " ", (value or "").strip()).lower()

    def _strip_urls_from_text(self, text: str) -> str:
        raw = str(text or "")
        for match in re.finditer(r"https?://[^\s\u4e00-\u9fff]+", raw, flags=re.IGNORECASE):
            candidate = str(match.group(0) or "")
            if "???" in candidate:
                candidate = candidate.split("???", 1)[0]
            elif candidate.count("?") > 1 and "=" not in candidate and "&" not in candidate:
                candidate = candidate.split("?", 1)[0]
            candidate = candidate.rstrip(
                ".,);]}>\"'?!:" + "\uFF0C\u3002\uFF01\uFF1F\uFF1B\uFF1A\u3001\uFF09\u300B\u300D\u300F"
            )
            if candidate:
                raw = raw.replace(candidate, " ")
        return raw

    def _task_mentions_interaction(self, task: str) -> bool:
        return self.decision._task_mentions_interaction(task)

    @staticmethod
    def _is_search_engine_url(url: str) -> bool:
        return is_search_engine_domain(url or "")

    @staticmethod
    def _urls_look_related(expected_url: str, current_url: str) -> bool:
        if not expected_url:
            return bool(current_url)
        if not current_url:
            return False

        expected = urlparse(expected_url)
        current = urlparse(current_url)
        expected_host = expected.netloc.lower()
        current_host = current.netloc.lower()

        if expected_host.startswith("www."):
            expected_host = expected_host[4:]
        if current_host.startswith("www."):
            current_host = current_host[4:]

        if not expected_host or not current_host:
            return expected_url.rstrip("/") == current_url.rstrip("/")

        return (
            expected_host == current_host
            or expected_host.endswith(f".{current_host}")
            or current_host.endswith(f".{expected_host}")
        )

    @staticmethod
    def _looks_like_blocked_page(url: str, title: str = "") -> bool:
        from agents.browser_decision import BrowserDecisionLayer
        return BrowserDecisionLayer._looks_like_blocked_page(url, title)

    @staticmethod
    def _looks_like_search_results_url(url: str) -> bool:
        from agents.browser_decision import BrowserDecisionLayer
        return BrowserDecisionLayer._looks_like_search_results_url(url)

    def _is_read_only_task(self, task: str, intent: Optional[TaskIntent] = None) -> bool:
        normalized = self._normalize_text(task)
        if not normalized:
            return False
        if intent:
            if intent.requires_interaction:
                return False
            if intent.intent_type in {"search", "form", "auth"}:
                return False
            if intent.target_text:
                return False
            if intent.intent_type == "navigate" and self._task_mentions_interaction(task):
                return False
        if len(self._extract_structured_pairs(task)) >= 2:
            return False
        if self._extract_click_target_text(task):
            return False
        if self._task_mentions_interaction(task):
            return False
        return True

    def _action_signature(self, action: BrowserAction) -> str:
        return self.decision._action_signature(action)

    def _record_action(self, action: BrowserAction) -> None:
        self._sync_state_to_layers()
        self.decision.record_action(action)
        self._sync_decision_state()

    def _format_intent_fields_for_llm(self, fields: Optional[Dict[str, str]]) -> str:
        return self.decision._format_intent_fields_for_llm(fields)

    def _step_action_signature(self, step: Dict[str, Any]) -> str:
        return self.decision._step_action_signature(step)

    def _format_recent_steps_for_llm(self, steps: Optional[List[Dict[str, Any]]], max_items: int = 0) -> str:
        return self.decision._format_recent_steps_for_llm(steps, max_items)

    def _action_requires_direct_target(self, action: BrowserAction) -> bool:
        return self.decision._action_requires_direct_target(action)

    def _recent_failed_action_matches(
        self,
        action: BrowserAction,
        recent_steps: Optional[List[Dict[str, Any]]],
        max_items: int = 2,
    ) -> bool:
        return self.decision._recent_failed_action_matches(action, recent_steps, max_items)

    def _sanitize_planned_action(
        self,
        task: str,
        current_url: str,
        elements: List[PageElement],
        intent: Optional[TaskIntent],
        data: List[Dict[str, str]],
        action: Optional[BrowserAction],
        snapshot: Optional[Dict[str, Any]] = None,
        recent_steps: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[BrowserAction]:
        return self.decision._sanitize_planned_action(
            task, current_url, elements, intent, data, action,
            snapshot=snapshot or self._last_semantic_snapshot,
            recent_steps=recent_steps,
        )

    def _is_action_looping(self, action: BrowserAction, threshold: int = 3) -> bool:
        """Detect if action is stuck in a loop. Delegates to the decision layer."""
        self._sync_state_to_layers()
        return self.decision.is_action_looping(action, threshold)

    def _is_noise_element(self, element: PageElement) -> bool:
        return self.decision._is_noise_element(element)

    def _filter_noise_elements(self, elements: List[PageElement]) -> List[PageElement]:
        filtered = [e for e in elements if not self._is_noise_element(e)]
        return filtered or elements

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

    async def _get_current_url_value(self, fallback: str = "") -> str:
        """Get current URL. Delegates to perception layer (Layer 1)."""
        return await self.perception.get_current_url(fallback)

    async def _get_title_value(self, fallback: str = "") -> str:
        """Get page title. Delegates to perception layer (Layer 1)."""
        return await self.perception.get_title(fallback)

    async def _get_page_html_value(self) -> str:
        """Get page HTML. Delegates to perception layer (Layer 1)."""
        return await self.perception.get_page_html()

    def _get_snapshot_blocked_signals(self, snapshot: Optional[Dict[str, Any]]) -> List[str]:
        """Get blocked signals from snapshot. Delegates to perception layer."""
        return self.perception.get_snapshot_blocked_signals(snapshot)

    def _get_snapshot_visible_text_blocks(self, snapshot: Optional[Dict[str, Any]]) -> List[str]:
        """Get visible text blocks. Delegates to perception layer."""
        return self.perception.get_snapshot_visible_text_blocks(snapshot)

    def _get_snapshot_main_text(self, snapshot: Optional[Dict[str, Any]]) -> str:
        """Get main text. Delegates to perception layer."""
        return self.perception.get_snapshot_main_text(snapshot)

    def _resolve_evidence_urls(
        self, evidence_indexes: List[int], data: List[Dict[str, Any]]
    ) -> List[str]:
        """Map evidence_indexes back to URL fields in data items (for answer_citations)."""
        urls: List[str] = []
        for idx in (evidence_indexes or []):
            if isinstance(idx, int) and 0 <= idx < len(data):
                item = data[idx]
                if isinstance(item, dict):
                    url = item.get("url") or item.get("link") or item.get("source_url") or ""
                    if url and str(url) not in urls:
                        urls.append(str(url))
        return urls[:settings.FINALIZER_MAX_CITATIONS]

    async def _format_snapshot_text_for_llm(self, snapshot: Optional[Dict[str, Any]], max_blocks: int = 0, query: str = "") -> str:
        return await self.decision._format_snapshot_text_for_llm(snapshot, max_blocks, query)

    def _stringify_llm_response(self, response: Any) -> str:
        return self.decision._stringify_llm_response(response)

    async def _build_fallback_semantic_snapshot(self) -> Dict[str, Any]:
        current_url = await self._get_current_url_value()
        title = await self._get_title_value()
        body_text = ""
        visible_text_blocks: List[Dict[str, str]] = []
        fallback_elements: List[Dict[str, Any]] = []
        fallback_controls: List[Dict[str, Any]] = []
        has_modal = False
        has_search_box = False
        search_input_selector = ""
        evaluate_result = await self._call_toolkit(
            "evaluate_js",
            SCRIPT_FALLBACK_SEMANTIC_SNAPSHOT,
        )
        if evaluate_result.success and isinstance(evaluate_result.data, dict):
            body_text = str(evaluate_result.data.get("bodyText", "") or "").strip()
            raw_blocks = evaluate_result.data.get("visibleTextBlocks", []) or []
            if isinstance(raw_blocks, list):
                for item in raw_blocks[:16]:
                    if isinstance(item, dict):
                        text = str(item.get("text", "") or "").strip()
                        if text:
                            visible_text_blocks.append(
                                {
                                    "text": text,
                                    "tag": str(item.get("tag", "") or "").strip(),
                                    "role": str(item.get("role", "") or "").strip(),
                                }
                            )
            raw_elements = evaluate_result.data.get("elements", []) or []
            if isinstance(raw_elements, list):
                fallback_elements = [e for e in raw_elements if isinstance(e, dict)]

            has_modal = bool(evaluate_result.data.get("hasModal", False))
            has_search_box = bool(evaluate_result.data.get("hasSearchBox", False))
            search_input_selector = str(evaluate_result.data.get("searchSelector", "") or "")

            # 如果有焦点元素（如刚打开的搜索弹窗），加入 controls
            focused = evaluate_result.data.get("focusedElement")
            if focused and isinstance(focused, dict):
                focused_tag = str(focused.get("tag", "") or "")
                if focused_tag in ("input", "textarea"):
                    fallback_controls.append({
                        "ref": "ctl_focused_input",
                        "kind": "focused_input",
                        "text": str(focused.get("placeholder", "") or "") or "focused input",
                        "selector": str(focused.get("selector", "") or ""),
                    })
                    # 如果焦点在搜索类输入框上，也标记为 search_input
                    focused_type = str(focused.get("type", "") or "")
                    focused_role = str(focused.get("role", "") or "")
                    if focused_type == "search" or focused_role == "searchbox" or "search" in str(focused.get("placeholder", "") or "").lower():
                        has_search_box = True
                        if not search_input_selector:
                            search_input_selector = str(focused.get("selector", "") or "")

        blocked_signals: List[str] = []
        if self._looks_like_blocked_page(current_url, title):
            blocked_signals.append(title or current_url)
        combined_text = " ".join([title, body_text]).lower()
        for token in ("unusual traffic", "robot check", "captcha", "异常流量", "人机身份验证", "验证码", "安全验证", "请解决以下难题"):
            if token in combined_text and token not in blocked_signals:
                blocked_signals.append(token)
        looks_like_serp = self._looks_like_search_results_url(current_url)
        page_type = "blocked" if blocked_signals else ("serp" if looks_like_serp else "unknown")
        page_stage = "blocked" if blocked_signals else ("selecting_source" if looks_like_serp else ("extracting" if body_text else "unknown"))
        if has_modal:
            page_stage = "dismiss_modal"
        affordances = {
            "has_results": looks_like_serp and (len(body_text) >= 240 or len(visible_text_blocks) >= 2),
            "collection_item_count": 0,
            "has_modal": has_modal,
            "has_pagination": False,
            "has_load_more": False,
            "has_search_box": has_search_box,
            "search_input_selector": search_input_selector,
        }
        return {
            "page_type": page_type,
            "page_stage": page_stage,
            "main_text": body_text,
            "visible_text_blocks": visible_text_blocks,
            "blocked_signals": blocked_signals,
            "cards": [],
            "collections": [],
            "controls": fallback_controls,
            "elements": fallback_elements,
            "affordances": affordances,
            "url": current_url,
            "title": title,
        }

    async def _get_semantic_snapshot(self) -> Dict[str, Any]:
        if hasattr(self.toolkit, "semantic_snapshot"):
            snapshot_r = await self._call_toolkit("semantic_snapshot", max_elements=80, include_cards=True)
            if snapshot_r.success and isinstance(snapshot_r.data, dict):
                self._last_semantic_snapshot = snapshot_r.data
                web_debug_recorder.write_json("browser_semantic_snapshot", self._last_semantic_snapshot)

                if web_debug_recorder.is_enabled():
                    log_warning(f"[DEBUG] ========== 语义快照 ==========")
                    log_warning(f"[DEBUG] 页面类型: {self._last_semantic_snapshot.get('page_type', 'unknown')}")
                    log_warning(f"[DEBUG] 页面阶段: {self._last_semantic_snapshot.get('page_stage', 'unknown')}")
                    log_warning(f"[DEBUG] 元素数量: {len(self._last_semantic_snapshot.get('elements', []))}")
                    log_warning(f"[DEBUG] 卡片数量: {len(self._last_semantic_snapshot.get('cards', []))}")
                    log_warning(f"[DEBUG] 集合数量: {len(self._last_semantic_snapshot.get('collections', []))}")
                    main_text = self._last_semantic_snapshot.get('main_text', '')
                    if main_text:
                        log_warning(f"[DEBUG] 主要文本 (前500字符): {main_text[:500]}...")
                    log_warning(f"[DEBUG] ====================================")

                return self._last_semantic_snapshot
            else:
                log_warning(f"semantic_snapshot 主路径失败: {snapshot_r.error or 'unknown error'}")

        fallback_snapshot = await self._build_fallback_semantic_snapshot()
        self._last_semantic_snapshot = fallback_snapshot or {}
        if self._last_semantic_snapshot:
            web_debug_recorder.write_json("browser_semantic_snapshot", self._last_semantic_snapshot)

            if web_debug_recorder.is_enabled():
                log_warning(f"[DEBUG] ========== 语义快照 (fallback) ==========")
                log_warning(f"[DEBUG] 页面类型: {self._last_semantic_snapshot.get('page_type', 'unknown')}")
                log_warning(f"[DEBUG] 元素数量: {len(self._last_semantic_snapshot.get('elements', []))}")
                log_warning(f"[DEBUG] ====================================")

        return self._last_semantic_snapshot

    # ── Unified observe pipeline ────────────────────────────────

    async def _observe_page(self) -> PageObservation:
        """
        Unified page observation: JS snapshot + a11y tree + perceiver + vision.
        Delegates to the perception layer (Layer 1).
        """
        self._sync_state_to_layers()
        observation = await self.perception.observe(self._get_semantic_snapshot)
        self._sync_perception_state()
        return observation

    def _compute_snapshot_hash(self, snapshot: Dict[str, Any]) -> str:
        """Delegates to perception layer."""
        return self.perception.compute_snapshot_hash(snapshot)

    def _merge_a11y_into_snapshot(
        self, snapshot: Dict[str, Any], a11y_elements: List[AccessibleElement]
    ) -> None:
        """Delegates to perception layer."""
        self.perception.merge_a11y_into_snapshot(snapshot, a11y_elements)

    def _merge_perceiver_content(
        self, snapshot: Dict[str, Any], page_content: PageContent
    ) -> None:
        """Delegates to perception layer."""
        self.perception.merge_perceiver_content(snapshot, page_content)

    def _compute_complexity_score(
        self, snapshot: Dict[str, Any], a11y_elements: List[AccessibleElement]
    ) -> float:
        """Delegates to perception layer."""
        return self.perception.compute_complexity_score(snapshot, a11y_elements)

    async def _get_vision_description(self, page) -> str:
        """Delegates to perception layer."""
        self._sync_state_to_layers()
        result = await self.perception.get_vision_description(page)
        self._sync_perception_state()
        return result

    async def _extract_data_with_vision(
        self,
        task: str,
        task_intent: TaskIntent,
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, str]]:
        """Screenshot -> vision model -> extract structured data. Delegates to perception layer."""
        self._sync_state_to_layers()
        result = await self.perception.extract_data_with_vision(
            task, task_intent, snapshot,
            derive_primary_query_fn=self._derive_primary_query,
        )
        self._sync_perception_state()
        return result

    async def _vision_check_page_relevance(
        self,
        task: str,
        query: str,
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, str]:
        """Screenshot -> vision model -> check page relevance. Delegates to perception layer."""
        if not self._vision_budget.can_call():
            log_warning(
                "vision budget exhausted, skipping page relevance check",
                extra={"remaining": self._vision_budget.remaining()},
            )
            return False, ""
        self._sync_state_to_layers()
        result = await self.perception.check_relevance(task, query, snapshot)
        self._vision_budget.record_call()
        self._sync_perception_state()
        return result

    async def _capture_final_screenshot(self) -> Optional[bytes]:
        """Capture final page screenshot. Delegates to perception layer."""
        return await self.perception.capture_screenshot()

    def _validate_action(
        self, action: Optional[BrowserAction], observation: PageObservation
    ) -> Optional[BrowserAction]:
        """Lightweight validation. Delegates to the decision layer (Layer 2)."""
        return self.decision.validate_action(action, observation)

    def _format_headings_for_llm(self, snapshot: Dict[str, Any]) -> str:
        return self.decision._format_headings_for_llm(snapshot)

    def _format_regions_for_llm(self, snapshot: Dict[str, Any]) -> str:
        return self.decision._format_regions_for_llm(snapshot)

    def _elements_from_snapshot(self, snapshot: Dict[str, Any]) -> List[PageElement]:
        """Convert snapshot to PageElement list. Delegates to perception layer."""
        return self.perception.elements_from_snapshot(snapshot)

    def _cards_from_snapshot(self, snapshot: Optional[Dict[str, Any]]) -> List[SearchResultCard]:
        """Convert snapshot to SearchResultCard list. Delegates to perception layer."""
        return self.perception.cards_from_snapshot(snapshot)

    def _collections_from_snapshot(self, snapshot: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Convert snapshot to collections list. Delegates to perception layer."""
        return self.perception.collections_from_snapshot(snapshot)

    def _get_snapshot_affordances(self, snapshot: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Get snapshot affordances. Delegates to perception layer."""
        return self.perception.get_snapshot_affordances(snapshot)

    def _extract_target_result_count(self, task: str) -> int:
        return self.decision._extract_target_result_count(task)

    def _get_snapshot_item_count(self, snapshot: Optional[Dict[str, Any]]) -> int:
        return self.decision._get_snapshot_item_count(snapshot)

    def _score_element_for_context(self, task: str, element: PageElement) -> float:
        return self.decision._score_element_for_context(task, element)

    def _prioritize_elements(self, task: str, elements: List[PageElement], limit: int = 12) -> List[PageElement]:
        return self.decision._prioritize_elements(task, elements, limit)

    def _choose_llm_element_limit(self, task: str) -> int:
        pair_count = len(self._extract_structured_pairs(task))
        if pair_count >= 2:
            return 12
        if len(self._derive_primary_query(task).split()) >= 8:
            return 10
        return 8

    def _extract_task_tokens(self, task: str) -> List[str]:
        return self.decision._extract_task_tokens(task)

    def _normalize_auth_field_name(self, field_name: str) -> str:
        return self.decision._normalize_auth_field_name(field_name)

    def _clean_auth_candidate_value(self, field_name: str, value: str) -> str:
        return self.decision._clean_auth_candidate_value(field_name, value)

    def _extract_query_tokens(self, query: str) -> List[str]:
        return self.decision._extract_query_tokens(query)

    # ── Unified lightweight relevance scorer (0~1) ──────────────

    @staticmethod
    def _char_ngrams(text: str, n: int = 2) -> set:
        from agents.browser_decision import BrowserDecisionLayer
        return BrowserDecisionLayer._char_ngrams(text, n)

    def _score_text_relevance(self, query: str, text: str) -> float:
        return self.decision._score_text_relevance(query, text)

    def _score_source_authority(self, task: str, host: str, source: str) -> float:
        return self.decision._score_source_authority(task, host, source)

    def _score_search_result_card(self, task: str, query: str, card: SearchResultCard) -> float:
        return self.decision._score_search_result_card(task, query, card)

    def _data_has_substantive_text(self, data: List[Dict[str, str]]) -> bool:
        return self.decision._data_has_substantive_text(data)

    def _search_results_have_answer_evidence(self, query: str, data: List[Dict[str, str]]) -> bool:
        return self.decision._search_results_have_answer_evidence(query, data)

    def _strip_search_instruction_phrases(self, value: str) -> str:
        return self.decision._strip_search_instruction_phrases(value)

    def _refine_search_query(self, task: str, candidate: str = "") -> str:
        return self.decision._refine_search_query(task, candidate)

    def _format_elements_for_llm(self, task: str, elements: List[PageElement], max_items: Optional[int] = None) -> str:
        limit = max_items or self._choose_llm_element_limit(task)
        return self.decision._format_elements_for_llm(task, elements, max_items=limit)

    def _format_data_for_llm(self, data: List[Dict[str, str]], max_items: int = 8) -> str:
        return self.decision._format_data_for_llm(data, max_items)

    def _format_cards_for_llm(self, cards: List[SearchResultCard], max_items: int = 10) -> str:
        return self.decision._format_cards_for_llm(cards, max_items)

    def _format_collections_for_llm(self, snapshot: Optional[Dict[str, Any]], max_items: int = 4) -> str:
        return self.decision._format_collections_for_llm(snapshot, max_items)

    def _format_controls_for_llm(self, snapshot: Optional[Dict[str, Any]], max_items: int = 6) -> str:
        return self.decision._format_controls_for_llm(snapshot, max_items)

    def _format_assessment_elements_for_llm(
        self,
        task: str,
        current_url: str,
        elements: List[PageElement],
        max_items: int = 0,
    ) -> str:
        return self.decision._format_assessment_elements_for_llm(task, current_url, elements, max_items)

    async def _build_budgeted_browser_prompt_context(
        self,
        *,
        task: str,
        current_url: str,
        data: List[Dict[str, str]],
        cards: List[SearchResultCard],
        snapshot: Optional[Dict[str, Any]],
        elements_text: str,
        total_tokens: int,
    ) -> Tuple[Dict[str, str], Dict[str, Dict[str, Any]]]:
        return await self.decision._build_budgeted_browser_prompt_context(
            task=task, current_url=current_url, data=data, cards=cards,
            snapshot=snapshot, elements_text=elements_text, total_tokens=total_tokens,
        )

    def _clone_action(self, action: Optional[BrowserAction]) -> Optional[BrowserAction]:
        return self.decision._clone_action(action)

    def _page_assessment_cache_key(
        self, task: str, current_url: str, title: str,
        intent: Optional[TaskIntent], data: List[Dict[str, str]],
        elements: List[PageElement], last_action: Optional[BrowserAction] = None,
        recent_steps: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        return self.decision._page_assessment_cache_key(
            task, current_url, title, intent, data, elements, last_action, recent_steps,
        )

    def _should_assess_page_with_llm(
        self, task: str, current_url: str, intent: Optional[TaskIntent],
        data: List[Dict[str, str]], elements: List[PageElement],
        last_action: Optional[BrowserAction] = None,
    ) -> bool:
        return self.decision._should_assess_page_with_llm(
            task, current_url, intent, data, elements, last_action,
        )

    async def _assess_page_with_llm(
        self, task: str, current_url: str, title: str,
        elements: List[PageElement], intent: Optional[TaskIntent],
        data: List[Dict[str, str]], last_action: Optional[BrowserAction] = None,
        recent_steps: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[BrowserAction]:
        self._sync_state_to_layers()
        result = await self.decision._assess_page_with_llm(
            task, current_url, title, elements, intent, data, last_action, recent_steps,
        )
        self._sync_decision_state()
        return result

    # ── element extraction (Agent's "eyes", uses toolkit.evaluate_js) ──

    async def _extract_interactive_elements(self) -> List[PageElement]:
        observation = await self._observe_page()
        snapshot = observation.snapshot
        snapshot_elements = self._elements_from_snapshot(snapshot)
        if snapshot_elements:
            elements = self._filter_noise_elements(snapshot_elements)
            self._element_cache = elements[:40]
            return self._element_cache

        r = await self._call_toolkit(
            "evaluate_js",
            SCRIPT_EXTRACT_INTERACTIVE_ELEMENTS,
        )
        raw = r.data if r.success and isinstance(r.data, list) else []
        elements: List[PageElement] = []
        for index, item in enumerate(raw or []):
            if not isinstance(item, dict):
                continue
            selector = str(item.get("selector", "") or "").strip()
            tag = str(item.get("tag", "") or "").strip()
            element_type = str(item.get("element_type", item.get("type", item.get("role", ""))) or "").strip()
            ref = str(item.get("ref", "") or "").strip()
            if not any([selector, tag, element_type, ref]):
                continue
            try:
                elements.append(PageElement(**item))
            except TypeError:
                elements.append(
                    PageElement(
                        index=int(item.get("index", index) or index),
                        tag=tag,
                        text=str(item.get("text", "") or ""),
                        element_type=element_type,
                        selector=selector,
                        ref=ref,
                        role=str(item.get("role", "") or ""),
                        attributes=dict(item.get("attributes", {}) or {}),
                        is_visible=bool(item.get("is_visible", item.get("visible", True))),
                        is_clickable=bool(item.get("is_clickable", item.get("enabled", True))),
                        context_before=str(item.get("context_before", "") or ""),
                        context_after=str(item.get("context_after", "") or ""),
                        parent_ref=str(item.get("parent_ref", "") or ""),
                        region=str(item.get("region", "") or ""),
                    )
                )
        elements = self._filter_noise_elements(elements)
        self._element_cache = elements[:40]
        return self._element_cache

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

    # ── element finding helpers ────────────────────────────────

    def _find_ranked_elements(self, task: str, elements: List[PageElement],
                              kinds: Optional[List[str]] = None, keywords: Optional[List[str]] = None,
                              exclude_selectors: Optional[List[str]] = None) -> List[PageElement]:
        return self.decision._find_ranked_elements(task, elements, kinds=kinds, keywords=keywords, exclude_selectors=exclude_selectors)

    def _find_best_element(self, task: str, elements: List[PageElement],
                           kinds: Optional[List[str]] = None, keywords: Optional[List[str]] = None,
                           exclude_selectors: Optional[List[str]] = None) -> Optional[PageElement]:
        return self.decision._find_best_element(task, elements, kinds=kinds, keywords=keywords, exclude_selectors=exclude_selectors)

    def _derive_primary_query(self, task: str) -> str:
        return self.decision._derive_primary_query(task)

    def _build_form_fill_action(self, mapping: Dict[str, str]) -> BrowserAction:
        return self.decision._build_form_fill_action(mapping)

    def _element_action_haystack(self, element: PageElement) -> str:
        return self.decision._element_action_haystack(element)

    def _extract_click_target_text(self, task: str) -> str:
        return self.decision._extract_click_target_text(task)

    def _extract_url_from_task(self, task: str) -> Optional[str]:
        return self.decision._extract_url_from_task(task)

    def _extract_auth_fields_from_free_text(self, task: str) -> Dict[str, str]:
        return self.decision._extract_auth_fields_from_free_text(task)

    def _extract_structured_pairs(self, task: str) -> Dict[str, str]:
        return self.decision._extract_structured_pairs(task)

    async def _infer_task_intent(self, task: str) -> TaskIntent:
        cache_key = self._normalize_text(task)
        cached = self._intent_cache.get(cache_key)
        if cached is not None:
            return cached

        query = self._derive_primary_query(task)
        fields = self._extract_structured_pairs(task)
        target_text = self._extract_click_target_text(task)
        extracted_url = self._extract_url_from_task(task)
        field_kinds = {self._normalize_auth_field_name(key) for key in fields}
        auth_like = "password" in field_kinds

        if extracted_url:
            if auth_like:
                fallback = TaskIntent(
                    intent_type="auth",
                    query=query,
                    confidence=0.65 if fields else 0.55,
                    fields=fields,
                    requires_interaction=True,
                    target_text=target_text,
                )
            elif len(fields) >= 2:
                fallback = TaskIntent(
                    intent_type="form",
                    query=query,
                    confidence=0.6,
                    fields=fields,
                    requires_interaction=True,
                    target_text=target_text,
                )
            elif target_text or self._task_mentions_interaction(task):
                fallback = TaskIntent(
                    intent_type="navigate",
                    query=query,
                    confidence=0.55,
                    requires_interaction=True,
                    target_text=target_text,
                )
            else:
                fallback = TaskIntent(intent_type="read", query=query, confidence=0.6, target_text=target_text)
        elif auth_like:
            fallback = TaskIntent(
                intent_type="auth",
                query=query,
                confidence=0.65 if fields else 0.45,
                fields=fields,
                requires_interaction=True,
                target_text=target_text,
            )
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
            # 数据收集类任务应偏向 read 意图，而非 search。
            # search 意图的 _task_looks_satisfied 在离开 SERP 后过于依赖 URL 匹配，
            # 导致已到达目标页面的情况无法被正确识别。
            _task_lower = self._normalize_text(task)
            _data_collection_signals = (
                "收集", "获取", "查询", "列出", "对比", "价格", "信息",
                "数据", "查看", "了解", "分析", "统计", "排名", "排行",
                "collect", "find", "get", "list", "compare", "price",
                "info", "data", "check", "look up", "analyze", "rank",
            )
            if any(s in _task_lower for s in _data_collection_signals):
                fallback = TaskIntent(intent_type="read", query=query, confidence=0.45, target_text=target_text)
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
                            "requires_interaction, fields, target_text. "
                            "If intent is search, query must be a short search-engine query, "
                            "not a full sentence or task instructions."
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

            confidence_raw = payload.get("confidence", 0.0) or 0.0
            # 🔥 修复：处理LLM返回字符串confidence的情况
            if isinstance(confidence_raw, str):
                confidence_str = confidence_raw.lower().strip()
                if confidence_str in {"high", "很高", "高"}:
                    confidence = 0.9
                elif confidence_str in {"medium", "中", "中等"}:
                    confidence = 0.6
                elif confidence_str in {"low", "低", "较低"}:
                    confidence = 0.3
                else:
                    try:
                        confidence = float(confidence_raw)
                    except (ValueError, TypeError):
                        confidence = 0.5  # 默认中等置信度
            else:
                try:
                    confidence = float(confidence_raw)
                except (ValueError, TypeError):
                    confidence = 0.5

            llm_query = self._refine_search_query(task, str(payload.get("query", "") or "").strip()) or query
            llm_target = self._normalize_text(str(payload.get("target_text", "") or "").strip())
            llm_fields = payload.get("fields", {})
            normalized_fields: Dict[str, str] = {}
            if isinstance(llm_fields, dict):
                for raw_key, raw_value in llm_fields.items():
                    key = self._normalize_auth_field_name(str(raw_key))
                    value = str(raw_value or "").strip()
                    if key and value:
                        normalized_fields[key] = value
            merged_fields = dict(fields)
            merged_fields.update(normalized_fields)

            llm_intent = TaskIntent(
                intent_type=intent_type,
                query=llm_query,
                confidence=max(min(confidence, 1.0), 0.0),
                fields=merged_fields,
                requires_interaction=bool(payload.get("requires_interaction", False)),
                target_text=llm_target,
            )

            if llm_intent.confidence >= max(fallback.confidence, 0.5):
                fallback = llm_intent
        except Exception as exc:
            log_warning(f"intent inference fallback: {str(exc)[:120]}")

        if not fallback.query:
            fallback.query = query
        fallback.query = self._refine_search_query(task, fallback.query) or fallback.query
        if not fallback.fields and fallback.intent_type in {"form", "auth"}:
            fallback.fields = fields
        if not fallback.target_text:
            fallback.target_text = target_text
        if fallback.intent_type in {"form", "auth"}:
            fallback.requires_interaction = True
        if extracted_url and not fallback.target_text and not fallback.requires_interaction:
            fallback.intent_type = "read"
        if fallback.target_text and fallback.intent_type in {"read", "unknown"}:
            fallback.intent_type = "navigate"
            fallback.requires_interaction = True

        self._intent_cache[cache_key] = fallback
        return fallback

    def _iter_input_candidates(self, elements: List[PageElement]) -> List[PageElement]:
        return self.decision._iter_input_candidates(elements)

    def _field_match_score(self, field_name: str, element: PageElement) -> float:
        return self.decision._field_match_score(field_name, element)

    def _mapping_matches_current_elements(
        self,
        mapping: Dict[str, str],
        elements: List[PageElement],
    ) -> bool:
        return self.decision._mapping_matches_current_elements(mapping, elements)

    def _build_form_mapping_from_pairs(
        self,
        fields: Dict[str, str],
        elements: List[PageElement],
    ) -> Dict[str, str]:
        return self.decision._build_form_mapping_from_pairs(fields, elements)

    def _find_primary_text_input(self, elements: List[PageElement]) -> Optional[PageElement]:
        return self.decision._find_primary_text_input(elements)

    def _find_primary_submit_control(self, elements: List[PageElement]) -> Optional[PageElement]:
        return self.decision._find_primary_submit_control(elements)

    def _find_auth_submit_control(
        self,
        task: str,
        elements: List[PageElement],
        intent: Optional[TaskIntent] = None,
    ) -> Optional[PageElement]:
        return self.decision._find_auth_submit_control(task, elements, intent)

    def _find_submit_control_for_intent(
        self,
        task: str,
        elements: List[PageElement],
        intent: Optional[TaskIntent] = None,
    ) -> Optional[PageElement]:
        return self.decision._find_submit_control_for_intent(task, elements, intent)

    def _interaction_requires_follow_up(
        self,
        task: str,
        intent: Optional[TaskIntent],
        elements: List[PageElement],
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> bool:
        return self.decision._interaction_requires_follow_up(task, intent, elements, snapshot)

    async def _bootstrap_search_results(self, query: str) -> bool:
        """Bootstrap search results. Delegates to execution layer (Layer 3)."""
        return await self.execution.bootstrap_search_results(
            query, wait_ready_fn=self._wait_for_search_results_ready
        )

    def _search_input_matches_query(self, elements: List[PageElement], query: str) -> bool:
        return self.decision._search_input_matches_query(elements, query)

    def _build_snapshot_click_action(
        self, snapshot: Optional[Dict[str, Any]], *,
        ref_key: str, selector_key: str, description: str,
        expected_page_type: str = "", confidence: float = 0.72,
    ) -> Optional[BrowserAction]:
        return self.decision._build_snapshot_click_action(
            snapshot, ref_key=ref_key, selector_key=selector_key,
            description=description, expected_page_type=expected_page_type, confidence=confidence,
        )

    def _choose_modal_action(
        self, task: str, elements: List[PageElement],
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> Optional[BrowserAction]:
        return self.decision._choose_modal_action(task, elements, snapshot)

    def _snapshot_has_actionable_modal(
        self, snapshot: Optional[Dict[str, Any]],
        elements: Optional[List[PageElement]] = None,
    ) -> bool:
        return self.decision._snapshot_has_actionable_modal(snapshot, elements)

    def _infer_page_state(
        self, task: str, current_url: str, intent: Optional[TaskIntent],
        data: List[Dict[str, str]], snapshot: Optional[Dict[str, Any]] = None,
        elements: Optional[List[PageElement]] = None,
    ) -> PageState:
        return self.decision._infer_page_state(task, current_url, intent, data, snapshot, elements)

    def _choose_snapshot_navigation_action(
        self, task: str, current_url: str, elements: List[PageElement],
        intent: Optional[TaskIntent], data: List[Dict[str, str]],
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> Optional[BrowserAction]:
        return self.decision._choose_snapshot_navigation_action(
            task, current_url, elements, intent, data, snapshot,
        )

    def _get_vision_llm(self) -> Optional[LLMClient]:
        """Get vision LLM. Delegates to perception layer (Layer 1)."""
        self._sync_state_to_layers()
        result = self.perception.get_vision_llm()
        self._sync_perception_state()
        return result

    async def _decide_action_with_vision(
        self,
        task: str,
        current_url: str,
        title: str,
        elements: List[PageElement],
        intent: Optional[TaskIntent],
        data: List[Dict[str, str]],
        snapshot: Optional[Dict[str, Any]] = None,
        last_action: Optional[BrowserAction] = None,
    ) -> Optional[BrowserAction]:
        if not self._vision_budget.can_call():
            log_warning(
                "vision budget exhausted, skipping vision fallback",
                extra={"remaining": self._vision_budget.remaining(), "calls_made": self._vision_budget.calls_made},
            )
            return None
        vision_llm = self._get_vision_llm()
        if vision_llm is None:
            return None

        screenshot_r = await self.toolkit.screenshot(full_page=False)
        if not screenshot_r.success or not screenshot_r.data:
            return None

        active_snapshot = snapshot or self._last_semantic_snapshot or {}
        page_state = self._infer_page_state(task, current_url, intent, data, active_snapshot)
        prompt = VISION_ACTION_PROMPT.format(
            task=task or "",
            url=current_url or "",
            title=title or "",
            page_type=page_state.page_type,
            page_stage=page_state.stage,
            last_action=(last_action.description or last_action.action_type.value) if last_action else "none",
            data=self._format_data_for_llm(data),
            cards=self._format_cards_for_llm(self._cards_from_snapshot(active_snapshot)),
            collections=self._format_collections_for_llm(active_snapshot),
            elements=self._format_assessment_elements_for_llm(task, current_url, elements),
        )

        try:
            vision_timeout = settings.VISION_CALL_TIMEOUT / 1000  # ms → seconds
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    vision_llm.chat_with_image,
                    prompt,
                    screenshot_r.data,
                    0.1,
                    1200,
                ),
                timeout=vision_timeout,
            )
            self._vision_budget.record_call(
                tokens=(response.usage or {}).get("total_tokens", 0)
                if hasattr(response, "usage") and response.usage
                else 0
            )
            web_debug_recorder.write_binary("browser_vision_screenshot", screenshot_r.data, ".png")
            web_debug_recorder.write_text("browser_vision_prompt", prompt)
            web_debug_recorder.write_text("browser_vision_response", response.content)
            action = self._action_from_llm(vision_llm.parse_json_response(response), elements)
            web_debug_recorder.write_json(
                "browser_vision_action",
                self._action_to_debug_payload(action),
            )
            if action.action_type in {ActionType.FAILED, ActionType.WAIT}:
                return None
            return action
        except asyncio.TimeoutError:
            log_warning(f"vision fallback timed out after {settings.VISION_CALL_TIMEOUT}ms")
            self._vision_budget.record_call()
            return None
        except Exception as exc:
            log_warning(f"vision fallback failed: {exc}")
            return None

    def _page_data_satisfies_goal(
        self,
        task: str,
        current_url: str,
        intent: Optional[TaskIntent],
        data: List[Dict[str, str]],
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> bool:
        return self.decision._page_data_satisfies_goal(
            task, current_url, intent, data,
            snapshot=snapshot or self._last_semantic_snapshot,
        )

    def _choose_observation_driven_action(
        self, task: str, current_url: str, elements: List[PageElement],
        intent: Optional[TaskIntent], data: List[Dict[str, str]],
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> Optional[BrowserAction]:
        return self.decision._choose_observation_driven_action(
            task, current_url, elements, intent, data, snapshot,
        )

    def _is_data_relevant(self, query: str, data: List[Dict[str, str]]) -> bool:
        return self.decision._is_data_relevant(query, data)

    # ── Agent decision: local heuristics ───────────────────────

    def _find_search_element(self, elements: List[PageElement]) -> Optional[PageElement]:
        return self.decision._find_search_element(elements)

    def _decide_action_locally(
        self, task: str, elements: List[PageElement],
        intent: Optional[TaskIntent] = None,
    ) -> Optional[BrowserAction]:
        return self.decision._decide_action_locally(task, elements, intent)

    # ── Agent decision: LLM ────────────────────────────────────

    def _action_from_llm(self, payload: Dict[str, Any], elements: List[PageElement]) -> BrowserAction:
        return self.decision._action_from_llm(payload, elements)

    async def _decide_action_with_llm(
        self, task: str, elements: List[PageElement],
        intent: Optional[TaskIntent] = None, data: Optional[List[Dict[str, str]]] = None,
        snapshot: Optional[Dict[str, Any]] = None, current_url: str = "", title: str = "",
        last_action: Optional[BrowserAction] = None, recent_steps: Optional[List[Dict[str, Any]]] = None,
    ) -> BrowserAction:
        self._sync_state_to_layers()
        result = await self.decision._decide_action_with_llm(
            task, elements, intent, data, snapshot, current_url, title, last_action, recent_steps,
        )
        self._sync_decision_state()
        return result

    async def _unified_plan_action(
        self, task: str, current_url: str, title: str,
        elements: List[PageElement], intent: Optional[TaskIntent],
        data: List[Dict[str, str]], observation: Optional[PageObservation] = None,
        last_action: Optional[BrowserAction] = None, recent_steps: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[BrowserAction]:
        self._sync_state_to_layers()
        result = await self.decision._unified_plan_action(
            task, current_url, title, elements, intent, data, observation, last_action, recent_steps,
        )
        self._sync_decision_state()
        return result

    async def _plan_next_action(
        self, task: str, current_url: str, title: str,
        elements: List[PageElement], intent: Optional[TaskIntent],
        data: List[Dict[str, str]], snapshot: Optional[Dict[str, Any]] = None,
        last_action: Optional[BrowserAction] = None, recent_steps: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[Optional[BrowserAction], str]:
        self._sync_state_to_layers()
        result = await self.decision._plan_next_action(
            task, current_url, title, elements, intent, data, snapshot, last_action, recent_steps,
        )
        self._sync_decision_state()
        return result

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
                                     target_ref=alternative.ref,
                                     description=f"recovery click {alternative.text[:24]}".strip(),
                                     confidence=max(action.confidence - 0.2, 0.35),
                                     use_keyboard_fallback=action.use_keyboard_fallback, keyboard_key=action.keyboard_key)
        if action.action_type == ActionType.INPUT:
            alternative = self._find_best_element(task, elements, kinds=["input", "search", "textarea", "text"],
                                                  keywords=self._extract_task_tokens(task)[:4],
                                                  exclude_selectors=[action.target_selector])
            if alternative:
                return BrowserAction(action_type=ActionType.INPUT, target_selector=alternative.selector,
                                     target_ref=alternative.ref,
                                     value=action.value, description=f"recovery input {alternative.text[:24]}".strip(),
                                     confidence=max(action.confidence - 0.2, 0.35),
                                     use_keyboard_fallback=action.use_keyboard_fallback, keyboard_key=action.keyboard_key)
        return None

    # ── click/input fallback strategies (Agent-level, calls toolkit) ──

    async def _try_click_with_fallbacks(self, selector: str, action: Optional[BrowserAction] = None) -> bool:
        """Click with fallback strategies. Delegates to execution layer (Layer 3)."""
        self._sync_state_to_layers()
        return await self.execution.try_click_with_fallbacks(selector, action)

    async def _try_input_with_fallbacks(
        self,
        selector: str,
        value: str,
        action: Optional[BrowserAction] = None,
    ) -> bool:
        """Input with fallback strategies. Delegates to execution layer (Layer 3)."""
        self._sync_state_to_layers()
        return await self.execution.try_input_with_fallbacks(selector, value, action)

    async def _fill_form(self, form_data_json: str) -> bool:
        """Fill form fields. Delegates to execution layer (Layer 3)."""
        self._sync_state_to_layers()
        return await self.execution.fill_form(form_data_json)

    # ── thin _execute_action mapping ───────────────────────────

    async def _execute_action(self, action: BrowserAction) -> bool:
        """Execute a browser action. Delegates to the execution layer (Layer 3)."""
        self._sync_state_to_layers()

        def _invalidate_perception_cache():
            self._last_snapshot_hash = ""
            self._last_observation = None
            self.perception.invalidate_cache()

        result = await self.execution.execute(action, invalidate_cache_fn=_invalidate_perception_cache)
        # Sync element cache back
        self._element_cache = self.execution.element_cache
        return result

    # ── verification (delegates to execution layer) ──────────

    async def _snapshot_page_state(self) -> Dict[str, Any]:
        """Capture lightweight page state for before/after comparison. Delegates to execution layer."""
        return await self.execution.snapshot_page_state(self._get_semantic_snapshot)

    def _action_must_change_state(self, action: BrowserAction) -> bool:
        return action.action_type in {
            ActionType.CLICK, ActionType.INPUT, ActionType.SELECT,
            ActionType.NAVIGATE, ActionType.PRESS_KEY, ActionType.FILL_FORM,
            ActionType.SCROLL,
        }

    async def _verify_action_effect(self, before: Dict[str, Any], action: BrowserAction) -> bool:
        """Verify action effect. Delegates to execution layer, with visual fallback."""
        dom_verified = await self.execution.verify_action_effect(before, action, self._get_semantic_snapshot)
        if dom_verified:
            return True
        # DOM diff inconclusive — try visual comparison if enabled
        if settings.VISION_VERIFY_ACTION and self._before_action_screenshot is not None:
            try:
                page = getattr(self.toolkit, '_page', None)
                if page:
                    after_screenshot = await page.screenshot(type="jpeg", quality=50, full_page=False)
                    from utils.image_diff import screenshots_meaningfully_differ
                    if screenshots_meaningfully_differ(self._before_action_screenshot, after_screenshot):
                        log_agent_action(self.name, "verify", "visual_diff_detected")
                        return True
            except Exception:
                pass
        return False

    async def _verify_form_values(self, form_payload: str) -> bool:
        """Verify form values. Delegates to execution layer (Layer 3)."""
        return await self.execution._verify_form_values(form_payload)

    async def _extract_search_results_data(self) -> List[Dict[str, str]]:
        current_url_r = await self.toolkit.get_current_url()
        current_url = current_url_r.data or ""
        if not self._is_search_engine_url(current_url):
            return []

        snapshot = self._last_semantic_snapshot or await self._get_semantic_snapshot()
        cards = self._cards_from_snapshot(snapshot)
        if cards:
            items: List[Dict[str, str]] = []
            seen_links: set[str] = set()
            for card in cards[:10]:
                link = decode_search_redirect_url(card.target_url or card.link or card.raw_link)
                if not link or self._is_search_engine_url(link) or link in seen_links:
                    continue
                seen_links.add(link)
                items.append(
                    {
                        "title": card.title,
                        "text": card.snippet,
                        "link": link,
                        "source": card.source,
                        "date": card.date,
                    }
                )
            if items:
                return items

        selectors = get_search_result_selectors(current_url)
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
        return r.data if r.success and isinstance(r.data, list) else []

    # ── data extraction ────────────────────────────────────────

    async def _maybe_extract_data_legacy(
        self,
        *,
        prefer_content: bool = False,
        prefer_links: bool = False,
    ) -> List[Dict[str, str]]:
        mode = "auto"
        if prefer_content:
            mode = "content"
        elif prefer_links:
            mode = "links"

        r = await self.toolkit.evaluate_js(
            r"""
            (mode) => {
              const normalize = (value) => (value || '').replace(/\s+/g, ' ').trim();
              const isVisible = (element) => {
                if (!element) return false;
                const style = window.getComputedStyle(element);
                if (!style || style.visibility === 'hidden' || style.display === 'none') return false;
                const rect = element.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
              };
              const isExcluded = (element) => Boolean(
                element.closest('nav, header, footer, aside, form, [role="navigation"], script, style, noscript')
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

              const contentSelectors = [
                'main li', 'article li', '[role="main"] li', 'section li',
                '#7d li', '#15d li', '.forecast li', '.weather li',
                'main p', 'article p', '[role="main"] p', 'section p',
                'main h1', 'article h1', 'main h2', 'article h2',
                'main h3', 'article h3', 'tr',
                'main div', 'article div', '[role="main"] div', 'section div',
                '.today div', '.forecast div', '.weather div',
                '[class*="weather"] div', '[class*="temp"] div',
                '[class*="detail"] div', '[class*="info"] div',
              ].join(', ');

              const denseContent = dedupe(
                Array.from(document.querySelectorAll(contentSelectors))
                  .filter(element => !isExcluded(element) && isVisible(element))
                  .map((element, index) => ({
                    index: index + 1,
                    text: normalize(element.innerText || element.textContent || ''),
                  }))
                  .filter(item => item.text.length >= 4 && item.text.length <= 240),
                (item) => item.text
              ).slice(0, 12);

              const bodyText = normalize(document.body ? document.body.innerText : '');
              const bodyLines = dedupe(
                bodyText
                  .split(/\n+/)
                  .map((text, index) => ({
                    index: index + 1,
                    text: normalize(text),
                  }))
                  .filter(item => item.text.length >= 8 && item.text.length <= 240),
                (item) => item.text
              ).slice(0, 12);

              const contentBlocks = denseContent.length >= 3 ? denseContent : bodyLines;

              const links = dedupe(
                Array.from(document.querySelectorAll('a[href]'))
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
                  .filter(item => (
                    item.link &&
                    item.title &&
                    !/^javascript:/i.test(item.link) &&
                    item.title.length >= 2
                  )),
                (item) => `${item.title}|${item.link}`
              ).slice(0, 10);

              if (mode === 'content') return contentBlocks.length ? contentBlocks : links;
              if (mode === 'links') return links.length ? links : contentBlocks;
              if (contentBlocks.length >= 4) return contentBlocks;
              if (links.length) return links;
              return contentBlocks;
            }
            """,
            mode,
        )
        return r.data or [] if r.success else []

    async def _extract_data_for_intent(self, intent: Optional[TaskIntent] = None) -> List[Dict[str, str]]:
        """Extract data based on task intent. Delegates to execution layer (Layer 3),
        but falls back to snapshot-based search results first."""
        active_intent = intent or TaskIntent(intent_type="read", query="", confidence=0.0)
        current_url_r = await self.toolkit.get_current_url()
        current_url = current_url_r.data or ""
        if active_intent.intent_type == "search" or self._is_search_engine_url(current_url):
            serp_data = await self._extract_search_results_data()
            if serp_data:
                return serp_data
        prefer_links = active_intent.intent_type == "search" or self._is_search_engine_url(current_url)
        return await self.execution.extract_data(
            prefer_content=not prefer_links,
            prefer_links=prefer_links,
        )

    def _task_requires_detail_page(self, task: str, intent: Optional[TaskIntent] = None) -> bool:
        return self.decision._task_requires_detail_page(task, intent)

    def _find_search_result_click_action(
        self,
        task: str,
        current_url: str,
        elements: List[PageElement],
        intent: Optional[TaskIntent] = None,
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> Optional[BrowserAction]:
        return self.decision._find_search_result_click_action(
            task, current_url, elements, intent=intent,
            snapshot=snapshot or self._last_semantic_snapshot,
        )

    def _coerce_intent_for_direct_page(
        self,
        task: str,
        intent: TaskIntent,
        target_url: str = "",
    ) -> TaskIntent:
        if not target_url:
            return intent
        if intent.intent_type in {"form", "auth"}:
            return intent
        if intent.target_text or intent.requires_interaction:
            return intent
        if self._task_mentions_interaction(task):
            return intent
        return TaskIntent(
            intent_type="read",
            query=intent.query,
            confidence=max(intent.confidence, 0.7),
            fields=intent.fields,
            requires_interaction=False,
            target_text="",
        )

    def _task_looks_satisfied(
        self,
        task: str,
        current_url: str,
        intent: Optional[TaskIntent] = None,
        target_url: str = "",
        snapshot: Optional[Dict[str, Any]] = None,
        elements: Optional[List[PageElement]] = None,
        data: Optional[List[Dict[str, str]]] = None,
    ) -> bool:
        return self.decision._task_looks_satisfied(
            task, current_url, intent=intent, target_url=target_url,
            snapshot=snapshot or self._last_semantic_snapshot,
            elements=elements, data=data,
        )

    async def _wait_for_page_ready(self) -> None:
        """Wait for page to be ready. Delegates to execution layer (Layer 3)."""
        await self.execution.wait_for_page_ready()

    def _snapshot_is_transient_loading(self, snapshot: Optional[Dict[str, Any]]) -> bool:
        return self.decision._snapshot_is_transient_loading(snapshot)

    async def _wait_for_interactive_hydration(self, max_rounds: int = 6) -> Dict[str, Any]:
        snapshot = self._last_semantic_snapshot or await self._get_semantic_snapshot()
        for _ in range(max_rounds):
            if not self._snapshot_is_transient_loading(snapshot):
                return snapshot
            await asyncio.sleep(1.0)
            snapshot = await self._get_semantic_snapshot()
        return snapshot

    async def _wait_for_search_results_ready_v2(self, search_url: str) -> bool:
        selector = ", ".join(get_search_result_selectors(search_url))

        last_probe: Dict[str, Any] = {}
        last_snapshot: Dict[str, Any] = {}
        for _ in range(4):
            current_url = await self._get_current_url_value(search_url)
            title = await self._get_title_value()
            summary = await self._call_toolkit(
                "evaluate_js",
                r"""(sel) => {
                    const nodes = Array.from(document.querySelectorAll(sel));
                    const visible = nodes.filter((node) => {
                        const style = window.getComputedStyle(node);
                        return style && style.visibility !== 'hidden' && style.display !== 'none';
                    });
                    const blockCandidates = Array.from(document.querySelectorAll('main, article, section, [role="main"], [data-testid], p, h1, h2, h3, li'))
                        .map((node) => {
                            const text = (node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim();
                            return text ? text.slice(0, 220) : '';
                        })
                        .filter(Boolean)
                        .slice(0, 10);
                    return {
                        matches: visible.length,
                        textLength: document.body && document.body.innerText ? document.body.innerText.length : 0,
                        bodyText: document.body && document.body.innerText ? document.body.innerText.slice(0, 4000) : '',
                        visibleTextBlocks: blockCandidates,
                    };
                }""",
                selector,
            )
            body_text = ""
            text_length = 0
            visible_text_blocks: List[str] = []
            if summary.success and isinstance(summary.data, dict):
                body_text = str(summary.data.get("bodyText", "") or "")
                text_length = int(summary.data.get("textLength", 0) or 0)
                raw_blocks = summary.data.get("visibleTextBlocks", []) or []
                if isinstance(raw_blocks, list):
                    visible_text_blocks = [str(item or "").strip() for item in raw_blocks if str(item or "").strip()]

            snapshot = await self._get_semantic_snapshot()
            affordances = self._get_snapshot_affordances(snapshot)
            card_count = len(snapshot.get("cards", []) or [])
            collection_item_count = int(affordances.get("collection_item_count", 0) or 0)
            snapshot_main_text = self._get_snapshot_main_text(snapshot)
            snapshot_blocks = self._get_snapshot_visible_text_blocks(snapshot)
            blocked_signals = self._get_snapshot_blocked_signals(snapshot)
            has_snapshot_results = bool(
                card_count > 0
                or collection_item_count >= 3
                or affordances.get("has_results")
                or (str(snapshot.get("page_type", "") or "") == "serp" and (snapshot_main_text or snapshot_blocks))
            )
            looks_like_results_url = self._looks_like_search_results_url(current_url)
            matches = int(summary.data.get("matches", 0) or 0) if summary.success and isinstance(summary.data, dict) else 0
            last_probe = {
                "search_url": search_url,
                "current_url": current_url,
                "title": title,
                "selector": selector,
                "matches": matches,
                "text_length": text_length,
                "looks_like_results_url": looks_like_results_url,
                "snapshot_page_type": str(snapshot.get("page_type", "") or ""),
                "snapshot_page_stage": str(snapshot.get("page_stage", "") or ""),
                "snapshot_card_count": card_count,
                "snapshot_collection_item_count": collection_item_count,
                "snapshot_has_results": has_snapshot_results,
                "snapshot_main_text_len": len(snapshot_main_text),
                "snapshot_visible_text_blocks": len(snapshot_blocks),
                "snapshot_blocked_signals": blocked_signals,
            }
            web_debug_recorder.write_json("browser_search_ready_probe", last_probe)
            if snapshot:
                last_snapshot = snapshot
            if blocked_signals or self._looks_like_blocked_page(current_url, title) or any(token in body_text.lower() for token in ("unusual traffic", "人机身份验证", "异常流量", "验证码", "安全验证")):
                return False
            if has_snapshot_results and (looks_like_results_url or text_length >= 300):
                return True
            if str(snapshot.get("page_type", "") or "") == "serp" and (len(snapshot_main_text) >= 180 or len(snapshot_blocks) >= 2):
                return True

            wait_result = await self._call_toolkit("wait_for_selector", selector, timeout=3000)
            if wait_result.success and matches > 0:
                return True
            if matches >= 3:
                return True
            if text_length >= 300 and (looks_like_results_url or bool(visible_text_blocks)):
                return True
            await self._call_toolkit("human_delay", 300, 900)

        if last_probe:
            web_debug_recorder.record_event("browser_search_ready_failed", **last_probe)
        if last_snapshot:
            web_debug_recorder.write_json("browser_search_ready_last_snapshot", last_snapshot)
        page_html = await self._get_page_html_value()
        if page_html:
            web_debug_recorder.write_text("browser_search_ready_page_html", page_html, suffix=".html")
        return False

    async def _wait_for_search_results_ready(self, search_url: str) -> bool:
        return await self._wait_for_search_results_ready_v2(search_url)

        selectors_by_host = {
            "bing.com": "li.b_algo, .b_ans, #b_results",
            "baidu.com": "#content_left .result, #content_left .c-container, #content_left",
            "google.com": "div.g, .tF2Cxc, #search, [data-sokoban-container]",
            "duckduckgo.com": ".result, .results, .result__body",
        }
        host = str(urlparse(search_url).netloc or "").lower()
        selector = "#search, #b_results, .results, [role='main']"
        for domain, candidate in selectors_by_host.items():
            if domain in host:
                selector = candidate
                break

        for _ in range(4):
            current_url_result = await self.toolkit.get_current_url()
            current_url = str(current_url_result.data or search_url) if current_url_result.success else search_url
            title_result = await self.toolkit.get_title()
            title = str(title_result.data or "") if title_result.success else ""
            summary = await self.toolkit.evaluate_js(
                """() => ({ textLength: document.body && document.body.innerText ? document.body.innerText.length : 0, bodyText: document.body && document.body.innerText ? document.body.innerText.slice(0, 4000) : '' })"""
            )
            body_text = ""
            text_length = 0
            if summary.success and isinstance(summary.data, dict):
                body_text = str(summary.data.get("bodyText", "") or "")
                text_length = int(summary.data.get("textLength", 0) or 0)
            if self._looks_like_blocked_page(current_url, title) or any(token in body_text.lower() for token in ("unusual traffic", "人机身份验证", "异常流量", "验证码", "安全验证")):
                return False
            wait_result = await self.toolkit.wait_for_selector(selector, timeout=3000)
            if wait_result.success:
                matches_result = await self.toolkit.evaluate_js(
                    """(sel) => ({ matches: document.querySelectorAll(sel).length })""",
                    selector,
                )
                matches = 0
                if matches_result.success and isinstance(matches_result.data, dict):
                    matches = int(matches_result.data.get("matches", 0) or 0)
                if matches > 0:
                    return True
                if text_length >= 300:
                    return True
            await self.toolkit.human_delay(300, 900)
        return False

    # ── main run loop ────────────────────────────────────────

    async def _initialize_session(
        self,
        task: str,
        expected_url: str,
        steps: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Phase 1 of run(): page creation, navigation, blocked-page bypass, URL
        validation, intent inference, search bootstrap, and read-only early-return
        check.

        Returns one of:
          {"ok": False, "result": dict}       -- unrecoverable failure
          {"ok": True, "early_result": dict}  -- read-only task satisfied immediately
          {"ok": True, "current_url": str, "page_title": str,
           "task_intent": TaskIntent, "initial_data": list}
        """
        tk = self.toolkit

        # B2: consult anti-bot profile for the target domain *before* launching
        # the browser. Applying a hint here lets us (a) flip headless→headed
        # (which keys the BrowserRuntimePool to a separate browser) and (b)
        # seed a UA override for new_context. Fully gated by ANTI_BOT_PROFILE_ENABLED.
        if settings.ANTI_BOT_PROFILE_ENABLED:
            try:
                from utils.anti_bot_profile import get_anti_bot_profile_store
                store = get_anti_bot_profile_store()
                if store is not None and expected_url:
                    hint = store.suggest_throttle(expected_url)
                    if hint and hint.headed and self.toolkit.headless:
                        self.toolkit.headless = False
                        log_agent_action(
                            self.name,
                            "anti-bot",
                            f"headed mode for {expected_url[:60]} (blocks={hint.block_rate:.2f})",
                        )
                    self.toolkit.apply_throttle_hint(hint)
                    if hint and (hint.delay_sec > 0 or hint.ua or hint.headed):
                        web_debug_recorder.record_event(
                            "browser_anti_bot_hint", **hint.as_dict()
                        )
            except Exception as hint_err:
                log_warning(f"anti-bot hint lookup skipped: {hint_err}")

        r = await tk.create_page()
        if not r.success:
            web_debug_recorder.record_event("browser_create_page_failed", error=r.error)
            return {"ok": False, "result": {"success": False, "message": f"浏览器启动失败: {r.error}", "steps": []}}

        url = expected_url or "about:blank"
        self._action_history = []
        self._page_assessment_cache.clear()

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
            return {"ok": False, "result": {"success": False, "message": f"初始导航失败: {str(nav_err)[:200]}", "url": url, "steps": steps}}

        await self._wait_for_page_ready()
        await self._wait_for_interactive_hydration()
        current_url_r = await tk.get_current_url()
        current_url = current_url_r.data or ""
        title_r = await tk.get_title()
        page_title = title_r.data or ""
        web_debug_recorder.record_event(
            "browser_initial_navigation",
            expected_url=expected_url,
            current_url=current_url,
            title=page_title,
        )

        if web_debug_recorder.is_enabled():
            log_warning(f"[DEBUG] ========== 初始导航完成 ==========")
            log_warning(f"[DEBUG] 目标URL: {expected_url}")
            log_warning(f"[DEBUG] 当前URL: {current_url}")
            log_warning(f"[DEBUG] 页面标题: {page_title}")
            log_warning(f"[DEBUG] ====================================")

        if self._looks_like_blocked_page(current_url, page_title):
            bypass_r = await tk.bypass_robot_challenge(max_retries=3)
            if not bypass_r.success:
                return {
                    "ok": False,
                    "result": {
                        "success": False,
                        "message": f"navigation landed on blocked page: {page_title or current_url}",
                        "url": current_url,
                        "expected_url": expected_url,
                        "title": page_title,
                        "steps": steps,
                    },
                }
            url_r = await tk.get_current_url()
            current_url = (url_r.data or current_url) if url_r.success else current_url
            title_r = await tk.get_title()
            page_title = (title_r.data or "") if title_r.success else page_title
            log_agent_action(self.name, "反机器人验证绕过成功", current_url[:80])

        if expected_url and current_url and not self._urls_look_related(expected_url, current_url):
            return {
                "ok": False,
                "result": {
                    "success": False,
                    "message": f"navigation landed on unexpected page: expected {expected_url}, got {current_url}",
                    "url": current_url,
                    "expected_url": expected_url,
                    "title": page_title,
                    "steps": steps,
                },
            }

        task_intent = await self._infer_task_intent(task)
        task_intent = self._coerce_intent_for_direct_page(task, task_intent, expected_url)
        landed_blank = (not current_url) or current_url.strip().lower() in {"about:blank", "chrome://newtab/"}
        should_bootstrap_search = (
            not self._extract_url_from_task(task)
            and not expected_url
            and (
                task_intent.intent_type == "search"
                or (landed_blank and task_intent.intent_type in {"read", "navigate", "unknown"})
            )
        )
        if should_bootstrap_search:
            query = task_intent.query or self._derive_primary_query(task)
            if query:
                await self._bootstrap_search_results(query)
                current_url_r = await tk.get_current_url()
                current_url = current_url_r.data or ""

        initial_data = await self._extract_data_for_intent(task_intent)
        if self._is_read_only_task(task, task_intent):
            if initial_data and len(initial_data) >= 3 and self._page_data_satisfies_goal(
                task,
                current_url or "",
                task_intent,
                initial_data,
                snapshot=self._last_semantic_snapshot,
            ):
                title_r = await tk.get_title()
                url_r = await tk.get_current_url()
                _final_screenshot = await self._capture_final_screenshot()
                return {
                    "ok": True,
                    "early_result": {
                        "success": True,
                        "message": "read-only task satisfied from initial page",
                        "url": url_r.data or "",
                        "title": title_r.data or "",
                        "expected_url": expected_url,
                        "steps": steps,
                        "data": initial_data,
                        "_page_screenshot": _final_screenshot,
                    },
                }
            else:
                log_warning(f"初始数据不足（{len(initial_data)} 条），继续执行步骤")

        return {
            "ok": True,
            "current_url": current_url,
            "page_title": page_title,
            "task_intent": task_intent,
            "initial_data": initial_data,
        }

    async def _execute_step(
        self,
        step_no: int,
        task: str,
        task_intent,
        expected_url: str,
        steps: List[Dict[str, Any]],
        accumulated_data: List[Dict[str, str]],
        seen_keys: set,
        prefetched: Dict[str, Any],
        visual_tracker,
        last_action,
    ) -> Dict[str, Any]:
        """
        Execute one iteration of the main step loop.

        accumulated_data and seen_keys are mutated in-place.
        prefetched dict (keys: "elements", "snapshot") is mutated in-place.

        Returns one of:
          {"status": "exit", "result": dict}                   -- return to run() caller
          {"status": "continue", "last_action": last_action}   -- next iteration
          {"status": "ok", "last_action": action}              -- normal completion
        """
        tk = self.toolkit

        def _merge_new_data(new_items: List[Dict[str, str]]):
            for item in (new_items or []):
                vals = [str(v)[:80] for v in list(item.values())[:2] if v]
                key = "|".join(vals)
                if key and key not in seen_keys:
                    seen_keys.add(key)
                    accumulated_data.append(item)

        current_url_r = await tk.get_current_url()
        title_r = await tk.get_title()
        if self._looks_like_blocked_page(current_url_r.data or "", title_r.data or ""):
            bypass_r = await tk.bypass_robot_challenge(max_retries=3)
            if not bypass_r.success:
                return {
                    "status": "exit",
                    "result": {
                        "success": False,
                        "message": f"browser landed on blocked page during execution: {title_r.data or current_url_r.data or ''}",
                        "url": current_url_r.data or "",
                        "title": title_r.data or "",
                        "expected_url": expected_url,
                        "steps": steps,
                        "data": accumulated_data,
                    },
                }
            log_agent_action(self.name, "执行中反机器人验证绕过成功")
            prefetched["elements"] = None
            prefetched["snapshot"] = None

        if prefetched["elements"] is not None:
            elements = prefetched["elements"]
            snapshot = prefetched["snapshot"] or self._last_semantic_snapshot
            prefetched["elements"] = None
            prefetched["snapshot"] = None
        else:
            _obs_elements_task = asyncio.ensure_future(self._extract_interactive_elements())
            _obs_snapshot_task = (
                asyncio.ensure_future(self._get_semantic_snapshot())
                if self._last_semantic_snapshot is None
                else None
            )
            elements = await _obs_elements_task
            snapshot = (
                await _obs_snapshot_task if _obs_snapshot_task is not None
                else self._last_semantic_snapshot
            )
        _obs_data_task = asyncio.ensure_future(self._extract_data_for_intent(task_intent))
        observed_data = await _obs_data_task
        _merge_new_data(observed_data)
        web_debug_recorder.write_json(
            f"browser_step_{step_no}_context",
            {
                "step": step_no,
                "url": current_url_r.data or "",
                "title": title_r.data or "",
                "intent": {
                    "intent_type": task_intent.intent_type,
                    "query": task_intent.query,
                    "confidence": task_intent.confidence,
                    "fields": task_intent.fields,
                    "requires_interaction": task_intent.requires_interaction,
                    "target_text": task_intent.target_text,
                },
                "elements": self._elements_to_debug_payload(elements),
                "snapshot": snapshot,
                "observed_data": observed_data,
                "accumulated_data": list(accumulated_data),
                "last_action": self._action_to_debug_payload(last_action),
            },
        )

        if web_debug_recorder.is_enabled():
            log_warning(f"[DEBUG] ========== Step {step_no} 开始 ==========")
            log_warning(f"[DEBUG] 当前URL: {current_url_r.data or ''}")
            log_warning(f"[DEBUG] 页面标题: {title_r.data or ''}")
            log_warning(f"[DEBUG] 可交互元素数量: {len(elements)}")
            log_warning(f"[DEBUG] 已收集数据: {len(accumulated_data)} 条")
            log_warning(f"[DEBUG] ====================================")

        action = None
        action_source = ""
        if (
            step_no >= 2
            and not observed_data
            and not accumulated_data
            and str((snapshot or {}).get("page_type", "")).strip() in {"", "unknown"}
        ):
            relevant, vision_summary = await self._vision_check_page_relevance(
                task, task_intent.query or self._derive_primary_query(task), snapshot,
            )
            if relevant:
                action = BrowserAction(
                    action_type=ActionType.EXTRACT,
                    description=f"vision detected relevant content: {vision_summary[:100]}",
                    confidence=0.7,
                )
                action_source = "vision_relevance"

        if action is None:
            action, action_source = await self._plan_next_action(
                task,
                current_url_r.data or "",
                title_r.data or "",
                elements,
                task_intent,
                accumulated_data or observed_data,
                snapshot=snapshot,
                last_action=last_action,
                recent_steps=steps,
            )
        if action is None or action.action_type == ActionType.WAIT:
            visual_action = await self._decide_action_with_vision(
                task,
                current_url_r.data or "",
                title_r.data or "",
                elements,
                task_intent,
                accumulated_data or observed_data,
                snapshot=snapshot,
                last_action=last_action,
            )
            if visual_action is not None:
                action = visual_action
                action_source = "vision_fallback"
        if action is None:
            action = BrowserAction(
                action_type=ActionType.WAIT,
                value="1",
                description="no actionable elements",
                confidence=0.05,
            )
            action_source = "implicit_wait"
        web_debug_recorder.write_json(
            f"browser_step_{step_no}_action",
            {
                **self._action_to_debug_payload(action),
                "source": action_source,
            },
        )

        if web_debug_recorder.is_enabled():
            log_warning(f"[DEBUG] Step {step_no} 决策动作: {action.action_type.value}")
            log_warning(f"[DEBUG] 动作描述: {action.description}")
            log_warning(f"[DEBUG] 目标选择器: {action.target_selector[:100] if action.target_selector else 'N/A'}")
            log_warning(f"[DEBUG] 置信度: {action.confidence}")

        if action.action_type == ActionType.DONE:
            data = await self._extract_data_for_intent(task_intent)
            _merge_new_data(data)
            if not accumulated_data:
                snapshot = self._last_semantic_snapshot or {}
                main_text = self._get_snapshot_main_text(snapshot)
                _max = settings.BROWSER_FALLBACK_TEXT_MAX_LEN
                _min = settings.BROWSER_FALLBACK_TEXT_MIN_LEN
                if main_text and len(main_text) >= _min:
                    _merge_new_data([{
                        "text": main_text[:_max],
                        "source": "page_main_text_fallback",
                        "truncated": len(main_text) > _max,
                    }])
            if not accumulated_data:
                vision_data = await self._extract_data_with_vision(task, task_intent, snapshot)
                if vision_data:
                    _merge_new_data(vision_data)
            log_success("browser task completed")
            url_r = await tk.get_current_url()
            title_r = await tk.get_title()
            _final_screenshot = await self._capture_final_screenshot()
            final_data = accumulated_data or data
            _answer_text = ""
            _answer_citations: List[str] = []
            if settings.BROWSER_ANSWER_TEXT_ENABLED:
                _answer_text = getattr(self.decision, "_last_assessment_reason", "") or ""
                _answer_citations = self._resolve_evidence_urls(
                    getattr(self.decision, "_last_evidence_indexes", []), final_data
                )
            return {
                "status": "exit",
                "result": {
                    "success": True, "message": "task completed",
                    "url": url_r.data or "", "title": title_r.data or "",
                    "expected_url": expected_url, "steps": steps,
                    "data": final_data,
                    "answer_text": _answer_text,
                    "answer_citations": _answer_citations,
                    "_page_screenshot": _final_screenshot,
                },
            }

        if action.action_type == ActionType.EXTRACT:
            # Reuse observed_data from step start instead of calling _extract_data_for_intent again
            data = observed_data
            _merge_new_data(data)
            if not accumulated_data:
                snapshot = self._last_semantic_snapshot or {}
                main_text = self._get_snapshot_main_text(snapshot)
                _max = settings.BROWSER_FALLBACK_TEXT_MAX_LEN
                _min = settings.BROWSER_FALLBACK_TEXT_MIN_LEN
                if main_text and len(main_text) >= _min:
                    _merge_new_data([{
                        "text": main_text[:_max],
                        "source": "page_main_text_fallback",
                        "truncated": len(main_text) > _max,
                    }])
            if not accumulated_data:
                vision_data = await self._extract_data_with_vision(task, task_intent, snapshot)
                if vision_data:
                    _merge_new_data(vision_data)
            url_r = await tk.get_current_url()
            title_r = await tk.get_title()
            _final_screenshot = await self._capture_final_screenshot()
            return {
                "status": "exit",
                "result": {
                    "success": True, "message": "data extracted",
                    "url": url_r.data or "", "title": title_r.data or "",
                    "expected_url": expected_url, "steps": steps,
                    "data": accumulated_data or data,
                    "_page_screenshot": _final_screenshot,
                },
            }

        if action.requires_confirmation and settings.REQUIRE_HUMAN_CONFIRM:
            from utils.human_confirm import HumanConfirm
            confirmed = await asyncio.to_thread(
                HumanConfirm.request_browser_action_confirmation,
                action=action.action_type.value,
                target=action.target_selector[:80],
                value=action.value[:80],
                description=action.description,
            )
            if not confirmed:
                return {
                    "status": "exit",
                    "result": {
                        "success": False, "message": "user declined action confirmation",
                        "requires_confirmation": True, "steps": steps,
                    },
                }

        if self._is_action_looping(action):
            url_r = await tk.get_current_url()
            title_r = await tk.get_title()
            if self._looks_like_blocked_page(url_r.data or "", title_r.data or ""):
                bypass_r = await tk.bypass_robot_challenge(max_retries=2)
                if not bypass_r.success:
                    return {
                        "status": "exit",
                        "result": {
                            "success": False,
                            "message": f"browser stuck on blocked page: {title_r.data or url_r.data or ''}",
                            "url": url_r.data or "", "title": title_r.data or "",
                            "expected_url": expected_url, "steps": steps, "data": accumulated_data,
                        },
                    }
                log_agent_action(self.name, "循环检测中反机器人验证绕过成功")
            if self._is_read_only_task(task, task_intent):
                _merge_new_data(await self._extract_data_for_intent(task_intent))
                _final_screenshot = await self._capture_final_screenshot()
                return {
                    "status": "exit",
                    "result": {
                        "success": True, "message": "repeated action avoided; extracted current page",
                        "url": url_r.data or "", "title": title_r.data or "",
                        "expected_url": expected_url, "steps": steps, "data": accumulated_data,
                        "_page_screenshot": _final_screenshot,
                    },
                }
            return {
                "status": "exit",
                "result": {
                    "success": False,
                    "message": f"repeated action loop detected at step {step_no}",
                    "url": url_r.data or "", "title": title_r.data or "",
                    "expected_url": expected_url, "steps": steps, "data": accumulated_data,
                },
            }

        self._record_action(action)
        last_action = action

        self._before_action_screenshot = None
        if settings.VISION_VERIFY_ACTION:
            try:
                page = getattr(self.toolkit, '_page', None)
                if page:
                    self._before_action_screenshot = await page.screenshot(type="jpeg", quality=50, full_page=False)
            except Exception:
                pass

        # Track state before action for richer step history
        _url_before = current_url_r.data or ""
        _title_before = title_r.data or ""
        _data_count_before = len(accumulated_data)
        _failure_reason = ""

        _is_input_action = action.action_type in {ActionType.INPUT, ActionType.FILL_FORM}
        _is_wait_action = action.action_type == ActionType.WAIT
        if _is_input_action:
            before = None
        else:
            before = await self._snapshot_page_state()
        success = await self._execute_action(action)

        if success and _is_wait_action and settings.VISION_WAIT_CHANGE_DETECT:
            try:
                page = getattr(self.toolkit, '_page', None)
                if page and self._before_action_screenshot is not None:
                    after_wait_img = await page.screenshot(type="jpeg", quality=50, full_page=False)
                    from utils.image_diff import screenshots_differ
                    if not screenshots_differ(self._before_action_screenshot, after_wait_img, threshold=settings.VISION_PIXEL_DIFF_THRESHOLD):
                        log_agent_action(self.name, "wait", "no_visual_change_during_wait")
            except Exception:
                pass

        if success and not _is_input_action and not _is_wait_action:
            await self._wait_for_page_ready()
            success = await self._verify_action_effect(before, action)
            if not success:
                _failure_reason = "action executed but no page effect detected"
        elif success and _is_input_action:
            await self.toolkit.wait_for_load("domcontentloaded", timeout=3000)
            success = True
        elif not success:
            _failure_reason = "action execution failed (element not found or interaction error)"
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
                    last_action = action
                    action_source = "local_recovery"
                    _failure_reason = ""
        if not success:
            visual_recovery = await self._decide_action_with_vision(
                task,
                current_url_r.data or "",
                title_r.data or "",
                elements,
                task_intent,
                accumulated_data or observed_data,
                snapshot=snapshot,
                last_action=action,
            )
            if visual_recovery and self._action_signature(visual_recovery) != self._action_signature(action):
                visual_before = await self._snapshot_page_state()
                success = await self._execute_action(visual_recovery)
                if success:
                    await self._wait_for_page_ready()
                    success = await self._verify_action_effect(visual_before, visual_recovery)
                if success:
                    action = visual_recovery
                    self._record_action(action)
                    last_action = action
                    action_source = "vision_recovery"
                    _failure_reason = ""

        url_r = await tk.get_current_url()
        _title_after_r = await tk.get_title()
        _url_after = url_r.data or ""
        _title_after = _title_after_r.data or ""
        _page_changed = (_url_before != _url_after) or (_title_before != _title_after)
        steps.append({
            "step": step_no,
            "plan": action.description or action.action_type.value,
            "action": action.target_selector or action.action_type.value,
            "source": action_source,
            "observation": "success" if success else "failed",
            "decision": "continue" if success else "retry_or_fail",
            "action_type": action.action_type.value,
            "selector": action.target_selector,
            "value": action.value,
            "description": action.description,
            "result": "success" if success else "failed",
            "url": url_r.data or "",
            "url_before": _url_before,
            "failure_reason": _failure_reason if not success else "",
            "page_changed": _page_changed,
            "data_before_count": _data_count_before,
            "data_after_count": len(accumulated_data),
        })
        from utils.structured_logger import get_structured_logger, LogContext
        _sl = get_structured_logger()
        with LogContext(agent="browser_agent", step_no=step_no):
            _sl.log_action(
                action_type=action.action_type.value,
                target=action.target_selector or "",
                confidence=action.confidence,
                result="success" if success else "failed",
            )
        web_debug_recorder.write_json(f"browser_step_{step_no}_result", steps[-1])

        _step_screenshot_bytes: Optional[bytes] = None
        if web_debug_recorder.is_enabled():
            try:
                _sc = await self.toolkit.screenshot(full_page=False)
                if _sc.success and _sc.data:
                    _step_screenshot_bytes = _sc.data
                    web_debug_recorder.write_binary(f"browser_step_{step_no}_screenshot", _sc.data, ".png")
            except Exception:
                pass

        if success and action.action_type not in {ActionType.DONE, ActionType.EXTRACT}:
            try:
                if _step_screenshot_bytes is None:
                    page = getattr(self.toolkit, '_page', None)
                    if page:
                        _step_screenshot_bytes = await page.screenshot(type="jpeg", quality=50, full_page=False)
                if _step_screenshot_bytes and not visual_tracker.record(_step_screenshot_bytes):
                    log_agent_action(self.name, "progress", f"visual_stuck_detected_at_step_{step_no}")
                    if self._is_read_only_task(task, task_intent):
                        _merge_new_data(await self._extract_data_for_intent(task_intent))
                        _final_screenshot = await self._capture_final_screenshot()
                        url_r = await tk.get_current_url()
                        return {
                            "status": "exit",
                            "result": {
                                "success": True, "message": "visually stuck, extracted current page",
                                "url": url_r.data or "", "steps": steps, "data": accumulated_data,
                                "_page_screenshot": _final_screenshot,
                            },
                        }
            except Exception:
                pass

        if not success:
            if action.action_type == ActionType.WAIT:
                return {"status": "continue", "last_action": last_action}
            _consecutive_fails = 0
            for _s in reversed(steps):
                if _s.get("result") == "failed":
                    _consecutive_fails += 1
                else:
                    break
            _max_fails = settings.BROWSER_MAX_CONSECUTIVE_FAILS
            if _consecutive_fails == 1:
                log_warning(f"step {step_no} 失败，将在下一步重新评估页面状态")
                await asyncio.sleep(1)
                return {"status": "continue", "last_action": last_action}
            elif _consecutive_fails == 2:
                log_warning(f"连续2步失败，尝试刷新页面恢复")
                await tk.refresh()
                await self._wait_for_page_ready()
                return {"status": "continue", "last_action": last_action}
            elif _consecutive_fails >= _max_fails:
                title_r = await tk.get_title()
                return {
                    "status": "exit",
                    "result": {
                        "success": False,
                        "message": f"连续 {_consecutive_fails} 步失败，已尝试恢复但仍失败 (最后在 step {step_no})",
                        "url": url_r.data or "", "title": title_r.data or "",
                        "expected_url": expected_url,
                        "steps": steps, "data": accumulated_data or await self._extract_data_for_intent(task_intent),
                    },
                }
            else:
                log_warning(f"连续{_consecutive_fails}步失败 (容忍上限{_max_fails})，继续尝试")
                return {"status": "continue", "last_action": last_action}

        if action.action_type in {ActionType.CLICK, ActionType.INPUT, ActionType.FILL_FORM, ActionType.PRESS_KEY}:
            post_snapshot = await self._get_semantic_snapshot()
            post_elements = self._filter_noise_elements(self._elements_from_snapshot(post_snapshot))
            if post_elements:
                self._element_cache = post_elements[:40]
            else:
                post_elements = await self._extract_interactive_elements()
                post_snapshot = self._last_semantic_snapshot or post_snapshot
            prefetched["elements"] = post_elements
            prefetched["snapshot"] = post_snapshot
            step_data = await self._extract_data_for_intent(task_intent)
            _merge_new_data(step_data)
            # main_text fallback: if structured extraction found nothing, use snapshot text
            if not accumulated_data and not step_data:
                _snap_main = self._get_snapshot_main_text(post_snapshot)
                _max = settings.BROWSER_FALLBACK_TEXT_MAX_LEN
                _min = settings.BROWSER_FALLBACK_TEXT_MIN_LEN
                if _snap_main and len(_snap_main) >= _min:
                    _merge_new_data([{
                        "text": _snap_main[:_max],
                        "source": "page_main_text_fallback",
                        "truncated": len(_snap_main) > _max,
                    }])
            candidate_data = accumulated_data or step_data
            requires_data = self._is_read_only_task(task, task_intent) or task_intent.intent_type == "search"
            has_sufficient_data = bool(candidate_data)
            if task_intent.intent_type == "search" and candidate_data:
                has_sufficient_data = self._is_data_relevant(task_intent.query, candidate_data)
            if (
                self._task_looks_satisfied(
                    task,
                    url_r.data or "",
                    task_intent,
                    target_url=expected_url,
                    snapshot=post_snapshot,
                    elements=post_elements,
                    data=candidate_data,
                )
                and (has_sufficient_data or not requires_data)
            ):
                title_r = await tk.get_title()
                _final_screenshot = await self._capture_final_screenshot()
                return {
                    "status": "exit",
                    "result": {
                        "success": True, "message": "task reached target page",
                        "url": url_r.data or "", "title": title_r.data or "",
                        "expected_url": expected_url,
                        "steps": steps, "data": candidate_data or await self._extract_data_for_intent(task_intent),
                        "_page_screenshot": _final_screenshot,
                    },
                }

        if action.action_type == ActionType.SCROLL:
            step_data = await self._extract_data_for_intent(task_intent)
            _merge_new_data(step_data)

        return {"status": "ok", "last_action": last_action}

    async def _build_final_result(
        self,
        task: str,
        task_intent,
        expected_url: str,
        steps: List[Dict[str, Any]],
        accumulated_data: List[Dict[str, str]],
        seen_keys: set,
    ) -> Dict[str, Any]:
        """Assemble the final result dict after max_steps is reached."""
        def _merge_new_data(new_items):
            for item in (new_items or []):
                vals = [str(v)[:80] for v in list(item.values())[:2] if v]
                key = "|".join(vals)
                if key and key not in seen_keys:
                    seen_keys.add(key)
                    accumulated_data.append(item)

        _merge_new_data(await self._extract_data_for_intent(task_intent))
        if not accumulated_data:
            snapshot = self._last_semantic_snapshot or {}
            main_text = self._get_snapshot_main_text(snapshot)
            _max = settings.BROWSER_FALLBACK_TEXT_MAX_LEN
            _min = settings.BROWSER_FALLBACK_TEXT_MIN_LEN
            if main_text and len(main_text) >= _min:
                _merge_new_data([{
                    "text": main_text[:_max],
                    "source": "page_main_text_fallback",
                    "truncated": len(main_text) > _max,
                }])
        if not accumulated_data:
            vision_data = await self._extract_data_with_vision(task, task_intent, self._last_semantic_snapshot)
            if vision_data:
                _merge_new_data(vision_data)
        url_r = await self.toolkit.get_current_url()
        title_r = await self.toolkit.get_title()
        _final_screenshot = await self._capture_final_screenshot()
        return {
            "success": len(accumulated_data) > 0,
            "message": "max steps reached" + (f", but collected {len(accumulated_data)} items" if accumulated_data else ""),
            "url": url_r.data or "", "title": title_r.data or "",
            "expected_url": expected_url,
            "steps": steps, "data": accumulated_data,
            "_page_screenshot": _final_screenshot,
        }

    # ── P4: Batch execution mode ──────────────────────────────

    def _sequence_action_to_browser_action(self, seq_action) -> BrowserAction:
        type_map = {
            "click": ActionType.CLICK,
            "input": ActionType.INPUT,
            "select": ActionType.SELECT,
            "scroll": ActionType.SCROLL,
            "wait": ActionType.WAIT,
            "navigate": ActionType.NAVIGATE,
            "press_key": ActionType.PRESS_KEY,
            "done": ActionType.DONE,
            "extract": ActionType.EXTRACT,
        }
        action_type = type_map.get(seq_action.action_type, ActionType.CLICK)
        return BrowserAction(
            action_type=action_type,
            target_ref=seq_action.target_ref,
            target_selector=seq_action.target_selector,
            value=seq_action.value,
            description=seq_action.description,
            confidence=0.8,
            keyboard_key=seq_action.keyboard_key,
        )

    async def _get_sequence_llm(self):
        from core.llm import LLMClient
        seq_model = settings.BROWSER_SEQUENCE_MODEL
        if seq_model:
            return LLMClient(model=seq_model)
        return self._get_llm()

    async def _get_correction_llm(self, deviation: str = "minor"):
        from core.llm import LLMClient
        if deviation == "major" and settings.BROWSER_CORRECTION_ESCALATE_TO_REASONING:
            return self._get_llm()
        seq_model = settings.BROWSER_SEQUENCE_MODEL
        if seq_model:
            return LLMClient(model=seq_model)
        return self._get_llm()

    async def _build_page_context_for_sequence(self) -> str:
        tk = self.toolkit
        url_r = await tk.get_current_url()
        title_r = await tk.get_title()
        snapshot = self._last_semantic_snapshot or {}
        page_type = snapshot.get("page_type", "unknown")
        main_text = self._get_snapshot_main_text(snapshot)
        lines = [
            f"URL: {url_r.data or ''}",
            f"Title: {title_r.data or ''}",
            f"Page type: {page_type}",
        ]
        if main_text:
            lines.append(f"Main text: {main_text[:500]}")
        headings = snapshot.get("headings", [])
        if headings:
            lines.append(f"Headings: {', '.join(str(h) for h in headings[:10])}")
        return "\n".join(lines)

    async def _run_batch_mode(
        self,
        task: str,
        task_intent,
        expected_url: str,
        steps: List[Dict[str, Any]],
        max_corrections: int = 0,
    ) -> Dict[str, Any]:
        from agents.browser_action_sequence import (
            generate_action_sequence, visual_verify, plan_correction,
        )
        from utils.dom_checkpoint import verify_dom_checkpoint

        tk = self.toolkit
        accumulated_data: List[Dict[str, str]] = []
        seen_keys: set = set()
        max_corrections = max_corrections or settings.BROWSER_MAX_CORRECTIONS

        def _merge_new_data(new_items):
            for item in (new_items or []):
                vals = [str(v)[:80] for v in list(item.values())[:2] if v]
                key = "|".join(vals)
                if key and key not in seen_keys:
                    seen_keys.add(key)
                    accumulated_data.append(item)

        elements = await self._extract_interactive_elements()
        snapshot = await self._get_semantic_snapshot() if self._last_semantic_snapshot is None else self._last_semantic_snapshot
        page_context = await self._build_page_context_for_sequence()
        elements_text = self.decision._format_assessment_elements_for_llm(task, "", elements)
        repeated_actions = self.decision.format_repeated_actions_for_llm()
        plan_context = "(no plan)"
        plan = getattr(self.decision, "_task_plan", None)
        if plan is not None:
            try:
                plan_context = plan.format_for_prompt()
            except Exception:
                pass

        log_agent_action(self.name, "batch mode", "generating action sequence")
        seq_llm = await self._get_sequence_llm()
        sequence = await generate_action_sequence(
            task=task,
            page_context=page_context,
            elements_text=elements_text,
            llm=seq_llm,
            repeated_actions=repeated_actions,
            plan_context=plan_context,
        )
        if sequence is None:
            log_warning("batch mode: action sequence generation failed, falling back to step mode")
            return None

        web_debug_recorder.write_json("browser_action_sequence", sequence.to_dict())
        log_agent_action(
            self.name, "动作序列已生成",
            f"{len(sequence.actions)} actions, goal: {sequence.goal_description[:60]}",
        )

        if not sequence.actions:
            log_agent_action(self.name, "batch mode", "empty sequence — goal already satisfied")
            _merge_new_data(await self._extract_data_for_intent(task_intent))
            url_r = await tk.get_current_url()
            title_r = await tk.get_title()
            return {
                "success": True,
                "message": sequence.goal_description or "goal already satisfied",
                "url": url_r.data or "", "title": title_r.data or "",
                "expected_url": expected_url, "steps": steps, "data": accumulated_data,
            }

        for correction_round in range(max_corrections + 1):
            batch_failed_at = None
            for seq_action in sequence.remaining():
                browser_action = self._sequence_action_to_browser_action(seq_action)
                self._record_action(browser_action)

                success = await self._execute_action(browser_action)
                step_record = {
                    "action_type": seq_action.action_type,
                    "description": seq_action.description,
                    "target_ref": seq_action.target_ref,
                    "success": success,
                    "batch_mode": True,
                }

                if not success:
                    recovery = self._recover_action(task, browser_action, elements)
                    if recovery:
                        success = await self._execute_action(recovery)
                        step_record["recovery"] = True

                if success and settings.BROWSER_DOM_CHECKPOINT_ENABLED:
                    page = getattr(tk, '_page', None)
                    if page and seq_action.dom_checkpoint.check_type != "none":
                        cp_result = await verify_dom_checkpoint(page, seq_action.dom_checkpoint)
                        step_record["checkpoint"] = cp_result.detail
                        if not cp_result.passed:
                            log_warning(f"batch mode: DOM checkpoint failed at action {sequence.execution_index}: {cp_result.detail}")
                            step_record["checkpoint_passed"] = False
                            batch_failed_at = seq_action
                            steps.append(step_record)
                            sequence.advance()
                            break
                elif not success:
                    log_warning(f"batch mode: action failed at index {sequence.execution_index}: {seq_action.description}")
                    step_record["success"] = False
                    batch_failed_at = seq_action
                    steps.append(step_record)
                    sequence.advance()
                    break

                steps.append(step_record)
                sequence.advance()

            if settings.BROWSER_VISUAL_VERIFY_ENABLED:
                url_r = await tk.get_current_url()
                title_r = await tk.get_title()
                page_context_now = await self._build_page_context_for_sequence()
                vision_desc = ""
                try:
                    page = getattr(tk, '_page', None)
                    if page and self._can_use_vision():
                        screenshot_bytes = await page.screenshot(type="jpeg", quality=50, full_page=False)
                        if screenshot_bytes:
                            vision_desc = await self._describe_screenshot(screenshot_bytes, task) or ""
                except Exception:
                    pass

                verify_llm = await self._get_sequence_llm()
                verify_result = await visual_verify(
                    task=task,
                    expected_outcome=sequence.expected_outcome,
                    executed_actions_summary=sequence.format_executed_for_prompt(),
                    current_page_context=page_context_now,
                    vision_description=vision_desc,
                    llm=verify_llm,
                )
                web_debug_recorder.write_json("browser_batch_verify", {
                    "goal_achieved": verify_result.goal_achieved,
                    "deviation": verify_result.deviation,
                    "detail": verify_result.detail,
                    "correction_round": correction_round,
                })
                log_agent_action(
                    self.name, "batch verify",
                    f"achieved={verify_result.goal_achieved}, deviation={verify_result.deviation}",
                )

                if verify_result.goal_achieved:
                    _merge_new_data(await self._extract_data_for_intent(task_intent))
                    _final_screenshot = await self._capture_final_screenshot()
                    return {
                        "success": True,
                        "message": "batch execution completed",
                        "url": url_r.data or "", "title": title_r.data or "",
                        "expected_url": expected_url, "steps": steps,
                        "data": accumulated_data,
                        "_page_screenshot": _final_screenshot,
                    }

                if correction_round >= max_corrections:
                    break

                log_agent_action(self.name, "batch correction", f"round {correction_round + 1}, deviation={verify_result.deviation}")
                elements = await self._extract_interactive_elements()
                page_context_now = await self._build_page_context_for_sequence()
                elements_text_now = self.decision._format_assessment_elements_for_llm(task, url_r.data or "", elements)
                repeated_actions_now = self.decision.format_repeated_actions_for_llm()

                corr_llm = await self._get_correction_llm(verify_result.deviation)
                correction_seq = await plan_correction(
                    task=task,
                    original_sequence_summary=sequence.format_executed_for_prompt(),
                    failure_detail=verify_result.detail,
                    page_context=page_context_now,
                    elements_text=elements_text_now,
                    llm=corr_llm,
                    repeated_actions=repeated_actions_now,
                )
                if correction_seq is None or not correction_seq.actions:
                    log_warning("batch mode: correction planning failed")
                    break

                web_debug_recorder.write_json(f"browser_correction_sequence_{correction_round + 1}", correction_seq.to_dict())
                log_agent_action(
                    self.name, f"纠偏序列已生成 (round {correction_round + 1})",
                    f"{len(correction_seq.actions)} actions",
                )
                sequence = correction_seq
                continue

            else:
                if batch_failed_at is None:
                    _merge_new_data(await self._extract_data_for_intent(task_intent))
                    url_r = await tk.get_current_url()
                    title_r = await tk.get_title()
                    _final_screenshot = await self._capture_final_screenshot()
                    return {
                        "success": True,
                        "message": "batch execution completed (no visual verify)",
                        "url": url_r.data or "", "title": title_r.data or "",
                        "expected_url": expected_url, "steps": steps,
                        "data": accumulated_data,
                        "_page_screenshot": _final_screenshot,
                    }
                break

        _merge_new_data(await self._extract_data_for_intent(task_intent))
        url_r = await tk.get_current_url()
        title_r = await tk.get_title()
        _final_screenshot = await self._capture_final_screenshot()
        return {
            "success": len(accumulated_data) > 0,
            "message": "batch execution finished with corrections exhausted",
            "url": url_r.data or "", "title": title_r.data or "",
            "expected_url": expected_url, "steps": steps,
            "data": accumulated_data,
            "_page_screenshot": _final_screenshot,
        }

    async def get_or_compute_assessment(self, page_hash: str, compute_fn):
        """Strategy-facing passthrough to the per-run PageAssessmentCache (B6).

        Strategies that want to share an expensive observation result
        across the fall-through chain call this instead of the underlying
        compute function directly. Empty ``page_hash`` bypasses the cache.
        """
        return await self._assessment_cache.get_or_compute(page_hash, compute_fn)

    async def _run_per_step_loop(
        self,
        task: str,
        task_intent,
        expected_url: str,
        steps: List[Dict[str, Any]],
        max_steps: int,
    ) -> Dict[str, Any]:
        """Original per-step loop body extracted from ``run()`` (B6).

        Returns the terminal result dict ready to be returned by ``run``.
        Mutates ``steps`` in-place.
        """
        accumulated_data: List[Dict[str, str]] = []
        seen_keys: set = set()
        last_action: Optional[BrowserAction] = None
        prefetched: Dict[str, Any] = {"elements": None, "snapshot": None}
        from utils.image_diff import VisualProgressTracker
        visual_tracker = VisualProgressTracker(window_size=settings.VISION_PROGRESS_WINDOW)

        _plan_stuck_counter: Dict[int, int] = {}
        for step_no in range(1, max_steps + 1):
            step_result = await self._execute_step(
                step_no, task, task_intent, expected_url, steps,
                accumulated_data, seen_keys, prefetched, visual_tracker, last_action,
            )
            if step_result["status"] == "exit":
                return step_result["result"]
            last_action = step_result.get("last_action", last_action)

            plan = getattr(self.decision, "_task_plan", None)
            if plan is not None and settings.BROWSER_PLAN_ENABLED:
                try:
                    cur_step = plan.current_step()
                    if cur_step is None:
                        pass
                    else:
                        from agents.browser_task_plan import step_advance, replan
                        last_step_record = steps[-1] if steps else {}
                        obs_summary = json.dumps({
                            "url": last_step_record.get("url", ""),
                            "title": last_step_record.get("title", ""),
                            "result": last_step_record.get("result", ""),
                            "failure_reason": last_step_record.get("failure_reason", ""),
                            "data_count": len(accumulated_data),
                            "last_action": last_step_record.get("action_type", ""),
                        }, ensure_ascii=False)
                        decision = await step_advance(plan, obs_summary, self._get_llm())
                        if decision.advance:
                            plan.advance()
                            _plan_stuck_counter[cur_step.index] = 0
                            log_agent_action(self.name, f"plan step {cur_step.index} 完成", decision.reason[:60])
                        elif decision.skip:
                            plan.skip_current()
                            log_agent_action(self.name, f"plan step {cur_step.index} 跳过", decision.reason[:60])
                        else:
                            _plan_stuck_counter[cur_step.index] = _plan_stuck_counter.get(cur_step.index, 0) + 1
                            stuck_thresh = max(1, settings.BROWSER_STEP_STUCK_THRESHOLD)
                            if decision.need_replan or _plan_stuck_counter[cur_step.index] >= stuck_thresh:
                                replanned = await replan(
                                    plan,
                                    decision.reason or f"step {cur_step.index} stuck",
                                    self._get_llm(),
                                )
                                if replanned:
                                    _plan_stuck_counter.clear()
                                    web_debug_recorder.write_json(
                                        f"browser_task_plan_revision_{plan.revisions}",
                                        plan.to_debug_payload(),
                                    )
                                    log_agent_action(self.name, f"plan replanned (#{plan.revisions})", decision.reason[:60])
                except Exception as _plan_hook_err:
                    log_warning(f"task plan hook failed: {_plan_hook_err}")

        return await self._build_final_result(
            task, task_intent, expected_url, steps, accumulated_data, seen_keys,
        )

    async def _run_with_strategies(
        self,
        task: str,
        task_intent,
        expected_url: str,
        steps: List[Dict[str, Any]],
        max_steps: int,
    ) -> Dict[str, Any]:
        """Strategy-driven orchestrator (B6).

        Walks the StrategyPicker chain, stopping at the first strategy
        that returns a non-None result. Falls back to the legacy loop
        via the terminal ``LegacyPerStepStrategy`` injected by the picker.
        """
        from agents.browser_strategies import StrategyContext, StrategyPicker

        ctx = StrategyContext(
            task=task,
            task_intent=task_intent,
            expected_url=expected_url,
            steps=steps,
            max_steps=max_steps,
        )
        chain = StrategyPicker().build_chain(self, ctx)
        last_result: Optional[Dict[str, Any]] = None
        final_strategy = None
        for strategy in chain:
            try:
                outcome = await strategy.execute(self, ctx)
            except Exception as exc:
                log_warning(f"strategy {strategy.name} raised: {exc}")
                await strategy.on_failure(self, ctx, f"exception: {exc}")
                continue
            if outcome is None:
                await strategy.on_failure(self, ctx, "yielded (None)")
                continue
            last_result = outcome
            final_strategy = strategy
            break

        if last_result is None:
            last_result = {
                "success": False,
                "message": "no strategy produced a result",
                "url": "", "expected_url": expected_url,
                "steps": steps, "data": [],
            }
        else:
            # B1 tail hook: persist a reusable action template when the run
            # succeeded AND the task intent is one we can meaningfully replay.
            try:
                from utils.browser_template_recorder import record_template_from_run
                record_template_from_run(
                    task_intent=task_intent,
                    steps=steps,
                    final_url=str(last_result.get("url") or expected_url or ""),
                    success=bool(last_result.get("success")),
                )
            except Exception as _tpl_err:
                log_warning(f"template tail hook failed: {_tpl_err}")

        if final_strategy is not None:
            try:
                await final_strategy.on_success(self, ctx, last_result)
            except Exception as _hook_err:
                log_warning(f"strategy {final_strategy.name} on_success raised: {_hook_err}")
        last_result["_strategy"] = getattr(final_strategy, "name", "none")
        last_result["_strategy_chain"] = list(ctx.attempted)
        return last_result

    async def run(self, task: str, start_url: Optional[str] = None, max_steps: int = 8) -> Dict[str, Any]:
        # Reset per-run vision budget
        self._vision_budget = VisionBudget(
            max_calls_per_run=settings.VISION_MAX_CALLS_PER_RUN,
            max_total_tokens=settings.VISION_MAX_TOKENS_PER_RUN,
            cooldown_seconds=settings.VISION_COOLDOWN_SECONDS,
        )
        # Reset per-run assessment cache (B6).
        self._assessment_cache.clear()
        tk = self.toolkit
        # Surface the task description to the perception layer so the
        # vision-cache (B3) can decide whether to bypass for sensitive flows.
        if hasattr(self, "perception") and self.perception is not None:
            try:
                self.perception.current_task = task or ""
            except Exception:
                pass
        expected_url = start_url or self._extract_url_from_task(task) or ""
        steps: List[Dict[str, Any]] = []
        trace = web_debug_recorder.start_trace(
            "browser_agent",
            {
                "task": task,
                "start_url": start_url or "",
                "max_steps": max_steps,
            },
        )
        token = web_debug_recorder.activate_trace(trace)
        if trace:
            log_agent_action(self.name, "网页调试记录已开启", str(trace.root_dir))
            log_warning(f"[DEBUG] 调试文件保存在: {trace.root_dir}")
            log_warning(f"[DEBUG] 你可以查看该目录下的HTML、prompt和response文件来分析感知差异")
        try:
            init = await self._initialize_session(task, expected_url, steps)
            if not init["ok"]:
                return init["result"]
            if init.get("early_result"):
                return init["early_result"]

            task_intent = init["task_intent"]
            # P1: build initial task-level plan (best-effort; failure is non-fatal)
            if settings.BROWSER_PLAN_ENABLED:
                try:
                    from agents.browser_task_plan import build_initial_plan
                    llm_for_plan = self._get_llm()
                    _plan = await build_initial_plan(
                        task,
                        getattr(task_intent, "intent_type", "unknown"),
                        llm_for_plan,
                        start_url=expected_url or "",
                    )
                    if _plan is not None:
                        self.decision._task_plan = _plan
                        web_debug_recorder.write_json("browser_task_plan", _plan.to_debug_payload())
                        log_agent_action(self.name, "任务级 Plan 已生成", f"{len(_plan.steps)} steps")
                except Exception as _plan_err:
                    log_warning(f"task plan initialization skipped: {_plan_err}")

            # B6: strategy-driven orchestration (default-off flag)
            if settings.BROWSER_STRATEGY_REFACTOR_ENABLED:
                log_agent_action(self.name, "strategy refactor enabled", "StrategyPicker")
                return await self._run_with_strategies(
                    task, task_intent, expected_url, steps, max_steps,
                )

            # P4: batch execution mode — one LLM call to plan, execute all, then verify
            if settings.BROWSER_BATCH_EXECUTE_ENABLED:
                log_agent_action(self.name, "batch mode enabled", "一次规划批量执行")
                batch_result = await self._run_batch_mode(
                    task, task_intent, expected_url, steps,
                )
                if batch_result is not None:
                    return batch_result
                log_warning("batch mode returned None, falling back to step-by-step mode")

            return await self._run_per_step_loop(
                task, task_intent, expected_url, steps, max_steps,
            )
        except Exception as exc:
            log_error(f"browser task failed: {exc}")
            url_r = await tk.get_current_url()
            return {"success": False, "message": str(exc), "url": url_r.data or "", "expected_url": expected_url, "steps": steps}
        finally:
            web_debug_recorder.deactivate_trace(token)

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
