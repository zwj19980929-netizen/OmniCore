"""
OmniCore LangGraph DAG 编排
将所有 Agent 串联成完整的执行图
支持 Worker 失败后反思重规划
"""
from typing import Literal
from langgraph.graph import StateGraph, END

from core.state import OmniCoreState, create_initial_state, ensure_task_defaults
from core.router import RouterAgent
from agents.web_worker import WebWorker
from agents.file_worker import FileWorker
from agents.system_worker import SystemWorker
from agents.critic import CriticAgent
from agents.validator import Validator
from agents.browser_agent import BrowserAgent
from core.llm import LLMClient
from core.capability_detector import CapabilityDetector
from core.model_registry import get_registry, ModelCapability
from utils.logger import log_agent_action, log_success, log_error, log_warning
from utils.human_confirm import HumanConfirm
from config.settings import settings

# 能力检测器实例
_capability_detector = CapabilityDetector()


# 初始化所有 Agent
router_agent = RouterAgent()
web_worker = WebWorker()
file_worker = FileWorker()
system_worker = SystemWorker()
critic_agent = CriticAgent()
validator_agent = Validator()
browser_agent = BrowserAgent()

MAX_REPLAN = 2  # 最多重规划 2 次


def _resolve_model_for_task(task: dict) -> str:
    """
    根据任务的 required_capabilities 选择最合适的模型

    Returns:
        模型全名（如 "gemini/gemini-2.5-pro"）或 None（使用默认）
    """
    try:
        registry = get_registry()

        # 1. 优先使用任务声明的能力
        required_caps = task.get("required_capabilities", [])

        # 2. 如果没有声明，自动检测
        if not required_caps:
            detected = _capability_detector.detect(
                task.get("description", ""),
                task.get("params"),
            )
            required_caps = [c.value for c in detected]

        # 3. 选择主要能力
        cap_set = set()
        for c in required_caps:
            try:
                cap_set.add(ModelCapability(c))
            except ValueError:
                pass

        if not cap_set:
            return None

        primary = _capability_detector.get_primary_capability(cap_set)

        # 4. 获取最合适的模型
        model = registry.get_model_for_capability(primary)
        if model:
            log_agent_action("ModelRouter", f"任务 [{task.get('task_id')}] 能力 {primary.value} → 模型 {model}")
        return model

    except Exception as e:
        log_warning(f"模型自动选择失败: {e}，将使用默认模型")
        return None


def route_node(state: OmniCoreState) -> OmniCoreState:
    """路由节点：分析意图并拆解任务"""
    return router_agent.route(state)


def web_worker_node(state: OmniCoreState) -> OmniCoreState:
    """Web Worker 节点"""
    return web_worker.process(state)


def file_worker_node(state: OmniCoreState) -> OmniCoreState:
    """File Worker 节点"""
    return file_worker.process(state)


def system_worker_node(state: OmniCoreState) -> OmniCoreState:
    """System Worker 节点"""
    return system_worker.process(state)


def browser_agent_node(state: OmniCoreState) -> OmniCoreState:
    """Browser Agent 节点 - 处理浏览器交互任务（PAOD trace 增强）"""
    import asyncio
    from agents.paod import classify_failure, make_trace_step

    for idx, task in enumerate(state["task_queue"]):
        if task["task_type"] == "browser_agent" and task["status"] == "pending":
            state["task_queue"][idx]["status"] = "running"
            log_agent_action("BrowserAgent", "开始执行交互任务", task["description"][:50])

            params = task["params"]
            task_desc = params.get("task", task["description"])
            start_url = params.get("start_url", "")
            headless = params.get("headless", False)

            async def _run_browser():
                agent = BrowserAgent(headless=headless)
                try:
                    return await agent.run(task_desc, start_url)
                finally:
                    await agent.close()

            try:
                result = asyncio.run(_run_browser())

                # 将 BrowserAgent 返回的 steps 转换为 execution_trace
                trace = []
                for i, step in enumerate(result.get("steps", []), 1):
                    trace.append(make_trace_step(
                        step_no=i,
                        plan=step.get("plan", step.get("action_type", "")),
                        action=step.get("action", step.get("selector", "")),
                        observation=step.get("observation", step.get("result", "")),
                        decision=step.get("decision", "continue"),
                    ))
                state["task_queue"][idx]["execution_trace"] = trace

                state["task_queue"][idx]["status"] = "completed" if result.get("success") else "failed"
                state["task_queue"][idx]["result"] = result
                state["shared_memory"][task["task_id"]] = result

                if not result.get("success"):
                    state["task_queue"][idx]["failure_type"] = classify_failure(
                        result.get("message", result.get("error", ""))
                    )
                    state["error_trace"] = result.get("message", "浏览器任务失败")
            except Exception as e:
                log_error(f"Browser Agent 执行失败: {e}")
                state["task_queue"][idx]["status"] = "failed"
                state["task_queue"][idx]["result"] = {"success": False, "error": str(e)}
                state["task_queue"][idx]["failure_type"] = classify_failure(str(e))
                state["task_queue"][idx]["execution_trace"] = [
                    make_trace_step(1, "run browser_agent", task_desc[:80], str(e), "exception")
                ]
                state["error_trace"] = str(e)

    return state


def replanner_node(state: OmniCoreState) -> OmniCoreState:
    """反思重规划节点：分析失败原因，制定新策略"""
    state["replan_count"] = state.get("replan_count", 0) + 1
    log_agent_action("Replanner", f"开始反思重规划（第 {state['replan_count']} 次）")

    # 收集失败信息
    failures = []
    for task in state["task_queue"]:
        if task["status"] == "failed":
            error = task.get("result", {}).get("error", "未知错误") if isinstance(task.get("result"), dict) else "未知错误"
            failures.append(f"- 任务: {task['description']}\n  失败原因: {error}")

    failure_summary = "\n".join(failures) if failures else "无明确失败信息，但任务结果不符合预期"

    # 让 LLM 分析失败原因并重新规划
    llm = LLMClient()
    response = llm.chat_with_system(
        system_prompt="""你是 OmniCore 的重规划专家。之前的任务执行失败了，你需要分析原因并制定新的执行策略。

你要像一个有经验的人一样思考：
1. 为什么失败了？是 URL 不对？页面结构变了？被反爬了？数据不在这个页面？
2. 新的策略是什么？换个 URL？换个方法？用搜索引擎找？用 browser_agent 代替 web_worker？
3. 给出具体可执行的新任务列表

返回 JSON：
```json
{
    "analysis": "失败原因分析",
    "new_strategy": "新策略描述",
    "tasks": [
        {
            "task_id": "replan_task_1",
            "task_type": "worker类型",
            "description": "新任务描述",
            "params": {},
            "priority": 10,
            "depends_on": []
        }
    ]
}
```

可用的 worker 类型：web_worker, browser_agent, file_worker, system_worker
""",
        user_message=f"用户原始需求：{state['user_input']}\n\n失败的任务：\n{failure_summary}\n\n请分析原因并重新规划。",
        temperature=0.3,
        json_mode=True,
    )

    try:
        result = llm.parse_json_response(response)
        log_agent_action("Replanner", f"分析: {result.get('analysis', '')[:80]}")
        log_agent_action("Replanner", f"新策略: {result.get('new_strategy', '')[:80]}")

        # 用新任务替换失败的任务
        import uuid
        new_tasks = []
        for task_data in result.get("tasks", []):
            from core.state import TaskItem
            t = TaskItem(
                task_id=task_data.get("task_id", f"replan_{uuid.uuid4().hex[:8]}"),
                task_type=task_data.get("task_type", "web_worker"),
                description=task_data.get("description", ""),
                params=task_data.get("params", {}),
                status="pending",
                result=None,
                priority=task_data.get("priority", 10),
            )
            ensure_task_defaults(t)
            new_tasks.append(t)

        if new_tasks:
            # 保留已完成的任务，替换失败的
            completed = [t for t in state["task_queue"] if t["status"] == "completed"]
            state["task_queue"] = completed + new_tasks
            state["error_trace"] = ""
            log_success(f"重规划完成，新增 {len(new_tasks)} 个任务")
        else:
            log_warning("重规划未产生新任务")

    except Exception as e:
        log_error(f"重规划失败: {e}")

    from langchain_core.messages import SystemMessage
    state["messages"].append(
        SystemMessage(content=f"Replanner 重规划完成（第 {state['replan_count']} 次）")
    )

    return state


def critic_node(state: OmniCoreState) -> OmniCoreState:
    """Critic 审查节点"""
    return critic_agent.review(state)


def validator_node(state: OmniCoreState) -> OmniCoreState:
    """Validator 硬规则验证节点"""
    return validator_agent.validate(state)


def human_confirm_node(state: OmniCoreState) -> OmniCoreState:
    """人类确认节点"""
    if state["needs_human_confirm"] and not state["human_approved"]:
        confirmed = HumanConfirm.request_confirmation(
            operation="执行任务队列",
            details=f"即将执行 {len(state['task_queue'])} 个任务",
            affected_items=[t["description"] for t in state["task_queue"]],
        )
        state["human_approved"] = confirmed
        if not confirmed:
            state["execution_status"] = "cancelled"
            state["error_trace"] = "用户取消执行"
    else:
        state["human_approved"] = True
    return state


def finalize_node(state: OmniCoreState) -> OmniCoreState:
    """最终输出节点"""
    if not state["task_queue"]:
        router_reasoning = ""
        for msg in reversed(state.get("messages", [])):
            content = getattr(msg, "content", "")
            if "Router 分析完成" in content:
                router_reasoning = content.replace("Router 分析完成: ", "")
                break
        try:
            llm = LLMClient()
            response = llm.chat_with_system(
                system_prompt="你是 OmniCore 智能助手。请根据分析结果，用简洁友好的语言直接回答用户的问题。不要提及内部系统、Router、Worker 等技术细节。",
                user_message=f"用户问题：{state['user_input']}\n\n分析结果：{router_reasoning}",
                temperature=0.7,
            )
            state["final_output"] = response.content
        except:
            state["final_output"] = router_reasoning or "抱歉，我没有理解你的意思，请再说一次。"
        state["execution_status"] = "completed"
        state["critic_approved"] = True
        return state

    results = []
    for task in state["task_queue"]:
        if task["status"] == "completed":
            results.append(f"✅ {task['description']}")
        elif task["status"] == "failed":
            results.append(f"❌ {task['description']}: {task.get('result', {}).get('error', '未知错误')}")

    state["final_output"] = "\n".join(results)

    if state["critic_approved"]:
        state["execution_status"] = "completed"
        log_success("所有任务执行完成")
    else:
        state["execution_status"] = "completed_with_issues"
        log_error(f"任务未通过审查: {state['critic_feedback']}")

    return state


# === 辅助函数 ===

def _is_task_ready(task: dict, task_queue: list) -> bool:
    """检查任务的 depends_on 是否全部完成（为 Phase 2 并发做准备）"""
    depends = task.get("depends_on", [])
    if not depends:
        return True
    completed_ids = {t["task_id"] for t in task_queue if t["status"] == "completed"}
    return all(dep in completed_ids for dep in depends)


# === 条件路由函数 ===

def should_continue_after_route(state: OmniCoreState) -> Literal["human_confirm", "finalize"]:
    if not state["task_queue"]:
        return "finalize"
    return "human_confirm"


def get_next_worker(state: OmniCoreState) -> Literal["web_worker", "file_worker", "system_worker", "browser_agent", "validator"]:
    for task in state["task_queue"]:
        if task["status"] == "pending" and _is_task_ready(task, state["task_queue"]):
            task_type = task["task_type"]
            if task_type in ["web_worker", "file_worker", "system_worker", "browser_agent"]:
                return task_type
    return "validator"


def after_validator(state: OmniCoreState) -> Literal["critic", "replanner", "finalize"]:
    """Validator 之后：passed → critic，failed + replan_count < MAX → replanner，否则 finalize"""
    if state.get("validator_passed", True):
        return "critic"
    if state.get("replan_count", 0) < MAX_REPLAN:
        return "replanner"
    return "finalize"


def should_retry_or_finish(state: OmniCoreState) -> Literal["finalize", "replanner"]:
    """Critic 审查后决定是否重试"""
    if state["critic_approved"]:
        return "finalize"
    if state.get("replan_count", 0) < MAX_REPLAN:
        return "replanner"
    return "finalize"


def get_first_worker(state: OmniCoreState) -> str:
    if not state["human_approved"]:
        return "end"
    for task in state["task_queue"]:
        if task["status"] == "pending" and _is_task_ready(task, state["task_queue"]):
            task_type = task["task_type"]
            if task_type in ["web_worker", "file_worker", "system_worker", "browser_agent"]:
                return task_type
    return "validator"


def build_graph() -> StateGraph:
    """
    构建 OmniCore 执行图 v0.2
    新流程: Router → human_confirm → Worker(s) → Validator → Critic → finalize
                                                    ↓ fail      ↓ fail
                                                 replanner    replanner
    """

    graph = StateGraph(OmniCoreState)

    # 添加节点（10 个）
    graph.add_node("router", route_node)
    graph.add_node("human_confirm", human_confirm_node)
    graph.add_node("web_worker", web_worker_node)
    graph.add_node("file_worker", file_worker_node)
    graph.add_node("system_worker", system_worker_node)
    graph.add_node("browser_agent", browser_agent_node)
    graph.add_node("validator", validator_node)
    graph.add_node("replanner", replanner_node)
    graph.add_node("critic", critic_node)
    graph.add_node("finalize", finalize_node)

    # 入口
    graph.set_entry_point("router")

    # Router → human_confirm 或 finalize
    graph.add_conditional_edges("router", should_continue_after_route, {
        "human_confirm": "human_confirm",
        "finalize": "finalize",
    })

    # human_confirm → 第一个 worker 或 validator（无 pending 时）或 end
    graph.add_conditional_edges("human_confirm", get_first_worker, {
        "web_worker": "web_worker",
        "file_worker": "file_worker",
        "system_worker": "system_worker",
        "browser_agent": "browser_agent",
        "validator": "validator",
        "end": END,
    })

    # 每个 Worker 执行后 → 下一个 ready worker 或 validator
    worker_edges = {
        "web_worker": "web_worker",
        "file_worker": "file_worker",
        "system_worker": "system_worker",
        "browser_agent": "browser_agent",
        "validator": "validator",
    }

    def after_worker(state: OmniCoreState) -> str:
        """Worker 执行后：还有 ready pending 就继续，否则进 validator"""
        for task in state["task_queue"]:
            if task["status"] == "pending" and _is_task_ready(task, state["task_queue"]):
                t = task["task_type"]
                if t in ["web_worker", "file_worker", "system_worker", "browser_agent"]:
                    return t
        return "validator"

    for worker in ["web_worker", "file_worker", "system_worker", "browser_agent"]:
        graph.add_conditional_edges(worker, after_worker, worker_edges)

    # Validator → critic / replanner / finalize
    graph.add_conditional_edges("validator", after_validator, {
        "critic": "critic",
        "replanner": "replanner",
        "finalize": "finalize",
    })

    # Replanner → 第一个 pending worker 或 validator
    graph.add_conditional_edges("replanner", get_first_worker, {
        "web_worker": "web_worker",
        "file_worker": "file_worker",
        "system_worker": "system_worker",
        "browser_agent": "browser_agent",
        "validator": "validator",
        "end": END,
    })

    # Critic → finalize 或 replanner
    graph.add_conditional_edges("critic", should_retry_or_finish, {
        "finalize": "finalize",
        "replanner": "replanner",
    })

    # 最终节点
    graph.add_edge("finalize", END)

    return graph


def compile_graph():
    """编译并返回可执行的图"""
    graph = build_graph()
    return graph.compile()


# 全局编译好的图实例
omnicore_graph = None


def get_graph():
    """获取编译好的图（单例）"""
    global omnicore_graph
    if omnicore_graph is None:
        omnicore_graph = compile_graph()
    return omnicore_graph
