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
from urllib.parse import parse_qs, urlparse
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from config.settings import settings
from core.llm import LLMClient
from utils.browser_toolkit import BrowserToolkit, ToolkitResult
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
_PAGE_ASSESSMENT_CONTEXT_TOKENS = 1600
_ACTION_DECISION_CONTEXT_TOKENS = 1400
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

    def _elements_to_debug_payload(self, elements: List[PageElement]) -> List[Dict[str, Any]]:
        return [
            {
                "index": element.index,
                "tag": element.tag,
                "text": element.text,
                "element_type": element.element_type,
                "selector": element.selector,
                "ref": element.ref,
                "role": element.role,
                "attributes": dict(element.attributes or {}),
                "is_visible": element.is_visible,
                "is_clickable": element.is_clickable,
                "context_before": element.context_before,
                "context_after": element.context_after,
                "parent_ref": element.parent_ref,
                "region": element.region,
            }
            for element in elements
        ]

    def _action_to_debug_payload(self, action: Optional[BrowserAction]) -> Dict[str, Any]:
        if action is None:
            return {}
        return {
            "action_type": action.action_type.value,
            "target_selector": action.target_selector,
            "target_ref": action.target_ref,
            "value": action.value,
            "description": action.description,
            "confidence": action.confidence,
            "requires_confirmation": action.requires_confirmation,
            "fallback_selector": action.fallback_selector,
            "use_keyboard_fallback": action.use_keyboard_fallback,
            "keyboard_key": action.keyboard_key,
            "expected_page_type": action.expected_page_type,
            "expected_text": action.expected_text,
        }

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
            candidate = candidate.rstrip(".,);]}>\"'锛屻€傦紒锛燂紱锛氥€侊級锛姐€戯綕銆夈€嬨€嶃€忊€濃€?")
            if candidate:
                raw = raw.replace(candidate, " ")
        return raw

    def _task_mentions_interaction(self, task: str) -> bool:
        normalized = self._normalize_text(self._strip_urls_from_text(task))
        if not normalized:
            return False
        interaction_tokens = (
            "click",
            "tap",
            "press",
            "search",
            "type",
            "input",
            "fill",
            "submit",
            "select",
            "login",
            "log in",
            "sign in",
            "upload",
            "download",
            "scroll",
            "点击",
            "搜索",
            "输入",
            "填写",
            "提交",
            "选择",
            "登录",
            "上传",
            "下载",
            "滚动",
        )
        return any(token in normalized for token in interaction_tokens)

    def _task_mentions_auth(self, task: str) -> bool:
        normalized = self._normalize_text(self._strip_urls_from_text(task))
        if not normalized:
            return False
        auth_tokens = (
            "login",
            "log in",
            "sign in",
            "signin",
            "password",
            "username",
            "account",
            "\u767b\u5f55",
            "\u767b\u5165",
            "\u7528\u6237\u540d",
            "\u8d26\u53f7",
            "\u8d26\u6237",
            "\u5bc6\u7801",
        )
        return any(token in normalized for token in auth_tokens)

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
        normalized_url = (url or "").lower()
        normalized_title = (title or "").lower()
        blocked_url_tokens = (
            "/forbidden",
            "/denied",
            "/captcha",
            "/verify",
            "/challenge",
            "/blocked",
            "/security-check",
            "/sorry",
        )
        blocked_title_tokens = (
            "403",
            "forbidden",
            "access denied",
            "request denied",
            "robot check",
            "security check",
            "captcha",
            "unusual traffic",
            "异常流量",
            "人机身份验证",
            "验证码",
            "安全验证",
            "访问受限",
            "拒绝访问",
        )
        title_blocked = any(token in normalized_title for token in blocked_title_tokens)
        url_blocked = any(token in normalized_url for token in blocked_url_tokens)
        # `/ok.html` is used by some sites as a generic holding/redirect path.
        # Treat it as blocked only when the title or URL also carries denial signals.
        ok_holding_page = "/ok.html" in normalized_url
        if ok_holding_page and not title_blocked:
            ok_holding_page = any(token in normalized_url for token in ("403", "forbidden", "denied", "blocked"))
        return url_blocked or title_blocked or ok_holding_page

    @staticmethod
    def _looks_like_search_results_url(url: str) -> bool:
        return looks_like_search_results_url(url or "")

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
        return "|".join([
            action.action_type.value,
            action.target_ref[:80],
            action.target_selector[:80],
            self._normalize_text(action.value)[:80],
            self._normalize_text(action.description)[:80],
        ])

    def _record_action(self, action: BrowserAction) -> None:
        self._action_history.append(self._action_signature(action))
        self._action_history = self._action_history[-6:]

    def _format_intent_fields_for_llm(self, fields: Optional[Dict[str, str]]) -> str:
        if not fields:
            return "(none)"
        compact = {
            str(key)[:48]: str(value)[:160]
            for key, value in fields.items()
            if key
        }
        return json.dumps(compact, ensure_ascii=False, sort_keys=True)

    def _step_action_signature(self, step: Dict[str, Any]) -> str:
        action_type_raw = str(step.get("action_type") or step.get("plan") or "failed").lower()
        try:
            action_type = ActionType(action_type_raw)
        except ValueError:
            action_type = ActionType.FAILED
        return self._action_signature(
            BrowserAction(
                action_type=action_type,
                target_selector=str(step.get("selector") or step.get("action") or ""),
                target_ref=str(step.get("target_ref") or ""),
                value=str(step.get("value") or ""),
                description=str(step.get("description") or step.get("plan") or ""),
            )
        )

    def _format_recent_steps_for_llm(self, steps: Optional[List[Dict[str, Any]]], max_items: int = 4) -> str:
        if not steps:
            return "(none)"
        lines: List[str] = []
        for step in steps[-max_items:]:
            parts = [f"step={step.get('step', '?')}"]
            action_type = str(step.get("action_type") or step.get("plan") or "unknown")
            parts.append(f"action={action_type[:48]}")
            description = str(step.get("description") or step.get("plan") or "")
            if description and description != action_type:
                parts.append(f"desc={description[:72]}")
            selector = str(step.get("selector") or step.get("action") or "")
            if selector:
                parts.append(f"target={selector[:96]}")
            value = str(step.get("value") or "")
            if value:
                parts.append(f"value={value[:64]}")
            result = str(step.get("result") or step.get("observation") or "")
            if result:
                parts.append(f"result={result[:24]}")
            url = str(step.get("url") or "")
            if url:
                parts.append(f"url={url[:120]}")
            lines.append(" | ".join(parts))
        return "\n".join(lines)

    def _action_requires_direct_target(self, action: BrowserAction) -> bool:
        return action.action_type in {
            ActionType.CLICK,
            ActionType.INPUT,
            ActionType.SELECT,
            ActionType.DOWNLOAD,
            ActionType.UPLOAD_FILE,
            ActionType.SWITCH_IFRAME,
        }

    def _recent_failed_action_matches(
        self,
        action: BrowserAction,
        recent_steps: Optional[List[Dict[str, Any]]],
        max_items: int = 2,
    ) -> bool:
        if not recent_steps:
            return False
        action_sig = self._action_signature(action)
        for step in reversed(recent_steps[-max_items:]):
            if str(step.get("result") or "") != "failed":
                continue
            if self._step_action_signature(step) == action_sig:
                return True
        return False

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
        if action is None:
            return None
        if action.action_type == ActionType.FAILED:
            return None
        if self._recent_failed_action_matches(action, recent_steps):
            return None

        active_intent = intent or TaskIntent(intent_type="read", query="", confidence=0.0)
        active_snapshot = snapshot or self._last_semantic_snapshot or {}
        current_elements = elements or []
        current_data = data or []

        if self._action_requires_direct_target(action) and not (action.target_selector or action.target_ref):
            if action.use_keyboard_fallback and action.keyboard_key:
                return BrowserAction(
                    action_type=ActionType.PRESS_KEY,
                    value=action.keyboard_key,
                    description=action.description or "use keyboard fallback",
                    confidence=action.confidence,
                )
            return None

        if action.action_type == ActionType.WAIT:
            if self._snapshot_is_transient_loading(active_snapshot) or (not current_elements and not current_data):
                if not action.value:
                    action = self._clone_action(action) or action
                    action.value = "1"
                return action
            return None

        if action.action_type == ActionType.DONE:
            if self._task_looks_satisfied(
                task,
                current_url,
                active_intent,
                snapshot=active_snapshot,
                elements=current_elements,
                data=current_data,
            ):
                return action
            return None

        if action.action_type == ActionType.EXTRACT:
            if active_intent.intent_type in {"form", "auth"} and self._interaction_requires_follow_up(
                task,
                active_intent,
                current_elements,
                snapshot=active_snapshot,
            ):
                return None
            if current_data:
                return action
            if self._is_read_only_task(task, active_intent):
                return action
            if self._get_snapshot_main_text(active_snapshot) and not active_intent.requires_interaction:
                return action
            return None

        if action.action_type == ActionType.INPUT and not action.value:
            query = active_intent.query or self._derive_primary_query(task)
            if query:
                action = self._clone_action(action) or action
                action.value = query
                return action
            return None

        return action

    def _is_action_looping(self, action: BrowserAction, threshold: int = 3) -> bool:
        """
        检测动作是否陷入循环

        改进：
        1. 提高阈值从2到3（允许重试一次）
        2. 检查最近的动作序列，而不是整个历史
        3. 只有连续重复才算循环
        """
        # 检查最近5个动作中的重复
        recent_actions = self._action_history[-5:] if len(self._action_history) >= 5 else self._action_history
        action_sig = self._action_signature(action)

        # 统计最近动作中的重复次数
        recent_count = recent_actions.count(action_sig)
        if action.action_type == ActionType.WAIT:
            return recent_count >= max(threshold + 2, 5)

        # 如果最近5个动作中重复3次以上，才判定为循环
        if recent_count >= threshold:
            return True

        # 检查是否连续重复（更严格的循环检测）
        if len(self._action_history) >= 2:
            last_two = self._action_history[-2:]
            if all(sig == action_sig for sig in last_two):
                # 连续3次相同动作才是真正的循环
                return True

        return False

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
        result = await self._call_toolkit("get_current_url")
        if result.success and result.data is not None:
            value = str(result.data or "").strip()
            if value:
                return value
        for attr_name in ("current_url", "_current_url", "_url"):
            value = str(getattr(self.toolkit, attr_name, "") or "").strip()
            if value:
                return value
        return str(fallback or "")

    async def _get_title_value(self, fallback: str = "") -> str:
        result = await self._call_toolkit("get_title")
        if result.success and result.data is not None:
            value = str(result.data or "").strip()
            if value:
                return value
        for attr_name in ("title", "_title"):
            value = str(getattr(self.toolkit, attr_name, "") or "").strip()
            if value:
                return value
        return str(fallback or "")

    async def _get_page_html_value(self) -> str:
        result = await self._call_toolkit("get_page_html")
        if result.success and result.data is not None:
            return str(result.data or "")
        for attr_name in ("html", "_html"):
            value = getattr(self.toolkit, attr_name, None)
            if value:
                return str(value)
        return ""

    def _get_snapshot_blocked_signals(self, snapshot: Optional[Dict[str, Any]]) -> List[str]:
        signals: List[str] = []
        for item in (snapshot or {}).get("blocked_signals", []) or []:
            text = ""
            if isinstance(item, dict):
                text = str(item.get("text", "") or item.get("signal", "") or "").strip()
            else:
                text = str(item or "").strip()
            if text and text not in signals:
                signals.append(text)
        return signals

    def _get_snapshot_visible_text_blocks(self, snapshot: Optional[Dict[str, Any]]) -> List[str]:
        blocks: List[str] = []
        for item in (snapshot or {}).get("visible_text_blocks", []) or []:
            text = ""
            if isinstance(item, dict):
                text = str(item.get("text", "") or "").strip()
            else:
                text = str(item or "").strip()
            if text:
                blocks.append(text)
        return blocks

    def _get_snapshot_main_text(self, snapshot: Optional[Dict[str, Any]]) -> str:
        return str((snapshot or {}).get("main_text", "") or "").strip()

    def _format_snapshot_text_for_llm(self, snapshot: Optional[Dict[str, Any]], max_blocks: int = 6) -> str:
        active_snapshot = snapshot or {}
        lines: List[str] = []
        page_type = str(active_snapshot.get("page_type", "") or "").strip()
        page_stage = str(active_snapshot.get("page_stage", "") or "").strip()
        if page_type or page_stage:
            lines.append(
                "Page snapshot: "
                + ", ".join(
                    part
                    for part in (
                        f"type={page_type}" if page_type else "",
                        f"stage={page_stage}" if page_stage else "",
                    )
                    if part
                )
            )
        blocked_signals = self._get_snapshot_blocked_signals(active_snapshot)
        if blocked_signals:
            lines.append("Blocked signals: " + " | ".join(blocked_signals[:4]))
        main_text = self._get_snapshot_main_text(active_snapshot)
        if main_text:
            lines.append("Main text: " + main_text[:420])
        blocks = self._get_snapshot_visible_text_blocks(active_snapshot)
        if blocks:
            lines.append("Visible text blocks:")
            lines.extend(f"{index}. {text[:220]}" for index, text in enumerate(blocks[:max_blocks], 1))
        return "\n".join(lines).strip()

    def _stringify_llm_response(self, response: Any) -> str:
        if response is None:
            return ""
        content = getattr(response, "content", None)
        if content is not None:
            if isinstance(content, str):
                return content
            try:
                return json.dumps(content, ensure_ascii=False)
            except TypeError:
                return str(content)
        if isinstance(response, str):
            return response
        try:
            return json.dumps(response, ensure_ascii=False)
        except TypeError:
            return str(response)

    async def _build_fallback_semantic_snapshot(self) -> Dict[str, Any]:
        current_url = await self._get_current_url_value()
        title = await self._get_title_value()
        body_text = ""
        visible_text_blocks: List[Dict[str, str]] = []
        evaluate_result = await self._call_toolkit(
            "evaluate_js",
            r"""() => {
                const bodyText = document.body && document.body.innerText ? document.body.innerText : '';
                const blockCandidates = Array.from(document.querySelectorAll('main, article, section, [role="main"], [data-testid], p, h1, h2, h3, li'))
                    .map((node) => {
                        const text = (node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim();
                        if (!text) return null;
                        const rect = node.getBoundingClientRect ? node.getBoundingClientRect() : { width: 0, height: 0 };
                        return {
                            text: text.slice(0, 240),
                            tag: (node.tagName || '').toLowerCase(),
                            role: node.getAttribute ? (node.getAttribute('role') || '') : '',
                            width: Math.round(rect.width || 0),
                            height: Math.round(rect.height || 0),
                        };
                    })
                    .filter(Boolean)
                    .slice(0, 12);
                return {
                    bodyText: bodyText.slice(0, 4000),
                    visibleTextBlocks: blockCandidates,
                };
            }""",
        )
        if evaluate_result.success and isinstance(evaluate_result.data, dict):
            body_text = str(evaluate_result.data.get("bodyText", "") or "").strip()
            raw_blocks = evaluate_result.data.get("visibleTextBlocks", []) or []
            if isinstance(raw_blocks, list):
                for item in raw_blocks[:12]:
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
        blocked_signals: List[str] = []
        if self._looks_like_blocked_page(current_url, title):
            blocked_signals.append(title or current_url)
        combined_text = " ".join([title, body_text]).lower()
        for token in ("unusual traffic", "robot check", "captcha", "异常流量", "人机身份验证", "验证码", "安全验证"):
            if token in combined_text and token not in blocked_signals:
                blocked_signals.append(token)
        looks_like_serp = self._looks_like_search_results_url(current_url)
        page_type = "blocked" if blocked_signals else ("serp" if looks_like_serp else "unknown")
        page_stage = "blocked" if blocked_signals else ("selecting_source" if looks_like_serp else ("extracting" if body_text else "unknown"))
        affordances = {
            "has_results": looks_like_serp and (len(body_text) >= 240 or len(visible_text_blocks) >= 2),
            "collection_item_count": 0,
            "has_modal": False,
            "has_pagination": False,
            "has_load_more": False,
        }
        return {
            "page_type": page_type,
            "page_stage": page_stage,
            "main_text": body_text,
            "visible_text_blocks": visible_text_blocks,
            "blocked_signals": blocked_signals,
            "cards": [],
            "collections": [],
            "controls": [],
            "elements": [],
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

                # 🔥 新增：输出语义快照摘要到控制台
                if web_debug_recorder.is_enabled():
                    log_warning(f"[DEBUG] ========== 语义快照 ==========")
                    log_warning(f"[DEBUG] 页面类型: {self._last_semantic_snapshot.get('page_type', 'unknown')}")
                    log_warning(f"[DEBUG] 页面阶段: {self._last_semantic_snapshot.get('page_stage', 'unknown')}")
                    log_warning(f"[DEBUG] 元素数量: {len(self._last_semantic_snapshot.get('elements', []))}")
                    log_warning(f"[DEBUG] 卡片数量: {len(self._last_semantic_snapshot.get('cards', []))}")
                    log_warning(f"[DEBUG] 集合数量: {len(self._last_semantic_snapshot.get('collections', []))}")
                    main_text = self._last_semantic_snapshot.get('main_text', '')
                    if main_text:
                        log_warning(f"[DEBUG] 主要文本 (前200字符): {main_text[:200]}...")
                    log_warning(f"[DEBUG] ====================================")

                return self._last_semantic_snapshot
        fallback_snapshot = await self._build_fallback_semantic_snapshot()
        self._last_semantic_snapshot = fallback_snapshot or {}
        if self._last_semantic_snapshot:
            web_debug_recorder.write_json("browser_semantic_snapshot", self._last_semantic_snapshot)

            # 🔥 新增：fallback快照也输出到控制台
            if web_debug_recorder.is_enabled():
                log_warning(f"[DEBUG] ========== 语义快照 (fallback) ==========")
                log_warning(f"[DEBUG] 页面类型: {self._last_semantic_snapshot.get('page_type', 'unknown')}")
                log_warning(f"[DEBUG] 元素数量: {len(self._last_semantic_snapshot.get('elements', []))}")
                log_warning(f"[DEBUG] ====================================")

        return self._last_semantic_snapshot

    def _elements_from_snapshot(self, snapshot: Dict[str, Any]) -> List[PageElement]:
        elements: List[PageElement] = []
        for index, item in enumerate((snapshot.get("elements", []) or [])[:60]):
            if not isinstance(item, dict):
                continue
            elements.append(
                PageElement(
                    index=int(item.get("index", index)),
                    tag=str(item.get("tag", "") or ""),
                    text=str(item.get("text", "") or ""),
                    element_type=str(item.get("type", item.get("role", "")) or ""),
                    selector=str(item.get("selector", "") or ""),
                    ref=str(item.get("ref", "") or ""),
                    role=str(item.get("role", "") or ""),
                    attributes={
                        "href": str(item.get("href", "") or ""),
                        "value": str(item.get("value", "") or ""),
                        "placeholder": str(item.get("placeholder", "") or ""),
                        "labelText": str(item.get("label", "") or ""),
                        "ariaLabel": str(item.get("label", "") or ""),
                    },
                    is_visible=bool(item.get("visible", True)),
                    is_clickable=bool(item.get("enabled", True)),
                    parent_ref=str(item.get("parent_ref", "") or ""),
                    region=str(item.get("region", "") or ""),
                )
            )
        return elements

    def _cards_from_snapshot(self, snapshot: Optional[Dict[str, Any]]) -> List[SearchResultCard]:
        cards: List[SearchResultCard] = []
        for item in (snapshot or {}).get("cards", []) or []:
            if not isinstance(item, dict):
                continue
            cards.append(
                SearchResultCard(
                    ref=str(item.get("ref", "") or ""),
                    title=str(item.get("title", "") or ""),
                    target_ref=str(item.get("target_ref", "") or ""),
                    target_selector=str(item.get("target_selector", "") or ""),
                    link=str(item.get("link", "") or ""),
                    raw_link=str(item.get("raw_link", "") or ""),
                    target_url=str(item.get("target_url", "") or ""),
                    snippet=str(item.get("snippet", "") or ""),
                    source=str(item.get("source", "") or ""),
                    host=str(item.get("host", "") or ""),
                    date=str(item.get("date", "") or ""),
                    rank=int(item.get("rank", 0) or 0),
                )
            )
        return cards

    def _collections_from_snapshot(self, snapshot: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        collections: List[Dict[str, Any]] = []
        for item in (snapshot or {}).get("collections", []) or []:
            if not isinstance(item, dict):
                continue
            collections.append(
                {
                    "ref": str(item.get("ref", "") or ""),
                    "kind": str(item.get("kind", "") or ""),
                    "item_count": int(item.get("item_count", 0) or 0),
                    "sample_items": [
                        str(sample or "")
                        for sample in (item.get("sample_items", []) or [])[:5]
                        if str(sample or "").strip()
                    ],
                }
            )
        return collections

    def _get_snapshot_affordances(self, snapshot: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        affordances = (snapshot or {}).get("affordances", {}) or {}
        return affordances if isinstance(affordances, dict) else {}

    def _extract_target_result_count(self, task: str) -> int:
        match = re.search(
            r'(\d+)\s*(?:个|条|款|项|条数据|items?|results?|records?|articles?|stories|news|vulnerabilities?|条漏洞)',
            task or "",
            flags=re.IGNORECASE,
        )
        if not match:
            return 0
        try:
            return max(int(match.group(1)), 0)
        except (TypeError, ValueError):
            return 0

    def _get_snapshot_item_count(self, snapshot: Optional[Dict[str, Any]]) -> int:
        active_snapshot = snapshot or {}
        card_count = len(active_snapshot.get("cards", []) or [])
        collection_count = max(
            (int(item.get("item_count", 0) or 0) for item in self._collections_from_snapshot(active_snapshot)),
            default=0,
        )
        affordances = self._get_snapshot_affordances(active_snapshot)
        affordance_count = int(affordances.get("collection_item_count", 0) or 0)
        return max(card_count, collection_count, affordance_count)

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

    def _normalize_auth_field_name(self, field_name: str) -> str:
        normalized = self._normalize_text(field_name)
        if not normalized:
            return ""
        if any(alias in normalized for alias in _AUTH_PASSWORD_ALIASES):
            return "password"
        if any(alias in normalized for alias in _AUTH_EMAIL_ALIASES):
            return "email"
        if any(alias in normalized for alias in _AUTH_USERNAME_ALIASES):
            return "username"
        return normalized

    def _clean_auth_candidate_value(self, field_name: str, value: str) -> str:
        cleaned = re.sub(r"\s+", " ", str(value or "")).strip(" \"'鈥溾€濃€樷€?")
        cleaned = cleaned.strip(".,;:!?)]}>")
        if not cleaned:
            return ""

        normalized = self._normalize_text(cleaned)
        if not normalized:
            return ""
        if "://" in cleaned or cleaned.startswith("//"):
            return ""
        if normalized in _AUTH_VALUE_NOISE_TOKENS:
            return ""
        if normalized in {
            *[self._normalize_text(item) for item in _AUTH_USERNAME_ALIASES],
            *[self._normalize_text(item) for item in _AUTH_EMAIL_ALIASES],
            *[self._normalize_text(item) for item in _AUTH_PASSWORD_ALIASES],
        }:
            return ""
        if field_name == "email" and "@" not in cleaned:
            return ""
        return cleaned

    def _extract_query_tokens(self, query: str) -> List[str]:
        tokens: List[str] = []
        for token in self._extract_task_tokens(query):
            if token in _QUERY_STOP_TOKENS:
                continue
            if token.isdigit():
                continue
            if len(token) < 3 and not any("\u4e00" <= ch <= "\u9fff" for ch in token):
                continue
            if token not in tokens:
                tokens.append(token)
        return tokens[:8]

    def _score_text_relevance(self, query: str, text: str) -> float:
        haystack = self._normalize_text(text)
        if not haystack:
            return 0.0

        score = 0.0
        query_norm = self._normalize_text(query)
        if query_norm and query_norm in haystack:
            score += 8.0

        token_hits = 0
        strong_hits = 0
        for token in self._extract_query_tokens(query):
            if token not in haystack:
                continue
            token_hits += 1
            weight = 2.0 if len(token) >= 5 or any("\u4e00" <= ch <= "\u9fff" for ch in token) else 1.0
            score += weight
            if weight >= 2.0:
                strong_hits += 1

        if token_hits >= 2:
            score += 2.0
        if strong_hits >= 2:
            score += 2.0
        return score

    def _score_source_authority(self, task: str, host: str, source: str) -> float:
        host_norm = self._normalize_text(host)
        source_norm = self._normalize_text(source)
        task_norm = self._normalize_text(task)
        score = 0.0

        if any(host_norm.endswith(suffix) for suffix in [".gov", ".edu", ".org"]):
            score += 2.2
        if any(token in host_norm for token in ["reuters", "apnews", "bloomberg", "wsj", "ft.com", "bbc", "nytimes"]):
            score += 2.4
        if any(token in source_norm for token in ["reuters", "associated press", "ap ", "bloomberg", "bbc"]):
            score += 1.6
        if any(token in task_norm for token in ["official", "announcement", "statement", "verify", "核实", "声明", "官方"]):
            if any(token in host_norm for token in [".gov", ".edu", ".org", "official", "gov.cn", "state.gov"]):
                score += 1.8
        return score

    def _score_search_result_card(self, task: str, query: str, card: SearchResultCard) -> float:
        haystack = " ".join([card.title, card.snippet, card.source, card.host, card.date])
        score = self._score_text_relevance(query, haystack)
        score += self._score_source_authority(task, card.host, card.source)
        if card.rank > 0:
            score += max(1.2 - ((card.rank - 1) * 0.1), 0.0)
        if any(token in self._normalize_text(card.title + " " + card.snippet) for token in ["官方", "official", "statement", "press release"]):
            score += 1.0
        return score

    def _data_has_substantive_text(self, data: List[Dict[str, str]]) -> bool:
        for item in data[:8]:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text", "") or "").strip()
            if len(text) >= 40:
                return True
            extra_keys = {key for key in item.keys() if key not in {"title", "link", "url", "index"}}
            if extra_keys:
                return True
        return False

    def _search_results_have_answer_evidence(self, query: str, data: List[Dict[str, str]]) -> bool:
        if not data:
            return False
        if not query:
            return self._data_has_substantive_text(data)

        relevant_hits = 0
        for item in data[:8]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "") or "").strip()
            snippet = str(item.get("text", "") or "").strip()
            source = str(item.get("source", "") or "").strip()
            date_hint = str(item.get("date", "") or "").strip()
            haystack = " ".join(part for part in [title, snippet, source, date_hint] if part)
            score = self._score_text_relevance(query, haystack)
            if score >= 6.0:
                relevant_hits += 1
            elif score >= 4.0 and (len(snippet) >= 40 or len(title) >= 24 or source):
                relevant_hits += 1
            if relevant_hits >= 1:
                return True
        return False

    def _strip_search_instruction_phrases(self, value: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            return ""

        cleaned = re.sub(
            r"^(?:browser|web|page)\s+task\s*[:：-]?\s*",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            r"^(?:浏览器任务|网页任务|任务)\s*[:：-]?\s*",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )

        split_patterns = (
            r"\s+(?=(?:wait(?:ing)?(?:\s+for)?|render(?:ing)?|load(?:ing|ed)?|open|visit|navigate|go\s+to|click|input|type|fill|submit|extract|show|display|return|report|summari[sz]e|collect|scrape)\b)",
            r"[\s，,。；;]+(?=(?:等待|渲染|加载|打开|访问|进入|点击|输入|填写|提交|提取|展示|显示|返回|总结|收集|抓取))",
            r"[\s，,。；;]+(?=(?:and then|then|next)\b)",
        )
        for pattern in split_patterns:
            parts = re.split(pattern, cleaned, maxsplit=1, flags=re.IGNORECASE)
            if parts and parts[0].strip():
                cleaned = parts[0].strip()

        return cleaned

    def _refine_search_query(self, task: str, candidate: str = "") -> str:
        raw = str(candidate or task or "")
        raw = re.sub(r"https?://\S+", " ", raw)
        raw = re.sub(r"\bsite:\s*[^\s]+", " ", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\b(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}\b", " ", raw)
        raw = re.sub(
            r"\b(?:from|use|using|via|prefer|preferred|primary|secondary)\b\s+[^\n,.;，。；]{0,120}\b(?:source|site|domain|url)\b",
            " ",
            raw,
            flags=re.IGNORECASE,
        )
        raw = re.sub(
            r"(?:作为|用作)?(?:主要|首选|次要|备用)?(?:来源|站点|域名)[^\n,.;，。；]{0,80}",
            " ",
            raw,
            flags=re.IGNORECASE,
        )
        normalized = re.sub(r"\s+", " ", raw).strip()
        if not normalized:
            return ""
        normalized = self._strip_search_instruction_phrases(normalized)
        if not normalized:
            return ""

        weather_match = re.search(
            r"(?:查询|查|搜索|搜|获取|看看)?\s*([\u4e00-\u9fff]{2,12}?)(?:(今天|明天|后天|当前))?(?:的)?(?:天气|天气预报|气温|空气质量)",
            normalized,
        )
        if weather_match:
            location = weather_match.group(1)
            timeframe = weather_match.group(2) or ""
            return " ".join(part for part in [location, timeframe, "天气"] if part).strip()

        for pattern in (
            r"(?:搜索|查询|查找|查一下|查查|搜一下|搜|获取)([^。！？\n]{2,80})",
            r"(?:search|find|get|look up|query)\s+([^\n.?!]{2,80})",
        ):
            match = re.search(pattern, normalized, flags=re.IGNORECASE)
            if match:
                normalized = match.group(1)
                break

        normalized = self._strip_search_instruction_phrases(normalized)
        stop_tokens = {
            "打开", "浏览器", "访问", "页面", "网页", "网站", "点击", "输入", "搜索", "查询", "查找",
            "提取", "展示", "显示", "查看", "操作", "过程", "结果", "用户", "详细", "详情", "完整",
            "use", "open", "browser", "page", "website", "click", "input", "search", "query",
            "extract", "show", "display", "user", "details", "process", "result", "results", "visible",
            "retrieve", "rendering", "after", "wait", "render", "data", "task", "tasks",
            "loading", "loaded", "load", "fully", "complete", "completed", "summary", "report",
            "等待", "加载", "渲染", "完成", "任务", "步骤", "总结", "收集",
        }
        tokens: List[str] = []
        for token in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9][A-Za-z0-9_+.-]{1,}", normalized):
            lowered = token.lower()
            if lowered in stop_tokens:
                continue
            if lowered.isdigit() and len(lowered) >= 4:
                continue
            if lowered not in tokens:
                tokens.append(lowered)

        if not tokens:
            return ""
        compact_tokens = self._extract_query_tokens(" ".join(tokens))
        if len(compact_tokens) >= 2:
            tokens = compact_tokens
        return " ".join(tokens[:8]).strip()

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
            ref_part = f" ref={element.ref}" if element.ref else ""
            lines.append(f"[{element.index}] type={element.element_type}{ref_part} selector={selector} info={descriptor}")
        return "\n".join(lines)

    def _format_data_for_llm(self, data: List[Dict[str, str]], max_items: int = 8) -> str:
        lines: List[str] = []
        for index, item in enumerate(data[:max_items]):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "") or "")[:100]
            text = str(item.get("text", "") or "")[:220]
            link = str(item.get("link", item.get("url", "")) or "")[:140]
            parts = [part for part in [title, text, link] if part]
            if parts:
                lines.append(f"[{index}] " + " | ".join(parts))
        return "\n".join(lines) or "(no visible data)"

    def _format_cards_for_llm(self, cards: List[SearchResultCard], max_items: int = 8) -> str:
        lines: List[str] = []
        for card in cards[:max_items]:
            parts = [
                card.title[:100],
                card.source[:48],
                card.host[:48],
                card.date[:40],
                card.snippet[:160],
            ]
            payload = " | ".join(part for part in parts if part)
            if payload:
                target = card.target_ref or card.ref
                lines.append(f"[{target}] {payload}")
        if len(cards) > max_items:
            lines.append(f"... {len(cards) - max_items} more cards omitted")
        return "\n".join(lines) or "(no cards)"

    def _format_collections_for_llm(self, snapshot: Optional[Dict[str, Any]], max_items: int = 4) -> str:
        lines: List[str] = []
        all_items = self._collections_from_snapshot(snapshot)
        for item in all_items[:max_items]:
            samples = " | ".join(sample[:120] for sample in item.get("sample_items", [])[:3] if sample)
            lines.append(
                f"[{item.get('ref', '') or 'collection'}] kind={item.get('kind', 'unknown')} "
                f"count={item.get('item_count', 0)} samples={samples or '(none)'}"
            )
        affordances = self._get_snapshot_affordances(snapshot)
        if affordances.get("has_load_more") or affordances.get("has_pagination"):
            controls: List[str] = []
            if affordances.get("has_load_more"):
                controls.append(f"load_more={affordances.get('load_more_ref') or affordances.get('load_more_selector')}")
            if affordances.get("has_pagination"):
                controls.append(f"next_page={affordances.get('next_page_ref') or affordances.get('next_page_selector')}")
            lines.append("controls: " + " | ".join(controls))
        if len(all_items) > max_items:
            lines.append(f"... {len(all_items) - max_items} more collections omitted")
        return "\n".join(lines) or "(no collections)"

    def _format_controls_for_llm(self, snapshot: Optional[Dict[str, Any]], max_items: int = 6) -> str:
        lines: List[str] = []
        controls = (snapshot or {}).get("controls", []) or []
        for control in controls[:max_items]:
            if not isinstance(control, dict):
                continue
            lines.append(
                f"[{str(control.get('ref', '') or 'control')}] "
                f"kind={str(control.get('kind', '') or '')} "
                f"text={str(control.get('text', '') or '')[:96]} "
                f"selector={str(control.get('selector', '') or '')[:72]}"
            )
        if len(controls) > max_items:
            lines.append(f"... {len(controls) - max_items} more controls omitted")
        return "\n".join(lines) or "(no controls)"

    def _format_assessment_elements_for_llm(
        self,
        task: str,
        current_url: str,
        elements: List[PageElement],
        max_items: int = 10,
    ) -> str:
        prioritized = self._prioritize_elements(task, elements, limit=max_items * 2)
        ranked: List[Tuple[float, PageElement]] = []
        for element in prioritized:
            attrs = element.attributes or {}
            score = self._score_element_for_context(task, element)
            href = str(attrs.get("href", "") or "")
            if href and not self._is_search_engine_url(href):
                score += 1.2
            if self._is_search_engine_url(current_url) and href and not self._is_search_engine_url(href):
                score += 1.2
            if attrs.get("value"):
                score += 0.3
            ranked.append((score, element))

        lines: List[str] = []
        seen_selectors = set()
        for _, element in sorted(ranked, key=lambda item: item[0], reverse=True):
            if element.selector in seen_selectors:
                continue
            seen_selectors.add(element.selector)
            attrs = element.attributes or {}
            details = " | ".join(
                part for part in [
                    element.text[:60],
                    attrs.get("labelText", "")[:48],
                    attrs.get("placeholder", "")[:48],
                    attrs.get("value", "")[:48],
                    attrs.get("href", "")[:100],
                ] if part
            )

            # 🔥 新增：包含上下文信息
            context_parts = []
            if element.context_before:
                context_parts.append(f"before: {element.context_before[:80]}")
            if element.context_after:
                context_parts.append(f"after: {element.context_after[:80]}")

            context_str = " | ".join(context_parts) if context_parts else ""

            ref_part = f" ref={element.ref}" if element.ref else ""
            line = f"[{element.index}] type={element.element_type}{ref_part} selector={element.selector[:72]} info={details}"
            if context_str:
                line += f" | context: {context_str}"

            lines.append(line)
            if len(lines) >= max_items:
                break
        total_candidates = len(seen_selectors)
        if len(ranked) > total_candidates:
            lines.append(f"... {len(ranked) - total_candidates} more candidate elements omitted")
        return "\n".join(lines) or "(no actionable elements)"

    def _build_budgeted_browser_prompt_context(
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
        data_text = self._format_data_for_llm(data, max_items=12)
        snapshot_text = self._format_snapshot_text_for_llm(snapshot)
        if snapshot_text:
            if data_text and data_text != "(no visible data)":
                data_text = f"{snapshot_text}\n{data_text}"
            else:
                data_text = snapshot_text
        cards_text = self._format_cards_for_llm(cards, max_items=14)
        collections_text = self._format_collections_for_llm(snapshot, max_items=6)
        controls_text = self._format_controls_for_llm(snapshot, max_items=6)
        rendered, report = render_budgeted_sections(
            [
                BudgetSection(
                    name="data",
                    text=data_text,
                    min_chars=240,
                    max_chars=1100,
                    weight=0.8,
                    mode="lines",
                    omission_label="data lines",
                ),
                BudgetSection(
                    name="cards",
                    text=cards_text,
                    min_chars=480,
                    max_chars=1700,
                    weight=1.4,
                    mode="lines",
                    omission_label="card lines",
                ),
                BudgetSection(
                    name="collections",
                    text=collections_text,
                    min_chars=260,
                    max_chars=900,
                    weight=0.9,
                    mode="lines",
                    omission_label="collection lines",
                ),
                BudgetSection(
                    name="controls",
                    text=controls_text,
                    min_chars=180,
                    max_chars=700,
                    weight=0.8,
                    mode="lines",
                    omission_label="control lines",
                ),
                BudgetSection(
                    name="elements",
                    text=elements_text,
                    min_chars=520,
                    max_chars=1900,
                    weight=1.5,
                    mode="lines",
                    omission_label="element lines",
                ),
            ],
            total_tokens=total_tokens,
            model=self._get_llm(),
        )
        report["context"] = {
            "task": task[:160],
            "current_url": current_url[:160],
            "total_budget_tokens": total_tokens,
        }
        coverage_parts = []
        for name in ("data", "cards", "collections", "controls", "elements"):
            item = report.get(name, {})
            requested = int(item.get("requested_chars", 0) or 0)
            used = int(item.get("used_chars", 0) or 0)
            if requested <= 0:
                coverage_parts.append(f"{name}=none")
                continue
            coverage_parts.append(f"{name}={used}/{requested} chars")
        rendered["context_coverage"] = "; ".join(coverage_parts)
        return rendered, report

    def _clone_action(self, action: Optional[BrowserAction]) -> Optional[BrowserAction]:
        if action is None:
            return None
        return BrowserAction(
            action_type=action.action_type,
            target_selector=action.target_selector,
            target_ref=action.target_ref,
            value=action.value,
            description=action.description,
            confidence=action.confidence,
            requires_confirmation=action.requires_confirmation,
            fallback_selector=action.fallback_selector,
            use_keyboard_fallback=action.use_keyboard_fallback,
            keyboard_key=action.keyboard_key,
            expected_page_type=action.expected_page_type,
            expected_text=action.expected_text,
        )

    def _page_assessment_cache_key(
        self,
        task: str,
        current_url: str,
        title: str,
        intent: Optional[TaskIntent],
        data: List[Dict[str, str]],
        elements: List[PageElement],
        last_action: Optional[BrowserAction] = None,
        recent_steps: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        active_intent = intent or TaskIntent(intent_type="read", query="", confidence=0.0)
        cards = self._cards_from_snapshot(self._last_semantic_snapshot)
        payload = {
            "task": self._normalize_text(task)[:240],
            "url": current_url[:220],
            "title": title[:120],
            "page_type": str((self._last_semantic_snapshot or {}).get("page_type", "") or ""),
            "page_stage": self._infer_page_state(task, current_url, active_intent, data, self._last_semantic_snapshot).stage,
            "intent": active_intent.intent_type,
            "query": active_intent.query[:160],
            "fields": self._format_intent_fields_for_llm(active_intent.fields),
            "last_action": self._action_signature(last_action) if last_action else "",
            "recent_steps": [
                {
                    "step": step.get("step"),
                    "action_type": step.get("action_type"),
                    "selector": str(step.get("selector") or step.get("action") or "")[:80],
                    "result": str(step.get("result") or "")[:16],
                    "url": str(step.get("url") or "")[:120],
                }
                for step in (recent_steps or [])[-3:]
            ],
            "data": [
                {
                    "title": str(item.get("title", "") or "")[:80],
                    "text": str(item.get("text", "") or "")[:140],
                    "link": str(item.get("link", item.get("url", "")) or "")[:120],
                }
                for item in (data or [])[:8]
                if isinstance(item, dict)
            ],
            "elements": [
                {
                    "index": element.index,
                    "type": element.element_type,
                    "selector": element.selector[:80],
                    "text": element.text[:80],
                    "href": str((element.attributes or {}).get("href", "") or "")[:120],
                    "value": str((element.attributes or {}).get("value", "") or "")[:80],
                }
                for element in (elements or [])[:10]
            ],
            "cards": [
                {
                    "ref": card.target_ref or card.ref,
                    "title": card.title[:100],
                    "source": card.source[:40],
                    "host": card.host[:40],
                }
                for card in cards[:6]
            ],
            "collections": self._collections_from_snapshot(self._last_semantic_snapshot)[:4],
        }
        return hashlib.sha1(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def _should_assess_page_with_llm(
        self,
        task: str,
        current_url: str,
        intent: Optional[TaskIntent],
        data: List[Dict[str, str]],
        elements: List[PageElement],
        last_action: Optional[BrowserAction] = None,
    ) -> bool:
        if not data and not elements:
            return False
        active_intent = intent or TaskIntent(intent_type="read", query="", confidence=0.0)
        if active_intent.intent_type in {"form", "auth"} and elements:
            return True
        if data and self._is_read_only_task(task, active_intent):
            return True
        if self._is_search_engine_url(current_url):
            query = active_intent.query or self._derive_primary_query(task)
            return bool(data) or self._search_input_matches_query(elements, query)
        if data and (self._data_has_substantive_text(data) or len(data) >= 2):
            return True
        if last_action and last_action.action_type in {ActionType.INPUT, ActionType.CLICK} and data:
            return True
        return False

    async def _assess_page_with_llm(
        self,
        task: str,
        current_url: str,
        title: str,
        elements: List[PageElement],
        intent: Optional[TaskIntent],
        data: List[Dict[str, str]],
        last_action: Optional[BrowserAction] = None,
        recent_steps: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[BrowserAction]:
        if not self._should_assess_page_with_llm(task, current_url, intent, data, elements, last_action):
            return None

        if not self._last_semantic_snapshot:
            await self._get_semantic_snapshot()
        cache_key = self._page_assessment_cache_key(
            task,
            current_url,
            title,
            intent,
            data,
            elements,
            last_action,
            recent_steps,
        )
        if cache_key in self._page_assessment_cache:
            web_debug_recorder.record_event(
                "browser_page_assessment_cache_hit",
                cache_key=cache_key,
                url=current_url,
            )
            return self._clone_action(self._page_assessment_cache[cache_key])

        try:
            active_intent = intent or TaskIntent(intent_type="read", query="", confidence=0.0)
            snapshot = self._last_semantic_snapshot or await self._get_semantic_snapshot()
            cards = self._cards_from_snapshot(snapshot)
            page_state = self._infer_page_state(task, current_url, active_intent, data, snapshot)
            elements_text = self._format_assessment_elements_for_llm(task, current_url, elements, max_items=18)
            prompt_context, prompt_budget = self._build_budgeted_browser_prompt_context(
                task=task,
                current_url=current_url,
                data=data,
                cards=cards,
                snapshot=snapshot,
                elements_text=elements_text,
                total_tokens=_PAGE_ASSESSMENT_CONTEXT_TOKENS,
            )
            llm = self._get_llm()
            prompt = PAGE_ASSESSMENT_PROMPT.format(
                task=task or "",
                intent=active_intent.intent_type,
                query=active_intent.query or self._derive_primary_query(task),
                fields=self._format_intent_fields_for_llm(active_intent.fields),
                url=current_url or "",
                title=title or "",
                page_type=page_state.page_type,
                page_stage=page_state.stage,
                last_action=(last_action.description or last_action.action_type.value) if last_action else "none",
                recent_steps=self._format_recent_steps_for_llm(recent_steps),
                context_coverage=prompt_context.get("context_coverage", ""),
                data=prompt_context.get("data", "(no visible data)"),
                cards=prompt_context.get("cards", "(no cards)"),
                collections=prompt_context.get("collections", "(no collections)"),
                controls=prompt_context.get("controls", "(no controls)"),
                elements=prompt_context.get("elements", "(no actionable elements)"),
            )
            if web_debug_recorder.is_enabled():
                page_html = await self._get_page_html_value()
                web_debug_recorder.write_text("browser_page_html", page_html, suffix=".html")
                # 🔥 新增：输出HTML摘要到控制台
                html_preview = page_html[:1000] if page_html else "(empty)"
                log_warning(f"[DEBUG] 页面HTML (前1000字符): {html_preview}...")
                log_warning(f"[DEBUG] 页面HTML总长度: {len(page_html)} 字符")
            web_debug_recorder.write_json(
                "browser_page_assessment_context",
                {
                    "task": task,
                    "url": current_url,
                    "title": title,
                    "intent": {
                        "intent_type": active_intent.intent_type,
                        "query": active_intent.query,
                        "confidence": active_intent.confidence,
                        "fields": active_intent.fields,
                        "requires_interaction": active_intent.requires_interaction,
                        "target_text": active_intent.target_text,
                    },
                    "page_state": {
                        "page_type": page_state.page_type,
                        "stage": page_state.stage,
                        "confidence": page_state.confidence,
                        "item_count": page_state.item_count,
                        "target_count": page_state.target_count,
                        "has_pagination": page_state.has_pagination,
                        "has_load_more": page_state.has_load_more,
                        "has_modal": page_state.has_modal,
                        "goal_satisfied": page_state.goal_satisfied,
                    },
                    "data": data,
                    "cards": [card.__dict__ for card in cards],
                    "elements": self._elements_to_debug_payload(elements),
                    "snapshot": snapshot,
                    "last_action": self._action_to_debug_payload(last_action),
                    "recent_steps": (recent_steps or [])[-4:],
                    "prompt_budget": prompt_budget,
                },
            )
            web_debug_recorder.write_text("browser_page_assessment_prompt", prompt)
            web_debug_recorder.write_json("browser_page_assessment_budget", prompt_budget)

            # 🔥 新增：输出prompt摘要到控制台
            if web_debug_recorder.is_enabled():
                log_warning(f"[DEBUG] 页面评估 Prompt (前800字符): {prompt[:800]}...")
                log_warning(f"[DEBUG] 页面评估 Prompt总长度: {len(prompt)} 字符")
                log_warning(f"[DEBUG] 元素数量: {len(elements)}, 数据条数: {len(data)}")

            response = await llm.achat(
                messages=[
                    {"role": "system", "content": "Return JSON only."},
                    {
                        "role": "user",
                        "content": prompt,
                    },
                ],
                temperature=0.1,
                json_mode=True,
            )
            web_debug_recorder.write_text("browser_page_assessment_response", self._stringify_llm_response(response))
            payload = llm.parse_json_response(response)
            web_debug_recorder.write_json("browser_page_assessment_payload", payload)

            # 🔥 新增：输出到控制台，方便实时查看
            if web_debug_recorder.is_enabled():
                log_warning(f"[DEBUG] 页面评估 LLM 响应: {self._stringify_llm_response(response)[:500]}...")
                log_warning(f"[DEBUG] 页面评估 payload: {json.dumps(payload, ensure_ascii=False)[:500]}...")

            action = self._action_from_llm(payload, elements)
            web_debug_recorder.write_json(
                "browser_page_assessment_action",
                self._action_to_debug_payload(action),
            )
            if action.action_type == ActionType.FAILED:
                self._page_assessment_cache[cache_key] = None
                return None

            if (
                page_state.page_type == "list"
                and page_state.target_count
                and len(data or []) < page_state.target_count
                and (page_state.has_pagination or page_state.has_load_more)
            ):
                if action.action_type in {ActionType.EXTRACT, ActionType.DONE, ActionType.WAIT}:
                    state_action = self._choose_snapshot_navigation_action(
                        task,
                        current_url,
                        elements,
                        active_intent,
                        data,
                        snapshot,
                    )
                    if state_action is not None:
                        action = state_action

            query = active_intent.query or self._derive_primary_query(task)
            if action.action_type == ActionType.INPUT and self._search_input_matches_query(elements, action.value or query):
                submit_control = self._find_primary_submit_control(elements)
                if submit_control is not None:
                    action = BrowserAction(
                        action_type=ActionType.CLICK,
                        target_selector=submit_control.selector,
                        description="submit assessed search query",
                        confidence=max(action.confidence, 0.45),
                        use_keyboard_fallback=True,
                        keyboard_key="Enter",
                    )
                else:
                    action = BrowserAction(
                        action_type=ActionType.PRESS_KEY,
                        value="Enter",
                        description="submit assessed search query",
                        confidence=max(action.confidence, 0.35),
                    )

            self._page_assessment_cache[cache_key] = self._clone_action(action)
            return self._clone_action(action)
        except Exception as exc:
            log_warning(f"LLM page assessment failed: {exc}")
            return None

    # ── element extraction (Agent's "eyes", uses toolkit.evaluate_js) ──

    async def _extract_interactive_elements(self) -> List[PageElement]:
        snapshot = await self._get_semantic_snapshot()
        snapshot_elements = self._elements_from_snapshot(snapshot)
        if snapshot_elements:
            elements = self._filter_noise_elements(snapshot_elements)
            self._element_cache = elements[:40]
            return self._element_cache

        r = await self._call_toolkit(
            "evaluate_js",
            r"""
            () => {
              // 🔥 扩展选择器：同时提取交互元素和内容元素
              const interactiveNodes = Array.from(document.querySelectorAll('a, button, input, textarea, select, [role="button"], [role="link"], [contenteditable="true"]'));

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

              // 🔥 新增：提取元素周围的上下文文本
              function extractContext(el) {
                const contextBefore = [];
                const contextAfter = [];

                // 向前查找文本节点（最多3个兄弟节点）
                let prev = el.previousSibling;
                let count = 0;
                while (prev && count < 3) {
                  if (prev.nodeType === Node.TEXT_NODE) {
                    const text = textOf(prev);
                    if (text.length > 0) {
                      contextBefore.unshift(text);
                      count++;
                    }
                  } else if (prev.nodeType === Node.ELEMENT_NODE) {
                    const text = textOf(prev);
                    if (text.length > 0 && text.length < 200) {
                      contextBefore.unshift(text);
                      count++;
                    }
                  }
                  prev = prev.previousSibling;
                }

                // 向后查找文本节点（最多3个兄弟节点）
                let next = el.nextSibling;
                count = 0;
                while (next && count < 3) {
                  if (next.nodeType === Node.TEXT_NODE) {
                    const text = textOf(next);
                    if (text.length > 0) {
                      contextAfter.push(text);
                      count++;
                    }
                  } else if (next.nodeType === Node.ELEMENT_NODE) {
                    const text = textOf(next);
                    if (text.length > 0 && text.length < 200) {
                      contextAfter.push(text);
                      count++;
                    }
                  }
                  next = next.nextSibling;
                }

                // 如果兄弟节点没有上下文，尝试从父元素提取
                if (contextBefore.length === 0 && contextAfter.length === 0) {
                  const parent = el.parentElement;
                  if (parent) {
                    const parentText = textOf(parent);
                    const elementText = textOf(el);
                    // 提取父元素中不属于当前元素的文本
                    const beforeText = parentText.split(elementText)[0];
                    const afterText = parentText.split(elementText)[1];
                    if (beforeText && beforeText.length > 0) {
                      contextBefore.push(beforeText.slice(-100));
                    }
                    if (afterText && afterText.length > 0) {
                      contextAfter.push(afterText.slice(0, 100));
                    }
                  }
                }

                return {
                  before: contextBefore.join(' ').slice(0, 150),
                  after: contextAfter.join(' ').slice(0, 150)
                };
              }

              return interactiveNodes
                .filter(el => isVisible(el))
                .slice(0, 60)
                .map((el, idx) => {
                  const context = extractContext(el);
                  return {
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
                      value: (typeof el.value === 'string' ? el.value : '') || '',
                      placeholder: el.getAttribute('placeholder') || '',
                      ariaLabel: el.getAttribute('aria-label') || '',
                      title: el.getAttribute('title') || '',
                      labelText: labelOf(el).slice(0, 120),
                    },
                    is_visible: true,
                    is_clickable: !el.disabled,
                    context_before: context.before,
                    context_after: context.after,
                  };
                });
            }
            """
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
        # Filter out very low-confidence matches (only task token hits, no keyword match)
        if keywords:
            min_score = 2.0
            filtered = [item for item in matches if item[0] >= min_score]
            if filtered:
                return [item[1] for item in filtered]
        return [item[1] for item in matches]

    def _find_best_element(self, task: str, elements: List[PageElement],
                           kinds: Optional[List[str]] = None, keywords: Optional[List[str]] = None,
                           exclude_selectors: Optional[List[str]] = None) -> Optional[PageElement]:
        ranked = self._find_ranked_elements(task, elements, kinds=kinds, keywords=keywords, exclude_selectors=exclude_selectors)
        return ranked[0] if ranked else None

    def _derive_primary_query(self, task: str) -> str:
        refined = self._refine_search_query(task)
        if refined:
            return refined
        normalized = re.sub(r"https?://\S+", " ", task or "")
        normalized = re.sub(r"[^\w\u4e00-\u9fff]+", " ", normalized, flags=re.UNICODE)
        chunks = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9][A-Za-z0-9_+.-]{1,}", normalized)
        if chunks:
            return " ".join(chunks[:8]).strip()
        return ""

    def _build_form_fill_action(self, mapping: Dict[str, str]) -> BrowserAction:
        return BrowserAction(
            action_type=ActionType.FILL_FORM,
            value=json.dumps(mapping, ensure_ascii=False),
            description="fill form fields",
            confidence=0.9,
        )

    def _element_action_haystack(self, element: PageElement) -> str:
        attrs = element.attributes or {}
        return self._normalize_text(
            " ".join(
                [
                    element.text,
                    attrs.get("labelText", ""),
                    attrs.get("ariaLabel", ""),
                    attrs.get("title", ""),
                    attrs.get("name", ""),
                    attrs.get("id", ""),
                    attrs.get("value", ""),
                    attrs.get("type", ""),
                ]
            )
        )

    def _extract_click_target_text(self, task: str) -> str:
        clean_patterns = (
            r'"([^"\n]{2,64})"',
            r"'([^'\n]{2,64})'",
            r"“([^”\n]{2,64})”",
            r"‘([^’\n]{2,64})’",
            r"「([^」\n]{2,64})」",
            r"『([^』\n]{2,64})』",
            r"《([^》\n]{2,64})》",
        )
        for pattern in clean_patterns:
            match = re.search(pattern, task or "")
            if not match:
                continue
            value = self._normalize_text(match.group(1))
            if len(value) >= 2:
                return value
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
        return extract_first_url(task) or None

    def _extract_auth_fields_from_free_text(self, task: str) -> Dict[str, str]:
        text = self._strip_urls_from_text(task)
        clean_patterns = {
            "username": [
                r"(?:\u767b\u5f55\u8d26\u53f7|\u767b\u5f55\u540d|\u7528\u6237\u540d|\u8d26\u53f7|\u8d26\u6237|\u5e10\u53f7|username|user\s*name|login\s*name|login\s*account|account)\s*(?:is|=|:|\u662f|\u4e3a)?\s*[\"'“”‘’]?([A-Za-z0-9_.@-]{2,})",
            ],
            "email": [
                r"(?:email|e-mail|mail|\u90ae\u7bb1|\u7535\u5b50\u90ae\u7bb1)\s*(?:is|=|:|\u662f|\u4e3a)?\s*[\"'“”‘’]?([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})",
            ],
            "password": [
                r"(?:password|passcode|passwd|pwd|\u5bc6\u7801|\u767b\u5f55\u5bc6\u7801)\s*(?:is|=|:|\u662f|\u4e3a)?\s*[\"'“”‘’]?([^\s,，。；;]+)",
            ],
        }
        clean_extracted: Dict[str, str] = {}
        for field_name, regexes in clean_patterns.items():
            for pattern in regexes:
                candidates: List[str] = []
                for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                    value = self._clean_auth_candidate_value(field_name, str(match.group(1) or ""))
                    if value:
                        candidates.append(value)
                if candidates:
                    clean_extracted[field_name] = candidates[-1]
                    break
        if clean_extracted:
            return clean_extracted
        patterns = {
            "username": [
                r"(?:\u767b\u5f55\u8d26\u53f7|\u767b\u5f55\u540d|\u7528\u6237\u540d|\u8d26\u53f7|\u8d26\u6237|\u5e10\u53f7|username|user\s*name|login\s*name|login\s*account|account)\s*(?:is|=|:|\u662f|\u4e3a)\s*[\"'“”‘’]?([^\s,，。;；]+)",
                r"(?:\u767b\u5f55\u8d26\u53f7|\u767b\u5f55\u540d|\u7528\u6237\u540d|\u8d26\u53f7|\u8d26\u6237|\u5e10\u53f7|username|user\s*name|login\s*name|login\s*account|account)\s*[\"'“”‘’]?([A-Za-z0-9_.@-]{2,})",
            ],
            "email": [
                r"(?:email|e-mail|mail|\u90ae\u7bb1|\u7535\u5b50\u90ae\u7bb1)\s*(?:is|=|:|\u662f|\u4e3a)\s*[\"'“”‘’]?([^\s,，。;；]+)",
                r"(?:email|e-mail|mail|\u90ae\u7bb1|\u7535\u5b50\u90ae\u7bb1)\s*[\"'“”‘’]?([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})",
            ],
            "password": [
                r"(?:password|passcode|passwd|pwd|\u5bc6\u7801|\u767b\u5f55\u5bc6\u7801)\s*(?:is|=|:|\u662f|\u4e3a)\s*[\"'“”‘’]?([^\s,，。;；]+)",
                r"(?:password|passcode|passwd|pwd|\u5bc6\u7801|\u767b\u5f55\u5bc6\u7801)\s*[\"'“”‘’]?([^\s,，。;；]{2,})",
            ],
        }
        extracted: Dict[str, str] = {}
        for field_name, regexes in patterns.items():
            for pattern in regexes:
                candidates: List[str] = []
                for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                    value = self._clean_auth_candidate_value(field_name, str(match.group(1) or ""))
                    if value:
                        candidates.append(value)
                if candidates:
                    extracted[field_name] = candidates[-1]
                    break
        return extracted

    def _extract_structured_pairs(self, task: str) -> Dict[str, str]:
        pairs: Dict[str, str] = {}
        stripped_task = self._strip_urls_from_text(task)
        for key, value in re.findall(
            r"([A-Za-z0-9_\u4e00-\u9fff]{1,24})\s*[:：]\s*(.{1,160}?)(?=(?:\s+[A-Za-z0-9_\u4e00-\u9fff]{1,24}\s*[:：])|[\n,，;；]|$)",
            stripped_task,
        ):
            normalized_key = self._normalize_auth_field_name(key)
            cleaned_value = re.sub(r"\s+", " ", value).strip()
            if not normalized_key or normalized_key in _STRUCTURED_PAIR_SKIP_KEYS:
                continue
            if normalized_key.isdigit():
                continue
            if not cleaned_value or "://" in cleaned_value or cleaned_value.startswith("//"):
                continue
            if normalized_key and cleaned_value:
                pairs[normalized_key] = cleaned_value
        auth_pairs = self._extract_auth_fields_from_free_text(task)
        if auth_pairs:
            for key in list(pairs.keys()):
                if self._normalize_auth_field_name(key) in auth_pairs:
                    pairs.pop(key, None)
            pairs.update(auth_pairs)
        return pairs

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
        auth_like = self._task_mentions_auth(task) or "password" in field_kinds

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
        candidates: List[PageElement] = []
        for element in elements:
            if not element.is_visible or not element.is_clickable:
                continue
            attrs = element.attributes or {}
            normalized_type = self._normalize_text(
                attrs.get("type", "") or element.element_type or element.tag
            )
            if element.tag == "textarea" or normalized_type == "textarea":
                candidates.append(element)
                continue
            if element.tag == "input" and normalized_type not in _NON_TEXT_INPUT_TYPES:
                candidates.append(element)
                continue
            if element.element_type in {"input", "text", "search", "email", "password"} and normalized_type not in _NON_TEXT_INPUT_TYPES:
                candidates.append(element)
        return candidates

    def _field_match_score(self, field_name: str, element: PageElement) -> float:
        attrs = element.attributes or {}
        element_type = self._normalize_text(
            attrs.get("type", "") or element.element_type or element.tag
        )
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
        canonical_field = self._normalize_auth_field_name(field_name)
        for token in self._extract_task_tokens(field_name):
            if token and token in haystack:
                score += 2.0
        if element.element_type in {"text", "search", "email", "password", "textarea"}:
            score += 0.8
        if attrs.get("name"):
            score += 0.2
        if canonical_field == "password":
            if element_type == "password":
                score += 6.0
            else:
                score -= 3.0
            if any(alias in haystack for alias in _AUTH_PASSWORD_ALIASES):
                score += 4.0
        elif canonical_field in {"username", "email"}:
            if element_type == "password":
                score -= 5.0
            if canonical_field == "email" and element_type == "email":
                score += 4.0
            elif element_type in {"text", "search", "email", "input", "textarea"}:
                score += 2.5
            alias_pool = _AUTH_EMAIL_ALIASES if canonical_field == "email" else _AUTH_USERNAME_ALIASES + _AUTH_EMAIL_ALIASES
            if any(alias in haystack for alias in alias_pool):
                score += 4.0
        return score

    def _mapping_matches_current_elements(
        self,
        mapping: Dict[str, str],
        elements: List[PageElement],
    ) -> bool:
        if not mapping:
            return False
        element_by_selector = {
            element.selector: element
            for element in elements
            if element.selector
        }
        matched = 0
        for selector, expected in mapping.items():
            element = element_by_selector.get(selector)
            if element is None:
                continue
            current_value = str((element.attributes or {}).get("value", "") or "")
            if self._normalize_text(current_value) == self._normalize_text(str(expected)):
                matched += 1
        return matched >= max(1, len(mapping))

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
        if not controls:
            return None

        ranked: List[Tuple[float, PageElement]] = []
        for control in controls:
            attrs = control.attributes or {}
            selector = str(control.selector or "")
            haystack = self._element_action_haystack(control)
            score = 0.0
            if self._normalize_text(attrs.get("type", "")) == "submit":
                score += 5.0
            if control.tag == "button":
                score += 1.2
            if any(token in haystack for token in _AUTH_SUBMIT_POSITIVE_TOKENS):
                score += 4.0
            if any(token in haystack for token in _AUTH_SUBMIT_NEGATIVE_TOKENS):
                score -= 6.0
            if any(token in haystack for token in _AUTH_SECONDARY_PROVIDER_TOKENS):
                score -= 4.0
            if selector and "form > button" in selector:
                score += 1.5
            score += max(0.0, 1.0 - 0.1 * float(control.index))
            ranked.append((score, control))

        ranked.sort(key=lambda item: item[0], reverse=True)
        return ranked[0][1] if ranked else controls[0]

    def _find_auth_submit_control(
        self,
        task: str,
        elements: List[PageElement],
        intent: Optional[TaskIntent] = None,
    ) -> Optional[PageElement]:
        controls = [
            item
            for item in elements
            if item.is_visible
            and item.is_clickable
            and (
                item.element_type in {"button", "submit", "link"}
                or item.tag in {"button", "a"}
                or self._normalize_text((item.attributes or {}).get("type", "")) in {"submit", "button"}
            )
        ]
        if not controls:
            return None

        target_hint = self._normalize_text((intent.target_text if intent else "") or "")
        ranked: List[Tuple[float, PageElement]] = []
        for control in controls:
            score = 0.0
            attrs = control.attributes or {}
            haystack = self._element_action_haystack(control)
            selector = str(control.selector or "")
            selector_depth = selector.count(">")
            if self._normalize_text(attrs.get("type", "")) == "submit":
                score += 2.5
            if any(token in haystack for token in _AUTH_SUBMIT_POSITIVE_TOKENS):
                score += 5.0
            if any(token in haystack for token in _AUTH_SUBMIT_NEGATIVE_TOKENS):
                score -= 6.0
            if any(token in haystack for token in _AUTH_SECONDARY_PROVIDER_TOKENS):
                score -= 4.0
            if target_hint and target_hint in haystack:
                score += 4.0
            if self._task_mentions_auth(task) and any(token in haystack for token in ("login", "\u767b\u5f55", "\u767b\u5165")):
                score += 2.0
            if selector:
                if "form > button" in selector:
                    score += 2.5
                score += max(0.0, 1.2 - 0.15 * selector_depth)
                score += max(0.0, 1.0 - 0.01 * len(selector))
            score += max(0.0, 1.0 - 0.1 * float(control.index))
            if score > 0:
                ranked.append((score, control))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return ranked[0][1] if ranked else None

    def _find_submit_control_for_intent(
        self,
        task: str,
        elements: List[PageElement],
        intent: Optional[TaskIntent] = None,
    ) -> Optional[PageElement]:
        active_intent = intent or TaskIntent(intent_type="form", query="", confidence=0.0)
        primary_submit = self._find_primary_submit_control(elements)
        if active_intent.intent_type != "auth" and not self._task_mentions_auth(task):
            return primary_submit

        auth_submit = self._find_auth_submit_control(task, elements, active_intent)
        if primary_submit is None:
            return auth_submit
        if auth_submit is None:
            return primary_submit

        primary_haystack = self._element_action_haystack(primary_submit)
        auth_haystack = self._element_action_haystack(auth_submit)
        auth_is_secondary = any(token in auth_haystack for token in _AUTH_SECONDARY_PROVIDER_TOKENS)
        primary_is_secondary = any(token in primary_haystack for token in _AUTH_SECONDARY_PROVIDER_TOKENS)
        if auth_is_secondary and not primary_is_secondary:
            return primary_submit
        if primary_submit.tag == "button" and auth_submit.tag == "a":
            return primary_submit
        if primary_submit.index <= auth_submit.index:
            return primary_submit
        return auth_submit

    def _interaction_requires_follow_up(
        self,
        task: str,
        intent: Optional[TaskIntent],
        elements: List[PageElement],
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> bool:
        active_intent = intent or TaskIntent(intent_type="form", query="", confidence=0.0)
        if active_intent.intent_type not in {"form", "auth"}:
            return False

        active_snapshot = snapshot or {}
        page_type = str(active_snapshot.get("page_type", "") or "")
        page_stage = str(active_snapshot.get("page_stage", "") or "")
        if page_type in {"form", "login"} or page_stage == "interacting":
            return True

        if not elements:
            return False

        mapping = self._build_form_mapping_from_pairs(active_intent.fields, elements)
        if not mapping or not self._mapping_matches_current_elements(mapping, elements):
            return False

        submit_control = self._find_submit_control_for_intent(task, elements, active_intent)
        return submit_control is not None

    async def _bootstrap_search_results(self, query: str) -> bool:
        if not query:
            return False
        for profile, search_url in build_direct_search_urls(query):
            result = await self.toolkit.goto(search_url, timeout=30000)
            if not result.success:
                continue
            await self._wait_for_page_ready()
            ready = await self._wait_for_search_results_ready(search_url)
            if ready:
                log_agent_action(self.name, f"bootstrap_search:{profile.name}", query[:120])
                return True
        return False

    def _search_input_matches_query(self, elements: List[PageElement], query: str) -> bool:
        if not query:
            return False
        input_element = self._find_primary_text_input(elements)
        if input_element is None:
            return False
        current_value = str((input_element.attributes or {}).get("value", "") or "").strip()
        if not current_value:
            return False
        return self._normalize_text(current_value) == self._normalize_text(query)

    def _build_snapshot_click_action(
        self,
        snapshot: Optional[Dict[str, Any]],
        *,
        ref_key: str,
        selector_key: str,
        description: str,
        expected_page_type: str = "",
        confidence: float = 0.72,
    ) -> Optional[BrowserAction]:
        affordances = self._get_snapshot_affordances(snapshot)
        target_ref = str(affordances.get(ref_key, "") or "")
        target_selector = str(affordances.get(selector_key, "") or "")
        if not target_ref and not target_selector:
            return None
        return BrowserAction(
            action_type=ActionType.CLICK,
            target_ref=target_ref,
            target_selector=target_selector,
            description=description,
            confidence=confidence,
            expected_page_type=expected_page_type,
        )

    def _choose_modal_action(
        self,
        task: str,
        elements: List[PageElement],
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> Optional[BrowserAction]:
        active_snapshot = snapshot or self._last_semantic_snapshot or {}
        for ref_key, selector_key, description, confidence in (
            ("modal_primary_ref", "modal_primary_selector", "accept or continue modal", 0.82),
            ("modal_secondary_ref", "modal_secondary_selector", "dismiss modal secondary action", 0.78),
            ("modal_close_ref", "modal_close_selector", "close blocking modal", 0.76),
        ):
            action = self._build_snapshot_click_action(
                active_snapshot,
                ref_key=ref_key,
                selector_key=selector_key,
                description=description,
                confidence=confidence,
            )
            if action is not None:
                return action

        modal_elements = [
            element
            for element in elements
            if element.region == "modal" and element.is_visible and element.is_clickable
        ]
        if not modal_elements:
            return None

        candidate = self._find_best_element(
            task,
            modal_elements,
            kinds=["button", "submit", "link"],
            keywords=[
                "同意", "接受", "允许", "继续", "确定", "好的", "知道了",
                "accept", "agree", "allow", "continue", "ok", "okay", "got it",
                "关闭", "取消", "稍后", "拒绝", "跳过",
                "close", "dismiss", "cancel", "not now", "later", "skip", "decline",
                "×",
            ],
        )
        if candidate is None:
            return None
        return BrowserAction(
            action_type=ActionType.CLICK,
            target_selector=candidate.selector,
            target_ref=candidate.ref,
            description="dismiss blocking modal",
            confidence=0.74,
        )

    def _snapshot_has_actionable_modal(
        self,
        snapshot: Optional[Dict[str, Any]],
        elements: Optional[List[PageElement]] = None,
    ) -> bool:
        active_snapshot = snapshot or {}
        affordances = self._get_snapshot_affordances(active_snapshot)
        controls = active_snapshot.get("controls") or []

        if any(
            affordances.get(key)
            for key in (
                "modal_primary_ref",
                "modal_primary_selector",
                "modal_secondary_ref",
                "modal_secondary_selector",
                "modal_close_ref",
                "modal_close_selector",
            )
        ):
            return True

        if any(str(control.get("kind", "") or "") in {"modal_primary", "modal_secondary", "modal_close"} for control in controls):
            return True

        if any(
            element.region == "modal" and element.is_visible and element.is_clickable
            for element in (elements or [])
        ):
            return True

        page_type = str(active_snapshot.get("page_type", "") or "")
        return bool(affordances.get("has_modal")) and page_type == "modal"

    def _infer_page_state(
        self,
        task: str,
        current_url: str,
        intent: Optional[TaskIntent],
        data: List[Dict[str, str]],
        snapshot: Optional[Dict[str, Any]] = None,
        elements: Optional[List[PageElement]] = None,
    ) -> PageState:
        active_intent = intent or TaskIntent(intent_type="read", query="", confidence=0.0)
        active_snapshot = snapshot or self._last_semantic_snapshot or {}
        page_type = str(active_snapshot.get("page_type", "") or "").strip()
        if not page_type:
            page_type = "serp" if self._looks_like_search_results_url(current_url) else "unknown"

        affordances = self._get_snapshot_affordances(active_snapshot)
        blocked_signals = self._get_snapshot_blocked_signals(active_snapshot)
        target_count = self._extract_target_result_count(task)
        goal_satisfied = self._page_data_satisfies_goal(
            task,
            current_url,
            active_intent,
            data,
            snapshot=active_snapshot,
        )
        item_count = max(self._get_snapshot_item_count(active_snapshot), len(data or []))
        has_pagination = bool(affordances.get("has_pagination"))
        has_load_more = bool(affordances.get("has_load_more"))
        has_modal = self._snapshot_has_actionable_modal(active_snapshot, elements)
        snapshot_stage = str(active_snapshot.get("page_stage", "") or "").strip()
        if blocked_signals or self._looks_like_blocked_page(current_url):
            page_type = "blocked"

        stage = "unknown"
        if page_type == "blocked":
            stage = "blocked"
        elif snapshot_stage in {
            "searching",
            "selecting_source",
            "extracting",
            "interacting",
            "dismiss_modal",
            "collecting_more",
            "completing",
        }:
            stage = snapshot_stage
        elif has_modal:
            stage = "dismiss_modal"
        elif page_type == "serp":
            stage = "completing" if goal_satisfied else "selecting_source"
        elif page_type == "list":
            if target_count and len(data or []) < target_count and (has_pagination or has_load_more):
                stage = "collecting_more"
            elif goal_satisfied:
                stage = "completing"
            else:
                stage = "extracting"
        elif page_type == "detail":
            stage = "completing" if goal_satisfied else "extracting"
        elif page_type in {"form", "login"}:
            stage = "interacting"
        elif goal_satisfied:
            stage = "completing"
        elif item_count > 0:
            stage = "extracting"

        confidence = 0.45
        if page_type == "blocked":
            confidence = 0.92
        elif page_type in {"serp", "list", "detail", "form", "login", "modal"}:
            confidence = 0.8
        elif item_count > 0:
            confidence = 0.65

        return PageState(
            page_type=page_type,
            stage=stage,
            confidence=confidence,
            item_count=item_count,
            target_count=target_count,
            has_pagination=has_pagination,
            has_load_more=has_load_more,
            has_modal=has_modal,
            goal_satisfied=goal_satisfied,
        )

    def _choose_snapshot_navigation_action(
        self,
        task: str,
        current_url: str,
        elements: List[PageElement],
        intent: Optional[TaskIntent],
        data: List[Dict[str, str]],
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> Optional[BrowserAction]:
        active_snapshot = snapshot or self._last_semantic_snapshot or {}
        page_state = self._infer_page_state(task, current_url, intent, data, active_snapshot, elements=elements)
        if page_state.goal_satisfied and data:
            return BrowserAction(
                action_type=ActionType.EXTRACT,
                description="extract current structured content",
                confidence=0.84,
            )

        if page_state.has_modal:
            modal_action = self._choose_modal_action(task, elements, active_snapshot)
            if modal_action is not None:
                return modal_action

        if page_state.page_type == "list":
            if data and (page_state.target_count == 0 or len(data) >= min(page_state.target_count or len(data), page_state.item_count or len(data))):
                return BrowserAction(
                    action_type=ActionType.EXTRACT,
                    description="extract visible list content",
                    confidence=0.78,
                )
            if page_state.target_count and len(data) < page_state.target_count:
                load_more_action = self._build_snapshot_click_action(
                    active_snapshot,
                    ref_key="load_more_ref",
                    selector_key="load_more_selector",
                    description="load more list items",
                    confidence=0.74,
                )
                if load_more_action is not None:
                    return load_more_action
                next_page_action = self._build_snapshot_click_action(
                    active_snapshot,
                    ref_key="next_page_ref",
                    selector_key="next_page_selector",
                    description="open next results page",
                    confidence=0.71,
                )
                if next_page_action is not None:
                    return next_page_action
                return BrowserAction(
                    action_type=ActionType.SCROLL,
                    value="900",
                    description="scroll for lazy-loaded list items",
                    confidence=0.52,
                )
            if data:
                return BrowserAction(
                    action_type=ActionType.EXTRACT,
                    description="extract current list page",
                    confidence=0.7,
                )

        if page_state.page_type in {"list", "unknown", "detail"} and not data:
            query = (intent or TaskIntent(intent_type="navigate", query=self._derive_primary_query(task))).query
            navigation_keywords = self._extract_query_tokens(query or task)[:5]
            nav_candidate = self._find_best_element(
                task,
                elements,
                kinds=["button", "submit", "link"],
                keywords=navigation_keywords,
            )
            if nav_candidate and self._score_element_for_context(task, nav_candidate) >= 2.0:
                return BrowserAction(
                    action_type=ActionType.CLICK,
                    target_selector=nav_candidate.selector,
                    target_ref=nav_candidate.ref,
                    description=f"open relevant page section {nav_candidate.text[:24]}".strip(),
                    confidence=0.63,
                )

        if page_state.page_type == "detail" and data:
            return BrowserAction(
                action_type=ActionType.EXTRACT,
                description="extract detail page content",
                confidence=0.76,
            )

        return None

    def _get_vision_llm(self) -> Optional[LLMClient]:
        if self._vision_llm is not None:
            return self._vision_llm
        if self._vision_llm_attempted:
            return None
        self._vision_llm_attempted = True
        try:
            self._vision_llm = LLMClient.for_vision()
            return self._vision_llm
        except Exception as exc:
            if not self._vision_llm_unavailable_logged:
                log_warning(f"vision llm unavailable: {exc}")
                self._vision_llm_unavailable_logged = True
            return None

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
            elements=self._format_assessment_elements_for_llm(task, current_url, elements, max_items=14),
        )

        try:
            response = await asyncio.to_thread(
                vision_llm.chat_with_image,
                prompt,
                screenshot_r.data,
                0.1,
                1200,
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
        active_intent = intent or TaskIntent(intent_type="read", query="", confidence=0.0)
        query = active_intent.query or self._derive_primary_query(task)
        if query and not self._is_data_relevant(query, data):
            return False
        target_count = self._extract_target_result_count(task)
        active_snapshot = snapshot or self._last_semantic_snapshot or {}
        page_type = str(active_snapshot.get("page_type", "") or "")
        if target_count and len(data or []) < target_count and page_type in {"serp", "list"}:
            return False
        if not self._is_search_engine_url(current_url):
            if page_type == "list" and target_count and len(data or []) < target_count:
                return False
            return bool(data)
        return self._search_results_have_answer_evidence(query, data)

    def _choose_observation_driven_action(
        self,
        task: str,
        current_url: str,
        elements: List[PageElement],
        intent: Optional[TaskIntent],
        data: List[Dict[str, str]],
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> Optional[BrowserAction]:
        active_intent = intent or TaskIntent(intent_type="read", query="", confidence=0.0)
        query = active_intent.query or self._derive_primary_query(task)

        snapshot_action = self._choose_snapshot_navigation_action(
            task,
            current_url,
            elements,
            active_intent,
            data,
            snapshot=snapshot,
        )
        if snapshot_action is not None and snapshot_action.action_type != ActionType.EXTRACT:
            return snapshot_action

        if data and self._page_data_satisfies_goal(task, current_url, active_intent, data, snapshot=snapshot):
            return BrowserAction(
                action_type=ActionType.EXTRACT,
                description="use current page results",
                confidence=0.82,
            )

        if not self._is_search_engine_url(current_url):
            return snapshot_action

        if self._search_input_matches_query(elements, query):
            click_action = self._find_search_result_click_action(
                task,
                current_url,
                elements,
                active_intent,
                snapshot=snapshot,
            )
            if click_action is not None:
                return click_action

            if data and self._search_results_have_answer_evidence(query, data):
                return BrowserAction(
                    action_type=ActionType.EXTRACT,
                    description="extract visible search results",
                    confidence=0.68,
                )

            submit_control = self._find_primary_submit_control(elements)
            if submit_control is not None:
                return BrowserAction(
                    action_type=ActionType.CLICK,
                    target_selector=submit_control.selector,
                    description="submit current search query",
                    confidence=0.45,
                    use_keyboard_fallback=True,
                    keyboard_key="Enter",
                )

            return BrowserAction(
                action_type=ActionType.PRESS_KEY,
                value="Enter",
                description="submit current search query",
                confidence=0.35,
            )

        return snapshot_action

    def _is_data_relevant(self, query: str, data: List[Dict[str, str]]) -> bool:
        if not data:
            return False
        tokens = self._extract_query_tokens(query)
        if not tokens:
            return True

        best_score = 0.0
        for item in data[:8]:
            if not isinstance(item, dict):
                continue
            haystack = " ".join(str(v) for v in item.values() if v)
            best_score = max(best_score, self._score_text_relevance(query, haystack))
            if best_score >= 4.0:
                return True
        return False

    # ── Agent decision: local heuristics ───────────────────────

    def _find_search_element(self, elements: List[PageElement]) -> Optional[PageElement]:
        """Find a search input or search-trigger button on the page."""
        _SEARCH_KEYWORDS = {"search", "搜索", "搜", "查找", "find", "lookup", "查询"}
        best: Optional[PageElement] = None
        best_score = 0.0
        for el in elements:
            if not el.is_visible or not el.is_clickable:
                continue
            attrs = el.attributes or {}
            haystack = " ".join([
                el.text, attrs.get("placeholder", ""), attrs.get("ariaLabel", ""),
                attrs.get("labelText", ""), attrs.get("name", ""), attrs.get("type", ""),
            ]).lower()
            score = 0.0
            # Actual search input types
            if el.element_type in {"search", "text"} and el.tag in {"input", "textarea"}:
                score += 3.0
            # Buttons/inputs with search-related text
            for kw in _SEARCH_KEYWORDS:
                if kw in haystack:
                    score += 4.0
                    break
            if score <= 0:
                continue
            # Penalize auth-related fields
            if any(auth_kw in haystack for auth_kw in {"email", "password", "sign up", "sign in", "login", "注册", "登录"}):
                score -= 5.0
            if el.element_type in {"email", "password"}:
                score -= 5.0
            if score > best_score:
                best_score = score
                best = el
        return best

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
                # Ensure the match is actually relevant — require keyword match, not just task token
                attrs = explicit_target.attributes or {}
                haystack = " ".join([
                    explicit_target.text, attrs.get("placeholder", ""),
                    attrs.get("ariaLabel", ""), attrs.get("labelText", ""),
                ]).lower()
                target_lower = click_target.lower()
                # Only return if the target text (or significant part of it) actually appears in the element
                target_tokens = self._extract_task_tokens(target_lower)
                if not target_tokens and any("\u4e00" <= ch <= "\u9fff" for ch in target_lower):
                    target_tokens = [target_lower]
                matched_tokens = sum(1 for t in target_tokens if t in haystack)
                if target_tokens and matched_tokens >= max(1, len(target_tokens) // 2):
                    return BrowserAction(
                        action_type=ActionType.CLICK,
                        target_selector=explicit_target.selector,
                        description=f"click target {click_target}",
                        confidence=0.82,
                    )

        if active_intent.intent_type in {"form", "auth"}:
            mapping = self._build_form_mapping_from_pairs(active_intent.fields, elements)
            if mapping:
                if self._mapping_matches_current_elements(mapping, elements):
                    submit_control = self._find_submit_control_for_intent(task, elements, active_intent)
                    if submit_control:
                        return BrowserAction(
                            action_type=ActionType.CLICK,
                            target_selector=submit_control.selector,
                            target_ref=submit_control.ref,
                            description="submit interactive form",
                            confidence=0.78,
                        )
                else:
                    return self._build_form_fill_action(mapping)
            if active_intent.intent_type == "auth" and self._iter_input_candidates(elements):
                submit_control = self._find_submit_control_for_intent(task, elements, active_intent)
                if submit_control and not active_intent.fields:
                    return None
            submit_control = self._find_submit_control_for_intent(task, elements, active_intent)
            if submit_control:
                return BrowserAction(
                    action_type=ActionType.CLICK,
                    target_selector=submit_control.selector,
                    target_ref=submit_control.ref,
                    description="submit interactive form",
                    confidence=0.62,
                )

        if active_intent.intent_type == "search":
            query = active_intent.query or self._derive_primary_query(task)
            search_el = self._find_search_element(elements)
            if search_el and query:
                # If it's an actual input field, type the query directly
                if search_el.element_type in {"input", "text", "search", "textarea"} or search_el.tag in {"input", "textarea"}:
                    return BrowserAction(
                        action_type=ActionType.INPUT,
                        target_selector=search_el.selector,
                        value=query,
                        description="fill search query",
                        confidence=0.9,
                        use_keyboard_fallback=True,
                        keyboard_key="Enter",
                    )
                else:
                    # It's a search-trigger button (e.g. GitHub's "Search or jump to...")
                    return BrowserAction(
                        action_type=ActionType.CLICK,
                        target_selector=search_el.selector,
                        description=f"open search to find {query}",
                        confidence=0.85,
                    )
            # Fallback: try plain text input
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
        if not isinstance(payload, dict):
            payload = {}
        action_payload = payload.get("action", {}) if isinstance(payload.get("action", {}), dict) else {}
        flat_action_payload = payload if not action_payload else {}
        action_type_raw = str(
            action_payload.get("type")
            or flat_action_payload.get("action_type")
            or flat_action_payload.get("type")
            or "failed"
        ).lower()
        try:
            action_type = ActionType(action_type_raw)
        except ValueError:
            action_type = ActionType.FAILED

        selector = str(
            action_payload.get("target_selector")
            or flat_action_payload.get("target_selector")
            or ""
        )
        target_ref = str(
            action_payload.get("target_ref")
            or flat_action_payload.get("target_ref")
            or ""
        )
        index = action_payload.get("element_index", flat_action_payload.get("element_index", -1))
        if not isinstance(index, int):
            try:
                index = int(index)
            except (TypeError, ValueError):
                index = -1
        if not selector and not target_ref and isinstance(index, int):
            for element in elements:
                if element.index == index:
                    selector = element.selector
                    target_ref = element.ref
                    break

        raw_value = action_payload.get("value", flat_action_payload.get("value", ""))
        if isinstance(raw_value, (dict, list)):
            value = json.dumps(raw_value, ensure_ascii=False)
        else:
            value = str(raw_value or "")

        return BrowserAction(
            action_type=action_type, target_selector=selector,
            target_ref=target_ref,
            value=value,
            description=str(action_payload.get("description") or flat_action_payload.get("description") or ""),
            confidence=float(payload.get("confidence", action_payload.get("confidence", flat_action_payload.get("confidence", 0.0))) or 0.0),
            requires_confirmation=bool(
                payload.get(
                    "requires_human_confirm",
                    action_payload.get("requires_human_confirm", flat_action_payload.get("requires_human_confirm", False)),
                )
            ),
            fallback_selector=str(
                action_payload.get("fallback_selector")
                or flat_action_payload.get("fallback_selector")
                or ""
            ),
            use_keyboard_fallback=bool(
                action_payload.get("use_keyboard", flat_action_payload.get("use_keyboard", False))
            ),
            keyboard_key=str(
                action_payload.get("keyboard_key")
                or flat_action_payload.get("keyboard_key")
                or ""
            ),
            expected_page_type=str(
                action_payload.get("expected_page_type")
                or flat_action_payload.get("expected_page_type")
                or ""
            ),
            expected_text=str(
                action_payload.get("expected_text")
                or flat_action_payload.get("expected_text")
                or ""
            ),
        )

    async def _decide_action_with_llm(
        self,
        task: str,
        elements: List[PageElement],
        intent: Optional[TaskIntent] = None,
        data: Optional[List[Dict[str, str]]] = None,
        snapshot: Optional[Dict[str, Any]] = None,
        current_url: str = "",
        title: str = "",
        last_action: Optional[BrowserAction] = None,
        recent_steps: Optional[List[Dict[str, Any]]] = None,
    ) -> BrowserAction:
        try:
            if not elements:
                if self._is_read_only_task(task, intent):
                    return BrowserAction(action_type=ActionType.EXTRACT, description="extract visible data")
                return BrowserAction(action_type=ActionType.WAIT, value="1", description="no actionable elements", confidence=0.05)

            page_title = title or ""
            if not page_title:
                title_r = await self.toolkit.get_title()
                page_title = title_r.data or ""
            resolved_url = current_url or ""
            if not resolved_url:
                url_r = await self.toolkit.get_current_url()
                resolved_url = url_r.data or ""

            active_intent = intent or TaskIntent(
                intent_type="read",
                query=self._derive_primary_query(task),
                confidence=0.0,
            )
            current_data = list(data or [])
            if not current_data:
                current_data = await self._extract_data_for_intent(active_intent)
            data_collected = len(current_data) if current_data else 0
            target_match = re.search(r'(\d+)\s*(?:个|条|款|项|条数据|items?|results?)', task or "")
            data_target = int(target_match.group(1)) if target_match else 10
            data_progress = f"Data progress: collected {data_collected} / target {data_target}"
            if data_collected >= data_target:
                data_progress += " (ENOUGH - consider using DONE)"
            active_snapshot = snapshot or await self._get_semantic_snapshot()
            cards = self._cards_from_snapshot(active_snapshot)
            page_state = self._infer_page_state(task, resolved_url, active_intent, current_data or [], active_snapshot)
            elements_text = self._format_assessment_elements_for_llm(task, resolved_url, elements, max_items=18)
            prompt_context, prompt_budget = self._build_budgeted_browser_prompt_context(
                task=task,
                current_url=resolved_url,
                data=current_data or [],
                cards=cards,
                snapshot=active_snapshot,
                elements_text=elements_text,
                total_tokens=_ACTION_DECISION_CONTEXT_TOKENS,
            )

            messages = [
                {"role": "system", "content": "Return JSON only."},
                {"role": "user", "content": ACTION_DECISION_PROMPT.format(
                    task=task,
                    intent=active_intent.intent_type,
                    query=active_intent.query or self._derive_primary_query(task),
                    fields=self._format_intent_fields_for_llm(active_intent.fields),
                    requires_interaction=str(bool(active_intent.requires_interaction)).lower(),
                    url=resolved_url,
                    title=page_title,
                    data_progress=data_progress,
                    page_type=page_state.page_type,
                    page_stage=page_state.stage,
                    last_action=(last_action.description or last_action.action_type.value) if last_action else "none",
                    recent_steps=self._format_recent_steps_for_llm(recent_steps),
                    context_coverage=prompt_context.get("context_coverage", ""),
                    data=prompt_context.get("data", "(no visible data)"),
                    cards=prompt_context.get("cards", "(no cards)"),
                    collections=prompt_context.get("collections", "(no collections)"),
                    controls=prompt_context.get("controls", "(no controls)"),
                    elements=prompt_context.get("elements", "(no actionable elements)"),
                )},
            ]
            web_debug_recorder.write_json("browser_action_decision_budget", prompt_budget)
            web_debug_recorder.write_text("browser_action_decision_prompt", messages[1]["content"])

            # 🔥 新增：输出action decision prompt到控制台
            if web_debug_recorder.is_enabled():
                log_warning(f"[DEBUG] 动作决策 Prompt (前800字符): {messages[1]['content'][:800]}...")
                log_warning(f"[DEBUG] 动作决策 Prompt总长度: {len(messages[1]['content'])} 字符")

            llm = self._get_llm()
            response = await llm.achat(messages, temperature=0.1, json_mode=True)
            web_debug_recorder.write_text("browser_action_decision_response", self._stringify_llm_response(response))

            # 🔥 新增：输出action decision response到控制台
            if web_debug_recorder.is_enabled():
                log_warning(f"[DEBUG] 动作决策 LLM 响应: {self._stringify_llm_response(response)[:500]}...")

            action = self._action_from_llm(llm.parse_json_response(response), elements)
            web_debug_recorder.write_json("browser_action_decision_action", self._action_to_debug_payload(action))
            if (
                page_state.page_type == "list"
                and page_state.target_count
                and data_collected < page_state.target_count
                and (page_state.has_pagination or page_state.has_load_more)
                and action.action_type in {ActionType.EXTRACT, ActionType.DONE, ActionType.WAIT}
            ):
                state_action = self._choose_snapshot_navigation_action(
                    task,
                    resolved_url,
                    elements,
                    active_intent,
                    current_data or [],
                    active_snapshot,
                )
                if state_action is not None:
                    return state_action
            return action
        except Exception as exc:
            log_warning(f"LLM action fallback failed: {exc}")
            return BrowserAction(action_type=ActionType.WAIT, value="1", description="fallback wait", confidence=0.1)

    async def _plan_next_action(
        self,
        task: str,
        current_url: str,
        title: str,
        elements: List[PageElement],
        intent: Optional[TaskIntent],
        data: List[Dict[str, str]],
        snapshot: Optional[Dict[str, Any]] = None,
        last_action: Optional[BrowserAction] = None,
        recent_steps: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[Optional[BrowserAction], str]:
        active_snapshot = snapshot or self._last_semantic_snapshot or {}

        assessed_action = await self._assess_page_with_llm(
            task,
            current_url,
            title,
            elements,
            intent,
            data,
            last_action=last_action,
            recent_steps=recent_steps,
        )
        active_snapshot = snapshot or self._last_semantic_snapshot or active_snapshot
        assessed_action = self._sanitize_planned_action(
            task,
            current_url,
            elements,
            intent,
            data,
            assessed_action,
            snapshot=active_snapshot,
            recent_steps=recent_steps,
        )
        if assessed_action is not None and assessed_action.action_type not in {ActionType.WAIT, ActionType.FAILED}:
            return assessed_action, "page_assessment_llm"

        llm_action = await self._decide_action_with_llm(
            task,
            elements,
            intent=intent,
            data=data,
            snapshot=active_snapshot,
            current_url=current_url,
            title=title,
            last_action=last_action,
            recent_steps=recent_steps,
        )
        llm_action = self._sanitize_planned_action(
            task,
            current_url,
            elements,
            intent,
            data,
            llm_action,
            snapshot=active_snapshot,
            recent_steps=recent_steps,
        )
        if llm_action is not None and llm_action.action_type not in {ActionType.WAIT, ActionType.FAILED}:
            return llm_action, "action_llm"

        observation_action = self._choose_observation_driven_action(
            task,
            current_url,
            elements,
            intent,
            data,
            snapshot=active_snapshot,
        )
        observation_action = self._sanitize_planned_action(
            task,
            current_url,
            elements,
            intent,
            data,
            observation_action,
            snapshot=active_snapshot,
            recent_steps=recent_steps,
        )
        if observation_action is not None and observation_action.action_type not in {ActionType.WAIT, ActionType.FAILED}:
            return observation_action, "observation_fallback"

        search_result_action = self._find_search_result_click_action(
            task,
            current_url,
            elements,
            intent,
            snapshot=active_snapshot,
        )
        search_result_action = self._sanitize_planned_action(
            task,
            current_url,
            elements,
            intent,
            data,
            search_result_action,
            snapshot=active_snapshot,
            recent_steps=recent_steps,
        )
        if search_result_action is not None and search_result_action.action_type not in {ActionType.WAIT, ActionType.FAILED}:
            return search_result_action, "search_result_fallback"

        local_action = self._decide_action_locally(task, elements, intent)
        local_action = self._sanitize_planned_action(
            task,
            current_url,
            elements,
            intent,
            data,
            local_action,
            snapshot=active_snapshot,
            recent_steps=recent_steps,
        )
        if local_action is not None and local_action.action_type not in {ActionType.WAIT, ActionType.FAILED}:
            return local_action, "local_fallback"

        if llm_action is not None:
            return llm_action, "action_llm_wait"
        if assessed_action is not None:
            return assessed_action, "page_assessment_wait"
        return None, "no_action"

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
        tk = self.toolkit
        strategies: List[Tuple[str, Any]] = []
        if action and action.target_ref:
            strategies.append((f"ref:{action.target_ref}", lambda r=action.target_ref: tk.click_ref(r)))
        if selector:
            strategies.append(("direct_click", lambda: tk.click(selector)))
        # semantic strategies from cache
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

    async def _try_input_with_fallbacks(
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
            success = await self._try_input_with_fallbacks(action.target_selector, action.value, action)
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
        url = await self._get_current_url_value()
        title = await self._get_title_value()
        html = await self._get_page_html_value()
        semantic_snapshot = await self._get_semantic_snapshot()
        return {
            "url": url,
            "title": title,
            "content_len": len(html),
            "content_hash": hashlib.sha1(html[:12000].encode("utf-8", errors="ignore")).hexdigest() if html else "",
            "page_type": str(semantic_snapshot.get("page_type", "") or ""),
            "page_stage": str(semantic_snapshot.get("page_stage", "") or ""),
            "card_count": len(semantic_snapshot.get("cards", []) or []),
            "item_count": self._get_snapshot_item_count(semantic_snapshot),
            "has_modal": self._snapshot_has_actionable_modal(semantic_snapshot),
            "has_pagination": bool(self._get_snapshot_affordances(semantic_snapshot).get("has_pagination")),
            "has_load_more": bool(self._get_snapshot_affordances(semantic_snapshot).get("has_load_more")),
            "blocked_signals": self._get_snapshot_blocked_signals(semantic_snapshot),
            "main_text_len": len(self._get_snapshot_main_text(semantic_snapshot)),
            "visible_text_block_count": len(self._get_snapshot_visible_text_blocks(semantic_snapshot)),
        }

    def _action_must_change_state(self, action: BrowserAction) -> bool:
        return action.action_type in {
            ActionType.CLICK, ActionType.INPUT, ActionType.SELECT,
            ActionType.NAVIGATE, ActionType.PRESS_KEY, ActionType.FILL_FORM,
            ActionType.SCROLL,
        }

    async def _verify_action_effect(self, before: Dict[str, Any], action: BrowserAction) -> bool:
        if not self._action_must_change_state(action):
            return True
        after = await self._snapshot_page_state()
        result = False
        if action.expected_page_type and after.get("page_type") == action.expected_page_type:
            result = True
        elif action.expected_text:
            text_wait = await self.toolkit.wait_for_text_appear(action.expected_text, timeout=2500)
            if text_wait.success:
                result = True
        elif after["url"] != before["url"]:
            result = True
        elif after["title"] != before["title"]:
            result = True
        elif before.get("page_type") and after.get("page_type") and before.get("page_type") != after.get("page_type"):
            result = True
        elif int(after.get("card_count", 0) or 0) > int(before.get("card_count", 0) or 0):
            result = True
        elif int(after.get("item_count", 0) or 0) > int(before.get("item_count", 0) or 0):
            result = True
        elif bool(before.get("has_modal")) and not bool(after.get("has_modal")):
            result = True
        elif abs(after["content_len"] - before["content_len"]) > 80:
            result = True
        elif after.get("content_hash") and after.get("content_hash") != before.get("content_hash"):
            result = True
        elif action.action_type == ActionType.INPUT:
            if action.target_ref:
                ref_info = self.toolkit.resolve_ref(action.target_ref)
                selector = str(ref_info.get("selector", "") or "")
            else:
                selector = action.target_selector
            if selector:
                r = await self.toolkit.get_input_value(selector)
                if r.success:
                    result = self._normalize_text(r.data) == self._normalize_text(action.value)
        elif action.action_type == ActionType.FILL_FORM:
            result = await self._verify_form_values(action.value)
        web_debug_recorder.write_json(
            "browser_action_verification",
            {
                "before": before,
                "after": after,
                "action": self._action_to_debug_payload(action),
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
        for selector, expected in form_data.items():
            r = await self.toolkit.get_input_value(str(selector))
            if r.success and self._normalize_text(r.data) == self._normalize_text(str(expected)):
                matched += 1
        return matched > 0

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

    async def _maybe_extract_data(
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
                'main h3', 'article h3', 'tr'
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
        active_intent = intent or TaskIntent(intent_type="read", query="", confidence=0.0)
        current_url_r = await self.toolkit.get_current_url()
        current_url = current_url_r.data or ""
        if active_intent.intent_type == "search" or self._is_search_engine_url(current_url):
            serp_data = await self._extract_search_results_data()
            if serp_data:
                return serp_data
        prefer_links = active_intent.intent_type == "search" or self._is_search_engine_url(current_url)
        return await self._maybe_extract_data(
            prefer_content=not prefer_links,
            prefer_links=prefer_links,
        )

    def _task_requires_detail_page_legacy(self, task: str, intent: Optional[TaskIntent] = None) -> bool:
        normalized = self._normalize_text(task)
        active_intent = intent or TaskIntent(intent_type="read", query="", confidence=0.0)
        detail_tokens = (
            "weather", "forecast", "temperature", "humidity", "wind", "aqi", "air quality",
            "detail", "details", "news", "article", "articles", "report", "reports", "source", "sources",
            "statement", "speech", "fact", "verify", "death", "died", "killed", "alive",
            "详情", "具体", "温度", "湿度", "风力", "空气质量", "天气", "天气预报",
            "新闻", "报道", "来源", "声明", "讲话", "死亡", "死了", "是否", "核实", "公开露面",
        )
        return active_intent.intent_type in {"search", "navigate"} and any(token in normalized for token in detail_tokens)

    def _task_requires_detail_page(self, task: str, intent: Optional[TaskIntent] = None) -> bool:
        active_intent = intent or TaskIntent(intent_type="read", query="", confidence=0.0)
        if active_intent.intent_type not in {"search", "navigate"}:
            return False
        if self._extract_url_from_task(task):
            return True
        if active_intent.target_text or active_intent.requires_interaction:
            return True
        if self._task_mentions_interaction(task):
            return True
        query = active_intent.query or self._derive_primary_query(task)
        return len(self._extract_query_tokens(query)) >= 2

    def _find_search_result_click_action(
        self,
        task: str,
        current_url: str,
        elements: List[PageElement],
        intent: Optional[TaskIntent] = None,
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> Optional[BrowserAction]:
        if not self._is_search_engine_url(current_url):
            return None

        active_intent = intent or TaskIntent(intent_type="search", query=self._derive_primary_query(task), confidence=0.0)
        if not self._task_requires_detail_page(task, active_intent):
            return None

        cards = self._cards_from_snapshot(snapshot or self._last_semantic_snapshot)
        if cards:
            ranked_cards: List[Tuple[float, SearchResultCard]] = []
            for card in cards:
                score = self._score_search_result_card(task, active_intent.query, card)
                if self._extract_query_tokens(active_intent.query) and score < 4.0:
                    continue
                ranked_cards.append((score, card))
            ranked_cards.sort(key=lambda item: item[0], reverse=True)
            if ranked_cards:
                best_score, best_card = ranked_cards[0]
                return BrowserAction(
                    action_type=ActionType.CLICK,
                    target_selector=best_card.target_selector,
                    target_ref=best_card.target_ref or best_card.ref,
                    description="open the strongest search result card",
                    confidence=min(best_score / 10.0, 0.9),
                    expected_page_type="detail",
                )

        query_tokens = self._extract_query_tokens(active_intent.query)
        best_match: Optional[tuple[float, PageElement]] = None
        for element in elements:
            if not element.is_visible or not element.is_clickable:
                continue
            if element.element_type not in {"link", "button"} and element.tag not in {"a", "button"}:
                continue
            if self._is_noise_element(element):
                continue

            attrs = element.attributes or {}
            href = str(attrs.get("href", "") or "")
            if not href or self._is_search_engine_url(href):
                continue

            haystack = self._normalize_text(" ".join([
                element.text,
                attrs.get("labelText", ""),
                attrs.get("title", ""),
                href,
            ]))
            score = self._score_text_relevance(active_intent.query, haystack)
            if "天气" in haystack or "weather" in haystack:
                score += 2.0
            if query_tokens and score < 4.0:
                continue
            if best_match is None or score > best_match[0]:
                best_match = (score, element)

        if best_match is None:
            return None

        return BrowserAction(
            action_type=ActionType.CLICK,
            target_selector=best_match[1].selector,
            target_ref=best_match[1].ref,
            description="open the most relevant detail result",
            confidence=min(best_match[0] / 10.0, 0.88),
            expected_page_type="detail",
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
        active_intent = intent or TaskIntent(
            intent_type="search",
            query=self._derive_primary_query(task),
            confidence=0.0,
        )
        if active_intent.intent_type == "search":
            if self._is_search_engine_url(current_url):
                return False
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
            active_snapshot = snapshot or self._last_semantic_snapshot or {}
            current_elements = elements or []
            current_data = data or []
            if self._page_data_satisfies_goal(
                task,
                current_url,
                active_intent,
                current_data,
                snapshot=active_snapshot,
            ):
                return True
            if self._interaction_requires_follow_up(
                task,
                active_intent,
                current_elements,
                snapshot=active_snapshot,
            ):
                return False
            if target_url and self._urls_look_related(target_url, current_url):
                return True
            page_type = str(active_snapshot.get("page_type", "") or "")
            page_stage = str(active_snapshot.get("page_stage", "") or "")
            if page_stage == "interacting" or page_type in {"form", "login", "modal"}:
                return False
            return bool(current_url)
        if active_intent.intent_type == "navigate":
            if target_url:
                return self._urls_look_related(target_url, current_url)
            return bool(current_url) and not active_intent.target_text
        return False

    async def _wait_for_page_ready(self) -> None:
        tk = self.toolkit
        await tk.wait_for_load("domcontentloaded", timeout=10000)
        if not tk.fast_mode:
            await tk.wait_for_load("networkidle", timeout=3000)
        await tk.human_delay(40, 80)

    def _snapshot_is_transient_loading(self, snapshot: Optional[Dict[str, Any]]) -> bool:
        active_snapshot = snapshot or {}
        page_type = str(active_snapshot.get("page_type", "") or "").strip().lower()
        title = self._normalize_text(str(active_snapshot.get("title", "") or ""))
        main_text = self._normalize_text(str(active_snapshot.get("main_text", "") or ""))
        has_structured_content = any(
            bool(active_snapshot.get(key))
            for key in ("elements", "cards", "collections", "controls", "regions")
        )
        if has_structured_content:
            return False
        if page_type not in {"", "unknown"}:
            return False
        if title:
            return False
        return main_text.startswith("loading")

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

    async def run(self, task: str, start_url: Optional[str] = None, max_steps: int = 8) -> Dict[str, Any]:
        tk = self.toolkit
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
            # 🔥 新增：明确告诉用户调试文件的位置
            log_warning(f"[DEBUG] 调试文件保存在: {trace.root_dir}")
            log_warning(f"[DEBUG] 你可以查看该目录下的HTML、prompt和response文件来分析感知差异")
        try:
            r = await tk.create_page()
            if not r.success:
                web_debug_recorder.record_event("browser_create_page_failed", error=r.error)
                return {"success": False, "message": f"浏览器启动失败: {r.error}", "steps": []}

            url = expected_url or "about:blank"
            self._action_history = []
            self._page_assessment_cache.clear()

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

            # 🔥 新增：输出初始页面信息到控制台
            if web_debug_recorder.is_enabled():
                log_warning(f"[DEBUG] ========== 初始导航完成 ==========")
                log_warning(f"[DEBUG] 目标URL: {expected_url}")
                log_warning(f"[DEBUG] 当前URL: {current_url}")
                log_warning(f"[DEBUG] 页面标题: {page_title}")
                log_warning(f"[DEBUG] ====================================")

            if self._looks_like_blocked_page(current_url, page_title):
                return {
                    "success": False,
                    "message": f"navigation landed on blocked page: {page_title or current_url}",
                    "url": current_url,
                    "expected_url": expected_url,
                    "title": page_title,
                    "steps": steps,
                }
            if expected_url and current_url and not self._urls_look_related(expected_url, current_url) and (start_url or self._extract_url_from_task(task)):
                return {
                    "success": False,
                    "message": f"navigation landed on unexpected page: expected {expected_url}, got {current_url}",
                    "url": current_url,
                    "expected_url": expected_url,
                    "title": page_title,
                    "steps": steps,
                }
            if expected_url and current_url and not self._urls_look_related(expected_url, current_url):
                # 不要立即退出，记录警告但继续执行
                log_warning(f"URL 不匹配: 期望 {expected_url}, 实际 {current_url}, 但继续尝试执行")
                # return {
                #     "success": False,
                #     "message": f"navigation landed on unexpected page: expected {expected_url}, got {current_url}",
                #     "url": current_url,
                #     "expected_url": expected_url,
                #     "title": page_title,
                #     "steps": steps,
                # }
            task_intent = await self._infer_task_intent(task)
            task_intent = self._coerce_intent_for_direct_page(task, task_intent, expected_url)
            if (
                task_intent.intent_type == "search"
                and not start_url
                and not self._extract_url_from_task(task)
            ):
                await self._bootstrap_search_results(task_intent.query or self._derive_primary_query(task))
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
                    # 只有在数据足够多（至少3条）且确实满足目标时才提前返回
                    title_r = await tk.get_title()
                    url_r = await tk.get_current_url()
                    return {"success": True, "message": "read-only task satisfied from initial page",
                            "url": url_r.data or "", "title": title_r.data or "",
                            "expected_url": expected_url, "steps": steps, "data": initial_data}
                else:
                    # 数据不够或不满足，继续执行步骤
                    log_warning(f"初始数据不足（{len(initial_data)} 条），继续执行步骤")

            # 累积数据容器
            _accumulated_data: List[Dict[str, str]] = []
            _seen_keys: set = set()
            last_action: Optional[BrowserAction] = None

            def _merge_new_data(new_items: List[Dict[str, str]]):
                for item in (new_items or []):
                    vals = [str(v)[:80] for v in list(item.values())[:2] if v]
                    key = "|".join(vals)
                    if key and key not in _seen_keys:
                        _seen_keys.add(key)
                        _accumulated_data.append(item)

            for step_no in range(1, max_steps + 1):
                current_url_r = await tk.get_current_url()
                title_r = await tk.get_title()
                if self._looks_like_blocked_page(current_url_r.data or "", title_r.data or ""):
                    return {
                        "success": False,
                        "message": f"browser landed on blocked page during execution: {title_r.data or current_url_r.data or ''}",
                        "url": current_url_r.data or "",
                        "title": title_r.data or "",
                        "expected_url": expected_url,
                        "steps": steps,
                        "data": _accumulated_data,
                    }
                elements = await self._extract_interactive_elements()
                snapshot = self._last_semantic_snapshot or await self._get_semantic_snapshot()
                observed_data = await self._extract_data_for_intent(task_intent)
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
                        "accumulated_data": list(_accumulated_data),
                        "last_action": self._action_to_debug_payload(last_action),
                    },
                )

                # 🔥 新增：输出每步的关键信息到控制台
                if web_debug_recorder.is_enabled():
                    log_warning(f"[DEBUG] ========== Step {step_no} 开始 ==========")
                    log_warning(f"[DEBUG] 当前URL: {current_url_r.data or ''}")
                    log_warning(f"[DEBUG] 页面标题: {title_r.data or ''}")
                    log_warning(f"[DEBUG] 可交互元素数量: {len(elements)}")
                    log_warning(f"[DEBUG] 已收集数据: {len(_accumulated_data)} 条")
                    log_warning(f"[DEBUG] ====================================")

                action, action_source = await self._plan_next_action(
                    task,
                    current_url_r.data or "",
                    title_r.data or "",
                    elements,
                    task_intent,
                    _accumulated_data or observed_data,
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
                        _accumulated_data or observed_data,
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

                # 🔥 新增：输出决策的动作到控制台
                if web_debug_recorder.is_enabled():
                    log_warning(f"[DEBUG] Step {step_no} 决策动作: {action.action_type.value}")
                    log_warning(f"[DEBUG] 动作描述: {action.description}")
                    log_warning(f"[DEBUG] 目标选择器: {action.target_selector[:100] if action.target_selector else 'N/A'}")
                    log_warning(f"[DEBUG] 置信度: {action.confidence}")


                if action.action_type == ActionType.DONE:
                    data = await self._extract_data_for_intent(task_intent)
                    _merge_new_data(data)
                    log_success("browser task completed")
                    url_r = await tk.get_current_url()
                    title_r = await tk.get_title()
                    return {"success": True, "message": "task completed",
                            "url": url_r.data or "", "title": title_r.data or "",
                            "expected_url": expected_url, "steps": steps, "data": _accumulated_data or data}

                if action.action_type == ActionType.EXTRACT:
                    data = await self._extract_data_for_intent(task_intent)
                    _merge_new_data(data)
                    url_r = await tk.get_current_url()
                    title_r = await tk.get_title()
                    return {"success": True, "message": "data extracted",
                            "url": url_r.data or "", "title": title_r.data or "",
                            "expected_url": expected_url, "steps": steps, "data": _accumulated_data or data}

                if action.requires_confirmation and settings.REQUIRE_HUMAN_CONFIRM:
                    # 🔥 修复：不要直接退出，而是询问用户
                    from utils.human_confirm import HumanConfirm
                    confirmed = await asyncio.to_thread(
                        HumanConfirm.request_browser_action_confirmation,
                        action=action.action_type.value,
                        target=action.target_selector[:80],
                        value=action.value[:80],
                        description=action.description
                    )
                    if not confirmed:
                        return {"success": False, "message": "user declined action confirmation",
                                "requires_confirmation": True, "steps": steps}
                    # 用户确认了，继续执行

                if self._is_action_looping(action):
                    url_r = await tk.get_current_url()
                    title_r = await tk.get_title()
                    if self._looks_like_blocked_page(url_r.data or "", title_r.data or ""):
                        return {"success": False, "message": f"browser stuck on blocked page: {title_r.data or url_r.data or ''}",
                                "url": url_r.data or "", "title": title_r.data or "",
                                "expected_url": expected_url, "steps": steps, "data": _accumulated_data}
                    if self._is_read_only_task(task, task_intent):
                        _merge_new_data(await self._extract_data_for_intent(task_intent))
                        return {"success": True, "message": "repeated action avoided; extracted current page",
                                "url": url_r.data or "", "title": title_r.data or "",
                                "expected_url": expected_url, "steps": steps, "data": _accumulated_data}
                    return {"success": False, "message": f"repeated action loop detected at step {step_no}",
                            "url": url_r.data or "", "title": title_r.data or "",
                            "expected_url": expected_url, "steps": steps, "data": _accumulated_data}
                self._record_action(action)
                last_action = action

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
                            last_action = action
                            action_source = "local_recovery"
                if not success:
                    visual_recovery = await self._decide_action_with_vision(
                        task,
                        current_url_r.data or "",
                        title_r.data or "",
                        elements,
                        task_intent,
                        _accumulated_data or observed_data,
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

                url_r = await tk.get_current_url()
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
                })
                web_debug_recorder.write_json(
                    f"browser_step_{step_no}_result",
                    steps[-1],
                )

                if not success:
                    if action.action_type == ActionType.WAIT:
                        continue
                    _consecutive_fails = sum(1 for s in reversed(steps) if s.get("result") == "failed")

                    # 改进：不要立即跳过，先尝试恢复
                    if _consecutive_fails == 1:
                        # 第一次失败：记录警告，但继续尝试（可能是临时问题）
                        log_warning(f"step {step_no} 失败，将在下一步重新评估页面状态")
                        # 等待一下，让页面稳定
                        await asyncio.sleep(1)
                        continue
                    elif _consecutive_fails == 2:
                        # 第二次失败：尝试刷新页面或回退
                        log_warning(f"连续2步失败，尝试刷新页面恢复")
                        await tk.refresh()
                        await self._wait_for_page_ready()
                        # 重新获取页面状态，让LLM重新决策
                        continue
                    else:
                        # 连续3次失败：放弃
                        title_r = await tk.get_title()
                        return {"success": False,
                                "message": f"连续 {_consecutive_fails} 步失败，已尝试恢复但仍失败 (最后在 step {step_no})",
                                "url": url_r.data or "", "title": title_r.data or "",
                                "expected_url": expected_url,
                                "steps": steps, "data": _accumulated_data or await self._extract_data_for_intent(task_intent)}

                if action.action_type in {ActionType.CLICK, ActionType.INPUT, ActionType.FILL_FORM, ActionType.PRESS_KEY}:
                    post_snapshot = await self._get_semantic_snapshot()
                    post_elements = self._filter_noise_elements(self._elements_from_snapshot(post_snapshot))
                    if post_elements:
                        self._element_cache = post_elements[:40]
                    else:
                        post_elements = await self._extract_interactive_elements()
                        post_snapshot = self._last_semantic_snapshot or post_snapshot
                    step_data = await self._extract_data_for_intent(task_intent)
                    _merge_new_data(step_data)
                    candidate_data = _accumulated_data or step_data
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
                        return {"success": True, "message": "task reached target page",
                                "url": url_r.data or "", "title": title_r.data or "",
                                "expected_url": expected_url,
                                "steps": steps, "data": candidate_data or await self._extract_data_for_intent(task_intent)}

                if action.action_type == ActionType.SCROLL:
                    step_data = await self._extract_data_for_intent(task_intent)
                    _merge_new_data(step_data)

            # max steps reached
            _merge_new_data(await self._extract_data_for_intent(task_intent))
            url_r = await tk.get_current_url()
            title_r = await tk.get_title()
            return {
                "success": len(_accumulated_data) > 0,
                "message": "max steps reached" + (f", but collected {len(_accumulated_data)} items" if _accumulated_data else ""),
                "url": url_r.data or "", "title": title_r.data or "",
                "expected_url": expected_url,
                "steps": steps, "data": _accumulated_data,
            }
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
