"""
OmniCore Router Agent
Receives user instructions, detects intent, and decomposes work into DAG tasks.
"""
import json
import re
from pathlib import Path
from typing import List, Dict, Any

from core.state import OmniCoreState, TaskItem
from core.task_planner import build_policy_decision_from_task, build_task_item_from_plan
from core.llm import LLMClient
from core.tool_registry import build_dynamic_tool_prompt_lines, get_builtin_tool_registry
from utils.logger import log_agent_action, logger


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


class RouterAgent:
    """
    主脑路由器 Agent
    负责意图识别和任务拆解
    """

    def __init__(self, llm_client: LLMClient = None):
        self.llm = llm_client or LLMClient()
        self.name = "Router"

    @staticmethod
    def _tokenize_text(text: str) -> set[str]:
        tokens = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9][A-Za-z0-9_.-]{1,}", str(text or ""))
        normalized = set()
        for raw in tokens:
            token = raw.strip("._-").lower()
            if len(token) >= 2:
                normalized.add(token)
        return normalized

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

        response = self.llm.chat_with_system(
            system_prompt=self._build_router_system_prompt(),
            user_message=user_message,
            temperature=0.3,
            max_tokens=16000,
            json_mode=True,
        )

        logger.debug(f"Router LLM 原始响应: {response.content[:300] if response.content else '(空)'}")

        try:
            result = self._normalize_task_plan_shape(
                self.llm.parse_json_response(response)
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
