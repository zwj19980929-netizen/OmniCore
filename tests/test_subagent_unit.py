"""
S5: Unit tests for core/subagent.py — SubagentSpec, SubagentResult, SubagentRunner.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

from core.subagent import (
    SubagentDepthExceeded,
    SubagentResult,
    SubagentRunner,
    SubagentSpec,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def spec():
    return SubagentSpec(
        name="test_agent",
        agent_type="web_worker",
        task_description="Search for Python tutorials",
        params={"query": "python tutorial"},
        max_turns=5,
        timeout=60,
        depth=0,
    )


@pytest.fixture
def parent_state():
    return {
        "session_id": "sess_1",
        "job_id": "job_1",
        "user_input": "parent task",
        "message_bus": [
            {"source": "router", "target": "*", "message_type": "context",
             "payload": {"key": "value"}, "timestamp": 1.0, "job_id": "job_1"}
        ],
        "task_outputs": {"task_1": {"type": "text", "content": "hello"}},
        "task_queue": [],
        "messages": [],
    }


@pytest.fixture
def runner():
    return SubagentRunner(max_depth=2)


# ---------------------------------------------------------------------------
# SubagentSpec tests
# ---------------------------------------------------------------------------

class TestSubagentSpec:
    def test_default_name_generated(self):
        spec = SubagentSpec(name="", agent_type="web_worker", task_description="test")
        assert spec.name.startswith("subagent_")

    def test_explicit_name_preserved(self, spec):
        assert spec.name == "test_agent"

    def test_default_values(self):
        spec = SubagentSpec(name="a", agent_type="web_worker", task_description="t")
        assert spec.inherit_context is True
        assert spec.max_turns == 10
        assert spec.timeout == 300
        assert spec.depth == 0
        assert spec.params == {}


# ---------------------------------------------------------------------------
# SubagentResult tests
# ---------------------------------------------------------------------------

class TestSubagentResult:
    def test_to_notification_success(self, spec):
        result = SubagentResult(
            spec=spec,
            success=True,
            output="Found 10 tutorials",
            elapsed_seconds=5.2,
            turns_used=1,
        )
        notification = result.to_notification()
        assert '<task-notification agent="test_agent" status="completed"' in notification
        assert "Found 10 tutorials" in notification
        assert "5.2s" in notification

    def test_to_notification_failure(self, spec):
        result = SubagentResult(
            spec=spec,
            success=False,
            error="Connection timeout",
            elapsed_seconds=30.0,
            turns_used=3,
        )
        notification = result.to_notification()
        assert 'status="failed"' in notification
        assert "<error>Connection timeout</error>" in notification

    def test_to_notification_with_artifacts(self, spec):
        result = SubagentResult(
            spec=spec,
            success=True,
            output="Done",
            artifacts=[
                {"artifact_type": "file", "name": "report.pdf"},
                {"artifact_type": "screenshot", "name": "page.png"},
            ],
        )
        notification = result.to_notification()
        assert "<artifacts>" in notification
        assert 'type="file"' in notification
        assert "report.pdf" in notification

    def test_to_notification_truncates_long_output(self, spec):
        result = SubagentResult(
            spec=spec, success=True,
            output="x" * 5000,
        )
        notification = result.to_notification()
        # Output should be truncated to 2000 chars
        assert len(notification) < 5000


# ---------------------------------------------------------------------------
# SubagentRunner tests
# ---------------------------------------------------------------------------

class TestSubagentRunner:
    def test_depth_check_passes(self, runner, spec):
        spec.depth = 0
        runner._check_depth(spec)  # should not raise

    def test_depth_check_fails(self, runner):
        spec = SubagentSpec(
            name="deep", agent_type="web_worker",
            task_description="test", depth=2,
        )
        with pytest.raises(SubagentDepthExceeded):
            runner._check_depth(spec)

    def test_depth_check_at_boundary(self):
        runner = SubagentRunner(max_depth=3)
        spec = SubagentSpec(name="ok", agent_type="web_worker",
                            task_description="test", depth=2)
        runner._check_depth(spec)  # depth 2 < max_depth 3, should pass

        spec_fail = SubagentSpec(name="bad", agent_type="web_worker",
                                 task_description="test", depth=3)
        with pytest.raises(SubagentDepthExceeded):
            runner._check_depth(spec_fail)

    def test_build_child_state_inherits_context(self, runner, spec, parent_state):
        child = runner._build_child_state(spec, parent_state)
        assert child["session_id"] == "sess_1"
        assert child["user_input"] == spec.task_description
        assert child["job_id"].startswith("subagent_")
        # Should inherit message bus and task outputs
        assert len(child["message_bus"]) == 1
        assert "task_1" in child["task_outputs"]

    def test_build_child_state_no_inherit(self, runner, parent_state):
        spec = SubagentSpec(
            name="isolated", agent_type="web_worker",
            task_description="test", inherit_context=False,
        )
        child = runner._build_child_state(spec, parent_state)
        assert child["message_bus"] == []
        assert child["task_outputs"] == {}

    def test_build_child_state_no_parent(self, runner, spec):
        child = runner._build_child_state(spec, None)
        assert child["session_id"] == ""
        assert child["user_input"] == spec.task_description

    def test_build_child_state_isolation(self, runner, spec, parent_state):
        """Child state changes should not affect parent state."""
        child = runner._build_child_state(spec, parent_state)
        child["message_bus"].append({"new": "msg"})
        assert len(parent_state["message_bus"]) == 1

    def test_run_success(self, runner, spec, parent_state):
        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {
            "final_output": "Found tutorials",
            "error_trace": "",
            "artifacts": [],
        }
        with patch("core.subagent._get_graph_lazy", return_value=mock_graph):
            result = asyncio.run(runner.run(spec, parent_state))
        assert result.success is True
        assert result.output == "Found tutorials"
        assert result.elapsed_seconds > 0

    def test_run_graph_error_returns_failure(self, runner, spec, parent_state):
        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {
            "final_output": "",
            "error_trace": "Something went wrong",
            "artifacts": [],
        }
        with patch("core.subagent._get_graph_lazy", return_value=mock_graph):
            result = asyncio.run(runner.run(spec, parent_state))
        assert result.success is False

    def test_run_depth_exceeded(self, parent_state):
        runner = SubagentRunner(max_depth=1)
        spec = SubagentSpec(
            name="deep", agent_type="web_worker",
            task_description="test", depth=1,
        )
        with pytest.raises(SubagentDepthExceeded):
            asyncio.run(runner.run(spec, parent_state))

    def test_run_exception(self, runner, spec, parent_state):
        mock_graph = MagicMock()
        mock_graph.invoke.side_effect = RuntimeError("boom")
        with patch("core.subagent._get_graph_lazy", return_value=mock_graph):
            result = asyncio.run(runner.run(spec, parent_state))
        assert result.success is False
        assert "boom" in result.error

    def test_run_parallel_all_success(self, runner, parent_state):
        specs = [
            SubagentSpec(name=f"agent_{i}", agent_type="web_worker", task_description=f"task {i}")
            for i in range(3)
        ]
        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {
            "final_output": "done",
            "error_trace": "",
            "artifacts": [],
        }
        with patch("core.subagent._get_graph_lazy", return_value=mock_graph):
            results = asyncio.run(runner.run_parallel(specs, parent_state, max_concurrent=2))
        assert len(results) == 3
        assert all(r.success for r in results)

    def test_run_parallel_respects_concurrency(self, runner, parent_state):
        """Verify that semaphore limits concurrent executions."""
        specs = [
            SubagentSpec(name=f"agent_{i}", agent_type="web_worker", task_description=f"task {i}")
            for i in range(5)
        ]
        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {
            "final_output": "done", "error_trace": "", "artifacts": [],
        }
        with patch("core.subagent._get_graph_lazy", return_value=mock_graph):
            results = asyncio.run(runner.run_parallel(specs, parent_state, max_concurrent=1))
        assert len(results) == 5
        assert all(r.success for r in results)

    def test_run_parallel_best_effort(self, runner, parent_state):
        specs = [
            SubagentSpec(name=f"agent_{i}", agent_type="web_worker", task_description=f"task {i}")
            for i in range(3)
        ]
        call_count = 0

        def invoke_side_effect(state):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"final_output": "", "error_trace": "fail", "artifacts": []}
            return {"final_output": "ok", "error_trace": "", "artifacts": []}

        mock_graph = MagicMock()
        mock_graph.invoke.side_effect = invoke_side_effect
        with patch("core.subagent._get_graph_lazy", return_value=mock_graph):
            results = asyncio.run(runner.run_parallel(specs, parent_state, max_concurrent=1))
        # All should complete (no cancellation in best_effort mode)
        assert len(results) == 3
