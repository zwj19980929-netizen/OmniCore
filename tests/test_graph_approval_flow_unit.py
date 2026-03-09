from core.graph import after_validator, get_first_executor, replanner_node, should_retry_or_finish
from core.task_executor import collect_ready_task_indexes
import core.graph as graph_module


class _FakeResponse:
    content = "{}"


class _MixedRiskLLM:
    def chat_with_system(self, **_kwargs):
        return _FakeResponse()

    def parse_json_response(self, _response):
        return {
            "analysis": "need broader sources",
            "failed_approach": "single-source search was weak",
            "new_strategy": "run read-only fetches and prepare one optional system step",
            "should_give_up": False,
            "direct_answer": "",
            "tasks": [
                {
                    "task_id": "replan_task_web",
                    "tool_name": "web.fetch_and_extract",
                    "description": "Fetch public reports from think tanks",
                    "params": {"url": "https://example.com/report"},
                    "priority": 10,
                    "depends_on": [],
                },
                {
                    "task_id": "replan_task_system",
                    "tool_name": "system.control",
                    "description": "Open a local browser profile for manual review",
                    "params": {"command": "start chrome"},
                    "priority": 5,
                    "depends_on": [],
                },
            ],
        }


def _make_failed_state():
    return {
        "messages": [],
        "user_input": "查接班人最新传闻",
        "session_id": "session_approval",
        "job_id": "job_approval",
        "current_intent": "information_query",
        "intent_confidence": 1.0,
        "task_queue": [
            {
                "task_id": "task_1",
                "task_type": "web_worker",
                "tool_name": "web.fetch_and_extract",
                "description": "Fetch source articles",
                "params": {"url": ""},
                "status": "failed",
                "result": {"success": False, "error": "no useful sources"},
                "priority": 10,
                "failure_type": "selector_not_found",
                "execution_trace": [],
            }
        ],
        "current_task_index": 0,
        "shared_memory": {},
        "artifacts": [],
        "policy_decisions": [],
        "critic_feedback": "",
        "critic_approved": False,
        "human_approved": True,
        "needs_human_confirm": False,
        "error_trace": "",
        "final_output": "",
        "delivery_package": {},
        "execution_status": "reviewing",
        "replan_count": 0,
        "validator_passed": True,
    }


def test_replanner_only_blocks_confirmation_required_tasks(monkeypatch):
    monkeypatch.setattr(graph_module, "LLMClient", lambda: _MixedRiskLLM())

    result = replanner_node(_make_failed_state())

    task_by_id = {task["task_id"]: task for task in result["task_queue"]}
    assert task_by_id["replan_task_web"]["status"] == "pending"
    assert task_by_id["replan_task_system"]["status"] == "waiting_for_approval"
    assert result["needs_human_confirm"] is True
    assert result["human_approved"] is False
    assert collect_ready_task_indexes(result) == [0]
    assert get_first_executor(result) == "parallel_executor"


def test_waiting_tasks_finalize_instead_of_replanning():
    state = {
        "task_queue": [
            {"task_id": "task_web", "status": "completed"},
            {"task_id": "task_system", "status": "waiting_for_approval"},
        ],
        "validator_passed": True,
        "critic_approved": False,
        "replan_count": 1,
    }

    assert after_validator(state) == "finalize"
    assert should_retry_or_finish(state) == "finalize"
