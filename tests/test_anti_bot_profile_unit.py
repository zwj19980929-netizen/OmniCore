"""
Unit tests for utils/anti_bot_profile.py (B2).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from utils.anti_bot_profile import (
    AntiBotProfileStore,
    ThrottleHint,
    normalize_domain,
    pick_ua,
)


@pytest.fixture(autouse=True)
def _enable_profile(monkeypatch):
    from config.settings import settings as _settings
    monkeypatch.setattr(_settings, "ANTI_BOT_PROFILE_ENABLED", True)
    monkeypatch.setattr(_settings, "ANTI_BOT_INITIAL_DELAY_SEC", 0.5)
    monkeypatch.setattr(_settings, "ANTI_BOT_MAX_DELAY_SEC", 5.0)
    monkeypatch.setattr(_settings, "ANTI_BOT_BLOCK_DECAY_DAYS", 14)
    monkeypatch.setattr(_settings, "ANTI_BOT_SUCCESS_TO_COOLDOWN", 3)
    yield


def _store(tmp_path):
    return AntiBotProfileStore(db_path=tmp_path / "ab.db")


class TestNormalizeDomain:
    def test_url(self):
        assert normalize_domain("https://news.acme.com/home?q=1") == "news.acme.com"

    def test_empty(self):
        assert normalize_domain("") == ""


class TestRecordRequest:
    def test_success_increments_consecutive_success(self, tmp_path):
        store = _store(tmp_path)
        for _ in range(2):
            store.record_request("acme.com", success=True)
        profile = store.get_profile("acme.com")
        assert profile is not None
        assert profile.success_count == 2
        assert profile.request_count == 2
        assert profile.block_count == 0
        assert profile.consecutive_success == 2

    def test_failure_resets_consecutive_success(self, tmp_path):
        store = _store(tmp_path)
        store.record_request("acme.com", success=True)
        store.record_request("acme.com", success=False)
        profile = store.get_profile("acme.com")
        assert profile.consecutive_success == 0
        assert profile.request_count == 2
        assert profile.success_count == 1


class TestRecordBlock:
    def test_first_block_sets_delay_and_headed(self, tmp_path):
        store = _store(tmp_path)
        store.record_block("acme.com", kind="captcha")
        profile = store.get_profile("acme.com")
        assert profile is not None
        assert profile.block_count == 1
        assert profile.last_block_kind == "captcha"
        assert profile.prefers_headed is True
        # initial delay is 0.5 → first block stores that
        assert profile.current_delay_sec == pytest.approx(0.5, rel=1e-3)

    def test_repeated_blocks_back_off_exponentially(self, tmp_path):
        store = _store(tmp_path)
        store.record_block("acme.com", kind="rate_limit")  # 0.5
        store.record_block("acme.com", kind="rate_limit")  # 1.0
        store.record_block("acme.com", kind="rate_limit")  # 2.0
        profile = store.get_profile("acme.com")
        assert profile.current_delay_sec == pytest.approx(2.0, rel=1e-3)

    def test_delay_capped_at_max(self, tmp_path):
        store = _store(tmp_path)
        for _ in range(10):
            store.record_block("acme.com", kind="captcha")
        profile = store.get_profile("acme.com")
        # max=5
        assert profile.current_delay_sec <= 5.0

    def test_unknown_kind_normalizes(self, tmp_path):
        store = _store(tmp_path)
        store.record_block("acme.com", kind="some_random_thing")
        profile = store.get_profile("acme.com")
        assert profile.last_block_kind == "unknown"


class TestConsecutiveSuccessClearsDelay:
    def test_enough_successes_reset_delay(self, tmp_path):
        store = _store(tmp_path)
        store.record_block("acme.com", kind="captcha")
        assert store.get_profile("acme.com").current_delay_sec > 0
        for _ in range(3):  # ANTI_BOT_SUCCESS_TO_COOLDOWN=3
            store.record_request("acme.com", success=True)
        profile = store.get_profile("acme.com")
        assert profile.current_delay_sec == 0.0

    def test_partial_successes_keep_delay(self, tmp_path):
        store = _store(tmp_path)
        store.record_block("acme.com", kind="captcha")
        initial_delay = store.get_profile("acme.com").current_delay_sec
        # only 2 successes (below threshold 3)
        for _ in range(2):
            store.record_request("acme.com", success=True)
        profile = store.get_profile("acme.com")
        assert profile.current_delay_sec == initial_delay


class TestSuggestThrottle:
    def test_disabled_returns_empty_hint(self, tmp_path, monkeypatch):
        from config.settings import settings as _settings
        store = _store(tmp_path)
        store.record_block("acme.com")
        monkeypatch.setattr(_settings, "ANTI_BOT_PROFILE_ENABLED", False)
        hint = store.suggest_throttle("acme.com")
        assert isinstance(hint, ThrottleHint)
        assert hint.delay_sec == 0.0
        assert hint.headed is False

    def test_no_profile_still_returns_ua(self, tmp_path):
        # UA pool may or may not exist in test env; ua is "" when load fails
        hint = _store(tmp_path).suggest_throttle("never-seen.com")
        assert isinstance(hint, ThrottleHint)
        assert hint.delay_sec == 0.0
        assert hint.reason == "no_profile"

    def test_hint_uses_current_delay_when_recent_block(self, tmp_path):
        store = _store(tmp_path)
        store.record_block("acme.com", kind="captcha")
        hint = store.suggest_throttle("acme.com")
        assert hint.delay_sec > 0
        assert hint.headed is True
        assert "blocks=1" in hint.reason

    def test_hint_decays_stale_block(self, tmp_path, monkeypatch):
        from config.settings import settings as _settings
        monkeypatch.setattr(_settings, "ANTI_BOT_BLOCK_DECAY_DAYS", 1)
        store = _store(tmp_path)
        store.record_block("acme.com", kind="captcha")
        # Rewind last_block_at in the DB to simulate a 10-day-old block
        conn = store._ensure_conn()
        old_ts = (datetime.now() - timedelta(days=10)).isoformat(timespec="seconds")
        conn.execute(
            "UPDATE anti_bot_domains SET last_block_at = ? WHERE domain = ?",
            (old_ts, "acme.com"),
        )
        conn.commit()
        hint = store.suggest_throttle("acme.com")
        # Past decay window → delay drops to zero
        assert hint.delay_sec == 0.0


class TestUAPool:
    def test_pick_ua_returns_string(self):
        # Real pool file should load; if not, empty string is acceptable
        ua = pick_ua()
        assert isinstance(ua, str)

    def test_preferred_ua_returned(self, tmp_path):
        store = _store(tmp_path)
        store.record_block("acme.com", kind="captcha")
        store.set_preferred_ua("acme.com", "Custom/1.0 Test UA")
        hint = store.suggest_throttle("acme.com")
        assert hint.ua == "Custom/1.0 Test UA"


class TestListProfiles:
    def test_ordered_by_block_count(self, tmp_path):
        store = _store(tmp_path)
        store.record_block("a.com")
        store.record_block("b.com")
        store.record_block("b.com")
        profiles = store.list_profiles()
        assert [p.domain for p in profiles[:2]] == ["b.com", "a.com"]
