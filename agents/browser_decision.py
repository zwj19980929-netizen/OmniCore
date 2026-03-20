"""
BrowserDecisionLayer - Decides next browser action based on current state and task context.

This is Layer 2 of the three-layer BrowserAgent architecture.
Responsibilities:
- Action planning (unified plan, page assessment, LLM decision, local heuristics)
- Loop/cycle detection
- Action validation against current observation
- Action sanitization
- "Is task done?" determination
- URL matching / target page detection
- Search result ranking and click target selection

This layer should be as pure as possible (no browser side effects).
All browser interactions go through the execution layer.
"""
import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from config.settings import settings
from core.llm import LLMClient
from utils.logger import log_agent_action, log_warning
from utils.prompt_manager import get_prompt
from utils.search_engine_profiles import (
    decode_search_redirect_url,
    is_search_engine_domain,
    looks_like_search_results_url,
)
from utils.web_prompt_budget import BudgetSection, render_budgeted_sections
from utils.text_relevance import extract_relevant_text_safe_async
import utils.web_debug_recorder as web_debug_recorder

from agents.browser_agent import (
    ActionType,
    BrowserAction,
    PageElement,
    PageObservation,
    PageState,
    SearchResultCard,
    TaskIntent,
    _AUTH_PASSWORD_ALIASES,
    _AUTH_EMAIL_ALIASES,
    _AUTH_USERNAME_ALIASES,
    _AUTH_SUBMIT_POSITIVE_TOKENS,
    _AUTH_SUBMIT_NEGATIVE_TOKENS,
    _AUTH_SECONDARY_PROVIDER_TOKENS,
    _NON_TEXT_INPUT_TYPES,
    _QUERY_STOP_TOKENS,
)

# Load prompts
ACTION_DECISION_PROMPT = get_prompt("browser_action_decision")
PAGE_ASSESSMENT_PROMPT = get_prompt("browser_page_assessment")
VISION_ACTION_PROMPT = get_prompt("browser_vision_decision")
UNIFIED_PLAN_PROMPT = get_prompt("browser_unified_plan")


class BrowserDecisionLayer:
    """Decides next browser action based on current state and task context.

    This layer contains all decision-making logic: action planning via LLM,
    local heuristic fallbacks, cycle detection, action validation, and
    goal satisfaction checks. It does NOT perform any browser side effects.
    """

    def __init__(
        self,
        llm_client_getter,
        agent_name: str = "BrowserAgent",
    ):
        self._get_llm = llm_client_getter
        self.name = agent_name

        # Action history for loop detection
        self._action_history: List[str] = []

        # Page assessment cache
        self._page_assessment_cache: Dict[str, Optional[BrowserAction]] = {}

    # ── Text / normalization helpers ─────────────────────────

    @staticmethod
    def _normalize_text(value: str) -> str:
        return re.sub(r"\s+", " ", (value or "").strip()).lower()

    @staticmethod
    def _strip_urls_from_text(text: str) -> str:
        raw = str(text or "")
        for match in re.finditer(r"https?://[^\s\u4e00-\u9fff]+", raw, flags=re.IGNORECASE):
            candidate = str(match.group(0) or "")
            if "???" in candidate:
                candidate = candidate.split("???", 1)[0]
            elif candidate.count("?") > 1 and "=" not in candidate and "&" not in candidate:
                candidate = candidate.split("?", 1)[0]
            candidate = candidate.rstrip(
                ".,);]}>\"'?!:" + "\uFF0C\u3002\uFF01\uFF1F\uFF1B\uFF1A\u3001\uFF09\u300B\u300D\u300F"
            )
            if candidate:
                raw = raw.replace(candidate, " ")
        return raw

    # ── Action signature & history ───────────────────────────

    def _action_signature(self, action: BrowserAction) -> str:
        return "|".join([
            action.action_type.value,
            action.target_ref[:80],
            action.target_selector[:80],
            self._normalize_text(action.value)[:80],
            self._normalize_text(action.description)[:80],
        ])

    def record_action(self, action: BrowserAction) -> None:
        self._action_history.append(self._action_signature(action))
        self._action_history = self._action_history[-6:]

    def reset_history(self) -> None:
        self._action_history = []
        self._page_assessment_cache.clear()

    # ── Cycle detection ──────────────────────────────────────

    def is_action_looping(self, action: BrowserAction, threshold: int = 3) -> bool:
        """Detect if action is stuck in a loop."""
        recent_actions = self._action_history[-5:] if len(self._action_history) >= 5 else self._action_history
        action_sig = self._action_signature(action)

        recent_count = recent_actions.count(action_sig)
        if action.action_type == ActionType.WAIT:
            return recent_count >= max(threshold + 2, 5)

        if recent_count >= threshold:
            return True

        if len(self._action_history) >= 2:
            last_two = self._action_history[-2:]
            if all(sig == action_sig for sig in last_two):
                return True

        return False

    # ── Action validation ────────────────────────────────────

    def validate_action(
        self, action: Optional[BrowserAction], observation: PageObservation
    ) -> Optional[BrowserAction]:
        """Lightweight validation: does target_ref exist? Auto-fix stale versioned refs."""
        if action is None:
            return None
        if action.action_type in {ActionType.DONE, ActionType.FAILED, ActionType.WAIT, ActionType.SCROLL,
                                   ActionType.PRESS_KEY, ActionType.NAVIGATE, ActionType.EXTRACT}:
            return action

        snapshot = observation.snapshot
        elements = snapshot.get("elements") or []
        ref_set = {str(e.get("ref", "") or "") for e in elements if isinstance(e, dict)}
        for card in snapshot.get("cards") or []:
            ref_set.add(str(card.get("ref", "") or ""))
            ref_set.add(str(card.get("target_ref", "") or ""))
        for ctrl in snapshot.get("controls") or []:
            ref_set.add(str(ctrl.get("ref", "") or ""))
        ref_set.discard("")

        if action.target_ref and action.target_ref not in ref_set:
            ref = action.target_ref
            bare_ref = ref.split(":", 1)[1] if ":" in ref else ref
            current_version = observation.snapshot_version

            versioned = f"{current_version}:{bare_ref}"
            if versioned in ref_set:
                log_warning(f"resolved stale ref '{ref}' -> '{versioned}'")
                action.target_ref = versioned
            else:
                log_warning(f"action target_ref '{ref}' not in snapshot, attempting name match")
                desc_lower = (action.description or "").lower()
                for elem in elements:
                    if isinstance(elem, dict):
                        text = str(elem.get("text", "") or "").lower()
                        if text and desc_lower and text[:20] in desc_lower:
                            action.target_ref = str(elem.get("ref", "") or "")
                            action.target_selector = str(elem.get("selector", "") or "")
                            break

        return action

    # ── Action sanitization ──────────────────────────────────
    # NOTE: sanitize_planned_action, _clone_action, and many helpers are
    # delegated from the orchestrator. In this first pass, the orchestrator
    # still calls the original methods on BrowserAgent. These will be
    # migrated in a future pass.
    # TODO: Move _sanitize_planned_action, _clone_action, and related
    # methods here in a follow-up refactor.

    # ── Formatting helpers for LLM prompts ───────────────────
    # These are used by the decision layer when constructing LLM prompts.
    # In this first pass they remain as delegated calls from the orchestrator.
    # TODO: Move formatting methods here in a follow-up refactor.

    # ── Goal satisfaction ────────────────────────────────────
    # TODO: Move _task_looks_satisfied, _page_data_satisfies_goal here.

    # ── Search result ranking ────────────────────────────────
    # TODO: Move _find_search_result_click_action, _score_search_result_card here.

    # ── Planning delegation entry points ─────────────────────
    # The actual planning methods (_plan_next_action, _unified_plan_action,
    # _assess_page_with_llm, _decide_action_with_llm, _decide_action_locally,
    # _choose_observation_driven_action) remain on BrowserAgent for now and
    # are called by the orchestrator. They will be migrated here in a
    # follow-up refactor once the layer boundaries are stable.
    # TODO: Move all planning methods here.
