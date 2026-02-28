import asyncio

from agents.browser_agent import ActionType, BrowserAction, BrowserAgent, PageElement


class FakeSurface:
    def __init__(self, name: str):
        self.name = name
        self.url = f"https://example.com/{name}"

    async def goto(self, *_args, **_kwargs):
        return None

    async def title(self):
        return self.name

    async def wait_for_load_state(self, *_args, **_kwargs):
        return None


class SwitchTabProbeAgent(BrowserAgent):
    def __init__(self):
        super().__init__(headless=True)
        self.page_one = FakeSurface("p1")
        self.page_two = FakeSurface("p2")
        self.seen = []
        self.step_no = 0

    async def _create_page(self):
        self._page = self.page_one
        return self.page_one

    async def _wait_for_page_ready(self, _surface):
        return None

    async def _extract_interactive_elements(self, surface):
        self.seen.append(surface.name)
        return []

    def _decide_action_locally(self, _task, _page, _elements):
        self.step_no += 1
        if self.step_no == 1:
            return BrowserAction(action_type=ActionType.SWITCH_TAB, value="last", description="switch")
        return BrowserAction(action_type=ActionType.DONE, description="done")

    async def _execute_action(self, _page, action):
        if action.action_type == ActionType.SWITCH_TAB:
            self._page = self.page_two
        return True

    async def _snapshot_page_state(self, page):
        return {"url": page.url, "title": await page.title(), "content_len": 0}

    async def _verify_action_effect(self, _page, _before, _action):
        return True

    async def _maybe_extract_data(self, _surface):
        return []


class IframeProbeAgent(BrowserAgent):
    def __init__(self):
        super().__init__(headless=True)
        self.page = FakeSurface("page")
        self.frame = FakeSurface("frame")
        self.seen = []
        self.step_no = 0

    async def _create_page(self):
        self._page = self.page
        return self.page

    async def _wait_for_page_ready(self, _surface):
        return None

    async def _extract_interactive_elements(self, surface):
        self.seen.append(surface.name)
        return []

    def _decide_action_locally(self, _task, _page, _elements):
        self.step_no += 1
        if self.step_no == 1:
            return BrowserAction(action_type=ActionType.SWITCH_IFRAME, description="enter iframe")
        return BrowserAction(action_type=ActionType.DONE, description="done")

    async def _execute_action(self, _page, action):
        if action.action_type == ActionType.SWITCH_IFRAME:
            self._current_frame = self.frame
            self._in_iframe = True
        return True

    async def _snapshot_page_state(self, page):
        return {"url": page.url, "title": await page.title(), "content_len": 0}

    async def _verify_action_effect(self, _page, _before, _action):
        return True

    async def _maybe_extract_data(self, _surface):
        return []


class FakeLocator:
    def __init__(self, value: str):
        self._value = value
        self.first = self

    async def count(self):
        return 1

    async def input_value(self):
        return self._value

    async def evaluate(self, _script):
        return self._value


class FakeInputSurface:
    def __init__(self, value: str):
        self._value = value

    def locator(self, _selector):
        return FakeLocator(self._value)


class FakeContentSurface:
    def __init__(self, url: str, content: str):
        self.url = url
        self._content = content

    async def content(self):
        return self._content


class FakePageForVerify:
    def __init__(self):
        self.url = "https://example.com/form"

    async def content(self):
        return "<html></html>"

    async def title(self):
        return "same"


class FakeKeyboard:
    def __init__(self, calls):
        self.calls = calls

    async def press(self, key):
        self.calls.append(("keyboard", key))


class FakeClickPage:
    def __init__(self):
        self.calls = []
        self.keyboard = FakeKeyboard(self.calls)

    async def click(self, selector, timeout=None):
        self.calls.append(("click", selector, timeout))

    def locator(self, _selector):
        raise AssertionError("locator fallback should not run when direct click succeeds")


class FakeKeyOnlySurface:
    def __init__(self):
        self.calls = []

    async def click(self, selector, timeout=None):
        raise RuntimeError("direct click failed")

    def locator(self, _selector):
        raise RuntimeError("locator fallback failed")


class NoSemanticSurface:
    pass


class LLMGuard:
    def __init__(self):
        self.called = False

    async def achat(self, *_args, **_kwargs):
        self.called = True
        raise AssertionError("LLM should not be called")

    def parse_json_response(self, _response):
        return {}


def test_run_tracks_active_page_after_switch_tab():
    agent = SwitchTabProbeAgent()
    result = asyncio.run(agent.run("switch tab", start_url="https://example.com"))

    assert agent.seen == ["p1", "p2"]
    assert result["success"] is True
    assert result["url"] == "https://example.com/p2"


def test_run_extracts_elements_from_active_iframe():
    agent = IframeProbeAgent()
    result = asyncio.run(agent.run("iframe", start_url="https://example.com"))

    assert agent.seen == ["page", "frame"]
    assert result["success"] is True


def test_verify_action_effect_checks_input_value():
    agent = BrowserAgent(headless=True)
    page = FakePageForVerify()
    before = asyncio.run(agent._snapshot_page_state(page))

    agent._get_active_surface = lambda _page: FakeInputSurface("hello")  # type: ignore[method-assign]
    ok = asyncio.run(
        agent._verify_action_effect(
            page,
            before,
            BrowserAction(action_type=ActionType.INPUT, target_selector="#q", value="hello"),
        )
    )
    assert ok is True

    agent._get_active_surface = lambda _page: FakeInputSurface("wrong")  # type: ignore[method-assign]
    bad = asyncio.run(
        agent._verify_action_effect(
            page,
            before,
            BrowserAction(action_type=ActionType.INPUT, target_selector="#q", value="hello"),
        )
    )
    assert bad is False


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


def test_click_keyboard_fallback_runs_after_real_click():
    agent = BrowserAgent(headless=True)
    page = FakeClickPage()

    ok = asyncio.run(
        agent._try_click_with_fallbacks(
            page,
            "#btn",
            BrowserAction(
                action_type=ActionType.CLICK,
                target_selector="#btn",
                use_keyboard_fallback=True,
                keyboard_key="Enter",
            ),
        )
    )

    assert ok is True
    assert page.calls == [("click", "#btn", 5000)]


def test_click_keyboard_fallback_uses_active_page_keyboard_for_frames():
    agent = BrowserAgent(headless=True)
    page = FakeClickPage()
    surface = FakeKeyOnlySurface()
    agent._page = page

    ok = asyncio.run(
        agent._try_click_with_fallbacks(
            surface,
            "#btn",
            BrowserAction(
                action_type=ActionType.CLICK,
                target_selector="#btn",
                use_keyboard_fallback=True,
                keyboard_key="Enter",
            ),
        )
    )

    assert ok is True
    assert page.calls == [("keyboard", "Enter")]


def test_verify_action_effect_accepts_iframe_surface_change():
    agent = BrowserAgent(headless=True)
    page = FakePageForVerify()
    frame = FakeContentSurface("https://example.com/frame", "a" * 10)
    agent._current_frame = frame
    agent._in_iframe = True

    before = asyncio.run(agent._snapshot_page_state(page))
    frame._content = "b" * 200

    ok = asyncio.run(
        agent._verify_action_effect(
            page,
            before,
            BrowserAction(action_type=ActionType.CLICK, target_selector="#inside"),
        )
    )

    assert ok is True


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


def test_fill_form_rejects_non_object_payload():
    agent = BrowserAgent(headless=True)

    ok = asyncio.run(agent._fill_form(FakeInputSurface(""), '["not", "an", "object"]'))

    assert ok is False


def test_semantic_strategy_builders_require_supported_surface_methods():
    agent = BrowserAgent(headless=True)
    agent._element_cache = [
        PageElement(
            index=0,
            tag="button",
            text="Search",
            element_type="button",
            selector="#search",
            attributes={"labelText": "Search", "ariaLabel": "Search", "title": "Search"},
        ),
        PageElement(
            index=1,
            tag="input",
            text="",
            element_type="input",
            selector="#query",
            attributes={"placeholder": "Search", "labelText": "Query", "name": "q"},
        ),
    ]

    click_strategies = agent._build_semantic_click_strategies(NoSemanticSurface(), "#search")
    input_strategies = agent._build_semantic_input_strategies(NoSemanticSurface(), "#query", "ai")

    assert click_strategies == []
    assert input_strategies == []


def test_local_decision_supports_explicit_click_target():
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

    action = agent._decide_action_locally("点击帮助中心", FakeSurface("page"), elements)

    assert action is not None
    assert action.action_type == ActionType.CLICK
    assert action.target_selector == "a.help"


def test_decide_action_with_llm_skips_llm_when_no_elements_for_extract_task():
    guard = LLMGuard()
    agent = BrowserAgent(llm_client=guard, headless=True)

    action = asyncio.run(agent._decide_action_with_llm("提取当前页面信息", FakeSurface("page"), []))

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


def test_read_only_task_returns_initial_extract_without_steps():
    agent = BrowserAgent(headless=True)
    page = FakeSurface("page")

    async def _fake_create_page():
        agent._page = page
        return page

    async def _fake_wait(_surface):
        return None

    async def _fake_extract(_surface):
        return [{"text": "summary"}]

    agent._create_page = _fake_create_page  # type: ignore[method-assign]
    agent._wait_for_page_ready = _fake_wait  # type: ignore[method-assign]
    agent._maybe_extract_data = _fake_extract  # type: ignore[method-assign]

    result = asyncio.run(agent.run("读取这个页面内容", start_url="https://example.com"))

    assert result["success"] is True
    assert result["message"] == "read-only task satisfied from initial page"
    assert result["steps"] == []
    assert result["data"] == [{"text": "summary"}]


def test_looping_action_stops_repeated_non_read_only_task():
    agent = BrowserAgent(headless=True)
    page = FakeSurface("page")
    action = BrowserAction(action_type=ActionType.WAIT, value="1", description="wait")

    async def _fake_create_page():
        agent._page = page
        return page

    async def _fake_wait(_surface):
        return None

    async def _fake_extract_elements(_surface):
        return []

    async def _fake_execute(_page, _action):
        return True

    async def _fake_verify(_page, _before, _action):
        return True

    async def _fake_snapshot(_page):
        return {"url": page.url, "title": "page", "content_len": 0, "surface_url": page.url, "surface_content_len": 0}

    agent._create_page = _fake_create_page  # type: ignore[method-assign]
    agent._wait_for_page_ready = _fake_wait  # type: ignore[method-assign]
    agent._extract_interactive_elements = _fake_extract_elements  # type: ignore[method-assign]
    agent._decide_action_locally = lambda *_args: action  # type: ignore[method-assign]
    agent._execute_action = _fake_execute  # type: ignore[method-assign]
    agent._verify_action_effect = _fake_verify  # type: ignore[method-assign]
    agent._snapshot_page_state = _fake_snapshot  # type: ignore[method-assign]

    result = asyncio.run(agent.run("wait for something", start_url="https://example.com", max_steps=4))

    assert result["success"] is False
    assert "repeated action loop detected" in result["message"]


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
