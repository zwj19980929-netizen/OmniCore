from core.task_planner import build_task_item_from_plan


def test_build_task_item_from_legacy_task_type_adds_tool_metadata():
    task = build_task_item_from_plan(
        {
            "task_type": "file_worker",
            "description": "Save the report to disk",
            "params": {"action": "write", "file_path": "report.txt"},
        }
    )

    assert task["task_type"] == "file_worker"
    assert task["tool_name"] == "file.read_write"
    assert task["requires_confirmation"] is True
    assert task["policy_reason"]


def test_build_task_item_from_tool_name_accepts_tool_args():
    task = build_task_item_from_plan(
        {
            "tool_name": "web.fetch_and_extract",
            "description": "Collect the latest headlines",
            "tool_args": {"url": "https://example.com"},
        }
    )

    assert task["task_type"] == "web_worker"
    assert task["tool_name"] == "web.fetch_and_extract"
    assert task["params"]["url"] == "https://example.com"
    assert task["requires_confirmation"] is False
