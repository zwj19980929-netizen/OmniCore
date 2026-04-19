"""Login flow replay (B1).

Replays a previously-recorded login sequence stored by
``SiteKnowledgeStore.record_login_flow`` without involving the LLM. Each
step is validated with a DOM checkpoint when the flow carries one; any
unverified step aborts the replay and the caller falls back to the
normal LLM-driven path.

The module is a thin, agent-agnostic helper: it only talks to the agent
through ``_execute_action`` (for side-effects) and to the toolkit's
``_page`` (for checkpoint DOM queries), so it stays decoupled from the
3-layer perception/decision/execution stack.

Disabled when ``BROWSER_PLAN_MEMORY_ENABLED=false`` or
``BROWSER_LOGIN_REPLAY_ENABLED=false``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from config.settings import settings
from utils.logger import log_agent_action, log_warning


@dataclass
class LoginReplayResult:
    """Outcome of a ``try_replay_login`` attempt.

    - ``skipped`` — feature disabled, no stored flow, or flow unusable.
    - ``success`` — every step executed and every checkpoint passed.
    - ``executed_steps`` — the ordered list of steps that were attempted
      (so the caller can persist them back via ``record_template`` /
      ``record_login_flow``).
    """

    success: bool = False
    skipped: bool = False
    reason: str = ""
    executed_steps: List[Dict[str, Any]] = field(default_factory=list)


_ACTION_CHECKPOINT_FIELDS = ("expected_checkpoint", "checkpoint", "dom_checkpoint")


def _coerce_action_type(raw: Any):
    """Map a string or ActionType-like value back to an ``ActionType`` enum."""
    from agents.browser_agent import ActionType

    if isinstance(raw, ActionType):
        return raw
    text = str(raw or "").strip().lower()
    if not text:
        return None
    try:
        return ActionType(text)
    except ValueError:
        return None


def _build_browser_action(step: Dict[str, Any]):
    """Materialize a ``BrowserAction`` from a stored flow step.

    Stored steps are schema-loose: they may carry ``selector`` /
    ``target_selector`` / ``target_ref`` / ``value``. Fall back
    gracefully so older records remain replayable.
    """
    from agents.browser_agent import BrowserAction

    action_type = _coerce_action_type(step.get("action_type") or step.get("type"))
    if action_type is None:
        return None
    selector = str(step.get("target_selector") or step.get("selector") or "").strip()
    target_ref = str(step.get("target_ref") or step.get("ref") or "").strip()
    value = str(step.get("value") or "")
    description = str(step.get("description") or f"login_replay: {action_type.value}")
    keyboard_key = str(step.get("keyboard_key") or "")
    return BrowserAction(
        action_type=action_type,
        target_selector=selector,
        target_ref=target_ref,
        value=value,
        description=description,
        confidence=0.9,
        keyboard_key=keyboard_key,
    )


def _extract_checkpoint(step: Dict[str, Any]):
    """Return a simple checkpoint namespace understood by ``verify_dom_checkpoint``."""
    raw = None
    for key in _ACTION_CHECKPOINT_FIELDS:
        if key in step and step[key]:
            raw = step[key]
            break
    if not raw:
        return None

    # Accept both dict and pre-built objects
    if hasattr(raw, "check_type"):
        return raw

    if not isinstance(raw, dict):
        return None

    from types import SimpleNamespace

    return SimpleNamespace(
        check_type=str(raw.get("check_type") or raw.get("type") or "none"),
        target_selector=str(raw.get("target_selector") or raw.get("selector") or ""),
        target_ref=str(raw.get("target_ref") or raw.get("ref") or ""),
        expected_value=str(raw.get("expected_value") or raw.get("value") or ""),
        text_contains=str(raw.get("text_contains") or ""),
    )


async def try_replay_login(
    agent,
    domain: str,
    *,
    credentials: Optional[Dict[str, str]] = None,
    max_fail_count: int = 3,
) -> LoginReplayResult:
    """Attempt to replay a stored login flow for ``domain``.

    Parameters
    ----------
    agent : BrowserAgent-like
        Must expose ``_execute_action`` and ``toolkit`` (with
        ``._page``). No other contract is required so the helper stays
        unit-testable against a light stub.
    domain : str
        Registered domain (or URL — it will be normalized).
    credentials : dict, optional
        ``{"username": ..., "password": ...}``. When provided,
        placeholder-style steps (``value`` in
        ``{"{{username}}", "{{password}}"}``) are substituted. When
        absent, stored values are replayed verbatim.
    max_fail_count : int
        Flows whose stored ``fail_count`` exceeds this are skipped.

    Returns
    -------
    LoginReplayResult
    """
    if not settings.BROWSER_PLAN_MEMORY_ENABLED or not settings.BROWSER_LOGIN_REPLAY_ENABLED:
        return LoginReplayResult(skipped=True, reason="login replay disabled")

    domain = (domain or "").strip()
    if not domain:
        return LoginReplayResult(skipped=True, reason="empty domain")

    try:
        from utils.site_knowledge_store import get_site_knowledge_store
        store = get_site_knowledge_store()
    except Exception as exc:
        return LoginReplayResult(skipped=True, reason=f"store unavailable: {exc}")
    if store is None:
        return LoginReplayResult(skipped=True, reason="store disabled")

    try:
        record = store.get_login_flow(domain)
    except Exception as exc:
        log_warning(f"get_login_flow failed during replay: {exc}")
        return LoginReplayResult(skipped=True, reason="get_login_flow raised")

    if not record or not record.get("flow"):
        return LoginReplayResult(skipped=True, reason="no stored flow")
    if int(record.get("fail_count", 0) or 0) >= max_fail_count:
        return LoginReplayResult(
            skipped=True,
            reason=f"flow marked unstable (fail_count={record.get('fail_count')})",
        )

    flow = record["flow"]
    if not isinstance(flow, list) or not flow:
        return LoginReplayResult(skipped=True, reason="flow malformed")

    creds = {k: str(v) for k, v in (credentials or {}).items()}
    page = getattr(getattr(agent, "toolkit", None), "_page", None)

    try:
        from utils.dom_checkpoint import verify_dom_checkpoint
    except Exception as exc:
        return LoginReplayResult(skipped=True, reason=f"checkpoint module unavailable: {exc}")

    executed: List[Dict[str, Any]] = []
    for idx, raw_step in enumerate(flow):
        if not isinstance(raw_step, dict):
            _record_failure(store, domain, flow, reason=f"step {idx} not a dict")
            return LoginReplayResult(
                success=False,
                reason=f"step {idx} has invalid shape",
                executed_steps=executed,
            )

        step = _substitute_placeholders(raw_step, creds)
        action = _build_browser_action(step)
        if action is None:
            _record_failure(store, domain, flow, reason=f"step {idx} unmapped action")
            return LoginReplayResult(
                success=False,
                reason=f"step {idx} has unknown action_type",
                executed_steps=executed,
            )

        try:
            ok = await agent._execute_action(action)
        except Exception as exc:
            _record_failure(store, domain, flow, reason=f"step {idx} raised: {exc}")
            return LoginReplayResult(
                success=False,
                reason=f"step {idx} raised {exc}",
                executed_steps=executed,
            )

        step_record = {
            "index": idx,
            "action_type": action.action_type.value,
            "target_selector": action.target_selector,
            "target_ref": action.target_ref,
            "success": bool(ok),
        }
        executed.append(step_record)

        if not ok:
            _record_failure(store, domain, flow, reason=f"step {idx} execution failed")
            return LoginReplayResult(
                success=False,
                reason=f"step {idx} execute returned False",
                executed_steps=executed,
            )

        checkpoint = _extract_checkpoint(step)
        if checkpoint is not None and page is not None:
            cp = await verify_dom_checkpoint(page, checkpoint)
            step_record["checkpoint_passed"] = cp.passed
            step_record["checkpoint_detail"] = cp.detail
            if not cp.passed:
                _record_failure(
                    store,
                    domain,
                    flow,
                    reason=f"step {idx} checkpoint failed: {cp.detail}",
                )
                return LoginReplayResult(
                    success=False,
                    reason=f"step {idx} checkpoint failed: {cp.detail}",
                    executed_steps=executed,
                )

    # All steps passed — refresh success timestamp, preserve original flow
    try:
        store.record_login_flow(domain, flow=flow, success=True)
    except Exception as exc:
        log_warning(f"record_login_flow(success=True) failed: {exc}")

    log_agent_action(
        getattr(agent, "name", "BrowserAgent"),
        "login replay 成功",
        f"domain={domain}, steps={len(executed)}",
    )
    return LoginReplayResult(success=True, executed_steps=executed)


def _record_failure(store, domain: str, flow: Any, *, reason: str) -> None:
    """Best-effort ``record_login_flow(success=False)`` + warn-log."""
    log_warning(f"login replay failed for {domain}: {reason}")
    try:
        store.record_login_flow(domain, flow=flow, success=False)
    except Exception as exc:
        log_warning(f"record_login_flow(success=False) failed: {exc}")


def _substitute_placeholders(step: Dict[str, Any], creds: Dict[str, str]) -> Dict[str, Any]:
    """Replace ``{{username}}`` / ``{{password}}`` in step values.

    Only the ``value`` field is templated — selectors are opaque strings
    that the site owns. When ``creds`` is empty or placeholders are
    absent the step is returned untouched.
    """
    if not creds:
        return step
    value = step.get("value")
    if not isinstance(value, str) or "{{" not in value:
        return step
    substituted = value
    for key, replacement in creds.items():
        substituted = substituted.replace("{{" + key + "}}", replacement)
    if substituted == value:
        return step
    new_step = dict(step)
    new_step["value"] = substituted
    return new_step
