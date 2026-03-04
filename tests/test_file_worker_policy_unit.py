import shutil
import uuid
from pathlib import Path

from agents.file_worker import FileWorker


def test_file_worker_write_honors_policy_confirmation(monkeypatch):
    worker = FileWorker()

    monkeypatch.setattr(
        "agents.file_worker.HumanConfirm.request_file_write_confirmation",
        lambda **kwargs: False,
    )

    result = worker.execute(
        {
            "task_id": "task_1",
            "task_type": "file_worker",
            "description": "save report",
            "params": {"action": "write", "file_path": "report.txt", "content": "hello"},
            "status": "pending",
            "result": None,
            "priority": 5,
            "execution_trace": [],
            "failure_type": None,
            "requires_confirmation": True,
        },
        {},
    )

    assert result["success"] is False
    assert "取消" in result["error"]


def test_file_worker_uses_preferred_output_directory_when_path_missing(monkeypatch):
    worker = FileWorker()
    captured = {}
    output_dir = Path.cwd() / "data" / f"test_file_worker_{uuid.uuid4().hex[:8]}" / "exports"
    output_dir.mkdir(parents=True, exist_ok=True)

    def fake_write_file(file_path, content, **kwargs):
        captured["file_path"] = file_path
        target = output_dir / "output.txt"
        target.write_text(content, encoding="utf-8")
        return {
            "success": True,
            "file_path": str(target),
        }

    monkeypatch.setattr(worker, "write_file", fake_write_file)

    try:
        result = worker.execute(
            {
                "task_id": "task_2",
                "task_type": "file_worker",
                "description": "save report",
                "params": {"action": "write", "content": "hello"},
                "status": "pending",
                "result": None,
                "priority": 5,
                "execution_trace": [],
                "failure_type": None,
                "requires_confirmation": False,
            },
            {
                "user_preferences": {
                    "default_output_directory": str(output_dir),
                }
            },
        )

        assert result["success"] is True
        assert captured["file_path"] == str(output_dir / "output.txt")
    finally:
        shutil.rmtree(output_dir.parent, ignore_errors=True)
