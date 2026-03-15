from core.graph import replanner_node
import core.graph as graph_module


class _FakeResponse:
    content = "{}"


class _FakeLLM:
    last_user_message = ""

    def chat_with_system(self, *, user_message, **_kwargs):
        self.last_user_message = user_message
        return _FakeResponse()

    def parse_json_response(self, _response):
        return {
            "analysis": "browser landed on the wrong page",
            "failed_approach": "lost the original target url",
            "new_strategy": "retry against the same target page",
            "should_give_up": False,
            "direct_answer": "",
            "tasks": [
                {
                    "task_id": "replan_task_1",
                    "task_type": "browser_agent",
                    "description": "Open the page and extract data after rendering",
                    "params": {"task": "open the page and read it"},
                    "priority": 10,
                    "depends_on": [],
                }
            ],
        }


class _SynthesisTaskLLM:
    def chat_with_system(self, *, user_message, **_kwargs):
        self.last_user_message = user_message
        return _FakeResponse()

    def parse_json_response(self, _response):
        return {
            "analysis": "need a concise conclusion after extraction",
            "failed_approach": "previous answer lacked a final summary",
            "new_strategy": "keep the fetch task and let finalize write the answer",
            "should_give_up": False,
            "direct_answer": "",
            "tasks": [
                {
                    "task_id": "replan_task_fetch",
                    "tool_name": "web.fetch_and_extract",
                    "description": "Fetch authoritative reports about the timeline",
                    "params": {"url": "https://example.com/report"},
                    "priority": 10,
                    "depends_on": [],
                },
                {
                    "task_id": "replan_task_summary",
                    "tool_name": "system.control",
                    "description": "Based on the extracted dates, calculate duration and answer the user directly.",
                    "params": {},
                    "priority": 8,
                    "depends_on": ["replan_task_fetch"],
                },
            ],
        }


def _make_state(user_input: str, failed_params: dict, failed_result: dict):
    return {
        "messages": [],
        "user_input": user_input,
        "session_id": "",
        "job_id": "",
        "current_intent": "weather_query",
        "intent_confidence": 1.0,
        "task_queue": [
            {
                "task_id": "task_1",
                "task_type": "browser_agent",
                "tool_name": "browser.interact",
                "description": "Open weather page",
                "params": failed_params,
                "status": "failed",
                "result": failed_result,
                "priority": 10,
                "failure_type": "navigation_error",
                "execution_trace": [],
            }
        ],
        "current_task_index": 0,
        "shared_memory": {},
        "artifacts": [],
        "policy_decisions": [],
        "critic_feedback": "",
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


def test_replanner_preserves_direct_user_target_url(monkeypatch):
    fake_llm = _FakeLLM()
    monkeypatch.setattr(graph_module, "LLMClient", lambda: fake_llm)

    target_url = "https://www.weather.com.cn/weather/101220101.shtml"
    state = _make_state(
        user_input=target_url,
        failed_params={"task": "open and read weather page", "start_url": target_url},
        failed_result={
            "success": False,
            "message": "navigation landed on unexpected page",
            "url": "https://www.google.com/",
            "expected_url": target_url,
        },
    )

    result = replanner_node(state)

    repaired_task = result["task_queue"][0]
    assert repaired_task["tool_name"] == "browser.interact"
    assert repaired_task["params"]["start_url"] == target_url
    assert "Authoritative target URL" in fake_llm.last_user_message


def test_replanner_can_reuse_target_url_from_failed_task_when_user_input_has_no_url(monkeypatch):
    fake_llm = _FakeLLM()
    monkeypatch.setattr(graph_module, "LLMClient", lambda: fake_llm)

    target_url = "https://www.weather.com.cn/weather/101220101.shtml"
    state = _make_state(
        user_input="Open the original weather page again and extract tomorrow's forecast",
        failed_params={"task": "open and read weather page", "start_url": target_url},
        failed_result={
            "success": False,
            "message": "navigation landed on unexpected page",
            "url": "https://www.google.com/",
            "expected_url": target_url,
        },
    )

    result = replanner_node(state)

    repaired_task = result["task_queue"][0]
    assert repaired_task["params"]["start_url"] == target_url


def test_replanner_sanitizes_polluted_target_url_before_reuse(monkeypatch):
    fake_llm = _FakeLLM()
    monkeypatch.setattr(graph_module, "LLMClient", lambda: fake_llm)

    polluted_url = "https://news.ycombinator.com），抓取前 3 条新闻"
    state = _make_state(
        user_input="重新打开原来的页面并继续抓取",
        failed_params={"task": "open and read page", "start_url": polluted_url},
        failed_result={
            "success": False,
            "message": "navigation failed",
            "url": "chrome-error://chromewebdata/",
            "expected_url": polluted_url,
        },
    )

    result = replanner_node(state)

    repaired_task = result["task_queue"][0]
    assert repaired_task["params"]["start_url"] == "https://news.ycombinator.com"


def test_replanner_preserves_critic_approved_completed_tasks(monkeypatch):
    fake_llm = _FakeLLM()
    monkeypatch.setattr(graph_module, "LLMClient", lambda: fake_llm)

    target_url = "https://www.weather.com.cn/weather/101220101.shtml"
    state = _make_state(
        user_input="Open the original weather page again and extract tomorrow's forecast",
        failed_params={"task": "open and read weather page", "start_url": target_url},
        failed_result={
            "success": False,
            "message": "navigation landed on unexpected page",
            "url": "https://www.google.com/",
            "expected_url": target_url,
        },
    )
    state["task_queue"].append(
        {
            "task_id": "task_ok",
            "task_type": "web_worker",
            "tool_name": "web.fetch_and_extract",
            "description": "Fetch backup weather source",
            "params": {"url": "https://example.com/weather"},
            "status": "completed",
            "result": {"success": True, "data": [{"text": "Cloudy 8C"}]},
            "priority": 8,
            "critic_approved": True,
            "critic_review": {"approved": True, "score": 0.9},
            "execution_trace": [],
        }
    )

    result = replanner_node(state)

    task_ids = [task["task_id"] for task in result["task_queue"]]
    assert "task_ok" in task_ids
    assert "replan_task_1" in task_ids


def test_replanner_does_not_treat_generic_weather_homepage_as_authoritative(monkeypatch):
    fake_llm = _FakeLLM()
    monkeypatch.setattr(graph_module, "LLMClient", lambda: fake_llm)

    state = _make_state(
        user_input="给我查查合肥的天气，我想看看你是怎么操作浏览器的",
        failed_params={"task": "open homepage and search city", "start_url": "https://www.weather.com.cn/"},
        failed_result={
            "success": False,
            "message": "browser landed on blocked page during execution",
            "url": "https://www.weather.com.cn/ok.html",
            "expected_url": "https://www.weather.com.cn/",
        },
    )

    replanner_node(state)

    assert "Authoritative target URL" not in fake_llm.last_user_message


def test_replanner_strips_non_executable_system_summary_task(monkeypatch):
    fake_llm = _SynthesisTaskLLM()
    monkeypatch.setattr(graph_module, "LLMClient", lambda: fake_llm)

    state = _make_state(
        user_input="最近美伊冲突持续了几天？",
        failed_params={"task": "fetch recent conflict timeline"},
        failed_result={"success": False, "error": "sources were too broad"},
    )

    result = replanner_node(state)

    task_ids = [task["task_id"] for task in result["task_queue"]]
    assert task_ids == ["replan_task_fetch"]
    assert result["needs_human_confirm"] is False
    assert result["human_approved"] is True
    assert result["shared_memory"]["_final_answer_instructions"] == [
        "Based on the extracted dates, calculate duration and answer the user directly."
    ]
