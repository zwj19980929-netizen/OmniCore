"""
Unit tests for utils/site_knowledge_store.py (B1).

Uses a temp SQLite file per test (``tmp_path``) to keep isolation strict.
"""
from __future__ import annotations

import pytest

from utils.site_knowledge_store import SiteKnowledgeStore, normalize_domain


@pytest.fixture(autouse=True)
def _enable_plan_memory(monkeypatch):
    from config.settings import settings as _settings
    monkeypatch.setattr(_settings, "BROWSER_PLAN_MEMORY_ENABLED", True)
    monkeypatch.setattr(_settings, "BROWSER_SELECTOR_HINT_TOP_K", 5)
    monkeypatch.setattr(_settings, "BROWSER_SELECTOR_MIN_SUCCESS_RATE", 0.5)
    monkeypatch.setattr(_settings, "BROWSER_SELECTOR_DECAY_DAYS", 30)
    yield


def _store(tmp_path):
    return SiteKnowledgeStore(db_path=tmp_path / "sk.db")


class TestNormalizeDomain:
    def test_url_extraction(self):
        assert normalize_domain("https://login.acme.com/path?q=1") == "login.acme.com"

    def test_raw_host(self):
        assert normalize_domain("ACME.COM") == "acme.com"

    def test_empty(self):
        assert normalize_domain("") == ""


class TestSelectorHints:
    def test_record_success_creates_row(self, tmp_path):
        store = _store(tmp_path)
        assert store.record_selector_success("https://acme.com/x", "click", "#submit") is True
        hints = store.get_selector_hints("https://acme.com")
        assert len(hints) == 1
        assert hints[0]["selector"] == "#submit"
        assert hints[0]["hit_count"] == 1
        assert hints[0]["success_rate"] == 1.0

    def test_record_success_is_idempotent_and_counts(self, tmp_path):
        store = _store(tmp_path)
        for _ in range(3):
            store.record_selector_success("acme.com", "click", "#submit")
        hints = store.get_selector_hints("acme.com")
        assert len(hints) == 1
        assert hints[0]["hit_count"] == 3

    def test_record_failure_lowers_rate(self, tmp_path):
        store = _store(tmp_path)
        store.record_selector_success("acme.com", "click", "#good")
        store.record_selector_success("acme.com", "click", "#good")
        store.record_selector_failure("acme.com", "click", "#good")
        hints = store.get_selector_hints("acme.com")
        assert len(hints) == 1
        # 2 hits / 3 total = 0.666 → above 0.5 default
        assert pytest.approx(hints[0]["success_rate"], rel=1e-2) == 2 / 3

    def test_low_success_rate_filtered_out(self, tmp_path, monkeypatch):
        from config.settings import settings as _settings
        monkeypatch.setattr(_settings, "BROWSER_SELECTOR_MIN_SUCCESS_RATE", 0.8)
        store = _store(tmp_path)
        store.record_selector_success("acme.com", "click", "#ok")
        store.record_selector_failure("acme.com", "click", "#ok")  # 50% → filtered
        assert store.get_selector_hints("acme.com") == []

    def test_role_filter(self, tmp_path):
        store = _store(tmp_path)
        store.record_selector_success("acme.com", "click", "#c1")
        store.record_selector_success("acme.com", "input", "#i1")
        assert {h["selector"] for h in store.get_selector_hints("acme.com", role="click")} == {"#c1"}
        assert {h["selector"] for h in store.get_selector_hints("acme.com", role="input")} == {"#i1"}

    def test_sorted_by_success_rate_then_hits(self, tmp_path):
        store = _store(tmp_path)
        # A: 5 hits, rate 1.0
        for _ in range(5):
            store.record_selector_success("acme.com", "click", "#A")
        # B: 10 hits, rate 0.83 (10 wins + 2 fails)
        for _ in range(10):
            store.record_selector_success("acme.com", "click", "#B")
        for _ in range(2):
            store.record_selector_failure("acme.com", "click", "#B")
        hints = store.get_selector_hints("acme.com")
        # A should rank first (higher rate) even though B has more hits
        assert hints[0]["selector"] == "#A"

    def test_limit(self, tmp_path):
        store = _store(tmp_path)
        for n in range(8):
            store.record_selector_success("acme.com", "click", f"#c{n}")
        assert len(store.get_selector_hints("acme.com", limit=3)) == 3

    def test_disabled_returns_empty(self, tmp_path, monkeypatch):
        from config.settings import settings as _settings
        store = _store(tmp_path)
        store.record_selector_success("acme.com", "click", "#x")
        monkeypatch.setattr(_settings, "BROWSER_PLAN_MEMORY_ENABLED", False)
        assert store.get_selector_hints("acme.com") == []
        # write paths also skip silently
        assert store.record_selector_success("acme.com", "click", "#y") is False


class TestLoginFlow:
    def test_record_and_read(self, tmp_path):
        store = _store(tmp_path)
        flow = [{"action": "input", "field": "email"}, {"action": "click", "target": "#login"}]
        assert store.record_login_flow("login.acme.com", flow=flow, auth_type="oauth")
        out = store.get_login_flow("login.acme.com")
        assert out is not None
        assert out["auth_type"] == "oauth"
        assert out["flow"] == flow
        assert out["fail_count"] == 0
        assert out["last_success_at"]

    def test_failure_increments_counter_without_overwriting(self, tmp_path):
        store = _store(tmp_path)
        store.record_login_flow("acme.com", flow=[{"a": 1}], success=True)
        store.record_login_flow("acme.com", flow=[{"a": 2}], success=False)
        out = store.get_login_flow("acme.com")
        assert out is not None
        assert out["fail_count"] == 1
        # flow_json should be unchanged (failure path doesn't overwrite)
        assert out["flow"] == [{"a": 1}]
        assert out["last_failure_at"]

    def test_missing_domain_returns_none(self, tmp_path):
        assert _store(tmp_path).get_login_flow("nope.com") is None


class TestTemplates:
    def test_record_and_read(self, tmp_path):
        store = _store(tmp_path)
        sequence = [{"click": "#search"}, {"input": "query"}]
        assert store.record_template("search.com", "basic_search", sequence)
        templates = store.get_templates("search.com")
        assert len(templates) == 1
        assert templates[0]["template_name"] == "basic_search"
        assert templates[0]["sequence"] == sequence
        assert templates[0]["hit_count"] == 1

    def test_repeat_record_increments_hit_count(self, tmp_path):
        store = _store(tmp_path)
        for _ in range(4):
            store.record_template("search.com", "basic", [{"x": 1}])
        templates = store.get_templates("search.com")
        assert templates[0]["hit_count"] == 4

    def test_blank_domain_or_name_rejected(self, tmp_path):
        store = _store(tmp_path)
        assert store.record_template("", "x", []) is False
        assert store.record_template("acme.com", "", []) is False
