"""
Graph utility functions shared by graph nodes, replanner, and finalizer.

Extracted from core/graph.py (R3 refactor).
"""

from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from core.state import OmniCoreState
from core.message_bus import (
    MessageBus, MSG_RESUME_STAGE, MSG_APPROVED_ACTIONS,
)
from core.statuses import BLOCKED, WAITING_FOR_APPROVAL, WAITING_FOR_EVENT
from core.task_executor import collect_ready_task_indexes
from utils.logger import log_agent_action, log_warning
from utils.text_repair import normalize_text_value
from utils.url_utils import sanitize_extracted_url


# ---------------------------------------------------------------------------
# MessageBus helpers
# ---------------------------------------------------------------------------

def get_bus(state: OmniCoreState) -> MessageBus:
    """Get or create MessageBus from state."""
    bus_data = state.get("message_bus", [])
    return MessageBus.from_dict(bus_data) if bus_data else MessageBus()


def save_bus(state: OmniCoreState, bus: MessageBus):
    """Save MessageBus back to state."""
    state["message_bus"] = bus.to_dict()


def bus_get_str(state: OmniCoreState, message_type: str, target: str = None) -> str:
    """Read a string value from the bus."""
    bus = get_bus(state)
    msg = bus.get_latest(message_type, target=target)
    if msg is not None:
        return str(msg.payload.get("value", "") or "").strip()
    return ""


def bus_get(state: OmniCoreState, message_type: str, default=None):
    """Read any value from the bus."""
    bus = get_bus(state)
    msg = bus.get_latest(message_type)
    if msg is not None:
        return msg.payload.get("value", default)
    return default


# ---------------------------------------------------------------------------
# Checkpoint stage ordering
# ---------------------------------------------------------------------------

CHECKPOINT_STAGE_ORDER = {
    "route": 1,
    "human_confirm": 2,
    "parallel_executor": 3,
    "validator": 4,
    "critic": 5,
    "replanner": 6,
    "finalize": 7,
}


# ---------------------------------------------------------------------------
# Runtime checkpoint persistence
# ---------------------------------------------------------------------------

def save_runtime_checkpoint(state: OmniCoreState, stage: str, note: str = "") -> None:
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

    # S3: emit checkpoint event
    try:
        from core.event_log import emit_event, EventType
        emit_event(
            EventType.CHECKPOINT_SAVED,
            session_id=session_id,
            job_id=job_id,
            data={"stage": stage, "note": note},
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Resume / skip logic
# ---------------------------------------------------------------------------

def should_skip_for_resume(state: OmniCoreState, stage: str) -> bool:
    resume_after = bus_get_str(state, MSG_RESUME_STAGE)
    if not resume_after:
        return False

    target_index = CHECKPOINT_STAGE_ORDER.get(resume_after)
    current_index = CHECKPOINT_STAGE_ORDER.get(stage)
    if target_index is None or current_index is None:
        return False

    return current_index <= target_index


# ---------------------------------------------------------------------------
# Task status helpers
# ---------------------------------------------------------------------------

def task_statuses(state: OmniCoreState) -> set:
    return {
        str(task.get("status", "") or "")
        for task in state.get("task_queue", []) or []
        if isinstance(task, dict)
    }


def has_waiting_tasks(state: OmniCoreState) -> bool:
    statuses = task_statuses(state)
    return any(status in statuses for status in (WAITING_FOR_APPROVAL, WAITING_FOR_EVENT, BLOCKED))


def mark_confirmation_required_tasks_waiting(state: OmniCoreState) -> None:
    approved_actions_raw = bus_get(state, MSG_APPROVED_ACTIONS, default=[])
    approved_actions = {
        str(item).strip()
        for item in (approved_actions_raw or [])
        if str(item).strip()
    }

    has_waiting = False
    for task in state.get("task_queue", []) or []:
        if not isinstance(task, dict):
            continue

        task_id = str(task.get("task_id", "") or "")
        status = str(task.get("status", "") or "")
        requires_confirmation = bool(task.get("requires_confirmation", False))
        already_approved = task_id in approved_actions or bool(state.get("human_approved", False))

        if requires_confirmation and not already_approved:
            if status in {"", "pending", "running"}:
                task["status"] = WAITING_FOR_APPROVAL
                status = WAITING_FOR_APPROVAL
            if status == WAITING_FOR_APPROVAL:
                has_waiting = True

    state["needs_human_confirm"] = has_waiting
    if has_waiting and not collect_ready_task_indexes(state):
        state["execution_status"] = WAITING_FOR_APPROVAL


# ---------------------------------------------------------------------------
# Adaptive re-routing (Direction 7)
# ---------------------------------------------------------------------------

def should_skip_remaining_tasks(state: OmniCoreState) -> bool:
    """Lightweight post-batch check: should remaining tasks be skipped?

    Rules (no LLM call needed):
    1. Single-answer task + answer already found -> skip remaining
    2. Accumulated data meets or exceeds the requested item count -> skip remaining
    Failed prerequisites are handled by the critic/replanner path instead of
    being marked as skipped success.
    """
    from utils.structured_extract import extract_requested_item_count

    task_queue = state.get("task_queue") or []
    if not task_queue:
        return False

    completed = [t for t in task_queue if str(t.get("status", "")) == "completed"]
    pending = [t for t in task_queue if str(t.get("status", "")) == "pending"]

    if not pending:
        return False

    # Rule 1: single-answer intent already answered
    intent = str(state.get("current_intent", "") or "").lower()
    single_answer_intents = ("direct_answer", "simple_query", "factual_question")
    if any(tok in intent for tok in single_answer_intents):
        if completed:
            return True

    # Rule 2: requested item count already met
    user_input = str(state.get("user_input", "") or "")
    requested_count = extract_requested_item_count(user_input)
    if requested_count > 0 and completed:
        total_items = 0
        for task in completed:
            result = task.get("result")
            if isinstance(result, dict):
                for key in ("data", "items", "content"):
                    payload = result.get(key)
                    if isinstance(payload, list):
                        total_items += len(payload)
        if total_items >= requested_count:
            return True

    return False


def apply_adaptive_skip(state: OmniCoreState) -> OmniCoreState:
    """Mark remaining pending tasks as skipped when adaptive re-routing triggers."""
    task_queue = state.get("task_queue") or []
    skipped_count = 0
    for task in task_queue:
        if str(task.get("status", "")) == "pending":
            task["status"] = "completed"
            existing = task.get("result")
            if not isinstance(existing, dict):
                task["result"] = {}
            task["result"]["skipped_by_adaptive_reroute"] = True
            task["result"]["success"] = True
            task["skipped_by_adaptive_reroute"] = True  # top-level flag for downstream checks
            skipped_count += 1
    if skipped_count:
        log_agent_action(
            "AdaptiveReroute",
            f"Skipped {skipped_count} remaining task(s) — goal already satisfied",
        )
    return state


# ---------------------------------------------------------------------------
# Authoritative URL derivation (used by replanner)
# ---------------------------------------------------------------------------

def derive_authoritative_target_url(state: OmniCoreState) -> str:
    from core.router import RouterAgent

    def _is_generic_entry_url(value: str) -> bool:
        candidate = str(value or "").strip()
        if not candidate:
            return False
        parsed = urlparse(candidate)
        host = parsed.netloc.lower()
        normalized = candidate.lower()
        if host.startswith("www."):
            host = host[4:]
        if any(token in normalized for token in ("/ok.html", "/captcha", "/verify", "/challenge", "/forbidden", "/blocked")):
            return True
        if host in {"google.com", "bing.com", "baidu.com", "duckduckgo.com", "sogou.com"}:
            return True
        return False

    direct_url = RouterAgent._extract_first_url(str(state.get("user_input", "") or ""))
    if direct_url:
        return direct_url

    for task in state.get("task_queue", []) or []:
        if not isinstance(task, dict):
            continue
        params = task.get("params", {}) if isinstance(task.get("params"), dict) else {}
        result = task.get("result", {}) if isinstance(task.get("result"), dict) else {}
        for candidate in (
            result.get("expected_url"),
            params.get("start_url"),
            params.get("url"),
            result.get("url"),
        ):
            value = sanitize_extracted_url(candidate)
            if value and not _is_generic_entry_url(value):
                return value
    return ""


# ---------------------------------------------------------------------------
# Replan task-param repair helpers (used by replanner)
# ---------------------------------------------------------------------------

def repair_replan_task_params(
    tasks: List[Dict[str, Any]],
    target_url: str,
    user_request: str = "",
    original_headless: Optional[bool] = None,
) -> List[Dict[str, Any]]:
    target_url = sanitize_extracted_url(target_url)
    user_request = str(user_request or "").strip()

    repaired = []
    for raw_task in tasks or []:
        task_data = dict(raw_task)
        params = task_data.get("params")
        if not isinstance(params, dict):
            params = {}

        tool_name = str(task_data.get("tool_name", "") or "").strip()
        task_type = str(task_data.get("task_type", "") or "").strip()
        is_browser_task = tool_name == "browser.interact" or task_type == "browser_agent"

        if is_browser_task:
            params = dict(params)
            if target_url and not str(params.get("start_url", "") or "").strip():
                params["start_url"] = target_url
            # Ensure the browser agent sees the original user goal rather than
            # the replanner's rationale text, so intent inference / search
            # bootstrap can pick up a meaningful query.
            if user_request:
                current_task_param = str(params.get("task", "") or "").strip()
                description = str(task_data.get("description", "") or "").strip()
                if not current_task_param or current_task_param == description:
                    params["task"] = user_request
            # Preserve headed/headless mode from original task if LLM didn't set it.
            if original_headless is not None and "headless" not in params:
                params["headless"] = original_headless
            task_data["params"] = params
        elif (
            tool_name in {"web.fetch_and_extract", "web.smart_extract"}
            and target_url
            and not str(params.get("url", "") or "").strip()
        ):
            params = dict(params)
            params["url"] = target_url
            task_data["params"] = params

        repaired.append(task_data)

    return repaired


# ---------------------------------------------------------------------------
# Finalize instruction extraction from replan tasks
# ---------------------------------------------------------------------------

_SYSTEM_EXECUTION_PARAM_KEYS = (
    "command",
    "application",
    "args",
    "working_directory",
)


def _has_actionable_system_params(params: Any) -> bool:
    if not isinstance(params, dict):
        return False
    for key in _SYSTEM_EXECUTION_PARAM_KEYS:
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, (list, tuple)) and any(str(item).strip() for item in value):
            return True
    return False


def extract_finalize_instructions_from_replan_tasks(
    tasks: List[Dict[str, Any]],
) -> tuple:
    """Split tasks into executable tasks and finalize-only instructions.

    Returns ``(executable_tasks, finalize_instructions)``.
    """
    executable_tasks: List[Dict[str, Any]] = []
    finalize_instructions: List[str] = []

    for raw_task in tasks or []:
        if not isinstance(raw_task, dict):
            continue

        task_data = dict(raw_task)
        tool_name = str(task_data.get("tool_name", "") or "").strip()
        task_type = str(task_data.get("task_type", "") or "").strip()
        params = task_data.get("params")
        if params is None:
            params = task_data.get("tool_args", {})

        is_system_task = tool_name == "system.control" or task_type == "system_worker"
        if is_system_task and not _has_actionable_system_params(params):
            instruction = normalize_text_value(task_data.get("description", ""))
            if instruction:
                finalize_instructions.append(instruction)
            continue

        executable_tasks.append(task_data)

    return executable_tasks, finalize_instructions


# ---------------------------------------------------------------------------
# Replan failure record helpers
# ---------------------------------------------------------------------------

def is_task_preservable_for_replan(task: Dict[str, Any]) -> bool:
    if not isinstance(task, dict):
        return False
    if str(task.get("status", "") or "") != "completed":
        return False
    return bool(task.get("critic_approved", False))


def build_replan_failure_record(task: Dict[str, Any]) -> Dict[str, Any]:
    result = task.get("result", {}) if isinstance(task.get("result"), dict) else {}
    review = task.get("critic_review", {}) if isinstance(task.get("critic_review"), dict) else {}
    params = task.get("params", {}) if isinstance(task.get("params"), dict) else {}

    expected_url = (
        sanitize_extracted_url(result.get("expected_url"))
        or sanitize_extracted_url(params.get("start_url"))
        or sanitize_extracted_url(params.get("url"))
        or ""
    )
    visited_url = result.get("url") or ""
    error = result.get("error") or result.get("message") or ""
    failure_type = str(task.get("failure_type", "") or "unknown")

    if str(task.get("status", "") or "") == "completed" and not bool(task.get("critic_approved", False)):
        failure_type = "critic_rejected"
        review_issues = review.get("issues", []) if isinstance(review.get("issues"), list) else []
        error = error or "; ".join(str(item) for item in review_issues if str(item).strip())
        error = error or str(review.get("summary", "") or "critic rejected the task result")

    if not error:
        error = "unknown error"

    url = expected_url or visited_url or ""
    if expected_url and visited_url and visited_url != expected_url:
        url = f"{visited_url} (expected {expected_url})"

    failure_source = str(task.get("failure_source", "") or "")
    if not failure_source:
        if failure_type == "critic_rejected":
            failure_source = "critic"
        else:
            failure_source = "validator"

    return {
        "url": url,
        "expected_url": str(expected_url or "").strip(),
        "visited_url": str(visited_url or "").strip(),
        "error": str(error),
        "failure_type": failure_type,
        "failure_source": failure_source,
    }
