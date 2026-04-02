"""
上下文成本控制工具（R1）

提供两个核心函数：
- truncate_tool_result: 工具返回结果截断（头 60% + 尾 30%）
- snip_history: 历史消息裁剪（保留最近 K 条完整，更早的截断到 200 字符）
"""
from __future__ import annotations

from typing import List, Optional


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
