"""P1: Task-level plan object tests."""
import asyncio
from dataclasses import dataclass

from agents.browser_task_plan import (
    AdvanceDecision,
    PlanStep,
    STATUS_ACTIVE,
    STATUS_DONE,
    STATUS_PENDING,
    STATUS_SKIPPED,
    TaskPlan,
    build_initial_plan,
    replan,
    step_advance,
)


class _FakeLLM:
    """Minimal LLM stub that replays canned JSON payloads."""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.calls = 0

    async def achat(self, messages, temperature=0.0, json_mode=False):
        self.calls += 1
        payload = self._payloads.pop(0) if self._payloads else {}
        return {"payload": payload}

    def parse_json_response(self, response):
        return response.get("payload", {})


def test_taskplan_formats_and_advances():
    plan = TaskPlan(task="find llama license", steps=[
        PlanStep(index=0, goal="search for llama", success_criteria="serp visible", status=STATUS_ACTIVE),
        PlanStep(index=1, goal="click top result", success_criteria="detail page", status=STATUS_PENDING),
        PlanStep(index=2, goal="extract license", success_criteria="license text found", status=STATUS_PENDING),
    ])
    assert plan.current_step().index == 0
    assert not plan.is_complete()

    text = plan.format_for_prompt()
    assert "search for llama" in text
    assert "Remaining:" in text

    plan.advance()
    assert plan.current_step().index == 1
    assert plan.steps[0].status == STATUS_DONE
    assert plan.steps[1].status == STATUS_ACTIVE

    plan.skip_current()
    assert plan.steps[1].status == STATUS_SKIPPED
    assert plan.current_step().index == 2

    plan.advance()
    assert plan.current_step() is None
    assert plan.is_complete()


def test_build_initial_plan_parses_llm_output():
    llm = _FakeLLM([
        {"steps": [
            {"goal": "search meta llama", "success_criteria": "serp", "hint": "use short query"},
            {"goal": "open top result", "success_criteria": "detail loaded"},
            {"goal": "extract license", "success_criteria": "license present"},
        ]}
    ])
    plan = asyncio.run(build_initial_plan("find meta llama license", "search", llm, start_url=""))
    assert plan is not None
    assert len(plan.steps) == 3
    assert plan.steps[0].status == STATUS_ACTIVE
    assert plan.steps[1].status == STATUS_PENDING
    assert plan.steps[0].goal.startswith("search meta llama")


def test_build_initial_plan_returns_none_on_empty_payload():
    llm = _FakeLLM([{"steps": []}])
    plan = asyncio.run(build_initial_plan("x", "read", llm))
    assert plan is None


def test_step_advance_recognizes_completion():
    plan = TaskPlan(task="t", steps=[PlanStep(index=0, goal="g", success_criteria="criteria", status=STATUS_ACTIVE)])
    llm = _FakeLLM([{"advance": True, "reason": "match"}])
    decision = asyncio.run(step_advance(plan, "observation text", llm))
    assert decision.advance is True
    assert decision.reason == "match"


def test_step_advance_signals_replan():
    plan = TaskPlan(task="t", steps=[PlanStep(index=0, goal="g", status=STATUS_ACTIVE)])
    llm = _FakeLLM([{"advance": False, "need_replan": True, "reason": "stuck"}])
    decision = asyncio.run(step_advance(plan, "obs", llm))
    assert decision.need_replan is True
    assert decision.advance is False


def test_replan_preserves_completed_steps_and_rewrites_tail():
    plan = TaskPlan(
        task="task",
        steps=[
            PlanStep(index=0, goal="step a", status=STATUS_DONE),
            PlanStep(index=1, goal="step b", status=STATUS_ACTIVE),
            PlanStep(index=2, goal="step c", status=STATUS_PENDING),
        ],
        current_index=1,
        revisions=0,
    )
    llm = _FakeLLM([
        {"steps": [
            {"goal": "alternative b", "success_criteria": "..."},
            {"goal": "alternative c", "success_criteria": "..."},
        ]}
    ])
    changed = asyncio.run(replan(plan, "reason: stuck", llm))
    assert changed is True
    assert plan.revisions == 1
    # First completed step preserved
    assert plan.steps[0].goal == "step a"
    assert plan.steps[0].status == STATUS_DONE
    # Tail rewritten
    assert plan.steps[1].goal == "alternative b"
    assert plan.steps[1].status == STATUS_ACTIVE
    assert plan.steps[2].goal == "alternative c"
    # Indices re-assigned
    assert [s.index for s in plan.steps] == [0, 1, 2]
    assert plan.current_index == 1


def test_replan_blocks_after_max_revisions():
    from config.settings import settings
    plan = TaskPlan(
        task="t", steps=[PlanStep(index=0, goal="g", status=STATUS_ACTIVE)],
        revisions=settings.BROWSER_MAX_REPLANS,
    )
    llm = _FakeLLM([{"steps": [{"goal": "new", "success_criteria": "x"}]}])
    changed = asyncio.run(replan(plan, "x", llm))
    assert changed is False
    assert llm.calls == 0
