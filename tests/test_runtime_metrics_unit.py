import core.runtime as runtime


class FakeGraph:
    def invoke(self, _initial_state):
        return {
            "execution_status": "completed",
            "final_output": "done",
            "error_trace": "",
            "task_queue": [
                {
                    "task_id": "task_1",
                    "task_type": "file_worker",
                    "tool_name": "file.read_write",
                    "status": "completed",
                    "params": {"action": "write", "file_path": "D:/tmp/out.txt"},
                    "result": {"success": True, "file_path": "D:/tmp/out.txt"},
                }
            ],
            "current_intent": "information_query",
            "critic_feedback": "",
        }


class FakeMetricsStore:
    def __init__(self):
        self.calls = []

    def append_record(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "runtime_delta": {
                "llm_cache": {"hits": kwargs["runtime_metrics"].get("llm_cache", {}).get("hits", 0)},
                "browser_pool": {"acquires": kwargs["runtime_metrics"].get("browser_pool", {}).get("acquires", 0)},
            }
        }


class FakeRuntimeStateStore:
    def __init__(self):
        self.submitted_jobs = []
        self.started_jobs = []
        self.completed_jobs = []
        self.claimed_jobs = []

    def get_or_create_session(self, session_id=None, source="runtime"):
        return {
            "session_id": session_id or "session_fake",
            "source": source,
        }

    def submit_job(self, **kwargs):
        self.submitted_jobs.append(kwargs)
        return {
            "job_id": "job_fake",
            "status": "queued",
            "is_special_command": kwargs.get("is_special_command", False),
        }

    def start_job(self, **kwargs):
        self.started_jobs.append(kwargs)
        return {"job_id": "job_fake"}

    def register_task_artifacts(self, **kwargs):
        tasks = kwargs.get("tasks", [])
        if tasks:
            return [{"artifact_id": "artifact_1", "path": "D:/tmp/out.txt", "name": "out.txt"}]
        return []

    def complete_job(self, **kwargs):
        self.completed_jobs.append(kwargs)
        return {
            "job_record": {"job_id": kwargs["job_id"], "status": kwargs["status"]},
            "session_record": {"session_id": kwargs["session_id"]},
        }

    def claim_next_queued_job(self):
        claimed = {
            "job_id": "job_claimed",
            "session_id": "session_claimed",
            "user_input": "queued task",
        }
        self.claimed_jobs.append(claimed)
        return claimed


def test_run_task_returns_runtime_metrics(monkeypatch):
    fake_store = FakeMetricsStore()
    fake_state_store = FakeRuntimeStateStore()
    monkeypatch.setattr(runtime, "get_graph", lambda: FakeGraph())
    monkeypatch.setattr(
        runtime,
        "_collect_runtime_metrics",
        lambda: {
            "llm_cache": {"hits": 2, "misses": 1},
            "browser_pool": {"acquires": 1, "reuse_hits": 0},
        },
    )
    monkeypatch.setattr("utils.runtime_metrics_store.get_runtime_metrics_store", lambda: fake_store)
    monkeypatch.setattr("utils.runtime_state_store.get_runtime_state_store", lambda: fake_state_store)

    result = runtime.run_task("summarize the latest note", session_id="session_existing")

    assert result["success"] is True
    assert result["runtime_metrics"]["llm_cache"]["hits"] == 2
    assert result["runtime_metrics"]["browser_pool"]["acquires"] == 1
    assert result["runtime_delta"]["llm_cache"]["hits"] == 2
    assert result["session_id"] == "session_existing"
    assert result["job_id"] == "job_fake"
    assert result["artifacts"][0]["path"] == "D:/tmp/out.txt"
    assert fake_store.calls[0]["user_input"] == "summarize the latest note"
    assert fake_state_store.submitted_jobs[0]["session_id"] == "session_existing"
    assert fake_state_store.started_jobs[0]["session_id"] == "session_existing"
    assert fake_state_store.completed_jobs[0]["job_id"] == "job_fake"


def test_special_command_keeps_runtime_metrics(monkeypatch):
    fake_store = FakeMetricsStore()
    fake_state_store = FakeRuntimeStateStore()
    monkeypatch.setattr(
        runtime,
        "_collect_runtime_metrics",
        lambda: {"llm_cache": {"hits": 0}, "browser_pool": {"acquires": 0}},
    )
    monkeypatch.setattr("utils.runtime_metrics_store.get_runtime_metrics_store", lambda: fake_store)
    monkeypatch.setattr("utils.runtime_state_store.get_runtime_state_store", lambda: fake_state_store)

    result = runtime.run_task("memory stats")

    assert result["is_special_command"] is True
    assert "runtime_metrics" in result
    assert result["runtime_metrics"]["llm_cache"]["hits"] == 0
    assert result["runtime_delta"]["llm_cache"]["hits"] == 0
    assert result["session_id"]
    assert result["job_id"] == "job_fake"
    assert fake_store.calls[0]["is_special_command"] is True
    assert fake_state_store.submitted_jobs[0]["is_special_command"] is True
    assert fake_state_store.completed_jobs[0]["is_special_command"] is True


def test_run_next_queued_task_executes_claimed_job(monkeypatch):
    fake_store = FakeMetricsStore()
    fake_state_store = FakeRuntimeStateStore()
    monkeypatch.setattr(runtime, "get_graph", lambda: FakeGraph())
    monkeypatch.setattr(
        runtime,
        "_collect_runtime_metrics",
        lambda: {"llm_cache": {"hits": 1}, "browser_pool": {"acquires": 0}},
    )
    monkeypatch.setattr("utils.runtime_metrics_store.get_runtime_metrics_store", lambda: fake_store)
    monkeypatch.setattr("utils.runtime_state_store.get_runtime_state_store", lambda: fake_state_store)

    result = runtime.run_next_queued_task()

    assert result is not None
    assert result["job_id"] == "job_claimed"
    assert fake_state_store.started_jobs[0]["job_id"] == "job_claimed"
    assert fake_state_store.completed_jobs[0]["job_id"] == "job_claimed"
