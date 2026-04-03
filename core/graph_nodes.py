"""
OmniCore graph node functions.

Each function is a LangGraph node: ``(state) -> state``.
Extracted from core/graph.py (R3 refactor).
"""

from datetime import datetime
from typing import Dict

from core.state import OmniCoreState
from core.router import RouterAgent
from core.task_planner import build_policy_decision_from_task
from agents.critic import CriticAgent
from agents.validator import Validator
from core.task_executor import collect_ready_task_indexes, run_ready_batch
from core.stage_registry import register_stage
from core.plan_validator import validate_plan
from core.graph_utils import (
    get_bus, save_bus, bus_get, bus_get_str,
    should_skip_for_resume, save_runtime_checkpoint,
    mark_confirmation_required_tasks_waiting,
)
from core.message_bus import (
    MSG_HIGH_RISK_REASON, MSG_USER_PREFERENCES,
)
from core.coordinator import coordinator_node as _coordinator_node_impl
from utils.logger import log_agent_action, log_warning
from utils.structured_logger import get_structured_logger, LogContext
from utils.human_confirm import HumanConfirm


# Singleton agents
router_agent = RouterAgent()
critic_agent = CriticAgent()
validator_agent = Validator()

# S2: singleton AutoCompactor (shared across nodes for circuit breaker state)
_auto_compactor = None


def _get_compactor():
    """Lazy-init singleton AutoCompactor."""
    global _auto_compactor
    if _auto_compactor is None:
        from utils.context_budget import AutoCompactor
        _auto_compactor = AutoCompactor()
    return _auto_compactor


def _maybe_auto_compact(state: OmniCoreState) -> None:
    """
    S2: Check context budget and trigger auto-compact if needed.
    Called at entry of key nodes (route, parallel_executor, replanner).
    """
    from utils.context_budget import ContextBudget, _count_message_tokens

    messages = state.get("messages", [])
    if len(messages) < 10:
        return

    budget = ContextBudget()
    budget.update_usage("history", _count_message_tokens(messages))
    budget.log_report()

    if not budget.should_compact():
        return

    compactor = _get_compactor()
    if compactor.is_tripped:
        return

    log_agent_action("AutoCompact", f"上下文使用率超限，触发压缩 ({len(messages)} msgs)")
    reinject_state = {
        "job_id": state.get("job_id", ""),
        "session_id": state.get("session_id", ""),
        "task_queue": state.get("task_queue", []),
    }
    state["messages"] = compactor.compact(
        messages, budget=budget, reinject_state=reinject_state,
    )


# ---------------------------------------------------------------------------
# route_node
# ---------------------------------------------------------------------------

@register_stage(name="router", order=10, required=True)
def route_node(state: OmniCoreState) -> OmniCoreState:
    """Route user request: analyze intent and decompose into sub-tasks."""
    if should_skip_for_resume(state, "route"):
        return state
    # S2: auto-compact if context budget exceeded
    _maybe_auto_compact(state)
    from utils.context_budget import snip_history
    # R7: inject session memory into history snip
    session_memory = _load_session_memory(state)
    state["messages"] = snip_history(state["messages"], session_memory=session_memory)
    sl = get_structured_logger()
    job_id = state.get("job_id", "")
    with LogContext(job_id=job_id, stage="router"):
        sl.log_event("stage_start")
        state = router_agent.route(state)
        sl.log_event("stage_end", detail=f"tasks={len(state.get('task_queue', []))}")

    # S5: coordinator decision is now set by router LLM (via analysis["needs_coordinator"])
    if state.get("_use_coordinator"):
        log_agent_action("Router", "LLM 判定 → 启用 Coordinator 模式")

    # R5: persist plan to Markdown file
    from core.plan_manager import save_plan
    plan_path = save_plan(
        job_id=job_id,
        task_queue=state.get("task_queue", []),
        user_input=state.get("user_input", ""),
    )
    if plan_path:
        log_agent_action("PlanManager", f"计划已保存: {plan_path}")

    # S3: emit plan_created event
    try:
        from core.event_log import emit_event, EventType
        task_queue = state.get("task_queue", [])
        emit_event(
            EventType.PLAN_CREATED,
            session_id=state.get("session_id", ""),
            job_id=job_id,
            data={
                "plan_summary": f"{len(task_queue)} tasks planned",
                "task_ids": [t.get("task_id", "") for t in task_queue[:20]],
            },
        )
    except Exception:
        pass

    save_runtime_checkpoint(state, "route", "Router completed")
    return state


# ---------------------------------------------------------------------------
# plan_validator_node
# ---------------------------------------------------------------------------

@register_stage(name="plan_validator", order=15, required=False, depends_on=("router",))
def plan_validator_node(state: OmniCoreState) -> OmniCoreState:
    """Plan pre-validation: detect structural issues before execution."""
    if should_skip_for_resume(state, "plan_validator"):
        return state
    task_queue = state.get("task_queue", [])
    if not task_queue:
        return state

    sl = get_structured_logger()
    job_id = state.get("job_id", "")
    with LogContext(job_id=job_id, stage="plan_validator"):
        sl.log_event("stage_start")
        result = validate_plan(task_queue)
        if result.auto_fixes:
            log_agent_action("PlanValidator", f"自动修复 {len(result.auto_fixes)} 项问题")
        if not result.passed:
            log_warning(f"PlanValidator 预检未通过: {result.issues}")
            state["error_trace"] = f"Plan validation failed: {'; '.join(result.issues)}"
        sl.log_event("stage_end", detail=f"passed={result.passed}, fixes={len(result.auto_fixes)}")
    save_runtime_checkpoint(state, "plan_validator", "Plan pre-validation completed")
    return state


# ---------------------------------------------------------------------------
# parallel_executor_node
# ---------------------------------------------------------------------------

@register_stage(name="parallel_executor", order=30, required=True, depends_on=("router",))
def parallel_executor_node(state: OmniCoreState) -> OmniCoreState:
    """Batch executor: run all ready tasks in the current batch."""
    if should_skip_for_resume(state, "parallel_executor"):
        return state

    # S2: auto-compact if context budget exceeded
    _maybe_auto_compact(state)

    # R7: trigger session memory extraction if due
    _maybe_extract_session_memory(state)

    # R5: inject plan reminder before execution
    from core.plan_reminder import generate_reminder
    reminder = generate_reminder(state)
    if reminder:
        from langchain_core.messages import SystemMessage
        state["messages"].append(SystemMessage(content=reminder))

    sl = get_structured_logger()
    job_id = state.get("job_id", "")
    with LogContext(job_id=job_id, stage="parallel_executor"):
        sl.log_event("stage_start")
        if collect_ready_task_indexes(state):
            state["execution_status"] = "executing"
        state = run_ready_batch(state)
        completed = len([t for t in state.get("task_queue", []) if t.get("status") == "completed"])
        sl.log_event("stage_end", detail=f"completed={completed}")

    # S3: emit task events for this batch
    try:
        from core.event_log import emit_event, EventType
        session_id = state.get("session_id", "")
        for task in state.get("task_queue", []):
            status = task.get("status", "")
            tid = task.get("task_id", "")
            if status == "completed":
                emit_event(
                    EventType.TASK_COMPLETED,
                    session_id=session_id, job_id=job_id,
                    data={
                        "task_id": tid,
                        "task_type": task.get("task_type", ""),
                        "tool_name": task.get("tool_name", ""),
                        "result_preview": str(task.get("result", ""))[:300],
                    },
                )
            elif status == "failed":
                emit_event(
                    EventType.TASK_FAILED,
                    session_id=session_id, job_id=job_id,
                    data={
                        "task_id": tid,
                        "task_type": task.get("task_type", ""),
                        "error": str(task.get("result", ""))[:500],
                        "failure_type": task.get("failure_type", ""),
                    },
                )
            elif status == "running":
                emit_event(
                    EventType.TASK_STARTED,
                    session_id=session_id, job_id=job_id,
                    data={
                        "task_id": tid,
                        "task_type": task.get("task_type", ""),
                        "description": task.get("description", "")[:200],
                        "tool_name": task.get("tool_name", ""),
                    },
                )
    except Exception:
        pass

    save_runtime_checkpoint(state, "parallel_executor", "Executed ready task batch")
    return state


# ---------------------------------------------------------------------------
# dynamic_replan_node
# ---------------------------------------------------------------------------

@register_stage(
    name="dynamic_replan", order=32, required=False,
    depends_on=("parallel_executor",),
)
def dynamic_replan_node(state: OmniCoreState) -> OmniCoreState:
    """Check completed task outputs and merge pending dynamic_task_additions."""
    from core.constants import MAX_DYNAMIC_TASK_ADDITIONS
    from core.state import ensure_task_defaults

    sl = get_structured_logger()
    job_id = state.get("job_id", "")

    additions = state.get("dynamic_task_additions") or []
    if not additions:
        return state

    with LogContext(job_id=job_id, stage="dynamic_replan"):
        sl.log_event("stage_start", detail=f"pending_additions={len(additions)}")

        existing_ids = {t["task_id"] for t in state["task_queue"] if t.get("task_id")}
        inserted = 0

        for new_task in additions:
            if inserted >= MAX_DYNAMIC_TASK_ADDITIONS:
                log_agent_action(
                    "DynamicReplan",
                    "达到动态任务插入上限",
                    f"max={MAX_DYNAMIC_TASK_ADDITIONS}, dropped={len(additions) - inserted}",
                )
                break

            task_id = new_task.get("task_id", "")
            if not task_id or task_id in existing_ids:
                continue

            deps = set(new_task.get("depends_on") or [])
            if task_id in deps:
                continue

            ensure_task_defaults(new_task)
            if not new_task.get("status"):
                new_task["status"] = "pending"
            state["task_queue"].append(new_task)
            existing_ids.add(task_id)
            inserted += 1

        state["dynamic_task_additions"] = []

        if inserted > 0:
            log_agent_action("DynamicReplan", f"动态插入 {inserted} 个新任务", "")
        sl.log_event("stage_end", detail=f"inserted={inserted}")

    return state


# ---------------------------------------------------------------------------
# critic_node
# ---------------------------------------------------------------------------

@register_stage(
    name="critic", order=50, required=False,
    depends_on=("validator",),
    skip_condition="state.get('validator_passed') == False",
)
def critic_node(state: OmniCoreState) -> OmniCoreState:
    """Critic review node: evaluate task output quality."""
    if should_skip_for_resume(state, "critic"):
        return state
    sl = get_structured_logger()
    job_id = state.get("job_id", "")
    with LogContext(job_id=job_id, stage="critic"):
        sl.log_event("stage_start")
        state = critic_agent.review(state)
        sl.log_event("stage_end", detail=f"approved={state.get('critic_approved', False)}")
    save_runtime_checkpoint(state, "critic", "Critic review completed")
    return state


# ---------------------------------------------------------------------------
# validator_node
# ---------------------------------------------------------------------------

@register_stage(name="validator", order=40, required=False, depends_on=("parallel_executor",))
def validator_node(state: OmniCoreState) -> OmniCoreState:
    """Hard-rule validation node."""
    if should_skip_for_resume(state, "validator"):
        return state
    sl = get_structured_logger()
    job_id = state.get("job_id", "")
    with LogContext(job_id=job_id, stage="validator"):
        sl.log_event("stage_start")
        state = validator_agent.validate(state)
        sl.log_event("stage_end", detail=f"passed={state.get('validator_passed', False)}")
    save_runtime_checkpoint(state, "validator", "Validator completed")
    return state


# ---------------------------------------------------------------------------
# human_confirm_node (legacy)
# ---------------------------------------------------------------------------

def human_confirm_node(state: OmniCoreState) -> OmniCoreState:
    """Legacy human confirmation node."""
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
            state["final_output"] = "操作已取消，任务队列未执行。"
    else:
        state["human_approved"] = True
    return state


# ---------------------------------------------------------------------------
# Policy-decision sync helper
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# human_confirm_node_v2 (deterministic-policy aware)
# ---------------------------------------------------------------------------

@register_stage(
    name="human_confirm", order=20, required=False,
    depends_on=("router",),
    skip_condition="not state.get('needs_human_confirm')",
)
def human_confirm_node_v2(state: OmniCoreState) -> OmniCoreState:
    """Deterministic-policy aware human confirmation node."""
    if should_skip_for_resume(state, "human_confirm"):
        return state
    sl = get_structured_logger()
    sl.log_event("stage_start", detail="human_confirm")
    user_preferences = bus_get(state, MSG_USER_PREFERENCES, default={})
    auto_queue_confirmations = bool(
        isinstance(user_preferences, dict) and user_preferences.get("auto_queue_confirmations", False)
    )
    if auto_queue_confirmations and state["needs_human_confirm"] and not state["human_approved"]:
        state["human_approved"] = True
        _sync_policy_decisions_after_confirmation(state, approved=True)
        save_runtime_checkpoint(state, "human_confirm", "Auto-approved by user preference")
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

        router_risk_reason = bus_get_str(state, MSG_HIGH_RISK_REASON)
        if router_risk_reason:
            details += f" Router risk signal: {router_risk_reason}"

        # S3: emit approval_requested event
        try:
            from core.event_log import emit_event, EventType
            emit_event(
                EventType.APPROVAL_REQUESTED,
                session_id=state.get("session_id", ""),
                job_id=state.get("job_id", ""),
                data={"reason": details[:500]},
            )
        except Exception:
            pass

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
            state["final_output"] = "Execution cancelled before running the queued tasks."
    else:
        state["human_approved"] = True
    # S3: emit approval events
    try:
        from core.event_log import emit_event, EventType
        session_id = state.get("session_id", "")
        job_id = state.get("job_id", "")
        approved = state.get("human_approved", False)
        emit_event(
            EventType.APPROVAL_RESOLVED,
            session_id=session_id, job_id=job_id,
            data={"decision": "approved" if approved else "rejected"},
        )
    except Exception:
        pass

    sl.log_event("stage_end", detail=f"approved={state.get('human_approved', False)}")
    save_runtime_checkpoint(state, "human_confirm", "Human confirmation handled")
    return state


# ---------------------------------------------------------------------------
# coordinator_node (S5)
# ---------------------------------------------------------------------------

@register_stage(
    name="coordinator", order=12, required=False,
    depends_on=("router",),
    skip_condition="not state.get('_use_coordinator')",
)
def coordinator_node(state: OmniCoreState) -> OmniCoreState:
    """S5: Coordinator node — decompose, dispatch subagents, synthesize."""
    if should_skip_for_resume(state, "coordinator"):
        return state
    from core.coordinator import is_coordinator_enabled
    if not is_coordinator_enabled():
        log_warning("Coordinator: COORDINATOR_ENABLED=false，跳过 coordinator 降级为普通流程")
        state["_use_coordinator"] = False
        return state
    sl = get_structured_logger()
    job_id = state.get("job_id", "")
    with LogContext(job_id=job_id, stage="coordinator"):
        sl.log_event("stage_start")
        state = _coordinator_node_impl(state)
        sl.log_event("stage_end", detail=f"status={state.get('execution_status', '')}")
    save_runtime_checkpoint(state, "coordinator", "Coordinator completed")
    return state


# ---------------------------------------------------------------------------
# R7: Session Memory helpers
# ---------------------------------------------------------------------------

def _load_session_memory(state: OmniCoreState) -> str:
    """Load session memory for the current session (returns empty string if disabled/absent)."""
    from config.settings import settings
    if not settings.SESSION_MEMORY_ENABLED:
        return ""
    session_id = state.get("session_id", "")
    if not session_id:
        return ""
    from core.session_memory import SessionMemoryManager
    return SessionMemoryManager(session_id).load()


def _maybe_extract_session_memory(state: OmniCoreState) -> None:
    """Trigger session memory extraction if the interval condition is met."""
    from config.settings import settings
    if not settings.SESSION_MEMORY_ENABLED:
        return
    session_id = state.get("session_id", "")
    if not session_id:
        return

    from core.loop_state import LoopState
    loop = LoopState.from_dict(state.get("loop_state", {}))
    from core.session_memory import SessionMemoryManager
    manager = SessionMemoryManager(session_id)
    if not manager.should_extract(loop.turn_count):
        return

    try:
        manager.extract(
            messages=state.get("messages", []),
            task_queue=state.get("task_queue", []),
            turn_count=loop.turn_count,
        )
    except Exception:
        pass  # extraction failure must not block main flow
