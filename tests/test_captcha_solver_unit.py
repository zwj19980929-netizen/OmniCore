from types import SimpleNamespace
from unittest.mock import patch

from utils.captcha_solver import CaptchaSolver


def test_analyze_captcha_with_vision_falls_back_to_default_model():
    solver = CaptchaSolver()
    solver.vision_model = "bad-model"

    calls = []

    class _FakeLLMClient:
        def __init__(self, model=None, **kwargs):
            self._model = model

        def chat_with_image(self, prompt, image_bytes):
            calls.append(self._model)
            if self._model == "bad-model":
                raise Exception("invalid model ID")
            return SimpleNamespace(
                content='{"captcha_type":"text","solution":"AB12","confidence":0.91,"instructions":"ok"}'
            )

    with patch("core.llm.LLMClient", _FakeLLMClient):
        result = solver.analyze_captcha_with_vision("ZmFrZQ==")

    assert calls[:2] == ["bad-model", "gpt-4o"]
    assert result["solution"] == "AB12"
    assert result["confidence"] == 0.91


def test_solve_stops_immediately_when_vision_model_is_fatally_unavailable():
    solver = CaptchaSolver(toolkit=SimpleNamespace())
    refreshed = {"count": 0}

    async def _detect():
        return {"has_captcha": True, "captcha_type": "text"}

    async def _screenshot():
        return b"fake", None

    async def _refresh():
        refreshed["count"] += 1
        return True

    solver.detect_captcha = _detect
    solver.screenshot_captcha = _screenshot
    solver._try_refresh_captcha = _refresh
    solver.analyze_captcha_with_vision = lambda *_args, **_kwargs: {
        "captcha_type": "unknown",
        "solution": None,
        "confidence": 0,
        "instructions": "识别失败: invalid model ID",
        "fatal": True,
    }

    import asyncio

    result = asyncio.run(solver.solve(max_retries=3))

    assert result is False
    assert refreshed["count"] == 0


def test_screenshot_captcha_prefers_captcha_element_over_full_page():
    class _FakeToolkit:
        async def screenshot(self):
            return SimpleNamespace(success=True, data=b"full-page")

        async def screenshot_element(self, selector):
            assert selector == "#cap-img"
            return SimpleNamespace(success=True, data=b"captcha-only")

        async def get_bounding_box(self, selector):
            assert selector == "#cap-img"
            return SimpleNamespace(success=True, data={"x": 1, "y": 2, "width": 30, "height": 10})

    solver = CaptchaSolver(toolkit=_FakeToolkit())

    import asyncio

    screenshot, bounds = asyncio.run(solver.screenshot_captcha())

    assert screenshot == b"captcha-only"
    assert bounds == {"x": 1, "y": 2, "width": 30, "height": 10}
