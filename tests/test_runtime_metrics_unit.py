import core.runtime as runtime


class FakeGraph:
    def invoke(self, _initial_state):
        return {
            "execution_status": "completed",
            "final_output": "done",
            "delivery_package": {
                "headline": "Completed all 1 planned task(s).",
                "completed_task_count": 1,
                "total_task_count": 1,
                "deliverables": [{"name": "out.txt"}],
                "issues": [],
            },
            "error_trace": "",
            "policy_decisions": [
                {
                    "task_id": "task_1",
                    "tool_name": "file.read_write",
                    "decision": "approved",
                    "requires_human_confirm": True,
                    "approved_by": "user",
                }
            ],
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
        self.saved_checkpoints = []
        self.notifications = []

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
        return {"job_id": kwargs.get("job_id", "job_fake")}

    def load_artifacts(self, **kwargs):
        return []

    def get_preferences(self, session_id=None):
        return {
            "default_output_directory": "",
            "user_location": "Shanghai, China",
            "preferred_tools": ["file.read_write"],
            "preferred_sites": [],
            "auto_queue_confirmations": False,
            "task_templates": {},
        }

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

    def save_checkpoint(self, **kwargs):
        record = {
            "checkpoint_id": f"checkpoint_{len(self.saved_checkpoints) + 1}",
            "session_id": kwargs.get("session_id", ""),
            "job_id": kwargs.get("job_id", ""),
            "stage": kwargs.get("stage", ""),
            "created_at": "2026-03-04T10:00:00",
            "state": kwargs.get("state", {}),
            "note": kwargs.get("note", ""),
        }
        self.saved_checkpoints.append(record)
        return record

    def create_notification(self, **kwargs):
        self.notifications.append(kwargs)
        return {"notification_id": "notice_1", **kwargs}

    def release_due_schedules(self, limit=5):
        return []

    def claim_next_queued_job(self):
        claimed = {
            "job_id": "job_claimed",
            "session_id": "session_claimed",
            "user_input": "queued task",
        }
        self.claimed_jobs.append(claimed)
        return claimed

    def get_job(self, job_id):
        if job_id == "job_failed":
            return {
                "job_id": "job_failed",
                "session_id": "session_failed",
                "user_input": "retry this",
                "status": "error",
            }
        if job_id == "job_done":
            return {
                "job_id": "job_done",
                "session_id": "session_done",
                "user_input": "done task",
                "status": "completed",
            }
        if job_id == "job_checkpoint":
            return {
                "job_id": "job_checkpoint",
                "session_id": "session_checkpoint",
                "user_input": "resume me",
                "status": "error",
                "is_special_command": False,
            }
        if job_id == "job_waiting":
            return {
                "job_id": "job_waiting",
                "session_id": "session_waiting",
                "user_input": "send webhook",
                "status": "waiting_for_approval",
                "is_special_command": False,
            }
        return {}

    def get_latest_checkpoint(self, job_id):
        for item in reversed(self.saved_checkpoints):
            if item.get("job_id") == job_id:
                return dict(item)
        if job_id == "job_checkpoint":
            return {
                "checkpoint_id": "checkpoint_resume",
                "job_id": "job_checkpoint",
                "session_id": "session_checkpoint",
                "stage": "parallel_executor",
                "created_at": "2026-03-04T10:00:00",
                "state": {
                    "session_id": "session_checkpoint",
                    "job_id": "job_checkpoint",
                    "message_bus": [],
                    "task_queue": [
                        {
                            "task_id": "task_resume",
                            "status": "pending",
                        }
                    ],
                },
            }
        return {}

    def load_checkpoints(self, job_id=None, limit=None):
        if job_id == "job_checkpoint":
            return [
                {
                    "checkpoint_id": "checkpoint_resume",
                    "job_id": "job_checkpoint",
                    "session_id": "session_checkpoint",
                    "stage": "parallel_executor",
                    "created_at": "2026-03-04T10:00:00",
                    "state": {
                        "session_id": "session_checkpoint",
                        "job_id": "job_checkpoint",
                        "message_bus": [],
                        "task_queue": [
                            {
                                "task_id": "task_resume",
                                "status": "pending",
                            }
                        ],
                    },
                },
                {
                    "checkpoint_id": "checkpoint_final",
                    "job_id": "job_checkpoint",
                    "session_id": "session_checkpoint",
                    "stage": "finalize",
                    "created_at": "2026-03-04T10:01:00",
                    "state": {
                        "session_id": "session_checkpoint",
                        "job_id": "job_checkpoint",
                        "message_bus": [],
                        "task_queue": [],
                    },
                },
            ]
        if job_id == "job_waiting":
            return [
                {
                    "checkpoint_id": "checkpoint_waiting",
                    "job_id": "job_waiting",
                    "session_id": "session_waiting",
                    "stage": "parallel_executor",
                    "created_at": "2026-03-04T10:02:00",
                    "state": {
                        "session_id": "session_waiting",
                        "job_id": "job_waiting",
                        "message_bus": [],
                        "task_queue": [
                            {
                                "task_id": "task_api",
                                "status": "waiting_for_approval",
                                "tool_name": "api.call",
                            }
                        ],
                    },
                }
            ]
        return []

    def set_job_status(self, **kwargs):
        return {
            "job_id": kwargs.get("job_id", ""),
            "session_id": "session_waiting",
            "status": kwargs.get("status", ""),
            "error": kwargs.get("error", ""),
        }


class FakeWorkContextStore:
    def __init__(self):
        self.experiences = []
        self.links = []

    def get_context_snapshot(self, **kwargs):
        return {"goal": {}, "project": {}, "todo": {}, "open_todos": []}

    def suggest_success_paths(self, **kwargs):
        return []

    def suggest_failure_avoidance(self, **kwargs):
        return []

    def record_experience(self, **kwargs):
        self.experiences.append(kwargs)
        return {"experience_id": "xp_1"}

    def record_job_link(self, **kwargs):
        self.links.append(kwargs)


class FakeArtifactStore:
    def __init__(self):
        self.records = []

    def search_artifacts(self, **kwargs):
        return []

    def record_artifacts(self, **kwargs):
        self.records.append(kwargs)
        return [{"catalog_id": "catalog_1"}]


def test_run_task_returns_runtime_metrics(monkeypatch):
    fake_store = FakeMetricsStore()
    fake_state_store = FakeRuntimeStateStore()
    fake_work_store = FakeWorkContextStore()
    fake_artifact_store = FakeArtifactStore()
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
    monkeypatch.setattr("utils.work_context_store.get_work_context_store", lambda: fake_work_store)
    monkeypatch.setattr("utils.artifact_store.get_artifact_store", lambda: fake_artifact_store)

    result = runtime.run_task("summarize the latest note", session_id="session_existing")

    assert result["success"] is True
    assert result["runtime_metrics"]["llm_cache"]["hits"] == 2
    assert result["runtime_metrics"]["browser_pool"]["acquires"] == 1
    assert result["runtime_delta"]["llm_cache"]["hits"] == 2
    assert result["session_id"] == "session_existing"
    assert result["job_id"] == "job_fake"
    assert result["artifacts"][0]["path"] == "D:/tmp/out.txt"
    assert result["policy_decisions"][0]["decision"] == "approved"
    assert result["delivery_package"]["headline"] == "Completed all 1 planned task(s)."
    assert result["checkpoint_summary"]["checkpoint_id"] == "checkpoint_1"
    assert fake_store.calls[0]["user_input"] == "summarize the latest note"
    assert fake_state_store.submitted_jobs[0]["session_id"] == "session_existing"
    assert fake_state_store.started_jobs[0]["session_id"] == "session_existing"
    assert fake_state_store.completed_jobs[0]["job_id"] == "job_fake"
    assert fake_state_store.completed_jobs[0]["policy_decisions"][0]["approved_by"] == "user"
    assert fake_state_store.notifications[0]["title"] == "Task completed"
    assert fake_work_store.experiences[0]["job_id"] == "job_fake"
    assert fake_artifact_store.records[0]["job_id"] == "job_fake"


def test_special_command_keeps_runtime_metrics(monkeypatch):
    fake_store = FakeMetricsStore()
    fake_state_store = FakeRuntimeStateStore()
    fake_work_store = FakeWorkContextStore()
    fake_artifact_store = FakeArtifactStore()
    monkeypatch.setattr(
        runtime,
        "_collect_runtime_metrics",
        lambda: {"llm_cache": {"hits": 0}, "browser_pool": {"acquires": 0}},
    )
    monkeypatch.setattr("utils.runtime_metrics_store.get_runtime_metrics_store", lambda: fake_store)
    monkeypatch.setattr("utils.runtime_state_store.get_runtime_state_store", lambda: fake_state_store)
    monkeypatch.setattr("utils.work_context_store.get_work_context_store", lambda: fake_work_store)
    monkeypatch.setattr("utils.artifact_store.get_artifact_store", lambda: fake_artifact_store)

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
    fake_work_store = FakeWorkContextStore()
    fake_artifact_store = FakeArtifactStore()
    monkeypatch.setattr(runtime, "get_graph", lambda: FakeGraph())
    monkeypatch.setattr(
        runtime,
        "_collect_runtime_metrics",
        lambda: {"llm_cache": {"hits": 1}, "browser_pool": {"acquires": 0}},
    )
    monkeypatch.setattr("utils.runtime_metrics_store.get_runtime_metrics_store", lambda: fake_store)
    monkeypatch.setattr("utils.runtime_state_store.get_runtime_state_store", lambda: fake_state_store)
    monkeypatch.setattr("utils.work_context_store.get_work_context_store", lambda: fake_work_store)
    monkeypatch.setattr("utils.artifact_store.get_artifact_store", lambda: fake_artifact_store)

    result = runtime.run_next_queued_task()

    assert result is not None
    assert result["job_id"] == "job_claimed"
    assert fake_state_store.started_jobs[0]["job_id"] == "job_claimed"
    assert fake_state_store.completed_jobs[0]["job_id"] == "job_claimed"


def test_submit_task_starts_background_worker(monkeypatch):
    fake_state_store = FakeRuntimeStateStore()
    started = []
    monkeypatch.setattr("utils.runtime_state_store.get_runtime_state_store", lambda: fake_state_store)
    monkeypatch.setattr(runtime, "start_background_worker", lambda: started.append(True) or True)

    result = runtime.submit_task("queue this")

    assert result["job_id"] == "job_fake"
    assert started == [True]


def test_rerun_and_resume_failed_job(monkeypatch):
    fake_state_store = FakeRuntimeStateStore()
    submissions = []
    monkeypatch.setattr("utils.runtime_state_store.get_runtime_state_store", lambda: fake_state_store)
    monkeypatch.setattr(
        runtime,
        "submit_task",
        lambda user_input, session_id=None, auto_start_worker=True, **kwargs: submissions.append({
            "user_input": user_input,
            "session_id": session_id,
            "auto_start_worker": auto_start_worker,
            **kwargs,
        }) or {"job_id": "job_resubmitted"},
    )

    rerun = runtime.rerun_job("job_failed")
    resumed = runtime.resume_failed_job("job_failed")
    skipped = runtime.resume_failed_job("job_done")

    assert rerun["job_id"] == "job_resubmitted"
    assert resumed["job_id"] == "job_resubmitted"
    assert skipped is None
    assert submissions[0]["user_input"] == "retry this"
    assert submissions[1]["session_id"] == "session_failed"


def test_resume_job_from_checkpoint_reuses_saved_state(monkeypatch):
    fake_state_store = FakeRuntimeStateStore()
    calls = []
    monkeypatch.setattr("utils.runtime_state_store.get_runtime_state_store", lambda: fake_state_store)
    monkeypatch.setattr(
        runtime,
        "_execute_submitted_job",
        lambda user_input, **kwargs: calls.append({"user_input": user_input, **kwargs}) or {"job_id": "job_checkpoint"},
    )

    resumed = runtime.resume_job_from_checkpoint("job_checkpoint")

    assert resumed["job_id"] == "job_checkpoint"
    assert resumed["resumed_from_checkpoint"] is True
    assert resumed["resume_checkpoint_id"] == "checkpoint_resume"
    assert resumed["resume_checkpoint_stage"] == "parallel_executor"
    assert calls[0]["runtime_session_id"] == "session_checkpoint"
    assert calls[0]["runtime_job_id"] == "job_checkpoint"
    from core.message_bus import MessageBus, MSG_RESUME_STAGE
    bus = MessageBus.from_dict(calls[0]["initial_state_override"].get("message_bus", []))
    msg = bus.get_latest(MSG_RESUME_STAGE)
    assert msg is not None and msg.payload["value"] == "parallel_executor"


def test_resume_job_from_specific_checkpoint(monkeypatch):
    fake_state_store = FakeRuntimeStateStore()
    calls = []
    monkeypatch.setattr("utils.runtime_state_store.get_runtime_state_store", lambda: fake_state_store)
    monkeypatch.setattr(
        runtime,
        "_execute_submitted_job",
        lambda user_input, **kwargs: calls.append({"user_input": user_input, **kwargs}) or {"job_id": "job_checkpoint"},
    )

    resumed = runtime.resume_job_from_checkpoint("job_checkpoint", checkpoint_id="checkpoint_resume")

    assert resumed["resume_checkpoint_id"] == "checkpoint_resume"
    assert resumed["resume_strategy"] == "checkpoint_replay"
    from core.message_bus import MessageBus, MSG_RESUME_CHECKPOINT_ID
    bus = MessageBus.from_dict(calls[0]["initial_state_override"].get("message_bus", []))
    msg = bus.get_latest(MSG_RESUME_CHECKPOINT_ID)
    assert msg is not None and msg.payload["value"] == "checkpoint_resume"


def test_approve_waiting_job_resumes_from_waiting_checkpoint(monkeypatch):
    fake_state_store = FakeRuntimeStateStore()
    calls = []
    monkeypatch.setattr("utils.runtime_state_store.get_runtime_state_store", lambda: fake_state_store)
    monkeypatch.setattr(
        runtime,
        "_execute_submitted_job",
        lambda user_input, **kwargs: calls.append({"user_input": user_input, **kwargs}) or {"job_id": "job_waiting"},
    )

    resumed = runtime.approve_waiting_job("job_waiting")

    assert resumed["approved_waiting_job"] is True
    assert resumed["resume_strategy"] == "approval_resume"
    task = calls[0]["initial_state_override"]["task_queue"][0]
    assert task["status"] == "pending"
    from core.message_bus import MessageBus, MSG_APPROVED_ACTIONS
    bus = MessageBus.from_dict(calls[0]["initial_state_override"].get("message_bus", []))
    msg = bus.get_latest(MSG_APPROVED_ACTIONS)
    assert msg is not None and msg.payload["value"] == ["task_api"]


def test_release_directory_watch_events_falls_back_when_template_is_missing(monkeypatch):
    class FakeAutomationStore:
        def poll_directory_watch_events(self, limit=5):
            return [
                {
                    "event_id": "event_1",
                    "session_id": "session_watch",
                    "template_id": "template_missing",
                    "user_input": "summarize the file",
                    "file_path": "D:/watch/new.txt",
                }
            ]

    fake_state_store = FakeRuntimeStateStore()
    submitted_tasks = []

    monkeypatch.setattr(
        "utils.workflow_automation_store.get_workflow_automation_store",
        lambda: FakeAutomationStore(),
    )
    monkeypatch.setattr(runtime, "submit_template", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        runtime,
        "submit_task",
        lambda user_input, **kwargs: submitted_tasks.append(
            {
                "user_input": user_input,
                **kwargs,
            }
        ) or {"job_id": "job_fallback"},
    )
    monkeypatch.setattr("utils.runtime_state_store.get_runtime_state_store", lambda: fake_state_store)

    released = runtime._release_directory_watch_events()

    assert len(released) == 1
    assert len(submitted_tasks) == 1
    assert "summarize the file" in submitted_tasks[0]["user_input"]
    assert "New file detected: D:/watch/new.txt" in submitted_tasks[0]["user_input"]
    assert submitted_tasks[0]["trigger_source"] == "file_event"
    assert fake_state_store.notifications
    assert fake_state_store.notifications[0]["category"] == "automation"
