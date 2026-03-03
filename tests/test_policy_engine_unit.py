from core.policy_engine import evaluate_task_policy


def test_policy_engine_marks_system_tasks_as_high_risk():
    decision = evaluate_task_policy(
        {
            "task_type": "system_worker",
            "tool_name": "system.control",
            "description": "Run a shell command",
            "params": {"command": "dir", "working_dir": "D:/tmp"},
        }
    )

    assert decision.requires_confirmation is True
    assert decision.risk_level == "high"
    assert "system operations" in decision.reason


def test_policy_engine_keeps_read_only_browser_tasks_low_risk():
    decision = evaluate_task_policy(
        {
            "task_type": "browser_agent",
            "tool_name": "browser.interact",
            "description": "Read the current page and collect visible headlines",
            "params": {"task": "read the current page", "start_url": "https://example.com"},
        }
    )

    assert decision.requires_confirmation is False
    assert decision.risk_level == "low"
