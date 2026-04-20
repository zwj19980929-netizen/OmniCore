"""Unit tests for F4: memory_query_cache TTL dedup."""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from utils.memory_query_cache import get_cached, set_cached, clear


@pytest.fixture(autouse=True)
def reset_cache():
    clear()
    yield
    clear()


class TestMemoryQueryCache:
    def test_miss_before_set(self):
        assert get_cached("col1", "q", 5, None) is None

    def test_hit_after_set(self):
        results = [{"content": "r1"}]
        set_cached("col1", "q", 5, None, results)
        assert get_cached("col1", "q", 5, None) == results

    def test_same_query_different_memory_type(self):
        set_cached("col1", "q", 5, "skill_definition", [{"s": 1}])
        assert get_cached("col1", "q", 5, None) is None
        assert get_cached("col1", "q", 5, "skill_definition") == [{"s": 1}]

    def test_different_query_no_hit(self):
        set_cached("col1", "query1", 5, None, [{"a": 1}])
        assert get_cached("col1", "query2", 5, None) is None

    def test_expired_entry_returns_none(self, monkeypatch):
        import utils.memory_query_cache as mod
        t0 = time.monotonic()
        set_cached("col1", "q", 5, None, [{"r": 1}])
        monkeypatch.setattr("time.monotonic", lambda: t0 + 120)
        assert get_cached("col1", "q", 5, None) is None

    def test_different_collection_no_hit(self):
        set_cached("col1", "q", 5, None, [{"r": 1}])
        assert get_cached("col2", "q", 5, None) is None

    def test_different_n_results_no_hit(self):
        set_cached("col1", "q", 5, None, [{"r": 1}])
        assert get_cached("col1", "q", 10, None) is None
