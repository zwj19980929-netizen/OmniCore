"""
Unit tests for the two-tier vision client design.

Covers:
  * ``LLMClient.for_vision_high`` — uses ``VISION_MODEL_HIGH`` when set,
    transparently falls back to ``for_vision`` when empty.
  * ``BrowserPerceptionLayer.get_vision_llm_high`` — caches HIGH and
    default clients independently, falls back when HIGH init fails.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from core.llm import LLMClient


# ----------------------------------------------------------------------
# LLMClient.for_vision_high
# ----------------------------------------------------------------------


class TestForVisionHigh:
    def test_uses_vision_model_high_when_set(self, monkeypatch):
        from config.settings import settings
        monkeypatch.setattr(settings, "VISION_MODEL_HIGH", "openai/gpt-5")
        client = LLMClient.for_vision_high()
        assert client.model == "openai/gpt-5"

    def test_trims_whitespace(self, monkeypatch):
        from config.settings import settings
        monkeypatch.setattr(settings, "VISION_MODEL_HIGH", "  openai/gpt-5  ")
        client = LLMClient.for_vision_high()
        assert client.model == "openai/gpt-5"

    def test_falls_back_to_for_vision_when_empty(self, monkeypatch):
        from config.settings import settings
        monkeypatch.setattr(settings, "VISION_MODEL_HIGH", "")
        sentinel = MagicMock(spec=LLMClient)
        with patch.object(LLMClient, "for_vision", return_value=sentinel) as mock_for_vision:
            client = LLMClient.for_vision_high()
        assert client is sentinel
        mock_for_vision.assert_called_once()

    def test_falls_back_when_attr_missing(self, monkeypatch):
        """Defensive path: if settings somehow lacks the attribute entirely,
        ``getattr`` returns ``""`` and we degrade cleanly to for_vision."""
        from config.settings import settings
        # Delete attribute for this test
        if hasattr(settings, "VISION_MODEL_HIGH"):
            monkeypatch.delattr(settings, "VISION_MODEL_HIGH", raising=False)
        sentinel = MagicMock(spec=LLMClient)
        with patch.object(LLMClient, "for_vision", return_value=sentinel):
            client = LLMClient.for_vision_high()
        assert client is sentinel


# ----------------------------------------------------------------------
# BrowserPerceptionLayer vision-client caching
# ----------------------------------------------------------------------


class TestPerceptionVisionTiers:
    def _make_layer(self):
        # The layer's constructor requires toolkit + a11y + perceiver — we
        # only exercise vision-client getters, so stubs are sufficient.
        from agents.browser_perception import BrowserPerceptionLayer
        layer = BrowserPerceptionLayer.__new__(BrowserPerceptionLayer)
        layer.name = "test"
        layer._vision_llm = None
        layer._vision_llm_attempted = False
        layer._vision_llm_unavailable_logged = False
        layer._vision_llm_high = None
        layer._vision_llm_high_attempted = False
        layer._vision_llm_high_unavailable_logged = False
        return layer

    def test_default_and_high_are_cached_independently(self):
        layer = self._make_layer()
        default_sentinel = MagicMock(spec=LLMClient)
        high_sentinel = MagicMock(spec=LLMClient)
        with patch.object(LLMClient, "for_vision", return_value=default_sentinel), \
             patch.object(LLMClient, "for_vision_high", return_value=high_sentinel):
            assert layer.get_vision_llm() is default_sentinel
            assert layer.get_vision_llm_high() is high_sentinel
            # Second call reuses the cached instances.
            assert layer.get_vision_llm() is default_sentinel
            assert layer.get_vision_llm_high() is high_sentinel

    def test_high_falls_back_when_init_raises(self):
        layer = self._make_layer()
        default_sentinel = MagicMock(spec=LLMClient)
        with patch.object(LLMClient, "for_vision", return_value=default_sentinel), \
             patch.object(LLMClient, "for_vision_high", side_effect=RuntimeError("boom")):
            result = layer.get_vision_llm_high()
        # HIGH failed → fell through to default tier.
        assert result is default_sentinel
        # Subsequent calls don't retry HIGH init.
        with patch.object(LLMClient, "for_vision_high") as mock_high:
            layer.get_vision_llm_high()
            mock_high.assert_not_called()

    def test_high_unavailable_does_not_poison_default(self):
        """After HIGH init fails, the default tier must still be reachable.

        The HIGH getter falls through to ``get_vision_llm`` on failure —
        which itself populates the default slot. We assert that post-
        fallback, the default slot is a real (non-None) client and the
        ``_unavailable_logged`` flag for the default tier is still clean.
        """
        layer = self._make_layer()
        default_sentinel = MagicMock(spec=LLMClient)
        with patch.object(LLMClient, "for_vision_high", side_effect=RuntimeError("boom")), \
             patch.object(LLMClient, "for_vision", return_value=default_sentinel):
            # HIGH fails, falls through to default getter which succeeds.
            result = layer.get_vision_llm_high()
        assert result is default_sentinel
        # Default slot populated, default-tier unavailable flag never tripped.
        assert layer._vision_llm is default_sentinel
        assert layer._vision_llm_unavailable_logged is False
        # And the HIGH slot is flagged so we won't retry it repeatedly.
        assert layer._vision_llm_high_attempted is True
        assert layer._vision_llm_high_unavailable_logged is True
