"""Downstream filter for browser result.data — removes page_main_text noise when structured data exists."""
from __future__ import annotations
from typing import Any

from config.settings import settings

_FALLBACK_SOURCES = {"page_main_text", "page_main_text_fallback"}


def sanitize_browser_data(data: Any, max_fallback_len: int | None = None) -> Any:
    """Remove page_main_text fallback entries when structured data is present.

    If structured entries exist, all fallback entries are dropped.
    If only fallback entries remain, they are kept but truncated to max_fallback_len.
    Non-list values are returned unchanged.
    """
    if not isinstance(data, list) or not data:
        return data

    if max_fallback_len is None:
        max_fallback_len = settings.BROWSER_FALLBACK_TEXT_MAX_LEN

    has_structured = any(
        isinstance(item, dict) and item.get("source") not in _FALLBACK_SOURCES
        for item in data
    )

    if has_structured:
        return [
            item for item in data
            if not (isinstance(item, dict) and item.get("source") in _FALLBACK_SOURCES)
        ]

    cleaned = []
    for item in data:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            text = item["text"]
            if len(text) > max_fallback_len:
                item = dict(item)
                item["text"] = text[:max_fallback_len]
                item["truncated"] = True
        cleaned.append(item)
    return cleaned
