"""
OmniCore LangGraph DAG 编排
将所有 Agent 串联成完整的执行图
支持 Worker 失败后反思重规划
"""
from pathlib import Path
from datetime import datetime
from typing import Literal
from langgraph.graph import StateGraph, END

from core.statuses import BLOCKED, WAITING_FOR_APPROVAL, WAITING_FOR_EVENT
from core.state import OmniCoreState
from core.router import RouterAgent
from core.task_planner import build_policy_decision_from_task, build_task_item_from_plan
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

_CHECKPOINT_STAGE_ORDER = {
    "route": 1,
    "human_confirm": 2,
    "parallel_executor": 3,
    "validator": 4,
    "critic": 5,
    "replanner": 6,
    "finalize": 7,
}


def _save_runtime_checkpoint(state: OmniCoreState, stage: str, note: str = "") -> None:
    session_id = str(state.get("session_id", "") or "").strip()
    job_id = str(state.get("job_id", "") or "").strip()
    if not session_id or not job_id:
        return

    try:
        from utils.runtime_state_store import get_runtime_state_store

        get_runtime_state_store().save_checkpoint(
            session_id=session_id,
            job_id=job_id,
            stage=stage,
            state=state,
            note=note,
        )
    except Exception as exc:
        log_warning(f"Runtime checkpoint persistence failed: {exc}")


def _should_skip_for_resume(state: OmniCoreState, stage: str) -> bool:
    shared_memory = state.get("shared_memory", {})
    if not isinstance(shared_memory, dict):
        return False

    resume_after = str(shared_memory.get("_resume_after_stage", "") or "").strip()
    if not resume_after:
        return False

    target_index = _CHECKPOINT_STAGE_ORDER.get(resume_after)
    current_index = _CHECKPOINT_STAGE_ORDER.get(stage)
    if target_index is None or current_index is None:
        shared_memory.pop("_resume_after_stage", None)
        return False

    if current_index <= target_index:
        return True

    shared_memory.pop("_resume_after_stage", None)
    return False


def route_node(state: OmniCoreState) -> OmniCoreState:
    """路由节点：分析意图并拆解任务"""
    if _should_skip_for_resume(state, "route"):
        return state
    state = router_agent.route(state)
    _save_runtime_checkpoint(state, "route", "Router completed")
    return state


def parallel_executor_node(state: OmniCoreState) -> OmniCoreState:
    """批次执行节点：执行当前批次所有 ready 任务。"""
    if _should_skip_for_resume(state, "parallel_executor"):
        return state
    if collect_ready_task_indexes(state):
        state["execution_status"] = "executing"
    state = run_ready_batch(state)
    _save_runtime_checkpoint(state, "parallel_executor", "Executed ready task batch")
    return state


def replanner_node(state: OmniCoreState) -> OmniCoreState:
    """反思重规划节点：分析失败原因，制定新策略"""
    if _should_skip_for_resume(state, "replanner"):
        return state
    state["replan_count"] = state.get("replan_count", 0) + 1
    log_agent_action("Replanner", f"开始反思重规划（第 {state['replan_count']} 次）")

    # 如果已经是最后一次重规划，标记为"必须给出答案"模式
    is_final_attempt = state["replan_count"] >= MAX_REPLAN

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
- **重要**：不要基于你自己的知识判断"某个产品是否存在"。你的知识可能过时。应该让 Worker 去实际搜索，基于搜索结果判断。
- **保持用户的原始意图**：如果用户要"价格对比"，就应该一直尝试找价格对比，而不是改成找"爆料"或"预测"。只有在确认产品完全不存在时才考虑替代方案。

返回 JSON：
```json
{
    "analysis": "失败原因分析（属于哪个层次的失败）",
    "failed_approach": "之前走的路为什么本质上走不通",
    "new_strategy": "新策略描述（必须和之前有本质区别，但要保持用户的原始意图）",
    "should_give_up": false,
    "give_up_reason": "如果 should_give_up 为 true，说明为什么应该放弃并直接回答用户",
    "direct_answer": "如果 should_give_up 为 true，这里填写给用户的直接回答",
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
        user_message=f"用户原始需求：{state['user_input']}\n\n失败的任务：\n{failure_summary}{history_summary}\n\n这是第 {state['replan_count']} 次重规划（{'最后一次，必须给出明确答案' if is_final_attempt else '请提出和之前所有尝试都不同的新策略'}）。",
        temperature=0.3,
        json_mode=True,
    )

    try:
        result = llm.parse_json_response(response)
        log_agent_action("Replanner", f"分析: {result.get('analysis', '')[:80]}")

        # 检查是否应该放弃并直接回答用户
        if result.get("should_give_up", False):
            log_warning(f"Replanner 决定放弃: {result.get('give_up_reason', '')}")
            state["final_output"] = result.get("direct_answer", "抱歉，无法完成您的请求。")
            state["execution_status"] = "completed"
            state["critic_approved"] = True
            state["task_queue"] = []  # 清空任务队列，直接结束
            return state

        log_agent_action("Replanner", f"新策略: {result.get('new_strategy', '')[:80]}")

        # 用新任务替换失败的任务
        new_tasks = []
        for task_data in result.get("tasks", []):
            new_tasks.append(
                build_task_item_from_plan(
                    task_data,
                    task_id_prefix="replan",
                    default_priority=10,
                )
            )

        if new_tasks:
            # 不保留之前的任务，因为既然触发了 Replanner，说明之前的任务都不满足要求
            # 保留它们只会导致 Critic 重复审查并持续失败
            state["task_queue"] = new_tasks
            state["policy_decisions"] = [
                build_policy_decision_from_task(task)
                for task in new_tasks
            ]
            state["needs_human_confirm"] = any(
                task.get("requires_confirmation", False) for task in new_tasks
            )
            state["human_approved"] = not state["needs_human_confirm"]
            state["error_trace"] = ""
            log_success(f"重规划完成，新增 {len(new_tasks)} 个任务（已清空旧任务）")
        else:
            log_warning("重规划未产生新任务")

    except Exception as e:
        log_error(f"重规划失败: {e}")

    from langchain_core.messages import SystemMessage
    state["messages"].append(
        SystemMessage(content=f"Replanner 重规划完成（第 {state['replan_count']} 次）")
    )
    _save_runtime_checkpoint(state, "replanner", "Replanner completed")

    return state


def critic_node(state: OmniCoreState) -> OmniCoreState:
    """Critic 审查节点"""
    if _should_skip_for_resume(state, "critic"):
        return state
    state = critic_agent.review(state)
    _save_runtime_checkpoint(state, "critic", "Critic review completed")
    return state


def validator_node(state: OmniCoreState) -> OmniCoreState:
    """Validator 硬规则验证节点"""
    if _should_skip_for_resume(state, "validator"):
        return state
    state = validator_agent.validate(state)
    _save_runtime_checkpoint(state, "validator", "Validator completed")
    return state


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


def _sync_policy_decisions_after_confirmation(
    state: OmniCoreState,
    *,
    approved: bool,
) -> None:
    timestamp = datetime.now().isoformat(timespec="seconds")
    decisions = []
    existing = {
        str(item.get("task_id", "") or ""): dict(item)
        for item in state.get("policy_decisions", []) or []
        if isinstance(item, dict) and item.get("task_id")
    }

    for task in state.get("task_queue", []) or []:
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("task_id", "") or "")
        current = existing.get(task_id) or dict(build_policy_decision_from_task(task))
        if bool(current.get("requires_human_confirm", False)):
            current["decision"] = "approved" if approved else "rejected"
            current["approved_by"] = "user"
            current["approved_at"] = timestamp
        decisions.append(current)

    state["policy_decisions"] = decisions


def human_confirm_node_v2(state: OmniCoreState) -> OmniCoreState:
    """Deterministic-policy aware human confirmation node."""
    if _should_skip_for_resume(state, "human_confirm"):
        return state
    user_preferences = state.get("shared_memory", {}).get("user_preferences", {})
    auto_queue_confirmations = bool(
        isinstance(user_preferences, dict) and user_preferences.get("auto_queue_confirmations", False)
    )
    if auto_queue_confirmations and state["needs_human_confirm"] and not state["human_approved"]:
        state["human_approved"] = True
        _sync_policy_decisions_after_confirmation(state, approved=True)
        _save_runtime_checkpoint(state, "human_confirm", "Auto-approved by user preference")
        return state
    if state["needs_human_confirm"] and not state["human_approved"]:
        flagged_tasks = [
            task for task in state["task_queue"] if task.get("requires_confirmation", False)
        ]
        tasks_for_review = flagged_tasks or state["task_queue"]
        affected_items = []
        for task in tasks_for_review:
            reason = str(task.get("policy_reason", "") or "").strip()
            if reason:
                affected_items.append(f"{task['description']} [{reason}]")
            else:
                affected_items.append(task["description"])

        details = f"About to execute {len(state['task_queue'])} task(s)."
        if flagged_tasks:
            details += f" {len(flagged_tasks)} task(s) were flagged by deterministic policy."

        router_risk_reason = str(
            state.get("shared_memory", {}).get("router_high_risk_reason", "") or ""
        ).strip()
        if router_risk_reason:
            details += f" Router risk signal: {router_risk_reason}"

        confirmed = HumanConfirm.request_confirmation(
            operation="Execute planned task queue",
            details=details,
            affected_items=affected_items,
        )
        state["human_approved"] = confirmed
        _sync_policy_decisions_after_confirmation(state, approved=confirmed)
        if not confirmed:
            state["execution_status"] = "cancelled"
            state["error_trace"] = "User cancelled execution"
    else:
        state["human_approved"] = True
    _save_runtime_checkpoint(state, "human_confirm", "Human confirmation handled")
    return state


def _collect_delivery_artifacts(state: OmniCoreState):
    artifacts = []
    seen = set()
    path_keys = ("file_path", "path", "output_path", "download_path", "screenshot_path")

    for artifact in state.get("artifacts", []) or []:
        if not isinstance(artifact, dict):
            continue
        fingerprint = (
            str(artifact.get("path", "") or ""),
            str(artifact.get("name", "") or ""),
            str(artifact.get("artifact_type", "") or ""),
        )
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        artifacts.append(dict(artifact))

    for task in state.get("task_queue", []) or []:
        result = task.get("result")
        if not isinstance(result, dict):
            continue

        for source_key in path_keys:
            raw_path = str(result.get(source_key, "") or "").strip()
            if not raw_path:
                continue
            artifact_type = "file"
            if source_key == "screenshot_path":
                artifact_type = "image"
            elif source_key == "download_path":
                artifact_type = "download"
            fingerprint = (raw_path, source_key, artifact_type)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            artifacts.append(
                {
                    "task_id": task.get("task_id", ""),
                    "task_type": task.get("task_type", ""),
                    "tool_name": task.get("tool_name", ""),
                    "artifact_type": artifact_type,
                    "source_key": source_key,
                    "path": raw_path,
                    "name": Path(raw_path).name or raw_path,
                }
            )

        for source_key in ("data", "items", "content"):
            payload = result.get(source_key)
            if payload in (None, "", [], {}):
                continue
            preview = str(payload).replace("\n", " ")[:220]
            fingerprint = ("inline", source_key, preview)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            artifacts.append(
                {
                    "task_id": task.get("task_id", ""),
                    "task_type": task.get("task_type", ""),
                    "tool_name": task.get("tool_name", ""),
                    "artifact_type": "structured_data",
                    "source_key": source_key,
                    "path": "",
                    "name": f"{task.get('task_id', 'task')}_{source_key}",
                    "preview": preview,
                }
            )

    return artifacts


def _build_delivery_package(state: OmniCoreState) -> dict:
    tasks = state.get("task_queue", []) or []
    completed = [task for task in tasks if task.get("status") == "completed"]
    failed = [task for task in tasks if task.get("status") == "failed"]
    waiting_approval = [task for task in tasks if task.get("status") == WAITING_FOR_APPROVAL]
    waiting_event = [task for task in tasks if task.get("status") == WAITING_FOR_EVENT]
    blocked = [task for task in tasks if task.get("status") == BLOCKED]
    pending = [
        task for task in tasks
        if task.get("status") not in {"completed", "failed", WAITING_FOR_APPROVAL, WAITING_FOR_EVENT, BLOCKED}
    ]
    artifacts = _collect_delivery_artifacts(state)

    deliverables = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        deliverables.append(
            {
                "artifact_type": str(artifact.get("artifact_type", "") or "artifact"),
                "name": str(artifact.get("name", "") or "artifact"),
                "location": str(artifact.get("path", "") or artifact.get("preview", "") or "").strip(),
                "task_id": str(artifact.get("task_id", "") or ""),
                "tool_name": str(artifact.get("tool_name", "") or ""),
            }
        )

    issues = []
    for task in failed:
        result = task.get("result") if isinstance(task.get("result"), dict) else {}
        error_message = result.get("error") or result.get("message") or "Unknown error"
        issues.append(
            {
                "task_id": str(task.get("task_id", "") or ""),
                "description": str(task.get("description", "") or task.get("task_id", "") or "Failed task"),
                "error": str(error_message),
            }
        )
    for task in waiting_approval:
        issues.append(
            {
                "task_id": str(task.get("task_id", "") or ""),
                "description": str(task.get("description", "") or task.get("task_id", "") or "Approval needed"),
                "error": "Waiting for approval",
            }
        )
    for task in waiting_event:
        issues.append(
            {
                "task_id": str(task.get("task_id", "") or ""),
                "description": str(task.get("description", "") or task.get("task_id", "") or "Waiting for event"),
                "error": "Waiting for external event",
            }
        )
    for task in blocked:
        issues.append(
            {
                "task_id": str(task.get("task_id", "") or ""),
                "description": str(task.get("description", "") or task.get("task_id", "") or "Blocked task"),
                "error": "Task is blocked",
            }
        )
    for task in pending:
        issues.append(
            {
                "task_id": str(task.get("task_id", "") or ""),
                "description": str(task.get("description", "") or task.get("task_id", "") or "Pending task"),
                "error": f"Task status is {task.get('status', 'pending')}",
            }
        )

    review_status = "approved" if state.get("critic_approved") else "needs_attention"
    work_context = state.get("shared_memory", {}).get("work_context", {})
    goal = work_context.get("goal", {}) if isinstance(work_context, dict) else {}
    project = work_context.get("project", {}) if isinstance(work_context, dict) else {}
    todo = work_context.get("todo", {}) if isinstance(work_context, dict) else {}
    open_todos = work_context.get("open_todos", []) if isinstance(work_context, dict) else []
    if not tasks:
        headline = "Answered directly without executing worker tasks."
    elif waiting_approval:
        headline = f"{len(waiting_approval)} task(s) are prepared and waiting for approval."
    elif waiting_event:
        headline = f"{len(waiting_event)} task(s) are waiting for an external event."
    elif blocked:
        headline = f"{len(blocked)} task(s) are blocked and require manual intervention."
    elif not issues:
        headline = f"Completed all {len(completed)} planned task(s)."
    else:
        headline = f"Completed {len(completed)} of {len(tasks)} task(s); follow-up review is recommended."

    recommended_next_step = ""
    if waiting_approval:
        recommended_next_step = "Review and approve the waiting action to continue execution."
    elif waiting_event:
        recommended_next_step = "Wait for the watched event or adjust the event source."
    elif blocked:
        recommended_next_step = "Unblock or rerun the blocked task after resolving the issue."
    elif failed:
        recommended_next_step = "Review the failed task(s) or retry from the latest checkpoint."
    elif pending:
        recommended_next_step = "Resume the unfinished task(s) to complete the workflow."
    elif review_status != "approved":
        recommended_next_step = "Review the critic feedback before reusing this result."

    return {
        "headline": headline,
        "intent": str(state.get("current_intent", "") or ""),
        "review_status": review_status,
        "goal": {
            "goal_id": str(goal.get("goal_id", "") or ""),
            "title": str(goal.get("title", "") or ""),
        },
        "project": {
            "project_id": str(project.get("project_id", "") or ""),
            "title": str(project.get("title", "") or ""),
        },
        "todo": {
            "todo_id": str(todo.get("todo_id", "") or ""),
            "title": str(todo.get("title", "") or ""),
            "status": str(todo.get("status", "") or ""),
        },
        "completed_task_count": len(completed),
        "total_task_count": len(tasks),
        "completed_tasks": [
            str(task.get("description", "") or task.get("task_id", "") or "Completed task")
            for task in completed
        ],
        "deliverables": deliverables,
        "issues": issues,
        "critic_feedback": str(state.get("critic_feedback", "") or "").strip(),
        "recommended_next_step": recommended_next_step,
        "open_todos": [
            {
                "todo_id": str(item.get("todo_id", "") or ""),
                "title": str(item.get("title", "") or ""),
                "status": str(item.get("status", "") or ""),
            }
            for item in open_todos[:8]
            if isinstance(item, dict)
        ],
    }


def _build_delivery_summary(state: OmniCoreState) -> str:
    package = _build_delivery_package(state)
    state["artifacts"] = _collect_delivery_artifacts(state)
    state["delivery_package"] = package

    lines = [
        package["headline"],
        f"Review status: {package['review_status']}",
        f"Progress: {package['completed_task_count']}/{package['total_task_count']} task(s) completed.",
    ]

    completed_tasks = package.get("completed_tasks", [])
    if completed_tasks:
        lines.append("")
        lines.append("Completed work:")
        for item in completed_tasks:
            lines.append(f"- {item}")

    deliverables = package.get("deliverables", [])
    if deliverables:
        lines.append("")
        lines.append("Deliverables:")
        for item in deliverables[:8]:
            if item.get("location"):
                lines.append(f"- [{item.get('artifact_type', 'artifact')}] {item.get('name', 'artifact')}: {item.get('location')}")
            else:
                lines.append(f"- [{item.get('artifact_type', 'artifact')}] {item.get('name', 'artifact')}")

    issues = package.get("issues", [])
    if issues:
        lines.append("")
        lines.append("Open issues:")
        for item in issues[:8]:
            lines.append(f"- {item.get('description', 'Issue')}: {item.get('error', 'Unknown error')}")

    critic_feedback = package.get("critic_feedback", "")
    if critic_feedback:
        lines.append("")
        lines.append(f"Review note: {critic_feedback}")

    next_step = package.get("recommended_next_step", "")
    if next_step:
        lines.append("")
        lines.append(f"Recommended next step: {next_step}")

    open_todos = package.get("open_todos", [])
    if open_todos:
        lines.append("")
        lines.append("Pending work:")
        for item in open_todos:
            lines.append(f"- {item.get('title', 'Todo')} [{item.get('status', 'pending')}]")

    return "\n".join(lines)


def finalize_node(state: OmniCoreState) -> OmniCoreState:
    """最终输出节点"""
    if _should_skip_for_resume(state, "finalize"):
        return state
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
        state["delivery_package"] = {
            "headline": "Answered directly without worker execution.",
            "intent": str(state.get("current_intent", "") or ""),
            "review_status": "approved",
            "completed_task_count": 0,
            "total_task_count": 0,
            "completed_tasks": [],
            "deliverables": [],
            "issues": [],
            "critic_feedback": "",
            "recommended_next_step": "",
        }
        _save_runtime_checkpoint(state, "finalize", "Finalize completed without task queue")
        return state

    results = []
    for task in state["task_queue"]:
        if task["status"] == "completed":
            results.append(f"✅ {task['description']}")
        elif task["status"] == "failed":
            results.append(f"❌ {task['description']}: {task.get('result', {}).get('error', '未知错误')}")

    state["final_output"] = _build_delivery_summary(state)

    statuses = {str(task.get("status", "") or "") for task in state["task_queue"]}
    if WAITING_FOR_APPROVAL in statuses:
        state["execution_status"] = WAITING_FOR_APPROVAL
    elif WAITING_FOR_EVENT in statuses:
        state["execution_status"] = WAITING_FOR_EVENT
    elif BLOCKED in statuses:
        state["execution_status"] = BLOCKED
    elif state["critic_approved"]:
        state["execution_status"] = "completed"
        log_success("所有任务执行完成")
    else:
        state["execution_status"] = "completed_with_issues"
        log_error(f"任务未通过审查: {state['critic_feedback']}")

    _save_runtime_checkpoint(state, "finalize", "Finalize completed")
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
    graph.add_node("human_confirm", human_confirm_node_v2)
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
