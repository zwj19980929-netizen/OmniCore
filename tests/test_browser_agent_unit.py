import asyncio

from agents.browser_agent import ActionType, BrowserAction, BrowserAgent, PageElement, SearchResultCard, TaskIntent
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


class SemanticAssessmentLLM:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    async def achat(self, *_args, **_kwargs):
        self.calls += 1
        return {"ok": True}

    def parse_json_response(self, _response):
        return self.payload


class VisionDecisionLLM:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def chat_with_image(self, *_args, **_kwargs):
        self.calls += 1

        class _Response:
            content = '{"ok": true}'

        return _Response()

    def parse_json_response(self, _response):
        return self.payload


class _BootstrapSearchToolkit:
    def __init__(self):
        self.visited = []
        self.current_url = ""
        self.fast_mode = False

    async def goto(self, url, timeout=30000):
        self.current_url = url
        self.visited.append(url)
        return ToolkitResult(success=True, data=url)

    async def wait_for_load(self, *_args, **_kwargs):
        return ToolkitResult(success=True)

    async def human_delay(self, *_args, **_kwargs):
        return ToolkitResult(success=True)

    async def wait_for_selector(self, _selector, timeout=None):
        del timeout
        if "google.com" in self.current_url:
            return ToolkitResult(success=True, data=True)
        return ToolkitResult(success=False, error="blank")

    async def evaluate_js(self, _script, _arg=None):
        if "google.com" in self.current_url:
            return ToolkitResult(success=True, data={"matches": 1, "textLength": 1200})
        return ToolkitResult(success=True, data={"matches": 0, "textLength": 20})


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


def test_bootstrap_search_results_falls_back_to_google_when_bing_is_blank():
    toolkit = _BootstrapSearchToolkit()
    agent = BrowserAgent(headless=False, toolkit=toolkit)

    success = asyncio.run(agent._bootstrap_search_results("US Iran war escalation 2026"))

    assert success is True
    assert any("bing.com/search" in url for url in toolkit.visited)
    assert any("google.com/search" in url for url in toolkit.visited)


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


def test_data_relevance_rejects_single_generic_token_overlap():
    agent = BrowserAgent(headless=True)

    relevant = agent._is_data_relevant(
        "Ali Khamenei death ayatollah killed iran us conflict",
        [{"title": "AliExpress Summer Sale", "link": "https://www.aliexpress.com/"}],
    )

    assert relevant is False


def test_observation_driven_action_uses_visible_answer_on_search_page():
    agent = BrowserAgent(headless=True)
    elements = [
        PageElement(
            index=0,
            tag="input",
            text="",
            element_type="search",
            selector="#sb_form_q",
            attributes={"placeholder": "Search", "value": "ali khamenei death ayatollah killed iran us conflict"},
            is_visible=True,
            is_clickable=True,
        )
    ]
    intent = TaskIntent(
        intent_type="search",
        query="ali khamenei death ayatollah killed iran us conflict",
        confidence=0.9,
    )
    data = [
        {
            "title": "Ali Khamenei reportedly alive after rumors spread during Iran-US crisis",
            "text": "Recent reports and public appearances indicate Ali Khamenei is alive.",
            "link": "https://example.com/report",
        }
    ]

    action = agent._choose_observation_driven_action(
        "核实阿亚图拉·阿里·哈梅内伊是否在最近冲突中死亡",
        "https://www.bing.com/search?q=ali+khamenei+death",
        elements,
        intent,
        data,
    )

    assert action is not None
    assert action.action_type == ActionType.EXTRACT


def test_assess_page_with_llm_prefers_extract_and_caches_result():
    llm = SemanticAssessmentLLM(
        {
            "page_relevant": True,
            "goal_satisfied": True,
            "confidence": 0.86,
            "action": {
                "type": "extract",
                "description": "visible snippet already answers the question",
            },
        }
    )
    agent = BrowserAgent(llm_client=llm, headless=True)
    elements = [
        PageElement(
            index=0,
            tag="input",
            text="",
            element_type="search",
            selector="#sb_form_q",
            attributes={"placeholder": "Search", "value": "ali khamenei death ayatollah killed iran us conflict"},
            is_visible=True,
            is_clickable=True,
        )
    ]
    intent = TaskIntent(
        intent_type="search",
        query="ali khamenei death ayatollah killed iran us conflict",
        confidence=0.9,
    )
    data = [
        {
            "title": "Ali Khamenei reportedly alive after rumors spread during Iran-US crisis",
            "text": "Recent reports and public appearances indicate Ali Khamenei is alive.",
            "link": "https://example.com/report",
        }
    ]

    action_1 = asyncio.run(
        agent._assess_page_with_llm(
            "核实阿亚图拉阿里哈梅内伊是否在最近冲突中死亡",
            "https://www.bing.com/search?q=ali+khamenei+death",
            "ali khamenei - Search",
            elements,
            intent,
            data,
        )
    )
    action_2 = asyncio.run(
        agent._assess_page_with_llm(
            "核实阿亚图拉阿里哈梅内伊是否在最近冲突中死亡",
            "https://www.bing.com/search?q=ali+khamenei+death",
            "ali khamenei - Search",
            elements,
            intent,
            data,
        )
    )

    assert action_1 is not None
    assert action_1.action_type == ActionType.EXTRACT
    assert action_2 is not None
    assert action_2.action_type == ActionType.EXTRACT
    assert llm.calls == 1


def test_assess_page_with_llm_can_click_semantic_result_candidate():
    llm = SemanticAssessmentLLM(
        {
            "page_relevant": True,
            "goal_satisfied": False,
            "confidence": 0.79,
            "action": {
                "type": "click",
                "element_index": 1,
                "description": "open the strongest source result",
            },
        }
    )
    agent = BrowserAgent(llm_client=llm, headless=True)
    elements = [
        PageElement(
            index=0,
            tag="input",
            text="",
            element_type="search",
            selector="#sb_form_q",
            attributes={"placeholder": "Search", "value": "ali khamenei death ayatollah killed iran us conflict"},
            is_visible=True,
            is_clickable=True,
        ),
        PageElement(
            index=1,
            tag="a",
            text="Reuters: Public appearances indicate Khamenei is alive",
            element_type="link",
            selector="a.result",
            attributes={"href": "https://www.reuters.com/world/middle-east/khamenei-update"},
            is_visible=True,
            is_clickable=True,
        ),
    ]
    intent = TaskIntent(
        intent_type="search",
        query="ali khamenei death ayatollah killed iran us conflict",
        confidence=0.9,
    )
    data = [
        {
            "title": "Rumors spread online after the crisis",
            "text": "Search snippets mention public appearances but do not provide the full source context.",
            "link": "https://example.com/summary",
        }
    ]

    action = asyncio.run(
        agent._assess_page_with_llm(
            "核实阿亚图拉阿里哈梅内伊是否在最近冲突中死亡",
            "https://www.bing.com/search?q=ali+khamenei+death",
            "ali khamenei - Search",
            elements,
            intent,
            data,
        )
    )

    assert action is not None
    assert action.action_type == ActionType.CLICK
    assert action.target_selector == "a.result"


def test_assess_page_with_llm_does_not_repeat_same_query_input():
    llm = SemanticAssessmentLLM(
        {
            "page_relevant": True,
            "goal_satisfied": False,
            "confidence": 0.61,
            "action": {
                "type": "input",
                "element_index": 0,
                "value": "ali khamenei death ayatollah killed iran us conflict",
                "description": "search again",
            },
        }
    )
    agent = BrowserAgent(llm_client=llm, headless=True)
    elements = [
        PageElement(
            index=0,
            tag="input",
            text="",
            element_type="search",
            selector="#sb_form_q",
            attributes={"placeholder": "Search", "value": "ali khamenei death ayatollah killed iran us conflict"},
            is_visible=True,
            is_clickable=True,
        )
    ]
    intent = TaskIntent(
        intent_type="search",
        query="ali khamenei death ayatollah killed iran us conflict",
        confidence=0.9,
    )

    action = asyncio.run(
        agent._assess_page_with_llm(
            "核实阿亚图拉阿里哈梅内伊是否在最近冲突中死亡",
            "https://www.bing.com/search?q=ali+khamenei+death",
            "ali khamenei - Search",
            elements,
            intent,
            [],
        )
    )

    assert action is not None
    assert action.action_type == ActionType.PRESS_KEY
    assert action.value == "Enter"


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


class _SearchResultsToolkit:
    fast_mode = False
    page = None

    def __init__(self, url: str, data):
        self._url = url
        self._data = data

    async def get_current_url(self):
        return ToolkitResult(success=True, data=self._url)

    async def evaluate_js(self, *_args, **_kwargs):
        return ToolkitResult(success=True, data=self._data)


class _SnapshotToolkit:
    fast_mode = False
    page = None

    def __init__(self, url: str, title: str, html: str):
        self._url = url
        self._title = title
        self.html = html

    async def get_current_url(self):
        return ToolkitResult(success=True, data=self._url)

    async def get_title(self):
        return ToolkitResult(success=True, data=self._title)

    async def get_page_html(self):
        return ToolkitResult(success=True, data=self.html)


class _RefClickToolkit:
    fast_mode = False
    page = None

    def __init__(self):
        self.clicked_ref = ""

    async def click_ref(self, ref: str):
        self.clicked_ref = ref
        return ToolkitResult(success=True)

    async def click(self, _selector, timeout=None):
        del timeout
        return ToolkitResult(success=False, error="should not use selector click")

    async def locator_click(self, _selector, timeout=None):
        del timeout
        return ToolkitResult(success=False, error="should not use locator click")

    async def force_click(self, _selector, timeout=None):
        del timeout
        return ToolkitResult(success=False, error="should not use force click")


class _SemanticSnapshotToolkit:
    fast_mode = False
    page = None

    def __init__(self, snapshots, html: str = "<html><body></body></html>", url: str = "https://www.bing.com/search?q=test", title: str = "Search"):
        self._snapshots = list(snapshots)
        self._html = html
        self._url = url
        self._title = title

    async def semantic_snapshot(self, max_elements=80, include_cards=True):
        del max_elements, include_cards
        current = self._snapshots[0]
        if len(self._snapshots) > 1:
            self._snapshots.pop(0)
        return ToolkitResult(success=True, data=current)

    async def get_current_url(self):
        return ToolkitResult(success=True, data=self._url)

    async def get_title(self):
        return ToolkitResult(success=True, data=self._title)

    async def get_page_html(self):
        return ToolkitResult(success=True, data=self._html)

    async def wait_for_text_appear(self, _text, timeout=None):
        del timeout
        return ToolkitResult(success=False, error="text not found")

    def resolve_ref(self, _ref: str):
        return {"selector": "a.result"}

    async def screenshot(self, full_page=False):
        del full_page
        return ToolkitResult(success=True, data=b"fake-png")


def test_infer_task_intent_treats_direct_url_as_read_only():
    agent = BrowserAgent(llm_client=FailingIntentLLM(), headless=True)

    intent = asyncio.run(agent._infer_task_intent("https://www.weather.com.cn/weather/101220101.shtml"))

    assert intent.intent_type == "read"
    assert intent.requires_interaction is False


def test_derive_primary_query_compresses_weather_browser_demo_instruction():
    agent = BrowserAgent(headless=True)

    query = agent._derive_primary_query("查询合肥明天的天气，并完整展示浏览器操作过程。请先打开浏览器再访问页面。")

    assert query == "合肥 明天 天气"


def test_refine_search_query_strips_instructional_sentence_into_short_query():
    agent = BrowserAgent(headless=True)

    query = agent._refine_search_query(
        "browser task",
        "has there been an official announcement about the death of Ali Khamenei in the last 14 days",
    )

    assert query == "death ali khamenei"


def test_refine_search_query_strips_browser_instruction_tail_in_chinese():
    agent = BrowserAgent(headless=True)

    query = agent._refine_search_query(
        "browser task",
        "openai api 等待页面完全加载后提取前3条结果并展示给用户",
    )

    assert query == "openai api"


def test_refine_search_query_strips_browser_task_prefix_and_render_steps():
    agent = BrowserAgent(headless=True)

    query = agent._refine_search_query(
        "browser task",
        "Browser task: OpenAI API pricing wait for rendering and extract top results",
    )

    assert query == "openai api pricing"

def test_refine_search_query_ignores_internal_source_hints_in_weather_tasks():
    agent = BrowserAgent(headless=True)

    query = agent._derive_primary_query(
        "Use weather.com.cn as the primary weather source for 合肥明天的天气详情. Extract temperature humidity wind and AQI."
    )

    assert query == "合肥 明天 天气"




def test_extract_data_for_intent_prefers_structured_search_results():
    toolkit = _SearchResultsToolkit(
        "https://www.bing.com/search?q=ali+khamenei+death",
        [
            {
                "title": "Reuters: Ali Khamenei appears in public after online death rumors",
                "text": "Recent public appearances indicate the reports of his death are false.",
                "link": "https://www.reuters.com/world/middle-east/khamenei-update",
                "source": "Reuters",
                "date": "2026-03-08",
            }
        ],
    )
    agent = BrowserAgent(headless=True, toolkit=toolkit)

    data = asyncio.run(
        agent._extract_data_for_intent(
            TaskIntent(intent_type="search", query="ali khamenei death", confidence=0.9)
        )
    )

    assert len(data) == 1
    assert data[0]["source"] == "Reuters"
    assert "reports of his death" in data[0]["text"]


def test_verify_action_effect_accepts_content_hash_change_without_url_change():
    toolkit = _SnapshotToolkit(
        "https://www.bing.com/search?q=ali+khamenei+death",
        "ali khamenei - Search",
        "<html><body>before</body></html>",
    )
    agent = BrowserAgent(headless=True, toolkit=toolkit)

    before = asyncio.run(agent._snapshot_page_state())
    toolkit.html = "<html><body>after search results updated</body></html>"
    success = asyncio.run(
        agent._verify_action_effect(
            before,
            BrowserAction(action_type=ActionType.CLICK, target_selector="a.result"),
        )
    )

    assert success is True


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


def test_find_search_result_click_action_prefers_semantic_card_ref():
    agent = BrowserAgent(headless=True)
    intent = TaskIntent(intent_type="search", query="ali khamenei death", confidence=0.9)
    snapshot = {
        "page_type": "serp",
        "cards": [
            {
                "ref": "card_1",
                "title": "Reuters: Public appearances indicate Khamenei is alive",
                "source": "Reuters",
                "host": "reuters.com",
                "snippet": "Recent public appearances indicate the reports are false.",
                "target_ref": "el_7",
                "target_selector": "a.result",
                "rank": 1,
            }
        ],
    }

    action = agent._find_search_result_click_action(
        "核实阿亚图拉阿里哈梅内伊是否在最近冲突中死亡",
        "https://www.bing.com/search?q=ali+khamenei+death",
        [],
        intent,
        snapshot=snapshot,
    )

    assert action is not None
    assert action.action_type == ActionType.CLICK
    assert action.target_ref == "el_7"
    assert action.expected_page_type == "detail"


def test_execute_action_prefers_click_ref_before_selector_click():
    toolkit = _RefClickToolkit()
    agent = BrowserAgent(headless=True, toolkit=toolkit)

    success = asyncio.run(
        agent._execute_action(
            BrowserAction(
                action_type=ActionType.CLICK,
                target_ref="el_7",
                target_selector="a.legacy",
                description="click strongest result",
            )
        )
    )

    assert success is True
    assert toolkit.clicked_ref == "el_7"


def test_action_from_llm_accepts_flat_action_payload():
    agent = BrowserAgent(headless=True)
    elements = [
        PageElement(
            index=7,
            tag="a",
            text="OpenClaw - GitHub",
            element_type="link",
            selector="div.g:nth-of-type(1) a",
            ref="el_7",
            attributes={"href": "https://github.com/openclaw"},
            is_visible=True,
            is_clickable=True,
        )
    ]

    action = agent._action_from_llm(
        {
            "action_type": "click",
            "target_ref": "el_7",
            "description": "open result",
            "confidence": 0.82,
            "expected_page_type": "detail",
        },
        elements,
    )

    assert action.action_type == ActionType.CLICK
    assert action.target_ref == "el_7"
    assert action.description == "open result"
    assert action.expected_page_type == "detail"
    assert action.confidence == 0.82


def test_verify_action_effect_accepts_expected_page_type_change():
    toolkit = _SemanticSnapshotToolkit(
        snapshots=[
            {"page_type": "serp", "cards": [{"ref": "card_1"}]},
            {"page_type": "detail", "cards": []},
        ]
    )
    agent = BrowserAgent(headless=True, toolkit=toolkit)

    before = asyncio.run(agent._snapshot_page_state())
    success = asyncio.run(
        agent._verify_action_effect(
            before,
            BrowserAction(
                action_type=ActionType.CLICK,
                target_ref="el_7",
                target_selector="a.result",
                expected_page_type="detail",
            ),
        )
    )

    assert success is True


def test_page_data_satisfies_goal_requires_target_count_on_list_page():
    agent = BrowserAgent(headless=True)
    snapshot = {
        "page_type": "list",
        "collections": [{"ref": "collection_1", "kind": "table", "item_count": 3}],
        "affordances": {"has_pagination": True, "next_page_ref": "ctl_next_page"},
    }

    satisfied = agent._page_data_satisfies_goal(
        "提取 10 条最新漏洞信息",
        "https://www.cnnvd.org.cn/web/xxk/ldxqById.tag",
        TaskIntent(intent_type="read", query="最新 漏洞", confidence=0.9),
        [{"title": "漏洞 1"}, {"title": "漏洞 2"}, {"title": "漏洞 3"}],
        snapshot=snapshot,
    )

    assert satisfied is False


def test_choose_snapshot_navigation_action_prefers_load_more_for_list_pages():
    agent = BrowserAgent(headless=True)
    snapshot = {
        "page_type": "list",
        "collections": [{"ref": "collection_1", "kind": "list", "item_count": 5}],
        "affordances": {
            "has_load_more": True,
            "load_more_ref": "ctl_load_more",
            "load_more_selector": "button.load-more",
        },
    }

    action = agent._choose_snapshot_navigation_action(
        "提取 10 条最新文章",
        "https://example.com/news",
        [],
        TaskIntent(intent_type="read", query="最新 文章", confidence=0.9),
        [{"title": f"文章 {idx}"} for idx in range(1, 4)],
        snapshot=snapshot,
    )

    assert action is not None
    assert action.action_type == ActionType.CLICK
    assert action.target_ref == "ctl_load_more"


def test_choose_snapshot_navigation_action_prefers_modal_primary_control():
    agent = BrowserAgent(headless=True)
    snapshot = {
        "page_type": "modal",
        "controls": [
            {"ref": "ctl_modal_primary", "kind": "modal_primary", "text": "Accept", "selector": "button.accept"},
        ],
        "affordances": {
            "has_modal": True,
            "modal_primary_ref": "ctl_modal_primary",
            "modal_primary_selector": "button.accept",
        },
    }

    action = agent._choose_snapshot_navigation_action(
        "继续打开页面并提取结果",
        "https://www.bing.com/search?q=openai+api",
        [],
        TaskIntent(intent_type="read", query="openai api", confidence=0.9),
        [],
        snapshot=snapshot,
    )

    assert action is not None
    assert action.action_type == ActionType.CLICK
    assert action.target_ref == "ctl_modal_primary"


def test_choose_snapshot_navigation_action_can_use_modal_region_elements():
    agent = BrowserAgent(headless=True)
    snapshot = {
        "page_type": "modal",
        "affordances": {
            "has_modal": True,
        },
    }
    elements = [
        PageElement(
            index=0,
            tag="button",
            text="Accept",
            element_type="button",
            selector="button.accept",
            ref="el_accept",
            region="modal",
            is_visible=True,
            is_clickable=True,
        )
    ]

    action = agent._choose_snapshot_navigation_action(
        "继续打开页面并提取结果",
        "https://www.bing.com/search?q=openai+api",
        elements,
        TaskIntent(intent_type="read", query="openai api", confidence=0.9),
        [],
        snapshot=snapshot,
    )

    assert action is not None
    assert action.action_type == ActionType.CLICK
    assert action.target_ref == "el_accept"


def test_choose_snapshot_navigation_action_ignores_non_actionable_modal_flag():
    agent = BrowserAgent(headless=True)
    snapshot = {
        "page_type": "list",
        "affordances": {
            "has_modal": True,
            "has_load_more": True,
            "load_more_ref": "ctl_load_more",
            "load_more_selector": "button.more",
        },
        "collections": [{"ref": "collection_1", "kind": "list", "item_count": 3}],
    }

    action = agent._choose_snapshot_navigation_action(
        "提取前 5 条结果",
        "https://example.com/news",
        [],
        TaskIntent(intent_type="read", query="新闻", confidence=0.9),
        [{"title": f"文章 {idx}"} for idx in range(1, 3)],
        snapshot=snapshot,
    )

    assert action is not None
    assert action.action_type == ActionType.CLICK
    assert action.target_ref == "ctl_load_more"


def test_verify_action_effect_accepts_item_count_growth_for_load_more():
    toolkit = _SemanticSnapshotToolkit(
        snapshots=[
            {
                "page_type": "list",
                "cards": [],
                "collections": [{"ref": "collection_1", "kind": "list", "item_count": 3}],
                "affordances": {"has_load_more": True},
            },
            {
                "page_type": "list",
                "cards": [],
                "collections": [{"ref": "collection_1", "kind": "list", "item_count": 8}],
                "affordances": {"has_load_more": True},
            },
        ],
        url="https://example.com/news",
        title="News",
    )
    agent = BrowserAgent(headless=True, toolkit=toolkit)

    before = asyncio.run(agent._snapshot_page_state())
    success = asyncio.run(
        agent._verify_action_effect(
            before,
            BrowserAction(
                action_type=ActionType.CLICK,
                target_ref="ctl_load_more",
                target_selector="button.load-more",
            ),
        )
    )

    assert success is True


def test_decide_action_with_vision_can_choose_semantic_ref():
    toolkit = _SemanticSnapshotToolkit(
        snapshots=[
            {
                "page_type": "unknown",
                "cards": [],
                "collections": [{"ref": "collection_1", "kind": "list", "item_count": 0}],
                "affordances": {"has_modal": False},
            }
        ],
        url="https://www.cnnvd.org.cn/",
        title="国家信息安全漏洞库",
    )
    agent = BrowserAgent(headless=True, toolkit=toolkit)
    agent._vision_llm = VisionDecisionLLM(
        {
            "confidence": 0.73,
            "action": {
                "type": "click",
                "target_ref": "el_9",
                "description": "open vulnerability list entry",
            },
        }
    )
    elements = [
        PageElement(
            index=0,
            tag="a",
            text="漏洞列表",
            element_type="link",
            selector="a.vuln-list",
            ref="el_9",
            attributes={"href": "https://www.cnnvd.org.cn/web/vulnerability/querylist.tag"},
            is_visible=True,
            is_clickable=True,
        )
    ]

    action = asyncio.run(
        agent._decide_action_with_vision(
            "进入漏洞列表并提取 3 条最新漏洞",
            "https://www.cnnvd.org.cn/",
            "国家信息安全漏洞库",
            elements,
            TaskIntent(intent_type="navigate", query="漏洞 列表 最新 漏洞", confidence=0.9),
            [],
            snapshot=toolkit._snapshots[0],
        )
    )

    assert action is not None
    assert action.action_type == ActionType.CLICK
    assert action.target_ref == "el_9"


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


def test_build_budgeted_browser_prompt_context_respects_budget_and_controls():
    agent = BrowserAgent(headless=True, toolkit=_SemanticSnapshotToolkit(snapshots=[{}]))
    snapshot = {
        "controls": [
            {"ref": "ctl_next", "kind": "next_page", "text": "Next", "selector": ".pagination .next"},
            {"ref": "ctl_more", "kind": "load_more", "text": "Load more results", "selector": "button.more"},
        ],
        "collections": [
            {
                "ref": "collection_1",
                "kind": "list",
                "item_count": 42,
                "sample_items": ["alpha", "beta", "gamma"],
            }
        ],
    }
    cards = [
        SearchResultCard(
            ref=f"card_{index}",
            title=f"Result {index} " + ("x" * 120),
            source="Example Source",
            host="example.com",
            snippet="snippet " * 40,
            target_ref=f"el_{index}",
            rank=index,
        )
        for index in range(1, 9)
    ]
    rendered, report = agent._build_budgeted_browser_prompt_context(
        task="extract 5 results and continue pagination if needed",
        current_url="https://example.com/list",
        data=[{"title": "Example title", "text": "body " * 80, "link": "https://example.com/a"}],
        cards=cards,
        snapshot=snapshot,
        elements_text="\n".join(
            f"[{index}] type=link ref=el_{index} selector=a.item-{index} info={'y' * 180}"
            for index in range(1, 18)
        ),
        total_tokens=450,
    )

    total_used = sum(
        int(report[name]["used_tokens"])
        for name in ("data", "cards", "collections", "controls", "elements")
    )
    assert total_used <= 450
    assert "ctl_next" in rendered["controls"]
    assert rendered["context_coverage"]


def test_looks_like_blocked_page_does_not_flag_plain_ok_html_without_denial_signals():
    assert BrowserAgent._looks_like_blocked_page(
        "https://example.com/ok.html",
        "Normal landing page",
    ) is False


def test_looks_like_blocked_page_flags_ok_html_with_forbidden_title():
    assert BrowserAgent._looks_like_blocked_page(
        "https://www.weather.com.cn/ok.html",
        "403 Forbidden",
    ) is True
