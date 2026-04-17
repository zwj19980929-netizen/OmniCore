"""
Time-decay scoring for memory retrieval (A1).

Reranks Chroma similarity search by combining:
- raw semantic similarity (from distance)
- exponential time decay over ``created_at`` / ``updated_at``
- logarithmic boost from ``hit_count`` (reuse signal)

All functions are pure so they can be unit-tested without Chroma.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Dict, List, Optional


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _age_days(created_at: Any, now: Optional[datetime] = None) -> float:
    dt = _parse_iso(created_at)
    if dt is None:
        return 0.0
    reference = now or datetime.now()
    delta = reference - dt
    return max(delta.total_seconds() / 86400.0, 0.0)


def compute_decay_score(
    distance: Optional[float],
    *,
    created_at: Any = None,
    updated_at: Any = None,
    hit_count: int = 0,
    half_life_days: float = 30.0,
    now: Optional[datetime] = None,
) -> float:
    """Combined semantic + time-decay + reuse score.

    Higher is better. Safe to call with missing fields — unknown timestamps
    default to zero age, unknown distance defaults to zero similarity.
    """
    similarity = max(0.0, 1.0 - float(distance if distance is not None else 1.0))
    reference_ts = updated_at or created_at
    age = _age_days(reference_ts, now)
    half_life = max(float(half_life_days), 0.1)
    decay = math.exp(-age / half_life)
    hits = max(int(hit_count or 0), 0)
    hit_boost = 1.0 + math.log1p(hits)
    return similarity * decay * hit_boost


def rerank_by_decay(
    memories: List[Dict[str, Any]],
    *,
    half_life_days: float = 30.0,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Return memories re-sorted by decay score, highest first.

    Each memory dict is expected to carry ``distance`` and
    ``metadata.{created_at, updated_at, hit_count}``. The original list
    is not mutated; returned items are shallow copies with an extra
    ``decay_score`` field for debug.
    """
    scored: List[Dict[str, Any]] = []
    for memory in memories or []:
        metadata = memory.get("metadata") or {}
        score = compute_decay_score(
            memory.get("distance"),
            created_at=metadata.get("created_at"),
            updated_at=metadata.get("updated_at"),
            hit_count=metadata.get("hit_count", 0),
            half_life_days=half_life_days,
            now=now,
        )
        copy = dict(memory)
        copy["decay_score"] = score
        scored.append(copy)
    scored.sort(key=lambda item: item.get("decay_score", 0.0), reverse=True)
    return scored
