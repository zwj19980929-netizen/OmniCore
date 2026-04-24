"""Browser Action Sequence — 一次 LLM 调用生成完整动作序列，批量执行。

替代逐步决策模式：感知页面 → 一次 LLM → 动作序列 → 批量执行 → 视觉验证 → 纠偏。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from config.settings import settings
from utils.logger import log_agent_action, log_warning
from utils.prompt_manager import get_prompt


SEQUENCE_PROMPT = get_prompt("browser_action_sequence", "") or ""
VISUAL_VERIFY_PROMPT = get_prompt("browser_visual_verify", "") or ""
CORRECTION_PROMPT = get_prompt("browser_correction", "") or ""


@dataclass
class DomCheckpoint:
    check_type: str = "none"
    target_ref: str = ""
    target_selector: str = ""
    expected_value: str = ""
    text_contains: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.check_type,
            "target_ref": self.target_ref,
            "target_selector": self.target_selector,
            "expected_value": self.expected_value,
            "text_contains": self.text_contains,
        }


@dataclass
class SequenceAction:
    action_type: str
    target_ref: str = ""
    target_selector: str = ""
    value: str = ""
    description: str = ""
    keyboard_key: str = ""
    dom_checkpoint: DomCheckpoint = field(default_factory=DomCheckpoint)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.action_type,
            "target_ref": self.target_ref,
            "target_selector": self.target_selector,
            "value": self.value,
            "description": self.description,
            "keyboard_key": self.keyboard_key,
            "dom_checkpoint": self.dom_checkpoint.to_dict(),
        }


@dataclass
class ActionSequence:
    actions: List[SequenceAction] = field(default_factory=list)
    goal_description: str = ""
    expected_outcome: str = ""
    execution_index: int = 0

    def current_action(self) -> Optional[SequenceAction]:
        if 0 <= self.execution_index < len(self.actions):
            return self.actions[self.execution_index]
        return None

    def advance(self) -> Optional[SequenceAction]:
        self.execution_index += 1
        return self.current_action()

    def remaining(self) -> List[SequenceAction]:
        return self.actions[self.execution_index:]

    def executed(self) -> List[SequenceAction]:
        return self.actions[:self.execution_index]

    def is_complete(self) -> bool:
        return self.execution_index >= len(self.actions)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "goal_description": self.goal_description,
            "expected_outcome": self.expected_outcome,
            "execution_index": self.execution_index,
            "actions": [a.to_dict() for a in self.actions],
        }

    def format_executed_for_prompt(self) -> str:
        lines: List[str] = []
        for i, a in enumerate(self.executed()):
            lines.append(f"  [{i}] {a.action_type}: {a.description}")
        return "\n".join(lines) if lines else "(none)"

    def format_remaining_for_prompt(self) -> str:
        lines: List[str] = []
        for a in self.remaining():
            lines.append(f"  - {a.action_type}: {a.description}")
        return "\n".join(lines) if lines else "(none)"


# ── Parsing helpers ──────────────────────────────────────────


def _parse_dom_checkpoint(raw: Any) -> DomCheckpoint:
    if not isinstance(raw, dict):
        return DomCheckpoint()
    return DomCheckpoint(
        check_type=str(raw.get("type", "none")).strip() or "none",
        target_ref=str(raw.get("target_ref", "")).strip(),
        target_selector=str(raw.get("target_selector", "")).strip(),
        expected_value=str(raw.get("expected_value", "")).strip(),
        text_contains=str(raw.get("text_contains", "")).strip(),
    )


def _parse_actions(raw_actions: Any, max_actions: int) -> List[SequenceAction]:
    actions: List[SequenceAction] = []
    if not isinstance(raw_actions, list):
        return actions
    for item in raw_actions[:max_actions]:
        if not isinstance(item, dict):
            continue
        action_type = str(item.get("type", "")).strip().lower()
        if not action_type:
            continue
        actions.append(SequenceAction(
            action_type=action_type,
            target_ref=str(item.get("target_ref", "")).strip(),
            target_selector=str(item.get("target_selector", "")).strip(),
            value=str(item.get("value", "")).strip(),
            description=str(item.get("description", "")).strip()[:200],
            keyboard_key=str(item.get("keyboard_key", "")).strip(),
            dom_checkpoint=_parse_dom_checkpoint(item.get("dom_checkpoint")),
        ))
    return actions


# ── LLM-driven generation ───────────────────────────────────


async def generate_action_sequence(
    task: str,
    page_context: str,
    elements_text: str,
    llm,
    repeated_actions: str = "",
    plan_context: str = "",
    vision_only_controls: str = "",
) -> Optional[ActionSequence]:
    if not SEQUENCE_PROMPT:
        return None
    max_actions = max(1, settings.BROWSER_MAX_SEQUENCE_ACTIONS)
    try:
        prompt = SEQUENCE_PROMPT.format(
            task=task or "",
            page_context=page_context or "",
            elements=elements_text or "",
            vision_only_controls=vision_only_controls or "",
            max_actions=max_actions,
            repeated_actions=repeated_actions or "(none)",
            plan_context=plan_context or "(no plan)",
        )
        response = await llm.achat(
            messages=[
                {"role": "system", "content": "Return JSON only."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            json_mode=True,
        )
        payload = llm.parse_json_response(response) or {}
        actions = _parse_actions(payload.get("actions", []), max_actions)
        if not actions:
            return None
        return ActionSequence(
            actions=actions,
            goal_description=str(payload.get("goal_description", "")).strip()[:300],
            expected_outcome=str(payload.get("expected_outcome", "")).strip()[:300],
        )
    except Exception as exc:
        log_warning(f"generate_action_sequence failed: {exc}")
        return None


# ── Visual verification ──────────────────────────────────────


@dataclass
class VerifyResult:
    goal_achieved: bool = False
    deviation: str = "none"
    detail: str = ""


async def visual_verify(
    task: str,
    expected_outcome: str,
    executed_actions_summary: str,
    current_page_context: str,
    vision_description: str,
    llm,
) -> VerifyResult:
    if not VISUAL_VERIFY_PROMPT:
        return VerifyResult(goal_achieved=True, deviation="none", detail="no verify prompt")
    try:
        prompt = VISUAL_VERIFY_PROMPT.format(
            task=task or "",
            expected_outcome=expected_outcome or "",
            executed_actions=executed_actions_summary or "",
            page_context=current_page_context or "",
            vision_description=vision_description or "(not available)",
        )
        response = await llm.achat(
            messages=[
                {"role": "system", "content": "Return JSON only."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            json_mode=True,
        )
        payload = llm.parse_json_response(response) or {}
        return VerifyResult(
            goal_achieved=bool(payload.get("goal_achieved", False)),
            deviation=str(payload.get("deviation", "major")).strip(),
            detail=str(payload.get("detail", "")).strip()[:500],
        )
    except Exception as exc:
        log_warning(f"visual_verify failed: {exc}")
        return VerifyResult(goal_achieved=False, deviation="major", detail=str(exc))


# ── Correction planning ──────────────────────────────────────


async def plan_correction(
    task: str,
    original_sequence_summary: str,
    failure_detail: str,
    page_context: str,
    elements_text: str,
    llm,
    repeated_actions: str = "",
) -> Optional[ActionSequence]:
    if not CORRECTION_PROMPT:
        return None
    max_actions = max(1, settings.BROWSER_MAX_SEQUENCE_ACTIONS)
    try:
        prompt = CORRECTION_PROMPT.format(
            task=task or "",
            original_actions=original_sequence_summary or "",
            failure_detail=failure_detail or "",
            page_context=page_context or "",
            elements=elements_text or "",
            max_actions=max_actions,
            repeated_actions=repeated_actions or "(none)",
        )
        response = await llm.achat(
            messages=[
                {"role": "system", "content": "Return JSON only."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            json_mode=True,
        )
        payload = llm.parse_json_response(response) or {}
        actions = _parse_actions(payload.get("actions", []), max_actions)
        if not actions:
            return None
        return ActionSequence(
            actions=actions,
            goal_description=str(payload.get("goal_description", "")).strip()[:300],
            expected_outcome=str(payload.get("expected_outcome", "")).strip()[:300],
        )
    except Exception as exc:
        log_warning(f"plan_correction failed: {exc}")
        return None
