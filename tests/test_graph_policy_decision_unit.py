from core.graph import get_first_executor, human_confirm_node_v2


def test_human_confirm_node_updates_policy_decisions_on_approval(monkeypatch):
    monkeypatch.setattr(
        "core.graph_nodes.HumanConfirm.request_confirmation",
        lambda **kwargs: True,
    )

    state = {
        "task_queue": [
            {
                "task_id": "task_1",
                "task_type": "file_worker",
                "tool_name": "file.read_write",
                "description": "Write the report",
                "requires_confirmation": True,
                "policy_reason": "file write operations require explicit approval",
                "risk_level": "medium",
                "affected_resources": ["report.txt"],
            }
        ],
        "policy_decisions": [
            {
                "task_id": "task_1",
                "tool_name": "file.read_write",
                "decision": "pending_confirmation",
                "requires_human_confirm": True,
            }
        ],
        "needs_human_confirm": True,
        "human_approved": False,
        "message_bus": [],
        "execution_status": "routing",
        "error_trace": "",
    }

    result = human_confirm_node_v2(state)

    assert result["human_approved"] is True
    assert result["policy_decisions"][0]["decision"] == "approved"
    assert result["policy_decisions"][0]["approved_by"] == "user"


def test_human_confirm_node_cancellation_sets_final_output_and_stops_execution(monkeypatch):
    monkeypatch.setattr(
        "core.graph_nodes.HumanConfirm.request_confirmation",
        lambda **kwargs: False,
    )

    state = {
        "task_queue": [
            {
                "task_id": "task_1",
                "task_type": "web_worker",
                "tool_name": "web.fetch_and_extract",
                "description": "Fetch public page",
                "requires_confirmation": False,
                "status": "pending",
                "priority": 10,
                "depends_on": [],
            },
            {
                "task_id": "task_2",
                "task_type": "file_worker",
                "tool_name": "file.read_write",
                "description": "Write report",
                "requires_confirmation": True,
                "status": "waiting_for_approval",
                "priority": 5,
                "depends_on": [],
            },
        ],
        "policy_decisions": [],
        "needs_human_confirm": True,
        "human_approved": False,
        "message_bus": [],
        "execution_status": "routing",
        "error_trace": "",
        "final_output": "",
    }

    result = human_confirm_node_v2(state)

    assert result["execution_status"] == "cancelled"
    assert "cancelled" in result["final_output"].lower()
    assert get_first_executor(result) == "end"
