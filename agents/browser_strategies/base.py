"""Base contract for BrowserAgent decision strategies (B6)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class StrategyContext:
    """Mutable per-run bundle passed between strategies.

    ``steps`` is shared by reference — strategies that delegate to the
    legacy loop or batch executor rely on in-place mutation, so a
    fallback path can still surface the partial work.
    """

    task: str
    task_intent: Any
    expected_url: str
    steps: List[Dict[str, Any]] = field(default_factory=list)
    max_steps: int = 8
    # Strategy chain bookkeeping — filled by StrategyPicker / orchestrator.
    attempted: List[str] = field(default_factory=list)


class DecisionStrategy(ABC):
    """Abstract strategy executed by ``BrowserAgent._run_with_strategies``.

    Implementations do the heavy lifting (driving the browser, LLM
    calls, data extraction) and return either:

    - a terminal result dict (same shape as ``BrowserAgent.run`` returns),
      which short-circuits the chain; or
    - ``None`` to signal "skip me, try the next strategy".
    """

    #: Stable identifier used in logs / StrategyPicker / tests.
    name: str = "base"

    @abstractmethod
    async def execute(
        self,
        agent: Any,
        ctx: StrategyContext,
    ) -> Optional[Dict[str, Any]]:
        """Run the strategy. Return terminal dict, or ``None`` to fall through."""
        raise NotImplementedError

    async def on_success(self, agent: Any, ctx: StrategyContext, result: Dict[str, Any]) -> None:
        """Hook called by the orchestrator once the chain terminates successfully."""

    async def on_failure(self, agent: Any, ctx: StrategyContext, reason: str) -> None:
        """Hook called when the strategy voluntarily yields or errors out."""
