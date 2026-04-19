"""
Unit tests for utils/strategy_stats.py (B5).
"""
from __future__ import annotations

import pytest

from utils.strategy_stats import StrategyStatsStore, get_strategy_stats_store


@pytest.fixture(autouse=True)
def _enable_strategy_learning(monkeypatch):
    from config.settings import settings as _settings
    monkeypatch.setattr(_settings, "BROWSER_STRATEGY_LEARNING_ENABLED", True)
    monkeypatch.setattr(_settings, "BROWSER_STRATEGY_MIN_SAMPLES", 3)
    monkeypatch.setattr(_settings, "BROWSER_STRATEGY_SKIP_THRESHOLD", 0.2)
    yield


def _store(tmp_path):
    return StrategyStatsStore(db_path=tmp_path / "ss.db")


class TestRecord:
    def test_success_bumps_counters(self, tmp_path):
        store = _store(tmp_path)
        assert store.record("https://acme.com/x", "click", "direct_click",
                            success=True, latency_ms=120) is True
        stats = store.get_stats("acme.com", "click")
        assert "direct_click" in stats
        assert stats["direct_click"]["success_count"] == 1
        assert stats["direct_click"]["fail_count"] == 0
        assert stats["direct_click"]["avg_latency_ms"] == 120

    def test_failure_bumps_fail(self, tmp_path):
        store = _store(tmp_path)
        store.record("acme.com", "click", "force_click", success=False, latency_ms=50)
        stats = store.get_stats("acme.com", "click")
        assert stats["force_click"]["fail_count"] == 1
        assert stats["force_click"]["success_count"] == 0
        assert stats["force_click"]["success_rate"] == 0.0

    def test_mixed_counts_success_rate(self, tmp_path):
        store = _store(tmp_path)
        for _ in range(3):
            store.record("acme.com", "click", "role", success=True, latency_ms=80)
        store.record("acme.com", "click", "role", success=False, latency_ms=200)
        stats = store.get_stats("acme.com", "click")["role"]
        assert stats["total"] == 4
        assert stats["success_count"] == 3
        assert abs(stats["success_rate"] - 0.75) < 1e-6
        # avg latency = (80*3 + 200) / 4 = 110
        assert abs(stats["avg_latency_ms"] - 110) < 1e-6

    def test_empty_inputs_rejected(self, tmp_path):
        store = _store(tmp_path)
        assert store.record("", "click", "x", success=True) is False
        assert store.record("acme.com", "", "x", success=True) is False
        assert store.record("acme.com", "click", "", success=True) is False

    def test_domain_normalization(self, tmp_path):
        store = _store(tmp_path)
        store.record("https://Login.ACME.com/foo?q=1", "click", "direct_click", success=True)
        # retrieve via bare host, lowercase
        stats = store.get_stats("login.acme.com", "click")
        assert "direct_click" in stats


class TestRanking:
    def test_min_samples_excludes_under_observed(self, tmp_path):
        store = _store(tmp_path)
        # only 2 samples < MIN_SAMPLES=3
        store.record("acme.com", "click", "direct_click", success=True)
        store.record("acme.com", "click", "direct_click", success=True)
        assert store.ranked_strategies("acme.com", "click") == []

    def test_ranking_by_success_rate_desc(self, tmp_path):
        store = _store(tmp_path)
        for _ in range(3):
            store.record("acme.com", "click", "slow_but_works", success=True)
        for _ in range(3):
            store.record("acme.com", "click", "fast_works", success=True)
        # tie on rate → tie-break on success_count (fast_works recorded later
        # but same count; order is stable but both valid). Assert both present.
        ranked = store.ranked_strategies("acme.com", "click")
        assert set(ranked) == {"slow_but_works", "fast_works"}

    def test_mixed_ranking(self, tmp_path):
        store = _store(tmp_path)
        # good: 4/4
        for _ in range(4):
            store.record("acme.com", "click", "good", success=True)
        # mid: 2/3 ≈ 0.667
        store.record("acme.com", "click", "mid", success=True)
        store.record("acme.com", "click", "mid", success=True)
        store.record("acme.com", "click", "mid", success=False)
        ranked = store.ranked_strategies("acme.com", "click")
        assert ranked[0] == "good"
        assert ranked[1] == "mid"

    def test_skip_threshold_excludes_from_ranked(self, tmp_path):
        store = _store(tmp_path)
        # bad: 0/4 success_rate=0 < 0.2
        for _ in range(4):
            store.record("acme.com", "click", "bad", success=False)
        # good: 3/3
        for _ in range(3):
            store.record("acme.com", "click", "good", success=True)
        ranked = store.ranked_strategies("acme.com", "click")
        assert ranked == ["good"]
        assert "bad" not in ranked

    def test_skip_strategies_set(self, tmp_path):
        store = _store(tmp_path)
        # 0/4 → skipped
        for _ in range(4):
            store.record("acme.com", "click", "dead", success=False)
        # 4/4 → not skipped
        for _ in range(4):
            store.record("acme.com", "click", "alive", success=True)
        # 1/5 = 0.2 → on threshold, NOT below → not skipped
        for _ in range(4):
            store.record("acme.com", "click", "border", success=False)
        store.record("acme.com", "click", "border", success=True)
        skip = store.skip_strategies("acme.com", "click")
        assert "dead" in skip
        assert "alive" not in skip
        # 0.2 >= 0.2 threshold → not below, not skipped
        assert "border" not in skip

    def test_role_isolation(self, tmp_path):
        store = _store(tmp_path)
        for _ in range(3):
            store.record("acme.com", "click", "direct_click", success=True)
        for _ in range(3):
            store.record("acme.com", "input", "direct_fill", success=False)
        assert store.ranked_strategies("acme.com", "click") == ["direct_click"]
        assert store.ranked_strategies("acme.com", "input") == []

    def test_domain_isolation(self, tmp_path):
        store = _store(tmp_path)
        for _ in range(3):
            store.record("a.com", "click", "s", success=True)
        for _ in range(3):
            store.record("b.com", "click", "s", success=False)
        assert store.ranked_strategies("a.com", "click") == ["s"]
        assert store.ranked_strategies("b.com", "click") == []
        assert "s" in store.skip_strategies("b.com", "click")

    def test_empty_inputs(self, tmp_path):
        store = _store(tmp_path)
        assert store.ranked_strategies("", "click") == []
        assert store.ranked_strategies("acme.com", "") == []
        assert store.skip_strategies("", "click") == set()


class TestFeatureFlag:
    def test_disabled_flag_noop(self, tmp_path, monkeypatch):
        from config.settings import settings as _settings
        monkeypatch.setattr(_settings, "BROWSER_STRATEGY_LEARNING_ENABLED", False)
        store = _store(tmp_path)
        assert store.record("acme.com", "click", "s", success=True) is False
        assert store.get_stats("acme.com", "click") == {}
        assert store.ranked_strategies("acme.com", "click") == []
        assert store.skip_strategies("acme.com", "click") == set()

    def test_get_singleton_disabled_returns_none(self, monkeypatch):
        from config.settings import settings as _settings
        import utils.strategy_stats as ss
        monkeypatch.setattr(_settings, "BROWSER_STRATEGY_LEARNING_ENABLED", False)
        monkeypatch.setattr(ss, "_SINGLETON", None)
        assert get_strategy_stats_store() is None

    def test_get_singleton_enabled_returns_instance(self, monkeypatch, tmp_path):
        from config.settings import settings as _settings
        import utils.strategy_stats as ss
        monkeypatch.setattr(_settings, "BROWSER_STRATEGY_LEARNING_ENABLED", True)
        monkeypatch.setattr(_settings, "BROWSER_STRATEGY_DB", str(tmp_path / "s.db"))
        monkeypatch.setattr(ss, "_SINGLETON", None)
        inst = get_strategy_stats_store()
        assert inst is not None
        # idempotent
        assert get_strategy_stats_store() is inst
