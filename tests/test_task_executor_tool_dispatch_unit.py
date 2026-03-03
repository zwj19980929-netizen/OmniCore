import asyncio

from core import task_executor


class _FakeFileWorker:
    def execute(self, task, shared_memory):
        return {
            "success": True,
            "file_path": "report.txt",
            "task_id": task["task_id"],
            "shared_memory_size": len(shared_memory),
        }


class _FakePool:
    def __init__(self):
        self.file_worker = _FakeFileWorker()


def test_execute_single_task_uses_tool_name_via_registry(monkeypatch):
    async def _fake_get_instance(cls):
        return _FakePool()

    monkeypatch.setattr(
        task_executor.WorkerPool,
        "get_instance",
        classmethod(_fake_get_instance),
    )

    outcome = asyncio.run(
        task_executor._execute_single_task_async(
            {
                "task_id": "task_1",
                "task_type": "legacy_unknown",
                "tool_name": "file.read_write",
                "description": "Persist the result",
                "params": {"action": "write", "file_path": "report.txt"},
                "status": "pending",
                "result": None,
                "priority": 5,
                "execution_trace": [],
                "failure_type": None,
                "risk_level": "medium",
            },
            {"seed": "value"},
        )
    )

    assert outcome["status"] == "completed"
    assert outcome["task_type"] == "file_worker"
    assert outcome["tool_name"] == "file.read_write"
