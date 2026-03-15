"""
OmniCore Router Agent
Receives user instructions, detects intent, and decomposes work into DAG tasks.
"""
import json
import re
from datetime import date, timedelta
from pathlib import Path
from typing import List, Dict, Any
from urllib.parse import parse_qs, urlparse

from core.state import OmniCoreState, TaskItem
from core.task_planner import build_policy_decision_from_task, build_task_item_from_plan
from core.llm import LLMClient
from core.tool_registry import build_dynamic_tool_prompt_lines, get_builtin_tool_registry
from utils.logger import log_agent_action, logger
from utils.url_utils import extract_first_url


# Router Agent 的系统提示词
# 从 prompts/router_system.txt 加载
_ROUTER_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "router_system.txt"


def _load_router_system_prompt() -> str:
    try:
        prompt = _ROUTER_PROMPT_PATH.read_text(encoding="utf-8-sig").strip()
        if prompt:
            return prompt
    except OSError:
        pass

    # Fallback: minimal built-in prompt to keep runtime available.
    return (
        "You are OmniCore's router. Detect user intent, decompose tasks when needed, "
        "and when no task is needed provide a direct answer. Output must be JSON."
    )


ROUTER_SYSTEM_PROMPT = _load_router_system_prompt()

ROUTER_OUTPUT_APPENDIX = """
## Tool Planning Output Upgrade
- Prefer `tool_name` and `tool_args` for each task.
- Treat `task_type` as a compatibility fallback only.
- Do not emit `task_type` unless you cannot avoid it.
- Always include top-level `direct_answer` in JSON output.
- If `tasks` is empty, `direct_answer` must be a user-facing final reply.
- If `tasks` is non-empty, set `direct_answer` to an empty string.
"""

FACT_VERIFICATION_GUARD_PROMPT = """
You decide whether a user question must be verified online before giving a direct final answer.
Return JSON only with keys:
- requires_verification: boolean
- confidence: number
- reason: string
- queries: array of short search queries

Require verification when the question depends on recent/current facts, leadership/office holder status,
alive/dead status, recent events, recent appointments or removals, or other time-sensitive public facts.
Do not require verification for timeless explanations, math, coding help, or asking the local time/date itself.

If verification is needed, each query must be short, search-engine ready, and evidence-oriented.
Do not output full task instructions as queries.

Current local date: {current_date}
User question: {user_input}
"""

_EXPLICIT_LOCATION_REQUEST_TOKENS = (
    "near me",
    "nearby",
    "around me",
    "nearest",
    "my location",
    "where am i",
    "local",
    "附近",
    "周边",
    "周围",
    "离我最近",
    "我附近",
    "我这里",
    "本地",
    "当地",
    "我所在",
    "我在哪",
    "我的位置",
    "定位",
)

_LOCATION_SENSITIVE_TOPIC_TOKENS = (
    "weather",
    "temperature",
    "rain",
    "aqi",
    "air quality",
    "traffic",
    "commute",
    "restaurant",
    "restaurants",
    "cafe",
    "cafes",
    "coffee",
    "hotel",
    "hotels",
    "pharmacy",
    "hospital",
    "cinema",
    "movie theater",
    "movie theatre",
    "events",
    "event",
    "taxi",
    "subway",
    "bus",
    "delivery",
    "food delivery",
    "天气",
    "气温",
    "温度",
    "下雨",
    "空气质量",
    "路况",
    "通勤",
    "餐厅",
    "饭店",
    "咖啡店",
    "酒店",
    "旅馆",
    "药店",
    "医院",
    "电影院",
    "活动",
    "演出",
    "打车",
    "地铁",
    "公交",
    "外卖",
)

_WEATHER_QUERY_TOKENS = (
    "weather",
    "forecast",
    "temperature",
    "humidity",
    "air quality",
    "aqi",
    "wind",
    "weather.com.cn",
    "moji.com",
    "/weather/",
    "天气",
    "天气预报",
    "气温",
    "空气质量",
    "风力",
    "湿度",
)

_WEATHER_QUERY_CUE_TOKENS = (
    "帮我",
    "给我",
    "查",
    "查询",
    "看看",
    "看下",
    "搜",
    "搜索",
    "what",
    "how",
    "today",
    "tomorrow",
    "forecast",
    "today's",
    "明天",
    "今天",
    "后天",
    "现在",
    "near me",
    "附近",
    "当地",
)

_WEATHER_BROWSER_DEMO_TOKENS = (
    "浏览器",
    "怎么操作浏览器",
    "操作浏览器",
    "网页操作",
    "展示浏览器",
    "browser demo",
    "show browser",
    "show steps",
    "visible browser",
    "有头",
    "headful",
    "炫技",
)

_WEATHER_RENDER_TOKENS = (
    "渲染",
    "render",
    "headless false",
    "headless: false",
    "有头",
    "headful",
)

_WEATHER_DOMAIN_HINTS = (
    "weather.com.cn",
    "moji.com",
    "tianqi.com",
)

_FACT_FRESHNESS_CUE_TOKENS = (
    "latest",
    "recent",
    "current",
    "currently",
    "today",
    "now",
    "as of",
    "截至",
    "目前",
    "现在",
    "最近",
    "最新",
    "当前",
)

_LOCAL_CLOCK_QUERY_TOKENS = (
    "current time",
    "time is it",
    "what time",
    "today's date",
    "what date",
    "time now",
    "the time",
    "现在几点",
    "现在的时间",
    "当前时间",
    "几点了",
    "时间是多少",
    "今天几号",
    "今天星期几",
    "现在日期",
    "日期是多少",
)


class RouterAgent:
    """
    主脑路由器 Agent
    负责意图识别和任务拆解
    """

    def __init__(self, llm_client: LLMClient = None):
        self.llm = llm_client or LLMClient()
        self.name = "Router"

    @staticmethod
    def _normalize_query_candidates(raw_queries: Any) -> List[str]:
        if isinstance(raw_queries, str):
            candidates = [raw_queries]
        elif isinstance(raw_queries, list):
            candidates = [str(item or "") for item in raw_queries]
        else:
            candidates = []

        normalized: List[str] = []
        seen = set()
        for candidate in candidates:
            query = re.sub(r"\s+", " ", str(candidate or "")).strip().strip("\"'")
            if len(query) < 4 or len(query) > 120:
                continue
            key = query.lower()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(query)
        return normalized[:2]

    @staticmethod
    def _looks_like_local_clock_query(user_input: str) -> bool:
        normalized = str(user_input or "").strip().lower()
        if not normalized:
            return False
        return any(token in normalized for token in _LOCAL_CLOCK_QUERY_TOKENS)

    @classmethod
    def _should_consult_fact_verification_guard(
        cls,
        user_input: str,
        result: Dict[str, Any],
    ) -> bool:
        tasks = result.get("tasks", []) or []
        if tasks:
            return False

        direct_answer = str(result.get("direct_answer", "") or "").strip()
        if not direct_answer:
            return False

        if cls._looks_like_local_clock_query(user_input):
            return False

        normalized = f"{user_input} {direct_answer}".lower()
        return any(token in normalized for token in _FACT_FRESHNESS_CUE_TOKENS)

    def _assess_fact_verification_need(
        self,
        user_input: str,
        current_time_context: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        current_date = (
            str((current_time_context or {}).get("local_date", "") or "").strip()
            or date.today().isoformat()
        )
        response = self.llm.chat_with_system(
            system_prompt=FACT_VERIFICATION_GUARD_PROMPT.format(
                current_date=current_date,
                user_input=user_input,
            ),
            user_message=user_input,
            temperature=0.1,
            max_tokens=2048,
            json_mode=True,
        )
        return self.llm.parse_json_response(response)

    @classmethod
    def _build_fact_verification_tasks(
        cls,
        user_input: str,
        queries: List[str],
    ) -> List[Dict[str, Any]]:
        task_list: List[Dict[str, Any]] = []
        effective_queries = queries or [str(user_input or "").strip()[:96]]

        for index, query in enumerate(effective_queries[:2], 1):
            description = (
                f"Verify the current factual claim using recent authoritative sources: {query}"
            )
            task_list.append(
                {
                    "description": description,
                    "tool_name": "web.fetch_and_extract",
                    "tool_args": {
                        "task": (
                            "Verify the user's time-sensitive factual question with recent authoritative "
                            f"sources. User question: {user_input}. Search query: {query}. Collect title, "
                            "source, published date, snippet, and link. Prefer official statements and major "
                            "news outlets. Avoid rumors, aggregators, shopping sites, and unrelated results."
                        ),
                        "query": query,
                        "limit": 8,
                    },
                    "priority": 10 - index,
                    "fallbacks": [
                        {"type": "retry", "param_patch": {"limit": 12}},
                    ],
                    "success_criteria": [
                        "result.success == True",
                        "len(result.data) > 0",
                    ],
                }
            )
        return task_list

    def _apply_fact_verification_guard(
        self,
        user_input: str,
        result: Dict[str, Any],
        current_time_context: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        if not self._should_consult_fact_verification_guard(user_input, result):
            return result

        try:
            assessment = self._assess_fact_verification_need(user_input, current_time_context)
        except Exception as exc:
            logger.warning(f"Router fact verification guard fallback: {exc}")
            return result

        if not bool(assessment.get("requires_verification", False)):
            return result

        queries = self._normalize_query_candidates(assessment.get("queries"))
        guarded_result = dict(result)
        guarded_result["direct_answer"] = ""
        guarded_result["tasks"] = self._build_fact_verification_tasks(user_input, queries)
        guard_reason = str(assessment.get("reason", "") or "").strip()
        if guard_reason:
            existing_reason = str(guarded_result.get("reasoning", "") or "").strip()
            guarded_result["reasoning"] = (
                f"{existing_reason}\nVerification guard: {guard_reason}".strip()
            )
        return self._normalize_task_plan_shape(guarded_result)

    @staticmethod
    def _tokenize_text(text: str) -> set[str]:
        tokens = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9][A-Za-z0-9_.-]{1,}", str(text or ""))
        normalized = set()
        for raw in tokens:
            token = raw.strip("._-").lower()
            if len(token) >= 2:
                normalized.add(token)
        return normalized

    @staticmethod
    def _extract_first_url(text: str) -> str:
        return extract_first_url(text)

    @staticmethod
    def _contains_any(text: str, tokens: tuple[str, ...]) -> bool:
        normalized = str(text or "").lower()
        return any(token in normalized for token in tokens if token)

    @classmethod
    def _looks_like_weather_query(cls, user_input: str) -> bool:
        normalized = str(user_input or "").strip().lower()
        if not normalized:
            return False

        if any(domain in normalized for domain in _WEATHER_DOMAIN_HINTS):
            return True

        has_weather_topic = cls._contains_any(normalized, _WEATHER_QUERY_TOKENS)
        if not has_weather_topic:
            return False

        return (
            bool(cls._extract_first_url(user_input))
            or cls._contains_any(normalized, _WEATHER_QUERY_CUE_TOKENS)
            or "?" in normalized
            or "？" in normalized
        )

    @classmethod
    def _wants_browser_weather_route(cls, user_input: str) -> bool:
        normalized = str(user_input or "").strip().lower()
        if not normalized:
            return False
        if cls._contains_any(normalized, _WEATHER_BROWSER_DEMO_TOKENS):
            return True
        return bool(cls._extract_first_url(user_input)) and cls._contains_any(
            normalized,
            _WEATHER_RENDER_TOKENS,
        )

    @staticmethod
    def _normalize_preferred_site(site: str) -> str:
        value = str(site or "").strip()
        if not value:
            return ""
        if not value.startswith(("http://", "https://")):
            value = f"https://{value.lstrip('/')}"
        return value.rstrip("/") + "/"

    @classmethod
    def _preferred_weather_source_urls(cls, user_preferences: dict | None = None) -> list[str]:
        source_urls: list[str] = []
        for item in (user_preferences or {}).get("preferred_sites", []) or []:
            normalized = cls._normalize_preferred_site(item)
            if normalized and any(domain in normalized.lower() for domain in _WEATHER_DOMAIN_HINTS):
                source_urls.append(normalized)
        return source_urls

    @staticmethod
    def _sanitize_weather_location(candidate: str) -> str:
        cleaned = str(candidate or "").strip()
        cleaned = re.sub(
            r"^(?:给我|帮我|麻烦|请|我想看|我想知道|想知道|查查|查一下|查询|看看|看下|搜一下|搜索一下|"
            r"抓取|提取|获取|读取|查看|打开|访问|进入|使用|前往|去|显示|展示|渲染)+",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            r"^(?:show|tell me|find|get|check|open|visit|read|extract|fetch|retrieve|"
            r"what(?:'s| is)?|how is)\s+",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            r"^(?:今天|明天|后天|现在|当前|本周|周末|这周|未来几天|未来7天|一周)+的?",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            r"(?:今天|明天|后天|现在|当前|本地|当地|附近|周末|本周|这周|未来几天|未来7天|一周|"
            r"天气详情|天气页面|天气数据|天气预报|天气情况|气温|空气质量|数据|页面|详情|信息)+$",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = cleaned.strip(" ,，。！？?：:;；")
        cleaned = cleaned.rstrip("的")
        if cleaned in {"今天", "明天", "后天", "当前", "现在", "本周", "周末", "本地", "当地", "附近"}:
            return ""
        if len(cleaned) < 2 or len(cleaned) > 32:
            return ""
        return cleaned

    @classmethod
    def _extract_weather_location(
        cls,
        user_input: str,
        current_location_context: dict | None = None,
    ) -> str:
        raw_text = str(user_input or "").strip()
        normalized = raw_text.lower()
        direct_url = cls._extract_first_url(raw_text)

        if cls._contains_any(normalized, _EXPLICIT_LOCATION_REQUEST_TOKENS):
            context_location = str((current_location_context or {}).get("location", "") or "").strip()
            if context_location:
                return context_location

        chinese_patterns = (
            r"([\u4e00-\u9fff]{2,12})的?(?:天气|天气预报|气温|空气质量)",
            r"(?:天气|天气预报|气温|空气质量)[^\u4e00-\u9fff]{0,4}([\u4e00-\u9fff]{2,12})",
        )
        for pattern in chinese_patterns:
            match = re.search(pattern, raw_text)
            if match:
                candidate = cls._sanitize_weather_location(match.group(1))
                if candidate:
                    return candidate

        if direct_url:
            return ""

        english_patterns = (
            r"\bweather in ([a-z][a-z .'\-]{1,40})",
            r"\bforecast for ([a-z][a-z .'\-]{1,40})",
            r"\b([a-z][a-z .'\-]{1,40}) weather\b",
        )
        for pattern in english_patterns:
            match = re.search(pattern, normalized, flags=re.IGNORECASE)
            if match:
                candidate = cls._sanitize_weather_location(match.group(1))
                if candidate:
                    return candidate

        if current_location_context and cls._should_include_location_context(user_input):
            context_location = str(current_location_context.get("location", "") or "").strip()
            if context_location:
                return context_location
        return ""

    @staticmethod
    def _parse_local_base_date(current_time_context: dict | None = None) -> date | None:
        local_date = str((current_time_context or {}).get("local_date", "") or "").strip()
        if not local_date:
            return None
        try:
            return date.fromisoformat(local_date)
        except ValueError:
            return None

    @classmethod
    def _describe_weather_timeframe(
        cls,
        user_input: str,
        current_time_context: dict | None = None,
    ) -> str:
        normalized = str(user_input or "").strip().lower()
        base_date = cls._parse_local_base_date(current_time_context)

        def _with_date(label: str, offset_days: int) -> str:
            if base_date is None:
                return label
            return f"{label}（{(base_date + timedelta(days=offset_days)).isoformat()}）"

        if "后天" in normalized or "day after tomorrow" in normalized:
            return _with_date("后天", 2)
        if "明天" in normalized or "tomorrow" in normalized:
            return _with_date("明天", 1)
        if "周末" in normalized or "weekend" in normalized:
            return "本周末"
        if "本周" in normalized or "这周" in normalized or "7天" in normalized or "7-day" in normalized:
            return "本周"
        if "今天" in normalized or "today" in normalized or "当前" in normalized or "现在" in normalized:
            return _with_date("今天", 0)
        return _with_date("当前", 0) if base_date is not None else "当前"

    @staticmethod
    def _source_label(source_url: str) -> str:
        label = re.sub(r"^https?://", "", str(source_url or "").strip(), flags=re.IGNORECASE)
        return label.rstrip("/") or str(source_url or "").strip()

    @classmethod
    def _build_deterministic_weather_plan(
        cls,
        user_input: str,
        *,
        user_preferences: dict | None = None,
        current_time_context: dict | None = None,
        current_location_context: dict | None = None,
    ) -> Dict[str, Any] | None:
        if not cls._looks_like_weather_query(user_input):
            return None

        direct_url = cls._extract_first_url(user_input)
        wants_browser = cls._wants_browser_weather_route(user_input)
        source_urls = cls._preferred_weather_source_urls(user_preferences)
        primary_source_url = direct_url or (source_urls[0] if source_urls else "")
        backup_source_url = source_urls[1] if len(source_urls) > 1 else ""
        location_hint = cls._extract_weather_location(user_input, current_location_context)
        timeframe_hint = cls._describe_weather_timeframe(user_input, current_time_context)
        target_label = f"{location_hint}的{timeframe_hint}天气" if location_hint else f"{timeframe_hint}天气"
        field_text = "temperature, weather condition, humidity, wind, and air quality (AQI)"
        search_timeframe_hint = re.sub(r"（\d{4}-\d{2}-\d{2}）", "", timeframe_hint).strip()
        browser_query = " ".join(part for part in [location_hint, search_timeframe_hint, "天气"] if part).strip() or "天气"

        tasks: list[dict[str, Any]] = []
        if wants_browser:
            browser_start_url = primary_source_url if direct_url else "https://www.bing.com/"
            browser_description = (
                f"Use a visible browser to retrieve {target_label}. "
                + (
                    f"Start from {primary_source_url} and treat the user-provided weather URL as authoritative. "
                    if direct_url
                    else f"Start from Bing, search the concise query '{browser_query}', open the most relevant weather detail page, then extract {field_text}. "
                )
                +
                f"Extract {field_text}. Do not stop at search-result pages, news pages, navigation pages, "
                f"403/redirect holding pages, or unrelated cities."
            )
            tasks.append(
                {
                    "task_id": "task_1",
                    "tool_name": "browser.interact",
                    "description": browser_description,
                    "params": {
                        "task": browser_description,
                        "query": browser_query,
                        "start_url": browser_start_url,
                        "headless": False,
                        "max_steps": 10,
                        "show_steps": True,
                    },
                    "priority": 10,
                    "success_criteria": [
                        "result.success == True",
                        "len(result.data) > 0",
                        "len(result.steps) > 0",
                    ],
                }
            )

            backup_description = (
                (
                    f"Use {cls._source_label(backup_source_url)} as a non-browser backup source for {target_label}. "
                    if backup_source_url else
                    f"Use a non-browser backup extraction path for {target_label}. "
                )
                + f"Prefer city weather detail pages over news pages and navigation pages. "
                + f"Extract {field_text} and reject unrelated cities."
            )
            tasks.append(
                {
                    "task_id": "task_2",
                    "tool_name": "web.fetch_and_extract",
                    "description": backup_description,
                    "params": {
                        "url": direct_url if direct_url else (backup_source_url or ""),
                        "query": browser_query,
                        "limit": 8,
                    },
                    "priority": 9,
                    "success_criteria": [
                        "result.success == True",
                        "len(result.data) > 0",
                    ],
                    "fallbacks": [
                        {"type": "retry", "param_patch": {"limit": 12}},
                    ],
                }
            )
        else:
            if direct_url:
                primary_description = (
                    f"Directly obtain {target_label} from {direct_url} as the user-provided weather page. "
                    f"Prefer city weather detail or forecast pages over news pages and navigation pages. "
                    f"Extract {field_text}. Reject unrelated cities and generic weather news."
                )
            elif primary_source_url:
                primary_description = (
                    f"Use {cls._source_label(primary_source_url)} as the preferred weather source for {target_label}. "
                    f"Prefer city weather detail or forecast pages over news pages and navigation pages. "
                    f"Extract {field_text}. Reject unrelated cities and generic weather news."
                )
            else:
                primary_description = (
                    f"Obtain {target_label} from a relevant city weather detail or forecast page. "
                    f"Prefer detail pages over news pages and navigation pages. "
                    f"Extract {field_text}. Reject unrelated cities and generic weather news."
                )
            tasks.append(
                {
                    "task_id": "task_1",
                    "tool_name": "web.fetch_and_extract",
                    "description": primary_description,
                    "params": {
                        "url": direct_url if direct_url else (primary_source_url or ""),
                        "query": browser_query,
                        "limit": 8,
                    },
                    "priority": 10,
                    "success_criteria": [
                        "result.success == True",
                        "len(result.data) > 0",
                    ],
                    "fallbacks": [
                        {"type": "retry", "param_patch": {"limit": 12}},
                    ],
                }
            )

            if not direct_url and backup_source_url:
                backup_description = (
                    f"Use {cls._source_label(backup_source_url)} as the secondary weather source for {target_label}. "
                    f"Extract {field_text}. Prefer weather detail pages and reject news, navigation pages, "
                    f"and unrelated cities."
                )
                tasks.append(
                    {
                        "task_id": "task_2",
                        "tool_name": "web.fetch_and_extract",
                        "description": backup_description,
                        "params": {
                            "url": backup_source_url,
                            "query": browser_query,
                            "limit": 8,
                        },
                        "priority": 9,
                        "success_criteria": [
                            "result.success == True",
                            "len(result.data) > 0",
                        ],
                        "fallbacks": [
                            {"type": "retry", "param_patch": {"limit": 12}},
                        ],
                    }
                )

        route_reason = (
            "Structured weather query matched the deterministic weather route. "
            "The router fixed the tool choice and required weather fields before execution."
        )
        if location_hint:
            route_reason += f" Location resolved as {location_hint}."
        if timeframe_hint:
            route_reason += f" Timeframe resolved as {timeframe_hint}."

        return {
            "intent": "weather_query_with_browser_demo" if wants_browser else "weather_query",
            "confidence": 0.99 if direct_url or location_hint else 0.95,
            "reasoning": route_reason,
            "direct_answer": "",
            "tasks": tasks,
            "is_high_risk": False,
        }

    @classmethod
    def _should_include_location_context(cls, user_input: str) -> bool:
        normalized_text = str(user_input or "").strip().lower()
        if not normalized_text:
            return False

        if any(token in normalized_text for token in _EXPLICIT_LOCATION_REQUEST_TOKENS):
            return True

        text_tokens = cls._tokenize_text(normalized_text)
        topic_tokens = {
            token.lower()
            for token in _LOCATION_SENSITIVE_TOPIC_TOKENS
        }
        return bool(text_tokens & topic_tokens)

    @classmethod
    def _collect_schema_keys(cls, schema: Any) -> set[str]:
        keys: set[str] = set()
        if isinstance(schema, dict):
            properties = schema.get("properties")
            if isinstance(properties, dict):
                for key, child in properties.items():
                    key_token = str(key or "").strip()
                    if key_token:
                        keys.add(key_token)
                    keys.update(cls._collect_schema_keys(child))

            required = schema.get("required")
            if isinstance(required, list):
                for item in required:
                    token = str(item or "").strip()
                    if token:
                        keys.add(token)

            for composite in ("anyOf", "oneOf", "allOf"):
                nodes = schema.get(composite)
                if isinstance(nodes, list):
                    for node in nodes:
                        keys.update(cls._collect_schema_keys(node))

            items = schema.get("items")
            if isinstance(items, (dict, list)):
                keys.update(cls._collect_schema_keys(items))
        elif isinstance(schema, list):
            for node in schema:
                keys.update(cls._collect_schema_keys(node))
        return keys

    @classmethod
    def _score_registered_tool(cls, registered_tool, text: str, params: Dict[str, Any]) -> int:
        lowered = str(text or "").lower()
        text_tokens = cls._tokenize_text(text)
        param_keys = {str(key).strip().lower() for key in params.keys() if str(key).strip()}
        param_tokens = cls._tokenize_text(" ".join(param_keys))

        spec = registered_tool.spec
        score = 0

        tool_name = str(spec.name or "")
        task_type = str(spec.task_type or "")
        if tool_name and tool_name.lower() in lowered:
            score += 10
        if task_type and task_type.lower() in lowered:
            score += 7

        name_tokens = cls._tokenize_text(tool_name.replace(".", " ").replace("_", " "))
        task_type_tokens = cls._tokenize_text(task_type.replace(".", " ").replace("_", " "))
        tag_tokens = cls._tokenize_text(" ".join(str(tag or "") for tag in (spec.tags or [])))
        description_tokens = cls._tokenize_text(str(spec.description or ""))
        schema_keys = {item.lower() for item in cls._collect_schema_keys(spec.input_schema)}
        schema_tokens = cls._tokenize_text(" ".join(schema_keys))

        score += 4 * len(text_tokens & name_tokens)
        score += 3 * len(text_tokens & task_type_tokens)
        score += 2 * len(text_tokens & tag_tokens)
        score += min(6, len(text_tokens & description_tokens))

        if schema_keys:
            score += 6 * len(param_keys & schema_keys)
            score += 3 * len(param_tokens & schema_tokens)
        else:
            score += 2 * len(param_tokens & (name_tokens | tag_tokens | description_tokens))

        score += len(param_tokens & text_tokens)
        return score

    @classmethod
    def _guess_registered_tool(cls, task_data: Dict[str, Any]):
        registry = get_builtin_tool_registry()
        params = task_data.get("tool_args")
        if not isinstance(params, dict):
            params = task_data.get("params", {})
        if not isinstance(params, dict):
            params = {}

        text_parts = [str(task_data.get("description", "") or "")]
        for key, value in params.items():
            if isinstance(value, str):
                text_parts.append(f"{key} {value}")
            else:
                text_parts.append(str(key))
        combined_text = " ".join(text_parts)

        ranked = []
        risk_rank = {"low": 0, "medium": 1, "high": 2}
        for tool in registry.list_tools():
            score = cls._score_registered_tool(tool, combined_text, params)
            if score <= 0:
                continue
            ranked.append(
                (
                    score,
                    -tool.max_parallelism,
                    -risk_rank.get(str(tool.spec.risk_level or "medium"), 1),
                    tool,
                )
            )

        if not ranked:
            return None

        ranked.sort(key=lambda item: item[:3], reverse=True)
        return ranked[0][-1]

    @classmethod
    def _normalize_task_plan_shape(cls, result: Dict[str, Any]) -> Dict[str, Any]:
        registry = get_builtin_tool_registry()
        normalized_tasks = []
        for raw_task in result.get("tasks", []) or []:
            task_data = dict(raw_task)
            tool_name = str(task_data.get("tool_name", "") or "").strip()
            task_type = str(task_data.get("task_type", "") or "").strip()
            registered_tool = None

            if tool_name:
                registered_tool = registry.get(tool_name)
            if registered_tool is None and task_type:
                registered_tool = registry.get_by_task_type(task_type)
            if registered_tool is None:
                registered_tool = cls._guess_registered_tool(task_data)
            if registered_tool is not None:
                tool_name = registered_tool.spec.name
                task_type = registered_tool.spec.task_type

            tool_args = task_data.get("tool_args")
            params = task_data.get("params")
            if isinstance(tool_args, dict):
                task_data["tool_args"] = tool_args
                task_data["params"] = dict(tool_args)
            elif isinstance(params, dict):
                task_data["params"] = params
                task_data["tool_args"] = dict(params)
            else:
                task_data["params"] = {}
                task_data["tool_args"] = {}

            task_data["tool_name"] = tool_name
            task_data["task_type"] = task_type
            normalized_tasks.append(task_data)

        direct_answer = result.get("direct_answer", "")
        if direct_answer is None:
            direct_answer = ""
        result["direct_answer"] = str(direct_answer)
        result["tasks"] = normalized_tasks
        return result

    @classmethod
    def _repair_task_params_from_user_input(
        cls,
        user_input: str,
        result: Dict[str, Any],
    ) -> Dict[str, Any]:
        direct_url = cls._extract_first_url(user_input)
        if not direct_url:
            return result

        search_results_url = cls._looks_like_search_results_url(direct_url)
        repaired_tasks = []
        for raw_task in result.get("tasks", []) or []:
            task_data = dict(raw_task)
            params = task_data.get("params")
            if not isinstance(params, dict):
                params = {}
            tool_args = task_data.get("tool_args")
            if isinstance(tool_args, dict):
                tool_args = dict(tool_args)
            else:
                tool_args = dict(params)

            tool_name = str(task_data.get("tool_name", "") or "").strip()
            if search_results_url and tool_name == "web.fetch_and_extract":
                tool_name = "web.smart_extract"
                task_data["tool_name"] = tool_name
                task_data["task_type"] = "enhanced_web_worker"
            if tool_name == "browser.interact" and not str(params.get("start_url", "") or "").strip():
                params["start_url"] = direct_url
                tool_args["start_url"] = direct_url
            elif tool_name in {"web.fetch_and_extract", "web.smart_extract"} and not str(params.get("url", "") or "").strip():
                params["url"] = direct_url
                tool_args["url"] = direct_url

            task_data["params"] = params
            task_data["tool_args"] = tool_args
            repaired_tasks.append(task_data)

        result["tasks"] = repaired_tasks
        return result

    @staticmethod
    def _looks_like_search_results_url(url: str) -> bool:
        normalized = str(url or "").strip()
        if not normalized:
            return False
        try:
            parsed = urlparse(normalized)
        except Exception:
            return False

        path = str(parsed.path or "").lower()
        query = {str(key or "").lower(): value for key, value in parse_qs(parsed.query or "").items()}
        if not query:
            return False

        query_keys = {"q", "query", "wd", "word", "keyword", "search", "text", "p"}
        has_query_term = any(key in query for key in query_keys)
        if not has_query_term:
            return False

        path_hints = ("/search", "/s", "/find", "/query")
        return any(hint in path for hint in path_hints) or "search" in str(parsed.netloc or "").lower()

    @staticmethod
    def _build_router_system_prompt() -> str:
        dynamic_catalog = "\n".join(build_dynamic_tool_prompt_lines())
        return f"{ROUTER_SYSTEM_PROMPT}\n\n{ROUTER_OUTPUT_APPENDIX}\n{dynamic_catalog}"

    @classmethod
    def _build_deterministic_tool_hints(
        cls,
        user_input: str,
        session_artifacts: list | None = None,
        user_preferences: dict | None = None,
    ) -> list[str]:
        registry = get_builtin_tool_registry()
        preferred_tools = {
            str(item).strip()
            for item in (user_preferences or {}).get("preferred_tools", []) or []
            if str(item).strip()
        }
        artifact_text = []
        for artifact in session_artifacts or []:
            if not isinstance(artifact, dict):
                continue
            artifact_text.append(str(artifact.get("name", "") or ""))
            artifact_text.append(str(artifact.get("artifact_type", "") or ""))
            artifact_text.append(str(artifact.get("preview", "") or ""))
        combined = " ".join([str(user_input or ""), *artifact_text])

        scored = []
        for tool in registry.list_tools():
            score = cls._score_registered_tool(tool, combined, {})
            if score <= 0:
                continue
            if tool.spec.name in preferred_tools:
                score += 6
            scored.append((score, tool.spec.name, tool.spec.description))

        scored.sort(reverse=True)
        hints = []
        for _, tool_name, description in scored[:2]:
            hints.append(f"- {tool_name}: {description}")
        return hints

    def analyze_intent(
        self,
        user_input: str,
        conversation_history: list = None,
        related_history: list = None,
        session_artifacts: list = None,
        user_preferences: dict = None,
        current_time_context: dict = None,
        current_location_context: dict = None,
        work_context: dict = None,
        resource_memory: list = None,
        successful_paths: list = None,
    ) -> Dict[str, Any]:
        """
        分析用户意图并拆解任务

        Args:
            user_input: 用户原始输入
            conversation_history: 最近的对话历史
            related_history: 向量检索到的相关历史记忆

        Returns:
            包含意图和任务列表的字典
        """
        log_agent_action(self.name, "开始分析用户意图", user_input[:50] + "...")

        deterministic_plan = self._build_deterministic_weather_plan(
            user_input,
            user_preferences=user_preferences,
            current_time_context=current_time_context,
            current_location_context=current_location_context,
        )
        if deterministic_plan is not None:
            deterministic_plan = self._normalize_task_plan_shape(deterministic_plan)
            deterministic_plan = self._repair_task_params_from_user_input(user_input, deterministic_plan)
            log_agent_action(
                self.name,
                f"意图识别完成: {deterministic_plan.get('intent')}",
                f"deterministic route, 子任务数: {len(deterministic_plan.get('tasks', []))}",
            )
            return deterministic_plan

        # 构建包含对话历史的用户消息
        user_message = ""
        if conversation_history:
            history_lines = []
            for turn in conversation_history:
                history_lines.append(f"用户: {turn['user_input']}")
                history_lines.append(f"结果: {'成功' if turn.get('success') else '失败'} - {turn.get('output', '')[:150]}")
            user_message += "## 最近的对话历史（用于理解上下文）：\n"
            user_message += "\n".join(history_lines)
            user_message += "\n\n---\n"

        if related_history:
            memory_lines = []
            for memory in related_history[:3]:
                content = str(memory.get("content", "")).replace("\n", " ").strip()
                if content:
                    memory_lines.append(f"- {content[:220]}")
            if memory_lines:
                user_message += "## 相关历史记忆（可用于复用上下文或直接回答追问）：\n"
                user_message += "\n".join(memory_lines)
                user_message += "\n\n---\n"

        if session_artifacts:
            artifact_lines = []
            for artifact in session_artifacts[:5]:
                if not isinstance(artifact, dict):
                    continue
                name = str(artifact.get("name", "") or "").strip()
                artifact_type = str(artifact.get("artifact_type", "") or "").strip()
                path_value = str(artifact.get("path", "") or "").strip()
                preview = str(artifact.get("preview", "") or "").strip()
                summary = path_value or preview
                if name and summary:
                    artifact_lines.append(f"- [{artifact_type}] {name}: {summary[:220]}")
                elif name:
                    artifact_lines.append(f"- [{artifact_type}] {name}")
            if artifact_lines:
                user_message += "## Recent session artifacts (can be reused as working context):\n"
                user_message += "\n".join(artifact_lines)
                user_message += "\n\n---\n"

        if user_preferences:
            preference_lines = []
            output_directory = str(user_preferences.get("default_output_directory", "") or "").strip()
            if output_directory:
                preference_lines.append(f"- Default output directory: {output_directory}")
            preferred_tools = [
                str(item).strip()
                for item in user_preferences.get("preferred_tools", []) or []
                if str(item).strip()
            ]
            if preferred_tools:
                preference_lines.append(f"- Preferred tools: {', '.join(preferred_tools[:5])}")
            preferred_sites = [
                str(item).strip()
                for item in user_preferences.get("preferred_sites", []) or []
                if str(item).strip()
            ]
            if preferred_sites:
                preference_lines.append(f"- Preferred sites: {', '.join(preferred_sites[:5])}")
            task_templates = user_preferences.get("task_templates", {}) or {}
            if task_templates:
                preference_lines.append(
                    f"- Saved templates: {', '.join(list(task_templates.keys())[:5])}"
                )
            if preference_lines:
                user_message += "## User preferences (prefer these when they fit):\n"
                user_message += "\n".join(preference_lines)
                user_message += "\n\n---\n"

        if current_time_context:
            time_lines = []
            iso_datetime = str(current_time_context.get("iso_datetime", "") or "").strip()
            local_date = str(current_time_context.get("local_date", "") or "").strip()
            local_time = str(current_time_context.get("local_time", "") or "").strip()
            weekday = str(current_time_context.get("weekday", "") or "").strip()
            timezone_name = str(current_time_context.get("timezone", "") or "").strip()
            if iso_datetime:
                time_lines.append(f"- Current datetime: {iso_datetime}")
            if local_date:
                time_lines.append(f"- Current date: {local_date}")
            if local_time:
                time_lines.append(f"- Current local time: {local_time}")
            if weekday:
                time_lines.append(f"- Weekday: {weekday}")
            if timezone_name:
                time_lines.append(f"- Timezone: {timezone_name}")
            if time_lines:
                user_message += "## Current local time (treat this as the authoritative current time for planning):\n"
                user_message += "\n".join(time_lines)
                user_message += "\n\n---\n"

        if current_location_context and self._should_include_location_context(user_input):
            location_lines = []
            location_name = str(current_location_context.get("location", "") or "").strip()
            timezone_name = str(current_location_context.get("timezone", "") or "").strip()
            source_name = str(current_location_context.get("source", "") or "").strip()
            if location_name:
                location_lines.append(f"- User location: {location_name}")
            if timezone_name:
                location_lines.append(f"- Location timezone: {timezone_name}")
            if source_name:
                location_lines.append(f"- Source: {source_name}")
            if location_lines:
                user_message += "## Current user location (treat this as the authoritative user location for geography-dependent planning):\n"
                user_message += "\n".join(location_lines)
                user_message += "\n\n---\n"

        if work_context:
            context_lines = []
            goal = work_context.get("goal") if isinstance(work_context, dict) else {}
            project = work_context.get("project") if isinstance(work_context, dict) else {}
            todo = work_context.get("todo") if isinstance(work_context, dict) else {}
            open_todos = work_context.get("open_todos") if isinstance(work_context, dict) else []
            if isinstance(goal, dict) and goal.get("title"):
                context_lines.append(f"- Active goal: {goal.get('title', '')}")
            if isinstance(project, dict) and project.get("title"):
                context_lines.append(f"- Active project: {project.get('title', '')}")
            if isinstance(todo, dict) and todo.get("title"):
                context_lines.append(f"- Current todo: {todo.get('title', '')} [{todo.get('status', '')}]")
            if open_todos:
                todo_labels = [
                    str(item.get("title", "") or "")
                    for item in open_todos[:5]
                    if isinstance(item, dict) and str(item.get("title", "")).strip()
                ]
                if todo_labels:
                    context_lines.append(f"- Open todos: {', '.join(todo_labels)}")
            if context_lines:
                user_message += "## Work context (continue this work when relevant):\n"
                user_message += "\n".join(context_lines)
                user_message += "\n\n---\n"

        if resource_memory:
            resource_lines = []
            for item in resource_memory[:5]:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "") or "").strip()
                artifact_type = str(item.get("artifact_type", "") or "").strip()
                location = str(item.get("path", "") or item.get("preview", "") or "").strip()
                if name and location:
                    resource_lines.append(f"- [{artifact_type}] {name}: {location[:220]}")
                elif name:
                    resource_lines.append(f"- [{artifact_type}] {name}")
            if resource_lines:
                user_message += "## Reusable resource memory (prefer reuse before regenerating):\n"
                user_message += "\n".join(resource_lines)
                user_message += "\n\n---\n"

        if successful_paths:
            pattern_lines = []
            for item in successful_paths[:3]:
                if not isinstance(item, dict):
                    continue
                tools = [str(tool).strip() for tool in item.get("tool_sequence", []) or [] if str(tool).strip()]
                if not tools:
                    continue
                pattern_lines.append(
                    f"- Similar success path: {' -> '.join(tools[:6])} | {str(item.get('user_input', '') or '')[:120]}"
                )
            if pattern_lines:
                user_message += "## Successful execution patterns (reuse when appropriate):\n"
                user_message += "\n".join(pattern_lines)
                user_message += "\n\n---\n"

        deterministic_hints = self._build_deterministic_tool_hints(
            user_input,
            session_artifacts,
            user_preferences,
        )
        if deterministic_hints:
            user_message += "## Deterministic tool hints (use if they fit the task):\n"
            user_message += "\n".join(deterministic_hints)
            user_message += "\n\n---\n"

        user_message += f"请分析以下用户指令并拆解任务：\n\n{user_input}"

        # 使用配置的 Router 专用 max_tokens
        from config.settings import settings

        response = self.llm.chat_with_system(
            system_prompt=self._build_router_system_prompt(),
            user_message=user_message,
            temperature=0.3,
            max_tokens=settings.LLM_ROUTER_MAX_TOKENS,
            json_mode=True,
        )

        logger.debug(f"Router LLM 原始响应: {response.content[:300] if response.content else '(空)'}")

        try:
            result = self._normalize_task_plan_shape(
                self.llm.parse_json_response(response)
            )
            result = self._repair_task_params_from_user_input(user_input, result)
            result = self._apply_fact_verification_guard(
                user_input,
                result,
                current_time_context,
            )
            log_agent_action(
                self.name,
                f"意图识别完成: {result.get('intent')}",
                f"置信度 {result.get('confidence', 0):.2f}, 子任务数: {len(result.get('tasks', []))}"
            )
            return result
        except Exception as e:
            logger.error(f"Router 解析失败: {e}")
            return {
                "intent": "unknown",
                "confidence": 0.0,
                "reasoning": f"解析失败: {str(e)}",
                "direct_answer": "",
                "tasks": [],
                "is_high_risk": False,
            }

    def route(self, state: OmniCoreState) -> OmniCoreState:
        """
        LangGraph 节点函数：执行路由逻辑

        Args:
            state: 当前图状态

        Returns:
            更新后的状态
        """
        user_input = state["user_input"]

        # 分析意图（传入对话历史）
        conversation_history = state.get("shared_memory", {}).get("conversation_history")
        related_history = state.get("shared_memory", {}).get("related_history")
        session_artifacts = state.get("shared_memory", {}).get("session_artifacts")
        user_preferences = state.get("shared_memory", {}).get("user_preferences")
        current_time_context = state.get("shared_memory", {}).get("current_time_context")
        current_location_context = state.get("shared_memory", {}).get("current_location_context")
        work_context = state.get("shared_memory", {}).get("work_context")
        resource_memory = state.get("shared_memory", {}).get("resource_memory")
        successful_paths = state.get("shared_memory", {}).get("successful_paths")
        analysis = self.analyze_intent(
            user_input,
            conversation_history,
            related_history,
            session_artifacts,
            user_preferences,
            current_time_context,
            current_location_context,
            work_context,
            resource_memory,
            successful_paths,
        )

        # 构建任务队列
        task_queue: List[TaskItem] = []
        for task_data in analysis.get("tasks", []):
            task_queue.append(build_task_item_from_plan(task_data))

        # 按优先级排序（高优先级在前）
        task_queue.sort(key=lambda x: x["priority"], reverse=True)

        # 更新状态
        state["current_intent"] = analysis.get("intent", "unknown")
        state["intent_confidence"] = analysis.get("confidence", 0.0)
        state["task_queue"] = task_queue
        state["policy_decisions"] = [
            build_policy_decision_from_task(task)
            for task in task_queue
        ]
        state["needs_human_confirm"] = analysis.get("is_high_risk", False) or any(
            task.get("requires_confirmation", False) for task in task_queue
        )
        state["shared_memory"]["router_high_risk_reason"] = analysis.get("high_risk_reason", "")
        state["shared_memory"]["router_direct_answer"] = str(analysis.get("direct_answer", "") or "").strip()
        state["execution_status"] = "routing"

        # 添加系统消息到 messages
        from langchain_core.messages import SystemMessage
        state["messages"].append(
            SystemMessage(content=f"Router 分析完成: {analysis.get('reasoning', '')}")
        )

        return state

    def create_hackernews_tasks(self) -> List[TaskItem]:
        """
        为 Hacker News 测试用例创建预定义任务
        这是一个便捷方法，用于测试
        """
        return [
            TaskItem(
                task_id="task_1_scrape",
                task_type="web_worker",
                tool_name="web.fetch_and_extract",
                description="抓取 Hacker News 首页前 5 条新闻的标题和链接",
                params={
                    "url": "https://news.ycombinator.com",
                    "action": "scrape",
                    "selectors": {
                        "items": ".athing",
                        "title": ".titleline > a",
                        "link": ".titleline > a@href",
                    },
                    "limit": 5,
                },
                status="pending",
                result=None,
                priority=10,
            ),
            TaskItem(
                task_id="task_2_save",
                task_type="file_worker",
                tool_name="file.read_write",
                description="将抓取的新闻数据保存到桌面的 txt 文件",
                params={
                    "action": "write",
                    "file_path": "~/Desktop/news_summary.txt",
                    "data_source": "task_1_scrape",  # 依赖上一个任务的结果
                    "format": "txt",
                },
                status="pending",
                result=None,
                priority=5,
            ),
        ]
