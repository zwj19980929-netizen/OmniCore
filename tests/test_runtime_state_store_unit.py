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
            artifacts=artifacts,
        )

        assert artifacts
        assert artifacts[0]["path"] == "D:/tmp/report.txt"
        assert any(item.get("artifact_type") == "structured_data" for item in artifacts)
        assert running["status"] == "running"
        assert completion["job_record"]["artifact_ids"] == [item["artifact_id"] for item in artifacts]
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
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)
