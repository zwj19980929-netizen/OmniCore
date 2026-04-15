"""P0: 浏览器步骤指纹去重单元测试."""
from agents.browser_agent import ActionType, BrowserAction, BrowserAgent, PageElement, TaskIntent
from config.settings import settings


def _make_agent() -> BrowserAgent:
    agent = BrowserAgent(headless=True)
    agent.decision.reset_history()
    return agent


def _click_action(selector: str = "#btn", value: str = "") -> BrowserAction:
    return BrowserAction(
        action_type=ActionType.CLICK,
        target_selector=selector,
        value=value,
        description="click",
        confidence=0.8,
    )


def test_fingerprint_stable_under_same_url_and_stage():
    agent = _make_agent()
    decision = agent.decision
    action = _click_action()
    fp1 = decision._fingerprint_action(action, "https://example.com/search", "serp")
    fp2 = decision._fingerprint_action(action, "https://example.com/search?q=1", "serp")
    assert fp1 != ""
    # Query string variation should still match on path
    assert fp1 == fp2


def test_fingerprint_differs_by_page_stage():
    agent = _make_agent()
    decision = agent.decision
    action = _click_action()
    fp_serp = decision._fingerprint_action(action, "https://example.com/x", "serp")
    fp_detail = decision._fingerprint_action(action, "https://example.com/x", "detail")
    assert fp_serp != fp_detail


def test_second_repeat_is_rejected_by_sanitize():
    agent = _make_agent()
    decision = agent.decision
    # Simulate two prior executions of the same action at the same (url, stage)
    fp = decision._fingerprint_action(_click_action(), "https://example.com/page", "interacting")
    decision._step_fingerprints[fp] = settings.BROWSER_DEDUP_THRESHOLD

    sanitized = agent._sanitize_planned_action(
        "task",
        "https://example.com/page",
        [PageElement(index=0, tag="button", text="Go", element_type="button",
                     selector="#btn", attributes={}, is_visible=True, is_clickable=True)],
        TaskIntent(intent_type="navigate", query="", confidence=0.5),
        [],
        _click_action(),
        snapshot={"page_type": "detail", "page_stage": "interacting"},
    )
    assert sanitized is None


def test_different_stage_same_action_is_not_rejected():
    agent = _make_agent()
    decision = agent.decision
    fp = decision._fingerprint_action(_click_action(), "https://example.com/page", "serp")
    decision._step_fingerprints[fp] = settings.BROWSER_DEDUP_THRESHOLD

    sanitized = agent._sanitize_planned_action(
        "task",
        "https://example.com/page",
        [PageElement(index=0, tag="button", text="Go", element_type="button",
                     selector="#btn", attributes={}, is_visible=True, is_clickable=True)],
        TaskIntent(intent_type="navigate", query="", confidence=0.5),
        [],
        _click_action(),
        snapshot={"page_type": "detail", "page_stage": "interacting"},
    )
    assert sanitized is not None


def test_extract_and_done_actions_are_not_deduped():
    agent = _make_agent()
    decision = agent.decision
    extract = BrowserAction(action_type=ActionType.EXTRACT, description="extract", confidence=0.9)
    fp = decision._fingerprint_action(extract, "https://example.com/page", "interacting")
    decision._step_fingerprints[fp] = 10  # way past threshold

    sanitized = agent._sanitize_planned_action(
        "task", "https://example.com/page", [],
        TaskIntent(intent_type="read", query="", confidence=0.9),
        [{"title": "a"}], extract,
        snapshot={"page_type": "detail", "page_stage": "interacting"},
    )
    assert sanitized is not None


def test_record_action_populates_fingerprint_memory():
    agent = _make_agent()
    decision = agent.decision

    class _FakeObs:
        url = "https://example.com/q"

    decision.last_observation = _FakeObs()
    decision.last_semantic_snapshot = {"page_stage": "interacting"}
    decision.record_action(_click_action())
    decision.record_action(_click_action())
    assert any(count >= 2 for count in decision._step_fingerprints.values())


def test_fingerprint_memory_is_bounded():
    agent = _make_agent()
    decision = agent.decision
    cap = settings.BROWSER_STEP_MEMORY_SIZE

    class _FakeObs:
        url = "https://example.com/q"

    decision.last_observation = _FakeObs()
    decision.last_semantic_snapshot = {"page_stage": "interacting"}
    for i in range(cap + 10):
        decision.record_action(_click_action(selector=f"#btn-{i}"))
    assert len(decision._step_fingerprints) <= cap


def test_format_repeated_actions_lists_blacklist():
    agent = _make_agent()
    decision = agent.decision
    fp = decision._fingerprint_action(_click_action(), "https://example.com/p", "serp")
    decision._step_fingerprints[fp] = settings.BROWSER_DEDUP_THRESHOLD
    out = decision.format_repeated_actions_for_llm()
    assert "click" in out
