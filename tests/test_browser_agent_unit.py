import asyncio

from agents.browser_agent import ActionType, BrowserAction, BrowserAgent, PageElement, TaskIntent
from utils.browser_toolkit import ToolkitResult


class LLMGuard:
    def __init__(self):
        self.called = False

    async def achat(self, *_args, **_kwargs):
        self.called = True
        raise AssertionError("LLM should not be called")

    def parse_json_response(self, _response):
        return {}


class FailingIntentLLM:
    async def achat(self, *_args, **_kwargs):
        raise RuntimeError("intent llm unavailable")

    def parse_json_response(self, _response):
        return {}


def test_find_best_element_skips_hidden_or_disabled_controls():
    agent = BrowserAgent(headless=True)
    elements = [
        PageElement(
            index=0,
            tag="input",
            text="",
            element_type="input",
            selector="#hidden",
            attributes={"placeholder": "search"},
            is_visible=False,
            is_clickable=True,
        ),
        PageElement(
            index=1,
            tag="input",
            text="",
            element_type="input",
            selector="#disabled",
            attributes={"placeholder": "search"},
            is_visible=True,
            is_clickable=False,
        ),
        PageElement(
            index=2,
            tag="input",
            text="",
            element_type="input",
            selector="#visible",
            attributes={"placeholder": "search"},
            is_visible=True,
            is_clickable=True,
        ),
    ]

    chosen = agent._find_best_element("search ai", elements, kinds=["input"], keywords=["search"])

    assert chosen is not None
    assert chosen.selector == "#visible"


def test_noise_filter_keeps_help_entry():
    agent = BrowserAgent(headless=True)
    element = PageElement(
        index=0,
        tag="a",
        text="Help Center",
        element_type="link",
        selector="a.help",
        attributes={"href": "/help"},
    )

    assert agent._is_noise_element(element) is False


def test_local_decision_supports_explicit_click_target_from_quoted_text():
    agent = BrowserAgent(headless=True)
    elements = [
        PageElement(
            index=0,
            tag="a",
            text="帮助中心",
            element_type="link",
            selector="a.help",
            attributes={"labelText": "帮助中心"},
            is_visible=True,
            is_clickable=True,
        ),
        PageElement(
            index=1,
            tag="button",
            text="登录",
            element_type="button",
            selector="#login",
            attributes={"labelText": "登录"},
            is_visible=True,
            is_clickable=True,
        ),
    ]

    action = agent._decide_action_locally('请操作 "帮助中心"', elements)

    assert action is not None
    assert action.action_type == ActionType.CLICK
    assert action.target_selector == "a.help"


def test_local_decision_uses_intent_target_text():
    agent = BrowserAgent(headless=True)
    elements = [
        PageElement(
            index=0,
            tag="button",
            text="登录",
            element_type="button",
            selector="#login",
            attributes={"labelText": "登录"},
            is_visible=True,
            is_clickable=True,
        ),
        PageElement(
            index=1,
            tag="button",
            text="帮助中心",
            element_type="button",
            selector="#help",
            attributes={"labelText": "帮助中心"},
            is_visible=True,
            is_clickable=True,
        ),
    ]

    action = agent._decide_action_locally(
        "继续当前流程",
        elements,
        TaskIntent(intent_type="navigate", target_text="登录", confidence=0.8, requires_interaction=True),
    )

    assert action is not None
    assert action.action_type == ActionType.CLICK
    assert action.target_selector == "#login"


def test_decide_action_with_llm_skips_llm_when_no_elements_for_extract_task():
    guard = LLMGuard()
    agent = BrowserAgent(llm_client=guard, headless=True)

    action = asyncio.run(agent._decide_action_with_llm("提取当前页面信息", []))

    assert action.action_type == ActionType.EXTRACT
    assert guard.called is False


def test_format_elements_for_llm_uses_compact_budget():
    agent = BrowserAgent(headless=True)
    elements = []
    for idx in range(12):
        elements.append(
            PageElement(
                index=idx,
                tag="input",
                text=f"keyword field {idx}" * 4,
                element_type="input",
                selector=f"div > form > input:nth-of-type({idx + 1})" + ("x" * 80),
                attributes={"placeholder": "search box" * 6},
                is_visible=True,
                is_clickable=True,
            )
        )

    payload = agent._format_elements_for_llm("search ai news", elements)
    lines = [line for line in payload.splitlines() if line.strip()]

    assert len(lines) <= 8
    assert all(len(line) < 220 for line in lines)


def test_recover_action_chooses_alternative_element():
    agent = BrowserAgent(headless=True)
    elements = [
        PageElement(
            index=0,
            tag="button",
            text="Search",
            element_type="button",
            selector="#primary",
            attributes={"labelText": "Search"},
            is_visible=True,
            is_clickable=True,
        ),
        PageElement(
            index=1,
            tag="button",
            text="Search",
            element_type="button",
            selector="#secondary",
            attributes={"labelText": "Search"},
            is_visible=True,
            is_clickable=True,
        ),
    ]
    agent._element_cache = elements
    failed = BrowserAction(action_type=ActionType.CLICK, target_selector="#primary", description="click search")

    recovery = agent._recover_action("search ai", failed, elements)

    assert recovery is not None
    assert recovery.target_selector == "#secondary"


def test_action_loop_detection_counts_repeated_signatures():
    agent = BrowserAgent(headless=True)
    action = BrowserAction(action_type=ActionType.WAIT, value="1", description="wait")

    agent._record_action(action)
    agent._record_action(action)

    assert agent._is_action_looping(action) is True


def test_infer_task_intent_fallback_uses_structured_pairs():
    agent = BrowserAgent(llm_client=FailingIntentLLM(), headless=True)

    intent = asyncio.run(agent._infer_task_intent("name: alice, email: alice@example.com"))

    assert intent.intent_type == "form"
    assert intent.requires_interaction is True
    assert "name" in intent.fields
    assert "email" in intent.fields


def test_decide_action_locally_prefers_intent_driven_search_input():
    agent = BrowserAgent(headless=True)
    elements = [
        PageElement(
            index=0,
            tag="input",
            text="",
            element_type="search",
            selector="#search-box",
            attributes={"placeholder": "Search"},
            is_visible=True,
            is_clickable=True,
        )
    ]

    action = agent._decide_action_locally(
        "今天美伊发生了什么",
        elements,
        TaskIntent(intent_type="search", query="今天 美伊 发生 了 什么", confidence=0.9),
    )

    assert action is not None
    assert action.action_type == ActionType.INPUT
    assert action.target_selector == "#search-box"


class _ReadOnlyToolkit:
    fast_mode = False
    page = None

    def __init__(self, landing_url: str, data, title: str = "Hefei Weather"):
        self._landing_url = landing_url
        self._current_url = ""
        self._data = data
        self._title = title

    async def create_page(self):
        return ToolkitResult(success=True)

    async def goto(self, url: str, **_kwargs):
        self._current_url = self._landing_url or url
        return ToolkitResult(success=True, data=self._current_url)

    async def wait_for_load(self, *_args, **_kwargs):
        return ToolkitResult(success=True)

    async def human_delay(self, *_args, **_kwargs):
        return ToolkitResult(success=True)

    async def evaluate_js(self, *_args, **_kwargs):
        return ToolkitResult(success=True, data=self._data)

    async def get_title(self):
        return ToolkitResult(success=True, data=self._title)

    async def get_current_url(self):
        return ToolkitResult(success=True, data=self._current_url)


def test_infer_task_intent_treats_direct_url_as_read_only():
    agent = BrowserAgent(llm_client=FailingIntentLLM(), headless=True)

    intent = asyncio.run(agent._infer_task_intent("https://www.weather.com.cn/weather/101220101.shtml"))

    assert intent.intent_type == "read"
    assert intent.requires_interaction is False


def test_derive_primary_query_compresses_weather_browser_demo_instruction():
    agent = BrowserAgent(headless=True)

    query = agent._derive_primary_query("查询合肥明天的天气，并完整展示浏览器操作过程。请先打开浏览器再访问页面。")

    assert query == "合肥 明天 天气"


def test_task_looks_satisfied_requires_matching_target_url():
    agent = BrowserAgent(headless=True)
    intent = TaskIntent(intent_type="navigate", query="", confidence=0.8)

    assert agent._task_looks_satisfied(
        "open target page",
        "https://www.google.com/",
        intent,
        target_url="https://www.weather.com.cn/weather/101220101.shtml",
    ) is False
    assert agent._task_looks_satisfied(
        "open target page",
        "https://www.weather.com.cn/weather/101220101.shtml",
        intent,
        target_url="https://www.weather.com.cn/weather/101220101.shtml",
    ) is True


def test_run_direct_url_extracts_current_page_content_without_interaction():
    toolkit = _ReadOnlyToolkit(
        landing_url="https://www.weather.com.cn/weather/101220101.shtml",
        data=[{"index": 1, "text": "03/07 Cloudy 8C to 15C East wind"}],
    )
    agent = BrowserAgent(llm_client=FailingIntentLLM(), headless=True, toolkit=toolkit)

    result = asyncio.run(agent.run("https://www.weather.com.cn/weather/101220101.shtml"))

    assert result["success"] is True
    assert result["url"] == "https://www.weather.com.cn/weather/101220101.shtml"
    assert result["expected_url"] == "https://www.weather.com.cn/weather/101220101.shtml"
    assert "03/07" in result["data"][0]["text"]


def test_run_direct_url_fails_when_browser_lands_on_unexpected_page():
    toolkit = _ReadOnlyToolkit(
        landing_url="https://www.google.com/",
        data=[{"title": "Google", "link": "https://www.google.com/"}],
    )
    agent = BrowserAgent(llm_client=FailingIntentLLM(), headless=True, toolkit=toolkit)

    result = asyncio.run(agent.run("https://www.weather.com.cn/weather/101220101.shtml"))

    assert result["success"] is False
    assert "unexpected page" in result["message"]


def test_run_fails_fast_on_blocked_same_site_holding_page():
    toolkit = _ReadOnlyToolkit(
        landing_url="https://www.weather.com.cn/ok.html",
        data=[],
        title="403 Forbidden",
    )
    agent = BrowserAgent(llm_client=FailingIntentLLM(), headless=True, toolkit=toolkit)

    result = asyncio.run(agent.run("Open the weather page and extract data", start_url="https://www.weather.com.cn/"))

    assert result["success"] is False
    assert "blocked page" in result["message"]


def test_find_search_result_click_action_prefers_external_weather_detail_result():
    agent = BrowserAgent(headless=True)
    elements = [
        PageElement(
            index=0,
            tag="a",
            text="图片",
            element_type="link",
            selector="a.images",
            attributes={"href": "https://www.bing.com/images"},
            is_visible=True,
            is_clickable=True,
        ),
        PageElement(
            index=1,
            tag="a",
            text="合肥天气预报",
            element_type="link",
            selector="a.weather",
            attributes={"href": "https://www.weather.com.cn/weather/101220101.shtml"},
            is_visible=True,
            is_clickable=True,
        ),
    ]

    action = agent._find_search_result_click_action(
        "查询合肥天气，并展示浏览器操作过程",
        "https://www.bing.com/search?q=%E5%90%88%E8%82%A5+%E5%A4%A9%E6%B0%94",
        elements,
        TaskIntent(intent_type="search", query="合肥 天气", confidence=0.9),
    )

    assert action is not None
    assert action.action_type == ActionType.CLICK
    assert action.target_selector == "a.weather"


def test_run_with_start_url_and_read_description_stays_in_read_mode():
    toolkit = _ReadOnlyToolkit(
        landing_url="https://www.weather.com.cn/weather/101220101.shtml",
        data=[{"index": 1, "text": "Tomorrow Cloudy 8C to 15C"}],
    )
    agent = BrowserAgent(llm_client=FailingIntentLLM(), headless=True, toolkit=toolkit)

    result = asyncio.run(
        agent.run(
            "Open the page, wait for rendering, and extract tomorrow weather",
            start_url="https://www.weather.com.cn/weather/101220101.shtml",
        )
    )

    assert result["success"] is True
    assert result["expected_url"] == "https://www.weather.com.cn/weather/101220101.shtml"
    assert "Tomorrow" in result["data"][0]["text"]
