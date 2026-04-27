from core.graph_utils import should_skip_remaining_tasks


def test_adaptive_skip_does_not_treat_failed_dependency_as_satisfied():
    state = {
        "current_intent": "information_comparison",
        "user_input": "对比 DeepSeek 和 OpenAI 最新模型",
        "task_queue": [
            {
                "task_id": "task_1",
                "status": "completed",
                "result": {"success": True, "data": [{"title": "DeepSeek result"}]},
            },
            {
                "task_id": "task_2",
                "status": "failed",
                "result": {"success": False, "error": "source extraction failed"},
            },
            {
                "task_id": "task_3",
                "status": "pending",
                "depends_on": ["task_1", "task_2"],
                "result": {},
            },
        ],
    }

    assert should_skip_remaining_tasks(state) is False
    assert state["task_queue"][2]["status"] == "pending"


def test_adaptive_skip_still_applies_when_requested_count_is_met():
    state = {
        "current_intent": "information_query",
        "user_input": "提取 2 条新闻",
        "task_queue": [
            {
                "task_id": "task_1",
                "status": "completed",
                "result": {"success": True, "data": [{"title": "A"}, {"title": "B"}]},
            },
            {
                "task_id": "task_2",
                "status": "pending",
                "result": {},
            },
        ],
    }

    assert should_skip_remaining_tasks(state) is True
