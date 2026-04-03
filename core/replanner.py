"""
Replanner — failure analysis and re-planning logic.

Extracted from core/graph.py (R3 refactor).
"""

import json
from typing import Any, Dict, List

from core.state import OmniCoreState
from core.llm import LLMClient
from core.task_planner import build_policy_decision_from_task, build_task_item_from_plan
from core.stage_registry import register_stage
from core.graph_utils import (
    get_bus, save_bus, bus_get, bus_get_str,
    should_skip_for_resume, save_runtime_checkpoint,
    mark_confirmation_required_tasks_waiting,
    derive_authoritative_target_url,
    repair_replan_task_params,
    extract_finalize_instructions_from_replan_tasks,
    is_task_preservable_for_replan,
    build_replan_failure_record,
)
from core.message_bus import (
    MSG_REPLAN_HISTORY, MSG_FINAL_INSTRUCTIONS, MSG_SUCCESSFUL_PATHS,
)
from utils.logger import log_agent_action, log_success, log_error, log_warning
from utils.structured_logger import get_structured_logger, LogContext
from core.prompt_registry import build_single_section_prompt
from utils.prompt_manager import get_prompt
from utils.text_repair import normalize_payload

MAX_REPLAN = 3


@register_stage(
    name="replanner", order=35, required=False,
    depends_on=("parallel_executor",),
    skip_condition="state.get('replan_count', 0) >= 3",
)
def replanner_node(state: OmniCoreState) -> OmniCoreState:
    """Reflect on failed execution and produce a better next plan."""
    if should_skip_for_resume(state, "replanner"):
        return state
    # S2: auto-compact if context budget exceeded
    from core.graph_nodes import _maybe_auto_compact
    _maybe_auto_compact(state)
    from utils.context_budget import snip_history
    # R7: inject session memory into history snip
    session_memory = ""
    from config.settings import settings as _r7_settings
    if _r7_settings.SESSION_MEMORY_ENABLED:
        _sid = state.get("session_id", "")
        if _sid:
            from core.session_memory import SessionMemoryManager
            session_memory = SessionMemoryManager(_sid).load()
    state["messages"] = snip_history(state["messages"], session_memory=session_memory)
    sl = get_structured_logger()
    job_id = state.get("job_id", "")
    with LogContext(job_id=job_id, stage="replanner"):
        sl.log_event("stage_start", detail=f"replan_count={state.get('replan_count', 0)}")
        sl.log_replan(reason=f"attempt {state.get('replan_count', 0) + 1}")

    state["replan_count"] = state.get("replan_count", 0) + 1
    log_agent_action("Replanner", f"开始反思重规划（第 {state['replan_count']} 次）")

    is_final_attempt = state["replan_count"] >= MAX_REPLAN
    authoritative_target_url = derive_authoritative_target_url(state)
    replan_history = bus_get(state, MSG_REPLAN_HISTORY, default=[])

    tried_urls: List[str] = []
    current_strategies: List[str] = []
    failure_types: List[str] = []
    error_summaries: List[str] = []
    preserved_tasks: List[Dict[str, Any]] = []
    failed_tasks_structured: List[Dict[str, Any]] = []

    for task in state.get("task_queue", []) or []:
        if is_task_preservable_for_replan(task):
            preserved_tasks.append(task)
            continue

        status = str(task.get("status", "") or "")
        critic_rejected = status == "completed" and not bool(task.get("critic_approved", False))
        if status not in {"failed", "completed"} or (status == "completed" and not critic_rejected):
            continue

        failure_record = build_replan_failure_record(task)

        # Determine failure layer
        ft = failure_record["failure_type"]
        failure_layer = "execution"
        if ft == "critic_rejected":
            failure_layer = "result"
        elif ft in ("blocked_or_captcha", "navigation_error"):
            failure_layer = "path"

        last_steps = []
        for step in task.get("execution_trace", [])[-3:]:
            last_steps.append({
                "step_no": step.get("step_no"),
                "plan": step.get("plan", ""),
                "observation": str(step.get("observation", ""))[:120],
            })

        failed_tasks_structured.append({
            "description": task.get("description", ""),
            "worker_type": task.get("task_type", ""),
            "url": failure_record["url"],
            "failure_type": ft,
            "failure_layer": failure_layer,
            "error": str(failure_record["error"])[:200],
            "last_steps": last_steps,
        })

        tried_url = (
            failure_record["expected_url"]
            or failure_record["visited_url"]
            or failure_record["url"]
        )
        if tried_url:
            tried_urls.append(tried_url)
        current_strategies.append(
            f"{task.get('task_type', '')}: {str(task.get('description', '') or '')[:80]}"
        )
        failure_types.append(ft)
        error_summaries.append(str(failure_record["error"])[:100])

    replan_history.append(
        {
            "round": state["replan_count"],
            "strategies": current_strategies,
            "urls": tried_urls,
            "failure_types": failure_types,
            "failure_layer": failed_tasks_structured[0]["failure_layer"] if failed_tasks_structured else "unknown",
            "error_summaries": error_summaries,
        }
    )
    bus = get_bus(state)
    bus.publish("system", "*", MSG_REPLAN_HISTORY, {"value": replan_history}, job_id=state.get("job_id", ""))

    # Build structured replan context
    structured_context = {
        "user_request": state.get("user_input", ""),
        "attempt_number": state["replan_count"],
        "is_final": is_final_attempt,
        "failed_tasks": failed_tasks_structured,
        "history": replan_history[:-1],
        "authoritative_url": authoritative_target_url,
        "successful_paths": bus_get(state, MSG_SUCCESSFUL_PATHS, default=[]),
    }

    llm = LLMClient()
    replanner_en_prompt = build_single_section_prompt(
        "replanner_system", get_prompt("replanner_system_en"),
    )
    response = llm.chat_with_system(
        system_prompt=replanner_en_prompt,
        user_message=json.dumps(structured_context, ensure_ascii=False, indent=2) + (
            f"\n\nReplan round: {state['replan_count']} "
            f"({'final attempt' if is_final_attempt else 'more retries allowed'})"
        ),
        temperature=0.3,
        json_mode=True,
    )

    try:
        result = normalize_payload(llm.parse_json_response(response))
        repaired_tasks = repair_replan_task_params(
            result.get("tasks", []),
            authoritative_target_url,
        )
        result["tasks"], finalize_instructions = extract_finalize_instructions_from_replan_tasks(
            repaired_tasks
        )
        if finalize_instructions:
            bus.publish("critic", "finalize", MSG_FINAL_INSTRUCTIONS, {"value": finalize_instructions}, job_id=state.get("job_id", ""))
        log_agent_action("Replanner", f"分析: {str(result.get('analysis', '') or '')[:80]}")

        if result.get("should_give_up", False):
            log_warning(f"Replanner 决定放弃: {result.get('give_up_reason', '')}")
            state["final_output"] = result.get(
                "direct_answer",
                "抱歉，当前没有足够的可验证证据来继续完成这个请求。",
            )
            state["execution_status"] = "completed_with_issues"
            state["critic_approved"] = False
            state["task_queue"] = preserved_tasks
            state["policy_decisions"] = [
                build_policy_decision_from_task(task)
                for task in state["task_queue"]
            ]
            return state

        log_agent_action("Replanner", f"新策略: {str(result.get('new_strategy', '') or '')[:80]}")

        new_tasks = [
            build_task_item_from_plan(
                task_data,
                task_id_prefix="replan",
                default_priority=10,
            )
            for task_data in result.get("tasks", [])
        ]

        if new_tasks or preserved_tasks:
            state["task_queue"] = preserved_tasks + new_tasks
            state["policy_decisions"] = [
                build_policy_decision_from_task(task)
                for task in state["task_queue"]
            ]
            state["needs_human_confirm"] = any(
                task.get("requires_confirmation", False)
                for task in state["task_queue"]
            )
            state["human_approved"] = not state["needs_human_confirm"]
            mark_confirmation_required_tasks_waiting(state)
            state["error_trace"] = ""
            log_success(
                f"重规划完成，保留 {len(preserved_tasks)} 个结果，新增 {len(new_tasks)} 个任务"
            )

            # R5: update plan file with replan record
            from core.plan_manager import save_plan
            replan_reason = str(result.get("analysis", "") or result.get("new_strategy", ""))[:200]
            save_plan(
                job_id=state.get("job_id", "unknown"),
                task_queue=state["task_queue"],
                replan_count=state["replan_count"],
                replan_reason=replan_reason or "重规划",
            )
        else:
            log_warning("重规划未生成新任务")

    except Exception as exc:
        log_error(f"重规划失败: {exc}")

    save_bus(state, bus)

    from langchain_core.messages import SystemMessage

    state["messages"].append(
        SystemMessage(content=f"Replanner 重规划完成（第 {state['replan_count']} 次）")
    )
    # S3: emit plan_updated event
    try:
        from core.event_log import emit_event, EventType
        task_queue = state.get("task_queue", [])
        emit_event(
            EventType.PLAN_UPDATED,
            session_id=state.get("session_id", ""),
            job_id=state.get("job_id", ""),
            data={
                "plan_summary": f"Replan #{state.get('replan_count', 0)}: {len(task_queue)} tasks",
                "task_ids": [t.get("task_id", "") for t in task_queue[:20]],
            },
        )
    except Exception:
        pass

    sl = get_structured_logger()
    sl.log_event("stage_end", detail=f"new_tasks={len(state.get('task_queue', []))}")
    save_runtime_checkpoint(state, "replanner", "Replanner completed")
    return state
