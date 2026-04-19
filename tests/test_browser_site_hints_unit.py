"""
Unit tests for ``BrowserDecisionLayer._build_site_hints_block`` (B1).

Only exercises the prompt-block builder — the surrounding decision layer
is not instantiated (it pulls in heavy dependencies). We access the
method through ``BrowserDecisionLayer.__dict__`` with a synthetic ``self``
stub.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agents.browser_decision import BrowserDecisionLayer


def _invoke(url: str, *, store_ret=None, error: bool = False) -> str:
    """Invoke the unbound ``_build_site_hints_block`` on a minimal self stub."""
    # The method uses only ``self`` as a dispatch anchor — no attributes read.
    fn = BrowserDecisionLayer._build_site_hints_block

    class _FakeStore:
        def get_selector_hints(self, url_arg, **kw):
            if error:
                raise RuntimeError("store down")
            return list(store_ret or [])

    def _fake_getter():
        if error:
            raise RuntimeError("getter boom")
        return _FakeStore() if store_ret is not None else None

    with patch("utils.site_knowledge_store.get_site_knowledge_store", _fake_getter):
        return fn(SimpleNamespace(), url)


class TestBuildSiteHintsBlock:
    def _enable(self, monkeypatch, *, memory=True, inject=True):
        from config.settings import settings as _settings
        monkeypatch.setattr(_settings, "BROWSER_PLAN_MEMORY_ENABLED", memory)
        monkeypatch.setattr(_settings, "BROWSER_SITE_HINTS_INJECT", inject)

    def test_empty_when_memory_disabled(self, monkeypatch):
        self._enable(monkeypatch, memory=False)
        assert _invoke("https://acme.com", store_ret=[{"role": "click", "selector": "#x",
                                                       "hit_count": 1, "success_rate": 1.0}]) == ""

    def test_empty_when_inject_disabled(self, monkeypatch):
        self._enable(monkeypatch, inject=False)
        assert _invoke("https://acme.com", store_ret=[{"role": "click", "selector": "#x",
                                                       "hit_count": 1, "success_rate": 1.0}]) == ""

    def test_empty_when_store_unavailable(self, monkeypatch):
        self._enable(monkeypatch)
        assert _invoke("https://acme.com", store_ret=None) == ""

    def test_empty_when_no_hints(self, monkeypatch):
        self._enable(monkeypatch)
        assert _invoke("https://acme.com", store_ret=[]) == ""

    def test_renders_hints(self, monkeypatch):
        self._enable(monkeypatch)
        hints = [
            {"role": "click", "selector": "#login", "hit_count": 4, "success_rate": 1.0,
             "domain": "acme.com", "fail_count": 0, "last_used_at": ""},
            {"role": "input", "selector": "input[name=email]", "hit_count": 3,
             "success_rate": 0.75, "domain": "acme.com", "fail_count": 1, "last_used_at": ""},
        ]
        block = _invoke("https://acme.com", store_ret=hints)
        assert "Site hints" in block
        assert "#login" in block
        assert "input[name=email]" in block
        assert "hits=4" in block
        assert "success=100%" in block
        assert "success=75%" in block
        # reference-only language
        assert "reference only" in block.lower()

    def test_swallows_errors(self, monkeypatch):
        self._enable(monkeypatch)
        assert _invoke("https://acme.com", error=True) == ""
