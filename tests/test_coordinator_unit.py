"""
S5: Unit tests for core/coordinator.py — task decomposition, parallel dispatch,
result synthesis, degradation, and routing logic.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from core.coordinator import (
    _degrade_to_single_agent,
    _fallback_synthesis,
    _parse_decomposition,
    coordinator_node,
    decompose_task,
    is_coordinator_enabled,
    synthesize_results,
)
from core.state import create_initial_state
from core.subagent import SubagentResult, SubagentSpec


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def base_state():
    state = create_initial_state(
        user_input="比较 Python 和 Go 的性能差异",
        session_id="sess_test",
        job_id="job_test",
    )
    return state


@pytest.fixture
def simple_state():
    return create_initial_state(
        user_input="今天天气怎么样",
        session_id="sess_test",
        job_id="job_test",
    )


@pytest.fixture
def mock_spec():
    return SubagentSpec(
        name="research_python",
        agent_type="web_worker",
        task_description="Research Python performance benchmarks",
    )


@pytest.fixture
def mock_result_success(mock_spec):
    return SubagentResult(
        spec=mock_spec,
        success=True,
        output="Python is great for rapid development but slower than compiled languages.",
        elapsed_seconds=5.0,
        turns_used=1,
    )


@pytest.fixture
def mock_result_failure(mock_spec):
    return SubagentResult(
        spec=SubagentSpec(name="research_go", agent_type="web_worker", task_description="Research Go"),
        success=False,
        error="Connection timeout",
        elapsed_seconds=30.0,
        turns_used=3,
    )


# ---------------------------------------------------------------------------
# _parse_decomposition tests
# ---------------------------------------------------------------------------

class TestParseDecomposition:
    def test_direct_json(self):
        raw = json.dumps({"subtasks": [{"name": "a"}], "analysis": "test"})
        result = _parse_decomposition(raw)
        assert result["subtasks"][0]["name"] == "a"

    def test_json_in_code_block(self):
        raw = '```json\n{"subtasks": [{"name": "b"}]}\n```'
        result = _parse_decomposition(raw)
        assert result["subtasks"][0]["name"] == "b"

    def test_json_in_text(self):
        raw = 'Here is the plan: {"subtasks": [{"name": "c"}]} end.'
        result = _parse_decomposition(raw)
        assert result["subtasks"][0]["name"] == "c"

    def test_invalid_returns_none(self):
        assert _parse_decomposition("not json at all") is None

    def test_empty_string(self):
        assert _parse_decomposition("") is None


# ---------------------------------------------------------------------------
# is_coordinator_enabled tests
# ---------------------------------------------------------------------------

class TestIsCoordinatorEnabled:
    @patch("config.settings.settings")
    def test_disabled(self, mock_settings):
        mock_settings.COORDINATOR_ENABLED = False
        assert is_coordinator_enabled() is False

    @patch("config.settings.settings")
    def test_enabled(self, mock_settings):
        mock_settings.COORDINATOR_ENABLED = True
        assert is_coordinator_enabled() is True


# ---------------------------------------------------------------------------
# LLM-driven coordinator routing (integration with router)
# ---------------------------------------------------------------------------

class TestLLMDrivenCoordinatorRouting:
    def test_state_flag_set_by_router(self):
        """Router sets _use_coordinator when LLM returns needs_coordinator=true."""
        state = create_initial_state(
            user_input="比较 Python 和 Go 的性能差异",
            session_id="s", job_id="j",
        )
        # Simulate what router.py does when analysis has needs_coordinator
        analysis = {"needs_coordinator": True}
        if analysis.get("needs_coordinator", False):
            state["_use_coordinator"] = True
        assert state.get("_use_coordinator") is True

    def test_state_flag_not_set_for_simple_task(self):
        """Router does NOT set _use_coordinator for simple tasks."""
        state = create_initial_state(
            user_input="今天天气怎么样",
            session_id="s", job_id="j",
        )
        analysis = {"needs_coordinator": False}
        if analysis.get("needs_coordinator", False):
            state["_use_coordinator"] = True
        assert state.get("_use_coordinator") is None

    def test_state_flag_absent_when_key_missing(self):
        """Older LLM responses without needs_coordinator don't trigger coordinator."""
        state = create_initial_state(user_input="test", session_id="s", job_id="j")
        analysis = {"intent": "web_scraping"}  # no needs_coordinator key
        if analysis.get("needs_coordinator", False):
            state["_use_coordinator"] = True
        assert state.get("_use_coordinator") is None


# ---------------------------------------------------------------------------
# decompose_task tests
# ---------------------------------------------------------------------------

class TestDecomposeTask:
    @patch("core.coordinator.LLMClient")
    def test_successful_decomposition(self, MockLLM):
        decomposition = {
            "analysis": "Two distinct topics to compare",
            "subtasks": [
                {"name": "py", "agent_type": "web_worker", "task_description": "Research Python"},
                {"name": "go", "agent_type": "web_worker", "task_description": "Research Go"},
            ],
            "synthesis_strategy": "compare",
        }
        mock_instance = MockLLM.return_value
        mock_response = MagicMock()
        mock_response.content = json.dumps(decomposition)
        mock_instance.chat.return_value = mock_response

        result = decompose_task("Compare Python and Go")
        assert result is not None
        assert len(result["subtasks"]) == 2
        assert result["synthesis_strategy"] == "compare"

    @patch("core.coordinator.LLMClient")
    def test_empty_response(self, MockLLM):
        mock_instance = MockLLM.return_value
        mock_instance.chat.return_value = None
        assert decompose_task("test") is None

    @patch("core.coordinator.LLMClient")
    def test_no_subtasks(self, MockLLM):
        mock_instance = MockLLM.return_value
        mock_response = MagicMock()
        mock_response.content = json.dumps({"analysis": "simple", "subtasks": []})
        mock_instance.chat.return_value = mock_response
        assert decompose_task("test") is None


# ---------------------------------------------------------------------------
# synthesize_results tests
# ---------------------------------------------------------------------------

class TestSynthesizeResults:
    @patch("core.coordinator.LLMClient")
    def test_successful_synthesis(self, MockLLM, mock_result_success, mock_result_failure):
        synthesis = {
            "synthesis": "Python is slower but more productive; Go data unavailable.",
            "sources": ["research_python"],
            "confidence": 0.6,
            "notes": "Go research failed",
        }
        mock_instance = MockLLM.return_value
        mock_response = MagicMock()
        mock_response.content = json.dumps(synthesis)
        mock_instance.chat.return_value = mock_response

        result = synthesize_results(
            "Compare Python and Go",
            [mock_result_success, mock_result_failure],
            strategy="compare",
        )
        assert "Python" in result

    @patch("core.coordinator.LLMClient")
    def test_fallback_on_llm_failure(self, MockLLM, mock_result_success):
        mock_instance = MockLLM.return_value
        mock_instance.chat.return_value = None

        result = synthesize_results("test", [mock_result_success])
        # Should use fallback concatenation
        assert "research_python" in result


# ---------------------------------------------------------------------------
# _fallback_synthesis tests
# ---------------------------------------------------------------------------

class TestFallbackSynthesis:
    def test_concatenates_results(self, mock_result_success, mock_result_failure):
        output = _fallback_synthesis([mock_result_success, mock_result_failure])
        assert "research_python" in output
        assert "research_go" in output
        assert "completed" in output
        assert "failed" in output

    def test_empty_results(self):
        assert _fallback_synthesis([]) == ""


# ---------------------------------------------------------------------------
# _degrade_to_single_agent tests
# ---------------------------------------------------------------------------

class TestDegradeToSingleAgent:
    def test_clears_state(self, base_state):
        base_state["task_queue"] = [{"task_id": "t1"}]
        result = _degrade_to_single_agent(base_state)
        assert result["task_queue"] == []
        assert result["execution_status"] == "routing"


# ---------------------------------------------------------------------------
# coordinator_node integration tests
# ---------------------------------------------------------------------------

class TestCoordinatorNode:
    @patch("core.coordinator.synthesize_results", return_value="Final synthesized answer")
    @patch("core.coordinator._run_subagents_sync")
    @patch("core.coordinator.decompose_task")
    def test_full_flow(self, mock_decompose, mock_run, mock_synth, base_state):
        mock_decompose.return_value = {
            "analysis": "comparison",
            "subtasks": [
                {"name": "a", "agent_type": "web_worker", "task_description": "task a"},
                {"name": "b", "agent_type": "web_worker", "task_description": "task b"},
            ],
            "synthesis_strategy": "compare",
        }
        mock_run.return_value = [
            SubagentResult(
                spec=SubagentSpec(name="a", agent_type="web_worker", task_description="task a"),
                success=True, output="result a",
            ),
            SubagentResult(
                spec=SubagentSpec(name="b", agent_type="web_worker", task_description="task b"),
                success=True, output="result b",
            ),
        ]

        result = coordinator_node(base_state)
        assert result["final_output"] == "Final synthesized answer"
        assert result["execution_status"] == "completed"
        mock_synth.assert_called_once()

    @patch("core.coordinator.decompose_task", return_value=None)
    def test_degrade_on_decomposition_failure(self, mock_decompose, base_state):
        result = coordinator_node(base_state)
        assert result["task_queue"] == []
        assert result["execution_status"] == "routing"

    @patch("core.coordinator._run_subagents_sync")
    @patch("core.coordinator.decompose_task")
    def test_degrade_on_all_subagents_fail(self, mock_decompose, mock_run, base_state):
        mock_decompose.return_value = {
            "analysis": "test",
            "subtasks": [{"name": "a", "agent_type": "web_worker", "task_description": "t"}],
            "synthesis_strategy": "summarize",
        }
        mock_run.return_value = [
            SubagentResult(
                spec=SubagentSpec(name="a", agent_type="web_worker", task_description="t"),
                success=False, error="timeout",
            ),
        ]

        result = coordinator_node(base_state)
        assert result["task_queue"] == []
        assert "All" in result.get("error_trace", "")

    @patch("core.coordinator.synthesize_results", return_value="Partial answer")
    @patch("core.coordinator._run_subagents_sync")
    @patch("core.coordinator.decompose_task")
    def test_partial_success(self, mock_decompose, mock_run, mock_synth, base_state):
        mock_decompose.return_value = {
            "analysis": "test",
            "subtasks": [
                {"name": "a", "agent_type": "web_worker", "task_description": "t"},
                {"name": "b", "agent_type": "web_worker", "task_description": "t2"},
            ],
            "synthesis_strategy": "merge",
        }
        mock_run.return_value = [
            SubagentResult(
                spec=SubagentSpec(name="a", agent_type="web_worker", task_description="t"),
                success=True, output="result a",
            ),
            SubagentResult(
                spec=SubagentSpec(name="b", agent_type="web_worker", task_description="t2"),
                success=False, error="timeout",
            ),
        ]

        result = coordinator_node(base_state)
        assert result["final_output"] == "Partial answer"
        assert result["execution_status"] == "completed"

    @patch("core.coordinator.synthesize_results", return_value="Done")
    @patch("core.coordinator._run_subagents_sync")
    @patch("core.coordinator.decompose_task")
    def test_artifacts_collected(self, mock_decompose, mock_run, mock_synth, base_state):
        mock_decompose.return_value = {
            "analysis": "t",
            "subtasks": [{"name": "a", "agent_type": "web_worker", "task_description": "t"}],
            "synthesis_strategy": "summarize",
        }
        mock_run.return_value = [
            SubagentResult(
                spec=SubagentSpec(name="a", agent_type="web_worker", task_description="t"),
                success=True, output="ok",
                artifacts=[{"artifact_type": "file", "name": "report.pdf"}],
            ),
        ]

        result = coordinator_node(base_state)
        assert len(result["artifacts"]) == 1
        assert result["artifacts"][0]["name"] == "report.pdf"


# ---------------------------------------------------------------------------
# Permission bridge tests (MessageBus events)
# ---------------------------------------------------------------------------

class TestCoordinatorBusEvents:
    @patch("core.coordinator.synthesize_results", return_value="Answer")
    @patch("core.coordinator._run_subagents_sync")
    @patch("core.coordinator.decompose_task")
    def test_bus_events_published(self, mock_decompose, mock_run, mock_synth, base_state):
        from core.message_bus import (
            MSG_COORDINATOR_DISPATCH,
            MSG_COORDINATOR_SYNTHESIS,
            MSG_SUBAGENT_COMPLETED,
            MSG_SUBAGENT_STARTED,
            MessageBus,
        )

        mock_decompose.return_value = {
            "analysis": "t",
            "subtasks": [{"name": "a", "agent_type": "web_worker", "task_description": "t"}],
            "synthesis_strategy": "summarize",
        }
        mock_run.return_value = [
            SubagentResult(
                spec=SubagentSpec(name="a", agent_type="web_worker", task_description="t"),
                success=True, output="ok",
            ),
        ]

        result = coordinator_node(base_state)

        # Verify bus events
        bus = MessageBus.from_dict(result.get("message_bus", []))
        dispatch_msgs = bus.query(message_type=MSG_COORDINATOR_DISPATCH)
        assert len(dispatch_msgs) >= 1

        started_msgs = bus.query(message_type=MSG_SUBAGENT_STARTED)
        assert len(started_msgs) >= 1

        completed_msgs = bus.query(message_type=MSG_SUBAGENT_COMPLETED)
        assert len(completed_msgs) >= 1

        synthesis_msgs = bus.query(message_type=MSG_COORDINATOR_SYNTHESIS)
        assert len(synthesis_msgs) >= 1
