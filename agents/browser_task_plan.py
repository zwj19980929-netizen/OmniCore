"""P1: Task-level plan object for BrowserAgent.

The BrowserAgent used to regenerate its next-step decision from scratch at every
tick, which caused the "proposes the same plan over and over" symptom. This
module introduces an explicit list of plan steps that the agent advances
through. Each tick becomes "push the current step forward", not "decide what
to do from zero".
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from config.settings import settings
from utils.logger import log_warning
from utils.prompt_manager import get_prompt


TASK_PLAN_PROMPT = get_prompt("browser_task_plan", "") or ""
STEP_ADVANCE_PROMPT = get_prompt("browser_step_advance", "") or ""


STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_DONE = "done"
STATUS_SKIPPED = "skipped"


@dataclass
class PlanStep:
    index: int
    goal: str
    success_criteria: str = ""
    hint: str = ""
    status: str = STATUS_PENDING

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "goal": self.goal,
            "success_criteria": self.success_criteria,
            "hint": self.hint,
            "status": self.status,
        }


@dataclass
class AdvanceDecision:
    advance: bool = False
    skip: bool = False
    need_replan: bool = False
    reason: str = ""


@dataclass
class TaskPlan:
    task: str
    steps: List[PlanStep] = field(default_factory=list)
    current_index: int = 0
    revisions: int = 0

    # ── Accessors ─────────────────────────────────────────

    def current_step(self) -> Optional[PlanStep]:
        if 0 <= self.current_index < len(self.steps):
            return self.steps[self.current_index]
        return None

    def completed_steps(self) -> List[PlanStep]:
        return [s for s in self.steps if s.status in {STATUS_DONE, STATUS_SKIPPED}]

    def remaining_steps(self) -> List[PlanStep]:
        return [s for s in self.steps if s.status == STATUS_PENDING]

    def is_complete(self) -> bool:
        return all(s.status in {STATUS_DONE, STATUS_SKIPPED} for s in self.steps) if self.steps else False

    # ── Mutation ──────────────────────────────────────────

    def mark_current(self, status: str) -> None:
        step = self.current_step()
        if step is None:
            return
        step.status = status

    def advance(self) -> Optional[PlanStep]:
        cur = self.current_step()
        if cur is not None and cur.status in {STATUS_PENDING, STATUS_ACTIVE}:
            cur.status = STATUS_DONE
        self.current_index += 1
        nxt = self.current_step()
        if nxt is not None and nxt.status == STATUS_PENDING:
            nxt.status = STATUS_ACTIVE
        return nxt

    def skip_current(self) -> Optional[PlanStep]:
        cur = self.current_step()
        if cur is not None:
            cur.status = STATUS_SKIPPED
        self.current_index += 1
        nxt = self.current_step()
        if nxt is not None and nxt.status == STATUS_PENDING:
            nxt.status = STATUS_ACTIVE
        return nxt

    # ── Prompt-facing helpers ─────────────────────────────

    def format_for_prompt(self) -> str:
        if not self.steps:
            return "(empty plan)"
        cur = self.current_step()
        lines: List[str] = [f"Task: {self.task[:200]}"]
        lines.append(f"Revisions: {self.revisions}")
        if cur is not None:
            lines.append(
                f"Current step [{cur.index}]: {cur.goal} | success: {cur.success_criteria} | hint: {cur.hint}"
            )
        else:
            lines.append("Current step: (none — plan may be complete)")
        done = self.completed_steps()
        if done:
            lines.append("Completed:")
            for s in done:
                lines.append(f"  - [{s.index}] ({s.status}) {s.goal}")
        remaining = [s for s in self.steps if s.status == STATUS_PENDING and s.index != (cur.index if cur else -1)]
        if remaining:
            lines.append("Remaining:")
            for s in remaining:
                lines.append(f"  - [{s.index}] {s.goal}")
        return "\n".join(lines)

    def to_debug_payload(self) -> Dict[str, Any]:
        return {
            "task": self.task,
            "current_index": self.current_index,
            "revisions": self.revisions,
            "steps": [s.to_dict() for s in self.steps],
        }


# ── LLM-driven helpers ─────────────────────────────────────────


def _coerce_steps(raw_steps: Any, max_steps: int) -> List[PlanStep]:
    steps: List[PlanStep] = []
    if not isinstance(raw_steps, list):
        return steps
    for idx, item in enumerate(raw_steps[:max_steps]):
        if not isinstance(item, dict):
            continue
        goal = str(item.get("goal") or item.get("description") or "").strip()
        if not goal:
            continue
        steps.append(PlanStep(
            index=idx,
            goal=goal[:200],
            success_criteria=str(item.get("success_criteria") or "").strip()[:200],
            hint=str(item.get("hint") or "").strip()[:200],
            status=STATUS_ACTIVE if idx == 0 else STATUS_PENDING,
        ))
    return steps


async def build_initial_plan(
    task: str,
    intent_type: str,
    llm,
    start_url: str = "",
) -> Optional[TaskPlan]:
    """Build the initial TaskPlan by asking the LLM. Returns None on failure."""
    if not settings.BROWSER_PLAN_ENABLED or not TASK_PLAN_PROMPT:
        return None
    max_steps = max(1, settings.BROWSER_MAX_PLAN_STEPS)
    try:
        prompt = TASK_PLAN_PROMPT.format(
            task=task or "",
            intent=intent_type or "unknown",
            start_url=start_url or "",
            max_steps=max_steps,
        )
        response = await llm.achat(
            messages=[
                {"role": "system", "content": "Return JSON only."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            json_mode=True,
        )
        payload = llm.parse_json_response(response) or {}
        raw_steps = payload.get("steps", [])
        steps = _coerce_steps(raw_steps, max_steps)
        if not steps:
            return None
        return TaskPlan(task=task, steps=steps)
    except Exception as exc:
        log_warning(f"build_initial_plan failed: {exc}")
        return None


async def step_advance(
    plan: TaskPlan,
    observation_summary: str,
    llm,
) -> AdvanceDecision:
    """Ask the LLM whether the current step is done / needs replan."""
    if plan is None or plan.current_step() is None or not STEP_ADVANCE_PROMPT:
        return AdvanceDecision()
    cur = plan.current_step()
    try:
        prompt = STEP_ADVANCE_PROMPT.format(
            task=plan.task[:240],
            step_index=cur.index,
            step_goal=cur.goal[:200],
            step_success=cur.success_criteria[:200],
            step_hint=cur.hint[:200],
            observation=observation_summary[:1200] or "(no observation)",
        )
        response = await llm.achat(
            messages=[
                {"role": "system", "content": "Return JSON only."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            json_mode=True,
        )
        payload = llm.parse_json_response(response) or {}
        return AdvanceDecision(
            advance=bool(payload.get("advance", False)),
            skip=bool(payload.get("skip", False)),
            need_replan=bool(payload.get("need_replan", False)),
            reason=str(payload.get("reason", ""))[:200],
        )
    except Exception as exc:
        log_warning(f"step_advance failed: {exc}")
        return AdvanceDecision()


async def replan(
    plan: TaskPlan,
    failure_reason: str,
    llm,
) -> bool:
    """Rewrite remaining steps via the LLM. Returns True if the plan was changed."""
    if plan is None or not TASK_PLAN_PROMPT:
        return False
    if plan.revisions >= max(0, settings.BROWSER_MAX_REPLANS):
        return False
    max_steps = max(1, settings.BROWSER_MAX_PLAN_STEPS)
    completed = plan.completed_steps()
    remaining_budget = max(1, max_steps - len(completed))
    try:
        context = (
            f"Previously completed: "
            f"{json.dumps([s.to_dict() for s in completed], ensure_ascii=False)}\n"
            f"Failure reason: {failure_reason[:200]}"
        )
        prompt = TASK_PLAN_PROMPT.format(
            task=plan.task or "",
            intent=context[:400],
            start_url="",
            max_steps=remaining_budget,
        )
        response = await llm.achat(
            messages=[
                {"role": "system", "content": "Return JSON only."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            json_mode=True,
        )
        payload = llm.parse_json_response(response) or {}
        raw_steps = payload.get("steps", [])
        new_tail = _coerce_steps(raw_steps, remaining_budget)
        if not new_tail:
            return False
        # Renumber new steps to start after completed ones.
        offset = len(completed)
        for i, s in enumerate(new_tail):
            s.index = offset + i
            s.status = STATUS_ACTIVE if i == 0 else STATUS_PENDING
        plan.steps = completed + new_tail
        plan.current_index = offset
        plan.revisions += 1
        return True
    except Exception as exc:
        log_warning(f"replan failed: {exc}")
        return False
