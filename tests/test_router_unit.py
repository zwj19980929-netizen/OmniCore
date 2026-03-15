from pathlib import Path

from config.settings import settings
from core.router import RouterAgent
from core.state import create_initial_state


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


def test_router_repairs_missing_browser_start_url_from_user_input():
    router = RouterAgent()

    result = router._repair_task_params_from_user_input(
        "https://www.weather.com.cn/weather/101220101.shtml",
        {
            "tasks": [
                {
                    "tool_name": "browser.interact",
                    "params": {"task": "open the page and read it"},
                    "tool_args": {"task": "open the page and read it"},
                }
            ]
        },
    )

    task = result["tasks"][0]
    assert task["params"]["start_url"] == "https://www.weather.com.cn/weather/101220101.shtml"
    assert task["tool_args"]["start_url"] == "https://www.weather.com.cn/weather/101220101.shtml"


def test_router_extract_first_url_strips_fullwidth_punctuation():
    router = RouterAgent()

    value = router._extract_first_url("打开这个页面：https://news.ycombinator.com），抓取前 3 条新闻")

    assert value == "https://news.ycombinator.com"


def test_router_repairs_missing_web_smart_extract_url_from_user_input():
    router = RouterAgent()

    result = router._repair_task_params_from_user_input(
        "请处理 https://news.ycombinator.com），抓取前 3 条新闻",
        {
            "tasks": [
                {
                    "tool_name": "web.smart_extract",
                    "params": {"task": "抓取前 3 条新闻"},
                    "tool_args": {"task": "抓取前 3 条新闻"},
                }
            ]
        },
    )

    task = result["tasks"][0]
    assert task["params"]["url"] == "https://news.ycombinator.com"
    assert task["tool_args"]["url"] == "https://news.ycombinator.com"


def test_router_upgrades_search_results_url_to_web_smart_extract():
    router = RouterAgent()

    result = router._repair_task_params_from_user_input(
        "去 https://www.bing.com/search?q=openai+api 抓取前 3 条结果的标题和链接",
        {
            "tasks": [
                {
                    "tool_name": "web.fetch_and_extract",
                    "task_type": "web_worker",
                    "params": {"limit": 3},
                    "tool_args": {"limit": 3},
                }
            ]
        },
    )

    task = result["tasks"][0]
    assert task["tool_name"] == "web.smart_extract"
    assert task["task_type"] == "enhanced_web_worker"
    assert task["params"]["url"] == "https://www.bing.com/search?q=openai+api"
    assert task["tool_args"]["url"] == "https://www.bing.com/search?q=openai+api"


def test_router_guesses_browser_tool_when_plan_shape_is_ambiguous():
    router = RouterAgent()

    result = router._normalize_task_plan_shape(
        {
            "tasks": [
                {
                    "description": "Log into the website and click the export button",
                    "params": {
                        "start_url": "https://example.com/login",
                        "task": "Click login and export",
                    },
                }
            ]
        }
    )

    task = result["tasks"][0]
    assert task["tool_name"] == "browser.interact"
    assert task["task_type"] == "browser_agent"


def test_router_guesses_tool_from_schema_param_overlap_without_keyword_lists():
    router = RouterAgent()

    result = router._normalize_task_plan_shape(
        {
            "tasks": [
                {
                    "description": "请继续执行这个流程",
                    "params": {
                        "start_url": "https://example.com",
                        "task": "继续执行",
                        "headless": True,
                    },
                }
            ]
        }
    )

    task = result["tasks"][0]
    assert task["tool_name"] == "browser.interact"
    assert task["task_type"] == "browser_agent"


class _FakeResponse:
    content = "{}"


class _FakeLLM:
    def __init__(self):
        self.last_user_message = ""
        self.call_count = 0

    def chat_with_system(self, *, system_prompt, user_message, **kwargs):
        self.call_count += 1
        self.last_user_message = user_message
        return _FakeResponse()

    def parse_json_response(self, _response):
        return {
            "intent": "information_query",
            "confidence": 1.0,
            "reasoning": "ok",
            "direct_answer": "",
            "tasks": [],
            "is_high_risk": False,
        }


class _FailIfCalledLLM(_FakeLLM):
    def chat_with_system(self, *, system_prompt, user_message, **kwargs):
        raise AssertionError("LLM should not be called for deterministic weather routing")


class _QueuedLLM(_FakeLLM):
    def __init__(self, payloads):
        super().__init__()
        self.payloads = list(payloads)

    def parse_json_response(self, _response):
        if not self.payloads:
            raise AssertionError("No queued payloads left")
        return self.payloads.pop(0)


def test_router_includes_recent_session_artifacts_in_context():
    fake_llm = _FakeLLM()
    router = RouterAgent(llm_client=fake_llm)

    router.analyze_intent(
        "summarize the last exported report",
        session_artifacts=[
            {
                "artifact_type": "file",
                "name": "report.txt",
                "path": "D:/tmp/report.txt",
            },
            {
                "artifact_type": "structured_data",
                "name": "task_1_data",
                "preview": "[{\"title\": \"A\"}]",
            },
        ],
    )

    assert "Recent session artifacts" in fake_llm.last_user_message
    assert "report.txt" in fake_llm.last_user_message
    assert "task_1_data" in fake_llm.last_user_message
    assert "Deterministic tool hints" in fake_llm.last_user_message


def test_router_includes_user_preferences_in_context():
    fake_llm = _FakeLLM()
    router = RouterAgent(llm_client=fake_llm)

    router.analyze_intent(
        "save the report",
        user_preferences={
            "default_output_directory": "D:/tmp/exports",
            "user_location": "Shanghai, China",
            "preferred_tools": ["file.read_write"],
            "preferred_sites": ["example.com"],
            "task_templates": {"daily": "Generate the daily digest"},
        },
    )

    assert "User preferences" in fake_llm.last_user_message
    assert "D:/tmp/exports" in fake_llm.last_user_message
    assert "file.read_write" in fake_llm.last_user_message
    assert "example.com" in fake_llm.last_user_message


def test_router_includes_current_time_context_from_state():
    fake_llm = _FakeLLM()
    router = RouterAgent(llm_client=fake_llm)
    state = create_initial_state("plan today's work")
    state["shared_memory"]["current_time_context"] = {
        "iso_datetime": "2026-03-04T18:30:00+08:00",
        "local_date": "2026-03-04",
        "local_time": "18:30:00",
        "weekday": "Wednesday",
        "timezone": "CST",
    }

    router.route(state)

    assert "Current local time" in fake_llm.last_user_message
    assert "2026-03-04T18:30:00+08:00" in fake_llm.last_user_message
    assert "Wednesday" in fake_llm.last_user_message


def test_router_uses_current_location_context_in_deterministic_weather_route():
    router = RouterAgent(llm_client=_FailIfCalledLLM())
    state = create_initial_state("what's the weather like near me?")
    state["shared_memory"]["current_location_context"] = {
        "location": "Shanghai, China",
        "timezone": "CST",
        "source": "user_preference",
    }

    router.route(state)

    assert state["current_intent"] == "weather_query"
    assert len(state["task_queue"]) == 2
    assert "Shanghai, China" in state["task_queue"][0]["description"]
    assert state["task_queue"][0]["tool_name"] == "web.fetch_and_extract"


def test_router_short_circuits_weather_query_to_deterministic_sources():
    router = RouterAgent(llm_client=_FailIfCalledLLM())

    result = router.analyze_intent(
        "帮我查一下明天的合肥天气",
        current_time_context={"local_date": "2026-03-06"},
    )

    assert result["intent"] == "weather_query"
    assert len(result["tasks"]) == 1
    assert result["tasks"][0]["tool_name"] == "web.fetch_and_extract"
    assert result["tasks"][0]["params"]["query"] == "合肥 明天 天气"
    assert "weather.com.cn" not in result["tasks"][0]["description"]
    assert "moji.com" not in result["tasks"][0]["description"]
    assert "2026-03-07" in result["tasks"][0]["description"]


def test_router_uses_browser_route_for_weather_browser_demo():
    router = RouterAgent(llm_client=_FailIfCalledLLM())

    result = router.analyze_intent("给我查查合肥的天气，我想看看你是怎么操作浏览器的")

    assert result["intent"] == "weather_query_with_browser_demo"
    assert len(result["tasks"]) == 2
    assert result["tasks"][0]["tool_name"] == "browser.interact"
    assert result["tasks"][0]["params"]["headless"] is False
    assert result["tasks"][0]["params"]["query"] == "合肥 当前 天气"
    assert result["tasks"][0]["params"]["start_url"] == "https://www.bing.com/"
    assert "合肥" in result["tasks"][0]["description"]
    assert "weather.com.cn" not in result["tasks"][0]["description"]
    assert result["tasks"][1]["tool_name"] == "web.fetch_and_extract"


def test_router_keeps_direct_weather_url_authoritative_in_deterministic_route():
    router = RouterAgent(llm_client=_FailIfCalledLLM())

    result = router.analyze_intent(
        "https://www.weather.com.cn/weather/101220101.shtml 有啊，你通过网页有头操作渲染完成之后就有数据了"
    )

    assert result["intent"] == "weather_query_with_browser_demo"
    assert result["tasks"][0]["params"]["start_url"] == "https://www.weather.com.cn/weather/101220101.shtml"
    assert result["tasks"][1]["params"]["url"] == "https://www.weather.com.cn/weather/101220101.shtml"


def test_router_does_not_treat_action_phrase_as_weather_location_for_direct_url():
    router = RouterAgent(llm_client=_FailIfCalledLLM())

    result = router.analyze_intent(
        "去 https://www.weather.com.cn/weather/101220101.shtml 抓取今天的天气详情",
        current_time_context={"local_date": "2026-03-15"},
    )

    task = result["tasks"][0]
    assert task["params"]["url"] == "https://www.weather.com.cn/weather/101220101.shtml"
    assert "抓取今天的" not in task["description"]
    assert "https://www.weather.com.cn/weather/101220101.shtml" in task["description"]


def test_router_skips_current_location_context_for_non_geographic_task():
    fake_llm = _FakeLLM()
    router = RouterAgent(llm_client=fake_llm)
    state = create_initial_state("save the report to disk")
    state["shared_memory"]["current_location_context"] = {
        "location": "Shanghai, China",
        "timezone": "CST",
        "source": "user_preference",
    }

    router.route(state)

    assert "Current user location" not in fake_llm.last_user_message
    assert "Shanghai, China" not in fake_llm.last_user_message


def test_router_persists_direct_answer_for_taskless_queries():
    class _FakeLLMDirectAnswer(_FakeLLM):
        def parse_json_response(self, _response):
            return {
                "intent": "information_query",
                "confidence": 1.0,
                "reasoning": "无需任务",
                "direct_answer": "当前时间是 2026-03-05 10:17:54。",
                "tasks": [],
                "is_high_risk": False,
            }

    fake_llm = _FakeLLMDirectAnswer()
    router = RouterAgent(llm_client=fake_llm)
    state = create_initial_state("现在的时间是多少？")

    router.route(state)

    assert state["task_queue"] == []
    assert state["shared_memory"]["router_direct_answer"] == "当前时间是 2026-03-05 10:17:54。"


def test_router_fact_freshness_guard_replaces_direct_answer_with_verification_tasks():
    fake_llm = _QueuedLLM(
        [
            {
                "intent": "information_query",
                "confidence": 0.98,
                "reasoning": "direct answer candidate",
                "direct_answer": "截至目前，伊朗最高领袖仍是阿里·哈梅内伊。",
                "tasks": [],
                "is_high_risk": False,
            },
            {
                "requires_verification": True,
                "confidence": 0.95,
                "reason": "This asks about a current public office holder and should be verified online first.",
                "queries": [
                    "current Supreme Leader of Iran March 2026",
                    "Ali Khamenei official update March 2026",
                ],
            },
        ]
    )
    router = RouterAgent(llm_client=fake_llm)

    result = router.analyze_intent(
        "给我查询一下最近伊朗最新的最高领袖是谁？",
        current_time_context={"local_date": "2026-03-09"},
    )

    assert result["direct_answer"] == ""
    assert len(result["tasks"]) == 2
    assert all(task["tool_name"] == "web.fetch_and_extract" for task in result["tasks"])
    assert "Verification guard" in result["reasoning"]
    assert result["tasks"][0]["params"]["query"] == "current Supreme Leader of Iran March 2026"


def test_router_fact_freshness_guard_skips_local_time_question():
    class _TimeAnswerLLM(_FakeLLM):
        def parse_json_response(self, _response):
            return {
                "intent": "information_query",
                "confidence": 1.0,
                "reasoning": "无需任务",
                "direct_answer": "当前时间是 2026-03-05 10:17:54。",
                "tasks": [],
                "is_high_risk": False,
            }

    fake_llm = _TimeAnswerLLM()
    router = RouterAgent(llm_client=fake_llm)

    result = router.analyze_intent("现在的时间是多少？")

    assert result["direct_answer"] == "当前时间是 2026-03-05 10:17:54。"
    assert result["tasks"] == []
    assert fake_llm.call_count == 1


def test_router_includes_work_context_and_success_patterns():
    fake_llm = _FakeLLM()
    router = RouterAgent(llm_client=fake_llm)

    router.analyze_intent(
        "continue the weekly update",
        work_context={
            "goal": {"title": "Client weekly update"},
            "project": {"title": "Operations sync"},
            "todo": {"title": "Prepare summary", "status": "in_progress"},
            "open_todos": [{"title": "Send draft", "status": "pending"}],
        },
        resource_memory=[
            {"artifact_type": "file", "name": "last_week.md", "path": "D:/tmp/last_week.md"},
        ],
        successful_paths=[
            {
                "tool_sequence": ["web.fetch_and_extract", "file.read_write"],
                "user_input": "collect notes and write report",
            }
        ],
    )

    assert "Work context" in fake_llm.last_user_message
    assert "Client weekly update" in fake_llm.last_user_message
    assert "Reusable resource memory" in fake_llm.last_user_message
    assert "last_week.md" in fake_llm.last_user_message
    assert "Successful execution patterns" in fake_llm.last_user_message


def test_router_system_prompt_uses_dynamic_tool_catalog(monkeypatch):
    module_root = Path(__file__).parent / "plugin_fixtures"
    monkeypatch.syspath_prepend(str(module_root))
    monkeypatch.setattr(settings, "TOOL_ADAPTER_PLUGIN_MODULES", ("dynamic_tool_adapter_plugin_fixture",))
    monkeypatch.setattr(settings, "TOOL_ADAPTER_PLUGIN_DIRS", ())
    monkeypatch.setattr(settings, "ENABLED_TOOL_PLUGIN_IDS", ())
    monkeypatch.setattr(settings, "DISABLED_TOOL_PLUGIN_IDS", ())
    monkeypatch.setattr("core.tool_registry._builtin_registry", None)

    prompt = RouterAgent._build_router_system_prompt()

    assert "Registered Tool Catalog" in prompt
    assert "plugin.dynamic_tool" in prompt


def test_router_system_prompt_excludes_disabled_plugins(monkeypatch):
    module_root = Path(__file__).parent / "plugin_fixtures"
    monkeypatch.syspath_prepend(str(module_root))
    monkeypatch.setattr(settings, "TOOL_ADAPTER_PLUGIN_MODULES", ("dynamic_tool_adapter_plugin_fixture",))
    monkeypatch.setattr(settings, "TOOL_ADAPTER_PLUGIN_DIRS", ())
    monkeypatch.setattr(settings, "ENABLED_TOOL_PLUGIN_IDS", ())
    monkeypatch.setattr(settings, "DISABLED_TOOL_PLUGIN_IDS", ("test.dynamic_fixture",))
    monkeypatch.setattr("core.tool_registry._builtin_registry", None)

    prompt = RouterAgent._build_router_system_prompt()

    assert "plugin.dynamic_tool" not in prompt
