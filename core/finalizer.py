"""
Finalizer — build the user-facing output from direct answers or executed tasks.

Extracted from core/graph.py (R3 refactor).
"""

from pathlib import Path
from typing import Any, Dict, List

from core.state import OmniCoreState
from core.statuses import BLOCKED, WAITING_FOR_APPROVAL, WAITING_FOR_EVENT
from core.llm import LLMClient
from core.stage_registry import register_stage
from core.graph_utils import (
    bus_get, bus_get_str,
    should_skip_for_resume, save_runtime_checkpoint,
)
from core.message_bus import (
    MSG_DIRECT_ANSWER, MSG_FINAL_INSTRUCTIONS,
    MSG_TIME_CONTEXT, MSG_LOCATION_CONTEXT, MSG_WORK_CONTEXT,
)
from utils.logger import log_agent_action, log_success, log_error, log_warning
from core.prompt_registry import build_single_section_prompt
from utils.prompt_manager import get_prompt
from utils.structured_logger import get_structured_logger, LogContext
from utils.text_repair import normalize_text_value, normalize_payload, payload_preview
from utils.structured_extract import (
    extract_structured_findings,
    build_deterministic_list_answer,
    extract_requested_item_count,
)
from utils.context_hints import build_finalize_time_hint, build_finalize_location_hint
from utils.result_sanitizer import sanitize_browser_data


# ---------------------------------------------------------------------------
# Delivery artifact collection
# ---------------------------------------------------------------------------

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
            if source_key == "data":
                payload = sanitize_browser_data(payload)
            if payload in (None, "", [], {}):
                continue
            preview = payload_preview(payload)
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


# ---------------------------------------------------------------------------
# Delivery package
# ---------------------------------------------------------------------------

def _build_delivery_package(state: OmniCoreState) -> dict:
    tasks = state.get("task_queue", []) or []
    completed = [
        task for task in tasks
        if task.get("status") == "completed" and not task.get("skipped_by_adaptive_reroute")
    ]
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
    work_context = bus_get(state, MSG_WORK_CONTEXT, default={})
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


# ---------------------------------------------------------------------------
# answer_text extraction helper (F3)
# ---------------------------------------------------------------------------

def _extract_answer_text(state: OmniCoreState) -> tuple[str, list[str]]:
    """Return (answer_text, answer_citations) from the first browser task that has them."""
    from config.settings import settings as _s
    if not _s.BROWSER_ANSWER_TEXT_ENABLED or not _s.FINALIZER_ANSWER_FIRST:
        return "", []
    for task in state.get("task_queue", []) or []:
        if task.get("skipped_by_adaptive_reroute"):
            continue
        result = task.get("result")
        if not isinstance(result, dict):
            continue
        answer_text = str(result.get("answer_text") or "").strip()
        if answer_text:
            citations = [str(c) for c in (result.get("answer_citations") or []) if c]
            return answer_text, citations
    return "", []


# ---------------------------------------------------------------------------
# Delivery summary text
# ---------------------------------------------------------------------------

def _build_delivery_summary(state: OmniCoreState) -> str:
    package = _build_delivery_package(state)
    state["artifacts"] = _collect_delivery_artifacts(state)
    state["delivery_package"] = package

    lines = [
        package["headline"],
        f"Review status: {package['review_status']}",
        f"Progress: {package['completed_task_count']}/{package['total_task_count']} task(s) completed.",
    ]

    answer_text, answer_citations = _extract_answer_text(state)
    if answer_text:
        lines.append("")
        lines.append(answer_text)
        if answer_citations:
            lines.append("Sources: " + ", ".join(answer_citations))

    findings_summary = extract_structured_findings(state.get("task_queue", []))
    if findings_summary:
        lines.append("")
        lines.append(findings_summary)

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


# ---------------------------------------------------------------------------
# Answer synthesis helpers
# ---------------------------------------------------------------------------

def _should_keep_delivery_summary_as_final_output(
    state: OmniCoreState,
    package: Dict[str, Any],
) -> bool:
    statuses = {
        str(task.get("status", "") or "")
        for task in state.get("task_queue", []) or []
        if isinstance(task, dict)
    }
    if WAITING_FOR_APPROVAL in statuses or WAITING_FOR_EVENT in statuses or BLOCKED in statuses:
        return True
    return int(package.get("completed_task_count", 0) or 0) <= 0


def _build_execution_evidence_for_answer(state: OmniCoreState) -> str:
    sections: List[str] = []

    findings_summary = extract_structured_findings(state.get("task_queue", []), max_items=8)
    if findings_summary:
        sections.append(findings_summary)

    task_result_lines = []
    for task in state.get("task_queue", []) or []:
        if not isinstance(task, dict):
            continue
        if str(task.get("status", "") or "") != "completed":
            continue
        result = task.get("result")
        if result in (None, "", [], {}):
            continue
        task_result_lines.append(
            f"- {str(task.get('description', '') or task.get('task_id', '') or 'Completed task')}: "
            f"{payload_preview(result, limit=500)}"
        )
        if len(task_result_lines) >= 4:
            break
    if task_result_lines:
        sections.append("Completed task results:\n" + "\n".join(task_result_lines))

    return "\n\n".join(section for section in sections if section).strip()


def _synthesize_user_facing_answer(
    state: OmniCoreState,
    delivery_summary: str,
) -> str:
    package = state.get("delivery_package", {}) or {}
    if not isinstance(package, dict):
        package = {}
    if _should_keep_delivery_summary_as_final_output(state, package):
        return delivery_summary

    deterministic_list_answer = build_deterministic_list_answer(
        task_queue=state.get("task_queue", []),
        critic_approved=bool(state.get("critic_approved", False)),
        user_input=str(state.get("user_input", "") or ""),
        package=package,
    )
    if deterministic_list_answer:
        return deterministic_list_answer

    evidence = _build_execution_evidence_for_answer(state)
    if not evidence:
        return delivery_summary

    current_time_context = bus_get_str(state, MSG_TIME_CONTEXT)
    current_location_context = bus_get_str(state, MSG_LOCATION_CONTEXT)
    final_answer_instructions = bus_get(state, MSG_FINAL_INSTRUCTIONS, default=[])
    if not isinstance(final_answer_instructions, list):
        final_answer_instructions = [final_answer_instructions]
    instruction_lines = [
        f"- {str(item).strip()}"
        for item in final_answer_instructions
        if str(item).strip()
    ]
    deliverables = package.get("deliverables", []) or []
    issues = package.get("issues", []) or []
    completed_tasks = package.get("completed_tasks", []) or []

    deliverable_lines = []
    for item in deliverables[:5]:
        if not isinstance(item, dict):
            continue
        location = str(item.get("location", "") or "").strip()
        label = str(item.get("name", "") or item.get("artifact_type", "") or "deliverable")
        if location:
            deliverable_lines.append(f"- {label}: {location}")
        else:
            deliverable_lines.append(f"- {label}")

    issue_lines = []
    for item in issues[:5]:
        if not isinstance(item, dict):
            continue
        issue_lines.append(
            f"- {str(item.get('description', '') or item.get('task_id', '') or 'Issue')}: "
            f"{str(item.get('error', '') or 'Unknown error')}"
        )

    completed_lines = [
        f"- {str(item or '').strip()}"
        for item in completed_tasks[:5]
        if str(item or "").strip()
    ]

    _FINALIZE_SYSTEM_PROMPT_FALLBACK = (
        "You are OmniCore's user-facing answer synthesizer.\n"
        "Write a direct answer for the user based only on executed evidence.\n"
        "Do not mention internal runtime components such as Router, Worker, Critic, "
        "Validator, task queue, or delivery package.\n"
        "If the evidence is partial, say so explicitly.\n"
        "If files or artifacts were produced, briefly mention where they are.\n"
        "If answer guidance is provided, follow it only when the executed evidence supports it."
    )
    try:
        llm = LLMClient()
        response = llm.chat_with_system(
            system_prompt=build_single_section_prompt(
                "finalize_system",
                get_prompt("finalize_system_static", _FINALIZE_SYSTEM_PROMPT_FALLBACK),
            ),
            user_message=(
                f"Original user request:\n{state.get('user_input', '')}\n\n"
                f"Execution headline:\n{package.get('headline', '')}\n\n"
                f"Evidence:\n{evidence}\n\n"
                f"Answer guidance:\n{chr(10).join(instruction_lines) if instruction_lines else '- None'}\n\n"
                f"Completed work:\n{chr(10).join(completed_lines) if completed_lines else '- None'}\n\n"
                f"Deliverables:\n{chr(10).join(deliverable_lines) if deliverable_lines else '- None'}\n\n"
                f"Open issues:\n{chr(10).join(issue_lines) if issue_lines else '- None'}"
                f"{build_finalize_time_hint(current_time_context)}"
                f"{build_finalize_location_hint(current_location_context)}"
            ),
            temperature=0.4,
        )
        synthesized = normalize_text_value(getattr(response, "content", ""))
        if synthesized:
            return synthesized
    except Exception:
        pass

    return delivery_summary


# ---------------------------------------------------------------------------
# finalize_node
# ---------------------------------------------------------------------------

@register_stage(name="finalize", order=90, required=True, depends_on=("router",))
def finalize_node(state: OmniCoreState) -> OmniCoreState:
    """Build the final user-facing output from direct answers or executed tasks."""
    if should_skip_for_resume(state, "finalize"):
        return state
    sl = get_structured_logger()
    job_id = state.get("job_id", "")
    with LogContext(job_id=job_id, stage="finalize"):
        sl.log_event("stage_start")

    if not state["task_queue"]:
        direct_answer = bus_get_str(state, MSG_DIRECT_ANSWER)
        if direct_answer:
            state["final_output"] = direct_answer
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
            get_structured_logger().log_event("stage_end", detail="finalize")
            save_runtime_checkpoint(state, "finalize", "Finalize completed without task queue")
            return state

        router_reasoning = ""
        for msg in reversed(state.get("messages", [])):
            raw_content = str(getattr(msg, "content", "") or "")
            content = normalize_text_value(raw_content)
            if "Router 分析完成" in content:
                router_reasoning = content.replace("Router 分析完成: ", "")
                break
            if "Router 鍒嗘瀽瀹屾垚" in raw_content:
                router_reasoning = raw_content.replace("Router 鍒嗘瀽瀹屾垚: ", "")
                break

        final_output = "这次没有形成可执行计划，也没有拿到可验证的结果，所以我不能直接给你事实性答案。请重试，或明确要查询的目标。"
        if router_reasoning:
            final_output += f"\n\nSystem note: {router_reasoning}"

        state["final_output"] = final_output
        state["execution_status"] = "completed_with_issues"
        state["critic_approved"] = False
        state["delivery_package"] = {
            "headline": "No verified result was produced.",
            "intent": str(state.get("current_intent", "") or ""),
            "review_status": "needs_attention",
            "completed_task_count": 0,
            "total_task_count": 0,
            "completed_tasks": [],
            "deliverables": [],
            "issues": [
                {
                    "task_id": "",
                    "description": "No executable plan or verifiable result",
                    "error": router_reasoning or "Router did not produce a valid executable answer.",
                }
            ],
            "critic_feedback": router_reasoning or "No verifiable result was produced.",
            "recommended_next_step": "Retry the request, or specify the exact target/source to query.",
        }
        get_structured_logger().log_event("stage_end", detail="finalize")
        save_runtime_checkpoint(state, "finalize", "Finalize completed without task queue")
        return state

    delivery_summary = _build_delivery_summary(state)
    state["final_output"] = _synthesize_user_facing_answer(state, delivery_summary)

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

    # Skill Library: feedback update
    matched_skill_id = str(state.get("matched_skill_id", "") or "").strip()
    if matched_skill_id:
        try:
            from memory.skill_store import SkillStore
            skill_store = SkillStore()
            skill_store.update_feedback(matched_skill_id, success=state.get("critic_approved", False))
        except Exception as exc:
            log_warning(f"Skill feedback update failed (non-blocking): {exc}")

    # Skill Library: extraction
    if state.get("critic_approved", False) and not matched_skill_id:
        try:
            from memory.skill_store import SkillStore
            skill_store = SkillStore()
            skill_id = skill_store.extract_and_save(state)
            if skill_id:
                log_agent_action("SkillLibrary", "Extracted skill", skill_id)
        except Exception as exc:
            log_warning(f"Skill extraction failed (non-blocking): {exc}")

    # Knowledge Base: auto-index
    try:
        from config.settings import settings as _kb_settings
        if _kb_settings.KNOWLEDGE_BASE_ENABLED:
            from memory.knowledge_store import KnowledgeStore
            kb = KnowledgeStore()

            final_output = str(state.get("final_output", "") or "")
            if final_output:
                kb.index_task_result(
                    summary=final_output,
                    user_input=str(state.get("user_input", "") or ""),
                    job_id=str(state.get("job_id", "") or ""),
                    session_id=str(state.get("session_id", "") or ""),
                )

            for task in state.get("task_queue", []):
                result = task.get("result")
                if not isinstance(result, dict):
                    continue
                page_url = str(result.get("page_url", "") or result.get("url", "") or "")
                page_content = str(
                    result.get("page_content", "")
                    or result.get("extracted_text", "")
                    or result.get("content", "")
                    or ""
                )
                if page_url and len(page_content) >= _kb_settings.KNOWLEDGE_MIN_CONTENT_LENGTH:
                    page_title = str(result.get("page_title", "") or result.get("title", "") or "")
                    kb.index_web_page(
                        url=page_url,
                        title=page_title,
                        content=page_content,
                        session_id=str(state.get("session_id", "") or ""),
                        job_id=str(state.get("job_id", "") or ""),
                    )
    except Exception as exc:
        log_warning(f"Knowledge indexing failed (non-blocking): {exc}")

    # C1: record episode trace for cross-session replay (Episodic Replay)
    try:
        from config.settings import settings as _ep_settings
        if _ep_settings.EPISODE_REPLAY_ENABLED:
            from memory.episode_store import get_episode_store
            get_episode_store().record_episode(state)
    except Exception as exc:
        log_warning(f"Episode record failed (non-blocking): {exc}")

    # R7: final session memory extraction (capture end state)
    try:
        from config.settings import settings as _r7_settings
        if _r7_settings.SESSION_MEMORY_ENABLED:
            _sid = state.get("session_id", "")
            if _sid:
                from core.session_memory import SessionMemoryManager
                from core.loop_state import LoopState
                _loop = LoopState.from_dict(state.get("loop_state", {}))
                SessionMemoryManager(_sid).extract(
                    messages=state.get("messages", []),
                    task_queue=state.get("task_queue", []),
                    turn_count=_loop.turn_count,
                )
    except Exception:
        pass  # non-blocking

    # R5: mark plan file as completed
    from core.plan_manager import complete_plan
    complete_plan(state.get("job_id", ""))

    get_structured_logger().log_event("stage_end", detail="finalize")
    save_runtime_checkpoint(state, "finalize", "Finalize completed")
    return state
