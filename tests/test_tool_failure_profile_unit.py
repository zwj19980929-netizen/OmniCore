"""
Unit tests for utils/tool_failure_profile.py (C2).
"""
from __future__ import annotations

import pytest

from utils.tool_failure_profile import (
    ToolFailureProfileStore,
    classify_error_tag,
    format_tool_health_block,
    get_tool_failure_profile_store,
    reset_tool_failure_profile_singleton_for_tests,
)


@pytest.fixture(autouse=True)
def _enable_profile(monkeypatch):
    from config.settings import settings as _settings
    monkeypatch.setattr(_settings, "TOOL_FAILURE_PROFILE_ENABLED", True)
    monkeypatch.setattr(_settings, "TOOL_FAILURE_WINDOW", 20)
    monkeypatch.setattr(_settings, "TOOL_FAILURE_MIN_SAMPLES", 3)
    monkeypatch.setattr(_settings, "TOOL_FAILURE_SKIP_THRESHOLD", 0.7)
    monkeypatch.setattr(_settings, "TOOL_FAILURE_WARN_THRESHOLD", 0.4)
    monkeypatch.setattr(_settings, "TOOL_FAILURE_HINT_TOP_K", 5)
    reset_tool_failure_profile_singleton_for_tests()
    yield
    reset_tool_failure_profile_singleton_for_tests()


def _store(tmp_path):
    return ToolFailureProfileStore(db_path=tmp_path / "tf.db")


# ---------------------------------------------------------------------------
# classify_error_tag
# ---------------------------------------------------------------------------

class TestClassifyErrorTag:
    @pytest.mark.parametrize("text,expected", [
        ("Read timed out after 30s", "timeout"),
        ("TimeoutError: deadline exceeded", "timeout"),
        ("HTTP 429 Too Many Requests", "rate_limit"),
        ("Hit rate-limit on api", "rate_limit"),
        ("401 Unauthorized", "auth"),
        ("Forbidden: invalid API key", "auth"),
        ("JSON decode error at line 1", "parse_error"),
        ("Malformed payload", "parse_error"),
        ("Connection refused", "network"),
        ("DNS lookup failed", "network"),
        ("HTTP 404 not found", "not_found"),
        ("Internal server error 500", "server_error"),
        ("Some random failure xyz", "unknown"),
    ])
    def test_known_buckets(self, text, expected):
        assert classify_error_tag(None, text) == expected

    def test_uses_error_type_when_message_empty(self):
        assert classify_error_tag("TimeoutError", "") == "timeout"

    def test_empty_returns_blank(self):
        assert classify_error_tag(None, None) == ""
        assert classify_error_tag("", "") == ""


# ---------------------------------------------------------------------------
# record_outcome / get_profile
# ---------------------------------------------------------------------------

class TestRecord:
    def test_records_success(self, tmp_path):
        store = _store(tmp_path)
        assert store.record_outcome("web_worker", success=True, latency_ms=120) is True
        profile = store.get_profile("web_worker")
        assert profile["total"] == 1
        assert profile["success_count"] == 1
        assert profile["success_rate"] == 1.0
        assert profile["fail_rate"] == 0.0
        assert profile["timeout_rate"] == 0.0
        assert profile["avg_latency_ms"] == 120

    def test_records_failure_with_tag(self, tmp_path):
        store = _store(tmp_path)
        store.record_outcome(
            "mcp_x", success=False, error_type="TimeoutError",
            error_message="timed out", latency_ms=5000,
        )
        profile = store.get_profile("mcp_x")
        assert profile["fail_count"] == 1
        assert profile["timeout_rate"] == 1.0
        assert profile["error_tags"] == {"timeout": 1}

    def test_mixed_outcomes(self, tmp_path):
        store = _store(tmp_path)
        for _ in range(7):
            store.record_outcome("file.read_write", success=True, latency_ms=50)
        for _ in range(3):
            store.record_outcome(
                "file.read_write", success=False,
                error_type="OSError", error_message="404 not found",
            )
        p = store.get_profile("file.read_write")
        assert p["total"] == 10
        assert p["success_rate"] == 0.7
        assert p["fail_rate"] == 0.3
        assert p["error_tags"]["not_found"] == 3

    def test_empty_tool_name_rejected(self, tmp_path):
        store = _store(tmp_path)
        assert store.record_outcome("", success=True) is False

    def test_disabled_returns_no_op(self, tmp_path, monkeypatch):
        from config.settings import settings as _settings
        monkeypatch.setattr(_settings, "TOOL_FAILURE_PROFILE_ENABLED", False)
        store = _store(tmp_path)
        assert store.record_outcome("any", success=True) is False
        assert store.get_profile("any") == {}

    def test_singleton_returns_none_when_disabled(self, monkeypatch):
        from config.settings import settings as _settings
        monkeypatch.setattr(_settings, "TOOL_FAILURE_PROFILE_ENABLED", False)
        reset_tool_failure_profile_singleton_for_tests()
        assert get_tool_failure_profile_store() is None


# ---------------------------------------------------------------------------
# Sliding window
# ---------------------------------------------------------------------------

class TestSlidingWindow:
    def test_window_caps_history(self, tmp_path, monkeypatch):
        from config.settings import settings as _settings
        monkeypatch.setattr(_settings, "TOOL_FAILURE_WINDOW", 5)
        store = _store(tmp_path)
        # 8 failures then 5 successes — window=5 should only see the 5 successes
        for _ in range(8):
            store.record_outcome("t1", success=False, error_type="TimeoutError")
        for _ in range(5):
            store.record_outcome("t1", success=True, latency_ms=10)
        p = store.get_profile("t1")
        assert p["total"] == 5
        assert p["success_count"] == 5
        assert p["fail_count"] == 0


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------

class TestRecommendations:
    def test_below_min_samples_no_hint(self, tmp_path):
        store = _store(tmp_path)
        store.record_outcome("t", success=False, error_type="TimeoutError")
        store.record_outcome("t", success=False, error_type="TimeoutError")
        # only 2 samples, MIN_SAMPLES=3 → no hint
        assert store.get_recommendation("t") is None

    def test_high_timeout_rate_yields_tune_timeout(self, tmp_path):
        store = _store(tmp_path)
        for _ in range(8):
            store.record_outcome("slow_tool", success=False, error_type="TimeoutError")
        for _ in range(2):
            store.record_outcome("slow_tool", success=True, latency_ms=100)
        hint = store.get_recommendation("slow_tool")
        assert hint is not None
        assert hint["level"] == "tune_timeout"
        assert "timeout" in hint["message"].lower()

    def test_high_fail_non_timeout_yields_skip(self, tmp_path):
        store = _store(tmp_path)
        for _ in range(8):
            store.record_outcome(
                "flaky", success=False, error_type="ParseError",
                error_message="json decode failed",
            )
        for _ in range(2):
            store.record_outcome("flaky", success=True)
        hint = store.get_recommendation("flaky")
        assert hint is not None
        assert hint["level"] == "skip"
        assert "parse_error" in hint["message"]

    def test_moderate_fail_yields_warn(self, tmp_path):
        store = _store(tmp_path)
        for _ in range(5):
            store.record_outcome("noisy", success=True)
        for _ in range(5):
            store.record_outcome(
                "noisy", success=False, error_type="ConnError",
                error_message="connection refused",
            )
        hint = store.get_recommendation("noisy")
        assert hint is not None
        assert hint["level"] == "warn"

    def test_healthy_tool_no_hint(self, tmp_path):
        store = _store(tmp_path)
        for _ in range(10):
            store.record_outcome("ok", success=True, latency_ms=20)
        assert store.get_recommendation("ok") is None

    def test_get_recommendations_sorted_and_limited(self, tmp_path):
        store = _store(tmp_path)
        # skip-level
        for _ in range(8):
            store.record_outcome(
                "bad", success=False, error_type="ParseError",
                error_message="json decode",
            )
        for _ in range(2):
            store.record_outcome("bad", success=True)
        # warn-level
        for _ in range(5):
            store.record_outcome("meh", success=True)
        for _ in range(5):
            store.record_outcome(
                "meh", success=False, error_type="ConnError",
                error_message="network down",
            )
        hints = store.get_recommendations(top_k=10)
        assert len(hints) == 2
        # skip should come first
        assert hints[0]["tool_name"] == "bad"
        assert hints[0]["level"] == "skip"
        assert hints[1]["tool_name"] == "meh"

        # top_k=1 keeps only the most severe
        only = store.get_recommendations(top_k=1)
        assert len(only) == 1
        assert only[0]["tool_name"] == "bad"


# ---------------------------------------------------------------------------
# Format helpers
# ---------------------------------------------------------------------------

class TestFormat:
    def test_empty_in_empty_out(self):
        assert format_tool_health_block([]) == ""

    def test_renders_block(self):
        text = format_tool_health_block([
            {"tool_name": "x", "level": "skip", "message": "fail_rate=80%"},
            {"tool_name": "y", "level": "warn", "message": "fail_rate=50%"},
        ])
        assert "Recent tool health" in text
        assert "[avoid] x" in text
        assert "[noisy] y" in text
        assert text.endswith("---\n")


# ---------------------------------------------------------------------------
# Purge
# ---------------------------------------------------------------------------

class TestPurge:
    def test_purge_single_tool(self, tmp_path):
        store = _store(tmp_path)
        store.record_outcome("a", success=True)
        store.record_outcome("b", success=True)
        assert store.purge("a") == 1
        assert store.get_profile("a") == {}
        assert store.get_profile("b")["total"] == 1

    def test_purge_all(self, tmp_path):
        store = _store(tmp_path)
        store.record_outcome("a", success=True)
        store.record_outcome("b", success=True)
        assert store.purge() == 2
        assert store.get_all_profiles() == []
