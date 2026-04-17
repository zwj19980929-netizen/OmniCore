"""
Unit tests for memory/decay.py (A1).
"""
from datetime import datetime, timedelta

import pytest

from memory.decay import compute_decay_score, rerank_by_decay


_NOW = datetime(2026, 4, 16, 12, 0, 0)


def _iso(days_ago: float) -> str:
    return (_NOW - timedelta(days=days_ago)).isoformat(timespec="seconds")


class TestComputeDecayScore:
    def test_newer_beats_older_at_equal_similarity(self):
        new = compute_decay_score(0.3, created_at=_iso(1), hit_count=0, half_life_days=30, now=_NOW)
        old = compute_decay_score(0.3, created_at=_iso(60), hit_count=0, half_life_days=30, now=_NOW)
        assert new > old

    def test_hit_count_boosts_score(self):
        cold = compute_decay_score(0.3, created_at=_iso(5), hit_count=0, half_life_days=30, now=_NOW)
        hot = compute_decay_score(0.3, created_at=_iso(5), hit_count=10, half_life_days=30, now=_NOW)
        assert hot > cold

    def test_half_life_boundary(self):
        """At age == half_life, decay factor should be exp(-1) ≈ 0.3679."""
        score = compute_decay_score(0.0, created_at=_iso(30), hit_count=0, half_life_days=30, now=_NOW)
        # similarity = 1, decay ≈ 0.3679, hit_boost = 1 → score ≈ 0.368
        assert 0.35 < score < 0.39

    def test_distance_none_returns_zero(self):
        assert compute_decay_score(None, created_at=_iso(1), hit_count=5, now=_NOW) == 0.0

    def test_far_distance_similarity_clamped(self):
        assert compute_decay_score(2.5, created_at=_iso(1), hit_count=0, now=_NOW) == 0.0

    def test_updated_at_takes_precedence_over_created_at(self):
        by_created = compute_decay_score(
            0.0, created_at=_iso(100), updated_at=None, hit_count=0, half_life_days=30, now=_NOW
        )
        by_updated = compute_decay_score(
            0.0, created_at=_iso(100), updated_at=_iso(1), hit_count=0, half_life_days=30, now=_NOW
        )
        assert by_updated > by_created

    def test_missing_timestamps_treated_as_zero_age(self):
        score = compute_decay_score(0.2, created_at=None, updated_at=None, hit_count=0, now=_NOW)
        # similarity=0.8, decay=1, hit_boost=1 → 0.8
        assert score == pytest.approx(0.8, abs=1e-6)


class TestRerankByDecay:
    def test_sorted_by_decay_score_desc(self):
        items = [
            {"id": "old_hot", "distance": 0.1, "metadata": {"created_at": _iso(90), "hit_count": 20}},
            {"id": "new_cold", "distance": 0.1, "metadata": {"created_at": _iso(1), "hit_count": 0}},
            {"id": "mid", "distance": 0.1, "metadata": {"created_at": _iso(20), "hit_count": 3}},
        ]
        sorted_items = rerank_by_decay(items, half_life_days=30, now=_NOW)
        # Returned in score-desc order, all items carry decay_score.
        scores = [item["decay_score"] for item in sorted_items]
        assert scores == sorted(scores, reverse=True)
        assert all("decay_score" in item for item in sorted_items)
        # Position check: an ancient hot record is still beaten by recent ones,
        # because exp(-3) ≈ 0.05 overwhelms the log(21) reuse boost.
        positions = {item["id"]: idx for idx, item in enumerate(sorted_items)}
        assert positions["old_hot"] > positions["new_cold"]
        assert positions["old_hot"] > positions["mid"]

    def test_empty_list_returns_empty(self):
        assert rerank_by_decay([], half_life_days=30) == []

    def test_does_not_mutate_input(self):
        items = [{"id": "a", "distance": 0.2, "metadata": {"created_at": _iso(1)}}]
        _ = rerank_by_decay(items, half_life_days=30, now=_NOW)
        assert "decay_score" not in items[0]
