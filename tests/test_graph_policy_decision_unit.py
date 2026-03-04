from core.graph import human_confirm_node_v2


def test_human_confirm_node_updates_policy_decisions_on_approval(monkeypatch):
    monkeypatch.setattr(
        "core.graph.HumanConfirm.request_confirmation",
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
        "shared_memory": {},
        "execution_status": "routing",
        "error_trace": "",
    }

    result = human_confirm_node_v2(state)

    assert result["human_approved"] is True
    assert result["policy_decisions"][0]["decision"] == "approved"
    assert result["policy_decisions"][0]["approved_by"] == "user"
