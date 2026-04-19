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
import asyncio
import hashlib
import json
import re
from collections import OrderedDict
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
from utils.url_utils import extract_first_url
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
    _AUTH_VALUE_NOISE_TOKENS,
    _NON_TEXT_INPUT_TYPES,
    _QUERY_STOP_TOKENS,
    _STRUCTURED_PAIR_SKIP_KEYS,
)

# Load prompts
ACTION_DECISION_PROMPT = get_prompt("browser_action_decision")
PAGE_ASSESSMENT_PROMPT = get_prompt("browser_page_assessment")
VISION_ACTION_PROMPT = get_prompt("browser_vision_decision")
UNIFIED_PLAN_PROMPT = get_prompt("browser_unified_plan")
# P2: single consolidated prompt (optional, controlled by BROWSER_UNIFIED_ACT_ENABLED)
BROWSER_ACT_PROMPT = get_prompt("browser_act", "") or ""
# P1: task-level plan prompts
TASK_PLAN_PROMPT = get_prompt("browser_task_plan", "") or ""
STEP_ADVANCE_PROMPT = get_prompt("browser_step_advance", "") or ""

# Load section-based prompt files for task-specific rules and stage hints
REFLECTION_PROMPT = get_prompt("browser_reflection") or ""
_TASK_RULES_RAW = get_prompt("browser_task_rules") or ""
_STAGE_HINTS_RAW = get_prompt("browser_stage_hints") or ""


def _parse_sections(raw: str) -> Dict[str, str]:
    """Parse a section-based prompt file into {section_name: content}."""
    sections: Dict[str, str] = {}
    current_key = ""
    lines: List[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if current_key:
                sections[current_key] = "\n".join(lines).strip()
            current_key = stripped[1:-1]
            lines = []
        else:
            lines.append(line)
    if current_key:
        sections[current_key] = "\n".join(lines).strip()
    return sections


_TASK_RULES = _parse_sections(_TASK_RULES_RAW)
_STAGE_HINTS = _parse_sections(_STAGE_HINTS_RAW)


def _get_task_specific_rules(intent_type: str) -> str:
    """Return task-specific decision rules for the given intent type."""
    return _TASK_RULES.get(intent_type, _TASK_RULES.get("default", ""))


def _get_stage_hint(stage: str) -> str:
    """Return stage-specific hint for the given page stage."""
    return _STAGE_HINTS.get(stage, _STAGE_HINTS.get("default", ""))


class BrowserDecisionLayer:
    """Decides next browser action based on current state and task context.

    This layer contains all decision-making logic: action planning via LLM,
    local heuristic fallbacks, cycle detection, action validation, and
    goal satisfaction checks. It does NOT perform any browser side effects.
    """

    def __init__(
        self,
        llm_client_getter,
        perception=None,
        orchestrator=None,
        agent_name: str = "BrowserAgent",
    ):
        self._get_llm = llm_client_getter
        self.perception = perception
        # Temporary back-reference to orchestrator for methods not yet migrated
        # (form-handling chain). Will be removed in Phase 4.
        self._orchestrator = orchestrator
        self.name = agent_name

        # Action history for loop detection
        self._action_history: List[str] = []

        # P0: Step fingerprint memory for dedup. Maps fingerprint -> executed count.
        self._step_fingerprints: "OrderedDict[str, int]" = OrderedDict()
        # P1: Optional task-level plan (lazy-assigned by BrowserAgent)
        self._task_plan = None

        # Page assessment cache
        self._page_assessment_cache: Dict[str, Optional[BrowserAction]] = {}

        # State synced from orchestrator (via _sync_state_to_layers)
        self.last_semantic_snapshot: Optional[Dict[str, Any]] = None
        self.last_observation = None  # PageObservation

        # Pending reflection text injected into LLM prompts (set by _reflect_on_failures)
        self._pending_reflection: str = ""

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
        # P0: also record fingerprint for dedup detection
        try:
            url, stage = self._current_url_and_stage()
            fp = self._fingerprint_action(action, url, stage)
            if fp:
                self._step_fingerprints[fp] = self._step_fingerprints.get(fp, 0) + 1
                self._step_fingerprints.move_to_end(fp)
                max_size = max(1, settings.BROWSER_STEP_MEMORY_SIZE)
                while len(self._step_fingerprints) > max_size:
                    self._step_fingerprints.popitem(last=False)
        except Exception:
            pass
        # B1: record the selector in the site-knowledge store (no-op when
        # BROWSER_PLAN_MEMORY_ENABLED=false, so existing runs pay no cost).
        self._record_site_selector_from_action(action)

    def reset_history(self) -> None:
        self._action_history = []
        self._page_assessment_cache.clear()
        self._step_fingerprints.clear()
        self._task_plan = None

    # ── P0: Action fingerprint & dedup ────────────────────────

    def _current_url_and_stage(self) -> Tuple[str, str]:
        url = ""
        obs = self.last_observation
        if obs is not None:
            url = getattr(obs, "url", "") or ""
        snapshot = self.last_semantic_snapshot or {}
        stage = str(snapshot.get("page_stage", "") or snapshot.get("page_type", "") or "")
        return url, stage

    def _fingerprint_action(
        self, action: Optional[BrowserAction], url: str = "", page_stage: str = ""
    ) -> str:
        if action is None or action.action_type == ActionType.FAILED:
            return ""
        try:
            parsed = urlparse(url or "")
            url_key = f"{parsed.netloc}{parsed.path}"[:120]
        except Exception:
            url_key = (url or "")[:120]
        target = (action.target_ref or action.target_selector or "")[:80]
        value = self._normalize_text(action.value or "")[:80]
        return "|".join([
            url_key, (page_stage or "")[:40],
            action.action_type.value,
            target, value,
        ])

    def _is_repeat_action(self, fp: str) -> bool:
        if not fp:
            return False
        threshold = max(1, settings.BROWSER_DEDUP_THRESHOLD)
        return self._step_fingerprints.get(fp, 0) >= threshold

    def get_repeated_action_signatures(self) -> List[str]:
        """Return list of fingerprints that have been executed ≥ dedup threshold times."""
        threshold = max(1, settings.BROWSER_DEDUP_THRESHOLD)
        return [fp for fp, count in self._step_fingerprints.items() if count >= threshold]

    def format_repeated_actions_for_llm(self) -> str:
        repeats = self.get_repeated_action_signatures()
        if not repeats:
            return "(none)"
        return "\n".join(f"- {sig}" for sig in repeats[-10:])

    # ── B1: Site-knowledge hint injection ──────────────────────

    def _build_site_hints_block(self, url: str) -> str:
        """Return an optional ``## Site hints`` markdown block for LLM prompts.

        Queries ``SiteKnowledgeStore`` for selectors this domain has
        historically succeeded with, and formats them as a reference-only
        hint. Empty string when the feature is disabled, no store is
        available, or no qualifying hints exist.
        """
        if not settings.BROWSER_PLAN_MEMORY_ENABLED or not settings.BROWSER_SITE_HINTS_INJECT:
            return ""
        try:
            from utils.site_knowledge_store import get_site_knowledge_store
            store = get_site_knowledge_store()
            if store is None:
                return ""
            hints = store.get_selector_hints(url or "")
        except Exception:
            return ""
        if not hints:
            return ""
        lines = [
            "",
            "## Site hints (selectors that succeeded here before — reference only, LLM decides):",
        ]
        for h in hints:
            rate_pct = int(round(h["success_rate"] * 100))
            lines.append(
                f"- [{h['role']}] {h['selector']}  (hits={h['hit_count']}, "
                f"success={rate_pct}%)"
            )
        return "\n".join(lines) + "\n"

    def _record_site_selector_from_action(self, action: Optional[BrowserAction]) -> None:
        """B1: best-effort write-back of a successful action's selector.

        Called from ``record_action``. Silently no-ops when the feature is
        disabled, the action has no selector, or the store is unavailable.
        Actions with ``FAILED`` type are skipped entirely.
        """
        if not settings.BROWSER_PLAN_MEMORY_ENABLED:
            return
        if action is None or action.action_type == ActionType.FAILED:
            return
        selector = (action.target_selector or action.target_ref or "").strip()
        if not selector:
            return
        # Use the action type as the role bucket — consistent with how
        # hints are queried by action context downstream.
        role = (action.action_type.value or "").lower()
        url, _ = self._current_url_and_stage()
        if not url:
            return
        try:
            from utils.site_knowledge_store import get_site_knowledge_store
            store = get_site_knowledge_store()
            if store is None:
                return
            store.record_selector_success(url, role, selector)
        except Exception:
            pass

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

    # ── Base utility methods ─────────────────────────────────

    def _extract_task_tokens(self, task: str) -> List[str]:
        return [
            token for token in re.split(r"[^a-zA-Z0-9_\u4e00-\u9fff]+", self._normalize_text(task))
            if len(token) >= 2
        ]

    def _extract_query_tokens(self, query: str) -> List[str]:
        tokens: List[str] = []
        for token in self._extract_task_tokens(query):
            if token in _QUERY_STOP_TOKENS:
                continue
            if token.isdigit():
                continue
            if len(token) < 3 and not any("\u4e00" <= ch <= "\u9fff" for ch in token):
                continue
            if token not in tokens:
                tokens.append(token)
        return tokens[:8]

    @staticmethod
    def _char_ngrams(text: str, n: int = 2) -> set:
        """Extract character n-grams from text for fuzzy Chinese matching."""
        return {text[i:i + n] for i in range(len(text) - n + 1)} if len(text) >= n else set()

    def _score_text_relevance(self, query: str, text: str) -> float:
        """Score relevance of *text* to *query*.  Returns 0.0 ~ 1.0.

        Three signals combined:
        1. Token exact-match ratio  (are query keywords present?)
        2. Character bi-gram overlap (fuzzy match for Chinese synonyms)
        3. Entity/number match bonus (exact figures matter)
        """
        haystack = self._normalize_text(text)
        if not haystack:
            return 0.0

        query_norm = self._normalize_text(query)
        if not query_norm:
            return 0.0

        # --- full query substring match → strong signal ---
        if query_norm in haystack:
            return 1.0

        tokens = self._extract_query_tokens(query)
        if not tokens:
            # No meaningful tokens; fall back to char bi-gram overlap
            q_ngrams = self._char_ngrams(query_norm)
            h_ngrams = self._char_ngrams(haystack)
            if not q_ngrams:
                return 0.0
            return len(q_ngrams & h_ngrams) / len(q_ngrams)

        # --- 1. token exact-match ratio (0~1) ---
        token_hits = 0
        strong_hits = 0
        for token in tokens:
            if token in haystack:
                token_hits += 1
                if len(token) >= 4 or any("\u4e00" <= ch <= "\u9fff" for ch in token):
                    strong_hits += 1
        token_ratio = token_hits / len(tokens) if tokens else 0.0

        # --- 2. char bi-gram overlap (0~1) — catches partial synonyms ---
        q_ngrams = self._char_ngrams(query_norm)
        h_ngrams = self._char_ngrams(haystack[:2000])  # limit for perf
        ngram_ratio = len(q_ngrams & h_ngrams) / len(q_ngrams) if q_ngrams else 0.0

        # --- 3. entity/number exact match bonus ---
        numbers_in_query = set(re.findall(r'\d+(?:\.\d+)?', query_norm))
        number_bonus = 0.0
        if numbers_in_query:
            number_hits = sum(1 for n in numbers_in_query if n in haystack)
            number_bonus = number_hits / len(numbers_in_query)

        # --- combine (weighted) ---
        score = (
            settings.TEXT_RELEVANCE_WEIGHT_TOKEN * token_ratio
            + settings.TEXT_RELEVANCE_WEIGHT_NGRAM * ngram_ratio
            + settings.TEXT_RELEVANCE_WEIGHT_NUMBER * number_bonus
        )

        # Strong-hit multiplier: if 2+ important tokens hit, boost
        if strong_hits >= 2:
            score = min(1.0, score * settings.TEXT_RELEVANCE_STRONG_HIT_MULTIPLIER)

        return min(1.0, score)

    def _score_source_authority(self, task: str, host: str, source: str) -> float:
        host_norm = self._normalize_text(host)
        source_norm = self._normalize_text(source)
        task_norm = self._normalize_text(task)
        score = 0.0

        if any(host_norm.endswith(suffix) for suffix in [".gov", ".edu", ".org"]):
            score += settings.SEARCH_AUTHORITY_BONUS_GOV_EDU_ORG
        return score

    def _score_search_result_card(self, task: str, query: str, card: SearchResultCard) -> float:
        haystack = " ".join([card.title, card.snippet, card.source, card.host, card.date])
        relevance = self._score_text_relevance(query, haystack)  # 0~1
        authority = self._score_source_authority(task, card.host, card.source)
        # Normalize authority to 0~1 range
        authority_norm = min(1.0, authority / settings.SEARCH_AUTHORITY_MAX)
        rank_bonus = 0.0
        if card.rank > 0:
            rank_bonus = max(settings.SEARCH_RANK_BONUS_BASE - ((card.rank - 1) * settings.SEARCH_RANK_BONUS_DECAY), 0.0)
        return min(1.0, settings.SEARCH_RANK_WEIGHT_RELEVANCE * relevance + settings.SEARCH_RANK_WEIGHT_AUTHORITY * authority_norm + rank_bonus)

    def _score_element_for_context(self, task: str, element: PageElement) -> float:
        attrs = element.attributes or {}
        haystack = " ".join([
            element.text, attrs.get("placeholder", ""), attrs.get("ariaLabel", ""),
            attrs.get("labelText", ""), attrs.get("title", ""), attrs.get("name", ""),
        ]).lower()
        score = 0.0
        for token in self._extract_task_tokens(task):
            if token in haystack:
                score += settings.ELEMENT_SCORE_TASK_TOKEN_MATCH
        if element.element_type == "input":
            score += settings.ELEMENT_SCORE_INPUT_TYPE
        if not element.is_visible:
            score += settings.ELEMENT_SCORE_NOT_VISIBLE
        if not element.is_clickable:
            score += settings.ELEMENT_SCORE_NOT_CLICKABLE
        if attrs.get("placeholder"):
            score += settings.ELEMENT_SCORE_HAS_PLACEHOLDER
        if attrs.get("labelText") or attrs.get("ariaLabel"):
            score += settings.ELEMENT_SCORE_HAS_LABEL
        if element.element_type in {"button", "link"}:
            score += settings.ELEMENT_SCORE_BUTTON_LINK
        return score

    def _prioritize_elements(self, task: str, elements: List[PageElement], limit: int = 12) -> List[PageElement]:
        ranked = sorted(elements, key=lambda item: self._score_element_for_context(task, item), reverse=True)
        chosen: List[PageElement] = []
        seen_signatures = set()
        for item in ranked:
            attrs = item.attributes or {}
            signature = (
                item.selector[:80],
                self._normalize_text(item.text)[:48],
                self._normalize_text(attrs.get("name", ""))[:32],
                self._normalize_text(attrs.get("placeholder", ""))[:32],
            )
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)
            chosen.append(item)
            if len(chosen) >= limit:
                break
        return chosen

    @staticmethod
    def _clone_action(action: Optional[BrowserAction]) -> Optional[BrowserAction]:
        if action is None:
            return None
        return BrowserAction(
            action_type=action.action_type,
            target_selector=action.target_selector,
            target_ref=action.target_ref,
            value=action.value,
            description=action.description,
            confidence=action.confidence,
            requires_confirmation=action.requires_confirmation,
            fallback_selector=action.fallback_selector,
            use_keyboard_fallback=action.use_keyboard_fallback,
            keyboard_key=action.keyboard_key,
            expected_page_type=action.expected_page_type,
            expected_text=action.expected_text,
        )

    # ── Action helpers ────────────────────────────────────────

    def _step_action_signature(self, step: Dict[str, Any]) -> str:
        action_type_raw = str(step.get("action_type") or step.get("plan") or "failed").lower()
        try:
            action_type = ActionType(action_type_raw)
        except ValueError:
            action_type = ActionType.FAILED
        return self._action_signature(
            BrowserAction(
                action_type=action_type,
                target_selector=str(step.get("selector") or step.get("action") or ""),
                target_ref=str(step.get("target_ref") or ""),
                value=str(step.get("value") or ""),
                description=str(step.get("description") or step.get("plan") or ""),
            )
        )

    @staticmethod
    def _action_requires_direct_target(action: BrowserAction) -> bool:
        return action.action_type in {
            ActionType.CLICK,
            ActionType.INPUT,
            ActionType.SELECT,
            ActionType.DOWNLOAD,
            ActionType.UPLOAD_FILE,
            ActionType.SWITCH_IFRAME,
        }

    def _recent_failed_action_matches(
        self,
        action: BrowserAction,
        recent_steps: Optional[List[Dict[str, Any]]],
        max_items: int = 2,
    ) -> bool:
        if not recent_steps:
            return False
        action_sig = self._action_signature(action)
        for step in reversed(recent_steps[-max_items:]):
            if str(step.get("result") or "") != "failed":
                continue
            if self._step_action_signature(step) == action_sig:
                return True
        return False

    # ── Formatting methods for LLM prompts ────────────────────

    def _format_intent_fields_for_llm(self, fields: Optional[Dict[str, str]]) -> str:
        if not fields:
            return "(none)"
        compact = {
            str(key)[:48]: str(value)[:160]
            for key, value in fields.items()
            if key
        }
        return json.dumps(compact, ensure_ascii=False, sort_keys=True)

    def _format_recent_steps_for_llm(self, steps: Optional[List[Dict[str, Any]]], max_items: int = 0) -> str:
        if not steps:
            return "(none)"
        if max_items <= 0:
            # P0: prefer the dedicated prompt-injection size; fall back to legacy setting.
            max_items = max(
                getattr(settings, "BROWSER_RECENT_STEPS_IN_PROMPT", 8),
                getattr(settings, "BROWSER_LLM_RECENT_STEPS", 6),
            )
        lines: List[str] = []
        for step in steps[-max_items:]:
            parts = [f"step={step.get('step', '?')}"]
            action_type = str(step.get("action_type") or step.get("plan") or "unknown")
            parts.append(f"action={action_type[:48]}")
            description = str(step.get("description") or step.get("plan") or "")
            if description and description != action_type:
                parts.append(f"desc={description[:72]}")
            selector = str(step.get("selector") or step.get("action") or "")
            if selector:
                parts.append(f"target={selector[:96]}")
            value = str(step.get("value") or "")
            if value:
                parts.append(f"value={value[:64]}")
            result = str(step.get("result") or step.get("observation") or "")
            if result:
                parts.append(f"result={result[:24]}")
            # Failure reason — helps LLM understand WHY an action failed
            failure_reason = str(step.get("failure_reason") or "")
            if failure_reason and result == "failed":
                parts.append(f"reason={failure_reason[:80]}")
            # Page change indicator
            if step.get("page_changed"):
                parts.append("page_changed=yes")
            # Data progress delta
            data_before = step.get("data_before_count", 0)
            data_after = step.get("data_after_count", 0)
            if isinstance(data_before, int) and isinstance(data_after, int) and data_after > data_before:
                parts.append(f"data_gained=+{data_after - data_before}")
            url = str(step.get("url") or "")
            if url:
                parts.append(f"url={url[:120]}")
            lines.append(" | ".join(parts))
        return "\n".join(lines)

    def _format_elements_for_llm(self, task: str, elements: List[PageElement], max_items: Optional[int] = None) -> str:
        limit = max_items or 8
        lines: List[str] = []
        for element in self._prioritize_elements(task, elements, limit=limit):
            attrs = element.attributes or {}
            descriptor = " | ".join(
                part for part in [
                    element.text[:48], attrs.get("labelText", "")[:36],
                    attrs.get("ariaLabel", "")[:36], attrs.get("placeholder", "")[:36],
                    attrs.get("title", "")[:36],
                ] if part
            )
            selector = element.selector[:72]
            ref_part = f" ref={element.ref}" if element.ref else ""
            lines.append(f"[{element.index}] type={element.element_type}{ref_part} selector={selector} info={descriptor}")
        return "\n".join(lines)

    def _format_data_for_llm(self, data: List[Dict[str, str]], max_items: int = 8) -> str:
        lines: List[str] = []
        for index, item in enumerate(data[:max_items]):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "") or "")[:100]
            text = str(item.get("text", "") or "")[:220]
            link = str(item.get("link", item.get("url", "")) or "")[:140]
            parts = [part for part in [title, text, link] if part]
            if parts:
                lines.append(f"[{index}] " + " | ".join(parts))
        return "\n".join(lines) or "(no visible data)"

    def _format_cards_for_llm(self, cards: List[SearchResultCard], max_items: int = 10) -> str:
        lines: List[str] = []
        for card in cards[:max_items]:
            parts = [
                card.title[:settings.CARD_TITLE_DISPLAY_CHARS],
                card.source[:settings.CARD_SOURCE_DISPLAY_CHARS],
                card.host[:48],
                card.date[:40],
                card.snippet[:settings.CARD_SNIPPET_DISPLAY_CHARS],
            ]
            payload = " | ".join(part for part in parts if part)
            if payload:
                target = card.target_ref or card.ref
                lines.append(f"[{target}] {payload}")
        if len(cards) > max_items:
            lines.append(f"... {len(cards) - max_items} more cards omitted")
        return "\n".join(lines) or "(no cards)"

    def _format_controls_for_llm(self, snapshot: Optional[Dict[str, Any]], max_items: int = 6) -> str:
        lines: List[str] = []
        controls = (snapshot or {}).get("controls", []) or []
        for control in controls[:max_items]:
            if not isinstance(control, dict):
                continue
            lines.append(
                f"[{str(control.get('ref', '') or 'control')}] "
                f"kind={str(control.get('kind', '') or '')} "
                f"text={str(control.get('text', '') or '')[:96]} "
                f"selector={str(control.get('selector', '') or '')[:72]}"
            )
        if len(controls) > max_items:
            lines.append(f"... {len(controls) - max_items} more controls omitted")
        return "\n".join(lines) or "(no controls)"

    def _format_headings_for_llm(self, snapshot: Dict[str, Any]) -> str:
        """Format headings from snapshot for LLM prompt."""
        headings = snapshot.get("headings") or []
        if not headings:
            return "(no headings)"
        lines = []
        for h in headings[:8]:
            level = str(h.get("level", "") or "")
            text = str(h.get("text", "") or "").strip()
            if text:
                lines.append(f"[{level}] {text[:120]}")
        return "\n".join(lines) if lines else "(no headings)"

    def _format_regions_for_llm(self, snapshot: Dict[str, Any]) -> str:
        """Format regions from snapshot for LLM prompt."""
        regions = snapshot.get("regions") or []
        if not regions:
            return "(no regions)"
        lines = []
        for r in regions[:6]:
            kind = str(r.get("kind", "") or "")
            heading = str(r.get("heading", "") or "").strip()
            items = int(r.get("item_count", 0) or 0)
            links = int(r.get("link_count", 0) or 0)
            parts = [f"[{kind}]"]
            if heading:
                parts.append(f'"{heading[:80]}"')
            metrics = []
            if items:
                metrics.append(f"{items} items")
            if links:
                metrics.append(f"{links} links")
            if metrics:
                parts.append(f"({', '.join(metrics)})")
            lines.append(" ".join(parts))
        return "\n".join(lines) if lines else "(no regions)"

    def _format_available_frames_for_llm(self, snapshot: Dict[str, Any]) -> str:
        """B4: render the list of child iframes so the LLM can SWITCH_IFRAME."""
        if not settings.BROWSER_IFRAME_ENABLED:
            return "(iframe tracking disabled)"
        frames = snapshot.get("available_frames") or []
        children = [
            f for f in frames
            if isinstance(f, dict) and not f.get("is_main") and not f.get("is_detached")
        ]
        if not children:
            return "(no iframes)"
        lines: List[str] = []
        for f in children[:8]:
            domain = str(f.get("domain", "") or "")
            name = str(f.get("name", "") or "")
            url = str(f.get("url", "") or "")
            idx = f.get("index", "?")
            label_parts = [f"#{idx}"]
            if domain:
                label_parts.append(f"domain={domain}")
            if name:
                label_parts.append(f'name="{name[:40]}"')
            if url and not domain:
                label_parts.append(f"url={url[:80]}")
            lines.append(" ".join(label_parts))
        return "\n".join(lines)

    def _format_available_tabs_for_llm(self, snapshot: Dict[str, Any]) -> str:
        """B4: render the list of open tabs so the LLM can SWITCH_TAB / CLOSE_TAB."""
        if not settings.BROWSER_TAB_MANAGEMENT_ENABLED:
            return "(tab tracking disabled)"
        tabs = snapshot.get("available_tabs") or []
        if not tabs:
            return "(no tabs)"
        if len(tabs) == 1:
            return "(1 tab; no switch needed)"
        lines: List[str] = []
        for t in tabs[:8]:
            if not isinstance(t, dict):
                continue
            active = "*" if t.get("is_active") else " "
            idx = t.get("index", "?")
            title = str(t.get("title", "") or "").strip()
            url = str(t.get("url", "") or "")
            label = title[:60] if title else url[:60]
            lines.append(f"{active} #{idx} {label}")
        return "\n".join(lines) if lines else "(no tabs)"

    def _format_collections_for_llm(self, snapshot: Optional[Dict[str, Any]], max_items: int = 4) -> str:
        lines: List[str] = []
        all_items = self.perception.collections_from_snapshot(snapshot) if self.perception else []
        for item in all_items[:max_items]:
            samples = " | ".join(sample[:120] for sample in item.get("sample_items", [])[:3] if sample)
            lines.append(
                f"[{item.get('ref', '') or 'collection'}] kind={item.get('kind', 'unknown')} "
                f"count={item.get('item_count', 0)} samples={samples or '(none)'}"
            )
        affordances = self.perception.get_snapshot_affordances(snapshot) if self.perception else {}
        if affordances.get("has_load_more") or affordances.get("has_pagination"):
            controls: List[str] = []
            if affordances.get("has_load_more"):
                controls.append(f"load_more={affordances.get('load_more_ref') or affordances.get('load_more_selector')}")
            if affordances.get("has_pagination"):
                controls.append(f"next_page={affordances.get('next_page_ref') or affordances.get('next_page_selector')}")
            lines.append("controls: " + " | ".join(controls))
        if len(all_items) > max_items:
            lines.append(f"... {len(all_items) - max_items} more collections omitted")
        return "\n".join(lines) or "(no collections)"

    def _format_assessment_elements_for_llm(
        self,
        task: str,
        current_url: str,
        elements: List[PageElement],
        max_items: int = 0,
    ) -> str:
        if max_items <= 0:
            max_items = settings.ELEMENT_DISPLAY_LIMIT
        prioritized = self._prioritize_elements(task, elements, limit=max_items * 2)
        ranked: List[Tuple[float, PageElement]] = []
        for element in prioritized:
            attrs = element.attributes or {}
            score = self._score_element_for_context(task, element)
            href = str(attrs.get("href", "") or "")
            if href and not is_search_engine_domain(href):
                score += 1.2
            if is_search_engine_domain(current_url) and href and not is_search_engine_domain(href):
                score += 1.2
            if attrs.get("value"):
                score += 0.3
            ranked.append((score, element))

        lines: List[str] = []
        seen_selectors = set()
        for _, element in sorted(ranked, key=lambda item: item[0], reverse=True):
            if element.selector in seen_selectors:
                continue
            seen_selectors.add(element.selector)
            attrs = element.attributes or {}
            el_text_limit = settings.ELEMENT_TEXT_DISPLAY_CHARS
            el_attr_limit = settings.ELEMENT_ATTR_DISPLAY_CHARS
            el_href_limit = settings.ELEMENT_HREF_DISPLAY_CHARS
            details = " | ".join(
                part for part in [
                    element.text[:el_text_limit],
                    attrs.get("labelText", "")[:el_attr_limit],
                    attrs.get("placeholder", "")[:el_attr_limit],
                    attrs.get("value", "")[:el_attr_limit],
                    attrs.get("href", "")[:el_href_limit],
                ] if part
            )

            context_parts = []
            if element.context_before:
                context_parts.append(f"before: {element.context_before[:80]}")
            if element.context_after:
                context_parts.append(f"after: {element.context_after[:80]}")

            context_str = " | ".join(context_parts) if context_parts else ""

            ref_part = f" ref={element.ref}" if element.ref else ""
            line = f"[{element.index}] type={element.element_type}{ref_part} selector={element.selector[:72]} info={details}"
            if context_str:
                line += f" | context: {context_str}"

            lines.append(line)
            if len(lines) >= max_items:
                break
        total_candidates = len(seen_selectors)
        if len(ranked) > total_candidates:
            lines.append(f"... {len(ranked) - total_candidates} more candidate elements omitted")
        return "\n".join(lines) or "(no actionable elements)"

    async def _format_snapshot_text_for_llm(self, snapshot: Optional[Dict[str, Any]], max_blocks: int = 0, query: str = "") -> str:
        if max_blocks <= 0:
            max_blocks = settings.TEXT_BLOCKS_DISPLAY_LIMIT
        active_snapshot = snapshot or {}
        lines: List[str] = []
        page_type = str(active_snapshot.get("page_type", "") or "").strip()
        page_stage = str(active_snapshot.get("page_stage", "") or "").strip()
        if page_type or page_stage:
            lines.append(
                "Page snapshot: "
                + ", ".join(
                    part
                    for part in (
                        f"type={page_type}" if page_type else "",
                        f"stage={page_stage}" if page_stage else "",
                    )
                    if part
                )
            )
        if self.perception:
            blocked_signals = self.perception.get_snapshot_blocked_signals(active_snapshot)
            if blocked_signals:
                lines.append("Blocked signals: " + " | ".join(blocked_signals[:4]))

        # 页面区域结构（regions）—— 帮助 LLM 理解页面布局
        regions = active_snapshot.get("regions") or []
        if regions:
            lines.append("Page regions:")
            for region in regions[:6]:
                kind = str(region.get("kind", "") or "")
                heading = str(region.get("heading", "") or "").strip()
                item_count = int(region.get("item_count", 0) or 0)
                link_count = int(region.get("link_count", 0) or 0)
                control_count = int(region.get("control_count", 0) or 0)
                text_sample = str(region.get("text_sample", "") or "").strip()
                sample_items = region.get("sample_items") or []

                parts = [f"[{kind}]"]
                if heading:
                    parts.append(f'"{heading[:80]}"')
                metrics = []
                if item_count:
                    metrics.append(f"{item_count} items")
                if link_count:
                    metrics.append(f"{link_count} links")
                if control_count:
                    metrics.append(f"{control_count} controls")
                if metrics:
                    parts.append(f"({', '.join(metrics)})")
                if sample_items:
                    parts.append("samples: " + " | ".join(s[:100] for s in sample_items[:3]))
                elif text_sample:
                    parts.append("text: " + text_sample[:160])
                lines.append("- " + " ".join(parts))

        # 页面标题结构（headings）
        headings = active_snapshot.get("headings") or []
        if headings:
            lines.append("Headings:")
            for h in headings[:8]:
                level = str(h.get("level", "") or "")
                text = str(h.get("text", "") or "").strip()
                if text:
                    lines.append(f"  [{level}] {text[:120]}")

        # 主要文本 —— 语义匹配提取最相关部分
        main_text = self.perception.get_snapshot_main_text(active_snapshot) if self.perception else ""
        if main_text:
            main_text_limit = settings.MAIN_TEXT_LIMIT_DETAIL if page_type in ("detail", "list", "serp") else settings.MAIN_TEXT_LIMIT_DEFAULT
            relevant_text = await extract_relevant_text_safe_async(
                main_text, query, fallback_limit=main_text_limit, max_chars=main_text_limit,
                page_type=page_type,
            )
            lines.append("Main text: " + relevant_text)

        blocks = self.perception.get_snapshot_visible_text_blocks(active_snapshot) if self.perception else []
        if blocks:
            lines.append("Visible text blocks:")
            tb_chars = settings.TEXT_BLOCK_DISPLAY_CHARS
            lines.extend(f"{index}. {text[:tb_chars]}" for index, text in enumerate(blocks[:max_blocks], 1))
        return "\n".join(lines).strip()

    async def _build_budgeted_browser_prompt_context(
        self,
        *,
        task: str,
        current_url: str,
        data: List[Dict[str, str]],
        cards: List[SearchResultCard],
        snapshot: Optional[Dict[str, Any]],
        elements_text: str,
        total_tokens: int,
    ) -> Tuple[Dict[str, str], Dict[str, Dict[str, Any]]]:
        data_text = self._format_data_for_llm(data, max_items=12)
        snapshot_text = await self._format_snapshot_text_for_llm(snapshot, query=task)
        if snapshot_text:
            if data_text and data_text != "(no visible data)":
                data_text = f"{snapshot_text}\n{data_text}"
            else:
                data_text = snapshot_text
        cards_text = self._format_cards_for_llm(cards, max_items=14)
        collections_text = self._format_collections_for_llm(snapshot, max_items=6)
        controls_text = self._format_controls_for_llm(snapshot, max_items=6)
        rendered, report = render_budgeted_sections(
            [
                BudgetSection(
                    name="data",
                    text=data_text,
                    min_chars=settings.DATA_BUDGET_MIN_CHARS,
                    max_chars=settings.DATA_BUDGET_MAX_CHARS,
                    weight=1.0,
                    mode="lines",
                    omission_label="data lines",
                ),
                BudgetSection(
                    name="cards",
                    text=cards_text,
                    min_chars=settings.CARDS_BUDGET_MIN_CHARS,
                    max_chars=settings.CARDS_BUDGET_MAX_CHARS,
                    weight=1.4,
                    mode="lines",
                    omission_label="card lines",
                ),
                BudgetSection(
                    name="collections",
                    text=collections_text,
                    min_chars=260,
                    max_chars=1200,
                    weight=0.9,
                    mode="lines",
                    omission_label="collection lines",
                ),
                BudgetSection(
                    name="controls",
                    text=controls_text,
                    min_chars=180,
                    max_chars=800,
                    weight=0.8,
                    mode="lines",
                    omission_label="control lines",
                ),
                BudgetSection(
                    name="elements",
                    text=elements_text,
                    min_chars=settings.ELEMENTS_BUDGET_MIN_CHARS,
                    max_chars=settings.ELEMENTS_BUDGET_MAX_CHARS,
                    weight=1.5,
                    mode="lines",
                    omission_label="element lines",
                ),
            ],
            total_tokens=total_tokens,
            model=self._get_llm(),
        )
        report["context"] = {
            "task": task[:160],
            "current_url": current_url[:160],
            "total_budget_tokens": total_tokens,
        }
        coverage_parts = []
        for name in ("data", "cards", "collections", "controls", "elements"):
            item = report.get(name, {})
            requested = int(item.get("requested_chars", 0) or 0)
            used = int(item.get("used_chars", 0) or 0)
            if requested <= 0:
                coverage_parts.append(f"{name}=none")
                continue
            coverage_parts.append(f"{name}={used}/{requested} chars")
        rendered["context_coverage"] = "; ".join(coverage_parts)
        return rendered, report

    # ── Phase 2: supporting helpers ─────────────────────────────

    def _task_mentions_interaction(self, task: str) -> bool:
        normalized = self._normalize_text(self._strip_urls_from_text(task))
        if not normalized:
            return False
        interaction_tokens = (
            "click", "tap", "press", "search", "type", "input", "fill",
            "submit", "select", "login", "log in", "sign in", "upload",
            "download", "scroll",
            "点击", "搜索", "输入", "填写", "提交", "选择", "登录", "上传", "下载", "滚动",
        )
        return any(token in normalized for token in interaction_tokens)

    def _is_noise_element(self, element: PageElement) -> bool:
        attrs = element.attributes or {}
        text = self._normalize_text(element.text)
        href = self._normalize_text(attrs.get("href", ""))
        if href in {"#", "javascript:void(0)", "javascript:;", "javascript:"}:
            return True
        if not text and not any(attrs.get(k) for k in ["placeholder", "ariaLabel", "labelText", "title"]):
            return True
        return any(term in text for term in ["cookie", "privacy", "terms"])

    def _snapshot_is_transient_loading(self, snapshot: Optional[Dict[str, Any]]) -> bool:
        active_snapshot = snapshot or {}
        page_type = str(active_snapshot.get("page_type", "") or "").strip().lower()
        title = self._normalize_text(str(active_snapshot.get("title", "") or ""))
        main_text = self._normalize_text(str(active_snapshot.get("main_text", "") or ""))
        has_structured_content = any(
            bool(active_snapshot.get(key))
            for key in ("elements", "cards", "collections", "controls", "regions")
        )
        if has_structured_content:
            return False
        if page_type not in {"", "unknown"}:
            return False
        if title:
            return False
        return main_text.startswith("loading")

    def _strip_search_instruction_phrases(self, value: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            return ""
        cleaned = re.sub(
            r"^(?:browser|web|page)\s+task\s*[:：-]?\s*",
            "", cleaned, flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            r"^(?:浏览器任务|网页任务|任务)\s*[:：-]?\s*",
            "", cleaned, flags=re.IGNORECASE,
        )
        split_patterns = (
            r"\s+(?=(?:wait(?:ing)?(?:\s+for)?|render(?:ing)?|load(?:ing|ed)?|open|visit|navigate|go\s+to|click|input|type|fill|submit|extract|show|display|return|report|summari[sz]e|collect|scrape)\b)",
            r"[\s，,。．；;！!？?]+(?:并且?|然后|再|同时|完整(?:地)?|请(?:先)?|还要?|接着)?(?:[\u4e00-\u9fff]{0,4})?(?=(?:等待|渲染|加载|打开|访问|进入|点击|输入|填写|提交|提取|展示|显示|返回|总结|收集|抓取))",
            r"[\s，,。．；;！!？?]+(?=(?:请(?:先)?)?(?:打开|访问|进入|点击|输入|填写|提交|展示|显示|等待))",
            r"[\s，,。；;]+(?=(?:and then|then|next)\b)",
        )
        for pattern in split_patterns:
            parts = re.split(pattern, cleaned, maxsplit=1, flags=re.IGNORECASE)
            if parts and parts[0].strip():
                cleaned = parts[0].strip()
        return cleaned

    def _refine_search_query(self, task: str, candidate: str = "") -> str:
        raw = str(candidate or task or "")
        raw = re.sub(r"https?://\S+", " ", raw)
        raw = re.sub(r"\bsite:\s*[^\s]+", " ", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\b(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}\b", " ", raw)
        raw = re.sub(
            r"\b(?:from|use|using|via|prefer|preferred|primary|secondary)\b\s+[^\n,.;，。；]{0,120}\b(?:source|site|domain|url)\b",
            " ", raw, flags=re.IGNORECASE,
        )
        raw = re.sub(
            r"(?:作为|用作)?(?:主要|首选|次要|备用)?(?:来源|站点|域名)[^\n,.;，。；]{0,80}",
            " ", raw, flags=re.IGNORECASE,
        )
        normalized = re.sub(r"\s+", " ", raw).strip()
        if not normalized:
            return ""
        normalized = self._strip_search_instruction_phrases(normalized)
        if not normalized:
            return ""

        for pattern in (
            r"(?:搜索|查询|查找|查一下|查查|搜一下|搜|获取)([^。！？\n]{2,80})",
            r"(?:search|find|get|look up|query)\s+([^\n.?!]{2,80})",
        ):
            match = re.search(pattern, normalized, flags=re.IGNORECASE)
            if match:
                normalized = match.group(1)
                break

        normalized = self._strip_search_instruction_phrases(normalized)
        stop_tokens = {
            "打开", "浏览器", "访问", "页面", "网页", "网站", "点击", "输入", "搜索", "查询", "查找",
            "提取", "展示", "显示", "查看", "操作", "过程", "结果", "用户", "详细", "详情", "完整",
            "use", "open", "browser", "page", "website", "click", "input", "search", "query",
            "extract", "show", "display", "user", "details", "process", "result", "results", "visible",
            "retrieve", "rendering", "after", "wait", "render", "data", "task", "tasks",
            "loading", "loaded", "load", "fully", "complete", "completed", "summary", "report",
            "等待", "加载", "渲染", "完成", "任务", "步骤", "总结", "收集",
        }
        tokens: List[str] = []
        for token in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9][A-Za-z0-9_+.-]{1,}", normalized):
            lowered = token.lower()
            if lowered in stop_tokens:
                continue
            if lowered.isdigit() and len(lowered) >= 4:
                continue
            if lowered not in tokens:
                tokens.append(lowered)

        if not tokens:
            return ""
        compact_tokens = self._extract_query_tokens(" ".join(tokens))
        if len(compact_tokens) >= 2:
            tokens = compact_tokens
        return " ".join(tokens[:8]).strip()

    def _derive_primary_query(self, task: str) -> str:
        refined = self._refine_search_query(task)
        if refined:
            return refined
        normalized = re.sub(r"https?://\S+", " ", task or "")
        normalized = re.sub(r"[^\w\u4e00-\u9fff]+", " ", normalized, flags=re.UNICODE)
        chunks = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9][A-Za-z0-9_+.-]{1,}", normalized)
        if chunks:
            return " ".join(chunks[:8]).strip()
        return ""

    def _extract_target_result_count(self, task: str) -> int:
        match = re.search(
            r'(\d+)\s*(?:个|条|款|项|条数据|items?|results?|records?|articles?|entries|pieces?)',
            task or "", flags=re.IGNORECASE,
        )
        if not match:
            return 0
        try:
            return max(int(match.group(1)), 0)
        except (TypeError, ValueError):
            return 0

    def _data_has_substantive_text(self, data: List[Dict[str, str]]) -> bool:
        for item in data[:8]:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text", "") or "").strip()
            if len(text) >= 40:
                return True
            extra_keys = {key for key in item.keys() if key not in {"title", "link", "url", "index"}}
            if extra_keys:
                return True
        return False

    def _search_results_have_answer_evidence(self, query: str, data: List[Dict[str, str]]) -> bool:
        if not data:
            return False
        if not query:
            return self._data_has_substantive_text(data)
        relevant_hits = 0
        for item in data[:8]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "") or "").strip()
            snippet = str(item.get("text", "") or "").strip()
            source = str(item.get("source", "") or "").strip()
            date_hint = str(item.get("date", "") or "").strip()
            haystack = " ".join(part for part in [title, snippet, source, date_hint] if part)
            score = self._score_text_relevance(query, haystack)
            if score >= 0.5:
                relevant_hits += 1
            elif score >= 0.3 and (len(snippet) >= 40 or len(title) >= 24 or source):
                relevant_hits += 1
            if relevant_hits >= 1:
                return True
        return False

    def _is_data_relevant(self, query: str, data: List[Dict[str, str]]) -> bool:
        if not data:
            return False
        tokens = self._extract_query_tokens(query)
        if not tokens:
            return True
        best_score = 0.0
        for item in data[:8]:
            if not isinstance(item, dict):
                continue
            haystack = " ".join(str(v) for v in item.values() if v)
            best_score = max(best_score, self._score_text_relevance(query, haystack))
            if best_score >= 0.3:
                return True
        return False

    @staticmethod
    def _extract_url_from_task(task: str) -> Optional[str]:
        return extract_first_url(task) or None

    @staticmethod
    def _is_search_engine_url(url: str) -> bool:
        return is_search_engine_domain(url or "")

    @staticmethod
    def _urls_look_related(expected_url: str, current_url: str) -> bool:
        if not expected_url:
            return bool(current_url)
        if not current_url:
            return False
        expected = urlparse(expected_url)
        current = urlparse(current_url)
        expected_host = expected.netloc.lower()
        current_host = current.netloc.lower()
        if expected_host.startswith("www."):
            expected_host = expected_host[4:]
        if current_host.startswith("www."):
            current_host = current_host[4:]
        if not expected_host or not current_host:
            return expected_url.rstrip("/") == current_url.rstrip("/")
        return (
            expected_host == current_host
            or expected_host.endswith(f".{current_host}")
            or current_host.endswith(f".{expected_host}")
        )

    def _task_requires_detail_page(self, task: str, intent: Optional[TaskIntent] = None) -> bool:
        active_intent = intent or TaskIntent(intent_type="read", query="", confidence=0.0)
        if active_intent.intent_type not in {"search", "navigate"}:
            return False
        if self._extract_url_from_task(task):
            return True
        if active_intent.target_text or active_intent.requires_interaction:
            return True
        if self._task_mentions_interaction(task):
            return True
        query = active_intent.query or self._derive_primary_query(task)
        return len(self._extract_query_tokens(query)) >= 2

    # ── Phase 2: main decision methods ────────────────────────

    def _page_data_satisfies_goal(
        self,
        task: str,
        current_url: str,
        intent: Optional[TaskIntent],
        data: List[Dict[str, str]],
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> bool:
        active_intent = intent or TaskIntent(intent_type="read", query="", confidence=0.0)
        query = active_intent.query or self._derive_primary_query(task)
        if query and not self._is_data_relevant(query, data):
            return False
        target_count = self._extract_target_result_count(task)
        active_snapshot = snapshot or {}
        page_type = str(active_snapshot.get("page_type", "") or "")
        if target_count and len(data or []) < target_count and page_type in {"serp", "list"}:
            return False
        if not self._is_search_engine_url(current_url):
            if page_type == "list" and target_count and len(data or []) < target_count:
                return False
            if data:
                return True
            main_text = self.perception.get_snapshot_main_text(active_snapshot) if self.perception else ""
            if main_text and len(main_text) >= 100:
                score = self._score_text_relevance(query, main_text[:3000])
                if score >= 0.35:
                    return True
            return False
        return self._search_results_have_answer_evidence(query, data)

    def _find_search_result_click_action(
        self,
        task: str,
        current_url: str,
        elements: List[PageElement],
        intent: Optional[TaskIntent] = None,
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> Optional[BrowserAction]:
        if not self._is_search_engine_url(current_url):
            return None
        active_intent = intent or TaskIntent(
            intent_type="search", query=self._derive_primary_query(task), confidence=0.0,
        )
        if not self._task_requires_detail_page(task, active_intent):
            return None

        cards = self.perception.cards_from_snapshot(snapshot) if self.perception else []
        if cards:
            ranked_cards: List[Tuple[float, SearchResultCard]] = []
            for card in cards:
                score = self._score_search_result_card(task, active_intent.query, card)
                if self._extract_query_tokens(active_intent.query) and score < 0.3:
                    continue
                ranked_cards.append((score, card))
            ranked_cards.sort(key=lambda item: item[0], reverse=True)
            if ranked_cards:
                best_score, best_card = ranked_cards[0]
                return BrowserAction(
                    action_type=ActionType.CLICK,
                    target_selector=best_card.target_selector,
                    target_ref=best_card.target_ref or best_card.ref,
                    description="open the strongest search result card",
                    confidence=min(best_score, 0.9),
                    expected_page_type="detail",
                )

        query_tokens = self._extract_query_tokens(active_intent.query)
        best_match: Optional[Tuple[float, PageElement]] = None
        for element in elements:
            if not element.is_visible or not element.is_clickable:
                continue
            if element.element_type not in {"link", "button"} and element.tag not in {"a", "button"}:
                continue
            if self._is_noise_element(element):
                continue
            attrs = element.attributes or {}
            href = str(attrs.get("href", "") or "")
            if not href or self._is_search_engine_url(href):
                continue
            haystack = self._normalize_text(" ".join([
                element.text,
                attrs.get("labelText", ""),
                attrs.get("title", ""),
                href,
            ]))
            score = self._score_text_relevance(active_intent.query, haystack)
            if query_tokens and score < 0.3:
                continue
            if best_match is None or score > best_match[0]:
                best_match = (score, element)

        if best_match is None:
            return None
        return BrowserAction(
            action_type=ActionType.CLICK,
            target_selector=best_match[1].selector,
            target_ref=best_match[1].ref,
            description="open the most relevant detail result",
            confidence=min(best_match[0], 0.88),
            expected_page_type="detail",
        )

    def _task_looks_satisfied(
        self,
        task: str,
        current_url: str,
        intent: Optional[TaskIntent] = None,
        target_url: str = "",
        snapshot: Optional[Dict[str, Any]] = None,
        elements: Optional[List[PageElement]] = None,
        data: Optional[List[Dict[str, str]]] = None,
    ) -> bool:
        active_intent = intent or TaskIntent(
            intent_type="search", query=self._derive_primary_query(task), confidence=0.0,
        )
        if active_intent.intent_type == "search":
            if self._is_search_engine_url(current_url):
                current_data = data or []
                if current_data:
                    query = active_intent.query or self._derive_primary_query(task)
                    if self._search_results_have_answer_evidence(query, current_data):
                        return True
                return False
            active_snapshot = snapshot or {}
            current_data = data or []
            if self._page_data_satisfies_goal(
                task, current_url, active_intent, current_data, snapshot=active_snapshot,
            ):
                return True
            main_text = self.perception.get_snapshot_main_text(active_snapshot) if self.perception else ""
            if main_text and len(main_text) >= 120:
                query = active_intent.query or self._derive_primary_query(task)
                if query:
                    relevance = self._score_text_relevance(query, main_text[:3000])
                    if relevance >= 0.3:
                        return True
            parsed = urlparse(current_url or "")
            query_string = " ".join(
                value for values in parse_qs(parsed.query).values() for value in values
            )
            haystack = self._normalize_text(f"{parsed.path} {query_string}")
            tokens = [token for token in self._extract_task_tokens(active_intent.query) if len(token) >= 2][:6]
            if not tokens:
                return bool(haystack)
            return any(token in haystack for token in tokens)

        if active_intent.intent_type in {"form", "auth"}:
            active_snapshot = snapshot or {}
            current_elements = elements or []
            current_data = data or []
            if self._page_data_satisfies_goal(
                task, current_url, active_intent, current_data, snapshot=active_snapshot,
            ):
                return True
            # Delegate to orchestrator for form-handling methods not yet migrated
            if self._orchestrator and self._orchestrator._interaction_requires_follow_up(
                task, active_intent, current_elements, snapshot=active_snapshot,
            ):
                return False
            if target_url and self._urls_look_related(target_url, current_url):
                return True
            page_type = str(active_snapshot.get("page_type", "") or "")
            page_stage = str(active_snapshot.get("page_stage", "") or "")
            if page_stage == "interacting" or page_type in {"form", "login", "modal"}:
                return False
            return bool(current_url)

        if active_intent.intent_type == "navigate":
            if target_url:
                return self._urls_look_related(target_url, current_url)
            return bool(current_url) and not active_intent.target_text

        if active_intent.intent_type == "read":
            active_snapshot = snapshot or {}
            current_data = data or []
            if self._page_data_satisfies_goal(
                task, current_url, active_intent, current_data, snapshot=active_snapshot,
            ):
                return True
            main_text = self.perception.get_snapshot_main_text(active_snapshot) if self.perception else ""
            if main_text and len(main_text) >= 120:
                query = active_intent.query or self._derive_primary_query(task)
                if query:
                    relevance = self._score_text_relevance(query, main_text[:3000])
                    if relevance >= 0.3:
                        return True
                else:
                    return True
            return False

        return False

    def _sanitize_planned_action(
        self,
        task: str,
        current_url: str,
        elements: List[PageElement],
        intent: Optional[TaskIntent],
        data: List[Dict[str, str]],
        action: Optional[BrowserAction],
        snapshot: Optional[Dict[str, Any]] = None,
        recent_steps: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[BrowserAction]:
        if action is None:
            return None
        if action.action_type == ActionType.FAILED:
            return None
        if self._recent_failed_action_matches(action, recent_steps):
            return None

        # P0: Reject actions whose fingerprint has already been executed
        # BROWSER_DEDUP_THRESHOLD times at the same (url, page_stage).
        if action.action_type not in {ActionType.DONE, ActionType.EXTRACT, ActionType.WAIT}:
            fp_url = current_url or ""
            fp_stage = str((snapshot or {}).get("page_stage", "") or (snapshot or {}).get("page_type", "") or "")
            if not fp_stage:
                _, fp_stage = self._current_url_and_stage()
            fp = self._fingerprint_action(action, fp_url, fp_stage)
            if fp and self._is_repeat_action(fp):
                try:
                    web_debug_recorder.record_event(
                        "browser_dedup_rejected",
                        fingerprint=fp,
                        count=self._step_fingerprints.get(fp, 0),
                        action_type=action.action_type.value,
                        url=fp_url,
                        page_stage=fp_stage,
                    )
                except Exception:
                    pass
                return None

        active_intent = intent or TaskIntent(intent_type="read", query="", confidence=0.0)
        active_snapshot = snapshot or {}
        current_elements = elements or []
        current_data = data or []

        if self._action_requires_direct_target(action) and not (action.target_selector or action.target_ref):
            if action.use_keyboard_fallback and action.keyboard_key:
                return BrowserAction(
                    action_type=ActionType.PRESS_KEY,
                    value=action.keyboard_key,
                    description=action.description or "use keyboard fallback",
                    confidence=action.confidence,
                )
            return None

        if action.action_type == ActionType.WAIT:
            if self._snapshot_is_transient_loading(active_snapshot) or (not current_elements and not current_data):
                if not action.value:
                    action = self._clone_action(action) or action
                    action.value = "1"
                return action
            return None

        if action.action_type == ActionType.DONE:
            if action.confidence >= 0.7:
                return action
            if self._task_looks_satisfied(
                task, current_url, active_intent,
                snapshot=active_snapshot, elements=current_elements, data=current_data,
            ):
                return action
            return None

        if action.action_type == ActionType.EXTRACT:
            if action.confidence >= 0.65:
                return action
            # Delegate to orchestrator for form-handling methods not yet migrated
            if active_intent.intent_type in {"form", "auth"} and self._orchestrator:
                if self._orchestrator._interaction_requires_follow_up(
                    task, active_intent, current_elements, snapshot=active_snapshot,
                ):
                    return None
            if current_data:
                return action
            # Delegate to orchestrator for _is_read_only_task (depends on form extraction chain)
            if self._orchestrator and self._orchestrator._is_read_only_task(task, active_intent):
                return action
            main_text = self.perception.get_snapshot_main_text(active_snapshot) if self.perception else ""
            if main_text and not active_intent.requires_interaction:
                return action
            return None

        if action.action_type == ActionType.INPUT and not action.value:
            query = active_intent.query or self._derive_primary_query(task)
            if query:
                action = self._clone_action(action) or action
                action.value = query
                return action
            return None

        return action

    # ── Phase 3a: pure helper methods ───────────────────────────

    def _stringify_llm_response(self, response) -> str:
        if response is None:
            return ""
        content = getattr(response, "content", None)
        if content is not None:
            if isinstance(content, str):
                return content
            try:
                return json.dumps(content, ensure_ascii=False)
            except TypeError:
                return str(content)
        if isinstance(response, str):
            return response
        try:
            return json.dumps(response, ensure_ascii=False)
        except TypeError:
            return str(response)

    def _action_from_llm(self, payload: Dict[str, Any], elements: List[PageElement]) -> BrowserAction:
        if not isinstance(payload, dict):
            payload = {}
        action_payload = payload.get("action", {}) if isinstance(payload.get("action", {}), dict) else {}
        flat_action_payload = payload if not action_payload else {}
        action_type_raw = str(
            action_payload.get("type")
            or flat_action_payload.get("action_type")
            or flat_action_payload.get("type")
            or "failed"
        ).lower()
        try:
            action_type = ActionType(action_type_raw)
        except ValueError:
            action_type = ActionType.FAILED

        selector = str(action_payload.get("target_selector") or flat_action_payload.get("target_selector") or "")
        target_ref = str(action_payload.get("target_ref") or flat_action_payload.get("target_ref") or "")
        index = action_payload.get("element_index", flat_action_payload.get("element_index", -1))
        if not isinstance(index, int):
            try:
                index = int(index)
            except (TypeError, ValueError):
                index = -1
        if not selector and not target_ref and isinstance(index, int):
            for element in elements:
                if element.index == index:
                    selector = element.selector
                    target_ref = element.ref
                    break

        raw_value = action_payload.get("value", flat_action_payload.get("value", ""))
        if isinstance(raw_value, (dict, list)):
            value = json.dumps(raw_value, ensure_ascii=False)
        else:
            value = str(raw_value or "")

        return BrowserAction(
            action_type=action_type, target_selector=selector,
            target_ref=target_ref, value=value,
            description=str(action_payload.get("description") or flat_action_payload.get("description") or ""),
            confidence=float(payload.get("confidence", action_payload.get("confidence", flat_action_payload.get("confidence", 0.0))) or 0.0),
            requires_confirmation=bool(
                payload.get("requires_human_confirm", action_payload.get("requires_human_confirm", flat_action_payload.get("requires_human_confirm", False)))
            ),
            fallback_selector=str(action_payload.get("fallback_selector") or flat_action_payload.get("fallback_selector") or ""),
            use_keyboard_fallback=bool(action_payload.get("use_keyboard", flat_action_payload.get("use_keyboard", False))),
            keyboard_key=str(action_payload.get("keyboard_key") or flat_action_payload.get("keyboard_key") or ""),
            expected_page_type=str(action_payload.get("expected_page_type") or flat_action_payload.get("expected_page_type") or ""),
            expected_text=str(action_payload.get("expected_text") or flat_action_payload.get("expected_text") or ""),
        )

    def _elements_to_debug_payload(self, elements: List[PageElement]) -> List[Dict[str, Any]]:
        return [
            {
                "index": element.index, "tag": element.tag, "text": element.text,
                "element_type": element.element_type, "selector": element.selector,
                "ref": element.ref, "role": element.role,
                "attributes": dict(element.attributes or {}),
                "is_visible": element.is_visible, "is_clickable": element.is_clickable,
                "context_before": element.context_before, "context_after": element.context_after,
                "parent_ref": element.parent_ref, "region": element.region,
            }
            for element in elements
        ]

    def _action_to_debug_payload(self, action: Optional[BrowserAction]) -> Dict[str, Any]:
        if action is None:
            return {}
        return {
            "action_type": action.action_type.value,
            "target_selector": action.target_selector, "target_ref": action.target_ref,
            "value": action.value, "description": action.description,
            "confidence": action.confidence, "requires_confirmation": action.requires_confirmation,
            "fallback_selector": action.fallback_selector,
            "use_keyboard_fallback": action.use_keyboard_fallback,
            "keyboard_key": action.keyboard_key,
            "expected_page_type": action.expected_page_type,
            "expected_text": action.expected_text,
        }

    def _element_action_haystack(self, element: PageElement) -> str:
        attrs = element.attributes or {}
        return self._normalize_text(" ".join([
            element.text, attrs.get("labelText", ""), attrs.get("ariaLabel", ""),
            attrs.get("title", ""), attrs.get("name", ""), attrs.get("id", ""),
            attrs.get("value", ""), attrs.get("type", ""),
        ]))

    def _find_primary_submit_control(self, elements: List[PageElement]) -> Optional[PageElement]:
        controls = [
            item for item in elements
            if item.is_visible and item.is_clickable and item.element_type in {"button", "submit", "link"}
        ]
        if not controls:
            return None
        ranked: List[Tuple[float, PageElement]] = []
        for control in controls:
            attrs = control.attributes or {}
            selector = str(control.selector or "")
            haystack = self._element_action_haystack(control)
            score = 0.0
            if self._normalize_text(attrs.get("type", "")) == "submit":
                score += 5.0
            if control.tag == "button":
                score += 1.2
            if any(token in haystack for token in _AUTH_SUBMIT_POSITIVE_TOKENS):
                score += 4.0
            if any(token in haystack for token in _AUTH_SUBMIT_NEGATIVE_TOKENS):
                score -= 6.0
            if any(token in haystack for token in _AUTH_SECONDARY_PROVIDER_TOKENS):
                score -= 4.0
            if selector and "form > button" in selector:
                score += 1.5
            score += max(0.0, 1.0 - 0.1 * float(control.index))
            ranked.append((score, control))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return ranked[0][1] if ranked else controls[0]

    def _find_search_element(self, elements: List[PageElement]) -> Optional[PageElement]:
        _SEARCH_KEYWORDS = {"search", "搜索", "搜", "查找", "find", "lookup", "查询"}
        best: Optional[PageElement] = None
        best_score = 0.0
        for el in elements:
            if not el.is_visible or not el.is_clickable:
                continue
            attrs = el.attributes or {}
            haystack = " ".join([
                el.text, attrs.get("placeholder", ""), attrs.get("ariaLabel", ""),
                attrs.get("labelText", ""), attrs.get("name", ""), attrs.get("type", ""),
            ]).lower()
            score = 0.0
            if el.element_type in {"search", "text"} and el.tag in {"input", "textarea"}:
                score += 3.0
            for kw in _SEARCH_KEYWORDS:
                if kw in haystack:
                    score += 4.0
                    break
            if score <= 0:
                continue
            if any(auth_kw in haystack for auth_kw in {"email", "password", "sign up", "sign in", "login", "注册", "登录"}):
                score -= 5.0
            if el.element_type in {"email", "password"}:
                score -= 5.0
            if score > best_score:
                best_score = score
                best = el
        return best

    def _iter_input_candidates(self, elements: List[PageElement]) -> List[PageElement]:
        candidates: List[PageElement] = []
        for element in elements:
            if not element.is_visible or not element.is_clickable:
                continue
            attrs = element.attributes or {}
            normalized_type = self._normalize_text(attrs.get("type", "") or element.element_type or element.tag)
            if element.tag == "textarea" or normalized_type == "textarea":
                candidates.append(element)
                continue
            if element.tag == "input" and normalized_type not in _NON_TEXT_INPUT_TYPES:
                candidates.append(element)
                continue
            if element.element_type in {"input", "text", "search", "email", "password"} and normalized_type not in _NON_TEXT_INPUT_TYPES:
                candidates.append(element)
        return candidates

    def _find_primary_text_input(self, elements: List[PageElement]) -> Optional[PageElement]:
        candidates = self._iter_input_candidates(elements)
        if not candidates:
            return None
        ranked = sorted(
            candidates,
            key=lambda item: (
                1 if (item.attributes or {}).get("placeholder") else 0,
                1 if (item.attributes or {}).get("labelText") else 0,
                1 if item.element_type in {"search", "text", "email"} else 0,
            ),
            reverse=True,
        )
        return ranked[0] if ranked else None

    def _search_input_matches_query(self, elements: List[PageElement], query: str) -> bool:
        if not query:
            return False
        input_element = self._find_primary_text_input(elements)
        if input_element is None:
            return False
        current_value = str((input_element.attributes or {}).get("value", "") or "").strip()
        if not current_value:
            return False
        return self._normalize_text(current_value) == self._normalize_text(query)

    @staticmethod
    def _looks_like_blocked_page(url: str, title: str = "") -> bool:
        normalized_url = (url or "").lower()
        normalized_title = (title or "").lower()
        blocked_url_tokens = (
            "/forbidden", "/denied", "/captcha", "/verify",
            "/challenge", "/blocked", "/security-check", "/sorry",
        )
        blocked_title_tokens = (
            "403", "forbidden", "access denied", "request denied",
            "robot check", "security check", "captcha", "unusual traffic",
            "异常流量", "人机身份验证", "验证码", "安全验证", "访问受限", "拒绝访问",
        )
        title_blocked = any(token in normalized_title for token in blocked_title_tokens)
        url_blocked = any(token in normalized_url for token in blocked_url_tokens)
        ok_holding_page = "/ok.html" in normalized_url
        if ok_holding_page and not title_blocked:
            ok_holding_page = any(token in normalized_url for token in ("403", "forbidden", "denied", "blocked"))
        return url_blocked or title_blocked or ok_holding_page

    @staticmethod
    def _looks_like_search_results_url(url: str) -> bool:
        return looks_like_search_results_url(url or "")

    def _find_ranked_elements(self, task: str, elements: List[PageElement],
                              kinds: Optional[List[str]] = None, keywords: Optional[List[str]] = None,
                              exclude_selectors: Optional[List[str]] = None) -> List[PageElement]:
        matches: List[Tuple[float, PageElement]] = []
        task_text = self._normalize_text(task)
        excluded = set(exclude_selectors or [])
        for element in elements:
            if not element.is_visible or not element.is_clickable:
                continue
            if element.selector in excluded:
                continue
            if kinds and element.element_type not in kinds and element.tag not in kinds:
                continue
            attrs = element.attributes or {}
            haystack = " ".join([
                element.text, attrs.get("placeholder", ""), attrs.get("ariaLabel", ""),
                attrs.get("labelText", ""), attrs.get("title", ""), attrs.get("name", ""),
            ]).lower()
            score = 0.0
            for token in [part for part in task_text.split() if len(part) >= 2]:
                if token in haystack:
                    score += 1.0
            if keywords:
                for keyword in keywords:
                    if keyword.lower() in haystack:
                        score += 3.0
            if attrs.get("placeholder"):
                score += 0.5
            if attrs.get("labelText"):
                score += 0.5
            if score > 0:
                matches.append((score, element))
        matches.sort(key=lambda item: item[0], reverse=True)
        if keywords:
            min_score = 2.0
            filtered = [item for item in matches if item[0] >= min_score]
            if filtered:
                return [item[1] for item in filtered]
        return [item[1] for item in matches]

    def _find_best_element(self, task: str, elements: List[PageElement],
                           kinds: Optional[List[str]] = None, keywords: Optional[List[str]] = None,
                           exclude_selectors: Optional[List[str]] = None) -> Optional[PageElement]:
        ranked = self._find_ranked_elements(task, elements, kinds=kinds, keywords=keywords, exclude_selectors=exclude_selectors)
        return ranked[0] if ranked else None

    # ── Phase 3b: state-dependent helpers ─────────────────────────

    def _snapshot_has_actionable_modal(
        self, snapshot: Optional[Dict[str, Any]], elements: Optional[List[PageElement]] = None,
    ) -> bool:
        active_snapshot = snapshot or {}
        affordances = self.perception.get_snapshot_affordances(active_snapshot) if self.perception else {}
        controls = active_snapshot.get("controls") or []
        if any(affordances.get(key) for key in (
            "modal_primary_ref", "modal_primary_selector",
            "modal_secondary_ref", "modal_secondary_selector",
            "modal_close_ref", "modal_close_selector",
        )):
            return True
        if any(str(control.get("kind", "") or "") in {"modal_primary", "modal_secondary", "modal_close"} for control in controls):
            return True
        if any(element.region == "modal" and element.is_visible and element.is_clickable for element in (elements or [])):
            return True
        page_type = str(active_snapshot.get("page_type", "") or "")
        return bool(affordances.get("has_modal")) and page_type == "modal"

    def _get_snapshot_item_count(self, snapshot: Optional[Dict[str, Any]]) -> int:
        active_snapshot = snapshot or {}
        card_count = len(active_snapshot.get("cards", []) or [])
        collections = self.perception.collections_from_snapshot(active_snapshot) if self.perception else []
        collection_count = max((int(item.get("item_count", 0) or 0) for item in collections), default=0)
        affordances = self.perception.get_snapshot_affordances(active_snapshot) if self.perception else {}
        affordance_count = int(affordances.get("collection_item_count", 0) or 0)
        return max(card_count, collection_count, affordance_count)

    def _build_snapshot_click_action(
        self, snapshot: Optional[Dict[str, Any]], *,
        ref_key: str, selector_key: str, description: str,
        expected_page_type: str = "", confidence: float = 0.72,
    ) -> Optional[BrowserAction]:
        affordances = self.perception.get_snapshot_affordances(snapshot) if self.perception else {}
        target_ref = str(affordances.get(ref_key, "") or "")
        target_selector = str(affordances.get(selector_key, "") or "")
        if not target_ref and not target_selector:
            return None
        return BrowserAction(
            action_type=ActionType.CLICK, target_ref=target_ref, target_selector=target_selector,
            description=description, confidence=confidence, expected_page_type=expected_page_type,
        )

    def _infer_page_state(
        self, task: str, current_url: str, intent: Optional[TaskIntent],
        data: List[Dict[str, str]], snapshot: Optional[Dict[str, Any]] = None,
        elements: Optional[List[PageElement]] = None,
    ) -> PageState:
        active_intent = intent or TaskIntent(intent_type="read", query="", confidence=0.0)
        active_snapshot = snapshot or self.last_semantic_snapshot or {}
        page_type = str(active_snapshot.get("page_type", "") or "").strip()
        if not page_type:
            page_type = "serp" if self._looks_like_search_results_url(current_url) else "unknown"

        affordances = self.perception.get_snapshot_affordances(active_snapshot) if self.perception else {}
        blocked_signals = self.perception.get_snapshot_blocked_signals(active_snapshot) if self.perception else []
        target_count = self._extract_target_result_count(task)
        goal_satisfied = self._page_data_satisfies_goal(task, current_url, active_intent, data, snapshot=active_snapshot)
        item_count = max(self._get_snapshot_item_count(active_snapshot), len(data or []))
        has_pagination = bool(affordances.get("has_pagination"))
        has_load_more = bool(affordances.get("has_load_more"))
        has_modal = self._snapshot_has_actionable_modal(active_snapshot, elements)
        snapshot_stage = str(active_snapshot.get("page_stage", "") or "").strip()
        if blocked_signals or self._looks_like_blocked_page(current_url):
            page_type = "blocked"

        stage = "unknown"
        if page_type == "blocked":
            stage = "blocked"
        elif snapshot_stage in {
            "searching", "selecting_source", "extracting", "interacting",
            "dismiss_modal", "collecting_more", "completing",
        }:
            stage = snapshot_stage
        elif has_modal:
            stage = "dismiss_modal"
        elif page_type == "serp":
            stage = "completing" if goal_satisfied else "selecting_source"
        elif page_type == "list":
            if target_count and len(data or []) < target_count and (has_pagination or has_load_more):
                stage = "collecting_more"
            elif goal_satisfied:
                stage = "completing"
            else:
                stage = "extracting"
        elif page_type == "detail":
            stage = "completing" if goal_satisfied else "extracting"
        elif page_type in {"form", "login"}:
            stage = "interacting"
        elif goal_satisfied:
            stage = "completing"
        elif item_count > 0:
            stage = "extracting"

        confidence = 0.45
        if page_type == "blocked":
            confidence = 0.92
        elif page_type in {"serp", "list", "detail", "form", "login", "modal"}:
            confidence = 0.8
        elif item_count > 0:
            confidence = 0.65

        return PageState(
            page_type=page_type, stage=stage, confidence=confidence,
            item_count=item_count, target_count=target_count,
            has_pagination=has_pagination, has_load_more=has_load_more,
            has_modal=has_modal, goal_satisfied=goal_satisfied,
        )

    def _choose_modal_action(
        self, task: str, elements: List[PageElement], snapshot: Optional[Dict[str, Any]] = None,
    ) -> Optional[BrowserAction]:
        active_snapshot = snapshot or self.last_semantic_snapshot or {}
        for ref_key, selector_key, description, confidence in (
            ("modal_primary_ref", "modal_primary_selector", "accept or continue modal", 0.82),
            ("modal_secondary_ref", "modal_secondary_selector", "dismiss modal secondary action", 0.78),
            ("modal_close_ref", "modal_close_selector", "close blocking modal", 0.76),
        ):
            action = self._build_snapshot_click_action(
                active_snapshot, ref_key=ref_key, selector_key=selector_key,
                description=description, confidence=confidence,
            )
            if action is not None:
                return action
        modal_elements = [
            element for element in elements
            if element.region == "modal" and element.is_visible and element.is_clickable
        ]
        if not modal_elements:
            return None
        candidate = self._find_best_element(
            task, modal_elements, kinds=["button", "submit", "link"],
            keywords=[
                "同意", "接受", "允许", "继续", "确定", "好的", "知道了",
                "accept", "agree", "allow", "continue", "ok", "okay", "got it",
                "关闭", "取消", "稍后", "拒绝", "跳过",
                "close", "dismiss", "cancel", "not now", "later", "skip", "decline", "×",
            ],
        )
        if candidate is None:
            return None
        return BrowserAction(
            action_type=ActionType.CLICK, target_selector=candidate.selector,
            target_ref=candidate.ref, description="dismiss blocking modal", confidence=0.74,
        )

    def _choose_snapshot_navigation_action(
        self, task: str, current_url: str, elements: List[PageElement],
        intent: Optional[TaskIntent], data: List[Dict[str, str]],
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> Optional[BrowserAction]:
        active_snapshot = snapshot or self.last_semantic_snapshot or {}
        page_state = self._infer_page_state(task, current_url, intent, data, active_snapshot, elements=elements)
        if page_state.goal_satisfied:
            if data:
                return BrowserAction(action_type=ActionType.EXTRACT, description="extract current structured content", confidence=0.84)
            main_text = self.perception.get_snapshot_main_text(active_snapshot) if self.perception else ""
            if main_text and len(main_text) >= 80:
                return BrowserAction(action_type=ActionType.EXTRACT, description="extract from page main text", confidence=0.80)

        if page_state.has_modal:
            modal_action = self._choose_modal_action(task, elements, active_snapshot)
            if modal_action is not None:
                return modal_action

        if page_state.page_type == "list":
            if data and (page_state.target_count == 0 or len(data) >= min(page_state.target_count or len(data), page_state.item_count or len(data))):
                return BrowserAction(action_type=ActionType.EXTRACT, description="extract visible list content", confidence=0.78)
            if page_state.target_count and len(data) < page_state.target_count:
                load_more_action = self._build_snapshot_click_action(
                    active_snapshot, ref_key="load_more_ref", selector_key="load_more_selector",
                    description="load more list items", confidence=0.74,
                )
                if load_more_action is not None:
                    return load_more_action
                next_page_action = self._build_snapshot_click_action(
                    active_snapshot, ref_key="next_page_ref", selector_key="next_page_selector",
                    description="open next results page", confidence=0.71,
                )
                if next_page_action is not None:
                    return next_page_action
                return BrowserAction(action_type=ActionType.SCROLL, value="900", description="scroll for lazy-loaded list items", confidence=0.52)
            if data:
                return BrowserAction(action_type=ActionType.EXTRACT, description="extract current list page", confidence=0.7)

        if page_state.page_type in {"list", "unknown", "detail"} and not data:
            query = (intent or TaskIntent(intent_type="navigate", query=self._derive_primary_query(task))).query
            navigation_keywords = self._extract_query_tokens(query or task)[:5]
            nav_candidate = self._find_best_element(
                task, elements, kinds=["button", "submit", "link"], keywords=navigation_keywords,
            )
            if nav_candidate and self._score_element_for_context(task, nav_candidate) >= 2.0:
                return BrowserAction(
                    action_type=ActionType.CLICK, target_selector=nav_candidate.selector,
                    target_ref=nav_candidate.ref,
                    description=f"open relevant page section {nav_candidate.text[:24]}".strip(),
                    confidence=0.63,
                )

        if page_state.page_type == "detail" and data:
            return BrowserAction(action_type=ActionType.EXTRACT, description="extract detail page content", confidence=0.76)

        return None

    def _should_assess_page_with_llm(
        self, task: str, current_url: str, intent: Optional[TaskIntent],
        data: List[Dict[str, str]], elements: List[PageElement],
        last_action: Optional[BrowserAction] = None,
    ) -> bool:
        if not data and not elements:
            return False
        active_intent = intent or TaskIntent(intent_type="read", query="", confidence=0.0)
        if active_intent.intent_type in {"form", "auth"} and elements:
            return True
        if data and self._orchestrator and self._orchestrator._is_read_only_task(task, active_intent):
            return True
        if self._is_search_engine_url(current_url):
            query = active_intent.query or self._derive_primary_query(task)
            return bool(data) or self._search_input_matches_query(elements, query)
        if data and (self._data_has_substantive_text(data) or len(data) >= 2):
            return True
        if last_action and last_action.action_type in {ActionType.INPUT, ActionType.CLICK} and data:
            return True
        return False

    def _page_assessment_cache_key(
        self, task: str, current_url: str, title: str,
        intent: Optional[TaskIntent], data: List[Dict[str, str]],
        elements: List[PageElement], last_action: Optional[BrowserAction] = None,
        recent_steps: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        active_intent = intent or TaskIntent(intent_type="read", query="", confidence=0.0)
        active_snapshot = self.last_semantic_snapshot or {}
        cards = self.perception.cards_from_snapshot(active_snapshot) if self.perception else []
        collections = self.perception.collections_from_snapshot(active_snapshot) if self.perception else []
        payload = {
            "task": self._normalize_text(task)[:240],
            "url": current_url[:220],
            "title": title[:120],
            "page_type": str(active_snapshot.get("page_type", "") or ""),
            "page_stage": self._infer_page_state(task, current_url, active_intent, data, active_snapshot).stage,
            "intent": active_intent.intent_type,
            "query": active_intent.query[:160],
            "fields": self._format_intent_fields_for_llm(active_intent.fields),
            "last_action": self._action_signature(last_action) if last_action else "",
            "recent_steps": [
                {
                    "step": step.get("step"), "action_type": step.get("action_type"),
                    "selector": str(step.get("selector") or step.get("action") or "")[:80],
                    "result": str(step.get("result") or "")[:16],
                    "url": str(step.get("url") or "")[:120],
                }
                for step in (recent_steps or [])[-3:]
            ],
            "data": [
                {
                    "title": str(item.get("title", "") or "")[:80],
                    "text": str(item.get("text", "") or "")[:140],
                    "link": str(item.get("link", item.get("url", "")) or "")[:120],
                }
                for item in (data or [])[:8] if isinstance(item, dict)
            ],
            "elements": [
                {
                    "index": element.index, "type": element.element_type,
                    "selector": element.selector[:80], "text": element.text[:80],
                    "href": str((element.attributes or {}).get("href", "") or "")[:120],
                    "value": str((element.attributes or {}).get("value", "") or "")[:80],
                }
                for element in (elements or [])[:10]
            ],
            "cards": [
                {"ref": card.target_ref or card.ref, "title": card.title[:100], "source": card.source[:40], "host": card.host[:40]}
                for card in cards[:6]
            ],
            "collections": collections[:4],
        }
        return hashlib.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    # ── Phase 3c: core decision methods ───────────────────────────

    def _choose_observation_driven_action(
        self, task: str, current_url: str, elements: List[PageElement],
        intent: Optional[TaskIntent], data: List[Dict[str, str]],
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> Optional[BrowserAction]:
        active_intent = intent or TaskIntent(intent_type="read", query="", confidence=0.0)
        query = active_intent.query or self._derive_primary_query(task)

        if data and self._page_data_satisfies_goal(task, current_url, active_intent, data, snapshot=snapshot):
            return BrowserAction(action_type=ActionType.EXTRACT, description="use current page results", confidence=0.82)

        snapshot_action = self._choose_snapshot_navigation_action(task, current_url, elements, active_intent, data, snapshot=snapshot)
        if snapshot_action is not None and snapshot_action.action_type != ActionType.EXTRACT:
            return snapshot_action

        if not self._is_search_engine_url(current_url):
            return snapshot_action

        if self._search_input_matches_query(elements, query):
            click_action = self._find_search_result_click_action(task, current_url, elements, active_intent, snapshot=snapshot)
            if click_action is not None:
                return click_action
            if data and self._search_results_have_answer_evidence(query, data):
                return BrowserAction(action_type=ActionType.EXTRACT, description="extract visible search results", confidence=0.68)
            submit_control = self._find_primary_submit_control(elements)
            if submit_control is not None:
                return BrowserAction(
                    action_type=ActionType.CLICK, target_selector=submit_control.selector,
                    description="submit current search query", confidence=0.45,
                    use_keyboard_fallback=True, keyboard_key="Enter",
                )
            return BrowserAction(action_type=ActionType.PRESS_KEY, value="Enter", description="submit current search query", confidence=0.35)

        return snapshot_action

    def _decide_action_locally(
        self, task: str, elements: List[PageElement], intent: Optional[TaskIntent] = None,
    ) -> Optional[BrowserAction]:
        active_intent = intent or TaskIntent(
            intent_type="read", query=self._derive_primary_query(task), confidence=0.0,
            fields=self._orchestrator._extract_structured_pairs(task) if self._orchestrator else {},
            requires_interaction=False,
        )

        click_target = active_intent.target_text or (self._orchestrator._extract_click_target_text(task) if self._orchestrator else "")
        if click_target:
            explicit_target = self._find_best_element(task, elements, kinds=["button", "submit", "link"], keywords=[click_target])
            if explicit_target:
                attrs = explicit_target.attributes or {}
                haystack = " ".join([
                    explicit_target.text, attrs.get("placeholder", ""),
                    attrs.get("ariaLabel", ""), attrs.get("labelText", ""),
                ]).lower()
                target_lower = click_target.lower()
                target_tokens = self._extract_task_tokens(target_lower)
                if not target_tokens and any("\u4e00" <= ch <= "\u9fff" for ch in target_lower):
                    target_tokens = [target_lower]
                matched_tokens = sum(1 for t in target_tokens if t in haystack)
                if target_tokens and matched_tokens >= max(1, len(target_tokens) // 2):
                    return BrowserAction(
                        action_type=ActionType.CLICK, target_selector=explicit_target.selector,
                        description=f"click target {click_target}", confidence=0.82,
                    )

        if active_intent.intent_type in {"form", "auth"} and self._orchestrator:
            mapping = self._orchestrator._build_form_mapping_from_pairs(active_intent.fields, elements)
            if mapping:
                if self._orchestrator._mapping_matches_current_elements(mapping, elements):
                    submit_control = self._orchestrator._find_submit_control_for_intent(task, elements, active_intent)
                    if submit_control:
                        return BrowserAction(
                            action_type=ActionType.CLICK, target_selector=submit_control.selector,
                            target_ref=submit_control.ref, description="submit interactive form", confidence=0.78,
                        )
                else:
                    return self._orchestrator._build_form_fill_action(mapping)
            if active_intent.intent_type == "auth" and self._iter_input_candidates(elements):
                submit_control = self._orchestrator._find_submit_control_for_intent(task, elements, active_intent)
                if submit_control and not active_intent.fields:
                    return None
            submit_control = self._orchestrator._find_submit_control_for_intent(task, elements, active_intent)
            if submit_control:
                return BrowserAction(
                    action_type=ActionType.CLICK, target_selector=submit_control.selector,
                    target_ref=submit_control.ref, description="submit interactive form", confidence=0.62,
                )

        if active_intent.intent_type == "search":
            query = active_intent.query or self._derive_primary_query(task)
            search_el = self._find_search_element(elements)
            if search_el and query:
                if search_el.element_type in {"input", "text", "search", "textarea"} or search_el.tag in {"input", "textarea"}:
                    return BrowserAction(
                        action_type=ActionType.INPUT, target_selector=search_el.selector,
                        value=query, description="fill search query", confidence=0.9,
                        use_keyboard_fallback=True, keyboard_key="Enter",
                    )
                else:
                    return BrowserAction(
                        action_type=ActionType.CLICK, target_selector=search_el.selector,
                        description=f"open search to find {query}", confidence=0.85,
                    )
            input_element = self._find_primary_text_input(elements)
            if input_element and query:
                return BrowserAction(
                    action_type=ActionType.INPUT, target_selector=input_element.selector,
                    value=query, description="fill search query", confidence=0.9,
                    use_keyboard_fallback=True, keyboard_key="Enter",
                )
            submit_control = self._find_primary_submit_control(elements)
            if submit_control:
                return BrowserAction(
                    action_type=ActionType.CLICK, target_selector=submit_control.selector,
                    description="continue search flow", confidence=0.55,
                    use_keyboard_fallback=True, keyboard_key="Enter",
                )
            return BrowserAction(action_type=ActionType.EXTRACT, description="extract visible search results", confidence=0.4)

        if active_intent.intent_type in {"read", "navigate", "unknown"} and self._orchestrator and self._orchestrator._is_read_only_task(task, active_intent):
            return BrowserAction(action_type=ActionType.EXTRACT, description="extract visible page content", confidence=0.45)

        return None

    async def _assess_page_with_llm(
        self, task: str, current_url: str, title: str,
        elements: List[PageElement], intent: Optional[TaskIntent],
        data: List[Dict[str, str]], last_action: Optional[BrowserAction] = None,
        recent_steps: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[BrowserAction]:
        if not self._should_assess_page_with_llm(task, current_url, intent, data, elements, last_action):
            return None

        snapshot_getter = getattr(self.perception, 'get_semantic_snapshot', None) if self.perception else None
        if not self.last_semantic_snapshot and snapshot_getter:
            await snapshot_getter()
            # Re-sync after async snapshot fetch
            if self._orchestrator:
                self._orchestrator._sync_perception_state()
                self.last_semantic_snapshot = self._orchestrator._last_semantic_snapshot

        cache_key = self._page_assessment_cache_key(task, current_url, title, intent, data, elements, last_action, recent_steps)
        if cache_key in self._page_assessment_cache:
            web_debug_recorder.record_event("browser_page_assessment_cache_hit", cache_key=cache_key, url=current_url)
            return self._clone_action(self._page_assessment_cache[cache_key])

        try:
            active_intent = intent or TaskIntent(intent_type="read", query="", confidence=0.0)
            snapshot = self.last_semantic_snapshot or {}
            if not snapshot and snapshot_getter:
                snapshot = await snapshot_getter()
            cards = self.perception.cards_from_snapshot(snapshot) if self.perception else []
            page_state = self._infer_page_state(task, current_url, active_intent, data, snapshot)
            elements_text = self._format_assessment_elements_for_llm(task, current_url, elements)
            prompt_context, prompt_budget = await self._build_budgeted_browser_prompt_context(
                task=task, current_url=current_url, data=data, cards=cards,
                snapshot=snapshot, elements_text=elements_text,
                total_tokens=settings.PAGE_ASSESSMENT_CONTEXT_TOKENS,
            )
            llm = self._get_llm()
            data_collected = len(data or [])
            target_match = re.search(r'(\d+)\s*(?:个|条|款|项|条数据|items?|results?)', task or "")
            if target_match:
                data_target = int(target_match.group(1))
                data_progress = f"Data progress: collected {data_collected} / target {data_target}"
                if data_collected >= data_target:
                    data_progress += " (ENOUGH - consider using DONE or EXTRACT)"
            else:
                data_progress = f"Data collected: {data_collected} items"
                if data_collected > 0:
                    data_progress += " — judge whether the task goal is already satisfied based on the task description and current page content, not an arbitrary count."
            prompt = PAGE_ASSESSMENT_PROMPT.format(
                task=task or "", intent=active_intent.intent_type,
                query=active_intent.query or self._derive_primary_query(task),
                fields=self._format_intent_fields_for_llm(active_intent.fields),
                url=current_url or "", title=title or "",
                page_type=page_state.page_type, page_stage=page_state.stage,
                last_action=(last_action.description or last_action.action_type.value) if last_action else "none",
                recent_steps=self._format_recent_steps_for_llm(recent_steps),
                data_progress=data_progress,
                repeated_actions=self.format_repeated_actions_for_llm(),
                context_coverage=prompt_context.get("context_coverage", ""),
                data=prompt_context.get("data", "(no visible data)"),
                cards=prompt_context.get("cards", "(no cards)"),
                collections=prompt_context.get("collections", "(no collections)"),
                controls=prompt_context.get("controls", "(no controls)"),
                elements=prompt_context.get("elements", "(no actionable elements)"),
                task_specific_rules=_get_task_specific_rules(active_intent.intent_type),
                stage_hint=_get_stage_hint(page_state.stage),
                reflection=self._pending_reflection,
            )
            if web_debug_recorder.is_enabled():
                page_html = await self.perception.get_page_html() if self.perception else ""
                web_debug_recorder.write_text("browser_page_html", page_html, suffix=".html")
                html_preview = page_html[:1000] if page_html else "(empty)"
                log_warning(f"[DEBUG] 页面HTML (前1000字符): {html_preview}...")
                log_warning(f"[DEBUG] 页面HTML总长度: {len(page_html)} 字符")
            web_debug_recorder.write_json("browser_page_assessment_context", {
                "task": task, "url": current_url, "title": title,
                "intent": {
                    "intent_type": active_intent.intent_type, "query": active_intent.query,
                    "confidence": active_intent.confidence, "fields": active_intent.fields,
                    "requires_interaction": active_intent.requires_interaction,
                    "target_text": active_intent.target_text,
                },
                "page_state": {
                    "page_type": page_state.page_type, "stage": page_state.stage,
                    "confidence": page_state.confidence, "item_count": page_state.item_count,
                    "target_count": page_state.target_count, "has_pagination": page_state.has_pagination,
                    "has_load_more": page_state.has_load_more, "has_modal": page_state.has_modal,
                    "goal_satisfied": page_state.goal_satisfied,
                },
                "data": data, "cards": [card.__dict__ for card in cards],
                "elements": self._elements_to_debug_payload(elements),
                "snapshot": snapshot,
                "last_action": self._action_to_debug_payload(last_action),
                "recent_steps": (recent_steps or [])[-4:],
                "prompt_budget": prompt_budget,
            })
            web_debug_recorder.write_text("browser_page_assessment_prompt", prompt)
            web_debug_recorder.write_json("browser_page_assessment_budget", prompt_budget)
            if web_debug_recorder.is_enabled():
                log_warning(f"[DEBUG] 页面评估 Prompt (前800字符): {prompt[:800]}...")
                log_warning(f"[DEBUG] 页面评估 Prompt总长度: {len(prompt)} 字符")
                log_warning(f"[DEBUG] 元素数量: {len(elements)}, 数据条数: {len(data)}")

            response = await llm.achat(
                messages=[{"role": "system", "content": "Return JSON only."}, {"role": "user", "content": prompt}],
                temperature=0.1, json_mode=True,
            )
            web_debug_recorder.write_text("browser_page_assessment_response", self._stringify_llm_response(response))
            payload = llm.parse_json_response(response)
            web_debug_recorder.write_json("browser_page_assessment_payload", payload)
            if web_debug_recorder.is_enabled():
                log_warning(f"[DEBUG] 页面评估 LLM 响应: {self._stringify_llm_response(response)[:500]}...")
                log_warning(f"[DEBUG] 页面评估 payload: {json.dumps(payload, ensure_ascii=False)[:500]}...")

            action = self._action_from_llm(payload, elements)
            web_debug_recorder.write_json("browser_page_assessment_action", self._action_to_debug_payload(action))
            if action.action_type == ActionType.FAILED:
                self._page_assessment_cache[cache_key] = None
                return None

            if (
                page_state.page_type == "list" and page_state.target_count
                and len(data or []) < page_state.target_count
                and (page_state.has_pagination or page_state.has_load_more)
            ):
                if action.action_type in {ActionType.EXTRACT, ActionType.DONE, ActionType.WAIT}:
                    state_action = self._choose_snapshot_navigation_action(task, current_url, elements, active_intent, data, snapshot)
                    if state_action is not None:
                        action = state_action

            query = active_intent.query or self._derive_primary_query(task)
            if action.action_type == ActionType.INPUT and self._search_input_matches_query(elements, action.value or query):
                submit_control = self._find_primary_submit_control(elements)
                if submit_control is not None:
                    action = BrowserAction(
                        action_type=ActionType.CLICK, target_selector=submit_control.selector,
                        description="submit assessed search query",
                        confidence=max(action.confidence, 0.45),
                        use_keyboard_fallback=True, keyboard_key="Enter",
                    )
                else:
                    action = BrowserAction(
                        action_type=ActionType.PRESS_KEY, value="Enter",
                        description="submit assessed search query",
                        confidence=max(action.confidence, 0.35),
                    )

            self._page_assessment_cache[cache_key] = self._clone_action(action)
            return self._clone_action(action)
        except Exception as exc:
            log_warning(f"LLM page assessment failed: {exc}")
            return None

    async def _decide_action_with_llm(
        self, task: str, elements: List[PageElement],
        intent: Optional[TaskIntent] = None, data: Optional[List[Dict[str, str]]] = None,
        snapshot: Optional[Dict[str, Any]] = None, current_url: str = "", title: str = "",
        last_action: Optional[BrowserAction] = None, recent_steps: Optional[List[Dict[str, Any]]] = None,
    ) -> BrowserAction:
        try:
            if not elements:
                if self._orchestrator and self._orchestrator._is_read_only_task(task, intent):
                    return BrowserAction(action_type=ActionType.EXTRACT, description="extract visible data")
                return BrowserAction(action_type=ActionType.WAIT, value="1", description="no actionable elements", confidence=0.05)

            page_title = title or ""
            if not page_title and self._orchestrator:
                page_title = await self._orchestrator._get_title_value()
            resolved_url = current_url or ""
            if not resolved_url and self._orchestrator:
                resolved_url = await self._orchestrator._get_current_url_value()

            active_intent = intent or TaskIntent(intent_type="read", query=self._derive_primary_query(task), confidence=0.0)
            current_data = list(data or [])
            if not current_data and self._orchestrator:
                current_data = await self._orchestrator._extract_data_for_intent(active_intent)
            data_collected = len(current_data) if current_data else 0
            target_match = re.search(r'(\d+)\s*(?:个|条|款|项|条数据|items?|results?)', task or "")
            if target_match:
                data_target = int(target_match.group(1))
                data_progress = f"Data progress: collected {data_collected} / target {data_target}"
                if data_collected >= data_target:
                    data_progress += " (ENOUGH - consider using DONE)"
            else:
                data_progress = f"Data collected: {data_collected} items"
                if data_collected > 0:
                    data_progress += " — judge whether the task goal is already satisfied based on the task description and current page content, not an arbitrary count."

            active_snapshot = snapshot or self.last_semantic_snapshot or {}
            if not active_snapshot and self.perception:
                active_snapshot = await self.perception.get_semantic_snapshot()
            cards = self.perception.cards_from_snapshot(active_snapshot) if self.perception else []
            page_state = self._infer_page_state(task, resolved_url, active_intent, current_data or [], active_snapshot)
            elements_text = self._format_assessment_elements_for_llm(task, resolved_url, elements)
            prompt_context, prompt_budget = await self._build_budgeted_browser_prompt_context(
                task=task, current_url=resolved_url, data=current_data or [],
                cards=cards, snapshot=active_snapshot, elements_text=elements_text,
                total_tokens=settings.ACTION_DECISION_CONTEXT_TOKENS,
            )

            obs = self.last_observation
            messages = [
                {"role": "system", "content": "Return JSON only."},
                {"role": "user", "content": ACTION_DECISION_PROMPT.format(
                    task=task, intent=active_intent.intent_type,
                    query=active_intent.query or self._derive_primary_query(task),
                    fields=self._format_intent_fields_for_llm(active_intent.fields),
                    requires_interaction=str(bool(active_intent.requires_interaction)).lower(),
                    url=resolved_url, title=page_title,
                    data_progress=data_progress,
                    page_type=page_state.page_type, page_stage=page_state.stage,
                    last_action=(last_action.description or last_action.action_type.value) if last_action else "none",
                    recent_steps=self._format_recent_steps_for_llm(recent_steps),
                    repeated_actions=self.format_repeated_actions_for_llm(),
                    context_coverage=prompt_context.get("context_coverage", ""),
                    data=prompt_context.get("data", "(no visible data)"),
                    cards=prompt_context.get("cards", "(no cards)"),
                    collections=prompt_context.get("collections", "(no collections)"),
                    controls=prompt_context.get("controls", "(no controls)"),
                    elements=prompt_context.get("elements", "(no actionable elements)"),
                    headings=self._format_headings_for_llm(active_snapshot),
                    regions=self._format_regions_for_llm(active_snapshot),
                    available_frames=self._format_available_frames_for_llm(active_snapshot),
                    available_tabs=self._format_available_tabs_for_llm(active_snapshot),
                    vision_description=getattr(obs, 'vision_description', '') if obs else '',
                    task_specific_rules=_get_task_specific_rules(active_intent.intent_type),
                    stage_hint=_get_stage_hint(page_state.stage),
                    reflection=self._pending_reflection,
                    site_hints=self._build_site_hints_block(resolved_url),
                )},
            ]
            web_debug_recorder.write_json("browser_action_decision_budget", prompt_budget)
            web_debug_recorder.write_text("browser_action_decision_prompt", messages[1]["content"])
            if web_debug_recorder.is_enabled():
                log_warning(f"[DEBUG] 动作决策 Prompt (前800字符): {messages[1]['content'][:800]}...")
                log_warning(f"[DEBUG] 动作决策 Prompt总长度: {len(messages[1]['content'])} 字符")

            llm = self._get_llm()
            response = await llm.achat(messages, temperature=0.1, json_mode=True)
            web_debug_recorder.write_text("browser_action_decision_response", self._stringify_llm_response(response))
            if web_debug_recorder.is_enabled():
                log_warning(f"[DEBUG] 动作决策 LLM 响应: {self._stringify_llm_response(response)[:500]}...")

            action = self._action_from_llm(llm.parse_json_response(response), elements)
            web_debug_recorder.write_json("browser_action_decision_action", self._action_to_debug_payload(action))
            if (
                page_state.page_type == "list" and page_state.target_count
                and data_collected < page_state.target_count
                and (page_state.has_pagination or page_state.has_load_more)
                and action.action_type in {ActionType.EXTRACT, ActionType.DONE, ActionType.WAIT}
            ):
                state_action = self._choose_snapshot_navigation_action(
                    task, resolved_url, elements, active_intent, current_data or [], active_snapshot,
                )
                if state_action is not None:
                    return state_action
            return action
        except Exception as exc:
            log_warning(f"LLM action fallback failed: {exc}")
            return BrowserAction(action_type=ActionType.WAIT, value="1", description="fallback wait", confidence=0.1)

    async def _unified_plan_action(
        self, task: str, current_url: str, title: str,
        elements: List[PageElement], intent: Optional[TaskIntent],
        data: List[Dict[str, str]], observation=None,
        last_action: Optional[BrowserAction] = None,
        recent_steps: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[BrowserAction]:
        obs = observation or self.last_observation
        if obs is None:
            return None

        active_snapshot = obs.snapshot or {}
        active_intent = intent or TaskIntent(intent_type="read", query=self._derive_primary_query(task), confidence=0.0)
        current_data = list(data or [])
        data_collected = len(current_data)
        target_match = re.search(r'(\d+)\s*(?:个|条|款|项|条数据|items?|results?)', task or "")
        if target_match:
            data_target = int(target_match.group(1))
            data_progress = f"Data progress: collected {data_collected} / target {data_target}"
            if data_collected >= data_target:
                data_progress += " (ENOUGH - consider using DONE)"
        else:
            data_progress = f"Data collected: {data_collected} items"
            if data_collected > 0:
                data_progress += " — judge whether the task goal is already satisfied based on the task description and current page content, not an arbitrary count."

        cards = self.perception.cards_from_snapshot(active_snapshot) if self.perception else []
        elements_text = self._format_assessment_elements_for_llm(task, current_url, elements)
        prompt_context, prompt_budget = await self._build_budgeted_browser_prompt_context(
            task=task, current_url=current_url, data=current_data,
            cards=cards, snapshot=active_snapshot, elements_text=elements_text,
            total_tokens=settings.ACTION_DECISION_CONTEXT_TOKENS,
        )

        text_blocks = self.perception.get_snapshot_visible_text_blocks(active_snapshot) if self.perception else []
        tb_limit = settings.TEXT_BLOCKS_DISPLAY_LIMIT
        tb_chars = settings.TEXT_BLOCK_DISPLAY_CHARS
        text_blocks_str = "\n".join(f"{i}. {t[:tb_chars]}" for i, t in enumerate(text_blocks[:tb_limit], 1)) if text_blocks else "(none)"
        main_text = self.perception.get_snapshot_main_text(active_snapshot) if self.perception else ""

        try:
            _unified_page_stage = str(active_snapshot.get("page_stage", "unknown") or "unknown")
            prompt = UNIFIED_PLAN_PROMPT.format(
                task=task, intent=active_intent.intent_type,
                query=active_intent.query or self._derive_primary_query(task),
                fields=self._format_intent_fields_for_llm(active_intent.fields),
                requires_interaction=str(bool(active_intent.requires_interaction)).lower(),
                url=current_url or "", title=title or "",
                page_type=active_snapshot.get("page_type", "unknown"),
                page_stage=_unified_page_stage,
                snapshot_version=obs.snapshot_version,
                headings=self._format_headings_for_llm(active_snapshot),
                regions=self._format_regions_for_llm(active_snapshot),
                main_text=main_text[:settings.MAIN_TEXT_LIMIT_DETAIL] if main_text else "(none)",
                visible_text_blocks=text_blocks_str,
                vision_description=obs.vision_description or "(not available)",
                cards=prompt_context.get("cards", "(no cards)"),
                collections=prompt_context.get("collections", "(no collections)"),
                controls=prompt_context.get("controls", "(no controls)"),
                elements=prompt_context.get("elements", "(no actionable elements)"),
                last_action=(last_action.description or last_action.action_type.value) if last_action else "none",
                recent_steps=self._format_recent_steps_for_llm(recent_steps),
                data_progress=data_progress,
                repeated_actions=self.format_repeated_actions_for_llm(),
                task_specific_rules=_get_task_specific_rules(active_intent.intent_type),
                stage_hint=_get_stage_hint(_unified_page_stage),
                reflection=self._pending_reflection,
            )
            web_debug_recorder.write_text("browser_unified_plan_prompt", prompt)

            llm = self._get_llm()
            response = await llm.achat(
                messages=[{"role": "system", "content": "Return JSON only."}, {"role": "user", "content": prompt}],
                temperature=0.1, json_mode=True,
            )
            web_debug_recorder.write_text("browser_unified_plan_response", self._stringify_llm_response(response))

            payload = llm.parse_json_response(response)
            action = self._action_from_llm(payload, elements)
            action = self.validate_action(action, obs)
            web_debug_recorder.write_json("browser_unified_plan_action", self._action_to_debug_payload(action))

            if action and action.action_type != ActionType.FAILED:
                return action
            return None
        except Exception as exc:
            log_warning(f"unified plan call failed: {exc}")
            return None

    # ── Reflection mechanism ───────────────────────────────────

    @staticmethod
    def _should_reflect(recent_steps: Optional[List[Dict[str, Any]]]) -> bool:
        """Check if recent consecutive failures warrant a reflection call."""
        if not settings.BROWSER_REFLECTION_ENABLED or not recent_steps:
            return False
        threshold = settings.BROWSER_REFLECTION_FAIL_THRESHOLD
        consecutive_fails = 0
        for step in reversed(recent_steps):
            if step.get("result") == "failed":
                consecutive_fails += 1
            else:
                break
        return consecutive_fails >= threshold

    async def _reflect_on_failures(
        self, task: str, current_url: str,
        recent_steps: Optional[List[Dict[str, Any]]],
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Analyze recent failures and return reflection text for LLM context."""
        if not REFLECTION_PROMPT or not recent_steps:
            return ""
        consecutive_fails = 0
        for step in reversed(recent_steps):
            if step.get("result") == "failed":
                consecutive_fails += 1
            else:
                break
        active_snapshot = snapshot or self.last_semantic_snapshot or {}
        page_type = str(active_snapshot.get("page_type", "unknown") or "unknown")
        try:
            prompt = REFLECTION_PROMPT.format(
                task=task or "",
                url=current_url or "",
                page_type=page_type,
                recent_steps=self._format_recent_steps_for_llm(recent_steps),
                fail_count=consecutive_fails,
            )
            llm = self._get_llm()
            response = await llm.achat(
                messages=[
                    {"role": "system", "content": "Return JSON only."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2, json_mode=True,
            )
            payload = llm.parse_json_response(response)
            if not payload:
                return ""
            parts = []
            if payload.get("root_cause"):
                parts.append(f"Root cause: {payload['root_cause']}")
            if payload.get("suggestion"):
                parts.append(f"Suggestion: {payload['suggestion']}")
            if payload.get("avoid"):
                parts.append(f"Avoid: {payload['avoid']}")
            reflection = "\n".join(parts)
            web_debug_recorder.write_text("browser_reflection", reflection)
            return reflection
        except Exception as exc:
            log_warning(f"reflection call failed: {exc}")
            return ""

    async def _act_with_llm(
        self, task: str, current_url: str, title: str,
        elements: List[PageElement], intent: Optional[TaskIntent],
        data: List[Dict[str, str]], observation=None,
        last_action: Optional[BrowserAction] = None,
        recent_steps: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[Optional[BrowserAction], bool]:
        """P2: Single-call consolidated LLM planner.

        Returns (action, goal_satisfied). Falls back to _unified_plan_action
        when the browser_act prompt is not present.
        """
        if not BROWSER_ACT_PROMPT:
            act = await self._unified_plan_action(
                task, current_url, title, elements, intent, data,
                observation=observation, last_action=last_action, recent_steps=recent_steps,
            )
            return act, False

        obs = observation or self.last_observation
        if obs is None:
            return None, False

        active_snapshot = obs.snapshot or {}
        active_intent = intent or TaskIntent(
            intent_type="read", query=self._derive_primary_query(task), confidence=0.0,
        )
        current_data = list(data or [])
        data_collected = len(current_data)
        target_match = re.search(r'(\d+)\s*(?:个|条|款|项|条数据|items?|results?)', task or "")
        if target_match:
            data_target = int(target_match.group(1))
            data_progress = f"Data progress: collected {data_collected} / target {data_target}"
            if data_collected >= data_target:
                data_progress += " (ENOUGH - consider using DONE)"
        else:
            data_progress = f"Data collected: {data_collected} items"

        cards = self.perception.cards_from_snapshot(active_snapshot) if self.perception else []
        elements_text = self._format_assessment_elements_for_llm(task, current_url, elements)
        prompt_context, _ = await self._build_budgeted_browser_prompt_context(
            task=task, current_url=current_url, data=current_data,
            cards=cards, snapshot=active_snapshot, elements_text=elements_text,
            total_tokens=settings.ACTION_DECISION_CONTEXT_TOKENS,
        )
        text_blocks = self.perception.get_snapshot_visible_text_blocks(active_snapshot) if self.perception else []
        tb_limit = settings.TEXT_BLOCKS_DISPLAY_LIMIT
        tb_chars = settings.TEXT_BLOCK_DISPLAY_CHARS
        text_blocks_str = "\n".join(
            f"{i}. {t[:tb_chars]}" for i, t in enumerate(text_blocks[:tb_limit], 1)
        ) if text_blocks else "(none)"
        main_text = self.perception.get_snapshot_main_text(active_snapshot) if self.perception else ""

        plan_context = "(no active plan)"
        if self._task_plan is not None:
            try:
                plan_context = self._task_plan.format_for_prompt()
            except Exception:
                plan_context = "(plan unavailable)"

        try:
            page_stage = str(active_snapshot.get("page_stage", "unknown") or "unknown")
            prompt = BROWSER_ACT_PROMPT.format(
                task=task or "", intent=active_intent.intent_type,
                query=active_intent.query or self._derive_primary_query(task),
                fields=self._format_intent_fields_for_llm(active_intent.fields),
                requires_interaction=str(bool(active_intent.requires_interaction)).lower(),
                url=current_url or "", title=title or "",
                page_type=active_snapshot.get("page_type", "unknown"),
                page_stage=page_stage,
                snapshot_version=getattr(obs, "snapshot_version", ""),
                headings=self._format_headings_for_llm(active_snapshot),
                regions=self._format_regions_for_llm(active_snapshot),
                available_frames=self._format_available_frames_for_llm(active_snapshot),
                available_tabs=self._format_available_tabs_for_llm(active_snapshot),
                main_text=main_text[:settings.MAIN_TEXT_LIMIT_DETAIL] if main_text else "(none)",
                visible_text_blocks=text_blocks_str,
                vision_description=getattr(obs, "vision_description", "") or "(not available)",
                cards=prompt_context.get("cards", "(no cards)"),
                collections=prompt_context.get("collections", "(no collections)"),
                controls=prompt_context.get("controls", "(no controls)"),
                elements=prompt_context.get("elements", "(no actionable elements)"),
                last_action=(last_action.description or last_action.action_type.value) if last_action else "none",
                recent_steps=self._format_recent_steps_for_llm(recent_steps),
                data_progress=data_progress,
                repeated_actions=self.format_repeated_actions_for_llm(),
                plan_context=plan_context,
                task_specific_rules=_get_task_specific_rules(active_intent.intent_type),
                stage_hint=_get_stage_hint(page_stage),
                reflection=self._pending_reflection,
                site_hints=self._build_site_hints_block(current_url or ""),
            )
            web_debug_recorder.write_text("browser_act_prompt", prompt)
            llm = self._get_llm()
            response = await llm.achat(
                messages=[{"role": "system", "content": "Return JSON only."}, {"role": "user", "content": prompt}],
                temperature=0.1, json_mode=True,
            )
            web_debug_recorder.write_text("browser_act_response", self._stringify_llm_response(response))
            payload = llm.parse_json_response(response) or {}
            goal_satisfied = bool(payload.get("goal_satisfied", False))
            action = self._action_from_llm(payload, elements)
            action = self.validate_action(action, obs)
            web_debug_recorder.write_json("browser_act_action", self._action_to_debug_payload(action))
            if action and action.action_type != ActionType.FAILED:
                return action, goal_satisfied
            return None, goal_satisfied
        except Exception as exc:
            log_warning(f"browser_act call failed: {exc}")
            return None, False

    async def _plan_next_action(
        self, task: str, current_url: str, title: str,
        elements: List[PageElement], intent: Optional[TaskIntent],
        data: List[Dict[str, str]], snapshot: Optional[Dict[str, Any]] = None,
        last_action: Optional[BrowserAction] = None,
        recent_steps: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[Optional[BrowserAction], str]:
        active_snapshot = snapshot or self.last_semantic_snapshot or {}
        observation = self.last_observation

        # Reflection: analyze consecutive failures before planning next action
        if self._should_reflect(recent_steps):
            self._pending_reflection = await self._reflect_on_failures(
                task, current_url, recent_steps, snapshot=active_snapshot,
            )
            if self._pending_reflection:
                self._pending_reflection = f"Reflection on recent failures:\n{self._pending_reflection}"
        else:
            self._pending_reflection = ""

        async def _try_unified() -> Optional[BrowserAction]:
            if observation is None:
                return None
            act = await self._unified_plan_action(
                task, current_url, title, elements, intent, data,
                observation=observation, last_action=last_action, recent_steps=recent_steps,
            )
            if act is not None:
                act = self._sanitize_planned_action(
                    task, current_url, elements, intent, data, act,
                    snapshot=active_snapshot, recent_steps=recent_steps,
                )
            return act

        async def _try_assess() -> Optional[BrowserAction]:
            act = await self._assess_page_with_llm(
                task, current_url, title, elements, intent, data,
                last_action=last_action, recent_steps=recent_steps,
            )
            if act is not None:
                act = self._sanitize_planned_action(
                    task, current_url, elements, intent, data, act,
                    snapshot=active_snapshot, recent_steps=recent_steps,
                )
            return act

        if settings.BROWSER_UNIFIED_ACT_ENABLED and BROWSER_ACT_PROMPT:
            # P2: single-call consolidated decision. Fall back to rule-based
            # strategies below when the LLM proposes WAIT/FAILED.
            act_result, _goal_satisfied = await self._act_with_llm(
                task, current_url, title, elements, intent, data,
                observation=observation, last_action=last_action, recent_steps=recent_steps,
            )
            if act_result is not None:
                act_result = self._sanitize_planned_action(
                    task, current_url, elements, intent, data, act_result,
                    snapshot=active_snapshot, recent_steps=recent_steps,
                )
            if act_result is not None and act_result.action_type not in {ActionType.WAIT, ActionType.FAILED}:
                return act_result, "browser_act"
            unified_result = None
            assessed_result = act_result
        else:
            unified_result, assessed_result = await asyncio.gather(_try_unified(), _try_assess())
            if unified_result is not None and unified_result.action_type not in {ActionType.WAIT, ActionType.FAILED}:
                return unified_result, "unified_plan"
            if assessed_result is not None and assessed_result.action_type not in {ActionType.WAIT, ActionType.FAILED}:
                return assessed_result, "page_assessment_llm"

        if settings.BROWSER_UNIFIED_ACT_ENABLED and BROWSER_ACT_PROMPT:
            # P2: skip the third LLM call — go straight to rule-based fallbacks.
            llm_action = None
        else:
            llm_action = await self._decide_action_with_llm(
                task, elements, intent=intent, data=data, snapshot=active_snapshot,
                current_url=current_url, title=title, last_action=last_action, recent_steps=recent_steps,
            )
            llm_action = self._sanitize_planned_action(
                task, current_url, elements, intent, data, llm_action,
                snapshot=active_snapshot, recent_steps=recent_steps,
            )
            if llm_action is not None and llm_action.action_type not in {ActionType.WAIT, ActionType.FAILED}:
                return llm_action, "action_llm"

        observation_action = self._choose_observation_driven_action(task, current_url, elements, intent, data, snapshot=active_snapshot)
        observation_action = self._sanitize_planned_action(
            task, current_url, elements, intent, data, observation_action,
            snapshot=active_snapshot, recent_steps=recent_steps,
        )
        if observation_action is not None and observation_action.action_type not in {ActionType.WAIT, ActionType.FAILED}:
            return observation_action, "observation_fallback"

        search_result_action = self._find_search_result_click_action(task, current_url, elements, intent, snapshot=active_snapshot)
        search_result_action = self._sanitize_planned_action(
            task, current_url, elements, intent, data, search_result_action,
            snapshot=active_snapshot, recent_steps=recent_steps,
        )
        if search_result_action is not None and search_result_action.action_type not in {ActionType.WAIT, ActionType.FAILED}:
            return search_result_action, "search_result_fallback"

        local_action = self._decide_action_locally(task, elements, intent)
        local_action = self._sanitize_planned_action(
            task, current_url, elements, intent, data, local_action,
            snapshot=active_snapshot, recent_steps=recent_steps,
        )
        if local_action is not None and local_action.action_type not in {ActionType.WAIT, ActionType.FAILED}:
            return local_action, "local_fallback"

        if llm_action is not None:
            return llm_action, "action_llm_wait"
        if assessed_result is not None:
            return assessed_result, "page_assessment_wait"
        return None, "no_action"

    # ── Phase 4: Form handling & auth helpers ──────────────────

    def _normalize_auth_field_name(self, field_name: str) -> str:
        normalized = self._normalize_text(field_name)
        if not normalized:
            return ""
        if any(alias in normalized for alias in _AUTH_PASSWORD_ALIASES):
            return "password"
        if any(alias in normalized for alias in _AUTH_EMAIL_ALIASES):
            return "email"
        if any(alias in normalized for alias in _AUTH_USERNAME_ALIASES):
            return "username"
        return normalized

    def _clean_auth_candidate_value(self, field_name: str, value: str) -> str:
        cleaned = re.sub(r"\s+", " ", str(value or "")).strip(" \"'" + "\u201c\u201d\u2018\u2019")
        cleaned = cleaned.strip(".,;:!?)]}>")
        if not cleaned:
            return ""

        normalized = self._normalize_text(cleaned)
        if not normalized:
            return ""
        if "://" in cleaned or cleaned.startswith("//"):
            return ""
        if normalized in _AUTH_VALUE_NOISE_TOKENS:
            return ""
        if normalized in {
            *[self._normalize_text(item) for item in _AUTH_USERNAME_ALIASES],
            *[self._normalize_text(item) for item in _AUTH_EMAIL_ALIASES],
            *[self._normalize_text(item) for item in _AUTH_PASSWORD_ALIASES],
        }:
            return ""
        if field_name == "email" and "@" not in cleaned:
            return ""
        return cleaned

    def _extract_auth_fields_from_free_text(self, task: str) -> Dict[str, str]:
        text = self._strip_urls_from_text(task)
        clean_patterns = {
            "username": [
                r"(?:\u767b\u5f55\u8d26\u53f7|\u767b\u5f55\u540d|\u7528\u6237\u540d|\u8d26\u53f7|\u8d26\u6237|\u5e10\u53f7|username|user\s*name|login\s*name|login\s*account|account)\s*(?:is|=|:|\u662f|\u4e3a)?\s*[\"'\u201c\u201d\u2018\u2019]?([A-Za-z0-9_.@-]{2,})",
            ],
            "email": [
                r"(?:email|e-mail|mail|\u90ae\u7bb1|\u7535\u5b50\u90ae\u7bb1)\s*(?:is|=|:|\u662f|\u4e3a)?\s*[\"'\u201c\u201d\u2018\u2019]?([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})",
            ],
            "password": [
                r"(?:password|passcode|passwd|pwd|\u5bc6\u7801|\u767b\u5f55\u5bc6\u7801)\s*(?:is|=|:|\u662f|\u4e3a)?\s*[\"'\u201c\u201d\u2018\u2019]?([^\s,\uff0c\u3002\uff1b;]+)",
            ],
        }
        clean_extracted: Dict[str, str] = {}
        for field_name, regexes in clean_patterns.items():
            for pattern in regexes:
                candidates: List[str] = []
                for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                    value = self._clean_auth_candidate_value(field_name, str(match.group(1) or ""))
                    if value:
                        candidates.append(value)
                if candidates:
                    clean_extracted[field_name] = candidates[-1]
                    break
        if clean_extracted:
            return clean_extracted
        patterns = {
            "username": [
                r"(?:\u767b\u5f55\u8d26\u53f7|\u767b\u5f55\u540d|\u7528\u6237\u540d|\u8d26\u53f7|\u8d26\u6237|\u5e10\u53f7|username|user\s*name|login\s*name|login\s*account|account)\s*(?:is|=|:|\u662f|\u4e3a)\s*[\"'\u201c\u201d\u2018\u2019]?([^\s,\uff0c\u3002;\uff1b]+)",
                r"(?:\u767b\u5f55\u8d26\u53f7|\u767b\u5f55\u540d|\u7528\u6237\u540d|\u8d26\u53f7|\u8d26\u6237|\u5e10\u53f7|username|user\s*name|login\s*name|login\s*account|account)\s*[\"'\u201c\u201d\u2018\u2019]?([A-Za-z0-9_.@-]{2,})",
            ],
            "email": [
                r"(?:email|e-mail|mail|\u90ae\u7bb1|\u7535\u5b50\u90ae\u7bb1)\s*(?:is|=|:|\u662f|\u4e3a)\s*[\"'\u201c\u201d\u2018\u2019]?([^\s,\uff0c\u3002;\uff1b]+)",
                r"(?:email|e-mail|mail|\u90ae\u7bb1|\u7535\u5b50\u90ae\u7bb1)\s*[\"'\u201c\u201d\u2018\u2019]?([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})",
            ],
            "password": [
                r"(?:password|passcode|passwd|pwd|\u5bc6\u7801|\u767b\u5f55\u5bc6\u7801)\s*(?:is|=|:|\u662f|\u4e3a)\s*[\"'\u201c\u201d\u2018\u2019]?([^\s,\uff0c\u3002;\uff1b]+)",
                r"(?:password|passcode|passwd|pwd|\u5bc6\u7801|\u767b\u5f55\u5bc6\u7801)\s*[\"'\u201c\u201d\u2018\u2019]?([^\s,\uff0c\u3002;\uff1b]{2,})",
            ],
        }
        extracted: Dict[str, str] = {}
        for field_name, regexes in patterns.items():
            for pattern in regexes:
                candidates: List[str] = []
                for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                    value = self._clean_auth_candidate_value(field_name, str(match.group(1) or ""))
                    if value:
                        candidates.append(value)
                if candidates:
                    extracted[field_name] = candidates[-1]
                    break
        return extracted

    def _extract_structured_pairs(self, task: str) -> Dict[str, str]:
        pairs: Dict[str, str] = {}
        stripped_task = self._strip_urls_from_text(task)
        for key, value in re.findall(
            r"([A-Za-z0-9_\u4e00-\u9fff]{1,24})\s*[:：]\s*(.{1,160}?)(?=(?:\s+[A-Za-z0-9_\u4e00-\u9fff]{1,24}\s*[:：])|[\n,，;；]|$)",
            stripped_task,
        ):
            normalized_key = self._normalize_auth_field_name(key)
            cleaned_value = re.sub(r"\s+", " ", value).strip()
            if not normalized_key or normalized_key in _STRUCTURED_PAIR_SKIP_KEYS:
                continue
            if normalized_key.isdigit():
                continue
            if not cleaned_value or "://" in cleaned_value or cleaned_value.startswith("//"):
                continue
            if normalized_key and cleaned_value:
                pairs[normalized_key] = cleaned_value
        auth_pairs = self._extract_auth_fields_from_free_text(task)
        if auth_pairs:
            for key in list(pairs.keys()):
                if self._normalize_auth_field_name(key) in auth_pairs:
                    pairs.pop(key, None)
            pairs.update(auth_pairs)
        return pairs

    def _extract_click_target_text(self, task: str) -> str:
        clean_patterns = (
            r'"([^"\n]{2,64})"',
            r"'([^'\n]{2,64})'",
            r"\u201c([^\u201d\n]{2,64})\u201d",
            r"\u2018([^\u2019\n]{2,64})\u2019",
            r"\u300c([^\u300d\n]{2,64})\u300d",
            r"\u300e([^\u300f\n]{2,64})\u300f",
            r"\u300a([^\u300b\n]{2,64})\u300b",
        )
        for pattern in clean_patterns:
            match = re.search(pattern, task or "")
            if not match:
                continue
            value = self._normalize_text(match.group(1))
            if len(value) >= 2:
                return value
        for pattern in (
            r'"([^"\n]{2,64})"',
            r"'([^'\n]{2,64})'",
            r"\u201c([^\u201d\n]{2,64})\u201d",
            r"\u2018([^\u2019\n]{2,64})\u2019",
            r"\u300c([^\u300d\n]{2,64})\u300d",
            r"\u300e([^\u300f\n]{2,64})\u300f",
            r"\u300a([^\u300b\n]{2,64})\u300b",
        ):
            match = re.search(pattern, task or "")
            if not match:
                continue
            value = self._normalize_text(match.group(1))
            if len(value) >= 2:
                return value
        pairs = self._extract_structured_pairs(task)
        if len(pairs) == 1:
            value = self._normalize_text(next(iter(pairs.values())))
            if 2 <= len(value) <= 40:
                return value
        return ""

    def _build_form_fill_action(self, mapping: Dict[str, str]) -> BrowserAction:
        import json as _json
        return BrowserAction(
            action_type=ActionType.FILL_FORM,
            value=_json.dumps(mapping, ensure_ascii=False),
            description="fill form fields",
            confidence=0.9,
        )

    def _field_match_score(self, field_name: str, element: PageElement) -> float:
        attrs = element.attributes or {}
        element_type = self._normalize_text(
            attrs.get("type", "") or element.element_type or element.tag
        )
        haystack = self._normalize_text(
            " ".join(
                [
                    element.text,
                    attrs.get("name", ""),
                    attrs.get("id", ""),
                    attrs.get("placeholder", ""),
                    attrs.get("labelText", ""),
                    attrs.get("ariaLabel", ""),
                    attrs.get("title", ""),
                    attrs.get("type", ""),
                ]
            )
        )
        score = 0.0
        canonical_field = self._normalize_auth_field_name(field_name)
        for token in self._extract_task_tokens(field_name):
            if token and token in haystack:
                score += 2.0
        if element.element_type in {"text", "search", "email", "password", "textarea"}:
            score += 0.8
        if attrs.get("name"):
            score += 0.2
        if canonical_field == "password":
            if element_type == "password":
                score += 6.0
            else:
                score -= 3.0
            if any(alias in haystack for alias in _AUTH_PASSWORD_ALIASES):
                score += 4.0
        elif canonical_field in {"username", "email"}:
            if element_type == "password":
                score -= 5.0
            if canonical_field == "email" and element_type == "email":
                score += 4.0
            elif element_type in {"text", "search", "email", "input", "textarea"}:
                score += 2.5
            alias_pool = _AUTH_EMAIL_ALIASES if canonical_field == "email" else _AUTH_USERNAME_ALIASES + _AUTH_EMAIL_ALIASES
            if any(alias in haystack for alias in alias_pool):
                score += 4.0
        return score

    def _mapping_matches_current_elements(
        self,
        mapping: Dict[str, str],
        elements: List[PageElement],
    ) -> bool:
        if not mapping:
            return False
        element_by_selector = {
            element.selector: element
            for element in elements
            if element.selector
        }
        matched = 0
        for selector, expected in mapping.items():
            element = element_by_selector.get(selector)
            if element is None:
                continue
            current_value = str((element.attributes or {}).get("value", "") or "")
            if self._normalize_text(current_value) == self._normalize_text(str(expected)):
                matched += 1
        return matched >= max(1, len(mapping))

    def _build_form_mapping_from_pairs(
        self,
        fields: Dict[str, str],
        elements: List[PageElement],
    ) -> Dict[str, str]:
        if not fields:
            return {}

        available = self._iter_input_candidates(elements)
        if not available:
            return {}

        mapping: Dict[str, str] = {}
        remaining = list(available)

        for field_name, value in fields.items():
            if not remaining:
                break
            scored = sorted(
                ((self._field_match_score(field_name, item), item) for item in remaining),
                key=lambda pair: pair[0],
                reverse=True,
            )
            selected = scored[0][1] if scored else remaining[0]
            mapping[selected.selector] = value
            remaining = [item for item in remaining if item.selector != selected.selector]

        return mapping

    def _find_auth_submit_control(
        self,
        task: str,
        elements: List[PageElement],
        intent: Optional[TaskIntent] = None,
    ) -> Optional[PageElement]:
        controls = [
            item
            for item in elements
            if item.is_visible
            and item.is_clickable
            and (
                item.element_type in {"button", "submit", "link"}
                or item.tag in {"button", "a"}
                or self._normalize_text((item.attributes or {}).get("type", "")) in {"submit", "button"}
            )
        ]
        if not controls:
            return None

        target_hint = self._normalize_text((intent.target_text if intent else "") or "")
        ranked: List[Tuple[float, PageElement]] = []
        for control in controls:
            score = 0.0
            attrs = control.attributes or {}
            haystack = self._element_action_haystack(control)
            selector = str(control.selector or "")
            selector_depth = selector.count(">")
            if self._normalize_text(attrs.get("type", "")) == "submit":
                score += 2.5
            if any(token in haystack for token in _AUTH_SUBMIT_POSITIVE_TOKENS):
                score += 5.0
            if any(token in haystack for token in _AUTH_SUBMIT_NEGATIVE_TOKENS):
                score -= 6.0
            if any(token in haystack for token in _AUTH_SECONDARY_PROVIDER_TOKENS):
                score -= 4.0
            if target_hint and target_hint in haystack:
                score += 4.0
            if intent and intent.intent_type == "auth" and any(token in haystack for token in ("login", "\u767b\u5f55", "\u767b\u5165")):
                score += 2.0
            if selector:
                if "form > button" in selector:
                    score += 2.5
                score += max(0.0, 1.2 - 0.15 * selector_depth)
                score += max(0.0, 1.0 - 0.01 * len(selector))
            score += max(0.0, 1.0 - 0.1 * float(control.index))
            if score > 0:
                ranked.append((score, control))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return ranked[0][1] if ranked else None

    def _find_submit_control_for_intent(
        self,
        task: str,
        elements: List[PageElement],
        intent: Optional[TaskIntent] = None,
    ) -> Optional[PageElement]:
        active_intent = intent or TaskIntent(intent_type="form", query="", confidence=0.0)
        primary_submit = self._find_primary_submit_control(elements)
        if active_intent.intent_type != "auth":
            return primary_submit

        auth_submit = self._find_auth_submit_control(task, elements, active_intent)
        if primary_submit is None:
            return auth_submit
        if auth_submit is None:
            return primary_submit

        primary_haystack = self._element_action_haystack(primary_submit)
        auth_haystack = self._element_action_haystack(auth_submit)
        auth_is_secondary = any(token in auth_haystack for token in _AUTH_SECONDARY_PROVIDER_TOKENS)
        primary_is_secondary = any(token in primary_haystack for token in _AUTH_SECONDARY_PROVIDER_TOKENS)
        if auth_is_secondary and not primary_is_secondary:
            return primary_submit
        if primary_submit.tag == "button" and auth_submit.tag == "a":
            return primary_submit
        if primary_submit.index <= auth_submit.index:
            return primary_submit
        return auth_submit

    def _interaction_requires_follow_up(
        self,
        task: str,
        intent: Optional[TaskIntent],
        elements: List[PageElement],
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> bool:
        active_intent = intent or TaskIntent(intent_type="form", query="", confidence=0.0)
        if active_intent.intent_type not in {"form", "auth"}:
            return False

        active_snapshot = snapshot or {}
        page_type = str(active_snapshot.get("page_type", "") or "")
        page_stage = str(active_snapshot.get("page_stage", "") or "")
        if page_type in {"form", "login"} or page_stage == "interacting":
            return True

        if not elements:
            return False

        mapping = self._build_form_mapping_from_pairs(active_intent.fields, elements)
        if not mapping or not self._mapping_matches_current_elements(mapping, elements):
            return False

        submit_control = self._find_submit_control_for_intent(task, elements, active_intent)
        return submit_control is not None
