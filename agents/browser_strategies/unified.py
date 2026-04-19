"""Unified single-prompt decision strategy (B6, P2).

The unified-prompt mode (``BROWSER_UNIFIED_ACT_ENABLED``) still runs
inside the per-step loop; the flag just changes how
``BrowserDecisionLayer._plan_next_action`` dispatches its LLM calls.

Here we simply delegate to the legacy per-step loop; the flag is read
at decision time, so no extra wiring is required. The separate class
exists so ``StrategyPicker`` can advertise the choice in logs / metrics
without branching on the flag in the orchestrator.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from agents.browser_strategies.base import DecisionStrategy, StrategyContext


class UnifiedActStrategy(DecisionStrategy):
    name = "unified"

    async def execute(
        self,
        agent: Any,
        ctx: StrategyContext,
    ) -> Optional[Dict[str, Any]]:
        ctx.attempted.append(self.name)
        return await agent._run_per_step_loop(
            ctx.task, ctx.task_intent, ctx.expected_url, ctx.steps, ctx.max_steps,
        )
