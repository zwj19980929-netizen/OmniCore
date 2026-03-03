from core.task_executor import _select_batch_indexes


def test_select_batch_indexes_uses_tool_registry_parallelism():
    state = {
        "task_queue": [
            {
                "task_id": "task_1",
                "task_type": "browser_agent",
                "tool_name": "browser.interact",
            },
            {
                "task_id": "task_2",
                "task_type": "browser_agent",
                "tool_name": "browser.interact",
            },
            {
                "task_id": "task_3",
                "task_type": "browser_agent",
                "tool_name": "browser.interact",
            },
        ]
    }

    selected = _select_batch_indexes(state, [0, 1, 2])

    assert selected == [0, 1]


def test_select_batch_indexes_serializes_system_tool():
    state = {
        "task_queue": [
            {
                "task_id": "task_1",
                "task_type": "system_worker",
                "tool_name": "system.control",
            },
            {
                "task_id": "task_2",
                "task_type": "web_worker",
                "tool_name": "web.fetch_and_extract",
            },
        ]
    }

    selected = _select_batch_indexes(state, [0, 1])

    assert selected == [0]
