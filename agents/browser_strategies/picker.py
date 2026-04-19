"""StrategyPicker — single-point strategy selection for B6.

Selection order (highest to lowest priority):

1. ``LoginReplayStrategy`` — when the current domain has a stored login
   flow AND the task looks auth-related. Yields to the next strategy on
   failure.
2. ``BatchExecuteStrategy`` — when ``BROWSER_BATCH_EXECUTE_ENABLED=true``.
   Yields to the next strategy if action-sequence generation fails.
3. ``UnifiedActStrategy`` — when ``BROWSER_UNIFIED_ACT_ENABLED=true``.
4. ``LegacyPerStepStrategy`` — always-last terminal fallback.

Callers iterate through ``build_chain()`` and take the first strategy
whose ``execute`` returns a non-None result.
"""
from __future__ import annotations

from typing import Any, List, Optional
from urllib.parse import urlparse

from config.settings import settings

from agents.browser_strategies.base import DecisionStrategy, StrategyContext
from agents.browser_strategies.batch import BatchExecuteStrategy
from agents.browser_strategies.legacy import LegacyPerStepStrategy
from agents.browser_strategies.login_replay import LoginReplayStrategy
from agents.browser_strategies.unified import UnifiedActStrategy


class StrategyPicker:
    """Resolves the ordered list of strategies for one run.

    The class is stateless but gets its own instance per run so future
    extensions (per-domain overrides, telemetry) have somewhere to live
    without mutating module globals.
    """

    def build_chain(
        self,
        agent: Any,
        ctx: StrategyContext,
    ) -> List[DecisionStrategy]:
        chain: List[DecisionStrategy] = []

        login = self._maybe_login_replay(agent, ctx)
        if login is not None:
            chain.append(login)

        if settings.BROWSER_BATCH_EXECUTE_ENABLED:
            chain.append(BatchExecuteStrategy())
        elif settings.BROWSER_UNIFIED_ACT_ENABLED:
            chain.append(UnifiedActStrategy())

        # Legacy loop is the always-present terminal strategy.
        chain.append(LegacyPerStepStrategy())
        return chain

    # ── internal selection rules ───────────────────────────────

    def _maybe_login_replay(
        self,
        agent: Any,
        ctx: StrategyContext,
    ) -> Optional[DecisionStrategy]:
        if not settings.BROWSER_PLAN_MEMORY_ENABLED or not settings.BROWSER_LOGIN_REPLAY_ENABLED:
            return None

        intent_type = str(getattr(ctx.task_intent, "intent_type", "") or "").lower()
        task_text = str(ctx.task or "").lower()
        looks_auth = intent_type == "auth" or any(
            kw in task_text for kw in ("login", "log in", "sign in", "signin", "登录", "登陆")
        )
        if not looks_auth:
            return None

        domain = self._domain_for(ctx.expected_url or getattr(ctx, "current_url", ""))
        if not domain:
            return None

        try:
            from utils.site_knowledge_store import get_site_knowledge_store
            store = get_site_knowledge_store()
            if store is None:
                return None
            record = store.get_login_flow(domain)
        except Exception:
            return None

        if not record or not record.get("flow"):
            return None

        return LoginReplayStrategy(domain=domain)

    @staticmethod
    def _domain_for(url: str) -> str:
        if not url:
            return ""
        text = url if "://" in url else f"http://{url}"
        try:
            host = urlparse(text).hostname or ""
        except ValueError:
            return ""
        return host.lower()
