"""Login-replay decision strategy (B6, wraps B1).

Runs ``try_replay_login`` for the page's domain; on success returns a
terminal dict. On skip/failure returns ``None`` so the orchestrator
falls through to ``batch`` / ``unified`` / ``legacy``.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from agents.browser_strategies.base import DecisionStrategy, StrategyContext


class LoginReplayStrategy(DecisionStrategy):
    name = "login_replay"

    def __init__(self, domain: str, credentials: Optional[Dict[str, str]] = None):
        self.domain = (domain or "").strip()
        self.credentials = dict(credentials or {})

    async def execute(
        self,
        agent: Any,
        ctx: StrategyContext,
    ) -> Optional[Dict[str, Any]]:
        ctx.attempted.append(self.name)
        from agents.browser_login_replay import try_replay_login

        replay = await try_replay_login(
            agent, self.domain, credentials=self.credentials,
        )
        if not replay.success:
            return None

        # Surface the replay's step log so downstream consumers (UI, debug
        # recorder) see what happened. We intentionally do not touch
        # ctx.steps, which is reserved for the primary strategy's record.
        tk = getattr(agent, "toolkit", None)
        final_url = ""
        final_title = ""
        try:
            if tk is not None:
                url_r = await tk.get_current_url()
                title_r = await tk.get_title()
                final_url = url_r.data or ""
                final_title = title_r.data or ""
        except Exception:
            pass

        return {
            "success": True,
            "message": f"login replay completed ({len(replay.executed_steps)} steps)",
            "url": final_url,
            "title": final_title,
            "expected_url": ctx.expected_url,
            "steps": [
                {
                    "step": idx + 1,
                    "action_type": s.get("action_type", ""),
                    "selector": s.get("target_selector", ""),
                    "description": f"login replay step {idx + 1}",
                    "result": "success",
                }
                for idx, s in enumerate(replay.executed_steps)
            ],
            "data": [],
            "login_replay": True,
        }
