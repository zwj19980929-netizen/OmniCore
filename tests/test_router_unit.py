from core.router import RouterAgent


def test_router_normalizes_tool_first_shape_from_legacy_task():
    router = RouterAgent()

    result = router._normalize_task_plan_shape(
        {
            "tasks": [
                {
                    "task_type": "file_worker",
                    "params": {"action": "write", "file_path": "out.txt"},
                }
            ]
        }
    )

    task = result["tasks"][0]
    assert task["tool_name"] == "file.read_write"
    assert task["tool_args"]["file_path"] == "out.txt"


def test_router_preserves_explicit_tool_args():
    router = RouterAgent()

    result = router._normalize_task_plan_shape(
        {
            "tasks": [
                {
                    "tool_name": "web.fetch_and_extract",
                    "tool_args": {"url": "https://example.com"},
                }
            ]
        }
    )

    task = result["tasks"][0]
    assert task["task_type"] == "web_worker"
    assert task["params"]["url"] == "https://example.com"
