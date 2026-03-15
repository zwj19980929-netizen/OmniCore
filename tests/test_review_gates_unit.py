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


def test_validator_fails_browser_task_when_expected_data_is_missing():
    validator = Validator()
    state = _base_state(
        [
            {
                "task_id": "task_browser",
                "task_type": "browser_agent",
                "description": "extract and show Hefei weather data",
                "status": "completed",
                "params": {"task": "extract Hefei weather"},
                "result": {
                    "success": True,
                    "url": "https://www.weather.com.cn/weather/101220101.shtml",
                    "expected_url": "https://www.weather.com.cn/weather/101220101.shtml",
                    "data": [],
                },
            }
        ]
    )

    result = validator.validate(state)
    assert result["validator_passed"] is False
    assert result["task_queue"][0]["status"] == "failed"


def test_validator_fails_browser_task_when_navigation_lands_on_wrong_site():
    validator = Validator()
    state = _base_state(
        [
            {
                "task_id": "task_browser",
                "task_type": "browser_agent",
                "description": "open target weather page and extract data",
                "status": "completed",
                "params": {"task": "extract Hefei weather"},
                "result": {
                    "success": True,
                    "url": "https://www.google.com/",
                    "expected_url": "https://www.weather.com.cn/weather/101220101.shtml",
                    "data": [{"title": "Google"}],
                },
            }
        ]
    )

    result = validator.validate(state)
    assert result["validator_passed"] is False
    assert result["task_queue"][0]["failure_type"] == "navigation_error"


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


class _CriticLLMGuard:
    def chat_with_system(self, *_args, **_kwargs):
        raise AssertionError("Critic LLM should not be called")

    def parse_json_response(self, _response):
        return {}


def test_critic_short_circuits_weather_results_with_signals():
    critic = CriticAgent(llm_client=_CriticLLMGuard())

    result = critic.review_task_result(
        "去 https://www.weather.com.cn/weather/101220101.shtml 抓取今天的天气情况",
        {
            "success": True,
            "data": [
                {"text": "今天 多云 12℃~19℃ 东风3级"},
                {"humidity": "湿度 61%"},
                {"aqi": "空气质量 82 良"},
            ],
        },
    )

    assert result["approved"] is True
    assert result["score"] >= 0.9


def test_critic_short_circuits_explicit_url_list_extraction_results():
    critic = CriticAgent(llm_client=_CriticLLMGuard())

    result = critic.review_task_result(
        "去 https://github.com/trending 抓取前 2 个仓库的标题和链接",
        {
            "success": True,
            "data": [
                {"title": "repo 1", "link": "https://github.com/a/b"},
                {"title": "repo 2", "link": "https://github.com/c/d"},
            ],
        },
    )

    assert result["approved"] is True
    assert result["score"] >= 0.9


def test_critic_does_not_short_circuit_when_list_target_count_not_met():
    critic = CriticAgent(llm_client=_CriticLLMGuard())

    result = critic.review_task_result(
        "去 https://news.ycombinator.com/ 抓取前 40 条新闻的标题和链接",
        {
            "success": True,
            "data": [
                {"title": "story 1", "link": "https://example.com/1"},
                {"title": "story 2", "link": "https://example.com/2"},
            ],
        },
    )

    assert result["approved"] is False


def test_critic_does_not_short_circuit_navigation_filter_links_as_valid_list_results():
    critic = CriticAgent(llm_client=_CriticLLMGuard())

    result = critic.review_task_result(
        "去 https://huggingface.co/models 抓取前 3 个模型的名称和链接",
        {
            "success": True,
            "data": [
                {"title": "Text Generation", "link": "https://huggingface.co/models?pipeline_tag=text-generation"},
                {"title": "TensorFlow", "link": "https://huggingface.co/models?library=tf"},
                {"title": "Page 2", "link": "https://huggingface.co/models?p=1"},
            ],
        },
    )

    assert result["approved"] is False
