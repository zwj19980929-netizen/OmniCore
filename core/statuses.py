"""
Shared runtime status constants.

This module keeps user-facing job/task state strings in one place so
runtime, persistence, and UI code do not drift over time.
"""
from __future__ import annotations


WAITING_FOR_APPROVAL = "waiting_for_approval"
WAITING_FOR_EVENT = "waiting_for_event"
BLOCKED = "blocked"

WAITING_JOB_STATUSES = frozenset(
    {
        WAITING_FOR_APPROVAL,
        WAITING_FOR_EVENT,
        BLOCKED,
    }
)

ACTION_REQUIRED_JOB_STATUSES = frozenset(
    {
        WAITING_FOR_APPROVAL,
        BLOCKED,
    }
)

SUCCESS_JOB_STATUSES = frozenset(
    {
        "completed",
        "completed_with_issues",
    }
)

RECOVERABLE_JOB_STATUSES = frozenset(
    {
        "error",
        "cancelled",
        "completed_with_issues",
    }
)

WORKER_ACTIVE_STATUSES = frozenset(
    {
        "starting",
        "running",
    }
)


def is_waiting_job_status(status: object) -> bool:
    return str(status or "").strip() in WAITING_JOB_STATUSES


def is_action_required_job_status(status: object) -> bool:
    return str(status or "").strip() in ACTION_REQUIRED_JOB_STATUSES


def is_success_job_status(status: object) -> bool:
    return str(status or "").strip() in SUCCESS_JOB_STATUSES


def is_recoverable_job_status(status: object) -> bool:
    return str(status or "").strip() in RECOVERABLE_JOB_STATUSES


def is_worker_active_status(status: object) -> bool:
    return str(status or "").strip() in WORKER_ACTIVE_STATUSES
