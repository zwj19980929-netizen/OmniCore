"""
OmniCore LangGraph DAG 编排
将所有 Agent 串联成完整的执行图
"""
from typing import Literal
from langgraph.graph import StateGraph, END

from core.state import OmniCoreState, create_initial_state
from core.router import RouterAgent
from agents.web_worker import WebWorker
from agents.file_worker import FileWorker
from agents.system_worker import SystemWorker
from agents.critic import CriticAgent
from agents.browser_agent import BrowserAgent
from utils.logger import log_agent_action, log_success, log_error
from utils.human_confirm import HumanConfirm
from config.settings import settings


# 初始化所有 Agent
router_agent = RouterAgent()
web_worker = WebWorker()
file_worker = FileWorker()
system_worker = SystemWorker()
critic_agent = CriticAgent()
browser_agent = BrowserAgent()


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
    """Browser Agent 节点 - 处理浏览器交互任务"""
    import asyncio

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
                state["task_queue"][idx]["status"] = "completed" if result.get("success") else "failed"
                state["task_queue"][idx]["result"] = result
                state["shared_memory"][task["task_id"]] = result

                if not result.get("success"):
                    state["error_trace"] = result.get("message", "浏览器任务失败")
            except Exception as e:
                log_error(f"Browser Agent 执行失败: {e}")
                state["task_queue"][idx]["status"] = "failed"
                state["task_queue"][idx]["result"] = {"success": False, "error": str(e)}
                state["error_trace"] = str(e)

    return state


def critic_node(state: OmniCoreState) -> OmniCoreState:
    """Critic 审查节点"""
    return critic_agent.review(state)


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
    # 如果没有任务（information_query），从 Router 的 reasoning 中提取回答
    if not state["task_queue"]:
        # 从 messages 中提取 Router 的分析结果
        for msg in reversed(state.get("messages", [])):
            content = getattr(msg, "content", "")
            if "Router 分析完成" in content:
                state["final_output"] = content.replace("Router 分析完成: ", "")
                break
        if not state.get("final_output"):
            state["final_output"] = "没有需要执行的任务。"
        state["execution_status"] = "completed"
        state["critic_approved"] = True
        return state

    # 汇总所有任务结果
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


# === 条件路由函数 ===

def should_continue_after_route(state: OmniCoreState) -> Literal["human_confirm", "finalize"]:
    """路由后决定下一步"""
    if not state["task_queue"]:
        return "finalize"
    return "human_confirm"


def should_continue_after_confirm(state: OmniCoreState) -> Literal["execute_workers", "end"]:
    """人类确认后决定下一步"""
    if state["human_approved"]:
        return "execute_workers"
    return "end"


def get_next_worker(state: OmniCoreState) -> Literal["web_worker", "file_worker", "system_worker", "browser_agent", "critic"]:
    """决定下一个要执行的 Worker"""
    for task in state["task_queue"]:
        if task["status"] == "pending":
            task_type = task["task_type"]
            if task_type == "web_worker":
                return "web_worker"
            elif task_type == "file_worker":
                return "file_worker"
            elif task_type == "system_worker":
                return "system_worker"
            elif task_type == "browser_agent":
                return "browser_agent"

    # 所有任务完成，进入审查
    return "critic"


def should_continue_after_worker(state: OmniCoreState) -> Literal["next_worker", "critic"]:
    """Worker 执行后决定下一步"""
    # 检查是否还有待执行的任务
    pending_tasks = [t for t in state["task_queue"] if t["status"] == "pending"]
    if pending_tasks:
        return "next_worker"
    return "critic"


def should_retry_or_finish(state: OmniCoreState) -> Literal["execute_workers", "finalize"]:
    """审查后决定是否重试"""
    if state["critic_approved"]:
        return "finalize"
    # 可以在这里添加重试逻辑
    return "finalize"


def build_graph() -> StateGraph:
    """构建 OmniCore 执行图"""

    # 创建图
    graph = StateGraph(OmniCoreState)

    # 添加节点
    graph.add_node("router", route_node)
    graph.add_node("human_confirm", human_confirm_node)
    graph.add_node("web_worker", web_worker_node)
    graph.add_node("file_worker", file_worker_node)
    graph.add_node("system_worker", system_worker_node)
    graph.add_node("browser_agent", browser_agent_node)
    graph.add_node("critic", critic_node)
    graph.add_node("finalize", finalize_node)

    # 设置入口
    graph.set_entry_point("router")

    # 添加边
    graph.add_conditional_edges(
        "router",
        should_continue_after_route,
        {
            "human_confirm": "human_confirm",
            "finalize": "finalize",
        }
    )

    # 人类确认后，根据第一个任务类型决定去哪个 worker
    def get_first_worker(state: OmniCoreState) -> str:
        if not state["human_approved"]:
            return "end"
        for task in state["task_queue"]:
            if task["status"] == "pending":
                task_type = task["task_type"]
                if task_type in ["web_worker", "file_worker", "system_worker", "browser_agent"]:
                    return task_type
        return "critic"

    graph.add_conditional_edges(
        "human_confirm",
        get_first_worker,
        {
            "web_worker": "web_worker",
            "file_worker": "file_worker",
            "system_worker": "system_worker",
            "browser_agent": "browser_agent",
            "critic": "critic",
            "end": END,
        }
    )

    # Worker 之间的流转 - 使用统一的 get_next_worker 函数
    graph.add_conditional_edges(
        "web_worker",
        get_next_worker,
        {
            "web_worker": "web_worker",
            "file_worker": "file_worker",
            "system_worker": "system_worker",
            "browser_agent": "browser_agent",
            "critic": "critic",
        }
    )

    graph.add_conditional_edges(
        "file_worker",
        get_next_worker,
        {
            "web_worker": "web_worker",
            "file_worker": "file_worker",
            "system_worker": "system_worker",
            "browser_agent": "browser_agent",
            "critic": "critic",
        }
    )

    graph.add_conditional_edges(
        "system_worker",
        get_next_worker,
        {
            "web_worker": "web_worker",
            "file_worker": "file_worker",
            "system_worker": "system_worker",
            "browser_agent": "browser_agent",
            "critic": "critic",
        }
    )

    graph.add_conditional_edges(
        "browser_agent",
        get_next_worker,
        {
            "web_worker": "web_worker",
            "file_worker": "file_worker",
            "system_worker": "system_worker",
            "browser_agent": "browser_agent",
            "critic": "critic",
        }
    )

    # Critic 审查后
    graph.add_conditional_edges(
        "critic",
        should_retry_or_finish,
        {
            "execute_workers": "web_worker",
            "finalize": "finalize",
        }
    )

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
