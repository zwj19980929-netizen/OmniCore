import shutil
import uuid
from pathlib import Path

from utils.workflow_automation_store import WorkflowAutomationStore


def _make_test_dir() -> Path:
    target = Path.cwd() / "data" / f"test_automation_{uuid.uuid4().hex[:8]}"
    target.mkdir(parents=True, exist_ok=True)
    return target


def test_workflow_automation_store_templates_and_directory_watches():
    state_dir = _make_test_dir()
    watch_dir = state_dir / "watch"
    watch_dir.mkdir(parents=True, exist_ok=True)
    store = WorkflowAutomationStore(state_dir=state_dir)

    try:
        template = store.create_template(
            session_id="session_1",
            name="daily sync",
            user_input="collect the new files and summarize them",
        )
        watch = store.create_directory_watch(
            session_id="session_1",
            directory_path=str(watch_dir),
            template_id=template["template_id"],
        )
        (watch_dir / "note.txt").write_text("hello", encoding="utf-8")
        events = store.poll_directory_watch_events(limit=5)

        assert template["name"] == "daily sync"
        assert watch["status"] == "waiting_for_event"
        assert events
        assert events[0]["watch_id"] == watch["watch_id"]
        assert events[0]["template_id"] == template["template_id"]
        assert store.list_directory_watch_events(session_id="session_1", limit=None)
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)
