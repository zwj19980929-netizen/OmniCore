"""BrowserAgent decision strategies (B6).

Encapsulates the three existing decision modes (legacy per-step,
unified-act single-prompt, batch execution) plus a new login-replay
strategy behind a common ``DecisionStrategy`` interface, so
``BrowserAgent.run`` no longer needs inline if/else branching.

Gated by ``BROWSER_STRATEGY_REFACTOR_ENABLED`` — existing code paths
remain untouched when the flag is off.
"""
from agents.browser_strategies.base import (
    DecisionStrategy,
    StrategyContext,
)
from agents.browser_strategies.batch import BatchExecuteStrategy
from agents.browser_strategies.legacy import LegacyPerStepStrategy
from agents.browser_strategies.login_replay import LoginReplayStrategy
from agents.browser_strategies.picker import StrategyPicker
from agents.browser_strategies.unified import UnifiedActStrategy

__all__ = [
    "DecisionStrategy",
    "StrategyContext",
    "BatchExecuteStrategy",
    "LegacyPerStepStrategy",
    "LoginReplayStrategy",
    "StrategyPicker",
    "UnifiedActStrategy",
]
