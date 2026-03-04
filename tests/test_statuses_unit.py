from core.statuses import (
    BLOCKED,
    WAITING_FOR_APPROVAL,
    WAITING_FOR_EVENT,
    is_action_required_job_status,
    is_recoverable_job_status,
    is_success_job_status,
    is_waiting_job_status,
    is_worker_active_status,
)


def test_status_helpers_cover_waiting_and_recovery_states():
    assert is_waiting_job_status(WAITING_FOR_APPROVAL)
    assert is_waiting_job_status(WAITING_FOR_EVENT)
    assert is_waiting_job_status(BLOCKED)

    assert is_action_required_job_status(WAITING_FOR_APPROVAL)
    assert is_action_required_job_status(BLOCKED)
    assert not is_action_required_job_status(WAITING_FOR_EVENT)

    assert is_success_job_status("completed")
    assert is_success_job_status("completed_with_issues")
    assert not is_success_job_status("error")

    assert is_recoverable_job_status("error")
    assert is_recoverable_job_status("cancelled")
    assert is_recoverable_job_status("completed_with_issues")
    assert not is_recoverable_job_status("waiting_for_approval")

    assert is_worker_active_status("starting")
    assert is_worker_active_status("running")
    assert not is_worker_active_status("stopped")
