"""
Unit tests for memory/episode_store.py (C1 Episodic Replay).
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from memory import episode_store as es


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    """Use an isolated SQLite file per test and force-enable the feature."""
    from config.settings import settings as _s
    db = tmp_path / "episodes.db"
    monkeypatch.setattr(_s, "EPISODE_REPLAY_ENABLED", True)
    monkeypatch.setattr(_s, "EPISODE_REPLAY_DB", str(db))
    monkeypatch.setattr(_s, "EPISODE_REPLAY_MAX_AGE_DAYS", 60)
    monkeypatch.setattr(_s, "EPISODE_REPLAY_MIN_SIMILARITY", 0.45)
    monkeypatch.setattr(_s, "EPISODE_REPLAY_MAX_DAG_STEPS", 8)
    es.reset_episode_store_singleton_for_tests()
    yield
    es.reset_episode_store_singleton_for_tests()


def _make_state(
    *,
    user_input: str,
    intent: str = "web_scraping",
    statuses=("completed",),
    execution_status: str = "completed",
    critic_approved: bool = True,
    cost_usd: float = 0.012,
    elapsed_ms: int = 4200,
    llm_calls: int = 5,
):
    task_queue = []
    for i, status in enumerate(statuses, start=1):
        task_queue.append(
            {
                "task_id": f"t{i}",
                "task_type": "web_worker",
                "tool_name": "web_worker",
                "description": f"step {i}",
                "params": {"query": f"query {i}"},
                "status": status,
                "result": {"summary": f"result {i}"},
            }
        )
    return {
        "user_input": user_input,
        "current_intent": intent,
        "execution_status": execution_status,
        "critic_approved": critic_approved,
        "task_queue": task_queue,
        "total_cost_usd": cost_usd,
        "elapsed_ms": elapsed_ms,
        "llm_call_count": llm_calls,
        "job_id": "job_xyz",
        "session_id": "sess_abc",
    }


# ─────────────────────── compute_task_signature ───────────────────────


def test_signature_is_stable_for_same_intent_and_input():
    s1 = es.compute_task_signature("web_scraping", "下载 arxiv 论文并摘要")
    s2 = es.compute_task_signature("web_scraping", "下载 arxiv 论文并摘要")
    assert s1 == s2
    assert s1.startswith("web_scraping:")


def test_signature_differs_when_intent_changes():
    a = es.compute_task_signature("web_scraping", "x y z")
    b = es.compute_task_signature("file_operation", "x y z")
    assert a != b


def test_signature_handles_empty_input():
    sig = es.compute_task_signature("", "")
    assert sig.startswith("unknown:")


# ─────────────────────── _compress_plan_dag ───────────────────────


def test_compress_plan_dag_truncates_step_count():
    queue = [
        {"tool_name": "web_worker", "description": "d", "params": {}, "result": {}, "status": "completed"}
        for _ in range(20)
    ]
    out = es._compress_plan_dag(queue, max_steps=5)
    assert len(out) == 5


def test_compress_plan_dag_extracts_brief_input_and_output():
    queue = [
        {
            "tool_name": "browser_agent",
            "description": "open and click",
            "params": {"url": "https://example.com/very/long/" + ("x" * 500)},
            "result": {"summary": "got it " + ("y" * 500)},
            "status": "completed",
        }
    ]
    out = es._compress_plan_dag(queue)
    assert out[0]["tool"] == "browser_agent"
    assert out[0]["input"].startswith("https://example.com")
    assert len(out[0]["input"]) <= 241
    assert out[0]["output"].startswith("got it")
    assert len(out[0]["output"]) <= 161


def test_compress_plan_dag_skips_non_dict_entries():
    out = es._compress_plan_dag([None, "not a dict", {"tool_name": "x", "params": {}, "result": {}}])
    assert len(out) == 1


# ─────────────────────── record_episode + outcome classification ───────────────────────


def test_record_success_episode_writes_row():
    store = es.get_episode_store()
    state = _make_state(user_input="hello world download arxiv", statuses=("completed", "completed"))
    rid = store.record_episode(state)
    assert isinstance(rid, int) and rid > 0
    rows = store.search_similar(
        user_input="hello world download arxiv", top_k=5, min_similarity=0.0
    )
    assert len(rows) == 1
    assert rows[0]["outcome"] == "success"
    assert rows[0]["cost_usd"] == pytest.approx(0.012)
    assert rows[0]["elapsed_ms"] == 4200
    assert rows[0]["llm_calls"] == 5
    assert len(rows[0]["plan_dag"]) == 2


def test_record_skips_when_disabled(monkeypatch):
    from config.settings import settings as _s
    monkeypatch.setattr(_s, "EPISODE_REPLAY_ENABLED", False)
    store = es.get_episode_store()
    rid = store.record_episode(_make_state(user_input="xyz"))
    assert rid is None


def test_record_skips_empty_task_queue():
    store = es.get_episode_store()
    state = _make_state(user_input="x", statuses=())
    state["task_queue"] = []
    assert store.record_episode(state) is None


def test_outcome_partial_when_mixed_status():
    store = es.get_episode_store()
    state = _make_state(
        user_input="partial run abc",
        statuses=("completed", "failed"),
        execution_status="completed_with_issues",
        critic_approved=False,
    )
    store.record_episode(state)
    rows = store.search_similar(user_input="partial run abc", top_k=5, min_similarity=0.0)
    assert rows[0]["outcome"] == "partial"


def test_outcome_fail_when_only_failed_tasks():
    store = es.get_episode_store()
    state = _make_state(
        user_input="all failed run",
        statuses=("failed", "failed"),
        execution_status="completed_with_issues",
        critic_approved=False,
    )
    store.record_episode(state)
    rows = store.search_similar(user_input="all failed run", top_k=5, min_similarity=0.0)
    assert rows[0]["outcome"] == "fail"


# ─────────────────────── search_similar ───────────────────────


def test_search_filters_by_min_similarity():
    store = es.get_episode_store()
    store.record_episode(_make_state(user_input="alpha beta gamma delta"))
    out = store.search_similar(
        user_input="something completely unrelated",
        top_k=5,
        min_similarity=0.5,
    )
    assert out == []


def test_search_filters_by_outcome():
    store = es.get_episode_store()
    store.record_episode(_make_state(user_input="kw shared topic one"))
    store.record_episode(
        _make_state(
            user_input="kw shared topic two",
            statuses=("failed",),
            execution_status="completed_with_issues",
            critic_approved=False,
        )
    )
    success_only = store.search_similar(
        user_input="kw shared topic", top_k=5, min_similarity=0.0, require_outcome="success"
    )
    fail_only = store.search_similar(
        user_input="kw shared topic", top_k=5, min_similarity=0.0, require_outcome="fail"
    )
    assert len(success_only) == 1 and success_only[0]["outcome"] == "success"
    assert len(fail_only) == 1 and fail_only[0]["outcome"] == "fail"


def test_fetch_brief_pair_returns_one_success_one_failure():
    store = es.get_episode_store()
    store.record_episode(_make_state(user_input="topic alpha success run"))
    store.record_episode(
        _make_state(
            user_input="topic alpha failure run",
            statuses=("failed",),
            execution_status="completed_with_issues",
            critic_approved=False,
        )
    )
    pair = store.fetch_brief_pair(user_input="topic alpha", min_similarity=0.0)
    outcomes = sorted(item["outcome"] for item in pair)
    assert outcomes == ["fail", "success"]


def test_search_excludes_rows_older_than_max_age():
    store = es.get_episode_store()
    store.record_episode(_make_state(user_input="old episode tokens here"))
    # 手工把 created_at 改成 100 天前
    with store._connect() as conn:
        cutoff = (datetime.now() - timedelta(days=100)).isoformat(timespec="seconds")
        conn.execute("UPDATE episodes SET created_at = ?", (cutoff,))
    out = store.search_similar(
        user_input="old episode tokens here",
        top_k=5,
        min_similarity=0.0,
        max_age_days=60,
    )
    assert out == []


def test_purge_older_than_removes_aged_rows():
    store = es.get_episode_store()
    store.record_episode(_make_state(user_input="aged"))
    with store._connect() as conn:
        cutoff = (datetime.now() - timedelta(days=200)).isoformat(timespec="seconds")
        conn.execute("UPDATE episodes SET created_at = ?", (cutoff,))
    removed = store.purge_older_than(max_age_days=60)
    assert removed == 1


def test_search_top_k_caps_results():
    store = es.get_episode_store()
    for i in range(5):
        store.record_episode(_make_state(user_input=f"common topic variant {i} foo bar"))
    out = store.search_similar(user_input="common topic foo bar", top_k=2, min_similarity=0.0)
    assert len(out) == 2


# ─────────────────────── update_lessons ───────────────────────


def test_update_lessons_persists_text():
    store = es.get_episode_store()
    rid = store.record_episode(_make_state(user_input="lesson topic foo bar"))
    store.update_lessons(rid, "Avoid step 3 retry; switch to login_replay first")
    rows = store.search_similar(user_input="lesson topic foo bar", top_k=1, min_similarity=0.0)
    assert "login_replay" in rows[0]["lessons"]


# ─────────────────────── format_episode_brief ───────────────────────


def test_format_episode_brief_empty_returns_empty_string():
    assert es.format_episode_brief([]) == ""


def test_format_episode_brief_includes_outcome_marker_and_steps():
    sample = [
        {
            "outcome": "success",
            "user_input": "do something foo",
            "plan_dag": [
                {"tool": "web_worker", "intent": "search", "status": "completed"},
                {"tool": "browser_agent", "intent": "open", "status": "completed"},
            ],
            "cost_usd": 0.018,
            "elapsed_ms": 42_000,
            "lessons": "",
        },
        {
            "outcome": "fail",
            "user_input": "do something else",
            "plan_dag": [
                {"tool": "browser_agent", "intent": "click", "status": "failed"},
            ],
            "cost_usd": 0.0,
            "elapsed_ms": 0,
            "lessons": "needs login_replay",
        },
    ]
    out = es.format_episode_brief(sample)
    assert "[success]" in out
    assert "[failure]" in out
    assert "web_worker" in out
    assert "$0.0180" in out
    assert "login_replay" in out
