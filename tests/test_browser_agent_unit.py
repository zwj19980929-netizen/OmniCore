import asyncio

from agents.browser_agent import ActionType, BrowserAction, BrowserAgent, PageElement, TaskIntent


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
