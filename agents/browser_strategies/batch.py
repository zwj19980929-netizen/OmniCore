"""Batch execution decision strategy (B6, wraps P4)."""
from __future__ import annotations

from typing import Any, Dict, Optional

from agents.browser_strategies.base import DecisionStrategy, StrategyContext
from utils.logger import log_warning


class BatchExecuteStrategy(DecisionStrategy):
    name = "batch"

    async def execute(
        self,
        agent: Any,
        ctx: StrategyContext,
    ) -> Optional[Dict[str, Any]]:
        ctx.attempted.append(self.name)
        result = await agent._run_batch_mode(
            ctx.task, ctx.task_intent, ctx.expected_url, ctx.steps,
        )
        # ``_run_batch_mode`` returns ``None`` when action-sequence generation
        # fails (legacy fall-through contract). Yield to the next strategy.
        if result is None:
            log_warning("BatchExecuteStrategy: sequence generation failed, falling through")
            return None
        return result
