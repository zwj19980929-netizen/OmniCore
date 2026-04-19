"""Unit tests for ``utils.browser_template_recorder`` (B1 tail hook)."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import patch

from utils.browser_template_recorder import (
    _domain_for,
    _pick_template_name,
    _simplify_step,
    record_template_from_run,
)


class _RecordingStore:
    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def record_template(self, domain: str, template_name: str, sequence: Any) -> bool:
        self.calls.append(
            {"domain": domain, "template_name": template_name, "sequence": sequence}
        )
        return True


def _enable(monkeypatch, enabled: bool = True):
    from config.settings import settings as _settings

    monkeypatch.setattr(_settings, "BROWSER_PLAN_MEMORY_ENABLED", enabled)


# ── _domain_for ──────────────────────────────────────────────

def test_domain_for_extracts_hostname():
    assert _domain_for("https://www.acme.com/path?q=1") == "www.acme.com"


def test_domain_for_handles_bare_host():
    assert _domain_for("acme.com") == "acme.com"


def test_domain_for_empty():
    assert _domain_for("") == ""


# ── _pick_template_name ──────────────────────────────────────

def test_pick_template_name_for_search():
    assert _pick_template_name(SimpleNamespace(intent_type="search")) == "search"


def test_pick_template_name_for_read_is_empty():
    assert _pick_template_name(SimpleNamespace(intent_type="read")) == ""


def test_pick_template_name_for_unknown_is_empty():
    assert _pick_template_name(SimpleNamespace(intent_type="")) == ""


# ── _simplify_step ───────────────────────────────────────────

def test_simplify_step_keeps_click():
    step = {
        "action_type": "click",
        "selector": "#submit",
        "description": "submit the form",
        "result": "success",
    }
    out = _simplify_step(step)
    assert out == {
        "action_type": "click",
        "description": "submit the form",
        "selector": "#submit",
    }


def test_simplify_step_drops_wait():
    assert _simplify_step({"action_type": "wait", "selector": "#x"}) is None


def test_simplify_step_drops_failed():
    step = {"action_type": "click", "selector": "#x", "result": "failed"}
    assert _simplify_step(step) is None


def test_simplify_step_drops_explicit_success_false():
    step = {"action_type": "click", "selector": "#x", "success": False}
    assert _simplify_step(step) is None


def test_simplify_step_drops_when_no_selector_or_ref_for_click():
    assert _simplify_step({"action_type": "click"}) is None


def test_simplify_step_keeps_navigate_without_selector():
    out = _simplify_step({"action_type": "navigate", "value": "https://x.com"})
    assert out is not None
    assert out["action_type"] == "navigate"


def test_simplify_step_preserves_value():
    step = {"action_type": "input", "selector": "#q", "value": "hello"}
    out = _simplify_step(step)
    assert out["value"] == "hello"


def test_simplify_step_truncates_long_value():
    step = {"action_type": "input", "selector": "#q", "value": "a" * 300}
    out = _simplify_step(step)
    assert len(out["value"]) == 120


# ── record_template_from_run ─────────────────────────────────

def test_noop_when_memory_disabled(monkeypatch):
    _enable(monkeypatch, enabled=False)
    store = _RecordingStore()
    with patch("utils.site_knowledge_store.get_site_knowledge_store", return_value=store):
        ok = record_template_from_run(
            task_intent=SimpleNamespace(intent_type="search"),
            steps=[{"action_type": "click", "selector": "#a", "result": "success"}],
            final_url="https://acme.com/",
            success=True,
        )
    assert ok is False
    assert store.calls == []


def test_noop_when_run_failed(monkeypatch):
    _enable(monkeypatch)
    store = _RecordingStore()
    with patch("utils.site_knowledge_store.get_site_knowledge_store", return_value=store):
        ok = record_template_from_run(
            task_intent=SimpleNamespace(intent_type="search"),
            steps=[
                {"action_type": "input", "selector": "#q", "value": "hi", "result": "success"},
                {"action_type": "click", "selector": "#go", "result": "success"},
            ],
            final_url="https://acme.com/",
            success=False,
        )
    assert ok is False


def test_noop_when_intent_not_template_worthy(monkeypatch):
    _enable(monkeypatch)
    store = _RecordingStore()
    with patch("utils.site_knowledge_store.get_site_knowledge_store", return_value=store):
        ok = record_template_from_run(
            task_intent=SimpleNamespace(intent_type="read"),
            steps=[
                {"action_type": "click", "selector": "#a", "result": "success"},
                {"action_type": "click", "selector": "#b", "result": "success"},
            ],
            final_url="https://acme.com/",
            success=True,
        )
    assert ok is False
    assert store.calls == []


def test_noop_when_sequence_too_short(monkeypatch):
    _enable(monkeypatch)
    store = _RecordingStore()
    with patch("utils.site_knowledge_store.get_site_knowledge_store", return_value=store):
        ok = record_template_from_run(
            task_intent=SimpleNamespace(intent_type="search"),
            steps=[{"action_type": "click", "selector": "#only", "result": "success"}],
            final_url="https://acme.com/",
            success=True,
        )
    assert ok is False


def test_noop_when_no_domain(monkeypatch):
    _enable(monkeypatch)
    store = _RecordingStore()
    with patch("utils.site_knowledge_store.get_site_knowledge_store", return_value=store):
        ok = record_template_from_run(
            task_intent=SimpleNamespace(intent_type="search"),
            steps=[
                {"action_type": "click", "selector": "#a", "result": "success"},
                {"action_type": "click", "selector": "#b", "result": "success"},
            ],
            final_url="",
            success=True,
        )
    assert ok is False


def test_records_search_template(monkeypatch):
    _enable(monkeypatch)
    store = _RecordingStore()
    with patch("utils.site_knowledge_store.get_site_knowledge_store", return_value=store):
        ok = record_template_from_run(
            task_intent=SimpleNamespace(intent_type="search"),
            steps=[
                {"action_type": "input", "selector": "#q", "value": "hello", "result": "success"},
                {"action_type": "click", "selector": "#search", "result": "success"},
                # noisy steps that must be filtered
                {"action_type": "wait", "selector": "#x"},
                {"action_type": "extract", "result": "success"},
                {"action_type": "click", "selector": "#failing", "result": "failed"},
            ],
            final_url="https://acme.com/?q=hello",
            success=True,
        )
    assert ok is True
    assert len(store.calls) == 1
    call = store.calls[0]
    assert call["domain"] == "acme.com"
    assert call["template_name"] == "search"
    seq = call["sequence"]
    assert len(seq) == 2
    assert seq[0]["action_type"] == "input"
    assert seq[0]["value"] == "hello"
    assert seq[1]["action_type"] == "click"


def test_records_navigate_allowed_without_selector(monkeypatch):
    _enable(monkeypatch)
    store = _RecordingStore()
    with patch("utils.site_knowledge_store.get_site_knowledge_store", return_value=store):
        ok = record_template_from_run(
            task_intent=SimpleNamespace(intent_type="navigate"),
            steps=[
                {"action_type": "navigate", "value": "https://x.com/a", "result": "success"},
                {"action_type": "click", "selector": "#b", "result": "success"},
            ],
            final_url="https://x.com/a",
            success=True,
        )
    assert ok is True
    assert store.calls[0]["template_name"] == "navigate"


def test_store_none_is_noop(monkeypatch):
    _enable(monkeypatch)
    with patch("utils.site_knowledge_store.get_site_knowledge_store", return_value=None):
        ok = record_template_from_run(
            task_intent=SimpleNamespace(intent_type="search"),
            steps=[
                {"action_type": "input", "selector": "#q", "value": "hi", "result": "success"},
                {"action_type": "click", "selector": "#go", "result": "success"},
            ],
            final_url="https://acme.com/",
            success=True,
        )
    assert ok is False


def test_handles_store_exception(monkeypatch):
    _enable(monkeypatch)

    class _Boom:
        def record_template(self, *a, **kw):
            raise RuntimeError("db down")

    with patch("utils.site_knowledge_store.get_site_knowledge_store", return_value=_Boom()):
        ok = record_template_from_run(
            task_intent=SimpleNamespace(intent_type="search"),
            steps=[
                {"action_type": "input", "selector": "#q", "value": "hi", "result": "success"},
                {"action_type": "click", "selector": "#go", "result": "success"},
            ],
            final_url="https://acme.com/",
            success=True,
        )
    assert ok is False
