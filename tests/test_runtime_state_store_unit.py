import shutil
import uuid
from pathlib import Path

from utils.runtime_state_store import RuntimeStateStore


def _make_test_dir() -> Path:
    target = Path.cwd() / "data" / f"test_runtime_state_{uuid.uuid4().hex[:8]}"
    target.mkdir(parents=True, exist_ok=True)
    return target


def test_runtime_state_store_tracks_session_job_and_artifacts():
    state_dir = _make_test_dir()
    store = RuntimeStateStore(state_dir=state_dir)

    try:
        session = store.get_or_create_session(session_id="session_demo", source="test")
        job = store.submit_job(
            session_id=session["session_id"],
            user_input="write a report",
        )
        running = store.start_job(
            job_id=job["job_id"],
            session_id=session["session_id"],
            user_input="write a report",
        )

        tasks = [
            {
                "task_id": "task_1",
                "task_type": "file_worker",
                "tool_name": "file.read_write",
                "status": "completed",
                "params": {
                    "action": "write",
                    "file_path": "D:/tmp/report.txt",
                },
                "result": {
                    "success": True,
                    "file_path": "D:/tmp/report.txt",
                },
            }
        ]
        tasks.append(
            {
                "task_id": "task_2",
                "task_type": "web_worker",
                "tool_name": "web.fetch_and_extract",
                "status": "completed",
                "params": {},
                "result": {
                    "success": True,
                    "data": [{"title": "A"}, {"title": "B"}],
                },
            }
        )

        artifacts = store.register_task_artifacts(
            session_id=session["session_id"],
            job_id=job["job_id"],
            tasks=tasks,
        )
        completion = store.complete_job(
            session_id=session["session_id"],
            job_id=job["job_id"],
            status="completed",
            success=True,
            output="done",
            error="",
            intent="file_operation",
            tasks=tasks,
            policy_decisions=[
                {
                    "task_id": "task_1",
                    "tool_name": "file.read_write",
                    "decision": "approved",
                    "requires_human_confirm": True,
                }
            ],
            artifacts=artifacts,
        )

        assert artifacts
        assert artifacts[0]["path"] == "D:/tmp/report.txt"
        assert any(item.get("artifact_type") == "structured_data" for item in artifacts)
        assert running["status"] == "running"
        assert completion["job_record"]["artifact_ids"] == [item["artifact_id"] for item in artifacts]
        assert completion["job_record"]["policy_decisions"][0]["decision"] == "approved"
        assert completion["job_record"]["tasks_completed"] == 2
        assert completion["session_record"]["last_job_id"] == job["job_id"]
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)


def test_runtime_state_store_reuses_existing_session():
    state_dir = _make_test_dir()
    store = RuntimeStateStore(state_dir=state_dir)

    try:
        first = store.get_or_create_session(session_id="session_keep", source="test")
        second = store.get_or_create_session(session_id="session_keep", source="ui")

        assert first["session_id"] == second["session_id"] == "session_keep"
        assert second["source"] == "ui"
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)


def test_runtime_state_store_queue_claims_next_job():
    state_dir = _make_test_dir()
    store = RuntimeStateStore(state_dir=state_dir)

    try:
        session = store.get_or_create_session(session_id="session_queue", source="test")
        first = store.submit_job(session_id=session["session_id"], user_input="first job")
        second = store.submit_job(session_id=session["session_id"], user_input="second job")

        claimed = store.claim_next_queued_job()
        queue_summary = store.get_queue_summary()

        assert claimed is not None
        assert claimed["job_id"] == first["job_id"]
        assert queue_summary["running"] == 1
        assert queue_summary["queued"] == 1
        assert second["status"] == "queued"
        assert store.get_job(first["job_id"])["job_id"] == first["job_id"]
        assert store.load_jobs(session_id=session["session_id"], status="queued", limit=None)
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)


def test_runtime_state_store_persists_checkpoints():
    state_dir = _make_test_dir()
    store = RuntimeStateStore(state_dir=state_dir)

    try:
        session = store.get_or_create_session(session_id="session_checkpoint", source="test")
        job = store.submit_job(session_id=session["session_id"], user_input="checkpoint me")

        checkpoint = store.save_checkpoint(
            session_id=session["session_id"],
            job_id=job["job_id"],
            stage="route",
            state={
                "session_id": session["session_id"],
                "job_id": job["job_id"],
                "execution_status": "routing",
                "task_queue": [{"task_id": "task_1", "status": "pending"}],
                "messages": [object()],
            },
            note="Router completed",
        )
        latest = store.get_latest_checkpoint(job["job_id"])
        persisted_job = store.get_job(job["job_id"])

        assert checkpoint["stage"] == "route"
        assert latest["checkpoint_id"] == checkpoint["checkpoint_id"]
        assert latest["state"]["task_queue"][0]["task_id"] == "task_1"
        assert persisted_job["checkpoint_count"] == 1
        assert persisted_job["last_checkpoint_stage"] == "route"
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)


def test_runtime_state_store_releases_due_schedules_and_persists_preferences():
    state_dir = _make_test_dir()
    store = RuntimeStateStore(state_dir=state_dir)

    try:
        session = store.get_or_create_session(session_id="session_schedule", source="test")
        updated_preferences = store.update_preferences(
            session_id=session["session_id"],
            preferences={
                "default_output_directory": "D:/tmp/exports",
                "preferred_tools": ["file.read_write", "web.fetch_and_extract"],
                "auto_queue_confirmations": True,
            },
        )
        schedule = store.create_schedule(
            session_id=session["session_id"],
            user_input="write the daily digest",
            schedule_type="once",
            run_at="2026-03-01T09:00:00",
        )

        released = store.release_due_schedules(limit=2)
        schedules = store.load_schedules(session_id=session["session_id"], limit=None)
        queue_summary = store.get_queue_summary()

        assert updated_preferences["default_output_directory"] == "D:/tmp/exports"
        assert updated_preferences["auto_queue_confirmations"] is True
        assert released
        assert released[0]["schedule_id"] == schedule["schedule_id"]
        assert queue_summary["queued"] == 1
        assert schedules[0]["status"] == "completed"
        claimed_job = store.get_job(released[0]["job_id"])
        assert claimed_job["trigger_source"] == "schedule"
        assert claimed_job["schedule_id"] == schedule["schedule_id"]
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)


def test_runtime_state_store_tracks_notifications():
    state_dir = _make_test_dir()
    store = RuntimeStateStore(state_dir=state_dir)

    try:
        notice = store.create_notification(
            session_id="session_notice",
            job_id="job_notice",
            title="Task completed",
            message="Your report is ready",
            level="success",
            category="job_result",
        )
        unread = store.load_notifications(session_id="session_notice", unread_only=True, limit=None)
        marked = store.mark_notification_read(notice["notification_id"])
        remaining = store.load_notifications(session_id="session_notice", unread_only=True, limit=None)

        assert unread[0]["notification_id"] == notice["notification_id"]
        assert marked["read"] is True
        assert remaining == []
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)
