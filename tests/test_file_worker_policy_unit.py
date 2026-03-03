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
