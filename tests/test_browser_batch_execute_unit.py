"""Unit tests for P4: Browser batch execution and correction."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agents.browser_action_sequence import (
    ActionSequence,
    DomCheckpoint,
    SequenceAction,
    VerifyResult,
    _parse_actions,
    _parse_dom_checkpoint,
    generate_action_sequence,
    visual_verify,
    plan_correction,
)
from utils.dom_checkpoint import (
    CheckpointResult,
    verify_dom_checkpoint,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── ActionSequence data structure tests ──────────────────────


class TestActionSequence:
    def test_empty_sequence(self):
        seq = ActionSequence()
        assert seq.is_complete()
        assert seq.current_action() is None
        assert seq.remaining() == []
        assert seq.executed() == []

    def test_advance_through_sequence(self):
        actions = [
            SequenceAction(action_type="input", target_ref="el_1", value="admin"),
            SequenceAction(action_type="input", target_ref="el_2", value="pass"),
            SequenceAction(action_type="click", target_ref="el_3"),
        ]
        seq = ActionSequence(actions=actions)
        assert not seq.is_complete()
        assert seq.current_action() == actions[0]
        assert len(seq.remaining()) == 3
        assert len(seq.executed()) == 0

        seq.advance()
        assert seq.current_action() == actions[1]
        assert len(seq.remaining()) == 2
        assert len(seq.executed()) == 1

        seq.advance()
        assert seq.current_action() == actions[2]

        seq.advance()
        assert seq.is_complete()
        assert seq.current_action() is None
        assert len(seq.executed()) == 3

    def test_to_dict_roundtrip(self):
        seq = ActionSequence(
            actions=[SequenceAction(
                action_type="click", target_ref="el_1",
                description="click button",
                dom_checkpoint=DomCheckpoint(check_type="url_change", expected_value="/dashboard"),
            )],
            goal_description="test goal",
            expected_outcome="should see dashboard",
        )
        d = seq.to_dict()
        assert d["goal_description"] == "test goal"
        assert len(d["actions"]) == 1
        assert d["actions"][0]["dom_checkpoint"]["type"] == "url_change"

    def test_format_executed_for_prompt(self):
        actions = [
            SequenceAction(action_type="input", description="type username"),
            SequenceAction(action_type="click", description="click login"),
        ]
        seq = ActionSequence(actions=actions)
        assert seq.format_executed_for_prompt() == "(none)"
        seq.advance()
        result = seq.format_executed_for_prompt()
        assert "input" in result
        assert "type username" in result


# ── Parsing tests ────────────────────────────────────────────


class TestParsing:
    def test_parse_dom_checkpoint_none(self):
        cp = _parse_dom_checkpoint(None)
        assert cp.check_type == "none"

    def test_parse_dom_checkpoint_valid(self):
        cp = _parse_dom_checkpoint({
            "type": "value_change",
            "target_ref": "el_1",
            "expected_value": "admin",
        })
        assert cp.check_type == "value_change"
        assert cp.target_ref == "el_1"
        assert cp.expected_value == "admin"

    def test_parse_actions_empty(self):
        assert _parse_actions(None, 10) == []
        assert _parse_actions("not a list", 10) == []
        assert _parse_actions([], 10) == []

    def test_parse_actions_valid(self):
        raw = [
            {"type": "input", "target_ref": "el_1", "value": "admin", "description": "username"},
            {"type": "click", "target_ref": "el_2", "description": "submit"},
        ]
        actions = _parse_actions(raw, 10)
        assert len(actions) == 2
        assert actions[0].action_type == "input"
        assert actions[0].value == "admin"
        assert actions[1].action_type == "click"

    def test_parse_actions_respects_max(self):
        raw = [{"type": "click", "target_ref": f"el_{i}"} for i in range(20)]
        actions = _parse_actions(raw, 5)
        assert len(actions) == 5

    def test_parse_actions_skips_invalid(self):
        raw = [
            {"type": "", "target_ref": "el_1"},
            "not a dict",
            {"type": "click", "target_ref": "el_2"},
        ]
        actions = _parse_actions(raw, 10)
        assert len(actions) == 1
        assert actions[0].target_ref == "el_2"


# ── DOM Checkpoint tests ─────────────────────────────────────


class TestDomCheckpoint:
    def test_none_checkpoint_passes(self):
        result = _run(verify_dom_checkpoint(None, None))
        assert result.passed

    def test_none_type_passes(self):
        cp = DomCheckpoint(check_type="none")
        result = _run(verify_dom_checkpoint(MagicMock(), cp))
        assert result.passed

    def test_value_change_success(self):
        element = AsyncMock()
        element.is_editable = AsyncMock(return_value=True)
        element.input_value = AsyncMock(return_value="admin")
        page = AsyncMock()
        page.query_selector = AsyncMock(return_value=element)

        cp = DomCheckpoint(
            check_type="value_change",
            target_selector="#username",
            expected_value="admin",
        )
        result = _run(verify_dom_checkpoint(page, cp))
        assert result.passed

    def test_value_change_failure(self):
        element = AsyncMock()
        element.is_editable = AsyncMock(return_value=True)
        element.input_value = AsyncMock(return_value="")
        page = AsyncMock()
        page.query_selector = AsyncMock(return_value=element)

        cp = DomCheckpoint(
            check_type="value_change",
            target_selector="#username",
            expected_value="admin",
        )
        result = _run(verify_dom_checkpoint(page, cp))
        assert not result.passed

    def test_url_change_success(self):
        page = AsyncMock()
        page.url = "https://example.com/dashboard"

        cp = DomCheckpoint(
            check_type="url_change",
            expected_value="/dashboard",
        )
        result = _run(verify_dom_checkpoint(page, cp))
        assert result.passed

    def test_url_change_failure(self):
        page = AsyncMock()
        page.url = "https://example.com/login"

        cp = DomCheckpoint(
            check_type="url_change",
            expected_value="/dashboard",
        )
        result = _run(verify_dom_checkpoint(page, cp))
        assert not result.passed

    def test_element_appear_success(self):
        page = AsyncMock()
        page.query_selector = AsyncMock(return_value=MagicMock())

        cp = DomCheckpoint(
            check_type="element_appear",
            target_selector=".success-toast",
        )
        result = _run(verify_dom_checkpoint(page, cp))
        assert result.passed

    def test_element_appear_failure(self):
        page = AsyncMock()
        page.query_selector = AsyncMock(return_value=None)

        cp = DomCheckpoint(
            check_type="element_appear",
            target_selector=".success-toast",
        )
        result = _run(verify_dom_checkpoint(page, cp))
        assert not result.passed

    def test_text_appear_success(self):
        page = AsyncMock()
        page.text_content = AsyncMock(return_value="Welcome back, admin!")

        cp = DomCheckpoint(
            check_type="text_appear",
            text_contains="Welcome",
        )
        result = _run(verify_dom_checkpoint(page, cp))
        assert result.passed

    def test_element_disappear_success(self):
        page = AsyncMock()
        page.query_selector = AsyncMock(return_value=None)

        cp = DomCheckpoint(
            check_type="element_disappear",
            target_selector="#loading-spinner",
        )
        result = _run(verify_dom_checkpoint(page, cp))
        assert result.passed

    def test_unknown_type_passes(self):
        cp = DomCheckpoint(check_type="some_future_type")
        result = _run(verify_dom_checkpoint(MagicMock(), cp))
        assert result.passed

    def test_exception_treated_as_pass(self):
        page = AsyncMock()
        page.query_selector = AsyncMock(side_effect=Exception("network error"))

        cp = DomCheckpoint(
            check_type="value_change",
            target_selector="#field",
            expected_value="test",
        )
        result = _run(verify_dom_checkpoint(page, cp))
        assert result.passed


# ── LLM integration tests (mocked) ──────────────────────────


class TestGenerateActionSequence:
    def test_generate_success(self):
        mock_llm = MagicMock()
        mock_llm.achat = AsyncMock(return_value="mock response")
        mock_llm.parse_json_response = MagicMock(return_value={
            "goal_description": "login to the site",
            "expected_outcome": "should see dashboard after login",
            "actions": [
                {"type": "input", "target_ref": "el_1", "value": "admin", "description": "username"},
                {"type": "input", "target_ref": "el_2", "value": "pass123", "description": "password"},
                {"type": "click", "target_ref": "el_3", "description": "click login"},
            ],
        })

        with patch("agents.browser_action_sequence.SEQUENCE_PROMPT", "mock prompt {task} {page_context} {elements} {max_actions} {repeated_actions} {plan_context}"):
            seq = _run(generate_action_sequence(
                task="login",
                page_context="URL: http://localhost",
                elements_text="el_1: username, el_2: password, el_3: login button",
                llm=mock_llm,
            ))

        assert seq is not None
        assert len(seq.actions) == 3
        assert seq.goal_description == "login to the site"
        assert seq.actions[0].value == "admin"

    def test_generate_no_prompt(self):
        with patch("agents.browser_action_sequence.SEQUENCE_PROMPT", ""):
            seq = _run(generate_action_sequence(
                task="test", page_context="", elements_text="", llm=MagicMock(),
            ))
        assert seq is None

    def test_generate_llm_failure(self):
        mock_llm = MagicMock()
        mock_llm.achat = AsyncMock(side_effect=Exception("API error"))

        with patch("agents.browser_action_sequence.SEQUENCE_PROMPT", "prompt {task} {page_context} {elements} {max_actions} {repeated_actions} {plan_context}"):
            seq = _run(generate_action_sequence(
                task="test", page_context="", elements_text="", llm=mock_llm,
            ))
        assert seq is None


class TestVisualVerify:
    def test_verify_goal_achieved(self):
        mock_llm = MagicMock()
        mock_llm.achat = AsyncMock(return_value="mock")
        mock_llm.parse_json_response = MagicMock(return_value={
            "goal_achieved": True,
            "deviation": "none",
            "detail": "dashboard is visible",
        })

        with patch("agents.browser_action_sequence.VISUAL_VERIFY_PROMPT", "p {task} {expected_outcome} {executed_actions} {page_context} {vision_description}"):
            result = _run(visual_verify(
                task="login",
                expected_outcome="dashboard",
                executed_actions_summary="input, click",
                current_page_context="URL: /dashboard",
                vision_description="dashboard page",
                llm=mock_llm,
            ))

        assert result.goal_achieved
        assert result.deviation == "none"

    def test_verify_no_prompt(self):
        with patch("agents.browser_action_sequence.VISUAL_VERIFY_PROMPT", ""):
            result = _run(visual_verify(
                task="t", expected_outcome="", executed_actions_summary="",
                current_page_context="", vision_description="", llm=MagicMock(),
            ))
        assert result.goal_achieved


class TestPlanCorrection:
    def test_correction_success(self):
        mock_llm = MagicMock()
        mock_llm.achat = AsyncMock(return_value="mock")
        mock_llm.parse_json_response = MagicMock(return_value={
            "goal_description": "retry with correct field",
            "expected_outcome": "login success",
            "actions": [
                {"type": "input", "target_ref": "el_4", "value": "admin", "description": "correct username field"},
            ],
        })

        with patch("agents.browser_action_sequence.CORRECTION_PROMPT", "p {task} {original_actions} {failure_detail} {page_context} {elements} {max_actions} {repeated_actions}"):
            seq = _run(plan_correction(
                task="login",
                original_sequence_summary="tried el_1",
                failure_detail="wrong field",
                page_context="URL: /login",
                elements_text="el_4: username",
                llm=mock_llm,
            ))

        assert seq is not None
        assert len(seq.actions) == 1
        assert seq.actions[0].target_ref == "el_4"
