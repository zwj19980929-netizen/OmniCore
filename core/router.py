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

    @staticmethod
    def _normalize_preferred_site(site: str) -> str:
        value = str(site or "").strip()
        if not value:
            return ""
        if not value.startswith(("http://", "https://")):
            value = f"https://{value.lstrip('/')}"
        return value.rstrip("/") + "/"

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

        # 🔥 新增：检测用户是否要求有头模式（显示浏览器）
        user_input_lower = str(user_input or "").strip().lower()
        wants_headed = any(token in user_input_lower for token in [
            "有头", "headful", "headed", "显示浏览器", "展示浏览器",
            "show browser", "visible browser", "浏览器操作", "看操作"
        ])

        if not direct_url and not wants_headed:
            return result

        search_results_url = cls._looks_like_search_results_url(direct_url) if direct_url else False
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
                if direct_url:
                    params["start_url"] = direct_url
                    tool_args["start_url"] = direct_url
            elif tool_name in {"web.fetch_and_extract", "web.smart_extract"} and not str(params.get("url", "") or "").strip():
                if direct_url:
                    params["url"] = direct_url
                    tool_args["url"] = direct_url

            # 🔥 新增：如果用户要求有头模式，设置 headless=False
            if wants_headed:
                if tool_name == "browser.interact":
                    params["headless"] = False
                    tool_args["headless"] = False
                elif tool_name in {"web.fetch_and_extract", "web.smart_extract"}:
                    params["headless"] = False
                    tool_args["headless"] = False

                    # 🔥 新增：将GitHub API URL转换为网页URL（有头模式需要真实网页）
                    current_url = str(params.get("url", "") or tool_args.get("url", "") or "").strip()
                    if "api.github.com/repos/" in current_url:
                        # 转换 https://api.github.com/repos/owner/repo/contents -> https://github.com/owner/repo
                        import re
                        match = re.search(r'api\.github\.com/repos/([^/]+/[^/]+)', current_url)
                        if match:
                            web_url = f"https://github.com/{match.group(1)}"
                            params["url"] = web_url
                            tool_args["url"] = web_url
                            # 更新任务描述，说明使用网页而不是API
                            if "description" in task_data:
                                task_data["description"] = task_data["description"].replace("API", "网页")
                            if "task" in params:
                                params["task"] = str(params["task"]).replace("API", "网页")

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
        base_prompt = ROUTER_SYSTEM_PROMPT
        # Inject agent descriptions from registry if placeholder exists
        if "{{AGENT_CAPABILITIES}}" in base_prompt:
            from core.agent_registry import get_agent_registry
            registry = get_agent_registry()
            agent_descriptions = registry.build_router_agent_descriptions(lang="zh")
            base_prompt = base_prompt.replace("{{AGENT_CAPABILITIES}}", agent_descriptions)
        return f"{base_prompt}\n\n{ROUTER_OUTPUT_APPENDIX}\n{dynamic_catalog}"

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
        failure_patterns: list = None,
        current_os_context: dict = None,
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
                    metadata = memory.get("metadata", {}) if isinstance(memory.get("metadata"), dict) else {}
                    labels = []
                    memory_type = str(metadata.get("type", "") or "").strip()
                    if memory_type:
                        labels.append(memory_type)
                    if "success" in metadata:
                        labels.append("success" if bool(metadata.get("success")) else "failure")
                    scope_match = str(memory.get("scope_match", "") or "").strip()
                    if scope_match:
                        labels.append(scope_match)
                    prefix = f"[{', '.join(labels)}] " if labels else ""
                    memory_lines.append(f"- {prefix}{content[:220]}")
            if memory_lines:
                user_message += "## 相关历史记忆（可用于复用上下文或直接回答追问）：\n"
                user_message += "\n".join(memory_lines)
                user_message += "\n\n---\n"

                # 🔥 新增：检测用户是否要求重新执行（有头操作、显示浏览器等）
                user_input_lower = str(user_input or "").strip().lower()
                wants_reexecution = any(token in user_input_lower for token in [
                    "有头", "headful", "headed", "显示浏览器", "展示浏览器",
                    "show browser", "visible browser", "浏览器操作", "看操作",
                    "重新", "再次", "again", "重做"
                ])
                if wants_reexecution:
                    user_message += "\n**重要提示**：用户明确要求重新执行或使用有头模式（显示浏览器），即使历史记忆中有答案，也必须创建新的web_scraping或browser.interact任务，不要使用information_query直接回答。\n\n---\n"

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

        if current_os_context:
            os_lines = []
            os_display = str(current_os_context.get("os_display", "") or "").strip()
            shell = str(current_os_context.get("shell", "") or "").strip()
            pkg_managers = str(current_os_context.get("available_package_managers", "") or "").strip()
            pkg_hint = str(current_os_context.get("package_manager_hint", "") or "").strip()
            if os_display:
                os_lines.append(f"- OS: {os_display}")
            if shell:
                os_lines.append(f"- Shell: {shell}")
            if pkg_managers:
                os_lines.append(f"- Available package managers: {pkg_managers}")
            if pkg_hint:
                os_lines.append(f"- Package manager guidance: {pkg_hint}")
            if os_lines:
                user_message += (
                    "## Current system environment "
                    "(IMPORTANT: use OS-appropriate commands when planning terminal tasks):\n"
                )
                user_message += "\n".join(os_lines)
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

        if failure_patterns:
            failure_lines = []
            for item in failure_patterns[:3]:
                if not isinstance(item, dict):
                    continue
                tools = [
                    str(tool).strip()
                    for tool in item.get("tool_sequence", []) or []
                    if str(tool).strip()
                ]
                reason = str(item.get("failure_reason", "") or item.get("summary", "") or "").strip()
                visited_urls = [
                    str(url).strip()
                    for url in item.get("visited_urls", []) or []
                    if str(url).strip()
                ]
                line = "- Avoid repeating"
                if tools:
                    line += f": {' -> '.join(tools[:6])}"
                if reason:
                    line += f" | reason: {reason[:140]}"
                if visited_urls:
                    line += f" | urls: {', '.join(visited_urls[:3])}"
                failure_lines.append(line)
            if failure_lines:
                user_message += "## Failure patterns to avoid (do not repeat these paths blindly):\n"
                user_message += "\n".join(failure_lines)
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

    # ──────────────────────────────────────────────
    # 终端快速通道（P3）
    # ──────────────────────────────────────────────

    _TERMINAL_FAST_PATTERNS = re.compile(
        r"^("
        r"ls\b|ll\b|la\b|cat\b|head\b|tail\b|grep\b|find\b|rg\b|awk\b|sed\b|"
        r"echo\b|pwd\b|which\b|wc\b|file\b|less\b|more\b|"
        r"git\s|python\s|python3\s|node\s|npm\s|pip\s|make\s|"
        r"cd\s|mkdir\s|touch\s|cp\s|mv\s|rm\s|"
        r"curl\s|wget\s|ssh\s|scp\s|rsync\s|"
        r"docker\s|kubectl\s|terraform\s|"
        r"pytest\s?|jest\s?|cargo\s|go\s|"
        r"\./|/usr/|/bin/|/opt/"
        r")",
        re.IGNORECASE,
    )

    def _is_terminal_fast_path(self, user_input: str) -> bool:
        """
        判断输入是否应该走终端快速通道（跳过 LLM 路由）。
        快速通道：明确的 shell 命令，直接构建 terminal_worker 任务。
        """
        from config.settings import settings
        if not settings.TERMINAL_ENABLED:
            return False
        stripped = user_input.strip()
        # 以 ! 开头的由 main.py 拦截，这里只处理纯命令模式
        return bool(self._TERMINAL_FAST_PATTERNS.match(stripped))

    def _build_terminal_fast_task(self, user_input: str, state: OmniCoreState) -> OmniCoreState:
        """
        快速构建终端任务，跳过 LLM 路由，不调用 analyze_intent。
        """
        import uuid
        from core.policy_engine import evaluate_task_policy

        task: TaskItem = {
            "task_id": f"terminal_fast_{uuid.uuid4().hex[:8]}",
            "task_type": "terminal_worker",
            "tool_name": "terminal.execute",
            "description": f"执行终端命令: {user_input[:100]}",
            "params": {
                "action": "shell",
                "command": user_input.strip(),
            },
            "status": "pending",
            "result": None,
            "priority": 10,
            "execution_trace": [],
            "depends_on": [],
        }

        policy = evaluate_task_policy(task)
        task["requires_confirmation"] = policy.requires_confirmation
        task["risk_level"] = policy.risk_level
        task["policy_reason"] = policy.reason
        task["affected_resources"] = policy.affected_resources

        state["current_intent"] = "terminal_command"
        state["intent_confidence"] = 1.0
        state["task_queue"] = [task]
        state["policy_decisions"] = [build_policy_decision_from_task(task)]
        state["needs_human_confirm"] = policy.requires_confirmation
        state["shared_memory"]["router_direct_answer"] = ""
        state["shared_memory"]["router_high_risk_reason"] = (
            policy.reason if policy.requires_confirmation else ""
        )
        state["execution_status"] = "routing"

        log_agent_action("Router", "终端快速通道", f"$ {user_input[:60]}")
        return state

    def route(self, state: OmniCoreState) -> OmniCoreState:
        """
        LangGraph 节点函数：执行路由逻辑

        Args:
            state: 当前图状态

        Returns:
            更新后的状态
        """
        user_input = state["user_input"]

        # 终端快速通道：明确的 shell 命令跳过 LLM 路由
        if self._is_terminal_fast_path(user_input):
            return self._build_terminal_fast_task(user_input, state)

        # 分析意图（传入对话历史）
        conversation_history = state.get("shared_memory", {}).get("conversation_history")
        related_history = state.get("shared_memory", {}).get("related_history")
        session_artifacts = state.get("shared_memory", {}).get("session_artifacts")
        user_preferences = state.get("shared_memory", {}).get("user_preferences")
        current_time_context = state.get("shared_memory", {}).get("current_time_context")
        current_location_context = state.get("shared_memory", {}).get("current_location_context")
        current_os_context = state.get("shared_memory", {}).get("current_os_context")
        work_context = state.get("shared_memory", {}).get("work_context")
        resource_memory = state.get("shared_memory", {}).get("resource_memory")
        successful_paths = state.get("shared_memory", {}).get("successful_paths")
        failure_patterns = state.get("shared_memory", {}).get("failure_patterns")
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
            failure_patterns,
            current_os_context,
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
        # Dual-write to MessageBus
        from core.message_bus import MessageBus, MSG_DIRECT_ANSWER, MSG_HIGH_RISK_REASON
        bus_data = state.get("message_bus", [])
        bus = MessageBus.from_dict(bus_data) if bus_data else MessageBus()
        direct_answer = state["shared_memory"]["router_direct_answer"]
        if direct_answer:
            bus.publish("router", "finalize", MSG_DIRECT_ANSWER, {"value": direct_answer}, job_id=state.get("job_id", ""))
        high_risk_reason = state["shared_memory"]["router_high_risk_reason"]
        if high_risk_reason:
            bus.publish("router", "executor", MSG_HIGH_RISK_REASON, {"value": high_risk_reason}, job_id=state.get("job_id", ""))
        state["message_bus"] = bus.to_dict()
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
