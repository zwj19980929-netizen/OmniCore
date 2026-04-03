"""
Unit tests for core/context_reinject.py (S2-3: 压缩后状态重注入)
"""
import os
import pytest

from core.context_reinject import (
    build_reinject_messages,
    _extract_plan_state,
    _extract_artifact_refs,
    _extract_session_memory,
    _PLAN_MAX_CHARS,
)


# ---------------------------------------------------------------------------
# build_reinject_messages
# ---------------------------------------------------------------------------

def test_reinject_empty_state():
    """Empty state should produce no messages."""
    result = build_reinject_messages({})
    assert result == []


def test_reinject_with_artifacts_in_task_queue():
    """Should include artifact references when task results contain file paths."""
    state = {
        "job_id": "",
        "session_id": "",
        "task_queue": [
            {
                "description": "Download report",
                "status": "completed",
                "result": {"file_path": "/tmp/report.pdf"},
            },
        ],
    }
    result = build_reinject_messages(state)
    assert len(result) == 1
    content = result[0].content
    assert "[上下文恢复]" in content
    assert "/tmp/report.pdf" in content


def test_reinject_with_plan(tmp_path, monkeypatch):
    """Should include plan state when a plan file exists."""
    # Create a fake plan file
    plans_dir = tmp_path / "data" / "plans"
    plans_dir.mkdir(parents=True)
    plan_file = plans_dir / "test-job.md"
    plan_file.write_text("# Plan\n- Task 1: done\n- Task 2: pending", encoding="utf-8")

    monkeypatch.setattr("core.plan_manager.PLANS_DIR", str(plans_dir))

    state = {"job_id": "test-job", "session_id": "", "task_queue": []}
    result = build_reinject_messages(state)
    assert len(result) == 1
    assert "Task 1: done" in result[0].content


# ---------------------------------------------------------------------------
# _extract_plan_state
# ---------------------------------------------------------------------------

def test_extract_plan_no_job_id():
    assert _extract_plan_state({}) == ""
    assert _extract_plan_state({"job_id": ""}) == ""


def test_extract_plan_truncates_long_plan(tmp_path, monkeypatch):
    plans_dir = tmp_path / "data" / "plans"
    plans_dir.mkdir(parents=True)
    plan_file = plans_dir / "long-job.md"
    plan_file.write_text("X" * (_PLAN_MAX_CHARS + 5000), encoding="utf-8")
    monkeypatch.setattr("core.plan_manager.PLANS_DIR", str(plans_dir))

    result = _extract_plan_state({"job_id": "long-job"})
    assert len(result) <= _PLAN_MAX_CHARS + 50  # +50 for truncation marker
    assert "[...plan truncated]" in result


# ---------------------------------------------------------------------------
# _extract_artifact_refs
# ---------------------------------------------------------------------------

def test_extract_artifacts_empty():
    assert _extract_artifact_refs({}) == ""
    assert _extract_artifact_refs({"task_queue": []}) == ""


def test_extract_artifacts_from_results():
    state = {
        "task_queue": [
            {"description": "Fetch", "result": {"path": "/tmp/a.csv"}},
            {"description": "Generate", "result": {"output_file": "/tmp/b.xlsx"}},
            {"description": "No result", "result": {"success": True}},
        ],
    }
    text = _extract_artifact_refs(state)
    assert "/tmp/a.csv" in text
    assert "/tmp/b.xlsx" in text


def test_extract_artifacts_nested():
    state = {
        "task_queue": [
            {
                "description": "Multi-output",
                "result": {
                    "artifacts": [
                        {"name": "chart.png"},
                        {"path": "data.json"},
                    ]
                },
            },
        ],
    }
    text = _extract_artifact_refs(state)
    assert "chart.png" in text
    assert "data.json" in text


# ---------------------------------------------------------------------------
# _extract_session_memory
# ---------------------------------------------------------------------------

def test_extract_memory_disabled(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "SESSION_MEMORY_ENABLED", False)
    assert _extract_session_memory({"session_id": "s1"}) == ""


def test_extract_memory_no_session_id(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "SESSION_MEMORY_ENABLED", True)
    assert _extract_session_memory({}) == ""
    assert _extract_session_memory({"session_id": ""}) == ""
