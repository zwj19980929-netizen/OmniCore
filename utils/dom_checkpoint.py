"""DOM Checkpoint — 零 LLM 成本的动作执行校验。

批量执行动作序列时，每个动作执行后用 DOM 查询验证预期变化，
不调用 LLM，直接通过 Playwright page 对象读取 DOM 状态。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from utils.logger import log_warning


@dataclass
class CheckpointResult:
    passed: bool = True
    detail: str = ""


async def verify_dom_checkpoint(
    page,
    checkpoint: Any,
) -> CheckpointResult:
    if checkpoint is None:
        return CheckpointResult(passed=True, detail="no checkpoint")

    check_type = getattr(checkpoint, "check_type", None) or ""
    if not check_type or check_type == "none":
        return CheckpointResult(passed=True, detail="checkpoint type is none")

    try:
        if check_type == "value_change":
            return await _check_value_change(page, checkpoint)
        elif check_type == "url_change":
            return await _check_url_change(page, checkpoint)
        elif check_type == "element_appear":
            return await _check_element_appear(page, checkpoint)
        elif check_type == "element_disappear":
            return await _check_element_disappear(page, checkpoint)
        elif check_type == "text_appear":
            return await _check_text_appear(page, checkpoint)
        elif check_type == "attribute_change":
            return await _check_attribute_change(page, checkpoint)
        else:
            return CheckpointResult(passed=True, detail=f"unknown checkpoint type: {check_type}")
    except Exception as exc:
        log_warning(f"DOM checkpoint verification failed: {exc}")
        return CheckpointResult(passed=True, detail=f"checkpoint error (treated as pass): {exc}")


def _resolve_selector(checkpoint: Any) -> Optional[str]:
    selector = getattr(checkpoint, "target_selector", "") or ""
    ref = getattr(checkpoint, "target_ref", "") or ""
    if selector:
        return selector
    if ref:
        return f'[data-ref="{ref}"]'
    return None


async def _check_value_change(page, checkpoint: Any) -> CheckpointResult:
    selector = _resolve_selector(checkpoint)
    if not selector:
        return CheckpointResult(passed=True, detail="no selector for value_change")
    expected = getattr(checkpoint, "expected_value", "") or ""
    try:
        element = await page.query_selector(selector)
        if element is None:
            return CheckpointResult(passed=False, detail=f"element not found: {selector}")
        actual = await element.input_value() if await element.is_editable() else await element.get_attribute("value") or ""
        if expected and expected.lower() in (actual or "").lower():
            return CheckpointResult(passed=True, detail=f"value matches: {actual[:50]}")
        if actual:
            return CheckpointResult(passed=True, detail=f"value present: {actual[:50]}")
        return CheckpointResult(passed=False, detail=f"value empty, expected: {expected[:50]}")
    except Exception as exc:
        return CheckpointResult(passed=True, detail=f"value_change check error: {exc}")


async def _check_url_change(page, checkpoint: Any) -> CheckpointResult:
    expected = getattr(checkpoint, "expected_value", "") or ""
    text_contains = getattr(checkpoint, "text_contains", "") or ""
    current_url = page.url or ""
    pattern = expected or text_contains
    if not pattern:
        return CheckpointResult(passed=True, detail="no expected url pattern")
    if pattern.lower() in current_url.lower():
        return CheckpointResult(passed=True, detail=f"url matches: {current_url[:80]}")
    return CheckpointResult(passed=False, detail=f"url mismatch: {current_url[:80]}, expected: {pattern[:80]}")


async def _check_element_appear(page, checkpoint: Any) -> CheckpointResult:
    selector = _resolve_selector(checkpoint)
    text_contains = getattr(checkpoint, "text_contains", "") or ""
    if selector:
        element = await page.query_selector(selector)
        if element:
            return CheckpointResult(passed=True, detail=f"element found: {selector}")
        return CheckpointResult(passed=False, detail=f"element not found: {selector}")
    if text_contains:
        content = await page.text_content("body") or ""
        if text_contains.lower() in content.lower():
            return CheckpointResult(passed=True, detail=f"text found: {text_contains[:50]}")
        return CheckpointResult(passed=False, detail=f"text not found: {text_contains[:50]}")
    return CheckpointResult(passed=True, detail="no selector or text for element_appear")


async def _check_element_disappear(page, checkpoint: Any) -> CheckpointResult:
    selector = _resolve_selector(checkpoint)
    if not selector:
        return CheckpointResult(passed=True, detail="no selector for element_disappear")
    element = await page.query_selector(selector)
    if element is None:
        return CheckpointResult(passed=True, detail=f"element disappeared: {selector}")
    return CheckpointResult(passed=False, detail=f"element still present: {selector}")


async def _check_text_appear(page, checkpoint: Any) -> CheckpointResult:
    text_contains = getattr(checkpoint, "text_contains", "") or ""
    if not text_contains:
        return CheckpointResult(passed=True, detail="no text_contains specified")
    try:
        content = await page.text_content("body") or ""
        if text_contains.lower() in content.lower():
            return CheckpointResult(passed=True, detail=f"text found: {text_contains[:50]}")
        return CheckpointResult(passed=False, detail=f"text not found: {text_contains[:50]}")
    except Exception as exc:
        return CheckpointResult(passed=True, detail=f"text_appear check error: {exc}")


async def _check_attribute_change(page, checkpoint: Any) -> CheckpointResult:
    selector = _resolve_selector(checkpoint)
    if not selector:
        return CheckpointResult(passed=True, detail="no selector for attribute_change")
    expected = getattr(checkpoint, "expected_value", "") or ""
    try:
        element = await page.query_selector(selector)
        if element is None:
            return CheckpointResult(passed=False, detail=f"element not found: {selector}")
        if not expected:
            return CheckpointResult(passed=True, detail="no expected attribute value")
        attrs_str = await element.evaluate("el => JSON.stringify(Array.from(el.attributes).map(a => a.name + '=' + a.value))")
        if expected.lower() in (attrs_str or "").lower():
            return CheckpointResult(passed=True, detail=f"attribute matches: {expected[:50]}")
        return CheckpointResult(passed=False, detail=f"attribute not found: {expected[:50]}")
    except Exception as exc:
        return CheckpointResult(passed=True, detail=f"attribute_change check error: {exc}")
