"""
上下文成本控制工具（R1 + S2）

R1 提供：
- truncate_tool_result: 工具返回结果截断（头 60% + 尾 30%）
- snip_history: 历史消息裁剪（保留最近 K 条完整，更早的截断到 200 字符）

S2 新增：
- ContextBudget: 全局 token 预算分配器（system_prompt / memory / history / tool_results）
- AutoCompactor: 自动压缩 + 熔断 + PTL fallback
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger("omnicore.context_budget")


def truncate_tool_result(text: str, max_chars: Optional[int] = None) -> str:
    """
    对工具返回的文本结果进行截断，保留头部和尾部关键内容。

    策略：保留头部 60% + 尾部 30%，中间插入截断标记。
    如果 text 不超过 max_chars，原样返回。

    Args:
        text: 原始文本
        max_chars: 最大字符数，默认读取 settings.TOOL_RESULT_MAX_CHARS

    Returns:
        截断后的文本（或原文，如未超限）
    """
    if not text:
        return text

    if max_chars is None:
        from config.settings import settings
        max_chars = settings.TOOL_RESULT_MAX_CHARS

    if len(text) <= max_chars:
        return text

    head_chars = int(max_chars * 0.6)
    tail_chars = int(max_chars * 0.3)
    omitted = len(text) - head_chars - tail_chars

    return (
        text[:head_chars]
        + f"\n[...truncated {omitted} chars...]\n"
        + text[-tail_chars:]
    )


# 工具结果中需要截断的文本字段名
_TEXT_RESULT_FIELDS = ("content", "output", "text", "extracted_text", "stdout", "stderr")


def truncate_result_dict(result: dict, max_chars: Optional[int] = None) -> dict:
    """
    对工具返回的 result dict 中的大文本字段应用截断。
    只截断 _TEXT_RESULT_FIELDS 中定义的字段，不影响 url、file_path 等元数据字段。

    Args:
        result: 工具返回的结果字典
        max_chars: 最大字符数，默认读取 settings.TOOL_RESULT_MAX_CHARS

    Returns:
        截断后的新 dict（浅拷贝，只修改了大文本字段）
    """
    if not isinstance(result, dict):
        return result

    modified = False
    new_result = dict(result)
    for field in _TEXT_RESULT_FIELDS:
        value = new_result.get(field)
        if isinstance(value, str):
            truncated = truncate_tool_result(value, max_chars)
            if truncated is not value:
                new_result[field] = truncated
                modified = True

    return new_result if modified else result


def snip_history(
    messages: list,
    max_messages: Optional[int] = None,
    keep_recent: Optional[int] = None,
    session_memory: str = "",
) -> list:
    """
    对消息历史进行裁剪：保留最近 keep_recent 条完整消息，更早的消息内容截断到 200 字符。

    只在消息总数超过 max_messages 时触发裁剪。
    裁剪后的消息保留原始 ID（确保 LangGraph add_messages reducer 正确更新）。

    如果提供了 session_memory，将其作为首条 SystemMessage 插入，确保
    LLM 始终能看到工作记忆摘要（R7）。

    Args:
        messages: LangChain 消息列表（HumanMessage / SystemMessage / AIMessage 等）
        max_messages: 触发裁剪的阈值，默认读取 settings.HISTORY_MAX_MESSAGES
        keep_recent: 保留完整内容的最近消息数，默认读取 settings.HISTORY_KEEP_RECENT
        session_memory: 如果非空，作为首条 SystemMessage 插入（R7）

    Returns:
        裁剪后的消息列表（长度不变，只有旧消息的 content 被截短）
    """
    if not messages:
        if session_memory:
            from langchain_core.messages import SystemMessage
            return [SystemMessage(content=f"[工作记忆]\n\n{session_memory}")]
        return messages

    if max_messages is None:
        from config.settings import settings
        max_messages = settings.HISTORY_MAX_MESSAGES
    if keep_recent is None:
        from config.settings import settings
        keep_recent = settings.HISTORY_KEEP_RECENT

    # Prefix: inject session memory as first message
    prefix: List = []
    if session_memory:
        from langchain_core.messages import SystemMessage
        prefix.append(SystemMessage(content=f"[工作记忆]\n\n{session_memory}"))

    if len(messages) <= max_messages:
        return (prefix + list(messages)) if prefix else messages

    # 需要截断的索引范围：所有早于 keep_recent 的消息
    cutoff = len(messages) - keep_recent
    result: List = []

    for i, msg in enumerate(messages):
        if i >= cutoff:
            # 最近 keep_recent 条：保持完整
            result.append(msg)
            continue

        # 早期消息：截断 content 到 200 字符
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        if len(content) <= 200:
            result.append(msg)
            continue

        truncated = content[:200] + "...(snipped)"
        # 保留原始 ID 以确保 LangGraph add_messages deduplication 正确工作
        msg_id = getattr(msg, "id", None)
        try:
            if msg_id is not None:
                new_msg = type(msg)(content=truncated, id=msg_id)
            else:
                new_msg = type(msg)(content=truncated)
        except Exception:
            # Fallback：无法构造新消息时保留原始
            new_msg = msg
        result.append(new_msg)

    return prefix + result


# ===========================================================================
# S2: Token 预算分配器
# ===========================================================================

def _count_tokens(text: str) -> int:
    """Count tokens — reuse prompt_registry's implementation."""
    from core.prompt_registry import _count_tokens as _pt
    return _pt(text)


def _count_message_tokens(messages: list) -> int:
    """Estimate total tokens across a list of LangChain messages."""
    total = 0
    for msg in messages:
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        total += _count_tokens(content)
    return total


# Slot names used by ContextBudget
SLOT_SYSTEM_PROMPT = "system_prompt"
SLOT_MEMORY = "memory"
SLOT_HISTORY = "history"
SLOT_TOOL_RESULTS = "tool_results"

# Default allocation ratios (must sum to 1.0)
DEFAULT_SLOT_RATIOS: Dict[str, float] = {
    SLOT_SYSTEM_PROMPT: 0.15,
    SLOT_MEMORY: 0.10,
    SLOT_HISTORY: 0.55,
    SLOT_TOOL_RESULTS: 0.20,
}


@dataclass
class SlotUsage:
    """Token usage for a single budget slot."""
    budget: int = 0
    used: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.budget - self.used)

    @property
    def utilization(self) -> float:
        return self.used / self.budget if self.budget > 0 else 0.0


class ContextBudget:
    """
    Global token budget allocator (S2-1).

    Reads the model's context window from models.yaml, reserves tokens for
    auto-compact, and distributes the remainder across four slots.
    """

    def __init__(
        self,
        total_tokens: int = 0,
        reserve_tokens: Optional[int] = None,
        slot_ratios: Optional[Dict[str, float]] = None,
    ):
        """
        Args:
            total_tokens: Model context window size. 0 = auto-detect from settings.
            reserve_tokens: Tokens reserved for compact operation.
            slot_ratios: Custom allocation ratios per slot.
        """
        from config.settings import settings

        if total_tokens <= 0:
            total_tokens = self._detect_context_window()
        self.total_tokens = total_tokens

        if reserve_tokens is None:
            reserve_tokens = getattr(settings, "CONTEXT_RESERVE_TOKENS", 20000)
        self.reserve_tokens = reserve_tokens

        self.effective_tokens = max(0, self.total_tokens - self.reserve_tokens)
        self._ratios = slot_ratios or dict(DEFAULT_SLOT_RATIOS)
        self._slots: Dict[str, SlotUsage] = {}
        self._allocate()

    # ---- allocation ----

    def _allocate(self) -> None:
        """Distribute effective_tokens across slots by ratio."""
        self._slots = {}
        for slot_name, ratio in self._ratios.items():
            self._slots[slot_name] = SlotUsage(
                budget=int(self.effective_tokens * ratio),
            )

    def get_slot_budget(self, slot: str) -> int:
        """Return the token budget for a slot."""
        s = self._slots.get(slot)
        return s.budget if s else 0

    # ---- usage tracking ----

    def update_usage(self, slot: str, tokens: int) -> None:
        """Record current token usage for a slot."""
        if slot in self._slots:
            self._slots[slot].used = tokens

    def check_usage(self) -> Dict[str, dict]:
        """Return per-slot usage report."""
        report: Dict[str, dict] = {}
        for name, s in self._slots.items():
            report[name] = {
                "budget": s.budget,
                "used": s.used,
                "remaining": s.remaining,
                "utilization": round(s.utilization, 3),
            }
        total_used = sum(s.used for s in self._slots.values())
        report["_total"] = {
            "effective_tokens": self.effective_tokens,
            "total_used": total_used,
            "total_remaining": max(0, self.effective_tokens - total_used),
            "utilization": round(total_used / self.effective_tokens, 3) if self.effective_tokens > 0 else 0.0,
        }
        return report

    def total_used(self) -> int:
        return sum(s.used for s in self._slots.values())

    # ---- compact trigger ----

    def should_compact(self, threshold: Optional[float] = None) -> bool:
        """Return True when total usage exceeds the compact threshold."""
        if threshold is None:
            from config.settings import settings
            threshold = getattr(settings, "CONTEXT_COMPACT_THRESHOLD", 0.85)
        if self.effective_tokens <= 0:
            return False
        return (self.total_used() / self.effective_tokens) >= threshold

    # ---- helpers ----

    @staticmethod
    def _detect_context_window() -> int:
        """Best-effort detection of the active model's context window."""
        try:
            from config.settings import settings
            import yaml
            config_path = settings.MODELS_CONFIG_PATH
            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
            # Parse DEFAULT_MODEL (e.g. "deepseek/deepseek-chat")
            model_str = settings.DEFAULT_MODEL
            parts = model_str.split("/", 1)
            provider = parts[0] if len(parts) == 2 else ""
            model_id = parts[1] if len(parts) == 2 else parts[0]
            overrides = config.get("capability_overrides", {})
            model_info = overrides.get(provider, {}).get(model_id, {})
            ctx = model_info.get("max_tokens")
            if ctx and ctx > 0:
                return int(ctx)
        except Exception:
            pass
        # fallback: conservative 64k
        return 64000

    def log_report(self) -> None:
        """Log current budget usage at debug level."""
        report = self.check_usage()
        lines = ["Context Budget Report:"]
        for name in [SLOT_SYSTEM_PROMPT, SLOT_MEMORY, SLOT_HISTORY, SLOT_TOOL_RESULTS]:
            r = report.get(name, {})
            lines.append(
                f"  {name:20s}  {r.get('used', 0):6d} / {r.get('budget', 0):6d} "
                f"({r.get('utilization', 0):.1%})"
            )
        t = report.get("_total", {})
        lines.append(
            f"  {'TOTAL':20s}  {t.get('total_used', 0):6d} / {t.get('effective_tokens', 0):6d} "
            f"({t.get('utilization', 0):.1%})"
        )
        logger.debug("\n".join(lines))


# ===========================================================================
# S2-2: AutoCompactor — compact + circuit breaker + PTL fallback
# ===========================================================================

class AutoCompactor:
    """
    Manages automatic context compaction with circuit-breaker protection.

    Compaction strategy:
    1. Call a low-cost LLM to summarize history messages
    2. Replace old messages with the summary
    3. On failure, apply PTL (Progressive Truncation Ladder) fallback

    Circuit breaker: after ``max_consecutive_failures`` consecutive compact
    failures, stop attempting and log a warning.
    """

    def __init__(self, max_consecutive_failures: Optional[int] = None):
        from config.settings import settings
        if max_consecutive_failures is None:
            max_consecutive_failures = getattr(
                settings, "COMPACT_MAX_CONSECUTIVE_FAILURES", 3
            )
        self.max_consecutive_failures = max_consecutive_failures
        self.consecutive_failures = 0
        self._tripped = False

    @property
    def is_tripped(self) -> bool:
        """True if the circuit breaker has tripped."""
        return self._tripped

    def compact(
        self,
        messages: list,
        *,
        budget: Optional[ContextBudget] = None,
        reinject_state: Optional[dict] = None,
    ) -> list:
        """
        Attempt to compact ``messages`` via LLM summarization.

        Args:
            messages: LangChain message list (will not be mutated).
            budget: Optional ContextBudget for logging.
            reinject_state: If provided, passed to ``build_reinject_context``
                after successful compaction.

        Returns:
            Compacted message list.
        """
        if self._tripped:
            logger.warning("AutoCompactor circuit breaker tripped — skipping compact")
            return messages

        if len(messages) <= 4:
            # Too few messages to compact meaningfully
            return messages

        try:
            compacted = self._do_llm_compact(messages)
            self.consecutive_failures = 0

            # S2-3: reinject context after successful compact
            if reinject_state is not None:
                from core.context_reinject import build_reinject_messages
                reinject_msgs = build_reinject_messages(reinject_state)
                if reinject_msgs:
                    compacted = reinject_msgs + compacted

            if budget:
                budget.log_report()

            logger.info(
                f"Compact succeeded: {len(messages)} → {len(compacted)} messages"
            )
            return compacted

        except Exception as exc:
            self.consecutive_failures += 1
            logger.warning(
                f"Compact failed ({self.consecutive_failures}/{self.max_consecutive_failures}): {exc}"
            )
            if self.consecutive_failures >= self.max_consecutive_failures:
                self._tripped = True
                logger.warning("AutoCompactor circuit breaker TRIPPED — no more compact attempts")

            # PTL fallback
            return self._ptl_fallback(messages)

    def _do_llm_compact(self, messages: list) -> list:
        """Summarize older messages using a low-cost LLM."""
        from langchain_core.messages import SystemMessage

        # Keep the most recent 6 messages intact, summarize the rest
        keep_recent = min(6, len(messages))
        old_messages = messages[:-keep_recent] if keep_recent < len(messages) else []
        recent_messages = messages[-keep_recent:]

        if not old_messages:
            return messages

        # Build summary request
        history_text_parts = []
        for msg in old_messages:
            role = type(msg).__name__.replace("Message", "")
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            # Truncate each message to avoid blowing up the compact call itself
            if len(content) > 800:
                content = content[:600] + "...(truncated)..." + content[-150:]
            history_text_parts.append(f"[{role}] {content}")
        history_text = "\n".join(history_text_parts)

        from core.llm import LLMClient
        llm = LLMClient(complexity=0.2)
        response = llm.chat_with_system(
            system_prompt=(
                "You are a conversation compressor. Summarize the following conversation history "
                "into a concise summary that preserves: key decisions, action outcomes, important "
                "facts, and current task state. Output a single paragraph in the same language as "
                "the conversation. Be concise but complete."
            ),
            user_message=history_text,
            temperature=0.1,
        )

        summary = response.content.strip() if response and response.content else ""
        if not summary:
            raise ValueError("LLM returned empty compact summary")

        summary_msg = SystemMessage(
            content=f"[上下文压缩摘要]\n\n{summary}"
        )
        return [summary_msg] + list(recent_messages)

    def _ptl_fallback(self, messages: list) -> list:
        """
        Progressive Truncation Ladder (PTL):
        1. Strip long tool result content from messages
        2. If still too many, truncate the oldest 20%
        """
        result = list(messages)

        # Level 1: strip large content from older messages (not recent 6)
        keep_recent = min(6, len(result))
        cutoff = len(result) - keep_recent

        for i in range(cutoff):
            msg = result[i]
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            if len(content) > 500:
                truncated = content[:300] + "\n[...PTL truncated...]\n" + content[-100:]
                msg_id = getattr(msg, "id", None)
                try:
                    if msg_id is not None:
                        result[i] = type(msg)(content=truncated, id=msg_id)
                    else:
                        result[i] = type(msg)(content=truncated)
                except Exception:
                    pass

        # Level 2: drop oldest 20% if message count is still high
        if len(result) > 20:
            drop_count = max(1, len(result) // 5)
            from langchain_core.messages import SystemMessage
            dropped_summary = SystemMessage(
                content=f"[PTL: {drop_count} earliest messages dropped to fit context window]"
            )
            result = [dropped_summary] + result[drop_count:]

        logger.info(f"PTL fallback applied: {len(messages)} → {len(result)} messages")
        return result

    def reset(self) -> None:
        """Reset the circuit breaker."""
        self.consecutive_failures = 0
        self._tripped = False
