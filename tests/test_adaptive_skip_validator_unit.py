"""Unit tests for F1: AdaptiveSkip ↔ Validator state sync fix."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from core.graph_utils import apply_adaptive_skip


def _make_state(*tasks):
    return {"task_queue": list(tasks)}


def _pending(result=None, **kwargs):
    t = {"status": "pending", "task_id": "t1", "description": "test", "result": result}
    t.update(kwargs)
    return t


# ---------------------------------------------------------------------------
# apply_adaptive_skip tests
# ---------------------------------------------------------------------------

class TestApplyAdaptiveSkip:
    def test_result_none_gets_overwritten(self):
        task = _pending(result=None)
        state = _make_state(task)
        apply_adaptive_skip(state)
        assert isinstance(task["result"], dict)
        assert task["result"]["skipped_by_adaptive_reroute"] is True
        assert task["result"]["success"] is True

    def test_top_level_flag_set(self):
        task = _pending(result=None)
        apply_adaptive_skip(_make_state(task))
        assert task.get("skipped_by_adaptive_reroute") is True

    def test_status_becomes_completed(self):
        task = _pending(result=None)
        apply_adaptive_skip(_make_state(task))
        assert task["status"] == "completed"

    def test_missing_result_key(self):
        task = {"status": "pending", "task_id": "t1", "description": "x"}
        apply_adaptive_skip(_make_state(task))
        assert task["result"]["skipped_by_adaptive_reroute"] is True
        assert task["result"]["success"] is True

    def test_existing_dict_result_preserved(self):
        task = _pending(result={"existing_field": 42})
        apply_adaptive_skip(_make_state(task))
        assert task["result"]["existing_field"] == 42
        assert task["result"]["skipped_by_adaptive_reroute"] is True

    def test_non_pending_tasks_untouched(self):
        done = {"status": "completed", "task_id": "t2", "result": {"success": True}}
        pending = _pending(result=None)
        apply_adaptive_skip(_make_state(done, pending))
        assert done["result"] == {"success": True}
        assert pending["result"]["skipped_by_adaptive_reroute"] is True


# ---------------------------------------------------------------------------
# Validator early-return tests
# ---------------------------------------------------------------------------

class TestValidatorSkipRecognition:
    @pytest.fixture
    def validator(self):
        from agents.validator import Validator
        return Validator()

    def test_top_level_flag_passes(self, validator):
        task = {
            "status": "completed",
            "task_id": "t1",
            "description": "x",
            "skipped_by_adaptive_reroute": True,
            "result": None,
        }
        vr = validator.validate_task(task)
        assert vr["passed"] is True
        assert vr["failure_type"] is None
        assert "skipped_by_adaptive_reroute" in vr["issues"]

    def test_nested_flag_passes(self, validator):
        task = {
            "status": "completed",
            "task_id": "t1",
            "description": "x",
            "result": {"skipped_by_adaptive_reroute": True, "success": True},
        }
        vr = validator.validate_task(task)
        assert vr["passed"] is True

    def test_result_none_without_skip_still_fails(self, validator):
        task = {
            "status": "completed",
            "task_id": "t1",
            "description": "x",
            "result": None,
        }
        vr = validator.validate_task(task)
        assert vr["passed"] is False

    def test_full_pipeline_skip_then_validate(self, validator):
        task = _pending(result=None)
        apply_adaptive_skip(_make_state(task))
        vr = validator.validate_task(task)
        assert vr["passed"] is True
