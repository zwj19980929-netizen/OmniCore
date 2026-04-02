"""Unit tests for R5: Plan persistence + Reminder."""

import os
import pytest
from unittest.mock import patch

# ---------------------------------------------------------------------------
# plan_manager tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _tmp_plans_dir(tmp_path, monkeypatch):
    """Redirect PLANS_DIR to a temp directory for every test."""
    monkeypatch.setattr("core.plan_manager.PLANS_DIR", str(tmp_path / "plans"))


def _sample_tasks(n=3):
    return [
        {
            "task_id": f"t{i}",
            "task_type": "web_worker",
            "tool_name": f"web.tool_{i}",
            "description": f"task {i} description",
            "params": {"description": f"task {i} params"},
            "status": "pending",
            "depends_on": [f"t{i-1}"] if i > 1 else [],
        }
        for i in range(1, n + 1)
    ]


class TestSavePlan:
    def test_creates_plan_file(self):
        from core.plan_manager import save_plan, PLANS_DIR

        path = save_plan("job1", _sample_tasks(), user_input="hello world")
        assert path.endswith("job1.md")
        assert os.path.exists(path)

        content = open(path, encoding="utf-8").read()
        assert "# 计划: hello world" in content
        assert "> Job ID: job1" in content
        assert "> 状态: executing" in content
        assert "web.tool_1" in content
        assert "web.tool_3" in content

    def test_disabled_returns_empty(self, monkeypatch):
        monkeypatch.setattr("core.plan_manager.Settings.PLAN_PERSISTENCE_ENABLED", False)
        from core.plan_manager import save_plan

        path = save_plan("job2", _sample_tasks())
        assert path == ""

    def test_task_row_formats_depends(self):
        from core.plan_manager import save_plan

        tasks = _sample_tasks(3)
        path = save_plan("job3", tasks, user_input="test")
        content = open(path, encoding="utf-8").read()
        # task 2 depends on t1
        assert "t1" in content
        # task 1 depends on nothing
        assert "| - |" in content

    def test_long_user_input_truncated(self):
        from core.plan_manager import save_plan

        long_input = "x" * 100
        path = save_plan("job4", _sample_tasks(1), user_input=long_input)
        content = open(path, encoding="utf-8").read()
        assert "..." in content.split("\n")[0]

    def test_replan_appends_record(self):
        from core.plan_manager import save_plan

        save_plan("job5", _sample_tasks(2), user_input="initial")
        save_plan(
            "job5",
            _sample_tasks(1),
            replan_count=1,
            replan_reason="task 2 timed out",
        )
        content = open(
            os.path.join(os.path.dirname(__file__), "..", "data", "plans", "job5.md")
            if False
            else save_plan("job5", _sample_tasks(1), replan_count=1, replan_reason=""),
            encoding="utf-8",
        ) if False else None

        # re-read to check
        from core.plan_manager import load_plan, PLANS_DIR

        path = os.path.join(PLANS_DIR, "job5.md")
        content = open(path, encoding="utf-8").read()
        assert "## 重规划记录" in content
        assert "重规划 #1" in content
        assert "task 2 timed out" in content
        assert "> 重规划次数: 1" in content

    def test_second_replan_does_not_duplicate_header(self):
        from core.plan_manager import save_plan, PLANS_DIR

        save_plan("job6", _sample_tasks(2), user_input="init")
        save_plan("job6", _sample_tasks(1), replan_count=1, replan_reason="reason 1")
        save_plan("job6", _sample_tasks(1), replan_count=2, replan_reason="reason 2")

        content = open(os.path.join(PLANS_DIR, "job6.md"), encoding="utf-8").read()
        assert content.count("## 重规划记录") == 1
        assert "重规划 #1" in content
        assert "重规划 #2" in content
        assert "> 重规划次数: 2" in content


class TestCompletePlan:
    def test_marks_completed(self):
        from core.plan_manager import save_plan, complete_plan, PLANS_DIR

        save_plan("job7", _sample_tasks(1), user_input="test")
        complete_plan("job7")

        content = open(os.path.join(PLANS_DIR, "job7.md"), encoding="utf-8").read()
        assert "> 状态: completed" in content
        assert "> 状态: executing" not in content

    def test_noop_if_missing(self):
        from core.plan_manager import complete_plan

        # should not raise
        complete_plan("nonexistent_job")


class TestLoadPlan:
    def test_returns_content(self):
        from core.plan_manager import save_plan, load_plan

        save_plan("job8", _sample_tasks(1), user_input="test")
        content = load_plan("job8")
        assert "# 计划:" in content

    def test_returns_empty_if_missing(self):
        from core.plan_manager import load_plan

        assert load_plan("missing") == ""


# ---------------------------------------------------------------------------
# plan_reminder tests
# ---------------------------------------------------------------------------


class TestGenerateReminder:
    def _state(self, *, tasks, turn_count=0, last_change=0):
        return {
            "task_queue": tasks,
            "loop_state": {
                "turn_count": turn_count,
                "last_status_change_turn": last_change,
            },
        }

    def test_no_tasks_returns_none(self):
        from core.plan_reminder import generate_reminder

        assert generate_reminder(self._state(tasks=[])) is None

    def test_stale_turns_triggers_reminder(self):
        from core.plan_reminder import generate_reminder

        tasks = [{"status": "pending"}] * 3
        result = generate_reminder(self._state(
            tasks=tasks, turn_count=10, last_change=3,
        ))
        assert result is not None
        assert "计划提醒" in result
        assert "7 轮" in result

    def test_no_reminder_when_fresh(self):
        from core.plan_reminder import generate_reminder

        tasks = [{"status": "pending"}] * 3
        result = generate_reminder(self._state(
            tasks=tasks, turn_count=5, last_change=3,
        ))
        assert result is None

    def test_low_completion_rate_triggers(self):
        from core.plan_reminder import generate_reminder

        tasks = [
            {"status": "completed"},
            {"status": "failed"},
            {"status": "failed"},
            {"status": "pending"},
            {"status": "pending"},
        ]
        # turn_count > total * 2 = 10, completion_rate = 1/5 = 0.2 < 0.3, failed > 0
        result = generate_reminder(self._state(
            tasks=tasks, turn_count=12, last_change=12,
        ))
        assert result is not None
        assert "完成率" in result

    def test_all_completed_nudges_finalize(self):
        from core.plan_reminder import generate_reminder

        tasks = [{"status": "completed"}] * 3
        result = generate_reminder(self._state(
            tasks=tasks, turn_count=5, last_change=5,
        ))
        assert result is not None
        assert "已完成" in result

    def test_multiple_reminders_concatenated(self):
        from core.plan_reminder import generate_reminder

        # stale + low completion: 5 tasks, 1 completed, 2 failed, 2 pending
        # turn=15, last_change=2 → stale_turns=13 ≥ 5, pending>0
        # turn=15 > total*2=10, rate=1/5=0.2 < 0.3, failed>0
        tasks = [
            {"status": "completed"},
            {"status": "failed"},
            {"status": "failed"},
            {"status": "pending"},
            {"status": "pending"},
        ]
        result = generate_reminder(self._state(
            tasks=tasks, turn_count=15, last_change=2,
        ))
        assert result is not None
        assert result.count("[计划提醒]") >= 2


class TestUpdateStatusChangeTurn:
    def test_updates_loop_state(self):
        from core.plan_reminder import update_status_change_turn

        state = {"loop_state": {"turn_count": 7, "last_status_change_turn": 2}}
        update_status_change_turn(state)
        assert state["loop_state"]["last_status_change_turn"] == 7

    def test_handles_empty_loop_state(self):
        from core.plan_reminder import update_status_change_turn

        state = {"loop_state": {}}
        update_status_change_turn(state)
        assert state["loop_state"]["last_status_change_turn"] == 0
