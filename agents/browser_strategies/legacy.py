"""Legacy per-step decision strategy (B6).

Delegates to ``BrowserAgent._run_per_step_loop`` which contains the
original multi-step loop extracted from ``run()``. Acts as the terminal
fallback — always returns a terminal dict (never ``None``).
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from agents.browser_strategies.base import DecisionStrategy, StrategyContext


class LegacyPerStepStrategy(DecisionStrategy):
    name = "legacy"

    async def execute(
        self,
        agent: Any,
        ctx: StrategyContext,
    ) -> Optional[Dict[str, Any]]:
        ctx.attempted.append(self.name)
        return await agent._run_per_step_loop(
            ctx.task, ctx.task_intent, ctx.expected_url, ctx.steps, ctx.max_steps,
        )
