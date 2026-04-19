"""
Unit tests for utils/vision_cache.py (B3).

Each test gets its own SQLite file via tmp_path so isolation is strict.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from utils.vision_cache import (
    VisionCache,
    should_bypass_for_task,
)


@pytest.fixture(autouse=True)
def _enable_cache(monkeypatch):
    from config.settings import settings as _settings
    monkeypatch.setattr(_settings, "BROWSER_VISION_CACHE_ENABLED", True)
    monkeypatch.setattr(_settings, "BROWSER_VISION_CACHE_TTL_DAYS", 7)
    monkeypatch.setattr(
        _settings,
        "BROWSER_VISION_CACHE_BYPASS_KEYWORDS",
        "login,payment,verify",
    )
    yield


def _cache(tmp_path):
    return VisionCache(db_path=tmp_path / "vc.db")


# ----------------------------------------------------------------------
# basic get/set
# ----------------------------------------------------------------------


class TestGetSet:
    def test_set_then_get_returns_description(self, tmp_path):
        cache = _cache(tmp_path)
        assert cache.set("hash1", "A search results page", "x.com/search") is True
        cached = cache.get("hash1")
        assert cached is not None
        assert cached.description == "A search results page"
        assert cached.url_template == "x.com/search"

    def test_miss_returns_none(self, tmp_path):
        cache = _cache(tmp_path)
        assert cache.get("does-not-exist") is None

    def test_set_empty_inputs_rejected(self, tmp_path):
        cache = _cache(tmp_path)
        assert cache.set("", "desc") is False
        assert cache.set("hash", "") is False
        assert cache.set("", "") is False

    def test_get_empty_hash_returns_none(self, tmp_path):
        cache = _cache(tmp_path)
        assert cache.get("") is None

    def test_get_increments_hit_count(self, tmp_path):
        cache = _cache(tmp_path)
        cache.set("h", "d")
        c1 = cache.get("h")
        c2 = cache.get("h")
        c3 = cache.get("h")
        assert c1.hit_count == 1
        assert c2.hit_count == 2
        assert c3.hit_count == 3

    def test_set_upserts_existing_entry(self, tmp_path):
        cache = _cache(tmp_path)
        cache.set("h", "old description", "old/template")
        cache.set("h", "new description", "new/template")
        cached = cache.get("h")
        assert cached.description == "new description"
        assert cached.url_template == "new/template"

    def test_disabled_cache_returns_none(self, tmp_path, monkeypatch):
        from config.settings import settings as _settings
        monkeypatch.setattr(_settings, "BROWSER_VISION_CACHE_ENABLED", False)
        cache = _cache(tmp_path)
        assert cache.set("h", "d") is False
        assert cache.get("h") is None


# ----------------------------------------------------------------------
# TTL behaviour
# ----------------------------------------------------------------------


class TestTTL:
    def test_expired_entry_returns_none(self, tmp_path, monkeypatch):
        cache = _cache(tmp_path)
        cache.set("h", "old")
        # Forge created_at to far past via direct DB poke.
        conn = cache._ensure_conn()
        old = (datetime.now() - timedelta(days=30)).isoformat(timespec="seconds")
        conn.execute("UPDATE vision_cache SET created_at = ? WHERE page_hash = ?", (old, "h"))
        conn.commit()
        assert cache.get("h") is None

    def test_fresh_entry_within_ttl_returns(self, tmp_path):
        cache = _cache(tmp_path)
        cache.set("h", "fresh")
        assert cache.get("h") is not None

    def test_set_purges_expired_rows(self, tmp_path):
        cache = _cache(tmp_path)
        cache.set("old", "old")
        cache.set("keep", "keep")
        conn = cache._ensure_conn()
        ancient = (datetime.now() - timedelta(days=99)).isoformat(timespec="seconds")
        conn.execute("UPDATE vision_cache SET created_at = ? WHERE page_hash = 'old'", (ancient,))
        conn.commit()
        # Trigger purge via a fresh set
        cache.set("trigger", "trigger")
        rows = conn.execute("SELECT page_hash FROM vision_cache").fetchall()
        hashes = {r["page_hash"] for r in rows}
        assert "old" not in hashes
        assert "keep" in hashes
        assert "trigger" in hashes


# ----------------------------------------------------------------------
# Bypass keywords
# ----------------------------------------------------------------------


class TestBypassKeywords:
    def test_login_task_bypasses(self):
        assert should_bypass_for_task("Help me login to acme.com") is True

    def test_payment_task_bypasses(self):
        assert should_bypass_for_task("Complete the PAYMENT for order 123") is True

    def test_normal_task_does_not_bypass(self):
        assert should_bypass_for_task("Search for python tutorials") is False

    def test_empty_task_does_not_bypass(self):
        assert should_bypass_for_task("") is False

    def test_empty_keyword_list_disables_bypass(self, monkeypatch):
        from config.settings import settings as _settings
        monkeypatch.setattr(_settings, "BROWSER_VISION_CACHE_BYPASS_KEYWORDS", "")
        assert should_bypass_for_task("login here") is False

    def test_partial_word_match(self):
        # "verify" appears inside "verifying"
        assert should_bypass_for_task("Currently verifying the receipt") is True


# ----------------------------------------------------------------------
# Stats
# ----------------------------------------------------------------------


class TestStats:
    def test_stats_after_inserts_and_hits(self, tmp_path):
        cache = _cache(tmp_path)
        cache.set("a", "alpha")
        cache.set("b", "beta")
        cache.get("a")
        cache.get("a")
        cache.get("b")
        stats = cache.stats()
        assert stats["entries"] == 2
        assert stats["total_hits"] == 3

    def test_stats_disabled(self, tmp_path, monkeypatch):
        from config.settings import settings as _settings
        cache = _cache(tmp_path)
        cache.set("a", "alpha")  # populated while enabled
        monkeypatch.setattr(_settings, "BROWSER_VISION_CACHE_ENABLED", False)
        # Cache stays initialised but flag-gated; stats should report empty
        # when the gate refuses a connection. Use a fresh instance to force
        # the gate to be evaluated.
        cache2 = _cache(tmp_path)
        assert cache2.stats() == {"entries": 0, "total_hits": 0}
