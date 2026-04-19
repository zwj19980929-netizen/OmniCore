"""Tail-hook helper that writes a reusable action template after a
successful BrowserAgent run (B1).

Kept out of ``browser_agent.py`` so all three execution modes (legacy
per-step, unified, batch) converge on a single, centralized recorder.
Invoked from the strategy orchestrator in ``browser_strategies/``.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from config.settings import settings
from utils.logger import log_warning


_TEMPLATE_INTENTS = frozenset({"search", "navigate", "form"})
# action_types worth preserving as template steps — dropped types are
# either LLM-internal (``wait``, ``extract``, ``done``, ``failed``) or
# too variable to replay (``scroll``).
_TEMPLATE_ACTION_TYPES = frozenset(
    {"click", "input", "navigate", "press_key", "select", "fill_form"}
)


def _domain_for(url: str) -> str:
    if not url:
        return ""
    text = url if "://" in url else f"http://{url}"
    try:
        host = urlparse(text).hostname or ""
    except ValueError:
        return ""
    return host.lower()


def _simplify_step(step: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Pull only the fields that are useful for later replay."""
    if not isinstance(step, dict):
        return None

    action_type = str(step.get("action_type") or "").strip().lower()
    if action_type not in _TEMPLATE_ACTION_TYPES:
        return None

    # Legacy loop marks failures with result=="failed" or success=False;
    # batch loop uses success=False. Skip anything that didn't succeed
    # so we only propagate known-good steps.
    result = str(step.get("result") or "").lower()
    success_flag = step.get("success")
    if result == "failed":
        return None
    if success_flag is False:
        return None

    selector = str(step.get("selector") or step.get("target_selector") or "").strip()
    target_ref = str(step.get("target_ref") or "").strip()
    if not selector and not target_ref and action_type != "navigate":
        return None

    simplified: Dict[str, Any] = {
        "action_type": action_type,
        "description": str(step.get("description") or "")[:120],
    }
    if selector:
        simplified["selector"] = selector
    if target_ref:
        simplified["target_ref"] = target_ref
    raw_value = step.get("value")
    if isinstance(raw_value, str) and raw_value:
        simplified["value"] = raw_value[:120]
    return simplified


def _pick_template_name(task_intent: Any) -> str:
    intent_type = str(getattr(task_intent, "intent_type", "") or "").strip().lower()
    if intent_type in _TEMPLATE_INTENTS:
        return intent_type
    return ""


def record_template_from_run(
    *,
    task_intent: Any,
    steps: List[Dict[str, Any]],
    final_url: str,
    success: bool,
) -> bool:
    """Persist a reusable template when the run succeeded.

    Returns ``True`` when a row was written, ``False`` otherwise. Errors
    and short-circuits are all silent so the tail hook never destabilizes
    the run path.
    """
    if not success or not settings.BROWSER_PLAN_MEMORY_ENABLED:
        return False

    template_name = _pick_template_name(task_intent)
    if not template_name:
        return False

    domain = _domain_for(final_url)
    if not domain:
        return False

    sequence = [simplified for simplified in (_simplify_step(s) for s in (steps or [])) if simplified]
    # A template with fewer than 2 steps is rarely worth replaying
    # (search/form need at least input + submit).
    if len(sequence) < 2:
        return False

    try:
        from utils.site_knowledge_store import get_site_knowledge_store
        store = get_site_knowledge_store()
    except Exception as exc:
        log_warning(f"template recorder: store import failed: {exc}")
        return False
    if store is None:
        return False

    try:
        return bool(store.record_template(domain, template_name, sequence))
    except Exception as exc:
        log_warning(f"record_template failed: {exc}")
        return False
