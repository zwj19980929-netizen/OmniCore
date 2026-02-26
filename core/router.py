"""
OmniCore 主脑路由器 Agent
负责接收用户指令，识别意图，拆解为子任务 DAG
"""
import json
import uuid
from typing import List, Dict, Any

from core.state import OmniCoreState, TaskItem
from core.llm import LLMClient
from utils.logger import log_agent_action, logger


# Router Agent 的系统提示词
ROUTER_SYSTEM_PROMPT = """你是 OmniCore 的主脑路由器。你是一个聪明的、有独立思考能力的 AI 调度中心。

## 你的核心能力
你能理解用户的自然语言指令，自主分析意图，并将任务智能拆解为可执行的子任务。你不是一个死板的规则引擎，而是一个能推理、能判断、能灵活应变的智能大脑。

## 思考方式
收到用户指令后，请按以下方式思考：
1. 用户到底想要什么？（不要只看字面意思，理解深层意图）
2. 完成这件事需要哪些步骤？（自主规划，不要套模板）
3. 每个步骤应该交给谁来做？（选择最合适的 Worker）
4. 步骤之间有什么依赖关系？（先做什么后做什么）

## 可用的 Worker
- web_worker: 网页数据抓取（打开网页、提取内容，只读操作）
- browser_agent: 智能浏览器代理（需要交互的任务：购物、登录、填表、搜索等）
- file_worker: 本地文件读写（保存数据、读取文件、生成报告）
- system_worker: 系统级操作（执行命令、操作应用程序）

## Worker 选择原则
- 只需要看网页、抓数据 → web_worker
- 需要点击、输入、多步交互 → browser_agent
- 需要读写本地文件 → file_worker
- 需要执行系统命令 → system_worker
- 只是问问题、聊天、查历史 → 不需要 Worker，直接在 reasoning 中回答

## 输出格式（必须是有效的 JSON）
{
    "intent": "你判断的意图类型（自由描述，如 web_scraping / file_operation / information_query 等）",
    "confidence": 0.95,
    "reasoning": "你的完整思考过程",
    "tasks": [
        {
            "task_id": "task_1",
            "task_type": "worker类型",
            "description": "清晰完整的任务描述，Worker 拿到就能执行",
            "params": {},
            "priority": 10,
            "depends_on": []
        }
    ],
    "is_high_risk": false,
    "high_risk_reason": ""
}

## params 参考（不是死规则，根据实际需要灵活填写）

web_worker params:
- url: 目标 URL（如果你知道的话；不确定就留空，Worker 会自己搜索）
- limit: 抓取数量限制

browser_agent params:
- task: 完整的任务描述
- start_url: 起始 URL（可选）
- headless: 是否无头模式

file_worker params:
- action: "write" 或 "read"
- file_path: 文件路径
- data_source: 数据来源的 task_id（用于写入从其他任务获取的数据）
- data_sources: 多个数据来源的 task_id 列表（多源对比场景）
- format: 输出格式（txt/xlsx/csv/markdown/html，根据场景智能选择）

## 关键原则
1. 灵活思考，不要死板套用规则
2. 任务描述要写清楚，让 Worker 拿到就能干活
3. 不确定 URL 时不要瞎猜，让 Worker 自己去搜索
4. 注意区分名称相似但不同的事物（靠你的推理能力判断）
5. 涉及付款、删除、发送等不可逆操作时，标记 is_high_risk
6. 文件格式根据场景智能选择：数据对比用 xlsx，报告用 html，简单文本用 txt
7. 如果用户只是在问问题或聊天，不需要创建任务，直接在 reasoning 中回答

## 对话上下文
如果提供了对话历史，结合历史理解用户意图。用户可能在追问之前的操作结果（如"文件在哪"、"刚才的数据"），这时直接从历史中找答案，用 information_query 意图回答即可。
"""


class RouterAgent:
    """
    主脑路由器 Agent
    负责意图识别和任务拆解
    """

    def __init__(self, llm_client: LLMClient = None):
        self.llm = llm_client or LLMClient()
        self.name = "Router"

    def analyze_intent(self, user_input: str, conversation_history: list = None) -> Dict[str, Any]:
        """
        分析用户意图并拆解任务

        Args:
            user_input: 用户原始输入
            conversation_history: 最近的对话历史

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

        user_message += f"请分析以下用户指令并拆解任务：\n\n{user_input}"

        response = self.llm.chat_with_system(
            system_prompt=ROUTER_SYSTEM_PROMPT,
            user_message=user_message,
            temperature=0.3,
            max_tokens=16000,
            json_mode=True,
        )

        logger.debug(f"Router LLM 原始响应: {response.content[:300] if response.content else '(空)'}")

        try:
            result = self.llm.parse_json_response(response)
            log_agent_action(
                self.name,
                f"意图识别完成: {result.get('intent')}",
                f"置信度: {result.get('confidence', 0):.2f}, 子任务数: {len(result.get('tasks', []))}"
            )
            return result
        except Exception as e:
            logger.error(f"Router 解析失败: {e}")
            return {
                "intent": "unknown",
                "confidence": 0.0,
                "reasoning": f"解析失败: {str(e)}",
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
        analysis = self.analyze_intent(user_input, conversation_history)

        # 构建任务队列
        task_queue: List[TaskItem] = []
        for task_data in analysis.get("tasks", []):
            task_item = TaskItem(
                task_id=task_data.get("task_id", f"task_{uuid.uuid4().hex[:8]}"),
                task_type=task_data.get("task_type", "unknown"),
                description=task_data.get("description", ""),
                params=task_data.get("params", {}),
                status="pending",
                result=None,
                priority=task_data.get("priority", 5),
            )
            task_queue.append(task_item)

        # 按优先级排序（高优先级在前）
        task_queue.sort(key=lambda x: x["priority"], reverse=True)

        # 更新状态
        state["current_intent"] = analysis.get("intent", "unknown")
        state["intent_confidence"] = analysis.get("confidence", 0.0)
        state["task_queue"] = task_queue
        state["needs_human_confirm"] = analysis.get("is_high_risk", False)
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
