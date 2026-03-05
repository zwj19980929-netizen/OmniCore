from agents.critic import CriticAgent
from agents.validator import Validator
from core.statuses import WAITING_FOR_APPROVAL


def _base_state(task_queue):
    return {
        "messages": [],
        "task_queue": task_queue,
        "critic_approved": False,
        "critic_feedback": "",
        "execution_status": "executing",
        "validator_passed": True,
    }


def test_validator_fails_when_failed_tasks_exist():
    validator = Validator()
    state = _base_state(
        [
            {
                "task_id": "task_1",
                "task_type": "web_worker",
                "description": "fetch latest updates",
                "status": "failed",
                "result": {"success": False, "error": "url not found"},
            }
        ]
    )
    result = validator.validate(state)
    assert result["validator_passed"] is False


def test_critic_rejects_when_all_tasks_failed():
    critic = CriticAgent()
    state = _base_state(
        [
            {
                "task_id": "task_1",
                "task_type": "web_worker",
                "description": "fetch latest updates",
                "status": "failed",
                "result": {"success": False, "error": "url not found"},
            }
        ]
    )
    result = critic.review(state)
    assert result["critic_approved"] is False
    assert "任务失败" in result["critic_feedback"]


def test_critic_allows_waiting_tasks_without_false_replan():
    critic = CriticAgent()
    state = _base_state(
        [
            {
                "task_id": "task_wait",
                "task_type": "api_worker",
                "description": "await user confirmation",
                "status": WAITING_FOR_APPROVAL,
                "result": {"approval_required": True},
            }
        ]
    )
    result = critic.review(state)
    assert result["critic_approved"] is True
