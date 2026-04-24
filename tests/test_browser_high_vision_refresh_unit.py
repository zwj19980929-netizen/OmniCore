"""
Unit tests for :meth:`BrowserAgent._refresh_vision_with_high_tier`.

The refresh is the pre-sequence HIGH-tier vision read added in stage 6.
It mutates the current observation in place and recomputes
``perception_gaps`` against the latest DOM. Tests drive the helper in
isolation by constructing a bare ``BrowserAgent`` (``__new__``) with just
the attributes the method touches.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.browser_agent import BrowserAgent, PageObservation
from utils.gap_tracker import GapTracker


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_agent(observation):
    agent = BrowserAgent.__new__(BrowserAgent)
    agent.name = "test"
    agent._last_observation = observation
    agent.perception = MagicMock()
    # Refresh now annotates gaps via the tracker; provide a fresh one so
    # streak lookups don't error out on bare test agents.
    agent._gap_tracker = GapTracker()
    return agent


class TestRefreshVisionWithHighTier:
    def test_updates_observation_when_high_returns_data(self):
        obs = PageObservation(
            snapshot={"elements": [
                {"role": "button", "tag": "button", "text": "Submit", "selector": "#a"},
            ]},
            vision_description="old",
            vision_controls=[{"role": "button", "label": "Submit"}],
            perception_gaps=[],
        )
        agent = _make_agent(obs)
        agent.perception.get_vision_description_high = AsyncMock(return_value=(
            "fresh HIGH summary",
            [
                {"role": "button", "label": "Submit"},
                {"role": "checkbox", "label": "I agree to terms"},
            ],
        ))

        result = _run(agent._refresh_vision_with_high_tier(MagicMock()))

        assert result is True
        # Description replaced (wrapped via wrap_untrusted so contains the text)
        assert "fresh HIGH summary" in obs.vision_description
        # Controls replaced
        assert obs.vision_controls == [
            {"role": "button", "label": "Submit"},
            {"role": "checkbox", "label": "I agree to terms"},
        ]
        # Recomputed gaps: Submit button matches the DOM, checkbox is a gap.
        assert len(obs.perception_gaps) == 1
        assert obs.perception_gaps[0]["role"] == "checkbox"

    def test_noop_when_high_returns_empty(self):
        obs = PageObservation(
            snapshot={"elements": []},
            vision_description="untouched",
            vision_controls=[{"role": "button", "label": "x"}],
            perception_gaps=[{"role": "button", "label": "x"}],
        )
        agent = _make_agent(obs)
        agent.perception.get_vision_description_high = AsyncMock(return_value=("", []))

        result = _run(agent._refresh_vision_with_high_tier(MagicMock()))

        assert result is False
        assert obs.vision_description == "untouched"
        assert obs.vision_controls == [{"role": "button", "label": "x"}]
        assert obs.perception_gaps == [{"role": "button", "label": "x"}]

    def test_swallows_exceptions(self):
        obs = PageObservation(
            vision_description="keep me",
            vision_controls=[{"role": "x", "label": "y"}],
        )
        agent = _make_agent(obs)
        agent.perception.get_vision_description_high = AsyncMock(
            side_effect=RuntimeError("vision died"),
        )

        result = _run(agent._refresh_vision_with_high_tier(MagicMock()))

        assert result is False
        # Original observation intact.
        assert obs.vision_description == "keep me"
        assert obs.vision_controls == [{"role": "x", "label": "y"}]

    def test_noop_when_no_observation(self):
        agent = _make_agent(None)
        agent.perception.get_vision_description_high = AsyncMock(return_value=(
            "x", [{"role": "button", "label": "y"}],
        ))

        result = _run(agent._refresh_vision_with_high_tier(MagicMock()))

        assert result is False
        # Method shouldn't even have been called — nothing to update into.
        agent.perception.get_vision_description_high.assert_not_called()

    def test_noop_when_page_is_none(self):
        obs = PageObservation()
        agent = _make_agent(obs)
        agent.perception.get_vision_description_high = AsyncMock()

        result = _run(agent._refresh_vision_with_high_tier(None))

        assert result is False
        agent.perception.get_vision_description_high.assert_not_called()

    def test_partial_returns_summary_only_keeps_old_controls(self):
        """If HIGH returns a summary but no controls, we keep the old
        controls list rather than wiping it — partial data is still data."""
        obs = PageObservation(
            snapshot={"elements": [
                {"role": "button", "tag": "button", "text": "OK", "selector": "#b"},
            ]},
            vision_description="old",
            vision_controls=[{"role": "button", "label": "OK"}],
            perception_gaps=[],
        )
        agent = _make_agent(obs)
        agent.perception.get_vision_description_high = AsyncMock(return_value=(
            "only a summary",
            [],
        ))

        result = _run(agent._refresh_vision_with_high_tier(MagicMock()))

        assert result is True
        assert "only a summary" in obs.vision_description
        # Controls preserved
        assert obs.vision_controls == [{"role": "button", "label": "OK"}]

    def test_partial_returns_controls_only_keeps_old_summary(self):
        obs = PageObservation(
            snapshot={"elements": []},
            vision_description="keep summary",
            vision_controls=[],
            perception_gaps=[],
        )
        agent = _make_agent(obs)
        agent.perception.get_vision_description_high = AsyncMock(return_value=(
            "",
            [{"role": "button", "label": "new"}],
        ))

        result = _run(agent._refresh_vision_with_high_tier(MagicMock()))

        assert result is True
        assert obs.vision_description == "keep summary"
        assert obs.vision_controls == [{"role": "button", "label": "new"}]
        # Recomputed gaps: no DOM elements to match → the new control is a gap.
        assert len(obs.perception_gaps) == 1
