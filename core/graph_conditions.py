"""
Conditional routing functions for the OmniCore execution graph.

Each function is used by ``add_conditional_edges`` to determine the next
node after a given stage.  Extracted from core/graph.py (R3 refactor).
"""

from typing import Literal

from core.state import OmniCoreState
from core.task_executor import collect_ready_task_indexes
from core.graph_utils import (
    has_waiting_tasks,
    should_skip_remaining_tasks,
    apply_adaptive_skip,
)

MAX_REPLAN = 3


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

def should_continue_after_route(state: OmniCoreState) -> Literal["coordinator", "plan_validator", "finalize"]:
    # S5: If coordinator mode is flagged, route to coordinator
    if state.get("_use_coordinator"):
        return "coordinator"
    if not state["task_queue"]:
        return "finalize"
    return "plan_validator"


# ---------------------------------------------------------------------------
# Human confirm → first executor
# ---------------------------------------------------------------------------

def get_first_executor(state: OmniCoreState) -> Literal["parallel_executor", "critic", "end"]:
    if str(state.get("execution_status", "") or "") == "cancelled":
        return "end"
    if collect_ready_task_indexes(state):
        return "parallel_executor"
    if has_waiting_tasks(state):
        return "critic"
    if not state["human_approved"]:
        return "end"
    return "critic"


# ---------------------------------------------------------------------------
# Parallel executor
# ---------------------------------------------------------------------------

def after_parallel_executor(state: OmniCoreState) -> Literal["parallel_executor", "dynamic_replan", "critic"]:
    if state.get("dynamic_task_additions"):
        return "dynamic_replan"
    if collect_ready_task_indexes(state):
        return "parallel_executor"
    return "critic"


def after_parallel_executor_adaptive(
    state: OmniCoreState,
) -> Literal["parallel_executor", "dynamic_replan", "critic"]:
    """Post-executor routing with adaptive re-routing check (Direction 7)."""
    if should_skip_remaining_tasks(state):
        apply_adaptive_skip(state)
        return "critic"
    return after_parallel_executor(state)


# ---------------------------------------------------------------------------
# Dynamic replan
# ---------------------------------------------------------------------------

def after_dynamic_replan(state: OmniCoreState) -> Literal["parallel_executor", "critic"]:
    if collect_ready_task_indexes(state):
        return "parallel_executor"
    return "critic"


# ---------------------------------------------------------------------------
# Critic
# ---------------------------------------------------------------------------

def should_retry_or_finish(state: OmniCoreState) -> Literal["finalize", "replanner"]:
    """Critic reviewed — retry or finish?"""
    if has_waiting_tasks(state):
        return "finalize"
    if state["critic_approved"]:
        return "finalize"
    if state.get("replan_count", 0) < MAX_REPLAN:
        return "replanner"
    return "finalize"
