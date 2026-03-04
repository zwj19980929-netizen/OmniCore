from core.graph import finalize_node


def test_finalize_node_builds_delivery_summary_with_artifacts():
    state = {
        "messages": [],
        "user_input": "summarize and save the results",
        "session_id": "session_1",
        "job_id": "job_1",
        "current_intent": "research",
        "intent_confidence": 1.0,
        "task_queue": [
            {
                "task_id": "task_1",
                "task_type": "file_worker",
                "tool_name": "file.read_write",
                "description": "Write the report to disk",
                "params": {"file_path": "D:/tmp/report.txt"},
                "status": "completed",
                "result": {"success": True, "file_path": "D:/tmp/report.txt"},
                "priority": 10,
            },
            {
                "task_id": "task_2",
                "task_type": "web_worker",
                "tool_name": "web.fetch_and_extract",
                "description": "Fetch additional context",
                "params": {},
                "status": "failed",
                "result": {"success": False, "error": "Request timed out"},
                "priority": 5,
            },
        ],
        "current_task_index": 0,
        "shared_memory": {},
        "artifacts": [],
        "critic_feedback": "One source could not be fetched.",
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

    result = finalize_node(state)

    assert "Completed 1 of 2 task(s)" in result["final_output"]
    assert "Completed work:" in result["final_output"]
    assert "Deliverables:" in result["final_output"]
    assert "report.txt" in result["final_output"]
    assert "Open issues:" in result["final_output"]
    assert "Request timed out" in result["final_output"]
    assert result["delivery_package"]["completed_task_count"] == 1
    assert result["delivery_package"]["issues"][0]["error"] == "Request timed out"
    assert result["execution_status"] == "completed_with_issues"
    assert result["artifacts"][0]["path"] == "D:/tmp/report.txt"


def test_finalize_node_marks_waiting_for_approval_state():
    state = {
        "messages": [],
        "user_input": "send the prepared webhook",
        "session_id": "session_2",
        "job_id": "job_2",
        "current_intent": "integration",
        "intent_confidence": 1.0,
        "task_queue": [
            {
                "task_id": "task_api",
                "task_type": "api_worker",
                "tool_name": "api.call",
                "description": "Send webhook",
                "params": {"method": "POST", "url": "https://example.com"},
                "status": "waiting_for_approval",
                "result": {"approval_required": True},
                "priority": 10,
            }
        ],
        "current_task_index": 0,
        "shared_memory": {},
        "artifacts": [],
        "critic_feedback": "",
        "critic_approved": True,
        "human_approved": True,
        "needs_human_confirm": False,
        "error_trace": "",
        "final_output": "",
        "delivery_package": {},
        "execution_status": "reviewing",
        "replan_count": 0,
        "validator_passed": True,
    }

    result = finalize_node(state)

    assert result["execution_status"] == "waiting_for_approval"
    assert "waiting for approval" in result["final_output"].lower()
    assert result["delivery_package"]["recommended_next_step"]
