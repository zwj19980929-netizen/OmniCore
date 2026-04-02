"""
Text repair utilities — mojibake detection and repair.

Extracted from core/graph.py (R3 refactor) so that graph nodes,
finalizer, and replanner can share the logic without importing graph.py.
"""

from typing import Any, Dict

_MOJIBAKE_MARKERS = (
    "\u00c3",
    "\u00c2",
    "\u00e6",
    "\u00e5",
    "\u00e4",
    "\u00e7",
    "\u00e8",
    "\u00e9",
    "\u00ea",
    "\u00ef",
    "\u00f0",
)


def looks_like_mojibake(text: str) -> bool:
    if not isinstance(text, str) or len(text) < 6:
        return False
    marker_hits = sum(text.count(marker) for marker in _MOJIBAKE_MARKERS)
    if marker_hits < 2:
        return False
    cjk_count = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk_count == 0


def repair_mojibake_text(text: str) -> str:
    if not isinstance(text, str) or not text:
        return text
    if not looks_like_mojibake(text):
        return text
    try:
        repaired = text.encode("latin-1", errors="ignore").decode("utf-8", errors="ignore")
    except Exception:
        return text
    if not repaired or repaired == text:
        return text
    repaired_cjk = sum(1 for ch in repaired if "\u4e00" <= ch <= "\u9fff")
    if repaired_cjk == 0:
        return text
    return repaired


def normalize_text_value(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return repair_mojibake_text(text)


def normalize_payload(value: Any) -> Any:
    if isinstance(value, str):
        return repair_mojibake_text(value)
    if isinstance(value, list):
        return [normalize_payload(item) for item in value]
    if isinstance(value, tuple):
        return tuple(normalize_payload(item) for item in value)
    if isinstance(value, dict):
        normalized: Dict[Any, Any] = {}
        for key, item in value.items():
            normalized[key] = normalize_payload(item)
        return normalized
    return value


def payload_preview(payload: Any, limit: int = 220) -> str:
    """Return a compact single-line preview of *payload*."""
    import json
    normalized = normalize_payload(payload)
    try:
        if isinstance(normalized, (dict, list, tuple)):
            text = json.dumps(normalized, ensure_ascii=False, default=str)
        else:
            text = str(normalized)
    except Exception:
        text = str(normalized)
    return text.replace("\n", " ")[:limit]
