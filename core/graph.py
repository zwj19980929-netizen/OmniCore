"""
OmniCore LangGraph DAG 编排
将所有 Agent 串联成完整的执行图
支持 Worker 失败后反思重规划
"""
from typing import Literal
from langgraph.graph import StateGraph, END

from core.state import OmniCoreState, ensure_task_defaults
from core.router import RouterAgent
from agents.critic import CriticAgent
from agents.validator import Validator
from core.llm import LLMClient
from core.task_executor import collect_ready_task_indexes, run_ready_batch
from utils.logger import log_agent_action, log_success, log_error, log_warning
from utils.human_confirm import HumanConfirm


# 初始化所有 Agent
router_agent = RouterAgent()
critic_agent = CriticAgent()
validator_agent = Validator()

MAX_REPLAN = 3  # 最多重规划 3 次（给 Replanner 足够空间做策略转换）


def route_node(state: OmniCoreState) -> OmniCoreState:
    """路由节点：分析意图并拆解任务"""
    return router_agent.route(state)


def parallel_executor_node(state: OmniCoreState) -> OmniCoreState:
    """批次执行节点：执行当前批次所有 ready 任务。"""
    if collect_ready_task_indexes(state):
        state["execution_status"] = "executing"
    state = run_ready_batch(state)
    return state


def replanner_node(state: OmniCoreState) -> OmniCoreState:
    """反思重规划节点：分析失败原因，制定新策略"""
    state["replan_count"] = state.get("replan_count", 0) + 1
    log_agent_action("Replanner", f"开始反思重规划（第 {state['replan_count']} 次）")

    # 记录本轮失败策略到历史（防止 Replanner 兜圈子）
    replan_history = state.get("shared_memory", {}).get("_replan_history", [])

    # 收集失败信息（包含已尝试的路径，帮助 Replanner 避免重蹈覆辙）
    failures = []
    tried_urls = []
    current_strategies = []
    for task in state["task_queue"]:
        if task["status"] == "failed":
            result = task.get("result", {}) if isinstance(task.get("result"), dict) else {}
            error = result.get("error", "未知错误")
            url = result.get("url") or task.get("params", {}).get("url") or task.get("params", {}).get("start_url") or ""
            failure_type = task.get("failure_type", "unknown")
            trace_summary = ""
            for step in task.get("execution_trace", [])[-3:]:
                trace_summary += f"\n    step {step.get('step_no')}: {step.get('plan')} → {step.get('observation', '')[:80]}"
            failures.append(
                f"- 任务: {task['description']}\n  访问的URL: {url}\n  失败类型: {failure_type}\n  失败原因: {error}{trace_summary}"
            )
            if url:
                tried_urls.append(url)
            current_strategies.append(f"{task['task_type']}: {task['description'][:80]}")

    # 把本轮策略加入历史
    replan_history.append({
        "round": state["replan_count"],
        "strategies": current_strategies,
        "urls": tried_urls,
    })
    state["shared_memory"]["_replan_history"] = replan_history

    failure_summary = "\n".join(failures) if failures else "无明确失败信息，但任务结果不符合预期"
    if tried_urls:
        failure_summary += f"\n\n已尝试过的URL（不要再访问）：{', '.join(tried_urls)}"

    # 构建历史策略摘要，让 Replanner 知道之前都试过什么
    history_summary = ""
    if len(replan_history) > 1:
        history_summary = "\n\n## 之前已经尝试过的策略（绝对不要重复）：\n"
        for h in replan_history[:-1]:
            history_summary += f"第 {h['round']} 轮：\n"
            for s in h["strategies"]:
                history_summary += f"  - {s}\n"
            if h["urls"]:
                history_summary += f"  访问过的URL: {', '.join(h['urls'])}\n"

    # 让 LLM 分析失败原因并重新规划
    llm = LLMClient()
    response = llm.chat_with_system(
        system_prompt="""你是 OmniCore 的重规划专家。之前的任务执行失败了，你需要分析原因并制定新的执行策略。

## 思考层次（由浅入深）
先判断失败属于哪个层次，再决定对策：

1. 执行层失败（选择器错了、超时、参数不对）→ 可以换参数重试
2. 路径层失败（目标网站需要登录、被反爬封锁、数据根本不在这个页面）→ 必须换一条路
3. 方向层失败（整个思路就不对，比如这类信息根本不适合从网页获取）→ 必须重新审视目标

## 核心原则
- 不要在一条死路上反复尝试。如果一个信息源本身就有访问壁垒，换再多参数也没用，应该换信息源。
- 想想一个真实的人遇到同样的障碍会怎么做——他会打开搜索引擎，找一条能走通的路。
- 新策略必须和之前失败的方案有本质区别，不能只是"换个参数再来一次"。
- 如果不确定该去哪，就先安排一个搜索任务，让 Worker 通过搜索引擎找到可行的信息来源。

返回 JSON：
```json
{
    "analysis": "失败原因分析（属于哪个层次的失败）",
    "failed_approach": "之前走的路为什么本质上走不通",
    "new_strategy": "新策略描述（必须和之前有本质区别）",
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
        user_message=f"用户原始需求：{state['user_input']}\n\n失败的任务：\n{failure_summary}{history_summary}\n\n这是第 {state['replan_count']} 次重规划，请提出和之前所有尝试都不同的新策略。",
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


# === 条件路由函数 ===

def should_continue_after_route(state: OmniCoreState) -> Literal["human_confirm", "finalize"]:
    if not state["task_queue"]:
        return "finalize"
    return "human_confirm"


def get_first_executor(state: OmniCoreState) -> Literal["parallel_executor", "validator", "end"]:
    if not state["human_approved"]:
        return "end"
    if collect_ready_task_indexes(state):
        return "parallel_executor"
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


def after_parallel_executor(state: OmniCoreState) -> Literal["parallel_executor", "validator"]:
    if collect_ready_task_indexes(state):
        return "parallel_executor"
    return "validator"


def build_graph() -> StateGraph:
    """
    构建 OmniCore 执行图 v0.2
    新流程: Router → human_confirm → parallel_executor(batch) → Validator → Critic → finalize
                                                    ↓ fail      ↓ fail
                                                 replanner    replanner
    """

    graph = StateGraph(OmniCoreState)

    # 添加节点（7 个）
    graph.add_node("router", route_node)
    graph.add_node("human_confirm", human_confirm_node)
    graph.add_node("parallel_executor", parallel_executor_node)
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

    # human_confirm → 执行批次 / validator / end
    graph.add_conditional_edges("human_confirm", get_first_executor, {
        "parallel_executor": "parallel_executor",
        "validator": "validator",
        "end": END,
    })

    # 批次执行后：还有 ready 任务就继续下一批，否则进入 validator
    graph.add_conditional_edges("parallel_executor", after_parallel_executor, {
        "parallel_executor": "parallel_executor",
        "validator": "validator",
    })

    # Validator → critic / replanner / finalize
    graph.add_conditional_edges("validator", after_validator, {
        "critic": "critic",
        "replanner": "replanner",
        "finalize": "finalize",
    })

    # Replanner → 执行批次 / validator / end
    graph.add_conditional_edges("replanner", get_first_executor, {
        "parallel_executor": "parallel_executor",
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
